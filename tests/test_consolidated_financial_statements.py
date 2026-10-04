import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart():
    return {
        "chart_id": "COA-G",
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
                "code": "1101",
                "name": "Intercompany AR",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "2101",
                "name": "Intercompany AP",
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
                "code": "4002",
                "name": "Intercompany Sales",
                "type": "revenue",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5001",
                "name": "COGS",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
        ],
    }


def entity(entity_id, opening_balances=None, entries=None):
    return {
        "entity_id": entity_id,
        "opening_balances": opening_balances or [],
        "entries": entries or [],
    }


def elimination(voucher_id="EL-1", posting_date="2026-01-20", lines=None, currency="CNY"):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": currency,
        "lines": lines
        if lines is not None
        else [
            {"line_id": "a", "account_code": "1001", "debit": "10", "credit": "0"},
            {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "10"},
        ],
    }


def request_payload(**overrides):
    payload = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "entities": [
            entity(
                "E1",
                opening_balances=[
                    {"account_code": "1001", "debit": "1000.00", "credit": "0"},
                    {"account_code": "3001", "debit": "0", "credit": "1000.00"},
                ],
                entries=[
                    {
                        "voucher_id": "E1-1",
                        "posting_date": "2026-01-10",
                        "currency": "CNY",
                        "lines": [
                            {"line_id": "a", "account_code": "1101", "debit": "300", "credit": "0"},
                            {"line_id": "b", "account_code": "4002", "debit": "0", "credit": "300"},
                        ],
                    }
                ],
            ),
            entity(
                "E2",
                opening_balances=[
                    {"account_code": "1001", "debit": "500.00", "credit": "0"},
                    {"account_code": "3001", "debit": "0", "credit": "500.00"},
                ],
                entries=[
                    {
                        "voucher_id": "E2-1",
                        "posting_date": "2026-01-12",
                        "currency": "CNY",
                        "lines": [
                            {"line_id": "a", "account_code": "5001", "debit": "300", "credit": "0"},
                            {"line_id": "b", "account_code": "2101", "debit": "0", "credit": "300"},
                        ],
                    },
                    {
                        "voucher_id": "E2-2",
                        "posting_date": "2026-01-15",
                        "currency": "CNY",
                        "lines": [
                            {"line_id": "a", "account_code": "1001", "debit": "100", "credit": "0"},
                            {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "100"},
                        ],
                    },
                ],
            ),
        ],
        "elimination_entries": [
            {
                "voucher_id": "EL-1",
                "posting_date": "2026-01-20",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "2101", "debit": "300", "credit": "0"},
                    {"line_id": "b", "account_code": "1101", "debit": "0", "credit": "300"},
                ],
            },
            {
                "voucher_id": "EL-2",
                "posting_date": "2026-01-21",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "4002", "debit": "300", "credit": "0"},
                    {"line_id": "b", "account_code": "5001", "debit": "0", "credit": "300"},
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload


class GenerateConsolidatedFinancialStatementsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def error_codes(self, payload):
        status, body = self.service.generate_consolidated_financial_statements(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return [(err["path"], err["code"]) for err in body["errors"]]

    def test_success_merges_entities_and_eliminations(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload()
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-G")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["entity_count"], 2)

        income = body["income_statement"]
        self.assertEqual(
            income["revenue"],
            [
                {"account_code": "4001", "name": "Sales", "amount": "100.00"},
                {"account_code": "4002", "name": "Intercompany Sales", "amount": "0.00"},
            ],
        )
        self.assertEqual(
            income["expense"],
            [{"account_code": "5001", "name": "COGS", "amount": "0.00"}],
        )
        self.assertEqual(income["total_revenue"], "100.00")
        self.assertEqual(income["total_expense"], "0.00")
        self.assertEqual(income["net_income"], "100.00")

        balance = body["balance_sheet"]
        self.assertEqual(
            balance["assets"],
            [
                {"account_code": "1001", "name": "Cash", "amount": "1600.00"},
                {"account_code": "1101", "name": "Intercompany AR", "amount": "0.00"},
            ],
        )
        self.assertEqual(
            balance["liabilities"],
            [{"account_code": "2101", "name": "Intercompany AP", "amount": "0.00"}],
        )
        self.assertEqual(
            balance["equity"],
            [{"account_code": "3001", "name": "Capital", "amount": "1500.00"}],
        )
        self.assertEqual(balance["total_assets"], "1600.00")
        self.assertEqual(balance["total_liabilities"], "0.00")
        self.assertEqual(balance["total_equity_before_net_income"], "1500.00")
        self.assertEqual(balance["current_period_net_income"], "100.00")
        self.assertEqual(balance["total_liabilities_and_equity"], "1600.00")
        self.assertIs(balance["balanced"], True)

        self.assertEqual(
            body["elimination_summary"],
            [
                {"account_code": "1101", "debit": "0.00", "credit": "300.00"},
                {"account_code": "2101", "debit": "300.00", "credit": "0.00"},
                {"account_code": "4002", "debit": "300.00", "credit": "0.00"},
                {"account_code": "5001", "debit": "0.00", "credit": "300.00"},
            ],
        )

    def test_empty_eliminations_is_allowed(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(
                entities=[
                    entity(
                        "E1",
                        opening_balances=[
                            {"account_code": "1001", "debit": "10.00", "credit": "0"},
                            {"account_code": "3001", "debit": "0", "credit": "10.00"},
                        ],
                    )
                ],
                elimination_entries=[],
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["entity_count"], 1)
        self.assertEqual(body["elimination_summary"], [])
        self.assertIs(body["balance_sheet"]["balanced"], True)

    def test_aggregates_multiple_entities_without_eliminations(self) -> None:
        status, body = self.service.generate_consolidated_financial_statements(
            request_payload(
                entities=[
                    entity(
                        "A",
                        opening_balances=[
                            {"account_code": "1001", "debit": "100.00", "credit": "0"},
                            {"account_code": "3001", "debit": "0", "credit": "100.00"},
                        ],
                    ),
                    entity(
                        "B",
                        opening_balances=[
                            {"account_code": "1001", "debit": "200.00", "credit": "0"},
                            {"account_code": "3001", "debit": "0", "credit": "200.00"},
                        ],
                    ),
                ],
                elimination_entries=[],
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["balance_sheet"]["total_assets"], "300.00")
        self.assertEqual(body["balance_sheet"]["total_equity_before_net_income"], "300.00")

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.service.generate_consolidated_financial_statements(request_payload())
        second = self.service.generate_consolidated_financial_statements(request_payload())
        self.assertEqual(first, second)

    def test_entities_type_and_cardinality_errors(self) -> None:
        payload = request_payload()
        del payload["entities"]
        self.assertEqual(self.error_codes(payload), [("/entities", "required")])
        self.assertEqual(
            self.error_codes(request_payload(entities="x")),
            [("/entities", "invalid_type")],
        )
        self.assertEqual(
            self.error_codes(request_payload(entities=[])),
            [("/entities", "too_few_entities")],
        )
        self.assertEqual(
            self.error_codes(request_payload(entities=["nope"])),
            [("/entities/0", "invalid_type")],
        )

    def test_duplicate_entity_id_flags_later_occurrence(self) -> None:
        codes = self.error_codes(
            request_payload(entities=[entity("A"), entity("B"), entity("A")])
        )
        self.assertEqual(codes, [("/entities/2/entity_id", "duplicate_entity_id")])

    def test_entity_rejects_unknown_field(self) -> None:
        codes = self.error_codes(
            request_payload(
                entities=[
                    {
                        "entity_id": "E1",
                        "opening_balances": [],
                        "entries": [],
                        "unexpected": 1,
                    }
                ]
            )
        )
        self.assertIn(("/entities/0/unexpected", "unknown_field"), codes)

    def test_entity_scoped_opening_and_entry_errors(self) -> None:
        codes = self.error_codes(
            request_payload(
                entities=[
                    entity(
                        "E1",
                        opening_balances=[
                            {"account_code": "1001", "debit": "5", "credit": "5"}
                        ],
                    )
                ]
            )
        )
        self.assertIn(("/entities/0/opening_balances/0", "invalid_side"), codes)
        self.assertFalse(any(path.startswith("/opening_balances") for path, _ in codes))

        codes = self.error_codes(
            request_payload(
                entities=[
                    entity(
                        "E1",
                        opening_balances=[
                            {"account_code": "1001", "debit": "5", "credit": "0"}
                        ],
                    )
                ]
            )
        )
        self.assertIn(
            ("/entities/0/opening_balances", "unbalanced_opening_balances"), codes
        )

        unbalanced_entry = {
            "voucher_id": "V",
            "posting_date": "2026-01-10",
            "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1001", "debit": "5", "credit": "0"},
                {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "4"},
            ],
        }
        codes = self.error_codes(
            request_payload(entities=[entity("E1", entries=[unbalanced_entry])])
        )
        self.assertIn(("/entities/0/entries/0", "unbalanced_entry"), codes)

    def test_entity_entry_cross_object_errors(self) -> None:
        bad_entry = {
            "voucher_id": "V",
            "posting_date": "2026-03-01",
            "currency": "USD",
            "lines": [
                {"line_id": "a", "account_code": "9999", "debit": "5", "credit": "0"},
                {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "5"},
            ],
        }
        codes = self.error_codes(
            request_payload(entities=[entity("E1", entries=[bad_entry])])
        )
        self.assertIn(("/entities/0/entries/0/currency", "currency_mismatch"), codes)
        self.assertIn(
            ("/entities/0/entries/0/posting_date", "posting_date_out_of_period"), codes
        )
        self.assertIn(
            ("/entities/0/entries/0/lines/0/account_code", "unknown_account"), codes
        )

    def test_shared_field_errors_reported_once_at_top_level(self) -> None:
        self.assertEqual(
            self.error_codes(
                request_payload(
                    period_start="2026-02-01",
                    entities=[entity("A"), entity("B")],
                )
            ),
            [("/period_start", "invalid_period")],
        )
        self.assertEqual(
            self.error_codes(request_payload(currency="bad")),
            [("/currency", "invalid_currency")],
        )

    def test_top_level_rejects_single_entity_fields_and_unknowns(self) -> None:
        codes = self.error_codes(request_payload(opening_balances=[], entries=[]))
        self.assertEqual(
            codes,
            [("/entries", "unknown_field"), ("/opening_balances", "unknown_field")],
        )

    def test_elimination_entries_shape_errors(self) -> None:
        payload = request_payload()
        del payload["elimination_entries"]
        self.assertEqual(
            self.error_codes(payload),
            [("/elimination_entries", "required")],
        )
        self.assertEqual(
            self.error_codes(request_payload(elimination_entries="x")),
            [("/elimination_entries", "invalid_type")],
        )

    def test_elimination_entry_cross_object_errors(self) -> None:
        self.assertIn(
            ("/elimination_entries/0/currency", "currency_mismatch"),
            self.error_codes(
                request_payload(elimination_entries=[elimination(currency="USD")])
            ),
        )
        self.assertIn(
            ("/elimination_entries/0/posting_date", "posting_date_out_of_period"),
            self.error_codes(
                request_payload(
                    elimination_entries=[elimination(posting_date="2026-02-02")]
                )
            ),
        )
        unknown_lines = [
            {"line_id": "a", "account_code": "9999", "debit": "5", "credit": "0"},
            {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "5"},
        ]
        self.assertIn(
            ("/elimination_entries/0/lines/0/account_code", "unknown_account"),
            self.error_codes(
                request_payload(
                    elimination_entries=[elimination(lines=unknown_lines)]
                )
            ),
        )

    def test_elimination_entry_inactive_account(self) -> None:
        coa = chart()
        coa["accounts"][0]["active"] = False
        codes = self.error_codes(
            request_payload(
                chart=coa,
                elimination_entries=[elimination()],
            )
        )
        self.assertTrue(
            any(
                code == "inactive_account" and "elimination_entries" in path
                for path, code in codes
            )
        )

    def test_elimination_entry_unbalanced(self) -> None:
        unbalanced_lines = [
            {"line_id": "a", "account_code": "1001", "debit": "5", "credit": "0"},
            {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "4"},
        ]
        self.assertIn(
            ("/elimination_entries/0", "unbalanced_entry"),
            self.error_codes(
                request_payload(
                    elimination_entries=[elimination(lines=unbalanced_lines)]
                )
            ),
        )

    def test_invalid_dependent_field_suppresses_derived_errors(self) -> None:
        codes = self.error_codes(
            request_payload(elimination_entries=[elimination(currency="bad")])
        )
        self.assertIn(("/elimination_entries/0/currency", "invalid_currency"), codes)
        self.assertFalse(any(code == "currency_mismatch" for _, code in codes))

        codes = self.error_codes(
            request_payload(elimination_entries=[elimination(posting_date="nope")])
        )
        self.assertIn(("/elimination_entries/0/posting_date", "invalid_date"), codes)
        self.assertFalse(
            any(code == "posting_date_out_of_period" for _, code in codes)
        )

        bad_chart = {"chart_id": "C", "effective_date": "2026-01-01", "accounts": []}
        codes = self.error_codes(
            request_payload(
                chart=bad_chart,
                entities=[
                    entity(
                        "E1",
                        opening_balances=[
                            {"account_code": "1001", "debit": "5", "credit": "0"},
                            {"account_code": "3001", "debit": "0", "credit": "5"},
                        ],
                    )
                ],
                elimination_entries=[elimination()],
            )
        )
        self.assertIn(("/chart/accounts", "too_few_accounts"), codes)
        self.assertFalse(
            any(code in ("unknown_account", "inactive_account") for _, code in codes)
        )

    def test_errors_sorted_by_path_then_code(self) -> None:
        codes = self.error_codes(
            request_payload(
                entities=[entity("Z")],
                elimination_entries=[elimination(currency="USD")],
            )
        )
        self.assertEqual(codes, sorted(codes))


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

    PATH = "/v1/consolidated-financial-statements/generate"

    def test_endpoint_returns_200(self) -> None:
        status, payload = self.request(self.PATH, request_payload(), "application/json")
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["entity_count"], 2)
        self.assertIn("income_statement", payload)
        self.assertIn("balance_sheet", payload)
        self.assertIn("elimination_summary", payload)

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(self.PATH, "{}", "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(self.PATH, "{bad", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(self.PATH, "[1]", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            self.PATH, request_payload(entities=[]), "application/json"
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/entities", "too_few_entities"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )

    def test_unknown_route_remains_404(self) -> None:
        status, payload = self.request("/v1/other", {}, "application/json")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
