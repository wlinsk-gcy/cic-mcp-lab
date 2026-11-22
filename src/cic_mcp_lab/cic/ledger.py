"""In-memory credit ledger: each billable turn is settled once, keyed by provider and taskId (design §5)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LedgerEntry:
    settlement_key: str  # taskId; the submission id for providers that answer directly
    provider_id: str
    agent_id: str
    conversation_id: str
    credit_usage: float
    price_per_credit: float  # HKD per Credit

    @property
    def hkd(self) -> Decimal:
        return Decimal(str(self.credit_usage)) * Decimal(str(self.price_per_credit))


class Ledger:
    def __init__(self) -> None:
        # taskIds are only unique per provider, so entries are keyed by (provider_id, settlement_key).
        self._entries: dict[tuple[str, str], LedgerEntry] = {}

    def settle(self, entry: LedgerEntry) -> bool:
        """Record a billable turn; False when this provider's settlement key was already recorded."""
        key = (entry.provider_id, entry.settlement_key)
        if key in self._entries:
            return False
        self._entries[key] = entry
        return True

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries.values())

    def total_credit(self) -> float:
        return sum(entry.credit_usage for entry in self._entries.values())

    def total_hkd(self) -> Decimal:
        return sum((entry.hkd for entry in self._entries.values()), Decimal(0))
