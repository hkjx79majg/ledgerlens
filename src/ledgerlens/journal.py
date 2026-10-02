"""无状态复式记账凭证校验。

纯函数实现：不落盘、不保留跨请求状态，相同输入必然得到相同输出。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

# 金额：无符号、无指数、最多两位小数的十进制字符串。
_AMOUNT_RE = re.compile(r"\d+(\.\d{1,2})?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_VOUCHER_FIELDS = ("voucher_id", "posting_date", "currency", "lines")
_LINE_FIELDS = ("line_id", "account_code", "debit", "credit")

_CENT = Decimal("0.01")


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _add(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _check_string(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> str | None:
    """校验非空字符串字段；可用时返回字符串值，否则记录错误并返回 None。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a string")
        return None
    if value == "":
        _add(errors, path, "blank_value", f"{key} must not be blank")
        return None
    return value


def _check_amount(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验金额字段；合法时返回精确十进制值，否则记录错误并返回 None。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a decimal string")
        return None
    if value == "":
        _add(errors, path, "blank_value", f"{key} must not be blank")
        return None
    if _AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be an unsigned decimal string with at most two fraction digits",
        )
        return None
    return Decimal(value)


def validate_journal_entry(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """校验一张凭证，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    for key in payload:
        if key not in _VOUCHER_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    _check_string(errors, payload, "voucher_id", "/voucher_id")

    posting_date = _check_string(errors, payload, "posting_date", "/posting_date")
    if posting_date is not None:
        if _DATE_RE.fullmatch(posting_date) is None:
            _add(
                errors,
                "/posting_date",
                "invalid_date",
                "posting_date must be a valid calendar date in YYYY-MM-DD format",
            )
        else:
            try:
                date.fromisoformat(posting_date)
            except ValueError:
                _add(
                    errors,
                    "/posting_date",
                    "invalid_date",
                    "posting_date must be a valid calendar date in YYYY-MM-DD format",
                )

    currency = _check_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )

    debit_total = Decimal("0")
    credit_total = Decimal("0")

    if "lines" not in payload:
        _add(errors, "/lines", "required", "lines is required")
    elif not isinstance(payload["lines"], list):
        _add(errors, "/lines", "invalid_type", "lines must be an array")
    else:
        lines = payload["lines"]
        if len(lines) < 2:
            _add(errors, "/lines", "too_few_lines", "lines must contain at least two entries")
        seen_line_ids: set[str] = set()
        for index, line in enumerate(lines):
            base = f"/lines/{index}"
            if not isinstance(line, dict):
                _add(errors, base, "invalid_type", "line must be an object")
                continue
            for key in line:
                if key not in _LINE_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            line_id = _check_string(errors, line, "line_id", f"{base}/line_id")
            if line_id is not None:
                if line_id in seen_line_ids:
                    _add(
                        errors,
                        f"{base}/line_id",
                        "duplicate_line_id",
                        f"line_id {line_id!r} is duplicated within the voucher",
                    )
                else:
                    seen_line_ids.add(line_id)
            _check_string(errors, line, "account_code", f"{base}/account_code")
            debit = _check_amount(errors, line, "debit", f"{base}/debit")
            credit = _check_amount(errors, line, "credit", f"{base}/credit")
            if debit is None or credit is None:
                continue
            if (debit > 0) == (credit > 0):
                _add(
                    errors,
                    base,
                    "invalid_side",
                    "exactly one of debit and credit must be greater than zero",
                )
            debit_total += debit
            credit_total += credit

    if errors:
        # 存在金额等结构错误时不追加余额错误。
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    if debit_total != credit_total:
        return 422, {"valid": False, "code": "unbalanced_entry"}

    return 200, {
        "valid": True,
        "debit_total": str(debit_total.quantize(_CENT)),
        "credit_total": str(credit_total.quantize(_CENT)),
    }
