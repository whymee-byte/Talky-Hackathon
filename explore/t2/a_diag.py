"""Chat A · diagnose confirming: rebuild step_pr internals on Q1 and compare with the golden."""
import duckdb, polars as pl, sys
sys.path.insert(0, ".")
from src.sidefiles import runs
from src.sidefiles.__main__ import glob_csv, detail_dirs

pl.Config.set_tbl_formatting("ASCII_MARKDOWN"); pl.Config.set_fmt_str_lengths(60); pl.Config.set_tbl_width_chars(300)
pl.Config.set_tbl_hide_column_data_types(True); pl.Config.set_tbl_hide_dataframe_shape(True); pl.Config.set_tbl_rows(40)
con = duckdb.connect()
con.sql("create view lines as select * from 'work/window/lines.parquet' where booking_date between '2024-07-01' and '2024-09-30'")
con.sql("create view items as select * from 'work/window/items.parquet'")
q = "quote='\"', escape='\"', delim=','"
con.sql(f"create view suppliers as select * from read_csv('data/masters/suppliers.csv', all_varchar=true, header=true, {q})")
runs.step_pr(con, glob_csv(detail_dirs("window", None), "payment_runs"))
con.sql(f"""create table g as select bank_txn_id, item_id, round(amount::double*100)::bigint ga from read_csv('data/train/golden_allocations.csv.gz', all_varchar=true, {q})
            where segment = 'ap_confirming' and bank_txn_id in (select bank_txn_id from lines)""")
def show(s, n=40): print(con.sql(s).pl().head(n), "\n")
