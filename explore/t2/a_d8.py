exec(open("explore/t2/a_d6.py").read().split('print("precision by total_ok")')[0])
con.sql("""create table item_runs as select pi.item_id, count(distinct p.run_id) n_runs, min(p.execution_date) first_ex, max(p.execution_date) last_ex
           from pr p join pr_item pi using(did) where p.run_type='confirming' group by 1""")
show("""select ir.n_runs, count(*) n, avg((gi.item_id is not null)::int) p from ours o join item_runs ir using(item_id)
        left join gall gi on gi.bank_txn_id = o.bank_txn_id and gi.item_id = o.item_id group by 1 order by 1""")
print("items in >1 run: which run does the golden debit belong to (first/last executed)?")
show("""with o2 as (select o.item_id, o.bank_txn_id, o.link_key from ours o join item_runs ir using(item_id) where ir.n_runs > 1)
        select (p.execution_date = ir.last_ex) ours_is_last_run, (gi.item_id is not null) correct, count(*)
        from o2 join item_runs ir using(item_id) join pr p on p.end_to_end_id = o2.link_key and p.run_type='confirming'
        left join gall gi on gi.bank_txn_id = o2.bank_txn_id and gi.item_id = o2.item_id group by 1, 2 order by 1, 2""")
