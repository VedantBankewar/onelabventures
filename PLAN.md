# Implementation Plan — Payment Reconciliation Engine

> **Time budget: 90 minutes**
> Phases are ordered by dependency. Estimated durations include buffer.

---

## Timeline Overview

| Phase | Task | Duration | Cumulative |
|-------|------|----------|------------|
| 1 | Project scaffolding | 5 min | 0:05 |
| 2 | Data models (`models.py`) | 10 min | 0:15 |
| 3 | Data generation (`data_gen.py`) | 20 min | 0:35 |
| 4 | Reconciliation engine (`reconciler.py`) | 25 min | 1:00 |
| 5 | Reporting (`reporter.py`) | 10 min | 1:10 |
| 6 | CLI entry point (`main.py`) | 5 min | 1:15 |
| 7 | Test suite (`test_recon.py`) | 10 min | 1:25 |
| 8 | End-to-end validation & polish | 5 min | 1:30 |

---

## Phase 1 — Project Scaffolding (5 min)

### Steps

1. Create directory structure:
   ```
   Assisment/
   ├── README.md          (already created)
   ├── PLAN.md            (this file)
   ├── requirements.txt
   ├── models.py
   ├── data_gen.py
   ├── reconciler.py
   ├── reporter.py
   ├── main.py
   ├── test_recon.py
   └── output/            (generated at runtime)
       ├── platform_transactions.csv
       ├── bank_settlements.csv
       └── reconciliation_report.json
   ```
2. Create `requirements.txt`:
   ```
   pytest>=7.0
   ```
   > No other dependencies. The project uses only Python standard library (`dataclasses`, `csv`, `json`, `datetime`, `decimal`, `uuid`, `random`, `argparse`, `enum`, `collections`).

### Deliverable
- Empty Python files with module docstrings, ready for implementation.

---

## Phase 2 — Data Models (`models.py`) (10 min)

### Steps

1. **Define enumerations**:
   ```python
   class TransactionType(Enum):
       PAYMENT = "payment"
       REFUND  = "refund"

   class TransactionStatus(Enum):
       COMPLETED = "completed"
       PENDING   = "pending"
       FAILED    = "failed"

   class SettlementStatus(Enum):
       SETTLED  = "settled"
       REJECTED = "rejected"

   class GapType(Enum):
       MATCHED              = "matched"
       CROSS_MONTH          = "cross_month"
       DUPLICATE_SETTLEMENT = "duplicate_settlement"
       DUPLICATE_TRANSACTION= "duplicate_transaction"
       ORPHAN_REFUND        = "orphan_refund"
       MISSING_SETTLEMENT   = "missing_settlement"
       UNEXPECTED_SETTLEMENT= "unexpected_settlement"
       ROUNDING_DRIFT       = "rounding_drift"
   ```

2. **Define data classes**:
   ```python
   @dataclass
   class PlatformTransaction:
       transaction_id: str
       timestamp: datetime
       amount: Decimal
       type: TransactionType
       status: TransactionStatus
       customer_id: str
       reference_id: str | None = None

   @dataclass
   class BankSettlement:
       settlement_id: str
       transaction_id: str
       settlement_date: date
       amount: Decimal
       status: SettlementStatus

   @dataclass
   class GapRecord:
       gap_type: GapType
       transaction_id: str
       platform_amount: Decimal | None
       bank_amount: Decimal | None
       details: str

   @dataclass
   class ReconciliationResult:
       month: str
       total_platform: int
       total_bank: int
       matched: int
       gaps: list[GapRecord]
       platform_sum: Decimal
       bank_sum: Decimal
       rounding_drift: Decimal
   ```

3. **Add helper methods**:
   - `to_dict()` on each data class for CSV/JSON serialization.
   - `from_dict()` class methods for deserialization.

### Deliverable
- Fully typed, documented data model module.

---

## Phase 3 — Data Generation (`data_gen.py`) (20 min)

### Design Principles
- All randomness is seeded (`random.seed(42)`) for reproducibility.
- Generate a **clean baseline** first, then **inject specific gaps** on top.

### Steps

#### 3.1 — Clean Baseline Generator

```
generate_clean_pairs(n=100, month=3, year=2026) → (List[PlatformTxn], List[BankSettlement])
```

- Generate `n` platform transactions with:
  - Timestamps spread across March 2026 (days 1–31, random hours).
  - Amounts between $10.00 and $500.00, rounded to 2 decimal places.
  - 90% PAYMENT, 10% REFUND (refunds reference a random earlier payment).
  - All statuses = COMPLETED.
- For each platform transaction, generate a matching bank settlement:
  - `settlement_date` = `transaction.timestamp.date() + timedelta(days=randint(1,2))`
  - `amount` = same as platform amount.
  - `status` = SETTLED.

#### 3.2 — Gap Injection Functions

Each function takes the clean pair lists and mutates them:

##### Gap 1: Cross-Month Settlement
```python
def inject_cross_month(platform_txns, bank_settlements):
    # Pick a transaction from March 30 or 31
    # Set its settlement_date to April 1 or 2
    # Result: platform txn exists in March, settlement is in April
```

##### Gap 2: Rounding Difference
```python
def inject_rounding_gaps(platform_txns, bank_settlements, count=15):
    # For ~15 matched pairs, add +0.01 or -0.01 to the bank amount
    # Individual differences are within tolerance (≤0.01)
    # But sum of all drifts will be ~0.08–0.15 (exceeds 0.05 threshold)
    # Strategy: make 12 of them +0.01 and 3 of them -0.01
    #           net drift = +0.09, which exceeds the 0.05 tolerance
```

##### Gap 3: Duplicate Bank Settlement
```python
def inject_duplicate(bank_settlements):
    # Pick a random settlement
    # Append a copy with a new settlement_id but same transaction_id & amount
    # Result: same transaction_id appears twice in bank data
```

##### Gap 4: Orphan Refund
```python
def inject_orphan_refund(bank_settlements):
    # Create a new bank settlement entry:
    #   - Negative amount (refund)
    #   - transaction_id that does NOT exist in platform data
    # Result: bank shows a refund with no platform record
```

#### 3.3 — Master Generator
```python
def generate_all(n=100, seed=42):
    random.seed(seed)
    platform, bank = generate_clean_pairs(n)
    inject_cross_month(platform, bank)
    inject_rounding_gaps(platform, bank)
    inject_duplicate(bank)
    inject_orphan_refund(bank)
    return platform, bank
```

#### 3.4 — CSV Export
```python
def export_csv(platform, bank, output_dir="output/"):
    # Write platform_transactions.csv
    # Write bank_settlements.csv
```

### Deliverable
- Deterministic data generator that produces ~102–105 records with all 4 gap types baked in.

---

## Phase 4 — Reconciliation Engine (`reconciler.py`) (25 min)

This is the core module. It implements a **5-pass sequential reconciliation**.

### Steps

#### 4.1 — Helper: Filter by Month
```python
def filter_platform_by_month(txns, year, month) -> list:
    return [t for t in txns if t.timestamp.year == year and t.timestamp.month == month]

def filter_bank_by_month(settlements, year, month) -> list:
    return [s for s in settlements if s.settlement_date.year == year and s.settlement_date.month == month]
```

#### 4.2 — Pass 1: Exact Match
```
Input:  platform_txns (filtered to month), bank_settlements (filtered to month)
Output: matched_pairs, unmatched_platform, unmatched_bank

Algorithm:
  - Build dict: bank_by_txn_id = {s.transaction_id: s for s in bank_settlements}
  - For each platform txn:
      if txn.transaction_id in bank_by_txn_id:
          bank = bank_by_txn_id[txn.transaction_id]
          if abs(txn.amount - bank.amount) <= 0.01:
              add to matched_pairs
          else:
              add to amount_mismatch list
      else:
          add to unmatched_platform
  - Remaining bank entries → unmatched_bank
```

#### 4.3 — Pass 2: Cross-Month Detection
```
Input:  unmatched_platform, ALL bank_settlements (unfiltered, next month)
Output: cross_month_gaps

Algorithm:
  - Filter unmatched_platform to those from last 2 days of month
  - For each, search bank_settlements where:
      settlement_date is in month+1
      AND transaction_id matches
  - If found → GapRecord(CROSS_MONTH, ...)
```

#### 4.4 — Pass 3: Duplicate Detection
```
Input:  ALL bank_settlements (for the month)
Output: duplicate_gaps

Algorithm:
  - Counter on transaction_id
  - Any count > 1 → GapRecord(DUPLICATE_SETTLEMENT, ...)
  - Same check on platform side for DUPLICATE_TRANSACTION
```

#### 4.5 — Pass 4: Orphan Refund Detection
```
Input:  unmatched_bank (negative amounts)
Output: orphan_refund_gaps

Algorithm:
  - For each unmatched bank settlement with amount < 0:
      If transaction_id not in platform_txn_ids → ORPHAN_REFUND
```

#### 4.6 — Pass 5: Aggregate Rounding Check
```
Input:  matched_pairs
Output: rounding_gap (if any)

Algorithm:
  - platform_sum = sum(pair.platform.amount for pair in matched_pairs)
  - bank_sum     = sum(pair.bank.amount for pair in matched_pairs)
  - drift = abs(platform_sum - bank_sum)
  - If drift > 0.05 → GapRecord(ROUNDING_DRIFT, drift details)
```

#### 4.7 — Assembly
```python
def reconcile(platform_txns, bank_settlements, year, month) -> ReconciliationResult:
    # Filter data
    # Run passes 1–5 in order
    # Collect all GapRecords
    # Build and return ReconciliationResult
```

### Deliverable
- Fully tested reconciliation engine with clear pass separation.

---

## Phase 5 — Reporting (`reporter.py`) (10 min)

### Steps

1. **Console summary** — formatted table with counts and totals (see README for example).
2. **Detail listing** — one line per gap, sorted by gap type.
3. **JSON export**:
   ```python
   def export_json(result: ReconciliationResult, path: str):
       # Serialize ReconciliationResult to JSON
       # Include metadata: run timestamp, seed, month, etc.
   ```

### Deliverable
- Human-readable console output.
- Machine-readable JSON report.

---

## Phase 6 — CLI Entry Point (`main.py`) (5 min)

### Steps

1. Use `argparse` for options:
   ```
   python main.py --month 3 --year 2026 --count 100 --seed 42 --output output/
   ```
2. Wire together:
   ```python
   def main():
       args = parse_args()
       platform, bank = generate_all(args.count, args.seed)
       export_csv(platform, bank, args.output)
       result = reconcile(platform, bank, args.year, args.month)
       print_report(result)
       export_json(result, args.output)
   ```

### Deliverable
- One-command demo that generates data, reconciles, and reports.

---

## Phase 7 — Test Suite (`test_recon.py`) (10 min)

### Test Cases

| # | Test Name | What It Verifies |
|---|-----------|-----------------|
| 1 | `test_exact_match_clean_data` | 10 clean pairs → 10 matches, 0 gaps |
| 2 | `test_cross_month_detected` | Transaction on day 31, settlement on day 1 of next month → CROSS_MONTH gap |
| 3 | `test_rounding_drift_detected` | 12× +0.01 and 3× −0.01 drift → net +0.09 → ROUNDING_DRIFT flag |
| 4 | `test_rounding_within_tolerance` | 2× +0.01 drift → net +0.02 → NO rounding flag |
| 5 | `test_duplicate_settlement` | Same transaction_id twice in bank → DUPLICATE_SETTLEMENT |
| 6 | `test_orphan_refund` | Negative bank entry with unknown transaction_id → ORPHAN_REFUND |
| 7 | `test_missing_settlement` | Platform txn with no bank entry → MISSING_SETTLEMENT |
| 8 | `test_unexpected_settlement` | Bank entry with no platform txn (positive amount) → UNEXPECTED_SETTLEMENT |
| 9 | `test_full_integration` | Run `generate_all()` + `reconcile()` → exactly 4 gap types detected |

### Test Helpers

```python
def make_platform_txn(**overrides) -> PlatformTransaction:
    """Factory with sensible defaults, override any field."""

def make_bank_settlement(**overrides) -> BankSettlement:
    """Factory with sensible defaults, override any field."""
```

### Deliverable
- All tests pass with `pytest test_recon.py -v`.

---

## Phase 8 — End-to-End Validation (5 min)

### Steps

1. Run `python main.py` with default arguments.
2. Verify:
   - `output/platform_transactions.csv` has ~103 rows.
   - `output/bank_settlements.csv` has ~102 rows.
   - Console report shows all 4 gap types.
   - `output/reconciliation_report.json` is valid JSON.
3. Run `pytest test_recon.py -v` — all 9 tests green.
4. Quick code review for docstrings and type hints.

### Deliverable
- Working demo and all tests passing.

---

## Demo / Deployment Strategy

### Local Demo (Primary)
```bash
# Install
pip install -r requirements.txt

# Run
python main.py

# Test
pytest test_recon.py -v
```

### Quick Presentation Points
1. Show the generated CSVs — "here's what the raw data looks like."
2. Run `main.py` — "here's the reconciliation in action."
3. Open the JSON report — "here's what a downstream system would consume."
4. Run `pytest` — "here are the edge cases we handle."

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Running over 90 min | Reporting (Phase 5) can be simplified to `print()` statements; JSON export is optional |
| Edge case in date handling | Use `calendar.monthrange()` for last-day-of-month calculation |
| Decimal precision issues | Use `decimal.Decimal` throughout, never `float` |
| Test flakiness | Fixed seed ensures deterministic data; no network calls |

---

## Definition of Done

- [ ] `python main.py` runs end-to-end without errors
- [ ] Console output clearly identifies all 4 gap types
- [ ] `pytest test_recon.py -v` — 9/9 tests pass
- [ ] CSV and JSON files are generated in `output/`
- [ ] Code has docstrings and type hints on all public functions
- [ ] README.md accurately describes the system
