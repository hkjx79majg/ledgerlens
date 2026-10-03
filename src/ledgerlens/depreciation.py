"""无状态固定资产直线法折旧计划（depreciation schedule）生成。

从 in_service_date 所在月的月末起，按整月连续计提 useful_life_months 个月。
可折旧金额为 acquisition_cost 减 residual_value：按整数分平均分配到各月，
余数从最早月份起各补一分；各月之和严格等于可折旧金额，期末累计折旧等于
成本减残值、账面净值等于残值。每个非零月份生成一张可通过既有凭证校验的
复式凭证：posting_date 取当月月末，voucher_id 为
``{voucher_id_prefix}-{YYYYMM}``，dep-1 行借记折旧费用科目、dep-2 行贷记
累计折旧科目；金额为 0.00 的月份仍保留在计划中，entry 为 null。

校验沿用既有错误码与排序规则：缺失、类型、空值、日期、币种错误与凭证校验
一致；科目体系整体复用 chart 校验器，错误路径加 /chart 前缀，
chart.effective_date 不得晚于 in_service_date（chart_not_effective）。
acquisition_cost 须为大于零、residual_value 须为不小于零且最多两位小数的
无符号字符串，否则报 invalid_amount；残值不小于成本时报
residual_not_less_than_cost；useful_life_months 须为 1 至 1200 的整数，
否则报 invalid_useful_life；两科目相同报 duplicate_depreciation_account；
末月超出日历范围报 schedule_out_of_range。两科目分别按存在
（unknown_account）、启用（inactive_account）、类别
（account_type_mismatch）依次只报一个，累计折旧科目须为 asset、折旧费用
科目须为 expense。关联错误只依赖自身字段有效：科目体系无效时不派生科目类
错误。纯函数实现：不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .trial_balance import (
    _AMOUNT_RE,
    _CURRENCY_RE,
    _ZERO,
    _add,
    _check_date,
    _check_nonempty_string,
    _escape,
    _money,
    _parse_date,
)

_REQUEST_FIELDS = (
    "asset_id",
    "voucher_id_prefix",
    "in_service_date",
    "currency",
    "acquisition_cost",
    "residual_value",
    "useful_life_months",
    "chart",
    "accumulated_depreciation_account_code",
    "depreciation_expense_account_code",
)

_MAX_USEFUL_LIFE_MONTHS = 1200
# 各科目要求的类别：累计折旧为 asset，折旧费用为 expense。
_EXPECTED_TYPES = {
    "accumulated_depreciation_account_code": "asset",
    "depreciation_expense_account_code": "expense",
}


def _check_cost_amount(
    errors: list[dict[str, str]], payload: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验金额字段：最多两位小数的无符号十进制字符串，合法时返回精确值。

    大于零 / 不小于零的下限由调用方按字段分别校验。
    """
    if key not in payload:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = payload[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a decimal string")
        return None
    if value == "":
        _add(errors, path, "blank_value", f"{key} must not be blank")
        return None
    if _AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be an unsigned decimal string with at most two fraction digits",
        )
        return None
    return Decimal(value)


def _check_useful_life(
    errors: list[dict[str, str]], payload: dict[str, Any]
) -> int | None:
    """校验 useful_life_months：1 至 1200 的整数。"""
    if "useful_life_months" not in payload:
        _add(errors, "/useful_life_months", "required", "useful_life_months is required")
        return None
    value = payload["useful_life_months"]
    if not isinstance(value, int) or isinstance(value, bool):
        _add(
            errors,
            "/useful_life_months",
            "invalid_type",
            "useful_life_months must be an integer",
        )
        return None
    if not 1 <= value <= _MAX_USEFUL_LIFE_MONTHS:
        _add(
            errors,
            "/useful_life_months",
            "invalid_useful_life",
            f"useful_life_months must be between 1 and {_MAX_USEFUL_LIFE_MONTHS}",
        )
        return None
    return value


def _month_ends(start: date, months: int) -> list[date] | None:
    """从 start 所在月起返回连续 months 个月的月末日期；超出日历范围返回 None。"""
    month_index = start.year * 12 + (start.month - 1)
    last_index = month_index + months - 1
    if last_index // 12 > 9999:
        return None
    ends: list[date] = []
    for offset in range(months):
        index = month_index + offset
        year, month = index // 12, index % 12 + 1
        ends.append(date(year, month, calendar.monthrange(year, month)[1]))
    return ends


def _allocate(total: Decimal, months: int) -> list[Decimal]:
    """把 total（至多两位小数）按整数分平均分配，余数从最早月份起各补一分。"""
    total_cents = int((total * 100).to_integral_exact())
    base, remainder = divmod(total_cents, months)
    return [
        Decimal(base + (1 if index < remainder else 0)) / 100
        for index in range(months)
    ]


def generate_depreciation_schedule(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成直线法月度折旧计划与复式分录，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    asset_id = _check_nonempty_string(errors, payload, "asset_id", "/asset_id")
    voucher_id_prefix = _check_nonempty_string(
        errors, payload, "voucher_id_prefix", "/voucher_id_prefix"
    )
    in_service_date = _check_date(errors, payload, "in_service_date", "/in_service_date")

    currency = _check_nonempty_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
        currency = None

    # ---- 金额：成本大于零、残值不小于零，均最多两位小数 ----
    acquisition_cost = _check_cost_amount(
        errors, payload, "acquisition_cost", "/acquisition_cost"
    )
    if acquisition_cost is not None and acquisition_cost <= 0:
        _add(
            errors,
            "/acquisition_cost",
            "invalid_amount",
            "acquisition_cost must be greater than zero",
        )
        acquisition_cost = None

    residual_value = _check_cost_amount(
        errors, payload, "residual_value", "/residual_value"
    )
    if (
        acquisition_cost is not None
        and residual_value is not None
        and residual_value >= acquisition_cost
    ):
        _add(
            errors,
            "/residual_value",
            "residual_not_less_than_cost",
            "residual_value must be less than acquisition_cost",
        )

    useful_life_months = _check_useful_life(errors, payload)

    # ---- 末月不得超出日历范围 ----
    month_ends: list[date] | None = None
    if in_service_date is not None and useful_life_months is not None:
        month_ends = _month_ends(in_service_date, useful_life_months)
        if month_ends is None:
            _add(
                errors,
                "/useful_life_months",
                "schedule_out_of_range",
                "depreciation schedule exceeds the supported calendar range",
            )

    # ---- chart：整体复用科目体系校验，错误路径加 /chart 前缀 ----
    chart_accounts: dict[str, dict[str, Any]] | None = None
    chart_id: str | None = None
    if "chart" not in payload:
        _add(errors, "/chart", "required", "chart is required")
    elif not isinstance(payload["chart"], dict):
        _add(errors, "/chart", "invalid_type", "chart must be an object")
    else:
        chart = payload["chart"]
        chart_status, chart_body = validate_chart_of_accounts(chart)
        if chart_status != 200:
            for err in chart_body["errors"]:
                _add(errors, "/chart" + err["path"], err["code"], err["message"])
        else:
            chart_id = chart_body["chart_id"]
            chart_accounts = {account["code"]: account for account in chart["accounts"]}
        # chart_not_effective 仅依赖 effective_date 字段本身与 in_service_date。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and in_service_date is not None and effective > in_service_date:
            _add(
                errors,
                "/chart/effective_date",
                "chart_not_effective",
                "chart effective_date must not be after in_service_date",
            )

    accumulated_code = _check_nonempty_string(
        errors,
        payload,
        "accumulated_depreciation_account_code",
        "/accumulated_depreciation_account_code",
    )
    expense_code = _check_nonempty_string(
        errors,
        payload,
        "depreciation_expense_account_code",
        "/depreciation_expense_account_code",
    )

    if (
        accumulated_code is not None
        and expense_code is not None
        and accumulated_code == expense_code
    ):
        _add(
            errors,
            "/depreciation_expense_account_code",
            "duplicate_depreciation_account",
            "accumulated_depreciation_account_code and "
            "depreciation_expense_account_code must differ",
        )

    # ---- 两科目：存在 -> 启用 -> 类别，依次只报一个；仅当科目体系整体有效时推导 ----
    if chart_accounts is not None:
        for code, path, key in (
            (
                accumulated_code,
                "/accumulated_depreciation_account_code",
                "accumulated_depreciation_account_code",
            ),
            (
                expense_code,
                "/depreciation_expense_account_code",
                "depreciation_expense_account_code",
            ),
        ):
            if code is None:
                continue
            account = chart_accounts.get(code)
            if account is None:
                _add(
                    errors,
                    path,
                    "unknown_account",
                    f"account_code {code!r} does not exist in the chart",
                )
            elif account["active"] is not True:
                _add(errors, path, "inactive_account", f"account {code!r} is inactive")
            else:
                expected_type = _EXPECTED_TYPES[key]
                if account["type"] != expected_type:
                    _add(
                        errors,
                        path,
                        "account_type_mismatch",
                        f"account {code!r} must be a {expected_type} account",
                    )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：按整数分平均分摊，逐月生成计划项与复式凭证 ----
    depreciable = acquisition_cost - residual_value
    amounts = _allocate(depreciable, useful_life_months)

    schedule: list[dict[str, Any]] = []
    accumulated = _ZERO
    for month_end, amount in zip(month_ends, amounts):
        accumulated += amount
        money = _money(amount)
        entry = None
        if amount != _ZERO:
            entry = {
                "voucher_id": f"{voucher_id_prefix}-{month_end:%Y%m}",
                "posting_date": month_end.isoformat(),
                "currency": currency,
                "lines": [
                    {
                        "line_id": "dep-1",
                        "account_code": expense_code,
                        "debit": money,
                        "credit": _money(_ZERO),
                    },
                    {
                        "line_id": "dep-2",
                        "account_code": accumulated_code,
                        "debit": _money(_ZERO),
                        "credit": money,
                    },
                ],
            }
        schedule.append(
            {
                "depreciation_date": month_end.isoformat(),
                "amount": money,
                "accumulated_depreciation": _money(accumulated),
                "net_book_value": _money(acquisition_cost - accumulated),
                "entry": entry,
            }
        )

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "asset_id": asset_id,
        "in_service_date": payload["in_service_date"],
        "currency": currency,
        "acquisition_cost": _money(acquisition_cost),
        "residual_value": _money(residual_value),
        "useful_life_months": useful_life_months,
        "depreciation_schedule": schedule,
    }
