"""T17 · repeated instalments: a receipt left without item that pays the same amount as an earlier/later PARTIAL payment
of an open invoice of its payer is the next instalment of that invoice.

    python -m src.instrep --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.refnoise and before src.unident; rewrites only no-item IN lines of the allocations parquet. Customers
paying in instalments (ar_instalment, POLICIES §3 PARTIAL) send the same amount each time; usually only one of those
lines quotes the invoice, the others end without item. No ids, amounts or texts: only exact cents and the balance.

  T17.inst_repeat  IN line with no item; candidate = AR invoice of one of the T4 candidate payers (rank <= RANK_MAX) in the
                   line currency that already has a PARTIAL row with amount = the line cash (±1 cent) and whose balance
                   (open at start − all its non-RETURNED rows) still covers the cash. Unique candidate per line; an
                   invoice takes at most as many new lines as its balance allows (in booking order).
                   Exception: PARTIAL while the balance after the row stays above the tolerance, else none (last one).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

RANK_MAX = 3
TOL = "greatest(1, 0.005 * {})"


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    if not os.path.exists(lp):
        print(f"[{phase}] instrep: no line_party, skipped")
        return
    con.execute(f"""CREATE TABLE L AS SELECT bank_txn_id, abs(amount_cents) cash, currency, booking_date bd
                    FROM '{w}/lines.parquet'""")
    # invoices with a PARTIAL row: the instalment amount(s) and the balance left after every existing row
    con.execute(f"""CREATE TABLE IT AS
      SELECT a.item_id, i.party_id, i.currency, i.open_at_start_cents
               - sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents
                          WHEN coalesce(a.exception, '') = 'DUPLICATE_PAYMENT' THEN 0 ELSE a.amount_cents END) bal,
             list(DISTINCT a.amount_cents) FILTER (WHERE a.exception = 'PARTIAL') inst
      FROM A a JOIN '{w}/items.parquet' i USING (item_id)
      WHERE i.kind = 'ARI' GROUP BY a.item_id, i.party_id, i.currency, i.open_at_start_cents
      HAVING count(*) FILTER (WHERE a.exception = 'PARTIAL') > 0""")
    con.execute(f"""CREATE TABLE C AS
      SELECT n.bank_txn_id, n.bd, n.cash, it.item_id, it.bal, min(p.rank) rk
      FROM (SELECT * FROM L WHERE bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)
              AND bank_txn_id IN (SELECT bank_txn_id FROM '{w}/lines.parquet' WHERE direction = 'IN')) n
      JOIN '{lp}' p ON p.bank_txn_id = n.bank_txn_id AND p.rank <= {RANK_MAX}
      JOIN IT it ON it.party_id = p.party_id AND it.currency = n.currency
      WHERE list_has_any(it.inst, [n.cash - 1, n.cash, n.cash + 1]) AND it.bal >= n.cash - {TOL.format('n.cash')}
      GROUP BY n.bank_txn_id, n.bd, n.cash, it.item_id, it.bal
      QUALIFY count(*) OVER (PARTITION BY n.bank_txn_id) = 1""")
    # an invoice takes new lines in booking order while its balance covers them
    con.execute(f"""CREATE TABLE NEW AS
      SELECT * FROM (SELECT *, sum(cash) OVER (PARTITION BY item_id ORDER BY bd, bank_txn_id) cum FROM C)
      WHERE cum <= bal + {TOL.format('bal')}""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, cash amount_cents, NULL::VARCHAR category,
                     'PARTIAL' exception, 'T17.inst_repeat' rule, CASE WHEN rk = 1 THEN 0.8 ELSE 0.65 END confidence,
                     'no item; same amount as a PARTIAL payment of this payer invoice (rank ' || rk || '), balance ' || bal evidence
                   FROM NEW""")
    # exceptions of the touched invoices from the balance in booking order: the row that leaves nothing open is the last
    con.execute(f"""CREATE TABLE R AS
      SELECT a.bank_txn_id, a.item_id, a.amount_cents,
             i.open_at_start_cents - coalesce(sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents
                                                       WHEN coalesce(a.exception, '') = 'DUPLICATE_PAYMENT' THEN 0 ELSE a.amount_cents END)
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) before
      FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      WHERE a.item_id IN (SELECT item_id FROM NEW) AND coalesce(a.exception, '') IN ('', 'PARTIAL')""")
    con.execute(f"""UPDATE A SET exception = CASE WHEN r.before - r.amount_cents > {TOL.format('r.before')} THEN 'PARTIAL' ELSE NULL END
                    FROM R r WHERE A.bank_txn_id = r.bank_txn_id AND A.item_id = r.item_id AND A.rule LIKE 'T17.%'""")
    n = con.execute("SELECT count(*), count(*) FILTER (WHERE exception = 'PARTIAL') FROM A WHERE rule LIKE 'T17.%'").fetchall()[0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] instrep: {n[0]} lines ({n[1]} PARTIAL, {n[0] - n[1]} last instalment)  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
