"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import EquipmentError, ValidationFailed
from .service import MetricQualityService


def dispatch(
    service: MetricQualityService, method: str, path: str, token: str, body: bytes
) -> tuple[int, dict]:
    """路由入口，HTTP 处理器与单元测试共用。

    请求体中的 Infinity/NaN 常量在此保留为浮点值，交由数值契约按字段拒绝，
    以便错误响应指出具体字段与规则。
    """

    try:
        payload: dict = {}
        if body:
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValidationFailed("请求体必须是 JSON 对象")
            payload = parsed
        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "service": "equipment-quality"}
        if method == "POST" and path == "/login":
            return 200, {"token": service.auth.login(payload["user_id"], payload["password"])}
        if method == "POST" and path == "/lots":
            return 201, service.create_lot(
                token, payload["lot_id"], payload["product"], payload["process_rev"],
                int(payload["sample_count"]),
            )
        if method == "POST" and path == "/instruments":
            return 201, service.register_instrument(
                token, payload["instrument_id"], payload["point_type"],
                payload["frequency_min_hz"], payload["frequency_max_hz"],
                payload["response_min"], payload["response_max"], payload["noise_max"],
            )
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "lots":
            lot_id = parts[1]
            if method == "GET" and len(parts) == 2:
                return 200, service.get_lot(token, lot_id)
            if method == "POST" and len(parts) == 3 and parts[2] == "measurements":
                return 201, service.add_measurement(
                    token, lot_id, payload.get("test_frequency_hz"), payload.get("response"),
                    payload.get("noise", 0.0), payload.get("instrument"),
                    observation_key=payload.get("observation_key"),
                    measured_at=payload.get("measured_at"),
                )
            if method == "POST" and len(parts) == 4 and parts[2] == "measurements" and parts[3] == "batch":
                return 201, service.add_measurements(token, lot_id, payload.get("measurements"))
            if method == "GET" and len(parts) == 4 and parts[2] == "measurements" and parts[3] == "scan":
                return 200, service.scan_measurements(token, lot_id)
            if method == "POST" and len(parts) == 3 and parts[2] == "analysis":
                return 200, service.analyze(
                    token, lot_id, include_quarantined=bool(payload.get("include_quarantined", False))
                )
            if method == "GET" and len(parts) == 3 and parts[2] == "quarantine":
                return 200, {"quarantine": service.list_quarantine(token, lot_id)}
            if method == "GET" and len(parts) == 3 and parts[2] == "audit":
                return 200, {"events": service.audit(token, lot_id)}
        if method == "POST" and len(parts) == 3 and parts[0] == "measurements" and parts[2] == "quarantine":
            return 201, service.quarantine_measurement(
                token, parts[1], payload.get("reason", ""), payload.get("violation", "manual")
            )
        if method == "POST" and len(parts) == 3 and parts[0] == "quarantine" and parts[2] == "release":
            return 200, service.release_quarantine(token, int(parts[1]), payload.get("reason", ""))
        return 404, {"error": {"code": "route_not_found", "message": "接口不存在"}}
    except EquipmentError as exc:
        return exc.status, {"error": {"code": exc.code, "message": str(exc), **exc.details()}}
    except PermissionError as exc:
        return 403, {"error": {"code": "forbidden", "message": str(exc)}}
    except KeyError as exc:
        return 404, {"error": {"code": "not_found", "message": str(exc)}}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return 422, {"error": {"code": "validation_failed", "message": f"请求体不是合法 JSON: {exc}"}}
    except (TypeError, ValueError) as exc:
        return 400, {"error": {"code": "invalid_request", "message": str(exc)}}


class Handler(BaseHTTPRequestHandler):
    service = MetricQualityService()
    _service_lock = threading.Lock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self) -> None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        with self._service_lock:
            status, payload = dispatch(
                self.service, self.command, urlparse(self.path).path, token, body
            )
        self._json(status, payload)

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def log_message(self, format, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = MetricQualityService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
