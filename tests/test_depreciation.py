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
                "code": "1601",
                "name": "Accumulated Depreciation",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1602",
                "name": "Dormant Asset",
                "type": "asset",
                "normal_balance": "debit",
                "active": False,
                "parent_code": None,
            },
            {
                "code": "2001",
                "name": "Liability",
                "type": "liability",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "5601",
                "name": "Depreciation Expense",
                "type": "expense",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
        ],
    }


def request_body(**overrides):
    body = {
        "asset_id": "FA-1",
        "voucher_id_prefix": "DEP",
        "in_service_date": "2026-01-15",
        "currency": "CNY",
        "acquisition_cost": "1200",
        "residual_value": "0",
        "useful_life_months": 12,
        "chart": chart(),
        "accumulated_depreciation_account_code": "1601",
        "depreciation_expense_account_code": "5601",
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

    def test_straight_line_schedule(self) -> None:
        status, body = self.service.generate_depreciation_schedule(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["asset_id"], "FA-1")
        self.assertEqual(body["in_service_date"], "2026-01-15")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["acquisition_cost"], "1200.00")
        self.assertEqual(body["residual_value"], "0.00")
        self.assertEqual(body["useful_life_months"], 12)

        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 12)
        # 从启用日所在月月末起连续整月计提，日期升序。
        self.assertEqual(schedule[0]["depreciation_date"], "2026-01-31")
        self.assertEqual(schedule[-1]["depreciation_date"], "2026-12-31")
        dates = [item["depreciation_date"] for item in schedule]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual({item["amount"] for item in schedule}, {"100.00"})
        self.assertEqual(schedule[-1]["accumulated_depreciation"], "1200.00")
        self.assertEqual(schedule[-1]["net_book_value"], "0.00")
        self.assertEqual(
            [item["entry"]["voucher_id"] for item in schedule],
            [f"DEP-2026{m:02d}" for m in range(1, 13)],
        )
        first = schedule[0]["entry"]
        self.assertEqual(first["posting_date"], "2026-01-31")
        self.assertEqual(first["currency"], "CNY")
        self.assertEqual(
            first["lines"],
            [
                {"line_id": "dep-1", "account_code": "5601", "debit": "100.00", "credit": "0.00"},
                {"line_id": "dep-2", "account_code": "1601", "debit": "0.00", "credit": "100.00"},
            ],
        )
        for item in schedule:
            entry_status, entry_body = validate_journal_entry(item["entry"])
            self.assertEqual(entry_status, 200, entry_body)

    def test_remainder_goes_to_earliest_months(self) -> None:
        # 可折旧金额 100.00 分 3 个月：33.34 / 33.33 / 33.33。
        status, body = self.service.generate_depreciation_schedule(
            request_body(
                acquisition_cost="100",
                useful_life_months=3,
                in_service_date="2026-02-01",
            )
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(
            [item["amount"] for item in schedule],
            ["33.34", "33.33", "33.33"],
        )
        self.assertEqual(
            [item["accumulated_depreciation"] for item in schedule],
            ["33.34", "66.67", "100.00"],
        )
        self.assertEqual(
            [item["net_book_value"] for item in schedule],
            ["66.66", "33.33", "0.00"],
        )
        total = sum(Decimal(item["amount"]) for item in schedule)
        self.assertEqual(total, Decimal("100.00"))

    def test_residual_value_caps_accumulated_depreciation(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(
                acquisition_cost="1000",
                residual_value="100",
                useful_life_months=2,
            )
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual([item["amount"] for item in schedule], ["450.00", "450.00"])
        self.assertEqual(schedule[-1]["accumulated_depreciation"], "900.00")
        self.assertEqual(schedule[-1]["net_book_value"], "100.00")

    def test_zero_amount_month_kept_with_null_entry(self) -> None:
        # 可折旧金额 0.01 分 2 个月：首月 0.01，次月 0.00 保留且 entry 为 null。
        status, body = self.service.generate_depreciation_schedule(
            request_body(
                acquisition_cost="0.01",
                residual_value="0",
                useful_life_months=2,
            )
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(len(schedule), 2)
        self.assertEqual(schedule[0]["amount"], "0.01")
        self.assertIsNotNone(schedule[0]["entry"])
        self.assertEqual(schedule[1]["amount"], "0.00")
        self.assertIsNone(schedule[1]["entry"])
        self.assertEqual(schedule[1]["accumulated_depreciation"], "0.01")
        self.assertEqual(schedule[1]["net_book_value"], "0.00")

    def test_year_boundary_months(self) -> None:
        status, body = self.service.generate_depreciation_schedule(
            request_body(in_service_date="2026-12-20", useful_life_months=2)
        )
        self.assertEqual(status, 200)
        schedule = body["depreciation_schedule"]
        self.assertEqual(
            [item["depreciation_date"] for item in schedule],
            ["2026-12-31", "2027-01-31"],
        )
        self.assertEqual(
            [item["entry"]["voucher_id"] for item in schedule],
            ["DEP-202612", "DEP-202701"],
        )

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

    def test_invalid_amounts(self) -> None:
        for bad in ("0", "0.00", "1.234", "abc", "1,000", "-1"):
            errs = self.errors(request_body(acquisition_cost=bad))
            self.assertIn(("/acquisition_cost", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(acquisition_cost=100))
        self.assertIn(("/acquisition_cost", "invalid_type"), errs)
        errs = self.errors(request_body(acquisition_cost=""))
        self.assertIn(("/acquisition_cost", "blank_value"), errs)
        for bad in ("1.234", "abc"):
            errs = self.errors(request_body(residual_value=bad))
            self.assertIn(("/residual_value", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(residual_value=0))
        self.assertIn(("/residual_value", "invalid_type"), errs)

    def test_residual_not_less_than_cost(self) -> None:
        errs = self.errors(request_body(acquisition_cost="100", residual_value="100"))
        self.assertIn(("/residual_value", "residual_not_less_than_cost"), errs)
        errs = self.errors(request_body(acquisition_cost="100", residual_value="100.01"))
        self.assertIn(("/residual_value", "residual_not_less_than_cost"), errs)
        # 成本本身无效时不派生残值比较错误。
        errs = self.errors(request_body(acquisition_cost="0", residual_value="0"))
        self.assertNotIn(("/residual_value", "residual_not_less_than_cost"), errs)

    def test_invalid_useful_life(self) -> None:
        for bad in (0, -1, 1201):
            errs = self.errors(request_body(useful_life_months=bad))
            self.assertIn(("/useful_life_months", "invalid_useful_life"), errs, bad)
        for bad in ("12", 1.5, True, None):
            errs = self.errors(request_body(useful_life_months=bad))
            self.assertIn(("/useful_life_months", "invalid_type"), errs, repr(bad))

    def test_schedule_out_of_range(self) -> None:
        errs = self.errors(
            request_body(in_service_date="9999-06-15", useful_life_months=12)
        )
        self.assertIn(("/useful_life_months", "schedule_out_of_range"), errs)
        # 边界：末月落在 9999-12 仍可生成。
        status, body = self.service.generate_depreciation_schedule(
            request_body(in_service_date="9999-01-15", useful_life_months=12)
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["depreciation_schedule"][-1]["depreciation_date"], "9999-12-31")

    def test_invalid_date_and_currency(self) -> None:
        errs = self.errors(request_body(in_service_date="2026-02-30"))
        self.assertIn(("/in_service_date", "invalid_date"), errs)
        errs = self.errors(request_body(currency="cny"))
        self.assertIn(("/currency", "invalid_currency"), errs)

    def test_duplicate_depreciation_account(self) -> None:
        errs = self.errors(request_body(depreciation_expense_account_code="1601"))
        self.assertIn(
            ("/depreciation_expense_account_code", "duplicate_depreciation_account"), errs
        )

    def test_unknown_and_inactive_accounts(self) -> None:
        errs = self.errors(request_body(accumulated_depreciation_account_code="9999"))
        self.assertIn(("/accumulated_depreciation_account_code", "unknown_account"), errs)
        errs = self.errors(request_body(accumulated_depreciation_account_code="1602"))
        self.assertIn(("/accumulated_depreciation_account_code", "inactive_account"), errs)
        errs = self.errors(request_body(depreciation_expense_account_code="9999"))
        self.assertIn(("/depreciation_expense_account_code", "unknown_account"), errs)

    def test_account_type_mismatch(self) -> None:
        # 累计折旧科目须为 asset、折旧费用科目须为 expense。
        errs = self.errors(request_body(accumulated_depreciation_account_code="2001"))
        self.assertIn(
            ("/accumulated_depreciation_account_code", "account_type_mismatch"), errs
        )
        errs = self.errors(request_body(depreciation_expense_account_code="2001"))
        self.assertIn(
            ("/depreciation_expense_account_code", "account_type_mismatch"), errs
        )

    def test_chart_errors_prefixed_and_suppress_account_errors(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        errs = self.errors(request_body(chart=bad_chart))
        self.assertIn(("/chart/accounts", "too_few_accounts"), errs)
        # 科目体系无效时不派生科目类错误。
        self.assertNotIn(
            ("/accumulated_depreciation_account_code", "unknown_account"), errs
        )

    def test_chart_not_effective(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "2026-02-01"
        errs = self.errors(request_body(chart=late_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), errs)

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
        status, payload = self.post(request_body(useful_life_months=0))
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/useful_life_months", "invalid_useful_life"),
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
