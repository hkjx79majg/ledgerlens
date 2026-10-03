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
                "code": "1004",
                "name": "Accumulated Impairment",
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
                "code": "5003",
                "name": "Impairment Loss",
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
        "voucher_id": "IMP-20260630",
        "test_date": "2026-06-30",
        "currency": "CNY",
        "carrying_amount": "1000.00",
        "recoverable_amount": "700.00",
        "chart": chart(),
        "accumulated_impairment_account_code": "1004",
        "impairment_loss_account_code": "5003",
    }
    body.update(overrides)
    return body


class AssetImpairmentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, payload):
        status, body = self.service.generate_asset_impairment(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_impairment_loss_basic(self) -> None:
        status, body = self.service.generate_asset_impairment(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["asset_id"], "AS-1")
        self.assertEqual(body["test_date"], "2026-06-30")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["carrying_amount"], "1000.00")
        self.assertEqual(body["recoverable_amount"], "700.00")
        self.assertEqual(body["impairment_loss"], "300.00")
        self.assertEqual(body["post_impairment_carrying_amount"], "700.00")

        entry = body["entry"]
        self.assertEqual(entry["voucher_id"], "IMP-20260630")
        self.assertEqual(entry["posting_date"], "2026-06-30")
        self.assertEqual(entry["currency"], "CNY")
        # imp-1 借记损失、imp-2 贷记累计减值。
        self.assertEqual(
            entry["lines"],
            [
                {"line_id": "imp-1", "account_code": "5003", "debit": "300.00", "credit": "0.00"},
                {"line_id": "imp-2", "account_code": "1004", "debit": "0.00", "credit": "300.00"},
            ],
        )
        entry_status, entry_body = validate_journal_entry(entry)
        self.assertEqual(entry_status, 200, entry_body)

    def test_decimal_precision(self) -> None:
        status, body = self.service.generate_asset_impairment(
            request_body(carrying_amount="1000.10", recoverable_amount="999.09")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["impairment_loss"], "1.01")
        self.assertEqual(body["post_impairment_carrying_amount"], "999.09")

    def test_no_impairment_when_recoverable_is_higher(self) -> None:
        status, body = self.service.generate_asset_impairment(
            request_body(carrying_amount="1000.00", recoverable_amount="1200.00")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["impairment_loss"], "0.00")
        self.assertEqual(body["post_impairment_carrying_amount"], "1000.00")
        self.assertIsNone(body["entry"])

    def test_no_impairment_when_equal(self) -> None:
        status, body = self.service.generate_asset_impairment(
            request_body(carrying_amount="1000.00", recoverable_amount="1000.00")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["impairment_loss"], "0.00")
        self.assertEqual(body["post_impairment_carrying_amount"], "1000.00")
        self.assertIsNone(body["entry"])

    def test_recoverable_zero_is_allowed(self) -> None:
        status, body = self.service.generate_asset_impairment(
            request_body(recoverable_amount="0.00")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["impairment_loss"], "1000.00")
        self.assertEqual(body["post_impairment_carrying_amount"], "0.00")
        entry_status, _ = validate_journal_entry(body["entry"])
        self.assertEqual(entry_status, 200)

    def test_deterministic(self) -> None:
        first = self.service.generate_asset_impairment(request_body())
        second = self.service.generate_asset_impairment(request_body())
        self.assertEqual(first, second)

    def test_unknown_field_rejected(self) -> None:
        errs = self.errors(request_body(extra="x"))
        self.assertIn(("/extra", "unknown_field"), errs)

    def test_missing_fields(self) -> None:
        errs = self.errors({"chart": chart()})
        for field in (
            "asset_id",
            "voucher_id",
            "test_date",
            "currency",
            "carrying_amount",
            "recoverable_amount",
            "accumulated_impairment_account_code",
            "impairment_loss_account_code",
        ):
            self.assertIn((f"/{field}", "required"), errs)

    def test_invalid_dates_and_currency(self) -> None:
        errs = self.errors(request_body(test_date="2026-02-30"))
        self.assertIn(("/test_date", "invalid_date"), errs)
        errs = self.errors(request_body(currency="cny"))
        self.assertIn(("/currency", "invalid_currency"), errs)

    def test_invalid_carrying_amount(self) -> None:
        for bad in ("0", "0.00", "1.234", "abc", "1,000", "-1"):
            errs = self.errors(request_body(carrying_amount=bad))
            self.assertIn(("/carrying_amount", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(carrying_amount=100))
        self.assertIn(("/carrying_amount", "invalid_type"), errs)
        errs = self.errors(request_body(carrying_amount=""))
        self.assertIn(("/carrying_amount", "blank_value"), errs)

    def test_invalid_recoverable_amount(self) -> None:
        for bad in ("1.234", "abc", "-1"):
            errs = self.errors(request_body(recoverable_amount=bad))
            self.assertIn(("/recoverable_amount", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(recoverable_amount=0))
        self.assertIn(("/recoverable_amount", "invalid_type"), errs)
        # 零是合法的。
        status, _ = self.service.generate_asset_impairment(
            request_body(recoverable_amount="0")
        )
        self.assertEqual(status, 200)

    def test_duplicate_impairment_account(self) -> None:
        errs = self.errors(
            request_body(
                accumulated_impairment_account_code="1004",
                impairment_loss_account_code="1004",
            )
        )
        self.assertIn(
            ("/impairment_loss_account_code", "duplicate_impairment_account"),
            errs,
        )

    def test_unknown_and_inactive_accounts(self) -> None:
        errs = self.errors(request_body(accumulated_impairment_account_code="9999"))
        self.assertIn(
            ("/accumulated_impairment_account_code", "unknown_account"), errs
        )
        errs = self.errors(request_body(impairment_loss_account_code="9999"))
        self.assertIn(
            ("/impairment_loss_account_code", "unknown_account"), errs
        )
        errs = self.errors(request_body(accumulated_impairment_account_code="1003"))
        self.assertIn(
            ("/accumulated_impairment_account_code", "inactive_account"), errs
        )
        errs = self.errors(request_body(impairment_loss_account_code="5002"))
        self.assertIn(
            ("/impairment_loss_account_code", "inactive_account"), errs
        )

    def test_account_type_mismatch(self) -> None:
        # 累计减值科目须为 asset，减值损失科目须为 expense。
        errs = self.errors(request_body(accumulated_impairment_account_code="5003"))
        self.assertIn(
            ("/accumulated_impairment_account_code", "account_type_mismatch"), errs
        )
        errs = self.errors(request_body(impairment_loss_account_code="1004"))
        self.assertIn(
            ("/impairment_loss_account_code", "account_type_mismatch"), errs
        )
        errs = self.errors(request_body(accumulated_impairment_account_code="4001"))
        self.assertIn(
            ("/accumulated_impairment_account_code", "account_type_mismatch"), errs
        )

    def test_account_errors_suppressed_without_valid_chart(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        errs = self.errors(request_body(chart=bad_chart))
        self.assertIn(("/chart/accounts", "too_few_accounts"), errs)
        self.assertNotIn(
            ("/accumulated_impairment_account_code", "unknown_account"), errs
        )
        self.assertNotIn(
            ("/impairment_loss_account_code", "unknown_account"), errs
        )

    def test_chart_not_effective(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "2026-07-01"
        errs = self.errors(request_body(chart=late_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), errs)
        # 生效日恰为测试日可以。
        same_day = chart()
        same_day["effective_date"] = "2026-06-30"
        status, _ = self.service.generate_asset_impairment(
            request_body(chart=same_day)
        )
        self.assertEqual(status, 200)

    def test_chart_not_effective_requires_valid_dates(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "not-a-date"
        errs = self.errors(request_body(test_date="bad", chart=late_chart))
        self.assertIn(("/test_date", "invalid_date"), errs)
        self.assertIn(("/chart/effective_date", "invalid_date"), errs)
        self.assertNotIn(("/chart/effective_date", "chart_not_effective"), errs)

    def test_chart_field_errors_prefixed(self) -> None:
        errs = self.errors(request_body(chart=[]))
        self.assertIn(("/chart", "invalid_type"), errs)
        errs = self.errors({k: v for k, v in request_body().items() if k != "chart"})
        self.assertIn(("/chart", "required"), errs)

    def test_errors_sorted_by_path_and_code(self) -> None:
        status, body = self.service.generate_asset_impairment({})
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))


class AssetImpairmentHttpTest(unittest.TestCase):
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
            "/v1/asset-impairments/generate",
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
        self.assertEqual(payload["impairment_loss"], "300.00")

    def test_route_422(self) -> None:
        status, payload = self.post(request_body(carrying_amount="0"))
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/carrying_amount", "invalid_amount"),
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
