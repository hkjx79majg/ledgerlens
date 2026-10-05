import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.dcf import calculate_dcf_valuation
from ledgerlens.server import Handler
from ledgerlens.service import Service


def request_body(**overrides):
    body = {
        "currency": "USD",
        "discount_rate": "0.1",
        "terminal_growth_rate": "0.02",
        "forecast_cash_flows": [
            {"period": 1, "free_cash_flow": "100"},
            {"period": 2, "free_cash_flow": "110"},
        ],
        "net_debt": "50",
    }
    body.update(overrides)
    return body


class DcfValuationTest(unittest.TestCase):
    def test_service_exposes_method(self) -> None:
        self.assertTrue(hasattr(Service(), "calculate_dcf_valuation"))

    def test_success_two_periods(self) -> None:
        status, body = calculate_dcf_valuation(request_body())
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "valid": True,
                "currency": "USD",
                "discount_rate": "0.1",
                "terminal_growth_rate": "0.02",
                "net_debt": "50.00",
                "periods": [
                    {
                        "period": 1,
                        "free_cash_flow": "100",
                        "discount_factor": "0.90909091",
                        "present_value": "90.91",
                    },
                    {
                        "period": 2,
                        "free_cash_flow": "110",
                        "discount_factor": "0.82644628",
                        "present_value": "90.91",
                    },
                ],
                "forecast_present_value_total": "181.82",
                "terminal_value": "1402.50",
                "terminal_present_value": "1159.09",
                "enterprise_value": "1340.91",
                "equity_value": "1290.91",
            },
        )

    def test_periods_sorted_ascending_in_output(self) -> None:
        body = request_body(
            forecast_cash_flows=[
                {"period": 2, "free_cash_flow": "110"},
                {"period": 1, "free_cash_flow": "100"},
            ]
        )
        status, payload = calculate_dcf_valuation(body)
        self.assertEqual(status, 200)
        self.assertEqual([p["period"] for p in payload["periods"]], [1, 2])
        self.assertEqual(payload["equity_value"], "1290.91")

    def test_negative_net_debt_is_net_cash(self) -> None:
        status, payload = calculate_dcf_valuation(request_body(net_debt="-25.50"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["net_debt"], "-25.50")
        self.assertEqual(payload["equity_value"], "1366.41")

    def test_negative_terminal_growth_allowed(self) -> None:
        status, payload = calculate_dcf_valuation(
            request_body(terminal_growth_rate="-0.02")
        )
        self.assertEqual(status, 200)
        # TV = 110 * 0.98 / 0.12 = 898.3333... -> 898.33
        self.assertEqual(payload["terminal_value"], "898.33")

    def test_negative_zero_eliminated(self) -> None:
        status, payload = calculate_dcf_valuation(
            request_body(
                terminal_growth_rate="0",
                forecast_cash_flows=[{"period": 1, "free_cash_flow": "-0.00"}],
                net_debt="0",
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["periods"][0]["present_value"], "0.00")
        self.assertEqual(payload["forecast_present_value_total"], "0.00")
        self.assertEqual(payload["terminal_value"], "0.00")
        self.assertEqual(payload["terminal_present_value"], "0.00")
        self.assertEqual(payload["enterprise_value"], "0.00")
        self.assertEqual(payload["equity_value"], "0.00")

    def test_rounding_half_up(self) -> None:
        # 1 / 1.005 = 0.995024875... -> factor 0.99502488 (HALF_UP at 8dp)
        status, payload = calculate_dcf_valuation(
            request_body(
                discount_rate="0.005",
                terminal_growth_rate="0",
                forecast_cash_flows=[{"period": 1, "free_cash_flow": "0.05"}],
                net_debt="0",
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["periods"][0]["discount_factor"], "0.99502488")
        # 0.05 / 1.005 = 0.0497512... -> 0.05
        self.assertEqual(payload["periods"][0]["present_value"], "0.05")

    def test_deterministic_and_input_not_mutated(self) -> None:
        body = request_body()
        snapshot = json.loads(json.dumps(body))
        first = calculate_dcf_valuation(body)
        second = calculate_dcf_valuation(body)
        self.assertEqual(first, second)
        self.assertEqual(body, snapshot)

    # ---- 错误路径 ----

    def errors(self, body):
        status, payload = calculate_dcf_valuation(body)
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        return payload["errors"]

    def test_unknown_top_level_field(self) -> None:
        errors = self.errors(request_body(extra="x"))
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/extra", "unknown_field")],
        )

    def test_missing_fields_report_required(self) -> None:
        errors = self.errors({})
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [
                ("/currency", "required"),
                ("/discount_rate", "required"),
                ("/forecast_cash_flows", "required"),
                ("/net_debt", "required"),
                ("/terminal_growth_rate", "required"),
            ],
        )

    def test_invalid_currency(self) -> None:
        for bad in ("usd", "US", "USDD", "U1D", ""):
            errors = self.errors(request_body(currency=bad))
            self.assertIn(
                ("/currency", "invalid_currency"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )

    def test_invalid_currency_type(self) -> None:
        errors = self.errors(request_body(currency=12))
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors], [("/currency", "invalid_type")]
        )

    def test_invalid_discount_rate(self) -> None:
        for bad in ("0", "-0.1", "1.00000001", "1.5", "abc", "1e-1", "", ".5", "0.123456789"):
            errors = self.errors(request_body(discount_rate=bad))
            self.assertIn(
                ("/discount_rate", "invalid_rate"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )

    def test_discount_rate_boundary_one_allowed(self) -> None:
        status, _ = calculate_dcf_valuation(request_body(discount_rate="1"))
        self.assertEqual(status, 200)

    def test_invalid_terminal_growth_rate(self) -> None:
        for bad in ("-1", "-1.5", "abc", "0.123456789", ""):
            errors = self.errors(request_body(terminal_growth_rate=bad))
            self.assertIn(
                ("/terminal_growth_rate", "invalid_rate"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )

    def test_terminal_growth_must_be_less_than_discount(self) -> None:
        errors = self.errors(
            request_body(discount_rate="0.1", terminal_growth_rate="0.1")
        )
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/terminal_growth_rate", "terminal_growth_not_less_than_discount_rate")],
        )

    def test_terminal_growth_comparison_skipped_when_rate_invalid(self) -> None:
        errors = self.errors(
            request_body(discount_rate="abc", terminal_growth_rate="0.5")
        )
        codes = {(e["path"], e["code"]) for e in errors}
        self.assertIn(("/discount_rate", "invalid_rate"), codes)
        self.assertNotIn(
            ("/terminal_growth_rate", "terminal_growth_not_less_than_discount_rate"),
            codes,
        )

    def test_invalid_net_debt(self) -> None:
        for bad in ("1.234", "abc", "", "1,000"):
            errors = self.errors(request_body(net_debt=bad))
            self.assertIn(
                ("/net_debt", "invalid_amount"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )
        errors = self.errors(request_body(net_debt=12.5))
        self.assertIn(
            ("/net_debt", "invalid_type"),
            {(e["path"], e["code"]) for e in errors},
        )

    def test_empty_forecast(self) -> None:
        errors = self.errors(request_body(forecast_cash_flows=[]))
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows", "too_few_forecast_periods")],
        )

    def test_forecast_not_array(self) -> None:
        errors = self.errors(request_body(forecast_cash_flows={}))
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows", "invalid_type")],
        )

    def test_forecast_item_not_object(self) -> None:
        errors = self.errors(request_body(forecast_cash_flows=[1]))
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows/0", "invalid_type")],
        )

    def test_forecast_item_unknown_field(self) -> None:
        errors = self.errors(
            request_body(
                forecast_cash_flows=[
                    {"period": 1, "free_cash_flow": "1", "growth": "0"}
                ]
            )
        )
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows/0/growth", "unknown_field")],
        )

    def test_period_type_errors(self) -> None:
        for bad in (True, "1", 1.5, None):
            errors = self.errors(
                request_body(
                    forecast_cash_flows=[{"period": bad, "free_cash_flow": "1"}]
                )
            )
            self.assertIn(
                ("/forecast_cash_flows/0/period", "invalid_type"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )

    def test_period_value_errors_at_array_path(self) -> None:
        cases = (
            [{"period": 0, "free_cash_flow": "1"}],
            [{"period": -2, "free_cash_flow": "1"}],
            [{"period": 2, "free_cash_flow": "1"}],
            [
                {"period": 1, "free_cash_flow": "1"},
                {"period": 1, "free_cash_flow": "2"},
            ],
            [
                {"period": 1, "free_cash_flow": "1"},
                {"period": 3, "free_cash_flow": "2"},
            ],
        )
        for flows in cases:
            errors = self.errors(request_body(forecast_cash_flows=flows))
            self.assertEqual(
                [(e["path"], e["code"]) for e in errors],
                [("/forecast_cash_flows", "invalid_forecast_periods")],
                flows,
            )

    def test_free_cash_flow_validation(self) -> None:
        errors = self.errors(
            request_body(forecast_cash_flows=[{"period": 1, "free_cash_flow": "1.234"}])
        )
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows/0/free_cash_flow", "invalid_amount")],
        )
        errors = self.errors(
            request_body(forecast_cash_flows=[{"period": 1}])
        )
        self.assertEqual(
            [(e["path"], e["code"]) for e in errors],
            [("/forecast_cash_flows/0/free_cash_flow", "required")],
        )

    def test_errors_sorted_by_path_and_code(self) -> None:
        errors = self.errors(
            request_body(currency="usd", discount_rate="0", net_debt="x", extra=1)
        )
        keys = [(e["path"], e["code"]) for e in errors]
        self.assertEqual(keys, sorted(keys))
        self.assertIn(("/extra", "unknown_field"), keys)
        self.assertIn(("/currency", "invalid_currency"), keys)
        self.assertIn(("/discount_rate", "invalid_rate"), keys)
        self.assertIn(("/net_debt", "invalid_amount"), keys)


class DcfHttpTest(unittest.TestCase):
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

    def request(self, path, body=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request("POST", path, body=data, headers={"Content-Type": content_type})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_route_success(self) -> None:
        status, payload = self.request("/v1/valuations/dcf/calculate", request_body())
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["equity_value"], "1290.91")

    def test_route_422(self) -> None:
        status, payload = self.request(
            "/v1/valuations/dcf/calculate", request_body(forecast_cash_flows=[])
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["errors"][0]["code"], "too_few_forecast_periods")

    def test_media_type_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/valuations/dcf/calculate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request("/v1/valuations/dcf/calculate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request("/v1/valuations/dcf/calculate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
