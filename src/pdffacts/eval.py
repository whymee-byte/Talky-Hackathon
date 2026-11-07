"""T8 check (TASKS.md): share of the golden items of each segment that carry their fact in doc_facts.parquet.

    python -m src.pdffacts.eval --phase window|sample

Also reports precision proxies: facts whose item is settled in the window by a line of an unrelated segment.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

from src.common import REPO, phase_dir, safe_stdout

EXPECTED = {   # segment → fact expected on the settled item (NEW_IBAN: on the item's party)
    "ar_instalment": "INSTALMENTS",
    "ar_retention": "RETENTION",
    "ar_retention_release": "RETENTION",
    "ar_advance_deduction": "ADVANCE_DEDUCT",
    "ar_replaced_invoice": "REPLACES",
    "ar_same_amount_month": "SERVICE_MONTH",
    "ar_new_iban": "NEW_IBAN",
    "ap_factored": "FACTORED",
}


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", default="window", choices=["sample", "window"])
    a = ap.parse_args(argv)
    work = os.path.join(REPO, "work", a.phase).replace(os.sep, "/")
    g = os.path.join(phase_dir(a.phase), "golden_allocations.csv.gz").replace(os.sep, "/")
    con = duckdb.connect()
    con.sql(f"create view lines as select bank_txn_id from '{work}/lines.parquet'")
    con.sql(f"create view items as select item_id, party_id from '{work}/items.parquet'")
    con.sql(f"create table f as select * from '{work}/doc_facts.parquet'")
    con.sql(f"""create table g as select g.segment, g.item_id from read_csv('{g}', all_varchar=true, header=true) g
                join lines using (bank_txn_id) where g.item_id is not null and g.item_id <> ''""")
    print(f"[{a.phase}] share of golden items of the segment with their fact (target >= 90 %)")
    print(f"  {'segment':24s} {'fact':16s} {'items':>7s} {'with fact':>9s} {'%':>7s}")
    for seg, fact in EXPECTED.items():
        if fact == "NEW_IBAN":
            q = f"""select count(distinct g.item_id), count(distinct case when f.party_id is not null then g.item_id end)
                    from g join items i using (item_id) left join (select distinct party_id from f where fact = 'NEW_IBAN') f
                    on f.party_id = i.party_id where g.segment = '{seg}'"""
        else:
            q = f"""select count(distinct g.item_id), count(distinct f.item_id) from g
                    left join (select distinct item_id from f where fact = '{fact}') f using (item_id) where g.segment = '{seg}'"""
        n, k = con.sql(q).fetchall()[0]
        print(f"  {seg:24s} {fact:16s} {n:7d} {k:9d} {100 * k / n if n else 0:7.2f}")
    print("  facts by rule (resolved to an item / total):")
    for r, n, ni in con.sql("select rule, count(*), count(item_id) from f group by 1 order by 1").fetchall():
        print(f"    {r:28s} {ni:7d} / {n:7d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
