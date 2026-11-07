"""T21 · fxlate: receipts whose item rows add up to more than the cash by exactly the early-payment discount of a
subset of its invoices (ar_discount_late / ar_short_discount on multi-invoice receipts).

    python -m src.fxlate --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Post-stage on the allocations parquet (run after the last item-adding stage, before excfix). Only touches lines whose
rows are all items with an empty exception, same currency as the line, and sum(amount) - cash = D > 0.

Rule T21.disc_subset: candidate items = AR invoices of the line whose payment terms carry a discount (items.discount_pct,
read from payment_terms). d_i = round(total_i * pct_i / 100) cents (POLICIES 3: discount on the invoice total). If exactly
ONE subset of candidates has sum(d_i) = D, each item of the subset is re-labelled (POLICIES 3):
  booking <= issue + discount_days + 3 (grace) -> SHORT_DISCOUNT, amount = open amount (fully cleared)
  otherwise (discount taken late)              -> PARTIAL, amount = open - d_i (what was actually applied)
No ids, amounts, texts, dates or entity/currency lists: only payment terms, invoice totals and exact cents.
"""
from __future__ import annotations

import argparse
import itertools
import os
import time

import duckdb
import numpy as np

from src.common import REPO
from src.match.combo import solve

GRACE = 3
RANK_MAX = 3
CAP = int(os.environ.get("FXLATE_CAP", 24))  # open unused invoices per (line, candidate payer); larger pools are skipped (ambiguous)
K_MAX = 14  # candidates per line for the exhaustive subset search


def fx_gap(con, w: str) -> list:
    """T21.fx_orig: receipts with no item whose bank line states the original amount in another currency
    (lines.orig_amount_cents / orig_currency): the only subset of a T4 candidate's open, unused AR invoices in that
    currency (same entity, issued on or before booking) adding up to the original amount exactly -> FX_DIFFERENCE."""
    lp = f"{w}/line_party.parquet"
    if not os.path.exists(lp):
        return []
    con.execute("""CREATE OR REPLACE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN coalesce(exception, '') = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE OR REPLACE TABLE E AS SELECT l.bank_txn_id, l.entity, l.orig_currency oc,
                    l.orig_amount_cents oa, l.booking_date bd
                    FROM '{w}/lines.parquet' l WHERE l.direction = 'IN' AND l.orig_amount_cents > 0
                    AND l.orig_currency IS NOT NULL AND l.orig_currency <> l.currency
                    AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    df = con.sql(f"""SELECT * FROM (
      SELECT e.bank_txn_id, e.oa, p.party_id, p.rank, i.item_id, i.open_at_start_cents v, i.issue_date
      FROM E e JOIN '{lp}' p ON p.bank_txn_id = e.bank_txn_id AND p.rank <= {RANK_MAX}
      JOIN '{w}/items.parquet' i ON i.party_id = p.party_id AND i.side = 'AR' AND i.kind = 'ARI' AND i.entity = e.entity
           AND i.currency = e.oc AND i.open_at_start_cents > 0 AND i.open_at_start_cents <= e.oa AND i.issue_date <= e.bd
      WHERE i.item_id NOT IN (SELECT item_id FROM USED))
      QUALIFY count(*) OVER (PARTITION BY bank_txn_id, party_id) <= {CAP}
      ORDER BY bank_txn_id, party_id, item_id""").df()
    out = []
    for txn, g in df.groupby("bank_txn_id", sort=False):
        sols, best = 0, None
        for party, h in g.groupby("party_id", sort=False):
            k, idx = solve(h["v"].to_numpy(np.int64), int(h["oa"].iloc[0]))
            sols += k
            if sols > 1:
                break
            if k == 1:
                best = (party, int(h["rank"].iloc[0]), h.iloc[idx])
        if sols == 1 and best is not None:
            party, rank, rows = best
            for it, v in zip(rows["item_id"], rows["v"]):
                out.append((txn, it, int(v), party, rank, len(rows)))
    return out


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE L AS
      SELECT a.bank_txn_id, any_value(l.amount_cents) cash, any_value(l.booking_date) bd,
             sum(a.amount_cents) s, count(*) n
      FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      LEFT JOIN '{w}/items.parquet' i ON i.item_id = a.item_id
      WHERE l.direction = 'IN'
      GROUP BY 1
      HAVING bool_and(i.item_id IS NOT NULL AND coalesce(a.exception, '') = '' AND i.side = 'AR'
                      AND i.currency = l.currency)
         AND sum(a.amount_cents) > any_value(l.amount_cents)""")
    df = con.sql(f"""
      SELECT L.bank_txn_id, L.s - L.cash d_tot, L.bd, a.item_id, a.amount_cents amt,
             CAST(round(i.total_cents * i.discount_pct / 100) AS BIGINT) d, i.issue_date, i.discount_days
      FROM L JOIN A a USING (bank_txn_id) JOIN '{w}/items.parquet' i ON i.item_id = a.item_id
      WHERE i.kind = 'ARI' AND coalesce(i.discount_pct, 0) > 0 AND i.discount_days IS NOT NULL
      ORDER BY 1, 4""").df()
    out = []
    for txn, g in df.groupby("bank_txn_id", sort=False):
        if len(g) > K_MAX:
            continue
        target = int(g["d_tot"].iloc[0])
        ds = g["d"].astype("int64").tolist()
        sols = []
        for k in range(1, len(ds) + 1):
            for comb in itertools.combinations(range(len(ds)), k):
                if sum(ds[j] for j in comb) == target:
                    sols.append(comb)
                    if len(sols) > 1:
                        break
            if len(sols) > 1:
                break
        if len(sols) != 1:
            continue
        for j in sols[0]:
            r = g.iloc[j]
            late = (r["bd"] - r["issue_date"]).days > int(r["discount_days"]) + GRACE
            exc = "PARTIAL" if late else "SHORT_DISCOUNT"
            amt = int(r["amt"]) - int(r["d"]) if late else int(r["amt"])
            out.append((txn, r["item_id"], amt, exc, int(r["d"]), len(sols[0]), len(g)))
    con.execute("CREATE TABLE X (bank_txn_id VARCHAR, item_id VARCHAR, amt BIGINT, exc VARCHAR, d BIGINT, k INT, n INT)")
    if out:
        con.executemany("INSERT INTO X VALUES (?, ?, ?, ?, ?, ?, ?)", out)
    con.execute("""UPDATE A SET amount_cents = X.amt, exception = X.exc,
                     rule = coalesce(A.rule, '') || '+T21.disc_subset', confidence = 0.85,
                     evidence = 'item rows exceed cash by the discount of the only matching subset (' || X.k || ' of '
                                || X.n || ' discount-term invoices); d=' || X.d || '; ' || X.exc
                   FROM X WHERE A.bank_txn_id = X.bank_txn_id AND A.item_id = X.item_id""")
    fx = fx_gap(con, w)
    con.execute("CREATE TABLE F (bank_txn_id VARCHAR, item_id VARCHAR, amount_cents BIGINT, party_id VARCHAR, rank INT, k INT)")
    if fx:
        con.executemany("INSERT INTO F VALUES (?, ?, ?, ?, ?, ?)", fx)
    con.execute("""DELETE FROM F WHERE bank_txn_id IN (SELECT bank_txn_id FROM F WHERE item_id IN
                     (SELECT item_id FROM F GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM F)")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category,
                     'FX_DIFFERENCE' exception, 'T21.fx_orig.r' || rank rule, CASE WHEN rank = 1 THEN 0.85 ELSE 0.75 END confidence,
                     'no item; only subset (' || k || ' invoices) of T4 candidate ' || party_id || ' (rank ' || rank
                       || ') in the original currency of the line adding up to its original amount' evidence FROM F""")
    print(f"[{phase}] fxlate fx_orig: {con.sql('SELECT count(*) FROM E').fetchall()[0][0]} lines with orig amount and no item -> "
          f"{con.sql('SELECT count(DISTINCT bank_txn_id), count(*) FROM F').fetchall()}")
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] fxlate: {con.sql('SELECT count(*) FROM L').fetchall()[0][0]} lines over cash -> "
          f"{con.sql('SELECT exc, count(DISTINCT bank_txn_id), count(*) FROM X GROUP BY 1 ORDER BY 1').fetchall()}"
          f" -> {p} ({time.time() - t0:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True)
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
