"""T13d · an invoice paid in two receipts that both ended without item: partial payment + remainder.

    python -m src.twopay --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.discombo and before src.unident; rewrites only no-item IN lines. Unplanned partial payments ("A CUENTA",
POLICIES §3 PARTIAL) are followed by the remainder; neither line needs to quote the invoice. No ids, amounts or texts:
only the T4 candidate payers and exact cents.

  T13.two_pay  two no-item IN lines L1 (earlier or same day) and L2 (at most DAYS later) sharing a T4 candidate payer
               (rank <= RANK_MAX, confidence >= PARTY_MIN), entity and currency, and one open unused invoice of that payer
               issued on or before L1 with open balance = cash(L1) + cash(L2) exactly. Unique for each line and for the
               invoice; a line cannot be first in one pair and second in another.
               L1 -> the invoice, PARTIAL, amount = cash(L1); L2 -> the invoice, no exception, amount = cash(L2).
               Window (TRAIN 2024-07 -> 2025-06): Q1 114 pairs, Q2-4 300 pairs, all correct (item and exception).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

DAYS = 180
RANK_MAX, PARTY_MIN = 3, 0.2


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    if not os.path.exists(lp):
        print(f"[{phase}] twopay: no line_party, skipped")
        return
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE N AS
      SELECT DISTINCT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.entity, l.booking_date bd, p.party_id
      FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
      WHERE l.direction = 'IN' AND p.rank <= {RANK_MAX} AND p.confidence >= {PARTY_MIN}
        AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    con.execute(f"""CREATE TABLE M AS
      SELECT n1.bank_txn_id t1, n2.bank_txn_id t2, i.item_id, n1.cash c1, n2.cash c2, i.party_id
      FROM N n1 JOIN N n2 ON n1.party_id = n2.party_id AND n1.currency = n2.currency AND n1.entity = n2.entity
           AND n2.bank_txn_id <> n1.bank_txn_id AND (n2.bd > n1.bd OR (n2.bd = n1.bd AND n2.bank_txn_id > n1.bank_txn_id))
           AND n2.bd <= n1.bd + {DAYS}
      JOIN '{w}/items.parquet' i ON i.party_id = n1.party_id AND i.currency = n1.currency AND i.entity = n1.entity
           AND i.kind = 'ARI' AND i.open_at_start_cents = n1.cash + n2.cash AND i.issue_date <= n1.bd
      WHERE i.item_id NOT IN (SELECT item_id FROM USED)
      QUALIFY count(*) OVER (PARTITION BY n1.bank_txn_id) = 1 AND count(*) OVER (PARTITION BY n2.bank_txn_id) = 1
          AND count(*) OVER (PARTITION BY i.item_id) = 1""")
    con.execute("DELETE FROM M WHERE t1 IN (SELECT t2 FROM M) OR t2 IN (SELECT t1 FROM M)")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND (bank_txn_id IN (SELECT t1 FROM M) OR bank_txn_id IN (SELECT t2 FROM M))")
    con.execute("""INSERT INTO A BY NAME
      SELECT t1 bank_txn_id, item_id, c1 amount_cents, NULL::VARCHAR category, 'PARTIAL' exception, 'T13.two_pay' rule,
             0.8 confidence, 'first of two receipts of payer ' || party_id || ' that together pay the open balance' evidence FROM M
      UNION ALL
      SELECT t2, item_id, c2, NULL::VARCHAR, NULL::VARCHAR, 'T13.two_pay', 0.8,
             'second of two receipts of payer ' || party_id || ' that together pay the open balance' FROM M""")
    n = con.execute("SELECT count(*) FROM M").fetchall()[0][0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] twopay: {n} invoices paid by two receipts  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
