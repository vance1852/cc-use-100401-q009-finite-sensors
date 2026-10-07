"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contracts import ContractViolation
from .errors import Conflict, InvalidState, NotFound, ServiceError
from .service import MetricQualityService


def _error(status: int, code: str, message: str, **extra: object) -> tuple[int, dict]:
    body = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    return status, body


def _map_exception(exc: Exception) -> tuple[int, dict]:
    if isinstance(exc, ContractViolation):
        return _error(422, exc.code, str(exc), issues=exc.issues)
    if isinstance(exc, NotFound):
        return _error(404, exc.code, str(exc))
    if isinstance(exc, (Conflict, InvalidState)):
        return _error(409, exc.code, str(exc))
    if isinstance(exc, PermissionError):
        return _error(403, "forbidden", str(exc))
    if isinstance(exc, ServiceError):
        return _error(400, exc.code, str(exc))
    return _error(400, "bad_request", str(exc))


class Handler(BaseHTTPRequestHandler):
    service = MetricQualityService()
    _dispatch_lock = threading.Lock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self):
        with self._dispatch_lock:
            return self._get()

    def do_POST(self):
        with self._dispatch_lock:
            return self._post()

    def _get(self):
        try:
            if self.path == "/health":
                return self._json(200, {"status": "ok", "service": "equipment-quality"})
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(self._token(), parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "violations":
                return self._json(200, self.service.scan_measurements(self._token(), parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "quarantines":
                return self._json(200, {"quarantines": self.service.list_quarantines(self._token(), parts[1])})
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return self._json(200, {"events": self.service.audit(self._token(), parts[1])})
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        except Exception as exc:
            status, body = _map_exception(exc)
            return self._json(status, body)

    def _post(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["sample_count"]))
            if self.path == "/instruments":
                return self._json(201, self.service.register_instrument(token, body.get("instrument_id"), body.get("point_type"), body.get("ranges", {})))
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token,
                    parts[1],
                    body.get("test_frequency_hz"),
                    body.get("response"),
                    body.get("noise"),
                    body.get("instrument"),
                    measurement_id=body.get("measurement_id"),
                    measured_at=body.get("measured_at"),
                ))
            if len(parts) == 4 and parts[0] == "lots" and parts[2] == "measurements" and parts[3] == "batch":
                return self._json(201, self.service.import_measurements(token, parts[1], body.get("measurements", [])))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if len(parts) == 3 and parts[0] == "measurements" and parts[2] == "quarantine":
                return self._json(201, self.service.quarantine_measurement(token, parts[1], body.get("reason", "")))
            if len(parts) == 3 and parts[0] == "measurements" and parts[2] == "quarantine-release":
                return self._json(200, self.service.release_quarantine(token, parts[1], body.get("reason", "")))
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        except json.JSONDecodeError as exc:
            return self._json(422, {"error": {"code": "invalid_json", "message": str(exc)}})
        except Exception as exc:
            status, body = _map_exception(exc)
            return self._json(status, body)


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
