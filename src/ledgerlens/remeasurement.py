"""无状态外币货币性项目期末重估（foreign currency remeasurement）与复式分录生成。

按结账日汇率重估外币货币性资产与负债头寸：折算额为外币金额乘结账汇率
四舍五入至两位小数，相对现有本位币账面金额的差额为调整额——资产增加记借、
减少记贷，负债相反。全部非零调整按输入顺序进入同一凭证，账户行净额以
汇兑收益科目贷记或汇兑损失科目借记抵平（净额为零时不加汇兑行），行标识
依次为 fxr-1、fxr-2……凭证沿用请求的 voucher_id、以重估日为
posting_date、币种取请求本位币，可通过既有凭证校验。全部调整为零时净
汇兑收益、损失均为 0.00 且 entry 为 null。

校验沿用既有错误码与排序规则：缺失、类型、空值、日期、币种错误与减值
端点一致；科目体系整体复用 chart 校验器，错误路径加 /chart 前缀，
chart.effective_date 不得晚于 remeasurement_date（chart_not_effective）。
positions 为非空数组，每项含唯一 position_id（重复报
duplicate_position_id）、account_code、foreign_currency、foreign_amount、
carrying_amount、exchange_rate；头寸层同样拒绝未知字段。foreign_amount
须大于零、carrying_amount 可不小于零，均为至多两位小数的无符号十进制
字符串，否则报 invalid_amount；exchange_rate 须大于零、至多八位小数且
不用指数，否则报 invalid_exchange_rate。外币等于本位币报
functional_currency_position；同一科目与外币组合重复报 duplicate_position。
头寸科目须为启用的 asset 或 liability，汇兑收益、损失科目分别须为启用的
revenue、expense，均按存在（unknown_account）、启用（inactive_account）、
类别（account_type_mismatch）依次只报一个；两汇兑科目相同报
duplicate_fx_account。关联字段无效时不派生错误：科目体系无效时不派生
科目类错误，币种或头寸字段无效时不派生外币与组合重复错误。纯函数实现：
不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .trial_balance import (
    _AMOUNT_RE,
    _CENT,
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
    "voucher_id",
    "remeasurement_date",
    "currency",
    "chart",
    "fx_gain_account_code",
    "fx_loss_account_code",
    "positions",
)
_POSITION_FIELDS = (
    "position_id",
    "account_code",
    "foreign_currency",
    "foreign_amount",
    "carrying_amount",
    "exchange_rate",
)

# 汇率：无符号、无指数、最多八位小数的十进制字符串。
_RATE_RE = re.compile(r"\d+(\.\d{1,8})?")

# 头寸科目只接受货币性资产与负债。
_POSITION_ACCOUNT_TYPES = ("asset", "liability")


def _check_money(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str, *, positive: bool
) -> Decimal | None:
    """校验至多两位小数的无符号金额字符串。

    positive 为 True 时要求严格大于零（foreign_amount），为 False 时允许
    零（carrying_amount）；格式或取值非法统一报 invalid_amount。
    """
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
    if _AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            path,
            "invalid_amount",
            f"{key} must be an unsigned decimal string with at most two fraction digits",
        )
        return None
    amount = Decimal(value)
    if positive and amount <= 0:
        _add(errors, path, "invalid_amount", f"{key} must be greater than zero")
        return None
    return amount


def _check_rate(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Decimal | None:
    """校验汇率字段：大于零、至多八位小数、不用指数，否则报 invalid_exchange_rate。"""
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
            "invalid_exchange_rate",
            f"{key} must be an unsigned decimal string with at most eight fraction digits",
        )
        return None
    rate = Decimal(value)
    if rate <= 0:
        _add(errors, path, "invalid_exchange_rate", f"{key} must be greater than zero")
        return None
    return rate


def generate_foreign_currency_remeasurement(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """按结账日汇率重估外币货币性头寸并生成复式分录，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    voucher_id = _check_nonempty_string(errors, payload, "voucher_id", "/voucher_id")
    remeasurement_date = _check_date(
        errors, payload, "remeasurement_date", "/remeasurement_date"
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
        # chart_not_effective 仅依赖 effective_date 字段本身与 remeasurement_date。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and remeasurement_date is not None:
            if effective > remeasurement_date:
                _add(
                    errors,
                    "/chart/effective_date",
                    "chart_not_effective",
                    "chart effective_date must not be after remeasurement_date",
                )

    gain_code = _check_nonempty_string(
        errors, payload, "fx_gain_account_code", "/fx_gain_account_code"
    )
    loss_code = _check_nonempty_string(
        errors, payload, "fx_loss_account_code", "/fx_loss_account_code"
    )

    if gain_code is not None and loss_code is not None and gain_code == loss_code:
        _add(
            errors,
            "/fx_loss_account_code",
            "duplicate_fx_account",
            "fx_gain_account_code and fx_loss_account_code must differ",
        )

    # ---- positions：逐项字段校验 ----
    position_nodes: list[dict[str, Any]] = []
    if "positions" not in payload:
        _add(errors, "/positions", "required", "positions is required")
    elif not isinstance(payload["positions"], list):
        _add(errors, "/positions", "invalid_type", "positions must be an array")
    else:
        positions = payload["positions"]
        if len(positions) < 1:
            _add(
                errors,
                "/positions",
                "too_few_positions",
                "positions must contain at least one position",
            )
        seen_ids: set[str] = set()
        seen_combinations: set[tuple[str, str]] = set()
        for index, item in enumerate(positions):
            base = f"/positions/{index}"
            if not isinstance(item, dict):
                _add(errors, base, "invalid_type", "position must be an object")
                continue
            for key in item:
                if key not in _POSITION_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            position_id = _check_nonempty_string(
                errors, item, "position_id", f"{base}/position_id"
            )
            if position_id is not None:
                if position_id in seen_ids:
                    _add(
                        errors,
                        f"{base}/position_id",
                        "duplicate_position_id",
                        f"position_id {position_id!r} is duplicated within positions",
                    )
                else:
                    seen_ids.add(position_id)
            account_code = _check_nonempty_string(
                errors, item, "account_code", f"{base}/account_code"
            )
            foreign_currency = _check_nonempty_string(
                errors, item, "foreign_currency", f"{base}/foreign_currency"
            )
            if foreign_currency is not None and _CURRENCY_RE.fullmatch(foreign_currency) is None:
                _add(
                    errors,
                    f"{base}/foreign_currency",
                    "invalid_currency",
                    "foreign_currency must be three uppercase ASCII letters",
                )
                foreign_currency = None
            foreign_amount = _check_money(
                errors, item, "foreign_amount", f"{base}/foreign_amount", positive=True
            )
            carrying_amount = _check_money(
                errors, item, "carrying_amount", f"{base}/carrying_amount", positive=False
            )
            exchange_rate = _check_rate(
                errors, item, "exchange_rate", f"{base}/exchange_rate"
            )
            # 关联错误仅依赖对应字段本身有效。
            if (
                currency is not None
                and foreign_currency is not None
                and foreign_currency == currency
            ):
                _add(
                    errors,
                    f"{base}/foreign_currency",
                    "functional_currency_position",
                    "foreign_currency must differ from the functional currency",
                )
            if account_code is not None and foreign_currency is not None:
                combination = (account_code, foreign_currency)
                if combination in seen_combinations:
                    _add(
                        errors,
                        f"{base}/account_code",
                        "duplicate_position",
                        "account_code and foreign_currency combination "
                        "is duplicated within positions",
                    )
                else:
                    seen_combinations.add(combination)
            position_nodes.append(
                {
                    "base": base,
                    "position_id": position_id,
                    "account_code": account_code,
                    "foreign_currency": foreign_currency,
                    "foreign_amount": foreign_amount,
                    "carrying_amount": carrying_amount,
                    "exchange_rate": exchange_rate,
                    "exchange_rate_raw": item.get("exchange_rate"),
                }
            )

    # ---- 科目：存在 -> 启用 -> 类别，依次只报一个；仅当科目体系整体
    #      有效时推导。头寸科目须为 asset 或 liability，汇兑收益、损失
    #      科目分别须为 revenue、expense。 ----
    if chart_accounts is not None:
        for code, path, expected_type in (
            (gain_code, "/fx_gain_account_code", ("revenue",)),
            (loss_code, "/fx_loss_account_code", ("expense",)),
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
            elif account["type"] not in expected_type:
                _add(
                    errors,
                    path,
                    "account_type_mismatch",
                    f"account {code!r} must be a {expected_type[0]} account",
                )
        for node in position_nodes:
            code = node["account_code"]
            if code is None:
                continue
            account = chart_accounts.get(code)
            path = f"{node['base']}/account_code"
            if account is None:
                _add(
                    errors,
                    path,
                    "unknown_account",
                    f"account_code {code!r} does not exist in the chart",
                )
            elif account["active"] is not True:
                _add(errors, path, "inactive_account", f"account {code!r} is inactive")
            elif account["type"] not in _POSITION_ACCOUNT_TYPES:
                _add(
                    errors,
                    path,
                    "account_type_mismatch",
                    f"account {code!r} must be an asset or liability account",
                )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：逐头寸折算并与账面金额比较 ----
    results: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    net_debit = _ZERO
    for node in position_nodes:
        remeasured = (node["foreign_amount"] * node["exchange_rate"]).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        adjustment = remeasured - node["carrying_amount"]
        account_type = chart_accounts[node["account_code"]]["type"]
        # 资产增加记借、减少记贷，负债相反；零调整无方向。
        if adjustment > 0:
            side: str | None = "debit" if account_type == "asset" else "credit"
        elif adjustment < 0:
            side = "credit" if account_type == "asset" else "debit"
        else:
            side = None
        results.append(
            {
                "position_id": node["position_id"],
                "account_code": node["account_code"],
                "foreign_currency": node["foreign_currency"],
                "foreign_amount": _money(node["foreign_amount"]),
                "carrying_amount": _money(node["carrying_amount"]),
                "exchange_rate": node["exchange_rate_raw"],
                "remeasured_amount": _money(remeasured),
                "adjustment": _money(adjustment),
                "side": side,
            }
        )
        if adjustment != 0:
            amount = abs(adjustment)
            debit = amount if side == "debit" else _ZERO
            credit = amount if side == "credit" else _ZERO
            net_debit += debit - credit
            lines.append(
                {
                    "line_id": f"fxr-{len(lines) + 1}",
                    "account_code": node["account_code"],
                    "debit": _money(debit),
                    "credit": _money(credit),
                }
            )

    # ---- 账户行净额以汇兑收益贷记或汇兑损失借记抵平；净额为零不加汇兑行 ----
    if net_debit > 0:
        net_fx_gain, net_fx_loss = net_debit, _ZERO
        lines.append(
            {
                "line_id": f"fxr-{len(lines) + 1}",
                "account_code": gain_code,
                "debit": _money(_ZERO),
                "credit": _money(net_debit),
            }
        )
    elif net_debit < 0:
        net_fx_gain, net_fx_loss = _ZERO, -net_debit
        lines.append(
            {
                "line_id": f"fxr-{len(lines) + 1}",
                "account_code": loss_code,
                "debit": _money(-net_debit),
                "credit": _money(_ZERO),
            }
        )
    else:
        net_fx_gain = net_fx_loss = _ZERO

    entry: dict[str, Any] | None = None
    if lines:
        entry = {
            "voucher_id": voucher_id,
            "posting_date": remeasurement_date.isoformat(),
            "currency": currency,
            "lines": lines,
        }

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "voucher_id": voucher_id,
        "remeasurement_date": payload["remeasurement_date"],
        "currency": currency,
        "positions": results,
        "net_fx_gain": _money(net_fx_gain),
        "net_fx_loss": _money(net_fx_loss),
        "entry": entry,
    }
