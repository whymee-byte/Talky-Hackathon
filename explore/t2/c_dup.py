exec(open("explore/t2/w.py").read())
con.sql("create view s as select * from 'work/window/side_links_q1_ddr.parquet' where rule='T2.dd_return'")
con.sql("create table sg as select s.*, g.item_id is not null ok from s left join g on g.bank_txn_id=s.bank_txn_id and g.item_id=s.item_id")
q("select regexp_extract(evidence,'code=([^;]*)',1)='MD06' md06, ok, cnt>1 shared, count(*) from (select *, count(*) over (partition by link_key, regexp_extract(evidence,'mandate=([^;]*)',1)) cnt from sg) group by all order by all")
