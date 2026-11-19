"""Study 影像协议完整性核查。

设计要点：
- 只统计**当前成功批次**（``batches.status = 'ready'``）中的实例；失败/处理中
  批次即使残留 ``batch_files`` 行也不参与；
- 实例按原 ``SOPInstanceUID`` 去重，同一实例出现在多个成功批次中只计一次；
- 实例必须同时满足“匿名 StudyInstanceUID == 所选 Study”与
  “匿名 PatientID == 所选 PatientID”，即成对一致；同 Study 下其他 PatientID
  的实例单独列为跨患者问题，且不计入任何类别数量；
- 逐条读取对象存储中的处理结果：文件不存在记为“文件缺失”，解析失败或解析
  身份与登记不一致记为“解析失败”；
- 核查结果连同当时的**发布快照**（成功批次清单、批次 zip 大小、实例集合摘要）
  一次性写入 ``study_verifications``；之后补传只改变当前可用批次，历史核查
  记录不回溯更新，必须重新核查才会生成反映新快照的结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pydicom

from . import config
from .db import get_db, immediate_tx

_UID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_MAX_UID_LEN = 64
_MAX_MINIMUM = 1_000_000

# 问题类别（页面与测试共用的稳定键）
ISSUE_MISSING_CLASS = "missing_class"
ISSUE_INSUFFICIENT = "insufficient"
ISSUE_CROSS_PATIENT = "cross_patient"
ISSUE_MISSING_FILE = "missing_file"
ISSUE_PARSE_FAILED = "parse_failed"

ISSUE_LABELS = {
    ISSUE_MISSING_CLASS: "缺少类别",
    ISSUE_INSUFFICIENT: "数量不足",
    ISSUE_CROSS_PATIENT: "跨患者",
    ISSUE_MISSING_FILE: "文件缺失",
    ISSUE_PARSE_FAILED: "解析失败",
}


class VerificationError(ValueError):
    """核查请求本身非法（未选 Study、SOP Class UID/数量格式错误等）。"""


@dataclass(frozen=True)
class Requirement:
    sop_class_uid: str
    minimum: int

    def as_dict(self) -> dict:
        return {"sop_class_uid": self.sop_class_uid, "minimum": self.minimum}


# ---------------------------------------------------------------- 输入校验
def validate_sop_class_uid(value: str) -> str:
    uid = (value or "").strip()
    if not uid:
        raise VerificationError("SOP Class UID 不能为空")
    if len(uid) > _MAX_UID_LEN:
        raise VerificationError(f"SOP Class UID 超过 {_MAX_UID_LEN} 字符: {uid}")
    if not _UID_RE.match(uid):
        raise VerificationError(f"SOP Class UID 格式非法（只能含数字与点）: {uid}")
    if any(part == "" or (len(part) > 1 and part[0] == "0") for part in uid.split(".")):
        raise VerificationError(f"SOP Class UID 含空段或前导零段: {uid}")
    return uid


def parse_requirements(
    sop_class_uids: list[str], minimums: list[str | int]
) -> list[Requirement]:
    """校验并合并重复 SOP Class 行；同类取最大最少实例数，避免重复计数/重复要求。"""
    if len(sop_class_uids) != len(minimums):
        raise VerificationError("SOP Class UID 与最少实例数行数不一致")
    merged: dict[str, int] = {}
    for raw_uid, raw_min in zip(sop_class_uids, minimums):
        uid = validate_sop_class_uid(raw_uid)
        try:
            minimum = int(str(raw_min).strip())
        except (TypeError, ValueError) as exc:
            raise VerificationError(f"类别 {uid} 的最少实例数不是整数") from exc
        if minimum < 1 or minimum > _MAX_MINIMUM:
            raise VerificationError(
                f"类别 {uid} 的最少实例数须在 1..{_MAX_MINIMUM} 之间"
            )
        merged[uid] = max(merged.get(uid, 0), minimum)
    if not merged:
        raise VerificationError("请至少填写一条 SOP Class UID 与最少实例数")
    return [Requirement(uid, n) for uid, n in merged.items()]


def parse_study_pair(value: str) -> tuple[str, str]:
    """解析下拉选项值 ``<匿名 Study UID>|<匿名 PatientID>``。"""
    study_uid, sep, patient_id = (value or "").rpartition("|")
    study_uid, patient_id = study_uid.strip(), patient_id.strip()
    if not sep or not study_uid or not patient_id:
        raise VerificationError("请选择要核查的 Study 与匿名 PatientID")
    return study_uid, patient_id


# ---------------------------------------------------------------- 只读快照查询
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _patient_expr() -> str:
    # 新数据直接存 new_patient_id；旧库迁移前为空时回退患者映射表
    return "COALESCE(NULLIF(bf.new_patient_id, ''), pm.new_patient_id, '')"


def list_study_options() -> list[dict]:
    """列出成功批次中可核查的（匿名 Study, 匿名 PatientID）组合。"""
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT bf.original_study_uid                       AS original_study_uid,
                   um.new_uid                                  AS study_uid,
                   {_patient_expr()}                           AS patient_id,
                   COUNT(DISTINCT bf.sop_uid)                  AS instances,
                   COUNT(DISTINCT bf.batch_id)                 AS batches,
                   MAX(b.finished_at)                          AS finished_at
            FROM batch_files AS bf
            JOIN batches AS b ON b.id = bf.batch_id AND b.status = 'ready'
            LEFT JOIN uid_mappings AS um
                   ON um.role = 'study' AND um.original_uid = bf.original_study_uid
            LEFT JOIN patient_mappings AS pm
                   ON pm.original_patient_id = bf.original_patient_id
            GROUP BY bf.original_study_uid, um.new_uid, patient_id
            HAVING um.new_uid IS NOT NULL AND um.new_uid != ''
               AND patient_id != ''
            ORDER BY um.new_uid, patient_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _ready_batch_summaries(conn) -> list[dict]:
    batches = conn.execute(
        "SELECT id, finished_at, zip_path, original_count FROM batches "
        "WHERE status='ready' ORDER BY id"
    ).fetchall()
    summaries = []
    for b in batches:
        size = None
        if b["zip_path"]:
            try:
                size = os.path.getsize(b["zip_path"])
            except OSError:
                size = None
        summaries.append({
            "id": b["id"],
            "finished_at": b["finished_at"],
            "zip_name": Path(b["zip_path"]).name if b["zip_path"] else None,
            "zip_size": size,
            "original_count": b["original_count"],
        })
    return summaries


def _candidate_rows(conn, original_study_uid: str) -> list:
    return conn.execute(
        f"""
        SELECT bf.batch_id, bf.sop_uid, bf.orig_name, bf.original_patient_id,
               bf.original_series_uid, bf.sop_class_uid, bf.new_sop_uid,
               {_patient_expr()} AS new_patient_id,
               b.finished_at
        FROM batch_files AS bf
        JOIN batches AS b ON b.id = bf.batch_id AND b.status = 'ready'
        LEFT JOIN patient_mappings AS pm
               ON pm.original_patient_id = bf.original_patient_id
        WHERE bf.original_study_uid = ?
        ORDER BY bf.sop_uid, b.created_at, bf.batch_id
        """,
        (original_study_uid,),
    ).fetchall()


def _instance_digest(instances: list[dict]) -> str:
    canonical = json.dumps(
        sorted(
            (
                i.get("sop_uid", ""),
                i.get("sha256", ""),
                i.get("new_sop_uid", ""),
                i.get("new_patient_id", ""),
            )
            for i in instances
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 核查主流程
def run_verification(
    study_uid: str, patient_id: str, requirements: list[Requirement]
) -> dict:
    """按当前成功批次核查一个（匿名 Study, 匿名 PatientID）对，返回结果字典。

    整个过程在一个只读事务内读取，保证本次核查看到的是同一时刻的批次快照。
    """
    if not requirements:
        raise VerificationError("请至少填写一条 SOP Class UID 与最少实例数")

    conn = get_db()
    try:
        conn.execute("BEGIN")  # 显式读事务：WAL 下获得一致快照
        mapping = conn.execute(
            "SELECT original_uid FROM uid_mappings "
            "WHERE role='study' AND new_uid=?",
            (study_uid,),
        ).fetchone()
        if mapping is None:
            raise VerificationError(f"找不到匿名 Study UID {study_uid} 对应的成功批次")
        original_study_uid = mapping["original_uid"]

        rows = _candidate_rows(conn, original_study_uid)
        if not rows:
            raise VerificationError("所选 Study 没有处于成功批次的实例，无法核查")
        pair_patients = {r["new_patient_id"] for r in rows if r["new_patient_id"]}
        if patient_id not in pair_patients:
            raise VerificationError(
                f"匿名 PatientID {patient_id} 在该 Study 的成功批次中不存在"
            )

        ready_batches = _ready_batch_summaries(conn)

        # 按原 SOP UID 去重：同一实例出现在多个成功批次也只算一次
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row["sop_uid"], []).append(row)
        if len(groups) > config.VERIFY_MAX_INSTANCES:
            raise VerificationError(
                f"该 Study 去重实例数 {len(groups)} 超过单次核查上限 "
                f"{config.VERIFY_MAX_INSTANCES}，请缩小核查范围或调高 "
                f"DICOM_VERIFY_MAX_INSTANCES"
            )

        counts: dict[str, int] = {}
        instances_by_class: dict[str, list[dict]] = {}
        issues: list[dict] = []
        digest_instances: list[dict] = []

        for sop_uid, group_rows in groups.items():
            batch_ids = sorted({r["batch_id"] for r in group_rows})
            orig_names = sorted({r["orig_name"] for r in group_rows})
            base = {
                "sop_uid": sop_uid,
                "orig_name": orig_names[0] if orig_names else "",
                "batch_ids": batch_ids,
            }

            inst = conn.execute(
                "SELECT sha256, new_sop_uid, object_path FROM instances "
                "WHERE sop_uid=?",
                (sop_uid,),
            ).fetchone()
            new_sop_uid = (
                inst["new_sop_uid"] if inst is not None
                else next((r["new_sop_uid"] for r in group_rows if r["new_sop_uid"]), "")
            )
            recorded_patient = next(
                (r["new_patient_id"] for r in group_rows if r["new_patient_id"]), ""
            )
            digest_instances.append({
                "sop_uid": sop_uid,
                "sha256": inst["sha256"] if inst is not None else "",
                "new_sop_uid": new_sop_uid,
                "new_patient_id": recorded_patient,
            })

            if inst is None or not inst["object_path"]:
                issues.append({
                    **base,
                    "category": ISSUE_MISSING_FILE,
                    "new_sop_uid": new_sop_uid,
                    "object_path": None,
                    "message": "实例未入库或对象路径缺失",
                })
                continue

            object_path = config.get_objects_dir() / inst["object_path"]
            if not object_path.is_file():
                issues.append({
                    **base,
                    "category": ISSUE_MISSING_FILE,
                    "new_sop_uid": new_sop_uid,
                    "object_path": inst["object_path"],
                    "message": "对象存储中找不到处理结果文件",
                })
                continue

            try:
                # 只读元数据即可完成协议完整性判断，避免对大体积像素做无谓解析
                ds = pydicom.dcmread(str(object_path), stop_before_pixels=True)
                parsed_sop = str(ds.SOPInstanceUID) if "SOPInstanceUID" in ds else ""
                parsed_study = str(ds.StudyInstanceUID) if "StudyInstanceUID" in ds else ""
                parsed_patient = str(ds.PatientID) if "PatientID" in ds else ""
                parsed_class = str(ds.SOPClassUID) if "SOPClassUID" in ds else ""
                if not parsed_sop or not parsed_class:
                    raise ValueError("缺少 SOPInstanceUID 或 SOPClassUID")
            except Exception as exc:  # noqa: BLE001 - 统一归为解析失败
                issues.append({
                    **base,
                    "category": ISSUE_PARSE_FAILED,
                    "new_sop_uid": new_sop_uid,
                    "object_path": inst["object_path"],
                    "message": f"无法解析处理结果: {exc}",
                })
                continue

            if new_sop_uid and parsed_sop != new_sop_uid:
                issues.append({
                    **base,
                    "category": ISSUE_PARSE_FAILED,
                    "new_sop_uid": new_sop_uid,
                    "object_path": inst["object_path"],
                    "message": (
                        f"解析出的 SOPInstanceUID {parsed_sop} 与登记的 "
                        f"{new_sop_uid} 不一致"
                    ),
                })
                continue
            if parsed_study != study_uid:
                issues.append({
                    **base,
                    "category": ISSUE_PARSE_FAILED,
                    "new_sop_uid": new_sop_uid,
                    "object_path": inst["object_path"],
                    "message": (
                        f"解析出的 StudyInstanceUID {parsed_study} 与所选 "
                        f"{study_uid} 不一致"
                    ),
                })
                continue

            # Study 已一致；PatientID 不一致即跨患者，不计入任何类别数量
            if parsed_patient != patient_id:
                issues.append({
                    **base,
                    "category": ISSUE_CROSS_PATIENT,
                    "new_sop_uid": new_sop_uid,
                    "patient_id": parsed_patient,
                    "selected_patient_id": patient_id,
                    "message": (
                        f"实例的匿名 PatientID 为 {parsed_patient or '（空）'}，"
                        f"与所选 {patient_id} 不一致"
                    ),
                })
                continue

            counts[parsed_class] = counts.get(parsed_class, 0) + 1
            instances_by_class.setdefault(parsed_class, []).append({
                "sop_uid": sop_uid,
                "new_sop_uid": new_sop_uid,
                "orig_name": base["orig_name"],
                "batch_ids": batch_ids,
            })

        class_results: list[dict] = []
        for req in requirements:
            found = counts.get(req.sop_class_uid, 0)
            satisfied = found >= req.minimum
            if not satisfied:
                issues.append({
                    "category": (
                        ISSUE_MISSING_CLASS if found == 0 else ISSUE_INSUFFICIENT
                    ),
                    "sop_class_uid": req.sop_class_uid,
                    "required": req.minimum,
                    "found": found,
                })
            class_results.append({
                "sop_class_uid": req.sop_class_uid,
                "required": req.minimum,
                "found": found,
                "satisfied": satisfied,
                "instances": instances_by_class.get(req.sop_class_uid, []),
            })

        snapshot = {
            "version": 1,
            "captured_at": _utcnow(),
            "study_uid": study_uid,
            "patient_id": patient_id,
            "requirements": [r.as_dict() for r in requirements],
            "ready_batches": ready_batches,
            "study_batch_ids": sorted({r["batch_id"] for r in rows}),
            "unique_instances": len(groups),
            "instance_digest": _instance_digest(digest_instances),
        }
        return {
            "original_study_uid": original_study_uid,
            "study_uid": study_uid,
            "patient_id": patient_id,
            "status": "usable" if not issues else "unusable",
            "requirements": [r.as_dict() for r in requirements],
            "class_results": class_results,
            "issues": issues,
            "batch_ids": snapshot["study_batch_ids"],
            "snapshot": snapshot,
            "total_instances": len(groups),
        }
    finally:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - 事务可能已因异常回滚
            pass
        conn.close()


def save_verification(result: dict) -> str:
    """持久化核查结果与其发布快照；快照 JSON 写入后不再更新。"""
    verification_id = uuid.uuid4().hex
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT INTO study_verifications("
                "id, original_study_uid, study_uid, expected_patient_ids, status, "
                "requirements_json, class_results_json, issues_json, batch_ids_json, "
                "snapshot_json, total_instances) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    verification_id,
                    result["original_study_uid"],
                    result["study_uid"],
                    json.dumps([result["patient_id"]], ensure_ascii=False),
                    result["status"],
                    json.dumps(result["requirements"], ensure_ascii=False),
                    json.dumps(result["class_results"], ensure_ascii=False),
                    json.dumps(result["issues"], ensure_ascii=False),
                    json.dumps(result["batch_ids"], ensure_ascii=False),
                    json.dumps(result["snapshot"], ensure_ascii=False),
                    result["total_instances"],
                ),
            )
    finally:
        conn.close()
    return verification_id


def get_verification(verification_id: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM study_verifications WHERE id=?", (verification_id,)
        ).fetchone()
        return _decode_row(row) if row is not None else None
    finally:
        conn.close()


def list_verifications(study_uid: str | None = None, limit: int = 30) -> list[dict]:
    conn = get_db()
    try:
        if study_uid:
            rows = conn.execute(
                "SELECT * FROM study_verifications WHERE study_uid=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (study_uid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM study_verifications "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decode_row(r) for r in rows]
    finally:
        conn.close()


def _decode_row(row) -> dict:
    data = dict(row)
    data["requirements"] = json.loads(data.pop("requirements_json"))
    data["class_results"] = json.loads(data.pop("class_results_json"))
    data["issues"] = json.loads(data.pop("issues_json"))
    data["batch_ids"] = json.loads(data.pop("batch_ids_json"))
    data["snapshot"] = json.loads(data.pop("snapshot_json"))
    data["patient_id"] = (json.loads(data.pop("expected_patient_ids")) or [""])[0]
    return data


def issues_by_category(issues: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {key: [] for key in ISSUE_LABELS}
    for issue in issues:
        grouped.setdefault(issue["category"], []).append(issue)
    return grouped


def is_stale(verification: dict) -> bool:
    """核查后该 Study 是否又纳入了新的成功批次（需重新核查才能看到）。"""
    snapshot_ids = set(verification.get("batch_ids") or [])
    conn = get_db()
    try:
        current_ids = {
            r["batch_id"] for r in conn.execute(
                "SELECT DISTINCT bf.batch_id FROM batch_files AS bf "
                "JOIN batches AS b ON b.id = bf.batch_id AND b.status='ready' "
                "WHERE bf.original_study_uid=?",
                (verification["original_study_uid"],),
            ).fetchall()
        }
    finally:
        conn.close()
    return current_ids != snapshot_ids
