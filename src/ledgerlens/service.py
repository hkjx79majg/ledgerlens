"""Core service surface for LedgerLens.

Currently exposes process health, stateless double-entry journal and
chart-of-accounts validation, plus stateless trial-balance,
financial-statement, consolidated-financial-statement, cash-flow-statement,
period-close, multi-period financial-analysis,
recognition-schedule, depreciation-schedule, asset-impairment,
foreign-currency-remeasurement generation, deferred-tax calculation
and group trial-balance consolidation with intercompany elimination.
Keep the public surface here backward compatible.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .cash_flow import generate_cash_flow_statement
from .chart import validate_chart_of_accounts
from .consolidated import generate_consolidated_financial_statements
from .deferred_tax import calculate_deferred_tax
from .depreciation import generate_depreciation_schedule
from .financial_analysis import generate_financial_analysis
from .financial_statements import generate_financial_statements
from .group_consolidation import consolidate_group_trial_balance
from .impairment import generate_asset_impairment
from .journal import validate_journal_entry
from .period_close import generate_period_close
from .recognition import generate_recognition_schedule
from .remeasurement import generate_foreign_currency_remeasurement
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

    def generate_financial_analysis(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """对至少两期账套做多期财务分析与趋势比率，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_financial_analysis(payload)

    def generate_consolidated_financial_statements(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """汇总多主体账务并抵消内部交易后生成合并财务报表，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_consolidated_financial_statements(payload)

    def generate_cash_flow_statement(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """由期初余额与期间凭证生成现金流量表，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_cash_flow_statement(payload)

    def generate_period_close(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """生成期末损益结转凭证与下一期期初余额，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_period_close(payload)

    def generate_recognition_schedule(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """生成待摊费用或递延收入的月度确认计划与复式分录，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_recognition_schedule(payload)

    def generate_depreciation_schedule(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """生成固定资产直线法折旧计划与复式分录，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_depreciation_schedule(payload)

    def generate_asset_impairment(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """进行固定资产减值测算并生成复式分录，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_asset_impairment(payload)

    def generate_foreign_currency_remeasurement(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """按结账日汇率重估外币货币性头寸并生成复式分录，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return generate_foreign_currency_remeasurement(payload)

    def calculate_deferred_tax(self, payload: dict[str, Any]) -> dict[str, Any]:
        """根据账面价值与计税基础计算递延所得税，校验失败统一抛出 ValueError。不保留任何状态。"""
        return calculate_deferred_tax(payload)

    def consolidate_group_trial_balance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """合并集团试算平衡表并抵消内部交易，校验失败抛出 ConsolidationError 相应子类。不保留任何状态。"""
        return consolidate_group_trial_balance(payload)
