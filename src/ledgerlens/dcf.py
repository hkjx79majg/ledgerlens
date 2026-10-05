"""无状态现金流折现（DCF）估值。

按请求的折现率与永续增长率对预测期自由现金流做折现估值：第 n 期折现
因子为 1/(1+discount_rate)^n，各期现值为自由现金流乘以对应因子；终值
为末期现金流乘以 (1+terminal_growth_rate) 后除以
(discount_rate-terminal_growth_rate)，并按末期因子折现。企业价值为预测
期现值合计与终值现值之和，股权价值为企业价值扣除 net_debt（负值表示净
现金，即加上净现金）。

请求只含 currency、discount_rate、terminal_growth_rate、
forecast_cash_flows、net_debt 五个顶层字段，拒绝未知字段。币种为三位
大写字母；两个比率为最多八位小数的十进制字符串（可带负号、不用指数），
折现率须大于 0 且不超过 1，永续增长率须大于 -1 且严格小于折现率；
net_debt 与各期 free_cash_flow 为最多两位小数的有符号十进制字符串。
forecast_cash_flows 至少一项，每项只含 period 与 free_cash_flow：period
为非布尔正整数，期号从 1 起连续且不重复。

错误沿用既有约定：缺失、类型、币种、金额错误分别报 required、
invalid_type、invalid_currency、invalid_amount；空预测报
too_few_forecast_periods；期号非法（非正整数取值）、重复或不连续统一在
/forecast_cash_flows 报 invalid_forecast_periods；比率格式或范围错误报
invalid_rate；永续增长率不低于折现率报
terminal_growth_not_less_than_discount_rate。任一业务错误返回 422、
valid: false 与按 path、code 字典序排序的完整 errors，不返回部分结果。

成功返回 200、valid: true，回显 currency、discount_rate、
terminal_growth_rate 与两位小数的 net_debt；periods 明细按期号升序，
每项含 period、原样带回的 free_cash_flow、八位小数 discount_factor 与
两位小数 present_value；另给出 forecast_present_value_total（预测期现值
合计）、terminal_value（终值）、terminal_present_value（终值现值）、
enterprise_value（企业价值）与 equity_value（股权价值）。中间值不先舍
入，全部输出按 ROUND_HALF_UP 舍入并消除负零。纯函数实现：不修改输入、
不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from .trial_balance import _add, _CURRENCY_RE, _escape

_REQUEST_FIELDS = (
    "currency",
    "discount_rate",
    "terminal_growth_rate",
    "forecast_cash_flows",
    "net_debt",
)
_FORECAST_FIELDS = ("period", "free_cash_flow")

# 比率：可带负号、无指数、最多八位小数的十进制字符串。
_RATE_RE = re.compile(r"-?\d+(\.\d{1,8})?")
# 有符号金额：可带负号、无指数、最多两位小数的十进制字符串。
_SIGNED_AMOUNT_RE = re.compile(r"-?\d+(\.\d{1,2})?")

_CENT = Decimal("0.01")
_FACTOR_QUANTUM = Decimal("0.00000001")
_ZERO = Decimal("0")
_ONE = Decimal("1")

# 中间值不先舍入：以 50 位有效数字计算，仅在输出时按 ROUND_HALF_UP 舍入。
_CALC_PRECISION = 50


def _format(value: Decimal, quantum: Decimal) -> str:
    """按 ROUND_HALF_UP 舍入到指定精度并消除负零。"""
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return str(rounded)


def _check_rate(
    errors: list[dict[str, str]],
    obj: dict[str, Any],
    key: str,
    path: str,
    *,
    exclusive_minimum: Decimal,
    inclusive_maximum: Decimal | None,
    range_message: str,
) -> Decimal | None:
    """校验至多八位小数的比率字符串；格式或范围非法统一报 invalid_rate。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a decimal string")
        return None
    if _RATE_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_rate",
            f"{key} must be a decimal string with at most eight fraction digits",
        )
        return None
    rate = Decimal(value)
    if rate <= exclusive_minimum or (
        inclusive_maximum is not None and rate > inclusive_maximum
    ):
        _add(errors, path, "invalid_rate", range_message)
        return None
    return rate


def _check_signed_money(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验至多两位小数的有符号金额字符串；格式非法报 invalid_amount。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a decimal string")
        return None
    if _SIGNED_AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be a signed decimal string with at most two fraction digits",
        )
        return None
    return Decimal(value)


def calculate_dcf_valuation(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """对预测期自由现金流做现金流折现估值，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- currency：三位大写字母 ----
    currency: str | None = None
    if "currency" not in payload:
        _add(errors, "/currency", "required", "currency is required")
    elif not isinstance(payload["currency"], str):
        _add(errors, "/currency", "invalid_type", "currency must be a string")
    elif _CURRENCY_RE.fullmatch(payload["currency"]) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
    else:
        currency = payload["currency"]

    # ---- 两个比率：格式与各自范围报 invalid_rate ----
    discount_rate = _check_rate(
        errors,
        payload,
        "discount_rate",
        "/discount_rate",
        exclusive_minimum=_ZERO,
        inclusive_maximum=_ONE,
        range_message="discount_rate must be greater than 0 and not exceed 1",
    )
    terminal_growth_rate = _check_rate(
        errors,
        payload,
        "terminal_growth_rate",
        "/terminal_growth_rate",
        exclusive_minimum=Decimal("-1"),
        inclusive_maximum=None,
        range_message="terminal_growth_rate must be greater than -1",
    )
    # 关联错误仅在两个比率各自有效时推导。
    if (
        discount_rate is not None
        and terminal_growth_rate is not None
        and terminal_growth_rate >= discount_rate
    ):
        _add(
            errors,
            "/terminal_growth_rate",
            "terminal_growth_not_less_than_discount_rate",
            "terminal_growth_rate must be strictly less than discount_rate",
        )

    net_debt = _check_signed_money(errors, payload, "net_debt", "/net_debt")

    # ---- forecast_cash_flows：逐项字段校验，期号问题统一在数组路径报告 ----
    flows: list[dict[str, Any]] = []
    if "forecast_cash_flows" not in payload:
        _add(errors, "/forecast_cash_flows", "required", "forecast_cash_flows is required")
    elif not isinstance(payload["forecast_cash_flows"], list):
        _add(
            errors,
            "/forecast_cash_flows",
            "invalid_type",
            "forecast_cash_flows must be an array",
        )
    else:
        items = payload["forecast_cash_flows"]
        if len(items) < 1:
            _add(
                errors,
                "/forecast_cash_flows",
                "too_few_forecast_periods",
                "forecast_cash_flows must contain at least one forecast period",
            )
        period_problem = False
        periods: list[int | None] = []
        for index, item in enumerate(items):
            base = f"/forecast_cash_flows/{index}"
            if not isinstance(item, dict):
                _add(errors, base, "invalid_type", "forecast cash flow must be an object")
                continue
            for key in item:
                if key not in _FORECAST_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            period: int | None = None
            if "period" not in item:
                _add(errors, f"{base}/period", "required", "period is required")
            else:
                raw_period = item["period"]
                if isinstance(raw_period, bool) or not isinstance(raw_period, int):
                    _add(
                        errors,
                        f"{base}/period",
                        "invalid_type",
                        "period must be an integer",
                    )
                elif raw_period < 1:
                    period_problem = True
                else:
                    period = raw_period
            amount = _check_signed_money(
                errors, item, "free_cash_flow", f"{base}/free_cash_flow"
            )
            flows.append(
                {
                    "period": period,
                    "amount": amount,
                    "raw": item.get("free_cash_flow"),
                }
            )
            periods.append(period)
        # 期号须从 1 起连续且不重复；任一违反统一报 invalid_forecast_periods。
        # 重复与连续性仅在全部期号本身为合法正整数时推导。
        if not period_problem and periods and all(p is not None for p in periods):
            if len(set(periods)) != len(periods):
                period_problem = True
            elif sorted(periods) != list(range(1, len(periods) + 1)):
                period_problem = True
        if period_problem:
            _add(
                errors,
                "/forecast_cash_flows",
                "invalid_forecast_periods",
                "periods must be positive integers, consecutive from 1, without duplicates",
            )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：中间值不先舍入，输出统一按 ROUND_HALF_UP 舍入并消除负零 ----
    ordered = sorted(flows, key=lambda flow: flow["period"])
    with localcontext() as context:
        context.prec = _CALC_PRECISION
        base = _ONE + discount_rate
        power = _ONE
        last_factor = _ONE
        last_amount = _ZERO
        forecast_pv_total = _ZERO
        periods_out: list[dict[str, Any]] = []
        for flow in ordered:
            power *= base
            factor = _ONE / power
            present_value = flow["amount"] * factor
            forecast_pv_total += present_value
            last_factor = factor
            last_amount = flow["amount"]
            periods_out.append(
                {
                    "period": flow["period"],
                    "free_cash_flow": flow["raw"],
                    "discount_factor": _format(factor, _FACTOR_QUANTUM),
                    "present_value": _format(present_value, _CENT),
                }
            )
        terminal_value = (
            last_amount * (_ONE + terminal_growth_rate) / (discount_rate - terminal_growth_rate)
        )
        terminal_pv = terminal_value * last_factor
        enterprise_value = forecast_pv_total + terminal_pv
        equity_value = enterprise_value - net_debt
        body = {
            "valid": True,
            "currency": currency,
            "discount_rate": payload["discount_rate"],
            "terminal_growth_rate": payload["terminal_growth_rate"],
            "net_debt": _format(net_debt, _CENT),
            "periods": periods_out,
            "forecast_present_value_total": _format(forecast_pv_total, _CENT),
            "terminal_value": _format(terminal_value, _CENT),
            "terminal_present_value": _format(terminal_pv, _CENT),
            "enterprise_value": _format(enterprise_value, _CENT),
            "equity_value": _format(equity_value, _CENT),
        }
    return 200, body
