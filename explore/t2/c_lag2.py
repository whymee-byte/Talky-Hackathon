exec(open("explore/t2/c_lag.py").read().split('q("select count(*), count(distinct')[0])
con.sql("""create table c as select r.bank_txn_id, r.code, r.booking_date, r.mandate, d.batch_id, d.cd, d.re_presentation, r.booking_date - d.cd lag,
   (i.item_id is not null) is_gold
  from r join dd d on d.mandate_id = r.mandate and d.amt = -r.amount_cents and d.cd <= r.booking_date and d.cd >= r.booking_date - 120
  left join it i on i.item_id = r.item_id and i.doc_norm = d.rnorm""")
q("select code='MD06' md06, n_gold, count(*) from (select bank_txn_id, any_value(code) code, sum(is_gold::int) n_gold from c group by 1) group by all order by all")
# MD06 within [21,75]: how many candidates, rank of gold from latest
con.sql("""create table m as select *, count(*) over w n, row_number() over (partition by bank_txn_id order by cd desc) rk
   from c where code='MD06' and lag between 21 and 80 window w as (partition by bank_txn_id)""")
q("select n, rk, count(*) from m where is_gold group by all order by all")
# has the non-gold candidate been returned by another line (any code) before this booking date?
con.sql("""create table ret_by as select c.batch_id, c.mandate, min(c.booking_date) first_ret from c where is_gold group by all""")
q("""select n, is_gold, (rb.first_ret is not null and rb.first_ret < m.booking_date) returned_before, count(*) from m
  left join ret_by rb on rb.batch_id = m.batch_id and rb.mandate = m.mandate where n > 1 group by all order by all""")
