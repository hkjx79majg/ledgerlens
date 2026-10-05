import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def request_body(**overrides):
    body = {
        "currency": "USD",
        "discount_rate": "0.10",
        "terminal_growth_rate": "0.02",
        "forecast_cash_flows": [
            {"period": 1, "free_cash_flow": "100"},
            {"period": 2, "free_cash_flow": "110"},
        ],
        "net_debt": "50",
    }
    body.update(overrides)
    return body


class DcfValuationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, body):
        status, payload = self.service.calculate_dcf_valuation(body)
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        return payload["errors"]

    def test_success_basic(self) -> None:
        status, payload = self.service.calculate_dcf_valuation(request_body())
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "valid": True,
                "currency": "USD",
                "forecast": [
                    {
                        "period": 1,
                        "free_cash_flow": "100.00",
                        "discount_factor": "0.90909091",
                        "present_value": "90.91",
                    },
                    {
                        "period": 2,
                        "free_cash_flow": "110.00",
                        "discount_factor": "0.82644628",
                        "present_value": "90.91",
                    },
                ],
                "forecast_present_value": "181.82",
                "terminal_value": "1402.50",
                "terminal_present_value": "1159.09",
                "enterprise_value": "1340.91",
                "equity_value": "1290.91",
            },
        )

    def test_periods_sorted_and_net_cash(self) -> None:
        body = request_body(
            forecast_cash_flows=[
                {"period": 2, "free_cash_flow": "110"},
                {"period": 1, "free_cash_flow": "100"},
            ],
            net_debt="-1290.91",
        )
        status, payload = self.service.calculate_dcf_valuation(body)
        self.assertEqual(status, 200)
        self.assertEqual([row["period"] for row in payload["forecast"]], [1, 2])
        # 负 net_debt 表示净现金，股权价值 = 企业价值 + 净现金。
        self.assertEqual(payload["equity_value"], "2631.82")

    def test_negative_growth_and_negative_cash_flow(self) -> None:
        body = request_body(
            terminal_growth_rate="-0.5",
            forecast_cash_flows=[{"period": 1, "free_cash_flow": "-100"}],
            net_debt="0",
        )
        status, payload = self.service.calculate_dcf_valuation(body)
        self.assertEqual(status, 200)
        # 终值 = -100 * 0.5 / (0.10 - (-0.5)) = -83.333…，现值 = 终值 / 1.1。
        self.assertEqual(payload["terminal_value"], "-83.33")
        self.assertEqual(payload["terminal_present_value"], "-75.76")
        self.assertEqual(payload["forecast_present_value"], "-90.91")
        self.assertEqual(payload["enterprise_value"], "-166.67")
        self.assertEqual(payload["equity_value"], "-166.67")

    def test_negative_zero_eliminated(self) -> None:
        body = request_body(
            terminal_growth_rate="0",
            forecast_cash_flows=[{"period": 1, "free_cash_flow": "-0.00"}],
            net_debt="-0.00",
        )
        status, payload = self.service.calculate_dcf_valuation(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["forecast"][0]["free_cash_flow"], "0.00")
        self.assertEqual(payload["forecast"][0]["present_value"], "0.00")
        self.assertEqual(payload["terminal_value"], "0.00")
        self.assertEqual(payload["enterprise_value"], "0.00")
        self.assertEqual(payload["equity_value"], "0.00")

    def test_discount_rate_one_allowed(self) -> None:
        body = request_body(
            discount_rate="1",
            terminal_growth_rate="0.99999999",
            forecast_cash_flows=[{"period": 1, "free_cash_flow": "1"}],
        )
        status, payload = self.service.calculate_dcf_valuation(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["forecast"][0]["discount_factor"], "0.50000000")

    def test_unknown_top_level_field(self) -> None:
        errors = self.errors(request_body(extra="x"))
        self.assertIn(
            {"path": "/extra", "code": "unknown_field", "message": "unknown field 'extra'"},
            errors,
        )

    def test_missing_fields_report_required(self) -> None:
        errors = self.errors({})
        self.assertEqual(
            {(e["path"], e["code"]) for e in errors},
            {
                ("/currency", "required"),
                ("/discount_rate", "required"),
                ("/terminal_growth_rate", "required"),
                ("/forecast_cash_flows", "required"),
                ("/net_debt", "required"),
            },
        )

    def test_currency_validation(self) -> None:
        for bad in ("usd", "US", "US1", "USDD"):
            errors = self.errors(request_body(currency=bad))
            self.assertIn(("/currency", "invalid_currency"), {(e["path"], e["code"]) for e in errors}, bad)
        errors = self.errors(request_body(currency=1))
        self.assertIn(("/currency", "invalid_type"), {(e["path"], e["code"]) for e in errors})

    def test_discount_rate_format_and_range(self) -> None:
        for bad in ("0", "-0.1", "1.00000001", "1.5", "abc", "0.123456789", "1e-1"):
            errors = self.errors(request_body(discount_rate=bad))
            self.assertIn(("/discount_rate", "invalid_rate"), {(e["path"], e["code"]) for e in errors}, bad)
        errors = self.errors(request_body(discount_rate=0.1))
        self.assertIn(("/discount_rate", "invalid_type"), {(e["path"], e["code"]) for e in errors})

    def test_terminal_growth_format_and_range(self) -> None:
        for bad in ("-1", "-1.5", "abc", "0.123456789"):
            errors = self.errors(request_body(terminal_growth_rate=bad))
            self.assertIn(
                ("/terminal_growth_rate", "invalid_rate"),
                {(e["path"], e["code"]) for e in errors},
                bad,
            )

    def test_terminal_growth_not_less_than_discount_rate(self) -> None:
        errors = self.errors(request_body(terminal_growth_rate="0.10"))
        self.assertEqual(
            {(e["path"], e["code"]) for e in errors},
            {("/terminal_growth_rate", "terminal_growth_not_less_than_discount_rate")},
        )
        errors = self.errors(request_body(terminal_growth_rate="0.5"))
        self.assertIn(
            ("/terminal_growth_rate", "terminal_growth_not_less_than_discount_rate"),
            {(e["path"], e["code"]) for e in errors},
        )

    def test_terminal_growth_comparison_skipped_when_rate_invalid(self) -> None:
        errors = self.errors(request_body(discount_rate="abc", terminal_growth_rate="0.5"))
        self.assertNotIn(
            "terminal_growth_not_less_than_discount_rate",
            {e["code"] for e in errors},
        )

    def test_net_debt_validation(self) -> None:
        for bad in ("1.234", "abc", "+1", "1."):
            errors = self.errors(request_body(net_debt=bad))
            self.assertIn(("/net_debt", "invalid_amount"), {(e["path"], e["code"]) for e in errors}, bad)
        errors = self.errors(request_body(net_debt=50))
        self.assertIn(("/net_debt", "invalid_type"), {(e["path"], e["code"]) for e in errors})

    def test_empty_forecast(self) -> None:
        errors = self.errors(request_body(forecast_cash_flows=[]))
        self.assertEqual(
            {(e["path"], e["code"]) for e in errors},
            {("/forecast_cash_flows", "too_few_forecast_periods")},
        )

    def test_forecast_not_array(self) -> None:
        errors = self.errors(request_body(forecast_cash_flows="x"))
        self.assertIn(
            ("/forecast_cash_flows", "invalid_type"),
            {(e["path"], e["code"]) for e in errors},
        )

    def test_forecast_item_structure(self) -> None:
        errors = self.errors(
            request_body(forecast_cash_flows=[{"period": 1, "free_cash_flow": "1", "x": 1}, "nope"])
        )
        pairs = {(e["path"], e["code"]) for e in errors}
        self.assertIn(("/forecast_cash_flows/0/x", "unknown_field"), pairs)
        self.assertIn(("/forecast_cash_flows/1", "invalid_type"), pairs)

    def test_period_type_and_presence(self) -> None:
        body = request_body(
            forecast_cash_flows=[
                {"free_cash_flow": "1"},
                {"period": True, "free_cash_flow": "1"},
                {"period": "1", "free_cash_flow": "1"},
            ]
        )
        errors = self.errors(body)
        pairs = {(e["path"], e["code"]) for e in errors}
        self.assertIn(("/forecast_cash_flows/0/period", "required"), pairs)
        self.assertIn(("/forecast_cash_flows/1/period", "invalid_type"), pairs)
        self.assertIn(("/forecast_cash_flows/2/period", "invalid_type"), pairs)
        # period 字段无效时不派生期号序列错误。
        self.assertNotIn("invalid_forecast_periods", {e["code"] for e in errors})

    def test_invalid_forecast_periods(self) -> None:
        for flows in (
            [{"period": 0, "free_cash_flow": "1"}],
            [{"period": -2, "free_cash_flow": "1"}],
            [{"period": 2, "free_cash_flow": "1"}],
            [{"period": 1, "free_cash_flow": "1"}, {"period": 1, "free_cash_flow": "2"}],
            [{"period": 1, "free_cash_flow": "1"}, {"period": 3, "free_cash_flow": "2"}],
        ):
            errors = self.errors(request_body(forecast_cash_flows=flows))
            self.assertEqual(
                {(e["path"], e["code"]) for e in errors},
                {("/forecast_cash_flows", "invalid_forecast_periods")},
                flows,
            )

    def test_free_cash_flow_validation(self) -> None:
        errors = self.errors(
            request_body(forecast_cash_flows=[{"period": 1, "free_cash_flow": "1.234"}])
        )
        self.assertEqual(
            {(e["path"], e["code"]) for e in errors},
            {("/forecast_cash_flows/0/free_cash_flow", "invalid_amount")},
        )
        errors = self.errors(
            request_body(forecast_cash_flows=[{"period": 1, "free_cash_flow": 1}])
        )
        self.assertIn(
            ("/forecast_cash_flows/0/free_cash_flow", "invalid_type"),
            {(e["path"], e["code"]) for e in errors},
        )

    def test_errors_sorted_by_path_and_code(self) -> None:
        errors = self.errors(request_body(currency="usd", net_debt="x", extra=1))
        self.assertEqual(errors, sorted(errors, key=lambda e: (e["path"], e["code"])))

    def test_input_not_mutated_and_deterministic(self) -> None:
        body = request_body()
        snapshot = json.loads(json.dumps(body))
        first = self.service.calculate_dcf_valuation(body)
        second = self.service.calculate_dcf_valuation(body)
        self.assertEqual(body, snapshot)
        self.assertEqual(first, second)


class DcfValuationHttpTest(unittest.TestCase):
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
        status, payload = self.request("/v1/valuations/dcf/calculate", request_body(net_debt="x"))
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["errors"][0]["code"], "invalid_amount")

    def test_media_type_json_and_object_errors(self) -> None:
        status, payload = self.request("/v1/valuations/dcf/calculate", "{}", "text/plain")
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
