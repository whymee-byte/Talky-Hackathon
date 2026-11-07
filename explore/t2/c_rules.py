# Chat C: compare tiebreak rules. Densities learned on TRAIN < 2024-07; evaluated in-sample (pre) and out-of-sample (window, by quarter).
import duckdb, polars as pl
pl.Config.set_tbl_formatting("ASCII_MARKDOWN"); pl.Config.set_tbl_rows(80); pl.Config.set_tbl_hide_column_data_types(True); pl.Config.set_tbl_hide_dataframe_shape(True); pl.Config.set_tbl_width_chars(300)
con = duckdb.connect(); T = "data/train"
def q(s, n=80): print(con.sql(s).pl().head(n), "\n")
N = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"
con.sql(f"""create table r as select b.bank_txn_id, b.booking_date::date booking_date, round(b.amount::double*100)::bigint amount_cents, g.item_id,
  regexp_extract(upper(concat_ws(' ', text, ref1, ref2, b.remittance_info)), 'VTM-[A-Z]{{2}}-[0-9]{{7}}') mandate,
  coalesce(nullif(regexp_extract(' ' || {N.format(x="concat_ws(' ', text, ref1, ref2, b.remittance_info)")} || ' ', ' ((?:AC|AM|MD|MS|SL|BE|AG|RR|RC|FF|DT|CN|ED|FO|MI|NO|PY|RF|TM)[0-9]{{2}}) ', 1), ''), '?') code
  from read_csv('{T}/bank_transactions.csv.gz', all_varchar=true) b join read_csv('{T}/golden_allocations.csv.gz', all_varchar=true) g using (bank_txn_id)
  where g.segment = 'ar_dd_returned' and b.amount::double < 0""")
con.sql(f"""create table dd as select distinct batch_id, collection_date::date cd, mandate_id, round(amount::double*100)::bigint amt, re_presentation rp,
   {N.format(x='remittance_info')} rnorm from read_csv('{T}/direct_debit_batches/[!.]*.csv', all_varchar=true, union_by_name=true)""")
con.sql("create view it as select item_id, doc_norm from 'work/window/items.parquet' where side='AR'")
con.sql("""create table c as select r.bank_txn_id, r.code, r.code='MD06' md06, r.booking_date, isodow(r.booking_date) dow, d.batch_id || '|' || d.mandate_id batch_id, d.cd, r.booking_date - d.cd lag,
   bool_or(i.item_id is not null) is_gold,
   case when r.booking_date < '2024-07-01' then 'pre' else 'w' || quarter(r.booking_date) end per
  from r join dd d on d.mandate_id = r.mandate and d.amt = -r.amount_cents and d.cd <= r.booking_date - 2 and d.cd >= r.booking_date - 90
  left join it i on i.item_id = r.item_id and i.doc_norm = d.rnorm group by all""")
# learned densities (pre only): true lag among unambiguous-gold returns (exactly one gold candidate)
con.sql("""create table one as select * from c where per='pre' and bank_txn_id in (select bank_txn_id from c where per='pre' group by 1 having sum(is_gold::int)=1) and is_gold""")
con.sql("create table d1 as select md06, lag, n / sum(n) over (partition by md06) p from (select md06, lag, count(*) n from one group by 1, 2)")
con.sql("create table d2 as select md06, dow, lag, n / sum(n) over (partition by md06, dow) p from (select md06, dow, lag, count(*) n from one group by 1, 2, 3)")
q("select md06, quantile_disc(lag, 0.002) q002, quantile_disc(lag, 0.005) q005, quantile_disc(lag,0.995) q995, quantile_disc(lag,0.998) q998, median(lag) from one group by 1")
# windows: current (MD06 14-80, else 2-16) vs learned (support from quantiles)
con.sql("""create table cc as select c.*, coalesce(d1.p, 0) p1, coalesce(d2.p, 0) p2,
   (case when md06 then lag between 14 and 80 else lag between 2 and 16 end) in_cur,
   (case when md06 then lag between 21 and 75 else lag between 2 and 16 end) in_new
  from c left join d1 using (md06, lag) left join d2 using (md06, dow, lag)""")
rules = {
 "cur_46":    ("in_cur", "case when md06 then abs(lag-46) else 0 end, cd desc"),
 "cur_latest":("in_cur", "cd desc"),
 "cur_dens1": ("in_cur", "p1 desc, cd desc"),
 "cur_dens2": ("in_cur", "p2 desc, cd desc"),
 "new_46":    ("in_new", "case when md06 then abs(lag-46) else 0 end, cd desc"),
 "new_latest":("in_new", "cd desc"),
 "new_earliest":("in_new", "cd"),
 "new_dens1": ("in_new", "p1 desc, cd desc"),
 "new_dens2": ("in_new", "p2 desc, cd desc"),
 "new_d1md06_latestoth": ("in_new", "case when md06 then -p1 else 0 end, cd desc"),
 "new_latestmd06_d1oth": ("in_new", "case when md06 then 0 else -p1 end, cd desc"),
}
parts = []
for k, (w, o) in rules.items():
    parts.append(f"""select '{k}' rule, per, md06, count(*) n, sum(is_gold::int) ok from
      (select * from cc where {w} qualify row_number() over (partition by bank_txn_id order by {o}, batch_id) = 1) group by all""")
con.sql("create table res as " + " union all ".join(parts))
q("""pivot (select rule, per, md06, ok || '/' || n v from res) on per using any_value(v) order by md06, rule""")
