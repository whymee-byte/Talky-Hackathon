"""Pair precision / recall of side_links against the golden, per segment (window/sample/train only).

    python -m src.sidefiles.eval --phase window [--segments ar_dd_batch,ar_dd_returned]

TP = (bank_txn_id, item_id) in the golden with amount within max(0.01, 0.5 %) — the scorer's definition — and
(column exc_ok) the same exception. Precision of a segment = TP / side_links pairs on lines of that segment.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

from src.common import REPO, phase_dir, safe_stdout

T2_SEGMENTS = ["ar_dd_batch", "ar_dd_returned", "ar_card_settlement", "ar_cheque_deposit", "ap_payment_multi",
               "ap_payment_single", "ap_confirming", "ap_paid_by_holding", "ap_credit_note_netted", "ap_direct_debit"]


def evaluate(phase: str, data: str | None = None, rules: list[str] | None = None):
    con = duckdb.connect()
    work = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    g = os.path.join(phase_dir(phase, data), "golden_allocations.csv.gz").replace(os.sep, "/")
    con.sql(f"create view lines as select bank_txn_id from '{work}/lines.parquet'")
    con.sql(f"""create table g as select g.bank_txn_id, nullif(g.item_id, '') item_id, round(g.amount::double * 100)::bigint amount_cents,
                coalesce(g.exception, '') exception, g.segment
                from read_csv('{g}', all_varchar=true, header=true) g join lines using (bank_txn_id)""")
    con.sql("create table seg as select bank_txn_id, any_value(segment) segment from g group by 1")
    filt = "" if not rules else "where rule in (" + ", ".join(f"'{r}'" for r in rules) + ")"
    con.sql(f"create table s as select * from '{work}/side_links.parquet' {filt}")
    con.sql("""create table m as select s.*, seg.segment,
                 g.item_id is not null and abs(s.amount_cents - g.amount_cents) <= greatest(1, 0.005 * g.amount_cents) tp,
                 g.item_id is not null and abs(s.amount_cents - g.amount_cents) <= greatest(1, 0.005 * g.amount_cents)
                   and coalesce(s.exception, '') = g.exception tp_exc
               from s left join seg using (bank_txn_id) left join g on g.bank_txn_id = s.bank_txn_id and g.item_id = s.item_id""")
    per_seg = con.sql("""
      with gs as (select segment, count(*) gold from g where item_id is not null group by 1),
           ss as (select segment, count(*) sub, sum(tp::int) tp, sum(tp_exc::int) tp_exc from m group by 1)
      select coalesce(gs.segment, ss.segment) segment, coalesce(gold, 0) gold, coalesce(sub, 0) sub, coalesce(tp, 0) tp,
             coalesce(tp_exc, 0) tp_exc
      from gs full join ss using (segment) order by gold desc nulls last""").fetchall()
    per_rule = con.sql("""select rule, count(*), sum(tp::int), sum(tp_exc::int) from m group by 1 order by 1""").fetchall()
    tot = con.sql("select count(*), sum(tp::int), sum(tp_exc::int) from m").fetchall()[0]
    gold_all = con.sql("select count(*) from g where item_id is not null").fetchall()[0][0]
    return per_seg, per_rule, tot, gold_all


def pct(a, b):
    return f"{100 * a / b:6.2f}" if b else "   -  "


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="window", choices=["sample", "train", "window"])
    ap.add_argument("--data")
    ap.add_argument("--segments", default=",".join(T2_SEGMENTS), help="segments to show ('all' = every segment)")
    ap.add_argument("--rules", help="only evaluate these side_links rules (comma separated)")
    a = ap.parse_args(argv)
    per_seg, per_rule, tot, gold_all = evaluate(a.phase, a.data, a.rules.split(",") if a.rules else None)
    show = None if a.segments == "all" else set(a.segments.split(","))
    print(f"[{a.phase}] side_links: {tot[0]} pairs, precision {pct(tot[1] or 0, tot[0])} %, "
          f"share of all golden pairs found {pct(tot[1] or 0, gold_all)} %")
    print(f"  {'segment':28s} {'gold':>8s} {'sub':>8s} {'P %':>7s} {'R %':>7s} {'R+exc %':>8s}")
    for seg, gold, sub, tp, tpe in per_seg:
        if show is not None and seg not in show and not sub:
            continue
        print(f"  {str(seg):28s} {gold:8d} {sub:8d} {pct(tp, sub):>7s} {pct(tp, gold):>7s} {pct(tpe, gold):>8s}")
    print("  by rule:")
    for rule, n, tp, tpe in per_rule:
        print(f"    {rule:24s} pairs={n:8d} P={pct(tp or 0, n)} % (with exception {pct(tpe or 0, n)} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
