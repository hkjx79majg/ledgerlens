"""无状态现金流量表生成。

请求在财务报表输入（period_start、period_end、currency、chart、
opening_balances、entries）之上新增两个顶层字段：

- cash_account_codes：非空、无重复的现金科目代码数组；科目须存在、启用且
  为 asset 类。
- entry_activities：与 entries 等长的活动标注数组，元素仅为 operating、
  investing、financing 或 null。

既有字段的字段级与跨对象校验完全复用 trial_balance.py 的 `_prepare`；依赖
无效时不派生关联错误。现金变动按指定现金科目的借方减贷方计算，正为流入、
负为流出，不滚算父子科目。纯函数实现：不落盘、不保留跨请求状态，相同输入
必然得到相同输出。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .trial_balance import _ZERO, _add, _money, _prepare

_EXTRA_FIELDS = ("cash_account_codes", "entry_activities")
_ACTIVITIES = ("operating", "investing", "financing")


def _entry_cash_change(entry: dict[str, Any], cash_codes: set[str]) -> Decimal:
    """单张有效凭证的现金净变动：现金科目借方合计减贷方合计。"""
    change = _ZERO
    for line in entry["lines"]:
        if line["account_code"] in cash_codes:
            change += Decimal(line["debit"]) - Decimal(line["credit"])
    return change


def generate_cash_flow_statement(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成现金流量表，返回 (HTTP 状态码, 响应体)。"""
    errors, context = _prepare(payload, extra_fields=_EXTRA_FIELDS)
    cash_flow_errors: list[dict[str, str]] = []
    chart_accounts = context["chart_accounts"]

    # ---- cash_account_codes：非空、无重复的科目代码数组 ----
    cash_ok = False
    cash_codes: list[str] = []
    if "cash_account_codes" not in payload:
        _add(
            cash_flow_errors,
            "/cash_account_codes",
            "required",
            "cash_account_codes is required",
        )
    elif not isinstance(payload["cash_account_codes"], list):
        _add(
            cash_flow_errors,
            "/cash_account_codes",
            "invalid_type",
            "cash_account_codes must be an array",
        )
    else:
        raw_codes = payload["cash_account_codes"]
        cash_ok = True
        if len(raw_codes) < 1:
            _add(
                cash_flow_errors,
                "/cash_account_codes",
                "too_few_cash_accounts",
                "cash_account_codes must contain at least one account code",
            )
            cash_ok = False
        seen_codes: set[str] = set()
        candidates: list[tuple[int, str]] = []
        for index, code in enumerate(raw_codes):
            path = f"/cash_account_codes/{index}"
            if not isinstance(code, str):
                _add(cash_flow_errors, path, "invalid_type", "cash account code must be a string")
                cash_ok = False
                continue
            if code == "":
                _add(cash_flow_errors, path, "blank_value", "cash account code must not be blank")
                cash_ok = False
                continue
            if code in seen_codes:
                _add(
                    cash_flow_errors,
                    path,
                    "duplicate_cash_account",
                    f"cash account code {code!r} is duplicated",
                )
                cash_ok = False
                continue
            seen_codes.add(code)
            candidates.append((index, code))
            cash_codes.append(code)
        # 存在性、启用状态与类别仅在 chart 整体有效时推导，按此顺序取首个命中。
        if chart_accounts is None:
            cash_ok = False
        else:
            for index, code in candidates:
                path = f"/cash_account_codes/{index}"
                account = chart_accounts.get(code)
                if account is None:
                    _add(
                        cash_flow_errors,
                        path,
                        "unknown_account",
                        f"account_code {code!r} does not exist in the chart",
                    )
                    cash_ok = False
                elif account["active"] is not True:
                    _add(
                        cash_flow_errors,
                        path,
                        "inactive_account",
                        f"account {code!r} is inactive",
                    )
                    cash_ok = False
                elif account["type"] != "asset":
                    _add(
                        cash_flow_errors,
                        path,
                        "cash_account_not_asset",
                        f"cash account {code!r} must be an asset account",
                    )
                    cash_ok = False

    # ---- entry_activities：与 entries 等长的活动标注数组 ----
    activities: list[Any] | None = None
    if "entry_activities" not in payload:
        _add(
            cash_flow_errors,
            "/entry_activities",
            "required",
            "entry_activities is required",
        )
    elif not isinstance(payload["entry_activities"], list):
        _add(
            cash_flow_errors,
            "/entry_activities",
            "invalid_type",
            "entry_activities must be an array",
        )
    else:
        activities = payload["entry_activities"]
        element_valid: list[bool] = []
        for index, activity in enumerate(activities):
            valid = activity is None or activity in _ACTIVITIES
            if not valid:
                _add(
                    cash_flow_errors,
                    f"/entry_activities/{index}",
                    "invalid_cash_flow_activity",
                    f"activity must be one of {', '.join(_ACTIVITIES)} or null",
                )
            element_valid.append(valid)
        entries_raw = payload.get("entries")
        if isinstance(entries_raw, list):
            if len(activities) != len(entries_raw):
                _add(
                    cash_flow_errors,
                    "/entry_activities",
                    "activity_count_mismatch",
                    "entry_activities must have the same length as entries",
                )
            elif cash_ok:
                # 现金变动与活动标注的一致性：仅在现金科目集合、对应凭证与
                # 活动元素各自有效时推导，依赖无效不派生关联错误。
                entry_valid = {
                    node["index"]: node["valid"] for node in context["entry_nodes"]
                }
                cash_set = set(cash_codes)
                for index, entry in enumerate(entries_raw):
                    if not element_valid[index] or not entry_valid.get(index, False):
                        continue
                    change = _entry_cash_change(entry, cash_set)
                    activity = activities[index]
                    if activity is None and change != 0:
                        _add(
                            cash_flow_errors,
                            f"/entry_activities/{index}",
                            "missing_cash_flow_activity",
                            "entry has a non-zero cash change and requires an activity",
                        )
                    elif activity is not None and change == 0:
                        _add(
                            cash_flow_errors,
                            f"/entry_activities/{index}",
                            "activity_without_cash_change",
                            "entry has no cash change and must be annotated with null",
                        )

    if errors or cash_flow_errors:
        merged = errors + cash_flow_errors
        merged.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": merged}

    # ---- 成功：期初现金 + 按活动分区的期间现金变动 = 期末现金 ----
    cash_set = set(payload["cash_account_codes"])
    opening_by_code = context["opening_by_code"]
    beginning = _ZERO
    for code in cash_set:
        opening = opening_by_code.get(code)
        if opening is not None:
            beginning += opening["debit"] - opening["credit"]

    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in _ACTIVITIES}
    totals = {name: _ZERO for name in _ACTIVITIES}
    for entry, activity in zip(payload["entries"], payload["entry_activities"]):
        change = _entry_cash_change(entry, cash_set)
        if change == 0:
            continue
        sections[activity].append(
            {
                "voucher_id": entry["voucher_id"],
                "posting_date": entry["posting_date"],
                "amount": _money(change),
            }
        )
        totals[activity] += change

    net_change = sum(totals.values(), _ZERO)
    ending = beginning + net_change

    return 200, {
        "valid": True,
        "chart_id": context["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": context["currency"],
        "cash_flow_statement": {
            name: {"items": sections[name], "total": _money(totals[name])}
            for name in _ACTIVITIES
        },
        "beginning_cash_balance": _money(beginning),
        "net_cash_change": _money(net_change),
        "ending_cash_balance": _money(ending),
    }
