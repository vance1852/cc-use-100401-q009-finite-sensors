"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .contracts import CONTRACT_VERSION
from .errors import ValidationFailed
from .service import MetricQualityService
from .storage import utcnow


def run() -> dict:
    service = MetricQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "metric-admin")
    service.create_lot(token, "BATCH-DEMO", "cross-border-service-index", "POLICY-3.2", 10)
    service.register_instrument(
        token, "reporting-gateway-1", "wellhead_telemetry", "1", "5000", "-5", "5", "1"
    )
    for seq, (report_period, response) in enumerate(((450, .71), (520, .93), (650, .84)), start=1):
        service.add_measurement(
            token, "BATCH-DEMO", report_period, response, .01, "reporting-gateway-1",
            observation_key=f"demo-obs-{seq}", measured_at=f"2026-10-06T0{seq}:00:00+00:00",
        )
    # 数值契约：把量程溢出写成 Infinity 的遥测在持久化之前被拒绝，不留下任何记录
    rejected = None
    try:
        service.add_measurement(
            token, "BATCH-DEMO", 520, float("inf"), .01, "reporting-gateway-1",
            observation_key="demo-obs-overflow",
        )
    except ValidationFailed as exc:
        rejected = {"field": exc.field, "rule": exc.rule}
    # 同一观测标识、内容一致的重放是幂等的，不产生新记录
    replay = service.add_measurement(
        token, "BATCH-DEMO", 450, .71, .01, "reporting-gateway-1",
        observation_key="demo-obs-1", measured_at="2026-10-06T01:00:00+00:00",
    )
    # 模拟契约上线前已经进入测量表的遗留溢出值，走"识别-隔离"流程
    service.db.execute(
        "INSERT INTO measurements(measurement_id,lot_id,test_frequency_hz,response,noise,"
        "instrument,operator,measured_at) VALUES(?,?,?,?,?,?,?,?)",
        ("legacy-overflow", "BATCH-DEMO", 520.0, float("inf"), 0.01,
         "reporting-gateway-1", "admin", utcnow()),
    )
    service.db.commit()
    scan = service.scan_measurements(token, "BATCH-DEMO")
    legacy = [item for item in scan["violations"] if item["measurement_id"] == "legacy-overflow"]
    quarantine = service.quarantine_measurement(
        token, "legacy-overflow", "历史量程溢出，按数值契约隔离", violation="response.finite"
    )
    result = service.analyze(token, "BATCH-DEMO")
    service.approve(token, "BATCH-DEMO", "hold", "awaiting data quality review")
    return {
        "status": "ok",
        "batch": result["lot_id"],
        "peak_period": result["response_profile"]["peak_test_frequency_hz"],
        "events": len(service.audit(token, "BATCH-DEMO")),
        "contract_version": CONTRACT_VERSION,
        "rejected": rejected,
        "replayed": replay["replayed"],
        "legacy_violations": len(legacy),
        "quarantine_id": quarantine["quarantine_id"],
        "excluded_quarantined": result["excluded_quarantined"],
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
