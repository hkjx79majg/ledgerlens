import copy
import json
import unittest
from decimal import Decimal

from ledgerlens.deferred_tax import (
    DeferredTaxError,
    calculate_deferred_tax,
)
from ledgerlens.service import Service


def _rate(**over):
    bracket = {"effective_from": "2025-01-01", "effective_to": "2026-12-31", "rate": "0.25"}
    bracket.update(over)
    return bracket


def _item(**over):
    item = {
        "item_id": "i",
        "nature": "asset",
        "carrying_amount": "100.00",
        "tax_base": "100.00",
        "expected_reversal_date": "2026-06-30",
        "attribution": "profit_or_loss",
    }
    item.update(over)
    return item


def _payload(**over):
    payload = {
        "report_date": "2026-06-30",
        "tax_rates": [_rate()],
        "items": [],
    }
    payload.update(over)
    return payload


class DeferredTaxCalculationTest(unittest.TestCase):
    def assert_error(self, payload, code, path):
        with self.assertRaises(ValueError):
            calculate_deferred_tax(payload)
        with self.assertRaises(DeferredTaxError) as caught:
            calculate_deferred_tax(payload)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.path, path)

    def test_empty_items_returns_all_zero_totals(self) -> None:
        result = calculate_deferred_tax(_payload())
        self.assertEqual(result["report_date"], "2026-06-30")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_deferred_tax_asset"], "0.00")
        self.assertEqual(result["total_deferred_tax_liability"], "0.00")
        self.assertEqual(result["net_deferred_tax"], "0.00")
        for attribution in ("profit_or_loss", "oci", "equity"):
            bucket = result["by_attribution"][attribution]
            self.assertEqual(bucket["deferred_tax_asset"], "0.00")
            self.assertEqual(bucket["deferred_tax_liability"], "0.00")
            self.assertEqual(bucket["net_deferred_tax"], "0.00")

    def test_asset_taxable_difference_creates_liability(self) -> None:
        payload = _payload(items=[
            _item(item_id="a1", carrying_amount="1200.00", tax_base="1000.00")
        ])
        result = calculate_deferred_tax(payload)
        row = result["items"][0]
        self.assertEqual(row["temporary_difference"], "200.00")
        self.assertEqual(row["tax_rate"], "0.25")
        self.assertEqual(row["recognized_deductible_difference"], "0.00")
        self.assertEqual(row["unrecognized_deductible_difference"], "0.00")
        self.assertEqual(row["deferred_tax_asset"], "0.00")
        self.assertEqual(row["deferred_tax_liability"], "50.00")
        self.assertEqual(row["attribution"], "profit_or_loss")
        self.assertEqual(result["total_deferred_tax_liability"], "50.00")
        self.assertEqual(result["total_deferred_tax_asset"], "0.00")
        self.assertEqual(result["net_deferred_tax"], "-50.00")
        self.assertEqual(
            result["by_attribution"]["profit_or_loss"]["deferred_tax_liability"],
            "50.00",
        )

    def test_liability_deductible_difference_creates_asset_within_cap(self) -> None:
        payload = _payload(items=[
            _item(
                item_id="l1",
                nature="liability",
                carrying_amount="100.00",
                tax_base="0.00",
                attribution="oci",
                deductible_recoverable_cap="80.00",
            )
        ])
        result = calculate_deferred_tax(payload)
        row = result["items"][0]
        self.assertEqual(row["temporary_difference"], "-100.00")
        self.assertEqual(row["recognized_deductible_difference"], "80.00")
        self.assertEqual(row["unrecognized_deductible_difference"], "20.00")
        self.assertEqual(row["deferred_tax_asset"], "20.00")
        self.assertEqual(row["deferred_tax_liability"], "0.00")
        self.assertEqual(result["total_deferred_tax_asset"], "20.00")
        self.assertEqual(result["net_deferred_tax"], "20.00")
        self.assertEqual(
            result["by_attribution"]["oci"]["deferred_tax_asset"], "20.00"
        )

    def test_zero_cap_recognizes_nothing(self) -> None:
        payload = _payload(items=[
            _item(
                item_id="l2",
                nature="liability",
                carrying_amount="50",
                tax_base="0",
                attribution="equity",
                deductible_recoverable_cap="0",
            )
        ])
        row = calculate_deferred_tax(payload)["items"][0]
        self.assertEqual(row["recognized_deductible_difference"], "0.00")
        self.assertEqual(row["unrecognized_deductible_difference"], "50.00")
        self.assertEqual(row["deferred_tax_asset"], "0.00")

    def test_rate_chosen_from_bracket_covering_reversal_date(self) -> None:
        payload = _payload(
            tax_rates=[
                _rate(effective_from="2025-01-01", effective_to="2025-12-31", rate="0.25"),
                _rate(effective_from="2026-01-01", effective_to="2026-12-31", rate="0.20"),
                _rate(effective_from="2027-01-01", effective_to="2027-12-31", rate="0.15"),
            ],
            items=[
                _item(item_id="a", carrying_amount="200.00", tax_base="100.00",
                      expected_reversal_date="2025-01-01"),
                _item(item_id="b", carrying_amount="200.00", tax_base="100.00",
                      expected_reversal_date="2026-12-31"),
                _item(item_id="c", carrying_amount="200.00", tax_base="100.00",
                      expected_reversal_date="2027-07-01"),
            ],
        )
        rows = calculate_deferred_tax(payload)["items"]
        self.assertEqual([row["tax_rate"] for row in rows], ["0.25", "0.20", "0.15"])
        self.assertEqual(
            [row["deferred_tax_liability"] for row in rows],
            ["25.00", "20.00", "15.00"],
        )

    def test_item_order_preserved_and_totals_from_rounded_details(self) -> None:
        payload = _payload(
            tax_rates=[
                _rate(effective_from="2026-01-01", effective_to="2026-12-31", rate="0.10"),
            ],
            items=[
                _item(item_id="first", carrying_amount="100.005", tax_base="0.00"),
                _item(item_id="second", nature="liability", carrying_amount="0.33",
                      tax_base="0.00", attribution="equity",
                      deductible_recoverable_cap="0.33"),
                _item(item_id="third", carrying_amount="0.00", tax_base="5.00",
                      attribution="oci", deductible_recoverable_cap="5.00"),
            ],
        )
        result = calculate_deferred_tax(payload)
        self.assertEqual(
            [row["item_id"] for row in result["items"]],
            ["first", "second", "third"],
        )
        rows = {row["item_id"]: row for row in result["items"]}
        # 100.005 四舍五入到分 -> 100.01；税额 10.001 -> 10.00。
        self.assertEqual(rows["first"]["temporary_difference"], "100.01")
        self.assertEqual(rows["first"]["deferred_tax_liability"], "10.00")
        self.assertEqual(rows["second"]["temporary_difference"], "-0.33")
        self.assertEqual(rows["second"]["deferred_tax_asset"], "0.03")
        self.assertEqual(rows["third"]["temporary_difference"], "-5.00")
        self.assertEqual(rows["third"]["deferred_tax_asset"], "0.50")
        # 汇总必须来自已舍入明细。
        self.assertEqual(result["total_deferred_tax_liability"], "10.00")
        self.assertEqual(result["total_deferred_tax_asset"], "0.53")
        self.assertEqual(result["net_deferred_tax"], "-9.47")
        pl = result["by_attribution"]["profit_or_loss"]
        self.assertEqual(pl["deferred_tax_liability"], "10.00")
        self.assertEqual(pl["net_deferred_tax"], "-10.00")
        self.assertEqual(
            result["by_attribution"]["equity"]["deferred_tax_asset"], "0.03"
        )
        self.assertEqual(
            result["by_attribution"]["oci"]["deferred_tax_asset"], "0.50"
        )

    def test_input_not_mutated_and_result_deterministic(self) -> None:
        payload = _payload(items=[
            _item(item_id="a", carrying_amount="1200", tax_base="1000"),
            _item(item_id="b", nature="liability", carrying_amount="100",
                  tax_base="0", attribution="equity",
                  deductible_recoverable_cap="40"),
        ])
        snapshot = copy.deepcopy(payload)
        first = calculate_deferred_tax(payload)
        self.assertEqual(payload, snapshot)
        second = calculate_deferred_tax(copy.deepcopy(payload))
        self.assertEqual(first, second)
        # 字段集合稳定，整体可 JSON 序列化。
        json.dumps(first, sort_keys=True)

    def test_integer_amounts_accepted(self) -> None:
        payload = _payload(items=[_item(carrying_amount=200, tax_base=100)])
        row = calculate_deferred_tax(payload)["items"][0]
        self.assertEqual(row["temporary_difference"], "100.00")
        self.assertEqual(row["deferred_tax_liability"], "25.00")

    def test_boundary_rates_zero_and_one(self) -> None:
        payload = _payload(
            tax_rates=[
                _rate(effective_from="2026-01-01", effective_to="2026-06-30", rate="0"),
                _rate(effective_from="2026-07-01", effective_to="2026-12-31", rate="1"),
            ],
            items=[
                _item(item_id="a", carrying_amount="10", tax_base="0",
                      expected_reversal_date="2026-03-01"),
                _item(item_id="b", carrying_amount="10", tax_base="0",
                      expected_reversal_date="2026-07-01"),
            ],
        )
        rows = calculate_deferred_tax(payload)["items"]
        self.assertEqual(rows[0]["deferred_tax_liability"], "0.00")
        self.assertEqual(rows[1]["deferred_tax_liability"], "10.00")

    # ---- 契约违反：统一 ValueError ----

    def test_duplicate_item_id(self) -> None:
        payload = _payload(items=[_item(item_id="x"), _item(item_id="x")])
        self.assert_error(payload, "duplicate_item_id", "/items/1/item_id")

    def test_invalid_nature_and_attribution(self) -> None:
        self.assert_error(
            _payload(items=[_item(nature="equity")]),
            "invalid_nature",
            "/items/0/nature",
        )
        self.assert_error(
            _payload(items=[_item(attribution="retained")]),
            "invalid_attribution",
            "/items/0/attribution",
        )

    def test_invalid_dates(self) -> None:
        self.assert_error(
            _payload(report_date="2026-02-30"), "invalid_date", "/report_date"
        )
        self.assert_error(
            _payload(tax_rates=[_rate(effective_from="2026-13-01")]),
            "invalid_date",
            "/tax_rates/0/effective_from",
        )
        self.assert_error(
            _payload(items=[_item(expected_reversal_date="not-a-date")]),
            "invalid_date",
            "/items/0/expected_reversal_date",
        )

    def test_missing_carrying_amount(self) -> None:
        item = _item()
        del item["carrying_amount"]
        self.assert_error(_payload(items=[item]), "required", "/items/0/carrying_amount")

    def test_amount_must_be_finite_lossless_decimal(self) -> None:
        for bad in ("1.5e2", "1/2", "", "NaN", "Infinity", "-Infinity", "+", " 10", 1.5, True):
            self.assert_error(
                _payload(items=[_item(carrying_amount=bad)]),
                "invalid_amount",
                "/items/0/carrying_amount",
            )

    def test_cap_validation(self) -> None:
        base = _item(nature="liability", carrying_amount="100", tax_base="0",
                     deductible_recoverable_cap="100")
        item = dict(base)
        item["deductible_recoverable_cap"] = "-0.01"
        self.assert_error(
            _payload(items=[item]),
            "recoverable_cap_below_zero",
            "/items/0/deductible_recoverable_cap",
        )
        item = dict(base)
        item["deductible_recoverable_cap"] = "100.01"
        self.assert_error(
            _payload(items=[item]),
            "recoverable_cap_exceeds_difference",
            "/items/0/deductible_recoverable_cap",
        )
        item = dict(base)
        del item["deductible_recoverable_cap"]
        self.assert_error(
            _payload(items=[item]),
            "required",
            "/items/0/deductible_recoverable_cap",
        )
        # 应纳税差异（正数）项目不得携带可收回上限。
        self.assert_error(
            _payload(items=[_item(deductible_recoverable_cap="0")]),
            "unknown_field",
            "/items/0/deductible_recoverable_cap",
        )

    def test_rate_matching_failures(self) -> None:
        self.assert_error(
            _payload(
                tax_rates=[_rate(effective_from="2027-01-01", effective_to="2027-12-31")],
                items=[_item()],
            ),
            "no_matching_tax_rate",
            "/items/0/expected_reversal_date",
        )
        # 闭区间端点相接即重叠；重叠后转回日匹配多个区间。
        self.assert_error(
            _payload(
                tax_rates=[
                    _rate(effective_from="2026-01-01", effective_to="2026-06-30"),
                    _rate(effective_from="2026-06-30", effective_to="2026-12-31"),
                ],
                items=[_item(expected_reversal_date="2026-06-30")],
            ),
            "tax_rate_period_overlap",
            "/tax_rates/1",
        )

    def test_rate_and_period_validation(self) -> None:
        self.assert_error(
            _payload(tax_rates=[_rate(rate="1.01")]),
            "invalid_tax_rate",
            "/tax_rates/0/rate",
        )
        self.assert_error(
            _payload(tax_rates=[_rate(rate="-0.01")]),
            "invalid_tax_rate",
            "/tax_rates/0/rate",
        )
        self.assert_error(
            _payload(tax_rates=[_rate(effective_from="2027-01-01", effective_to="2026-12-31")]),
            "invalid_tax_rate_period",
            "/tax_rates/0/effective_from",
        )

    def test_required_and_unknown_fields(self) -> None:
        self.assert_error({}, "required", "/report_date")
        self.assert_error(_payload(extra=1), "unknown_field", "/extra")
        self.assert_error(
            _payload(tax_rates=[dict(_rate(), bogus=1)]),
            "unknown_field",
            "/tax_rates/0/bogus",
        )
        self.assert_error(
            _payload(items=[dict(_item(), bogus=1)]),
            "unknown_field",
            "/items/0/bogus",
        )
        self.assert_error(_payload(tax_rates="x"), "invalid_type", "/tax_rates")
        self.assert_error(_payload(items="x"), "invalid_type", "/items")

    def test_value_error_is_plain_value_error(self) -> None:
        try:
            calculate_deferred_tax(_payload(items=[_item(item_id=None)]))
        except ValueError as exc:
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("expected ValueError")

    def test_decimal_used_internally(self) -> None:
        payload = _payload(items=[_item(carrying_amount=Decimal("200.10"), tax_base="100.10")])
        row = calculate_deferred_tax(payload)["items"][0]
        self.assertEqual(row["temporary_difference"], "100.00")
        self.assertEqual(row["deferred_tax_liability"], "25.00")


class DeferredTaxServiceTest(unittest.TestCase):
    def test_service_success_tuple(self) -> None:
        status, body = Service().calculate_deferred_tax(_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["total_deferred_tax_asset"], "0.00")

    def test_service_value_error_mapped_to_400(self) -> None:
        status, body = Service().calculate_deferred_tax({})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "required")
        self.assertEqual(body["error"]["path"], "/report_date")
        self.assertNotIn("items", body)
        self.assertNotIn("totals", body)


if __name__ == "__main__":
    unittest.main()
