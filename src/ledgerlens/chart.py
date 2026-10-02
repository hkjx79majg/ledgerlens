"""无状态会计科目体系校验。

纯函数实现：不落盘、不保留跨请求状态，相同输入必然得到相同输出。
错误结构与排序沿用凭证校验：每项含 JSON Pointer 形式的
``path``、``code``、``message``，最终按 ``(path, code)`` 排序。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_CHART_FIELDS = ("chart_id", "effective_date", "accounts")
_ACCOUNT_FIELDS = ("code", "name", "type", "normal_balance", "active", "parent_code")

_ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
_NORMAL_BALANCES = ("debit", "credit")
# 资产、费用为借方余额；负债、权益、收入为贷方余额。
_TYPE_TO_BALANCE = {
    "asset": "debit",
    "expense": "debit",
    "liability": "credit",
    "equity": "credit",
    "revenue": "credit",
}


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _add(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _check_string(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> str | None:
    """校验非空字符串字段；合法时返回字符串值，否则记录错误并返回 None。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a string")
        return None
    if value == "":
        _add(errors, path, "blank_value", f"{key} must not be blank")
        return None
    return value


def validate_chart_of_accounts(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """校验一套科目体系，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    for key in payload:
        if key not in _CHART_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    chart_id = _check_string(errors, payload, "chart_id", "/chart_id")

    effective_date = _check_string(errors, payload, "effective_date", "/effective_date")
    if effective_date is not None and (
        _DATE_RE.fullmatch(effective_date) is None
        or _invalid_calendar_date(effective_date)
    ):
        _add(
            errors,
            "/effective_date",
            "invalid_date",
            "effective_date must be a valid calendar date in YYYY-MM-DD format",
        )

    accounts: list[Any] | None = None
    records: list[dict[str, Any] | None] = []

    if "accounts" not in payload:
        _add(errors, "/accounts", "required", "accounts is required")
    elif not isinstance(payload["accounts"], list):
        _add(errors, "/accounts", "invalid_type", "accounts must be an array")
    else:
        accounts = payload["accounts"]
        if len(accounts) < 1:
            _add(errors, "/accounts", "too_few_accounts", "accounts must contain at least one entry")
        for index, account in enumerate(accounts):
            base = f"/accounts/{index}"
            if not isinstance(account, dict):
                _add(errors, base, "invalid_type", "account must be an object")
                records.append(None)
                continue
            records.append(_validate_account_fields(errors, account, index, base))

    # 仅字段全部合法的科目参与关系推导，保证字段无效不派生关系错误。
    valid_records = [r for r in records if r is not None]

    if accounts is not None and valid_records:
        _validate_account_relations(errors, valid_records)

    if errors:
        # 环检测可从多个入口重复触达同一环节点，按 (path, code) 去重后排序。
        deduped = {(item["path"], item["code"]): item for item in errors}
        ordered = sorted(deduped.values(), key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": ordered}

    assert accounts is not None and chart_id is not None and effective_date is not None
    type_counts = {acc_type: 0 for acc_type in _ACCOUNT_TYPES}
    root_count = 0
    for record in valid_records:
        type_counts[record["type"]] += 1
        if record["parent_code"] is None:
            root_count += 1

    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "effective_date": effective_date,
        "account_count": len(valid_records),
        "root_count": root_count,
        "type_counts": type_counts,
    }


def _invalid_calendar_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return True
    return False


def _validate_account_fields(
    errors: list[dict[str, str]], account: dict[str, Any], index: int, base: str
) -> dict[str, Any] | None:
    """校验单个科目的自有字段；全部合法时返回记录，否则返回 None。"""
    record: dict[str, Any] = {
        "index": index,
        "base": base,
        "code": None,
        "type": None,
        "normal_balance": None,
        "active": None,
        "parent_code": None,
        "parent": None,
        "parent_valid": False,
    }
    field_invalid = False

    for key in account:
        if key not in _ACCOUNT_FIELDS:
            _add(errors, f"{base}/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    if _check_string(errors, account, "code", f"{base}/code") is None:
        field_invalid = True
    else:
        record["code"] = account["code"]

    if _check_string(errors, account, "name", f"{base}/name") is None:
        field_invalid = True

    if "type" not in account:
        _add(errors, f"{base}/type", "required", "type is required")
        field_invalid = True
    elif not isinstance(account["type"], str):
        _add(errors, f"{base}/type", "invalid_type", "type must be a string")
        field_invalid = True
    elif account["type"] not in _ACCOUNT_TYPES:
        _add(
            errors,
            f"{base}/type",
            "invalid_account_type",
            "type must be one of asset, liability, equity, revenue, expense",
        )
        field_invalid = True
    else:
        record["type"] = account["type"]

    if "normal_balance" not in account:
        _add(errors, f"{base}/normal_balance", "required", "normal_balance is required")
        field_invalid = True
    elif not isinstance(account["normal_balance"], str):
        _add(
            errors,
            f"{base}/normal_balance",
            "invalid_type",
            "normal_balance must be a string",
        )
        field_invalid = True
    elif account["normal_balance"] not in _NORMAL_BALANCES:
        _add(
            errors,
            f"{base}/normal_balance",
            "invalid_normal_balance",
            "normal_balance must be one of debit, credit",
        )
        field_invalid = True
    else:
        record["normal_balance"] = account["normal_balance"]

    if "active" not in account:
        _add(errors, f"{base}/active", "required", "active is required")
        field_invalid = True
    elif not isinstance(account["active"], bool):
        _add(errors, f"{base}/active", "invalid_type", "active must be a boolean")
        field_invalid = True
    else:
        record["active"] = account["active"]

    if "parent_code" not in account:
        _add(errors, f"{base}/parent_code", "required", "parent_code is required")
        field_invalid = True
    else:
        raw_parent = account["parent_code"]
        if raw_parent is None:
            record["parent_code"] = None
        elif not isinstance(raw_parent, str):
            _add(
                errors,
                f"{base}/parent_code",
                "invalid_type",
                "parent_code must be a string or null",
            )
            field_invalid = True
        elif raw_parent == "":
            _add(errors, f"{base}/parent_code", "blank_value", "parent_code must not be blank")
            field_invalid = True
        else:
            record["parent_code"] = raw_parent

    return None if field_invalid else record


def _validate_account_relations(
    errors: list[dict[str, str]], records: list[dict[str, Any]]
) -> None:
    """在字段合法的科目间推导唯一性、父子与余额方向关系错误。"""
    by_code: dict[str, dict[str, Any]] = {}
    for record in records:
        code = record["code"]
        if code in by_code:
            _add(
                errors,
                f"{record['base']}/code",
                "duplicate_account_code",
                f"code {code!r} is duplicated within the chart",
            )
        else:
            by_code[code] = record

    # 父级解析：自引优先报 self_parent，其次 parent_not_found；解析成功才参与后续推导。
    for record in records:
        parent_code = record["parent_code"]
        if parent_code is None:
            continue
        parent_path = f"{record['base']}/parent_code"
        if parent_code == record["code"]:
            _add(
                errors,
                parent_path,
                "self_parent",
                "account must not reference itself as parent",
            )
            continue
        parent = by_code.get(parent_code)
        if parent is None:
            _add(
                errors,
                parent_path,
                "parent_not_found",
                f"parent_code {parent_code!r} does not reference another account",
            )
            continue
        record["parent"] = parent
        record["parent_valid"] = True

    _report_cycles(errors, records)

    for record in records:
        expected_balance = _TYPE_TO_BALANCE[record["type"]]
        if record["normal_balance"] != expected_balance:
            _add(
                errors,
                f"{record['base']}/normal_balance",
                "normal_balance_mismatch",
                f"{record['type']} accounts must have normal_balance {expected_balance}",
            )
        if not record["parent_valid"]:
            continue
        parent = record["parent"]
        parent_path = f"{record['base']}/parent_code"
        if record["type"] != parent["type"]:
            _add(
                errors,
                parent_path,
                "parent_type_mismatch",
                "child account must have the same type as its parent",
            )
        if record["active"] and not parent["active"]:
            _add(
                errors,
                parent_path,
                "inactive_parent",
                "active account must not be attached to an inactive parent",
            )


def _report_cycles(
    errors: list[dict[str, str]], records: list[dict[str, Any]]
) -> None:
    """沿 parent 链检测成环，环内每项在其 parent_code 路径报错。"""
    reported: set[tuple[str, str]] = set()
    for start in records:
        if not start["parent_valid"]:
            continue
        order: list[dict[str, Any]] = []
        positions: dict[int, int] = {}
        node: dict[str, Any] | None = start
        while node is not None and node["parent_valid"] and node["parent"] is not None:
            idx = node["index"]
            if idx in positions:
                for cycle_node in order[positions[idx]:]:
                    key = (f"{cycle_node['base']}/parent_code", "parent_cycle")
                    if key not in reported:
                        reported.add(key)
                        _add(
                            errors,
                            key[0],
                            "parent_cycle",
                            "parent chain must not contain a cycle",
                        )
                break
            positions[idx] = len(order)
            order.append(node)
            node = node["parent"]
