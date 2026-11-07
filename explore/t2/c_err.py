# Chat C: diagnose ar_dd_returned errors on q1 (tag ddr)
exec(open("explore/t2/w.py").read())
con.sql("create view s as select * from 'work/window/side_links_q1_ddr.parquet' where rule='T2.dd_return'")
con.sql("""create table e as select s.bank_txn_id, s.item_id sub_item, s.evidence, g.item_id g_item
 from s join g on g.bank_txn_id=s.bank_txn_id where g.segment='ar_dd_returned' and g.item_id <> s.item_id""")
q("select count(*) from e")
q("select regexp_extract(evidence, 'code=([^;]*)', 1) code, regexp_extract(evidence, 'batch_in_text=([^;]*)', 1) bt, count(*) from e group by all order by 3 desc")
# candidates per error: all receipts with same mandate & amount within 90 d, mark which maps to gold / sub item
con.sql("""create table cands as
 select e.bank_txn_id, l.booking_date, regexp_extract(e.evidence, 'code=([^;]*)', 1) code, d.batch_id, d.collection_date::date cd,
        l.booking_date - d.collection_date::date lag, d.remittance_info, i.item_id, i.item_id = e.g_item is_gold, i.item_id = e.sub_item is_sub
 from e join l using(bank_txn_id) join dd d on d.mandate_id = l.mandate_ids[1] and round(d.amount::double*100)::bigint = -l.amount_cents
   and d.collection_date::date <= l.booking_date and d.collection_date::date >= l.booking_date - interval 100 day
 left join it i on i.side='AR' and i.doc_norm = trim(regexp_replace(upper(d.remittance_info), '[^A-Z0-9]+', ' ', 'g'))""")
q("select * from cands order by bank_txn_id, cd", 80)
q("select code, is_gold, is_sub, count(*), median(lag), min(lag), max(lag) from cands group by all order by all")
