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
        route = {
            "/v1/journal-entries/validate": self.service.validate_journal_entry,
            "/v1/chart-of-accounts/validate": self.service.validate_chart_of_accounts,
            "/v1/trial-balances/generate": self.service.generate_trial_balance,
            "/v1/financial-statements/generate": self.service.generate_financial_statements,
            "/v1/cash-flow-statements/generate": self.service.generate_cash_flow_statement,
            "/v1/period-closes/generate": self.service.generate_period_close,
            "/v1/recognition-schedules/generate": self.service.generate_recognition_schedule,
            "/v1/depreciation-schedules/generate": self.service.generate_depreciation_schedule,
            "/v1/asset-impairments/generate": self.service.generate_asset_impairment,
            "/v1/foreign-currency-remeasurements/generate": self.service.generate_foreign_currency_remeasurement,
        }.get(self.path)
        if route is None:
            self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})
            return
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self.send_json(
                415,
                {"error": {"code": "unsupported_media_type", "message": "expected application/json"}},
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(length, 0))
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.send_json(400, {"error": {"code": "invalid_json", "message": "request body is not valid JSON"}})
            return
        if not isinstance(payload, dict):
            self.send_json(
                400,
                {"error": {"code": "request_not_object", "message": "request body must be a JSON object"}},
            )
            return
        status, body = route(payload)
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
