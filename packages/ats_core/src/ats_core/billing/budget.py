"""Daily spending limit, checked before every analysis and every Claude call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from ats_core.billing.ledger import BILLING_TZ, Ledger

__all__ = ["DEFAULT_DAILY_LIMIT_USD", "BudgetDecision", "DailyBudget"]

DEFAULT_DAILY_LIMIT_USD: Final = Decimal("2.00")


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    limit: Decimal
    spent: Decimal
    requested: Decimal

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.limit - self.spent)


@dataclass(frozen=True, slots=True)
class DailyBudget:
    limit: Decimal = DEFAULT_DAILY_LIMIT_USD

    def __post_init__(self) -> None:
        if not self.limit.is_finite() or self.limit < 0:
            raise ValueError("daily limit must be a finite, non-negative amount")

    def check(
        self, ledger: Ledger, requested: Decimal, *, now: datetime | None = None
    ) -> BudgetDecision:
        """Would spending ``requested`` more today stay within the limit?"""
        if requested < 0:
            raise ValueError("requested amount must not be negative")
        spent = ledger.spent_today(now=now, tz=BILLING_TZ)
        return BudgetDecision(
            allowed=spent + requested <= self.limit,
            limit=self.limit,
            spent=spent,
            requested=requested,
        )
