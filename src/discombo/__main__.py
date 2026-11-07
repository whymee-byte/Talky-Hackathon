"""T18 · receipts that take the early-payment discount on one or more invoices (POLICIES §3 SHORT_DISCOUNT).

    python -m src.discombo --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.refnoise and before src.unident; rewrites only no-item IN lines. The SQL/mitm matchers look for sums of
OPEN balances; a customer that pays within the discount period sends total × (1 − discount %) for those invoices, so
the line never adds up. Here each candidate invoice is valued at what the customer would pay on the booking date:
  * terms with discount and booking date <= issue date + discount days + 3 → round(total × (1 − pct / 100)), SHORT_DISCOUNT
  * otherwise → open balance, no exception.
Candidates: open, unused AR invoices of a T4 candidate payer (rank <= 3, confidence >= PARTY_MIN), same entity and
currency, issued on or before the booking date and at most AGE_MAX days before; the CAP most recent. A subset (meet in the
middle, src.match.combo.solve) that adds up to the cash exactly and contains at least one discounted invoice is accepted
only if it is the only one over all candidate payers. Rows go at the full open balance (POLICIES §2).
  T18.disc_combo
  T18.disc_unique  no payer needed: cash = discounted total of the only open unused invoice of the entity/currency booked
                   inside its discount period (and no other line proposes it) -> SHORT_DISCOUNT at the open balance
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import numpy as np

from src.common import REPO
from src.match.combo import solve

CAP = 16
AGE_MAX = 200
RANK_MAX, PARTY_MIN = 3, 0.2


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    if not os.path.exists(lp):
        print(f"[{phase}] discombo: no line_party, skipped")
        return
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    rows = con.sql(f"""
      WITH n AS (SELECT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.entity, l.booking_date bd, p.party_id
                 FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
                 WHERE l.direction = 'IN' AND p.rank <= {RANK_MAX} AND p.confidence >= {PARTY_MIN}
                   AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)),
           c AS (SELECT n.bank_txn_id, n.party_id, n.cash, i.item_id, i.open_at_start_cents open_c, i.issue_date,
                        (i.discount_pct IS NOT NULL AND i.discount_days IS NOT NULL
                         AND n.bd <= i.issue_date + i.discount_days + 3 AND i.open_at_start_cents = i.total_cents) disc,
                        CASE WHEN i.discount_pct IS NOT NULL AND i.discount_days IS NOT NULL
                                  AND n.bd <= i.issue_date + i.discount_days + 3 AND i.open_at_start_cents = i.total_cents
                             THEN round(i.total_cents * (1 - i.discount_pct / 100))::BIGINT ELSE i.open_at_start_cents END v
                 FROM n JOIN '{w}/items.parquet' i ON i.party_id = n.party_id AND i.currency = n.currency
                      AND i.entity = n.entity AND i.kind = 'ARI' AND i.open_at_start_cents > 0
                      AND i.issue_date <= n.bd AND n.bd - i.issue_date <= {AGE_MAX}
                 WHERE i.item_id NOT IN (SELECT item_id FROM USED)
                 QUALIFY row_number() OVER (PARTITION BY n.bank_txn_id, n.party_id ORDER BY i.issue_date DESC, i.item_id) <= {CAP})
      SELECT bank_txn_id, party_id, any_value(cash), list(item_id ORDER BY issue_date DESC, item_id),
             list(v ORDER BY issue_date DESC, item_id), list(disc ORDER BY issue_date DESC, item_id), list(open_c ORDER BY issue_date DESC, item_id)
      FROM c GROUP BY 1, 2 HAVING bool_or(disc) ORDER BY 1, 2""").fetchall()
    sols = {}   # line -> list of (items, discs, opens); None when ambiguous
    for t, party, cash, ids, vals, discs, opens in rows:
        if sols.get(t, 0) is None:
            continue
        k, sol = solve(np.array(vals, dtype=np.int64), int(cash))
        if k == 0:
            continue
        if k > 1 or t in sols or not any(discs[i] for i in sol):
            sols[t] = None
            continue
        sols[t] = [(ids[i], discs[i], opens[i]) for i in sol]
    out = [(t, it, op, "SHORT_DISCOUNT" if d else None, len(s)) for t, s in sols.items() if s for it, d, op in s]
    con.execute("CREATE TABLE NEW (bank_txn_id VARCHAR, item_id VARCHAR, amount_cents BIGINT, exception VARCHAR, k INTEGER)")
    if out:
        con.executemany("INSERT INTO NEW VALUES (?, ?, ?, ?, ?)", out)
    # an invoice proposed by two lines is dropped from both lines
    con.execute("""DELETE FROM NEW WHERE bank_txn_id IN (SELECT bank_txn_id FROM NEW WHERE item_id IN
                     (SELECT item_id FROM NEW GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category, exception,
                     'T18.disc_combo' rule, 0.75 confidence,
                     'unique subset of ' || k || ' payer invoices valued net of early-payment discount = cash' evidence FROM NEW""")
    n = con.execute("SELECT count(DISTINCT bank_txn_id), count(*) FROM NEW").fetchall()[0]
    # disc_unique: no payer needed. Cash = discounted total of exactly one open, unused invoice of the line's entity and
    # currency booked inside its discount period, and no other line proposes that invoice.
    con.execute("DROP TABLE USED")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE U AS
      WITH n AS (SELECT bank_txn_id, abs(amount_cents) cash, currency, entity, booking_date bd FROM '{w}/lines.parquet'
                 WHERE direction = 'IN' AND bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)),
           d AS (SELECT item_id, entity, currency, issue_date, issue_date + discount_days + 3 dl, open_at_start_cents open_c,
                        round(total_cents * (1 - discount_pct / 100))::BIGINT v
                 FROM '{w}/items.parquet' WHERE kind = 'ARI' AND discount_pct IS NOT NULL AND discount_days IS NOT NULL
                   AND open_at_start_cents = total_cents AND total_cents > 0 AND item_id NOT IN (SELECT item_id FROM USED))
      SELECT n.bank_txn_id, d.item_id, d.open_c FROM n JOIN d ON d.entity = n.entity AND d.currency = n.currency
           AND d.v = n.cash AND n.bd BETWEEN d.issue_date AND d.dl
      QUALIFY count(*) OVER (PARTITION BY n.bank_txn_id) = 1 AND count(*) OVER (PARTITION BY d.item_id) = 1""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM U)")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
                     'SHORT_DISCOUNT' exception, 'T18.disc_unique' rule, 0.8 confidence,
                     'cash = discounted total of the only open invoice of the entity in its discount period' evidence FROM U""")
    nu = con.execute("SELECT count(*) FROM U").fetchall()[0][0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] discombo: disc_combo {n[0]} lines, {n[1]} rows · disc_unique {nu} lines  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
