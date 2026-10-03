"""无状态固定资产减值测算（asset impairment）与复式分录生成。

当可收回金额低于账面金额时，差额确认为减值损失：减值后账面金额等于
可收回金额，并生成一张可通过既有凭证校验的复式凭证——posting_date 取
测试日，voucher_id 沿用请求凭证标识，imp-1 借记减值损失科目，
imp-2 贷记累计减值科目。可收回金额不低于账面金额时损失为 0.00、
账面金额不变且不生成凭证（entry 为 null）。

校验沿用既有错误码与排序规则：缺失、类型、空值、日期、币种错误与
折旧端点一致；科目体系整体复用 chart 校验器，错误路径加 /chart 前缀，
chart.effective_date 不得晚于 test_date（chart_not_effective）。
carrying_amount 与 recoverable_amount 为至多两位小数的无符号十进制
字符串：账面金额须大于零、可收回金额须不小于零，否则 invalid_amount；
累计减值科目与减值损失科目相同报 duplicate_impairment_account；两科目
分别按存在（unknown_account）、启用（inactive_account）、类别
（account_type_mismatch）依次只报一个，累计减值科目须为 asset、
减值损失科目须为 expense。依赖字段无效时不派生关联错误：科目体系
无效时不派生科目类错误。纯函数实现：不落盘、不保留跨请求状态，
相同输入结果一致。
"""

from __future__ import annotations

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
    "voucher_id",
    "test_date",
    "currency",
    "carrying_amount",
    "recoverable_amount",
    "chart",
    "accumulated_impairment_account_code",
    "impairment_loss_account_code",
)


def _check_amount(
    errors: list[dict[str, str]], payload: dict[str, Any], key: str, *, positive: bool
) -> Decimal | None:
    """校验至多两位小数的无符号金额字符串。

    positive 为 True 时要求严格大于零（carrying_amount），为 False 时允许
    零（recoverable_amount）；格式或取值非法统一报 invalid_amount。
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


def generate_asset_impairment(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """进行固定资产减值测算并生成复式分录，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    asset_id = _check_nonempty_string(errors, payload, "asset_id", "/asset_id")
    voucher_id = _check_nonempty_string(errors, payload, "voucher_id", "/voucher_id")
    test_date_value = _check_date(errors, payload, "test_date", "/test_date")

    currency = _check_nonempty_string(errors, payload, "currency", "/currency")
    if currency is not None and _CURRENCY_RE.fullmatch(currency) is None:
        _add(
            errors,
            "/currency",
            "invalid_currency",
            "currency must be three uppercase ASCII letters",
        )
        currency = None

    carrying_amount = _check_amount(
        errors, payload, "carrying_amount", positive=True
    )
    recoverable_amount = _check_amount(
        errors, payload, "recoverable_amount", positive=False
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
        # chart_not_effective 仅依赖 effective_date 字段本身与 test_date。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and test_date_value is not None:
            if effective > test_date_value:
                _add(
                    errors,
                    "/chart/effective_date",
                    "chart_not_effective",
                    "chart effective_date must not be after test_date",
                )

    accumulated_code = _check_nonempty_string(
        errors,
        payload,
        "accumulated_impairment_account_code",
        "/accumulated_impairment_account_code",
    )
    loss_code = _check_nonempty_string(
        errors,
        payload,
        "impairment_loss_account_code",
        "/impairment_loss_account_code",
    )

    if accumulated_code is not None and loss_code is not None:
        if accumulated_code == loss_code:
            _add(
                errors,
                "/impairment_loss_account_code",
                "duplicate_impairment_account",
                "accumulated_impairment_account_code and "
                "impairment_loss_account_code must differ",
            )

    # ---- 两科目：存在 -> 启用 -> 类别，依次只报一个；仅当科目体系整体
    #      有效时推导。 ----
    if chart_accounts is not None:
        for code, path, expected_type in (
            (
                accumulated_code,
                "/accumulated_impairment_account_code",
                "asset",
            ),
            (loss_code, "/impairment_loss_account_code", "expense"),
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

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：可收回金额低于账面金额时确认减值损失 ----
    if recoverable_amount < carrying_amount:
        impairment_loss = carrying_amount - recoverable_amount
        post_carrying = recoverable_amount
        loss_money = _money(impairment_loss)
        entry: dict[str, Any] | None = {
            "voucher_id": voucher_id,
            "posting_date": test_date_value.isoformat(),
            "currency": currency,
            "lines": [
                {
                    "line_id": "imp-1",
                    "account_code": loss_code,
                    "debit": loss_money,
                    "credit": _money(_ZERO),
                },
                {
                    "line_id": "imp-2",
                    "account_code": accumulated_code,
                    "debit": _money(_ZERO),
                    "credit": loss_money,
                },
            ],
        }
    else:
        impairment_loss = _ZERO
        post_carrying = carrying_amount
        entry = None

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "asset_id": asset_id,
        "test_date": payload["test_date"],
        "currency": currency,
        "carrying_amount": _money(carrying_amount),
        "recoverable_amount": _money(recoverable_amount),
        "impairment_loss": _money(impairment_loss),
        "post_impairment_carrying_amount": _money(post_carrying),
        "entry": entry,
    }
