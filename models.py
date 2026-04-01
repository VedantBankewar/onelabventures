"""
models.py — Data models for the Payment Reconciliation Engine.

Defines enumerations, dataclasses, and serialization helpers for
platform transactions, bank settlements, and reconciliation results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional


# ─── Enumerations ────────────────────────────────────────────────────────────

class TransactionType(Enum):
    """Type of platform transaction."""
    PAYMENT = "payment"
    REFUND = "refund"


class TransactionStatus(Enum):
    """Processing status of a platform transaction."""
    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"


class SettlementStatus(Enum):
    """Processing status of a bank settlement."""
    SETTLED = "settled"
    REJECTED = "rejected"


class GapType(Enum):
    """Category of reconciliation mismatch."""
    MATCHED = "matched"
    CROSS_MONTH = "cross_month"
    DUPLICATE_SETTLEMENT = "duplicate_settlement"
    DUPLICATE_TRANSACTION = "duplicate_transaction"
    ORPHAN_REFUND = "orphan_refund"
    MISSING_SETTLEMENT = "missing_settlement"
    UNEXPECTED_SETTLEMENT = "unexpected_settlement"
    ROUNDING_DRIFT = "rounding_drift"


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class PlatformTransaction:
    """A transaction recorded by the payments platform."""
    transaction_id: str
    timestamp: datetime
    amount: Decimal
    type: TransactionType
    status: TransactionStatus
    customer_id: str
    reference_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for CSV / JSON."""
        return {
            "transaction_id": self.transaction_id,
            "timestamp": self.timestamp.isoformat(),
            "amount": str(self.amount),
            "type": self.type.value,
            "status": self.status.value,
            "customer_id": self.customer_id,
            "reference_id": self.reference_id or "",
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlatformTransaction:
        """Deserialize from a plain dict."""
        return cls(
            transaction_id=d["transaction_id"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            amount=Decimal(d["amount"]),
            type=TransactionType(d["type"]),
            status=TransactionStatus(d["status"]),
            customer_id=d["customer_id"],
            reference_id=d["reference_id"] if d.get("reference_id") else None,
        )


@dataclass
class BankSettlement:
    """A settlement record from the acquiring bank."""
    settlement_id: str
    transaction_id: str
    settlement_date: date
    amount: Decimal
    status: SettlementStatus

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for CSV / JSON."""
        return {
            "settlement_id": self.settlement_id,
            "transaction_id": self.transaction_id,
            "settlement_date": self.settlement_date.isoformat(),
            "amount": str(self.amount),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BankSettlement:
        """Deserialize from a plain dict."""
        return cls(
            settlement_id=d["settlement_id"],
            transaction_id=d["transaction_id"],
            settlement_date=date.fromisoformat(d["settlement_date"]),
            amount=Decimal(d["amount"]),
            status=SettlementStatus(d["status"]),
        )


@dataclass
class GapRecord:
    """A single reconciliation mismatch."""
    gap_type: GapType
    transaction_id: str
    platform_amount: Optional[Decimal] = None
    bank_amount: Optional[Decimal] = None
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "gap_type": self.gap_type.value,
            "transaction_id": self.transaction_id,
            "platform_amount": str(self.platform_amount) if self.platform_amount is not None else None,
            "bank_amount": str(self.bank_amount) if self.bank_amount is not None else None,
            "details": self.details,
        }


@dataclass
class ReconciliationResult:
    """Aggregate result of a reconciliation run."""
    month: str
    total_platform: int
    total_bank: int
    matched: int
    gaps: list[GapRecord] = field(default_factory=list)
    platform_sum: Decimal = Decimal("0")
    bank_sum: Decimal = Decimal("0")
    rounding_drift: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "total_platform": self.total_platform,
            "total_bank": self.total_bank,
            "matched": self.matched,
            "platform_sum": str(self.platform_sum),
            "bank_sum": str(self.bank_sum),
            "rounding_drift": str(self.rounding_drift),
            "gaps": [g.to_dict() for g in self.gaps],
            "gap_summary": self._gap_summary(),
        }

    def _gap_summary(self) -> dict[str, int]:
        """Count gaps by type."""
        counts: dict[str, int] = {}
        for g in self.gaps:
            key = g.gap_type.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
