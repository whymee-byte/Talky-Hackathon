"""T16 · cnrest: the remainder of an AR receipt is an invoice net of its own credit note (ar_1toN_credit_note).

    python -m src.cnrest --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Customers pay many invoices net of credit notes; the credit note (kind ARC) names its invoice in `related_doc`. Earlier
stages often explain part of the receipt and leave a remainder r = cash - (sum invoices - sum credit notes) > 0, or no
item at all (r = cash). A *unit* is one open, unused invoice of a candidate payer, together with every open unused credit
note of the same party/entity/currency whose related_doc is its doc_number (net = open(inv) - sum open(ARC)).

  T16.cn_unit  r equals exactly the net of K_MAX or fewer units of one candidate payer (T4 rank <= RANK_MAX, conf >=
               PARTY_MIN, or the party of items already on the line), units with a credit note only for K = 1 ... the
               solution is unique for the line (over all payers) and each unit is claimed by one line only. Units must be
               issued on or before booking and due within [booking - DUE_LO, booking + DUE_HI].
No ids, amounts, texts or dates: only party candidates, document links and exact cents.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import numpy as np

from src.match.combo import solve

from src.common import REPO

RANK_MAX, PARTY_MIN = 3, 0.2
DUE_LO, DUE_HI = 200, 15
CAP = int(os.environ.get('CNREST_CAP', 20))
K_MAX = 30
NEED_CN = os.environ.get('CNREST_NEED_CN', '1') == '1'


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    if not os.path.exists(lp):
        print(f"[{phase}] cnrest: no line_party, skipped")
        return
    con = duckdb.connect(config={"memory_limit": "3GB", "threads": 4, "preserve_insertion_order": False})
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE I AS SELECT item_id, kind, party_id, entity, currency, doc_number, related_doc, issue_date,
                    due_date, open_at_start_cents o FROM '{w}/items.parquet' WHERE kind IN ('ARI', 'ARC') AND open_at_start_cents > 0""")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute("DELETE FROM I WHERE item_id IN (SELECT item_id FROM USED)")
    # lines: IN, AR, remainder > 0 (no other side / category on the line)
    con.execute(f"""CREATE TABLE R AS
      SELECT l.bank_txn_id, l.entity, l.currency, l.booking_date bd,
             l.amount_cents - coalesce(sum(CASE WHEN a.item_id IS NULL THEN 0 WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END), 0) r
      FROM '{w}/lines.parquet' l JOIN A a USING (bank_txn_id)
      LEFT JOIN (SELECT item_id, kind FROM '{w}/items.parquet') i ON i.item_id = a.item_id
      WHERE l.direction = 'IN' AND l.amount_cents > 0
      GROUP BY l.bank_txn_id, l.entity, l.currency, l.booking_date, l.amount_cents
      HAVING bool_and(CASE WHEN a.item_id IS NULL THEN coalesce(a.category, 'UNIDENTIFIED') = 'UNIDENTIFIED'
                           ELSE i.kind IN ('ARI', 'ARC') END)
             AND bool_and(coalesce(a.exception, '') NOT IN ('RETURNED', 'DUPLICATE'))""")
    con.execute("DELETE FROM R WHERE r <= 0")
    con.execute(f"""CREATE TABLE RP AS
      SELECT DISTINCT bank_txn_id, party_id FROM (
        SELECT bank_txn_id, party_id FROM '{lp}' WHERE rank <= {RANK_MAX} AND confidence >= {PARTY_MIN}
        UNION ALL SELECT a.bank_txn_id, i.party_id FROM A a JOIN '{w}/items.parquet' i USING (item_id) WHERE i.kind IN ('ARI', 'ARC'))
      WHERE bank_txn_id IN (SELECT bank_txn_id FROM R) AND party_id IS NOT NULL""")
    # units: invoice + its open credit notes
    con.execute("""CREATE TABLE U AS
      SELECT v.item_id inv, v.party_id, v.entity, v.currency, v.issue_date, v.due_date,
             v.o - coalesce(sum(c.o), 0) net, list(c.item_id) FILTER (WHERE c.item_id IS NOT NULL) cns, count(c.item_id) ncn
      FROM I v LEFT JOIN I c ON c.kind = 'ARC' AND c.related_doc = v.doc_number AND c.party_id = v.party_id
           AND c.entity = v.entity AND c.currency = v.currency
      WHERE v.kind = 'ARI' GROUP BY v.item_id, v.party_id, v.entity, v.currency, v.issue_date, v.due_date, v.o""")
    con.execute("DELETE FROM U WHERE net <= 0")
    con.execute(f"""CREATE TABLE K AS
      SELECT r.bank_txn_id, r.r, rp.party_id, u.inv, u.cns, u.net, u.ncn, u.due_date
      FROM R r JOIN RP rp USING (bank_txn_id)
      JOIN U u ON u.party_id = rp.party_id AND u.entity = r.entity AND u.currency = r.currency AND u.net <= r.r
           AND u.issue_date <= r.bd AND u.due_date BETWEEN r.bd - {DUE_LO} AND r.bd + {DUE_HI}
      QUALIFY row_number() OVER (PARTITION BY r.bank_txn_id, rp.party_id ORDER BY u.due_date DESC, u.inv) <= {CAP}""")
    df = con.execute("SELECT bank_txn_id, party_id, r, inv, net, ncn FROM K ORDER BY bank_txn_id, party_id, due_date DESC, inv").df()
    sol = {}
    bad = set()
    for (bt, party), g in df.groupby(["bank_txn_id", "party_id"], sort=False):
        if bt in bad:
            continue
        vals = g["net"].to_numpy(np.int64)
        k, idx = solve(vals, int(g["r"].iloc[0]))
        if k == 0:
            continue
        if k > 1 or bt in sol or len(idx) > K_MAX:
            bad.add(bt); sol.pop(bt, None); continue
        if NEED_CN and g["ncn"].to_numpy()[idx].sum() == 0:
            continue
        sol[bt] = (party, list(g["inv"].to_numpy()[idx]))
    rows = [(bt, party, inv) for bt, (party, invs) in sol.items() for inv in invs]
    con.execute("CREATE TABLE S (bank_txn_id VARCHAR, party_id VARCHAR, inv VARCHAR)")
    if rows:
        con.executemany("INSERT INTO S VALUES (?, ?, ?)", rows)
    con.execute("""CREATE TABLE M AS SELECT s.bank_txn_id, k.r, s.party_id, s.inv, k.cns, k.net FROM S s
                   JOIN K k ON k.bank_txn_id = s.bank_txn_id AND k.party_id = s.party_id AND k.inv = s.inv""")
    con.execute("DELETE FROM M WHERE inv IN (SELECT inv FROM M GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1)")
    con.execute("DELETE FROM M WHERE bank_txn_id IN (SELECT bank_txn_id FROM S EXCEPT SELECT bank_txn_id FROM M) ")
    con.execute("DELETE FROM M WHERE bank_txn_id IN (SELECT s.bank_txn_id FROM S s LEFT JOIN M m USING (bank_txn_id, inv) WHERE m.inv IS NULL)")
    # a credit note may not be claimed twice either
    con.execute("""DELETE FROM M WHERE bank_txn_id IN (SELECT bank_txn_id FROM (SELECT bank_txn_id, unnest(cns) c FROM M)
                   WHERE c IN (SELECT c FROM (SELECT unnest(cns) c FROM M) GROUP BY 1 HAVING count(*) > 1))""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM M)")
    con.execute(f"""INSERT INTO A BY NAME
      SELECT bank_txn_id, inv item_id, i.o amount_cents, NULL::VARCHAR category, NULL::VARCHAR exception, 'T16.cn_unit' rule,
             0.8 confidence, 'remainder ' || r || ' = invoice net of its credit notes, payer ' || m.party_id evidence
      FROM M m JOIN I i ON i.item_id = m.inv
      UNION ALL
      SELECT bank_txn_id, c, i.o, NULL, NULL, 'T16.cn_unit', 0.8,
             'credit note of invoice ' || m.inv || ' netted in remainder ' || r
      FROM (SELECT bank_txn_id, inv, r, unnest(cns) c FROM M) m JOIN I i ON i.item_id = m.c""")
    n = con.execute("SELECT count(DISTINCT bank_txn_id) FROM M").fetchall()[0][0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] cnrest: {n} remainders explained by an invoice net of its credit notes  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
