import unittest

from ledgerlens.service import Service


def account(**overrides) -> dict:
    base = {
        "code": "1000",
        "name": "Cash",
        "type": "asset",
        "normal_balance": "debit",
        "active": True,
        "parent_code": None,
    }
    base.update(overrides)
    return base


def valid_payload(**overrides) -> dict:
    payload = {
        "chart_id": "COA-2026",
        "effective_date": "2026-10-03",
        "accounts": [
            account(code="1000", name="Assets", type="asset", normal_balance="debit"),
            account(
                code="1100",
                name="Cash",
                type="asset",
                normal_balance="debit",
                parent_code="1000",
            ),
        ],
    }
    payload.update(overrides)
    return payload


class ChartValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def validate(self, payload: dict):
        return self.service.validate_chart_of_accounts(payload)

    def error_set(self, body: dict) -> set:
        return {(e["path"], e["code"]) for e in body["errors"]}

    # ---- 成功路径 ----
    def test_valid_chart_returns_200_with_counts(self) -> None:
        status, body = self.validate(valid_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "valid": True,
                "chart_id": "COA-2026",
                "effective_date": "2026-10-03",
                "account_count": 2,
                "root_count": 1,
                "type_counts": {
                    "asset": 2,
                    "liability": 0,
                    "equity": 0,
                    "revenue": 0,
                    "expense": 0,
                },
            },
        )

    def test_type_counts_cover_all_five_categories(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="1", type="asset", normal_balance="debit"),
                account(code="2", type="liability", normal_balance="credit"),
                account(code="3", type="equity", normal_balance="credit"),
                account(code="4", type="revenue", normal_balance="credit"),
                account(code="5", type="expense", normal_balance="debit"),
            ]
        )
        status, body = self.validate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["account_count"], 5)
        self.assertEqual(body["root_count"], 5)
        self.assertEqual(
            body["type_counts"],
            {"asset": 1, "liability": 1, "equity": 1, "revenue": 1, "expense": 1},
        )

    def test_stateless_same_input_same_output(self) -> None:
        payload = valid_payload()
        self.assertEqual(self.validate(payload), self.validate(payload))

    # ---- 顶层字段 ----
    def test_required_top_level_fields(self) -> None:
        status, body = self.validate({})
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        codes = dict(self.error_set(body))
        self.assertEqual(codes["/chart_id"], "required")
        self.assertEqual(codes["/effective_date"], "required")
        self.assertEqual(codes["/accounts"], "required")

    def test_blank_and_type_errors_reuse_existing_codes(self) -> None:
        status, body = self.validate(
            {"chart_id": "", "effective_date": 20261003, "accounts": []}
        )
        self.assertEqual(status, 422)
        errors = self.error_set(body)
        self.assertIn(("/chart_id", "blank_value"), errors)
        self.assertIn(("/effective_date", "invalid_type"), errors)
        self.assertIn(("/accounts", "too_few_accounts"), errors)

    def test_invalid_calendar_dates(self) -> None:
        for bad in ("2026-02-30", "2026-13-01", "2026/10/03", "not-a-date"):
            status, body = self.validate(valid_payload(effective_date=bad))
            self.assertEqual(status, 422, bad)
            self.assertIn(("/effective_date", "invalid_date"), self.error_set(body))

    def test_accounts_must_be_array_and_nonempty(self) -> None:
        status, body = self.validate(valid_payload(accounts="nope"))
        self.assertIn(("/accounts", "invalid_type"), self.error_set(body))
        status, body = self.validate(valid_payload(accounts=[]))
        self.assertIn(("/accounts", "too_few_accounts"), self.error_set(body))

    def test_unknown_fields_rejected_top_and_account_level(self) -> None:
        payload = valid_payload()
        payload["extra"] = 1
        payload["accounts"][0]["bogus"] = 2
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        errors = self.error_set(body)
        self.assertIn(("/extra", "unknown_field"), errors)
        self.assertIn(("/accounts/0/bogus", "unknown_field"), errors)

    def test_non_object_account(self) -> None:
        status, body = self.validate(valid_payload(accounts=["x"]))
        self.assertIn(("/accounts/0", "invalid_type"), self.error_set(body))

    # ---- 科目字段 ----
    def test_account_field_required_blank_type(self) -> None:
        status, body = self.validate(
            valid_payload(
                accounts=[
                    {"name": "", "type": 5, "normal_balance": None, "active": "yes"}
                ]
            )
        )
        errors = self.error_set(body)
        self.assertIn(("/accounts/0/code", "required"), errors)
        self.assertIn(("/accounts/0/name", "blank_value"), errors)
        self.assertIn(("/accounts/0/type", "invalid_type"), errors)
        self.assertIn(("/accounts/0/normal_balance", "invalid_type"), errors)
        self.assertIn(("/accounts/0/active", "invalid_type"), errors)
        self.assertIn(("/accounts/0/parent_code", "required"), errors)

    def test_invalid_enums_use_dedicated_codes(self) -> None:
        status, body = self.validate(
            valid_payload(
                accounts=[account(type="capital", normal_balance="sideways")]
            )
        )
        errors = self.error_set(body)
        self.assertIn(("/accounts/0/type", "invalid_account_type"), errors)
        self.assertIn(("/accounts/0/normal_balance", "invalid_normal_balance"), errors)

    def test_active_must_be_boolean_not_int(self) -> None:
        status, body = self.validate(valid_payload(accounts=[account(active=1)]))
        self.assertIn(("/accounts/0/active", "invalid_type"), self.error_set(body))

    def test_parent_code_string_or_null(self) -> None:
        status, body = self.validate(valid_payload(accounts=[account(parent_code=7)]))
        self.assertIn(
            ("/accounts/0/parent_code", "invalid_type"), self.error_set(body)
        )
        status, body = self.validate(valid_payload(accounts=[account(parent_code="")]))
        self.assertIn(
            ("/accounts/0/parent_code", "blank_value"), self.error_set(body)
        )

    # ---- 关系错误 ----
    def test_duplicate_account_code(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="1000"),
                account(code="1000"),
            ]
        )
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(("/accounts/1/code", "duplicate_account_code"), self.error_set(body))

    def test_parent_not_found(self) -> None:
        status, body = self.validate(
            valid_payload(accounts=[account(code="1", parent_code="999")])
        )
        self.assertIn(
            ("/accounts/0/parent_code", "parent_not_found"), self.error_set(body)
        )

    def test_self_parent(self) -> None:
        status, body = self.validate(
            valid_payload(accounts=[account(code="1", parent_code="1")])
        )
        self.assertIn(
            ("/accounts/0/parent_code", "self_parent"), self.error_set(body)
        )

    def test_cycle_each_member_flagged_on_its_own_parent_code(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="A", parent_code="C"),
                account(code="B", parent_code="A", type="asset", normal_balance="debit"),
                account(code="C", parent_code="B", type="asset", normal_balance="debit"),
            ]
        )
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        errors = self.error_set(body)
        self.assertIn(("/accounts/0/parent_code", "parent_cycle"), errors)
        self.assertIn(("/accounts/1/parent_code", "parent_cycle"), errors)
        self.assertIn(("/accounts/2/parent_code", "parent_cycle"), errors)

    def test_tail_into_cycle_is_not_flagged(self) -> None:
        # A<->B form the cycle; D points into it but is not part of it.
        payload = valid_payload(
            accounts=[
                account(code="D", parent_code="A"),
                account(code="A", parent_code="B"),
                account(code="B", parent_code="A"),
            ]
        )
        status, body = self.validate(payload)
        errors = self.error_set(body)
        self.assertNotIn(("/accounts/0/parent_code", "parent_cycle"), errors)
        self.assertIn(("/accounts/1/parent_code", "parent_cycle"), errors)
        self.assertIn(("/accounts/2/parent_code", "parent_cycle"), errors)

    def test_parent_type_mismatch(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="A", type="asset", normal_balance="debit"),
                account(
                    code="B",
                    type="expense",
                    normal_balance="debit",
                    parent_code="A",
                ),
            ]
        )
        status, body = self.validate(payload)
        self.assertIn(
            ("/accounts/1/parent_code", "parent_type_mismatch"), self.error_set(body)
        )

    def test_inactive_parent(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="A", active=False),
                account(code="B", parent_code="A", active=True),
            ]
        )
        status, body = self.validate(payload)
        self.assertIn(
            ("/accounts/1/parent_code", "inactive_parent"), self.error_set(body)
        )

    def test_inactive_child_allowed_under_inactive_parent(self) -> None:
        payload = valid_payload(
            accounts=[
                account(code="A", active=False),
                account(code="B", parent_code="A", active=False),
            ]
        )
        status, body = self.validate(payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["root_count"], 1)

    def test_normal_balance_mismatch(self) -> None:
        # asset must be debit; liability must be credit.
        status, body = self.validate(
            valid_payload(accounts=[account(code="A", type="asset", normal_balance="credit")])
        )
        self.assertIn(
            ("/accounts/0/normal_balance", "normal_balance_mismatch"), self.error_set(body)
        )
        status, body = self.validate(
            valid_payload(
                accounts=[account(code="L", type="liability", normal_balance="debit")]
            )
        )
        self.assertIn(
            ("/accounts/0/normal_balance", "normal_balance_mismatch"), self.error_set(body)
        )

    def test_allowed_balances_per_type(self) -> None:
        cases = {
            "asset": "debit",
            "expense": "debit",
            "liability": "credit",
            "equity": "credit",
            "revenue": "credit",
        }
        for i, (typ, bal) in enumerate(cases.items()):
            status, body = self.validate(
                valid_payload(
                    accounts=[account(code=str(i), type=typ, normal_balance=bal)]
                )
            )
            self.assertEqual(status, 200, (typ, bal, body))

    def test_invalid_field_does_not_derive_relation_errors(self) -> None:
        # 子科目 type 字段非法（字段错误）：不再派生 parent_type_mismatch。
        payload = valid_payload(
            accounts=[
                account(code="A", type="asset", normal_balance="debit"),
                account(
                    code="B",
                    type="nope",
                    normal_balance="debit",
                    parent_code="A",
                ),
            ]
        )
        status, body = self.validate(payload)
        errors = self.error_set(body)
        self.assertIn(("/accounts/1/type", "invalid_account_type"), errors)
        self.assertNotIn(("/accounts/1/parent_code", "parent_type_mismatch"), errors)

    def test_errors_sorted_by_path_then_code(self) -> None:
        payload = valid_payload()
        payload["zzz"] = 1
        payload["chart_id"] = ""
        payload["accounts"][0]["active"] = "x"
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        for e in body["errors"]:
            self.assertEqual(set(e), {"path", "code", "message"})


if __name__ == "__main__":
    unittest.main()
