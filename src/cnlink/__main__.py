"""T13 · credit notes netted in receipts: complete a line with the open credit notes of the invoices it pays.

    python -m src.cnlink --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after src.refnoise and before src.unident; rewrites work/<phase>/allocations.parquet. A customer that received a
credit note for an invoice pays the invoice net of it (ar_1toN_credit_note: in the TRAIN golden 94 % of the credit notes
of a receipt relate, via `related_doc`, to an invoice of the same receipt). No ids, amounts or texts: only the masters'
`related_doc` link and exact cents.

  T13.cn_net    IN line with AR invoices allocated; open, unused credit notes of the same party/currency, issued on or
                before the booking date, whose related_doc is the doc_number of one of those invoices. If
                cash = Σ open(invoices) − Σ open(credit notes already there) − Σ open(new credit notes) (exact, ±1 cent per
                item) the credit notes are added and every invoice/credit note goes at its open balance with no
                exception (a PARTIAL that only reflected the netting disappears). Tried with all the related credit
                notes and, if that does not fit, with exactly one of them (unique fit).
  T13.cn_pair   IN line with no item, likely party (T4 rank 1, confidence >= 0.5): exactly one pair (open unused
                invoice, open unused credit note related to it) of that party with open(inv) − open(cn) = cash.
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
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE I AS SELECT item_id, kind, party_id, currency, doc_number, related_doc, issue_date,
                    open_at_start_cents open_c FROM '{w}/items.parquet' WHERE kind IN ('ARI', 'ARC')""")
    # items in use: more non-RETURNED rows than RETURNED rows
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE L AS SELECT bank_txn_id, abs(amount_cents) cash, currency, booking_date
                    FROM '{w}/lines.parquet' WHERE direction = 'IN'""")
    # lines whose rows are all same-currency AR items, no RETURNED/DUPLICATE
    con.execute("""CREATE TABLE LI AS
      SELECT a.bank_txn_id, l.cash, l.currency, l.booking_date, any_value(i.party_id) party_id,
             count(*) n, sum(CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END) net_open,
             list(i.doc_number) FILTER (WHERE i.kind = 'ARI') docs
      FROM A a JOIN L l USING (bank_txn_id) JOIN I i USING (item_id)
      WHERE i.currency = l.currency
      GROUP BY a.bank_txn_id, l.cash, l.currency, l.booking_date
      HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
         AND count(DISTINCT i.party_id) = 1
         AND bool_and(coalesce(a.exception, '') NOT IN ('RETURNED', 'DUPLICATE_PAYMENT'))
         AND bool_and(i.open_c > 0)""")
    con.execute("""CREATE TABLE C AS
      SELECT li.bank_txn_id, c.item_id, c.open_c, li.cash, li.net_open, li.n
      FROM LI li JOIN I c ON c.kind = 'ARC' AND c.party_id = li.party_id AND c.currency = li.currency
           AND c.open_c > 0 AND c.issue_date <= li.booking_date AND list_contains(li.docs, c.related_doc)
      WHERE c.item_id NOT IN (SELECT item_id FROM USED)""")
    # all related credit notes, else exactly one
    con.execute("""CREATE TABLE NET AS
      WITH s AS (SELECT bank_txn_id, any_value(cash) cash, any_value(net_open) net_open, any_value(n) n,
                        sum(open_c) cn, count(*) k FROM C GROUP BY 1),
           alln AS (SELECT c.bank_txn_id, c.item_id, c.open_c FROM C c JOIN s USING (bank_txn_id)
                    WHERE abs(s.net_open - s.cn - s.cash) <= s.n + s.k),
           one AS (SELECT c.bank_txn_id, c.item_id, c.open_c FROM C c
                   WHERE c.bank_txn_id NOT IN (SELECT bank_txn_id FROM alln)
                     AND abs(c.net_open - c.open_c - c.cash) <= c.n + 1
                   QUALIFY count(*) OVER (PARTITION BY c.bank_txn_id) = 1)
      SELECT * FROM (SELECT * FROM alln UNION ALL SELECT * FROM one)
      QUALIFY count(*) OVER (PARTITION BY item_id) = 1""")
    lines_net = "SELECT DISTINCT bank_txn_id FROM NET"
    # existing rows of those lines: open balance, no exception
    con.execute(f"""UPDATE A SET amount_cents = i.open_c, exception = NULL,
                              rule = CASE WHEN coalesce(A.rule, '') = '' THEN 'T13.cn_net'
                                          ELSE A.rule || '+T13.cn_net' END,
                              confidence = greatest(coalesce(A.confidence, 0.0), 0.85),
                              evidence = 'credit note related to an invoice of the line; cash = invoices - credit notes'
                    FROM I i WHERE A.item_id = i.item_id AND A.bank_txn_id IN ({lines_net})""")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
                     NULL::VARCHAR exception, 'T13.cn_net' rule, 0.85 confidence,
                     'credit note related to an invoice of the line; cash = invoices - credit notes' evidence FROM NET""")
    n_net = con.execute(f"SELECT count(*), count(DISTINCT bank_txn_id) FROM NET").fetchall()[0]
    # cn_pair on lines with no item
    lp = f"{w}/line_party.parquet"
    n_pair = (0, 0)
    if os.path.exists(lp):
        con.execute("DROP TABLE USED")
        con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                       HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
        con.execute(f"""CREATE TABLE PR AS
          WITH n AS (SELECT l.*, p.party_id FROM L l JOIN '{lp}' p USING (bank_txn_id)
                     WHERE p.rank = 1 AND p.confidence >= {PARTY_MIN}
                       AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)),
               inv AS (SELECT * FROM I WHERE kind = 'ARI' AND open_c > 0 AND item_id NOT IN (SELECT item_id FROM USED)),
               cn AS (SELECT * FROM I WHERE kind = 'ARC' AND open_c > 0 AND item_id NOT IN (SELECT item_id FROM USED))
          SELECT n.bank_txn_id, inv.item_id inv_id, inv.open_c inv_c, cn.item_id cn_id, cn.open_c cn_c
          FROM n JOIN inv ON inv.party_id = n.party_id AND inv.currency = n.currency AND inv.issue_date <= n.booking_date
               JOIN cn ON cn.party_id = n.party_id AND cn.currency = n.currency AND cn.related_doc = inv.doc_number
                    AND cn.issue_date <= n.booking_date
          WHERE abs(inv.open_c - cn.open_c - n.cash) <= 2
          QUALIFY count(*) OVER (PARTITION BY n.bank_txn_id) = 1 AND count(*) OVER (PARTITION BY inv.item_id) = 1
              AND count(*) OVER (PARTITION BY cn.item_id) = 1""")
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM PR)")
        for col, amt in (("inv_id", "inv_c"), ("cn_id", "cn_c")):
            con.execute(f"""INSERT INTO A BY NAME SELECT bank_txn_id, {col} item_id, {amt} amount_cents,
                             NULL::VARCHAR category, NULL::VARCHAR exception, 'T13.cn_pair' rule, 0.7 confidence,
                             'party invoice net of its related credit note = cash' evidence FROM PR""")
        n_pair = con.execute("SELECT count(*), count(DISTINCT bank_txn_id) FROM PR").fetchall()[0]
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] cnlink: cn_net {n_net[0]} credit notes on {n_net[1]} lines · cn_pair {n_pair[1]} lines "
          f"({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
