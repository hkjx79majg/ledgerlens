import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart():
    return {
        "chart_id": "COA-1",
        "effective_date": "2026-01-01",
        "accounts": [
            {
                "code": "1001",
                "name": "Cash",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "2001",
                "name": "Payables",
                "type": "liability",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "3001",
                "name": "Capital",
                "type": "equity",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "4001",
                "name": "Sales",
                "type": "revenue",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5001",
                "name": "Rent",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5002",
                "name": "Unused Expense",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
        ],
    }


def request_payload(**overrides):
    payload = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "opening_balances": [
            {"account_code": "1001", "debit": "1000.00", "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": "1000.00"},
        ],
        "entries": [
            {
                "voucher_id": "JV-1",
                "posting_date": "2026-01-10",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                    {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "200"},
                ],
            },
            {
                "voucher_id": "JV-2",
                "posting_date": "2026-01-20",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "5001", "debit": "50", "credit": "0"},
                    {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "50"},
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload


class GenerateFinancialStatementsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_success_income_statement_and_balance_sheet(self) -> None:
        status, body = self.service.generate_financial_statements(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")

        income = body["income_statement"]
        self.assertEqual(
            income["revenue"],
            [{"account_code": "4001", "name": "Sales", "amount": "200.00"}],
        )
        self.assertEqual(
            income["expense"],
            [
                {"account_code": "5001", "name": "Rent", "amount": "50.00"},
                {"account_code": "5002", "name": "Unused Expense", "amount": "0.00"},
            ],
        )
        self.assertEqual(income["total_revenue"], "200.00")
        self.assertEqual(income["total_expense"], "50.00")
        self.assertEqual(income["net_income"], "150.00")

        balance = body["balance_sheet"]
        self.assertEqual(
            balance["assets"],
            [{"account_code": "1001", "name": "Cash", "amount": "1150.00"}],
        )
        self.assertEqual(
            balance["liabilities"],
            [{"account_code": "2001", "name": "Payables", "amount": "0.00"}],
        )
        self.assertEqual(
            balance["equity"],
            [{"account_code": "3001", "name": "Capital", "amount": "1000.00"}],
        )
        self.assertEqual(balance["total_assets"], "1150.00")
        self.assertEqual(balance["total_liabilities"], "0.00")
        self.assertEqual(balance["total_equity_before_net_income"], "1000.00")
        self.assertEqual(balance["current_period_net_income"], "150.00")
        self.assertEqual(balance["total_liabilities_and_equity"], "1150.00")
        self.assertIs(balance["balanced"], True)

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.service.generate_financial_statements(request_payload())
        second = self.service.generate_financial_statements(request_payload())
        self.assertEqual(first, second)

    def test_validation_errors_match_trial_balance(self) -> None:
        payload = request_payload(period_start="2026-02-01")
        expected = self.service.generate_trial_balance(payload)
        actual = self.service.generate_financial_statements(payload)
        self.assertEqual(expected, actual)
        self.assertEqual(actual[0], 422)
        self.assertFalse(actual[1]["valid"])

    def test_negative_amounts_and_unbalanced_balance_sheet(self) -> None:
        # 收入科目带期初贷方余额：本期利润不含它，资产负债表不再平衡。
        payload = request_payload(
            opening_balances=[
                {"account_code": "1001", "debit": "30.00", "credit": "0"},
                {"account_code": "4001", "debit": "0", "credit": "30.00"},
            ],
            entries=[],
        )
        status, body = self.service.generate_financial_statements(payload)
        self.assertEqual(status, 200)
        balance = body["balance_sheet"]
        self.assertEqual(balance["total_assets"], "30.00")
        self.assertEqual(balance["current_period_net_income"], "0.00")
        self.assertEqual(balance["total_liabilities_and_equity"], "0.00")
        self.assertIs(balance["balanced"], False)

    def test_reverse_balance_keeps_negative_sign(self) -> None:
        # 现金被费用透支：资产科目出现贷方余额，金额保留负号。
        payload = request_payload(
            opening_balances=[],
            entries=[
                {
                    "voucher_id": "JV-9",
                    "posting_date": "2026-01-05",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "5001", "debit": "80", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "80"},
                    ],
                }
            ],
        )
        status, body = self.service.generate_financial_statements(payload)
        self.assertEqual(status, 200)
        balance = body["balance_sheet"]
        self.assertEqual(balance["assets"][0]["amount"], "-80.00")
        self.assertEqual(balance["total_assets"], "-80.00")


class GenerateFinancialStatementsHttpTest(unittest.TestCase):
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

    def request(self, path, body=None, content_type=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            if content_type is not None:
                headers["Content-Type"] = content_type
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_endpoint_returns_200(self) -> None:
        status, payload = self.request(
            "/v1/financial-statements/generate", request_payload(), "application/json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertIn("income_statement", payload)
        self.assertIn("balance_sheet", payload)

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/financial-statements/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "/v1/financial-statements/generate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "/v1/financial-statements/generate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/financial-statements/generate",
            request_payload(currency="usd"),
            "application/json",
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/currency", "invalid_currency"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )


if __name__ == "__main__":
    unittest.main()
