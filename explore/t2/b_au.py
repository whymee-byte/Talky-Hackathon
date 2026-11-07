import duckdb, glob
from src.common import phase_dir
con = duckdb.connect()
f = [p.replace(chr(92), '/') for p in glob.glob(phase_dir('window') + '/payment_runs/*.csv')]
con.sql(f"create view pr as select * from read_csv({f}, all_varchar=true, union_by_name=true)")
print(con.sql("select run_id, run_type, execution_date, debtor_account_id, end_to_end_id, beneficiary, transfer_amount, document_number, document_amount from pr where end_to_end_id like '%PR010024082290%0328%' or (run_id like '%PR010024082290%' and transfer_amount like '12508%')").fetchall())
