import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler
from ledgerlens.service import Service


def chart():
    return {
        "chart_id": "COA-CF",
        "effective_date": "2026-01-01",
        "accounts": [
            {"code": "1001", "name": "Cash on hand", "type": "asset",
             "normal_balance": "debit", "active": True, "parent_code": None},
            {"code": "1002", "name": "Bank", "type": "asset",
             "normal_balance": "debit", "active": True, "parent_code": None},
            {"code": "1003", "name": "Dormant cash", "type": "asset",
             "normal_balance": "debit", "active": False, "parent_code": None},
            {"code": "1101", "name": "Equipment", "type": "asset",
             "normal_balance": "debit", "active": True, "parent_code": None},
            {"code": "2001", "name": "Payables", "type": "liability",
             "normal_balance": "credit", "active": True, "parent_code": None},
            {"code": "3001", "name": "Capital", "type": "equity",
             "normal_balance": "credit", "active": True, "parent_code": None},
            {"code": "4001", "name": "Sales", "type": "revenue",
             "normal_balance": "credit", "active": True, "parent_code": None},
            {"code": "5001", "name": "Rent", "type": "expense",
             "normal_balance": "debit", "active": True, "parent_code": None},
        ],
    }


def entries():
    return [
        # 现金科目间划转：净现金变动 0，活动为 null，且不出现在报表中。
        {
            "voucher_id": "JV-1", "posting_date": "2026-01-05", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1002", "debit": "300", "credit": "0"},
                {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "300"},
            ],
        },
        # 经营流入 +200。
        {
            "voucher_id": "JV-2", "posting_date": "2026-01-10", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1001", "debit": "200", "credit": "0"},
                {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "200"},
            ],
        },
        # 经营流出 -80。
        {
            "voucher_id": "JV-3", "posting_date": "2026-01-15", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "5001", "debit": "80", "credit": "0"},
                {"line_id": "b", "account_code": "1001", "debit": "0", "credit": "80"},
            ],
        },
        # 投资流出 -500（银行购买设备）。
        {
            "voucher_id": "JV-4", "posting_date": "2026-01-20", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1101", "debit": "500", "credit": "0"},
                {"line_id": "b", "account_code": "1002", "debit": "0", "credit": "500"},
            ],
        },
        # 筹资流入 +400。
        {
            "voucher_id": "JV-5", "posting_date": "2026-01-25", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1001", "debit": "400", "credit": "0"},
                {"line_id": "b", "account_code": "3001", "debit": "0", "credit": "400"},
            ],
        },
        # 不涉及现金科目的凭证：活动为 null，不出现在报表中。
        {
            "voucher_id": "JV-6", "posting_date": "2026-01-26", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "5001", "debit": "10", "credit": "0"},
                {"line_id": "b", "account_code": "2001", "debit": "0", "credit": "10"},
            ],
        },
    ]


ACTIVITIES = [None, "operating", "operating", "investing", "financing", None]


def request_payload(**overrides):
    payload = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "currency": "CNY",
        "chart": chart(),
        "opening_balances": [
            {"account_code": "1001", "debit": "1000.00", "credit": "0"},
            {"account_code": "3001", "debit": "0", "credit": "1000.00"},
        ],
        "entries": entries(),
        "cash_account_codes": ["1001", "1002"],
        "entry_activities": list(ACTIVITIES),
    }
    payload.update(overrides)
    return payload


class GenerateCashFlowServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def codes(self, body):
        return {(e["path"], e["code"]) for e in body["errors"]}

    def test_success_partitions_items_and_totals(self) -> None:
        status, body = self.service.generate_cash_flow_statement(request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertEqual(body["chart_id"], "COA-CF")
        self.assertEqual(body["period_start"], "2026-01-01")
        self.assertEqual(body["period_end"], "2026-01-31")
        self.assertEqual(body["currency"], "CNY")

        statement = body["cash_flow_statement"]
        self.assertEqual(
            statement["operating"]["items"],
            [
                {"voucher_id": "JV-2", "posting_date": "2026-01-10", "amount": "200.00"},
                {"voucher_id": "JV-3", "posting_date": "2026-01-15", "amount": "-80.00"},
            ],
        )
        self.assertEqual(statement["operating"]["total"], "120.00")
        self.assertEqual(
            statement["investing"]["items"],
            [{"voucher_id": "JV-4", "posting_date": "2026-01-20", "amount": "-500.00"}],
        )
        self.assertEqual(statement["investing"]["total"], "-500.00")
        self.assertEqual(
            statement["financing"]["items"],
            [{"voucher_id": "JV-5", "posting_date": "2026-01-25", "amount": "400.00"}],
        )
        self.assertEqual(statement["financing"]["total"], "400.00")

        self.assertEqual(statement["beginning_cash_balance"], "1000.00")
        self.assertEqual(statement["net_cash_change"], "20.00")
        self.assertEqual(statement["ending_cash_balance"], "1020.00")

    def test_section_totals_equal_net_change_and_ending_identity(self) -> None:
        _, body = self.service.generate_cash_flow_statement(request_payload())
        statement = body["cash_flow_statement"]
        from decimal import Decimal

        total = sum(
            (Decimal(statement[a]["total"]) for a in ("operating", "investing", "financing")),
            Decimal("0"),
        )
        self.assertEqual(total, Decimal(statement["net_cash_change"]))
        self.assertEqual(
            Decimal(statement["beginning_cash_balance"]) + Decimal(statement["net_cash_change"]),
            Decimal(statement["ending_cash_balance"]),
        )

    def test_zero_change_vouchers_are_listed_nowhere(self) -> None:
        _, body = self.service.generate_cash_flow_statement(request_payload())
        statement = body["cash_flow_statement"]
        listed = {
            item["voucher_id"]
            for section in ("operating", "investing", "financing")
            for item in statement[section]["items"]
        }
        self.assertEqual(listed, {"JV-2", "JV-3", "JV-4", "JV-5"})

    def test_deterministic_same_input_same_output(self) -> None:
        first = self.service.generate_cash_flow_statement(request_payload())
        second = self.service.generate_cash_flow_statement(request_payload())
        self.assertEqual(first, second)

    def test_beginning_balance_sums_only_listed_cash_accounts(self) -> None:
        # 仅把 1001 视为现金：1002 不再是现金科目，JV-1 对 1001 是 -300 流出，
        # JV-4（只动 1002）现金净变动为 0，活动必须为 null 且不列入报表。
        payload = request_payload(
            cash_account_codes=["1001"],
            entry_activities=["financing", "operating", "operating", None, "financing", None],
        )
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 200)
        statement = body["cash_flow_statement"]
        self.assertEqual(statement["beginning_cash_balance"], "1000.00")
        self.assertEqual(statement["operating"]["total"], "120.00")
        self.assertEqual(statement["financing"]["total"], "100.00")
        self.assertEqual(statement["investing"]["items"], [])
        self.assertEqual(statement["investing"]["total"], "0.00")
        self.assertEqual(statement["net_cash_change"], "220.00")
        self.assertEqual(statement["ending_cash_balance"], "1220.00")

    # ---- 顶层与既有字段沿用原校验 ----

    def test_top_level_unknown_field_rejected(self) -> None:
        status, body = self.service.generate_cash_flow_statement(request_payload(bogus=1))
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertIn(("/bogus", "unknown_field"), self.codes(body))

    def test_existing_endpoints_still_reject_new_fields(self) -> None:
        for caller in (
            self.service.generate_trial_balance,
            self.service.generate_financial_statements,
        ):
            status, body = caller(request_payload())
            self.assertEqual(status, 422)
            self.assertIn(("/cash_account_codes", "unknown_field"), self.codes(body))
            self.assertIn(("/entry_activities", "unknown_field"), self.codes(body))

    def test_inherited_base_validation_still_applies(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(currency="usd")
        )
        self.assertEqual(status, 422)
        self.assertIn(("/currency", "invalid_currency"), self.codes(body))

    # ---- cash_account_codes ----

    def test_cash_account_codes_missing(self) -> None:
        payload = request_payload()
        del payload["cash_account_codes"]
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes", "required"), self.codes(body))

    def test_cash_account_codes_not_array(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes="1001")
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes", "invalid_type"), self.codes(body))

    def test_cash_account_codes_empty(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=[])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes", "too_few_cash_accounts"), self.codes(body))

    def test_cash_account_codes_blank_element(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["1001", ""])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes/1", "blank_value"), self.codes(body))

    def test_cash_account_codes_wrong_element_type(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["1001", 9])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes/1", "invalid_type"), self.codes(body))

    def test_cash_account_codes_duplicate(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["1001", "1001"])
        )
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/cash_account_codes/1", "duplicate_cash_account"), codes)
        # 重复元素不派生引用错误，首元素本身也无错误。
        self.assertNotIn(("/cash_account_codes/0", "duplicate_cash_account"), codes)

    def test_cash_account_unknown(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["9999"])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes/0", "unknown_account"), self.codes(body))

    def test_cash_account_inactive(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["1003"])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/cash_account_codes/0", "inactive_account"), self.codes(body))

    def test_cash_account_not_asset(self) -> None:
        # 负债科目：已知且启用但非资产，只报 cash_account_not_asset。
        status, body = self.service.generate_cash_flow_statement(
            request_payload(cash_account_codes=["2001"])
        )
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/cash_account_codes/0", "cash_account_not_asset"), codes)
        self.assertNotIn(("/cash_account_codes/0", "unknown_account"), codes)
        self.assertNotIn(("/cash_account_codes/0", "inactive_account"), codes)

    def test_unknown_cash_account_not_derived_when_chart_invalid(self) -> None:
        bad_chart = chart()
        bad_chart["accounts"] = []
        status, body = self.service.generate_cash_flow_statement(
            request_payload(chart=bad_chart, cash_account_codes=["9999"])
        )
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/chart/accounts", "too_few_accounts"), codes)
        self.assertFalse(
            any(path.startswith("/cash_account_codes") and code == "unknown_account"
                for path, code in codes)
        )

    # ---- entry_activities ----

    def test_entry_activities_missing(self) -> None:
        payload = request_payload()
        del payload["entry_activities"]
        status, body = self.service.generate_cash_flow_statement(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "required"), self.codes(body))

    def test_entry_activities_not_array(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities="operating")
        )
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "invalid_type"), self.codes(body))

    def test_entry_activities_count_mismatch(self) -> None:
        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities=[None, "operating"])
        )
        self.assertEqual(status, 422)
        self.assertIn(("/entry_activities", "activity_count_mismatch"), self.codes(body))

    def test_entry_activities_invalid_value(self) -> None:
        activities = list(ACTIVITIES)
        activities[1] = "operatingg"
        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities=activities)
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/1", "invalid_cash_flow_activity"), self.codes(body)
        )

    def test_missing_activity_for_nonzero_change(self) -> None:
        activities = list(ACTIVITIES)
        activities[1] = None  # JV-2 有 +200 现金变动却给 null。
        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities=activities)
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/1", "missing_cash_flow_activity"), self.codes(body)
        )

    def test_activity_without_cash_change(self) -> None:
        activities = list(ACTIVITIES)
        activities[0] = "investing"  # JV-1 现金净变动为 0 却给了活动。
        status, body = self.service.generate_cash_flow_statement(
            request_payload(entry_activities=activities)
        )
        self.assertEqual(status, 422)
        self.assertIn(
            ("/entry_activities/0", "activity_without_cash_change"), self.codes(body)
        )

    def test_activity_errors_not_derived_for_unbalanced_entry(self) -> None:
        payload = request_payload()
        payload["entries"][1] = {
            "voucher_id": "JV-2", "posting_date": "2026-01-10", "currency": "CNY",
            "lines": [
                {"line_id": "a", "account_code": "1001", "debit": "100", "credit": "0"},
                {"line_id": "b", "account_code": "4001", "debit": "0", "credit": "90"},
            ],
        }
        activities = list(ACTIVITIES)
        activities[1] = None  # 凭证不平衡：只报 unbalanced_entry，不派生活动错误。
        status, body = self.service.generate_cash_flow_statement(
            request_payload(
                entries=payload["entries"], entry_activities=activities
            )
        )
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/entries/1", "unbalanced_entry"), codes)
        self.assertNotIn(
            ("/entry_activities/1", "missing_cash_flow_activity"), codes
        )

    def test_errors_sorted_by_path_then_code(self) -> None:
        activities = list(ACTIVITIES)
        activities[1] = "nope"
        status, body = self.service.generate_cash_flow_statement(
            request_payload(
                cash_account_codes=["9999"], entry_activities=activities
            )
        )
        self.assertEqual(status, 422)
        pairs = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(pairs, sorted(pairs))


class GenerateCashFlowHttpTest(unittest.TestCase):
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

    def test_endpoint_returns_200(self) -> None:
        status, payload = self.request(
            "/v1/cash-flow-statements/generate", request_payload(), "application/json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertIn("cash_flow_statement", payload)
        self.assertEqual(
            payload["cash_flow_statement"]["ending_cash_balance"], "1020.00"
        )

    def test_media_json_and_object_errors(self) -> None:
        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "{bad", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

        status, payload = self.request(
            "/v1/cash-flow-statements/generate", "[1]", "application/json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "request_not_object")

    def test_business_validation_failure_422(self) -> None:
        status, payload = self.request(
            "/v1/cash-flow-statements/generate",
            request_payload(cash_account_codes=[]),
            "application/json",
        )
        self.assertEqual(status, 422)
        self.assertFalse(payload["valid"])
        self.assertIn(
            ("/cash_account_codes", "too_few_cash_accounts"),
            {(e["path"], e["code"]) for e in payload["errors"]},
        )


if __name__ == "__main__":
    unittest.main()
