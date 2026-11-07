"""Measure T6 remit_links against the golden (window): pair P/R per segment, exception agreement.
    python -m src.remit.eval --phase window
"""
import argparse
import duckdb


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--phase", default="window"); a = ap.parse_args()
    w = f"work/{a.phase}"
    con = duckdb.connect()
    con.execute(f"""CREATE TABLE G AS SELECT g.bank_txn_id, g.item_id, round(g.amount::DOUBLE * 100)::BIGINT amt, coalesce(g.exception, '') exc, g.segment
                    FROM read_csv('data/train/golden_allocations.csv.gz', all_varchar=true) g
                    WHERE bank_txn_id IN (SELECT bank_txn_id FROM '{w}/lines.parquet') AND item_id IS NOT NULL""")
    con.execute(f"CREATE TABLE P AS SELECT * FROM '{w}/remit_links.parquet'")
    con.execute("""CREATE TABLE J AS SELECT p.*, g.amt, g.exc, g.segment, g.item_id IS NOT NULL hit,
                   g.item_id IS NOT NULL AND abs(p.amount_cents - g.amt) <= greatest(1, 0.005 * abs(g.amt)) amt_ok
                   FROM P p LEFT JOIN G g USING (bank_txn_id, item_id)""")
    print(con.sql("""SELECT count(*) pairs, round(avg(hit::INT) * 100, 2) pair_prec, round(avg(amt_ok::INT) * 100, 2) prec_amt,
                     round(avg((exception = exc)::INT) FILTER (WHERE hit) * 100, 2) exc_agree FROM J"""))
    print(con.sql("""SELECT rule, count(*) n, round(avg(hit::INT) * 100, 1) prec, round(avg(amt_ok::INT) * 100, 1) amt_ok,
                     round(avg((exception = exc)::INT) FILTER (WHERE hit) * 100, 1) exc_ok FROM J GROUP BY 1 ORDER BY 2 DESC"""))
    print(con.sql("""SELECT exception pred, exc gold, count(*) n FROM J WHERE hit AND exception <> exc GROUP BY ALL ORDER BY n DESC LIMIT 12"""))
    # recall on the remittance segments (lines the golden marks with these segments)
    print(con.sql("""SELECT g.segment, count(*) gold_pairs, count(p.item_id) found,
                     round(count(p.item_id) * 100.0 / count(*), 1) recall,
                     round(sum((abs(p.amount_cents - g.amt) <= greatest(1, 0.005 * abs(g.amt)))::INT) * 100.0 / count(*), 1) recall_amt
                     FROM G g LEFT JOIN P p USING (bank_txn_id, item_id) GROUP BY 1 HAVING count(p.item_id) > 0 OR g.segment LIKE '%remit%' OR g.segment LIKE '%dispute%'
                     ORDER BY 2 DESC LIMIT 15"""))


if __name__ == "__main__":
    main()
