import unittest

from ledgerlens.service import Service


def account(code, name="Cash", type="asset", normal_balance="debit", active=True, parent_code=None):
    return {
        "code": code,
        "name": name,
        "type": type,
        "normal_balance": normal_balance,
        "active": active,
        "parent_code": parent_code,
    }


def valid_payload():
    return {
        "chart_id": "COA-2026",
        "effective_date": "2026-10-03",
        "accounts": [
            account("1000", "Assets", type="asset", normal_balance="debit"),
            account("1100", "Cash", parent_code="1000"),
            account("2000", "Liabilities", type="liability", normal_balance="credit"),
            account("4000", "Revenue", type="revenue", normal_balance="credit"),
            account("5000", "Expense", type="expense", normal_balance="debit"),
        ],
    }


class ChartValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def validate(self, payload):
        return self.service.validate_chart_of_accounts(payload)

    def codes(self, body):
        return {(e["path"], e["code"]) for e in body["errors"]}

    # ---- success -------------------------------------------------------

    def test_valid_chart_returns_200_with_counts(self) -> None:
        status, body = self.validate(valid_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "valid": True,
                "chart_id": "COA-2026",
                "effective_date": "2026-10-03",
                "account_count": 5,
                "root_count": 4,
                "type_counts": {
                    "asset": 2,
                    "liability": 1,
                    "equity": 0,
                    "revenue": 1,
                    "expense": 1,
                },
            },
        )

    def test_stateless_same_input_same_output(self) -> None:
        payload = valid_payload()
        self.assertEqual(self.validate(payload), self.validate(payload))

    def test_root_only_chart(self) -> None:
        payload = {
            "chart_id": "c",
            "effective_date": "2026-01-01",
            "accounts": [account("1", type="equity", normal_balance="credit")],
        }
        status, body = self.validate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["root_count"], 1)
        self.assertEqual(body["account_count"], 1)
        self.assertEqual(body["type_counts"]["equity"], 1)

    # ---- top-level field errors ---------------------------------------

    def test_required_top_level_fields(self) -> None:
        status, body = self.validate({})
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        codes = self.codes(body)
        self.assertIn(("/chart_id", "required"), codes)
        self.assertIn(("/effective_date", "required"), codes)
        self.assertIn(("/accounts", "required"), codes)

    def test_blank_and_type_top_level(self) -> None:
        status, body = self.validate(
            {"chart_id": "", "effective_date": 20261003, "accounts": "x"}
        )
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/chart_id", "blank_value"), codes)
        self.assertIn(("/effective_date", "invalid_type"), codes)
        self.assertIn(("/accounts", "invalid_type"), codes)

    def test_unknown_top_level_field(self) -> None:
        payload = valid_payload()
        payload["extra"] = 1
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/extra", "unknown_field"), self.codes(body))

    def test_invalid_dates(self) -> None:
        for bad in ("2026-02-30", "2026-1-01", "2026/10/03", "not-a-date"):
            payload = valid_payload()
            payload["effective_date"] = bad
            status, body = self.validate(payload)
            self.assertEqual(status, 422, bad)
            self.assertIn(("/effective_date", "invalid_date"), self.codes(body))

    def test_too_few_accounts(self) -> None:
        payload = valid_payload()
        payload["accounts"] = []
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/accounts", "too_few_accounts"), self.codes(body))

    # ---- account field errors -----------------------------------------

    def test_account_must_be_object(self) -> None:
        payload = valid_payload()
        payload["accounts"] = ["nope", 42]
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/accounts/0", "invalid_type"), codes)
        self.assertIn(("/accounts/1", "invalid_type"), codes)

    def test_account_required_blank_type_fields(self) -> None:
        payload = valid_payload()
        payload["accounts"] = [
            {"code": "", "name": None, "type": 7, "normal_balance": 1, "active": "y"}
        ]
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        codes = self.codes(body)
        self.assertIn(("/accounts/0/code", "blank_value"), codes)
        self.assertIn(("/accounts/0/name", "invalid_type"), codes)
        self.assertIn(("/accounts/0/type", "invalid_type"), codes)
        self.assertIn(("/accounts/0/normal_balance", "invalid_type"), codes)
        self.assertIn(("/accounts/0/active", "invalid_type"), codes)
        self.assertIn(("/accounts/0/parent_code", "required"), codes)

    def test_account_missing_fields(self) -> None:
        payload = valid_payload()
        payload["accounts"] = [{}]
        status, body = self.validate(payload)
        codes = self.codes(body)
        for field in ("code", "name", "type", "normal_balance", "active", "parent_code"):
            self.assertIn((f"/accounts/0/{field}", "required"), codes, field)

    def test_unknown_account_field(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["bogus"] = 1
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/accounts/0/bogus", "unknown_field"), self.codes(body))

    def test_invalid_account_type_enum(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["type"] = "capital"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/type", "invalid_account_type"), self.codes(body))

    def test_invalid_normal_balance_enum(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["normal_balance"] = "side"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/normal_balance", "invalid_normal_balance"), self.codes(body))

    def test_active_must_be_boolean_not_int(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["active"] = 1
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/active", "invalid_type"), self.codes(body))

    def test_parent_code_string_or_null(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["parent_code"] = 7
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/parent_code", "invalid_type"), self.codes(body))
        payload = valid_payload()
        payload["accounts"][0]["parent_code"] = ""
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/parent_code", "blank_value"), self.codes(body))

    # ---- relation errors ----------------------------------------------

    def test_duplicate_account_code(self) -> None:
        payload = valid_payload()
        payload["accounts"].append(account("1000", "Dup"))
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/accounts/5/code", "duplicate_account_code"), self.codes(body))

    def test_parent_not_found(self) -> None:
        payload = valid_payload()
        payload["accounts"][1]["parent_code"] = "9999"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/1/parent_code", "parent_not_found"), self.codes(body))

    def test_self_parent(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["parent_code"] = "1000"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/parent_code", "self_parent"), self.codes(body))

    def test_parent_cycle_reports_each_node_in_cycle(self) -> None:
        payload = valid_payload()
        payload["accounts"] = [
            account("a", type="asset", normal_balance="debit", parent_code="b"),
            account("b", type="asset", normal_balance="debit", parent_code="c"),
            account("c", type="asset", normal_balance="debit", parent_code="b"),
        ]
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        codes = self.codes(body)
        # b 与 c 成环；a 仅指向环，不在环内。
        self.assertIn(("/accounts/1/parent_code", "parent_cycle"), codes)
        self.assertIn(("/accounts/2/parent_code", "parent_cycle"), codes)
        self.assertNotIn(("/accounts/0/parent_code", "parent_cycle"), codes)

    def test_two_node_cycle(self) -> None:
        payload = valid_payload()
        payload["accounts"] = [
            account("a", type="asset", normal_balance="debit", parent_code="b"),
            account("b", type="asset", normal_balance="debit", parent_code="a"),
        ]
        status, body = self.validate(payload)
        codes = self.codes(body)
        self.assertIn(("/accounts/0/parent_code", "parent_cycle"), codes)
        self.assertIn(("/accounts/1/parent_code", "parent_cycle"), codes)

    def test_parent_type_mismatch(self) -> None:
        payload = valid_payload()
        # Cash(asset) 挂到 Revenue(revenue) 下
        payload["accounts"][1]["parent_code"] = "4000"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/1/parent_code", "parent_type_mismatch"), self.codes(body))

    def test_inactive_parent(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["active"] = False
        payload["accounts"][1]["active"] = True
        payload["accounts"][1]["parent_code"] = "1000"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/1/parent_code", "inactive_parent"), self.codes(body))

    def test_inactive_child_under_inactive_parent_allowed(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["active"] = False
        payload["accounts"][1]["active"] = False
        payload["accounts"][1]["parent_code"] = "1000"
        status, body = self.validate(payload)
        self.assertEqual(status, 200)

    def test_normal_balance_mismatch(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["normal_balance"] = "credit"
        status, body = self.validate(payload)
        self.assertIn(("/accounts/0/normal_balance", "normal_balance_mismatch"), self.codes(body))
        payload = valid_payload()
        payload["accounts"][3]["normal_balance"] = "debit"  # revenue must be credit
        status, body = self.validate(payload)
        self.assertIn(("/accounts/3/normal_balance", "normal_balance_mismatch"), self.codes(body))

    def test_all_type_balance_constraints(self) -> None:
        # asset/expense -> debit; liability/equity/revenue -> credit
        payload = valid_payload()
        payload["accounts"] = [
            account("a", type="asset", normal_balance="credit"),
            account("e", type="expense", normal_balance="credit"),
            account("l", type="liability", normal_balance="debit"),
            account("q", type="equity", normal_balance="debit"),
            account("r", type="revenue", normal_balance="debit"),
        ]
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        mismatches = {p for p, c in self.codes(body) if c == "normal_balance_mismatch"}
        self.assertEqual(
            mismatches,
            {
                "/accounts/0/normal_balance",
                "/accounts/1/normal_balance",
                "/accounts/2/normal_balance",
                "/accounts/3/normal_balance",
                "/accounts/4/normal_balance",
            },
        )

    def test_invalid_field_does_not_derive_relation_errors(self) -> None:
        # 科目自身 type 非法时，不再派生 normal_balance_mismatch / parent_type_mismatch。
        payload = valid_payload()
        payload["accounts"][1]["type"] = "bogus"
        payload["accounts"][1]["normal_balance"] = "credit"
        status, body = self.validate(payload)
        codes = self.codes(body)
        self.assertIn(("/accounts/1/type", "invalid_account_type"), codes)
        self.assertNotIn(("/accounts/1/normal_balance", "normal_balance_mismatch"), codes)
        self.assertNotIn(("/accounts/1/parent_code", "parent_type_mismatch"), codes)

    def test_errors_sorted_by_path_then_code(self) -> None:
        payload = valid_payload()
        payload["chart_id"] = ""
        payload["zzz"] = 1
        payload["accounts"][0]["normal_balance"] = "credit"
        payload["accounts"].append(account("1000", "Dup"))
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        for e in body["errors"]:
            self.assertEqual(set(e), {"path", "code", "message"})

    def test_inactive_parent_not_counted_relation_for_invalid_child(self) -> None:
        payload = valid_payload()
        payload["accounts"][0]["active"] = False
        # child missing active -> invalid record -> no inactive_parent derived
        child = dict(payload["accounts"][1])
        del child["active"]
        payload["accounts"][1] = child
        status, body = self.validate(payload)
        codes = self.codes(body)
        self.assertIn(("/accounts/1/active", "required"), codes)
        self.assertNotIn(("/accounts/1/parent_code", "inactive_parent"), codes)


if __name__ == "__main__":
    unittest.main()
