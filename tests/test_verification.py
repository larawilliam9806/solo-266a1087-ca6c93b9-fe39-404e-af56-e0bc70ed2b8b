"""Study 协议完整性核查：去重、成对一致、问题分类、发布快照与重新核查。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pydicom
import pytest
from pydicom.uid import CTImageStorage, EnhancedSRStorage

from app import config, dicom_proc, verification
from app.db import get_db, init_db
from samples import generate_samples as gen

CT = str(CTImageStorage)                 # 1.2.840.10008.5.1.4.1.1.2
RT = "1.2.840.10008.5.1.4.1.1.481.3"
SR = str(EnhancedSRStorage)


def _upload(client, paths):
    from werkzeug.datastructures import MultiDict
    data = MultiDict()
    handles = []
    for p in paths:
        fh = open(p, "rb")
        handles.append(fh)
        data.add("dicom_files", (fh, Path(p).name, "application/dicom"))
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    for fh in handles:
        fh.close()
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def _study_pair(client, study_uid=None) -> str:
    page = client.get("/verify")
    txt = page.data.decode()
    options = re.findall(r'<option value="([^"]+)"', txt)
    if study_uid is None:
        return options[0]
    return next(o for o in options if o.startswith(study_uid + "|"))


def _submit(client, pair, requirements):
    return client.post("/verify", data={
        "study_pair": pair,
        "sop_class_uid": [r[0] for r in requirements],
        "minimum_instances": [str(r[1]) for r in requirements],
    })


def _result(client, resp):
    assert resp.status_code == 302, resp.data[:500]
    vid = resp.headers["Location"].rstrip("/").split("/")[-1]
    return verification.get_verification(vid)


# ---------------------------------------------------------------- 输入校验
def test_parse_requirements_dedupes_and_takes_max():
    reqs = verification.parse_requirements(
        [CT, " " + CT + " ", RT], ["2", "5", "1"]
    )
    assert [(r.sop_class_uid, r.minimum) for r in reqs] == [(CT, 5), (RT, 1)]


@pytest.mark.parametrize("uid,minimum", [
    ("", "1"),
    ("abc.1", "1"),
    ("1..2", "1"),
    ("01.2", "1"),
    (CT, "0"),
    (CT, "-1"),
    (CT, "x"),
])
def test_invalid_requirements_rejected(uid, minimum):
    with pytest.raises(verification.VerificationError):
        verification.parse_requirements([uid], [minimum])


def test_verify_page_and_bad_submission(client):
    assert client.get("/verify").status_code == 200
    resp = client.post("/verify", data={"study_pair": "", "sop_class_uid": [CT],
                                        "minimum_instances": ["1"]})
    assert resp.status_code == 400
    assert "请选择".encode() in resp.data


# ---------------------------------------------------------------- 全量满足
def test_all_requirements_met_marks_usable(client, sample_files):
    _upload(client, sorted(sample_files.glob("*.dcm")))
    pair = _study_pair(client)
    result = _result(client, _submit(client, pair, [(CT, 2), (RT, 1), (SR, 1)]))
    assert result["status"] == "usable"
    assert result["issues"] == []
    assert {(c["sop_class_uid"], c["found"]) for c in result["class_results"]} == {
        (CT, 2), (RT, 1), (SR, 1),
    }
    assert result["total_instances"] == 4


# ---------------------------------------------------------------- 重复上传不重复计数
def test_reupload_does_not_double_count(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    _upload(client, paths)
    pair = _study_pair(client)
    _upload(client, paths)  # 同一 Study 的第二个成功批次
    result = _result(client, _submit(client, pair, [(CT, 3)]))
    assert result["total_instances"] == 4
    ct = next(c for c in result["class_results"] if c["sop_class_uid"] == CT)
    assert ct["found"] == 2          # 不是 4
    assert ct["satisfied"] is False
    assert any(i["category"] == verification.ISSUE_INSUFFICIENT
               and i["found"] == 2 for i in result["issues"])


# ---------------------------------------------------------------- 逐类问题
def test_missing_class_and_insufficient_listed_separately(client, sample_files):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)
    result = _result(client, _submit(client, pair, [(CT, 3), (RT, 1), ("1.2.3.4", 1)]))
    assert result["status"] == "unusable"
    by_cat = verification.issues_by_category(result["issues"])
    assert [i["required"] for i in by_cat[verification.ISSUE_INSUFFICIENT]] == [3]
    assert by_cat[verification.ISSUE_INSUFFICIENT][0]["found"] == 1
    missing = {i["sop_class_uid"] for i in by_cat[verification.ISSUE_MISSING_CLASS]}
    assert missing == {RT, "1.2.3.4"}


def test_failed_batch_is_not_counted(client, sample_files, tmp_path):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)
    # 构造一个字节冲突的失败批次：同一 SOP 不同像素
    ds = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    ds.PixelData = bytes([0x55]) * len(ds.PixelData)
    evil = tmp_path / "evil.dcm"
    ds.save_as(str(evil), write_like_original=False)
    failed_id = _upload(client, [evil])
    assert client.get(f"/batches/{failed_id}/download").status_code == 409

    result = _result(client, _submit(client, pair, [(CT, 2)]))
    ct = result["class_results"][0]
    assert ct["found"] == 1
    assert result["status"] == "unusable"


def test_cross_patient_instances_listed_and_excluded(app, client, sample_files):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)

    # 同 Study 下另一个匿名患者：替换 PatientID 后作为新批次上传
    other = pydicom.dcmread(str(sample_files / "ct_002.dcm"))
    other.PatientID = "PID-OTHER-PATIENT"
    other_path = Path(config.get_upload_tmp_dir().parent) / "other.dcm"
    other.save_as(str(other_path), write_like_original=False)
    bid = _upload(client, [other_path])
    assert client.get(f"/batches/{bid}/download").status_code == 200

    result = _result(client, _submit(client, pair, [(CT, 2)]))
    by_cat = verification.issues_by_category(result["issues"])
    cross = by_cat[verification.ISSUE_CROSS_PATIENT]
    assert len(cross) == 1
    assert cross[0]["sop_uid"] == gen.SOP_CT_2
    assert cross[0]["patient_id"] != pair.split("|")[1]
    ct = next(c for c in result["class_results"] if c["sop_class_uid"] == CT)
    assert ct["found"] == 1          # 跨患者实例不计入


def test_missing_object_file_reported(client, sample_files):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)
    conn = get_db()
    try:
        row = conn.execute("SELECT object_path FROM instances WHERE sop_uid=?",
                           (gen.SOP_CT_1,)).fetchone()
    finally:
        conn.close()
    lost = config.get_objects_dir() / row["object_path"]
    backup = lost.with_suffix(".bak")
    shutil.move(str(lost), str(backup))
    try:
        result = _result(client, _submit(client, pair, [(CT, 1)]))
    finally:
        shutil.move(str(backup), str(lost))
    by_cat = verification.issues_by_category(result["issues"])
    assert [i["sop_uid"] for i in by_cat[verification.ISSUE_MISSING_FILE]] == [
        gen.SOP_CT_1
    ]
    assert result["class_results"][0]["found"] == 0


def test_unparsable_object_file_reported(client, sample_files):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)
    conn = get_db()
    try:
        row = conn.execute("SELECT object_path FROM instances WHERE sop_uid=?",
                           (gen.SOP_CT_1,)).fetchone()
    finally:
        conn.close()
    target = config.get_objects_dir() / row["object_path"]
    original = target.read_bytes()
    target.write_bytes(b"corrupted-not-dicom")
    try:
        result = _result(client, _submit(client, pair, [(CT, 1)]))
    finally:
        target.write_bytes(original)
    by_cat = verification.issues_by_category(result["issues"])
    assert [i["sop_uid"] for i in by_cat[verification.ISSUE_PARSE_FAILED]] == [
        gen.SOP_CT_1
    ]
    assert result["class_results"][0]["found"] == 0


# ---------------------------------------------------------------- 快照语义
def test_snapshot_is_frozen_until_reverification(client, sample_files):
    _upload(client, [sample_files / "ct_001.dcm"])
    pair = _study_pair(client)
    first = _result(client, _submit(client, pair, [(CT, 2)]))
    assert first["status"] == "unusable"

    # 补传第二张 CT；旧核查记录不回溯更新
    _upload(client, [sample_files / "ct_002.dcm"])
    old = verification.get_verification(first["id"])
    assert old["status"] == "unusable"
    assert old["class_results"][0]["found"] == 1
    assert len(old["snapshot"]["ready_batches"]) == 1
    assert verification.is_stale(old) is True
    detail = client.get(f"/verify/{first['id']}")
    assert "重新核查".encode() in detail.data

    # 重新核查才纳入补传实例
    fresh = _result(client, _submit(client, pair, [(CT, 2)]))
    assert fresh["status"] == "usable"
    assert fresh["class_results"][0]["found"] == 2
    assert len(fresh["snapshot"]["ready_batches"]) == 2
    assert fresh["snapshot"]["instance_digest"] != first["snapshot"]["instance_digest"]
    assert verification.is_stale(fresh) is False

    # 快照持久化内容不可变
    assert verification.get_verification(first["id"])["snapshot"] == first["snapshot"]

    # 补充上传包含其他 Study 的文件，不应让本 Study 的核查结果变为“过期”
    other_study = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    other_study.StudyInstanceUID = "1.2.826.0.1.9999.8888.1"
    other_study.SeriesInstanceUID = "1.2.826.0.1.9999.8888.2"
    other_study.SOPInstanceUID = "1.2.826.0.1.9999.8888.3"
    other_study.file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.9999.8888.3"
    other_path = Path(config.get_upload_tmp_dir().parent) / "other_study.dcm"
    other_study.save_as(str(other_path), write_like_original=False)
    assert client.get(f"/batches/{_upload(client, [other_path])}/download").status_code == 200
    assert verification.is_stale(verification.get_verification(first["id"])) is True
    assert verification.is_stale(verification.get_verification(fresh["id"])) is False


def test_verification_row_persists_requirements_and_snapshot(app, client, sample_files):
    _upload(client, sorted(sample_files.glob("*.dcm")))
    pair = _study_pair(client)
    result = _result(client, _submit(client, pair, [(CT, 2), (SR, 1)]))
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM study_verifications WHERE id=?", (result["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["requirements_json"]) == [
        {"sop_class_uid": CT, "minimum": 2},
        {"sop_class_uid": SR, "minimum": 1},
    ]
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["study_uid"] == pair.split("|")[0]
    assert snapshot["unique_instances"] == 4
    assert json.loads(row["expected_patient_ids"]) == [pair.split("|")[1]]
    assert snapshot["ready_batches"][0]["zip_size"] > 0
    # 最近核查记录可见
    assert f"/verify/{result['id']}".encode() in client.get("/verify").data


def test_unknown_study_pair_rejected(client, sample_files):
    _upload(client, sorted(sample_files.glob("*.dcm")))
    resp = _submit(client, "1.2.3.4|P000000000000", [(CT, 1)])
    assert resp.status_code == 400
    assert "找不到".encode() in resp.data

    pair = _study_pair(client)
    resp = _submit(client, pair.split("|")[0] + "|P999999999999", [(CT, 1)])
    assert resp.status_code == 400


def test_instance_cap_rejects_oversized_study(client, sample_files, monkeypatch):
    _upload(client, sorted(sample_files.glob("*.dcm")))
    pair = _study_pair(client)
    monkeypatch.setattr(config, "VERIFY_MAX_INSTANCES", 3)
    resp = _submit(client, pair, [(CT, 1)])
    assert resp.status_code == 400
    assert "上限".encode() in resp.data


# ---------------------------------------------------------------- 旧库迁移
def test_init_db_migrates_legacy_batch_files(app):
    """旧版本数据库缺少匿名字段时，启动建表应自动补齐且可重复执行。"""
    import sqlite3

    init_db()
    db_path = config.get_db_path()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE batch_files")
        conn.execute(
            "CREATE TABLE batch_files ("
            "batch_id TEXT NOT NULL, sop_uid TEXT NOT NULL, orig_name TEXT NOT NULL, "
            "original_patient_id TEXT NOT NULL DEFAULT '', "
            "original_study_uid TEXT NOT NULL DEFAULT '', "
            "original_series_uid TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (batch_id, sop_uid))"
        )
        conn.commit()
    finally:
        conn.close()

    init_db()
    init_db()
    conn = get_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(batch_files)")}
    finally:
        conn.close()
    assert {"sop_class_uid", "new_sop_uid", "new_patient_id"} <= cols


def test_legacy_rows_fall_back_to_mapping_tables(app, client, sample_files):
    """迁移前写入的 batch_files 匿名字段为空时，仍能通过映射表核查。"""
    _upload(client, sorted(sample_files.glob("*.dcm")))
    conn = get_db()
    try:
        conn.execute(
            "UPDATE batch_files SET new_sop_uid='', new_patient_id='', sop_class_uid=''"
        )
        conn.commit()
    finally:
        conn.close()

    pair = _study_pair(client)
    result = _result(client, _submit(client, pair, [(CT, 2), (RT, 1), (SR, 1)]))
    assert result["status"] == "usable"
    assert result["total_instances"] == 4
    # SOP Class 从处理结果中解析，不依赖旧行的空字段
    assert {c["sop_class_uid"] for c in result["class_results"]} == {CT, RT, SR}


def test_batch_files_records_anonymous_values(client, sample_files):
    batch_id = _upload(client, [sample_files / "ct_001.dcm"])
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM batch_files WHERE batch_id=?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["sop_class_uid"] == CT
    assert row["new_sop_uid"] and row["new_sop_uid"] != gen.SOP_CT_1
    assert row["new_patient_id"].startswith("P")
    assert dicom_proc  # 模块可导入
