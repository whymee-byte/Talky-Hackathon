import duckdb, sys
import pandas as pd; pd.set_option('display.width',260); pd.set_option('display.max_colwidth',120)
c=duckdb.connect()
where=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 12
print(c.sql(f"""
with p as (select bank_txn_id, list(party_id order by rank) cands, list(rule order by rank)[1] r1, max(confidence) c1 from 'work/window/line_party.parquet' group by 1)
select t.parties[1:2] gold, p.cands, p.r1, round(p.c1,2) c1, left(l.text_norm,100) text, l.cp_name_norm cp
from 'work/train/party_truth.parquet' t join 'work/window/party_lines.parquet' l using(bank_txn_id) left join p using(bank_txn_id)
where t.booking_date>='2024-07-01' and not coalesce(list_contains(t.parties,p.cands[1]),false) and {where}
order by hash(bank_txn_id) limit {n}""").df().to_string())
