"""无状态科目体系（Chart of Accounts）校验。

纯函数实现：不落盘、不保留跨请求状态，相同输入必然得到相同输出。
错误结构、状态码与排序规则与凭证校验（journal.py）保持一致。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_CHART_FIELDS = ("chart_id", "effective_date", "accounts")
_ACCOUNT_FIELDS = ("code", "name", "type", "normal_balance", "active", "parent_code")

_ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
_BALANCES = ("debit", "credit")
# 每个科目类别允许的正常余额方向：asset/expense 为 debit，其余为 credit。
_EXPECTED_BALANCE = {
    "asset": "debit",
    "expense": "debit",
    "liability": "credit",
    "equity": "credit",
    "revenue": "credit",
}

# parent_code 的根标记：None 表示字段无效，_ROOT 表示显式 null。
_ROOT = object()


def _escape(token: str) -> str:
    """RFC 6901 JSON Pointer 转义。"""
    return token.replace("~", "~0").replace("/", "~1")


def _add(errors: list[dict[str, str]], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _check_nonempty_string(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> str | None:
    """校验必填非空字符串字段；合法返回字符串值，否则记录错误并返回 None。"""
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


def _check_boolean(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> bool | None:
    """校验必填布尔字段（bool 须先于 int 判断）；合法返回布尔值，否则返回 None。"""
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if not isinstance(value, bool):
        _add(errors, path, "invalid_type", f"{key} must be a boolean")
        return None
    return value


def _check_parent_code(
    errors: list[dict[str, str]], obj: dict[str, Any], key: str, path: str
) -> Any:
    """校验 parent_code：null（返回 _ROOT）表示根，否则须为非空字符串（返回该串）。

    缺失、类型错误或空白时记录错误并返回 None。
    """
    if key not in obj:
        _add(errors, path, "required", f"{key} is required")
        return None
    value = obj[key]
    if value is None:
        return _ROOT
    if not isinstance(value, str):
        _add(errors, path, "invalid_type", f"{key} must be a string or null")
        return None
    if value == "":
        _add(errors, path, "blank_value", f"{key} must not be blank")
        return None
    return value


def validate_chart_of_accounts(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """校验一个科目体系请求，返回 (HTTP 状态码, 响应体)。"""
    errors: list[dict[str, str]] = []

    # ---- 顶层未知字段 ----
    for key in payload:
        if key not in _CHART_FIELDS:
            _add(errors, f"/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

    # ---- chart_id / effective_date ----
    chart_id = _check_nonempty_string(errors, payload, "chart_id", "/chart_id")

    effective_date = _check_nonempty_string(
        errors, payload, "effective_date", "/effective_date"
    )
    if effective_date is not None:
        valid_calendar = False
        if _DATE_RE.fullmatch(effective_date) is not None:
            try:
                date.fromisoformat(effective_date)
                valid_calendar = True
            except ValueError:
                pass
        if not valid_calendar:
            _add(
                errors,
                "/effective_date",
                "invalid_date",
                "effective_date must be a valid calendar date in YYYY-MM-DD format",
            )

    # ---- accounts：必填、数组、至少一项 ----
    accounts: list[Any] = []
    if "accounts" not in payload:
        _add(errors, "/accounts", "required", "accounts is required")
    elif not isinstance(payload["accounts"], list):
        _add(errors, "/accounts", "invalid_type", "accounts must be an array")
    else:
        accounts = payload["accounts"]
        if len(accounts) < 1:
            _add(errors, "/accounts", "too_few_accounts", "accounts must contain at least one account")

    # ---- 第一阶段：逐科目字段校验 ----
    # 仅“字段全部有效”的科目（clean）参与第二阶段的关系推导，
    # 落实“字段无效不派生相关关系错误”。
    nodes: list[dict[str, Any]] = []
    for index, account in enumerate(accounts):
        base = f"/accounts/{index}"
        if not isinstance(account, dict):
            _add(errors, base, "invalid_type", "account must be an object")
            continue

        error_marker = len(errors)

        for key in account:
            if key not in _ACCOUNT_FIELDS:
                _add(errors, f"{base}/{_escape(key)}", "unknown_field", f"unknown field {key!r}")

        code = _check_nonempty_string(errors, account, "code", f"{base}/code")
        _check_nonempty_string(errors, account, "name", f"{base}/name")

        account_type: str | None = None
        if "type" not in account:
            _add(errors, f"{base}/type", "required", "type is required")
        elif not isinstance(account["type"], str):
            _add(errors, f"{base}/type", "invalid_type", "type must be a string")
        elif account["type"] not in _ACCOUNT_TYPES:
            _add(
                errors,
                f"{base}/type",
                "invalid_account_type",
                f"type must be one of {', '.join(_ACCOUNT_TYPES)}",
            )
        else:
            account_type = account["type"]

        balance: str | None = None
        if "normal_balance" not in account:
            _add(errors, f"{base}/normal_balance", "required", "normal_balance is required")
        elif not isinstance(account["normal_balance"], str):
            _add(
                errors,
                f"{base}/normal_balance",
                "invalid_type",
                "normal_balance must be a string",
            )
        elif account["normal_balance"] not in _BALANCES:
            _add(
                errors,
                f"{base}/normal_balance",
                "invalid_normal_balance",
                f"normal_balance must be one of {', '.join(_BALANCES)}",
            )
        else:
            balance = account["normal_balance"]

        active = _check_boolean(errors, account, "active", f"{base}/active")
        parent_raw = _check_parent_code(errors, account, "parent_code", f"{base}/parent_code")

        clean = len(errors) == error_marker
        if parent_raw is _ROOT:
            is_root = True
            parent_code: str | None = None
        elif isinstance(parent_raw, str):
            is_root = False
            parent_code = parent_raw
        else:
            # 字段无效：该科目不参与关系推导。
            is_root = False
            parent_code = None

        nodes.append(
            {
                "index": index,
                "base": base,
                "clean": clean,
                "code": code,
                "type": account_type,
                "balance": balance,
                "active": active,
                "is_root": is_root,
                "parent_code": parent_code,
                "parent_node": None,
            }
        )

    # ---- 第二阶段：关系校验（仅 clean 科目）----
    clean_nodes = [node for node in nodes if node["clean"]]

    # code -> 科目；重复 code 时首个占据映射，后续各项报 duplicate_account_code。
    by_code: dict[str, dict[str, Any]] = {}
    for node in clean_nodes:
        code = node["code"]
        if code in by_code:
            _add(
                errors,
                f"{node['base']}/code",
                "duplicate_account_code",
                f"account code {code!r} is duplicated within the chart",
            )
        else:
            by_code[code] = node

    # normal_balance 与 type 匹配：asset/expense 仅 debit，其余仅 credit。
    for node in clean_nodes:
        expected = _EXPECTED_BALANCE[node["type"]]
        if node["balance"] != expected:
            _add(
                errors,
                f"{node['base']}/normal_balance",
                "normal_balance_mismatch",
                f"normal_balance for {node['type']} accounts must be {expected}",
            )

    # 先解析每个非根科目的直接父节点；父必须指向请求内另一个“字段有效”的科目。
    for node in clean_nodes:
        if node["is_root"]:
            continue
        pc = node["parent_code"]
        if pc == node["code"]:
            # 按值判定：parent_code 等于自身 code 即自引（重复 code 亦然）。
            node["relation"] = "self_parent"
        else:
            parent = by_code.get(pc)
            if parent is None:
                node["relation"] = "parent_not_found"
            else:
                node["parent_node"] = parent
                node["relation"] = None

    # 环检测：从直接父沿链向上，若回到自身则该科目位于环内。
    def in_cycle(node: dict[str, Any]) -> bool:
        cursor = node["parent_node"]
        visited: set[int] = set()
        while cursor is not None:
            if cursor is node:
                return True
            if id(cursor) in visited:
                # 链上存在不经过当前科目的环；当前科目不在环内。
                return False
            visited.add(id(cursor))
            if cursor["is_root"]:
                return False
            cursor = cursor["parent_node"]
        return False

    for node in clean_nodes:
        if node["is_root"]:
            continue
        relation = node["relation"]
        if relation == "self_parent":
            _add(
                errors,
                f"{node['base']}/parent_code",
                "self_parent",
                "parent_code must not reference the account itself",
            )
            continue
        if relation == "parent_not_found":
            _add(
                errors,
                f"{node['base']}/parent_code",
                "parent_not_found",
                f"parent_code {node['parent_code']!r} must reference another valid account in the request",
            )
            continue
        if in_cycle(node):
            _add(
                errors,
                f"{node['base']}/parent_code",
                "parent_cycle",
                "parent_code chain must not form a cycle",
            )
            continue
        parent = node["parent_node"]
        if node["type"] != parent["type"]:
            _add(
                errors,
                f"{node['base']}/parent_code",
                "parent_type_mismatch",
                "child account must have the same type as its parent",
            )
            continue
        if node["active"] is True and parent["active"] is False:
            _add(
                errors,
                f"{node['base']}/parent_code",
                "inactive_parent",
                "an active account must not be attached to an inactive parent",
            )
            continue

    if errors:
        errors.sort(key=lambda item: (item["path"], item["code"]))
        return 422, {"valid": False, "errors": errors}

    # ---- 成功：原样带回标识，附统计 ----
    root_count = sum(1 for node in clean_nodes if node["is_root"])
    type_counts = {account_type: 0 for account_type in _ACCOUNT_TYPES}
    for node in clean_nodes:
        type_counts[node["type"]] += 1
    return 200, {
        "valid": True,
        "chart_id": chart_id,
        "effective_date": effective_date,
        "account_count": len(clean_nodes),
        "root_count": root_count,
        "type_counts": type_counts,
    }
