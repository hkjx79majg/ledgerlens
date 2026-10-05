"""无状态递延所得税计算（deferred income tax）。

根据单一报告日的账面价值（carrying amount）与计税基础（tax base）计算
可追溯的暂时性差异及其递延所得税影响：资产项目暂时性差异为账面价值减
计税基础，负债项目为计税基础减账面价值；正数差异按预计转回日覆盖的
税率区间计量为递延所得税负债，负数差异为可抵扣差异，在本期有证据支持
的可收回上限内确认为递延所得税资产，超出部分不确认。结果仅用于报表
列示与后续分录生成：不自动写入凭证，不改变任何既有端点语义。

契约要点：
- 金额只接受可无损转换为 Decimal 的有限十进制值（JSON 字符串或整数；
  布尔、浮点、NaN/Infinity、指数记法一律拒绝），税率为闭区间 [0, 1]。
- 日期为真实 YYYY-MM-DD 日历日；税率区间为闭区间、起止有效且不得重叠。
- 预计转回日必须恰好落在一个税率区间内，按该区间税率计量。
- 每个项目的差异先按 ROUND_HALF_UP 保留两位小数，再以已舍入差异
  乘税率并同样四舍五入到两位小数；汇总值一律由已舍入明细相加。
- 空项目集合成功返回全零汇总；不修改输入对象；相同输入字段与值一致。

Python 入口 calculate_deferred_tax 对任何契约违反统一抛出 ValueError
（DeferredTaxError，附带稳定的 code 与 JSON Pointer 形式 path）；HTTP
层据此返回 400，且不返回任何部分计算结果。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any

# 严格有限十进制：可选负号、整数部分（允许前导零，金额输入不做无符号限制）、
# 可选小数部分；不接受指数、正号、空白、NaN/Infinity。
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_ONE = Decimal("1")

_REQUEST_FIELDS = ("report_date", "tax_rates", "items")
_BRACKET_FIELDS = ("effective_from", "effective_to", "rate")
_ITEM_FIELDS = (
    "item_id",
    "nature",
    "carrying_amount",
    "tax_base",
    "expected_reversal_date",
    "attribution",
)
_CAP_FIELD = "deductible_recoverable_cap"

_NATURES = ("asset", "liability")
_ATTRIBUTIONS = ("profit_or_loss", "oci", "equity")


class DeferredTaxError(ValueError):
    """递延所得税契约违反；code/path 供 HTTP 层稳定地表达同一原因。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def _fail(code: str, path: str, message: str) -> None:
    raise DeferredTaxError(code, path, message)


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _parse_date(value: Any) -> date | None:
    """解析真实日历日期；非法返回 None。"""
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    """解析可无损转换的有限十进制；非法返回 None。

    接受 str（严格文法）、int（bool 除外）与已是有限值的 Decimal；
    拒绝 float（二进制浮点不保证无损）及非有限值。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        return None
    if _DECIMAL_RE.fullmatch(value) is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _check_unknown_fields(obj: dict[str, Any], allowed: tuple[str, ...], base: str) -> None:
    for key in sorted(obj):
        if key not in allowed:
            path = f"{base}/{_escape(key)}" if base else f"/{_escape(key)}"
            _fail("unknown_field", path, f"unknown field {key!r}")


def _parse_report_date(payload: dict[str, Any]) -> None:
    if "report_date" not in payload:
        _fail("required", "/report_date", "report_date is required")
    if _parse_date(payload["report_date"]) is None:
        _fail(
            "invalid_date",
            "/report_date",
            "report_date must be a valid calendar date in YYYY-MM-DD format",
        )


def _parse_tax_rates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """校验税率区间：字段、日期、税率取值以及闭区间不重叠。"""
    if "tax_rates" not in payload:
        _fail("required", "/tax_rates", "tax_rates is required")
    raw_rates = payload["tax_rates"]
    if not isinstance(raw_rates, list):
        _fail("invalid_type", "/tax_rates", "tax_rates must be an array")

    brackets: list[dict[str, Any]] = []
    for index, bracket in enumerate(raw_rates):
        base = f"/tax_rates/{index}"
        if not isinstance(bracket, dict):
            _fail("invalid_type", base, "tax rate bracket must be an object")
        _check_unknown_fields(bracket, _BRACKET_FIELDS, base)

        if "effective_from" not in bracket:
            _fail("required", f"{base}/effective_from", "effective_from is required")
        if "effective_to" not in bracket:
            _fail("required", f"{base}/effective_to", "effective_to is required")
        if "rate" not in bracket:
            _fail("required", f"{base}/rate", "rate is required")

        effective_from = _parse_date(bracket["effective_from"])
        if effective_from is None:
            _fail(
                "invalid_date",
                f"{base}/effective_from",
                "effective_from must be a valid calendar date in YYYY-MM-DD format",
            )
        effective_to = _parse_date(bracket["effective_to"])
        if effective_to is None:
            _fail(
                "invalid_date",
                f"{base}/effective_to",
                "effective_to must be a valid calendar date in YYYY-MM-DD format",
            )
        if effective_from > effective_to:
            _fail(
                "invalid_tax_rate_period",
                f"{base}/effective_from",
                "effective_from must be on or before effective_to",
            )

        rate = _parse_decimal(bracket["rate"])
        if rate is None:
            _fail(
                "invalid_tax_rate",
                f"{base}/rate",
                "rate must be a finite decimal value without exponent notation",
            )
        if not _ZERO <= rate <= _ONE:
            _fail("invalid_tax_rate", f"{base}/rate", "rate must be within [0, 1]")

        # 税率原样回显（字符串保持原写法），整数输入则规范化为十进制字符串。
        rate_echo = bracket["rate"] if isinstance(bracket["rate"], str) else str(rate)
        brackets.append(
            {
                "index": index,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "rate": rate,
                "rate_echo": rate_echo,
            }
        )

    # 闭区间不得重叠：按时间线归并，落在既有并集内即重叠，路径定位到后出现者。
    ordered = sorted(
        brackets,
        key=lambda item: (item["effective_from"], item["effective_to"], item["index"]),
    )
    if ordered:
        merged_end = ordered[0]["effective_to"]
        for bracket in ordered[1:]:
            if bracket["effective_from"] <= merged_end:
                _fail(
                    "tax_rate_period_overlap",
                    f"/tax_rates/{bracket['index']}",
                    "tax rate brackets must not overlap",
                )
            if bracket["effective_to"] > merged_end:
                merged_end = bracket["effective_to"]
    return brackets


def _parse_items(
    payload: dict[str, Any], brackets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """逐项校验并解析暂时性差异项目。"""
    if "items" not in payload:
        _fail("required", "/items", "items is required")
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        _fail("invalid_type", "/items", "items must be an array")

    seen_ids: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        base = f"/items/{index}"
        if not isinstance(item, dict):
            _fail("invalid_type", base, "item must be an object")

        # 可收回上限仅可抵扣项目可携带，先从未知字段检查中剔除，再按方向定性。
        _check_unknown_fields(item, _ITEM_FIELDS + (_CAP_FIELD,), base)

        if "item_id" not in item:
            _fail("required", f"{base}/item_id", "item_id is required")
        item_id = item["item_id"]
        if not isinstance(item_id, str):
            _fail("invalid_type", f"{base}/item_id", "item_id must be a string")
        if item_id == "":
            _fail("blank_value", f"{base}/item_id", "item_id must not be blank")
        if item_id in seen_ids:
            _fail(
                "duplicate_item_id",
                f"{base}/item_id",
                f"item_id {item_id!r} is duplicated within items",
            )
        seen_ids.add(item_id)

        if "nature" not in item:
            _fail("required", f"{base}/nature", "nature is required")
        nature = item["nature"]
        if not isinstance(nature, str) or nature not in _NATURES:
            _fail(
                "invalid_nature",
                f"{base}/nature",
                "nature must be 'asset' or 'liability'",
            )

        if "carrying_amount" not in item:
            _fail("required", f"{base}/carrying_amount", "carrying_amount is required")
        carrying = _parse_decimal(item["carrying_amount"])
        if carrying is None:
            _fail(
                "invalid_amount",
                f"{base}/carrying_amount",
                "carrying_amount must be a finite decimal value convertible to Decimal",
            )

        if "tax_base" not in item:
            _fail("required", f"{base}/tax_base", "tax_base is required")
        tax_base = _parse_decimal(item["tax_base"])
        if tax_base is None:
            _fail(
                "invalid_amount",
                f"{base}/tax_base",
                "tax_base must be a finite decimal value convertible to Decimal",
            )

        if "expected_reversal_date" not in item:
            _fail(
                "required",
                f"{base}/expected_reversal_date",
                "expected_reversal_date is required",
            )
        reversal_date = _parse_date(item["expected_reversal_date"])
        if reversal_date is None:
            _fail(
                "invalid_date",
                f"{base}/expected_reversal_date",
                "expected_reversal_date must be a valid calendar date in YYYY-MM-DD format",
            )

        if "attribution" not in item:
            _fail("required", f"{base}/attribution", "attribution is required")
        attribution = item["attribution"]
        if not isinstance(attribution, str) or attribution not in _ATTRIBUTIONS:
            _fail(
                "invalid_attribution",
                f"{base}/attribution",
                "attribution must be one of 'profit_or_loss', 'oci', 'equity'",
            )

        # 差异方向：资产为账面减计税基础，负债相反；按项目先四舍五入到分。
        with localcontext() as ctx:
            ctx.prec = max(
                50,
                len(carrying.as_tuple().digits) + len(tax_base.as_tuple().digits) + 5,
            )
            signed = carrying - tax_base
            if nature == "liability":
                signed = -signed
            if signed == 0:
                difference = _ZERO
            else:
                difference = signed.quantize(_CENT, rounding=ROUND_HALF_UP)
        deductible = difference < 0

        # 非可抵扣项目携带可收回上限属于契约外字段。
        if not deductible and _CAP_FIELD in item:
            _fail(
                "unknown_field",
                f"{base}/{_CAP_FIELD}",
                f"unknown field {_CAP_FIELD!r}",
            )

        cap: Decimal | None = None
        if deductible:
            if _CAP_FIELD not in item:
                _fail(
                    "required",
                    f"{base}/{_CAP_FIELD}",
                    f"{_CAP_FIELD} is required for deductible temporary differences",
                )
            cap = _parse_decimal(item[_CAP_FIELD])
            if cap is None:
                _fail(
                    "invalid_amount",
                    f"{base}/{_CAP_FIELD}",
                    f"{_CAP_FIELD} must be a finite decimal value convertible to Decimal",
                )
            if cap < _ZERO:
                _fail(
                    "recoverable_cap_below_zero",
                    f"{base}/{_CAP_FIELD}",
                    f"{_CAP_FIELD} must not be less than zero",
                )
            if cap > -difference:
                _fail(
                    "recoverable_cap_exceeds_difference",
                    f"{base}/{_CAP_FIELD}",
                    f"{_CAP_FIELD} must not exceed the deductible temporary difference",
                )

        # 预计转回日必须恰好匹配一个覆盖该日的闭区间税率区间。
        matches = [
            bracket
            for bracket in brackets
            if bracket["effective_from"] <= reversal_date <= bracket["effective_to"]
        ]
        if len(matches) == 0:
            _fail(
                "no_matching_tax_rate",
                f"{base}/expected_reversal_date",
                "expected_reversal_date must be covered by exactly one tax rate bracket",
            )
        if len(matches) > 1:
            _fail(
                "multiple_matching_tax_rates",
                f"{base}/expected_reversal_date",
                "expected_reversal_date must match exactly one tax rate bracket",
            )

        items.append(
            {
                "item_id": item_id,
                "nature": nature,
                "attribution": attribution,
                "difference": difference,
                "cap": cap,
                "rate": matches[0]["rate"],
                "rate_echo": matches[0]["rate_echo"],
            }
        )
    return items


def _money(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = max(50, len(value.as_tuple().digits) + 5)
        return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _tax_amount(base: Decimal, rate: Decimal) -> Decimal:
    """已舍入差异乘税率后四舍五入到分；按操作数位宽提升精度，结果精确。"""
    with localcontext() as ctx:
        ctx.prec = max(
            50, len(base.as_tuple().digits) + len(rate.as_tuple().digits) + 10
        )
        product = base * rate
    return product.quantize(_CENT, rounding=ROUND_HALF_UP)


def calculate_deferred_tax(payload: dict[str, Any]) -> dict[str, Any]:
    """根据报告日账面价值与计税基础计算递延所得税，返回可追溯结果字典。

    任何契约违反统一抛出 ValueError（实为 DeferredTaxError）；不修改
    输入对象；不落盘、不保留状态，相同输入字段与值完全一致。
    """
    if not isinstance(payload, dict):
        _fail("request_not_object", "", "request body must be a JSON object")
    _check_unknown_fields(payload, _REQUEST_FIELDS, "")
    _parse_report_date(payload)
    brackets = _parse_tax_rates(payload)
    items = _parse_items(payload, brackets)

    # ---- 成功：逐项以已舍入差异计量，汇总值由已舍入明细相加 ----
    results: list[dict[str, Any]] = []
    total_assets = _ZERO
    total_liabilities = _ZERO
    attribution_totals = {
        name: {"asset": _ZERO, "liability": _ZERO} for name in _ATTRIBUTIONS
    }

    for node in items:
        difference = node["difference"]
        rate = node["rate"]
        recognized = unrecognized = _ZERO
        asset_amount = liability_amount = _ZERO
        if difference > 0:
            liability_amount = _tax_amount(difference, rate)
        elif difference < 0:
            recognized = node["cap"]
            unrecognized = -difference - recognized
            if recognized > 0:
                asset_amount = _tax_amount(recognized, rate)

        attribution = node["attribution"]
        total_assets += asset_amount
        total_liabilities += liability_amount
        attribution_totals[attribution]["asset"] += asset_amount
        attribution_totals[attribution]["liability"] += liability_amount

        results.append(
            {
                "item_id": node["item_id"],
                "nature": node["nature"],
                "attribution": attribution,
                "temporary_difference": _money(difference),
                "tax_rate": node["rate_echo"],
                "recognized_deductible_difference": _money(recognized),
                "unrecognized_deductible_difference": _money(unrecognized),
                "deferred_tax_asset": _money(asset_amount),
                "deferred_tax_liability": _money(liability_amount),
            }
        )

    by_attribution: dict[str, dict[str, str]] = {}
    for name in _ATTRIBUTIONS:
        asset_total = attribution_totals[name]["asset"]
        liability_total = attribution_totals[name]["liability"]
        by_attribution[name] = {
            "deferred_tax_asset": _money(asset_total),
            "deferred_tax_liability": _money(liability_total),
            "net_deferred_tax": _money(asset_total - liability_total),
        }

    return {
        "report_date": payload["report_date"],
        "items": results,
        "total_deferred_tax_asset": _money(total_assets),
        "total_deferred_tax_liability": _money(total_liabilities),
        "net_deferred_tax": _money(total_assets - total_liabilities),
        "by_attribution": by_attribution,
    }
