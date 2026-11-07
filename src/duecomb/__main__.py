"""T23 · receipts still without item: unique subset of the payer's RECENTLY DUE open invoices that sums to the cash.

    python -m src.duecomb --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.lastexact (before src.latedisc); rewrites only IN lines with no item. Multi-invoice receipts without any
reference ("PAYMENT", "ACCOUNT PAYMENT") pay 4-16 invoices of payers that carry 40-90 open items, so the general
subset-sum (src.match comb/mitm) finds many solutions and abstains. Learned on the TRAIN window: the invoices such
receipts pay are due 5-77 days before the receipt (median 26), while the payer's other open invoices are stale (median
154 days overdue, 90th percentile 1 360). Restricting the candidates to invoices due in [booking - DUE_LO, booking +
DUE_HI] makes the solution unique for ~150 receipts of the window with pair precision 0.71-1.00 (sets of <= 5 items or
<= 18 candidates: >= 0.89). No ids, amounts or texts: T4 rank-1 party, due dates and exact cents.

  T23.due_window_comb  IN line with no item, T4 rank-1 party with confidence >= PARTY_MIN, candidates = open unused ARI
                       of that party (same entity and currency, issued on or before the line, due within the window),
                       2..MAX_CAND of them, exactly one subset summing to the cash -> full clear, no exception.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import pandas as pd

from src.common import REPO
from src.match import combo

PARTY_MIN = 0.5
DUE_LO, DUE_HI = 90, 10   # days before / after the booking date (TRAIN window: 90 % of paid invoices within 5..77 overdue)
MAX_CAND = 24


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    if not os.path.exists(lp):
        print(f"[{phase}] duecomb: no line_party, skipped")
        return
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE C AS
      SELECT l.bank_txn_id, abs(l.amount_cents) cash, p.party_id, p.confidence, i.item_id, i.open_at_start_cents v
      FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
      JOIN '{w}/items.parquet' i
        ON i.party_id = p.party_id AND i.currency = l.currency AND i.entity = l.entity AND i.kind = 'ARI'
       AND i.open_at_start_cents > 0 AND i.issue_date <= l.booking_date
       AND i.due_date BETWEEN l.booking_date - INTERVAL {DUE_LO} DAY AND l.booking_date + INTERVAL {DUE_HI} DAY
      WHERE l.direction = 'IN' AND p.rank = 1 AND p.confidence >= {PARTY_MIN}
        AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)
        AND i.item_id NOT IN (SELECT item_id FROM USED)""")
    rows = con.execute(f"""SELECT bank_txn_id, any_value(cash), any_value(party_id), any_value(confidence), list(item_id), list(v)
                           FROM C GROUP BY 1 HAVING count(*) BETWEEN 2 AND {MAX_CAND}""").fetchall()
    picks, taken = [], set()
    for t, cash, party, conf, ids, vals in rows:
        k, sol = combo.solve(list(vals), int(cash))
        if k != 1 or not sol:
            continue
        items = [ids[j] for j in sol]
        if any(i in taken for i in items):
            continue
        taken.update(items)
        for j in sol:
            picks.append((t, ids[j], int(vals[j]), round(float(conf) * (0.85 if len(sol) <= 5 else 0.65), 3),
                          f"unique subset of {len(ids)} open invoices of party {party} due within -{DUE_LO}/+{DUE_HI} days; {len(sol)} items = cash"))
    n = len({t for t, *_ in picks})
    if picks:
        pk = pd.DataFrame(picks, columns=["bank_txn_id", "item_id", "amount_cents", "confidence", "evidence"])  # noqa: F841
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM pk)")
        con.execute("""INSERT INTO A BY NAME
          SELECT bank_txn_id::VARCHAR bank_txn_id, item_id::VARCHAR item_id, amount_cents::BIGINT amount_cents,
                 NULL::VARCHAR category, NULL::VARCHAR exception, 'T23.due_window_comb' rule, confidence::DOUBLE confidence,
                 evidence::VARCHAR evidence FROM pk""")
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] duecomb: {n} receipts, {len(picks)} pairs (unique subset of recently due invoices)  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
