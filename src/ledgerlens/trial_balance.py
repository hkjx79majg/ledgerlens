"""无状态试算平衡表生成。

由期初余额与期间凭证生成试算平衡表。纯函数实现：不落盘、不保留
跨请求状态，相同输入必然得到相同输出。科目体系与凭证的字段级校验
复用 chart.py 与 journal.py（错误路径分别加 /chart 与 /entries/{i}
前缀）；本模块只新增跨对象错误码。字段无效时不派生依赖它的错误。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .journal import _check_amount, _check_string, validate_journal_entry

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_REQUEST_FIELDS = ("period_start", "period_end", "currency", "chart", "opening_balances", "entries")
_OPENING_FIELDS = ("account_code", "debit", "credit")
_AMOUNT_KEYS = (
    "opening_debit",
    "opening_credit",
    "period_debit",
    "period_credit",
    "ending_debit",
    "ending_credit",
)

_CENT = Decimal("0.01")
_ZERO = Decimal("0")


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _add(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _parse_date(value: Any) -> date | None:
    """value 为真实 YYYY-MM-DD 日历日期时返回 date，否则返回 None。"""
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_date(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> date | None:
    """校验必填日期字段；合法返回 date，否则记录错误并返回 None。"""
    value = _check_string(errors, obj, key, path)
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        _add(
            errors,
            path,
            "invalid_date",
            f"{key} must be a valid calendar date in YYYY-MM-DD format",
        )
    return parsed


def _fmt(value: Decimal) -> str:
    """金额统一输出两位小数。"""
    return str(value.quantize(_CENT))


def generate_trial_balance(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """由期初余额与期间凭证生成试算平衡表，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- 期间与币种 ----
    period_start = _check_date(errors, payload, "period_start", "/period_start")
    period_end = _check_date(errors, payload, "period_end", "/period_end")
    period_valid = period_start is not None and period_end is not None
    if period_valid and period_start > period_end:
        _add(
            errors,
            "/period_start",
            "invalid_period",
            "period_start must not be after period_end",
        )
        # 区间无意义，不再派生 posting_date_out_of_period。
        period_valid = False

    currency = _check_string(errors, payload, "currency", "/currency")
    currency_valid = currency is not None and _CURRENCY_RE.fullmatch(currency) is not None
    if currency is not None and not currency_valid:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )

    # ---- 科目体系：字段级校验复用 chart.py，错误路径加 /chart 前缀 ----
    chart_obj: dict[str, Any] | None = None
    if "chart" not in payload:
        _add(errors, "/chart", "required", "chart is required")
    elif not isinstance(payload["chart"], dict):
        _add(errors, "/chart", "invalid_type", "chart must be an object")
    else:
        chart_obj = payload["chart"]
        _, chart_body = validate_chart_of_accounts(chart_obj)
        for err in chart_body.get("errors", []):
            _add(errors, "/chart" + err["path"], err["code"], err["message"])

    # chart.effective_date 不得晚于 period_start（两者均为有效日期时才推导）。
    effective = _parse_date(chart_obj.get("effective_date")) if chart_obj is not None else None
    if effective is not None and period_start is not None and effective > period_start:
        _add(
            errors,
            "/chart/effective_date",
            "chart_not_effective",
            "effective_date must not be later than period_start",
        )

    # 科目引用表：仅收集字段本身有效的 code/active；accounts 不是数组时
    # 科目集不可知，跳过 unknown_account/inactive_account 推导。
    active_by_code: dict[str, bool | None] = {}
    refs_known = False
    if chart_obj is not None and isinstance(chart_obj.get("accounts"), list):
        refs_known = True
        for account in chart_obj["accounts"]:
            if not isinstance(account, dict):
                continue
            code = account.get("code")
            if not isinstance(code, str) or code == "" or code in active_by_code:
                continue
            active = account.get("active")
            active_by_code[code] = active if isinstance(active, bool) else None

    def check_account_ref(code: str, path: str) -> None:
        if not refs_known:
            return
        if code not in active_by_code:
            _add(
                errors,
                path,
                "unknown_account",
                f"account_code {code!r} does not reference an account in the chart",
            )
        elif active_by_code[code] is False:
            _add(
                errors,
                path,
                "inactive_account",
                f"account_code {code!r} references an inactive account",
            )

    # ---- 期初余额 ----
    opening_amounts_ok = True
    if "opening_balances" not in payload:
        _add(errors, "/opening_balances", "required", "opening_balances is required")
    elif not isinstance(payload["opening_balances"], list):
        _add(errors, "/opening_balances", "invalid_type", "opening_balances must be an array")
    else:
        seen_codes: set[str] = set()
        for index, item in enumerate(payload["opening_balances"]):
            base = f"/opening_balances/{index}"
            if not isinstance(item, dict):
                _add(errors, base, "invalid_type", "opening balance must be an object")
                opening_amounts_ok = False
                continue
            for key in item:
                if key not in _OPENING_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            code = _check_string(errors, item, "account_code", f"{base}/account_code")
            if code is not None:
                if code in seen_codes:
                    _add(
                        errors,
                        f"{base}/account_code",
                        "duplicate_opening_account",
                        f"account_code {code!r} is duplicated within opening_balances",
                    )
                else:
                    seen_codes.add(code)
                    check_account_ref(code, f"{base}/account_code")
            debit = _check_amount(errors, item, "debit", f"{base}/debit")
            credit = _check_amount(errors, item, "credit", f"{base}/credit")
            if debit is None or credit is None:
                opening_amounts_ok = False
                continue
            if (debit > 0) == (credit > 0):
                _add(
                    errors,
                    base,
                    "invalid_side",
                    "exactly one of debit and credit must be greater than zero",
                )
                opening_amounts_ok = False
        # 期初借贷总额平衡：仅在全部期初项金额与方向有效时推导。
        if opening_amounts_ok:
            debit_total = Decimal(0)
            credit_total = Decimal(0)
            for item in payload["opening_balances"]:
                debit_total += Decimal(item["debit"])
                credit_total += Decimal(item["credit"])
            if debit_total != credit_total:
                _add(
                    errors,
                    "/opening_balances",
                    "unbalanced_opening_balances",
                    "opening debit and credit totals must balance",
                )

    # ---- 期间凭证：字段级校验复用 journal.py，错误路径加 /entries/{i} 前缀 ----
    if "entries" not in payload:
        _add(errors, "/entries", "required", "entries is required")
    elif not isinstance(payload["entries"], list):
        _add(errors, "/entries", "invalid_type", "entries must be an array")
    else:
        for index, entry in enumerate(payload["entries"]):
            base = f"/entries/{index}"
            if not isinstance(entry, dict):
                _add(errors, base, "invalid_type", "entry must be an object")
                continue
            status, entry_body = validate_journal_entry(entry)
            if "errors" in entry_body:
                for err in entry_body["errors"]:
                    _add(errors, base + err["path"], err["code"], err["message"])
            elif status != 200:
                _add(
                    errors,
                    base,
                    entry_body["code"],
                    "debit and credit totals must balance",
                )
            # 跨对象校验：仅在其依赖的字段各自有效时推导。
            entry_currency = entry.get("currency")
            if (
                currency_valid
                and isinstance(entry_currency, str)
                and _CURRENCY_RE.fullmatch(entry_currency) is not None
                and entry_currency != currency
            ):
                _add(
                    errors,
                    f"{base}/currency",
                    "currency_mismatch",
                    f"currency must match the request currency {currency!r}",
                )
            posting = _parse_date(entry.get("posting_date"))
            if (
                posting is not None
                and period_valid
                and not period_start <= posting <= period_end
            ):
                _add(
                    errors,
                    f"{base}/posting_date",
                    "posting_date_out_of_period",
                    f"posting_date must be within the period {period_start}..{period_end}",
                )
            lines = entry.get("lines")
            if isinstance(lines, list):
                for line_index, line in enumerate(lines):
                    if not isinstance(line, dict):
                        continue
                    line_code = line.get("account_code")
                    if isinstance(line_code, str) and line_code != "":
                        check_account_ref(
                            line_code, f"{base}/lines/{line_index}/account_code"
                        )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：计算试算平衡表（此时输入全部有效）----
    opening_debit: dict[str, Decimal] = {}
    opening_credit: dict[str, Decimal] = {}
    for item in payload["opening_balances"]:
        opening_debit[item["account_code"]] = Decimal(item["debit"])
        opening_credit[item["account_code"]] = Decimal(item["credit"])

    period_debit: dict[str, Decimal] = {}
    period_credit: dict[str, Decimal] = {}
    for entry in payload["entries"]:
        for line in entry["lines"]:
            line_code = line["account_code"]
            period_debit[line_code] = period_debit.get(line_code, _ZERO) + Decimal(line["debit"])
            period_credit[line_code] = period_credit.get(line_code, _ZERO) + Decimal(
                line["credit"]
            )

    # 全部科目按 code 字典序输出；每行只计自身余额，父子不重复汇总。
    rows: list[dict[str, Any]] = []
    totals = {key: _ZERO for key in _AMOUNT_KEYS}
    for account in sorted(chart_obj["accounts"], key=lambda item: item["code"]):
        code = account["code"]
        values = {
            "opening_debit": opening_debit.get(code, _ZERO),
            "opening_credit": opening_credit.get(code, _ZERO),
            "period_debit": period_debit.get(code, _ZERO),
            "period_credit": period_credit.get(code, _ZERO),
        }
        # 期末 = 期初净额 + 期间净发生额，抵销后仅落一侧；零额两侧均为 0.00。
        net = (
            values["opening_debit"]
            - values["opening_credit"]
            + values["period_debit"]
            - values["period_credit"]
        )
        values["ending_debit"] = net if net > 0 else _ZERO
        values["ending_credit"] = -net if net < 0 else _ZERO
        for key in _AMOUNT_KEYS:
            totals[key] += values[key]
        rows.append(
            {
                "code": code,
                "name": account["name"],
                "type": account["type"],
                **{key: _fmt(values[key]) for key in _AMOUNT_KEYS},
            }
        )

    return 200, {
        "valid": True,
        "chart_id": chart_obj["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": payload["currency"],
        "rows": rows,
        "totals": {key: _fmt(totals[key]) for key in _AMOUNT_KEYS},
    }
