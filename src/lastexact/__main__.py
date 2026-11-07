"""T22 · last chance for a line still without item: the one open, unused item of its T4 payer for exactly the cash.

    python -m src.lastexact --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs last (after src.excfix / tolmatch --overfix), rewrites only lines with no item. Earlier stages leave such lines
when the item they first took was dropped later (T15 dead/tiny drops), when the payer is a supplier refunding one of
its credit notes (POLICIES: a refund of a credit note settles the APC item, it is not SUPPLIER_REFUND) or when the
payer's queue was shifted by a missed receipt. No ids, amounts or texts: only the T4 rank-1 party and exact cents.

  T22.last_exact  line with no item, T4 rank-1 party with confidence >= PARTY_MIN, and exactly one open item of that
                  party (same entity and currency, issued on or before the line, kind compatible with the direction:
                  IN -> ARI/APC, OUT -> API/ARC) with open balance = cash, not used by any pair of the output; the item
                  must be the single candidate of a single line. -> full clear, no exception.
                  Window (TRAIN 2024-07 -> 2025-06): 313 lines, 310 pairs in golden (ARI 184/187, APC 126/126).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

PARTY_MIN = 0.5


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    if not os.path.exists(lp):
        print(f"[{phase}] lastexact: no line_party, skipped")
        return
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE N AS
      SELECT DISTINCT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.entity, l.booking_date bd, l.direction,
             p.party_id, p.confidence
      FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
      WHERE p.rank = 1 AND p.confidence >= {PARTY_MIN}
        AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    con.execute(f"""CREATE TABLE M AS
      SELECT n.bank_txn_id, i.item_id, n.cash, n.party_id, n.confidence, i.kind
      FROM N n JOIN '{w}/items.parquet' i
        ON i.party_id = n.party_id AND i.currency = n.currency AND i.entity = n.entity
       AND i.open_at_start_cents = n.cash AND i.issue_date <= n.bd
       AND i.kind IN (CASE n.direction WHEN 'IN' THEN 'ARI' ELSE 'API' END, CASE n.direction WHEN 'IN' THEN 'APC' ELSE 'ARC' END)
      WHERE i.item_id NOT IN (SELECT item_id FROM USED)
      QUALIFY count(*) OVER (PARTITION BY n.bank_txn_id) = 1 AND count(*) OVER (PARTITION BY i.item_id) = 1""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM M)")
    con.execute("""INSERT INTO A BY NAME
      SELECT bank_txn_id, item_id, cash amount_cents, NULL::VARCHAR category, NULL::VARCHAR exception,
             'T22.last_exact' rule, 0.9 confidence,
             'only open unused ' || kind || ' of T4 rank-1 party ' || party_id || ' (conf=' || round(confidence, 2)
               || ') with open balance = cash' evidence
      FROM M""")
    n = con.execute("SELECT count(*) FROM M").fetchall()[0][0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] lastexact: {n} lines paired with the single exact open item of their payer  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
