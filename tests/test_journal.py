import json
import unittest

from ledgerlens.service import Service


def valid_payload() -> dict:
    return {
        "voucher_id": "JV-2026-0001",
        "posting_date": "2026-10-03",
        "currency": "USD",
        "lines": [
            {"line_id": "L1", "account_code": "1001", "debit": "100.00", "credit": "0"},
            {"line_id": "L2", "account_code": "4001", "debit": "0", "credit": "100.00"},
        ],
    }


class JournalValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def validate(self, payload: dict):
        return self.service.validate_journal_entry(payload)

    def test_balanced_entry_returns_200_with_normalized_totals(self) -> None:
        status, body = self.validate(valid_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body, {"valid": True, "debit_total": "100.00", "credit_total": "100.00"})

    def test_totals_are_exact_decimal_and_normalized(self) -> None:
        payload = valid_payload()
        payload["lines"][0]["debit"] = "0.1"
        payload["lines"][1]["credit"] = "0.10"
        payload["lines"].append(
            {"line_id": "L3", "account_code": "1002", "debit": "0.2", "credit": "0"}
        )
        payload["lines"].append(
            {"line_id": "L4", "account_code": "4002", "debit": "0", "credit": "0.20"}
        )
        status, body = self.validate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["debit_total"], "0.30")
        self.assertEqual(body["credit_total"], "0.30")

    def test_unbalanced_entry_returns_422_without_totals(self) -> None:
        payload = valid_payload()
        payload["lines"][1]["credit"] = "99.99"
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertEqual(body, {"valid": False, "code": "unbalanced_entry"})

    def test_stateless_same_input_same_output(self) -> None:
        payload = valid_payload()
        first = self.validate(payload)
        second = self.validate(payload)
        self.assertEqual(first, second)

    def test_required_and_blank_and_type_errors(self) -> None:
        status, body = self.validate(
            {
                "voucher_id": "",
                "posting_date": 20261003,
                "currency": "USD",
                "lines": [
                    {"line_id": "L1", "account_code": "1001", "debit": "1", "credit": "0"},
                    {"line_id": "L2", "account_code": "4001", "debit": "0"},
                ],
            }
        )
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        by_path = {e["path"]: e["code"] for e in body["errors"]}
        self.assertEqual(by_path["/voucher_id"], "blank_value")
        self.assertEqual(by_path["/posting_date"], "invalid_type")
        self.assertEqual(by_path["/lines/1/credit"], "required")

    def test_unknown_fields_at_voucher_and_line_level(self) -> None:
        payload = valid_payload()
        payload["memo"] = "x"
        payload["lines"][0]["extra"] = 1
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        codes = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/memo", "unknown_field"), codes)
        self.assertIn(("/lines/0/extra", "unknown_field"), codes)

    def test_invalid_date_and_currency(self) -> None:
        for bad_date in ("2026-02-30", "2026-1-01", "2026/10/03", "not-a-date"):
            payload = valid_payload()
            payload["posting_date"] = bad_date
            status, body = self.validate(payload)
            self.assertEqual(status, 422, bad_date)
            self.assertIn(
                ("/posting_date", "invalid_date"),
                {(e["path"], e["code"]) for e in body["errors"]},
            )
        for bad_currency in ("usd", "US", "USDD", "U1D"):
            payload = valid_payload()
            payload["currency"] = bad_currency
            status, body = self.validate(payload)
            self.assertEqual(status, 422, bad_currency)
            self.assertIn(
                ("/currency", "invalid_currency"),
                {(e["path"], e["code"]) for e in body["errors"]},
            )

    def test_invalid_amount_formats(self) -> None:
        for bad in ("-1", "+1", "1.234", "1.", ".5", "1e3", "abc", "1,00"):
            payload = valid_payload()
            payload["lines"][0]["debit"] = bad
            status, body = self.validate(payload)
            self.assertEqual(status, 422, bad)
            errors = {(e["path"], e["code"]) for e in body["errors"]}
            self.assertIn(("/lines/0/debit", "invalid_amount"), errors)
            # 存在金额错误时不追加余额错误
            self.assertNotIn("unbalanced_entry", json.dumps(body))
            self.assertNotIn("debit_total", body)

    def test_amount_must_be_string(self) -> None:
        payload = valid_payload()
        payload["lines"][0]["debit"] = 100
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/lines/0/debit", "invalid_type"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_too_few_lines_and_invalid_lines_type(self) -> None:
        payload = valid_payload()
        payload["lines"] = payload["lines"][:1]
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/lines", "too_few_lines"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

        payload = valid_payload()
        payload["lines"] = "nope"
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/lines", "invalid_type"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_duplicate_line_id(self) -> None:
        payload = valid_payload()
        payload["lines"][1]["line_id"] = "L1"
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/lines/1/line_id", "duplicate_line_id"),
            {(e["path"], e["code"]) for e in body["errors"]},
        )

    def test_invalid_side(self) -> None:
        for debit, credit in (("0", "0.00"), ("5", "5")):
            payload = valid_payload()
            payload["lines"][0]["debit"] = debit
            payload["lines"][0]["credit"] = credit
            status, body = self.validate(payload)
            self.assertEqual(status, 422, (debit, credit))
            self.assertIn(
                ("/lines/0", "invalid_side"),
                {(e["path"], e["code"]) for e in body["errors"]},
            )

    def test_errors_sorted_by_path_then_code(self) -> None:
        payload = valid_payload()
        payload["voucher_id"] = ""
        payload["currency"] = "usd"
        payload["lines"][0]["debit"] = "abc"
        payload["zzz"] = 1
        status, body = self.validate(payload)
        self.assertEqual(status, 422)
        keys = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(keys, sorted(keys))
        for e in body["errors"]:
            self.assertEqual(set(e), {"path", "code", "message"})


if __name__ == "__main__":
    unittest.main()
