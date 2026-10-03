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
                "name": "Bank USD",
                "type": "asset",
                "normal_balance": "debit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "1002",
                "name": "Receivable EUR",
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
                "code": "2001",
                "name": "Payable USD",
                "type": "liability",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "2002",
                "name": "Inactive Liability",
                "type": "liability",
                "normal_balance": "credit",
                "active": False,
                "parent_code": None,
            },
            {
                "code": "3001",
                "name": "Equity",
                "type": "equity",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "4001",
                "name": "FX Gain",
                "type": "revenue",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "4002",
                "name": "Inactive Revenue",
                "type": "revenue",
                "normal_balance": "credit",
                "active": False,
                "parent_code": None,
            },
            {
                "code": "5001",
                "name": "FX Loss",
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


def position(**overrides):
    item = {
        "position_id": "POS-1",
        "account_code": "1001",
        "foreign_currency": "USD",
        "foreign_amount": "1000.00",
        "carrying_amount": "6900.00",
        "exchange_rate": "7.10",
    }
    item.update(overrides)
    return item


def request_body(**overrides):
    body = {
        "voucher_id": "FXR-20260630",
        "remeasurement_date": "2026-06-30",
        "currency": "CNY",
        "chart": chart(),
        "fx_gain_account_code": "4001",
        "fx_loss_account_code": "5001",
        "positions": [position()],
    }
    body.update(overrides)
    return body


class ForeignCurrencyRemeasurementServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, payload):
        status, body = self.service.generate_foreign_currency_remeasurement(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_asset_gain_basic(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement(request_body())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["voucher_id"], "FXR-20260630")
        self.assertEqual(body["remeasurement_date"], "2026-06-30")
        self.assertEqual(body["currency"], "CNY")
        # 1000.00 * 7.10 = 7100.00，相对账面 6900.00 增加 200.00，资产增加记借。
        self.assertEqual(
            body["positions"],
            [
                {
                    "position_id": "POS-1",
                    "account_code": "1001",
                    "foreign_currency": "USD",
                    "foreign_amount": "1000.00",
                    "carrying_amount": "6900.00",
                    "exchange_rate": "7.10",
                    "remeasured_amount": "7100.00",
                    "adjustment": "200.00",
                    "side": "debit",
                }
            ],
        )
        self.assertEqual(body["net_fx_gain"], "200.00")
        self.assertEqual(body["net_fx_loss"], "0.00")

        entry = body["entry"]
        self.assertEqual(entry["voucher_id"], "FXR-20260630")
        self.assertEqual(entry["posting_date"], "2026-06-30")
        self.assertEqual(entry["currency"], "CNY")
        self.assertEqual(
            entry["lines"],
            [
                {"line_id": "fxr-1", "account_code": "1001", "debit": "200.00", "credit": "0.00"},
                {"line_id": "fxr-2", "account_code": "4001", "debit": "0.00", "credit": "200.00"},
            ],
        )
        entry_status, entry_body = validate_journal_entry(entry)
        self.assertEqual(entry_status, 200, entry_body)

    def test_liability_increase_is_credit_and_loss(self) -> None:
        # 负债折算后增加记贷，净额为贷方时以损失科目借记抵平。
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(
                        account_code="2001",
                        foreign_amount="500.00",
                        carrying_amount="3450.00",
                        exchange_rate="7.10",
                    )
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["remeasured_amount"], "3550.00")
        self.assertEqual(body["positions"][0]["adjustment"], "100.00")
        self.assertEqual(body["positions"][0]["side"], "credit")
        self.assertEqual(body["net_fx_gain"], "0.00")
        self.assertEqual(body["net_fx_loss"], "100.00")
        self.assertEqual(
            body["entry"]["lines"],
            [
                {"line_id": "fxr-1", "account_code": "2001", "debit": "0.00", "credit": "100.00"},
                {"line_id": "fxr-2", "account_code": "5001", "debit": "100.00", "credit": "0.00"},
            ],
        )
        entry_status, entry_body = validate_journal_entry(body["entry"])
        self.assertEqual(entry_status, 200, entry_body)

    def test_positions_keep_input_order_and_net_offset(self) -> None:
        # 资产增加 200（借）与负债减少 10（借）同向，净借方 210 贷记收益。
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(position_id="POS-1", account_code="1001"),
                    position(
                        position_id="POS-2",
                        account_code="2001",
                        foreign_amount="500.00",
                        carrying_amount="3560.00",
                        exchange_rate="7.10",
                    ),
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual([p["position_id"] for p in body["positions"]], ["POS-1", "POS-2"])
        self.assertEqual(body["positions"][1]["adjustment"], "-10.00")
        self.assertEqual(body["positions"][1]["side"], "debit")
        self.assertEqual(body["net_fx_gain"], "210.00")
        self.assertEqual(body["net_fx_loss"], "0.00")
        self.assertEqual(
            body["entry"]["lines"],
            [
                {"line_id": "fxr-1", "account_code": "1001", "debit": "200.00", "credit": "0.00"},
                {"line_id": "fxr-2", "account_code": "2001", "debit": "10.00", "credit": "0.00"},
                {"line_id": "fxr-3", "account_code": "4001", "debit": "0.00", "credit": "210.00"},
            ],
        )
        entry_status, entry_body = validate_journal_entry(body["entry"])
        self.assertEqual(entry_status, 200, entry_body)

    def test_zero_net_skips_fx_line(self) -> None:
        # 资产增加 100 与负债增加 100 抵平，净额为零时不加汇兑行。
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(
                        position_id="POS-1",
                        account_code="1001",
                        foreign_amount="1000.00",
                        carrying_amount="7000.00",
                        exchange_rate="7.10",
                    ),
                    position(
                        position_id="POS-2",
                        account_code="2001",
                        foreign_amount="500.00",
                        carrying_amount="3450.00",
                        exchange_rate="7.10",
                    ),
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["net_fx_gain"], "0.00")
        self.assertEqual(body["net_fx_loss"], "0.00")
        self.assertEqual(
            body["entry"]["lines"],
            [
                {"line_id": "fxr-1", "account_code": "1001", "debit": "100.00", "credit": "0.00"},
                {"line_id": "fxr-2", "account_code": "2001", "debit": "0.00", "credit": "100.00"},
            ],
        )
        entry_status, entry_body = validate_journal_entry(body["entry"])
        self.assertEqual(entry_status, 200, entry_body)

    def test_all_zero_adjustments_give_null_entry(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(
                        position_id="POS-1",
                        account_code="1001",
                        foreign_amount="1000.00",
                        carrying_amount="7100.00",
                        exchange_rate="7.10",
                    ),
                    position(
                        position_id="POS-2",
                        account_code="2001",
                        foreign_amount="500.00",
                        carrying_amount="3550.00",
                        exchange_rate="7.10",
                    ),
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["adjustment"], "0.00")
        self.assertIsNone(body["positions"][0]["side"])
        self.assertEqual(body["net_fx_gain"], "0.00")
        self.assertEqual(body["net_fx_loss"], "0.00")
        self.assertIsNone(body["entry"])

    def test_zero_carrying_amount_allowed(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(positions=[position(carrying_amount="0.00")])
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["adjustment"], "7100.00")
        entry_status, _ = validate_journal_entry(body["entry"])
        self.assertEqual(entry_status, 200)

    def test_remeasured_amount_rounds_half_up(self) -> None:
        # 1.00 * 2.345 = 2.345，四舍五入到 2.35（非银行家舍入的 2.34）。
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(
                        foreign_amount="1.00",
                        carrying_amount="0.00",
                        exchange_rate="2.345",
                    )
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["remeasured_amount"], "2.35")
        self.assertEqual(body["positions"][0]["adjustment"], "2.35")

    def test_exchange_rate_eight_decimals(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(
                        foreign_amount="100.00",
                        carrying_amount="0.00",
                        exchange_rate="7.12345678",
                    )
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["remeasured_amount"], "712.35")

    def test_amounts_normalized_to_two_decimals(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(foreign_amount="1000", carrying_amount="6900", exchange_rate="7.1")
                ]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["foreign_amount"], "1000.00")
        self.assertEqual(body["positions"][0]["carrying_amount"], "6900.00")
        self.assertEqual(body["positions"][0]["remeasured_amount"], "7100.00")

    def test_deterministic(self) -> None:
        first = self.service.generate_foreign_currency_remeasurement(request_body())
        second = self.service.generate_foreign_currency_remeasurement(request_body())
        self.assertEqual(first, second)

    def test_unknown_field_rejected(self) -> None:
        errs = self.errors(request_body(extra="x"))
        self.assertIn(("/extra", "unknown_field"), errs)
        bad_position = position(extra="x")
        errs = self.errors(request_body(positions=[bad_position]))
        self.assertIn(("/positions/0/extra", "unknown_field"), errs)

    def test_missing_fields(self) -> None:
        errs = self.errors({"chart": chart()})
        for field in (
            "voucher_id",
            "remeasurement_date",
            "currency",
            "fx_gain_account_code",
            "fx_loss_account_code",
            "positions",
        ):
            self.assertIn((f"/{field}", "required"), errs)

    def test_missing_position_fields(self) -> None:
        errs = self.errors(request_body(positions=[{}]))
        for field in (
            "position_id",
            "account_code",
            "foreign_currency",
            "foreign_amount",
            "carrying_amount",
            "exchange_rate",
        ):
            self.assertIn((f"/positions/0/{field}", "required"), errs)

    def test_positions_must_be_nonempty_array(self) -> None:
        errs = self.errors(request_body(positions=[]))
        self.assertIn(("/positions", "too_few_positions"), errs)
        errs = self.errors(request_body(positions="x"))
        self.assertIn(("/positions", "invalid_type"), errs)
        errs = self.errors(request_body(positions=[1]))
        self.assertIn(("/positions/0", "invalid_type"), errs)

    def test_invalid_dates_and_currency(self) -> None:
        errs = self.errors(request_body(remeasurement_date="2026-02-30"))
        self.assertIn(("/remeasurement_date", "invalid_date"), errs)
        errs = self.errors(request_body(currency="cny"))
        self.assertIn(("/currency", "invalid_currency"), errs)
        errs = self.errors(request_body(positions=[position(foreign_currency="usd")]))
        self.assertIn(("/positions/0/foreign_currency", "invalid_currency"), errs)

    def test_invalid_foreign_amount(self) -> None:
        for bad in ("0", "0.00", "1.234", "abc", "1,000", "-1"):
            errs = self.errors(request_body(positions=[position(foreign_amount=bad)]))
            self.assertIn(("/positions/0/foreign_amount", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(positions=[position(foreign_amount=100)]))
        self.assertIn(("/positions/0/foreign_amount", "invalid_type"), errs)
        errs = self.errors(request_body(positions=[position(foreign_amount="")]))
        self.assertIn(("/positions/0/foreign_amount", "blank_value"), errs)

    def test_invalid_carrying_amount(self) -> None:
        for bad in ("1.234", "abc", "-1"):
            errs = self.errors(request_body(positions=[position(carrying_amount=bad)]))
            self.assertIn(("/positions/0/carrying_amount", "invalid_amount"), errs, bad)
        errs = self.errors(request_body(positions=[position(carrying_amount=0)]))
        self.assertIn(("/positions/0/carrying_amount", "invalid_type"), errs)
        # 零是合法的账面金额。
        status, _ = self.service.generate_foreign_currency_remeasurement(
            request_body(positions=[position(carrying_amount="0")])
        )
        self.assertEqual(status, 200)

    def test_invalid_exchange_rate(self) -> None:
        for bad in ("0", "0.00", "1.123456789", "abc", "-1", "1e2", "1E-2"):
            errs = self.errors(request_body(positions=[position(exchange_rate=bad)]))
            self.assertIn(("/positions/0/exchange_rate", "invalid_exchange_rate"), errs, bad)
        errs = self.errors(request_body(positions=[position(exchange_rate=7.1)]))
        self.assertIn(("/positions/0/exchange_rate", "invalid_type"), errs)
        errs = self.errors(request_body(positions=[position(exchange_rate="")]))
        self.assertIn(("/positions/0/exchange_rate", "blank_value"), errs)

    def test_functional_currency_position(self) -> None:
        errs = self.errors(request_body(positions=[position(foreign_currency="CNY")]))
        self.assertIn(
            ("/positions/0/foreign_currency", "functional_currency_position"), errs
        )
        # 本位币本身无效时不派生该错误。
        errs = self.errors(
            request_body(currency="cny", positions=[position(foreign_currency="CNY")])
        )
        self.assertNotIn(
            ("/positions/0/foreign_currency", "functional_currency_position"), errs
        )

    def test_duplicate_position_id(self) -> None:
        errs = self.errors(
            request_body(
                positions=[
                    position(position_id="POS-1", account_code="1001"),
                    position(position_id="POS-1", account_code="1002"),
                ]
            )
        )
        self.assertIn(("/positions/1/position_id", "duplicate_position_id"), errs)

    def test_duplicate_position(self) -> None:
        errs = self.errors(
            request_body(
                positions=[
                    position(position_id="POS-1", account_code="1001"),
                    position(position_id="POS-2", account_code="1001"),
                ]
            )
        )
        self.assertIn(("/positions/1/account_code", "duplicate_position"), errs)
        # 同科目不同外币不算重复。
        status, _ = self.service.generate_foreign_currency_remeasurement(
            request_body(
                positions=[
                    position(position_id="POS-1", account_code="1001", foreign_currency="USD"),
                    position(position_id="POS-2", account_code="1001", foreign_currency="EUR"),
                ]
            )
        )
        self.assertEqual(status, 200)

    def test_duplicate_fx_account(self) -> None:
        errs = self.errors(
            request_body(fx_gain_account_code="4001", fx_loss_account_code="4001")
        )
        self.assertIn(("/fx_loss_account_code", "duplicate_fx_account"), errs)

    def test_fx_account_checks(self) -> None:
        errs = self.errors(request_body(fx_gain_account_code="9999"))
        self.assertIn(("/fx_gain_account_code", "unknown_account"), errs)
        errs = self.errors(request_body(fx_loss_account_code="9999"))
        self.assertIn(("/fx_loss_account_code", "unknown_account"), errs)
        errs = self.errors(request_body(fx_gain_account_code="4002"))
        self.assertIn(("/fx_gain_account_code", "inactive_account"), errs)
        errs = self.errors(request_body(fx_loss_account_code="5002"))
        self.assertIn(("/fx_loss_account_code", "inactive_account"), errs)
        # 收益科目须为 revenue，损失科目须为 expense。
        errs = self.errors(request_body(fx_gain_account_code="5001"))
        self.assertIn(("/fx_gain_account_code", "account_type_mismatch"), errs)
        errs = self.errors(request_body(fx_loss_account_code="4001"))
        self.assertIn(("/fx_loss_account_code", "account_type_mismatch"), errs)

    def test_position_account_checks(self) -> None:
        errs = self.errors(request_body(positions=[position(account_code="9999")]))
        self.assertIn(("/positions/0/account_code", "unknown_account"), errs)
        errs = self.errors(request_body(positions=[position(account_code="1003")]))
        self.assertIn(("/positions/0/account_code", "inactive_account"), errs)
        errs = self.errors(request_body(positions=[position(account_code="2002")]))
        self.assertIn(("/positions/0/account_code", "inactive_account"), errs)
        # 仅 asset/liability 可用：revenue、expense、equity 均报类别错误。
        for code in ("4001", "5001", "3001"):
            errs = self.errors(request_body(positions=[position(account_code=code)]))
            self.assertIn(("/positions/0/account_code", "account_type_mismatch"), errs, code)
        # 负债科目可以。
        status, _ = self.service.generate_foreign_currency_remeasurement(
            request_body(positions=[position(account_code="2001")])
        )
        self.assertEqual(status, 200)

    def test_account_errors_suppressed_without_valid_chart(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        errs = self.errors(request_body(chart=bad_chart))
        self.assertIn(("/chart/accounts", "too_few_accounts"), errs)
        self.assertNotIn(("/fx_gain_account_code", "unknown_account"), errs)
        self.assertNotIn(("/positions/0/account_code", "unknown_account"), errs)

    def test_chart_not_effective(self) -> None:
        late_chart = chart()
        late_chart["effective_date"] = "2026-07-01"
        errs = self.errors(request_body(chart=late_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), errs)
        # 生效日恰为重估日可以。
        same_day = chart()
        same_day["effective_date"] = "2026-06-30"
        status, _ = self.service.generate_foreign_currency_remeasurement(
            request_body(chart=same_day)
        )
        self.assertEqual(status, 200)

    def test_chart_field_errors_prefixed(self) -> None:
        errs = self.errors(request_body(chart=[]))
        self.assertIn(("/chart", "invalid_type"), errs)
        errs = self.errors({k: v for k, v in request_body().items() if k != "chart"})
        self.assertIn(("/chart", "required"), errs)

    def test_errors_sorted_by_path_and_code(self) -> None:
        status, body = self.service.generate_foreign_currency_remeasurement({})
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))


class ForeignCurrencyRemeasurementHttpTest(unittest.TestCase):
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
            "/v1/foreign-currency-remeasurements/generate",
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
        self.assertEqual(payload["net_fx_gain"], "200.00")

    def test_route_422(self) -> None:
        status, payload = self.post(
            request_body(positions=[position(exchange_rate="0")])
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/positions/0/exchange_rate", "invalid_exchange_rate"),
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
