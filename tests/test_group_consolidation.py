import unittest
from decimal import Decimal

import ledgerlens
from ledgerlens import (
    AccountBalance,
    AccountMappingError,
    DuplicateEntityError,
    EntityBalances,
    InvalidConsolidationInputError,
    PeriodMismatchError,
    UnbalancedEntityError,
    consolidate_group_trial_balance,
)
from ledgerlens.service import Service

D = Decimal


def entity(entity_id, period, balances):
    return {"entity_id": entity_id, "period": period, "balances": balances}


def balance(code, direction, amount):
    return {"account_code": code, "direction": direction, "amount": D(amount)}


def pair(entity_a, account_a, amount_a, entity_b, account_b, amount_b,
         category="receivable_payable", reference=None):
    result = {
        "entity_a": entity_a,
        "account_a": account_a,
        "amount_a": D(amount_a),
        "entity_b": entity_b,
        "account_b": account_b,
        "amount_b": D(amount_b),
        "category": category,
    }
    if reference is not None:
        result["reference"] = reference
    return result


def balances_by_code(result):
    return {row.account_code: row for row in result.group_balances}


class PublicEntryTest(unittest.TestCase):
    def test_top_level_package_exposes_public_entry(self) -> None:
        self.assertIn("consolidate_group_trial_balance", ledgerlens.__all__)
        self.assertIs(ledgerlens.consolidate_group_trial_balance,
                      consolidate_group_trial_balance)

    def test_service_delegates_to_public_entry(self) -> None:
        service = Service()
        result = service.consolidate_group_trial_balance(
            entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "10"),
                                               balance("3001", "credit", "10")])],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
        )
        self.assertEqual(result.total_debit, D("10"))
        self.assertEqual(result.total_credit, D("10"))


class EmptyAndSingleEntityTest(unittest.TestCase):
    def test_empty_entities_returns_zero_result(self) -> None:
        result = consolidate_group_trial_balance()
        self.assertIsNone(result.period)
        self.assertEqual(result.group_balances, ())
        self.assertEqual(result.total_debit, D("0"))
        self.assertEqual(result.total_credit, D("0"))
        self.assertEqual(result.elimination_entries, ())
        self.assertEqual(result.reconciliation_differences, ())
        self.assertEqual(result.unmatched_items, ())
        self.assertEqual(result.minority_interests, ())
        self.assertEqual(result.minority_interest_total, D("0"))

    def test_single_wholly_owned_entity_matches_mapped_balances(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[entity("E1", "2026-Q1", [
                balance("1001", "debit", "1000.00"),
                balance("4001", "credit", "250.50"),
                balance("3001", "credit", "749.50"),
            ])],
            account_mapping={"1001": "G-CASH", "4001": "G-REV", "3001": "G-EQUITY"},
            ownership={"E1": D("1")},
        )
        self.assertEqual(result.period, "2026-Q1")
        rows = balances_by_code(result)
        self.assertEqual(
            {code: (row.debit, row.credit) for code, row in rows.items()},
            {
                "G-CASH": (D("1000.00"), D("0")),
                "G-REV": (D("0"), D("250.50")),
                "G-EQUITY": (D("0"), D("749.50")),
            },
        )
        self.assertEqual(result.total_debit, D("1000.00"))
        self.assertEqual(result.total_credit, D("1000.00"))
        self.assertEqual(result.elimination_entries, ())
        self.assertEqual(result.minority_interests, ())
        self.assertEqual(result.minority_interest_total, D("0"))

    def test_accounts_merged_into_same_group_account(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[
                entity("E1", "2026-Q1", [balance("1001", "debit", "100"),
                                         balance("3001", "credit", "100")]),
                entity("E2", "2026-Q1", [balance("1010", "debit", "50"),
                                         balance("3010", "credit", "50")]),
            ],
            account_mapping={
                "1001": "G-CASH", "1010": "G-CASH",
                "3001": "G-EQUITY", "3010": "G-EQUITY",
            },
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["G-CASH"].debit, D("150"))
        self.assertEqual(rows["G-EQUITY"].credit, D("150"))

    def test_accepts_trial_balance_style_rows(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[entity("E1", "2026-Q1", [
                {"code": "1001", "ending_debit": "1000.00", "ending_credit": "0.00"},
                {"code": "3001", "ending_debit": "0.00", "ending_credit": "1000.00"},
                {"code": "5002", "ending_debit": "0.00", "ending_credit": "0.00"},
            ])],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY", "5002": "G-EXP"},
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["G-CASH"].debit, D("1000.00"))
        self.assertEqual(rows["G-EQUITY"].credit, D("1000.00"))
        self.assertNotIn("G-EXP", rows)

    def test_accepts_dataclass_inputs(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[EntityBalances("E1", "2026-Q1", (
                AccountBalance("1001", "debit", D("10")),
                AccountBalance("3001", "credit", D("10")),
            ))],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
        )
        self.assertEqual(result.total_debit, D("10"))


class EliminationTest(unittest.TestCase):
    def entities(self):
        return [
            entity("P", "2026-Q1", [
                balance("1101", "debit", "500"),    # 内部应收
                balance("1001", "debit", "1500"),
                balance("3001", "credit", "2000"),
            ]),
            entity("S", "2026-Q1", [
                balance("1001", "debit", "500"),
                balance("2101", "credit", "500"),   # 内部应付
            ]),
        ]

    def mapping(self):
        return {
            "1001": "G-CASH",
            "1101": "G-AR",
            "2101": "G-AP",
            "3001": "G-EQUITY",
        }

    def test_equal_amounts_fully_eliminated(self) -> None:
        result = consolidate_group_trial_balance(
            entities=self.entities(),
            account_mapping=self.mapping(),
            intercompany_pairs=[pair("P", "1101", "500", "S", "2101", "500",
                                     reference="IC-1")],
        )
        rows = balances_by_code(result)
        self.assertEqual((rows["G-AR"].debit, rows["G-AR"].credit), (D("0"), D("0")))
        self.assertEqual((rows["G-AP"].debit, rows["G-AP"].credit), (D("0"), D("0")))
        self.assertEqual(rows["G-CASH"].debit, D("2000"))
        self.assertEqual(rows["G-EQUITY"].credit, D("2000"))
        self.assertEqual(result.total_debit, D("2000"))
        self.assertEqual(result.total_credit, D("2000"))
        self.assertEqual(result.reconciliation_differences, ())
        self.assertEqual(result.unmatched_items, ())

        self.assertEqual(len(result.elimination_entries), 1)
        entry = result.elimination_entries[0]
        self.assertEqual(entry.category, "receivable_payable")
        self.assertEqual(entry.reference, "IC-1")
        self.assertEqual(entry.amount, D("500"))
        self.assertEqual(
            [(line.entity_id, line.account_code, line.group_account_code,
              line.direction, line.amount) for line in entry.lines],
            [("P", "1101", "G-AR", "credit", D("500")),
             ("S", "2101", "G-AP", "debit", D("500"))],
        )

    def test_within_tolerance_eliminates_smaller_and_records_difference(self) -> None:
        result = consolidate_group_trial_balance(
            entities=self.entities(),
            account_mapping=self.mapping(),
            intercompany_pairs=[pair("P", "1101", "500", "S", "2101", "495",
                                     reference="IC-2")],
            tolerance=D("10"),
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["G-AR"].debit, D("5"))
        self.assertEqual(rows["G-AP"].credit, D("5"))
        self.assertEqual(result.unmatched_items, ())
        self.assertEqual(len(result.elimination_entries), 1)
        self.assertEqual(result.elimination_entries[0].amount, D("495"))

        self.assertEqual(len(result.reconciliation_differences), 1)
        difference = result.reconciliation_differences[0]
        self.assertEqual(difference.reference, "IC-2")
        self.assertEqual(difference.eliminated_amount, D("495"))
        self.assertEqual(difference.difference, D("5"))
        self.assertEqual(
            [(side.entity_id, side.account_code, side.amount) for side in difference.sides],
            [("P", "1101", D("500")), ("S", "2101", D("495"))],
        )

    def test_difference_equal_to_tolerance_is_eliminated(self) -> None:
        result = consolidate_group_trial_balance(
            entities=self.entities(),
            account_mapping=self.mapping(),
            intercompany_pairs=[pair("P", "1101", "500", "S", "2101", "490")],
            tolerance=D("10"),
        )
        self.assertEqual(result.unmatched_items, ())
        self.assertEqual(result.elimination_entries[0].amount, D("490"))
        self.assertEqual(result.reconciliation_differences[0].difference, D("10"))

    def test_beyond_tolerance_keeps_balances_and_lists_unmatched(self) -> None:
        result = consolidate_group_trial_balance(
            entities=self.entities(),
            account_mapping=self.mapping(),
            intercompany_pairs=[pair("P", "1101", "500", "S", "2101", "400",
                                     reference="IC-3")],
            tolerance=D("10"),
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["G-AR"].debit, D("500"))
        self.assertEqual(rows["G-AP"].credit, D("500"))
        self.assertEqual(result.elimination_entries, ())
        self.assertEqual(result.reconciliation_differences, ())
        self.assertEqual(len(result.unmatched_items), 1)
        item = result.unmatched_items[0]
        self.assertEqual(item.reference, "IC-3")
        self.assertEqual(item.difference, D("100"))
        self.assertEqual(item.reason, "amount_difference_exceeds_tolerance")

    def test_revenue_cost_and_dividend_categories(self) -> None:
        entities = [
            entity("P", "2026-Q1", [
                balance("1001", "debit", "1600"),
                balance("4001", "credit", "600"),   # 内部收入
                balance("4101", "credit", "300"),   # 股利收入
                balance("3001", "credit", "700"),
            ]),
            entity("S", "2026-Q1", [
                balance("1001", "debit", "1300"),
                balance("5001", "debit", "600"),    # 内部成本
                balance("3201", "debit", "300"),    # 已宣告股利
                balance("3001", "credit", "2200"),
            ]),
        ]
        mapping = {
            "1001": "G-CASH", "4001": "G-REV", "4101": "G-DIV-INC",
            "5001": "G-COST", "3201": "G-DIV-DECL", "3001": "G-EQUITY",
        }
        result = consolidate_group_trial_balance(
            entities=entities,
            account_mapping=mapping,
            intercompany_pairs=[
                pair("P", "4001", "600", "S", "5001", "600",
                     category="revenue_cost", reference="SALE-1"),
                pair("P", "4101", "300", "S", "3201", "300",
                     category="dividend", reference="DIV-1"),
            ],
        )
        rows = balances_by_code(result)
        self.assertEqual((rows["G-REV"].debit, rows["G-REV"].credit), (D("0"), D("0")))
        self.assertEqual((rows["G-COST"].debit, rows["G-COST"].credit), (D("0"), D("0")))
        self.assertEqual((rows["G-DIV-INC"].debit, rows["G-DIV-INC"].credit), (D("0"), D("0")))
        self.assertEqual((rows["G-DIV-DECL"].debit, rows["G-DIV-DECL"].credit), (D("0"), D("0")))
        self.assertEqual(result.total_debit, result.total_credit)
        categories = [entry.category for entry in result.elimination_entries]
        self.assertEqual(sorted(categories), ["dividend", "revenue_cost"])

    def test_elimination_entries_sorted_by_entity_account_reference(self) -> None:
        entities = [
            entity("A", "2026-Q1", [
                balance("1101", "debit", "100"),
                balance("1102", "debit", "200"),
                balance("3001", "credit", "300"),
            ]),
            entity("B", "2026-Q1", [
                balance("2101", "credit", "100"),
                balance("2102", "credit", "200"),
                balance("1001", "debit", "300"),
            ]),
        ]
        mapping = {
            "1101": "G-AR1", "1102": "G-AR2", "2101": "G-AP1",
            "2102": "G-AP2", "3001": "G-EQUITY", "1001": "G-CASH",
        }
        result = consolidate_group_trial_balance(
            entities=entities,
            account_mapping=mapping,
            intercompany_pairs=[
                pair("A", "1102", "200", "B", "2102", "200", reference="R-2"),
                pair("A", "1101", "100", "B", "2101", "100", reference="R-1"),
            ],
        )
        self.assertEqual(
            [entry.reference for entry in result.elimination_entries],
            ["R-1", "R-2"],
        )


class MinorityInterestTest(unittest.TestCase):
    def test_minority_interest_listed_separately(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[
                entity("P", "2026-Q1", [balance("1001", "debit", "5000"),
                                        balance("3001", "credit", "5000")]),
                entity("S", "2026-Q1", [balance("1001", "debit", "1000"),
                                        balance("3001", "credit", "1000")]),
            ],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
            ownership={"P": D("1"), "S": D("0.8")},
        )
        # 少数股东权益单列，不并入集团科目余额。
        rows = balances_by_code(result)
        self.assertEqual(rows["G-CASH"].debit, D("6000"))
        self.assertEqual(rows["G-EQUITY"].credit, D("6000"))
        self.assertEqual(len(result.minority_interests), 1)
        minority = result.minority_interests[0]
        self.assertEqual(minority.entity_id, "S")
        self.assertEqual(minority.ownership_ratio, D("0.8"))
        self.assertEqual(minority.net_assets, D("1000"))
        self.assertEqual(minority.minority_interest, D("200"))
        self.assertEqual(result.minority_interest_total, D("200"))

    def test_missing_ownership_defaults_to_wholly_owned(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "10"),
                                               balance("3001", "credit", "10")])],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
        )
        self.assertEqual(result.minority_interests, ())
        self.assertEqual(result.minority_interest_total, D("0"))

    def test_minority_interest_keeps_decimal_precision(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[entity("S", "2026-Q1", [balance("1001", "debit", "0.03"),
                                              balance("3001", "credit", "0.03")])],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
            ownership={"S": D("0.67")},
        )
        self.assertEqual(result.minority_interest_total, D("0.0099"))


class ValidationTest(unittest.TestCase):
    def mapping(self):
        return {"1001": "G-CASH", "3001": "G-EQUITY"}

    def test_period_mismatch_raises(self) -> None:
        with self.assertRaises(PeriodMismatchError):
            consolidate_group_trial_balance(
                entities=[
                    entity("E1", "2026-Q1", []),
                    entity("E2", "2026-Q2", []),
                ],
                account_mapping=self.mapping(),
            )

    def test_duplicate_entity_id_raises(self) -> None:
        with self.assertRaises(DuplicateEntityError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", []), entity("E1", "2026-Q1", [])],
                account_mapping=self.mapping(),
            )

    def test_unbalanced_entity_raises(self) -> None:
        with self.assertRaises(UnbalancedEntityError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "100"),
                                                   balance("3001", "credit", "90")])],
                account_mapping=self.mapping(),
            )

    def test_missing_mapping_raises(self) -> None:
        with self.assertRaises(AccountMappingError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "100"),
                                                   balance("3001", "credit", "100")])],
                account_mapping={"1001": "G-CASH"},
            )

    def test_ownership_out_of_range_raises(self) -> None:
        for ratio in (D("1.01"), D("-0.1"), D("2")):
            with self.assertRaises(InvalidConsolidationInputError):
                consolidate_group_trial_balance(
                    entities=[entity("E1", "2026-Q1", [])],
                    account_mapping=self.mapping(),
                    ownership={"E1": ratio},
                )

    def test_negative_tolerance_raises(self) -> None:
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(
                entities=[], account_mapping=self.mapping(), tolerance=D("-0.01")
            )

    def test_unknown_category_raises(self) -> None:
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "10"),
                                                   balance("3001", "credit", "10")])],
                account_mapping=self.mapping(),
                intercompany_pairs=[pair("E1", "1001", "10", "E1", "3001", "10",
                                         category="magic")],
            )

    def test_unknown_entity_in_pair_raises(self) -> None:
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "10"),
                                                   balance("3001", "credit", "10")])],
                account_mapping=self.mapping(),
                intercompany_pairs=[pair("E1", "1001", "10", "E9", "1001", "10")],
            )

    def test_declared_account_without_balance_raises(self) -> None:
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [balance("1001", "debit", "10"),
                                                   balance("3001", "credit", "10")])],
                account_mapping=self.mapping(),
                intercompany_pairs=[pair("E1", "1001", "10", "E1", "9999", "10")],
            )

    def test_invalid_direction_raises(self) -> None:
        with self.assertRaises(InvalidConsolidationInputError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", [
                    {"account_code": "1001", "direction": "sideways", "amount": D("10")},
                ])],
                account_mapping=self.mapping(),
            )

    def test_exceptions_are_catchable_via_base_class(self) -> None:
        with self.assertRaises(ledgerlens.ConsolidationError):
            consolidate_group_trial_balance(
                entities=[entity("E1", "2026-Q1", []), entity("E1", "2026-Q1", [])],
                account_mapping=self.mapping(),
            )


class StatelessTest(unittest.TestCase):
    def test_repeated_calls_return_equal_results_without_accumulation(self) -> None:
        kwargs = {
            "entities": [
                entity("P", "2026-Q1", [balance("1101", "debit", "500"),
                                        balance("1001", "debit", "500"),
                                        balance("3001", "credit", "1000")]),
                entity("S", "2026-Q1", [balance("1001", "debit", "500"),
                                        balance("2101", "credit", "500")]),
            ],
            "account_mapping": {
                "1001": "G-CASH", "1101": "G-AR", "2101": "G-AP", "3001": "G-EQUITY",
            },
            "ownership": {"S": D("0.75")},
            "intercompany_pairs": [pair("P", "1101", "500", "S", "2101", "500")],
        }
        first = consolidate_group_trial_balance(**kwargs)
        second = consolidate_group_trial_balance(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(second.elimination_entries), 1)
        self.assertEqual(second.total_debit, D("1000"))
        self.assertEqual(second.minority_interest_total, D("125"))

    def test_decimal_precision_without_binary_float(self) -> None:
        result = consolidate_group_trial_balance(
            entities=[
                entity("E1", "2026-Q1", [balance("1001", "debit", "0.1"),
                                         balance("3001", "credit", "0.1")]),
                entity("E2", "2026-Q1", [balance("1001", "debit", "0.2"),
                                         balance("3001", "credit", "0.2")]),
            ],
            account_mapping={"1001": "G-CASH", "3001": "G-EQUITY"},
        )
        rows = balances_by_code(result)
        self.assertEqual(rows["G-CASH"].debit, D("0.3"))
        self.assertEqual(result.total_debit, D("0.3"))
        self.assertIsInstance(result.total_debit, Decimal)


if __name__ == "__main__":
    unittest.main()
