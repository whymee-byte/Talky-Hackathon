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
print(c.sql("select nparty>1 multi, count(*) from gp join bt using(bank_txn_id) group by 1"))
q=sys.argv[1] if len(sys.argv)>1 else None
for fmt in ['n43','mt940','camt053']:
  print('=====',fmt)
  print(c.sql(f"""select seg, party, left(text,110) text, ref1, ref2, left(counterparty_name,30) cp, left(counterparty_account,24) cpa
   from bt join gp using(bank_txn_id) where statement_format='{fmt}' and nparty=1 using sample 12 rows""").df().to_string())
