"""Where do window points go? Exception confusion (pair level) and per-segment line failure reasons.

    python tools/analysis/gap.py out/window_<...>.csv.gz [--seg ar_ref_noise] [--exc OVERPAYMENT]
"""
from __future__ import annotations

import argparse

import duckdb

G = "data_cache/window/golden_allocations.csv.gz"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sub")
    ap.add_argument("--seg")
    ap.add_argument("--exc")
    a = ap.parse_args()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE g AS SELECT bank_txn_id, item_id, round(amount*100)::BIGINT amt, category, coalesce(exception,'') exc, segment FROM read_csv('{G}', all_varchar=false)")
    con.execute(f"CREATE TABLE s AS SELECT bank_txn_id, item_id, round(amount*100)::BIGINT amt, category, coalesce(exception,'') exc FROM read_csv('{a.sub}', header=true, columns={{'bank_txn_id':'VARCHAR','item_id':'VARCHAR','amount':'DOUBLE','category':'VARCHAR','exception':'VARCHAR'}})")
    con.execute("CREATE TABLE seg AS SELECT bank_txn_id, any_value(segment) segment FROM g GROUP BY 1")
    # exception confusion on pairs (gold pairs with item vs submitted pair)
    con.execute("""CREATE TABLE pj AS SELECT coalesce(g.bank_txn_id, s.bank_txn_id) bank_txn_id, coalesce(g.item_id, s.item_id) item_id,
                   g.exc gexc, s.exc sexc, g.amt gamt, s.amt samt, g.item_id IS NOT NULL ing, s.item_id IS NOT NULL ins
                   FROM (SELECT * FROM g WHERE item_id IS NOT NULL) g FULL JOIN (SELECT * FROM s WHERE item_id IS NOT NULL) s
                   USING (bank_txn_id, item_id)""")
    print("== exception confusion on pairs (gold exc -> sub exc; '-' = pair missing / extra)")
    q = """SELECT CASE WHEN ing THEN gexc ELSE '-' END g, CASE WHEN ins THEN sexc ELSE '-' END s, count(*) n FROM pj
           WHERE coalesce(gexc,'') <> coalesce(sexc,'') OR NOT ing OR NOT ins GROUP BY ALL HAVING g <> '' OR s <> '' ORDER BY n DESC LIMIT 45"""
    for r in con.sql(q).fetchall():
        print(f"  {r[0]:20s} -> {r[1]:20s} {r[2]:8d}")
    # line level failure reasons by segment
    con.execute("""CREATE TABLE lg AS SELECT bank_txn_id, count(item_id) ni, any_value(category) cat, any_value(exc) gexc1 FROM g GROUP BY 1""")
    con.execute("""CREATE TABLE ls AS SELECT bank_txn_id, count(item_id) ni, any_value(category) cat, any_value(exc) sexc1 FROM s GROUP BY 1""")
    con.execute("""CREATE TABLE lp AS SELECT bank_txn_id,
          count(*) FILTER (WHERE ing AND ins) both_, count(*) FILTER (WHERE ing AND NOT ins) miss, count(*) FILTER (WHERE ins AND NOT ing) extra,
          count(*) FILTER (WHERE ing AND ins AND abs(gamt - samt) > greatest(1, 0.005*abs(gamt))) badamt,
          count(*) FILTER (WHERE ing AND ins AND gexc <> sexc) badexc FROM pj GROUP BY 1""")
    con.execute("""CREATE TABLE L AS SELECT seg.segment, lg.bank_txn_id, lg.ni gni, coalesce(ls.ni,0) sni,
          coalesce(lp.both_,0) both_, coalesce(lp.miss,0) miss, coalesce(lp.extra,0) extra, coalesce(lp.badamt,0) badamt, coalesce(lp.badexc,0) badexc,
          lg.cat gcat, ls.cat scat, lg.gexc1, ls.sexc1
          FROM lg JOIN seg USING (bank_txn_id) LEFT JOIN ls USING (bank_txn_id) LEFT JOIN lp USING (bank_txn_id)""")
    con.execute("""CREATE TABLE LR AS SELECT *, CASE
          WHEN gni = 0 AND sni = 0 AND gcat = scat AND gexc1 = sexc1 THEN 'ok'
          WHEN gni = 0 AND sni = 0 THEN 'cat/exc'
          WHEN gni = 0 THEN 'items_on_noitem'
          WHEN sni = 0 THEN 'no_items'
          WHEN both_ = 0 THEN 'all_wrong_items'
          WHEN miss > 0 AND extra > 0 THEN 'some_wrong_items'
          WHEN miss > 0 THEN 'missing_items'
          WHEN extra > 0 THEN 'extra_items'
          WHEN badamt > 0 THEN 'amount'
          WHEN badexc > 0 THEN 'exception'
          ELSE 'ok' END why FROM L""")
    print("\n== failed lines by segment and reason (top)")
    q = "SELECT segment, why, count(*) n FROM LR WHERE why <> 'ok' GROUP BY ALL ORDER BY n DESC LIMIT 50"
    for r in con.sql(q).fetchall():
        print(f"  {r[0]:28s} {r[1]:18s} {r[2]:7d}")
    print("\n== failed lines by reason:", con.sql("SELECT why, count(*) FROM LR GROUP BY 1 ORDER BY 2 DESC").fetchall())
    if a.seg:
        print(f"\n== {a.seg}: exception mismatches on matched pairs")
        q = f"""SELECT gexc, sexc, count(*) FROM pj JOIN seg USING (bank_txn_id) WHERE segment = '{a.seg}' AND ing AND ins AND gexc <> sexc GROUP BY ALL ORDER BY 3 DESC LIMIT 15"""
        for r in con.sql(q).fetchall():
            print("  ", r)
        con.execute(f"COPY (SELECT * FROM LR WHERE segment = '{a.seg}' AND why <> 'ok') TO 'work/gap_{a.seg}.parquet'")
    con.execute("COPY (SELECT pj.*, seg.segment FROM pj JOIN seg USING (bank_txn_id)) TO 'work/gap_pairs.parquet'")
    con.execute("COPY LR TO 'work/gap_lines.parquet'")


if __name__ == "__main__":
    main()
