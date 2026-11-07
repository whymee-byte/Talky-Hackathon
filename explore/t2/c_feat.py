# Chat C: candidate features vs gold on TRAIN < 2024-07
exec(open("explore/t2/c_lag.py").read().split('q("select count(*), count(distinct')[0])
con.sql("""create table c as select r.bank_txn_id, coalesce(r.code,'?') code, r.booking_date, d.batch_id, d.cd, d.re_presentation rp, r.booking_date - d.cd lag,
   bool_or(i.item_id is not null) is_gold
  from r join dd d on d.mandate_id = r.mandate and d.amt = -r.amount_cents
     and d.cd <= r.booking_date - (case when r.code='MD06' then 14 else 2 end) and d.cd >= r.booking_date - (case when r.code='MD06' then 80 else 16 end)
  left join it i on i.item_id = r.item_id and i.doc_norm = d.rnorm group by all""")
con.sql("""create table c2 as select *, count(*) over w n, max(is_gold::int) over w any_gold, row_number() over (partition by bank_txn_id order by cd desc) rk,
   sum((rp='Y')::int) over w n_rp from c window w as (partition by bank_txn_id)""")
q("select code='MD06' md06, n>1 amb, count(distinct bank_txn_id), avg(any_gold) from c2 group by all order by all")
print("non-MD06, ambiguous: gold by rp/rank")
q("select rp, rk, count(*), avg(is_gold::int) p from c2 where code<>'MD06' and n>1 and any_gold=1 group by all order by all")
q("select n_rp>0 and n_rp<n mixed, count(distinct bank_txn_id) from c2 where code<>'MD06' and n>1 and any_gold=1 group by all")
print("MD06 ambiguous: gold by rp/rank and by lag bins")
q("select rp, rk, count(*), avg(is_gold::int) p from c2 where code='MD06' and n>1 and any_gold=1 group by all order by all")
q("select (lag/7)*7 lag7, count(*) n, sum(is_gold::int) gold, avg(is_gold::int) p from c2 where code='MD06' and any_gold=1 group by all order by 1")
