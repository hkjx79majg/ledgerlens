"""无状态现金流量表生成。

请求在试算平衡表/财务报表的财务输入之上新增两个顶层字段：

- ``cash_account_codes``：非空、无重复的科目代码数组，且每个科目必须存在、
  启用并属于资产类（现金及现金等价物科目）。
- ``entry_activities``：与 ``entries`` 等长的数组，元素仅可为
  ``operating``/``investing``/``financing`` 或 null。

口径：指定现金科目的期初余额与每张凭证的现金变动均按「借方减贷方」计算，
正为流入、负为流出；父子科目不滚算，只累加被显式指定的科目。非零现金变动
的凭证必须给出活动分类，零变动凭证必须为 null。

字段级与跨对象校验、错误结构、状态码与排序规则沿用 trial_balance.py 的
``_prepare``；纯函数实现：不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .journal import validate_journal_entry
from .trial_balance import _AMOUNT_RE, _ZERO, _add, _money, _prepare

_CASH_FIELDS = ("cash_account_codes", "entry_activities")
_ACTIVITIES = ("operating", "investing", "financing")


def _validate_cash_account_codes(
    payload: dict[str, Any],
    chart_accounts: dict[str, dict[str, Any]] | None,
    errors: list[dict[str, str]],
) -> list[str] | None:
    """校验 cash_account_codes，合法时返回代码列表，否则返回 None。

    依次执行：数组类型 -> 非空 -> 元素类型/空白 -> 重复 -> 科目存在、启用、
    资产类（跨对象规则仅在 chart 整体有效时推导）。
    """
    if "cash_account_codes" not in payload:
        _add(errors, "/cash_account_codes", "required", "cash_account_codes is required")
        return None
    raw = payload["cash_account_codes"]
    if not isinstance(raw, list):
        _add(
            errors,
            "/cash_account_codes",
            "invalid_type",
            "cash_account_codes must be an array",
        )
        return None
    if len(raw) == 0:
        _add(
            errors,
            "/cash_account_codes",
            "too_few_cash_accounts",
            "cash_account_codes must contain at least one account code",
        )
        return None

    codes: list[str | None] = []
    seen: set[str] = set()
    duplicate_indices: set[int] = set()
    structure_ok = True
    for index, value in enumerate(raw):
        path = f"/cash_account_codes/{index}"
        if not isinstance(value, str):
            _add(errors, path, "invalid_type", "cash account code must be a string")
            structure_ok = False
            codes.append(None)
            continue
        if value == "":
            _add(errors, path, "blank_value", "cash account code must not be blank")
            structure_ok = False
            codes.append(None)
            continue
        if value in seen:
            _add(
                errors,
                path,
                "duplicate_cash_account",
                f"cash account code {value!r} is duplicated",
            )
            structure_ok = False
            duplicate_indices.add(index)
        else:
            seen.add(value)
        codes.append(value)

    # 跨对象规则依赖有效科目体系：未知、停用、非资产类依次只报一个；
    # 重复出现的元素本身已有 duplicate_cash_account，不再派生引用错误。
    reference_ok = structure_ok and chart_accounts is not None
    if chart_accounts is not None:
        for index, code in enumerate(codes):
            if code is None or index in duplicate_indices:
                continue
            path = f"/cash_account_codes/{index}"
            account = chart_accounts.get(code)
            if account is None:
                _add(
                    errors,
                    path,
                    "unknown_account",
                    f"account_code {code!r} does not exist in the chart",
                )
                reference_ok = False
            elif account["active"] is not True:
                _add(errors, path, "inactive_account", f"account {code!r} is inactive")
                reference_ok = False
            elif account["type"] != "asset":
                _add(
                    errors,
                    path,
                    "cash_account_not_asset",
                    f"cash account {code!r} must be an asset account",
                )
                reference_ok = False

    if not reference_ok:
        return None
    return codes


def _validate_entry_activities(
    payload: dict[str, Any],
    errors: list[dict[str, str]],
) -> tuple[list[Any] | None, bool]:
    """校验 entry_activities，返回 (activities, structurally_ok)。

    依次执行：数组类型 -> 长度（仅当 entries 为数组时推导）-> 元素取值。
    structurally_ok 要求数组合法且长度已确认匹配，可供后续关联推导使用。
    """
    if "entry_activities" not in payload:
        _add(errors, "/entry_activities", "required", "entry_activities is required")
        return None, False
    raw = payload["entry_activities"]
    if not isinstance(raw, list):
        _add(
            errors,
            "/entry_activities",
            "invalid_type",
            "entry_activities must be an array",
        )
        return None, False

    entries = payload.get("entries")
    length_ok = isinstance(entries, list)
    if length_ok and len(raw) != len(entries):
        _add(
            errors,
            "/entry_activities",
            "activity_count_mismatch",
            "entry_activities must have the same length as entries",
        )
        length_ok = False

    for index, value in enumerate(raw):
        if value is not None and (
            not isinstance(value, str) or value not in _ACTIVITIES
        ):
            _add(
                errors,
                f"/entry_activities/{index}",
                "invalid_cash_flow_activity",
                f"activity must be one of {', '.join(_ACTIVITIES)} or null",
            )

    return raw, length_ok


def _entry_cash_change(entry: Any, cash_codes: set[str]) -> Decimal | None:
    """计算一张凭证在指定现金科目上的借方减贷方变动；无法可靠计算返回 None。"""
    if not isinstance(entry, dict):
        return None
    lines = entry.get("lines")
    if not isinstance(lines, list):
        return None
    change = _ZERO
    for line in lines:
        if not isinstance(line, dict):
            return None
        code = line.get("account_code")
        if not isinstance(code, str) or code not in cash_codes:
            continue
        debit = line.get("debit")
        credit = line.get("credit")
        if not isinstance(debit, str) or not isinstance(credit, str):
            return None
        if _AMOUNT_RE.fullmatch(debit) is None or _AMOUNT_RE.fullmatch(credit) is None:
            return None
        change += Decimal(debit) - Decimal(credit)
    return change


def generate_cash_flow_statement(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成现金流量表，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
    errors, context = _prepare(payload, extra_fields=_CASH_FIELDS)

    chart_accounts = context["chart_accounts"] if context is not None else None
    cash_codes = _validate_cash_account_codes(payload, chart_accounts, errors)
    activities, activities_ok = _validate_entry_activities(payload, errors)

    # 关联推导仅在各依赖字段均有效时进行：现金科目集合可用、活动数组等长、
    # 对应凭证本身通过复式记账校验（现金行金额必然可解析）。
    entries = payload.get("entries")
    if (
        cash_codes is not None
        and activities_ok
        and isinstance(entries, list)
        and isinstance(activities, list)
    ):
        cash_set = set(cash_codes)
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            entry_status, _ = validate_journal_entry(entry)
            if entry_status != 200:
                continue
            change = _entry_cash_change(entry, cash_set)
            if change is None:
                continue
            activity = activities[index]
            path = f"/entry_activities/{index}"
            if change != _ZERO and activity is None:
                _add(
                    errors,
                    path,
                    "missing_cash_flow_activity",
                    "a cash flow activity is required when the voucher moves cash",
                )
            elif change == _ZERO and activity is not None:
                _add(
                    errors,
                    path,
                    "activity_without_cash_change",
                    "activity must be null when the voucher has no net cash change",
                )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：期初现金、逐凭证分类变动 ----
    opening_by_code = context["opening_by_code"]
    beginning_cash_balance = _ZERO
    for code in cash_codes:
        opening = opening_by_code.get(code)
        if opening is not None:
            beginning_cash_balance += opening["debit"] - opening["credit"]

    cash_set = set(cash_codes)
    sections: dict[str, dict[str, Any]] = {
        activity: {"items": [], "total": _ZERO} for activity in _ACTIVITIES
    }
    net_cash_change = _ZERO
    for index, entry in enumerate(entries):
        change = _entry_cash_change(entry, cash_set)
        if change == _ZERO:
            continue
        activity = activities[index]
        item = {
            "voucher_id": entry["voucher_id"],
            "posting_date": entry["posting_date"],
            "amount": _money(change),
        }
        sections[activity]["items"].append(item)
        sections[activity]["total"] += change
        net_cash_change += change

    ending_cash_balance = beginning_cash_balance + net_cash_change

    cash_flow_statement: dict[str, Any] = {
        activity: {
            "items": sections[activity]["items"],
            "total": _money(sections[activity]["total"]),
        }
        for activity in _ACTIVITIES
    }
    cash_flow_statement["beginning_cash_balance"] = _money(beginning_cash_balance)
    cash_flow_statement["net_cash_change"] = _money(net_cash_change)
    cash_flow_statement["ending_cash_balance"] = _money(ending_cash_balance)

    return 200, {
        "valid": True,
        "chart_id": context["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": context["currency"],
        "cash_flow_statement": cash_flow_statement,
    }
