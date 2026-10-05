"""无状态现金流折现（DCF）估值。

请求只含五个顶层字段（拒绝未知字段）：``currency`` 为三位大写字母；
``discount_rate``、``terminal_growth_rate`` 为至多八位小数的十进制字符串，
折现率须大于 0 且不超过 1，永续增长率须大于 -1 且严格小于折现率；
``net_debt`` 为至多两位小数的有符号十进制字符串（负值表示净现金）；
``forecast_cash_flows`` 为至少一项的预测现金流数组，每项只含 ``period``
（非布尔正整数，期号从 1 起连续且不重复）与 ``free_cash_flow``（至多两位
小数的有符号十进制字符串）。

成功返回 200、``valid: true``：``forecast`` 按期升序给出每期原现金流、
八位小数折现因子（第 n 期为 1/(1+discount_rate)^n）与两位小数现值；
终值为末期现金流乘 (1+terminal_growth_rate) 再除以
(discount_rate-terminal_growth_rate)，并按末期因子折现。另返回预测期
现值合计、终值及其现值、企业价值与扣除 net_debt 后的股权价值。中间值
不先舍入，输出统一按 ROUND_HALF_UP 舍入并消除负零。

错误结构、状态码与排序规则沿用既有端点：缺失、类型、币种与金额错误沿用
required、invalid_type、invalid_currency、invalid_amount；空预测报
too_few_forecast_periods；期号非法、重复或不连续统一在
/forecast_cash_flows 报 invalid_forecast_periods；比率格式或范围错误报
invalid_rate；增长率不低于折现率报
terminal_growth_not_less_than_discount_rate。纯函数实现：不修改输入、
不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .trial_balance import (
    _CENT,
    _CURRENCY_RE,
    _ZERO,
    _add,
    _check_nonempty_string,
    _escape,
)

_REQUEST_FIELDS = (
    "currency",
    "discount_rate",
    "terminal_growth_rate",
    "forecast_cash_flows",
    "net_debt",
)
_FORECAST_FIELDS = ("period", "free_cash_flow")

# 有符号、至多两位小数的十进制字符串（金额）。
_SIGNED_AMOUNT_RE = re.compile(r"-?\d+(\.\d{1,2})?")
# 有符号、至多八位小数的十进制字符串（比率）。
_RATE_RE = re.compile(r"-?\d+(\.\d{1,8})?")

_ONE = Decimal("1")
_FACTOR_QUANT = Decimal("0.00000001")


def _rounded(value: Decimal, quant: Decimal) -> str:
    """按 ROUND_HALF_UP 舍入到指定精度并消除负零。"""
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    if rounded == 0:
        # 统一 -0.00… 与 0.00… 的输出。
        rounded = abs(rounded)
    return str(rounded)


def _money(value: Decimal) -> str:
    """金额统一输出两位小数。"""
    return _rounded(value, _CENT)


def _factor_text(value: Decimal) -> str:
    """折现因子统一输出八位小数。"""
    return _rounded(value, _FACTOR_QUANT)


def _check_signed_amount(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验至多两位小数的有符号金额字符串；合法返回精确值，否则返回 None。"""
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
    if _SIGNED_AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be a signed decimal string with at most two fraction digits",
        )
        return None
    return Decimal(value)


def _check_rate(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验至多八位小数的比率字符串；格式非法报 invalid_rate。"""
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
    if _RATE_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_rate",
            f"{key} must be a decimal string with at most eight fraction digits",
        )
        return None
    return Decimal(value)


def _validate_forecast(
    payload: dict[str, Any], errors: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """校验 forecast_cash_flows 并返回逐项解析结果（period 与精确金额）。

    逐项执行字段级校验；期号序列（从 1 起连续且不重复）仅在每项 period
    均为合法整数时推导，非法、重复或不连续统一在 /forecast_cash_flows
    报一处 invalid_forecast_periods。
    """
    if "forecast_cash_flows" not in payload:
        _add(errors, "/forecast_cash_flows", "required", "forecast_cash_flows is required")
        return []
    raw = payload["forecast_cash_flows"]
    if not isinstance(raw, list):
        _add(
            errors,
            "/forecast_cash_flows",
            "invalid_type",
            "forecast_cash_flows must be an array",
        )
        return []
    if len(raw) == 0:
        _add(
            errors,
            "/forecast_cash_flows",
            "too_few_forecast_periods",
            "forecast_cash_flows must contain at least one period",
        )
        return []

    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        base = f"/forecast_cash_flows/{index}"
        if not isinstance(item, dict):
            _add(errors, base, "invalid_type", "forecast cash flow must be an object")
            items.append({"period": None, "amount": None})
            continue
        for key in item:
            if key not in _FORECAST_FIELDS:
                _add(
                    errors,
                    f"{base}/{_escape(key)}",
                    "unknown_field",
                    f"unknown field {key!r}",
                )
        if "period" not in item:
            _add(errors, f"{base}/period", "required", "period is required")
            period = None
        elif isinstance(item["period"], bool) or not isinstance(item["period"], int):
            _add(errors, f"{base}/period", "invalid_type", "period must be an integer")
            period = None
        else:
            period = item["period"]
        amount = _check_signed_amount(
            errors, item, "free_cash_flow", f"{base}/free_cash_flow"
        )
        items.append({"period": period, "amount": amount})

    # 期号序列：非正、重复或不连续（从 1 起）统一报一处；period 字段本身
    # 无效时不派生该错误。
    periods = [node["period"] for node in items]
    if all(period is not None for period in periods):
        if sorted(periods) != list(range(1, len(periods) + 1)):
            _add(
                errors,
                "/forecast_cash_flows",
                "invalid_forecast_periods",
                "periods must be consecutive positive integers starting from 1 without duplicates",
            )
    return items


def calculate_dcf_valuation(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """计算现金流折现估值，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

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

    # ---- 折现率：大于 0 且不超过 1 ----
    discount_rate = _check_rate(errors, payload, "discount_rate", "/discount_rate")
    if discount_rate is not None and not (_ZERO < discount_rate <= _ONE):
        _add(
            errors,
            "/discount_rate",
            "invalid_rate",
            "discount_rate must be greater than 0 and at most 1",
        )
        discount_rate = None

    # ---- 永续增长率：大于 -1，且严格小于折现率 ----
    terminal_growth_rate = _check_rate(
        errors, payload, "terminal_growth_rate", "/terminal_growth_rate"
    )
    if terminal_growth_rate is not None and terminal_growth_rate <= -_ONE:
        _add(
            errors,
            "/terminal_growth_rate",
            "invalid_rate",
            "terminal_growth_rate must be greater than -1",
        )
        terminal_growth_rate = None
    # 交叉规则仅在两个比率字段本身均有效时推导。
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

    # ---- 净债务：有符号金额，负值表示净现金 ----
    net_debt = _check_signed_amount(errors, payload, "net_debt", "/net_debt")

    # ---- 预测现金流 ----
    items = _validate_forecast(payload, errors)

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：逐期折现，中间值不舍入 ----
    base = _ONE + discount_rate
    forecast: list[dict[str, Any]] = []
    forecast_present_value = _ZERO
    last_factor = _ZERO
    last_amount = _ZERO
    for node in sorted(items, key=lambda entry: entry["period"]):
        period = node["period"]
        amount = node["amount"]
        factor = _ONE / base**period
        present_value = amount * factor
        forecast_present_value += present_value
        last_factor = factor
        last_amount = amount
        forecast.append(
            {
                "period": period,
                "free_cash_flow": _money(amount),
                "discount_factor": _factor_text(factor),
                "present_value": _money(present_value),
            }
        )

    # 终值：末期现金流按永续增长率外推后除以折现率与增长率之差，
    # 再按末期折现因子折现。
    terminal_value = last_amount * (_ONE + terminal_growth_rate) / (
        discount_rate - terminal_growth_rate
    )
    terminal_present_value = terminal_value * last_factor
    enterprise_value = forecast_present_value + terminal_present_value
    equity_value = enterprise_value - net_debt

    return 200, {
        "valid": True,
        "currency": currency,
        "forecast": forecast,
        "forecast_present_value": _money(forecast_present_value),
        "terminal_value": _money(terminal_value),
        "terminal_present_value": _money(terminal_present_value),
        "enterprise_value": _money(enterprise_value),
        "equity_value": _money(equity_value),
    }
