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


def _collect_statement_amounts(
    chart_accounts: dict[str, dict[str, Any]],
    opening_by_code: dict[str, dict[str, Any]],
    period_by_code: dict[str, Any],
) -> tuple[dict[str, list[tuple[str, str, Decimal]]], dict[str, Decimal]]:
    """汇总损益表与资产负债表的各行（code/name/Decimal 金额）与六项合计。

    合计以未舍入 Decimal 返回，供报表格式化与跨期比率分析共用同一口径。
    """
    revenue_rows: list[tuple[str, str, Decimal]] = []
    expense_rows: list[tuple[str, str, Decimal]] = []
    asset_rows: list[tuple[str, str, Decimal]] = []
    liability_rows: list[tuple[str, str, Decimal]] = []
    equity_rows: list[tuple[str, str, Decimal]] = []
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
            amount = period_credit - period_debit
            total_revenue += amount
            revenue_rows.append((code, account["name"], amount))
        elif account_type == "expense":
            amount = period_debit - period_credit
            total_expense += amount
            expense_rows.append((code, account["name"], amount))
        else:
            # 期末净额：asset 取借方减贷方，liability/equity 取相反方向。
            net = (opening_debit - opening_credit) + (period_debit - period_credit)
            if account_type == "asset":
                amount = net
                total_assets += amount
                asset_rows.append((code, account["name"], amount))
            elif account_type == "liability":
                amount = -net
                total_liabilities += amount
                liability_rows.append((code, account["name"], amount))
            else:
                amount = -net
                total_equity += amount
                equity_rows.append((code, account["name"], amount))

    net_income = total_revenue - total_expense
    rows = {
        "revenue": revenue_rows,
        "expense": expense_rows,
        "assets": asset_rows,
        "liabilities": liability_rows,
        "equity": equity_rows,
    }
    totals = {
        "total_revenue": total_revenue,
        "total_expense": total_expense,
        "net_income": net_income,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity_before_net_income": total_equity,
    }
    return rows, totals


def _build_statements(
    chart_accounts: dict[str, dict[str, Any]],
    opening_by_code: dict[str, dict[str, Any]],
    period_by_code: dict[str, Any],
) -> dict[str, Any]:
    """由科目体系、期初与本期发生额汇总构建损益表与资产负债表。"""
    rows, totals = _collect_statement_amounts(
        chart_accounts, opening_by_code, period_by_code
    )
    formatted = {
        key: [_row(code, name, amount) for code, name, amount in row_list]
        for key, row_list in rows.items()
    }
    net_income = totals["net_income"]
    total_liabilities_and_equity = (
        totals["total_liabilities"]
        + totals["total_equity_before_net_income"]
        + net_income
    )

    return {
        "income_statement": {
            "revenue": formatted["revenue"],
            "expense": formatted["expense"],
            "total_revenue": _money(totals["total_revenue"]),
            "total_expense": _money(totals["total_expense"]),
            "net_income": _money(net_income),
        },
        "balance_sheet": {
            "assets": formatted["assets"],
            "liabilities": formatted["liabilities"],
            "equity": formatted["equity"],
            "total_assets": _money(totals["total_assets"]),
            "total_liabilities": _money(totals["total_liabilities"]),
            "total_equity_before_net_income": _money(
                totals["total_equity_before_net_income"]
            ),
            "current_period_net_income": _money(net_income),
            "total_liabilities_and_equity": _money(total_liabilities_and_equity),
            "balanced": totals["total_assets"] == total_liabilities_and_equity,
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
