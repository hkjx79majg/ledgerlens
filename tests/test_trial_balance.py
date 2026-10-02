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
                "code": "2000",
                "name": "Payables",
                "type": "liability",
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
            {"line_id": "a", "account_code": "1001", "debit": "50", "credit": "0"},
            {"line_id": "b", "account_code": "2000", "debit": "0", "credit": "50"},
        ],
    }


def request_payload(**overrides):
    payload = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "opening_balances": [
            {"account_code": "1001", "debit": "100.00", "credit": "0"},
            {"account_code": "2000", "debit": "0", "credit": "100.00"},
        ],
        "entries": [entry()],
    }
    payload.update(overrides)
    return payload


class TrialBalanceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def generate(self, payload):
        return self.service.generate_trial_balance(payload)

    def error_pairs(self, payload):
        status, body = self.generate(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_valid_request_returns_full_statement(self) -> None:
        status, body = self.generate(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")

        rows = body["accounts"]
        self.assertEqual([row["code"] for row in rows], ["1000", "1001", "2000", "9000"])
        by_code = {row["code"]: row for row in rows}
        self.assertEqual(by_code["1000"]["name"], "Assets")
        self.assertEqual(by_code["1000"]["type"], "asset")
        # 父科目不滚算子科目余额，每行只计自身。
        self.assertEqual(by_code["1000"]["opening_debit"], "0.00")
        self.assertEqual(by_code["1000"]["ending_debit"], "0.00")

        cash = by_code["1001"]
        self.assertEqual(cash["opening_debit"], "100.00")
        self.assertEqual(cash["opening_credit"], "0.00")
        self.assertEqual(cash["period_debit"], "50.00")
        self.assertEqual(cash["period_credit"], "0.00")
        self.assertEqual(cash["ending_debit"], "150.00")
        self.assertEqual(cash["ending_credit"], "0.00")

        payables = by_code["2000"]
        self.assertEqual(payables["opening_credit"], "100.00")
        self.assertEqual(payables["period_credit"], "50.00")
        self.assertEqual(payables["ending_credit"], "150.00")
        self.assertEqual(payables["ending_debit"], "0.00")

        dormant = by_code["9000"]
        for key in (
            "opening_debit",
            "opening_credit",
            "period_debit",
            "period_credit",
            "ending_debit",
            "ending_credit",
        ):
            self.assertEqual(dormant[key], "0.00")

        self.assertEqual(
            body["totals"],
            {
                "opening_debit": "100.00",
                "opening_credit": "100.00",
                "period_debit": "50.00",
                "period_credit": "50.00",
                "ending_debit": "150.00",
                "ending_credit": "150.00",
            },
        )

    def test_ending_nets_to_zero_on_both_sides(self) -> None:
        payload = request_payload(
            entries=[
                entry(
                    lines=[
                        {"line_id": "a", "account_code": "2000", "debit": "100", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "100"},
                    ]
                )
            ]
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 200)
        by_code = {row["code"]: row for row in body["accounts"]}
        # 2000：期初贷 100 与期间借 100 抵销后为零，两侧均为 0.00。
        self.assertEqual(by_code["2000"]["ending_debit"], "0.00")
        self.assertEqual(by_code["2000"]["ending_credit"], "0.00")
        # 1001：期初借 100 被期间贷 100 抵销。
        self.assertEqual(by_code["1001"]["ending_debit"], "0.00")
        self.assertEqual(by_code["1001"]["ending_credit"], "0.00")
        self.assertEqual(body["totals"]["ending_debit"], "0.00")
        self.assertEqual(body["totals"]["ending_credit"], "0.00")

    def test_empty_entries_and_opening_balances_are_allowed(self) -> None:
        payload = request_payload(opening_balances=[], entries=[])
        status, body = self.generate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["opening_debit"], "0.00")
        self.assertEqual(len(body["accounts"]), 4)

    def test_invalid_period_and_date_errors(self) -> None:
        pairs = self.error_pairs(
            request_payload(period_start="2026-02-01", period_end="2026-01-01")
        )
        self.assertIn(("/period_start", "invalid_period"), pairs)

        # 日期本身无效时不派生 invalid_period。
        pairs = self.error_pairs(request_payload(period_start="2026-13-01"))
        self.assertIn(("/period_start", "invalid_date"), pairs)
        self.assertNotIn(("/period_start", "invalid_period"), pairs)

    def test_chart_not_effective(self) -> None:
        bad_chart = chart()
        bad_chart["effective_date"] = "2026-02-01"
        pairs = self.error_pairs(request_payload(chart=bad_chart))
        self.assertIn(("/chart/effective_date", "chart_not_effective"), pairs)

    def test_currency_rules(self) -> None:
        pairs = self.error_pairs(request_payload(entries=[entry(currency="USD")]))
        self.assertIn(("/entries/0/currency", "currency_mismatch"), pairs)

        # 请求币种无效时不派生 currency_mismatch。
        pairs = self.error_pairs(
            request_payload(currency="cny", entries=[entry(currency="USD")])
        )
        self.assertIn(("/currency", "invalid_currency"), pairs)
        self.assertNotIn(("/entries/0/currency", "currency_mismatch"), pairs)

    def test_posting_date_out_of_period(self) -> None:
        pairs = self.error_pairs(
            request_payload(entries=[entry(posting_date="2026-02-01")])
        )
        self.assertIn(("/entries/0/posting_date", "posting_date_out_of_period"), pairs)

        # 边界：起止日当天均在闭区间内。
        for day in ("2026-01-01", "2026-01-31"):
            status, _ = self.generate(
                request_payload(entries=[entry(posting_date=day)])
            )
            self.assertEqual(status, 200, day)

    def test_account_reference_errors(self) -> None:
        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "9999", "debit": "100.00", "credit": "0"},
                    {"account_code": "2000", "debit": "0", "credit": "100.00"},
                ]
            )
        )
        self.assertIn(("/opening_balances/0/account_code", "unknown_account"), pairs)

        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "9000", "debit": "100.00", "credit": "0"},
                    {"account_code": "2000", "debit": "0", "credit": "100.00"},
                ]
            )
        )
        self.assertIn(("/opening_balances/0/account_code", "inactive_account"), pairs)

        pairs = self.error_pairs(
            request_payload(
                entries=[
                    entry(
                        lines=[
                            {"line_id": "a", "account_code": "9999", "debit": "50", "credit": "0"},
                            {"line_id": "b", "account_code": "2000", "debit": "0", "credit": "50"},
                        ]
                    )
                ]
            )
        )
        self.assertIn(("/entries/0/lines/0/account_code", "unknown_account"), pairs)

    def test_duplicate_opening_account(self) -> None:
        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "60.00", "credit": "0"},
                    {"account_code": "1001", "debit": "40.00", "credit": "0"},
                    {"account_code": "2000", "debit": "0", "credit": "100.00"},
                ]
            )
        )
        self.assertIn(
            ("/opening_balances/1/account_code", "duplicate_opening_account"), pairs
        )

    def test_unbalanced_opening_balances(self) -> None:
        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "100.00", "credit": "0"},
                    {"account_code": "2000", "debit": "0", "credit": "50.00"},
                ]
            )
        )
        self.assertIn(("/opening_balances", "unbalanced_opening_balances"), pairs)

        # 期初项金额无效时不派生平衡错误。
        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "abc", "credit": "0"},
                ]
            )
        )
        self.assertIn(("/opening_balances/0/debit", "invalid_amount"), pairs)
        self.assertNotIn(("/opening_balances", "unbalanced_opening_balances"), pairs)

    def test_opening_item_must_be_one_sided(self) -> None:
        pairs = self.error_pairs(
            request_payload(
                opening_balances=[
                    {"account_code": "1001", "debit": "0", "credit": "0"},
                ]
            )
        )
        self.assertIn(("/opening_balances/0", "invalid_side"), pairs)

    def test_nested_chart_and_entry_errors_are_repathed(self) -> None:
        bad_chart = chart()
        del bad_chart["chart_id"]
        pairs = self.error_pairs(request_payload(chart=bad_chart))
        self.assertIn(("/chart/chart_id", "required"), pairs)

        bad_entry = entry()
        del bad_entry["voucher_id"]
        pairs = self.error_pairs(request_payload(entries=[bad_entry]))
        self.assertIn(("/entries/0/voucher_id", "required"), pairs)

        unbalanced = entry(
            lines=[
                {"line_id": "a", "account_code": "1001", "debit": "50", "credit": "0"},
                {"line_id": "b", "account_code": "2000", "debit": "0", "credit": "40"},
            ]
        )
        pairs = self.error_pairs(request_payload(entries=[unbalanced]))
        self.assertIn(("/entries/0", "unbalanced_entry"), pairs)

    def test_errors_sorted_by_path_then_code(self) -> None:
        payload = request_payload(
            period_start="2026-02-01",
            period_end="2026-01-01",
            extra="nope",
        )
        status, body = self.generate(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        self.assertIn(("/extra", "unknown_field"), set(keys))


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

    def request(self, path, body=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request("POST", path, body=data, headers={"Content-Type": content_type})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_generate_over_http(self) -> None:
        status, payload = self.request("/v1/trial-balances/generate", request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["chart_id"], "COA-1")

    def test_transport_errors_match_existing_endpoints(self) -> None:
        status, payload = self.request(
            "/v1/trial-balances/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request("/v1/trial-balances/generate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request("/v1/trial-balances/generate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
