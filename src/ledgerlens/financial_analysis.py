"""无状态多期财务分析（比较报表与比率）生成。

请求顶层只含 ``periods`` 数组，至少两项；每项沿用单期财务报表端点的请求
契约（``period_start``、``period_end``、``currency``、``chart``、
``opening_balances``、``entries``），另含非空 ``period_id``，期内层拒绝未知
字段。各期独立复用 trial_balance.py 的 ``_prepare``，错误路径加
``/periods/{i}`` 前缀；跨期错误仅在依赖字段有效时推导：

- 少于两期：``/periods`` 报 ``too_few_periods``；
- 后项与更早的有效期 ``period_id`` 相同：该项报 ``duplicate_period_id``；
- 后项币种、科目体系版本与首个有效期不同：分别报 ``currency_mismatch``、
  ``chart_mismatch``；
- 日期闭区间相交：开始较晚的一期报 ``overlapping_periods``（开始日相同时
  取索引靠后者）。

成功时期间按 ``period_end``、``period_start``、``period_id`` 升序输出，
金额两位小数；三项比率与三项增长率以当期未舍入金额按精确十进制计算，
``ROUND_HALF_UP`` 输出四位小数字符串，分母为零时 ``value`` 为 null、
``status`` 为 ``zero_denominator``。纯函数实现：不修改输入、不落盘、不保留
跨请求状态，相同输入必然得到相同输出。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .financial_statements import _collect_statement_amounts
from .trial_balance import (
    _ZERO,
    _add,
    _check_nonempty_string,
    _escape,
    _money,
    _prepare,
)

_RATIO_QUANT = Decimal("0.0001")

# 每项对外输出的六项合计（两位小数字符串）。
_TOTAL_FIELDS = (
    "total_revenue",
    "total_expense",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity_before_net_income",
)

_PERIOD_EXTRA_FIELDS = ("period_id",)


def _ratio(numerator: Decimal, denominator: Decimal) -> dict[str, Any]:
    """计算比值并输出四位小数字符串；分母为零时 value=null。"""
    if denominator == _ZERO:
        return {"value": None, "status": "zero_denominator"}
    value = (numerator / denominator).quantize(_RATIO_QUANT, rounding=ROUND_HALF_UP)
    if value == 0:
        # 统一 -0.0000 与 0.0000 的输出。
        value = Decimal("0.0000")
    return {"value": str(value), "status": "ok"}


def generate_financial_analysis(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """生成多期比较财务分析，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段：仅允许 periods ----
    for key in payload:
        if key != "periods":
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- periods 结构与最少期数 ----
    periods: list[Any] | None = None
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

    # ---- 各期独立沿用财务报表校验，错误路径加 /periods/{i} 前缀 ----
    nodes: list[dict[str, Any] | None] = []
    if periods is not None:
        for index, item in enumerate(periods):
            base = f"/periods/{index}"
            if not isinstance(item, dict):
                _add(errors, base, "invalid_type", "period must be an object")
                nodes.append(None)
                continue

            period_id = _check_nonempty_string(
                errors, item, "period_id", f"{base}/period_id"
            )

            # period_id 为该端点新增字段；其余字段（含期内未知字段）的校验
            # 完全沿用 _prepare，错误路径统一加 /periods/{i} 前缀。
            item_errors, context = _prepare(item, extra_fields=_PERIOD_EXTRA_FIELDS)
            for err in item_errors:
                _add(errors, base + err["path"], err["code"], err["message"])

            nodes.append(
                {
                    "base": base,
                    "item": item,
                    "context": context,
                    "period_id": period_id,
                }
            )

    # ---- 跨期推导：仅依赖各自有效字段 ----
    valid_nodes = [node for node in nodes if node is not None and node["context"] is not None]
    reference = valid_nodes[0] if valid_nodes else None
    seen_ids: set[str] = set()
    for node in nodes:
        if node is None:
            continue
        period_id = node["period_id"]
        context = node["context"]
        base = node["base"]

        if period_id is not None:
            if period_id in seen_ids:
                _add(
                    errors,
                    f"{base}/period_id",
                    "duplicate_period_id",
                    f"period_id {period_id!r} is duplicated within periods",
                )
            else:
                seen_ids.add(period_id)

        if context is not None and reference is not None:
            if context["currency"] != reference["context"]["currency"]:
                _add(
                    errors,
                    f"{base}/currency",
                    "currency_mismatch",
                    "period currency must match the currency of the other periods",
                )
            if context["chart_id"] != reference["context"]["chart_id"]:
                _add(
                    errors,
                    f"{base}/chart/chart_id",
                    "chart_mismatch",
                    "period chart_id must match the chart_id of the other periods",
                )

    # 区间相交：日期随 context 有效；开始较晚（同日取索引靠后）的一期报错。
    overlap_flags: set[int] = set()
    valid_indexed = [
        (
            index,
            date.fromisoformat(node["item"]["period_start"]),
            date.fromisoformat(node["item"]["period_end"]),
        )
        for index, node in enumerate(nodes)
        if node is not None and node["context"] is not None
    ]
    for pos in range(len(valid_indexed)):
        index_a, start_a, end_a = valid_indexed[pos]
        for later in range(pos + 1, len(valid_indexed)):
            index_b, start_b, end_b = valid_indexed[later]
            if start_a <= end_b and start_b <= end_a:
                overlap_flags.add(index_b if start_b >= start_a else index_a)
    for index in sorted(overlap_flags):
        _add(
            errors,
            f"/periods/{index}",
            "overlapping_periods",
            "period date ranges must not overlap",
        )

    if errors:
        errors.sort(key=lambda entry: (entry["path"], entry["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：各期构建未舍入合计，按 period_end/period_start/period_id 排序 ----
    built: list[dict[str, Any]] = []
    for node in nodes:
        item = node["item"]
        context = node["context"]
        _, totals = _collect_statement_amounts(
            context["chart_accounts"],
            context["opening_by_code"],
            context["period_by_code"],
        )
        built.append(
            {
                "period_id": node["period_id"],
                "period_start": date.fromisoformat(item["period_start"]),
                "period_end": date.fromisoformat(item["period_end"]),
                "period_start_raw": item["period_start"],
                "period_end_raw": item["period_end"],
                "totals": totals,
            }
        )
    built.sort(key=lambda entry: (entry["period_end"], entry["period_start"], entry["period_id"]))

    output_periods: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for entry in built:
        totals = entry["totals"]
        period_output: dict[str, Any] = {
            "period_id": entry["period_id"],
            "period_start": entry["period_start_raw"],
            "period_end": entry["period_end_raw"],
        }
        for field in _TOTAL_FIELDS:
            period_output[field] = _money(totals[field])

        ratios: dict[str, Any] = {
            "net_profit_margin": _ratio(totals["net_income"], totals["total_revenue"]),
            "debt_to_assets": _ratio(
                totals["total_liabilities"], totals["total_assets"]
            ),
            "net_income_to_assets": _ratio(
                totals["net_income"], totals["total_assets"]
            ),
        }
        if previous is not None:
            previous_totals = previous["totals"]
            trend: dict[str, Any] = {"previous_period_id": previous["period_id"]}
            trend_specs = (
                ("revenue_growth", totals["total_revenue"], previous_totals["total_revenue"]),
                ("net_income_growth", totals["net_income"], previous_totals["net_income"]),
                ("asset_growth", totals["total_assets"], previous_totals["total_assets"]),
            )
            for name, current_value, previous_value in trend_specs:
                trend[name] = _ratio(current_value - previous_value, abs(previous_value))
            ratios["trend"] = trend

        period_output["ratios"] = ratios
        output_periods.append(period_output)
        previous = entry

    return 200, {
        "valid": True,
        "currency": reference["context"]["currency"],
        "chart_id": reference["context"]["chart_id"],
        "periods": output_periods,
    }
