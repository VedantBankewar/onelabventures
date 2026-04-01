# Payment Reconciliation Engine

## Problem Statement

A payments platform records customer transactions **instantly** at the point of sale. The acquiring bank settles funds **1–2 business days later**. At the end of every calendar month the finance team must verify that **every platform transaction has a corresponding bank settlement** and that the amounts agree.

In practice, four categories of mismatch ("gap") routinely appear:

| # | Gap Type | Root Cause |
|---|----------|-----------|
| 1 | **Cross-month settlement** | A transaction captured on 30/31 Jan settles on 1/2 Feb — it appears "missing" in the January reconciliation window. |
| 2 | **Rounding difference** | Individual amounts match to ≤ ±0.01, but when hundreds of rows are summed the cumulative error becomes material. |
| 3 | **Duplicate settlement** | The bank accidentally processes the same settlement twice (or the platform double-records a transaction). |
| 4 | **Orphan refund** | A refund appears in the bank ledger but there is no matching original payment on the platform side. |

This prototype generates synthetic data that **guarantees all four gap types are present**, then runs a multi-pass reconciliation engine to detect and report every mismatch.

---

## Assumptions

### Business Rules

1. **Reconciliation window** — One calendar month (e.g. 2026-03-01 to 2026-03-31).
2. **Settlement lag** — Bank settlements arrive 1–2 calendar days after the platform records the transaction. Transactions from the last 1–2 days of the month may therefore settle in the following month.
3. **Matching key** — A platform transaction and a bank settlement are linked by a shared `transaction_id`. Each `transaction_id` should appear **exactly once** in each dataset for a clean match.
4. **Amount tolerance** — Individual amounts are considered matching if the absolute difference is **≤ 0.01** (one cent). Aggregate totals are compared separately with a **≤ 0.05** tolerance.
5. **Currency** — All amounts are in a single currency (USD). No FX conversion.
6. **Refunds** — Represented as negative amounts. A valid refund must reference a prior payment's `transaction_id` (via a `reference_id` field). An orphan refund has no such reference.
7. **Idempotency** — The reconciliation engine is stateless; re-running with the same inputs must produce identical output.

### Technical Constraints

- **Language**: Python 3.10+
- **No external database** — data lives in CSV files or in-memory dataclass lists.
- **No third-party reconciliation libraries** — all logic is hand-written for auditability.
- **Deterministic data generation** — a fixed random seed ensures reproducibility across runs.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      main.py (CLI)                       │
│  • Parses arguments (month, seed, record count)          │
│  • Orchestrates pipeline                                 │
└────────────┬────────────────────────┬────────────────────┘
             │                        │
             ▼                        ▼
┌────────────────────┐   ┌────────────────────────────────┐
│   data_gen.py      │   │        reconciler.py           │
│                    │   │                                │
│  • Generates base  │   │  Pass 1: Exact match           │
│    clean dataset   │──▶│  Pass 2: Cross-month detect    │
│  • Injects 4 gap   │   │  Pass 3: Duplicate detect      │
│    scenarios       │   │  Pass 4: Orphan refund detect   │
│  • Exports CSVs    │   │  Pass 5: Aggregate rounding     │
│                    │   │                                │
└────────────────────┘   └───────────────┬────────────────┘
                                         │
┌────────────────────┐                   │
│    models.py       │                   ▼
│                    │   ┌────────────────────────────────┐
│  PlatformTxn       │   │        reporter.py             │
│  BankSettlement    │   │                                │
│  Enums / Result    │   │  • Console summary table       │
│                    │   │  • Detailed gap listing        │
└────────────────────┘   │  • JSON export                 │
                         └────────────────────────────────┘

         ┌──────────────────────────────────┐
         │        test_recon.py             │
         │  pytest suite — 9+ test cases    │
         └──────────────────────────────────┘
```

### File Inventory

| File | Responsibility |
|------|---------------|
| `models.py` | Data classes (`PlatformTransaction`, `BankSettlement`, `ReconciliationResult`) and enumerations |
| `data_gen.py` | Synthetic data generator with controlled gap injection |
| `reconciler.py` | 5-pass reconciliation engine |
| `reporter.py` | Console + JSON output formatting |
| `main.py` | CLI entry point, wires everything together |
| `test_recon.py` | pytest-based test suite (unit + integration) |

---

## Data Schemas

### `platform_transactions`

| Field | Type | Description |
|-------|------|-------------|
| `transaction_id` | `str` (UUID-4) | Unique identifier for each transaction |
| `timestamp` | `datetime` | When the platform recorded the transaction |
| `amount` | `Decimal` | Positive for payments, negative for refunds |
| `type` | `enum` | `PAYMENT` or `REFUND` |
| `status` | `enum` | `COMPLETED`, `PENDING`, `FAILED` |
| `customer_id` | `str` | Anonymised customer reference |
| `reference_id` | `str \| None` | For refunds: the `transaction_id` of the original payment |

### `bank_settlements`

| Field | Type | Description |
|-------|------|-------------|
| `settlement_id` | `str` (UUID-4) | Bank's own unique settlement identifier |
| `transaction_id` | `str` (UUID-4) | References the platform `transaction_id` |
| `settlement_date` | `date` | Date the bank settled the funds |
| `amount` | `Decimal` | Settled amount (should equal platform amount) |
| `status` | `enum` | `SETTLED`, `REJECTED` |

---

## Reconciliation Logic

The engine runs **five sequential passes** over the data:

### Pass 1 — Exact Match

```
For each platform transaction in the reconciliation month:
    Look for a bank settlement with the same transaction_id
    If found AND |platform.amount − bank.amount| ≤ 0.01:
        → MATCHED — remove from both unmatched pools
```

### Pass 2 — Cross-Month Detection

```
For each UNMATCHED platform transaction from the last 2 days of the month:
    Look for a bank settlement with the same transaction_id
        where settlement_date is in the NEXT month
    If found:
        → flag as CROSS_MONTH gap (expected, not an error)
```

### Pass 3 — Duplicate Detection

```
Group bank settlements by transaction_id
For any transaction_id with count > 1:
    → flag as DUPLICATE_SETTLEMENT
    Keep first occurrence as the valid match

Also group platform transactions by transaction_id
For any transaction_id with count > 1:
    → flag as DUPLICATE_TRANSACTION
```

### Pass 4 — Orphan Refund Detection

```
For each bank settlement with a negative amount:
    If its transaction_id has NO matching platform refund
    OR its reference_id points to a non-existent original payment:
        → flag as ORPHAN_REFUND
```

### Pass 5 — Aggregate Rounding Check

```
Sum all MATCHED platform amounts → P_total
Sum all MATCHED bank amounts     → B_total
If |P_total − B_total| > 0.05:
    → flag as ROUNDING_DRIFT with the delta
```

### After All Passes

Any remaining unmatched platform transactions → `MISSING_SETTLEMENT`
Any remaining unmatched bank settlements → `UNEXPECTED_SETTLEMENT`

---

## Expected Output

### Console Summary (example)

```
═══════════════════════════════════════════════════════
  RECONCILIATION REPORT — March 2026
═══════════════════════════════════════════════════════

  Total platform transactions :  103
  Total bank settlements      :  102

  ✅  Matched                  :   97
  ⚠️  Cross-month settlements  :    1
  🔴  Duplicate settlements    :    1
  🔴  Orphan refunds           :    1
  🔴  Missing settlements      :    1
  🔴  Unexpected settlements   :    0

  Platform total  : $12,345.67
  Bank total      : $12,345.80
  Rounding drift  :     $0.13  ⚠️  EXCEEDS TOLERANCE

═══════════════════════════════════════════════════════
  DETAILS
───────────────────────────────────────────────────────
  [CROSS_MONTH]  TXN-a1b2c3  $150.00  settles 2026-04-01
  [DUPLICATE]    TXN-d4e5f6  $75.00   appears 2x in bank
  [ORPHAN]       TXN-g7h8i9  -$30.00  no matching payment
  [ROUNDING]     Cumulative drift $0.13 across 97 matches
═══════════════════════════════════════════════════════
```

### JSON Export

A `reconciliation_report.json` file containing the full structured result for downstream consumption or dashboarding.

---

## Limitations

1. **Single-month window** — The engine reconciles one month at a time. Multi-month carry-forward logic (e.g., resolving last month's cross-month items) is out of scope.
2. **Single currency** — No FX handling; all amounts assumed USD.
3. **No partial settlements** — A transaction is either fully settled or not; split settlements are not modelled.
4. **Synthetic data only** — The prototype uses generated data. Production integration with real bank files (MT940, BAI2, CSV from payment processors) would require adapters.
5. **No persistence layer** — Results are ephemeral (CSV + JSON). A production system would write to a database with audit trail.
6. **Rounding detection is aggregate-only** — Individual ±0.01 differences are tolerated silently; only the sum is checked against a threshold.
7. **Sequential processing** — Designed for clarity, not throughput. Production volumes (millions of rows) would need batching/parallelism.
