"""无状态期末损益结转（Period Close）。

请求在试算平衡表/财务报表的财务输入之上新增两个顶层非空字符串字段：

- ``retained_earnings_account_code``：留存收益科目，必须存在、启用且属于
  equity 类，依次报 unknown_account、inactive_account、
  retained_earnings_not_equity。
- ``closing_voucher_id``：结账凭证号，不得与任一字段有效的输入凭证
  ``voucher_id`` 相同，否则报 duplicate_voucher_id。

另有跨对象规则：revenue/expense 科目（临时科目）的期初余额必须为零，
非零时在对应 ``/opening_balances/{i}`` 报 nonzero_temporary_opening_balance。
依赖字段无效时不派生上述关联错误。

结账只取本期发生额：非零收入按「贷方减借方」的反方向清零，非零费用按
「借方减贷方」的反方向清零，两类合计之差为 net_income；正数贷记留存收益，
负数借记，零值不生成留存收益行。没有非零损益发生额时 closing_entry 为
null，否则借贷相等。next_opening_balances 只含 asset/liability/equity 的
期末净额，留存收益叠加净利润；非零余额抵销后仅落一侧，零余额省略，按
account_code 排序，父子科目仍只计自身。

字段级与跨对象校验、错误结构、状态码与排序规则沿用 trial_balance.py 的
``_prepare``；纯函数实现：不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

from typing import Any

from .trial_balance import _ZERO, _add, _check_nonempty_string, _money, _prepare

_PERIOD_CLOSE_FIELDS = ("retained_earnings_account_code", "closing_voucher_id")


def generate_period_close(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成期末结账凭证与下一期期初余额，返回 (HTTP 状态码, 响应体)。"""
    errors, context = _prepare(payload, extra_fields=_PERIOD_CLOSE_FIELDS)

    # ---- 两个新增必填非空字符串字段 ----
    retained_code = _check_nonempty_string(
        errors, payload, "retained_earnings_account_code", "/retained_earnings_account_code"
    )
    closing_voucher_id = _check_nonempty_string(
        errors, payload, "closing_voucher_id", "/closing_voucher_id"
    )

    chart_accounts = context["chart_accounts"] if context is not None else None

    # ---- 留存收益科目：存在 -> 启用 -> equity，依次只报一个 ----
    if retained_code is not None and chart_accounts is not None:
        account = chart_accounts.get(retained_code)
        if account is None:
            _add(
                errors,
                "/retained_earnings_account_code",
                "unknown_account",
                f"account_code {retained_code!r} does not exist in the chart",
            )
        elif account["active"] is not True:
            _add(
                errors,
                "/retained_earnings_account_code",
                "inactive_account",
                f"account {retained_code!r} is inactive",
            )
        elif account["type"] != "equity":
            _add(
                errors,
                "/retained_earnings_account_code",
                "retained_earnings_not_equity",
                f"retained earnings account {retained_code!r} must be an equity account",
            )

    # ---- 临时科目期初余额必须为零：仅在整体输入有效时推导 ----
    if context is not None:
        for item in context["opening_by_code"].values():
            code = item["code"]
            if chart_accounts[code]["type"] not in ("revenue", "expense"):
                continue
            if item["debit"] != _ZERO or item["credit"] != _ZERO:
                _add(
                    errors,
                    item["base"],
                    "nonzero_temporary_opening_balance",
                    f"temporary account {code!r} must have a zero opening balance",
                )

    # ---- 结账凭证号不得与输入凭证重复：仅在整体输入有效时推导 ----
    if closing_voucher_id is not None and context is not None:
        existing_ids = {entry["voucher_id"] for entry in payload["entries"]}
        if closing_voucher_id in existing_ids:
            _add(
                errors,
                "/closing_voucher_id",
                "duplicate_voucher_id",
                f"voucher_id {closing_voucher_id!r} is already used by an entry in the request",
            )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：结清损益类科目，差额计入留存收益 ----
    opening_by_code = context["opening_by_code"]
    period_by_code = context["period_by_code"]

    closing_lines: list[dict[str, Any]] = []
    total_revenue = _ZERO
    total_expense = _ZERO
    # 先按 account_code 汇总全部非零损益行，再统一编号。
    pnl_items: list[tuple[str, Decimal]] = []
    for code in sorted(chart_accounts):
        account_type = chart_accounts[code]["type"]
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        if account_type == "revenue":
            amount = period_credit - period_debit
            total_revenue += amount
            if amount != _ZERO:
                pnl_items.append((code, amount))
        elif account_type == "expense":
            amount = period_debit - period_credit
            total_expense += amount
            if amount != _ZERO:
                pnl_items.append((code, amount))
    pnl_items.sort(key=lambda item: item[0])

    for code, amount in pnl_items:
        # 收入正值（贷方余额）借记清零；费用正值（借方余额）贷记清零；
        # 负值方向相反。金额始终非负且仅一侧大于零。
        is_revenue = chart_accounts[code]["type"] == "revenue"
        positive = amount > _ZERO
        debit_positive = positive == is_revenue
        if debit_positive:
            debit, credit = abs(amount), _ZERO
        else:
            debit, credit = _ZERO, abs(amount)
        closing_lines.append(
            {
                "line_id": f"close-{len(closing_lines) + 1}",
                "account_code": code,
                "debit": _money(debit),
                "credit": _money(credit),
            }
        )

    net_income = total_revenue - total_expense
    if net_income > _ZERO:
        closing_lines.append(
            {
                "line_id": f"close-{len(closing_lines) + 1}",
                "account_code": retained_code,
                "debit": _money(_ZERO),
                "credit": _money(net_income),
            }
        )
    elif net_income < _ZERO:
        closing_lines.append(
            {
                "line_id": f"close-{len(closing_lines) + 1}",
                "account_code": retained_code,
                "debit": _money(-net_income),
                "credit": _money(_ZERO),
            }
        )

    closing_entry: dict[str, Any] | None = None
    if pnl_items:
        closing_entry = {
            "voucher_id": closing_voucher_id,
            "posting_date": payload["period_end"],
            "currency": context["currency"],
            "lines": closing_lines,
        }

    # ---- 下一期期初：资产/负债/权益期末净额，留存收益叠加净利润 ----
    next_opening_balances: list[dict[str, Any]] = []
    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        if account["type"] not in ("asset", "liability", "equity"):
            continue
        opening = opening_by_code.get(code)
        opening_debit = opening["debit"] if opening is not None else _ZERO
        opening_credit = opening["credit"] if opening is not None else _ZERO
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        net = (opening_debit - opening_credit) + (period_debit - period_credit)
        if code == retained_code:
            # 净利润贷记留存收益（借-贷净额减少），净亏损方向相反。
            net -= net_income
        if net == _ZERO:
            continue
        if net > _ZERO:
            debit, credit = net, _ZERO
        else:
            debit, credit = _ZERO, -net
        next_opening_balances.append(
            {
                "account_code": code,
                "debit": _money(debit),
                "credit": _money(credit),
            }
        )

    return 200, {
        "valid": True,
        "chart_id": context["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": context["currency"],
        "net_income": _money(net_income),
        "closing_entry": closing_entry,
        "next_opening_balances": next_opening_balances,
    }
