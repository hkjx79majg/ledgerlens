"""无状态财务报表（损益表与资产负债表）生成。

请求契约、字段级与跨对象校验、错误结构、状态码与排序规则完全复用
trial_balance.py 的 `_prepare`：相同输入必然得到相同输出，不落盘、不保留
跨请求状态。报表口径：

- 损益表：revenue 科目金额 = 本期贷方发生额 - 借方发生额；expense 科目金额
  = 本期借方发生额 - 贷方发生额；net_income = total_revenue - total_expense。
- 资产负债表：asset 科目金额 = 期末借方余额 - 贷方余额；liability/equity 取
  相反方向。current_period_net_income 等于 net_income；
  total_liabilities_and_equity = 负债 + 期末既有权益 + 本期利润；
  balanced 仅在其与 total_assets 精确相等时为 true。

父子科目均只展示自身金额，不作层级滚算；零值科目仍保留；反向余额保留负号。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .trial_balance import _ZERO, _money, _prepare


def _row(code: str, name: str, amount: Decimal) -> dict[str, Any]:
    return {"account_code": code, "name": name, "amount": _money(amount)}


def _statement_totals(
    chart_accounts: dict[str, dict[str, Any]],
    opening_by_code: dict[str, dict[str, Any]],
    period_by_code: dict[str, Any],
) -> dict[str, Decimal]:
    """按报表口径汇总未舍入 Decimal 总额（供报表与多期分析共用）。

    口径与 `_build_statements` 完全一致：收入为本期贷方减借方，费用为本期
    借方减贷方；资产为期末借方减贷方，负债和权益取相反方向；
    net_income = 收入合计 - 费用合计；
    total_liabilities_and_equity = 负债 + 期末既有权益 + 本期利润。
    """
    total_revenue = _ZERO
    total_expense = _ZERO
    total_assets = _ZERO
    total_liabilities = _ZERO
    total_equity = _ZERO

    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        opening = opening_by_code.get(code)
        opening_debit = opening["debit"] if opening is not None else _ZERO
        opening_credit = opening["credit"] if opening is not None else _ZERO
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        account_type = account["type"]
        if account_type == "revenue":
            total_revenue += period_credit - period_debit
        elif account_type == "expense":
            total_expense += period_debit - period_credit
        else:
            net = (opening_debit - opening_credit) + (period_debit - period_credit)
            if account_type == "asset":
                total_assets += net
            elif account_type == "liability":
                total_liabilities += -net
            else:
                total_equity += -net

    net_income = total_revenue - total_expense
    return {
        "total_revenue": total_revenue,
        "total_expense": total_expense,
        "net_income": net_income,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity_before_net_income": total_equity,
        "total_liabilities_and_equity": total_liabilities + total_equity + net_income,
    }


def _build_statements(
    chart_accounts: dict[str, dict[str, Any]],
    opening_by_code: dict[str, dict[str, Any]],
    period_by_code: dict[str, Any],
) -> dict[str, Any]:
    """由科目体系、期初与本期发生额汇总构建损益表与资产负债表。"""
    revenue_rows: list[dict[str, Any]] = []
    expense_rows: list[dict[str, Any]] = []
    asset_rows: list[dict[str, Any]] = []
    liability_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    totals = _statement_totals(chart_accounts, opening_by_code, period_by_code)
    total_revenue = totals["total_revenue"]
    total_expense = totals["total_expense"]
    total_assets = totals["total_assets"]
    total_liabilities = totals["total_liabilities"]
    total_equity = totals["total_equity_before_net_income"]
    net_income = totals["net_income"]
    total_liabilities_and_equity = totals["total_liabilities_and_equity"]

    for code in sorted(chart_accounts):
        account = chart_accounts[code]
        opening = opening_by_code.get(code)
        opening_debit = opening["debit"] if opening is not None else _ZERO
        opening_credit = opening["credit"] if opening is not None else _ZERO
        period_debit, period_credit = period_by_code.get(code, (_ZERO, _ZERO))
        account_type = account["type"]
        if account_type == "revenue":
            amount = period_credit - period_debit
            revenue_rows.append(_row(code, account["name"], amount))
        elif account_type == "expense":
            amount = period_debit - period_credit
            expense_rows.append(_row(code, account["name"], amount))
        else:
            # 期末净额：asset 取借方减贷方，liability/equity 取相反方向。
            net = (opening_debit - opening_credit) + (period_debit - period_credit)
            if account_type == "asset":
                asset_rows.append(_row(code, account["name"], net))
            elif account_type == "liability":
                liability_rows.append(_row(code, account["name"], -net))
            else:
                equity_rows.append(_row(code, account["name"], -net))

    return {
        "income_statement": {
            "revenue": revenue_rows,
            "expense": expense_rows,
            "total_revenue": _money(total_revenue),
            "total_expense": _money(total_expense),
            "net_income": _money(net_income),
        },
        "balance_sheet": {
            "assets": asset_rows,
            "liabilities": liability_rows,
            "equity": equity_rows,
            "total_assets": _money(total_assets),
            "total_liabilities": _money(total_liabilities),
            "total_equity_before_net_income": _money(total_equity),
            "current_period_net_income": _money(net_income),
            "total_liabilities_and_equity": _money(total_liabilities_and_equity),
            "balanced": total_assets == total_liabilities_and_equity,
        },
    }


def generate_financial_statements(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成损益表与资产负债表，返回 (HTTP 状态码, 响应体)。"""
    errors, context = _prepare(payload)
    if errors:
        return 422, {"valid": False, "errors": errors}

    statements = _build_statements(
        context["chart_accounts"],
        context["opening_by_code"],
        context["period_by_code"],
    )

    return 200, {
        "valid": True,
        "chart_id": context["chart_id"],
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": context["currency"],
        **statements,
    }
