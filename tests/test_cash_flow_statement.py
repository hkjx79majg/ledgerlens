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
                "code": "1002",
                "name": "Bank",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1003",
                "name": "Old Cash",
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
            {
                "voucher_id": "JV-3",
                "posting_date": "2026-01-25",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "1002", "debit": "300", "credit": "0"},
                    {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "300"},
                ],
            },
            {
                "voucher_id": "JV-4",
                "posting_date": "2026-01-26",
                "currency": "CNY",
                "lines": [
                    {"line_id": "a", "account_code": "5001", "debit": "20", "credit": "0"},
                    {"line_id": "b", "account_code": "2001", "debit": "0", "credit": "20"},
                ],
            },
        ],
        "cash_account_codes": ["1001", "1002"],
        "entry_activities": ["operating", "operating", "financing", None],
    }
    payload.update(overrides)
    return payload


def error_codes(body):
    return {(e["path"], e["code"]) for e in body["errors"]}


class GenerateCashFlowStatementServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_success_sections_and_balances(self) -> None:
        status, body = self.service.generate_cash_flow_statement(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")

        statement = body["cash_flow_statement"]
        self.assertEqual(
            statement["operating"],
            {
                "items": [
                    {"voucher_id": "JV-1", "posting_date": "2026-01-10", "amount": "200.00"},
                    {"voucher_id": "JV-2", "posting_date": "2026-01-20", "amount": "-50.00"},
                ],
                "total": "150.00",
            },
        )
        self.assertEqual(statement["investing"], {"items": [], "total": "0.00"})
        self.assertEqual(
            statement["financing"],
            {
                "items": [
                    {"voucher_id": "JV-3", "posting_date": "2026-01-25", "amount": "300.00"}
                ],
                "total": "300.00",
            },
        )

        self.assertEqual(body["beginning_cash_balance"], "1000.00")
        self.assertEqual(body["net_cash_change"], "450.00")
        self.assertEqual(body["ending_cash_balance"], "1450.00")

    def test_only_cash_accounts_count_and_entries_order_preserved(self) -> None:
        # 仅 1001 为现金科目：JV-3 不再影响现金；内部转账凭证净变动为零。
        payload = request_payload(
            cash_account_codes=["1001"],
            entries=[
                {
                    "voucher_id": "JV-5",
                    "posting_date": "2026-01-11",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "1002", "debit": "70", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "70"},
                    ],
                },
                {
                    "voucher_id": "JV-6",
                    "posting_date": "2026-01-12",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "1001", "debit": "40", "credit": "0"},
                        {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "40"},
                    ],
                },
            ],
            entry_activities=["operating", "investing"],
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 200)
        statement = body["cash_flow_statement"]
        self.assertEqual(
            statement["operating"]["items"],
            [{"voucher_id": "JV-5", "posting_date": "2026-01-11", "amount": "-70.00"}],
        )
        self.assertEqual(
            statement["investing"]["items"],
            [{"voucher_id": "JV-6", "posting_date": "2026-01-12", "amount": "40.00"}],
        )
        self.assertEqual(body["net_cash_change"], "-30.00")
        self.assertEqual(body["ending_cash_balance"], "970.00")

    def test_internal_cash_transfer_has_zero_change(self) -> None:
        # 现金科目之间互转：借方减贷方净额为零，凭证不出现且活动须为 null。
        payload = request_payload(
            entries=[
                {
                    "voucher_id": "JV-7",
                    "posting_date": "2026-01-15",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "1002", "debit": "60", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "60"},
                    ],
                }
            ],
            entry_activities=[None],
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 200)
        statement = body["cash_flow_statement"]
        for section in ("operating", "investing", "financing"):
            self.assertEqual(statement[section], {"items": [], "total": "0.00"})
        self.assertEqual(body["net_cash_change"], "0.00")
        self.assertEqual(body["ending_cash_balance"], body["beginning_cash_balance"])

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.service.generate_cash_flow_statement(request_payload())
        second = self.service.generate_cash_flow_statement(request_payload())
        self.assertEqual(first, second)

    def test_base_validation_matches_financial_statements(self) -> None:
        # 既有字段沿用原校验：同一非法输入与财务报表端点错误一致。
        payload = request_payload(period_start="2026-02-01")
        base_payload = {
            key: value
            for key, value in payload.items()
            if key not in ("cash_account_codes", "entry_activities")
        }
        _, expected = self.service.generate_financial_statements(base_payload)
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertEqual(body["errors"], expected["errors"])

    def test_unknown_top_level_field_rejected(self) -> None:
        payload = request_payload()
        payload["surprise"] = 1
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/surprise", "unknown_field"), error_codes(body))

    def test_cash_account_codes_field_errors(self) -> None:
        cases = [
            # (override, expected (path, code))
            ({"cash_account_codes": None}, None),  # 占位，下方单独处理缺失
            ({"cash_account_codes": "1001"}, ("/cash_account_codes", "invalid_type")),
            ({"cash_account_codes": []}, ("/cash_account_codes", "too_few_cash_accounts")),
            ({"cash_account_codes": [""]}, ("/cash_account_codes/0", "blank_value")),
            ({"cash_account_codes": [123]}, ("/cash_account_codes/0", "invalid_type")),
            (
                {"cash_account_codes": ["1001", "1001"]},
                ("/cash_account_codes/1", "duplicate_cash_account"),
            ),
            ({"cash_account_codes": ["9999"]}, ("/cash_account_codes/0", "unknown_account")),
            ({"cash_account_codes": ["1003"]}, ("/cash_account_codes/0", "inactive_account")),
            (
                {"cash_account_codes": ["2001"]},
                ("/cash_account_codes/0", "cash_account_not_asset"),
            ),
        ]
        for override, expected in cases[1:]:
            with self.subTest(override=override):
                status, body = self.service.generate_cash_flow_statement(
                    request_payload(**override)
                )
                self.assertEqual(status, 422)
                self.assertFalse(body["valid"])
                self.assertIn(expected, error_codes(body))

        payload = request_payload()
        del payload["cash_account_codes"]
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes", "required"), error_codes(body))

    def test_inactive_account_takes_precedence_over_not_asset(self) -> None:
        # 科目未知、停用、非资产类依次判定：停用科目只报 inactive_account。
        payload = request_payload()
        payload["chart"]["accounts"].append(
            {
                "code": "2002",
                "name": "Old Payables",
                "type": "liability",
                "normal_balance": "credit",
                "active": False,
                "parent_code": None,
            }
        )
        payload["cash_account_codes"] = ["2002"]
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        codes = error_codes(body)
        self.assertIn(("/cash_account_codes/0", "inactive_account"), codes)
        self.assertNotIn(("/cash_account_codes/0", "cash_account_not_asset"), codes)

    def test_entry_activities_field_errors(self) -> None:
        payload = request_payload()
        del payload["entry_activities"]
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "required"), error_codes(body))

        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities="operating")
        )
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "invalid_type"), error_codes(body))

        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities=["operating"])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "activity_count_mismatch"), error_codes(body))

        status, body = self.service.generate_cash_flow_statement(
            request_payload(
                entry_activities=["operating", "operating", "other", None]
            )
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/2", "invalid_cash_flow_activity"), error_codes(body)
        )

    def test_missing_cash_flow_activity(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(
                entry_activities=["operating", None, "financing", None]
            )
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/1", "missing_cash_flow_activity"), error_codes(body)
        )

    def test_activity_without_cash_change(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(
                entry_activities=["operating", "operating", "financing", "investing"]
            )
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/3", "activity_without_cash_change"), error_codes(body)
        )

    def test_no_derived_errors_when_dependencies_invalid(self) -> None:
        # 凭证无效：不派生该凭证的活动一致性错误。
        payload = request_payload(
            entries=[
                {
                    "voucher_id": "JV-BAD",
                    "posting_date": "2026-01-10",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                        {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "100"},
                    ],
                }
            ],
            entry_activities=[None],
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        codes = error_codes(body)
        self.assertIn(("/entries/0", "unbalanced_entry"), codes)
        self.assertNotIn(("/entry_activities/0", "missing_cash_flow_activity"), codes)

        # chart 无效：不派生现金科目的存在性/启用/类别错误。
        payload = request_payload(cash_account_codes=["9999"])
        payload["chart"]["effective_date"] = "2026-13-01"
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        codes = error_codes(body)
        self.assertIn(("/chart/effective_date", "invalid_date"), codes)
        self.assertNotIn(("/cash_account_codes/0", "unknown_account"), codes)

        # 现金科目自身无效：不派生活动一致性错误。
        payload = request_payload(
            cash_account_codes=["9999"],
            entry_activities=[None, None, None, None],
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        codes = error_codes(body)
        self.assertIn(("/cash_account_codes/0", "unknown_account"), codes)
        self.assertFalse(any(code == "missing_cash_flow_activity" for _, code in codes))

    def test_errors_sorted_by_path_and_code(self) -> None:
        payload = request_payload(
            cash_account_codes=["9999"],
            entry_activities=["operating"],
            currency="usd",
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))

    def test_existing_endpoints_still_reject_new_fields(self) -> None:
        # 既有公开入口保持兼容：新字段在旧契约中仍是未知字段。
        payload = request_payload()
        for generate in (
            self.service.generate_trial_balance,
            self.service.generate_financial_statements,
        ):
            status, body = generate(payload)
            self.assertEqual(status, 422)
            self.assertIn(("/cash_account_codes", "unknown_field"), error_codes(body))
            self.assertIn(("/entry_activities", "unknown_field"), error_codes(body))


class GenerateCashFlowStatementHttpTest(unittest.TestCase):
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
            "/v1/cash-flow-statements/generate", request_payload(), "application/json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertIn("cash_flow_statement", payload)
        self.assertIn("ending_cash_balance", payload)

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/cash-flow-statements/generate",
            request_payload(cash_account_codes=[]),
            "application/json",
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/cash_account_codes", "too_few_cash_accounts"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )


if __name__ == "__main__":
    unittest.main()
