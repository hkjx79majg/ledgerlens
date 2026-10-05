import copy
import json
import unittest
from decimal import Decimal

import ledgerlens
from ledgerlens import (
    AccountMappingError,
    ConsolidationError,
    DuplicateEntityError,
    InvalidConsolidationInputError,
    PeriodMismatchError,
    UnbalancedEntityError,
    consolidate_group_trial_balance,
)
from ledgerlens.service import Service


def entity(entity_id, period="2026-09", ratio=None, balances=()):
    node = {"entity_id": entity_id, "period": period, "balances": list(balances)}
    if ratio is not None:
        node["ownership_ratio"] = ratio
    return node


def side(account_code, direction, amount):
    return {"account_code": account_code, "side": direction, "amount": amount}


def pair(account_code, debit="0.00", credit="0.00"):
    return {"account_code": account_code, "debit": debit, "credit": credit}


def txn(entity_a, account_a, amount_a, entity_b, account_b, amount_b,
        category="receivable_payable", reference=None):
    node = {
        "side_a": {"entity_id": entity_a, "account_code": account_a, "amount": amount_a},
        "side_b": {"entity_id": entity_b, "account_code": account_b, "amount": amount_b},
        "category": category,
    }
    if reference is not None:
        node["reference"] = reference
    return node


def balances_by_code(result):
    return {row["group_account_code"]: row for row in result["balances"]}


class PublicEntryTest(unittest.TestCase):
    def test_top_level_package_exports_entry_and_errors(self):
        self.assertIs(ledgerlens.consolidate_group_trial_balance,
                      consolidate_group_trial_balance)
        for error in (PeriodMismatchError, DuplicateEntityError,
                      UnbalancedEntityError, AccountMappingError,
                      InvalidConsolidationInputError):
            self.assertTrue(issubclass(error, ConsolidationError))
            self.assertTrue(issubclass(error, ValueError))

    def test_service_delegates(self):
        result = Service().consolidate_group_trial_balance({"entities": []})
        self.assertEqual(result["entity_count"], 0)


class EmptyAndSingleEntityTest(unittest.TestCase):
    def test_empty_entities_returns_zero_result(self):
        result = consolidate_group_trial_balance({"entities": []})
        self.assertIsNone(result["period"])
        self.assertEqual(result["entity_count"], 0)
        self.assertEqual(result["balances"], [])
        self.assertEqual(result["total_debit"], "0.00")
        self.assertEqual(result["total_credit"], "0.00")
        self.assertEqual(result["eliminations"], [])
        self.assertEqual(result["reconciliation_differences"], [])
        self.assertEqual(result["unmatched_items"], [])
        self.assertEqual(result["minority_interests"], [])
        json.dumps(result)

    def test_single_wholly_owned_entity_matches_mapped_balances(self):
        payload = {
            "entities": [
                entity("P", balances=[
                    side("1001", "debit", Decimal("1000.00")),
                    side("3001", "credit", Decimal("1000.00")),
                ])
            ],
            "account_mapping": {"P": {"1001": "CASH", "3001": "EQUITY"}},
        }
        result = consolidate_group_trial_balance(payload)
        self.assertEqual(result["period"], "2026-09")
        rows = balances_by_code(result)
        self.assertEqual(rows["CASH"], {"group_account_code": "CASH",
                                        "debit": "1000.00", "credit": "0.00"})
        self.assertEqual(rows["EQUITY"], {"group_account_code": "EQUITY",
                                          "debit": "0.00", "credit": "1000.00"})
        self.assertEqual(result["total_debit"], "1000.00")
        self.assertEqual(result["total_credit"], "1000.00")
        self.assertEqual(result["eliminations"], [])
        self.assertEqual(result["minority_interests"], [])

    def test_pair_form_balances_match_existing_balance_shape(self):
        # 与试算平衡表期初/期末余额同构的 {"account_code", "debit", "credit"} 形式。
        payload = {
            "entities": [
                entity("P", balances=[
                    pair("1001", debit="500.00"),
                    pair("3001", credit="500.00"),
                    pair("1009"),  # 两侧均为零：不参与汇总，也不要求映射
                ])
            ],
            "account_mapping": {"P": {"1001": "CASH", "3001": "EQUITY"}},
        }
        result = consolidate_group_trial_balance(payload)
        rows = balances_by_code(result)
        self.assertEqual(set(rows), {"CASH", "EQUITY"})
        self.assertEqual(rows["CASH"]["debit"], "500.00")
        self.assertEqual(rows["EQUITY"]["credit"], "500.00")

    def test_entity_accounts_merge_into_unified_group_account(self):
        payload = {
            "entities": [
                entity("P", balances=[
                    side("1001", "debit", "300.00"),
                    side("3001", "credit", "300.00"),
                ]),
                entity("S", balances=[
                    side("101", "debit", "200.00"),
                    side("301", "credit", "200.00"),
                ]),
            ],
            "account_mapping": {
                "P": {"1001": "CASH", "3001": "EQUITY"},
                "S": {"101": "CASH", "301": "EQUITY"},
            },
        }
        result = consolidate_group_trial_balance(payload)
        rows = balances_by_code(result)
        self.assertEqual(rows["CASH"]["debit"], "500.00")
        self.assertEqual(rows["EQUITY"]["credit"], "500.00")


class EliminationTest(unittest.TestCase):
    def payload(self, amount_a="100.00", amount_b="100.00", tolerance="0",
                category="receivable_payable"):
        return {
            "entities": [
                entity("P", balances=[
                    side("1122", "debit", "100.00"),
                    side("1001", "debit", "900.00"),
                    side("3001", "credit", "1000.00"),
                ]),
                entity("S", balances=[
                    side("2202", "credit", "100.00"),
                    side("1001", "debit", "700.00"),
                    side("3001", "credit", "600.00"),
                ]),
            ],
            "account_mapping": {
                "P": {"1122": "AR", "1001": "CASH", "3001": "EQUITY"},
                "S": {"2202": "AP", "1001": "CASH", "3001": "EQUITY"},
            },
            "intercompany_transactions": [
                txn("P", "1122", amount_a, "S", "2202", amount_b,
                    category=category, reference="INV-1")
            ],
            "tolerance": tolerance,
        }

    def test_equal_amounts_eliminate_in_full(self):
        result = consolidate_group_trial_balance(self.payload())
        rows = balances_by_code(result)
        self.assertEqual(rows["AR"], {"group_account_code": "AR",
                                      "debit": "0.00", "credit": "0.00"})
        self.assertEqual(rows["AP"], {"group_account_code": "AP",
                                      "debit": "0.00", "credit": "0.00"})
        self.assertEqual(rows["CASH"]["debit"], "1600.00")
        self.assertEqual(rows["EQUITY"]["credit"], "1600.00")
        self.assertEqual(result["total_debit"], "1600.00")
        self.assertEqual(result["total_credit"], "1600.00")
        self.assertEqual(result["reconciliation_differences"], [])
        self.assertEqual(result["unmatched_items"], [])

        [elimination] = result["eliminations"]
        self.assertEqual(elimination["elimination_id"], "elim-1")
        self.assertEqual(elimination["category"], "receivable_payable")
        self.assertEqual(elimination["reference"], "INV-1")
        self.assertEqual(elimination["amount"], "100.00")
        line_p, line_s = elimination["lines"]
        # 方向取各方余额的反方向：P 的应收为借方余额（贷记），S 的应付为贷方余额（借记）。
        self.assertEqual(line_p, {"entity_id": "P", "account_code": "1122",
                                  "group_account_code": "AR", "side": "credit",
                                  "amount": "100.00"})
        self.assertEqual(line_s, {"entity_id": "S", "account_code": "2202",
                                  "group_account_code": "AP", "side": "debit",
                                  "amount": "100.00"})

    def test_difference_within_tolerance_eliminates_smaller_amount(self):
        result = consolidate_group_trial_balance(
            self.payload(amount_a="100.00", amount_b="100.05", tolerance="0.10")
        )
        rows = balances_by_code(result)
        # 以较小金额抵消：双方余额各减 100.00，差额只写入对账差异。
        self.assertEqual(rows["AR"]["debit"], "0.00")
        self.assertEqual(rows["AP"]["credit"], "0.00")
        [difference] = result["reconciliation_differences"]
        self.assertEqual(difference["elimination_id"], "elim-1")
        self.assertEqual(difference["reference"], "INV-1")
        self.assertEqual(difference["entity_a"], "P")
        self.assertEqual(difference["entity_b"], "S")
        self.assertEqual(difference["amount_a"], "100.00")
        self.assertEqual(difference["amount_b"], "100.05")
        self.assertEqual(difference["difference"], "0.05")
        self.assertEqual(difference["eliminated_amount"], "100.00")
        self.assertEqual(result["unmatched_items"], [])
        self.assertEqual(result["total_debit"], result["total_credit"])

    def test_difference_beyond_tolerance_keeps_balances_and_unmatched(self):
        result = consolidate_group_trial_balance(
            self.payload(amount_a="100.00", amount_b="100.20", tolerance="0.10")
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["AR"]["debit"], "100.00")
        self.assertEqual(rows["AP"]["credit"], "100.00")
        self.assertEqual(result["eliminations"], [])
        self.assertEqual(result["reconciliation_differences"], [])
        [unmatched] = result["unmatched_items"]
        self.assertEqual(unmatched["entity_a"], "P")
        self.assertEqual(unmatched["account_a"], "1122")
        self.assertEqual(unmatched["entity_b"], "S")
        self.assertEqual(unmatched["account_b"], "2202")
        self.assertEqual(unmatched["difference"], "0.20")
        self.assertEqual(unmatched["reference"], "INV-1")

    def test_revenue_cost_and_dividend_categories_eliminate(self):
        for category in ("revenue_cost", "dividend"):
            result = consolidate_group_trial_balance(self.payload(category=category))
            rows = balances_by_code(result)
            self.assertEqual(rows["AR"]["debit"], "0.00")
            self.assertEqual(rows["AP"]["credit"], "0.00")
            [elimination] = result["eliminations"]
            self.assertEqual(elimination["category"], category)

    def test_eliminations_sorted_by_entity_account_reference(self):
        payload = self.payload()
        payload["intercompany_transactions"] = [
            txn("S", "2202", "10.00", "P", "1122", "10.00", reference="B-2"),
            txn("P", "1122", "20.00", "S", "2202", "20.00", reference="A-1"),
        ]
        result = consolidate_group_trial_balance(payload)
        references = [entry["reference"] for entry in result["eliminations"]]
        self.assertEqual(references, ["A-1", "B-2"])
        ids = [entry["elimination_id"] for entry in result["eliminations"]]
        self.assertEqual(ids, ["elim-1", "elim-2"])

    def test_totals_stay_balanced_with_mixed_outcomes(self):
        payload = self.payload()
        payload["tolerance"] = "0.10"
        payload["intercompany_transactions"] = [
            txn("P", "1122", "40.00", "S", "2202", "40.00", reference="F-1"),
            txn("P", "1122", "30.00", "S", "2202", "30.05", reference="F-2"),
            txn("P", "1122", "30.00", "S", "2202", "30.50", reference="F-3"),
        ]
        result = consolidate_group_trial_balance(payload)
        self.assertEqual(len(result["eliminations"]), 2)
        self.assertEqual(len(result["reconciliation_differences"]), 1)
        self.assertEqual(len(result["unmatched_items"]), 1)
        self.assertEqual(result["total_debit"], result["total_credit"])


class MinorityInterestTest(unittest.TestCase):
    def test_minority_interest_from_net_assets_and_unheld_ratio(self):
        payload = {
            "entities": [
                entity("P", balances=[
                    side("1001", "debit", "1000.00"),
                    side("3001", "credit", "1000.00"),
                ]),
                entity("S", ratio=Decimal("0.8"), balances=[
                    side("1001", "debit", "1000.00"),
                    side("2202", "credit", "400.00"),
                    side("3001", "credit", "600.00"),
                ]),
            ],
            "account_mapping": {
                "P": {"1001": "CASH", "3001": "EQUITY"},
                "S": {
                    "1001": {"group_account_code": "CASH", "category": "asset"},
                    "2202": {"group_account_code": "AP", "category": "liability"},
                    "3001": {"group_account_code": "EQUITY", "category": "equity"},
                },
            },
        }
        result = consolidate_group_trial_balance(payload)
        [minority] = result["minority_interests"]
        self.assertEqual(minority["entity_id"], "S")
        self.assertEqual(minority["ownership_ratio"], "0.8")
        self.assertEqual(minority["net_assets"], "600.00")
        self.assertEqual(minority["minority_ratio"], "0.2")
        self.assertEqual(minority["minority_interest"], "120.00")

    def test_wholly_owned_entity_produces_no_minority_interest(self):
        payload = {
            "entities": [
                entity("S", ratio="1", balances=[
                    side("1001", "debit", "10.00"),
                    side("3001", "credit", "10.00"),
                ]),
            ],
            "account_mapping": {"S": {"1001": "CASH", "3001": "EQUITY"}},
        }
        result = consolidate_group_trial_balance(payload)
        self.assertEqual(result["minority_interests"], [])


class ValidationErrorTest(unittest.TestCase):
    def base_payload(self):
        return {
            "entities": [
                entity("P", balances=[
                    side("1001", "debit", "100.00"),
                    side("3001", "credit", "100.00"),
                ]),
                entity("S", balances=[
                    side("1001", "debit", "50.00"),
                    side("3001", "credit", "50.00"),
                ]),
            ],
            "account_mapping": {
                "P": {"1001": "CASH", "3001": "EQUITY"},
                "S": {"1001": "CASH", "3001": "EQUITY"},
            },
        }

    def test_period_mismatch(self):
        payload = self.base_payload()
        payload["entities"][1]["period"] = "2026-10"
        with self.assertRaises(PeriodMismatchError):
            consolidate_group_trial_balance(payload)

    def test_duplicate_entity_id(self):
        payload = self.base_payload()
        payload["entities"][1]["entity_id"] = "P"
        with self.assertRaises(DuplicateEntityError):
            consolidate_group_trial_balance(payload)

    def test_unbalanced_entity(self):
        payload = self.base_payload()
        payload["entities"][0]["balances"] = [side("1001", "debit", "100.00")]
        with self.assertRaises(UnbalancedEntityError):
            consolidate_group_trial_balance(payload)

    def test_missing_account_mapping(self):
        payload = self.base_payload()
        del payload["account_mapping"]["S"]["1001"]
        with self.assertRaises(AccountMappingError):
            consolidate_group_trial_balance(payload)

    def test_declaration_account_requires_mapping(self):
        payload = self.base_payload()
        payload["intercompany_transactions"] = [
            txn("P", "1001", "10.00", "S", "9999", "10.00")
        ]
        with self.assertRaises(AccountMappingError):
            consolidate_group_trial_balance(payload)

    def test_ownership_ratio_out_of_range(self):
        for ratio in ("-0.1", "1.5"):
            payload = self.base_payload()
            payload["entities"][1]["ownership_ratio"] = ratio
            with self.assertRaises(InvalidConsolidationInputError):
                consolidate_group_trial_balance(payload)

    def test_negative_tolerance(self):
        payload = self.base_payload()
        payload["tolerance"] = "-0.01"
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(payload)

    def test_unknown_entity_in_declaration(self):
        payload = self.base_payload()
        payload["intercompany_transactions"] = [
            txn("P", "1001", "10.00", "X", "1001", "10.00")
        ]
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(payload)

    def test_invalid_category_and_unknown_fields(self):
        payload = self.base_payload()
        payload["intercompany_transactions"] = [
            txn("P", "1001", "10.00", "S", "1001", "10.00", category="loan")
        ]
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(payload)
        payload = self.base_payload()
        payload["unexpected"] = True
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(payload)
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance("not a dict")


class DeterminismAndPrecisionTest(unittest.TestCase):
    def payload(self):
        return {
            "entities": [
                entity("P", balances=[
                    side("1122", "debit", "100.00"),
                    side("1001", "debit", "900.00"),
                    side("3001", "credit", "1000.00"),
                ]),
                entity("S", ratio="0.75", balances=[
                    side("2202", "credit", "100.00"),
                    side("1001", "debit", "700.00"),
                    side("3001", "credit", "600.00"),
                ]),
            ],
            "account_mapping": {
                "P": {"1122": "AR", "1001": "CASH", "3001": "EQUITY"},
                "S": {
                    "2202": {"group_account_code": "AP", "category": "liability"},
                    "1001": {"group_account_code": "CASH", "category": "asset"},
                    "3001": {"group_account_code": "EQUITY", "category": "equity"},
                },
            },
            "intercompany_transactions": [
                txn("P", "1122", "100.00", "S", "2202", "100.00", reference="INV-1")
            ],
        }

    def test_repeated_calls_do_not_accumulate_state(self):
        payload = self.payload()
        first = consolidate_group_trial_balance(payload)
        second = consolidate_group_trial_balance(payload)
        self.assertEqual(first, second)

    def test_input_is_not_mutated(self):
        payload = self.payload()
        snapshot = copy.deepcopy(payload)
        consolidate_group_trial_balance(payload)
        self.assertEqual(payload, snapshot)

    def test_decimal_precision_no_binary_float(self):
        payload = {
            "entities": [
                entity("P", balances=[
                    side("1001", "debit", "0.1"),
                    side("1002", "debit", "0.2"),
                    side("3001", "credit", "0.3"),
                ]),
            ],
            "account_mapping": {
                "P": {"1001": "CASH", "1002": "CASH", "3001": "EQUITY"}
            },
        }
        result = consolidate_group_trial_balance(payload)
        rows = balances_by_code(result)
        self.assertEqual(rows["CASH"]["debit"], "0.30")
        self.assertEqual(rows["EQUITY"]["credit"], "0.30")
        self.assertEqual(result["total_debit"], "0.30")
        self.assertEqual(result["total_credit"], "0.30")

    def test_result_is_json_serializable(self):
        json.dumps(consolidate_group_trial_balance(self.payload()))


if __name__ == "__main__":
    unittest.main()
