"""T10 · one side_links.parquet from T2 (src/sidefiles) and T2b (src/sidefiles_ap).

    python -m src.sidemerge --phase window|live|final

T2b (payment runs, cards, cheques: FX / PARTIAL / DUPLICATE set at source, confirming by maturity) wins line by line:
every line that T2b explains takes T2b's rows only; T2's rows are kept for the other lines (direct-debit batches,
returns, supplier direct debits). Run after both modules and before T6/T3f, which only look at lines left over.
"""
import argparse
import os

import duckdb

from src.common import REPO


def build(phase: str) -> None:
    w = os.path.join(REPO, "work", phase)
    sl, ap = f"{w}/side_links.parquet", f"{w}/side_links_ap.parquet"
    if not os.path.exists(ap):
        print(f"[{phase}] no side_links_ap.parquet: side_links unchanged"); return
    con = duckdb.connect()
    con.execute(f"CREATE TABLE S AS SELECT * FROM '{sl}' WHERE rule LIKE 'T2.%'")   # only T2 rows survive: later modules (T6, T3f, T13, T14…) re-add theirs
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{ap}'")
    con.execute(f"""COPY (SELECT * FROM S WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM A)
                          UNION ALL BY NAME SELECT * FROM A) TO '{sl}' (FORMAT parquet)""")
    print(f"[{phase}]", con.sql(f"""SELECT count(*) FILTER (WHERE bank_txn_id IN (SELECT bank_txn_id FROM A)) replaced_T2_rows,
                                   (SELECT count(*) FROM A) T2b_rows, (SELECT count(*) FROM '{sl}') total FROM S""").fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
