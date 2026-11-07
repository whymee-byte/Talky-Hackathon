# Chat C: 1:1 assignment between return lines and receipts (batch_id+mandate), on top of the best tiebreak
exec(open("explore/t2/c_rules.py").read().split("rules = {")[0].replace('q("select md06, quantile', '0 and q("select md06, quantile'))
q("select per, count(*) n_receipts, count(*) filter (where k>1) returned_twice from (select per, batch_id, count(*) k from cc where is_gold and in_new group by all) group by 1 order by 1")
ORDER = "case when md06 then abs(lag-46) else 0 end, case when md06 then 0 else -p1 end, cd desc, batch_id"
con.sql(f"create table pref as select bank_txn_id, batch_id, per, md06, is_gold, row_number() over (partition by bank_txn_id order by {ORDER}) pr, count(*) over (partition by bank_txn_id) n from cc where in_new")
def one_to_one(rounds):
    con.sql("create or replace table taken as select ''::varchar bank_txn_id, ''::varchar batch_id where false")
    for _ in range(rounds):
        # each unassigned line proposes its best still-free receipt; per receipt the line with fewest alternatives (then best rank) wins
        con.sql("""insert into taken select bank_txn_id, batch_id from (
            select * from (select p.* from pref p where p.bank_txn_id not in (select bank_txn_id from taken) and p.batch_id not in (select batch_id from taken)
                           qualify row_number() over (partition by p.bank_txn_id order by pr) = 1)
            qualify row_number() over (partition by batch_id order by n, pr, bank_txn_id) = 1)""")
    return con.sql("select p.per, p.md06, sum(p.is_gold::int) ok, count(*) n from taken t join pref p using (bank_txn_id, batch_id) group by all order by 2, 1").fetchall()
base = con.sql("select per, md06, sum(is_gold::int), count(*) from pref where pr=1 group by all order by 2, 1").fetchall()
print("no 1:1  ", base)
print("1:1     ", one_to_one(6))
# asymmetric: non-MD06 picks fixed; MD06 avoids receipts picked by non-MD06 lines or by an MD06 line with a single candidate; fallback = own first choice
con.sql("""create or replace table first as select * from pref where pr = 1""")
con.sql("""create or replace table blocked as select distinct batch_id from first where not md06 or n = 1""")
con.sql("""create or replace table md as select p.*, (p.batch_id in (select batch_id from blocked) and not (p.n = 1)) blk from pref p where md06""")
con.sql("""create or replace table md_pick as select * from md qualify row_number() over (partition by bank_txn_id order by
           case when blk and exists (select 1 from first f where f.batch_id = md.batch_id and f.bank_txn_id <> md.bank_txn_id) then 1 else 0 end, pr) = 1""")
print("asym    ", con.sql("select per, md06, sum(is_gold::int), count(*) from (select * from first where not md06 union all select * exclude (blk) from md_pick) group by all order by 2, 1").fetchall())
