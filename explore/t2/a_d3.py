exec(open("explore/t2/a_d2.py").read().split('print("FP breakdown')[0])
con.sql(f"create table gtrain as select g.bank_txn_id, g.item_id, g.segment, b.booking_date::date bd from read_csv('data/train/golden_allocations.csv.gz', all_varchar=true, {q}) g join read_csv('data/train/bank_transactions.csv.gz', all_varchar=true, {q}) b using(bank_txn_id) where g.item_id in (select item_id from ours)")
con.sql("""create table fp as select o.*, p.execution_date ex, p.end_to_end_id e2e, pi.due_date due, l.booking_date obd from ours o join pr p on p.end_to_end_id = o.link_key and p.run_type='confirming'
           join pr_item pi on pi.did = p.did and pi.item_id = o.item_id join lines l on l.bank_txn_id = o.bank_txn_id
           where not exists (select 1 from gall where gall.bank_txn_id = o.bank_txn_id and gall.item_id = o.item_id)""")
print("FP pairs: where does the golden settle the item (anywhere in TRAIN)?")
show("""select coalesce(t.segment, '(never settled)') seg, count(*), median(t.bd - fp.obd) days_after_ours, median(fp.obd - fp.ex) ours_minus_exec, median(fp.due - fp.ex) due_minus_exec
        from fp left join gtrain t using(item_id) group by 1 order by 2 desc""")
print("TP pairs: due minus exec")
show("""select median(pi.due_date - p.execution_date) due_minus_exec, median(l.booking_date - p.execution_date) bd_minus_exec, median(l.booking_date - pi.due_date) bd_minus_due from ours o join pr p on p.end_to_end_id = o.link_key and p.run_type='confirming'
        join pr_item pi on pi.did = p.did and pi.item_id = o.item_id join lines l on l.bank_txn_id = o.bank_txn_id join gall on gall.bank_txn_id = o.bank_txn_id and gall.item_id = o.item_id""")
