"""LedgerLens - 财务报表与会计分析引擎."""

from .group_consolidation import (
    AccountMappingError,
    ConsolidationError,
    DuplicateEntityError,
    InvalidConsolidationInputError,
    PeriodMismatchError,
    UnbalancedEntityError,
    consolidate_group_trial_balance,
)

__version__ = "0.1.0"

__all__ = [
    "AccountMappingError",
    "ConsolidationError",
    "DuplicateEntityError",
    "InvalidConsolidationInputError",
    "PeriodMismatchError",
    "UnbalancedEntityError",
    "consolidate_group_trial_balance",
    "__version__",
]
