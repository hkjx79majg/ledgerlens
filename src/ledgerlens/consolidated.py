"""无状态多主体合并财务报表（含内部交易抵消）生成。

请求沿用财务报表端点的期间、币种与科目体系，新增 `entities` 与
`elimination_entries`：每个主体含非空 `entity_id`、`opening_balances` 与
`entries`，全部主体共享期间、本位币和科目体系；抵消凭证只影响合并结果。
主体的期初余额与凭证校验完全复用 trial_balance.py 的校验助手，错误路径加
`/entities/{i}` 前缀；抵消凭证逐张复用凭证校验并推导币种一致、过账日期
落在期间内、科目存在且启用，错误路径加 `/elimination_entries/{i}` 前缀。
依赖字段无效时不派生关联错误。纯函数实现：不落盘、不保留跨请求状态，
相同输入必然得到相同输出。

成功时汇总各主体期初与本期发生额，再把抵消分录计入合并本期发生额；损益表
与资产负债表的结构、金额方向、净利润计入权益、科目顺序、零值与 balanced
口径与单账套财务报表完全一致（复用 financial_statements._build_statements）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .chart import validate_chart_of_accounts
from .financial_statements import _build_statements
from .trial_balance import (
    _CURRENCY_RE,
    _ZERO,
    _add,
    _check_account_references,
    _check_date,
    _check_nonempty_string,
    _escape,
    _money,
    _parse_date,
    _validate_entry_list,
    _validate_opening_balances,
)

_REQUEST_FIELDS = (
    "period_start",
    "period_end",
    "currency",
    "chart",
    "entities",
    "elimination_entries",
)
_ENTITY_FIELDS = ("entity_id", "opening_balances", "entries")


def generate_consolidated_financial_statements(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """生成多主体合并财务报表，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- 期间：真实日期且先后有序 ----
    period_start = _check_date(errors, payload, "period_start", "/period_start")
    period_end = _check_date(errors, payload, "period_end", "/period_end")
    period_ok = False
    if period_start is not None and period_end is not None:
        if period_start > period_end:
            _add(
                errors,
                "/period_start",
                "invalid_period",
                "period_start must be on or before period_end",
            )
        else:
            period_ok = True

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
        # chart_not_effective 仅依赖 effective_date 字段本身与 period_start。
        effective_raw = chart.get("effective_date")
        effective = _parse_date(effective_raw) if isinstance(effective_raw, str) else None
        if effective is not None and period_start is not None and effective > period_start:
            _add(
                errors,
                "/chart/effective_date",
                "chart_not_effective",
                "chart effective_date must not be after period_start",
            )

    # ---- entities：非空数组，逐项校验并沿用试算平衡表的期初与凭证校验 ----
    entity_nodes: list[dict[str, Any]] = []
    if "entities" not in payload:
        _add(errors, "/entities", "required", "entities is required")
    elif not isinstance(payload["entities"], list):
        _add(errors, "/entities", "invalid_type", "entities must be an array")
    else:
        entities = payload["entities"]
        if len(entities) < 1:
            _add(
                errors,
                "/entities",
                "too_few_entities",
                "entities must contain at least one entity",
            )
        seen_ids: set[str] = set()
        for index, entity in enumerate(entities):
            base = f"/entities/{index}"
            if not isinstance(entity, dict):
                _add(errors, base, "invalid_type", "entity must be an object")
                continue
            for key in entity:
                if key not in _ENTITY_FIELDS:
                    _add(
                        errors,
                        f"{base}/{_escape(key)}",
                        "unknown_field",
                        f"unknown field {key!r}",
                    )
            entity_id = _check_nonempty_string(
                errors, entity, "entity_id", f"{base}/entity_id"
            )
            if entity_id is not None:
                if entity_id in seen_ids:
                    _add(
                        errors,
                        f"{base}/entity_id",
                        "duplicate_entity_id",
                        f"entity_id {entity_id!r} is duplicated within entities",
                    )
                else:
                    seen_ids.add(entity_id)
            opening_items = _validate_opening_balances(
                entity, f"{base}/opening_balances", errors
            )
            entry_nodes = _validate_entry_list(
                entity,
                "entries",
                f"{base}/entries",
                currency,
                period_start,
                period_end,
                period_ok,
                errors,
            )
            entity_nodes.append(
                {"opening_items": opening_items, "entry_nodes": entry_nodes}
            )

    # ---- elimination_entries：须为数组、可为空，逐张复用凭证校验 ----
    elimination_nodes = _validate_entry_list(
        payload,
        "elimination_entries",
        "/elimination_entries",
        currency,
        period_start,
        period_end,
        period_ok,
        errors,
    )

    # ---- 科目引用：仅当 chart 整体有效时推导存在性与启用状态 ----
    if chart_accounts is not None:
        for node in entity_nodes:
            _check_account_references(
                chart_accounts, node["opening_items"], node["entry_nodes"], errors
            )
        _check_account_references(chart_accounts, [], elimination_nodes, errors)

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：汇总各主体期初与本期发生额，抵消分录计入合并本期发生额 ----
    opening_totals: dict[str, list[Decimal]] = {}
    period_totals: dict[str, list[Decimal]] = {}
    for node in entity_nodes:
        for item in node["opening_items"]:
            bucket = opening_totals.setdefault(item["code"], [_ZERO, _ZERO])
            bucket[0] += item["debit"]
            bucket[1] += item["credit"]
        for entry_node in node["entry_nodes"]:
            for line in entry_node["entry"]["lines"]:
                bucket = period_totals.setdefault(line["account_code"], [_ZERO, _ZERO])
                bucket[0] += Decimal(line["debit"])
                bucket[1] += Decimal(line["credit"])

    elimination_totals: dict[str, list[Decimal]] = {}
    for node in elimination_nodes:
        for line in node["entry"]["lines"]:
            debit = Decimal(line["debit"])
            credit = Decimal(line["credit"])
            bucket = elimination_totals.setdefault(line["account_code"], [_ZERO, _ZERO])
            bucket[0] += debit
            bucket[1] += credit
            period_bucket = period_totals.setdefault(line["account_code"], [_ZERO, _ZERO])
            period_bucket[0] += debit
            period_bucket[1] += credit

    opening_by_code = {
        code: {"debit": amounts[0], "credit": amounts[1]}
        for code, amounts in opening_totals.items()
    }
    statements = _build_statements(chart_accounts, opening_by_code, period_totals)

    elimination_summary = [
        {
            "account_code": code,
            "debit": _money(amounts[0]),
            "credit": _money(amounts[1]),
        }
        for code, amounts in sorted(elimination_totals.items())
    ]

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "currency": currency,
        "entity_count": len(payload["entities"]),
        **statements,
        "elimination_summary": elimination_summary,
    }
