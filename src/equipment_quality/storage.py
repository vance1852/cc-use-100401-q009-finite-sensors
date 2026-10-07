"""装备样本批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .contracts import measurement_digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_batches(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES metric_batches(lot_id),
 test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS instruments(
 instrument_id TEXT PRIMARY KEY, point_type TEXT NOT NULL,
 frequency_min_hz REAL NOT NULL, frequency_max_hz REAL NOT NULL,
 response_min REAL NOT NULL, response_max REAL NOT NULL, noise_max REAL NOT NULL,
 registered_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantines(
 quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
 measurement_id TEXT NOT NULL REFERENCES measurements(measurement_id),
 lot_id TEXT NOT NULL, reason TEXT NOT NULL, rule_version TEXT NOT NULL,
 status TEXT NOT NULL, handled_by TEXT NOT NULL, handled_at TEXT NOT NULL,
 released_by TEXT, released_at TEXT, release_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS quarantines_active
 ON quarantines(measurement_id) WHERE status='active';
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(db: sqlite3.Connection) -> None:
    """把既有数据库升级到当前结构,并回填观测内容摘要。"""

    columns = {row[1] for row in db.execute("PRAGMA table_info(measurements)")}
    if "content_sha256" not in columns:
        db.execute("ALTER TABLE measurements ADD COLUMN content_sha256 TEXT")
        rows = db.execute(
            "SELECT measurement_id,instrument,measured_at,test_frequency_hz,response,noise "
            "FROM measurements"
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE measurements SET content_sha256=? WHERE measurement_id=?",
                (
                    measurement_digest(row[1], row[2], row[3], row[4], row[5]),
                    row[0],
                ),
            )


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务在处理线程中使用连接,由 api 层的调度锁保证串行访问。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, text, utcnow()),
    )
