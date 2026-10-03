"""无状态跨月权责发生（待摊费用 / 递延收入）月度计划生成。

请求在独立顶层字段上描述一份合同总额与摊销区间：

- ``contract_id``、``voucher_id_prefix``：非空字符串。
- ``recognition_type``：仅 ``revenue``（递延收入）或 ``expense``（待摊费用），
  否则报 ``invalid_recognition_type``。
- ``start_date`` / ``end_date``：真实日历日期，首尾日均计入天数；倒置时在
  ``/start_date`` 报 ``invalid_period``。
- ``currency``：三位大写字母；``total_amount``：大于零且最多两位小数的无符号
  十进制字符串，否则报 ``invalid_amount``。
- ``chart``：与科目体系校验同构，错误路径加 ``/chart`` 前缀；其生效日不得晚于
  ``start_date``（``chart_not_effective``）。
- ``source_account_code`` / ``target_account_code``：非空字符串且不得相同
  （相同在 ``/target_account_code`` 报 ``duplicate_recognition_account``）；
  仅当科目体系整体有效且模式合法时，依次按存在（``unknown_account``）、启用
  （``inactive_account``）、类别（``account_type_mismatch``，在对应字段上报）
  校验，每科只报一个。revenue 要求来源为 liability、目标为 revenue；expense
  要求来源为 asset、目标为 expense。

区间按自然月切段，各段按天数占比分配总额：份额先向下取整到分，剩余分按小数
余数从大到小分配，余数相同时较早月份优先，各项金额之和严格等于
``total_amount``。每项以分段末日为 ``posting_date``、以
``voucher_id_prefix-YYYYMM`` 为 ``voucher_id`` 生成复式凭证：revenue 借记
来源、贷记目标，expense 借记目标、贷记来源；币种不变、借贷相等，可通过既有
凭证校验。

缺失、类型、空值、日期、币种等通用字段错误沿用既有错误码；失败返回 422、
``valid: false`` 与按 ``path``、``code`` 排序的 ``errors``。纯函数实现：
不落盘、不保留跨请求状态，相同输入结果一致。
"""

from __future__ import annotations

import calendar
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .trial_balance import (
    _AMOUNT_RE,
    _CENT,
    _CURRENCY_RE,
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

# 各模式下两个科目字段的期望类别：revenue 借记负债来源、贷记收入目标；
# expense 借记费用目标、贷记资产来源。
_EXPECTED_TYPES = {
    "revenue": {"source_account_code": "liability", "target_account_code": "revenue"},
    "expense": {"source_account_code": "asset", "target_account_code": "expense"},
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
    if _AMOUNT_RE.fullmatch(value) is None:
        _add(
            errors,
            "/total_amount",
            "invalid_amount",
            "total_amount must be an unsigned decimal string with at most two fraction digits",
        )
        return None
    amount = Decimal(value)
    if amount <= 0:
        _add(
            errors,
            "/total_amount",
            "invalid_amount",
            "total_amount must be greater than zero",
        )
        return None
    return amount


def _month_segments(start, end) -> list[dict[str, Any]]:
    """把闭区间 [start, end] 按自然月切段，每段给出起止日期与计入天数。"""
    segments: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        month_end = cursor.replace(
            day=calendar.monthrange(cursor.year, cursor.month)[1]
        )
        segment_end = min(month_end, end)
        segments.append(
            {
                "start": cursor,
                "end": segment_end,
                "days": (segment_end - cursor).days + 1,
            }
        )
        cursor = segment_end + timedelta(days=1)
    return segments


def _allocate(total_cents: int, days: list[int]) -> list[int]:
    """按天数占比把 total_cents 分到各段：先向下取整，剩余分按小数余数
    从大到小分配，余数相同时较早月份优先。返回各段分额，和严格等于总额。"""
    total_days = sum(days)
    floors: list[int] = []
    remainders: list[int] = []
    for segment_days in days:
        quotient, remainder = divmod(total_cents * segment_days, total_days)
        floors.append(quotient)
        remainders.append(remainder)
    leftover = total_cents - sum(floors)
    shares = list(floors)
    # 余数降序、下标升序：leftover 小于段数，每段至多补一分。
    for index in sorted(range(len(days)), key=lambda i: (-remainders[i], i))[:leftover]:
        shares[index] += 1
    return shares


def generate_recognition_schedule(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """生成月度权责发生计划与复式分录，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- 通用字段 ----
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
    source_code = _check_nonempty_string(
        errors, payload, "source_account_code", "/source_account_code"
    )
    target_code = _check_nonempty_string(
        errors, payload, "target_account_code", "/target_account_code"
    )

    # ---- 两科目不得相同：只依赖两个字段本身有效 ----
    if source_code is not None and target_code is not None and source_code == target_code:
        _add(
            errors,
            "/target_account_code",
            "duplicate_recognition_account",
            "source_account_code and target_account_code must differ",
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

    # ---- 科目：仅当科目体系整体有效且模式合法时，按存在、启用、类别依次校验 ----
    if chart_accounts is not None and recognition_type is not None:
        expected = _EXPECTED_TYPES[recognition_type]
        for key, code in (
            ("source_account_code", source_code),
            ("target_account_code", target_code),
        ):
            if code is None:
                continue
            account = chart_accounts.get(code)
            if account is None:
                _add(
                    errors,
                    f"/{key}",
                    "unknown_account",
                    f"account_code {code!r} does not exist in the chart",
                )
            elif account["active"] is not True:
                _add(
                    errors,
                    f"/{key}",
                    "inactive_account",
                    f"account {code!r} is inactive",
                )
            elif account["type"] != expected[key]:
                _add(
                    errors,
                    f"/{key}",
                    "account_type_mismatch",
                    f"{key} {code!r} must be a {expected[key]} account for {recognition_type} recognition",
                )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：按自然月切段并按天数占比分配 ----
    segments = _month_segments(start_date, end_date)
    total_cents = int(total_amount * 100)
    shares = _allocate(total_cents, [segment["days"] for segment in segments])

    # revenue 借记来源、贷记目标；expense 借记目标、贷记来源。
    if recognition_type == "revenue":
        debit_code, credit_code = source_code, target_code
    else:
        debit_code, credit_code = target_code, source_code

    schedule: list[dict[str, Any]] = []
    for segment, cents in zip(segments, shares):
        amount = _money(Decimal(cents) * _CENT)
        entry = {
            "voucher_id": f"{voucher_id_prefix}-{segment['end']:%Y%m}",
            "posting_date": segment["end"].isoformat(),
            "currency": currency,
            "lines": [
                {
                    "line_id": "rec-1",
                    "account_code": debit_code,
                    "debit": amount,
                    "credit": _money(Decimal(0)),
                },
                {
                    "line_id": "rec-2",
                    "account_code": credit_code,
                    "debit": _money(Decimal(0)),
                    "credit": amount,
                },
            ],
        }
        schedule.append(
            {
                "period_start": segment["start"].isoformat(),
                "period_end": segment["end"].isoformat(),
                "days": segment["days"],
                "amount": amount,
                "entry": entry,
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
