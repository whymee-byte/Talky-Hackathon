"""P/R de pares de side_links_ap contra el golden, por segmento y por regla (window/sample/train).

    python -m src.sidefiles_ap.eval --phase window [--file side_links_ap.parquet]
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

from src.common import REPO, phase_dir, safe_stdout

SEGMENTS = ["ap_payment_multi", "ap_payment_single", "ap_paid_by_holding", "ap_fx", "ap_credit_note_netted", "ap_partial",
            "ap_confirming", "ap_factored", "ap_returned_transfer", "ap_duplicate_payment", "ar_card_settlement",
            "ar_card_chargeback", "ar_cheque_deposit", "ar_cheque_returned"]


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="window")
    ap.add_argument("--file", default="side_links_ap.parquet")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    work = os.path.join(REPO, "work", a.phase).replace(os.sep, "/")
    g = os.path.join(phase_dir(a.phase), "golden_allocations.csv.gz").replace(os.sep, "/")
    con = duckdb.connect()
    con.sql(f"create view lines as select bank_txn_id from '{work}/lines.parquet'")
    con.sql(f"""create table g as select g.bank_txn_id, nullif(g.item_id, '') item_id, round(g.amount::double * 100)::bigint amount_cents,
                coalesce(g.exception, '') exception, g.segment from read_csv('{g}', all_varchar=true, header=true) g join lines using (bank_txn_id)""")
    con.sql("create table seg as select bank_txn_id, any_value(segment) segment from g group by 1")
    con.sql(f"create table s as select * from '{work}/{a.file}'")
    con.sql("""create table m as select s.*, seg.segment,
                 g.item_id is not null and abs(s.amount_cents - g.amount_cents) <= greatest(1, 0.005 * g.amount_cents) tp,
                 g.item_id is not null and abs(s.amount_cents - g.amount_cents) <= greatest(1, 0.005 * g.amount_cents) and coalesce(s.exception, '') = g.exception tp_exc
               from s left join seg using (bank_txn_id) left join g on g.bank_txn_id = s.bank_txn_id and g.item_id = s.item_id""")
    tot = con.sql("select count(*), sum(tp::int), sum(tp_exc::int) from m").fetchall()[0]
    print(f"[{a.phase}] {a.file}: {tot[0]} pairs, precision {100 * (tot[1] or 0) / max(tot[0], 1):.2f} %, with exception {100 * (tot[2] or 0) / max(tot[0], 1):.2f} %")
    print(f"  {'segment':26s} {'gold':>8s} {'sub':>8s} {'P %':>7s} {'R %':>7s} {'R+exc %':>8s}")
    rows = con.sql("""with gs as (select segment, count(*) gold from g where item_id is not null group by 1),
           ss as (select segment, count(*) sub, sum(tp::int) tp, sum(tp_exc::int) tp_exc from m group by 1)
      select coalesce(gs.segment, ss.segment), coalesce(gold, 0), coalesce(sub, 0), coalesce(tp, 0), coalesce(tp_exc, 0)
      from gs full join ss using (segment) order by 2 desc nulls last""").fetchall()
    for seg, gold, sub, tp, tpe in rows:
        if not a.all and seg not in SEGMENTS and not sub:
            continue
        print(f"  {str(seg):26s} {gold:8d} {sub:8d} {100 * tp / sub if sub else 0:7.2f} {100 * tp / gold if gold else 0:7.2f} {100 * tpe / gold if gold else 0:8.2f}")
    print("  by rule:")
    for rule, n, tp, tpe in con.sql("select rule, count(*), sum(tp::int), sum(tp_exc::int) from m group by 1 order by 1").fetchall():
        print(f"    {rule:22s} pairs={n:8d} P={100 * (tp or 0) / n:6.2f} % (+exc {100 * (tpe or 0) / n:6.2f} %)")
    print("  false pairs by rule/exception:", con.sql("select rule, exception, count(*) from m where not tp group by all order by 3 desc limit 8").fetchall())
    print("  exception errors:", con.sql("select s.exception, g.exception gexc, count(*) from m s join g on g.bank_txn_id=s.bank_txn_id and g.item_id=s.item_id where s.tp and not s.tp_exc group by all order by 3 desc limit 8").fetchall())
    return 0


if __name__ == "__main__":
    sys.exit(main())
