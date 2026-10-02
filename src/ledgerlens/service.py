"""Core service surface for LedgerLens.

Currently exposes process health, stateless double-entry journal and
chart-of-accounts validation, plus stateless trial-balance and
financial-statement generation.
Keep the public surface here backward compatible.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .chart import validate_chart_of_accounts
from .financial_statements import generate_financial_statements
from .journal import validate_journal_entry
from .trial_balance import generate_trial_balance


class Service:
    """Stateless service surface: health, validation and report generation."""

    name = "ledgerlens"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def validate_journal_entry(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """校验复式记账凭证，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return validate_journal_entry(payload)

    def validate_chart_of_accounts(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """校验科目体系，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return validate_chart_of_accounts(payload)

    def generate_trial_balance(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """由期初余额与期间凭证生成试算平衡表，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_trial_balance(payload)

    def generate_financial_statements(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """由期初余额与期间凭证生成损益表与资产负债表，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_financial_statements(payload)
