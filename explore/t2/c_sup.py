# Chat C: 'superseded' receipts (a later re-presentation of the same mandate+ref exists before the return) on TRAIN < 2024-07
exec(open("explore/t2/c_feat.py").read().split('q("select code=')[0])
con.sql("""create table c3 as select c2.*, exists (select 1 from dd d2 join dd d1 on d1.batch_id = c2.batch_id and d1.mandate_id = d2.mandate_id and d1.rnorm = d2.rnorm
     where d2.re_presentation = 'Y' and d2.cd > c2.cd and d2.cd < c2.booking_date and d2.mandate_id = (select mandate from r where r.bank_txn_id = c2.bank_txn_id)) sup
  from c2""")
q("select code='MD06' md06, n>1 amb, sup, count(*), avg(is_gold::int) p from c3 where any_gold=1 group by all order by all")
