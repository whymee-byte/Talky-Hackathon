"""Coverage of the simulated FINAL (tools/sim_final_za01.py) on ZA01 lines, and agreement with a LIVE reference run.

    .venv/bin/python tools/sim_final_report.py <ref_dir with LIVE line_party.parquet + allocations.parquet>
Agreement: same bank line in the plain LIVE run (AU01 ids) -> top party / item set, ids mapped CZA01 -> CAU01.
"""
import sys

import duckdb

ref = sys.argv[1]
c = duckdb.connect()
c.execute("""CREATE VIEW za AS SELECT bank_txn_id, account_id, CAST(amount AS DOUBLE) > 0 AS is_in
             FROM read_csv('data/final/bank_transactions.csv.gz', all_varchar=true) WHERE entity = 'ZA01'""")
c.execute("CREATE VIEW lp AS SELECT * FROM 'work/final/line_party.parquet' WHERE rank = 1")
c.execute("""CREATE VIEW al AS SELECT bank_txn_id, list_sort(list_filter(list(replace(item_id, '-Z', '-2')), lambda x: x IS NOT NULL AND x <> '')) items,
                    bool_or(category = 'UNIDENTIFIED') unid FROM 'work/final/allocations.parquet' GROUP BY 1""")
c.execute(f"CREATE VIEW rlp AS SELECT * FROM '{ref}/line_party.parquet' WHERE rank = 1")
c.execute(f"""CREATE VIEW ral AS SELECT bank_txn_id, list_sort(list_filter(list(item_id), lambda x: x IS NOT NULL AND x <> '')) items,
                     bool_or(category = 'UNIDENTIFIED') unid FROM '{ref}/allocations.parquet' GROUP BY 1""")
print(c.sql("""
  SELECT CASE WHEN account_id LIKE 'ZA01-UMVC-ZAR-2' THEN 'ZAR-2 (prev. sim)' ELSE 'other ZA01 accts' END AS scope,
         count(*) n, sum(is_in::int) n_in,
         round(avg((lp.party_id IS NOT NULL)::int), 4) party,
         round(avg((len(al.items) > 0)::int), 4) items,
         round(avg((lp.party_id IS NOT NULL)::int) FILTER (WHERE is_in), 4) party_in,
         round(avg((len(al.items) > 0)::int) FILTER (WHERE is_in), 4) items_in,
         round(avg(coalesce(al.unid, false)::int) FILTER (WHERE is_in), 4) unident_in,
         round(avg((replace(lp.party_id, 'ZA01', 'AU01') = r.party_id)::int) FILTER (WHERE r.party_id IS NOT NULL), 4) party_agree_ref,
         round(avg((len(ra.items) > 0)::int), 4) ref_items,
         round(avg((al.items = ra.items)::int) FILTER (WHERE len(ra.items) > 0), 4) items_agree_ref
  FROM za LEFT JOIN lp USING (bank_txn_id) LEFT JOIN al USING (bank_txn_id)
       LEFT JOIN rlp r USING (bank_txn_id) LEFT JOIN ral ra USING (bank_txn_id)
  GROUP BY GROUPING SETS ((), (scope)) ORDER BY scope NULLS FIRST""").df().fillna({'scope': 'ALL ZA01'}).to_string())

