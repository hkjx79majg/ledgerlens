"""无状态试算平衡表生成。

复用已冻结的科目体系校验（chart.py）与凭证校验（journal.py）收集字段级
错误，再在各个字段有效的前提下做跨对象推导：期间先后、科目体系生效日、
币种一致、过账日期落在期间内、科目引用存在且启用、期初科目不重复、期初
借贷平衡。纯函数实现：不落盘、不保留跨请求状态，相同输入必然得到相同输出。
错误结构、状态码与排序规则与两个校验器保持一致。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .journal import validate_journal_entry

# 金额、日期、币种格式与 journal.py 保持一致。
_AMOUNT_RE = re.compile(r"\d+(\.\d{1,2})?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_REQUEST_FIELDS = (
    "period_start",
    "period_end",
    "currency",
    "chart",
    "opening_balances",
    "entries",
)
_OPENING_FIELDS = ("account_code", "debit", "credit")

_CENT = Decimal("0.01")
_ZERO = Decimal("0")


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _add(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _parse_date(value: str) -> date | None:
    """解析真实日历日期；非法返回 None。"""
    if _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_nonempty_string(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> str | None:
    """校验必填非空字符串字段；合法返回字符串值，否则记录错误并返回 None。"""
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


def _check_date(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> date | None:
    """校验必填真实日历日期字段；合法返回 date，否则记录错误并返回 None。"""
    value = _check_nonempty_string(errors, obj, key, path)
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
        return None
    return parsed


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


def _money(value: Decimal) -> str:
    """金额统一输出两位小数。"""
    return str(value.quantize(_CENT))


def _prepare(
    payload: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """校验请求并汇总期初与期间发生额，供试算平衡表与财务报表共用。

    返回 (errors, context)：errors 非空（已按 path、code 排序）时 context 为
    None；否则 context 含 chart_id、currency、chart_accounts、opening_by_code、
    period_by_code。
    """
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- 期间：真实日期且先后有序 ----
    period_start = _check_date(errors, payload, "period_start", "/period_start")
    period_end = _check_date(errors, payload, "period_end", "/period_end")
    period_ok = False
    if period_start is not None and period_end is not None:
        if period_start > period_end:
            _add(
                errors,
                "/period_start",
                "invalid_period",
                "period_start must be on or before period_end",
            )
        else:
            period_ok = True

    # ---- 币种：三位大写字母 ----
    currency = _check_nonempty_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
        currency = None

    # ---- chart：整体复用科目体系校验，错误路径加 /chart 前缀 ----
    chart_accounts: dict[str, dict[str, Any]] | None = None
    chart_id: str | None = None
    if "chart" not in payload:
        _add(errors, "/chart", "required", "chart is required")
    elif not isinstance(payload["chart"], dict):
        _add(errors, "/chart", "invalid_type", "chart must be an object")
    else:
        chart = payload["chart"]
        chart_status, chart_body = validate_chart_of_accounts(chart)
        if chart_status != 200:
            for err in chart_body["errors"]:
                _add(errors, "/chart" + err["path"], err["code"], err["message"])
        else:
            chart_id = chart_body["chart_id"]
            chart_accounts = {account["code"]: account for account in chart["accounts"]}
        # chart_not_effective 仅依赖 effective_date 字段本身与 period_start。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and period_start is not None and effective > period_start:
            _add(
                errors,
                "/chart/effective_date",
                "chart_not_effective",
                "chart effective_date must not be after period_start",
            )

    # ---- opening_balances：逐项字段校验 ----
    opening_items: list[dict[str, Any]] = []
    opening_amounts_ok = True
    if "opening_balances" not in payload:
        _add(errors, "/opening_balances", "required", "opening_balances is required")
        opening_amounts_ok = False
    elif not isinstance(payload["opening_balances"], list):
        _add(errors, "/opening_balances", "invalid_type", "opening_balances must be an array")
        opening_amounts_ok = False
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
            code = _check_nonempty_string(
                errors, item, "account_code", f"{base}/account_code"
            )
            debit = _check_amount(errors, item, "debit", f"{base}/debit")
            credit = _check_amount(errors, item, "credit", f"{base}/credit")
            if debit is None or credit is None:
                opening_amounts_ok = False
            elif (debit > 0) == (credit > 0):
                _add(
                    errors,
                    base,
                    "invalid_side",
                    "exactly one of debit and credit must be greater than zero",
                )
                opening_amounts_ok = False
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
            opening_items.append(
                {"base": base, "code": code, "debit": debit, "credit": credit}
            )

    # 期初借贷总额平衡：仅在每个期初项金额均有效且单侧时推导。
    if opening_amounts_ok:
        opening_debit_total = sum(
            (item["debit"] for item in opening_items), _ZERO
        )
        opening_credit_total = sum(
            (item["credit"] for item in opening_items), _ZERO
        )
        if opening_debit_total != opening_credit_total:
            _add(
                errors,
                "/opening_balances",
                "unbalanced_opening_balances",
                "opening debit total must equal opening credit total",
            )

    # ---- entries：逐张复用凭证校验，错误路径加 /entries/{i} 前缀 ----
    entry_nodes: list[dict[str, Any]] = []
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
            entry_status, entry_body = validate_journal_entry(entry)
            if entry_status != 200:
                if "errors" in entry_body:
                    for err in entry_body["errors"]:
                        _add(errors, base + err["path"], err["code"], err["message"])
                else:
                    _add(
                        errors,
                        base,
                        entry_body["code"],
                        "journal entry is not balanced",
                    )
            # 跨对象错误仅依赖对应字段本身有效。
            entry_currency = entry.get("currency")
            if (
                currency is not None
                and isinstance(entry_currency, str)
                and _CURRENCY_RE.fullmatch(entry_currency) is not None
                and entry_currency != currency
            ):
                _add(
                    errors,
                    f"{base}/currency",
                    "currency_mismatch",
                    "entry currency must match the request currency",
                )
            posting_raw = entry.get("posting_date")
            posting = _parse_date(posting_raw) if isinstance(posting_raw, str) else None
            if (
                period_ok
                and posting is not None
                and not (period_start <= posting <= period_end)
            ):
                _add(
                    errors,
                    f"{base}/posting_date",
                    "posting_date_out_of_period",
                    "posting_date must be within [period_start, period_end]",
                )
            entry_nodes.append({"base": base, "entry": entry})

    # ---- 科目引用：仅当 chart 整体有效时推导存在性与启用状态 ----
    if chart_accounts is not None:
        for item in opening_items:
            code = item["code"]
            if code is None:
                continue
            account = chart_accounts.get(code)
            if account is None:
                _add(
                    errors,
                    f"{item['base']}/account_code",
                    "unknown_account",
                    f"account_code {code!r} does not exist in the chart",
                )
            elif account["active"] is not True:
                _add(
                    errors,
                    f"{item['base']}/account_code",
                    "inactive_account",
                    f"account {code!r} is inactive",
                )
        for node in entry_nodes:
            lines = node["entry"].get("lines")
            if not isinstance(lines, list):
                continue
            for line_index, line in enumerate(lines):
                if not isinstance(line, dict):
                    continue
                code = line.get("account_code")
                if not isinstance(code, str) or code == "":
                    continue
                account = chart_accounts.get(code)
                path = f"{node['base']}/lines/{line_index}/account_code"
                if account is None:
                    _add(
                        errors,
                        path,
                        "unknown_account",
                        f"account_code {code!r} does not exist in the chart",
                    )
                elif account["active"] is not True:
                    _add(
                        errors,
                        path,
                        "inactive_account",
                        f"account {code!r} is inactive",
                    )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return errors, None

    # ---- 汇总期初与期间发生额，供各报表共用 ----
    opening_by_code = {item["code"]: item for item in opening_items}
    period_by_code: dict[str, list[Decimal]] = {}
    for entry in payload["entries"]:
        for line in entry["lines"]:
            bucket = period_by_code.setdefault(line["account_code"], [_ZERO, _ZERO])
            bucket[0] += Decimal(line["debit"])
            bucket[1] += Decimal(line["credit"])

    return [], {
        "chart_id": chart_id,
        "currency": currency,
        "chart_accounts": chart_accounts,
        "opening_by_code": opening_by_code,
        "period_by_code": period_by_code,
    }


def generate_trial_balance(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成试算平衡表，返回 (HTTP 状态码, 响应体)。"""
    errors, context = _prepare(payload)
    if errors:
        return 422, {"valid": False, "errors": errors}

    chart_accounts = context["chart_accounts"]
    opening_by_code = context["opening_by_code"]
    period_by_code = context["period_by_code"]

    # ---- 成功：按科目汇总期初、期间发生与期末 ----
    rows: list[dict[str, Any]] = []
    totals = [_ZERO] * 6
    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        opening = opening_by_code.get(code)
        opening_debit = opening["debit"] if opening is not None else _ZERO
        opening_credit = opening["credit"] if opening is not None else _ZERO
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        # 期末 = 期初净额 + 期间净发生额，抵销后仅落一侧；零额两侧均为 0。
        net = (opening_debit - opening_credit) + (period_debit - period_credit)
        if net > 0:
            ending_debit, ending_credit = net, _ZERO
        elif net < 0:
            ending_debit, ending_credit = _ZERO, -net
        else:
            ending_debit, ending_credit = _ZERO, _ZERO
        # 每行只计自身余额，父子科目不做滚算，totals 直接逐行相加。
        amounts = (
            opening_debit,
            opening_credit,
            period_debit,
            period_credit,
            ending_debit,
            ending_credit,
        )
        for column, amount in enumerate(amounts):
            totals[column] += amount
        rows.append(
            {
                "code": code,
                "name": account["name"],
                "type": account["type"],
                "opening_debit": _money(opening_debit),
                "opening_credit": _money(opening_credit),
                "period_debit": _money(period_debit),
                "period_credit": _money(period_credit),
                "ending_debit": _money(ending_debit),
                "ending_credit": _money(ending_credit),
            }
        )

    return 200, {
        "valid": True,
        "chart_id": context["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": context["currency"],
        "accounts": rows,
        "totals": {
            "opening_debit": _money(totals[0]),
            "opening_credit": _money(totals[1]),
            "period_debit": _money(totals[2]),
            "period_credit": _money(totals[3]),
            "ending_debit": _money(totals[4]),
            "ending_credit": _money(totals[5]),
        },
    }
