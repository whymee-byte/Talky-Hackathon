"""FX patch: receipts in another currency (orig_currency <> account currency) left without items although the T4 party
is known, whose party has open unused ARI invoices in the ORIGINAL currency summing exactly to orig_amount.

    python tools/fx_patch.py --phase window --measure
    python tools/fx_patch.py --phase final --out work/final/fx_patch.parquet --ids work/final/fx_ids.txt

Pairs: single invoice with open == orig_amount (if several with the same amount: the oldest due = FIFO convention),
else a unique pair of invoices summing to orig_amount. Exception FX_DIFFERENCE, amount = full open (POLICIES: full
clearing in the invoice currency).
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MIN_CONF = 0.5


def build(con, phase: str) -> None:
    w = f"work/{phase}"
    con.execute(f"CREATE OR REPLACE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE P AS SELECT bank_txn_id, party_id, confidence FROM '{w}/line_party.parquet' WHERE rank = 1")
    con.execute(f"""CREATE OR REPLACE TABLE CAND AS
        SELECT l.bank_txn_id, l.booking_date, l.currency, l.orig_currency, l.orig_amount_cents AS oa, abs(l.amount_cents) AS cash, p.party_id, p.confidence
        FROM L l JOIN (SELECT bank_txn_id, count(item_id) AS n FROM A GROUP BY 1) a USING (bank_txn_id) JOIN P p USING (bank_txn_id)
        WHERE a.n = 0 AND l.direction = 'IN' AND l.orig_currency IS NOT NULL AND l.orig_currency <> l.currency
          AND l.orig_amount_cents IS NOT NULL AND l.orig_amount_cents > 0 AND p.confidence >= {MIN_CONF}""")
    con.execute("CREATE OR REPLACE TABLE USED AS SELECT DISTINCT item_id FROM A WHERE item_id IS NOT NULL")
    con.execute("""CREATE OR REPLACE TABLE OPEN AS
        SELECT c.bank_txn_id, c.oa, i.item_id, i.open_at_start_cents AS open_c, i.due_date, i.issue_date
        FROM CAND c JOIN I i ON i.party_id = c.party_id AND i.kind = 'ARI' AND i.currency = c.orig_currency
             AND i.open_at_start_cents > 0 AND i.issue_date <= c.booking_date
        WHERE i.item_id NOT IN (SELECT item_id FROM USED)""")
    # single exact (FIFO among equal amounts)
    con.execute("""CREATE OR REPLACE TABLE S1 AS
        SELECT bank_txn_id, item_id, open_c AS amount_cents, 'single' AS how
        FROM (SELECT *, row_number() OVER (PARTITION BY bank_txn_id ORDER BY due_date NULLS LAST, issue_date, item_id) AS rn
              FROM OPEN WHERE abs(open_c - oa) <= greatest(1, oa * 0.005))
        WHERE rn = 1""")
    # unique pair summing to oa (lines without a single match)
    con.execute("""CREATE OR REPLACE TABLE S2 AS
        WITH o AS (SELECT * FROM OPEN WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM S1)),
             pr AS (SELECT a.bank_txn_id, a.item_id AS i1, b.item_id AS i2, a.open_c AS c1, b.open_c AS c2
                    FROM o a JOIN o b ON a.bank_txn_id = b.bank_txn_id AND a.item_id < b.item_id
                    WHERE abs(a.open_c + b.open_c - a.oa) <= greatest(1, a.oa * 0.005)),
             u AS (SELECT bank_txn_id, any_value(i1) i1, any_value(i2) i2, any_value(c1) c1, any_value(c2) c2 FROM pr GROUP BY 1 HAVING count(*) = 1)
        SELECT bank_txn_id, i1 AS item_id, c1 AS amount_cents, 'pair' AS how FROM u
        UNION ALL SELECT bank_txn_id, i2, c2, 'pair' FROM u""")
    con.execute("CREATE OR REPLACE TABLE FX AS SELECT *, 'FX_DIFFERENCE' AS exception FROM S1 UNION ALL SELECT *, 'FX_DIFFERENCE' FROM S2")


def measure(con) -> None:
    con.execute("""CREATE OR REPLACE TABLE G AS
        SELECT bank_txn_id, item_id, round(amount::DOUBLE * 100)::BIGINT AS amt, coalesce(exception, '') AS exc, segment
        FROM 'data_cache/train/golden.parquet'""")
    print(con.sql("""
        SELECT f.how, count(*) AS pairs, count(DISTINCT f.bank_txn_id) AS lines,
               round(100.0 * avg((g.item_id IS NOT NULL)::INT), 1) AS pct_item_in_golden,
               round(100.0 * avg((g.item_id IS NOT NULL AND abs(f.amount_cents - g.amt) <= greatest(1, g.amt * 0.005))::INT), 1) AS pair_precision,
               round(100.0 * avg((g.item_id IS NOT NULL AND abs(f.amount_cents - g.amt) <= greatest(1, g.amt * 0.005) AND g.exc = 'FX_DIFFERENCE')::INT), 1) AS row_ok
        FROM FX f LEFT JOIN G g ON g.bank_txn_id = f.bank_txn_id AND g.item_id = f.item_id GROUP BY 1""").df().to_string(index=False))
    print(con.sql("""
        WITH ours AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amount_cents || '|' || exception)) AS s FROM FX GROUP BY 1),
             gold AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amt || '|' || exc)) AS s, any_value(segment) seg
                      FROM G WHERE bank_txn_id IN (SELECT bank_txn_id FROM FX) AND item_id IS NOT NULL AND item_id <> '' GROUP BY 1)
        SELECT coalesce(g.seg, 'no_item') AS seg, count(*) AS lines, round(100.0 * avg((o.s = g.s)::INT), 1) AS line_exact_pct
        FROM ours o LEFT JOIN gold g USING (bank_txn_id) GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--ids")
    a = ap.parse_args()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    build(con, a.phase)
    print("candidate lines:", con.sql("SELECT count(*) FROM CAND").fetchall()[0][0],
          "· patched lines:", con.sql("SELECT count(DISTINCT bank_txn_id) FROM FX").fetchall()[0][0],
          "· pairs:", con.sql("SELECT count(*) FROM FX").fetchall()[0][0])
    if a.measure:
        measure(con)
    if a.out:
        out = a.out.replace(os.sep, "/")
        con.execute(f"""COPY (SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR AS category, exception,
                                     'T24.fx_orig_amount' AS rule, 0.6::DOUBLE AS confidence, 'orig_amount = open invoice(s) of T4 party in orig currency (' || how || ')' AS evidence
                              FROM FX) TO '{out}' (FORMAT parquet)""")
        print("patch rows ->", out)
    if a.ids:
        ids = [r[0] for r in con.sql("SELECT DISTINCT bank_txn_id FROM FX ORDER BY 1").fetchall()]
        with open(a.ids, "w", encoding="utf-8") as fh:
            fh.write("\n".join(ids) + "\n")
        print(f"{len(ids)} ids -> {a.ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
