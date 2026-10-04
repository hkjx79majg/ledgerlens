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
                "code": "1009",
                "name": "Frozen",
                "type": "asset",
                "normal_balance": "debit",
                "active": False,
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


def entity_parent():
    return {
        "entity_id": "E-1",
        "opening_balances": [
            {"account_code": "1001", "debit": "1000.00", "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": "1000.00"},
        ],
        "entries": [
            {
                "voucher_id": "JV-A1",
                "posting_date": "2026-01-10",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                    {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "200"},
                ],
            },
        ],
    }


def entity_subsidiary():
    return {
        "entity_id": "E-2",
        "opening_balances": [
            {"account_code": "1001", "debit": "500.00", "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": "500.00"},
        ],
        "entries": [
            {
                "voucher_id": "JV-B1",
                "posting_date": "2026-01-20",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "5001", "debit": "50", "credit": "0"},
                    {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "50"},
                ],
            },
        ],
    }


def elimination_entry():
    return {
        "voucher_id": "ELIM-1",
        "posting_date": "2026-01-25",
        "currency": "CNY",
        "lines": [
            {"line_id": "a", "account_code": "4001", "debit": "30", "credit": "0"},
            {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "30"},
        ],
    }


def request_payload(**overrides):
    payload = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "entities": [entity_parent(), entity_subsidiary()],
        "elimination_entries": [elimination_entry()],
    }
    payload.update(overrides)
    return payload


class GenerateConsolidatedFinancialStatementsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_success_consolidated_statements(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["entity_count"], 2)

        income = body["income_statement"]
        self.assertEqual(
            income["revenue"],
            [{"account_code": "4001", "name": "Sales", "amount": "170.00"}],
        )
        self.assertEqual(
            income["expense"],
            [
                {"account_code": "5001", "name": "Rent", "amount": "50.00"},
                {"account_code": "5002", "name": "Unused Expense", "amount": "0.00"},
            ],
        )
        self.assertEqual(income["total_revenue"], "170.00")
        self.assertEqual(income["total_expense"], "50.00")
        self.assertEqual(income["net_income"], "120.00")

        balance = body["balance_sheet"]
        self.assertEqual(
            balance["assets"],
            [
                {"account_code": "1001", "name": "Cash", "amount": "1620.00"},
                {"account_code": "1009", "name": "Frozen", "amount": "0.00"},
            ],
        )
        self.assertEqual(
            balance["liabilities"],
            [{"account_code": "2001", "name": "Payables", "amount": "0.00"}],
        )
        self.assertEqual(
            balance["equity"],
            [{"account_code": "3001", "name": "Capital", "amount": "1500.00"}],
        )
        self.assertEqual(balance["total_assets"], "1620.00")
        self.assertEqual(balance["total_liabilities"], "0.00")
        self.assertEqual(balance["total_equity_before_net_income"], "1500.00")
        self.assertEqual(balance["current_period_net_income"], "120.00")
        self.assertEqual(balance["total_liabilities_and_equity"], "1620.00")
        self.assertIs(balance["balanced"], True)

        self.assertEqual(
            body["elimination_summary"],
            [
                {"account_code": "1001", "debit": "0.00", "credit": "30.00"},
                {"account_code": "4001", "debit": "30.00", "credit": "0.00"},
            ],
        )

    def test_elimination_entries_may_be_empty(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(elimination_entries=[])
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["elimination_summary"], [])
        # 无抵消时合并结果为两主体简单汇总。
        self.assertEqual(body["income_statement"]["net_income"], "150.00")
        self.assertEqual(body["balance_sheet"]["total_assets"], "1650.00")
        self.assertIs(body["balance_sheet"]["balanced"], True)

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.service.generate_consolidated_financial_statements(request_payload())
        second = self.service.generate_consolidated_financial_statements(request_payload())
        self.assertEqual(first, second)

    def test_entities_required_and_invalid_type(self) -> None:
        payload = request_payload()
        del payload["entities"]
        status, body = self.service.generate_consolidated_financial_statements(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entities", "required"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities={})
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entities", "invalid_type"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_entities_too_few(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities=[])
        )
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertIn(
            ("/entities", "too_few_entities"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_duplicate_entity_id_reported_on_later_occurrence(self) -> None:
        entities = [entity_parent(), entity_subsidiary(), entity_parent()]
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities=entities)
        )
        self.assertEqual(status, 422)
        duplicates = [
            e for e in body["errors"] if e["code"] == "duplicate_entity_id"
        ]
        self.assertEqual([e["path"] for e in duplicates], ["/entities/2/entity_id"])

    def test_entity_rejects_unknown_field(self) -> None:
        entity = entity_parent()
        entity["memo"] = "x"
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities=[entity])
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entities/0/memo", "unknown_field"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_entity_opening_and_entry_errors_carry_entity_prefix(self) -> None:
        entity = entity_parent()
        entity["opening_balances"] = [
            {"account_code": "1001", "debit": "10", "credit": "0"}
        ]
        entity["entries"] = [
            {
                "voucher_id": "JV-X",
                "posting_date": "2026-02-01",
                "currency": "USD",
                "lines": [
                    {"line_id": "a", "account_code": "1001", "debit": "5", "credit": "0"},
                    {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "5"},
                ],
            }
        ]
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities=[entity])
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(
            ("/entities/0/opening_balances", "unbalanced_opening_balances"), pairs
        )
        self.assertIn(("/entities/0/entries/0/currency", "currency_mismatch"), pairs)
        self.assertIn(
            ("/entities/0/entries/0/posting_date", "posting_date_out_of_period"), pairs
        )

    def test_entity_journal_errors_carry_entity_prefix(self) -> None:
        entity = entity_parent()
        entity["entries"] = [
            {
                "voucher_id": "JV-X",
                "posting_date": "2026-01-10",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "1001", "debit": "5", "credit": "0"},
                    {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "6"},
                ],
            }
        ]
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(entities=[entity])
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entities/0/entries/0", "unbalanced_entry"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_elimination_entries_required_and_invalid_type(self) -> None:
        payload = request_payload()
        del payload["elimination_entries"]
        status, body = self.service.generate_consolidated_financial_statements(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/elimination_entries", "required"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(elimination_entries="x")
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/elimination_entries", "invalid_type"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_elimination_entry_cross_object_errors(self) -> None:
        elimination = elimination_entry()
        elimination["currency"] = "USD"
        elimination["posting_date"] = "2026-02-01"
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(elimination_entries=[elimination])
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/elimination_entries/0/currency", "currency_mismatch"), pairs)
        self.assertIn(
            ("/elimination_entries/0/posting_date", "posting_date_out_of_period"), pairs
        )

    def test_elimination_entry_account_reference_errors(self) -> None:
        elimination = elimination_entry()
        elimination["lines"] = [
            {"line_id": "a", "account_code": "9999", "debit": "30", "credit": "0"},
            {"line_id": "b", "account_code": "1009", "debit": "0", "credit": "30"},
        ]
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(elimination_entries=[elimination])
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(
            ("/elimination_entries/0/lines/0/account_code", "unknown_account"), pairs
        )
        self.assertIn(
            ("/elimination_entries/0/lines/1/account_code", "inactive_account"), pairs
        )

    def test_dependent_errors_not_derived_from_invalid_fields(self) -> None:
        # 请求币种无效时不派生凭证币种一致性错误。
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(currency="usd")
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/currency", "invalid_currency"), pairs)
        self.assertNotIn(("/elimination_entries/0/currency", "currency_mismatch"), pairs)
        self.assertNotIn(("/entities/0/entries/0/currency", "currency_mismatch"), pairs)

    def test_errors_sorted_by_path_and_code(self) -> None:
        payload = request_payload(entities=[], elimination_entries="x", extra="y")
        status, body = self.service.generate_consolidated_financial_statements(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))

    def test_top_level_unknown_field_rejected(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(opening_balances=[])
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/opening_balances", "unknown_field"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )


class GenerateConsolidatedFinancialStatementsHttpTest(unittest.TestCase):
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
            "/v1/consolidated-financial-statements/generate",
            request_payload(),
            "application/json",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["entity_count"], 2)
        self.assertIn("income_statement", payload)
        self.assertIn("balance_sheet", payload)
        self.assertIn("elimination_summary", payload)

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/consolidated-financial-statements/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "/v1/consolidated-financial-statements/generate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "/v1/consolidated-financial-statements/generate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/consolidated-financial-statements/generate",
            request_payload(entities=[]),
            "application/json",
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/entities", "too_few_entities"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )


if __name__ == "__main__":
    unittest.main()
