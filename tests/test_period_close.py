import http.client
import json
import threading
import unittest
from decimal import Decimal
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
                "name": "Retained Earnings",
                "type": "equity",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "3002",
                "name": "Dormant Equity",
                "type": "equity",
                "normal_balance": "credit",
                "active": False,
                "parent_code": None,
            },
            {
                "code": "4001",
                "name": "Revenue",
                "type": "revenue",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5001",
                "name": "Expense",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
        ],
    }


def revenue_entry(amount="300", voucher_id="JV-1", posting_date="2026-01-10"):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": "CNY",
        "lines": [
            {"line_id": "a", "account_code": "1001", "debit": amount, "credit": "0"},
            {"line_id": "b", "account_code": "4001", "debit": "0", "credit": amount},
        ],
    }


def expense_entry(amount="100", voucher_id="JV-2", posting_date="2026-01-20"):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": "CNY",
        "lines": [
            {"line_id": "a", "account_code": "5001", "debit": amount, "credit": "0"},
            {"line_id": "b", "account_code": "1001", "debit": "0", "credit": amount},
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
            {"account_code": "2001", "debit": "0", "credit": "400.00"},
            {"account_code": "3001", "debit": "0", "credit": "600.00"},
        ],
        "entries": [revenue_entry(), expense_entry()],
        "retained_earnings_account_code": "3001",
        "closing_voucher_id": "CLS-1",
    }
    payload.update(overrides)
    return payload


def assert_balanced(lines):
    debit = sum(Decimal(line["debit"]) for line in lines)
    credit = sum(Decimal(line["credit"]) for line in lines)
    assert debit == credit, (debit, credit)
    return debit, credit


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

    def test_profit_generates_balanced_closing_entry(self) -> None:
        status, body = self.generate(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["net_income"], "200.00")

        entry = body["closing_entry"]
        self.assertEqual(entry["voucher_id"], "CLS-1")
        self.assertEqual(entry["posting_date"], "2026-01-31")
        self.assertEqual(entry["currency"], "CNY")

        lines = entry["lines"]
        # 损益行按 account_code 排序，留存收益行置后；line_id 依次编号。
        self.assertEqual(
            [line["account_code"] for line in lines], ["4001", "5001", "3001"]
        )
        self.assertEqual([line["line_id"] for line in lines], ["close-1", "close-2", "close-3"])
        # 收入借记、费用贷记、利润贷记留存收益；金额非负两位小数且仅一侧大于零。
        self.assertEqual(lines[0], {"line_id": "close-1", "account_code": "4001", "debit": "300.00", "credit": "0.00"})
        self.assertEqual(lines[1], {"line_id": "close-2", "account_code": "5001", "debit": "0.00", "credit": "100.00"})
        self.assertEqual(lines[2], {"line_id": "close-3", "account_code": "3001", "debit": "0.00", "credit": "200.00"})
        assert_balanced(lines)

    def test_loss_debits_retained_earnings(self) -> None:
        status, body = self.generate(request_payload(entries=[expense_entry("100")]))
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "-100.00")
        lines = body["closing_entry"]["lines"]
        self.assertEqual(
            [line["account_code"] for line in lines], ["5001", "3001"]
        )
        self.assertEqual(lines[0], {"line_id": "close-1", "account_code": "5001", "debit": "0.00", "credit": "100.00"})
        self.assertEqual(lines[1], {"line_id": "close-2", "account_code": "3001", "debit": "100.00", "credit": "0.00"})
        assert_balanced(lines)

    def test_zero_net_income_omits_retained_earnings_line(self) -> None:
        status, body = self.generate(
            request_payload(entries=[revenue_entry("100"), expense_entry("100")])
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "0.00")
        lines = body["closing_entry"]["lines"]
        self.assertEqual(
            [line["account_code"] for line in lines], ["4001", "5001"]
        )
        self.assertEqual([line["line_id"] for line in lines], ["close-1", "close-2"])
        assert_balanced(lines)

    def test_no_movements_yields_null_closing_entry(self) -> None:
        status, body = self.generate(request_payload(entries=[]))
        self.assertEqual(status, 200)
        self.assertEqual(body["net_income"], "0.00")
        self.assertIsNone(body["closing_entry"])

    def test_next_opening_balances_only_permanent_accounts(self) -> None:
        status, body = self.generate(request_payload())
        self.assertEqual(status, 200)
        rows = body["next_opening_balances"]
        # 只含资产/负债/权益，按 account_code 排序；损益科目不出现。
        self.assertEqual(
            [row["account_code"] for row in rows], ["1001", "2001", "3001"]
        )
        # 现金 1000 +300 -100 = 1200 借；负债 400 贷；留存收益 600 + 利润 200 = 800 贷。
        self.assertEqual(rows[0], {"account_code": "1001", "debit": "1200.00", "credit": "0.00"})
        self.assertEqual(rows[1], {"account_code": "2001", "debit": "0.00", "credit": "400.00"})
        self.assertEqual(rows[2], {"account_code": "3001", "debit": "0.00", "credit": "800.00"})
        # 抵销为零的资产负债表科目被省略；每行仅一侧大于零。
        for row in rows:
            self.assertTrue((Decimal(row["debit"]) > 0) ^ (Decimal(row["credit"]) > 0))

    def test_loss_reduces_retained_earnings_in_next_opening(self) -> None:
        status, body = self.generate(request_payload(entries=[expense_entry("100")]))
        self.assertEqual(status, 200)
        by_code = {row["account_code"]: row for row in body["next_opening_balances"]}
        # 现金 1000 - 100 = 900 借；留存收益 600 - 亏损 100 = 500 贷。
        self.assertEqual(by_code["1001"]["debit"], "900.00")
        self.assertEqual(by_code["3001"]["credit"], "500.00")

    def test_retained_earnings_account_validation_chain(self) -> None:
        self.assertIn(
            ("/retained_earnings_account_code", "unknown_account"),
            self.error_pairs(request_payload(retained_earnings_account_code="9999")),
        )
        self.assertIn(
            ("/retained_earnings_account_code", "inactive_account"),
            self.error_pairs(request_payload(retained_earnings_account_code="3002")),
        )
        for code in ("1001", "4001"):
            pairs = self.error_pairs(request_payload(retained_earnings_account_code=code))
            self.assertIn(
                ("/retained_earnings_account_code", "retained_earnings_not_equity"),
                pairs,
                code,
            )

    def test_new_fields_required_typed_and_nonblank(self) -> None:
        payload = request_payload()
        del payload["retained_earnings_account_code"]
        del payload["closing_voucher_id"]
        pairs = self.error_pairs(payload)
        self.assertIn(("/retained_earnings_account_code", "required"), pairs)
        self.assertIn(("/closing_voucher_id", "required"), pairs)

        pairs = self.error_pairs(
            request_payload(retained_earnings_account_code=5, closing_voucher_id=None)
        )
        self.assertIn(("/retained_earnings_account_code", "invalid_type"), pairs)
        self.assertIn(("/closing_voucher_id", "invalid_type"), pairs)

        pairs = self.error_pairs(
            request_payload(retained_earnings_account_code="", closing_voucher_id="")
        )
        self.assertIn(("/retained_earnings_account_code", "blank_value"), pairs)
        self.assertIn(("/closing_voucher_id", "blank_value"), pairs)

    def test_duplicate_voucher_id(self) -> None:
        pairs = self.error_pairs(request_payload(closing_voucher_id="JV-1"))
        self.assertIn(("/closing_voucher_id", "duplicate_voucher_id"), pairs)

        # 输入凭证 voucher_id 字段无效（空白）时不派生重复错误。
        bad_entry = revenue_entry("JV-1")
        bad_entry["voucher_id"] = ""
        pairs = self.error_pairs(
            request_payload(entries=[bad_entry], closing_voucher_id="JV-1")
        )
        self.assertNotIn(("/closing_voucher_id", "duplicate_voucher_id"), pairs)
        self.assertIn(("/entries/0/voucher_id", "blank_value"), pairs)

    def test_nonzero_temporary_opening_balance(self) -> None:
        payload = request_payload(
            opening_balances=[
                {"account_code": "1001", "debit": "1030.00", "credit": "0"},
                {"account_code": "4001", "debit": "30.00", "credit": "0"},
                {"account_code": "3001", "debit": "0", "credit": "1060.00"},
            ]
        )
        pairs = self.error_pairs(payload)
        self.assertIn(
            ("/opening_balances/1", "nonzero_temporary_opening_balance"), pairs
        )

    def test_temporary_balance_error_suppressed_when_dependencies_invalid(self) -> None:
        # 期初项金额字段无效时不派生 nonzero_temporary_opening_balance。
        payload = request_payload(
            opening_balances=[
                {"account_code": "4001", "debit": "abc", "credit": "0"},
            ]
        )
        pairs = self.error_pairs(payload)
        self.assertNotIn(
            ("/opening_balances/0", "nonzero_temporary_opening_balance"), pairs
        )

        # 科目体系无效时不派生留存收益引用错误与期初类错误。
        bad_chart = chart()
        bad_chart["accounts"][0]["name"] = ""
        payload = request_payload(
            chart=bad_chart,
            retained_earnings_account_code="9999",
            opening_balances=[
                {"account_code": "4001", "debit": "1.00", "credit": "0"},
            ],
        )
        pairs = self.error_pairs(payload)
        self.assertNotIn(("/retained_earnings_account_code", "unknown_account"), pairs)
        self.assertFalse(
            any(code == "nonzero_temporary_opening_balance" for _, code in pairs)
        )

    def test_reference_error_suppressed_when_field_invalid(self) -> None:
        pairs = self.error_pairs(request_payload(retained_earnings_account_code=123))
        self.assertIn(("/retained_earnings_account_code", "invalid_type"), pairs)
        self.assertFalse(
            any(
                path == "/retained_earnings_account_code"
                and code in ("unknown_account", "inactive_account", "retained_earnings_not_equity")
                for path, code in pairs
            )
        )

    def test_reuses_base_trial_balance_validation(self) -> None:
        # 新增端点完整复用试算平衡表的字段级与跨对象校验。
        pairs = self.error_pairs(request_payload(period_start="2026-02-01", period_end="2026-01-01"))
        self.assertIn(("/period_start", "invalid_period"), pairs)

        pairs = self.error_pairs(
            request_payload(entries=[{**revenue_entry(), "currency": "USD"}])
        )
        self.assertIn(("/entries/0/currency", "currency_mismatch"), pairs)

        pairs = self.error_pairs(request_payload(nope="x"))
        self.assertIn(("/nope", "unknown_field"), pairs)

    def test_errors_sorted_by_path_then_code(self) -> None:
        payload = request_payload(
            period_start="2026-02-01",
            period_end="2026-01-01",
            retained_earnings_account_code="9999",
            closing_voucher_id="JV-1",
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))


class ExistingEndpointsUnaffectedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_trial_balance_rejects_new_fields(self) -> None:
        payload = request_payload()
        status, body = self.service.generate_trial_balance(payload)
        self.assertEqual(status, 422)
        codes = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/retained_earnings_account_code", "unknown_field"), codes)
        self.assertIn(("/closing_voucher_id", "unknown_field"), codes)

    def test_stateless_and_deterministic(self) -> None:
        first = self.service.generate_period_close(request_payload())
        second = self.service.generate_period_close(request_payload())
        self.assertEqual(first, second)


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
        self.assertEqual(payload["net_income"], "200.00")
        self.assertIsNotNone(payload["closing_entry"])

    def test_business_failure_is_422(self) -> None:
        status, payload = self.request(
            "/v1/period-closes/generate",
            request_payload(retained_earnings_account_code="9999"),
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])

    def test_transport_errors_match_existing_endpoints(self) -> None:
        status, payload = self.request("/v1/period-closes/generate", "{}", "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request("/v1/period-closes/generate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request("/v1/period-closes/generate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

        status, payload = self.request("/v1/period-closes/generate", {}, "application/json")
        # Unknown route test lives separately; here a known POST route with empty body is 422.
        self.assertIn(status, (422,))


if __name__ == "__main__":
    unittest.main()
