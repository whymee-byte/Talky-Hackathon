exec(open("explore/t2/a_diag.py").read())
# golden doc -> our pr doc (did) via item, its e2e, its due; golden line booking date
con.sql("""create table gd as select g.bank_txn_id gl, l.booking_date gbd, g.item_id, p.did, p.end_to_end_id e2e, p.run_id, pi.due_date due, p.execution_date ex
           from g join lines l using(bank_txn_id) left join pr_item pi using(item_id) left join pr p using(did) where p.run_type = 'confirming' or p.did is null""")
show("select count(*) golden_pairs, count(did) with_pr_doc, count(distinct gl) lines from gd")
print("is every e2e within a single golden line?")
show("select n_lines, count(*) e2e from (select e2e, count(distinct gl) n_lines from gd where e2e is not null group by 1) group by 1 order by 1")
print("golden booking date minus the doc's own due date / minus the e2e max due")
con.sql("create table e2emax as select end_to_end_id e2e, due emax from pr_cf_e2e")
show("select gbd - due d_doc, count(*) from gd group by 1 order by 2 desc limit 12")
show("select gd.gbd - e.emax d_e2e, count(*) from gd join e2emax e using(e2e) group by 1 order by 2 desc limit 12")
print("our line for the e2e vs golden line")
show("""select (x.bank_txn_id = gd.gl) ok, count(*) from gd join pr_cf_e2e_line x on x.end_to_end_id = gd.e2e group by 1""")
