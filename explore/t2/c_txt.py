exec(open("explore/t2/w.py").read())
con.sql("create view s as select * from 'work/window/side_links_q1_ddr.parquet' where rule='T2.dd_return'")
con.sql("create table e as select s.*, g.item_id g_item from s join g on g.bank_txn_id=s.bank_txn_id where g.item_id<>s.item_id")
con.sql(f"create view bt as select * from read_csv('{T}/bank_transactions.csv.gz', all_varchar=true)")
q("select b.bank, b.statement_format, b.booking_date, b.value_date, b.text, b.ref1, b.ref2, b.remittance_info, b.end_to_end_id, b.counterparty_name from e join bt b using(bank_txn_id) limit 14")
q("""select l.bank_txn_id, d.batch_id, d.collection_date, d.end_to_end_id, d.remittance_info, i.item_id=e.g_item gold, i.item_id=e.item_id sub
 from e join l using(bank_txn_id) join dd d on d.mandate_id=l.mandate_ids[1] and round(d.amount::double*100)::bigint=-l.amount_cents
  and d.collection_date::date between l.booking_date - 90 and l.booking_date
 join it i on i.side='AR' and i.party_id = (select any_value(item_id) from it where false) or (i.item_id in (e.g_item, e.item_id) and i.doc_norm = trim(regexp_replace(upper(d.remittance_info), '[^A-Z0-9]+', ' ', 'g')))
 order by 1,3 limit 30""")
