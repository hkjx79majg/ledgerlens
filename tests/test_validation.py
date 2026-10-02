import unittest

from ledgerlens.service import Service


def valid_voucher(**overrides):
    voucher = {
        "voucher_id": "V-0001",
        "posting_date": "2026-03-31",
        "currency": "CNY",
        "lines": [
            {"line_id": "L1", "account_code": "1001", "debit": "100.00", "credit": "0"},
            {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "100.00"},
        ],
    }
    voucher.update(overrides)
    return voucher


class ValidationHappyPathTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def test_balanced_voucher_is_valid(self):
        status, body = self.service.validate_journal_entry(valid_voucher())
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"valid": True, "debit_total": "100.00", "credit_total": "100.00"},
        )

    def test_totals_canonicalized_to_two_decimals(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "100.1", "credit": "0"},
                {"line_id": "L2", "account_code": "1002", "debit": "0.90", "credit": "0"},
                {"line_id": "L3", "account_code": "2001", "debit": "0", "credit": "101"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 200)
        self.assertEqual(body["debit_total"], "101.00")
        self.assertEqual(body["credit_total"], "101.00")

    def test_exact_decimal_summing(self):
        # 0.1 + 0.2 must equal 0.3 exactly, unlike binary floats.
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "0.1", "credit": "0"},
                {"line_id": "L2", "account_code": "1002", "debit": "0.2", "credit": "0"},
                {"line_id": "L3", "account_code": "2001", "debit": "0", "credit": "0.3"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 200)
        self.assertEqual(body["valid"], True)

    def test_same_input_yields_same_result_and_keeps_no_state(self):
        first = self.service.validate_journal_entry(valid_voucher())
        # Interleave a failing voucher; the service must not retain state.
        self.service.validate_journal_entry({})
        second = self.service.validate_journal_entry(valid_voucher())
        self.assertEqual(first, second)


class UnbalancedTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def test_unbalanced_returns_422_without_totals(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "100.00", "credit": "0"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "99.99"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        self.assertEqual(body["valid"], False)
        self.assertNotIn("debit_total", body)
        self.assertNotIn("credit_total", body)
        self.assertEqual(
            body["errors"],
            [{"path": "/lines", "code": "unbalanced_entry", "message": body["errors"][0]["message"]}],
        )


class StructuralErrorTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    @staticmethod
    def _codes(body):
        return [(e["path"], e["code"]) for e in body["errors"]]

    def test_missing_top_level_fields(self):
        status, body = self.service.validate_journal_entry({})
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertIn(("/currency", "required"), self._codes(body))
        self.assertIn(("/lines", "required"), self._codes(body))
        self.assertIn(("/posting_date", "required"), self._codes(body))
        self.assertIn(("/voucher_id", "required"), self._codes(body))

    def test_blank_strings(self):
        voucher = valid_voucher(
            voucher_id=" ",
            currency="CNY",
            lines=[
                {"line_id": " ", "account_code": " ", "debit": "1", "credit": "0"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "1"},
            ],
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 200)  # whitespace-only is not an empty string

        voucher["voucher_id"] = ""
        voucher["lines"][0]["line_id"] = ""
        voucher["lines"][0]["account_code"] = ""
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = self._codes(body)
        self.assertIn(("/voucher_id", "blank_value"), codes)
        self.assertIn(("/lines/0/line_id", "blank_value"), codes)
        self.assertIn(("/lines/0/account_code", "blank_value"), codes)

    def test_invalid_types(self):
        voucher = {
            "voucher_id": 123,
            "posting_date": 20260331,
            "currency": ["CNY"],
            "lines": {"line_id": "L1"},
        }
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = self._codes(body)
        self.assertIn(("/voucher_id", "invalid_type"), codes)
        self.assertIn(("/posting_date", "invalid_type"), codes)
        self.assertIn(("/currency", "invalid_type"), codes)
        self.assertIn(("/lines", "invalid_type"), codes)

    def test_unknown_fields(self):
        voucher = valid_voucher()
        voucher["extra"] = 1
        voucher["lines"][0]["mystery"] = 1
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = self._codes(body)
        self.assertIn(("/extra", "unknown_field"), codes)
        self.assertIn(("/lines/0/mystery", "unknown_field"), codes)

    def test_json_pointer_escaping_in_unknown_field(self):
        voucher = valid_voucher()
        voucher["a/b~c"] = 1
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        self.assertIn(("/a~1b~0c", "unknown_field"), self._codes(body))

    def test_line_elements_must_be_objects(self):
        voucher = valid_voucher(lines=[{"line_id": "L1"}, 42])
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        self.assertIn(("/lines/1", "invalid_type"), self._codes(body))

    def test_errors_sorted_by_path_then_code(self):
        voucher = valid_voucher(lines=[])
        voucher["zzz"] = 1
        voucher["aaa"] = 1
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        pairs = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(pairs, sorted(pairs))


class DateTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def test_invalid_dates(self):
        for bad in ("2026-02-30", "2026-13-01", "2026-00-01", "2026/03/31", "2026-3-1", "xx260331"):
            with self.subTest(bad=bad):
                status, body = self.service.validate_journal_entry(valid_voucher(posting_date=bad))
                self.assertEqual(status, 422)
                self.assertIn(
                    ("/posting_date", "invalid_date"),
                    [(e["path"], e["code"]) for e in body["errors"]],
                )

    def test_leap_day_rules(self):
        self.assertEqual(
            self.service.validate_journal_entry(valid_voucher(posting_date="2024-02-29"))[0],
            200,
        )
        status, body = self.service.validate_journal_entry(
            valid_voucher(posting_date="2026-02-29")
        )
        self.assertEqual(status, 422)
        self.assertIn(("/posting_date", "invalid_date"), [(e["path"], e["code"]) for e in body["errors"]])


class CurrencyTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def test_invalid_currencies(self):
        for bad in ("cny", "CN", "CNYS", "CN1", " CNY", "CNY "):
            with self.subTest(bad=bad):
                status, body = self.service.validate_journal_entry(valid_voucher(currency=bad))
                self.assertEqual(status, 422)
                self.assertIn(
                    ("/currency", "invalid_currency"),
                    [(e["path"], e["code"]) for e in body["errors"]],
                )


class AmountTest(unittest.TestCase):
    BAD_AMOUNTS = [
        "-1", "-0.01", "1.234", "1e3", "1E3", ".5", "1.", "+1", "0x1",
        " 1", "1 ", "1,00", "NaN", "Infinity", "0.001",
    ]

    def setUp(self):
        self.service = Service()

    def test_bad_amount_shapes(self):
        for bad in self.BAD_AMOUNTS:
            with self.subTest(bad=bad):
                voucher = valid_voucher(
                    lines=[
                        {"line_id": "L1", "account_code": "1001", "debit": bad, "credit": "0"},
                        {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "1"},
                    ]
                )
                status, body = self.service.validate_journal_entry(voucher)
                self.assertEqual(status, 422)
                self.assertIn(
                    ("/lines/0/debit", "invalid_amount"),
                    [(e["path"], e["code"]) for e in body["errors"]],
                )

    def test_accepted_amount_shapes(self):
        for good in ("0", "0.0", "0.00", "100", "100.1", "100.12", "00012"):
            with self.subTest(good=good):
                voucher = valid_voucher(
                    lines=[
                        {"line_id": "L1", "account_code": "1001", "debit": good, "credit": "0"},
                        {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": good},
                    ]
                )
                status, _ = self.service.validate_journal_entry(voucher)
                if good in ("0", "0.0", "0.00"):
                    # Both sides zero on each line: invalid_side, but amount itself is fine.
                    continue
                self.assertEqual(status, 200)

    def test_amount_error_suppresses_balance_error(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "1.234", "credit": "0"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "999.99"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertIn(("/lines/0/debit", "invalid_amount"), codes)
        self.assertNotIn(("/lines", "unbalanced_entry"), codes)


class LinesTest(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def test_too_few_lines(self):
        for lines in ([], [{"line_id": "L1", "account_code": "1001", "debit": "1", "credit": "0"}]):
            with self.subTest(lines=lines):
                status, body = self.service.validate_journal_entry(valid_voucher(lines=lines))
                self.assertEqual(status, 422)
                self.assertIn(
                    ("/lines", "too_few_lines"),
                    [(e["path"], e["code"]) for e in body["errors"]],
                )

    def test_duplicate_line_ids_flagged_on_each_repeat(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "DUP", "account_code": "1001", "debit": "1", "credit": "0"},
                {"line_id": "DUP", "account_code": "1002", "debit": "1", "credit": "0"},
                {"line_id": "DUP", "account_code": "2001", "debit": "0", "credit": "2"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        dup_paths = [p for p, c in ((e["path"], e["code"]) for e in body["errors"]) if c == "duplicate_line_id"]
        self.assertEqual(dup_paths, ["/lines/1/line_id", "/lines/2/line_id"])

    def test_invalid_side_both_zero(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "0", "credit": "0.00"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "1"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        self.assertIn(
            ("/lines/0", "invalid_side"),
            [(e["path"], e["code"]) for e in body["errors"]],
        )

    def test_invalid_side_both_positive_but_totals_tie(self):
        # Every line has both sides positive, yet the debit and credit
        # columns tie 10 == 10: invalid_side is reported, unbalanced is not.
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "5", "credit": "5"},
                {"line_id": "L2", "account_code": "2001", "debit": "5", "credit": "5"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertEqual(
            [c for c in codes if c[1] == "invalid_side"],
            [("/lines/0", "invalid_side"), ("/lines/1", "invalid_side")],
        )
        self.assertNotIn(("/lines", "unbalanced_entry"), codes)

    def test_invalid_side_and_genuinely_unbalanced_both_returned(self):
        voucher = valid_voucher(
            lines=[
                {"line_id": "L1", "account_code": "1001", "debit": "5", "credit": "5"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "10"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertIn(("/lines/0", "invalid_side"), codes)
        # Columns are 5 vs 15: the imbalance is decidable and must be reported.
        self.assertIn(("/lines", "unbalanced_entry"), codes)

    def test_missing_line_fields(self):
        voucher = valid_voucher(
            lines=[
                {"debit": "1"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "1"},
            ]
        )
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = [(e["path"], e["code"]) for e in body["errors"]]
        self.assertIn(("/lines/0/line_id", "required"), codes)
        self.assertIn(("/lines/0/account_code", "required"), codes)
        self.assertIn(("/lines/0/credit", "required"), codes)

    def test_multiple_errors_returned_at_once(self):
        voucher = valid_voucher(voucher_id="", currency="cny", posting_date="2026-02-30")
        voucher["nope"] = True
        voucher["lines"][0]["debit"] = "-1"
        voucher["lines"][1]["credit"] = "2.000"
        status, body = self.service.validate_journal_entry(voucher)
        self.assertEqual(status, 422)
        codes = {(e["path"], e["code"]) for e in body["errors"]}
        self.assertIn(("/voucher_id", "blank_value"), codes)
        self.assertIn(("/currency", "invalid_currency"), codes)
        self.assertIn(("/posting_date", "invalid_date"), codes)
        self.assertIn(("/nope", "unknown_field"), codes)
        self.assertIn(("/lines/0/debit", "invalid_amount"), codes)
        self.assertIn(("/lines/1/credit", "invalid_amount"), codes)
        for error in body["errors"]:
            self.assertEqual(set(error), {"path", "code", "message"})
            self.assertTrue(error["message"])


if __name__ == "__main__":
    unittest.main()
