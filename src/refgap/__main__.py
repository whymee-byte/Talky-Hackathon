"""T16 · receipts still without item → the unique subset of the payer's open, unused, due invoices that adds up to the cash.

    python -m src.refgap --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs at the very end of the AR chain (after src.mixfix, before src.unident/submit); rewrites only IN lines whose rows
all have no item. src.match.combo already tried the 16 most recent invoices of each T4 candidate when the matcher ran;
by now the other stages have consumed many of those invoices, so the pool of each payer is smaller and cleaner.

Rule T16.gap_combo.r<rank>: candidate items = open AR invoices (open_at_start_cents > 0) of one of T4's top RANK_MAX
parties (confidence >= PARTY_MIN), same entity and currency, issued on or before the booking date and at most AGE_MAX
days before, due no later than booking + DUE_HI days, not allocated to any line (RETURNED re-opens). Keep the CAP most
recent per (line, party). Accepted only if over all candidate parties there is exactly ONE subset (meet-in-the-middle,
exact cents) summing to the cash, and no item is proposed by two lines. Amount = each item's open balance.
No ids, names, amounts, dates or entity/currency lists: only T4 payers, maturity window and exact cents.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import numpy as np

from src.common import REPO
from src.match.combo import solve

RANK_MAX = int(os.environ.get("REFGAP_RANK", 3))
PARTY_MIN = float(os.environ.get("REFGAP_PMIN", 0.0))
CAP = int(os.environ.get("REFGAP_CAP", 20))
AGE_MAX = int(os.environ.get("REFGAP_AGE", 200))
DUE_HI = 15
K_MIN = 2
CONF = {1: 0.85, 2: 0.8, 3: 0.75}


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}' WHERE rule IS NULL OR rule NOT LIKE 'T16.%'")
    if not os.path.exists(lp):
        print(f"[{phase}] refgap: no line_party, skipped")
        return
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN coalesce(exception, '') = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE E AS SELECT l.bank_txn_id, l.entity, l.currency, l.booking_date bd, l.amount_cents cash
                    FROM '{w}/lines.parquet' l WHERE l.direction = 'IN' AND l.amount_cents > 0
                    AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    con.execute(f"""CREATE TABLE CI AS SELECT * FROM (
      SELECT e.bank_txn_id, e.cash, p.party_id, p.rank, p.confidence pconf, i.item_id, i.open_at_start_cents v, i.issue_date
      FROM E e JOIN '{lp}' p ON p.bank_txn_id = e.bank_txn_id AND p.rank <= {RANK_MAX} AND p.confidence >= {PARTY_MIN}
      JOIN '{w}/items.parquet' i ON i.party_id = p.party_id AND i.side = 'AR' AND i.kind = 'ARI' AND i.entity = e.entity
           AND i.currency = e.currency AND i.open_at_start_cents > 0 AND i.open_at_start_cents <= e.cash
           AND i.issue_date <= e.bd AND i.issue_date >= e.bd - {AGE_MAX} AND i.due_date <= e.bd + {DUE_HI}
      WHERE i.item_id NOT IN (SELECT item_id FROM USED))
      QUALIFY count(*) OVER (PARTITION BY bank_txn_id, party_id) <= {CAP}
      ORDER BY bank_txn_id, party_id, issue_date DESC, item_id""")
    df = con.sql("SELECT bank_txn_id, cash, party_id, rank, pconf, item_id, v FROM CI").df()
    out = []
    for txn, g in df.groupby("bank_txn_id", sort=False):
        sols = 0
        best = None
        for party, h in g.groupby("party_id", sort=False):
            vals = h["v"].to_numpy(np.int64)
            k, idx = solve(vals, int(h["cash"].iloc[0]))
            sols += k
            if sols > 1:
                break
            if k == 1:
                best = (party, int(h["rank"].iloc[0]), float(h["pconf"].iloc[0]), h.iloc[idx])
        if sols == 1 and best is not None and len(best[3]) >= K_MIN:
            party, rank, pconf, rows = best
            for it, v in zip(rows["item_id"], rows["v"]):
                out.append((txn, it, int(v), party, rank, pconf, len(rows), len(g[g.party_id == party])))
    con.execute("""CREATE TABLE X (bank_txn_id VARCHAR, item_id VARCHAR, amount_cents BIGINT, party_id VARCHAR,
                   rank INT, pconf DOUBLE, k INT, n INT)""")
    if out:
        con.executemany("INSERT INTO X VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
    con.execute("""DELETE FROM X WHERE bank_txn_id IN (SELECT bank_txn_id FROM X WHERE item_id IN
                     (SELECT item_id FROM X GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    case = " ".join(f"WHEN {r} THEN {c}" for r, c in CONF.items())
    con.execute(f"""CREATE TABLE NEW AS
      SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category, '' exception,
             'T16.gap_combo.r' || rank rule, CASE rank {case} END confidence,
             'no item at the end of the chain; only subset (' || k || ' of ' || n || ' open unused due invoices) of T4 '
               || 'candidate ' || party_id || ' (rank ' || rank || ', conf ' || round(pconf, 2) || ') adding up to the cash'
               evidence
      FROM X""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] refgap: {con.sql('SELECT count(*) FROM E').fetchall()[0][0]} receipts without item -> "
          f"{con.sql('SELECT rule, count(DISTINCT bank_txn_id), count(*) FROM NEW GROUP BY 1 ORDER BY 1').fetchall()}"
          f" -> {p} ({time.time() - t0:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
