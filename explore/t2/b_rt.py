"""B: missed ap_returned_transfer / ar_cheque_returned pairs in a date range (diagnosis only)."""
import sys, duckdb
from src.common import phase_dir
seg, lo, hi, tag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
con = duckdb.connect()
g = phase_dir("window") + "/golden_allocations.csv.gz"
con.sql(f"create view l as select * from 'work/window/lines.parquet' where booking_date between '{lo}' and '{hi}'")
con.sql(f"""create table g as select g.bank_txn_id, g.item_id, g.amount, g.exception from read_csv('{g}', all_varchar=true) g
            join l using (bank_txn_id) where g.segment = '{seg}'""")
con.sql(f"create table s as select * from 'work/window/side_links_q1_{tag}.parquet'" if tag != '-' else "create table s as select null bank_txn_id, null item_id where false")
print(con.sql("""select count(*) gold, count(s.item_id) found from g left join s using (bank_txn_id, item_id)"""))
print(con.sql("""select l.booking_date, l.account_id, l.direction, l.amount_cents, l.text_norm, l.batch_refs, g.item_id
  from g left join s using (bank_txn_id, item_id) join l using (bank_txn_id) where s.item_id is null order by 1 limit 30""").fetchall().__repr__().replace("), (", "),\n("))
