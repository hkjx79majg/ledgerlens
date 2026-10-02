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
                "code": "4001",
                "name": "Revenue",
                "type": "revenue",
                "normal_balance": "credit",
                "active": True,
                "parent_code": None,
            },
            {
                "code": "9000",
                "name": "Dormant",
                "type": "expense",
                "normal_balance": "debit",
                "active": False,
                "parent_code": None,
            },
        ],
    }


def entry(voucher_id="JV-1", posting_date="2026-01-15", currency="CNY", lines=None):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": currency,
        "lines": lines
        or [
            {"line_id": "a", "account_code": "1001", "debit": "25.5", "credit": "0"},
            {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "25.50"},
        ],
    }


def payload(**overrides):
    body = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "opening_balances": [
            {"account_code": "1001", "debit": "100.00", "credit": "0"},
            {"account_code": "4001", "debit": "0", "credit": "100.00"},
        ],
        "entries": [entry()],
    }
    body.update(overrides)
    return body


class TrialBalanceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def errors(self, body):
        status, response = self.service.generate_trial_balance(body)
        self.assertEqual(status, 422, response)
        self.assertFalse(response["valid"])
        return {(e["path"], e["code"]) for e in response["errors"]}

    def test_success_rows_totals_and_echo(self) -> None:
        status, body = self.service.generate_trial_balance(payload())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")

        rows = {row["code"]: row for row in body["rows"]}
        # 全部科目按 code 字典序输出。
        self.assertEqual([row["code"] for row in body["rows"]], ["1000", "1001", "4001", "9000"])
        self.assertEqual(rows["1000"]["name"], "Assets")
        self.assertEqual(rows["1000"]["type"], "asset")
        # 无发生额科目：六列均为 0.00。
        for key in (
            "opening_debit",
            "opening_credit",
            "period_debit",
            "period_credit",
            "ending_debit",
            "ending_credit",
        ):
            self.assertEqual(rows["1000"][key], "0.00")
            self.assertEqual(rows["9000"][key], "0.00")
        # 借方科目：期初 + 期间净额落借方。
        self.assertEqual(rows["1001"]["opening_debit"], "100.00")
        self.assertEqual(rows["1001"]["opening_credit"], "0.00")
        self.assertEqual(rows["1001"]["period_debit"], "25.50")
        self.assertEqual(rows["1001"]["period_credit"], "0.00")
        self.assertEqual(rows["1001"]["ending_debit"], "125.50")
        self.assertEqual(rows["1001"]["ending_credit"], "0.00")
        # 贷方科目：期末净额落贷方。
        self.assertEqual(rows["4001"]["opening_debit"], "0.00")
        self.assertEqual(rows["4001"]["opening_credit"], "100.00")
        self.assertEqual(rows["4001"]["period_debit"], "0.00")
        self.assertEqual(rows["4001"]["period_credit"], "25.50")
        self.assertEqual(rows["4001"]["ending_debit"], "0.00")
        self.assertEqual(rows["4001"]["ending_credit"], "125.50")
        # totals 为六列逐行简单求和，父子不重复汇总。
        self.assertEqual(
            body["totals"],
            {
                "opening_debit": "100.00",
                "opening_credit": "100.00",
                "period_debit": "25.50",
                "period_credit": "25.50",
                "ending_debit": "125.50",
                "ending_credit": "125.50",
            },
        )

    def test_ending_nets_to_single_side(self) -> None:
        body = payload(
            opening_balances=[
                {"account_code": "1001", "debit": "10.00", "credit": "0"},
                {"account_code": "4001", "debit": "0", "credit": "10.00"},
            ],
            entries=[
                entry(
                    lines=[
                        {"line_id": "a", "account_code": "1001", "debit": "0", "credit": "30"},
                        {"line_id": "b", "account_code": "4001", "debit": "30", "credit": "0"},
                    ]
                )
            ],
        )
        status, response = self.service.generate_trial_balance(body)
        self.assertEqual(status, 200, response)
        rows = {row["code"]: row for row in response["rows"]}
        # 1001：期初借 10，期间贷 30，净贷 20 落贷方。
        self.assertEqual(rows["1001"]["ending_debit"], "0.00")
        self.assertEqual(rows["1001"]["ending_credit"], "20.00")
        # 4001：期初贷 10，期间借 30，净借 20 落借方。
        self.assertEqual(rows["4001"]["ending_debit"], "20.00")
        self.assertEqual(rows["4001"]["ending_credit"], "0.00")

    def test_invalid_period(self) -> None:
        found = self.errors(payload(period_start="2026-02-01", period_end="2026-01-01"))
        self.assertIn(("/period_start", "invalid_period"), found)

    def test_invalid_period_dates_not_derived(self) -> None:
        # period_start 本身无效时不派生 invalid_period。
        found = self.errors(payload(period_start="2026-13-01"))
        self.assertIn(("/period_start", "invalid_date"), found)
        self.assertNotIn(("/period_start", "invalid_period"), found)

    def test_chart_not_effective(self) -> None:
        late = chart()
        late["effective_date"] = "2026-02-01"
        found = self.errors(payload(chart=late))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), found)

    def test_chart_field_errors_prefixed(self) -> None:
        bad = chart()
        del bad["chart_id"]
        found = self.errors(payload(chart=bad))
        self.assertIn(("/chart/chart_id", "required"), found)

    def test_currency_mismatch(self) -> None:
        found = self.errors(payload(entries=[entry(currency="USD")]))
        self.assertIn(("/entries/0/currency", "currency_mismatch"), found)

    def test_currency_mismatch_not_derived_from_invalid_fields(self) -> None:
        # 请求币种无效时不派生 currency_mismatch。
        found = self.errors(payload(currency="cny", entries=[entry(currency="USD")]))
        self.assertIn(("/currency", "invalid_currency"), found)
        self.assertNotIn(("/entries/0/currency", "currency_mismatch"), found)

    def test_posting_date_out_of_period(self) -> None:
        found = self.errors(payload(entries=[entry(posting_date="2026-02-01")]))
        self.assertIn(("/entries/0/posting_date", "posting_date_out_of_period"), found)
        # 闭区间端点合法。
        status, _ = self.service.generate_trial_balance(
            payload(entries=[entry(posting_date="2026-01-31")])
        )
        self.assertEqual(status, 200)

    def test_unknown_and_inactive_account(self) -> None:
        found = self.errors(
            payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "1", "credit": "0"},
                    {"account_code": "9999", "debit": "0", "credit": "1"},
                ],
                entries=[
                    entry(
                        lines=[
                            {"line_id": "a", "account_code": "9000", "debit": "1", "credit": "0"},
                            {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "1"},
                        ]
                    )
                ],
            )
        )
        self.assertIn(("/opening_balances/1/account_code", "unknown_account"), found)
        self.assertIn(("/entries/0/lines/0/account_code", "inactive_account"), found)

    def test_duplicate_opening_account(self) -> None:
        found = self.errors(
            payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "1", "credit": "0"},
                    {"account_code": "1001", "debit": "0", "credit": "1"},
                ]
            )
        )
        self.assertIn(("/opening_balances/1/account_code", "duplicate_opening_account"), found)

    def test_unbalanced_opening_balances(self) -> None:
        found = self.errors(
            payload(opening_balances=[{"account_code": "1001", "debit": "5", "credit": "0"}])
        )
        self.assertIn(("/opening_balances", "unbalanced_opening_balances"), found)

    def test_unbalanced_opening_not_derived_from_invalid_amounts(self) -> None:
        found = self.errors(
            payload(
                opening_balances=[{"account_code": "1001", "debit": "abc", "credit": "0"}]
            )
        )
        self.assertIn(("/opening_balances/0/debit", "invalid_amount"), found)
        self.assertNotIn(("/opening_balances", "unbalanced_opening_balances"), found)

    def test_opening_invalid_side(self) -> None:
        found = self.errors(
            payload(
                opening_balances=[{"account_code": "1001", "debit": "1", "credit": "1"}]
            )
        )
        self.assertIn(("/opening_balances/0", "invalid_side"), found)

    def test_entry_field_and_balance_errors_prefixed(self) -> None:
        found = self.errors(payload(entries=[entry(posting_date="not-a-date")]))
        self.assertIn(("/entries/0/posting_date", "invalid_date"), found)

        unbalanced = entry(
            lines=[
                {"line_id": "a", "account_code": "1001", "debit": "2", "credit": "0"},
                {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "1"},
            ]
        )
        found = self.errors(payload(entries=[unbalanced]))
        self.assertIn(("/entries/0", "unbalanced_entry"), found)

    def test_errors_sorted_by_path_then_code(self) -> None:
        status, body = self.service.generate_trial_balance(
            payload(
                period_start="2026-02-01",
                period_end="2026-01-01",
                opening_balances=[{"account_code": "9999", "debit": "1", "credit": "0"}],
                entries=[entry(currency="USD", posting_date="2026-03-01")],
            )
        )
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))

    def test_top_level_unknown_field_and_required(self) -> None:
        body = payload(extra="x")
        del body["entries"]
        found = self.errors(body)
        self.assertIn(("/extra", "unknown_field"), found)
        self.assertIn(("/entries", "required"), found)


class TrialBalanceHttpTest(unittest.TestCase):
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

    def request(self, body=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request(
            "POST", "/v1/trial-balances/generate", body=data, headers={"Content-Type": content_type}
        )
        resp = conn.getresponse()
        result = resp.status, json.loads(resp.read())
        conn.close()
        return result

    def test_generate_over_http(self) -> None:
        status, body = self.request(payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(len(body["rows"]), 4)

    def test_transport_errors(self) -> None:
        status, body = self.request("{}", "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

        status, body = self.request("{bad", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.request("[1]", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
