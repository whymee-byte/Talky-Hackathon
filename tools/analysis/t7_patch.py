"""Re-score allocations after swapping the no-item rows' category for T7's (no matcher rerun).
    python tools/analysis/t7_patch.py work/window/allocations_q1.parquet work/window/line_category_q1.parquet out.parquet
"""
import sys
import duckdb
a, c, out = sys.argv[1:4]
duckdb.sql(f"""COPY (
  SELECT a.bank_txn_id, a.item_id, a.amount_cents,
         CASE WHEN a.item_id IS NULL AND c.category IS NOT NULL AND c.category <> 'UNIDENTIFIED' THEN c.category ELSE a.category END category,
         CASE WHEN a.item_id IS NULL AND c.category IS NOT NULL AND c.category <> 'UNIDENTIFIED' THEN '' ELSE a.exception END exception,
         CASE WHEN a.item_id IS NULL AND c.category IS NOT NULL THEN c.rule ELSE a.rule END rule,
         CASE WHEN a.item_id IS NULL AND c.category IS NOT NULL THEN c.confidence ELSE a.confidence END confidence,
         CASE WHEN a.item_id IS NULL AND c.category IS NOT NULL THEN c.evidence ELSE a.evidence END evidence
  FROM '{a}' a LEFT JOIN '{c}' c USING (bank_txn_id)) TO '{out}' (FORMAT parquet)""")
print("patched →", out)
