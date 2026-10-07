"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .contracts import (
    CONTRACT_VERSION,
    FUTURE_TOLERANCE,
    ContractViolation,
    InstrumentRange,
    ValidatedObservation,
    canonical_number,
    parse_instrument,
    validate_observation,
)
from .errors import Conflict, NotFound, ValidationFailed
from .storage import connect, event, transaction, utcnow


class MetricQualityService:
    def __init__(self, database: str = ":memory:", *, clock=None):
        self.db = connect(database)
        self.auth = Auth(self.db)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

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
        with transaction(self.db):
            self.db.execute("INSERT INTO metric_batches VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, sample_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"批次不存在: {lot_id}")
        return dict(row)

    def _lot_or_404(self, lot_id: str) -> None:
        if not self.db.execute("SELECT 1 FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone():
            raise NotFound(f"批次不存在: {lot_id}")

    def register_instrument(
        self,
        token: str,
        instrument_id: str,
        point_type: str,
        frequency_min_hz,
        frequency_max_hz,
        response_min,
        response_max,
        noise_max,
    ) -> dict:
        """注册仪器量程；未注册的仪器导入观测时会被契约拒绝。"""

        actor = self.auth.require(token, "submit")
        spec = parse_instrument({
            "instrument_id": instrument_id,
            "point_type": point_type,
            "frequency_min_hz": frequency_min_hz,
            "frequency_max_hz": frequency_max_hz,
            "response_min": response_min,
            "response_max": response_max,
            "noise_max": noise_max,
        })
        with transaction(self.db):
            if self.db.execute(
                "SELECT 1 FROM instrument_catalog WHERE instrument_id=?", (spec.instrument_id,)
            ).fetchone():
                raise Conflict(f"仪器已注册: {spec.instrument_id}", field="instrument_id", rule="unique")
            self.db.execute(
                "INSERT INTO instrument_catalog VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    spec.instrument_id, spec.point_type,
                    str(spec.frequency_min_hz), str(spec.frequency_max_hz),
                    str(spec.response_min), str(spec.response_max), str(spec.noise_max),
                    actor.user_id, self._now_iso(),
                ),
            )
            event(self.db, spec.instrument_id, "instrument.registered", actor.user_id, {
                "point_type": spec.point_type,
                "frequency_range_hz": [str(spec.frequency_min_hz), str(spec.frequency_max_hz)],
                "response_range": [str(spec.response_min), str(spec.response_max)],
                "noise_max": str(spec.noise_max),
                "contract_version": CONTRACT_VERSION,
            })
        return {
            "instrument_id": spec.instrument_id,
            "point_type": spec.point_type,
            "contract_version": CONTRACT_VERSION,
        }

    def _instrument(self, instrument_id: str) -> InstrumentRange:
        row = self.db.execute(
            "SELECT * FROM instrument_catalog WHERE instrument_id=?", (instrument_id,)
        ).fetchone()
        if row is None:
            raise ContractViolation(
                "instrument", "instrument_registered", f"仪器未注册量程，观测来源不可追溯: {instrument_id}"
            )
        return InstrumentRange(
            instrument_id=row["instrument_id"],
            point_type=row["point_type"],
            frequency_min_hz=Decimal(row["frequency_min_hz"]),
            frequency_max_hz=Decimal(row["frequency_max_hz"]),
            response_min=Decimal(row["response_min"]),
            response_max=Decimal(row["response_max"]),
            noise_max=Decimal(row["noise_max"]),
        )

    def _validate_one(self, raw: Mapping, lot_id: str) -> ValidatedObservation:
        if not isinstance(raw, Mapping):
            raise ContractViolation("observation", "mapping", "观测必须是对象")
        instrument_id = raw.get("instrument")
        if not isinstance(instrument_id, str) or not instrument_id.strip():
            raise ContractViolation("instrument", "required", "instrument 必须是非空字符串")
        return validate_observation(
            raw, self._instrument(instrument_id.strip()), now=self._clock(), lot_id=lot_id
        )

    def _persist_observations(
        self, actor, lot_id: str, items: Sequence[ValidatedObservation]
    ) -> list[dict]:
        """在单个事务内写入全部观测；任何冲突都会回滚，不留业务或审计记录。"""

        results: list[dict] = []
        with transaction(self.db):
            for item in items:
                measurement_id = uuid.uuid4().hex
                replayed = False
                if item.observation_key is not None:
                    existing = self.db.execute(
                        "SELECT measurement_id,content_sha256 FROM measurements "
                        "WHERE lot_id=? AND observation_key=?",
                        (lot_id, item.observation_key),
                    ).fetchone()
                    if existing is not None:
                        if existing["content_sha256"] != item.content_sha256:
                            raise Conflict(
                                f"观测标识 {item.observation_key} 的重放内容不一致，整批未生效",
                                field="observation_key",
                                rule="replay_consistent_content",
                            )
                        measurement_id = existing["measurement_id"]
                        replayed = True
                if not replayed:
                    self.db.execute(
                        "INSERT INTO measurements(measurement_id,lot_id,test_frequency_hz,response,noise,"
                        "instrument,operator,measured_at,observation_key,content_sha256) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            measurement_id, lot_id,
                            float(item.test_frequency_hz), float(item.response), float(item.noise),
                            item.instrument, actor.user_id, item.measured_at,
                            item.observation_key, item.content_sha256,
                        ),
                    )
                results.append({
                    "measurement_id": measurement_id,
                    "observation_key": item.observation_key,
                    "replayed": replayed,
                })
            inserted = sum(1 for result in results if not result["replayed"])
            if len(items) == 1:
                if inserted:
                    event(self.db, lot_id, "measurement.imported", actor.user_id, {
                        "measurement_id": results[0]["measurement_id"],
                        "observation_key": results[0]["observation_key"],
                        "test_frequency_hz": canonical_number(items[0].test_frequency_hz),
                        "contract_version": CONTRACT_VERSION,
                    })
            elif inserted:
                event(self.db, lot_id, "measurements.batch_imported", actor.user_id, {
                    "inserted": inserted,
                    "replayed": len(items) - inserted,
                    "observation_keys": [result["observation_key"] for result in results],
                    "contract_version": CONTRACT_VERSION,
                })
        return results

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        test_frequency_hz,
        response,
        noise=0.0,
        instrument: str | None = None,
        *,
        observation_key: str | None = None,
        measured_at: str | None = None,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        self._lot_or_404(lot_id)
        validated = self._validate_one({
            "observation_key": observation_key,
            "instrument": instrument,
            "test_frequency_hz": test_frequency_hz,
            "response": response,
            "noise": noise,
            "measured_at": measured_at,
        }, lot_id)
        result = self._persist_observations(actor, lot_id, [validated])[0]
        return {
            "measurement_id": result["measurement_id"],
            "lot_id": lot_id,
            "observation_key": result["observation_key"],
            "replayed": result["replayed"],
            "contract_version": CONTRACT_VERSION,
        }

    def add_measurements(self, token: str, lot_id: str, items) -> dict:
        """批量导入：先完成全部契约校验，再单事务写入，任一条失败则整批不生效。"""

        actor = self.auth.require(token, "measure")
        self._lot_or_404(lot_id)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
            raise ValidationFailed("measurements 必须是非空数组")
        validated: list[ValidatedObservation] = []
        failures: list[dict] = []
        for index, raw in enumerate(items):
            try:
                validated.append(self._validate_one(raw, lot_id))
            except ContractViolation as exc:
                failures.append({
                    "index": index,
                    "observation_key": raw.get("observation_key") if isinstance(raw, Mapping) else None,
                    "field": exc.field,
                    "rule": exc.rule,
                    "message": str(exc),
                })
        if failures:
            raise ValidationFailed(
                f"批量导入未通过数值契约（{len(failures)} 条失败），未写入任何记录", failures=failures
            )
        seen: set[str] = set()
        for item in validated:
            if item.observation_key is None:
                continue
            if item.observation_key in seen:
                raise ValidationFailed(
                    f"同一批次内观测标识重复: {item.observation_key}",
                    field="observation_key",
                    rule="unique_in_batch",
                )
            seen.add(item.observation_key)
        results = self._persist_observations(actor, lot_id, validated)
        return {
            "lot_id": lot_id,
            "inserted": sum(1 for result in results if not result["replayed"]),
            "replayed": sum(1 for result in results if result["replayed"]),
            "contract_version": CONTRACT_VERSION,
            "measurements": results,
        }

    def scan_measurements(self, token: str, lot_id: str) -> dict:
        """按当前契约版本识别库中已存在的异常观测，只读不写。"""

        self.auth.require(token, "read")
        self._lot_or_404(lot_id)
        instruments = {
            row["instrument_id"]: self._instrument(row["instrument_id"])
            for row in self.db.execute("SELECT instrument_id FROM instrument_catalog")
        }
        quarantined = {
            row["measurement_id"]
            for row in self.db.execute(
                "SELECT measurement_id FROM quarantine_records WHERE lot_id=? AND status='active'", (lot_id,)
            )
        }
        now = self._clock().astimezone(timezone.utc)
        violations: list[dict] = []

        def report(measurement_id: str, field: str, rule: str, message: str) -> None:
            violations.append({
                "measurement_id": measurement_id,
                "field": field,
                "rule": rule,
                "message": message,
                "already_quarantined": measurement_id in quarantined,
            })

        rows = self.db.execute(
            "SELECT * FROM measurements WHERE lot_id=? ORDER BY measured_at,measurement_id", (lot_id,)
        ).fetchall()
        for row in rows:
            measurement_id = row["measurement_id"]
            spec = instruments.get(row["instrument"])
            if spec is None:
                report(measurement_id, "instrument", "instrument_registered",
                       f"仪器未注册量程: {row['instrument']}")
            for field in ("test_frequency_hz", "response", "noise"):
                value = row[field]
                if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
                    report(measurement_id, field, "finite", f"{field} 不是有限数值: {value!r}")
                    continue
                if spec is not None:
                    low, high = spec.field_range(field)
                    number = Decimal(str(value))
                    if number < low or number > high:
                        report(measurement_id, field, "range",
                               f"{field}={value} 超出设备量程 [{low}, {high}]")
            try:
                parsed = datetime.fromisoformat(row["measured_at"])
                if parsed.tzinfo is None:
                    raise ValueError("缺少时区")
                if parsed.astimezone(timezone.utc) > now + FUTURE_TOLERANCE:
                    report(measurement_id, "measured_at", "timestamp_not_future",
                           "measured_at 晚于当前时间")
            except (TypeError, ValueError):
                report(measurement_id, "measured_at", "timestamp_format",
                       f"measured_at 不是带时区的 ISO-8601 时间: {row['measured_at']!r}")
        return {
            "lot_id": lot_id,
            "contract_version": CONTRACT_VERSION,
            "scanned": len(rows),
            "violation_count": len(violations),
            "violations": violations,
        }

    def quarantine_measurement(
        self, token: str, measurement_id: str, reason: str, violation: str = "manual"
    ) -> dict:
        """隔离一条异常观测，记录处置人、原因与当前规则版本。"""

        actor = self.auth.require(token, "quarantine")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("处置原因不能为空", field="reason", rule="required")
        row = self.db.execute(
            "SELECT measurement_id,lot_id FROM measurements WHERE measurement_id=?", (measurement_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"测量不存在: {measurement_id}")
        with transaction(self.db):
            if self.db.execute(
                "SELECT 1 FROM quarantine_records WHERE measurement_id=? AND status='active'",
                (measurement_id,),
            ).fetchone():
                raise Conflict("该测量已处于隔离状态", field="measurement_id", rule="quarantine_unique")
            cursor = self.db.execute(
                "INSERT INTO quarantine_records(measurement_id,lot_id,violation,rule_version,reason,"
                "handled_by,handled_at,status) VALUES(?,?,?,?,?,?,?,'active')",
                (measurement_id, row["lot_id"], violation, CONTRACT_VERSION,
                 reason.strip(), actor.user_id, self._now_iso()),
            )
            quarantine_id = cursor.lastrowid
            event(self.db, row["lot_id"], "measurement.quarantined", actor.user_id, {
                "measurement_id": measurement_id,
                "quarantine_id": quarantine_id,
                "violation": violation,
                "rule_version": CONTRACT_VERSION,
                "reason": reason.strip(),
            })
        return {
            "quarantine_id": quarantine_id,
            "measurement_id": measurement_id,
            "status": "active",
            "rule_version": CONTRACT_VERSION,
        }

    def release_quarantine(self, token: str, quarantine_id: int, reason: str) -> dict:
        """解除隔离，保留完整处置历史。"""

        actor = self.auth.require(token, "quarantine")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("解除原因不能为空", field="reason", rule="required")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT * FROM quarantine_records WHERE quarantine_id=?", (quarantine_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"隔离记录不存在: {quarantine_id}")
            if row["status"] != "active":
                raise Conflict("隔离记录已解除", field="quarantine_id", rule="quarantine_active")
            self.db.execute(
                "UPDATE quarantine_records SET status='lifted',lifted_by=?,lifted_at=?,lift_reason=? "
                "WHERE quarantine_id=?",
                (actor.user_id, self._now_iso(), reason.strip(), quarantine_id),
            )
            event(self.db, row["lot_id"], "measurement.quarantine_released", actor.user_id, {
                "measurement_id": row["measurement_id"],
                "quarantine_id": quarantine_id,
                "reason": reason.strip(),
            })
        return {"quarantine_id": quarantine_id, "measurement_id": row["measurement_id"], "status": "lifted"}

    def list_quarantine(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_or_404(lot_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM quarantine_records WHERE lot_id=? ORDER BY quarantine_id", (lot_id,)
            ).fetchall()
        ]

    def analyze(self, token: str, lot_id: str, include_quarantined: bool = False) -> dict:
        """生成统计分析；默认只使用仍然有效且可追溯来源的观测。"""

        self.auth.require(token, "analyze")
        if include_quarantined:
            rows = self.db.execute(
                "SELECT test_frequency_hz,response FROM measurements WHERE lot_id=? "
                "ORDER BY test_frequency_hz", (lot_id,)
            ).fetchall()
            excluded = 0
        else:
            rows = self.db.execute(
                "SELECT m.test_frequency_hz,m.response FROM measurements m "
                "LEFT JOIN quarantine_records q ON q.measurement_id=m.measurement_id AND q.status='active' "
                "WHERE m.lot_id=? AND q.quarantine_id IS NULL ORDER BY m.test_frequency_hz", (lot_id,)
            ).fetchall()
            excluded = self.db.execute(
                "SELECT count(*) FROM quarantine_records q JOIN measurements m "
                "ON m.measurement_id=q.measurement_id WHERE m.lot_id=? AND q.status='active'", (lot_id,)
            ).fetchone()[0]
        if len(rows) < 3:
            raise ValueError(
                f"有效测量不足 3 条（{len(rows)} 条有效，{excluded} 条隔离中），无法生成统计"
            )
        summary = summarize_response_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["sample_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {
            "lot_id": lot_id,
            "contract_version": CONTRACT_VERSION,
            "excluded_quarantined": excluded,
            "response_profile": summary.__dict__,
            "yield": rates,
            "response_ci": ci,
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
