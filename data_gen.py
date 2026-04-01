"""
data_gen.py — Synthetic data generator for the Payment Reconciliation Engine.

Generates a clean baseline of matched platform transactions and bank settlements,
then injects the four required gap scenarios:
  1. Cross-month settlement
  2. Rounding drift (cumulative)
  3. Duplicate bank settlement
  4. Orphan refund
"""

from __future__ import annotations

import csv
import os
import random
import uuid
from calendar import monthrange
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Tuple, List

from models import (
    PlatformTransaction,
    BankSettlement,
    TransactionType,
    TransactionStatus,
    SettlementStatus,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _new_id() -> str:
    """Generate a short, readable UUID-based ID."""
    return f"TXN-{uuid.uuid4().hex[:8].upper()}"


def _new_settlement_id() -> str:
    return f"STL-{uuid.uuid4().hex[:8].upper()}"


def _random_amount(low: float = 10.0, high: float = 500.0) -> Decimal:
    """Return a random monetary amount between low and high, rounded to 2dp."""
    val = random.uniform(low, high)
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _random_datetime(year: int, month: int, day_start: int = 1, day_end: int | None = None) -> datetime:
    """Return a random datetime within the given month and day range."""
    if day_end is None:
        day_end = monthrange(year, month)[1]
    day = random.randint(day_start, day_end)
    hour = random.randint(0, 23)
    minute = random.randint(0, 59)
    second = random.randint(0, 59)
    return datetime(year, month, day, hour, minute, second)


def _settlement_date_for(txn_date: datetime, max_lag: int = 2) -> date:
    """Bank settles 1–2 days after the transaction."""
    lag = random.randint(1, max_lag)
    return (txn_date + timedelta(days=lag)).date()


# ─── Clean Baseline Generator ───────────────────────────────────────────────

def generate_clean_pairs(
    n: int = 100,
    year: int = 2026,
    month: int = 3,
) -> Tuple[List[PlatformTransaction], List[BankSettlement]]:
    """
    Generate n matched (platform_transaction, bank_settlement) pairs.

    - 90% payments, 10% refunds (refunds reference a prior payment).
    - All transactions are COMPLETED / SETTLED.
    - Bank amount exactly equals platform amount (no drift).
    - Settlement dates are 1–2 days after the transaction timestamp.
    """
    platform_txns: List[PlatformTransaction] = []
    bank_settlements: List[BankSettlement] = []

    # Determine last safe day so settlements don't spill into next month
    last_day = monthrange(year, month)[1]
    safe_last_day = last_day - 2  # leave room for settlement lag

    payment_ids: List[str] = []

    for i in range(n):
        txn_id = _new_id()
        timestamp = _random_datetime(year, month, day_start=1, day_end=safe_last_day)
        amount = _random_amount()

        # 90% payments, 10% refunds
        if i < int(n * 0.9) or not payment_ids:
            txn_type = TransactionType.PAYMENT
            reference_id = None
            payment_ids.append(txn_id)
        else:
            txn_type = TransactionType.REFUND
            reference_id = random.choice(payment_ids)
            amount = -amount  # refunds are negative

        customer_id = f"CUST-{random.randint(1000, 9999)}"

        platform_txns.append(PlatformTransaction(
            transaction_id=txn_id,
            timestamp=timestamp,
            amount=amount,
            type=txn_type,
            status=TransactionStatus.COMPLETED,
            customer_id=customer_id,
            reference_id=reference_id,
        ))

        bank_settlements.append(BankSettlement(
            settlement_id=_new_settlement_id(),
            transaction_id=txn_id,
            settlement_date=_settlement_date_for(timestamp),
            amount=amount,  # exact match — no drift
            status=SettlementStatus.SETTLED,
        ))

    return platform_txns, bank_settlements


# ─── Gap Injection Functions ─────────────────────────────────────────────────

def inject_cross_month(
    platform_txns: List[PlatformTransaction],
    bank_settlements: List[BankSettlement],
    year: int = 2026,
    month: int = 3,
) -> str:
    """
    Gap 1: Move one transaction to the last day of the month and push its
    settlement into the first day of the next month.

    Returns the transaction_id of the affected record.
    """
    last_day = monthrange(year, month)[1]

    # Pick a payment (not a refund) to modify
    candidates = [
        (i, t) for i, t in enumerate(platform_txns)
        if t.type == TransactionType.PAYMENT
    ]
    idx, txn = random.choice(candidates)

    # Move transaction to last day of month
    new_ts = datetime(year, month, last_day, 23, 45, 0)
    platform_txns[idx] = PlatformTransaction(
        transaction_id=txn.transaction_id,
        timestamp=new_ts,
        amount=txn.amount,
        type=txn.type,
        status=txn.status,
        customer_id=txn.customer_id,
        reference_id=txn.reference_id,
    )

    # Push corresponding settlement to next month
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    for j, s in enumerate(bank_settlements):
        if s.transaction_id == txn.transaction_id:
            bank_settlements[j] = BankSettlement(
                settlement_id=s.settlement_id,
                transaction_id=s.transaction_id,
                settlement_date=date(next_year, next_month, random.randint(1, 2)),
                amount=s.amount,
                status=s.status,
            )
            break

    return txn.transaction_id


def inject_rounding_gaps(
    bank_settlements: List[BankSettlement],
    count: int = 15,
) -> Decimal:
    """
    Gap 2: Introduce tiny ±0.01 differences in bank amounts.

    Strategy: 12 × +0.01 and 3 × -0.01 → net drift = +0.09.
    Individual differences are within the ±0.01 tolerance, but the
    aggregate exceeds the 0.05 threshold.

    Returns the expected net drift.
    """
    # Pick `count` distinct settlements to modify
    indices = random.sample(range(len(bank_settlements)), min(count, len(bank_settlements)))

    positive_count = 12
    net_drift = Decimal("0")

    for i, idx in enumerate(indices):
        s = bank_settlements[idx]
        if i < positive_count:
            drift = Decimal("0.01")
        else:
            drift = Decimal("-0.01")

        net_drift += drift
        bank_settlements[idx] = BankSettlement(
            settlement_id=s.settlement_id,
            transaction_id=s.transaction_id,
            settlement_date=s.settlement_date,
            amount=s.amount + drift,
            status=s.status,
        )

    return net_drift


def inject_duplicate(
    bank_settlements: List[BankSettlement],
) -> str:
    """
    Gap 3: Duplicate one bank settlement (same transaction_id, new settlement_id).

    Returns the transaction_id of the duplicated record.
    """
    original = random.choice(bank_settlements)
    duplicate = BankSettlement(
        settlement_id=_new_settlement_id(),
        transaction_id=original.transaction_id,
        settlement_date=original.settlement_date,
        amount=original.amount,
        status=original.status,
    )
    bank_settlements.append(duplicate)
    return original.transaction_id


def inject_orphan_refund(
    bank_settlements: List[BankSettlement],
    existing_txn_ids: set[str],
) -> str:
    """
    Gap 4: Add a bank settlement for a refund that has no matching platform transaction.

    Returns the orphan transaction_id.
    """
    orphan_txn_id = _new_id()
    # Ensure it doesn't collide with existing IDs
    while orphan_txn_id in existing_txn_ids:
        orphan_txn_id = _new_id()

    orphan = BankSettlement(
        settlement_id=_new_settlement_id(),
        transaction_id=orphan_txn_id,
        settlement_date=date(2026, 3, 15),
        amount=Decimal("-30.00"),  # negative = refund
        status=SettlementStatus.SETTLED,
    )
    bank_settlements.append(orphan)
    return orphan_txn_id


# ─── Master Generator ───────────────────────────────────────────────────────

def generate_all(
    n: int = 100,
    seed: int = 42,
    year: int = 2026,
    month: int = 3,
) -> Tuple[List[PlatformTransaction], List[BankSettlement], dict]:
    """
    Generate a complete test dataset with all four gap scenarios injected.

    Returns:
        (platform_txns, bank_settlements, injection_metadata)

    injection_metadata contains the IDs / values for verification.
    """
    random.seed(seed)

    platform_txns, bank_settlements = generate_clean_pairs(n, year, month)

    # Inject gaps
    cross_month_id = inject_cross_month(platform_txns, bank_settlements, year, month)
    rounding_drift = inject_rounding_gaps(bank_settlements, count=15)
    duplicate_id = inject_duplicate(bank_settlements)

    existing_ids = {t.transaction_id for t in platform_txns}
    orphan_id = inject_orphan_refund(bank_settlements, existing_ids)

    metadata = {
        "cross_month_txn_id": cross_month_id,
        "expected_rounding_drift": str(rounding_drift),
        "duplicate_txn_id": duplicate_id,
        "orphan_refund_txn_id": orphan_id,
        "total_platform": len(platform_txns),
        "total_bank": len(bank_settlements),
    }

    return platform_txns, bank_settlements, metadata


# ─── CSV Export ──────────────────────────────────────────────────────────────

def export_csv(
    platform_txns: List[PlatformTransaction],
    bank_settlements: List[BankSettlement],
    output_dir: str = "output",
) -> Tuple[str, str]:
    """
    Write both datasets to CSV files in output_dir.

    Returns the paths of the two files created.
    """
    os.makedirs(output_dir, exist_ok=True)

    platform_path = os.path.join(output_dir, "platform_transactions.csv")
    bank_path = os.path.join(output_dir, "bank_settlements.csv")

    # Platform transactions
    if platform_txns:
        fieldnames = list(platform_txns[0].to_dict().keys())
        with open(platform_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for txn in platform_txns:
                writer.writerow(txn.to_dict())

    # Bank settlements
    if bank_settlements:
        fieldnames = list(bank_settlements[0].to_dict().keys())
        with open(bank_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in bank_settlements:
                writer.writerow(s.to_dict())

    return platform_path, bank_path
