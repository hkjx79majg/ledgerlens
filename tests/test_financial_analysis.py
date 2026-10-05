import copy
import http.client
import json
import threading
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart(chart_id="COA-1"):
    return {
        "chart_id": chart_id,
        "effective_date": "2025-01-01",
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
                "name": "Capital",
                "type": "equity",
                "normal_balance": "credit",
                "active": True,
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


def period(
    period_id,
    start,
    end,
    revenue="200",
    expense="50",
    opening_cash="1000",
    currency="CNY",
    chart_id="COA-1",
):
    lines = []
    if Decimal(revenue) > 0:
        lines += [
            {"line_id": "a", "account_code": "1001", "debit": revenue, "credit": "0"},
            {"line_id": "b", "account_code": "4001", "debit": "0", "credit": revenue},
        ]
    if Decimal(expense) > 0:
        lines += [
            {"line_id": "c", "account_code": "5001", "debit": expense, "credit": "0"},
            {"line_id": "d", "account_code": "1001", "debit": "0", "credit": expense},
        ]
    entries = []
    if lines:
        entries.append(
            {
                "voucher_id": f"JV-{period_id}",
                "posting_date": start,
                "currency": currency,
                "lines": lines,
            }
        )
    return {
        "period_id": period_id,
        "period_start": start,
        "period_end": end,
        "currency": currency,
        "chart": chart(chart_id),
        "opening_balances": [
            {"account_code": "1001", "debit": opening_cash, "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": opening_cash},
        ],
        "entries": entries,
    }


def two_periods():
    return {
        "periods": [
            period("p1", "2025-01-01", "2025-01-31"),
            period("p2", "2025-02-01", "2025-02-28", revenue="300", expense="90"),
        ]
    }


class GenerateFinancialAnalysisServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_success_structure_sorting_and_sums(self) -> None:
        # 输入故意逆序，输出按 period_end、period_start、period_id 升序。
        status, body = self.service.generate_financial_analysis(
            {"periods": list(reversed(two_periods()["periods"]))}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual([p["period_id"] for p in body["periods"]], ["p1", "p2"])

        first, second = body["periods"]
        self.assertEqual(first["period_start"], "2025-01-01")
        self.assertEqual(first["period_end"], "2025-01-31")
        for field in (
            "total_revenue",
            "total_expense",
            "net_income",
            "total_assets",
            "total_liabilities",
            "total_equity_before_net_income",
        ):
            self.assertIn(field, first)
        self.assertEqual(first["total_revenue"], "200.00")
        self.assertEqual(first["total_expense"], "50.00")
        self.assertEqual(first["net_income"], "150.00")
        self.assertEqual(first["total_assets"], "1150.00")
        self.assertEqual(first["total_liabilities"], "0.00")
        self.assertEqual(first["total_equity_before_net_income"], "1000.00")
        self.assertEqual(second["total_revenue"], "300.00")
        self.assertEqual(second["total_expense"], "90.00")
        self.assertEqual(second["net_income"], "210.00")
        self.assertEqual(second["total_assets"], "1210.00")

    def test_ratios_use_unrounded_amounts_half_up_four_places(self) -> None:
        status, body = self.service.generate_financial_analysis(two_periods())
        self.assertEqual(status, 200)
        first, second = body["periods"]

        self.assertEqual(
            first["ratios"]["net_profit_margin"], {"value": "0.7500", "status": "ok"}
        )
        self.assertEqual(
            first["ratios"]["debt_to_assets"], {"value": "0.0000", "status": "ok"}
        )
        # 150 / 1150 = 0.130434... -> 0.1304
        self.assertEqual(
            first["ratios"]["net_income_to_assets"],
            {"value": "0.1304", "status": "ok"},
        )
        self.assertNotIn("trend", first["ratios"])

        trend = second["ratios"]["trend"]
        self.assertEqual(trend["previous_period_id"], "p1")
        # (300-200)/|200| = 0.5；(210-150)/|150| = 0.4；(1210-1150)/|1150| ≈ 0.05217 -> 0.0522
        self.assertEqual(trend["revenue_growth"], {"value": "0.5000", "status": "ok"})
        self.assertEqual(
            trend["net_income_growth"], {"value": "0.4000", "status": "ok"}
        )
        self.assertEqual(trend["asset_growth"], {"value": "0.0522", "status": "ok"})
        # 210 / 1210 = 0.173553... HALF_UP -> 0.1736
        self.assertEqual(
            second["ratios"]["net_income_to_assets"],
            {"value": "0.1736", "status": "ok"},
        )

    def test_round_half_up_on_exact_tie(self) -> None:
        # 2469/20000 = 0.12345：ROUND_HALF_UP -> 0.1235（银行家舍入会得 0.1234）。
        # 收入挂到负债科目（借记应付），使资产总额维持期初 20000.00。
        tied = {
            "period_id": "p1",
            "period_start": "2025-01-01",
            "period_end": "2025-01-31",
            "currency": "CNY",
            "chart": chart(),
            "opening_balances": [
                {"account_code": "1001", "debit": "20000", "credit": "0"},
                {"account_code": "3001", "debit": "0", "credit": "20000"},
            ],
            "entries": [
                {
                    "voucher_id": "JV-tie",
                    "posting_date": "2025-01-10",
                    "currency": "CNY",
                    "lines": [
                        {"line_id": "a", "account_code": "2001", "debit": "2469", "credit": "0"},
                        {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "2469"},
                    ],
                }
            ],
        }
        p2 = period("p2", "2025-02-01", "2025-02-28")
        status, body = self.service.generate_financial_analysis(
            {"periods": [tied, p2]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["periods"][0]["ratios"]["net_income_to_assets"]["value"], "0.1235"
        )

        # 增长率平局：(22469-20000)/20000 = 0.12345 -> 0.1235。
        later = period("p3", "2025-03-01", "2025-03-31", revenue="22469", expense="0")
        status, body = self.service.generate_financial_analysis(
            {
                "periods": [
                    period("base", "2025-01-01", "2025-01-31", revenue="20000", expense="0"),
                    later,
                ]
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["periods"][1]["ratios"]["trend"]["revenue_growth"]["value"], "0.1235"
        )

    def test_zero_denominator(self) -> None:
        p1 = period("p1", "2025-01-01", "2025-01-31", revenue="0", expense="0")
        p2 = period("p2", "2025-02-01", "2025-02-28", revenue="0", expense="0")
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, p2]}
        )
        self.assertEqual(status, 200)
        ratios = body["periods"][0]["ratios"]
        self.assertIsNone(ratios["net_profit_margin"]["value"])
        self.assertEqual(ratios["net_profit_margin"]["status"], "zero_denominator")
        trend = body["periods"][1]["ratios"]["trend"]
        self.assertIsNone(trend["revenue_growth"]["value"])
        self.assertEqual(trend["revenue_growth"]["status"], "zero_denominator")
        self.assertIsNone(trend["net_income_growth"]["value"])

    def test_too_few_periods_and_shape_errors(self) -> None:
        for payload, expected in [
            ({}, ("/periods", "required")),
            ({"periods": "x"}, ("/periods", "invalid_type")),
            ({"periods": []}, ("/periods", "too_few_periods")),
            ({"periods": [period("p1", "2025-01-01", "2025-01-31")]},
             ("/periods", "too_few_periods")),
            ({"periods": [period("p1", "2025-01-01", "2025-01-31"), 5]},
             ("/periods/1", "invalid_type")),
        ]:
            status, body = self.service.generate_financial_analysis(payload)
            self.assertEqual(status, 422, payload)
            self.assertFalse(body["valid"])
            self.assertIn(
                expected, {(e["path"], e["code"]) for e in body["errors"]}, payload
            )

    def test_period_id_required_blank_and_duplicate(self) -> None:
        p1 = period("p1", "2025-01-01", "2025-01-31")
        p2 = period("p2", "2025-02-01", "2025-02-28")
        dup = dict(p2, period_id="p1")
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, dup]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/1/period_id", "duplicate_period_id"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        blank = dict(p1)
        blank["period_id"] = ""
        status, body = self.service.generate_financial_analysis(
            {"periods": [blank, p2]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/0/period_id", "blank_value"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        missing = dict(p1)
        del missing["period_id"]
        status, body = self.service.generate_financial_analysis(
            {"periods": [missing, p2]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/0/period_id", "required"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_currency_and_chart_mismatch(self) -> None:
        p1 = period("p1", "2025-01-01", "2025-01-31")
        p2 = period("p2", "2025-02-01", "2025-02-28", currency="USD")
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, p2]}
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/periods/1/currency", "currency_mismatch"), pairs)
        # 期内分录币种与该期一致，故不再派生期内 currency_mismatch。
        self.assertNotIn(("/periods/1/entries/0/currency", "currency_mismatch"), pairs)

        p2b = period("p2", "2025-02-01", "2025-02-28", chart_id="COA-2")
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, p2b]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/1/chart/chart_id", "chart_mismatch"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_overlapping_periods_flags_later_starting(self) -> None:
        later = period("a", "2025-01-01", "2025-03-31", revenue="0", expense="0")
        earlier = period("b", "2025-02-01", "2025-03-31", revenue="0", expense="0")
        status, body = self.service.generate_financial_analysis(
            {"periods": [later, earlier]}
        )
        self.assertEqual(status, 422)
        # 第二项开始更晚（02-01 晚于 01-01），在 /periods/1 报错。
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]],
            [("/periods/1", "overlapping_periods")],
        )

        # 第二项开始更早：开始较晚的是索引 0。
        status, body = self.service.generate_financial_analysis(
            {
                "periods": [
                    period("a", "2025-02-01", "2025-03-31", revenue="0", expense="0"),
                    period("b", "2025-01-15", "2025-02-15", revenue="0", expense="0"),
                ]
            }
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/0", "overlapping_periods"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        # 相邻闭区间不相交：前一期 end 与下一期 start 相邻允许通过。
        status, body = self.service.generate_financial_analysis(two_periods())
        self.assertEqual(status, 200)

    def test_nested_validation_reuses_financial_statement_codes(self) -> None:
        p1 = period("p1", "2025-01-01", "2025-01-31")
        bad = dict(p1)
        bad["period_id"] = "p2"
        bad["period_start"] = "2025-02-15"
        bad["period_end"] = "2025-02-01"
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, bad]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/1/period_start", "invalid_period"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        bad_currency = dict(
            period("p2", "2025-02-01", "2025-02-28"), currency="usd"
        )
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, bad_currency]}
        )
        self.assertEqual(status, 422)
        pairs = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/periods/1/currency", "invalid_currency"), pairs)

        unknown = dict(p1)
        unknown["period_id"] = "p2"
        unknown["bogus"] = 1
        status, body = self.service.generate_financial_analysis(
            {"periods": [p1, unknown]}
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/periods/1/bogus", "unknown_field"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_top_level_unknown_field_and_error_sorting(self) -> None:
        payload = two_periods()
        payload["extra"] = 1
        status, body = self.service.generate_financial_analysis(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/extra", "unknown_field"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )
        paths = [e["path"] for e in body["errors"]]
        self.assertEqual(paths, sorted(paths))

    def test_errors_sorted_and_no_partial_results(self) -> None:
        status, body = self.service.generate_financial_analysis(
            {"periods": [period("p2", "2025-02-01", "2025-02-28", currency="USD"),
                         period("p1", "2025-01-01", "2025-01-31")]}
        )
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertNotIn("periods", body)
        self.assertNotIn("currency", body)
        paths = [e["path"] for e in body["errors"]]
        self.assertEqual(paths, sorted(paths))

    def test_deterministic_and_input_unchanged(self) -> None:
        payload = {"periods": list(reversed(two_periods()["periods"]))}
        snapshot = copy.deepcopy(payload)
        first = self.service.generate_financial_analysis(payload)
        second = self.service.generate_financial_analysis(payload)
        self.assertEqual(first, second)
        self.assertEqual(payload, snapshot)

    def test_three_periods_trend_chain(self) -> None:
        p1 = period("p1", "2025-01-01", "2025-01-31", revenue="100", expense="40")
        p2 = period("p2", "2025-02-01", "2025-02-28", revenue="200", expense="80")
        p3 = period("p3", "2025-03-01", "2025-03-31", revenue="400", expense="100")
        status, body = self.service.generate_financial_analysis(
            {"periods": [p3, p1, p2]}
        )
        self.assertEqual(status, 200)
        ids = [p["period_id"] for p in body["periods"]]
        self.assertEqual(ids, ["p1", "p2", "p3"])
        self.assertNotIn("trend", body["periods"][0]["ratios"])
        self.assertEqual(
            body["periods"][1]["ratios"]["trend"]["previous_period_id"], "p1"
        )
        self.assertEqual(
            body["periods"][2]["ratios"]["trend"]["previous_period_id"], "p2"
        )
        # (400-200)/|200| = 1.0000
        self.assertEqual(
            body["periods"][2]["ratios"]["trend"]["revenue_growth"]["value"],
            "1.0000",
        )


class GenerateFinancialAnalysisHttpTest(unittest.TestCase):
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

    def request(self, path, body=None, content_type=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            if content_type is not None:
                headers["Content-Type"] = content_type
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    PATH = "/v1/financial-analyses/generate"

    def test_endpoint_returns_200(self) -> None:
        status, payload = self.request(self.PATH, two_periods(), "application/json")
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(len(payload["periods"]), 2)

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(self.PATH, "{}", "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(self.PATH, "{bad", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(self.PATH, "[1]", "application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(self.PATH, {"periods": []}, "application/json")
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertEqual(
            payload["errors"][0]["code"], "too_few_periods"
        )

    def test_other_endpoints_unchanged(self) -> None:
        status, payload = self.request(
            "/v1/financial-statements/generate",
            {
                "period_start": "2025-01-01",
                "period_end": "2025-01-31",
                "currency": "CNY",
                "chart": chart(),
                "opening_balances": [],
                "entries": [],
            },
            "application/json",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertNotIn("periods", payload)


if __name__ == "__main__":
    unittest.main()
