import copy
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.deferred_tax import DeferredTaxError, calculate_deferred_tax
from ledgerlens.server import Handler
from ledgerlens.service import Service


def band(start="2026-01-01", end="2027-12-31", rate="0.25"):
    return {"start_date": start, "end_date": end, "rate": rate}


def item(**overrides):
    base = {
        "item_id": "IT-1",
        "classification": "asset",
        "carrying_amount": "1200.00",
        "tax_base": "1000.00",
        "expected_reversal_date": "2027-06-30",
        "attribution": "profit_or_loss",
    }
    base.update(overrides)
    return base


def payload(**overrides):
    base = {
        "reporting_date": "2026-12-31",
        "currency": "CNY",
        "tax_rates": [band()],
        "items": [item()],
    }
    base.update(overrides)
    return base


class CalculateDeferredTaxTest(unittest.TestCase):
    def test_taxable_asset_item(self) -> None:
        result = calculate_deferred_tax(payload())
        self.assertEqual(result["reporting_date"], "2026-12-31")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(len(result["items"]), 1)
        line = result["items"][0]
        self.assertEqual(line["item_id"], "IT-1")
        self.assertEqual(line["classification"], "asset")
        self.assertEqual(line["temporary_difference"], "200.00")
        self.assertEqual(line["tax_rate"], "0.25")
        self.assertEqual(line["recognized_deductible_difference"], "0")
        self.assertEqual(line["unrecognized_deductible_difference"], "0")
        self.assertEqual(line["deferred_tax_asset"], "0.00")
        self.assertEqual(line["deferred_tax_liability"], "50.00")
        self.assertEqual(line["attribution"], "profit_or_loss")
        self.assertEqual(result["total_deferred_tax_asset"], "0.00")
        self.assertEqual(result["total_deferred_tax_liability"], "50.00")
        self.assertEqual(result["net_deferred_tax"], "-50.00")
        self.assertEqual(
            result["attribution_totals"]["profit_or_loss"],
            {
                "deferred_tax_asset": "0.00",
                "deferred_tax_liability": "50.00",
                "net_deferred_tax": "-50.00",
            },
        )
        self.assertEqual(
            result["attribution_totals"]["oci"],
            {
                "deferred_tax_asset": "0.00",
                "deferred_tax_liability": "0.00",
                "net_deferred_tax": "0.00",
            },
        )

    def test_liability_difference_direction(self) -> None:
        # 负债账面价值高于计税基础：差异为负，在可收回上限内形成递延所得税资产。
        result = calculate_deferred_tax(
            payload(items=[item(classification="liability", recoverable_cap="200.00")])
        )
        line = result["items"][0]
        self.assertEqual(line["temporary_difference"], "-200.00")
        self.assertEqual(line["deferred_tax_asset"], "50.00")
        # 负债计税基础高于账面价值：差异为正，形成递延所得税负债。
        result = calculate_deferred_tax(
            payload(
                items=[
                    item(
                        classification="liability",
                        carrying_amount="1000.00",
                        tax_base="1200.00",
                    )
                ]
            )
        )
        line = result["items"][0]
        self.assertEqual(line["temporary_difference"], "200.00")
        self.assertEqual(line["deferred_tax_liability"], "50.00")

    def test_deductible_item_within_recoverable_cap(self) -> None:
        result = calculate_deferred_tax(
            payload(
                items=[
                    item(
                        carrying_amount="800.00",
                        tax_base="1000.00",
                        recoverable_cap="150.00",
                        attribution="oci",
                    )
                ]
            )
        )
        line = result["items"][0]
        self.assertEqual(line["temporary_difference"], "-200.00")
        self.assertEqual(line["recognized_deductible_difference"], "150.00")
        self.assertEqual(line["unrecognized_deductible_difference"], "50.00")
        self.assertEqual(line["deferred_tax_asset"], "37.50")
        self.assertEqual(line["deferred_tax_liability"], "0.00")
        self.assertEqual(result["total_deferred_tax_asset"], "37.50")
        self.assertEqual(result["net_deferred_tax"], "37.50")
        self.assertEqual(
            result["attribution_totals"]["oci"]["deferred_tax_asset"], "37.50"
        )

    def test_order_preserved_and_attribution_totals(self) -> None:
        items = [
            item(item_id="A", attribution="equity"),
            item(item_id="B", carrying_amount="500.00", tax_base="1000.00",
                 recoverable_cap="500.00", attribution="oci"),
            item(item_id="C", classification="liability",
                 carrying_amount="100.00", tax_base="400.00"),
        ]
        result = calculate_deferred_tax(payload(items=items))
        self.assertEqual([line["item_id"] for line in result["items"]], ["A", "B", "C"])
        # A: 差异 200 -> 负债 50.00；B: 可抵扣 500 -> 资产 125.00；C: 差异 300 -> 负债 75.00。
        self.assertEqual(result["total_deferred_tax_asset"], "125.00")
        self.assertEqual(result["total_deferred_tax_liability"], "125.00")
        self.assertEqual(result["net_deferred_tax"], "0.00")
        self.assertEqual(
            result["attribution_totals"]["equity"]["deferred_tax_liability"], "50.00"
        )
        self.assertEqual(
            result["attribution_totals"]["oci"]["deferred_tax_asset"], "125.00"
        )
        self.assertEqual(
            result["attribution_totals"]["profit_or_loss"]["deferred_tax_liability"],
            "75.00",
        )

    def test_item_rounding_and_totals_from_rounded_items(self) -> None:
        # 每项 0.05 * 0.1 = 0.005，四舍五入为 0.01；汇总由已舍入明细相加得 0.02。
        items = [
            item(item_id="R-1", carrying_amount="0.05", tax_base="0.00"),
            item(item_id="R-2", carrying_amount="0.05", tax_base="0.00"),
        ]
        result = calculate_deferred_tax(
            payload(tax_rates=[band(rate="0.1")], items=items)
        )
        self.assertEqual(result["items"][0]["deferred_tax_liability"], "0.01")
        self.assertEqual(result["items"][1]["deferred_tax_liability"], "0.01")
        self.assertEqual(result["total_deferred_tax_liability"], "0.02")

    def test_empty_items_returns_zero_totals(self) -> None:
        result = calculate_deferred_tax(payload(items=[]))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_deferred_tax_asset"], "0.00")
        self.assertEqual(result["total_deferred_tax_liability"], "0.00")
        self.assertEqual(result["net_deferred_tax"], "0.00")
        for attribution in ("profit_or_loss", "oci", "equity"):
            self.assertEqual(
                result["attribution_totals"][attribution],
                {
                    "deferred_tax_asset": "0.00",
                    "deferred_tax_liability": "0.00",
                    "net_deferred_tax": "0.00",
                },
            )

    def test_zero_difference_item(self) -> None:
        result = calculate_deferred_tax(
            payload(items=[item(carrying_amount="1000.00", tax_base="1000.00")])
        )
        line = result["items"][0]
        self.assertEqual(line["temporary_difference"], "0")
        self.assertEqual(line["deferred_tax_asset"], "0.00")
        self.assertEqual(line["deferred_tax_liability"], "0.00")

    def test_amounts_accept_lossless_decimal_values(self) -> None:
        result = calculate_deferred_tax(
            payload(items=[item(carrying_amount=1200, tax_base=1000.0)])
        )
        self.assertEqual(result["items"][0]["temporary_difference"], "200")
        self.assertEqual(result["total_deferred_tax_liability"], "50.00")

    def test_rate_band_selected_by_reversal_date(self) -> None:
        bands = [
            band(start="2026-01-01", end="2026-12-31", rate="0.30"),
            band(start="2027-01-01", end="2027-12-31", rate="0.20"),
        ]
        result = calculate_deferred_tax(payload(tax_rates=bands))
        self.assertEqual(result["items"][0]["tax_rate"], "0.20")
        self.assertEqual(result["items"][0]["deferred_tax_liability"], "40.00")

    def test_input_not_mutated(self) -> None:
        request = payload(
            items=[item(recoverable_cap="0.00"), item(item_id="IT-2")]
        )
        snapshot = copy.deepcopy(request)
        calculate_deferred_tax(request)
        self.assertEqual(request, snapshot)

    def test_deterministic_result(self) -> None:
        request = payload()
        self.assertEqual(calculate_deferred_tax(request), calculate_deferred_tax(request))

    def assert_error(self, request, code, path) -> None:
        with self.assertRaises(ValueError) as ctx:
            calculate_deferred_tax(request)
        exc = ctx.exception
        self.assertIsInstance(exc, DeferredTaxError)
        self.assertEqual(exc.code, code)
        self.assertEqual(exc.path, path)

    def test_request_must_be_object(self) -> None:
        with self.assertRaises(ValueError):
            calculate_deferred_tax([1, 2])

    def test_unknown_field(self) -> None:
        self.assert_error(payload(extra=1), "unknown_field", "/extra")

    def test_missing_reporting_date(self) -> None:
        request = payload()
        del request["reporting_date"]
        self.assert_error(request, "required", "/reporting_date")

    def test_invalid_reporting_date(self) -> None:
        self.assert_error(
            payload(reporting_date="2026-02-30"), "invalid_date", "/reporting_date"
        )

    def test_invalid_currency(self) -> None:
        self.assert_error(payload(currency="cny"), "invalid_currency", "/currency")

    def test_duplicate_item_id(self) -> None:
        request = payload(items=[item(), item()])
        self.assert_error(request, "duplicate_item_id", "/items/1/item_id")

    def test_invalid_classification(self) -> None:
        self.assert_error(
            payload(items=[item(classification="equity")]),
            "invalid_classification",
            "/items/0/classification",
        )

    def test_invalid_attribution(self) -> None:
        self.assert_error(
            payload(items=[item(attribution="income")]),
            "invalid_attribution",
            "/items/0/attribution",
        )

    def test_invalid_reversal_date(self) -> None:
        self.assert_error(
            payload(items=[item(expected_reversal_date="2027-13-01")]),
            "invalid_date",
            "/items/0/expected_reversal_date",
        )

    def test_missing_carrying_amount(self) -> None:
        request_item = item()
        del request_item["carrying_amount"]
        self.assert_error(
            payload(items=[request_item]), "required", "/items/0/carrying_amount"
        )

    def test_invalid_amount(self) -> None:
        self.assert_error(
            payload(items=[item(carrying_amount="abc")]),
            "invalid_amount",
            "/items/0/carrying_amount",
        )
        self.assert_error(
            payload(items=[item(tax_base=float("nan"))]),
            "invalid_amount",
            "/items/0/tax_base",
        )
        self.assert_error(
            payload(items=[item(carrying_amount=True)]),
            "invalid_amount",
            "/items/0/carrying_amount",
        )

    def test_invalid_tax_rate(self) -> None:
        self.assert_error(
            payload(tax_rates=[band(rate="1.5")]), "invalid_tax_rate", "/tax_rates/0/rate"
        )
        self.assert_error(
            payload(tax_rates=[band(rate="-0.1")]),
            "invalid_tax_rate",
            "/tax_rates/0/rate",
        )

    def test_rate_bounds_accepted(self) -> None:
        for rate in ("0", "1"):
            result = calculate_deferred_tax(payload(tax_rates=[band(rate=rate)]))
            self.assertEqual(result["items"][0]["tax_rate"], rate)

    def test_invalid_band_period(self) -> None:
        self.assert_error(
            payload(
                tax_rates=[band(start="2027-01-01", end="2026-01-01")], items=[]
            ),
            "invalid_period",
            "/tax_rates/0/start_date",
        )

    def test_overlapping_tax_rates(self) -> None:
        bands = [
            band(start="2026-01-01", end="2026-12-31"),
            band(start="2026-12-31", end="2027-12-31"),
        ]
        self.assert_error(
            payload(tax_rates=bands, items=[]), "overlapping_tax_rates", "/tax_rates/1"
        )

    def test_missing_recoverable_cap_for_deductible_item(self) -> None:
        self.assert_error(
            payload(items=[item(carrying_amount="800.00", tax_base="1000.00")]),
            "required",
            "/items/0/recoverable_cap",
        )

    def test_negative_recoverable_cap(self) -> None:
        self.assert_error(
            payload(
                items=[
                    item(
                        carrying_amount="800.00",
                        tax_base="1000.00",
                        recoverable_cap="-1.00",
                    )
                ]
            ),
            "invalid_recoverable_cap",
            "/items/0/recoverable_cap",
        )

    def test_recoverable_cap_exceeds_deductible(self) -> None:
        self.assert_error(
            payload(
                items=[
                    item(
                        carrying_amount="800.00",
                        tax_base="1000.00",
                        recoverable_cap="200.01",
                    )
                ]
            ),
            "recoverable_cap_exceeds_deductible",
            "/items/0/recoverable_cap",
        )

    def test_no_applicable_tax_rate(self) -> None:
        self.assert_error(
            payload(items=[item(expected_reversal_date="2028-01-01")]),
            "no_applicable_tax_rate",
            "/items/0/expected_reversal_date",
        )

    def test_ambiguous_tax_rate(self) -> None:
        bands = [
            band(start="2026-01-01", end="2027-06-30"),
            band(start="2027-06-30", end="2028-12-31"),
        ]
        self.assert_error(
            payload(tax_rates=bands),
            "ambiguous_tax_rate",
            "/items/0/expected_reversal_date",
        )

    def test_service_entry_raises_value_error(self) -> None:
        service = Service()
        with self.assertRaises(ValueError):
            service.calculate_deferred_tax(payload(items=[item(), item()]))
        result = service.calculate_deferred_tax(payload())
        self.assertEqual(result["total_deferred_tax_liability"], "50.00")


class DeferredTaxHttpTest(unittest.TestCase):
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
        result = resp.status, json.loads(resp.read())
        conn.close()
        return result

    def test_calculate_success_200(self) -> None:
        status, body = self.request("/deferred-tax/calculate", payload())
        self.assertEqual(status, 200)
        self.assertEqual(body, calculate_deferred_tax(payload()))
        self.assertEqual(body["total_deferred_tax_liability"], "50.00")

    def test_calculate_empty_items_200(self) -> None:
        status, body = self.request("/deferred-tax/calculate", payload(items=[]))
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["net_deferred_tax"], "0.00")

    def test_calculate_validation_error_400(self) -> None:
        status, body = self.request(
            "/deferred-tax/calculate", payload(items=[item(), item()])
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "duplicate_item_id")
        self.assertEqual(body["error"]["path"], "/items/1/item_id")
        self.assertIn("message", body["error"])
        self.assertNotIn("items", body)

    def test_calculate_no_applicable_tax_rate_400(self) -> None:
        status, body = self.request(
            "/deferred-tax/calculate",
            payload(items=[item(expected_reversal_date="2030-01-01")]),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "no_applicable_tax_rate")
        self.assertEqual(body["error"]["path"], "/items/0/expected_reversal_date")

    def test_calculate_band_error_path_400(self) -> None:
        status, body = self.request(
            "/deferred-tax/calculate", payload(tax_rates=[band(rate="2")])
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_tax_rate")
        self.assertEqual(body["error"]["path"], "/tax_rates/0/rate")

    def test_calculate_media_type_and_json_errors(self) -> None:
        status, body = self.request(
            "/deferred-tax/calculate", "{}", "text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

        status, body = self.request("/deferred-tax/calculate", "{bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.request("/deferred-tax/calculate", "[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "request_not_object")


if __name__ == "__main__":
    unittest.main()
