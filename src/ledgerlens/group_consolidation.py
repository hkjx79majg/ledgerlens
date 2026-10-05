"""无状态集团试算平衡表合并与内部交易抵消。

面向同一记账本位币、同一会计期间的母子公司余额数据，公开入口为
``ledgerlens.consolidate_group_trial_balance``（顶层包导出）。请求为单一
字典，含四个顶层字段（拒绝未知字段）：

- ``entities``：实体数组（可为空）。每个实体含唯一非空 ``entity_id``、
  非空 ``period``、可选 ``ownership_ratio``（持股比例，默认 1，须落在
  [0, 1] 闭区间）与 ``balances`` 余额数组。余额项支持两种等价形式：
  ``{"account_code", "side", "amount"}``（side 取 debit/credit），或
  既有功能产出的 ``{"account_code", "debit", "credit"}``（仅一侧大于零，
  两侧均为零的项不参与汇总也不要求映射）；金额接受可无损转换为 Decimal
  的有限十进制值且不得为负。
- ``account_mapping``：按实体标识组织的集团科目映射，把各实体科目归并到
  统一集团科目。映射值可为集团科目编码字符串，或含
  ``{"group_account_code", "category"}`` 的对象；category 取
  asset/liability/equity/revenue/expense，仅用于少数股东权益的净资产口径。
- ``intercompany_transactions``：成对的内部往来声明（可为空）。每对含
  ``side_a``/``side_b``（各含 entity_id、account_code、amount）、
  ``category``（receivable_payable/revenue_cost/dividend）与可选
  ``reference`` 业务引用；只按声明的类别抵消，不凭科目名称猜测交易关系。
- ``tolerance``：非负容差，默认 0。

校验顺序：先做结构与字段校验（含持股比例区间与容差非负，失败抛
InvalidConsolidationInputError），再依次检查全部实体期间一致
（PeriodMismatchError）、实体标识不重复（DuplicateEntityError）、每份
余额借贷平衡（UnbalancedEntityError）、余额与声明涉及的科目映射完整
（AccountMappingError）。任一校验失败即抛出异常，不返回部分合并结果。

抵消规则：双方声明金额相等时全额抵消；差额绝对值不超过容差时以较小金额
抵消，并把差额写入 reconciliation_differences；超过容差时不生成该对抵消
分录、双方余额保留，并列入 unmatched_items。抵消分录按集团科目记账，
行方向取该方该集团科目余额的反方向（该方无余额时按对方方向推导，双方
均无法确定或方向相同无法对冲时约定 side_a 贷记、side_b 借记），分录可
按实体与业务引用追溯。

结果含抵消后的集团科目余额 balances 与借贷合计、抵消分录 eliminations、
对账差异 reconciliation_differences、未匹配项目 unmatched_items，以及
按子公司净资产余额（映射类别为 asset/liability 的余额项按借方减贷方
净额加总，取抵消前口径）乘以未持股比例计算并单列的 minority_interests。
各返回集合按实体标识、科目编码、业务引用稳定排序；全部金额运算使用
Decimal，不转为二进制浮点数；不修改输入、不落盘、不保留跨调用状态，
重复调用结果一致。空实体集合返回金额全为零且明细为空的有效结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .deferred_tax import _exact, _money, _parse_decimal
from .trial_balance import _ZERO

_ONE = Decimal("1")

_REQUEST_FIELDS = (
    "entities",
    "account_mapping",
    "intercompany_transactions",
    "tolerance",
)
_ENTITY_FIELDS = ("entity_id", "period", "ownership_ratio", "balances")
_SIDE_FORM_FIELDS = ("account_code", "side", "amount")
_PAIR_FORM_FIELDS = ("account_code", "debit", "credit")
_MAPPING_VALUE_FIELDS = ("group_account_code", "category")
_TRANSACTION_FIELDS = ("side_a", "side_b", "category", "reference")
_DECLARATION_SIDE_FIELDS = ("entity_id", "account_code", "amount")

_SIDES = ("debit", "credit")
_ELIMINATION_CATEGORIES = ("receivable_payable", "revenue_cost", "dividend")
_ACCOUNT_CATEGORIES = ("asset", "liability", "equity", "revenue", "expense")


class ConsolidationError(ValueError):
    """集团合并计算错误的基类：ValueError 子类，携带 message。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PeriodMismatchError(ConsolidationError):
    """实体期间不一致。"""

    def __init__(self, periods: list[str]) -> None:
        self.periods = sorted(set(periods))
        super().__init__(
            f"all entities must share the same period, got {self.periods!r}"
        )


class DuplicateEntityError(ConsolidationError):
    """实体标识重复。"""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"entity_id {entity_id!r} is duplicated within entities")


class UnbalancedEntityError(ConsolidationError):
    """实体余额借贷不平衡。"""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(
            f"entity {entity_id!r} debit total must equal credit total"
        )


class AccountMappingError(ConsolidationError):
    """科目缺少集团映射。"""

    def __init__(self, entity_id: str, account_code: str) -> None:
        self.entity_id = entity_id
        self.account_code = account_code
        super().__init__(
            f"account {account_code!r} of entity {entity_id!r} "
            "has no group account mapping"
        )


class InvalidConsolidationInputError(ConsolidationError):
    """合并输入无效：结构、字段、持股比例或容差不合法。"""


def _fail(message: str) -> None:
    raise InvalidConsolidationInputError(message)


def _check_unknown_keys(obj: dict[str, Any], allowed: tuple[str, ...], what: str) -> None:
    for key in obj:
        if key not in allowed:
            _fail(f"unknown field {key!r} in {what}")


def _require_nonempty_string(value: Any, what: str) -> str:
    if not isinstance(value, str) or value == "":
        _fail(f"{what} must be a non-empty string")
    return value


def _require_decimal(value: Any, what: str) -> Decimal:
    parsed = _parse_decimal(value)
    if parsed is None:
        _fail(
            f"{what} must be a finite decimal value losslessly convertible to Decimal"
        )
    return parsed


def _parse_balance_item(item: Any) -> tuple[str, Decimal, Decimal]:
    """解析余额项为 (account_code, debit, credit)，两种等价形式择一。"""
    if not isinstance(item, dict):
        _fail("balance item must be an object")
    if "side" in item or "amount" in item:
        _check_unknown_keys(item, _SIDE_FORM_FIELDS, "balance item")
        code = _require_nonempty_string(item.get("account_code"), "account_code")
        side = item.get("side")
        if side not in _SIDES:
            _fail("side must be 'debit' or 'credit'")
        amount = _require_decimal(item.get("amount"), "amount")
        if amount < 0:
            _fail("amount must not be negative")
        return (code, amount, _ZERO) if side == "debit" else (code, _ZERO, amount)
    _check_unknown_keys(item, _PAIR_FORM_FIELDS, "balance item")
    code = _require_nonempty_string(item.get("account_code"), "account_code")
    debit = _require_decimal(item.get("debit"), "debit")
    credit = _require_decimal(item.get("credit"), "credit")
    if debit < 0 or credit < 0:
        _fail("debit and credit must not be negative")
    if debit > 0 and credit > 0:
        _fail("exactly one of debit and credit must be greater than zero")
    return code, debit, credit


def _parse_entity(node: Any) -> dict[str, Any]:
    """解析单个实体；零额余额项丢弃（不参与汇总，也不要求映射）。"""
    if not isinstance(node, dict):
        _fail("entity must be an object")
    _check_unknown_keys(node, _ENTITY_FIELDS, "entity")
    entity_id = _require_nonempty_string(node.get("entity_id"), "entity_id")
    period = _require_nonempty_string(node.get("period"), "period")
    if "ownership_ratio" in node:
        ratio = _require_decimal(node["ownership_ratio"], "ownership_ratio")
        if ratio < 0 or ratio > _ONE:
            _fail("ownership_ratio must be between 0 and 1 inclusive")
    else:
        ratio = _ONE
    balances = node.get("balances")
    if not isinstance(balances, list):
        _fail("balances must be an array")
    items: list[dict[str, Any]] = []
    for raw in balances:
        code, debit, credit = _parse_balance_item(raw)
        if debit == 0 and credit == 0:
            continue
        items.append({"account_code": code, "debit": debit, "credit": credit})
    return {
        "entity_id": entity_id,
        "period": period,
        "ownership_ratio": ratio,
        "items": items,
    }


def _parse_mapping(node: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """解析集团科目映射为 {entity_id: {account_code: {group_account_code, category}}}。"""
    if node is None:
        return {}
    if not isinstance(node, dict):
        _fail("account_mapping must be an object")
    mapping: dict[str, dict[str, dict[str, Any]]] = {}
    for entity_id, per_entity in node.items():
        if not isinstance(entity_id, str) or entity_id == "":
            _fail("account_mapping keys must be non-empty entity id strings")
        if not isinstance(per_entity, dict):
            _fail(f"account_mapping for entity {entity_id!r} must be an object")
        entries: dict[str, dict[str, Any]] = {}
        for account_code, target in per_entity.items():
            if not isinstance(account_code, str) or account_code == "":
                _fail("account_mapping inner keys must be non-empty account code strings")
            if isinstance(target, str):
                if target == "":
                    _fail("group account code must be a non-empty string")
                entries[account_code] = {"group_account_code": target, "category": None}
            elif isinstance(target, dict):
                _check_unknown_keys(target, _MAPPING_VALUE_FIELDS, "mapping value")
                group_code = _require_nonempty_string(
                    target.get("group_account_code"), "group_account_code"
                )
                category = target.get("category")
                if category is not None and category not in _ACCOUNT_CATEGORIES:
                    _fail(
                        f"category must be one of: {', '.join(_ACCOUNT_CATEGORIES)}"
                    )
                entries[account_code] = {
                    "group_account_code": group_code,
                    "category": category,
                }
            else:
                _fail("mapping value must be a group account code string or an object")
        mapping[entity_id] = entries
    return mapping


def _parse_declaration_side(node: Any, what: str) -> dict[str, Any]:
    if not isinstance(node, dict):
        _fail(f"{what} must be an object")
    _check_unknown_keys(node, _DECLARATION_SIDE_FIELDS, what)
    entity_id = _require_nonempty_string(node.get("entity_id"), "entity_id")
    account_code = _require_nonempty_string(node.get("account_code"), "account_code")
    amount = _require_decimal(node.get("amount"), "amount")
    if amount < 0:
        _fail("amount must not be negative")
    return {"entity_id": entity_id, "account_code": account_code, "amount": amount}


def _parse_transaction(node: Any, index: int) -> dict[str, Any]:
    if not isinstance(node, dict):
        _fail("intercompany transaction must be an object")
    _check_unknown_keys(node, _TRANSACTION_FIELDS, "intercompany transaction")
    side_a = _parse_declaration_side(node.get("side_a"), "side_a")
    side_b = _parse_declaration_side(node.get("side_b"), "side_b")
    category = node.get("category")
    if category not in _ELIMINATION_CATEGORIES:
        _fail(f"category must be one of: {', '.join(_ELIMINATION_CATEGORIES)}")
    reference = node.get("reference")
    if reference is not None:
        reference = _require_nonempty_string(reference, "reference")
    return {
        "index": index,
        "side_a": side_a,
        "side_b": side_b,
        "category": category,
        "reference": reference,
    }


def consolidate_group_trial_balance(payload: dict[str, Any]) -> dict[str, Any]:
    """合并集团试算平衡表并抵消内部交易。

    校验失败抛出 ConsolidationError 相应子类，不返回部分合并结果；成功
    返回结果字典。不修改输入对象，不保留跨调用状态。
    """
    if not isinstance(payload, dict):
        raise InvalidConsolidationInputError("request must be an object")
    _check_unknown_keys(payload, _REQUEST_FIELDS, "request")

    # ---- 容差：非负有限十进制值，默认 0 ----
    if "tolerance" in payload:
        tolerance = _require_decimal(payload["tolerance"], "tolerance")
        if tolerance < 0:
            _fail("tolerance must not be negative")
    else:
        tolerance = _ZERO

    # ---- 实体：结构校验（含持股比例区间），可为空数组 ----
    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        _fail("entities must be an array")
    entities = [_parse_entity(node) for node in raw_entities]

    # ---- 集团科目映射与内部往来声明：结构校验 ----
    mapping = _parse_mapping(payload.get("account_mapping"))

    raw_transactions = payload.get("intercompany_transactions", [])
    if not isinstance(raw_transactions, list):
        _fail("intercompany_transactions must be an array")
    transactions = [
        _parse_transaction(node, index) for index, node in enumerate(raw_transactions)
    ]

    # 声明引用的实体必须存在于 entities。
    entity_ids = [entity["entity_id"] for entity in entities]
    known_ids = set(entity_ids)
    for txn in transactions:
        for side in (txn["side_a"], txn["side_b"]):
            if side["entity_id"] not in known_ids:
                _fail(
                    f"intercompany transaction references unknown entity "
                    f"{side['entity_id']!r}"
                )

    # ---- 全部实体期间一致 ----
    periods = [entity["period"] for entity in entities]
    if len(set(periods)) > 1:
        raise PeriodMismatchError(periods)

    # ---- 实体标识不重复（在后出现者报错） ----
    seen_ids: set[str] = set()
    for entity_id in entity_ids:
        if entity_id in seen_ids:
            raise DuplicateEntityError(entity_id)
        seen_ids.add(entity_id)

    # ---- 每份余额借贷平衡 ----
    for entity in entities:
        total_debit = sum((item["debit"] for item in entity["items"]), _ZERO)
        total_credit = sum((item["credit"] for item in entity["items"]), _ZERO)
        if total_debit != total_credit:
            raise UnbalancedEntityError(entity["entity_id"])

    # ---- 映射完整：余额与声明涉及的科目均须有集团映射 ----
    def group_of(entity_id: str, account_code: str) -> dict[str, Any]:
        per_entity = mapping.get(entity_id)
        if per_entity is None or account_code not in per_entity:
            raise AccountMappingError(entity_id, account_code)
        return per_entity[account_code]

    for entity in entities:
        for item in entity["items"]:
            group_of(entity["entity_id"], item["account_code"])
    for txn in transactions:
        for side in (txn["side_a"], txn["side_b"]):
            group_of(side["entity_id"], side["account_code"])

    # ---- 按集团科目汇总各实体余额与集团合计（抵消前） ----
    entity_accounts: dict[str, dict[str, list[Decimal]]] = {}
    group_totals: dict[str, list[Decimal]] = {}
    for entity in entities:
        per_group: dict[str, list[Decimal]] = {}
        for item in entity["items"]:
            target = group_of(entity["entity_id"], item["account_code"])
            code = target["group_account_code"]
            bucket = per_group.setdefault(code, [_ZERO, _ZERO])
            bucket[0] += item["debit"]
            bucket[1] += item["credit"]
            total = group_totals.setdefault(code, [_ZERO, _ZERO])
            total[0] += item["debit"]
            total[1] += item["credit"]
        entity_accounts[entity["entity_id"]] = per_group

    def balance_line_side(entity_id: str, group_code: str) -> str | None:
        """该方该集团科目余额的反方向作为抵消分录行方向；无余额时返回 None。"""
        amounts = entity_accounts.get(entity_id, {}).get(group_code)
        if amounts is None:
            return None
        net = amounts[0] - amounts[1]
        if net > 0:
            return "credit"
        if net < 0:
            return "debit"
        return None

    # ---- 逐对声明匹配：相等全额抵消，容差内以较小金额抵消，超差保留 ----
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for txn in transactions:
        side_a, side_b = txn["side_a"], txn["side_b"]
        difference = abs(side_a["amount"] - side_b["amount"])
        if difference > tolerance:
            unmatched.append({"txn": txn, "difference": difference})
            continue
        eliminated = min(side_a["amount"], side_b["amount"])
        group_a = group_of(side_a["entity_id"], side_a["account_code"])[
            "group_account_code"
        ]
        group_b = group_of(side_b["entity_id"], side_b["account_code"])[
            "group_account_code"
        ]
        line_a = balance_line_side(side_a["entity_id"], group_a)
        line_b = balance_line_side(side_b["entity_id"], group_b)
        if line_a is None and line_b is not None:
            line_a = "debit" if line_b == "credit" else "credit"
        if line_b is None and line_a is not None:
            line_b = "debit" if line_a == "credit" else "credit"
        if line_a is None or line_a == line_b:
            # 双方均无余额或方向相同无法对冲时，按约定方向记账。
            line_a, line_b = "credit", "debit"
        matched.append(
            {
                "txn": txn,
                "difference": difference,
                "eliminated": eliminated,
                "lines": [
                    {
                        "entity_id": side_a["entity_id"],
                        "account_code": side_a["account_code"],
                        "group_account_code": group_a,
                        "side": line_a,
                        "amount": eliminated,
                    },
                    {
                        "entity_id": side_b["entity_id"],
                        "account_code": side_b["account_code"],
                        "group_account_code": group_b,
                        "side": line_b,
                        "amount": eliminated,
                    },
                ],
            }
        )

    # ---- 抵消分录计入集团合计 ----
    for entry in matched:
        for line in entry["lines"]:
            bucket = group_totals.setdefault(line["group_account_code"], [_ZERO, _ZERO])
            if line["side"] == "debit":
                bucket[0] += line["amount"]
            else:
                bucket[1] += line["amount"]

    # ---- 输出集合按实体标识、科目编码、业务引用稳定排序 ----
    def sort_key_of(txn: dict[str, Any]) -> tuple[Any, ...]:
        side_a, side_b = txn["side_a"], txn["side_b"]
        return (
            side_a["entity_id"],
            side_a["account_code"],
            side_b["entity_id"],
            side_b["account_code"],
            txn["reference"] or "",
            txn["category"],
            txn["index"],
        )

    matched.sort(key=lambda entry: sort_key_of(entry["txn"]))
    eliminations: list[dict[str, Any]] = []
    reconciliation_differences: list[dict[str, Any]] = []
    for number, entry in enumerate(matched, start=1):
        elimination_id = f"elim-{number}"
        txn = entry["txn"]
        eliminations.append(
            {
                "elimination_id": elimination_id,
                "category": txn["category"],
                "reference": txn["reference"],
                "amount": _money(entry["eliminated"]),
                "lines": [
                    {
                        "entity_id": line["entity_id"],
                        "account_code": line["account_code"],
                        "group_account_code": line["group_account_code"],
                        "side": line["side"],
                        "amount": _money(line["amount"]),
                    }
                    for line in entry["lines"]
                ],
            }
        )
        if entry["difference"] > 0:
            side_a, side_b = txn["side_a"], txn["side_b"]
            reconciliation_differences.append(
                {
                    "elimination_id": elimination_id,
                    "category": txn["category"],
                    "reference": txn["reference"],
                    "entity_a": side_a["entity_id"],
                    "account_a": side_a["account_code"],
                    "amount_a": _money(side_a["amount"]),
                    "entity_b": side_b["entity_id"],
                    "account_b": side_b["account_code"],
                    "amount_b": _money(side_b["amount"]),
                    "difference": _money(entry["difference"]),
                    "eliminated_amount": _money(entry["eliminated"]),
                }
            )

    unmatched.sort(key=lambda entry: sort_key_of(entry["txn"]))
    unmatched_items = [
        {
            "category": entry["txn"]["category"],
            "reference": entry["txn"]["reference"],
            "entity_a": entry["txn"]["side_a"]["entity_id"],
            "account_a": entry["txn"]["side_a"]["account_code"],
            "amount_a": _money(entry["txn"]["side_a"]["amount"]),
            "entity_b": entry["txn"]["side_b"]["entity_id"],
            "account_b": entry["txn"]["side_b"]["account_code"],
            "amount_b": _money(entry["txn"]["side_b"]["amount"]),
            "difference": _money(entry["difference"]),
        }
        for entry in unmatched
    ]

    # ---- 抵消后的集团科目余额：净额抵销后仅落一侧，零额两侧均为 0 ----
    balances: list[dict[str, Any]] = []
    total_debit = _ZERO
    total_credit = _ZERO
    for code in sorted(group_totals):
        debit_sum, credit_sum = group_totals[code]
        net = debit_sum - credit_sum
        debit = net if net > 0 else _ZERO
        credit = -net if net < 0 else _ZERO
        total_debit += debit
        total_credit += credit
        balances.append(
            {
                "group_account_code": code,
                "debit": _money(debit),
                "credit": _money(credit),
            }
        )

    # ---- 少数股东权益：子公司净资产余额（抵消前）乘以未持股比例，单列 ----
    minority_interests: list[dict[str, Any]] = []
    for entity in sorted(entities, key=lambda item: item["entity_id"]):
        ratio = entity["ownership_ratio"]
        if ratio >= _ONE:
            continue
        net_assets = _ZERO
        for item in entity["items"]:
            category = group_of(entity["entity_id"], item["account_code"])["category"]
            if category in ("asset", "liability"):
                net_assets += item["debit"] - item["credit"]
        minority_ratio = _ONE - ratio
        minority_interests.append(
            {
                "entity_id": entity["entity_id"],
                "ownership_ratio": _exact(ratio),
                "net_assets": _money(net_assets),
                "minority_ratio": _exact(minority_ratio),
                "minority_interest": _money(net_assets * minority_ratio),
            }
        )

    return {
        "period": periods[0] if periods else None,
        "entity_count": len(entities),
        "tolerance": _exact(tolerance),
        "balances": balances,
        "total_debit": _money(total_debit),
        "total_credit": _money(total_credit),
        "eliminations": eliminations,
        "reconciliation_differences": reconciliation_differences,
        "unmatched_items": unmatched_items,
        "minority_interests": minority_interests,
    }
