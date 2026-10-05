import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler


class HttpEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method: str, path: str, body=None, content_type=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            if content_type is not None:
                headers["Content-Type"] = content_type
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_healthz_unchanged(self) -> None:
        status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "ledgerlens")

    def test_unknown_get_still_404(self) -> None:
        status, payload = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_validate_endpoint_accepts_balanced_entry(self) -> None:
        body = {
            "voucher_id": "JV-1",
            "posting_date": "2026-10-03",
            "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1001", "debit": "10", "credit": "0"},
                {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "10.00"},
            ],
        }
        status, payload = self.request(
            "POST", "/v1/journal-entries/validate", body, "application/json; charset=utf-8"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"valid": True, "debit_total": "10.00", "credit_total": "10.00"})

    def test_unsupported_media_type(self) -> None:
        status, payload = self.request(
            "POST", "/v1/journal-entries/validate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

    def test_invalid_json(self) -> None:
        status, payload = self.request(
            "POST", "/v1/journal-entries/validate", "{not json", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_request_not_object(self) -> None:
        for body in ("[1,2]", '"x"', "42"):
            status, payload = self.request(
                "POST", "/v1/journal-entries/validate", body, "application/json"
            )
            self.assertEqual(status, 400, body)
            self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_unknown_post_path_404(self) -> None:
        status, payload = self.request("POST", "/v1/other", {}, "application/json")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_chart_endpoint_accepts_valid_chart(self) -> None:
        body = {
            "chart_id": "COA-1",
            "effective_date": "2026-10-03",
            "accounts": [
                {
                    "code": "1000",
                    "name": "Assets",
                    "type": "asset",
                    "normal_balance": "debit",
                    "active": True,
                    "parent_code": None,
                }
            ],
        }
        status, payload = self.request(
            "POST", "/v1/chart-of-accounts/validate", body, "application/json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["chart_id"], "COA-1")
        self.assertEqual(payload["effective_date"], "2026-10-03")
        self.assertEqual(payload["account_count"], 1)
        self.assertEqual(payload["root_count"], 1)
        self.assertEqual(
            payload["type_counts"],
            {"asset": 1, "liability": 0, "equity": 0, "revenue": 0, "expense": 0},
        )

    def test_chart_endpoint_422_on_validation_error(self) -> None:
        status, payload = self.request(
            "POST",
            "/v1/chart-of-accounts/validate",
            {"chart_id": "c", "effective_date": "2026-10-03", "accounts": []},
            "application/json",
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/accounts", "too_few_accounts"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )

    def test_chart_endpoint_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "POST", "/v1/chart-of-accounts/validate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "POST", "/v1/chart-of-accounts/validate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "POST", "/v1/chart-of-accounts/validate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_deferred_tax_endpoint_success(self) -> None:
        body = {
            "report_date": "2026-06-30",
            "tax_rates": [
                {"effective_from": "2026-01-01", "effective_to": "2026-12-31", "rate": "0.25"}
            ],
            "items": [
                {
                    "item_id": "a1",
                    "nature": "asset",
                    "carrying_amount": "1200.00",
                    "tax_base": "1000.00",
                    "expected_reversal_date": "2026-06-30",
                    "attribution": "profit_or_loss",
                }
            ],
        }
        status, payload = self.request(
            "POST", "/deferred-tax/calculate", body, "application/json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_deferred_tax_liability"], "50.00")
        self.assertEqual(payload["items"][0]["tax_rate"], "0.25")

    def test_deferred_tax_empty_items_is_zero_summary(self) -> None:
        body = {
            "report_date": "2026-06-30",
            "tax_rates": [
                {"effective_from": "2026-01-01", "effective_to": "2026-12-31", "rate": "0.25"}
            ],
            "items": [],
        }
        status, payload = self.request(
            "POST", "/deferred-tax/calculate", body, "application/json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["net_deferred_tax"], "0.00")

    def test_deferred_tax_400_with_stable_code_and_item_path(self) -> None:
        body = {
            "report_date": "2026-06-30",
            "tax_rates": [
                {"effective_from": "2026-01-01", "effective_to": "2026-12-31", "rate": "0.25"}
            ],
            "items": [
                {
                    "item_id": "a1",
                    "nature": "asset",
                    "carrying_amount": "1200.00",
                    "tax_base": "1000.00",
                    "expected_reversal_date": "2025-06-30",
                    "attribution": "profit_or_loss",
                }
            ],
        }
        status, payload = self.request(
            "POST", "/deferred-tax/calculate", body, "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "no_matching_tax_rate")
        self.assertEqual(
            payload["error"]["path"], "/items/0/expected_reversal_date"
        )
        self.assertNotIn("items", payload)

    def test_deferred_tax_400_bracket_path(self) -> None:
        body = {
            "report_date": "2026-06-30",
            "tax_rates": [
                {"effective_from": "2027-01-01", "effective_to": "2026-12-31", "rate": "0.25"}
            ],
            "items": [],
        }
        status, payload = self.request(
            "POST", "/deferred-tax/calculate", body, "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_tax_rate_period")
        self.assertEqual(payload["error"]["path"], "/tax_rates/0/effective_from")

    def test_deferred_tax_media_json_and_parse_errors(self) -> None:
        status, payload = self.request(
            "POST", "/deferred-tax/calculate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "POST", "/deferred-tax/calculate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "POST", "/deferred-tax/calculate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_deferred_tax_unknown_route_unchanged(self) -> None:
        status, payload = self.request(
            "POST", "/deferred-tax/other", {}, "application/json"
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
