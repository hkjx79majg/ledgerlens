"""无状态固定资产直线法折旧计划（depreciation schedule）生成。

从启用日（in_service_date）所在月末起按整月连续计提 useful_life_months 个月：
可折旧金额（acquisition_cost - residual_value）按整数分平均分配到各月，
余数从最早月份起各补一分。末月计提日不得超出日历范围（date.max 为
9999-12-31），否则报 schedule_out_of_range。

每月计划项含计提日、本月金额、截至该月的累计折旧与账面净值。非零月份生成
一张可通过既有凭证校验的复式凭证：posting_date 取当月月末，voucher_id 为
``{voucher_id_prefix}-{YYYYMM}``，dep-1 借记折旧费用科目，dep-2 贷记累计折旧
科目。本月金额为 0.00 的月份仍保留在计划中，entry 为 null。

校验沿用既有错误码与排序规则：缺失、类型、空值、日期、币种错误与凭证校验
一致；科目体系整体复用 chart 校验器，错误路径加 /chart 前缀，
chart.effective_date 不得晚于 in_service_date（chart_not_effective）。
acquisition_cost 与 residual_value 为至多两位小数的无符号十进制字符串：
成本须大于零、残值须不小于零，否则 invalid_amount；残值不小于成本报
residual_not_less_than_cost；useful_life_months 须为 1 至 1200 的整数
（布尔不算整数），否则 invalid_useful_life；累计折旧科目与折旧费用科目相同
报 duplicate_depreciation_account；两科目分别按存在（unknown_account）、启用
（inactive_account）、类别（account_type_mismatch）依次只报一个，累计折旧科目
须为 asset、折旧费用科目须为 expense。依赖字段无效时不派生关联错误：科目体系
无效时不派生科目类错误，金额字段无效时不派生残值比较，月数或启用日无效时不
派生日历范围错误。纯函数实现：不落盘、不保留跨请求状态，相同输入结果一致。
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

_MIN_LIFE_MONTHS = 1
_MAX_LIFE_MONTHS = 1200


def _check_cost(
    errors: list[dict[str, str]], payload: dict[str, Any], key: str, *, positive: bool
) -> Decimal | None:
    """校验至多两位小数的无符号金额字符串。

    positive 为 True 时要求严格大于零（acquisition_cost），为 False 时允许零
    （residual_value）；格式或取值非法统一报 invalid_amount。
    """
    if key not in payload:
        _add(errors, f"/{key}", "required", f"{key} is required")
        return None
    value = payload[key]
    if not isinstance(value, str):
        _add(errors, f"/{key}", "invalid_type", f"{key} must be a decimal string")
        return None
    if value == "":
        _add(errors, f"/{key}", "blank_value", f"{key} must not be blank")
        return None
    if _AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            f"/{key}",
            "invalid_amount",
            f"{key} must be an unsigned decimal string with at most two fraction digits",
        )
        return None
    amount = Decimal(value)
    if positive and amount <= 0:
        _add(
            errors,
            f"/{key}",
            "invalid_amount",
            f"{key} must be greater than zero",
        )
        return None
    return amount


def _check_useful_life(errors: list[dict[str, str]], payload: dict[str, Any]) -> int | None:
    """校验 useful_life_months：1 至 1200 的整数（bool 不算整数）。"""
    key = "useful_life_months"
    if key not in payload:
        _add(errors, f"/{key}", "required", f"{key} is required")
        return None
    value = payload[key]
    # bool 是 int 的子类型，须先行排除。
    if isinstance(value, bool) or not isinstance(value, int):
        _add(errors, f"/{key}", "invalid_type", f"{key} must be an integer")
        return None
    if not _MIN_LIFE_MONTHS <= value <= _MAX_LIFE_MONTHS:
        _add(
            errors,
            f"/{key}",
            "invalid_useful_life",
            f"{key} must be an integer between {_MIN_LIFE_MONTHS} and {_MAX_LIFE_MONTHS}",
        )
        return None
    return value


def _month_end(year: int, month: int) -> date | None:
    """返回给定年月的月末日期；超出日历范围时返回 None。"""
    try:
        return date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, OverflowError):
        return None


def generate_depreciation_schedule(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
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
    in_service_date = _check_date(
        errors, payload, "in_service_date", "/in_service_date"
    )

    currency = _check_nonempty_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
        currency = None

    acquisition_cost = _check_cost(
        errors, payload, "acquisition_cost", positive=True
    )
    residual_value = _check_cost(
        errors, payload, "residual_value", positive=False
    )
    # residual_not_less_than_cost 仅依赖两个金额字段本身有效。
    if acquisition_cost is not None and residual_value is not None:
        if residual_value >= acquisition_cost:
            _add(
                errors,
                "/residual_value",
                "residual_not_less_than_cost",
                "residual_value must be less than acquisition_cost",
            )

    useful_life_months = _check_useful_life(errors, payload)

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
        if effective is not None and in_service_date is not None:
            if effective > in_service_date:
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

    if accumulated_code is not None and expense_code is not None:
        if accumulated_code == expense_code:
            _add(
                errors,
                "/depreciation_expense_account_code",
                "duplicate_depreciation_account",
                "accumulated_depreciation_account_code and "
                "depreciation_expense_account_code must differ",
            )

    # ---- 两科目：存在 -> 启用 -> 类别，依次只报一个；仅当科目体系整体
    #      有效且两科目不相同时推导。 ----
    if chart_accounts is not None:
        for code, path, expected_type in (
            (
                accumulated_code,
                "/accumulated_depreciation_account_code",
                "asset",
            ),
            (expense_code, "/depreciation_expense_account_code", "expense"),
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
            elif account["type"] != expected_type:
                _add(
                    errors,
                    path,
                    "account_type_mismatch",
                    f"account {code!r} must be a {expected_type} account",
                )

    # ---- 末月超出日历范围：仅依赖启用日与月数均有效 ----
    depreciation_months: list[date] = []
    schedule_out_of_range = False
    if in_service_date is not None and useful_life_months is not None:
        start_year = in_service_date.year
        start_month = in_service_date.month
        for offset in range(useful_life_months):
            index = start_month - 1 + offset
            year, month = start_year + index // 12, index % 12 + 1
            month_end = _month_end(year, month)
            if month_end is None:
                schedule_out_of_range = True
                break
            depreciation_months.append(month_end)
        if schedule_out_of_range:
            _add(
                errors,
                "/in_service_date",
                "schedule_out_of_range",
                "last depreciation month is beyond the supported calendar range",
            )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：可折旧金额按整数分平均分配，余数从最早月份起各补一分 ----
    depreciable_cents = int(
        ((acquisition_cost - residual_value) * 100).to_integral_exact()
    )
    base, leftover = divmod(depreciable_cents, useful_life_months)
    monthly_cents = [
        base + (1 if index < leftover else 0)
        for index in range(useful_life_months)
    ]

    cost_cents = int((acquisition_cost * 100).to_integral_exact())
    schedule: list[dict[str, Any]] = []
    accumulated_cents = 0
    for posting_date, cents in zip(depreciation_months, monthly_cents):
        accumulated_cents += cents
        net_book_cents = cost_cents - accumulated_cents
        amount = Decimal(cents) / 100
        if cents == 0:
            # 借贷两侧均为零无法通过既有凭证校验：保留该月份但不生成凭证。
            entry: dict[str, Any] | None = None
        else:
            money = _money(amount)
            entry = {
                "voucher_id": f"{voucher_id_prefix}-{posting_date:%Y%m}",
                "posting_date": posting_date.isoformat(),
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
                "posting_date": posting_date.isoformat(),
                "amount": _money(amount),
                "accumulated_depreciation": _money(Decimal(accumulated_cents) / 100),
                "net_book_value": _money(Decimal(net_book_cents) / 100),
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
