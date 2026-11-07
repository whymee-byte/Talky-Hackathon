import duckdb, sys
con = duckdb.connect()
R = "data/bankrec-docs-and-masters"
con.sql(f"""
create view bt_train as select * from read_csv_auto('{R}/train/bank_transactions.csv.gz', all_varchar=true);
create view bt_live  as select * from read_csv_auto('{R}/live/bank_transactions.csv.gz', all_varchar=true);
create view g as select * from read_csv_auto('{R}/train/golden_allocations.csv.gz', all_varchar=true);
create view ar as select * from read_csv_auto('{R}/train/ar_invoices.csv.gz', all_varchar=true);
create view api as select * from read_csv_auto('{R}/train/ap_invoices.csv.gz', all_varchar=true);
create view cn as select * from read_csv_auto('{R}/train/credit_notes.csv.gz', all_varchar=true);
create view oi as select * from read_csv_auto('{R}/live/open_items_at_start.csv.gz', all_varchar=true);
""")
for q in sys.argv[1:]:
    print(">>>", q[:120]); print(con.sql(q).fetchdf().to_string(max_rows=80)); print()
