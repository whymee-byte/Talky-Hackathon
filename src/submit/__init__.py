"""T0 · submission writer.

Two modes (see __main__):
- from `work/<phase>/allocations.parquet` (§8 of contracts/schemas.md) when it exists → serialises it;
- trivial fallback (no upstream modules yet): exact pairs of the starter (our document number in the bank text and
  amount == item total) + every other line as a no-item row: receipts → UNIDENTIFIED/UNIDENTIFIED, debits → BANK_FEES.
Every bank line of the phase appears exactly once (omitted lines count as wrong).
"""
