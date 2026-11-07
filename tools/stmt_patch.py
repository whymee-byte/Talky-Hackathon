"""Statement-payment patch: receipts without items whose T4 party's open unused invoices (all of them, or all of one
issue month) sum exactly to the cash -> pair them all, full amounts, no exception.

    python tools/stmt_patch.py --phase window --measure
    python tools/stmt_patch.py --phase final --out work/final/stmt_patch.parquet --ids work/final/stmt_ids.txt

Window measurement uses balances at the line date computed from the golden (fair estimate of what the live balance
tracking gives in FINAL); FINAL uses open_at_start minus every item our own allocations already used.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MIN_CONF = 0.5
MIN_ITEMS = 2
MAX_ITEMS = 60


def build(con, phase: str) -> None:
    w = f"work/{phase}"
    con.execute(f"CREATE OR REPLACE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE P AS SELECT bank_txn_id, party_id, confidence FROM '{w}/line_party.parquet' WHERE rank = 1")
    con.execute(f"""CREATE OR REPLACE TABLE CAND AS
        SELECT l.bank_txn_id, l.booking_date, l.currency, abs(l.amount_cents) AS cash, p.party_id, p.confidence
        FROM L l JOIN (SELECT bank_txn_id, count(item_id) AS n FROM A GROUP BY 1) a USING (bank_txn_id) JOIN P p USING (bank_txn_id)
        WHERE a.n = 0 AND l.direction = 'IN' AND p.confidence >= {MIN_CONF}""")
    if phase == "window":
        con.execute("""CREATE OR REPLACE TABLE GP AS
            SELECT g.item_id, l.booking_date, CASE WHEN g.exception = 'RETURNED' THEN -1 ELSE 1 END * round(g.amount::DOUBLE * 100)::BIGINT AS amt
            FROM 'data_cache/train/golden.parquet' g JOIN 'work/window/lines.parquet' l USING (bank_txn_id)
            WHERE g.item_id IS NOT NULL AND g.item_id <> ''""")
        con.execute("""CREATE OR REPLACE TABLE OPEN AS
            SELECT c.bank_txn_id, c.booking_date, c.cash, i.item_id, i.issue_date, i.due_date,
                   i.open_at_start_cents - coalesce((SELECT sum(amt) FROM GP WHERE GP.item_id = i.item_id AND GP.booking_date < c.booking_date), 0) AS open_c
            FROM CAND c JOIN I i ON i.party_id = c.party_id AND i.kind = 'ARI' AND i.currency = c.currency AND i.issue_date <= c.booking_date
            WHERE i.open_at_start_cents > 0""")
        con.execute("DELETE FROM OPEN WHERE open_c <= 0")
    else:
        con.execute("CREATE OR REPLACE TABLE USED AS SELECT DISTINCT item_id FROM A WHERE item_id IS NOT NULL")
        con.execute("""CREATE OR REPLACE TABLE OPEN AS
            SELECT c.bank_txn_id, c.booking_date, c.cash, i.item_id, i.issue_date, i.due_date, i.open_at_start_cents AS open_c
            FROM CAND c JOIN I i ON i.party_id = c.party_id AND i.kind = 'ARI' AND i.currency = c.currency AND i.issue_date <= c.booking_date
            WHERE i.open_at_start_cents > 0 AND i.item_id NOT IN (SELECT item_id FROM USED)""")
    # rule ALL: every open invoice sums to the cash
    con.execute(f"""CREATE OR REPLACE TABLE R_ALL AS
        SELECT bank_txn_id, item_id, open_c AS amount_cents, 'all' AS how
        FROM (SELECT *, sum(open_c) OVER (PARTITION BY bank_txn_id) AS tot, count(*) OVER (PARTITION BY bank_txn_id) AS n FROM OPEN)
        WHERE abs(tot - cash) <= greatest(1, cash * 0.005) AND n BETWEEN {MIN_ITEMS} AND {MAX_ITEMS}""")
    # rule MONTH: the invoices of exactly one issue month sum to the cash (lines not solved by ALL)
    con.execute(f"""CREATE OR REPLACE TABLE R_MON AS
        WITH m AS (SELECT bank_txn_id, strftime(issue_date, '%Y-%m') AS mon, sum(open_c) AS tot, count(*) AS n, any_value(cash) AS cash
                   FROM OPEN WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM R_ALL) GROUP BY 1, 2),
             ok AS (SELECT bank_txn_id, mon FROM m WHERE abs(tot - cash) <= greatest(1, cash * 0.005) AND n BETWEEN {MIN_ITEMS} AND {MAX_ITEMS}
                    QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1)
        SELECT o.bank_txn_id, o.item_id, o.open_c AS amount_cents, 'month' AS how
        FROM OPEN o JOIN ok ON ok.bank_txn_id = o.bank_txn_id AND strftime(o.issue_date, '%Y-%m') = ok.mon""")
    # rule DUE: invoices due on or before the booking date sum to the cash
    con.execute(f"""CREATE OR REPLACE TABLE R_DUE AS
        SELECT bank_txn_id, item_id, open_c AS amount_cents, 'due' AS how
        FROM (SELECT *, sum(open_c) OVER (PARTITION BY bank_txn_id) AS tot, count(*) OVER (PARTITION BY bank_txn_id) AS n
              FROM OPEN WHERE due_date IS NOT NULL AND due_date <= booking_date
                AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_ALL) AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_MON))
        WHERE abs(tot - cash) <= greatest(1, cash * 0.005) AND n BETWEEN {MIN_ITEMS} AND {MAX_ITEMS}""")
    # rule FIFO: the k oldest open invoices (issue date) sum exactly to the cash, for some k >= MIN_ITEMS
    con.execute(f"""CREATE OR REPLACE TABLE R_FIFO AS
        WITH o AS (SELECT *, row_number() OVER (PARTITION BY bank_txn_id ORDER BY issue_date, due_date NULLS LAST, item_id) AS rn,
                          sum(open_c) OVER (PARTITION BY bank_txn_id ORDER BY issue_date, due_date NULLS LAST, item_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum
                   FROM OPEN WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM R_ALL) AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_MON)
                     AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_DUE)),
             k AS (SELECT bank_txn_id, min(rn) AS k FROM o WHERE abs(cum - cash) <= greatest(1, cash * 0.005) AND rn BETWEEN {MIN_ITEMS} AND {MAX_ITEMS} GROUP BY 1)
        SELECT o.bank_txn_id, o.item_id, o.open_c AS amount_cents, 'fifo' AS how FROM o JOIN k USING (bank_txn_id) WHERE o.rn <= k.k""")
    # rule DUEFIFO: same, ordered by due date
    con.execute(f"""CREATE OR REPLACE TABLE R_DFIFO AS
        WITH o AS (SELECT *, row_number() OVER (PARTITION BY bank_txn_id ORDER BY due_date NULLS LAST, issue_date, item_id) AS rn,
                          sum(open_c) OVER (PARTITION BY bank_txn_id ORDER BY due_date NULLS LAST, issue_date, item_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum
                   FROM OPEN WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM R_ALL) AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_MON)
                     AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_DUE) AND bank_txn_id NOT IN (SELECT bank_txn_id FROM R_FIFO)),
             k AS (SELECT bank_txn_id, min(rn) AS k FROM o WHERE abs(cum - cash) <= greatest(1, cash * 0.005) AND rn BETWEEN {MIN_ITEMS} AND {MAX_ITEMS} GROUP BY 1)
        SELECT o.bank_txn_id, o.item_id, o.open_c AS amount_cents, 'dfifo' AS how FROM o JOIN k USING (bank_txn_id) WHERE o.rn <= k.k""")
    con.execute("CREATE OR REPLACE TABLE R AS SELECT * FROM R_ALL UNION ALL SELECT * FROM R_MON UNION ALL SELECT * FROM R_DUE UNION ALL SELECT * FROM R_FIFO UNION ALL SELECT * FROM R_DFIFO")


def measure(con) -> None:
    con.execute("""CREATE OR REPLACE TABLE G AS
        SELECT bank_txn_id, item_id, round(amount::DOUBLE * 100)::BIGINT AS amt, coalesce(exception, '') AS exc, segment
        FROM 'data_cache/train/golden.parquet' WHERE bank_txn_id IN (SELECT bank_txn_id FROM CAND)""")
    print(con.sql("""
        SELECT r.how, count(*) AS pairs, count(DISTINCT r.bank_txn_id) AS lines,
               round(100.0 * avg((g.item_id IS NOT NULL)::INT), 1) AS pct_item_in_golden,
               round(100.0 * avg((g.item_id IS NOT NULL AND abs(r.amount_cents - g.amt) <= greatest(1, g.amt * 0.005) AND g.exc = '')::INT), 1) AS row_precision
        FROM R r LEFT JOIN G g ON g.bank_txn_id = r.bank_txn_id AND g.item_id = r.item_id GROUP BY 1""").df().to_string(index=False))
    print(con.sql("""
        WITH ours AS (SELECT bank_txn_id, any_value(how) how, list_sort(list(item_id || '|' || amount_cents::BIGINT)) AS s FROM R GROUP BY 1),
             gold AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amt)) AS s, any_value(segment) seg, bool_and(exc = '') no_exc
                      FROM G WHERE item_id IS NOT NULL AND item_id <> '' GROUP BY 1)
        SELECT o.how, coalesce(g.seg, 'no_item') AS seg, count(*) AS lines, round(100.0 * avg((o.s = g.s AND g.no_exc)::INT), 1) AS line_exact_pct
        FROM ours o LEFT JOIN gold g USING (bank_txn_id) GROUP BY 1, 2 ORDER BY 1, 3 DESC""").df().to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--ids")
    ap.add_argument("--rules", default="all,month,due")
    a = ap.parse_args()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    build(con, a.phase)
    keep = [x.strip() for x in a.rules.split(",")]
    con.execute(f"DELETE FROM R WHERE how NOT IN ({', '.join(repr(k) for k in keep)})")
    print("candidate lines:", con.sql("SELECT count(*) FROM CAND").fetchall()[0][0],
          "· patched lines:", con.sql("SELECT count(DISTINCT bank_txn_id) FROM R").fetchall()[0][0],
          "· pairs:", con.sql("SELECT count(*) FROM R").fetchall()[0][0])
    if a.measure:
        measure(con)
    if a.out:
        out = a.out.replace(os.sep, "/")
        con.execute(f"""COPY (SELECT bank_txn_id, item_id, amount_cents::BIGINT AS amount_cents, NULL::VARCHAR AS category, '' AS exception,
                                     'T25.statement_' || how AS rule, 0.6::DOUBLE AS confidence, 'open unused invoices of T4 party sum to cash (' || how || ')' AS evidence
                              FROM R) TO '{out}' (FORMAT parquet)""")
        print("patch rows ->", out)
    if a.ids:
        ids = [r[0] for r in con.sql("SELECT DISTINCT bank_txn_id FROM R ORDER BY 1").fetchall()]
        with open(a.ids, "w", encoding="utf-8") as fh:
            fh.write("\n".join(ids) + "\n")
        print(f"{len(ids)} ids -> {a.ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
