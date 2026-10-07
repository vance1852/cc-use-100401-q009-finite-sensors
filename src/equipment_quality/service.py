"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .contracts import (
    CONTRACT_VERSION,
    ContractViolation,
    InstrumentRanges,
    NormalizedMeasurement,
    stored_row_issues,
    validate_instrument,
    validate_measurement,
)
from .errors import Conflict, InvalidState, NotFound
from .storage import connect, event, transaction, utcnow


class MetricQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "metric-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, sample_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if sample_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute("INSERT INTO metric_batches VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, sample_count, "engineering", actor.user_id, now, now))
                event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"lot already exists: {lot_id}") from exc
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"lot not found: {lot_id}")
        return dict(row)

    def register_instrument(
        self,
        token: str,
        instrument_id: str,
        point_type: str,
        ranges: Mapping[str, Any],
    ) -> dict:
        """注册设备的测点类型和量程;未注册设备的测点会被拒绝。"""

        actor = self.auth.require(token, "submit")
        contract = validate_instrument(instrument_id, point_type, ranges)
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        contract.instrument_id,
                        contract.point_type,
                        contract.frequency_min_hz,
                        contract.frequency_max_hz,
                        contract.response_min,
                        contract.response_max,
                        contract.noise_max,
                        actor.user_id,
                        utcnow(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"instrument already registered: {contract.instrument_id}") from exc
        return {"instrument_id": contract.instrument_id, "point_type": contract.point_type}

    def _instrument_map(self) -> dict[str, InstrumentRanges]:
        rows = self.db.execute("SELECT * FROM instruments").fetchall()
        return {
            row["instrument_id"]: InstrumentRanges(
                instrument_id=row["instrument_id"],
                point_type=row["point_type"],
                frequency_min_hz=row["frequency_min_hz"],
                frequency_max_hz=row["frequency_max_hz"],
                response_min=row["response_min"],
                response_max=row["response_max"],
                noise_max=row["noise_max"],
            )
            for row in rows
        }

    def _lot_or_raise(self, lot_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"lot not found: {lot_id}")
        return row

    def import_measurements(
        self,
        token: str,
        lot_id: str,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict:
        """批量导入测点:全部通过契约才写入,否则不产生任何业务或审计记录。"""

        actor = self.auth.require(token, "measure")
        raw_rows = list(rows)
        if not raw_rows:
            raise ContractViolation([{"field": "measurements", "rule": "required", "message": "测点数组不能为空"}])
        self._lot_or_raise(lot_id)
        instruments = self._instrument_map()
        now = datetime.now(timezone.utc)
        normalized: list[NormalizedMeasurement] = []
        issues: list[dict] = []
        for index, raw in enumerate(raw_rows):
            try:
                normalized.append(validate_measurement(raw, instruments, now=now, row=index))
            except ContractViolation as exc:
                issues.extend(exc.issues)
        if issues:
            raise ContractViolation(issues)
        # 同一批内的重复标识:内容一致视为重放,内容不一致直接拒绝。
        deduped: list[NormalizedMeasurement] = []
        seen: dict[str, str] = {}
        replayed_in_batch = 0
        for item in normalized:
            digest = seen.get(item.measurement_id)
            if digest is None:
                seen[item.measurement_id] = item.content_sha256
                deduped.append(item)
            elif digest != item.content_sha256:
                raise Conflict(f"measurement {item.measurement_id} 的重放内容不一致")
            else:
                replayed_in_batch += 1
        # 与库中既有观测比对:相同内容幂等返回,不同内容拒绝。
        existing = {
            row["measurement_id"]: row["content_sha256"]
            for row in self.db.execute(
                f"SELECT measurement_id,content_sha256 FROM measurements "
                f"WHERE measurement_id IN ({','.join('?' * len(seen))})",
                tuple(seen),
            ).fetchall()
        }
        fresh: list[NormalizedMeasurement] = []
        replayed = replayed_in_batch
        for item in deduped:
            stored = existing.get(item.measurement_id)
            if stored is None:
                fresh.append(item)
            elif stored != item.content_sha256:
                raise Conflict(f"measurement {item.measurement_id} 的重放内容不一致")
            else:
                replayed += 1
        recorded_at = utcnow()
        try:
            with transaction(self.db):
                for item in fresh:
                    self.db.execute(
                        "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            item.measurement_id,
                            lot_id,
                            item.test_frequency_hz,
                            item.response,
                            item.noise,
                            item.instrument_id,
                            actor.user_id,
                            item.measured_at,
                            item.content_sha256,
                        ),
                    )
                    event(
                        self.db,
                        lot_id,
                        "measurement",
                        actor.user_id,
                        {
                            "measurement_id": item.measurement_id,
                            "instrument": item.instrument_id,
                            "measured_at": item.measured_at,
                            "test_frequency_hz": item.test_frequency_hz,
                            "response": item.response,
                            "noise": item.noise,
                            "content_sha256": item.content_sha256,
                            "rule_version": CONTRACT_VERSION,
                        },
                    )
        except sqlite3.IntegrityError as exc:
            raise Conflict("测点标识并发冲突,请按幂等重放重试") from exc
        return {
            "lot_id": lot_id,
            "inserted": len(fresh),
            "replayed": replayed,
            "recorded_at": recorded_at,
            "measurement_ids": [item.measurement_id for item in deduped],
        }

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        test_frequency_hz: object,
        response: object,
        noise: object = None,
        instrument: object = None,
        *,
        measurement_id: object = None,
        measured_at: object = None,
    ) -> dict:
        """单条导入,与批量导入共用同一契约和原子性保证。"""

        raw = {
            "measurement_id": measurement_id,
            "instrument": instrument,
            "measured_at": measured_at,
            "test_frequency_hz": test_frequency_hz,
            "response": response,
            "noise": noise,
        }
        result = self.import_measurements(token, lot_id, [raw])
        return {
            "measurement_id": result["measurement_ids"][0],
            "lot_id": lot_id,
            "replayed": result["inserted"] == 0,
        }

    def scan_measurements(self, token: str, lot_id: str | None = None) -> dict:
        """按当前规则版本复核库中测点,识别需要隔离处置的异常值。"""

        self.auth.require(token, "read")
        instruments = self._instrument_map()
        sql = (
            "SELECT measurement_id,lot_id,instrument,measured_at,test_frequency_hz,response,noise "
            "FROM measurements"
        )
        params: tuple = ()
        if lot_id is not None:
            self._lot_or_raise(lot_id)
            sql += " WHERE lot_id=?"
            params = (lot_id,)
        rows = self.db.execute(sql + " ORDER BY measurement_id", params).fetchall()
        violations = []
        for row in rows:
            issues = stored_row_issues(
                row["instrument"],
                row["measured_at"],
                row["test_frequency_hz"],
                row["response"],
                row["noise"],
                instruments.get(row["instrument"]),
            )
            if issues:
                violations.append(
                    {
                        "measurement_id": row["measurement_id"],
                        "lot_id": row["lot_id"],
                        "issues": issues,
                    }
                )
        return {
            "rule_version": CONTRACT_VERSION,
            "scanned": len(rows),
            "violations": violations,
        }

    def quarantine_measurement(self, token: str, measurement_id: str, reason: str) -> dict:
        """隔离一条异常测点,保存处置人、原因和规则版本。"""

        actor = self.auth.require(token, "quarantine")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation([{"field": "reason", "rule": "required", "message": "处置原因必填"}])
        row = self.db.execute(
            "SELECT measurement_id,lot_id FROM measurements WHERE measurement_id=?", (measurement_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"measurement not found: {measurement_id}")
        try:
            with transaction(self.db):
                cursor = self.db.execute(
                    "INSERT INTO quarantines(measurement_id,lot_id,reason,rule_version,status,handled_by,handled_at) "
                    "VALUES(?,?,?,?,'active',?,?)",
                    (measurement_id, row["lot_id"], reason.strip(), CONTRACT_VERSION, actor.user_id, utcnow()),
                )
                event(
                    self.db,
                    row["lot_id"],
                    "measurement.quarantined",
                    actor.user_id,
                    {
                        "measurement_id": measurement_id,
                        "reason": reason.strip(),
                        "rule_version": CONTRACT_VERSION,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"measurement already quarantined: {measurement_id}") from exc
        return {
            "quarantine_id": cursor.lastrowid,
            "measurement_id": measurement_id,
            "status": "active",
            "rule_version": CONTRACT_VERSION,
        }

    def release_quarantine(self, token: str, measurement_id: str, reason: str) -> dict:
        """解除隔离;解除动作同样进入审计事件流。"""

        actor = self.auth.require(token, "quarantine")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation([{"field": "reason", "rule": "required", "message": "解除原因必填"}])
        row = self.db.execute(
            "SELECT quarantine_id,lot_id FROM quarantines WHERE measurement_id=? AND status='active'",
            (measurement_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no active quarantine for measurement: {measurement_id}")
        with transaction(self.db):
            cursor = self.db.execute(
                "UPDATE quarantines SET status='released',released_by=?,released_at=?,release_reason=? "
                "WHERE quarantine_id=? AND status='active'",
                (actor.user_id, utcnow(), reason.strip(), row["quarantine_id"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("quarantine status changed concurrently")
            event(
                self.db,
                row["lot_id"],
                "measurement.quarantine_released",
                actor.user_id,
                {"measurement_id": measurement_id, "reason": reason.strip()},
            )
        return {"measurement_id": measurement_id, "status": "released"}

    def list_quarantines(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_or_raise(lot_id)
        rows = self.db.execute(
            "SELECT * FROM quarantines WHERE lot_id=? ORDER BY quarantine_id", (lot_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def analyze(self, token: str, lot_id: str, include_quarantined: bool = False) -> dict:
        """生成批次分析报告;默认只使用仍然有效且能追溯来源的观测。"""

        self.auth.require(token, "analyze")
        lot = self._lot_or_raise(lot_id)
        instruments = self._instrument_map()
        rows = self.db.execute(
            "SELECT m.measurement_id,m.test_frequency_hz,m.response,m.noise,m.instrument,m.measured_at,"
            "q.quarantine_id AS active_quarantine "
            "FROM measurements m "
            "LEFT JOIN quarantines q ON q.measurement_id=m.measurement_id AND q.status='active' "
            "WHERE m.lot_id=? ORDER BY m.measured_at,m.measurement_id",
            (lot_id,),
        ).fetchall()
        used: list[sqlite3.Row] = []
        excluded: list[dict] = []
        for row in rows:
            issues = stored_row_issues(
                row["instrument"],
                row["measured_at"],
                row["test_frequency_hz"],
                row["response"],
                row["noise"],
                instruments.get(row["instrument"]),
            )
            if issues:
                excluded.append(
                    {"measurement_id": row["measurement_id"], "reason": "contract_violation", "issues": issues}
                )
            elif row["active_quarantine"] is not None and not include_quarantined:
                excluded.append({"measurement_id": row["measurement_id"], "reason": "quarantined"})
            else:
                used.append(row)
        if len(used) < 3:
            raise ValueError(
                f"at least three valid measurements are required, got {len(used)} "
                f"({len(excluded)} excluded)"
            )
        pairs = sorted((row["test_frequency_hz"], row["response"]) for row in used)
        summary = summarize_response_profile([p[0] for p in pairs], [p[1] for p in pairs])
        rates = yield_rate(lot["sample_count"], sum(1 for row in used if row["response"] >= 0.8), 0)
        ci = confidence_interval([row["response"] for row in used])
        return {
            "lot_id": lot_id,
            "response_profile": summary.__dict__,
            "yield": rates,
            "response_ci": ci,
            "rule_version": CONTRACT_VERSION,
            "measurements_used": [row["measurement_id"] for row in used],
            "measurements_excluded": excluded,
        }

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE metric_batches SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
