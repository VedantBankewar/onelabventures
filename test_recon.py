"""
test_recon.py — Test suite for the Payment Reconciliation Engine.

Run with:  pytest test_recon.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal

import pytest

from models import (
    PlatformTransaction,
    BankSettlement,
    GapType,
    TransactionType,
    TransactionStatus,
    SettlementStatus,
)
from reconciler import reconcile, INDIVIDUAL_TOLERANCE, AGGREGATE_TOLERANCE
from data_gen import generate_all


# ─── Test Helpers / Factories ────────────────────────────────────────────────

def make_platform_txn(**overrides) -> PlatformTransaction:
    """Factory for PlatformTransaction with sensible defaults."""
    defaults = {
        "transaction_id": f"TXN-{uuid.uuid4().hex[:8].upper()}",
        "timestamp": datetime(2026, 3, 15, 12, 0, 0),
        "amount": Decimal("100.00"),
        "type": TransactionType.PAYMENT,
        "status": TransactionStatus.COMPLETED,
        "customer_id": "CUST-0001",
        "reference_id": None,
    }
    defaults.update(overrides)
    return PlatformTransaction(**defaults)


def make_bank_settlement(**overrides) -> BankSettlement:
    """Factory for BankSettlement with sensible defaults."""
    defaults = {
        "settlement_id": f"STL-{uuid.uuid4().hex[:8].upper()}",
        "transaction_id": f"TXN-{uuid.uuid4().hex[:8].upper()}",
        "settlement_date": date(2026, 3, 16),
        "amount": Decimal("100.00"),
        "status": SettlementStatus.SETTLED,
    }
    defaults.update(overrides)
    return BankSettlement(**defaults)


def _make_clean_pair(
    txn_id: str | None = None,
    amount: Decimal = Decimal("100.00"),
    txn_day: int = 15,
) -> tuple[PlatformTransaction, BankSettlement]:
    """Create a matched platform + bank pair."""
    tid = txn_id or f"TXN-{uuid.uuid4().hex[:8].upper()}"
    txn = make_platform_txn(transaction_id=tid, amount=amount,
                            timestamp=datetime(2026, 3, txn_day, 12, 0, 0))
    stl = make_bank_settlement(transaction_id=tid, amount=amount,
                               settlement_date=date(2026, 3, txn_day + 1))
    return txn, stl


# ─── Test Cases ──────────────────────────────────────────────────────────────

class TestExactMatch:
    """Pass 1: transactions that match perfectly."""

    def test_clean_data_all_matched(self):
        """10 clean pairs → 10 matches, 0 gaps."""
        platform, bank = [], []
        for _ in range(10):
            txn, stl = _make_clean_pair()
            platform.append(txn)
            bank.append(stl)

        result = reconcile(platform, bank, 2026, 3)

        assert result.matched == 10
        # Only gap type allowed is ROUNDING_DRIFT (if any), nothing else
        non_rounding_gaps = [g for g in result.gaps if g.gap_type != GapType.ROUNDING_DRIFT]
        assert len(non_rounding_gaps) == 0


class TestCrossMonth:
    """Pass 2: transaction at end of month settles next month."""

    def test_cross_month_detected(self):
        """Transaction on March 31, settlement on April 1 → CROSS_MONTH gap."""
        tid = "TXN-CROSSMON1"
        txn = make_platform_txn(
            transaction_id=tid,
            timestamp=datetime(2026, 3, 31, 23, 0, 0),
            amount=Decimal("150.00"),
        )
        stl = make_bank_settlement(
            transaction_id=tid,
            settlement_date=date(2026, 4, 1),
            amount=Decimal("150.00"),
        )

        result = reconcile([txn], [stl], 2026, 3)

        cross_gaps = [g for g in result.gaps if g.gap_type == GapType.CROSS_MONTH]
        assert len(cross_gaps) == 1
        assert cross_gaps[0].transaction_id == tid


class TestRoundingDrift:
    """Pass 5: aggregate rounding check."""

    def test_rounding_drift_exceeds_tolerance(self):
        """12× +0.01 and 3× −0.01 → net +0.09 → ROUNDING_DRIFT flagged."""
        platform, bank = [], []
        for i in range(15):
            txn, stl = _make_clean_pair(amount=Decimal("50.00"))
            if i < 12:
                stl = make_bank_settlement(
                    transaction_id=stl.transaction_id,
                    settlement_date=stl.settlement_date,
                    amount=Decimal("50.01"),
                )
            else:
                stl = make_bank_settlement(
                    transaction_id=stl.transaction_id,
                    settlement_date=stl.settlement_date,
                    amount=Decimal("49.99"),
                )
            platform.append(txn)
            bank.append(stl)

        result = reconcile(platform, bank, 2026, 3)

        rounding_gaps = [g for g in result.gaps if g.gap_type == GapType.ROUNDING_DRIFT]
        assert len(rounding_gaps) == 1
        # Net drift should be 12*0.01 - 3*0.01 = 0.09
        assert result.rounding_drift >= Decimal("0.09")

    def test_rounding_within_tolerance(self):
        """2× +0.01 → net +0.02 → NO rounding flag."""
        platform, bank = [], []
        for i in range(5):
            txn, stl = _make_clean_pair(amount=Decimal("50.00"))
            if i < 2:
                stl = make_bank_settlement(
                    transaction_id=stl.transaction_id,
                    settlement_date=stl.settlement_date,
                    amount=Decimal("50.01"),
                )
            platform.append(txn)
            bank.append(stl)

        result = reconcile(platform, bank, 2026, 3)

        rounding_gaps = [g for g in result.gaps if g.gap_type == GapType.ROUNDING_DRIFT]
        assert len(rounding_gaps) == 0


class TestDuplicate:
    """Pass 3: duplicate detection."""

    def test_duplicate_settlement_detected(self):
        """Same transaction_id twice in bank → DUPLICATE_SETTLEMENT."""
        tid = "TXN-DUPBANK01"
        txn = make_platform_txn(transaction_id=tid, amount=Decimal("75.00"))
        stl1 = make_bank_settlement(
            settlement_id="STL-ORIG0001",
            transaction_id=tid,
            amount=Decimal("75.00"),
        )
        stl2 = make_bank_settlement(
            settlement_id="STL-DUPE0001",
            transaction_id=tid,
            amount=Decimal("75.00"),
        )

        result = reconcile([txn], [stl1, stl2], 2026, 3)

        dup_gaps = [g for g in result.gaps if g.gap_type == GapType.DUPLICATE_SETTLEMENT]
        assert len(dup_gaps) == 1
        assert dup_gaps[0].transaction_id == tid


class TestOrphanRefund:
    """Pass 4: orphan refund detection."""

    def test_orphan_refund_detected(self):
        """Negative bank entry with unknown transaction_id → ORPHAN_REFUND."""
        orphan_stl = make_bank_settlement(
            transaction_id="TXN-ORPHAN01",
            amount=Decimal("-30.00"),
        )

        result = reconcile([], [orphan_stl], 2026, 3)

        orphan_gaps = [g for g in result.gaps if g.gap_type == GapType.ORPHAN_REFUND]
        assert len(orphan_gaps) == 1
        assert orphan_gaps[0].transaction_id == "TXN-ORPHAN01"


class TestMissingSettlement:
    """Leftover: platform transaction with no bank entry."""

    def test_missing_settlement(self):
        """Platform txn with no bank entry → MISSING_SETTLEMENT."""
        txn = make_platform_txn(transaction_id="TXN-MISSING1")

        result = reconcile([txn], [], 2026, 3)

        missing_gaps = [g for g in result.gaps if g.gap_type == GapType.MISSING_SETTLEMENT]
        assert len(missing_gaps) == 1
        assert missing_gaps[0].transaction_id == "TXN-MISSING1"


class TestUnexpectedSettlement:
    """Leftover: bank entry with no platform transaction (positive amount)."""

    def test_unexpected_settlement(self):
        """Bank entry with no platform txn (positive) → UNEXPECTED_SETTLEMENT."""
        stl = make_bank_settlement(
            transaction_id="TXN-UNEXP001",
            amount=Decimal("200.00"),
        )

        result = reconcile([], [stl], 2026, 3)

        unexpected_gaps = [g for g in result.gaps if g.gap_type == GapType.UNEXPECTED_SETTLEMENT]
        assert len(unexpected_gaps) == 1
        assert unexpected_gaps[0].transaction_id == "TXN-UNEXP001"


class TestFullIntegration:
    """End-to-end: generate data + reconcile → all 4 gap types present."""

    def test_all_four_gaps_detected(self):
        """generate_all() + reconcile() → detects cross-month, rounding, duplicate, orphan."""
        platform, bank, meta = generate_all(n=100, seed=42)
        result = reconcile(platform, bank, 2026, 3)

        gap_types_found = {g.gap_type for g in result.gaps}

        assert GapType.CROSS_MONTH in gap_types_found, "Cross-month gap not detected"
        assert GapType.DUPLICATE_SETTLEMENT in gap_types_found, "Duplicate settlement not detected"
        assert GapType.ORPHAN_REFUND in gap_types_found, "Orphan refund not detected"
        assert GapType.ROUNDING_DRIFT in gap_types_found, "Rounding drift not detected"

        # Verify against injection metadata
        cross_gaps = [g for g in result.gaps if g.gap_type == GapType.CROSS_MONTH]
        assert any(g.transaction_id == meta["cross_month_txn_id"] for g in cross_gaps)

        dup_gaps = [g for g in result.gaps if g.gap_type == GapType.DUPLICATE_SETTLEMENT]
        assert any(g.transaction_id == meta["duplicate_txn_id"] for g in dup_gaps)

        orphan_gaps = [g for g in result.gaps if g.gap_type == GapType.ORPHAN_REFUND]
        assert any(g.transaction_id == meta["orphan_refund_txn_id"] for g in orphan_gaps)

        # Matched count should be close to n
        assert result.matched >= 85, f"Expected ≥85 matches, got {result.matched}"
