"""HTTP entry point for LedgerLens."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Service


def env_address() -> tuple[str, int]:
    raw = os.environ.get("LEDGERLENS_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid LEDGERLENS_ADDR: {raw!r}")
    return host, int(port)


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path == "/v1/journal-entries/validate":
            handler = self.service.validate_journal_entry
        elif self.path == "/v1/chart-of-accounts/validate":
            handler = self.service.validate_chart_of_accounts
        else:
            self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})
            return

        payload = self._read_json_object()
        if isinstance(payload, tuple):
            status, body = payload
            self.send_json(status, body)
            return
        status, body = handler(payload)
        self.send_json(status, body)

    def _read_json_object(self) -> dict | tuple[int, dict]:
        """读取并解析 JSON 对象请求体；失败时返回 (状态码, 错误体)。"""
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return 415, {
                "error": {"code": "unsupported_media_type", "message": "expected application/json"}
            }
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(length, 0))
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return 400, {
                "error": {"code": "invalid_json", "message": "request body is not valid JSON"}
            }
        if not isinstance(payload, dict):
            return 400, {
                "error": {
                    "code": "request_not_object",
                    "message": "request body must be a JSON object",
                }
            }
        return payload

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging so recorded output stays stable."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="ledgerlens.server", description="财务报表与会计分析引擎")
    host, port = env_address()
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"LedgerLens listening on http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
