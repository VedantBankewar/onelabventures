"""
main.py — CLI entry point for the Payment Reconciliation Engine.

Usage:
    python main.py
    python main.py --month 3 --year 2026 --count 100 --seed 42 --output output
"""

from __future__ import annotations

import argparse
import json
import sys

from data_gen import generate_all, export_csv
from reconciler import reconcile
from reporter import print_report, export_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Payment Reconciliation Engine — generates test data, "
                    "detects gaps, and produces a reconciliation report.",
    )
    parser.add_argument(
        "--month", type=int, default=3,
        help="Reconciliation month (1–12). Default: 3 (March).",
    )
    parser.add_argument(
        "--year", type=int, default=2026,
        help="Reconciliation year. Default: 2026.",
    )
    parser.add_argument(
        "--count", type=int, default=100,
        help="Number of baseline transactions to generate. Default: 100.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible data generation. Default: 42.",
    )
    parser.add_argument(
        "--output", type=str, default="output",
        help="Output directory for CSVs and JSON report. Default: 'output'.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the full reconciliation pipeline."""
    args = parse_args(argv)

    print(f"\n[GEN]  Generating {args.count} transactions (seed={args.seed})...")
    platform, bank, metadata = generate_all(
        n=args.count, seed=args.seed, year=args.year, month=args.month
    )

    print(f"[CSV]  Exporting CSVs to {args.output}/...")
    platform_path, bank_path = export_csv(platform, bank, args.output)
    print(f"     > {platform_path}  ({len(platform)} records)")
    print(f"     > {bank_path}  ({len(bank)} records)")

    # Save injection metadata for reference
    meta_path = f"{args.output}/injection_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"     > {meta_path}")

    print(f"\n[RUN]  Running reconciliation for {args.year}-{args.month:02d}...")
    result = reconcile(platform, bank, args.year, args.month)

    print_report(result)

    json_path = export_json(result, args.output)
    print(f"[OUT]  JSON report saved to {json_path}")
    print()


if __name__ == "__main__":
    main()
