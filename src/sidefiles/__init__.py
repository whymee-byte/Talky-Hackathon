"""T2 · detail files → work/<phase>/side_links.parquet (contracts/schemas.md §3).

Reads only Parquet from T1 (lines.parquet, items.parquet) plus the official detail files of the phase
(direct_debit_batches/, payment_runs/, card_settlements/, cheque_deposits/) and masters. All joins in duckdb.
"""
