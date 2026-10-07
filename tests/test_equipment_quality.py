from __future__ import annotations

import json
import sqlite3
import unittest

from equipment_quality import acceptance
from equipment_quality.api import _map_exception
from equipment_quality.contracts import CONTRACT_VERSION, ContractViolation, measurement_digest
from equipment_quality.errors import Conflict, NotFound
from equipment_quality.service import MetricQualityService


RANGES = {
    "frequency_min_hz": 100,
    "frequency_max_hz": 1200,
    "response_min": 0,
    "response_max": 1.5,
    "noise_max": 0.5,
}


def row(measurement_id: str, **overrides) -> dict:
    base = {
        "measurement_id": measurement_id,
        "instrument": "wh-1",
        "measured_at": "2026-10-01T08:00:00+00:00",
        "test_frequency_hz": 450,
        "response": 0.9,
        "noise": 0.01,
    }
    base.update(overrides)
    return base


class EquipmentQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "metric-admin")
        self.service.register_instrument(self.token, "wh-1", "wellhead-telemetry", RANGES)
        self.service.create_lot(self.token, "LOT-1", "wellhead-sensor", "REV-1", 10)

    def counts(self) -> tuple[int, int]:
        measurements = self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0]
        events = self.service.db.execute("SELECT count(*) FROM lot_events").fetchone()[0]
        return measurements, events

    def test_rejects_non_finite_and_non_numeric_values(self) -> None:
        bad_values = [float("inf"), float("-inf"), float("nan"), "Infinity", "abc", True, None]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ContractViolation) as ctx:
                    self.service.import_measurements(self.token, "LOT-1", [row("m-x", response=value)])
                issue = ctx.exception.issues[0]
                self.assertEqual(issue["field"], "response")
                self.assertIn(issue["rule"], {"finite", "numeric_string", "numeric_type", "required"})
        self.assertEqual(self.counts(), (0, 1))  # 只有批次创建事件

    def test_rejects_each_numeric_field_with_field_and_rule(self) -> None:
        for field in ("test_frequency_hz", "response", "noise"):
            with self.subTest(field=field):
                with self.assertRaises(ContractViolation) as ctx:
                    self.service.import_measurements(self.token, "LOT-1", [row("m-x", **{field: float("inf")})])
                self.assertEqual(ctx.exception.issues[0]["field"], field)
                self.assertEqual(ctx.exception.issues[0]["rule"], "finite")
        self.assertEqual(self.counts(), (0, 1))

    def test_rejects_values_outside_device_range(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            self.service.import_measurements(self.token, "LOT-1", [row("m-x", response=1.6)])
        issue = ctx.exception.issues[0]
        self.assertEqual((issue["field"], issue["rule"]), ("response", "device_range"))
        self.assertIn("[0.0, 1.5]", issue["message"])
        with self.assertRaises(ContractViolation) as ctx:
            self.service.import_measurements(self.token, "LOT-1", [row("m-y", noise=0.9)])
        self.assertEqual(ctx.exception.issues[0]["rule"], "device_range")
        with self.assertRaises(ContractViolation) as ctx:
            self.service.import_measurements(self.token, "LOT-1", [row("m-z", test_frequency_hz=0)])
        self.assertEqual(ctx.exception.issues[0]["rule"], "positive")

    def test_rejects_unknown_instrument_and_bad_time(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            self.service.import_measurements(self.token, "LOT-1", [row("m-x", instrument="ghost")])
        self.assertEqual(ctx.exception.issues[0]["rule"], "instrument_unknown")
        for bad_time in ("2026-10-01 08:00:00", "not-a-time", "2999-01-01T00:00:00+00:00", None):
            with self.subTest(measured_at=bad_time):
                with self.assertRaises(ContractViolation) as ctx:
                    self.service.import_measurements(self.token, "LOT-1", [row("m-x", measured_at=bad_time)])
                self.assertEqual(ctx.exception.issues[0]["field"], "measured_at")
        self.assertEqual(self.counts(), (0, 1))

    def test_numeric_strings_are_accepted(self) -> None:
        result = self.service.import_measurements(
            self.token, "LOT-1", [row("m-s", response="0.93", noise="0.01")]
        )
        self.assertEqual(result["inserted"], 1)
        stored = self.service.db.execute("SELECT response FROM measurements WHERE measurement_id='m-s'").fetchone()
        self.assertAlmostEqual(stored[0], 0.93)

    def test_idempotent_replay_and_conflicting_replay(self) -> None:
        first = self.service.import_measurements(self.token, "LOT-1", [row("m-1")])
        self.assertEqual(first["inserted"], 1)
        second = self.service.import_measurements(self.token, "LOT-1", [row("m-1")])
        self.assertEqual((second["inserted"], second["replayed"]), (0, 1))
        self.assertEqual(self.counts(), (1, 2))  # 重放不产生新业务或审计记录
        with self.assertRaises(Conflict):
            self.service.import_measurements(self.token, "LOT-1", [row("m-1", response=0.5)])
        self.assertEqual(self.counts(), (1, 2))
        # 同一时刻的不同时区写法视为同一内容
        replay = self.service.import_measurements(
            self.token, "LOT-1", [row("m-1", measured_at="2026-10-01T16:00:00+08:00")]
        )
        self.assertEqual(replay["replayed"], 1)

    def test_batch_import_is_all_or_nothing(self) -> None:
        rows = [row("m-1"), row("m-2"), row("m-3", response=float("inf"))]
        with self.assertRaises(ContractViolation):
            self.service.import_measurements(self.token, "LOT-1", rows)
        self.assertEqual(self.counts(), (0, 1))  # 一条非法则整批不生效
        result = self.service.import_measurements(self.token, "LOT-1", rows[:2])
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(self.counts(), (2, 3))

    def test_batch_rejects_inconsistent_identifier_within_batch(self) -> None:
        with self.assertRaises(Conflict):
            self.service.import_measurements(
                self.token, "LOT-1", [row("m-1"), row("m-1", response=0.4)]
            )
        self.assertEqual(self.counts(), (0, 1))

    def test_single_import_shares_contract_and_atomicity(self) -> None:
        with self.assertRaises(ContractViolation):
            self.service.add_measurement(
                self.token, "LOT-1", 450, float("inf"), 0.01, "wh-1",
                measurement_id="m-1", measured_at="2026-10-01T08:00:00+00:00",
            )
        self.assertEqual(self.counts(), (0, 1))
        ok = self.service.add_measurement(
            self.token, "LOT-1", 450, 0.9, 0.01, "wh-1",
            measurement_id="m-1", measured_at="2026-10-01T08:00:00+00:00",
        )
        self.assertEqual(ok["measurement_id"], "m-1")
        self.assertFalse(ok["replayed"])

    def _seed_valid_rows(self, count: int) -> None:
        self.service.import_measurements(
            self.token, "LOT-1", [row(f"m-{index}") for index in range(1, count + 1)]
        )

    def test_scan_quarantine_and_release_are_auditable(self) -> None:
        self._seed_valid_rows(4)
        # 模拟契约建立前已进入库中的量程溢出 Infinity
        self.service.db.execute("UPDATE measurements SET response=? WHERE measurement_id='m-4'", (float("inf"),))
        self.service.db.commit()

        scan = self.service.scan_measurements(self.token, "LOT-1")
        self.assertEqual(scan["rule_version"], CONTRACT_VERSION)
        self.assertEqual(scan["scanned"], 4)
        self.assertEqual(len(scan["violations"]), 1)
        violation = scan["violations"][0]
        self.assertEqual(violation["measurement_id"], "m-4")
        self.assertEqual(violation["issues"][0]["field"], "response")
        self.assertEqual(violation["issues"][0]["rule"], "finite")

        quarantined = self.service.quarantine_measurement(self.token, "m-4", "井口遥测量程溢出,按设备故障处置")
        self.assertEqual(quarantined["rule_version"], CONTRACT_VERSION)
        record = self.service.list_quarantines(self.token, "LOT-1")[0]
        self.assertEqual(record["handled_by"], "admin")
        self.assertEqual(record["reason"], "井口遥测量程溢出,按设备故障处置")
        self.assertEqual(record["rule_version"], CONTRACT_VERSION)
        self.assertEqual(record["status"], "active")

        with self.assertRaises(Conflict):
            self.service.quarantine_measurement(self.token, "m-4", "重复隔离")
        with self.assertRaises(NotFound):
            self.service.quarantine_measurement(self.token, "m-404", "不存在")

        released = self.service.release_quarantine(self.token, "m-4", "复核后维持排除,转由分析过滤")
        self.assertEqual(released["status"], "released")
        record = self.service.list_quarantines(self.token, "LOT-1")[0]
        self.assertEqual(record["released_by"], "admin")

        event_types = [e["event_type"] for e in self.service.audit(self.token, "LOT-1")]
        self.assertIn("measurement.quarantined", event_types)
        self.assertIn("measurement.quarantine_released", event_types)

    def test_analyze_uses_only_valid_traceable_observations(self) -> None:
        self._seed_valid_rows(5)
        self.service.db.execute("UPDATE measurements SET response=? WHERE measurement_id='m-5'", (float("inf"),))
        self.service.db.commit()
        self.service.quarantine_measurement(self.token, "m-4", "现场记录失效")

        result = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(result["measurements_used"], ["m-1", "m-2", "m-3"])
        reasons = {item["measurement_id"]: item["reason"] for item in result["measurements_excluded"]}
        self.assertEqual(reasons, {"m-4": "quarantined", "m-5": "contract_violation"})
        self.assertEqual(result["rule_version"], CONTRACT_VERSION)

        included = self.service.analyze(self.token, "LOT-1", include_quarantined=True)
        self.assertEqual(included["measurements_used"], ["m-1", "m-2", "m-3", "m-4"])

    def test_analyze_reports_when_valid_rows_are_insufficient(self) -> None:
        self._seed_valid_rows(3)
        self.service.quarantine_measurement(self.token, "m-1", "待复核")
        with self.assertRaises(ValueError):
            self.service.analyze(self.token, "LOT-1")
        released = self.service.release_quarantine(self.token, "m-1", "复核通过")
        self.assertEqual(released["status"], "released")
        self.assertEqual(len(self.service.analyze(self.token, "LOT-1")["measurements_used"]), 3)

    def test_quarantine_requires_permission(self) -> None:
        self._seed_valid_rows(1)
        self.service.auth.create_user("op", "password123", "operator")
        operator_token = self.service.auth.login("op", "password123")
        with self.assertRaises(PermissionError):
            self.service.quarantine_measurement(operator_token, "m-1", "越权")

    def test_event_stream_contains_only_strict_json(self) -> None:
        self._seed_valid_rows(3)
        payloads = self.service.db.execute("SELECT payload FROM lot_events").fetchall()
        for (payload,) in payloads:
            json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(AssertionError(value)))

    def test_error_mapping_points_to_field_and_rule(self) -> None:
        try:
            self.service.import_measurements(self.token, "LOT-1", [row("m-x", response=float("inf"))])
        except ContractViolation as exc:
            status, body = _map_exception(exc)
        self.assertEqual(status, 422)
        issue = body["error"]["issues"][0]
        self.assertEqual((issue["field"], issue["rule"]), ("response", "finite"))

    def test_acceptance_smoke(self) -> None:
        result = acceptance.run()
        self.assertEqual(result["status"], "ok")


class MigrationTests(unittest.TestCase):
    def test_existing_database_is_migrated_and_scanned(self) -> None:
        path = self.mktemp()
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE metric_batches(
             lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
             sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE measurements(
             measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES metric_batches(lot_id),
             test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
             instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
             UNIQUE(lot_id,measurement_id));
            CREATE TABLE lot_events(
             event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
             event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE approvals(
             lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
             reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
            """
        )
        legacy.execute(
            "INSERT INTO metric_batches VALUES('LOT-OLD','p','r',10,'engineering','op','2026-10-01','2026-10-01')"
        )
        legacy.execute(
            "INSERT INTO measurements VALUES('legacy-1','LOT-OLD',450,?,0.01,'wh-1','op','2026-10-01T08:00:00+00:00')",
            (float("inf"),),
        )
        legacy.commit()
        legacy.close()

        service = MetricQualityService(path)
        service.bootstrap_admin()
        token = service.auth.login("admin", "metric-admin")
        row = service.db.execute("SELECT content_sha256 FROM measurements WHERE measurement_id='legacy-1'").fetchone()
        self.assertEqual(
            row[0],
            measurement_digest("wh-1", "2026-10-01T08:00:00+00:00", 450.0, float("inf"), 0.01),
        )
        scan = service.scan_measurements(token, "LOT-OLD")
        self.assertEqual(len(scan["violations"]), 1)
        quarantined = service.quarantine_measurement(token, "legacy-1", "历史量程溢出")
        self.assertEqual(quarantined["status"], "active")

    def mktemp(self) -> str:
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".sqlite3")
        import os

        os.close(handle)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path


if __name__ == "__main__":
    unittest.main()
