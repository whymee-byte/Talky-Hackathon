# Chat C: collision risk of the 'last number group' rule over all TRAIN receipts (same party)
import duckdb; con = duckdb.connect(); T = "data/train"
N = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"
con.sql(f"create table dd as select distinct batch_id, mandate_id, {N.format(x='remittance_info')} rn from read_csv('{T}/direct_debit_batches/[!.]*.csv', all_varchar=true, union_by_name=true)")
con.sql("""create table cm as select sepa_mandate_id, any_value(customer_id) cid from read_csv('data/masters/customers.csv', all_varchar=true, quote='"', escape='"', delim=',') where sepa_mandate_id<>'' group by 1""")
con.sql("create table x as select dd.*, cm.cid, nullif(regexp_extract(rn, '(?:^| )([0-9]{4,})$', 1), '') tail from dd join cm on cm.sepa_mandate_id = dd.mandate_id")
con.sql("create view it as select item_id, party_id, doc_norm, regexp_extract(doc_norm, '(?:^| )([0-9]+)$', 1) dtail from 'work/window/items.parquet' where side='AR' and kind='ARI'")
print(con.sql("""select k, count(*) receipts from (select x.batch_id, x.mandate_id, count(i.item_id) k from x left join it i on i.party_id = x.cid and i.dtail = x.tail
   where x.tail is not null group by all) group by 1 order by 1""").fetchall())
print(con.sql("select length(tail) len, count(*) from x where tail is not null group by 1 order by 1").fetchall())
