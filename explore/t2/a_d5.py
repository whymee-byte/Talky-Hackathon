exec(open("explore/t2/a_d1.py").read().split('show("select count(*) golden_pairs')[0])
con.sql(f"create table prraw as select debtor_account_id || '|' || run_id run_id, run_type, execution_date::date ex, end_to_end_id, document_number, trim(regexp_replace(strip_accents(upper(document_number)), '[^A-Z0-9]+', ' ', 'g')) dn, beneficiary from {glob_csv(detail_dirs('window', None), 'payment_runs')} where run_type = 'confirming'")
con.sql("create table miss as select gd.*, i.doc_norm, i.party_id, i.entity, i.total_cents from gd join items i using(item_id) where gd.did is null")
print("missing: candidate raw run docs with the same number; exec date vs golden debit; is that run kept in pr?")
show("""select m.gl, m.gbd, m.doc_norm, p.run_id, p.ex, m.gbd - p.ex d, (p.run_id in (select run_id from pr)) kept
        from miss m join prraw p on p.dn = m.doc_norm order by m.gl limit 12""")
show("""select (p.run_id in (select run_id from pr)) kept, count(distinct m.item_id) from miss m left join prraw p on p.dn = m.doc_norm and m.gbd - p.ex between -3 and 15 group by 1""")
