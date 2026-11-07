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
FILL_MIN_N = 1     # cn_fill: min items already on the line
FILL_MAX_U = 1000  # cn_fill: max candidate units per line
FILL3_MAX_U = 150  # cn_fill with three units: max candidate units per line (0 = off)


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
    n_fill = fill(con, w)
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
    print(f"[{phase}] cnlink: cn_net {n_net[0]} credit notes on {n_net[1]} lines · cn_fill {n_fill[0]} items on "
          f"{n_fill[1]} lines · cn_pair {n_pair[1]} lines "
          f"({time.time() - t0:.0f}s)")


def fill(con, w: str) -> tuple[int, int]:
    """T13.cn_fill: complete an AR line whose cash differs from the open balance of its items with ONE or TWO units
    (unused credit note related to an invoice of the line; unused open invoice of the party; such an invoice with its
    unused related credit notes) whose net open balance is exactly the residual. Unique solution only."""
    con.execute("DROP TABLE IF EXISTS USED")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE OR REPLACE TABLE LF AS
      SELECT a.bank_txn_id, l.cash, l.currency, l.booking_date, any_value(i.party_id) party_id, count(*) n,
             l.cash - sum(CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END) resid,
             list(i.doc_number) FILTER (WHERE i.kind = 'ARI') docs
      FROM A a JOIN L l USING (bank_txn_id) JOIN I i USING (item_id)
      WHERE i.currency = l.currency
      GROUP BY a.bank_txn_id, l.cash, l.currency, l.booking_date
      HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
         AND count(DISTINCT i.party_id) = 1 AND count(DISTINCT a.item_id) = count(*)
         AND bool_and(coalesce(a.exception, '') NOT IN ('RETURNED', 'DUPLICATE_PAYMENT'))
         AND bool_and(i.open_c > 0) AND count(*) >= {FILL_MIN_N}
         AND l.cash <> sum(CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END)""")
    # free items of the parties involved
    con.execute("""CREATE OR REPLACE TABLE FI AS SELECT i.* FROM I i
      WHERE i.open_c > 0 AND i.party_id IN (SELECT party_id FROM LF) AND i.item_id NOT IN (SELECT item_id FROM USED)""")
    con.execute("""CREATE OR REPLACE TABLE FC AS SELECT c.*, v.item_id inv_id FROM FI c
      JOIN I v ON v.kind = 'ARI' AND v.party_id = c.party_id AND v.doc_number = c.related_doc
      WHERE c.kind = 'ARC' AND c.open_c < v.open_c  -- a credit note cancelling the whole invoice is not netted""")
    con.execute("""CREATE OR REPLACE TABLE U AS
      -- credit note of an invoice already on the line
      SELECT lf.bank_txn_id, 'c:' || c.item_id uid, c.item_id key, -c.open_c val, [c.item_id] items, 'cn' t
      FROM LF lf JOIN FC c ON c.party_id = lf.party_id AND c.currency = lf.currency AND c.issue_date <= lf.booking_date
           AND list_contains(lf.docs, c.related_doc)
      UNION ALL
      -- free invoice alone
      SELECT lf.bank_txn_id, 'i:' || v.item_id, v.item_id, v.open_c, [v.item_id], 'inv'
      FROM LF lf JOIN FI v ON v.kind = 'ARI' AND v.party_id = lf.party_id AND v.currency = lf.currency
           AND v.issue_date <= lf.booking_date
      UNION ALL
      -- free invoice net of its free related credit notes
      SELECT lf.bank_txn_id, 'n:' || v.item_id, v.item_id, v.open_c - sum(c.open_c), list_prepend(v.item_id, list(c.item_id)),
             'inv+cn'
      FROM LF lf JOIN FI v ON v.kind = 'ARI' AND v.party_id = lf.party_id AND v.currency = lf.currency
           AND v.issue_date <= lf.booking_date
           JOIN FC c ON c.inv_id = v.item_id AND c.issue_date <= lf.booking_date
      GROUP BY lf.bank_txn_id, v.item_id, v.open_c""")
    # parties with too many free items: the search is neither cheap nor discriminating
    con.execute(f"DELETE FROM U WHERE bank_txn_id IN (SELECT bank_txn_id FROM U GROUP BY 1 HAVING count(*) > {FILL_MAX_U})")
    con.execute("""CREATE OR REPLACE TABLE SOL AS
      WITH s1 AS (SELECT u.bank_txn_id, 1 k, u.items, u.t FROM U u JOIN LF lf USING (bank_txn_id) WHERE u.val = lf.resid),
           s2 AS (SELECT a.bank_txn_id, 2 k, list_concat(a.items, b.items) items, a.t || '&' || b.t t
                  FROM U a JOIN LF lf USING (bank_txn_id)
                       JOIN U b ON b.bank_txn_id = a.bank_txn_id AND b.val = lf.resid - a.val AND a.uid < b.uid
                            AND a.key <> b.key),
           s AS (SELECT * FROM s1 UNION ALL SELECT * FROM s2 WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM s1))
      SELECT * FROM s""")
    if FILL3_MAX_U:
        # three units, only on lines with few candidates and no 1/2-unit solution
        con.execute(f"""CREATE OR REPLACE TABLE U3 AS SELECT * FROM U WHERE bank_txn_id IN (SELECT bank_txn_id FROM U
                         WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM SOL) GROUP BY 1 HAVING count(*) <= {FILL3_MAX_U})""")
        con.execute("""INSERT INTO SOL
          WITH p AS (SELECT a.bank_txn_id, a.uid u1, b.uid u2, a.key k1, b.key k2, a.val + b.val val,
                            list_concat(a.items, b.items) items, a.t || '&' || b.t t
                     FROM U3 a JOIN U3 b ON b.bank_txn_id = a.bank_txn_id AND a.uid < b.uid AND a.key <> b.key)
          SELECT p.bank_txn_id, 3 k, list_concat(p.items, c.items), p.t || '&' || c.t
          FROM p JOIN LF lf USING (bank_txn_id)
               JOIN U3 c ON c.bank_txn_id = p.bank_txn_id AND c.val = lf.resid - p.val AND p.u2 < c.uid
                    AND c.key <> p.k1 AND c.key <> p.k2""")
    con.execute("DELETE FROM SOL WHERE bank_txn_id IN (SELECT bank_txn_id FROM SOL GROUP BY 1 HAVING count(*) > 1)")
    con.execute("""CREATE OR REPLACE TABLE FILL AS
      SELECT s.bank_txn_id, x.item_id, i.open_c, s.k, s.t FROM SOL s, unnest(s.items) x(item_id) JOIN I i USING (item_id)
      QUALIFY count(*) OVER (PARTITION BY x.item_id) = 1""")
    # drop lines where an item was claimed by two lines (partial solution would not fit)
    con.execute("""DELETE FROM FILL WHERE bank_txn_id IN (SELECT s.bank_txn_id FROM SOL s
                   WHERE len(s.items) <> (SELECT count(*) FROM FILL f WHERE f.bank_txn_id = s.bank_txn_id))""")
    lines = "SELECT DISTINCT bank_txn_id FROM FILL"
    con.execute(f"""UPDATE A SET amount_cents = i.open_c, exception = NULL, rule = A.rule || '+T13.cn_fill'
                    FROM I i WHERE A.item_id = i.item_id AND A.bank_txn_id IN ({lines})""")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
                     NULL::VARCHAR exception, 'T13.cn_fill' rule, CASE WHEN k = 1 THEN 0.8 WHEN k = 2 THEN 0.7 ELSE 0.6 END confidence,
                     'residual cash = net open of ' || t || ' (unique, exact cents)' evidence FROM FILL""")
    return con.execute("SELECT count(*), count(DISTINCT bank_txn_id) FROM FILL").fetchall()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
