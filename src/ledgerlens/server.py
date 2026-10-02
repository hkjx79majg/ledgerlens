"""HTTP entry point for LedgerLens."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Service

VALIDATE_PATH = "/v1/journal-entries/validate"


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

    def send_error_json(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"error": {"code": code, "message": message}})

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path != VALIDATE_PATH:
            self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})
            return

        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self.send_error_json(
                415,
                "unsupported_media_type",
                "Content-Type 必须为 application/json",
            )
            return

        length = self.headers.get("Content-Length")
        try:
            raw = self.rfile.read(int(length)) if length is not None else b""
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error_json(400, "invalid_json", "请求体不是合法的 JSON")
            return

        if not isinstance(payload, dict):
            self.send_error_json(
                400, "request_not_object", "请求顶层必须为 JSON 对象"
            )
            return

        status, body = self.service.validate_journal_entry(payload)
        self.send_json(status, body)

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
