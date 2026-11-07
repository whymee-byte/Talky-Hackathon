"""Exception diagnosis on the window: for each golden class, where the misses come from (pair missing / other code, by rule).
    .venv/bin/python explore/exc_diag.py [CLASS ...]"""
import sys
import duckdb
con = duckdb.connect()
con.execute("""CREATE TABLE G AS SELECT bank_txn_id, item_id, coalesce(exception,'') exc, segment, round(amount::DOUBLE*100)::BIGINT amt
               FROM read_csv('data/train/golden_allocations.csv.gz', all_varchar=true)
               WHERE bank_txn_id IN (SELECT bank_txn_id FROM 'work/window/lines.parquet')""")
con.execute("CREATE TABLE A AS SELECT bank_txn_id, item_id, coalesce(exception,'') exc, rule, amount_cents FROM 'work/window/allocations.parquet'")
classes = sys.argv[1:] or ["DUPLICATE_PAYMENT", "OVERPAYMENT", "PARTIAL", "SHORT_BANK_CHARGES", "FX_DIFFERENCE", "UNIDENTIFIED"]
for c in classes:
    print("=====", c)
    print(con.sql(f"""SELECT g.segment, CASE WHEN a.bank_txn_id IS NULL THEN 'MISSING' ELSE a.exc END pred,
                      split_part(coalesce(a.rule,''), '.', 1) || '.' || split_part(coalesce(a.rule,''), '.', 2) rule, count(*) n
                      FROM G g LEFT JOIN A a ON a.bank_txn_id = g.bank_txn_id AND a.item_id IS NOT DISTINCT FROM g.item_id
                      WHERE g.exc = '{c}' AND coalesce(a.exc, '?') <> '{c}' GROUP BY ALL ORDER BY n DESC LIMIT 8""").fetchall())
    print(" FP:", con.sql(f"""SELECT coalesce(g.exc, 'NOPAIR') gold, split_part(a.rule, '.', 1) || '.' || split_part(a.rule, '.', 2) rule, count(*) n
                      FROM A a LEFT JOIN G g ON a.bank_txn_id = g.bank_txn_id AND a.item_id IS NOT DISTINCT FROM g.item_id
                      WHERE a.exc = '{c}' AND coalesce(g.exc, '?') <> '{c}' GROUP BY ALL ORDER BY n DESC LIMIT 6""").fetchall())
