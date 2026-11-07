"""T3u · learned UNIDENTIFIED exception for receipts that ended with no item and category UNIDENTIFIED.

    python -m src.tolmatch.unid learn --lo 2024-07-01 --hi 2024-09-30     # learn on window lines in [lo, hi]
    python -m src.tolmatch.unid apply --phase window|live|final           # last step: set/clear the exception

The table (src/tolmatch/unid_table.csv, versioned so FINAL never depends on a previous window run) holds
P(golden UNIDENTIFIED | features) learned from TRAIN golden. Features are generic (no ids, names or amounts):
  rnd   cash is a whole multiple of 100 currency units (cash deposits / round transfers)
  nob   open AR invoices of the T4 rank-1 party issued before the line: 0 / 1-3 / 4-10 / >10
  meth  T4 evidence family of rank 1 (iban, mandate, doc, learned, master, fuzzy, payrun, amount, none)
  cb    T4 rank-1 confidence bucket: <0.4 / 0.4-0.8 / >=0.8
Only ADDS the flag to lines left without it (never clears one). A fine cell needs >= MIN_N training lines, else the coarse cell (rnd, nob) is used. Flag iff p >= THETA (~F1/2).
"""
import argparse
import os

import duckdb

TABLE = os.path.join(os.path.dirname(__file__), "unid_table.csv")
MIN_N = 30
THETA = 0.45


def features(con, w):
    con.execute(f"""CREATE OR REPLACE TABLE UF AS
      WITH cand AS (SELECT a.bank_txn_id FROM '{w}/allocations.parquet' a GROUP BY 1
                    HAVING count(item_id) = 0 AND count(*) = 1 AND any_value(a.category) = 'UNIDENTIFIED'
                       AND coalesce(any_value(a.exception), '') = ''),   -- only lines the pipeline left without the flag
           p AS (SELECT bank_txn_id, party_id, confidence, rule FROM '{w}/line_party.parquet' WHERE rank = 1),
           x AS (SELECT c.bank_txn_id, l.booking_date bd, abs(l.amount_cents) cash, p.party_id, coalesce(p.confidence, 0) pc,
                        coalesce(split_part(p.rule, '_', 1), 'none') meth
                 FROM cand c JOIN '{w}/lines.parquet' l USING (bank_txn_id) LEFT JOIN p USING (bank_txn_id)
                 WHERE l.direction = 'IN'),
           o AS (SELECT x.bank_txn_id, count(i.item_id) n_open FROM x LEFT JOIN '{w}/items.parquet' i
                   ON i.party_id = x.party_id AND i.kind = 'ARI' AND i.open_at_start_cents > 0 AND i.issue_date <= x.bd
                 GROUP BY 1)
      SELECT x.bank_txn_id, (x.cash % 10000 = 0) AS rnd,
             CASE WHEN o.n_open = 0 THEN '0' WHEN o.n_open <= 3 THEN '1-3' WHEN o.n_open <= 10 THEN '4-10' ELSE '>10' END nob,
             x.meth, CASE WHEN x.pc < 0.4 THEN 'lo' WHEN x.pc < 0.8 THEN 'mid' ELSE 'hi' END cb
      FROM x JOIN o USING (bank_txn_id)""")


def learn(lo, hi, w="work/window"):
    con = duckdb.connect()
    features(con, w)
    con.execute(f"""CREATE TABLE LBL AS
      SELECT f.*, coalesce(bool_or(g.exception = 'UNIDENTIFIED'), false) y
      FROM UF f JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      JOIN 'data_cache/train/golden_allocations.parquet' g USING (bank_txn_id)
      WHERE l.booking_date BETWEEN DATE '{lo}' AND DATE '{hi}' GROUP BY ALL""")
    con.execute(f"""COPY (
      SELECT 'fine' lvl, rnd, nob, meth, cb, count(*) n, avg(y::int) p FROM LBL GROUP BY ALL HAVING count(*) >= {MIN_N}
      UNION ALL
      SELECT 'coarse', rnd, nob, NULL, NULL, count(*), avg(y::int) FROM LBL GROUP BY ALL HAVING count(*) >= {MIN_N}
    ) TO '{TABLE}' (HEADER)""")
    print(f"learned {TABLE} from {con.sql('SELECT count(*) FROM LBL').fetchall()[0][0]} lines in [{lo}, {hi}]")


def apply(phase):
    w = f"work/{phase}"
    if not os.path.exists(TABLE):
        print(f"[{phase}] T3u: no {TABLE}, skipped")
        return
    con = duckdb.connect()
    features(con, w)
    con.execute(f"CREATE TABLE T AS SELECT * FROM read_csv('{TABLE}', header=true)")
    con.execute(f"""CREATE TABLE D AS
      SELECT f.bank_txn_id, coalesce(fi.p, co.p) p FROM UF f
      LEFT JOIN T fi ON fi.lvl = 'fine' AND fi.rnd = f.rnd AND fi.nob = f.nob AND fi.meth = f.meth AND fi.cb = f.cb
      LEFT JOIN T co ON co.lvl = 'coarse' AND co.rnd = f.rnd AND co.nob = f.nob""")
    con.execute(f"""COPY (
      SELECT a.* REPLACE (
        CASE WHEN d.p >= {THETA} THEN 'UNIDENTIFIED' ELSE a.exception END AS exception,
        CASE WHEN d.p >= {THETA} THEN coalesce(a.rule, '') || '+T3u' ELSE a.rule END AS rule,
        CASE WHEN d.p >= {THETA} THEN coalesce(a.evidence, '') || '; P(unidentified)=' || round(d.p, 3) ELSE a.evidence END AS evidence)
      FROM '{w}/allocations.parquet' a LEFT JOIN D d USING (bank_txn_id)
    ) TO '{w}/allocations_t3u.parquet' (FORMAT parquet)""")
    os.replace(f"{w}/allocations_t3u.parquet", f"{w}/allocations.parquet")
    print(f"[{phase}] T3u:", con.sql(f"SELECT count(*) FILTER (WHERE p >= {THETA}) flagged, count(*) FILTER (WHERE p < {THETA}) cleared, "
                                     "count(*) FILTER (WHERE p IS NULL) no_cell FROM D").fetchall())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["learn", "apply"])
    ap.add_argument("--phase", default="window")
    ap.add_argument("--lo", default="2024-07-01")
    ap.add_argument("--hi", default="2025-06-30")
    a = ap.parse_args()
    learn(a.lo, a.hi) if a.cmd == "learn" else apply(a.phase)
