"""T13c · receipts that mix invoices of one customer with stray items of another (same number, other customer).

    python -m src.mixfix --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.refnoise and before src.instrep; rewrites the allocations parquet. A customer pays its own invoices (or
its group's: parent / subsidiaries share `group_id`). Reference matching on truncated numbers sometimes adds an item of
another customer whose number shares the digits (FV21-0100544 vs FV25-0100544). No ids or texts: only the masters.

  T13.drop_minority  IN line with AR items whose dominant group (customer group, else customer) has >= 2 items and more
                     than twice the items of any other group: the AR rows of other groups are dropped, except rows from
                     side files (rules T2*: remittance, cheque, card batches list their items explicitly).
                     Window (TRAIN 2024-07 -> 2025-06): 1,944 rows dropped, 0 of them in the golden (Q1 and Q2-4 alike).
  T13.rest_fill      then: a line whose AR rows are all of one customer at their open balance and still short of the cash
                     gets the unique subset of that customer's open unused invoices that adds up to the remainder.
Dropped rows are written to work/<phase>/mixfix_dropped.parquet for audit (rule, evidence).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import numpy as np

from src.common import REPO
from src.match.combo import solve

CAP, AGE_MAX = 16, 200


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE X AS SELECT a.bank_txn_id, a.item_id, a.rule, coalesce(i.group_id, i.party_id) grp
                    FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                    WHERE l.direction = 'IN' AND i.side = 'AR'""")
    con.execute("""CREATE TABLE M AS SELECT bank_txn_id, grp, n FROM (
                     SELECT bank_txn_id, grp, count(*) n, row_number() OVER (PARTITION BY bank_txn_id ORDER BY count(*) DESC) r,
                            lead(count(*)) OVER (PARTITION BY bank_txn_id ORDER BY count(*) DESC) n2,
                            count(*) OVER (PARTITION BY bank_txn_id) k
                     FROM X GROUP BY 1, 2)
                   WHERE r = 1 AND k > 1 AND n >= 2 AND n > 2 * coalesce(n2, 0)""")
    con.execute("""CREATE TABLE D AS SELECT x.bank_txn_id, x.item_id, x.rule, 'T13.drop_minority' AS drop_rule,
                     'line dominated by group ' || m.grp || ' (' || m.n || ' items); item of group ' || x.grp AS drop_evidence
                   FROM X x JOIN M m USING (bank_txn_id) WHERE x.grp <> m.grp AND coalesce(x.rule, '') NOT LIKE 'T2%'""")
    con.execute("DELETE FROM A WHERE (bank_txn_id, item_id) IN (SELECT bank_txn_id, item_id FROM D)")
    con.execute(f"COPY D TO '{w}/mixfix_dropped.parquet' (FORMAT parquet)")
    n = con.execute("SELECT count(*), count(DISTINCT bank_txn_id) FROM D").fetchall()[0]
    nf = _fill(con, w)
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] mixfix: dropped {n[0]} stray rows on {n[1]} lines · rest_fill {nf[0]} lines, {nf[1]} rows  ({time.time() - t0:.0f}s)")


def _fill(con, w: str):
    """T13.rest_fill: IN line whose AR rows all belong to one customer, all at their open balance with no exception, same
    currency, and still short of the cash: a unique subset of that customer's open unused invoices (CAP most recent,
    issued <= booking date, at most AGE_MAX days before) adds up to the remainder exactly -> those invoices."""
    con.execute("""CREATE OR REPLACE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    rows = con.sql(f"""
      WITH ln AS (SELECT a.bank_txn_id, any_value(l.booking_date) bd, any_value(l.currency) cur, any_value(l.entity) ent,
                         any_value(i.party_id) party_id,
                         any_value(abs(l.amount_cents)) - sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END) rem
                  FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
                  WHERE l.direction = 'IN'
                  GROUP BY a.bank_txn_id
                  HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
                     AND count(DISTINCT i.party_id) = 1 AND bool_and(i.side = 'AR' AND i.currency = l.currency)
                     AND bool_and(coalesce(a.exception, '') = '' AND a.amount_cents = i.open_at_start_cents)),
           c AS (SELECT ln.bank_txn_id, ln.rem, i.item_id, i.open_at_start_cents v, i.issue_date
                 FROM ln JOIN '{w}/items.parquet' i ON i.party_id = ln.party_id AND i.currency = ln.cur AND i.entity = ln.ent
                      AND i.kind = 'ARI' AND i.open_at_start_cents > 0 AND i.issue_date <= ln.bd AND ln.bd - i.issue_date <= {AGE_MAX}
                 WHERE ln.rem > 100 AND i.item_id NOT IN (SELECT item_id FROM USED)
                 QUALIFY row_number() OVER (PARTITION BY ln.bank_txn_id ORDER BY i.issue_date DESC, i.item_id) <= {CAP})
      SELECT bank_txn_id, any_value(rem), list(item_id ORDER BY issue_date DESC, item_id), list(v ORDER BY issue_date DESC, item_id)
      FROM c GROUP BY 1""").fetchall()
    out = []
    for t, rem, ids, vals in rows:
        k, sol = solve(np.array(vals, dtype=np.int64), int(rem))
        if k == 1:
            out += [(t, ids[i], vals[i], len(sol)) for i in sol]
    con.execute("CREATE OR REPLACE TABLE F (bank_txn_id VARCHAR, item_id VARCHAR, amount_cents BIGINT, k INTEGER)")
    if out:
        con.executemany("INSERT INTO F VALUES (?, ?, ?, ?)", out)
    con.execute("""DELETE FROM F WHERE bank_txn_id IN (SELECT bank_txn_id FROM F WHERE item_id IN
                     (SELECT item_id FROM F GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category, NULL::VARCHAR exception,
                     'T13.rest_fill' rule, 0.7 confidence,
                     'line short of cash; unique subset of ' || k || ' open invoices of the same customer = remainder' evidence FROM F""")
    return con.execute("SELECT count(DISTINCT bank_txn_id), count(*) FROM F").fetchall()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
