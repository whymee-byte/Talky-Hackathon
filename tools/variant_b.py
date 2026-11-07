"""Variant B of a FINAL run: UNIDENTIFIED exception on the T7u.known_party.name category lines.

    python tools/variant_b.py --phase final            # work/final/allocations.parquet -> work/final/allocations_B.parquet
    python -m src.submit --phase final --from work/final/allocations_B.parquet --out out/final_B.csv.gz
    python tools/validate.py out/final_B.csv.gz --phase final

Only rows with rule LIKE 'T7u.known_party.name%' AND category = 'UNIDENTIFIED' change (exception '' -> 'UNIDENTIFIED');
.strong / .weak / T15 drops keep the empty exception. Nothing else is touched.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="final")
    ap.add_argument("--src")
    ap.add_argument("--out")
    a = ap.parse_args()
    src = (a.src or os.path.join(REPO, "work", a.phase, "allocations.parquet")).replace(os.sep, "/")
    out = (a.out or os.path.join(REPO, "work", a.phase, "allocations_B.parquet")).replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"""COPY (
        SELECT * REPLACE (CASE WHEN rule LIKE 'T7u.known_party.name%' AND category = 'UNIDENTIFIED' THEN 'UNIDENTIFIED'
                               ELSE exception END AS exception)
        FROM read_parquet('{src}')) TO '{out}' (FORMAT parquet)""")
    n = con.execute(f"""SELECT count(*) FROM read_parquet('{src}') WHERE rule LIKE 'T7u.known_party.name%' AND category = 'UNIDENTIFIED'
                        AND coalesce(exception, '') = ''""").fetchone()[0]
    tot = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"variant B: {n} lines get exception UNIDENTIFIED (of {tot} rows) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
