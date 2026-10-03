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
                "name": "Cash",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1002",
                "name": "Prepaid",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1003",
                "name": "Dormant Asset",
                "type": "asset",
                "normal_balance": "debit",
                "active": False,
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


def request_payload(**overrides):
    payload = {
        "contract_id": "CT-1",
        "recognition_type": "revenue",
        "start_date": "2026-01-15",
        "end_date": "2026-03-10",
        "currency": "CNY",
        "total_amount": "100.00",
        "chart": chart(),
        "source_account_code": "2001",
        "target_account_code": "4001",
        "voucher_id_prefix": "REC",
    }
    payload.update(overrides)
    return payload


class RecognitionScheduleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_revenue_schedule_allocates_by_days_with_remainder(self) -> None:
        status, body = self.service.generate_recognition_schedule(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["contract_id"], "CT-1")
        self.assertEqual(body["recognition_type"], "revenue")
        self.assertEqual(body["start_date"], "2026-01-15")
        self.assertEqual(body["end_date"], "2026-03-10")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["total_amount"], "100.00")

        schedule = body["recognition_schedule"]
        # 区间按自然月切段、日期升序，首尾日均计入天数。
        self.assertEqual(
            [(item["period_start"], item["period_end"], item["days"]) for item in schedule],
            [
                ("2026-01-15", "2026-01-31", 17),
                ("2026-02-01", "2026-02-28", 28),
                ("2026-03-01", "2026-03-10", 10),
            ],
        )
        # 17/28/10 天占比，向下取整后余 2 分按小数余数分配（1 月、2 月余数并列
        # 较大，较早月份优先）。
        self.assertEqual(
            [item["amount"] for item in schedule], ["30.91", "50.91", "18.18"]
        )
        total = sum(Decimal(item["amount"]) for item in schedule)
        self.assertEqual(total, Decimal("100.00"))

        first = schedule[0]["entry"]
        self.assertEqual(first["voucher_id"], "REC-202601")
        self.assertEqual(first["posting_date"], "2026-01-31")
        self.assertEqual(first["currency"], "CNY")
        # revenue 模式借记来源、贷记目标。
        self.assertEqual(
            first["lines"],
            [
                {"line_id": "rec-1", "account_code": "2001", "debit": "30.91", "credit": "0.00"},
                {"line_id": "rec-2", "account_code": "4001", "debit": "0.00", "credit": "30.91"},
            ],
        )
        self.assertEqual(schedule[1]["entry"]["voucher_id"], "REC-202602")
        self.assertEqual(schedule[2]["entry"]["voucher_id"], "REC-202603")
        for item in schedule:
            entry_status, entry_body = validate_journal_entry(item["entry"])
            self.assertEqual(entry_status, 200, entry_body)

    def test_expense_schedule_reverses_sides(self) -> None:
        payload = request_payload(
            recognition_type="expense",
            start_date="2026-02-01",
            end_date="2026-02-28",
            total_amount="10",
            source_account_code="1002",
            target_account_code="5001",
        )
        status, body = self.service.generate_recognition_schedule(payload)
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(len(schedule), 1)
        item = schedule[0]
        self.assertEqual(item["days"], 28)
        self.assertEqual(item["amount"], "10.00")
        self.assertEqual(
            item["entry"]["lines"],
            [
                {"line_id": "rec-1", "account_code": "5001", "debit": "10.00", "credit": "0.00"},
                {"line_id": "rec-2", "account_code": "1002", "debit": "0.00", "credit": "10.00"},
            ],
        )

    def test_same_day_period_counts_one_day(self) -> None:
        payload = request_payload(start_date="2026-01-31", end_date="2026-01-31")
        status, body = self.service.generate_recognition_schedule(payload)
        self.assertEqual(status, 200)
        schedule = body["recognition_schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["days"], 1)
        self.assertEqual(schedule[0]["amount"], "100.00")

    def test_deterministic_across_calls(self) -> None:
        first = self.service.generate_recognition_schedule(request_payload())
        second = self.service.generate_recognition_schedule(request_payload())
        self.assertEqual(first, second)

    def test_unknown_field_rejected(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(period_start="2026-01-15")
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/period_start", "unknown_field"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_invalid_recognition_type(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(recognition_type="accrual")
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]],
            [("/recognition_type", "invalid_recognition_type")],
        )

    def test_invalid_period(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(start_date="2026-03-10", end_date="2026-01-15")
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/start_date", "invalid_period"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_invalid_amount(self) -> None:
        for amount in ("0", "0.00", "-5", "1.005", "abc"):
            status, body = self.service.generate_recognition_schedule(
                request_payload(total_amount=amount)
            )
            self.assertEqual(status, 422, amount)
            self.assertIn(
                ("/total_amount", "invalid_amount"),
                {(e["path"], e["code"]) for e in body["errors"]},
                amount,
            )

    def test_generic_field_errors_reuse_existing_codes(self) -> None:
        status, body = self.service.generate_recognition_schedule({})
        self.assertEqual(status, 422)
        codes = {(e["path"], e["code"]) for e in body["errors"]}
        for field in (
            "contract_id",
            "recognition_type",
            "start_date",
            "end_date",
            "currency",
            "total_amount",
            "chart",
            "source_account_code",
            "target_account_code",
            "voucher_id_prefix",
        ):
            self.assertIn((f"/{field}", "required"), codes)

    def test_duplicate_recognition_account(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(target_account_code="2001")
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/target_account_code", "duplicate_recognition_account"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_account_checks_in_order(self) -> None:
        # 存在 -> 启用 -> 类别，每科只报一个。
        status, body = self.service.generate_recognition_schedule(
            request_payload(source_account_code="9999")
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]],
            [("/source_account_code", "unknown_account")],
        )

        broken = chart()
        broken["accounts"][3]["active"] = False
        status, body = self.service.generate_recognition_schedule(
            request_payload(chart=broken)
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]],
            [("/source_account_code", "inactive_account")],
        )

        status, body = self.service.generate_recognition_schedule(
            request_payload(source_account_code="1001")
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]],
            [("/source_account_code", "account_type_mismatch")],
        )

    def test_expense_mode_type_expectations(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(
                recognition_type="expense",
                source_account_code="2001",
                target_account_code="4001",
            )
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            {(e["path"], e["code"]) for e in body["errors"]},
            {
                ("/source_account_code", "account_type_mismatch"),
                ("/target_account_code", "account_type_mismatch"),
            },
        )

    def test_chart_not_effective(self) -> None:
        late = chart()
        late["effective_date"] = "2026-02-01"
        status, body = self.service.generate_recognition_schedule(
            request_payload(chart=late)
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/chart/effective_date", "chart_not_effective"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_chart_field_errors_prefixed(self) -> None:
        broken = chart()
        broken["accounts"] = []
        status, body = self.service.generate_recognition_schedule(
            request_payload(chart=broken)
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/chart/accounts", "too_few_accounts"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_errors_sorted_by_path_and_code(self) -> None:
        status, body = self.service.generate_recognition_schedule(
            request_payload(
                recognition_type="bad",
                total_amount="0",
                extra="x",
            )
        )
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        self.assertFalse(body["valid"])


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

    def request(self, path, body=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request("POST", path, body=data, headers={"Content-Type": content_type})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_route_success(self) -> None:
        status, payload = self.request(
            "/v1/recognition-schedules/generate", request_payload()
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(len(payload["recognition_schedule"]), 3)

    def test_route_422(self) -> None:
        status, payload = self.request(
            "/v1/recognition-schedules/generate", request_payload(total_amount="0")
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])

    def test_route_media_and_json_errors(self) -> None:
        status, payload = self.request(
            "/v1/recognition-schedules/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request("/v1/recognition-schedules/generate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request("/v1/recognition-schedules/generate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
