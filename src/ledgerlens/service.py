"""Core service surface for LedgerLens.

The service is stateless: every public method derives its result solely
from its arguments, nothing is persisted, and no state is retained across
calls. Keep the public surface here backward compatible.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Context, Decimal, localcontext

from . import __version__

_AMOUNT_RE = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_TWO_PLACES = Decimal("0.01")
_SUM_CONTEXT = Context(prec=100)

_TOP_LEVEL_FIELDS = frozenset({"voucher_id", "posting_date", "currency", "lines"})
_LINE_FIELDS = frozenset({"line_id", "account_code", "debit", "credit"})

_MESSAGES = {
    "required": "缺少必填字段",
    "invalid_type": "字段类型错误",
    "blank_value": "字段不能为空字符串",
    "unknown_field": "存在未知字段",
    "invalid_date": "日期必须为 YYYY-MM-DD 格式的有效公历日期",
    "invalid_currency": "币种必须为三位大写字母",
    "invalid_amount": "金额必须为无符号、无指数、最多两位小数的非负十进制字符串",
    "too_few_lines": "凭证至少需要两条分录",
    "duplicate_line_id": "line_id 在同一凭证内必须唯一",
    "invalid_side": "每条分录的借贷必须恰有一方严格大于零，另一方为零",
    "unbalanced_entry": "借贷合计不相等",
}


def _is_str(value: object) -> bool:
    return isinstance(value, str) and not isinstance(value, bool)


def _pointer(token: str) -> str:
    return "/" + token.replace("~", "~0").replace("/", "~1")


class Service:
    """Stateless service: health reporting and voucher validation."""

    name = "ledgerlens"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def validate_journal_entry(self, voucher: dict) -> tuple[int, dict]:
        """Validate one double-entry voucher without persisting anything.

        Returns ``(http_status, response_body)``. All currently decidable
        structural and business errors are returned at once, sorted by
        JSON Pointer path and then by code.
        """
        errors: list[dict[str, str]] = []

        def add_error(path: str, code: str) -> None:
            errors.append({"path": path, "code": code, "message": _MESSAGES[code]})

        for key in voucher:
            if key not in _TOP_LEVEL_FIELDS:
                add_error(_pointer(key), "unknown_field")

        self._validate_identifier(voucher, "voucher_id", errors)
        self._validate_posting_date(voucher, errors)
        self._validate_currency(voucher, errors)
        amounts, balance_decidable = self._validate_lines(voucher, errors)

        balanced = False
        debit_total = credit_total = None
        if balance_decidable:
            with localcontext(_SUM_CONTEXT):
                debit_total = sum((debit for debit, _ in amounts), Decimal("0"))
                credit_total = sum((credit for _, credit in amounts), Decimal("0"))
                balanced = debit_total == credit_total
            if not balanced:
                add_error("/lines", "unbalanced_entry")

        if errors or not balanced:
            errors.sort(key=lambda item: (item["path"], item["code"]))
            return 422, {"valid": False, "errors": errors}

        with localcontext(_SUM_CONTEXT):
            return 200, {
                "valid": True,
                "debit_total": str(debit_total.quantize(_TWO_PLACES)),
                "credit_total": str(credit_total.quantize(_TWO_PLACES)),
            }

    def _validate_identifier(
        self, voucher: dict, field: str, errors: list[dict[str, str]]
    ) -> None:
        if field not in voucher:
            add = ("/" + field, "required")
        elif not _is_str(voucher[field]):
            add = ("/" + field, "invalid_type")
        elif voucher[field] == "":
            add = ("/" + field, "blank_value")
        else:
            add = None
        if add is not None:
            errors.append(
                {"path": add[0], "code": add[1], "message": _MESSAGES[add[1]]}
            )

    def _validate_posting_date(
        self, voucher: dict, errors: list[dict[str, str]]
    ) -> None:
        value = voucher.get("posting_date")
        if "posting_date" not in voucher:
            code = "required"
        elif not _is_str(value) or not _DATE_RE.match(value):
            code = "invalid_date" if _is_str(value) else "invalid_type"
        else:
            try:
                date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
            except ValueError:
                code = "invalid_date"
            else:
                code = None
        if code is not None:
            errors.append(
                {"path": "/posting_date", "code": code, "message": _MESSAGES[code]}
            )

    def _validate_currency(self, voucher: dict, errors: list[dict[str, str]]) -> None:
        value = voucher.get("currency")
        if "currency" not in voucher:
            code = "required"
        elif not _is_str(value):
            code = "invalid_type"
        elif not _CURRENCY_RE.match(value):
            code = "invalid_currency"
        else:
            code = None
        if code is not None:
            errors.append(
                {"path": "/currency", "code": code, "message": _MESSAGES[code]}
            )

    def _validate_lines(
        self, voucher: dict, errors: list[dict[str, str]]
    ) -> tuple[list[tuple[Decimal, Decimal]], bool]:
        """Validate the lines array.

        Returns the parsed ``(debit, credit)`` pairs and whether the
        balance check is soundly decidable (every line yielded two usable
        amounts). Balance is intentionally not evaluated when an amount is
        unusable, since the totals would otherwise be misleading.
        """
        if "lines" not in voucher:
            errors.append(
                {"path": "/lines", "code": "required", "message": _MESSAGES["required"]}
            )
            return [], False

        lines = voucher["lines"]
        if not isinstance(lines, list) or isinstance(lines, bool):
            errors.append(
                {
                    "path": "/lines",
                    "code": "invalid_type",
                    "message": _MESSAGES["invalid_type"],
                }
            )
            return [], False

        if len(lines) < 2:
            errors.append(
                {
                    "path": "/lines",
                    "code": "too_few_lines",
                    "message": _MESSAGES["too_few_lines"],
                }
            )

        amounts: list[tuple[Decimal, Decimal]] = []
        decidable = True
        seen_line_ids: set[str] = set()

        for index, line in enumerate(lines):
            base = f"/lines/{index}"
            if not isinstance(line, dict) or isinstance(line, bool):
                errors.append(
                    {
                        "path": base,
                        "code": "invalid_type",
                        "message": _MESSAGES["invalid_type"],
                    }
                )
                decidable = False
                continue

            for key in line:
                if key not in _LINE_FIELDS:
                    errors.append(
                        {
                            "path": f"{base}{_pointer(key)}",
                            "code": "unknown_field",
                            "message": _MESSAGES["unknown_field"],
                        }
                    )

            self._validate_line_field(line, "line_id", base, errors)
            line_id = line.get("line_id")
            if _is_str(line_id) and line_id != "":
                if line_id in seen_line_ids:
                    errors.append(
                        {
                            "path": f"{base}/line_id",
                            "code": "duplicate_line_id",
                            "message": _MESSAGES["duplicate_line_id"],
                        }
                    )
                else:
                    seen_line_ids.add(line_id)

            self._validate_line_field(line, "account_code", base, errors)

            debit = self._validate_amount(line, "debit", base, errors)
            credit = self._validate_amount(line, "credit", base, errors)
            if debit is None or credit is None:
                decidable = False
                continue

            if not (
                (debit > 0 and credit == 0) or (debit == 0 and credit > 0)
            ):
                errors.append(
                    {
                        "path": base,
                        "code": "invalid_side",
                        "message": _MESSAGES["invalid_side"],
                    }
                )
            amounts.append((debit, credit))

        return amounts, decidable

    def _validate_line_field(
        self,
        line: dict,
        field: str,
        base: str,
        errors: list[dict[str, str]],
    ) -> None:
        if field not in line:
            code = "required"
        elif not _is_str(line[field]):
            code = "invalid_type"
        elif line[field] == "":
            code = "blank_value"
        else:
            return
        errors.append(
            {
                "path": f"{base}/{field}",
                "code": code,
                "message": _MESSAGES[code],
            }
        )

    def _validate_amount(
        self,
        line: dict,
        field: str,
        base: str,
        errors: list[dict[str, str]],
    ) -> Decimal | None:
        path = f"{base}/{field}"
        if field not in line:
            code = "required"
        elif not _is_str(line[field]):
            code = "invalid_type"
        elif line[field] == "":
            code = "blank_value"
        elif not _AMOUNT_RE.match(line[field]):
            code = "invalid_amount"
        else:
            return Decimal(line[field])
        errors.append(
            {"path": path, "code": code, "message": _MESSAGES[code]}
        )
        return None
