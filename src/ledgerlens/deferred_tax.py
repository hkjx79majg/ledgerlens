"""无状态递延所得税计算。

根据报告日的账面价值与计税基础生成可追溯的递延所得税结果：只计算、不
自动写入凭证，不改变既有报表、合并、期间结账与 HTTP 接口的既有语义。
公开 Python 入口 calculate_deferred_tax 在校验失败时统一抛出
DeferredTaxError（ValueError 子类，携带稳定的 code 与 JSON Pointer
path）；HTTP 入口 POST /deferred-tax/calculate 成功返回 200，失败返回
400 与 {"error": {"code", "path", "message"}}，以同一原因表达且不返回
部分计算结果。

请求含四个顶层字段（拒绝未知字段，unknown_field）：reporting_date 为
真实 YYYY-MM-DD 日历日期；currency 为三位大写字母的记账本位币；
tax_rates 为税率区间数组（可为空），每项含 start_date、end_date（均为
真实日历日期且起始不晚于终止，否则 invalid_period）与 rate（0 到 1 之间
的有限十进制值，否则 invalid_tax_rate），区间两两不得重叠
（overlapping_tax_rates，在后出现者路径报错）；items 为暂时性差异项目
数组，可为空。每个项目含唯一 item_id（重复报 duplicate_item_id）、
classification（asset 或 liability，否则 invalid_classification）、
carrying_amount、tax_base、expected_reversal_date 与 attribution
（profit_or_loss、oci 或 equity，否则 invalid_attribution）；金额只接受
可无损转换为 Decimal 的有限十进制值（否则 invalid_amount）。资产项目的
暂时性差异为账面价值减计税基础，负债项目为计税基础减账面价值；正数形成
递延所得税负债，负数在可收回上限内形成递延所得税资产。可抵扣项目（差异
为负）另须提供 recoverable_cap——本期有证据支持的可收回差异上限（缺失
报 required），小于零报 invalid_recoverable_cap、超过可抵扣差异报
recoverable_cap_exceeds_deductible。expected_reversal_date 须恰好被一
个税率区间覆盖：没有覆盖报 no_applicable_tax_rate，覆盖不止一个报
ambiguous_tax_rate。多个校验失败时按 (path, code) 字典序只报告首个错误。

成功时响应保留输入项目顺序，逐项给出 item_id、classification、原始
temporary_difference、适用 tax_rate、recognized_deductible_difference、
unrecognized_deductible_difference、deferred_tax_asset、
deferred_tax_liability 与 attribution；每个项目的递延所得税按四舍五入
（ROUND_HALF_UP）保留两位小数，total_deferred_tax_asset、
total_deferred_tax_liability、net_deferred_tax（资产减负债）与按三种
归属汇总的 attribution_totals 均由已舍入明细相加得到。空项目集合成功
返回全零汇总。处理不修改输入、不落盘、不保留跨请求状态，相同输入产生
字段与值均一致的结果。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .trial_balance import (
    _CENT,
    _CURRENCY_RE,
    _ZERO,
    _add,
    _check_date,
    _check_nonempty_string,
    _escape,
)

_REQUEST_FIELDS = ("reporting_date", "currency", "tax_rates", "items")
_BAND_FIELDS = ("start_date", "end_date", "rate")
_ITEM_FIELDS = (
    "item_id",
    "classification",
    "carrying_amount",
    "tax_base",
    "expected_reversal_date",
    "attribution",
    "recoverable_cap",
)

_CLASSIFICATIONS = ("asset", "liability")
_ATTRIBUTIONS = ("profit_or_loss", "oci", "equity")

_ONE = Decimal("1")


class DeferredTaxError(ValueError):
    """递延所得税校验错误：ValueError 子类，携带稳定 code 与 JSON Pointer path。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def _parse_decimal(value: Any) -> Decimal | None:
    """把可无损转换为 Decimal 的有限十进制值解析为 Decimal；否则返回 None。

    接受十进制字符串、整数（布尔除外）、有限浮点数与 Decimal；拒绝非有限
    值（NaN、Infinity）与其他类型。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(value)
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            return None
    else:
        return None
    if not candidate.is_finite():
        return None
    return candidate


def _money(value: Decimal) -> str:
    """递延所得税金额四舍五入到两位小数输出，负零规范为 0.00。"""
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP) + _ZERO)


def _exact(value: Decimal) -> str:
    """精确十进制值以无指数记法输出，负零规范为 0。"""
    if value == 0:
        return "0"
    return format(value, "f")


def _check_decimal(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验金额为可无损转换的有限十进制值；合法返回 Decimal，否则记录错误。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = _parse_decimal(obj[key])
    if value is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be a finite decimal value losslessly convertible to Decimal",
        )
        return None
    return value


def _check_rate(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验税率为 0 到 1 之间的有限十进制值，否则报 invalid_tax_rate。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = _parse_decimal(obj[key])
    if value is None or value < 0 or value > _ONE:
        _add(
            errors,
            path,
            "invalid_tax_rate",
            f"{key} must be a finite decimal value between 0 and 1 inclusive",
        )
        return None
    return value


def _check_enum(
    errors: list[dict[str, str]],
    obj: dict[str, Any],
    key: str,
    path: str,
    allowed: tuple[str, ...],
    code: str,
) -> str | None:
    """校验非空字符串枚举字段；取值非法时按给定错误码记录并返回 None。"""
    value = _check_nonempty_string(errors, obj, key, path)
    if value is None:
        return None
    if value not in allowed:
        _add(errors, path, code, f"{key} must be one of: {', '.join(allowed)}")
        return None
    return value


def calculate_deferred_tax(payload: dict[str, Any]) -> dict[str, Any]:
    """根据账面价值与计税基础计算递延所得税。

    校验失败统一抛出 DeferredTaxError（ValueError 子类）；成功返回结果
    字典。不修改输入对象。
    """
    if not isinstance(payload, dict):
        raise DeferredTaxError(
            "request_not_object", "", "request must be an object"
        )

    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    _check_date(errors, payload, "reporting_date", "/reporting_date")

    currency = _check_nonempty_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
        currency = None

    # ---- 税率区间：逐项字段校验，再检查起止有效与两两不重叠 ----
    band_nodes: list[dict[str, Any]] = []
    if "tax_rates" not in payload:
        _add(errors, "/tax_rates", "required", "tax_rates is required")
    elif not isinstance(payload["tax_rates"], list):
        _add(errors, "/tax_rates", "invalid_type", "tax_rates must be an array")
    else:
        for index, band in enumerate(payload["tax_rates"]):
            base = f"/tax_rates/{index}"
            if not isinstance(band, dict):
                _add(errors, base, "invalid_type", "tax rate band must be an object")
                continue
            for key in band:
                if key not in _BAND_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            start = _check_date(errors, band, "start_date", f"{base}/start_date")
            end = _check_date(errors, band, "end_date", f"{base}/end_date")
            rate = _check_rate(errors, band, "rate", f"{base}/rate")
            if start is not None and end is not None and start > end:
                _add(
                    errors,
                    f"{base}/start_date",
                    "invalid_period",
                    "start_date must not be after end_date",
                )
            band_nodes.append({"base": base, "start": start, "end": end, "rate": rate})
        for later in range(len(band_nodes)):
            node = band_nodes[later]
            if (
                node["start"] is None
                or node["end"] is None
                or node["start"] > node["end"]
            ):
                continue
            for earlier in range(later):
                other = band_nodes[earlier]
                if (
                    other["start"] is None
                    or other["end"] is None
                    or other["start"] > other["end"]
                ):
                    continue
                if node["start"] <= other["end"] and other["start"] <= node["end"]:
                    _add(
                        errors,
                        node["base"],
                        "overlapping_tax_rates",
                        "tax rate bands must not overlap",
                    )
                    break

    # ---- 暂时性差异项目：逐项字段校验，再推导差异、上限与适用税率 ----
    item_nodes: list[dict[str, Any]] = []
    if "items" not in payload:
        _add(errors, "/items", "required", "items is required")
    elif not isinstance(payload["items"], list):
        _add(errors, "/items", "invalid_type", "items must be an array")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(payload["items"]):
            base = f"/items/{index}"
            if not isinstance(item, dict):
                _add(errors, base, "invalid_type", "item must be an object")
                continue
            for key in item:
                if key not in _ITEM_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            item_id = _check_nonempty_string(errors, item, "item_id", f"{base}/item_id")
            if item_id is not None:
                if item_id in seen_ids:
                    _add(
                        errors,
                        f"{base}/item_id",
                        "duplicate_item_id",
                        f"item_id {item_id!r} is duplicated within items",
                    )
                else:
                    seen_ids.add(item_id)
            classification = _check_enum(
                errors,
                item,
                "classification",
                f"{base}/classification",
                _CLASSIFICATIONS,
                "invalid_classification",
            )
            carrying = _check_decimal(
                errors, item, "carrying_amount", f"{base}/carrying_amount"
            )
            tax_base = _check_decimal(errors, item, "tax_base", f"{base}/tax_base")
            reversal = _check_date(
                errors, item, "expected_reversal_date", f"{base}/expected_reversal_date"
            )
            attribution = _check_enum(
                errors,
                item,
                "attribution",
                f"{base}/attribution",
                _ATTRIBUTIONS,
                "invalid_attribution",
            )

            # 暂时性差异：资产为账面价值减计税基础，负债相反。
            difference: Decimal | None = None
            if classification is not None and carrying is not None and tax_base is not None:
                if classification == "asset":
                    difference = carrying - tax_base
                else:
                    difference = tax_base - carrying

            # 可收回上限：可抵扣项目必填；提供时须不小于零且不超过可抵扣差异。
            cap: Decimal | None = None
            cap_present = "recoverable_cap" in item
            if cap_present:
                raw_cap = _parse_decimal(item["recoverable_cap"])
                if raw_cap is None:
                    _add(
                        errors,
                        f"{base}/recoverable_cap",
                        "invalid_amount",
                        "recoverable_cap must be a finite decimal value "
                        "losslessly convertible to Decimal",
                    )
                elif raw_cap < 0:
                    _add(
                        errors,
                        f"{base}/recoverable_cap",
                        "invalid_recoverable_cap",
                        "recoverable_cap must not be negative",
                    )
                else:
                    cap = raw_cap
            if difference is not None:
                deductible = -difference if difference < 0 else _ZERO
                if deductible > 0 and not cap_present:
                    _add(
                        errors,
                        f"{base}/recoverable_cap",
                        "required",
                        "recoverable_cap is required for deductible items",
                    )
                if cap is not None and cap > deductible:
                    _add(
                        errors,
                        f"{base}/recoverable_cap",
                        "recoverable_cap_exceeds_deductible",
                        "recoverable_cap must not exceed the deductible temporary difference",
                    )

            # 预计转回日须恰好被一个起止有效的税率区间覆盖。
            matched_rate: Decimal | None = None
            if reversal is not None:
                matches = [
                    band
                    for band in band_nodes
                    if band["start"] is not None
                    and band["end"] is not None
                    and band["start"] <= band["end"]
                    and band["start"] <= reversal <= band["end"]
                ]
                if len(matches) == 0:
                    _add(
                        errors,
                        f"{base}/expected_reversal_date",
                        "no_applicable_tax_rate",
                        "expected_reversal_date is not covered by any tax rate band",
                    )
                elif len(matches) > 1:
                    _add(
                        errors,
                        f"{base}/expected_reversal_date",
                        "ambiguous_tax_rate",
                        "expected_reversal_date is covered by more than one tax rate band",
                    )
                else:
                    matched_rate = matches[0]["rate"]

            item_nodes.append(
                {
                    "item_id": item_id,
                    "classification": classification,
                    "difference": difference,
                    "cap": cap,
                    "matched_rate": matched_rate,
                    "attribution": attribution,
                }
            )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        first = errors[0]
        raise DeferredTaxError(first["code"], first["path"], first["message"])

    # ---- 成功：逐项计量（先四舍五入到分），汇总由已舍入明细相加 ----
    result_items: list[dict[str, Any]] = []
    total_asset = _ZERO
    total_liability = _ZERO
    attribution_sums: dict[str, list[Decimal]] = {
        attribution: [_ZERO, _ZERO] for attribution in _ATTRIBUTIONS
    }
    for node in item_nodes:
        difference = node["difference"]
        rate = node["matched_rate"]
        recognized = _ZERO
        unrecognized = _ZERO
        asset_amount = _ZERO
        liability_amount = _ZERO
        if difference > 0:
            liability_amount = (difference * rate).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
        elif difference < 0:
            deductible = -difference
            recognized = node["cap"]
            unrecognized = deductible - recognized
            asset_amount = (recognized * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
        total_asset += asset_amount
        total_liability += liability_amount
        attribution_sums[node["attribution"]][0] += asset_amount
        attribution_sums[node["attribution"]][1] += liability_amount
        result_items.append(
            {
                "item_id": node["item_id"],
                "classification": node["classification"],
                "temporary_difference": _exact(difference),
                "tax_rate": _exact(rate),
                "recognized_deductible_difference": _exact(recognized),
                "unrecognized_deductible_difference": _exact(unrecognized),
                "deferred_tax_asset": _money(asset_amount),
                "deferred_tax_liability": _money(liability_amount),
                "attribution": node["attribution"],
            }
        )

    attribution_totals: dict[str, dict[str, str]] = {}
    for attribution in _ATTRIBUTIONS:
        attr_asset, attr_liability = attribution_sums[attribution]
        attribution_totals[attribution] = {
            "deferred_tax_asset": _money(attr_asset),
            "deferred_tax_liability": _money(attr_liability),
            "net_deferred_tax": _money(attr_asset - attr_liability),
        }

    return {
        "reporting_date": payload["reporting_date"],
        "currency": currency,
        "items": result_items,
        "total_deferred_tax_asset": _money(total_asset),
        "total_deferred_tax_liability": _money(total_liability),
        "net_deferred_tax": _money(total_asset - total_liability),
        "attribution_totals": attribution_totals,
    }
