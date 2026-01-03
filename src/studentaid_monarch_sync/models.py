from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field
from typing import Optional


class LoanSnapshot(BaseModel):
    group: str

    principal_balance_cents: int
    accrued_interest_cents: int
    outstanding_balance_cents: int
    daily_interest_accrual_cents: int = 0

    due_date: Optional[date] = None
    last_payment_date: Optional[date] = None
    last_payment_amount_cents: Optional[int] = None

    # Optional metadata useful for debugging/auditing
    raw_effective_interest_rate: Optional[str] = None
    raw_regulatory_interest_rate: Optional[str] = None
    scraped_at: date = Field(default_factory=lambda: date.today())


class PaymentAllocation(BaseModel):
    payment_date: date
    group: str

    total_applied_cents: int
    principal_applied_cents: int
    interest_applied_cents: int

    payment_total_cents: int
    payment_reference: Optional[str] = None

    def allocation_key(self) -> str:
        # Used for idempotency. Keep stable and human-readable.
        parts = [
            self.payment_date.isoformat(),
            self.payment_reference or "",
            self.group,
            str(self.total_applied_cents),
            str(self.principal_applied_cents),
            str(self.interest_applied_cents),
            str(self.payment_total_cents),
        ]
        return "|".join(parts)


