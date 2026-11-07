import duckdb, sys
c=duckdb.connect()
C='data_cache'
c.sql(f"""create view items as
 select item_id, customer_id party_id from '{C}/train/ar_invoices.parquet'
 union all select item_id, supplier_id from '{C}/train/ap_invoices.parquet'
 union all select item_id, party_id from '{C}/train/credit_notes.parquet'""")
c.sql(f"""create view gp as select g.bank_txn_id, any_value(segment) seg, count(distinct i.party_id) nparty, min(i.party_id) party
 from '{C}/train/golden_allocations.parquet' g join items i using(item_id) group by 1""")
c.sql(f"create view bt as select * from '{C}/train/bank_transactions.parquet' where booking_date>='2024-07-01'")
where=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 10
import pandas as pd; pd.set_option('display.width',250); pd.set_option('display.max_colwidth',140)
print(c.sql(f"""select seg, party, statement_format f, text, counterparty_name cp, counterparty_account cpa from bt join gp using(bank_txn_id) where {where} order by hash(bank_txn_id) limit {n}""").df().to_string())
