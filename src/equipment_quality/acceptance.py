"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import MetricQualityService


def run() -> dict:
    service = MetricQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "metric-admin")
    service.register_instrument(
        token,
        "reporting-gateway-1",
        "wellhead-telemetry",
        {
            "frequency_min_hz": 100,
            "frequency_max_hz": 1200,
            "response_min": 0,
            "response_max": 1.5,
            "noise_max": 0.5,
        },
    )
    service.create_lot(token, "BATCH-DEMO", "cross-border-service-index", "POLICY-3.2", 10)
    observations = (
        ("BATCH-DEMO-001", "2026-10-06T08:00:00+00:00", 450, .71),
        ("BATCH-DEMO-002", "2026-10-06T08:05:00+00:00", 520, .93),
        ("BATCH-DEMO-003", "2026-10-06T08:10:00+00:00", 650, .84),
    )
    for measurement_id, measured_at, report_period, response in observations:
        service.add_measurement(
            token,
            "BATCH-DEMO",
            report_period,
            response,
            .01,
            "reporting-gateway-1",
            measurement_id=measurement_id,
            measured_at=measured_at,
        )
    result = service.analyze(token, "BATCH-DEMO")
    service.approve(token, "BATCH-DEMO", "hold", "awaiting data quality review")
    return {"status": "ok", "batch": result["lot_id"], "peak_period": result["response_profile"]["peak_test_frequency_hz"], "events": len(service.audit(token, "BATCH-DEMO"))}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
