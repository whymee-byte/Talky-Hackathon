exec(open("explore/t2/a_d6.py").read().split('print("precision by total_ok")')[0])
con.sql("""create table gx as select g.bank_txn_id, l.booking_date gbd, p.end_to_end_id, p.execution_date ex, e.due, e.expected
           from g join lines l using(bank_txn_id) join pr_item pi using(item_id) join pr p on p.did = pi.did and p.run_type='confirming'
           join pr_cf_e2e e on e.end_to_end_id = p.end_to_end_id""")
con.sql("create table ge as select end_to_end_id, any_value(gbd) gbd, any_value(ex) ex, any_value(due) due, any_value(expected) expected from gx group by 1")
print("golden debit - expected (maturity rolled): late ones; and late ones relative to execution date")
show("""select gbd - expected late, gbd - ex from_exec, due - ex due_minus_exec, count(*) n from ge where gbd - expected > 3 group by all order by n desc limit 15""")
show("""select case when gbd - expected <= 3 then 'on time' else 'late' end k, count(*) from ge group by 1""")
