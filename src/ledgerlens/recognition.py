"""无状态权责发生确认计划（recognition schedule）生成。

按自然月把 [start_date, end_date] 切段（首尾日均计入天数），各段按天数占比
分配 total_amount：使用精确十进制，份额先向下取整到分，剩余分按小数余数
从大到小分配，余数相同时较早月份优先；各段金额之和严格等于 total_amount。
每段生成一张可通过既有凭证校验的复式凭证：posting_date 取分段末日，
voucher_id 为 ``{voucher_id_prefix}-{YYYYMM}``；revenue 模式借记来源科目、
贷记目标科目，expense 模式借记目标科目、贷记来源科目。分摊为 0.00 的分段
无法通过凭证校验（借贷两侧均为零），不出现在计划中。

校验沿用既有错误码与排序规则：缺失、类型、空值、日期、币种错误与凭证校验
一致；科目体系整体复用 chart 校验器，错误路径加 /chart 前缀，
chart.effective_date 不得晚于 start_date（chart_not_effective）。start_date
晚于 end_date 报 invalid_period；total_amount 不是大于零且最多两位小数的
无符号字符串时报 invalid_amount；recognition_type 只接受 revenue 或
expense，否则报 invalid_recognition_type；两科目相同报
duplicate_recognition_account；两科目分别按存在（unknown_account）、启用
（inactive_account）、类别（account_type_mismatch）依次只报一个，revenue
要求来源为 liability、目标为 revenue，expense 要求来源为 asset、目标为
expense。关联错误只依赖自身字段有效：科目体系无效时不派生科目类错误，
recognition_type 无效时不派生类别错误。纯函数实现：不落盘、不保留跨请求
状态，相同输入结果一致。
"""

from __future__ import annotations

from datetime import date, timedelta
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
    "contract_id",
    "recognition_type",
    "start_date",
    "end_date",
    "currency",
    "total_amount",
    "chart",
    "source_account_code",
    "target_account_code",
    "voucher_id_prefix",
)

_RECOGNITION_TYPES = ("revenue", "expense")
# 各模式下 (来源科目类别, 目标科目类别)。
_EXPECTED_TYPES = {
    "revenue": ("liability", "revenue"),
    "expense": ("asset", "expense"),
}


def _check_total_amount(
    errors: list[dict[str, str]], payload: dict[str, Any]
) -> Decimal | None:
    """校验 total_amount：大于零且最多两位小数的无符号十进制字符串。"""
    if "total_amount" not in payload:
        _add(errors, "/total_amount", "required", "total_amount is required")
        return None
    value = payload["total_amount"]
    if not isinstance(value, str):
        _add(errors, "/total_amount", "invalid_type", "total_amount must be a decimal string")
        return None
    if value == "":
        _add(errors, "/total_amount", "blank_value", "total_amount must not be blank")
        return None
    if _AMOUNT_RE.fullmatch(value) is None or Decimal(value) <= 0:
        _add(
            errors,
            "/total_amount",
            "invalid_amount",
            "total_amount must be a positive unsigned decimal string with at most two fraction digits",
        )
        return None
    return Decimal(value)


def _month_segments(start: date, end: date) -> list[tuple[date, date, int]]:
    """把闭区间 [start, end] 按自然月切段，返回 (段首, 段末, 天数) 列表。"""
    segments: list[tuple[date, date, int]] = []
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            month_end = date(cursor.year, 12, 31)
        else:
            month_end = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
        seg_end = min(month_end, end)
        segments.append((cursor, seg_end, (seg_end - cursor).days + 1))
        cursor = seg_end + timedelta(days=1)
    return segments


def _allocate(total: Decimal, days_list: list[int]) -> list[Decimal]:
    """按天数占比把 total（至多两位小数）分配到各段，返回各段精确金额。

    份额先向下取整到分，剩余分按小数余数从大到小各补一分，余数相同时
    较早月份优先；各段之和严格等于 total。
    """
    total_cents = int((total * 100).to_integral_exact())
    total_days = sum(days_list)
    cents: list[int] = []
    remainders: list[int] = []
    for days in days_list:
        quotient, remainder = divmod(total_cents * days, total_days)
        cents.append(quotient)
        remainders.append(remainder)
    leftover = total_cents - sum(cents)
    order = sorted(range(len(days_list)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        cents[i] += 1
    return [Decimal(c) / 100 for c in cents]


def generate_recognition_schedule(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成月度确认计划与复式分录，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    contract_id = _check_nonempty_string(errors, payload, "contract_id", "/contract_id")

    recognition_type = _check_nonempty_string(
        errors, payload, "recognition_type", "/recognition_type"
    )
    if recognition_type is not None and recognition_type not in _RECOGNITION_TYPES:
        _add(
            errors,
            "/recognition_type",
            "invalid_recognition_type",
            f"recognition_type must be one of {', '.join(_RECOGNITION_TYPES)}",
        )
        recognition_type = None

    # ---- 期间：真实日期且先后有序 ----
    start_date = _check_date(errors, payload, "start_date", "/start_date")
    end_date = _check_date(errors, payload, "end_date", "/end_date")
    if start_date is not None and end_date is not None and start_date > end_date:
        _add(
            errors,
            "/start_date",
            "invalid_period",
            "start_date must be on or before end_date",
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

    total_amount = _check_total_amount(errors, payload)

    voucher_id_prefix = _check_nonempty_string(
        errors, payload, "voucher_id_prefix", "/voucher_id_prefix"
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
        # chart_not_effective 仅依赖 effective_date 字段本身与 start_date。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and start_date is not None and effective > start_date:
            _add(
                errors,
                "/chart/effective_date",
                "chart_not_effective",
                "chart effective_date must not be after start_date",
            )

    source_code = _check_nonempty_string(
        errors, payload, "source_account_code", "/source_account_code"
    )
    target_code = _check_nonempty_string(
        errors, payload, "target_account_code", "/target_account_code"
    )

    if source_code is not None and target_code is not None and source_code == target_code:
        _add(
            errors,
            "/target_account_code",
            "duplicate_recognition_account",
            "source_account_code and target_account_code must differ",
        )

    # ---- 两科目：存在 -> 启用 -> 类别，依次只报一个；仅当科目体系整体有效
    #      时推导，类别校验另需 recognition_type 有效。 ----
    if chart_accounts is not None:
        expected = _EXPECTED_TYPES.get(recognition_type) if recognition_type else None
        for code, path, role in (
            (source_code, "/source_account_code", "source"),
            (target_code, "/target_account_code", "target"),
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
            elif expected is not None:
                expected_type = expected[0] if role == "source" else expected[1]
                if account["type"] != expected_type:
                    _add(
                        errors,
                        path,
                        "account_type_mismatch",
                        f"{role} account {code!r} must be a {expected_type} account "
                        f"for {recognition_type} recognition",
                    )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：按自然月切段、按天数占比分摊，逐段生成复式凭证 ----
    segments = _month_segments(start_date, end_date)
    amounts = _allocate(total_amount, [days for _, _, days in segments])

    if recognition_type == "revenue":
        debit_code, credit_code = source_code, target_code
    else:
        debit_code, credit_code = target_code, source_code

    schedule: list[dict[str, Any]] = []
    for (seg_start, seg_end, days), amount in zip(segments, amounts):
        if amount == _ZERO:
            # 借贷两侧均为零无法通过既有凭证校验，该分段不生成计划项。
            continue
        money = _money(amount)
        schedule.append(
            {
                "period_start": seg_start.isoformat(),
                "period_end": seg_end.isoformat(),
                "days": days,
                "amount": money,
                "entry": {
                    "voucher_id": f"{voucher_id_prefix}-{seg_end:%Y%m}",
                    "posting_date": seg_end.isoformat(),
                    "currency": currency,
                    "lines": [
                        {
                            "line_id": "rec-1",
                            "account_code": debit_code,
                            "debit": money,
                            "credit": _money(_ZERO),
                        },
                        {
                            "line_id": "rec-2",
                            "account_code": credit_code,
                            "debit": _money(_ZERO),
                            "credit": money,
                        },
                    ],
                },
            }
        )

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "contract_id": contract_id,
        "recognition_type": recognition_type,
        "start_date": payload["start_date"],
        "end_date": payload["end_date"],
        "currency": currency,
        "total_amount": _money(total_amount),
        "recognition_schedule": schedule,
    }
