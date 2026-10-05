import http.client
import json
import threading
import unittest
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart(chart_id="COA-1"):
    return {
        "chart_id": chart_id,
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


def entry(voucher_id, posting_date, lines, currency="CNY"):
    return {
        "voucher_id": voucher_id,
        "posting_date": posting_date,
        "currency": currency,
        "lines": lines,
    }


def revenue_entry(voucher_id, posting_date, amount, account="1001", currency="CNY"):
    return entry(
        voucher_id,
        posting_date,
        [
            {"line_id": "a", "account_code": account, "debit": amount, "credit": "0"},
            {"line_id": "b", "account_code": "4001", "debit": "0", "credit": amount},
        ],
        currency,
    )


def expense_entry(voucher_id, posting_date, amount, account="1001", currency="CNY"):
    return entry(
        voucher_id,
        posting_date,
        [
            {"line_id": "a", "account_code": "5001", "debit": amount, "credit": "0"},
            {"line_id": "b", "account_code": account, "debit": "0", "credit": amount},
        ],
        currency,
    )


def period(
    period_id,
    period_start,
    period_end,
    opening_balances=None,
    entries=None,
    currency="CNY",
    chart_obj=None,
    **extra,
):
    payload = {
        "period_id": period_id,
        "period_start": period_start,
        "period_end": period_end,
        "currency": currency,
        "chart": chart_obj if chart_obj is not None else chart(),
        "opening_balances": [] if opening_balances is None else opening_balances,
        "entries": [] if entries is None else entries,
    }
    payload.update(extra)
    return payload


def standard_q1(period_id="p1"):
    # 收入 300、费用 100、期初现金/资本各 1000：净利润 200，资产 1200，无负债。
    return period(
        period_id,
        "2026-01-01",
        "2026-01-31",
        opening_balances=[
            {"account_code": "1001", "debit": "1000.00", "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": "1000.00"},
        ],
        entries=[
            revenue_entry("R-1", "2026-01-10", "300"),
            expense_entry("E-1", "2026-01-20", "100"),
        ],
    )


def standard_q2(period_id="p2"):
    # 无期初：收入 450、费用 200，净利润 250，资产 250。
    return period(
        period_id,
        "2026-02-01",
        "2026-02-28",
        entries=[
            revenue_entry("R-2", "2026-02-10", "450"),
            expense_entry("E-2", "2026-02-20", "200"),
        ],
    )


def q4(numerator, denominator):
    """以精确十进制按 ROUND_HALF_UP 输出四位小数字符串，供断言对照。"""
    return str(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    )


class GenerateFinancialAnalysisServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def generate(self, *periods):
        return self.service.generate_financial_analysis({"periods": list(periods)})

    def test_success_periods_sorted_and_totals_match_single_endpoint(self) -> None:
        p1, p2 = standard_q1(), standard_q2()
        status, body = self.generate(p2, p1)  # 故意逆序提交
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["chart_id"], "COA-1")
        self.assertEqual([p["period_id"] for p in body["periods"]], ["p1", "p2"])

        first, second = body["periods"]
        self.assertEqual(
            first,
            {
                "period_id": "p1",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "total_revenue": "300.00",
                "total_expense": "100.00",
                "net_income": "200.00",
                "total_assets": "1200.00",
                "total_liabilities": "0.00",
                "total_equity_before_net_income": "1000.00",
            },
        )
        self.assertEqual(second["total_revenue"], "450.00")
        self.assertEqual(second["total_assets"], "250.00")
        self.assertEqual(second["total_equity_before_net_income"], "0.00")

        # 各期总额与单期财务报表端点完全一致（单期端点不接受 period_id）。
        for source, result in ((p1, first), (p2, second)):
            single_payload = {k: v for k, v in source.items() if k != "period_id"}
            single_status, single = self.service.generate_financial_statements(
                single_payload
            )
            self.assertEqual(single_status, 200)
            for key in (
                "total_revenue",
                "total_expense",
                "net_income",
                "total_assets",
                "total_liabilities",
                "total_equity_before_net_income",
            ):
                section = (
                    single["income_statement"]
                    if key in ("total_revenue", "total_expense", "net_income")
                    else single["balance_sheet"]
                )
                self.assertEqual(result[key], section[key])

    def test_ratios_use_unrounded_current_amounts(self) -> None:
        status, body = self.generate(standard_q1(), standard_q2())
        self.assertEqual(status, 200)
        ratios = body["ratios"]
        self.assertEqual(
            ratios[0]["net_profit_margin"],
            {"value": "0.6667", "status": "ok"},
        )
        self.assertEqual(
            ratios[0]["debt_to_assets"], {"value": "0.0000", "status": "ok"}
        )
        self.assertEqual(
            ratios[0]["net_income_to_assets"], {"value": "0.1667", "status": "ok"}
        )
        self.assertNotIn("trend", ratios[0])

        # 450/250 收入、250 净利润、250 资产。
        self.assertEqual(
            ratios[1]["net_profit_margin"], {"value": "0.5556", "status": "ok"}
        )
        self.assertEqual(
            ratios[1]["net_income_to_assets"], {"value": "1.0000", "status": "ok"}
        )

        # 分数输入下比率取自未舍入金额：66.67 / 100.01 = 0.6666...
        fractional = period(
            "f1",
            "2026-01-01",
            "2026-01-31",
            entries=[
                revenue_entry("R", "2026-01-10", "100.01"),
                expense_entry("E", "2026-01-20", "33.34"),
            ],
        )
        expected = q4("66.67", "100.01")
        status, body = self.generate(fractional, standard_q2())
        self.assertEqual(
            body["ratios"][0]["net_profit_margin"],
            {"value": expected, "status": "ok"},
        )

    def test_trend_growth_against_previous_sorted_period(self) -> None:
        # 三期逆序提交，趋势按排序后的相邻前期计算。
        p1 = period(
            "a",
            "2026-01-01",
            "2026-01-31",
            entries=[revenue_entry("R", "2026-01-10", "100")],
        )
        p2 = period(
            "b",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "120")],
        )
        p3 = period(
            "c",
            "2026-03-01",
            "2026-03-31",
            entries=[revenue_entry("R", "2026-03-10", "90")],
        )
        status, body = self.generate(p3, p1, p2)
        self.assertEqual(status, 200)
        self.assertEqual([p["period_id"] for p in body["periods"]], ["a", "b", "c"])
        self.assertNotIn("trend", body["ratios"][0])
        self.assertEqual(
            body["ratios"][1]["trend"],
            {
                "previous_period_id": "a",
                "revenue_growth": {"value": "0.2000", "status": "ok"},
                "net_income_growth": {"value": "0.2000", "status": "ok"},
                "asset_growth": {"value": "0.2000", "status": "ok"},
            },
        )
        self.assertEqual(
            body["ratios"][2]["trend"]["previous_period_id"], "b"
        )
        self.assertEqual(
            body["ratios"][2]["trend"]["revenue_growth"],
            {"value": "-0.2500", "status": "ok"},
        )

    def test_growth_denominator_uses_absolute_value_of_previous(self) -> None:
        # 前期收入为 -100（借方收入），本期收入 100：增长率 (100-(-100))/|-100|=2。
        negative_revenue = period(
            "neg",
            "2026-01-01",
            "2026-01-31",
            entries=[
                entry(
                    "X",
                    "2026-01-10",
                    [
                        {"line_id": "a", "account_code": "4001", "debit": "100", "credit": "0"},
                        {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "100"},
                    ],
                )
            ],
        )
        positive = period(
            "pos",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "100")],
        )
        status, body = self.generate(negative_revenue, positive)
        self.assertEqual(status, 200)
        trend = body["ratios"][1]["trend"]
        self.assertEqual(trend["previous_period_id"], "neg")
        # 前期收入、净利润、资产均为 -100：(100-(-100))/|-100|=2，三个增长率均为 2。
        self.assertEqual(trend["revenue_growth"], {"value": "2.0000", "status": "ok"})
        self.assertEqual(trend["net_income_growth"], {"value": "2.0000", "status": "ok"})
        self.assertEqual(trend["asset_growth"], {"value": "2.0000", "status": "ok"})

    def test_zero_denominator_value_null(self) -> None:
        empty = period("e", "2026-01-01", "2026-01-31")
        with_revenue = period(
            "r",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "10")],
        )
        status, body = self.generate(empty, with_revenue)
        self.assertEqual(status, 200)
        for key in ("net_profit_margin", "debt_to_assets", "net_income_to_assets"):
            self.assertEqual(
                body["ratios"][0][key], {"value": None, "status": "zero_denominator"}
            )
        for key in ("revenue_growth", "net_income_growth", "asset_growth"):
            self.assertEqual(
                body["ratios"][1]["trend"][key],
                {"value": None, "status": "zero_denominator"},
            )

    def test_round_half_up_not_bankers(self) -> None:
        # 净利润 0.01、收入 1.60：0.00625 第 5 位为 5，HALF_UP 进位为 0.0063。
        p1 = period(
            "h1",
            "2026-01-01",
            "2026-01-31",
            entries=[
                revenue_entry("R", "2026-01-10", "1.60"),
                expense_entry("E", "2026-01-20", "1.59"),
            ],
        )
        p2 = standard_q2()
        status, body = self.generate(p1, p2)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["ratios"][0]["net_profit_margin"], {"value": "0.0063", "status": "ok"}
        )

    def test_deterministic_and_input_not_mutated(self) -> None:
        p1, p2 = standard_q1(), standard_q2()
        snapshot = deepcopy({"periods": [p1, p2]})
        first = self.generate(p1, p2)
        second = self.generate(deepcopy(p1), deepcopy(p2))
        self.assertEqual(first, second)
        self.assertEqual({"periods": [p1, p2]}, snapshot)

    # ---- 业务校验 ----

    def assert_errors(self, payload, expected):
        status, body = self.service.generate_financial_analysis(payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertEqual(set(body), {"valid", "errors"})  # 无部分结果
        self.assertEqual(
            [(e["path"], e["code"]) for e in body["errors"]], expected
        )
        return body["errors"]

    def test_periods_required_type_and_count(self) -> None:
        self.assert_errors({}, [("/periods", "required")])
        self.assert_errors({"periods": []}, [("/periods", "too_few_periods")])
        self.assert_errors(
            {"periods": [standard_q1()]}, [("/periods", "too_few_periods")]
        )
        self.assert_errors({"periods": {}}, [("/periods", "invalid_type")])

    def test_period_element_must_be_object_others_still_validated(self) -> None:
        # 元素总数仍为两项，故不因数量报错；非对象元素只报自身类型错误。
        errors = self.assert_errors(
            {"periods": ["nope", standard_q2()]},
            [("/periods/0", "invalid_type")],
        )
        self.assertEqual(errors[0]["message"], "period must be an object")

    def test_period_id_required_blank_type(self) -> None:
        p_missing = standard_q1()
        del p_missing["period_id"]
        p_blank = period(
            "",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "1")],
        )
        self.assert_errors(
            {"periods": [p_missing, p_blank]},
            [
                ("/periods/0/period_id", "required"),
                ("/periods/1/period_id", "blank_value"),
            ],
        )
        p_number = period(
            7,
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "1")],
        )
        good = standard_q1()
        self.assert_errors(
            {"periods": [good, p_number]},
            [("/periods/1/period_id", "invalid_type")],
        )

    def test_duplicate_period_id_reported_on_later(self) -> None:
        p1 = standard_q1("same")
        p2 = standard_q2("same")
        self.assert_errors(
            {"periods": [p1, p2]},
            [("/periods/1/period_id", "duplicate_period_id")],
        )
        p3 = period(
            "same",
            "2026-03-01",
            "2026-03-31",
            entries=[revenue_entry("R", "2026-03-10", "1")],
        )
        self.assert_errors(
            {"periods": [p1, p2, p3]},
            [
                ("/periods/1/period_id", "duplicate_period_id"),
                ("/periods/2/period_id", "duplicate_period_id"),
            ],
        )

    def test_currency_mismatch_reported_on_later_period(self) -> None:
        # 第二项内部自洽但币种为 USD（分录同为 USD，不触发各期内部校验）。
        usd = period(
            "u",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "10", currency="USD")],
            currency="USD",
        )
        self.assert_errors(
            {"periods": [standard_q1(), usd]},
            [("/periods/1/currency", "currency_mismatch")],
        )

    def test_chart_mismatch_reported_on_later_period(self) -> None:
        other = period(
            "c",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "10")],
            chart_obj=chart("COA-2"),
        )
        self.assert_errors(
            {"periods": [standard_q1(), other]},
            [("/periods/1/chart/chart_id", "chart_mismatch")],
        )

    def test_overlapping_periods_reported_on_later_start(self) -> None:
        # 两期共享 2026-01-31：开始较晚的第二项报错。
        touching = period(
            "o",
            "2026-01-31",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-01-31", "10")],
        )
        q1 = standard_q1()
        self.assert_errors(
            {"periods": [q1, touching]},
            [("/periods/1", "overlapping_periods")],
        )
        # 相邻但不共享日期（01-31 与 02-01）不相交。
        status, _ = self.generate(standard_q1(), standard_q2())
        self.assertEqual(status, 200)
        # 开始日相同：输入顺序靠后者报错。
        a = period(
            "a",
            "2026-01-01",
            "2026-01-10",
            entries=[revenue_entry("R", "2026-01-05", "1")],
        )
        b = period(
            "b",
            "2026-01-01",
            "2026-01-31",
            entries=[revenue_entry("R", "2026-01-05", "2")],
        )
        self.assert_errors(
            {"periods": [a, b]}, [("/periods/1", "overlapping_periods")]
        )

    def test_nested_period_validation_reuses_financial_statement_rules(self) -> None:
        bad_currency = period(
            "b",
            "2026-02-01",
            "2026-02-28",
            currency="usd",
            entries=[revenue_entry("R", "2026-02-10", "1")],
        )
        self.assert_errors(
            {"periods": [standard_q1(), bad_currency]},
            [
                # 各期自身的币种错误：分录币种与请求一致，故只有请求字段报错。
                ("/periods/1/currency", "invalid_currency"),
            ],
        )

        bad_entry = period(
            "b",
            "2026-02-01",
            "2026-02-28",
            entries=[
                entry(
                    "R",
                    "2026-02-10",
                    [
                        {"line_id": "a", "account_code": "1001", "debit": "10", "credit": "0"},
                        {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "9"},
                    ],
                )
            ],
        )
        self.assert_errors(
            {"periods": [standard_q1(), bad_entry]},
            [("/periods/1/entries/0", "unbalanced_entry")],
        )

    def test_errors_sorted_by_path_and_code(self) -> None:
        p0 = standard_q1()
        del p0["currency"]  # /periods/0/currency required
        p1 = period(
            "",
            "2026-02-01",
            "2026-02-28",
            entries=[revenue_entry("R", "2026-02-10", "1")],
        )
        payload = {"periods": [p0, p1], "strange": 1}
        errors = self.service.generate_financial_analysis(payload)[1]["errors"]
        paths = [(e["path"], e["code"]) for e in errors]
        self.assertEqual(
            paths,
            sorted(paths),
        )
        self.assertIn(("/strange", "unknown_field"), paths)
        self.assertIn(("/periods/0/currency", "required"), paths)
        self.assertIn(("/periods/1/period_id", "blank_value"), paths)

    def test_unknown_field_inside_period_rejected(self) -> None:
        p1 = standard_q1()
        p1["nope"] = 1
        self.assert_errors(
            {"periods": [p1, standard_q2()]},
            [("/periods/0/nope", "unknown_field")],
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

    def request(self, path, body=None, content_type=None, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            if content_type is not None:
                headers["Content-Type"] = content_type
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_endpoint_returns_200(self) -> None:
        body = {"periods": [standard_q1(), standard_q2()]}
        status, payload = self.request(
            "/v1/financial-analyses/generate", body, "application/json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual([p["period_id"] for p in payload["periods"]], ["p1", "p2"])
        self.assertEqual(len(payload["ratios"]), 2)
        self.assertIn("trend", payload["ratios"][1])

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/financial-analyses/generate", {"periods": []}, "application/json"
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertEqual(
            [(e["path"], e["code"]) for e in payload["errors"]],
            [("/periods", "too_few_periods")],
        )

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/financial-analyses/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "/v1/financial-analyses/generate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "/v1/financial-analyses/generate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_unknown_route_still_404(self) -> None:
        status, payload = self.request(
            "/v1/financial-analyses/generate-typo", {}, "application/json"
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
