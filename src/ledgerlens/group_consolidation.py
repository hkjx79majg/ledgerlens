"""无状态集团试算平衡表合并与内部交易抵消。

面向同一记账本位币、同一会计期间的母子公司余额数据：每个实体携带唯一
实体标识、期间与若干 (科目编码, 借贷方向, Decimal 金额) 余额，集团科目
映射把各实体科目归并到统一集团科目，持股比例用于计算非全资子公司的少数
股东权益。本次不处理汇率折算、分步收购、处置与购买日公允价值调整。

内部往来由调用方以成对声明提交：每对含双方实体、各自科目、各自声明金额、
抵消类别（receivable_payable / revenue_cost / dividend）与可选业务引用；
系统只按声明抵消，不凭科目名称猜测交易关系。金额匹配规则：双方声明金额
相等时全额抵消；差额绝对值不超过调用方给出的非负容差时，以较小金额抵消
并把差额写入对账差异；超过容差时保留双方原余额、不生成该对抵消分录，
并把它列为未匹配项目。

校验顺序：输入结构、容差与持股比例（InvalidConsolidationInputError）→
实体期间一致（PeriodMismatchError）→ 实体标识不重复
（DuplicateEntityError）→ 每份余额借贷平衡（UnbalancedEntityError）→
科目映射完整（AccountMappingError）。任一校验失败即抛出异常，不返回部分
合并结果。空实体集合返回金额全为零且明细为空的有效结果。

所有金额运算保持 Decimal 精度，不转为二进制浮点数。纯函数实现：不修改
输入、不落盘、不保留跨调用状态，相同输入必然得到相同输出；返回集合按
实体标识、科目编码与业务引用稳定排序。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "AccountBalance",
    "AccountMappingError",
    "ConsolidationError",
    "DuplicateEntityError",
    "EliminationCategory",
    "EliminationEntry",
    "EliminationLine",
    "EntityBalances",
    "GroupAccountBalance",
    "GroupConsolidationResult",
    "IntercompanyPair",
    "IntercompanySide",
    "InvalidConsolidationInputError",
    "MinorityInterest",
    "PeriodMismatchError",
    "ReconciliationDifference",
    "UnbalancedEntityError",
    "UnmatchedIntercompanyPair",
    "consolidate_group_trial_balance",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")

_DEBIT = "debit"
_CREDIT = "credit"
_DIRECTIONS = {"debit": _DEBIT, "credit": _CREDIT, "dr": _DEBIT, "cr": _CREDIT}


class ConsolidationError(Exception):
    """集团合并相关错误的公共基类。"""


class PeriodMismatchError(ConsolidationError):
    """实体会计期间不一致。"""


class DuplicateEntityError(ConsolidationError):
    """实体标识在输入中重复。"""


class UnbalancedEntityError(ConsolidationError):
    """任一实体的借贷余额合计不相等。"""


class AccountMappingError(ConsolidationError):
    """实体科目缺少集团科目映射。"""


class InvalidConsolidationInputError(ConsolidationError, ValueError):
    """合并输入结构、持股比例或容差无效。"""


class EliminationCategory(str, Enum):
    """内部交易抵消类别。"""

    RECEIVABLE_PAYABLE = "receivable_payable"
    REVENUE_COST = "revenue_cost"
    DIVIDEND = "dividend"


@dataclass(frozen=True)
class AccountBalance:
    """单科目余额：科目编码、借贷方向与非负 Decimal 金额。"""

    account_code: str
    direction: str
    amount: Decimal


@dataclass(frozen=True)
class EntityBalances:
    """单一实体的余额输入：唯一实体标识、期间与科目余额序列。"""

    entity_id: str
    period: str
    balances: Sequence[Any] = ()


@dataclass(frozen=True)
class IntercompanySide:
    """内部往来声明的单方：实体、科目与声明金额。"""

    entity_id: str
    account_code: str
    amount: Decimal


@dataclass(frozen=True)
class IntercompanyPair:
    """成对的内部往来声明：双方、抵消类别与可选业务引用。"""

    side_a: IntercompanySide
    side_b: IntercompanySide
    category: Any
    reference: str | None = None


@dataclass(frozen=True)
class GroupAccountBalance:
    """抵消后的集团科目余额：净额仅落一侧，零额两侧均为零。"""

    account_code: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True)
class EliminationLine:
    """抵消分录行：可按实体、科目与集团科目追溯。"""

    entity_id: str
    account_code: str
    group_account_code: str
    direction: str
    amount: Decimal


@dataclass(frozen=True)
class EliminationEntry:
    """一笔抵消分录：类别、可选业务引用、抵消金额与双方行。"""

    category: EliminationCategory
    reference: str | None
    amount: Decimal
    lines: tuple[EliminationLine, ...]


@dataclass(frozen=True)
class ReconciliationDifference:
    """容差内按较小金额抵消后留下的对账差异。"""

    category: EliminationCategory
    reference: str | None
    sides: tuple[IntercompanySide, IntercompanySide]
    eliminated_amount: Decimal
    difference: Decimal


@dataclass(frozen=True)
class UnmatchedIntercompanyPair:
    """差额超过容差而未抵消的内部往来声明。"""

    category: EliminationCategory
    reference: str | None
    sides: tuple[IntercompanySide, IntercompanySide]
    difference: Decimal
    reason: str


@dataclass(frozen=True)
class MinorityInterest:
    """非全资子公司的少数股东权益：净资产余额乘以未持股比例。"""

    entity_id: str
    ownership_ratio: Decimal
    net_assets: Decimal
    minority_interest: Decimal


@dataclass(frozen=True)
class GroupConsolidationResult:
    """集团合并结果：抵消后余额、借贷合计、抵消分录、对账差异、
    未匹配项目与单列的少数股东权益。"""

    period: str | None
    group_balances: tuple[GroupAccountBalance, ...]
    total_debit: Decimal
    total_credit: Decimal
    elimination_entries: tuple[EliminationEntry, ...]
    reconciliation_differences: tuple[ReconciliationDifference, ...]
    unmatched_items: tuple[UnmatchedIntercompanyPair, ...]
    minority_interests: tuple[MinorityInterest, ...]
    minority_interest_total: Decimal


def _to_decimal(value: Any, what: str) -> Decimal:
    """把可无损转换的输入解析为有限 Decimal；否则抛 InvalidConsolidationInputError。"""
    if isinstance(value, bool):
        raise InvalidConsolidationInputError(f"{what} must be a decimal number, got bool")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            raise InvalidConsolidationInputError(
                f"{what} must be a decimal number, got {value!r}"
            ) from None
    else:
        raise InvalidConsolidationInputError(
            f"{what} must be a decimal number, got {type(value).__name__}"
        )
    if not candidate.is_finite():
        raise InvalidConsolidationInputError(f"{what} must be a finite decimal number")
    return candidate


def _to_direction(value: Any, what: str) -> str:
    """把借贷方向规范为 "debit" 或 "credit"；否则抛 InvalidConsolidationInputError。"""
    if isinstance(value, str):
        direction = _DIRECTIONS.get(value.strip().lower())
        if direction is not None:
            return direction
    raise InvalidConsolidationInputError(
        f"{what} must be 'debit' or 'credit', got {value!r}"
    )


def _to_nonempty_string(value: Any, what: str) -> str:
    """校验非空字符串；否则抛 InvalidConsolidationInputError。"""
    if not isinstance(value, str) or value == "":
        raise InvalidConsolidationInputError(
            f"{what} must be a non-empty string, got {value!r}"
        )
    return value


def _normalize_balance(item: Any, entity_id: str) -> AccountBalance | None:
    """规范化一条科目余额；零额试算平衡表行返回 None（不参与汇总）。

    接受 AccountBalance、{account_code/code, direction/side, amount} 字典，
    以及 {account_code/code, debit, credit} 或 {account_code/code,
    ending_debit, ending_credit} 的试算平衡表式字典（仅一侧大于零）。
    """
    what = f"balance of entity {entity_id!r}"
    if isinstance(item, AccountBalance):
        code = _to_nonempty_string(item.account_code, f"{what} account_code")
        direction = _to_direction(item.direction, f"{what} direction")
        amount = _to_decimal(item.amount, f"{what} amount")
    elif isinstance(item, Mapping):
        raw_code = item.get("account_code", item.get("code"))
        code = _to_nonempty_string(raw_code, f"{what} account_code")
        if "direction" in item or "side" in item:
            direction = _to_direction(
                item.get("direction", item.get("side")), f"{what} direction"
            )
            if "amount" not in item:
                raise InvalidConsolidationInputError(f"{what} amount is required")
            amount = _to_decimal(item["amount"], f"{what} amount")
        else:
            for debit_key, credit_key in (
                ("debit", "credit"),
                ("ending_debit", "ending_credit"),
            ):
                if debit_key in item or credit_key in item:
                    debit = _to_decimal(item.get(debit_key, _ZERO), f"{what} {debit_key}")
                    credit = _to_decimal(item.get(credit_key, _ZERO), f"{what} {credit_key}")
                    if debit < _ZERO or credit < _ZERO:
                        raise InvalidConsolidationInputError(
                            f"{what} amounts must not be negative"
                        )
                    if debit > _ZERO and credit > _ZERO:
                        raise InvalidConsolidationInputError(
                            f"{what} must not have both debit and credit greater than zero"
                        )
                    if debit == _ZERO and credit == _ZERO:
                        return None
                    direction = _DEBIT if debit > _ZERO else _CREDIT
                    amount = debit if debit > _ZERO else credit
                    break
            else:
                raise InvalidConsolidationInputError(
                    f"{what} must carry direction/amount or debit/credit amounts"
                )
    else:
        raise InvalidConsolidationInputError(
            f"{what} must be an AccountBalance or a mapping, got {type(item).__name__}"
        )
    if amount < _ZERO:
        raise InvalidConsolidationInputError(f"{what} amount must not be negative")
    return AccountBalance(code, direction, amount)


def _normalize_entity(entity: Any) -> tuple[str, str, list[AccountBalance]]:
    """规范化一个实体输入，返回 (entity_id, period, balances)。"""
    if isinstance(entity, EntityBalances):
        entity_id, period, balances = entity.entity_id, entity.period, entity.balances
    elif isinstance(entity, Mapping):
        entity_id = entity.get("entity_id")
        period = entity.get("period")
        balances = entity.get("balances", ())
    else:
        raise InvalidConsolidationInputError(
            f"entity must be an EntityBalances or a mapping, got {type(entity).__name__}"
        )
    entity_id = _to_nonempty_string(entity_id, "entity_id")
    period = _to_nonempty_string(period, f"period of entity {entity_id!r}")
    if isinstance(balances, (str, bytes)) or not isinstance(balances, Sequence):
        raise InvalidConsolidationInputError(
            f"balances of entity {entity_id!r} must be a sequence"
        )
    normalized: list[AccountBalance] = []
    for item in balances:
        balance = _normalize_balance(item, entity_id)
        if balance is not None:
            normalized.append(balance)
    return entity_id, period, normalized


def _normalize_side(side: Any, what: str) -> IntercompanySide:
    """规范化内部往来声明的单方。"""
    if isinstance(side, IntercompanySide):
        entity_id, account_code, amount = side.entity_id, side.account_code, side.amount
    elif isinstance(side, Mapping):
        entity_id = side.get("entity_id")
        account_code = side.get("account_code", side.get("code"))
        amount = side.get("amount")
    else:
        raise InvalidConsolidationInputError(
            f"{what} must be an IntercompanySide or a mapping, got {type(side).__name__}"
        )
    entity_id = _to_nonempty_string(entity_id, f"{what} entity_id")
    account_code = _to_nonempty_string(account_code, f"{what} account_code")
    amount = _to_decimal(amount, f"{what} amount")
    if amount < _ZERO:
        raise InvalidConsolidationInputError(f"{what} amount must not be negative")
    return IntercompanySide(entity_id, account_code, amount)


def _normalize_category(value: Any) -> EliminationCategory:
    """规范化抵消类别；未知类别抛 InvalidConsolidationInputError。"""
    if isinstance(value, EliminationCategory):
        return value
    if isinstance(value, str):
        try:
            return EliminationCategory(value.strip().lower())
        except ValueError:
            pass
    raise InvalidConsolidationInputError(
        f"elimination category must be one of "
        f"{[category.value for category in EliminationCategory]}, got {value!r}"
    )


def _normalize_pair(pair: Any) -> IntercompanyPair:
    """规范化一对内部往来声明。"""
    if isinstance(pair, IntercompanyPair):
        side_a, side_b = pair.side_a, pair.side_b
        category, reference = pair.category, pair.reference
    elif isinstance(pair, Mapping):
        category = pair.get("category")
        reference = pair.get("reference")
        if "sides" in pair:
            sides = pair["sides"]
            if not isinstance(sides, Sequence) or isinstance(sides, (str, bytes)):
                raise InvalidConsolidationInputError("intercompany sides must be a sequence")
            if len(sides) != 2:
                raise InvalidConsolidationInputError(
                    f"intercompany sides must contain exactly two sides, got {len(sides)}"
                )
            side_a, side_b = sides[0], sides[1]
        elif "side_a" in pair or "side_b" in pair:
            side_a, side_b = pair.get("side_a"), pair.get("side_b")
        else:
            side_a = {
                "entity_id": pair.get("entity_a"),
                "account_code": pair.get("account_a"),
                "amount": pair.get("amount_a"),
            }
            side_b = {
                "entity_id": pair.get("entity_b"),
                "account_code": pair.get("account_b"),
                "amount": pair.get("amount_b"),
            }
    else:
        raise InvalidConsolidationInputError(
            f"intercompany pair must be an IntercompanyPair or a mapping, "
            f"got {type(pair).__name__}"
        )
    normalized_a = _normalize_side(side_a, "intercompany side_a")
    normalized_b = _normalize_side(side_b, "intercompany side_b")
    category = _normalize_category(category)
    if reference is not None:
        reference = _to_nonempty_string(reference, "intercompany reference")
    return IntercompanyPair(normalized_a, normalized_b, category, reference)


def _side_key(side: IntercompanySide) -> tuple[str, str]:
    return (side.entity_id, side.account_code)


def _pair_key(
    category: EliminationCategory,
    reference: str | None,
    sides: tuple[IntercompanySide, IntercompanySide],
) -> tuple[str, str, str, str]:
    first = sides[0]
    return (first.entity_id, first.account_code, reference or "", category.value)


def consolidate_group_trial_balance(
    entities: Sequence[Any] = (),
    account_mapping: Mapping[str, str] | None = None,
    ownership: Mapping[str, Any] | None = None,
    intercompany_pairs: Sequence[Any] = (),
    tolerance: Any = _ZERO,
) -> GroupConsolidationResult:
    """合并集团试算平衡表并抵消内部交易，返回 GroupConsolidationResult。

    参数：
        entities: 实体余额序列，每项为 EntityBalances 或
            {"entity_id", "period", "balances"} 字典；余额项为 AccountBalance、
            {"account_code", "direction", "amount"} 或试算平衡表式
            {"account_code", "debit", "credit"}（含 ending_debit/ending_credit）。
        account_mapping: 实体科目编码到集团科目编码的映射，须覆盖全部实体科目。
        ownership: 实体标识到持股比例（0 到 1 闭区间）的映射，缺省视为 1（全资）。
        intercompany_pairs: 成对内部往来声明序列，每项为 IntercompanyPair 或
            含 category、可选 reference 与双方（sides/side_a+side_b/扁平字段）的字典。
        tolerance: 非负容差，双方声明金额差额绝对值不超过它时按较小金额抵消。

    抛出：PeriodMismatchError、DuplicateEntityError、UnbalancedEntityError、
        AccountMappingError、InvalidConsolidationInputError；异常时不返回
        部分合并结果。纯函数：不保留跨调用状态。
    """
    # ---- 标量参数：容差非负、持股比例落在 [0, 1] 闭区间 ----
    tolerance = _to_decimal(tolerance, "tolerance")
    if tolerance < _ZERO:
        raise InvalidConsolidationInputError("tolerance must not be negative")

    ratios: dict[str, Decimal] = {}
    if ownership is not None:
        if not isinstance(ownership, Mapping):
            raise InvalidConsolidationInputError("ownership must be a mapping")
        for owner_id, raw_ratio in ownership.items():
            owner_id = _to_nonempty_string(owner_id, "ownership entity_id")
            ratio = _to_decimal(raw_ratio, f"ownership ratio of entity {owner_id!r}")
            if ratio < _ZERO or ratio > _ONE:
                raise InvalidConsolidationInputError(
                    f"ownership ratio of entity {owner_id!r} must be within [0, 1]"
                )
            ratios[owner_id] = ratio

    # ---- 实体结构规范化 ----
    if isinstance(entities, (str, bytes)) or not isinstance(entities, Sequence):
        raise InvalidConsolidationInputError("entities must be a sequence")
    normalized = [_normalize_entity(entity) for entity in entities]

    # ---- 期间一致 ----
    periods = {period for _, period, _ in normalized}
    if len(periods) > 1:
        raise PeriodMismatchError(
            f"all entities must share one accounting period, got {sorted(periods)!r}"
        )

    # ---- 实体标识不重复 ----
    seen_ids: set[str] = set()
    for entity_id, _, _ in normalized:
        if entity_id in seen_ids:
            raise DuplicateEntityError(f"entity_id {entity_id!r} is duplicated")
        seen_ids.add(entity_id)

    # ---- 每份余额借贷平衡，并汇总每实体每科目净额 ----
    # entity_nets[entity_id][account_code] = [direction, amount]
    entity_nets: dict[str, dict[str, list[Any]]] = {}
    entity_totals: dict[str, Decimal] = {}
    for entity_id, _, balances in normalized:
        total_debit = _ZERO
        total_credit = _ZERO
        nets: dict[str, list[Any]] = {}
        for balance in balances:
            if balance.direction == _DEBIT:
                total_debit += balance.amount
            else:
                total_credit += balance.amount
            net = nets.setdefault(balance.account_code, [_ZERO, _ZERO])
            if balance.direction == _DEBIT:
                net[0] += balance.amount
            else:
                net[1] += balance.amount
        if total_debit != total_credit:
            raise UnbalancedEntityError(
                f"entity {entity_id!r} is not balanced: "
                f"debit {total_debit} != credit {total_credit}"
            )
        entity_nets[entity_id] = {
            code: [_DEBIT, amounts[0] - amounts[1]]
            if amounts[0] > amounts[1]
            else [_CREDIT, amounts[1] - amounts[0]]
            for code, amounts in nets.items()
            if amounts[0] != amounts[1]
        }
        entity_totals[entity_id] = total_debit

    # ---- 科目映射完整 ----
    if account_mapping is not None and not isinstance(account_mapping, Mapping):
        raise InvalidConsolidationInputError("account_mapping must be a mapping")
    mapping: dict[str, str] = dict(account_mapping) if account_mapping else {}
    for _, _, balances in normalized:
        for balance in balances:
            group_code = mapping.get(balance.account_code)
            if group_code is None:
                raise AccountMappingError(
                    f"account {balance.account_code!r} has no group account mapping"
                )
            _to_nonempty_string(
                group_code, f"group account of {balance.account_code!r}"
            )

    # ---- 内部往来声明：结构、类别、引用实体与科目余额存在 ----
    if isinstance(intercompany_pairs, (str, bytes)) or not isinstance(
        intercompany_pairs, Sequence
    ):
        raise InvalidConsolidationInputError("intercompany_pairs must be a sequence")
    pairs = [_normalize_pair(pair) for pair in intercompany_pairs]
    for pair in pairs:
        for side in (pair.side_a, pair.side_b):
            nets = entity_nets.get(side.entity_id)
            if nets is None:
                raise InvalidConsolidationInputError(
                    f"intercompany declaration references unknown entity "
                    f"{side.entity_id!r}"
                )
            if side.account_code not in nets:
                raise InvalidConsolidationInputError(
                    f"entity {side.entity_id!r} has no balance on account "
                    f"{side.account_code!r} declared as intercompany"
                )

    # ---- 按集团科目汇总各实体余额 ----
    group: dict[str, list[Decimal]] = {}
    for entity_id, _, _ in normalized:
        for code, (direction, amount) in entity_nets[entity_id].items():
            bucket = group.setdefault(mapping[code], [_ZERO, _ZERO])
            bucket[0 if direction == _DEBIT else 1] += amount

    # ---- 抵消：相等全额抵消；容差内按较小金额并记对账差异；超容差不抵消 ----
    elimination_entries: list[EliminationEntry] = []
    differences: list[ReconciliationDifference] = []
    unmatched: list[UnmatchedIntercompanyPair] = []
    for pair in pairs:
        side_a, side_b = pair.side_a, pair.side_b
        difference = abs(side_a.amount - side_b.amount)
        sides = tuple(sorted((side_a, side_b), key=_side_key))
        if difference > tolerance:
            unmatched.append(
                UnmatchedIntercompanyPair(
                    pair.category,
                    pair.reference,
                    sides,
                    difference,
                    "amount_difference_exceeds_tolerance",
                )
            )
            continue
        amount = min(side_a.amount, side_b.amount)
        lines: list[EliminationLine] = []
        for side in (side_a, side_b):
            balance_direction = entity_nets[side.entity_id][side.account_code][0]
            post_direction = _CREDIT if balance_direction == _DEBIT else _DEBIT
            group_code = mapping[side.account_code]
            bucket = group.setdefault(group_code, [_ZERO, _ZERO])
            bucket[0 if post_direction == _DEBIT else 1] += amount
            lines.append(
                EliminationLine(
                    side.entity_id, side.account_code, group_code, post_direction, amount
                )
            )
        lines.sort(key=lambda line: (line.entity_id, line.account_code))
        elimination_entries.append(
            EliminationEntry(pair.category, pair.reference, amount, tuple(lines))
        )
        if difference > _ZERO:
            differences.append(
                ReconciliationDifference(
                    pair.category, pair.reference, sides, amount, difference
                )
            )

    elimination_entries.sort(
        key=lambda entry: (
            entry.lines[0].entity_id,
            entry.lines[0].account_code,
            entry.reference or "",
            entry.category.value,
        )
    )
    differences.sort(
        key=lambda item: _pair_key(item.category, item.reference, item.sides)
    )
    unmatched.sort(key=lambda item: _pair_key(item.category, item.reference, item.sides))

    # ---- 抵消后的集团科目余额：净额仅落一侧，零额两侧均为零 ----
    group_balances: list[GroupAccountBalance] = []
    total_debit = _ZERO
    total_credit = _ZERO
    for code in sorted(group):
        debit, credit = group[code]
        net = debit - credit
        if net > _ZERO:
            balance_debit, balance_credit = net, _ZERO
        elif net < _ZERO:
            balance_debit, balance_credit = _ZERO, -net
        else:
            balance_debit = balance_credit = _ZERO
        group_balances.append(GroupAccountBalance(code, balance_debit, balance_credit))
        total_debit += balance_debit
        total_credit += balance_credit

    # ---- 少数股东权益：子公司净资产余额（借贷平衡，取借方合计）乘以未持股比例，单列 ----
    minority_interests: list[MinorityInterest] = []
    for entity_id in sorted(entity_totals):
        ratio = ratios.get(entity_id, _ONE)
        if ratio >= _ONE:
            continue
        net_assets = entity_totals[entity_id]
        minority_interests.append(
            MinorityInterest(
                entity_id, ratio, net_assets, net_assets * (_ONE - ratio)
            )
        )
    minority_total = sum(
        (item.minority_interest for item in minority_interests), _ZERO
    )

    return GroupConsolidationResult(
        period=next(iter(periods)) if periods else None,
        group_balances=tuple(group_balances),
        total_debit=total_debit,
        total_credit=total_credit,
        elimination_entries=tuple(elimination_entries),
        reconciliation_differences=tuple(differences),
        unmatched_items=tuple(unmatched),
        minority_interests=tuple(minority_interests),
        minority_interest_total=minority_total,
    )
