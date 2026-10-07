from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from equipment_quality.api import dispatch
from equipment_quality.contracts import CONTRACT_VERSION
from equipment_quality.errors import Conflict, NotFound, ValidationFailed
from equipment_quality.service import MetricQualityService


NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
OBSERVED = "2026-10-07T07:00:00+00:00"


class EquipmentQualityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService(clock=lambda: NOW)
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "metric-admin")
        self.service.create_lot(self.admin, "LOT-1", "wellhead-sensor", "REV-1", 10)
        self.service.register_instrument(
            self.admin, "gw-1", "wellhead_telemetry", "1", "5000", "-5", "5", "1"
        )

    def counts(self) -> tuple[int, int]:
        measurements = self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0]
        events = self.service.db.execute("SELECT count(*) FROM lot_events").fetchone()[0]
        return measurements, events

    def seed_legacy_overflow(self) -> None:
        # 模拟契约上线前已经进入测量表的量程溢出遗留数据
        self.service.db.execute(
            "INSERT INTO measurements(measurement_id,lot_id,test_frequency_hz,response,noise,"
            "instrument,operator,measured_at) VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-1", "LOT-1", 520.0, float("inf"), 0.01, "gw-1", "admin",
             "2026-10-06T23:00:00+00:00"),
        )
        self.service.db.commit()


class NumericContractTests(EquipmentQualityTestBase):
    def test_infinity_rejected_with_field_and_rule_and_no_trace(self) -> None:
        before = self.counts()
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(
                self.admin, "LOT-1", 520, float("inf"), 0.01, "gw-1", observation_key="k-1"
            )
        self.assertEqual(ctx.exception.field, "response")
        self.assertEqual(ctx.exception.rule, "finite")
        self.assertEqual(self.counts(), before)

    def test_nan_infinity_text_and_bool_rejected(self) -> None:
        for bad in (float("nan"), float("-inf"), "Infinity", "-Infinity", "NaN", "abc", "", True, None):
            with self.assertRaises(ValidationFailed, msg=repr(bad)):
                self.service.add_measurement(self.admin, "LOT-1", bad, 0.5, 0.01, "gw-1")
        self.assertEqual(self.counts()[0], 0)

    def test_numeric_strings_accepted(self) -> None:
        result = self.service.add_measurement(
            self.admin, "LOT-1", "520.5", "0.93", "0.01", "gw-1",
            observation_key="k-num", measured_at=OBSERVED,
        )
        self.assertFalse(result["replayed"])
        self.assertEqual(result["contract_version"], CONTRACT_VERSION)

    def test_device_range_checked_per_field(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.admin, "LOT-1", 99999, 0.5, 0.01, "gw-1")
        self.assertEqual((ctx.exception.field, ctx.exception.rule), ("test_frequency_hz", "range"))
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.admin, "LOT-1", 520, 99, 0.01, "gw-1")
        self.assertEqual((ctx.exception.field, ctx.exception.rule), ("response", "range"))
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.admin, "LOT-1", 520, 0.5, -0.1, "gw-1")
        self.assertEqual((ctx.exception.field, ctx.exception.rule), ("noise", "range"))

    def test_timestamp_rules(self) -> None:
        future = (NOW + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(
                self.admin, "LOT-1", 520, 0.5, 0.01, "gw-1", measured_at=future
            )
        self.assertEqual(ctx.exception.rule, "timestamp_not_future")
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(
                self.admin, "LOT-1", 520, 0.5, 0.01, "gw-1", measured_at="2026-10-07 07:00:00"
            )
        self.assertEqual(ctx.exception.rule, "timestamp_format")
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(
                self.admin, "LOT-1", 520, 0.5, 0.01, "gw-1", measured_at="1990-01-01T00:00:00+00:00"
            )
        self.assertEqual(ctx.exception.rule, "timestamp_not_ancient")

    def test_unregistered_instrument_rejected(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.admin, "LOT-1", 520, 0.5, 0.01, "ghost")
        self.assertEqual(ctx.exception.rule, "instrument_registered")

    def test_instrument_range_must_fit_point_type(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.register_instrument(
                self.admin, "gw-2", "wellhead_telemetry", "1", "99999999", "-5", "5", "1"
            )
        self.assertEqual(ctx.exception.rule, "point_type_bounds")
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.register_instrument(self.admin, "gw-3", "unknown-type", "1", "10", "0", "1", "1")
        self.assertEqual(ctx.exception.rule, "point_type_known")


class ReplayTests(EquipmentQualityTestBase):
    def test_same_key_same_content_is_idempotent(self) -> None:
        first = self.service.add_measurement(
            self.admin, "LOT-1", 520, 0.93, 0.01, "gw-1",
            observation_key="w-1", measured_at=OBSERVED,
        )
        before = self.counts()
        second = self.service.add_measurement(
            self.admin, "LOT-1", 520, "0.93", 0.01, "gw-1",
            observation_key="w-1", measured_at=OBSERVED,
        )
        self.assertTrue(second["replayed"])
        self.assertEqual(first["measurement_id"], second["measurement_id"])
        self.assertEqual(self.counts(), before)

    def test_same_key_different_content_conflicts(self) -> None:
        self.service.add_measurement(
            self.admin, "LOT-1", 520, 0.93, 0.01, "gw-1",
            observation_key="w-1", measured_at=OBSERVED,
        )
        before = self.counts()
        with self.assertRaises(Conflict) as ctx:
            self.service.add_measurement(
                self.admin, "LOT-1", 520, 0.94, 0.01, "gw-1",
                observation_key="w-1", measured_at=OBSERVED,
            )
        self.assertEqual(ctx.exception.field, "observation_key")
        self.assertEqual(ctx.exception.rule, "replay_consistent_content")
        self.assertEqual(self.counts(), before)


class BatchImportTests(EquipmentQualityTestBase):
    def rows(self) -> list[dict]:
        return [
            {"observation_key": f"b-{seq}", "instrument": "gw-1",
             "test_frequency_hz": freq, "response": resp, "noise": 0.01,
             "measured_at": f"2026-10-07T07:0{seq}:00+00:00"}
            for seq, (freq, resp) in enumerate(((450, 0.71), (520, 0.93), (650, 0.84)), start=1)
        ]

    def test_batch_all_or_nothing_on_contract_violation(self) -> None:
        rows = self.rows()
        rows[1]["response"] = float("inf")
        before = self.counts()
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurements(self.admin, "LOT-1", rows)
        failures = ctx.exception.failures
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["index"], 1)
        self.assertEqual(failures[0]["observation_key"], "b-2")
        self.assertEqual(failures[0]["field"], "response")
        self.assertEqual(failures[0]["rule"], "finite")
        self.assertEqual(self.counts(), before)

    def test_batch_success_then_full_replay(self) -> None:
        result = self.service.add_measurements(self.admin, "LOT-1", self.rows())
        self.assertEqual((result["inserted"], result["replayed"]), (3, 0))
        before = self.counts()
        replay = self.service.add_measurements(self.admin, "LOT-1", self.rows())
        self.assertEqual((replay["inserted"], replay["replayed"]), (0, 3))
        self.assertEqual(self.counts(), before)

    def test_batch_conflict_rolls_back_new_rows_too(self) -> None:
        self.service.add_measurements(self.admin, "LOT-1", self.rows())
        before = self.counts()
        tampered = dict(self.rows()[0], response=0.99)
        new_row = {"observation_key": "b-9", "instrument": "gw-1", "test_frequency_hz": 700,
                   "response": 0.5, "noise": 0.01, "measured_at": "2026-10-07T07:09:00+00:00"}
        with self.assertRaises(Conflict):
            self.service.add_measurements(self.admin, "LOT-1", [new_row, tampered])
        self.assertEqual(self.counts(), before)

    def test_batch_duplicate_key_within_payload_rejected(self) -> None:
        rows = self.rows()
        rows[1]["observation_key"] = "b-1"
        before = self.counts()
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurements(self.admin, "LOT-1", rows)
        self.assertEqual(ctx.exception.rule, "unique_in_batch")
        self.assertEqual(self.counts(), before)


class QuarantineTests(EquipmentQualityTestBase):
    def seed_valid_observations(self) -> None:
        for seq, (freq, resp) in enumerate(((450, 0.71), (520, 0.93), (650, 0.84)), start=1):
            self.service.add_measurement(
                self.admin, "LOT-1", freq, resp, 0.01, "gw-1",
                observation_key=f"ok-{seq}", measured_at=f"2026-10-07T07:0{seq}:00+00:00",
            )

    def test_scan_quarantine_and_release_are_auditable(self) -> None:
        self.seed_valid_observations()
        self.seed_legacy_overflow()
        scan = self.service.scan_measurements(self.admin, "LOT-1")
        self.assertEqual(scan["contract_version"], CONTRACT_VERSION)
        self.assertEqual(scan["scanned"], 4)
        bad = [item for item in scan["violations"] if item["measurement_id"] == "legacy-1"]
        self.assertTrue(any(item["field"] == "response" and item["rule"] == "finite" for item in bad))
        self.assertFalse(bad[0]["already_quarantined"])
        # 隔离前，遗留的 Infinity 会让统计失败——即当初的日报事故
        with self.assertRaises(ValueError):
            self.service.analyze(self.admin, "LOT-1")
        record = self.service.quarantine_measurement(
            self.admin, "legacy-1", "井口遥测量程溢出，按数值契约隔离", violation="response.finite"
        )
        self.assertEqual(record["rule_version"], CONTRACT_VERSION)
        row = self.service.db.execute(
            "SELECT * FROM quarantine_records WHERE quarantine_id=?", (record["quarantine_id"],)
        ).fetchone()
        self.assertEqual(row["handled_by"], "admin")
        self.assertEqual(row["reason"], "井口遥测量程溢出，按数值契约隔离")
        self.assertEqual(row["rule_version"], CONTRACT_VERSION)
        self.assertEqual(row["violation"], "response.finite")
        events = self.service.db.execute(
            "SELECT * FROM lot_events WHERE event_type='measurement.quarantined'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        # 分析与报告默认只使用仍然有效的观测
        result = self.service.analyze(self.admin, "LOT-1")
        self.assertEqual(result["excluded_quarantined"], 1)
        self.assertEqual(result["response_profile"]["count"], 3)
        # 重复隔离被拒绝
        with self.assertRaises(Conflict):
            self.service.quarantine_measurement(self.admin, "legacy-1", "重复隔离")
        # 解除隔离同样留痕
        lifted = self.service.release_quarantine(self.admin, record["quarantine_id"], "复核完成")
        self.assertEqual(lifted["status"], "lifted")
        row = self.service.db.execute(
            "SELECT status,lifted_by,lift_reason FROM quarantine_records WHERE quarantine_id=?",
            (record["quarantine_id"],),
        ).fetchone()
        self.assertEqual((row["status"], row["lifted_by"]), ("lifted", "admin"))
        listing = self.service.list_quarantine(self.admin, "LOT-1")
        self.assertEqual(len(listing), 1)

    def test_quarantine_requires_quality_permission(self) -> None:
        self.service.auth.create_user("op", "operator-pass", "operator")
        operator = self.service.auth.login("op", "operator-pass")
        self.seed_legacy_overflow()
        with self.assertRaises(PermissionError):
            self.service.quarantine_measurement(operator, "legacy-1", "无权操作")

    def test_quarantine_unknown_measurement_is_404(self) -> None:
        with self.assertRaises(NotFound):
            self.service.quarantine_measurement(self.admin, "missing", "不存在")


class ApiTests(EquipmentQualityTestBase):
    def test_single_measurement_error_shape(self) -> None:
        status, body = dispatch(
            self.service, "POST", "/lots/LOT-1/measurements", self.admin,
            json.dumps({"observation_key": "a-1", "instrument": "gw-1",
                        "test_frequency_hz": 520, "response": float("inf"), "noise": 0.01}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")
        self.assertEqual(body["error"]["field"], "response")
        self.assertEqual(body["error"]["rule"], "finite")

    def test_batch_endpoint_atomic_with_failure_list(self) -> None:
        payload = {"measurements": [
            {"observation_key": "x-1", "instrument": "gw-1", "test_frequency_hz": 450,
             "response": 0.7, "noise": 0.01, "measured_at": OBSERVED},
            {"observation_key": "x-2", "instrument": "gw-1", "test_frequency_hz": 520,
             "response": "NaN", "measured_at": OBSERVED},
        ]}
        before = self.counts()
        status, body = dispatch(
            self.service, "POST", "/lots/LOT-1/measurements/batch", self.admin,
            json.dumps(payload).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["failures"][0]["field"], "response")
        self.assertEqual(body["error"]["failures"][0]["rule"], "finite")
        self.assertEqual(body["error"]["failures"][0]["observation_key"], "x-2")
        self.assertEqual(self.counts(), before)

    def test_replay_conflict_over_http(self) -> None:
        payload = {"observation_key": "r-1", "instrument": "gw-1", "test_frequency_hz": 520,
                   "response": 0.9, "noise": 0.01, "measured_at": OBSERVED}
        status, _ = dispatch(
            self.service, "POST", "/lots/LOT-1/measurements", self.admin, json.dumps(payload).encode()
        )
        self.assertEqual(status, 201)
        status, body = dispatch(
            self.service, "POST", "/lots/LOT-1/measurements", self.admin,
            json.dumps(dict(payload, response=0.95)).encode(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["rule"], "replay_consistent_content")

    def test_scan_and_quarantine_endpoints(self) -> None:
        self.seed_legacy_overflow()
        status, scan = dispatch(self.service, "GET", "/lots/LOT-1/measurements/scan", self.admin, b"")
        self.assertEqual(status, 200)
        self.assertEqual(scan["violation_count"], 1)
        status, record = dispatch(
            self.service, "POST", "/measurements/legacy-1/quarantine", self.admin,
            json.dumps({"reason": "量程溢出", "violation": "response.finite"}).encode(),
        )
        self.assertEqual(status, 201)
        self.assertEqual(record["rule_version"], CONTRACT_VERSION)
        status, listing = dispatch(self.service, "GET", "/lots/LOT-1/quarantine", self.admin, b"")
        self.assertEqual(status, 200)
        self.assertEqual(listing["quarantine"][0]["handled_by"], "admin")
        status, lifted = dispatch(
            self.service, "POST", f"/quarantine/{record['quarantine_id']}/release", self.admin,
            json.dumps({"reason": "复核完成"}).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(lifted["status"], "lifted")

    def test_analysis_endpoint_excludes_quarantined(self) -> None:
        for seq, (freq, resp) in enumerate(((450, 0.71), (520, 0.93), (650, 0.84)), start=1):
            dispatch(
                self.service, "POST", "/lots/LOT-1/measurements", self.admin,
                json.dumps({"observation_key": f"a-{seq}", "instrument": "gw-1",
                            "test_frequency_hz": freq, "response": resp, "noise": 0.01,
                            "measured_at": f"2026-10-07T07:0{seq}:00+00:00"}).encode(),
            )
        self.seed_legacy_overflow()
        dispatch(
            self.service, "POST", "/measurements/legacy-1/quarantine", self.admin,
            json.dumps({"reason": "溢出"}).encode(),
        )
        status, body = dispatch(self.service, "POST", "/lots/LOT-1/analysis", self.admin, b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(body["excluded_quarantined"], 1)
        self.assertEqual(body["contract_version"], CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
