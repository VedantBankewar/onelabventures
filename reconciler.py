"""
reconciler.py — 5-pass reconciliation engine for the Payment Reconciliation Engine.

Pass 1: Exact match (by transaction_id + amount tolerance)
Pass 2: Cross-month settlement detection
Pass 3: Duplicate detection (bank & platform)
Pass 4: Orphan refund detection
Pass 5: Aggregate rounding check
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from calendar import monthrange
from decimal import Decimal
from typing import List, Tuple, Optional

from models import (
    PlatformTransaction,
    BankSettlement,
    GapRecord,
    GapType,
    ReconciliationResult,
    TransactionType,
)


# ─── Configuration ───────────────────────────────────────────────────────────

INDIVIDUAL_TOLERANCE = Decimal("0.01")  # per-transaction amount tolerance
AGGREGATE_TOLERANCE = Decimal("0.05")   # cumulative rounding tolerance
CROSS_MONTH_LOOKBACK_DAYS = 2           # how many end-of-month days to check


# ─── Filtering Helpers ───────────────────────────────────────────────────────

def filter_platform_by_month(
    txns: List[PlatformTransaction], year: int, month: int
) -> List[PlatformTransaction]:
    """Return only platform transactions whose timestamp falls in the given month."""
    return [t for t in txns if t.timestamp.year == year and t.timestamp.month == month]


def filter_bank_by_month(
    settlements: List[BankSettlement], year: int, month: int
) -> List[BankSettlement]:
    """Return only bank settlements whose settlement_date falls in the given month."""
    return [s for s in settlements if s.settlement_date.year == year and s.settlement_date.month == month]


# ─── Pass 1: Exact Match ────────────────────────────────────────────────────

def _pass_exact_match(
    platform_txns: List[PlatformTransaction],
    bank_settlements: List[BankSettlement],
) -> Tuple[
    List[Tuple[PlatformTransaction, BankSettlement]],  # matched pairs
    List[PlatformTransaction],                          # unmatched platform
    List[BankSettlement],                               # unmatched bank
]:
    """
    Match platform transactions to bank settlements by transaction_id.
    Amounts must be within INDIVIDUAL_TOLERANCE to count as matched.
    """
    # Build lookup: transaction_id → list of bank settlements
    bank_by_txn: dict[str, List[BankSettlement]] = {}
    for s in bank_settlements:
        bank_by_txn.setdefault(s.transaction_id, []).append(s)

    matched: List[Tuple[PlatformTransaction, BankSettlement]] = []
    unmatched_platform: List[PlatformTransaction] = []
    used_settlement_ids: set[str] = set()

    for txn in platform_txns:
        candidates = bank_by_txn.get(txn.transaction_id, [])
        found = False
        for s in candidates:
            if s.settlement_id not in used_settlement_ids:
                if abs(txn.amount - s.amount) <= INDIVIDUAL_TOLERANCE:
                    matched.append((txn, s))
                    used_settlement_ids.add(s.settlement_id)
                    found = True
                    break
        if not found:
            unmatched_platform.append(txn)

    unmatched_bank = [s for s in bank_settlements if s.settlement_id not in used_settlement_ids]

    return matched, unmatched_platform, unmatched_bank


# ─── Pass 2: Cross-Month Detection ──────────────────────────────────────────

def _pass_cross_month(
    unmatched_platform: List[PlatformTransaction],
    all_bank_settlements: List[BankSettlement],
    year: int,
    month: int,
) -> Tuple[List[GapRecord], List[PlatformTransaction]]:
    """
    For unmatched platform transactions from the last N days of the month,
    look for a matching settlement in the following month.
    """
    last_day = monthrange(year, month)[1]
    cutoff_day = last_day - CROSS_MONTH_LOOKBACK_DAYS + 1  # e.g., day 30 for a 31-day month

    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    # Bank settlements in the next month, indexed by transaction_id
    next_month_bank = {
        s.transaction_id: s
        for s in all_bank_settlements
        if s.settlement_date.year == next_year and s.settlement_date.month == next_month
    }

    cross_month_gaps: List[GapRecord] = []
    still_unmatched: List[PlatformTransaction] = []

    for txn in unmatched_platform:
        if txn.timestamp.day >= cutoff_day:
            bank = next_month_bank.get(txn.transaction_id)
            if bank is not None:
                cross_month_gaps.append(GapRecord(
                    gap_type=GapType.CROSS_MONTH,
                    transaction_id=txn.transaction_id,
                    platform_amount=txn.amount,
                    bank_amount=bank.amount,
                    details=(
                        f"Transaction on {txn.timestamp.date()} settles "
                        f"{bank.settlement_date} (next month)"
                    ),
                ))
                continue
        still_unmatched.append(txn)

    return cross_month_gaps, still_unmatched


# ─── Pass 3: Duplicate Detection ────────────────────────────────────────────

def _pass_duplicates(
    all_bank_settlements: List[BankSettlement],
    all_platform_txns: List[PlatformTransaction],
) -> List[GapRecord]:
    """
    Detect duplicate transaction_ids in either dataset.
    """
    gaps: List[GapRecord] = []

    # Bank duplicates
    bank_counter = Counter(s.transaction_id for s in all_bank_settlements)
    for txn_id, count in bank_counter.items():
        if count > 1:
            # Find amount for detail
            amounts = [s.amount for s in all_bank_settlements if s.transaction_id == txn_id]
            gaps.append(GapRecord(
                gap_type=GapType.DUPLICATE_SETTLEMENT,
                transaction_id=txn_id,
                bank_amount=amounts[0],
                details=f"Appears {count}x in bank settlements (amounts: {amounts})",
            ))

    # Platform duplicates
    platform_counter = Counter(t.transaction_id for t in all_platform_txns)
    for txn_id, count in platform_counter.items():
        if count > 1:
            gaps.append(GapRecord(
                gap_type=GapType.DUPLICATE_TRANSACTION,
                transaction_id=txn_id,
                details=f"Appears {count}x in platform transactions",
            ))

    return gaps


# ─── Pass 4: Orphan Refund Detection ────────────────────────────────────────

def _pass_orphan_refunds(
    unmatched_bank: List[BankSettlement],
    platform_txn_ids: set[str],
) -> Tuple[List[GapRecord], List[BankSettlement]]:
    """
    Bank settlements with negative amounts that have no matching platform transaction.
    """
    orphan_gaps: List[GapRecord] = []
    remaining_bank: List[BankSettlement] = []

    for s in unmatched_bank:
        if s.amount < 0 and s.transaction_id not in platform_txn_ids:
            orphan_gaps.append(GapRecord(
                gap_type=GapType.ORPHAN_REFUND,
                transaction_id=s.transaction_id,
                bank_amount=s.amount,
                details=f"Refund of {s.amount} with no matching platform transaction",
            ))
        else:
            remaining_bank.append(s)

    return orphan_gaps, remaining_bank


# ─── Pass 5: Aggregate Rounding Check ───────────────────────────────────────

def _pass_rounding(
    matched_pairs: List[Tuple[PlatformTransaction, BankSettlement]],
) -> Optional[GapRecord]:
    """
    Sum all matched amounts and check if the cumulative drift exceeds tolerance.
    """
    if not matched_pairs:
        return None

    platform_sum = sum(p.amount for p, _ in matched_pairs)
    bank_sum = sum(b.amount for _, b in matched_pairs)
    drift = bank_sum - platform_sum  # positive = bank overpaid

    if abs(drift) > AGGREGATE_TOLERANCE:
        return GapRecord(
            gap_type=GapType.ROUNDING_DRIFT,
            transaction_id="AGGREGATE",
            platform_amount=platform_sum,
            bank_amount=bank_sum,
            details=f"Cumulative drift of {drift} across {len(matched_pairs)} matched pairs (tolerance: ±{AGGREGATE_TOLERANCE})",
        )

    return None


# ─── Main Reconciliation Function ───────────────────────────────────────────

def reconcile(
    platform_txns: List[PlatformTransaction],
    bank_settlements: List[BankSettlement],
    year: int = 2026,
    month: int = 3,
) -> ReconciliationResult:
    """
    Run the full 5-pass reconciliation for the given month.

    Args:
        platform_txns:   All platform transactions (may span multiple months).
        bank_settlements: All bank settlements (may span multiple months).
        year:  Reconciliation year.
        month: Reconciliation month.

    Returns:
        ReconciliationResult with all detected gaps.
    """
    # Filter to reconciliation window
    month_platform = filter_platform_by_month(platform_txns, year, month)
    month_bank = filter_bank_by_month(bank_settlements, year, month)

    all_gaps: List[GapRecord] = []

    # ── Pass 3 first (detection only, doesn't modify data) ──
    duplicate_gaps = _pass_duplicates(month_bank, month_platform)
    all_gaps.extend(duplicate_gaps)

    # ── Pass 1: Exact match ──
    matched, unmatched_platform, unmatched_bank = _pass_exact_match(
        month_platform, month_bank
    )

    # ── Pass 2: Cross-month ──
    cross_month_gaps, unmatched_platform = _pass_cross_month(
        unmatched_platform, bank_settlements, year, month
    )
    all_gaps.extend(cross_month_gaps)

    # ── Pass 4: Orphan refunds ──
    platform_txn_ids = {t.transaction_id for t in platform_txns}
    orphan_gaps, unmatched_bank = _pass_orphan_refunds(
        unmatched_bank, platform_txn_ids
    )
    all_gaps.extend(orphan_gaps)

    # ── Pass 5: Rounding ──
    rounding_gap = _pass_rounding(matched)
    rounding_drift = Decimal("0")
    if rounding_gap:
        all_gaps.append(rounding_gap)
        rounding_drift = abs(
            (rounding_gap.bank_amount or Decimal("0"))
            - (rounding_gap.platform_amount or Decimal("0"))
        )

    # ── Remaining unmatched → MISSING / UNEXPECTED ──
    for txn in unmatched_platform:
        all_gaps.append(GapRecord(
            gap_type=GapType.MISSING_SETTLEMENT,
            transaction_id=txn.transaction_id,
            platform_amount=txn.amount,
            details=f"No bank settlement found for platform transaction dated {txn.timestamp.date()}",
        ))

    for s in unmatched_bank:
        all_gaps.append(GapRecord(
            gap_type=GapType.UNEXPECTED_SETTLEMENT,
            transaction_id=s.transaction_id,
            bank_amount=s.amount,
            details=f"Bank settlement with no matching platform transaction (settled {s.settlement_date})",
        ))

    # ── Build result ──
    platform_sum = sum(t.amount for t in month_platform)
    bank_sum = sum(s.amount for s in month_bank)

    month_str = f"{year}-{month:02d}"

    return ReconciliationResult(
        month=month_str,
        total_platform=len(month_platform),
        total_bank=len(month_bank),
        matched=len(matched),
        gaps=all_gaps,
        platform_sum=platform_sum,
        bank_sum=bank_sum,
        rounding_drift=rounding_drift,
    )
