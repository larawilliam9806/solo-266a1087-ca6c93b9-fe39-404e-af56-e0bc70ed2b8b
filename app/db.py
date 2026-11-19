"""SQLite 连接管理与建表/恢复逻辑。

设计要点：
- WAL + busy_timeout + 每次写事务使用 IMMEDIATE，保证多 gunicorn worker 并发安全；
- 标识映射（UID/PatientID）全局唯一且只插入、不更新，保证跨批次一致与重试不变号；
- 实例按 (原 SOP UID, 原文件 sha256) 去重，字节不同直接冲突，由上层整批拒绝。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS uid_mappings (
    role          TEXT NOT NULL CHECK (role IN ('study','series','sop')),
    original_uid  TEXT NOT NULL,
    new_uid       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (role, original_uid)
);

CREATE TABLE IF NOT EXISTS patient_mappings (
    original_patient_id TEXT PRIMARY KEY,
    new_patient_id      TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    sop_uid        TEXT PRIMARY KEY,           -- 原 SOPInstanceUID
    sha256         TEXT NOT NULL,
    new_sop_uid    TEXT NOT NULL UNIQUE,
    object_path    TEXT NOT NULL,              -- 处理结果在对象存储中的相对路径
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
    original_count   INTEGER NOT NULL DEFAULT 0,
    zip_path         TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS batch_files (
    batch_id              TEXT NOT NULL REFERENCES batches(id),
    sop_uid               TEXT NOT NULL,                  -- 原 SOP UID
    orig_name             TEXT NOT NULL,
    original_patient_id   TEXT NOT NULL DEFAULT '',
    original_study_uid    TEXT NOT NULL DEFAULT '',
    original_series_uid   TEXT NOT NULL DEFAULT '',
    sop_class_uid         TEXT NOT NULL DEFAULT '',       -- 原 SOP Class UID
    new_sop_uid           TEXT NOT NULL DEFAULT '',       -- 匿名化后的 SOP UID
    new_patient_id        TEXT NOT NULL DEFAULT '',       -- 匿名化后的 PatientID
    PRIMARY KEY (batch_id, sop_uid)
);

CREATE INDEX IF NOT EXISTS idx_batch_files_sop ON batch_files(sop_uid);
CREATE INDEX IF NOT EXISTS idx_instances_sha  ON instances(sha256);

-- 完整性核查结果：每条记录固定保存核查当时的“发布快照”，
-- 后续补传/新批次不会回写历史结果，只有重新核查才会生成新记录。
CREATE TABLE IF NOT EXISTS study_verifications (
    id                      TEXT PRIMARY KEY,
    original_study_uid      TEXT NOT NULL,
    study_uid               TEXT NOT NULL,              -- 匿名化后的 Study UID
    expected_patient_ids    TEXT NOT NULL DEFAULT '[]', -- JSON 数组（匿名 PatientID）
    status                  TEXT NOT NULL CHECK (status IN ('usable','unusable')),
    requirements_json       TEXT NOT NULL,
    class_results_json      TEXT NOT NULL,
    issues_json             TEXT NOT NULL,
    batch_ids_json          TEXT NOT NULL,              -- JSON 数组，快照所用成功批次
    snapshot_json           TEXT NOT NULL,              -- JSON，发布快照（批次/zip 哈希等）
    total_instances         INTEGER NOT NULL DEFAULT 0, -- 去重后的候选实例数
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_study_verifications_study
    ON study_verifications(original_study_uid);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.get_db_path(),
        timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 手动事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 事务，避免写-写竞争下的 deferred 死锁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db() -> None:
    config.ensure_dirs()
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


# 增量迁移：老数据库的 batch_files 没有核查所需的匿名字段
_BATCH_FILES_NEW_COLUMNS = {
    "sop_class_uid": "TEXT NOT NULL DEFAULT ''",
    "new_sop_uid": "TEXT NOT NULL DEFAULT ''",
    "new_patient_id": "TEXT NOT NULL DEFAULT ''",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(batch_files)")
    }
    for name, ddl in _BATCH_FILES_NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE batch_files ADD COLUMN {name} {ddl}")


def recover_interrupted_batches() -> list[str]:
    """进程启动时把上次中断遗留的 processing 批次标记为 failed。

    已成功入库的 instances/映射一律保留（它们本身是有效的、可被后续批次复用）；
    没有生成 zip 的批次绝不可能以 ready 状态出现，因此不存在可下载的半批。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM batches WHERE status='processing'"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                "UPDATE batches SET status='failed', error='服务重启，批次中断', "
                "finished_at=datetime('now') WHERE status='processing'"
            )
        return ids
    finally:
        conn.close()
