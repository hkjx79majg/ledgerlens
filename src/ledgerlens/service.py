"""Core service surface for LedgerLens.

Currently exposes process health plus stateless double-entry journal
validation. Keep the public surface here backward compatible.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .journal import validate_journal_entry


class Service:
    """Stateless service surface: health reporting and journal validation."""

    name = "ledgerlens"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def validate_journal_entry(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """校验复式记账凭证，返回 (HTTP 状态码, 响应体)。不保留任何状态。"""
        return validate_journal_entry(payload)
