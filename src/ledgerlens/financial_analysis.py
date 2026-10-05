"""无状态多期财务分析生成。

请求仅含顶层 ``periods`` 数组（至少两项），每项完全沿用财务报表端点的
请求契约（period_start、period_end、currency、chart、opening_balances、
entries），另含非空字符串 ``period_id``。各期独立复用 trial_balance.py 的
``_prepare`` 做字段级与跨对象校验，错误路径统一加 ``/periods/{i}`` 前缀；
跨期规则只在相关字段本身有效时推导：

- 少于两项：``too_few_periods``；
- ``period_id`` 与较早某项重复：在后者路径报 ``duplicate_period_id``；
- 后项币种、科目体系版本与首项不同：分别报 ``currency_mismatch``、
  ``chart_mismatch``；
- 日期区间两两相交：开始较晚的一项报 ``overlapping_periods``（开始相同时
  取输入顺序靠后者）。

成功时各期按 period_end、period_start、period_id 升序输出未舍入口径汇总
（金额两位小数），并按该顺序给出比率：净利润率、资产负债率、资产净利率
使用当期未舍入金额；非首项另含相对紧邻前一期的收入、净利润、资产增长率，
增长率为（当期 - 前期）/ |前期|。比率以精确十进制按 ROUND_HALF_UP 输出
四位小数字符串，分母为零时 value 为 null、status 为 zero_denominator。
纯函数实现：不修改输入、不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .financial_statements import _statement_totals
from .trial_balance import _ZERO, _add, _check_nonempty_string, _escape, _money, _parse_date, _prepare

_REQUEST_FIELDS = ("periods",)
_FOUR_PLACES = Decimal("0.0001")


def _ratio(numerator: Decimal, denominator: Decimal) -> dict[str, Any]:
    """计算单个比率：分母为零返回 zero_denominator，否则 ROUND_HALF_UP 四位小数。"""
    if denominator == _ZERO:
        return {"value": None, "status": "zero_denominator"}
    value = (numerator / denominator).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    return {"value": str(value), "status": "ok"}


def _ratios_for(totals: dict[str, Decimal]) -> dict[str, Any]:
    """由当期未舍入总额构建三个比率。"""
    return {
        "net_profit_margin": _ratio(totals["net_income"], totals["total_revenue"]),
        "debt_to_assets": _ratio(totals["total_liabilities"], totals["total_assets"]),
        "net_income_to_assets": _ratio(totals["net_income"], totals["total_assets"]),
    }


def _trend_for(
    current: dict[str, Decimal], previous: dict[str, Decimal], previous_period_id: str
) -> dict[str, Any]:
    """构建相对前一期的增长率：（当期 - 前期）/ |前期|。"""
    pairs = (
        ("revenue_growth", "total_revenue"),
        ("net_income_growth", "net_income"),
        ("asset_growth", "total_assets"),
    )
    trend: dict[str, Any] = {"previous_period_id": previous_period_id}
    for key, total_key in pairs:
        prior = previous[total_key]
        trend[key] = _ratio(current[total_key] - prior, abs(prior))
    return trend


def generate_financial_analysis(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成多期财务分析，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- periods：数组且至少两项；非对象元素只报类型错误 ----
    period_nodes: list[dict[str, Any]] = []
    if "periods" not in payload:
        _add(errors, "/periods", "required", "periods is required")
    elif not isinstance(payload["periods"], list):
        _add(errors, "/periods", "invalid_type", "periods must be an array")
    else:
        periods = payload["periods"]
        if len(periods) < 2:
            _add(
                errors,
                "/periods",
                "too_few_periods",
                "periods must contain at least two periods",
            )
        for index, period in enumerate(periods):
            base = f"/periods/{index}"
            if not isinstance(period, dict):
                _add(errors, base, "invalid_type", "period must be an object")
                continue
            # period_id 为该端点在财务报表请求之外唯一新增字段。
            period_id = _check_nonempty_string(
                errors, period, "period_id", f"{base}/period_id"
            )
            # 各期独立沿用财务报表的全部校验，错误路径加 /periods/{i} 前缀。
            period_errors, context = _prepare(period, extra_fields=("period_id",))
            for err in period_errors:
                _add(errors, base + err["path"], err["code"], err["message"])

            start = _parse_date(period["period_start"]) if isinstance(
                period.get("period_start"), str
            ) else None
            end = _parse_date(period["period_end"]) if isinstance(
                period.get("period_end"), str
            ) else None
            period_ok = start is not None and end is not None and start <= end
            period_nodes.append(
                {
                    "index": index,
                    "period": period,
                    "period_id": period_id,
                    "context": context,
                    "start": start,
                    "end": end,
                    "period_ok": period_ok,
                }
            )

    # ---- 跨期规则：仅依赖各字段本身有效 ----
    valid_nodes = [node for node in period_nodes if node["context"] is not None]
    if valid_nodes:
        first = valid_nodes[0]
        first_currency = first["context"]["currency"]
        first_chart_id = first["context"]["chart_id"]
        seen_ids: set[str] = set()
        for node in valid_nodes:
            base = f"/periods/{node['index']}"
            if node["period_id"] is not None:
                if node["period_id"] in seen_ids:
                    _add(
                        errors,
                        f"{base}/period_id",
                        "duplicate_period_id",
                        f"period_id {node['period_id']!r} is duplicated within periods",
                    )
                else:
                    seen_ids.add(node["period_id"])
            if node is not first:
                if node["context"]["currency"] != first_currency:
                    _add(
                        errors,
                        f"{base}/currency",
                        "currency_mismatch",
                        "period currency must match the currency of the first period",
                    )
                if node["context"]["chart_id"] != first_chart_id:
                    _add(
                        errors,
                        f"{base}/chart/chart_id",
                        "chart_mismatch",
                        "period chart_id must match the chart_id of the first period",
                    )

        # 日期区间两两不得相交；相交时由开始较晚的一项报错（开始相同取后者）。
        dated_nodes = [node for node in valid_nodes if node["period_ok"]]
        overlapping: set[int] = set()
        for later_pos in range(len(dated_nodes)):
            later = dated_nodes[later_pos]
            for earlier_pos in range(later_pos):
                earlier = dated_nodes[earlier_pos]
                if later["start"] <= earlier["end"] and earlier["start"] <= later["end"]:
                    if later["start"] >= earlier["start"]:
                        overlapping.add(later["index"])
                    else:
                        overlapping.add(earlier["index"])
        for index in sorted(overlapping):
            _add(
                errors,
                f"/periods/{index}",
                "overlapping_periods",
                "periods must not overlap",
            )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：各期独立汇总未舍入金额，再按 period_end、period_start、period_id 排序 ----
    built: list[tuple[date, date, str, dict[str, Any], dict[str, Decimal]]] = []
    for node in period_nodes:
        context = node["context"]
        totals = _statement_totals(
            context["chart_accounts"],
            context["opening_by_code"],
            context["period_by_code"],
        )
        period = node["period"]
        period_body = {
            "period_id": node["period_id"],
            "period_start": period["period_start"],
            "period_end": period["period_end"],
            "total_revenue": _money(totals["total_revenue"]),
            "total_expense": _money(totals["total_expense"]),
            "net_income": _money(totals["net_income"]),
            "total_assets": _money(totals["total_assets"]),
            "total_liabilities": _money(totals["total_liabilities"]),
            "total_equity_before_net_income": _money(
                totals["total_equity_before_net_income"]
            ),
        }
        built.append((node["start"], node["end"], node["period_id"], period_body, totals))

    built.sort(key=lambda item: (item[1], item[0], item[2]))

    ratio_entries: list[dict[str, Any]] = []
    for position, (_start, _end, _period_id, _body, totals) in enumerate(built):
        entry = _ratios_for(totals)
        if position > 0:
            previous_totals = built[position - 1][4]
            previous_period_id = built[position - 1][2]
            entry["trend"] = _trend_for(totals, previous_totals, previous_period_id)
        ratio_entries.append(entry)

    first_context = period_nodes[0]["context"]
    return 200, {
        "valid": True,
        "currency": first_context["currency"],
        "chart_id": first_context["chart_id"],
        "periods": [item[3] for item in built],
        "ratios": ratio_entries,
    }
