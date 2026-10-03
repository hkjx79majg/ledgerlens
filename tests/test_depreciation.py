import http.client
import json
import threading
import unittest
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
                "name": "Fixed Asset",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1002",
                "name": "Accumulated Depreciation",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1003",
                "name": "Inactive Asset",
                "type": "asset",
                "normal_balance": "debit",
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
                "name": "Depreciation Expense",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5002",
                "name": "Inactive Expense",
                "type": "expense",
                "normal_balance": "debit",
                "active": False,
                "parent_code": None,
            },
        ],
    }


def request_body(**overrides):
    body = {
        "asset_id": "AS-1",
        "voucher_id_prefix": "DEP",
        "in_service_date": "2026-01-15",
        "currency": "CNY",
        "acquisition_cost": "1200.00",
        "residual_value": "0.00",
        "useful_life_months": 12,
        "chart": chart(),
        "accumulated_depreciation_account_code": "1002",
        "depreciation_expense_account_code": "5001",
    }
    body.update(overrides)
    return body


class DepreciationScheduleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, payload):
        status, body = self.service.generate_depreciation_schedule(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_straight_line_schedule_basic(self) -> None:
        status, body = self.service.generate_depreciation_schedule(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["asset_id"], "AS-1")
        self.assertEqual(body["in_service_date"], "2026-01-15")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["acquisition_cost"], "1200.00")
        self.assertEqual(body["residual_value"], "0.00")
        self.assertEqual(body["useful_life_months"], 12)

        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 12)
        # 计提日为从启用日所在月末起的连续整月。
        self.assertEqual(
            [item["posting_date"] for item in schedule],
            [
                "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30",
                "2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31",
                "2026-09-30", "2026-10-31", "2026-11-30", "2026-12-31",
            ],
        )
        self.assertEqual([item["amount"] for item in schedule], ["100.00"] * 12)
        # 累计折旧逐月累加，账面净值逐月递减。
        for index, item in enumerate(schedule, start=1):
            self.assertEqual(item["accumulated_depreciation"], f"{index * 100}.00")
            self.assertEqual(item["net_book_value"], f"{1200 - index * 100}.00")
        # 期末累计折旧等于成本减残值，账面净值等于残值。
        self.assertEqual(schedule[-1]["accumulated_depreciation"], "1200.00")
        self.assertEqual(schedule[-1]["net_book_value"], "0.00")

        jan = schedule[0]["entry"]
        self.assertEqual(jan["voucher_id"], "DEP-202601")
        self.assertEqual(jan["posting_date"], "2026-01-31")
        self.assertEqual(jan["currency"], "CNY")
        # dep-1 借记费用、dep-2 贷记累计折旧。
        self.assertEqual(
            jan["lines"],
            [
                {"line_id": "dep-1", "account_code": "5001", "debit": "100.00", "credit": "0.00"},
                {"line_id": "dep-2", "account_code": "1002", "debit": "0.00", "credit": "100.00"},
            ],
        )
        self.assertEqual(
            [item["entry"]["voucher_id"] for item in schedule],
            [f"DEP-2026{m:02d}" for m in range(1, 13)],
        )
        for item in schedule:
            entry_status, entry_body = validate_journal_entry(item["entry"])
            self.assertEqual(entry_status, 200, entry_body)

    def test_remainder_cents_go_to_earliest_months(self) -> None:
        # 100.05 / 10 个月：每月 10.00，余 5 分补给最早 5 个月。
        status, body = self.service.generate_depreciation_schedule(
            request_body(acquisition_cost="100.05", useful_life_months=10)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(
            [item["amount"] for item in schedule],
            ["10.01"] * 5 + ["10.00"] * 5,
        )
        self.assertEqual(schedule[-1]["accumulated_depreciation"], "100.05")
        self.assertEqual(schedule[-1]["net_book_value"], "0.00")

    def test_residual_value_is_final_net_book_value(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(acquisition_cost="1000.00", residual_value="100.00", useful_life_months=3)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        # 900.00 / 3 = 每月 300.00。
        self.assertEqual([item["amount"] for item in schedule], ["300.00"] * 3)
        self.assertEqual(
            [item["net_book_value"] for item in schedule],
            ["700.00", "400.00", "100.00"],
        )
        self.assertEqual(schedule[-1]["net_book_value"], "100.00")

    def test_zero_amount_months_kept_with_null_entry(self) -> None:
        # 可折旧 0.02 / 3 个月：最早两个月各 1 分，第三个月为 0.00。
        status, body = self.service.generate_depreciation_schedule(
            request_body(acquisition_cost="10.02", residual_value="10.00", useful_life_months=3)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 3)
        self.assertEqual([item["amount"] for item in schedule], ["0.01", "0.01", "0.00"])
        self.assertIsNone(schedule[2]["entry"])
        self.assertIsNotNone(schedule[0]["entry"])
        self.assertEqual(schedule[2]["posting_date"], "2026-03-31")
        self.assertEqual(schedule[2]["accumulated_depreciation"], "0.02")
        self.assertEqual(schedule[2]["net_book_value"], "10.00")

    def test_leap_year_and_year_boundary(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(
                in_service_date="2024-02-10",
                useful_life_months=13,
                chart={**chart(), "effective_date": "2024-01-01"},
            )
        )
        self.assertEqual(status, 200)
        dates_ = [item["posting_date"] for item in body["depreciation_schedule"]]
        self.assertEqual(dates_[0], "2024-02-29")
        self.assertEqual(dates_[1], "2024-03-31")
        self.assertEqual(dates_[11], "2025-01-31")
        self.assertEqual(dates_[12], "2025-02-28")

    def test_single_month_life(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(acquisition_cost="5.00", useful_life_months=1)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["posting_date"], "2026-01-31")
        self.assertEqual(schedule[0]["amount"], "5.00")
        self.assertEqual(schedule[0]["net_book_value"], "0.00")
        self.assertEqual(schedule[0]["entry"]["voucher_id"], "DEP-202601")

    def test_max_life_still_supported(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(in_service_date="2026-01-01", useful_life_months=1200)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 1200)
        self.assertEqual(schedule[-1]["posting_date"], "2125-12-31")

    def test_deterministic(self) -> None:
        first = self.service.generate_depreciation_schedule(request_body())
        second = self.service.generate_depreciation_schedule(request_body())
        self.assertEqual(first, second)

    def test_unknown_field_rejected(self) -> None:
        errs = self.errors(request_body(extra="x"))
        self.assertIn(("/extra", "unknown_field"), errs)

    def test_missing_fields(self) -> None:
        errs = self.errors({"chart": chart()})
        for field in (
            "asset_id",
            "voucher_id_prefix",
            "in_service_date",
            "currency",
            "acquisition_cost",
            "residual_value",
            "useful_life_months",
            "accumulated_depreciation_account_code",
            "depreciation_expense_account_code",
        ):
            self.assertIn((f"/{field}", "required"), errs)

    def test_invalid_dates_and_currency(self) -> None:
        errs = self.errors(request_body(in_service_date="2026-02-30"))
        self.assertIn(("/in_service_date", "invalid_date"), errs)
        errs = self.errors(request_body(currency="cny"))
        self.assertIn(("/currency", "invalid_currency"), errs)

    def test_invalid_acquisition_cost(self) -> None:
        for bad in ("0", "0.00", "1.234", "abc", "1,000"):
            errs = self.errors(request_body(acquisition_cost=bad))
            self.assertIn(("/acquisition_cost", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(acquisition_cost=100))
        self.assertIn(("/acquisition_cost", "invalid_type"), errs)
        errs = self.errors(request_body(acquisition_cost=""))
        self.assertIn(("/acquisition_cost", "blank_value"), errs)

    def test_invalid_residual_value(self) -> None:
        for bad in ("1.234", "abc", "-1"):
            errs = self.errors(request_body(residual_value=bad))
            self.assertIn(("/residual_value", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(residual_value=0))
        self.assertIn(("/residual_value", "invalid_type"), errs)

    def test_residual_not_less_than_cost(self) -> None:
        errs = self.errors(
            request_body(acquisition_cost="100.00", residual_value="100.00")
        )
        self.assertIn(("/residual_value", "residual_not_less_than_cost"), errs)
        errs = self.errors(
            request_body(acquisition_cost="100.00", residual_value="100.01")
        )
        self.assertIn(("/residual_value", "residual_not_less_than_cost"), errs)

    def test_residual_comparison_requires_valid_amounts(self) -> None:
        # 成本格式非法时不派生 residual_not_less_than_cost。
        errs = self.errors(
            request_body(acquisition_cost="oops", residual_value="100.00")
        )
        self.assertIn(("/acquisition_cost", "invalid_amount"), errs)
        self.assertNotIn(("/residual_value", "residual_not_less_than_cost"), errs)

    def test_invalid_useful_life(self) -> None:
        for bad in (0, -1, 1201, 10000):
            errs = self.errors(request_body(useful_life_months=bad))
            self.assertIn(("/useful_life_months", "invalid_useful_life"), errs, bad)
        for bad in ("12", 1.5, True, False, None):
            errs = self.errors(request_body(useful_life_months=bad))
            self.assertIn(("/useful_life_months", "invalid_type"), errs, bad)

    def test_schedule_out_of_range(self) -> None:
        # 9999-02 起计提 12 个月，末月为 10000-01，超出日历范围。
        errs = self.errors(
            request_body(in_service_date="9999-02-01", useful_life_months=12)
        )
        self.assertIn(("/in_service_date", "schedule_out_of_range"), errs)
        # 11 个月时末月仍在 9999 年内，正常。
        status, _ = self.service.generate_depreciation_schedule(
            request_body(in_service_date="9999-02-01", useful_life_months=11)
        )
        self.assertEqual(status, 200)

    def test_out_of_range_requires_valid_life_and_date(self) -> None:
        errs = self.errors(
            request_body(in_service_date="9999-02-01", useful_life_months=1201)
        )
        self.assertIn(("/useful_life_months", "invalid_useful_life"), errs)
        self.assertNotIn(("/in_service_date", "schedule_out_of_range"), errs)

    def test_duplicate_depreciation_account(self) -> None:
        errs = self.errors(
            request_body(
                accumulated_depreciation_account_code="1002",
                depreciation_expense_account_code="1002",
            )
        )
        self.assertIn(
            ("/depreciation_expense_account_code", "duplicate_depreciation_account"),
            errs,
        )

    def test_unknown_and_inactive_accounts(self) -> None:
        errs = self.errors(request_body(accumulated_depreciation_account_code="9999"))
        self.assertIn(
            ("/accumulated_depreciation_account_code", "unknown_account"), errs
        )
        errs = self.errors(request_body(depreciation_expense_account_code="9999"))
        self.assertIn(
            ("/depreciation_expense_account_code", "unknown_account"), errs
        )
        errs = self.errors(request_body(accumulated_depreciation_account_code="1003"))
        self.assertIn(
            ("/accumulated_depreciation_account_code", "inactive_account"), errs
        )
        errs = self.errors(request_body(depreciation_expense_account_code="5002"))
        self.assertIn(
            ("/depreciation_expense_account_code", "inactive_account"), errs
        )

    def test_account_type_mismatch(self) -> None:
        # 累计折旧科目须为 asset，折旧费用科目须为 expense。
        errs = self.errors(request_body(accumulated_depreciation_account_code="5001"))
        self.assertIn(
            ("/accumulated_depreciation_account_code", "account_type_mismatch"), errs
        )
        errs = self.errors(request_body(depreciation_expense_account_code="1002"))
        self.assertIn(
            ("/depreciation_expense_account_code", "account_type_mismatch"), errs
        )
        errs = self.errors(request_body(accumulated_depreciation_account_code="4001"))
        self.assertIn(
            ("/accumulated_depreciation_account_code", "account_type_mismatch"), errs
        )

    def test_account_errors_suppressed_without_valid_chart(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        errs = self.errors(request_body(chart=bad_chart))
        self.assertIn(("/chart/accounts", "too_few_accounts"), errs)
        self.assertNotIn(
            ("/accumulated_depreciation_account_code", "unknown_account"), errs
        )
        self.assertNotIn(
            ("/depreciation_expense_account_code", "unknown_account"), errs
        )

    def test_chart_not_effective(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "2026-02-01"
        errs = self.errors(request_body(chart=late_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), errs)
        # 生效日恰为启用日可以。
        same_day = chart()
        same_day["effective_date"] = "2026-01-15"
        status, _ = self.service.generate_depreciation_schedule(
            request_body(chart=same_day)
        )
        self.assertEqual(status, 200)

    def test_chart_field_errors_prefixed(self) -> None:
        errs = self.errors(request_body(chart=[]))
        self.assertIn(("/chart", "invalid_type"), errs)
        errs = self.errors({k: v for k, v in request_body().items() if k != "chart"})
        self.assertIn(("/chart", "required"), errs)

    def test_errors_sorted_by_path_and_code(self) -> None:
        status, body = self.service.generate_depreciation_schedule({})
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))


class DepreciationScheduleHttpTest(unittest.TestCase):
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
            "/v1/depreciation-schedules/generate",
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
        self.assertEqual(len(payload["depreciation_schedule"]), 12)

    def test_route_422(self) -> None:
        status, payload = self.post(request_body(acquisition_cost="0"))
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/acquisition_cost", "invalid_amount"),
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
