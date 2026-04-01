"""
reporter.py — Output formatting for the Payment Reconciliation Engine.

Provides console summary, detail listing, and JSON export.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

from models import GapType, ReconciliationResult


# ─── Console Formatting ─────────────────────────────────────────────────────

_GAP_ICONS = {
    GapType.CROSS_MONTH: "[WARN]",
    GapType.DUPLICATE_SETTLEMENT: "[FAIL]",
    GapType.DUPLICATE_TRANSACTION: "[FAIL]",
    GapType.ORPHAN_REFUND: "[FAIL]",
    GapType.MISSING_SETTLEMENT: "[FAIL]",
    GapType.UNEXPECTED_SETTLEMENT: "[FAIL]",
    GapType.ROUNDING_DRIFT: "[WARN]",
}

_GAP_LABELS = {
    GapType.CROSS_MONTH: "Cross-month settlements",
    GapType.DUPLICATE_SETTLEMENT: "Duplicate settlements",
    GapType.DUPLICATE_TRANSACTION: "Duplicate transactions",
    GapType.ORPHAN_REFUND: "Orphan refunds",
    GapType.MISSING_SETTLEMENT: "Missing settlements",
    GapType.UNEXPECTED_SETTLEMENT: "Unexpected settlements",
    GapType.ROUNDING_DRIFT: "Rounding drift",
}


def _count_by_type(result: ReconciliationResult) -> dict[GapType, int]:
    """Count gaps grouped by type."""
    counts: dict[GapType, int] = {}
    for g in result.gaps:
        counts[g.gap_type] = counts.get(g.gap_type, 0) + 1
    return counts


def _format_amount(amount: Optional[Decimal]) -> str:
    """Format a Decimal as a dollar string."""
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def print_summary(result: ReconciliationResult) -> None:
    """Print a formatted console summary of the reconciliation result."""
    counts = _count_by_type(result)
    w = 60  # width

    print()
    print("=" * w)
    print(f"  RECONCILIATION REPORT — {result.month}")
    print("=" * w)
    print()
    print(f"  Total platform transactions :  {result.total_platform}")
    print(f"  Total bank settlements      :  {result.total_bank}")
    print()
    print(f"  [OK]  Matched                  :  {result.matched}")

    # Print each gap type
    for gap_type in [
        GapType.CROSS_MONTH,
        GapType.DUPLICATE_SETTLEMENT,
        GapType.DUPLICATE_TRANSACTION,
        GapType.ORPHAN_REFUND,
        GapType.MISSING_SETTLEMENT,
        GapType.UNEXPECTED_SETTLEMENT,
    ]:
        count = counts.get(gap_type, 0)
        icon = _GAP_ICONS[gap_type]
        label = _GAP_LABELS[gap_type]
        print(f"  {icon}  {label:<28s}:  {count}")

    print()
    print(f"  Platform total  : {_format_amount(result.platform_sum)}")
    print(f"  Bank total      : {_format_amount(result.bank_sum)}")

    drift = result.rounding_drift
    drift_str = _format_amount(drift)
    flag = "  << EXCEEDS TOLERANCE" if drift > Decimal("0.05") else ""
    print(f"  Rounding drift  : {drift_str}{flag}")

    print()
    print("=" * w)


def print_details(result: ReconciliationResult) -> None:
    """Print a detailed listing of every detected gap."""
    if not result.gaps:
        print("  No gaps detected — perfect reconciliation! ✅")
        return

    w = 60
    print(f"  DETAILS")
    print("-" * w)

    for gap in result.gaps:
        tag = gap.gap_type.value.upper()
        amount = _format_amount(gap.bank_amount or gap.platform_amount)
        print(f"  [{tag}]  {gap.transaction_id}  {amount}")
        print(f"          {gap.details}")

    print("=" * w)
    print()


def print_report(result: ReconciliationResult) -> None:
    """Print the full report (summary + details)."""
    print_summary(result)
    print_details(result)


# ─── JSON Export ─────────────────────────────────────────────────────────────

def export_json(
    result: ReconciliationResult,
    output_dir: str = "output",
    filename: str = "reconciliation_report.json",
) -> str:
    """
    Write the reconciliation result to a JSON file.

    Returns the path to the created file.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)

    report = result.to_dict()
    report["generated_at"] = datetime.now().isoformat()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    return path
