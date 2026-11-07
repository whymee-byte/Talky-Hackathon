"""FIFO patch for receipts left without items although the payer is known (rule T7u.known_party.name / name_text).

    python tools/fifo_patch.py --phase window --measure        # precision / accuracy against the TRAIN golden
    python tools/fifo_patch.py --phase final --out work/final/fifo_patch.parquet --ids work/final/fifo_ids.txt

Candidate line = receipt (IN) whose allocation is a category row with rule T7u.known_party.name* (final) or
T7u.name_text without a T7 category (window analog), T4 rank-1 party with confidence >= MIN_CONF.
Pairs = the party's open, unused ARI invoices in the line currency, issued on or before the booking date, oldest
first (due_date, issue_date), until the cash is exhausted; the invoice that absorbs the remainder gets PARTIAL.
Only lines with 1..MAX_PAIRS pairs and total open >= cash are patched.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MIN_CONF = 0.7
MAX_PAIRS = 6
MAX_OPEN = 40      # parties with more open invoices than this are too ambiguous


def build(con, phase: str, rule_like: str, analog_window: bool) -> None:
    w = f"work/{phase}"
    con.execute(f"CREATE OR REPLACE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute(f"CREATE OR REPLACE TABLE P AS SELECT bank_txn_id, party_id, confidence FROM '{w}/line_party.parquet' WHERE rank = 1")
    extra = ""
    if analog_window:
        con.execute(f"CREATE OR REPLACE TABLE LC AS SELECT DISTINCT bank_txn_id FROM '{w}/line_category.parquet'")
        extra = "AND a.bank_txn_id NOT IN (SELECT bank_txn_id FROM LC)"
    con.execute(f"""CREATE OR REPLACE TABLE CAND AS
        SELECT a.bank_txn_id, l.booking_date, l.currency, abs(l.amount_cents) AS cash, p.party_id, p.confidence
        FROM A a JOIN L l USING (bank_txn_id) JOIN P p USING (bank_txn_id)
        WHERE a.item_id IS NULL AND a.rule LIKE '{rule_like}' AND l.direction = 'IN' AND p.confidence >= {MIN_CONF} {extra}""")
    con.execute("CREATE OR REPLACE TABLE USED AS SELECT DISTINCT item_id FROM A WHERE item_id IS NOT NULL")
    con.execute(f"""CREATE OR REPLACE TABLE OPEN AS
        SELECT c.bank_txn_id, c.cash, i.item_id, i.open_at_start_cents AS open_c, i.due_date, i.issue_date,
               row_number() OVER (PARTITION BY c.bank_txn_id ORDER BY i.due_date NULLS LAST, i.issue_date, i.item_id) AS rn,
               count(*) OVER (PARTITION BY c.bank_txn_id) AS n_open,
               sum(i.open_at_start_cents) OVER (PARTITION BY c.bank_txn_id) AS tot_open
        FROM CAND c JOIN I i ON i.party_id = c.party_id AND i.kind = 'ARI' AND i.currency = c.currency
             AND i.open_at_start_cents > 0 AND i.issue_date <= c.booking_date
        WHERE i.item_id NOT IN (SELECT item_id FROM USED)""")
    con.execute(f"""CREATE OR REPLACE TABLE FIFO AS
        WITH x AS (SELECT *, sum(open_c) OVER (PARTITION BY bank_txn_id ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS cum_before
                   FROM OPEN WHERE n_open <= {MAX_OPEN} AND tot_open >= cash),
             y AS (SELECT *, coalesce(cum_before, 0) AS cb FROM x WHERE coalesce(cum_before, 0) < cash),
             z AS (SELECT bank_txn_id, item_id, least(open_c, cash - cb) AS amount_cents,
                          CASE WHEN least(open_c, cash - cb) < open_c THEN 'PARTIAL' ELSE '' END AS exception,
                          count(*) OVER (PARTITION BY bank_txn_id) AS n_pairs
                   FROM y)
        SELECT * FROM z WHERE n_pairs <= {MAX_PAIRS}""")


def measure(con) -> None:
    con.execute("""CREATE OR REPLACE TABLE G AS
        SELECT bank_txn_id, item_id, round(amount::DOUBLE * 100)::BIGINT AS amt, coalesce(exception, '') AS exc, segment
        FROM 'data_cache/train/golden.parquet'""")
    print(con.sql("""
        WITH f AS (SELECT * FROM FIFO),
             j AS (SELECT f.bank_txn_id, f.item_id, f.amount_cents, f.exception,
                          g.item_id IS NOT NULL AS item_ok,
                          g.item_id IS NOT NULL AND abs(f.amount_cents - g.amt) <= greatest(1, g.amt * 0.005) AS pair_tp,
                          g.item_id IS NOT NULL AND abs(f.amount_cents - g.amt) <= greatest(1, g.amt * 0.005) AND f.exception = g.exc AS row_ok
                   FROM f LEFT JOIN G g ON g.bank_txn_id = f.bank_txn_id AND g.item_id = f.item_id)
        SELECT count(*) AS pairs, count(DISTINCT bank_txn_id) AS lines,
               round(100.0 * avg(item_ok::INT), 1) AS pct_item_in_golden,
               round(100.0 * avg(pair_tp::INT), 1) AS pair_precision,
               round(100.0 * avg(row_ok::INT), 1) AS row_with_exception_ok
        FROM j""").df().to_string(index=False))
    print(con.sql("""
        WITH ours AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amount_cents || '|' || exception)) AS s, count(*) AS n FROM FIFO GROUP BY 1),
             gold AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amt || '|' || exc)) AS s, count(*) AS n, any_value(segment) AS seg
                      FROM G WHERE bank_txn_id IN (SELECT bank_txn_id FROM FIFO) AND item_id IS NOT NULL AND item_id <> '' GROUP BY 1),
             m AS (SELECT o.bank_txn_id, o.n AS ours_n, g.n AS gold_n, g.seg,
                          g.bank_txn_id IS NULL AS gold_no_item,
                          g.bank_txn_id IS NOT NULL AND o.s = g.s AS exact
                   FROM ours o LEFT JOIN gold g USING (bank_txn_id))
        SELECT count(*) AS lines, round(100.0 * avg(exact::INT), 1) AS line_exact_pct, round(100.0 * avg(gold_no_item::INT), 1) AS gold_no_item_pct,
               round(avg(ours_n), 2) AS ours_pairs, round(avg(gold_n), 2) AS gold_pairs
        FROM m""").df().to_string(index=False))
    print(con.sql("""
        WITH ours AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amount_cents || '|' || exception)) AS s, count(*) AS n FROM FIFO GROUP BY 1),
             gold AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amt || '|' || exc)) AS s, count(*) AS n, any_value(segment) AS seg
                      FROM G WHERE bank_txn_id IN (SELECT bank_txn_id FROM FIFO) AND item_id IS NOT NULL AND item_id <> '' GROUP BY 1),
             m AS (SELECT o.bank_txn_id, o.n AS ours_n, coalesce(g.seg, 'unidentified') AS seg, g.bank_txn_id IS NOT NULL AND o.s = g.s AS exact
                   FROM ours o LEFT JOIN gold g USING (bank_txn_id))
        SELECT seg, count(*) AS lines, round(100.0 * avg(exact::INT), 1) AS exact_pct, round(avg(ours_n), 2) AS ours_pairs
        FROM m GROUP BY 1 ORDER BY 2 DESC LIMIT 12""").df().to_string(index=False))
    print(con.sql("""
        WITH ours AS (SELECT bank_txn_id, count(*) AS n FROM FIFO GROUP BY 1),
             gold AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amt || '|' || exc)) AS s FROM G WHERE item_id IS NOT NULL AND item_id <> '' GROUP BY 1),
             o2 AS (SELECT bank_txn_id, list_sort(list(item_id || '|' || amount_cents || '|' || exception)) AS s, count(*) n FROM FIFO GROUP BY 1)
        SELECT o2.n AS ours_pairs, count(*) AS lines, round(100.0 * avg((o2.s = g.s)::INT), 1) AS exact_pct
        FROM o2 LEFT JOIN gold g USING (bank_txn_id) GROUP BY 1 ORDER BY 1""").df().to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--ids")
    ap.add_argument("--max-pairs", type=int, default=6)
    a = ap.parse_args()
    globals()["MAX_PAIRS"] = a.max_pairs
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    if a.phase == "window":
        build(con, "window", "T7u.name_text%", analog_window=True)
    else:
        build(con, a.phase, "T7u.known_party.name%", analog_window=False)
    print("candidate lines:", con.sql("SELECT count(*) FROM CAND").fetchall()[0][0],
          "· with FIFO pairs:", con.sql("SELECT count(DISTINCT bank_txn_id) FROM FIFO").fetchall()[0][0])
    if a.measure:
        measure(con)
    if a.out:
        out = a.out.replace(os.sep, "/")
        con.execute(f"""COPY (
            SELECT a.bank_txn_id, f.item_id, f.amount_cents, NULL::VARCHAR AS category, f.exception,
                   'T23.fifo_known_party' AS rule, 0.5::DOUBLE AS confidence, 'fifo over open unused invoices of T4 party' AS evidence
            FROM FIFO f JOIN (SELECT DISTINCT bank_txn_id FROM A) a USING (bank_txn_id)
        ) TO '{out}' (FORMAT parquet)""")
        print("patch rows ->", out)
    if a.ids:
        ids = [r[0] for r in con.sql("SELECT DISTINCT bank_txn_id FROM FIFO ORDER BY 1").fetchall()]
        with open(a.ids, "w", encoding="utf-8") as fh:
            fh.write("\n".join(ids) + "\n")
        print(f"{len(ids)} ids -> {a.ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
