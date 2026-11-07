exec(open("explore/t2/a_d6.py").read().split('print("precision by total_ok")')[0])
con.sql("""create table e2einfo as select p.end_to_end_id, count(*) docs, count(pi.item_id) resolved, count(distinct pi.due_date) n_due,
           max(pi.due_date) - min(pi.due_date) due_span from pr p left join pr_item pi using(did) where p.run_type='confirming' group by 1""")
show("""select (e.resolved < e.docs) has_unresolved, e.n_due > 1 multi_due, count(*) n, avg((gi.item_id is not null)::int) p
        from ours o join e2einfo e on e.end_to_end_id = o.link_key left join gall gi on gi.bank_txn_id = o.bank_txn_id and gi.item_id = o.item_id group by 1, 2 order by 1, 2""")
print("FP pairs: golden debit of the item vs ours (days), and the item due vs our e2e expected")
show("""select gx.bd - l.booking_date d_gold_minus_ours, count(*) from ours o join lines l using(bank_txn_id)
        left join gall gi on gi.bank_txn_id = o.bank_txn_id and gi.item_id = o.item_id
        join (select g2.item_id, any_value(l2.booking_date) bd from gall g2 join lines l2 using(bank_txn_id) group by 1) gx on gx.item_id = o.item_id
        where gi.item_id is null group by 1 order by 2 desc limit 10""")
