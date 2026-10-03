"""无状态期末损益结转（period close）生成。

请求在试算平衡表的财务输入之上新增两个顶层字段：

- ``retained_earnings_account_code``：非空字符串，须指向存在、启用的 equity
  科目，依次以 ``unknown_account``、``inactive_account``、
  ``retained_earnings_not_equity`` 报错。
- ``closing_voucher_id``：非空字符串，不得与任一 ``entries`` 中字段有效的
  ``voucher_id`` 相同，否则报 ``duplicate_voucher_id``。

收入或费用科目带非零期初余额时，在对应 ``/opening_balances/{i}`` 报
``nonzero_temporary_opening_balance``。关联错误只依赖自身字段有效：科目体系
无效时不派生留存收益与期初类错误，期初项本身无效时不派生其余额类错误。

结账只取本期发生额：非零收入按「贷方减借方」的反方向（借方）清零，非零费用
按「借方减贷方」的反方向（贷方）清零；``net_income`` 为收入合计减费用合计，
正数贷记留存收益、负数借记、零值不生成该行。无非零损益发生额时
``closing_entry`` 为 null。``next_opening_balances`` 只保留资产、负债、权益
科目的期末净额（留存收益叠加净利润），零余额省略，父子科目只计自身。

字段级与跨对象校验、错误结构、状态码与排序规则沿用 trial_balance.py 的
``_prepare``；纯函数实现：不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .trial_balance import _AMOUNT_RE, _ZERO, _add, _money, _prepare

_PERIOD_CLOSE_FIELDS = ("retained_earnings_account_code", "closing_voucher_id")
_TEMPORARY_TYPES = ("revenue", "expense")


def _check_required_nonempty_string(
    errors: list[dict[str, str]], payload: dict[str, Any], key: str
) -> str | None:
    """校验新增的必填非空字符串顶层字段；合法返回字符串值。"""
    if key not in payload:
        _add(errors, f"/{key}", "required", f"{key} is required")
        return None
    value = payload[key]
    if not isinstance(value, str):
        _add(errors, f"/{key}", "invalid_type", f"{key} must be a string")
        return None
    if value == "":
        _add(errors, f"/{key}", "blank_value", f"{key} must not be blank")
        return None
    return value


def _valid_chart_accounts(payload: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """科目体系整体有效时返回 code -> account 映射，否则 None。

    与 trial_balance._prepare 内部口径一致，独立于其他请求字段，使留存收益
    与期初类关联错误只依赖科目体系本身是否有效。
    """
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return None
    status, _ = validate_chart_of_accounts(chart)
    if status != 200:
        return None
    return {account["code"]: account for account in chart["accounts"]}


def _opening_amounts(item: Any) -> tuple[Decimal, Decimal] | None:
    """期初项金额字段均为合法金额字符串时返回 (debit, credit)，否则 None。"""
    if not isinstance(item, dict):
        return None
    debit = item.get("debit")
    credit = item.get("credit")
    if not isinstance(debit, str) or not isinstance(credit, str):
        return None
    if _AMOUNT_RE.fullmatch(debit) is None or _AMOUNT_RE.fullmatch(credit) is None:
        return None
    return Decimal(debit), Decimal(credit)


def generate_period_close(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成期末损益结转凭证与下一期期初余额，返回 (HTTP 状态码, 响应体)。"""
    errors, context = _prepare(payload, extra_fields=_PERIOD_CLOSE_FIELDS)

    # ---- 两个新增顶层字段：必填非空字符串 ----
    retained_code = _check_required_nonempty_string(
        errors, payload, "retained_earnings_account_code"
    )
    closing_voucher_id = _check_required_nonempty_string(
        errors, payload, "closing_voucher_id"
    )

    chart_accounts = _valid_chart_accounts(payload)

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

    # ---- 凭证号重复：与任一字段有效的输入 voucher_id 比较；只依赖双方
    #      voucher_id 字段本身有效，不派生自凭证的其他错误。 ----
    if closing_voucher_id is not None:
        entries = payload.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                voucher_id = entry.get("voucher_id")
                if (
                    isinstance(voucher_id, str)
                    and voucher_id != ""
                    and voucher_id == closing_voucher_id
                ):
                    _add(
                        errors,
                        "/closing_voucher_id",
                        "duplicate_voucher_id",
                        "closing_voucher_id must not duplicate an existing entry voucher_id",
                    )
                    break

    # ---- 收入/费用科目不得带非零期初余额：仅当科目体系有效、该期初项
    #      科目引用存在且启用、金额字段有效且单侧时推导。 ----
    if chart_accounts is not None:
        opening_balances = payload.get("opening_balances")
        if isinstance(opening_balances, list):
            seen_codes: set[str] = set()
            for index, item in enumerate(opening_balances):
                if not isinstance(item, dict):
                    continue
                code = item.get("account_code")
                if not isinstance(code, str) or code == "":
                    continue
                account = chart_accounts.get(code)
                if account is None or account["active"] is not True:
                    continue
                amounts = _opening_amounts(item)
                if amounts is None:
                    continue
                debit, credit = amounts
                if (debit > 0) == (credit > 0):
                    continue
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                if account["type"] in _TEMPORARY_TYPES and (
                    debit != _ZERO or credit != _ZERO
                ):
                    _add(
                        errors,
                        f"/opening_balances/{index}",
                        "nonzero_temporary_opening_balance",
                        "revenue and expense accounts must have zero opening balances",
                    )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：结转只取本期发生额 ----
    opening_by_code = context["opening_by_code"]
    period_by_code = context["period_by_code"]

    close_lines: list[dict[str, Any]] = []
    total_revenue = _ZERO
    total_expense = _ZERO
    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        account_type = account["type"]
        # 收入按贷方减借方、费用按借方减贷方合计；net_income 为两者之差。
        if account_type == "revenue":
            total_revenue += period_credit - period_debit
        elif account_type == "expense":
            total_expense += period_debit - period_credit
        else:
            continue
        # 反方向清零：以期内「借方减贷方」的净余额为准，借方余额贷记、
        # 贷方余额借记（收入正常在贷方、费用正常在借方）。
        balance = period_debit - period_credit
        if balance == _ZERO:
            continue
        if balance > 0:
            line = {"account_code": code, "debit": _money(_ZERO), "credit": _money(balance)}
        else:
            line = {"account_code": code, "debit": _money(-balance), "credit": _money(_ZERO)}
        close_lines.append(line)

    net_income = total_revenue - total_expense

    closing_entry: dict[str, Any] | None = None
    if close_lines:
        # 留存收益行置后：正数贷记、负数借记，零值不生成该行。
        retained_line: dict[str, Any] | None = {
            "account_code": retained_code,
            "debit": _money(_ZERO),
            "credit": _money(_ZERO),
        }
        if net_income > 0:
            retained_line["credit"] = _money(net_income)
        elif net_income < 0:
            retained_line["debit"] = _money(-net_income)
        else:
            retained_line = None
        ordered_lines = close_lines + ([retained_line] if retained_line is not None else [])
        # line_id 按最终顺序依次 close-1、close-2……
        lines = [
            {
                "line_id": f"close-{index}",
                "account_code": line["account_code"],
                "debit": line["debit"],
                "credit": line["credit"],
            }
            for index, line in enumerate(ordered_lines, start=1)
        ]
        closing_entry = {
            "voucher_id": closing_voucher_id,
            "posting_date": payload["period_end"],
            "currency": context["currency"],
            "lines": lines,
        }

    # ---- 下一期期初：仅资产、负债、权益的期末净额，留存收益叠加净利润 ----
    next_opening_balances: list[dict[str, Any]] = []
    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        if account["type"] in _TEMPORARY_TYPES:
            continue
        opening = opening_by_code.get(code)
        opening_debit = opening["debit"] if opening is not None else _ZERO
        opening_credit = opening["credit"] if opening is not None else _ZERO
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        # 以借方为正的期末净额；正利润贷记留存收益，即从其借方净额中扣减。
        net = (opening_debit - opening_credit) + (period_debit - period_credit)
        if code == retained_code:
            net -= net_income
        if net == _ZERO:
            continue
        if net > 0:
            item = {"account_code": code, "debit": _money(net), "credit": _money(_ZERO)}
        else:
            item = {"account_code": code, "debit": _money(_ZERO), "credit": _money(-net)}
        next_opening_balances.append(item)

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
