import http.client
import json
import threading
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer

from ledgerlens.journal import validate_journal_entry
from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart():
    return {
        "chart_id": "COA-1",
        "effective_date": "2026-01-01",
        "accounts": [
            {
                "code": "1001",
                "name": "Prepaid Expense",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "2001",
                "name": "Deferred Revenue",
                "type": "liability",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "2002",
                "name": "Dormant Liability",
                "type": "liability",
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


def request_body(**overrides):
    body = {
        "contract_id": "CT-1",
        "recognition_type": "revenue",
        "start_date": "2026-01-15",
        "end_date": "2026-03-10",
        "currency": "CNY",
        "total_amount": "1000",
        "chart": chart(),
        "source_account_code": "2001",
        "target_account_code": "4001",
        "voucher_id_prefix": "RV",
    }
    body.update(overrides)
    return body


class RecognitionScheduleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, payload):
        status, body = self.service.generate_recognition_schedule(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_revenue_schedule_allocates_by_days(self) -> None:
        status, body = self.service.generate_recognition_schedule(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["contract_id"], "CT-1")
        self.assertEqual(body["recognition_type"], "revenue")
        self.assertEqual(body["start_date"], "2026-01-15")
        self.assertEqual(body["end_date"], "2026-03-10")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["total_amount"], "1000.00")

        schedule = body["recognition_schedule"]
        self.assertEqual(
            [(item["period_start"], item["period_end"], item["days"]) for item in schedule],
            [("2026-01-15", "2026-01-31", 17), ("2026-02-01", "2026-02-28", 28), ("2026-03-01", "2026-03-10", 10)],
        )
        # 17/28/10 天共 55 天：份额向下取整后余 1 分，补给小数余数最大的 3 月。
        self.assertEqual(
            [item["amount"] for item in schedule],
            ["309.09", "509.09", "181.82"],
        )
        total = sum(Decimal(item["amount"]) for item in schedule)
        self.assertEqual(total, Decimal("1000.00"))

        # revenue：借记来源（liability）、贷记目标（revenue）。
        jan = schedule[0]["entry"]
        self.assertEqual(jan["voucher_id"], "RV-202601")
        self.assertEqual(jan["posting_date"], "2026-01-31")
        self.assertEqual(jan["currency"], "CNY")
        self.assertEqual(
            jan["lines"],
            [
                {"line_id": "rec-1", "account_code": "2001", "debit": "309.09", "credit": "0.00"},
                {"line_id": "rec-2", "account_code": "4001", "debit": "0.00", "credit": "309.09"},
            ],
        )
        self.assertEqual([item["entry"]["voucher_id"] for item in schedule], ["RV-202601", "RV-202602", "RV-202603"])
        for item in schedule:
            entry_status, entry_body = validate_journal_entry(item["entry"])
            self.assertEqual(entry_status, 200, entry_body)

    def test_expense_schedule_debits_target(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_body(
                recognition_type="expense",
                start_date="2026-02-01",
                end_date="2026-02-28",
                total_amount="99.9",
                source_account_code="1001",
                target_account_code="5001",
                voucher_id_prefix="EXP",
            )
        )
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["days"], 28)
        self.assertEqual(schedule[0]["amount"], "99.90")
        entry = schedule[0]["entry"]
        self.assertEqual(entry["voucher_id"], "EXP-202602")
        self.assertEqual(entry["posting_date"], "2026-02-28")
        self.assertEqual(
            entry["lines"],
            [
                {"line_id": "rec-1", "account_code": "5001", "debit": "99.90", "credit": "0.00"},
                {"line_id": "rec-2", "account_code": "1001", "debit": "0.00", "credit": "99.90"},
            ],
        )

    def test_remainder_tie_prefers_earlier_month_and_drops_zero_segment(self) -> None:
        # 两段各 28 天、总额 0.01：余数相同，较早月份优先分得 1 分，
        # 另一段为 0.00 不出现在计划中。
        status, body = self.service.generate_recognition_schedule(
            request_body(
                start_date="2026-02-01",
                end_date="2026-03-28",
                total_amount="0.01",
            )
        )
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["period_start"], "2026-02-01")
        self.assertEqual(schedule[0]["period_end"], "2026-02-28")
        self.assertEqual(schedule[0]["amount"], "0.01")

    def test_single_day_period(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_body(start_date="2026-12-31", end_date="2026-12-31", total_amount="5")
        )
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["days"], 1)
        self.assertEqual(schedule[0]["amount"], "5.00")
        self.assertEqual(schedule[0]["entry"]["voucher_id"], "RV-202612")

    def test_year_boundary_segments(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_body(start_date="2026-12-15", end_date="2027-01-15", total_amount="62")
        )
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(
            [(item["period_start"], item["period_end"], item["days"]) for item in schedule],
            [("2026-12-15", "2026-12-31", 17), ("2027-01-01", "2027-01-15", 15)],
        )
        self.assertEqual([item["entry"]["voucher_id"] for item in schedule], ["RV-202612", "RV-202701"])
        total = sum(Decimal(item["amount"]) for item in schedule)
        self.assertEqual(total, Decimal("62.00"))

    def test_deterministic(self) -> None:
        first = self.service.generate_recognition_schedule(request_body())
        second = self.service.generate_recognition_schedule(request_body())
        self.assertEqual(first, second)

    def test_unknown_field_rejected(self) -> None:
        errs = self.errors(request_body(extra="x"))
        self.assertIn(("/extra", "unknown_field"), errs)

    def test_missing_fields(self) -> None:
        errs = self.errors({"chart": chart()})
        for field in (
            "contract_id",
            "recognition_type",
            "start_date",
            "end_date",
            "currency",
            "total_amount",
            "source_account_code",
            "target_account_code",
            "voucher_id_prefix",
        ):
            self.assertIn((f"/{field}", "required"), errs)

    def test_invalid_recognition_type(self) -> None:
        errs = self.errors(request_body(recognition_type="accrual"))
        self.assertIn(("/recognition_type", "invalid_recognition_type"), errs)
        # recognition_type 无效时不派生类别错误。
        self.assertNotIn(("/source_account_code", "account_type_mismatch"), errs)

    def test_invalid_period(self) -> None:
        errs = self.errors(request_body(start_date="2026-03-10", end_date="2026-01-15"))
        self.assertIn(("/start_date", "invalid_period"), errs)

    def test_invalid_dates(self) -> None:
        errs = self.errors(request_body(start_date="2026-02-30"))
        self.assertIn(("/start_date", "invalid_date"), errs)
        errs = self.errors(request_body(end_date="2026-13-01"))
        self.assertIn(("/end_date", "invalid_date"), errs)

    def test_invalid_amount(self) -> None:
        for bad in ("0", "0.00", "1.234", "abc", "1,000"):
            errs = self.errors(request_body(total_amount=bad))
            self.assertIn(("/total_amount", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(total_amount=100))
        self.assertIn(("/total_amount", "invalid_type"), errs)
        errs = self.errors(request_body(total_amount=""))
        self.assertIn(("/total_amount", "blank_value"), errs)

    def test_invalid_currency(self) -> None:
        errs = self.errors(request_body(currency="cny"))
        self.assertIn(("/currency", "invalid_currency"), errs)

    def test_duplicate_recognition_account(self) -> None:
        errs = self.errors(request_body(target_account_code="2001"))
        self.assertIn(("/target_account_code", "duplicate_recognition_account"), errs)

    def test_unknown_and_inactive_accounts(self) -> None:
        errs = self.errors(request_body(source_account_code="9999"))
        self.assertIn(("/source_account_code", "unknown_account"), errs)
        errs = self.errors(request_body(source_account_code="2002"))
        self.assertIn(("/source_account_code", "inactive_account"), errs)

    def test_account_type_mismatch(self) -> None:
        # revenue：来源须为 liability、目标须为 revenue。
        errs = self.errors(request_body(source_account_code="1001"))
        self.assertIn(("/source_account_code", "account_type_mismatch"), errs)
        errs = self.errors(request_body(target_account_code="5001"))
        self.assertIn(("/target_account_code", "account_type_mismatch"), errs)
        # expense：来源须为 asset、目标须为 expense。
        errs = self.errors(
            request_body(
                recognition_type="expense",
                source_account_code="2001",
                target_account_code="4001",
            )
        )
        self.assertIn(("/source_account_code", "account_type_mismatch"), errs)
        self.assertIn(("/target_account_code", "account_type_mismatch"), errs)

    def test_chart_errors_prefixed(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        errs = self.errors(request_body(chart=bad_chart))
        self.assertIn(("/chart/accounts", "too_few_accounts"), errs)

    def test_chart_not_effective(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "2026-02-01"
        errs = self.errors(request_body(chart=late_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), errs)

    def test_errors_sorted_by_path_and_code(self) -> None:
        status, body = self.service.generate_recognition_schedule({})
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))


class RecognitionScheduleHttpTest(unittest.TestCase):
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

    def post(self, body, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request(
            "POST",
            "/v1/recognition-schedules/generate",
            body=data,
            headers={"Content-Type": content_type},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_route_success(self) -> None:
        status, payload = self.post(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(len(payload["recognition_schedule"]), 3)

    def test_route_422(self) -> None:
        status, payload = self.post(request_body(total_amount="0"))
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/total_amount", "invalid_amount"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )

    def test_media_type_and_json_errors(self) -> None:
        status, payload = self.post("{}", "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.post("{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.post("[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
