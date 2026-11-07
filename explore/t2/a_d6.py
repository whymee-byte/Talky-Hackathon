exec(open("explore/t2/a_diag.py").read())
con.sql(f"create table gall as select bank_txn_id, item_id, segment from read_csv('data/train/golden_allocations.csv.gz', all_varchar=true, {q}) where bank_txn_id in (select bank_txn_id from lines)")
con.sql("create table ours as select o.*, c.total_ok from links_pr o join pr_cf_line c on c.bank_txn_id = o.bank_txn_id and c.end_to_end_id = o.link_key where rule = 'T2.run_confirming'")
print("precision by total_ok")
show("""select o.total_ok, count(*) n, avg((gi.item_id is not null)::int) p from ours o left join gall gi on gi.bank_txn_id = o.bank_txn_id and gi.item_id = o.item_id group by 1""")
print("golden confirming pairs we miss: why")
show("""select case when pi.item_id is null then 'doc not resolved to item (pr_item)'
                 when x.end_to_end_id is null then 'no debit in the expected slot'
                 when x.bank_txn_id <> g.bank_txn_id then 'assigned to another debit'
                 else 'other' end reason, count(*)
        from g left join pr_item pi using (item_id) left join pr p on p.did = pi.did and p.run_type = 'confirming'
        left join pr_cf_e2e_line x on x.end_to_end_id = p.end_to_end_id
        where not exists (select 1 from ours o where o.bank_txn_id = g.bank_txn_id and o.item_id = g.item_id)
        group by 1 order by 2 desc""")
print("missed with no debit in slot: golden debit date - expected")
show("""select l.booking_date - e.expected d, count(*) from g join pr_item pi using(item_id) join pr p on p.did = pi.did and p.run_type='confirming'
        join pr_cf_e2e e on e.end_to_end_id = p.end_to_end_id join lines l on l.bank_txn_id = g.bank_txn_id
        left join pr_cf_e2e_line x on x.end_to_end_id = p.end_to_end_id where x.end_to_end_id is null group by 1 order by 2 desc limit 10""")
