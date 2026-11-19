"""Flask 应用工厂与路由。"""
from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import config, services, verification
from .db import get_db, init_db, recover_interrupted_batches


def _posted_requirements(request) -> list[dict]:
    """回填校验失败时的表单行，避免用户重填。"""
    rows = []
    for uid, minimum in zip(
        request.form.getlist("sop_class_uid"),
        request.form.getlist("minimum_instances"),
    ):
        rows.append({"sop_class_uid": uid, "minimum": minimum})
    return rows


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

    init_db()
    recover_interrupted_batches()

    @app.get("/")
    def index():
        return render_template("index.html", batches=services.list_batches())

    @app.post("/upload")
    def upload():
        files = request.files.getlist("dicom_files")
        if not files or all(not f.filename for f in files):
            flash("请至少选择一个 DICOM 文件", "error")
            return redirect(url_for("index"))
        try:
            batch_id, saved = services.save_uploads(files)
        except Exception as exc:  # noqa: BLE001
            flash(f"上传暂存失败: {exc}", "error")
            return redirect(url_for("index"))
        try:
            services.run_batch(batch_id, saved)
        except services.BatchError as exc:
            flash(str(exc), "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("批次 %s 处理异常", batch_id)
            flash(f"处理失败: {exc}", "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        flash(f"批次 {batch_id} 处理完成", "ok")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.get("/batches/<batch_id>")
    def batch_detail(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        conn = get_db()
        try:
            files = conn.execute(
                "SELECT * FROM batch_files WHERE batch_id=? ORDER BY orig_name",
                (batch_id,),
            ).fetchall()
        finally:
            conn.close()
        return render_template("batch.html", batch=batch, files=files)

    @app.get("/batches/<batch_id>/download")
    def download(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        if batch["status"] != "ready" or not batch["zip_path"]:
            # 半成品绝不提供下载
            abort(409, description="批次尚未处理成功，无可下载文件包")
        zip_path = Path(batch["zip_path"])
        if not zip_path.exists():
            abort(404, description="文件包缺失")
        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"batch_{batch_id}.zip",
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ------------------------------------------------------------ 完整性核查
    @app.get("/verify")
    def verify_page():
        return render_template(
            "verify.html",
            studies=verification.list_study_options(),
            requirements=[],
            selected_study="",
            verifications=verification.list_verifications(limit=20),
            issue_labels=verification.ISSUE_LABELS,
        )

    @app.post("/verify")
    def verify_submit():
        try:
            study_uid, patient_id = verification.parse_study_pair(
                request.form.get("study_pair", "")
            )
            requirements = verification.parse_requirements(
                request.form.getlist("sop_class_uid"),
                request.form.getlist("minimum_instances"),
            )
            result = verification.run_verification(
                study_uid, patient_id, requirements
            )
        except verification.VerificationError as exc:
            flash(str(exc), "error")
            return render_template(
                "verify.html",
                studies=verification.list_study_options(),
                requirements=_posted_requirements(request),
                selected_study=request.form.get("study_pair", ""),
                verifications=verification.list_verifications(limit=20),
                issue_labels=verification.ISSUE_LABELS,
            ), 400

        verification_id = verification.save_verification(result)
        flash(
            "核查完成："
            + ("影像完整，可用于研究" if result["status"] == "usable"
               else "影像不完整，不可用"),
            "ok" if result["status"] == "usable" else "error",
        )
        return redirect(url_for("verify_result", verification_id=verification_id))

    @app.get("/verify/<verification_id>")
    def verify_result(verification_id: str):
        result = verification.get_verification(verification_id)
        if result is None:
            abort(404)
        return render_template(
            "verification_detail.html",
            result=result,
            grouped=verification.issues_by_category(result["issues"]),
            issue_labels=verification.ISSUE_LABELS,
            stale=verification.is_stale(result),
        )

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传内容超过服务端大小限制", "error")
        return redirect(url_for("index"))

    return app
