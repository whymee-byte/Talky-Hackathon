"""T5b · exception fixes on allocations.parquet, with the item's running balance in booking order.

    python -m src.postfix --phase window|live|final

remaining = open at start − what earlier lines of the phase applied to the item (RETURNED / DUPLICATE excluded).
  fx_run      T2 payment-run row, item currency ≠ line currency, invoice, no exception → FX_DIFFERENCE   (POLICIES §3)
  partial_run T2 payment-run row on a supplier invoice that pays less than `remaining` → PARTIAL
  dup         T3/T5/T6 invoice row with nothing left to pay (remaining ≤ 0), no exception → DUPLICATE_PAYMENT
  last_part   PARTIAL row that pays the whole remaining balance → no exception (last instalment / remainder)
  over_big    OVERPAYMENT whose cash exceeds the item by > 10 % → no exception (the line pays more than this item)
Window (2024-07 → 2025-06): fx_run 2366 / 53 wrong · partial_run 5506 / 6 · dup 479 / 1 · last_part removes 7262 FP.
"""
from __future__ import annotations

import argparse
import os

import duckdb

from src.common import REPO

TOL = "greatest(1, 0.005 * abs(remaining))"


def build(phase: str) -> None:
    w = os.path.join(REPO, "work", phase)
    p = f"{w}/allocations.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE X AS
      SELECT a.bank_txn_id, a.item_id, i.kind, i.currency icur, l.currency lcur, l.booking_date, abs(l.amount_cents) cash,
             i.open_at_start_cents - coalesce(sum(a.amount_cents) FILTER (WHERE coalesce(a.exception, '') NOT IN ('RETURNED', 'DUPLICATE_PAYMENT'))
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) remaining
      FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      WHERE a.item_id IS NOT NULL""")
    con.execute(f"""CREATE TABLE FIX AS
      SELECT a.bank_txn_id, a.item_id,
        CASE WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur <> x.lcur AND x.kind IN ('API', 'ARI') THEN 'FX_DIFFERENCE'
             WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur = x.lcur AND x.kind = 'API'
                  AND a.amount_cents < x.remaining - {TOL} THEN 'PARTIAL'
             WHEN coalesce(a.exception, '') = '' AND x.kind IN ('ARI', 'API') AND x.remaining <= 0
                  AND (a.rule LIKE 'T3%' OR a.rule LIKE 'T5%' OR a.rule LIKE 'T6%') THEN 'DUPLICATE_PAYMENT'
             WHEN a.exception = 'PARTIAL' AND x.remaining > 0 AND a.amount_cents >= x.remaining - {TOL} THEN ''
             WHEN a.exception = 'OVERPAYMENT' AND x.icur = x.lcur AND x.cash > 1.10 * greatest(x.remaining, 1) THEN ''
        END AS new_exc,
        CASE WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur <> x.lcur THEN 'fx_run'
             WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' THEN 'partial_run'
             WHEN coalesce(a.exception, '') = '' THEN 'dup'
             WHEN a.exception = 'OVERPAYMENT' THEN 'over_big' ELSE 'last_part' END AS why
      FROM A a JOIN X x USING (bank_txn_id, item_id)""")
    con.execute("DELETE FROM FIX WHERE new_exc IS NULL")
    print(con.sql("SELECT why, new_exc, count(*) FROM FIX GROUP BY ALL ORDER BY 3 DESC").fetchall())
    con.execute(f"""COPY (SELECT a.* REPLACE (coalesce(f.new_exc, a.exception) AS exception,
                                         CASE WHEN f.why IS NULL THEN a.rule ELSE a.rule || '+T5b.' || f.why END AS rule)
                         FROM A a LEFT JOIN FIX f ON f.bank_txn_id = a.bank_txn_id AND f.item_id IS NOT DISTINCT FROM a.item_id)
                    TO '{p}' (FORMAT parquet)""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
