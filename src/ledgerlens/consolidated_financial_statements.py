"""无状态多主体合并报表（损益表与资产负债表）生成。

请求沿用财务报表的期间、币种与 chart 契约，新增 entities 与
elimination_entries 两个顶层字段：

- entities 为非空数组，每个主体含非空 entity_id 以及与试算平衡表同构的
  opening_balances、entries；各主体共享期间、本位币与科目体系。共享字段
  （period_start/period_end/currency/chart）只在顶层校验一次，主体的期初与
  凭证校验完全复用 trial_balance._prepare，仅取 /opening_balances、/entries
  路径下的错误并加 /entities/{i} 前缀。
- elimination_entries 为可为空的凭证数组，逐项沿用凭证校验，并做币种一致、
  日期在期间内、科目存在且启用的跨对象校验，路径加
  /elimination_entries/{i} 前缀；抵消凭证只影响合并本期发生额。

成功时汇总各主体期初与本期发生额，再把抵消分录入账到合并本期发生额，报表
口径、结构、排序与零值行为与 financial_statements 完全一致。纯函数实现：
不落盘、不保留跨请求状态，相同输入必然得到相同输出。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .financial_statements import _build_financial_statements
from .journal import validate_journal_entry
from .trial_balance import (
    _CURRENCY_RE,
    _ZERO,
    _add,
    _check_nonempty_string,
    _escape,
    _money,
    _parse_date,
    _prepare,
)

# 合并请求的顶层字段：共享期间、本位币与科目体系，外加主体与抵消凭证；
# 单主体报表的 opening_balances/entries 下沉到每个主体内。
_CONSOLIDATED_REQUEST_FIELDS = (
    "period_start",
    "period_end",
    "currency",
    "chart",
    "entities",
    "elimination_entries",
)
_ENTITY_FIELDS = ("entity_id", "opening_balances", "entries")
# 主体内复用试算平衡表校验时，仅这些路径属于主体自身，其余为共享字段错误
# （已在顶层上报一次）。
_ENTITY_SCOPED_PATHS = ("/opening_balances", "/entries")


def _prefix_errors(
    errors: list[dict[str, str]], prefix: str, source: list[dict[str, str]]
) -> None:
    """把校验错误整体加上路径前缀。"""
    for err in source:
        _add(errors, prefix + err["path"], err["code"], err["message"])


def _check_entity(
    errors: list[dict[str, str]],
    index: int,
    entity: Any,
    payload: dict[str, Any],
    seen_ids: set[str],
) -> dict[str, Any] | None:
    """校验单个主体；全部有效时返回其 _prepare 汇总上下文，否则返回 None。"""
    base = f"/entities/{index}"
    if not isinstance(entity, dict):
        _add(errors, base, "invalid_type", "entity must be an object")
        return None

    # ---- 主体层未知字段 ----
    for key in entity:
        if key not in _ENTITY_FIELDS:
            _add(
                errors,
                f"{base}/{_escape(key)}",
                "unknown_field",
                f"unknown field {key!r}",
            )

    # ---- entity_id：非空字符串，重复时后出现者报错 ----
    entity_id = _check_nonempty_string(errors, entity, "entity_id", f"{base}/entity_id")
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

    # ---- 期初与凭证：沿用试算平衡表契约，共享期间、币种与科目体系 ----
    sub_payload = {
        "period_start": payload.get("period_start"),
        "period_end": payload.get("period_end"),
        "currency": payload.get("currency"),
        "chart": payload.get("chart"),
        "opening_balances": entity.get("opening_balances"),
        "entries": entity.get("entries"),
    }
    entity_errors, context = _prepare(sub_payload)
    scoped_errors = [
        err
        for err in entity_errors
        if err["path"].startswith(_ENTITY_SCOPED_PATHS)
    ]
    _prefix_errors(errors, base, scoped_errors)
    return context


def _check_elimination_entries(
    errors: list[dict[str, str]],
    payload: dict[str, Any],
    period_start: date | None,
    period_end: date | None,
    period_ok: bool,
    currency: str | None,
    chart_accounts: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """校验抵消凭证数组，返回通过凭证校验的凭证节点（供成功路径入账）。"""
    if "elimination_entries" not in payload:
        _add(errors, "/elimination_entries", "required", "elimination_entries is required")
        return []
    entries = payload["elimination_entries"]
    if not isinstance(entries, list):
        _add(
            errors,
            "/elimination_entries",
            "invalid_type",
            "elimination_entries must be an array",
        )
        return []

    nodes: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        base = f"/elimination_entries/{index}"
        if not isinstance(entry, dict):
            _add(errors, base, "invalid_type", "entry must be an object")
            continue
        entry_status, entry_body = validate_journal_entry(entry)
        entry_valid = entry_status == 200
        if not entry_valid:
            if "errors" in entry_body:
                _prefix_errors(errors, base, entry_body["errors"])
            else:
                _add(
                    errors,
                    base,
                    entry_body["code"],
                    "journal entry is not balanced",
                )

        # 跨对象错误仅依赖对应字段本身有效。
        entry_currency = entry.get("currency")
        if (
            currency is not None
            and isinstance(entry_currency, str)
            and _CURRENCY_RE.fullmatch(entry_currency) is not None
            and entry_currency != currency
        ):
            _add(
                errors,
                f"{base}/currency",
                "currency_mismatch",
                "entry currency must match the request currency",
            )
        posting_raw = entry.get("posting_date")
        posting = _parse_date(posting_raw) if isinstance(posting_raw, str) else None
        if period_ok and posting is not None and not (period_start <= posting <= period_end):
            _add(
                errors,
                f"{base}/posting_date",
                "posting_date_out_of_period",
                "posting_date must be within [period_start, period_end]",
            )

        # 科目引用：仅当 chart 整体有效时推导存在性与启用状态。
        if chart_accounts is not None:
            lines = entry.get("lines")
            if isinstance(lines, list):
                for line_index, line in enumerate(lines):
                    if not isinstance(line, dict):
                        continue
                    code = line.get("account_code")
                    if not isinstance(code, str) or code == "":
                        continue
                    account = chart_accounts.get(code)
                    path = f"{base}/lines/{line_index}/account_code"
                    if account is None:
                        _add(
                            errors,
                            path,
                            "unknown_account",
                            f"account_code {code!r} does not exist in the chart",
                        )
                    elif account["active"] is not True:
                        _add(
                            errors,
                            path,
                            "inactive_account",
                            f"account {code!r} is inactive",
                        )

        # 仅凭证本身校验通过的凭证参与成功路径入账。
        if entry_valid:
            nodes.append({"base": base, "entry": entry})

    return nodes


def _merge_context(merged: dict[str, Any], context: dict[str, Any]) -> None:
    """把单个主体的汇总上下文并入合并上下文。"""
    for code, item in context["opening_by_code"].items():
        existing = merged["opening_by_code"].get(code)
        if existing is None:
            merged["opening_by_code"][code] = {
                "code": code,
                "debit": item["debit"],
                "credit": item["credit"],
            }
        else:
            existing["debit"] += item["debit"]
            existing["credit"] += item["credit"]
    for code, (debit, credit) in context["period_by_code"].items():
        bucket = merged["period_by_code"].setdefault(code, [_ZERO, _ZERO])
        bucket[0] += debit
        bucket[1] += credit


def generate_consolidated_financial_statements(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """生成多主体合并损益表与资产负债表，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段（含拒绝单主体报表的 opening_balances/entries）----
    for key in payload:
        if key not in _CONSOLIDATED_REQUEST_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- 共享期间、币种、chart：只在顶层校验一次（空期初/空凭证探针）----
    probe = {
        "period_start": payload.get("period_start"),
        "period_end": payload.get("period_end"),
        "currency": payload.get("currency"),
        "chart": payload.get("chart"),
        "opening_balances": [],
        "entries": [],
    }
    shared_errors, shared_context = _prepare(probe)
    errors.extend(shared_errors)
    if shared_context is not None:
        chart_id = shared_context["chart_id"]
        currency = shared_context["currency"]
        chart_accounts = shared_context["chart_accounts"]
        period_start = _parse_date(payload["period_start"])
        period_end = _parse_date(payload["period_end"])
        period_ok = True
    else:
        chart_id = None
        currency = None
        chart_accounts = None
        period_start = None
        period_end = None
        period_ok = False

    # ---- entities：非空数组、id 唯一、逐主体复用试算平衡表校验 ----
    entity_contexts: list[dict[str, Any]] = []
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
            context = _check_entity(errors, index, entity, payload, seen_ids)
            if context is not None:
                entity_contexts.append(context)

    # ---- elimination_entries：数组（可空），逐项凭证校验 + 跨对象校验 ----
    elimination_nodes = _check_elimination_entries(
        errors,
        payload,
        period_start,
        period_end,
        period_ok,
        currency,
        chart_accounts,
    )

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 汇总各主体期初与本期发生额 ----
    merged: dict[str, Any] = {
        "chart_id": chart_id,
        "currency": currency,
        "chart_accounts": chart_accounts,
        "opening_by_code": {},
        "period_by_code": {},
    }
    for context in entity_contexts:
        _merge_context(merged, context)

    # ---- 抵消凭证计入合并本期发生额，并按科目汇总抵消影响 ----
    elimination_by_code: dict[str, list[Decimal]] = {}
    for node in elimination_nodes:
        for line in node["entry"]["lines"]:
            code = line["account_code"]
            bucket = merged["period_by_code"].setdefault(code, [_ZERO, _ZERO])
            bucket[0] += Decimal(line["debit"])
            bucket[1] += Decimal(line["credit"])
            elim_bucket = elimination_by_code.setdefault(code, [_ZERO, _ZERO])
            elim_bucket[0] += Decimal(line["debit"])
            elim_bucket[1] += Decimal(line["credit"])

    body = _build_financial_statements(
        merged, payload["period_start"], payload["period_end"]
    )
    body["entity_count"] = len(entity_contexts)
    body["elimination_summary"] = [
        {
            "account_code": code,
            "debit": _money(elimination_by_code[code][0]),
            "credit": _money(elimination_by_code[code][1]),
        }
        for code in sorted(elimination_by_code)
    ]
    return 200, body
