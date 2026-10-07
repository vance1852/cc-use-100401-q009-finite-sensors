"""装备样本批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


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
CREATE TABLE IF NOT EXISTS instrument_catalog(
 instrument_id TEXT PRIMARY KEY, point_type TEXT NOT NULL,
 frequency_min_hz TEXT NOT NULL, frequency_max_hz TEXT NOT NULL,
 response_min TEXT NOT NULL, response_max TEXT NOT NULL, noise_max TEXT NOT NULL,
 registered_by TEXT NOT NULL, registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantine_records(
 quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
 measurement_id TEXT NOT NULL REFERENCES measurements(measurement_id),
 lot_id TEXT NOT NULL, violation TEXT NOT NULL, rule_version TEXT NOT NULL,
 reason TEXT NOT NULL, handled_by TEXT NOT NULL, handled_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','lifted')),
 lifted_by TEXT, lifted_at TEXT, lift_reason TEXT);
"""


def _migrate(db: sqlite3.Connection) -> None:
    """为契约上线前已存在的库补充观测标识、内容摘要与索引。"""

    columns = {row[1] for row in db.execute("PRAGMA table_info(measurements)")}
    if "observation_key" not in columns:
        db.execute("ALTER TABLE measurements ADD COLUMN observation_key TEXT")
    if "content_sha256" not in columns:
        db.execute("ALTER TABLE measurements ADD COLUMN content_sha256 TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS measurements_observation_key "
        "ON measurements(lot_id, observation_key) WHERE observation_key IS NOT NULL"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_quarantine_per_measurement "
        "ON quarantine_records(measurement_id) WHERE status='active'"
    )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 在各请求线程中复用同一连接，
    # 并发安全由 api.Handler 的锁保证。
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
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
