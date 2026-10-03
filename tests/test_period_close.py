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
                "code": "1000",
                "name": "Assets",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1001",
                "name": "Cash",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": "1000",
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
                "code": "3002",
                "name": "Retained Earnings",
                "type": "equity",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "3003",
                "name": "Frozen Equity",
                "type": "equity",
                "normal_balance": "credit",
                "active": False,
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


def entry(voucher_id, lines, posting_date="2026-01-15", currency="CNY"):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": currency,
        "lines": lines,
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
            entry(
                "JV-1",
                [
                    {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                    {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "200"},
                ],
                posting_date="2026-01-10",
            ),
            entry(
                "JV-2",
                [
                    {"line_id": "a", "account_code": "5001", "debit": "50", "credit": "0"},
                    {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "50"},
                ],
                posting_date="2026-01-20",
            ),
        ],
        "retained_earnings_account_code": "3002",
        "closing_voucher_id": "CL-1",
    }
    payload.update(overrides)
    return payload


class PeriodCloseServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def generate(self, payload):
        return self.service.generate_period_close(payload)

    def error_pairs(self, payload):
        status, body = self.generate(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_success_closing_entry_and_next_opening(self) -> None:
        status, body = self.generate(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["net_income"], "150.00")

        close = body["closing_entry"]
        self.assertEqual(close["voucher_id"], "CL-1")
        self.assertEqual(close["posting_date"], "2026-01-31")
        self.assertEqual(close["currency"], "CNY")
        self.assertEqual(
            close["lines"],
            [
                {"line_id": "close-1", "account_code": "4001", "debit": "200.00", "credit": "0.00"},
                {"line_id": "close-2", "account_code": "5001", "debit": "0.00", "credit": "50.00"},
                {"line_id": "close-3", "account_code": "3002", "debit": "0.00", "credit": "150.00"},
            ],
        )
        debit_total = sum(int(line["debit"].replace(".", "")) for line in close["lines"])
        credit_total = sum(int(line["credit"].replace(".", "")) for line in close["lines"])
        self.assertEqual(debit_total, credit_total)

        self.assertEqual(
            body["next_opening_balances"],
            [
                {"account_code": "1001", "debit": "1150.00", "credit": "0.00"},
                {"account_code": "3001", "debit": "0.00", "credit": "1000.00"},
                {"account_code": "3002", "debit": "0.00", "credit": "150.00"},
            ],
        )
        # 零余额（父科目 1000、负债 2001、停用权益 3003）一律省略。
        codes = {row["account_code"] for row in body["next_opening_balances"]}
        self.assertNotIn("1000", codes)
        self.assertNotIn("2001", codes)
        self.assertNotIn("3003", codes)

    def test_net_loss_debits_retained_earnings(self) -> None:
        payload = request_payload(
            entries=[
                entry(
                    "JV-9",
                    [
                        {"line_id": "a", "account_code": "5001", "debit": "80", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "80"},
                    ],
                )
            ]
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "-80.00")
        self.assertEqual(
            body["closing_entry"]["lines"],
            [
                {"line_id": "close-1", "account_code": "5001", "debit": "0.00", "credit": "80.00"},
                {"line_id": "close-2", "account_code": "3002", "debit": "80.00", "credit": "0.00"},
            ],
        )
        self.assertEqual(
            body["next_opening_balances"],
            [
                {"account_code": "1001", "debit": "920.00", "credit": "0.00"},
                {"account_code": "3001", "debit": "0.00", "credit": "1000.00"},
                {"account_code": "3002", "debit": "80.00", "credit": "0.00"},
            ],
        )

    def test_negative_revenue_cleared_to_credit(self) -> None:
        # 收入科目本期为借方余额：反向按贷方清零。
        payload = request_payload(
            entries=[
                entry(
                    "JV-9",
                    [
                        {"line_id": "a", "account_code": "4001", "debit": "200", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "200"},
                    ],
                )
            ]
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "-200.00")
        self.assertEqual(
            body["closing_entry"]["lines"],
            [
                {"line_id": "close-1", "account_code": "4001", "debit": "0.00", "credit": "200.00"},
                {"line_id": "close-2", "account_code": "3002", "debit": "200.00", "credit": "0.00"},
            ],
        )

    def test_no_pnl_movement_yields_null_closing_entry(self) -> None:
        status, body = self.generate(request_payload(entries=[]))
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "0.00")
        self.assertIsNone(body["closing_entry"])
        # 留存收益无自身余额且无净利润时同样省略。
        self.assertEqual(
            body["next_opening_balances"],
            [
                {"account_code": "1001", "debit": "1000.00", "credit": "0.00"},
                {"account_code": "3001", "debit": "0.00", "credit": "1000.00"},
            ],
        )

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.generate(request_payload())
        second = self.generate(request_payload())
        self.assertEqual(first, second)

    def test_zero_net_income_with_movement_still_closes(self) -> None:
        # 收入与费用发生额相等：有非零损益行但无留存收益行，借贷仍相等。
        payload = request_payload(
            entries=[
                entry(
                    "JV-1",
                    [
                        {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                        {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "200"},
                    ],
                    posting_date="2026-01-10",
                ),
                entry(
                    "JV-2",
                    [
                        {"line_id": "a", "account_code": "5001", "debit": "200", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "200"},
                    ],
                    posting_date="2026-01-20",
                ),
            ]
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "0.00")
        self.assertEqual(
            body["closing_entry"]["lines"],
            [
                {"line_id": "close-1", "account_code": "4001", "debit": "200.00", "credit": "0.00"},
                {"line_id": "close-2", "account_code": "5001", "debit": "0.00", "credit": "200.00"},
            ],
        )
        # 现金本期净变动为零，留存收益叠加零净利润，期初平衡结构保持不变。
        self.assertEqual(
            body["next_opening_balances"],
            [
                {"account_code": "1001", "debit": "1000.00", "credit": "0.00"},
                {"account_code": "3001", "debit": "0.00", "credit": "1000.00"},
            ],
        )

    # ---- 留存收益科目校验 ----

    def test_retained_earnings_account_must_exist(self) -> None:
        pairs = self.error_pairs(request_payload(retained_earnings_account_code="9999"))
        self.assertIn(("/retained_earnings_account_code", "unknown_account"), pairs)

    def test_retained_earnings_account_must_be_active(self) -> None:
        pairs = self.error_pairs(request_payload(retained_earnings_account_code="3003"))
        self.assertIn(("/retained_earnings_account_code", "inactive_account"), pairs)
        self.assertNotIn(("/retained_earnings_account_code", "retained_earnings_not_equity"), pairs)

    def test_retained_earnings_account_must_be_equity(self) -> None:
        pairs = self.error_pairs(request_payload(retained_earnings_account_code="1001"))
        self.assertIn(("/retained_earnings_account_code", "retained_earnings_not_equity"), pairs)
        pairs = self.error_pairs(request_payload(retained_earnings_account_code="5001"))
        self.assertIn(("/retained_earnings_account_code", "retained_earnings_not_equity"), pairs)

    def test_retained_earnings_field_type_errors(self) -> None:
        pairs = self.error_pairs(request_payload(retained_earnings_account_code=""))
        self.assertIn(("/retained_earnings_account_code", "blank_value"), pairs)
        self.assertNotIn(("/retained_earnings_account_code", "unknown_account"), pairs)

        payload = request_payload()
        del payload["retained_earnings_account_code"]
        pairs = self.error_pairs(payload)
        self.assertIn(("/retained_earnings_account_code", "required"), pairs)

        pairs = self.error_pairs(request_payload(retained_earnings_account_code=3002))
        self.assertIn(("/retained_earnings_account_code", "invalid_type"), pairs)

    def test_retained_reference_not_derived_when_chart_invalid(self) -> None:
        bad_chart = chart()
        del bad_chart["chart_id"]
        pairs = self.error_pairs(
            request_payload(chart=bad_chart, retained_earnings_account_code="9999")
        )
        self.assertIn(("/chart/chart_id", "required"), pairs)
        self.assertNotIn(("/retained_earnings_account_code", "unknown_account"), pairs)

    # ---- 结账凭证号校验 ----

    def test_duplicate_closing_voucher_id(self) -> None:
        pairs = self.error_pairs(request_payload(closing_voucher_id="JV-1"))
        self.assertIn(("/closing_voucher_id", "duplicate_voucher_id"), pairs)

        # 与未使用的凭证号不冲突。
        status, _ = self.generate(request_payload(closing_voucher_id="JV-3"))
        self.assertEqual(status, 200)

    def test_closing_voucher_id_field_errors(self) -> None:
        payload = request_payload()
        del payload["closing_voucher_id"]
        pairs = self.error_pairs(payload)
        self.assertIn(("/closing_voucher_id", "required"), pairs)
        self.assertNotIn(("/closing_voucher_id", "duplicate_voucher_id"), pairs)

        pairs = self.error_pairs(request_payload(closing_voucher_id=""))
        self.assertIn(("/closing_voucher_id", "blank_value"), pairs)

    # ---- 临时科目期初余额 ----

    def test_nonzero_temporary_opening_balance(self) -> None:
        payload = request_payload(
            opening_balances=[
                {"account_code": "1001", "debit": "30.00", "credit": "0"},
                {"account_code": "4001", "debit": "0", "credit": "30.00"},
            ]
        )
        pairs = self.error_pairs(payload)
        self.assertIn(
            ("/opening_balances/1", "nonzero_temporary_opening_balance"), pairs
        )

    def test_temporary_opening_error_not_derived_when_account_unknown(self) -> None:
        # 科目代码本身无效：只报 unknown_account，不派生临时科目余额错误。
        payload = request_payload(
            opening_balances=[
                {"account_code": "1001", "debit": "30.00", "credit": "0"},
                {"account_code": "4999", "debit": "0", "credit": "30.00"},
            ]
        )
        pairs = self.error_pairs(payload)
        self.assertIn(("/opening_balances/1/account_code", "unknown_account"), pairs)
        self.assertNotIn(
            ("/opening_balances/1", "nonzero_temporary_opening_balance"), pairs
        )

    def test_existing_trial_balance_endpoint_rejects_new_fields(self) -> None:
        status, body = self.service.generate_trial_balance(request_payload())
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/retained_earnings_account_code", "unknown_field"), pairs)
        self.assertIn(("/closing_voucher_id", "unknown_field"), pairs)

    def test_errors_sorted_by_path_then_code(self) -> None:
        status, body = self.generate(
            request_payload(
                retained_earnings_account_code="9999",
                closing_voucher_id="JV-1",
                extra="nope",
            )
        )
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        self.assertIn(("/extra", "unknown_field"), set(keys))


class PeriodCloseHttpTest(unittest.TestCase):
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

    def test_generate_over_http(self) -> None:
        status, payload = self.request("/v1/period-closes/generate", request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["net_income"], "150.00")
        self.assertEqual(payload["closing_entry"]["voucher_id"], "CL-1")

    def test_transport_errors_match_existing_endpoints(self) -> None:
        status, payload = self.request(
            "/v1/period-closes/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request("/v1/period-closes/generate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request("/v1/period-closes/generate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/period-closes/generate",
            request_payload(retained_earnings_account_code="9999"),
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/retained_earnings_account_code", "unknown_account"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )


if __name__ == "__main__":
    unittest.main()
