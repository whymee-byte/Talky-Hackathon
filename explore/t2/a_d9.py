exec(open("explore/t2/a_d6.py").read().split('print("precision by total_ok")')[0])
con.sql("""create table fpx as select o.* from ours o where not exists (select 1 from gall gi where gi.bank_txn_id = o.bank_txn_id and gi.item_id = o.item_id)""")
e2e = con.sql("select link_key from fpx using sample 1 rows (reservoir, 11)").fetchall()[0][0]
print("e2e", e2e)
show(f"""select p.document_number, p.document_type, p.doc_cents, p.execution_date, pi.item_id, pi.due_date, i.issue_date, i.payment_terms, i.total_cents
         from pr p join pr_item pi using(did) join items i using(item_id) where p.end_to_end_id = '{e2e}'""")
show(f"""select o.bank_txn_id ours, lo.booking_date ours_d, gi.bank_txn_id gold, lg.booking_date gold_d, lg.amount_cents gold_amt
         from fpx o join lines lo on lo.bank_txn_id = o.bank_txn_id left join gall gi on gi.item_id = o.item_id left join lines lg on lg.bank_txn_id = gi.bank_txn_id where o.link_key = '{e2e}' limit 3""")
run = e2e.split('-')[0]
show(f"""select l.bank_txn_id, l.booking_date, l.amount_cents, left(l.text_norm, 60) tx from lines l where list_contains(l.batch_refs, '{run.split('|')[1]}') and l.account_id = '{run.split('|')[0]}' order by 2""")
show(f"""select e.end_to_end_id, e.due, e.expected, e.cents from pr_cf_e2e e where e.run_id = '{run}' order by e.due""")
