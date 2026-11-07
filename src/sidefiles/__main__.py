"""Build side_links.parquet for a phase.

    python -m src.sidefiles --phase window [--steps dd]       # steps: dd (more added step by step)
    python -m src.sidefiles --phase live

Rules (rule ids are stable, used in the audit trail):
- T2.dd_batch      batch credit (IN, same account, batch_id in the bank text) → every receipt of the batch;
                   re-presented receipts (re_presentation=Y) carry no exception.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

from src.common import REPO, data_root, phase_dir, safe_stdout

STEPS = ["dd"]
COLUMNS = "bank_txn_id, item_id, amount_cents, exception, rule, link_key, confidence, evidence"


def detail_dirs(phase: str, data: str | None) -> list[str]:
    """Phase folders whose detail files may explain lines of the phase (earlier phases for returns of old batches)."""
    src = {"sample": ["sample"], "train": ["train"], "window": ["train"], "live": ["train", "live"],
           "final": ["train", "live", "final"]}[phase]
    return [phase_dir(s, data) for s in src]


def glob_csv(dirs: list[str], sub: str) -> str | None:
    pats = [os.path.join(d, sub, "*.csv").replace(os.sep, "/") for d in dirs if os.path.isdir(os.path.join(d, sub))]
    if not pats:
        return None
    lst = ", ".join(f"'{p}'" for p in pats)
    return f"read_csv([{lst}], all_varchar=true, header=true, union_by_name=true, quote='\"', escape='\"', delim=',')"


NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"
DIGITS = "nullif(ltrim(regexp_replace(coalesce({x}, ''), '[^0-9]', '', 'g'), '0'), '')"


def setup(con, phase: str, data: str | None):
    work = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    con.sql(f"create view lines as select * from '{work}/lines.parquet'")
    con.sql(f"create view items as select * from '{work}/items.parquet'")
    masters = os.path.join(data_root(data), "masters").replace(os.sep, "/")
    q = "quote='\"', escape='\"', delim=','"
    con.sql(f"create view customers as select * from read_csv('{masters}/customers.csv', all_varchar=true, header=true, {q})")
    con.sql(f"create view suppliers as select * from read_csv('{masters}/suppliers.csv', all_varchar=true, header=true, {q})")


def resolve_receipts(con, src_table: str, out_table: str):
    """Receipt (party via mandate/IBAN, reference text, amount) → best AR item, by joins.

    src_table needs: rid, entity, mandate_id, debtor_iban, ref, amount_cents.
    Score: exact doc number 4 · doc digits (equal or suffix ≥ 6) 3 · amount == open/total 2 · same entity 1.
    """
    con.sql(f"""
    create or replace table {out_table} as
    with r as (
      select s.*, {NORM.format(x='s.ref')} ref_norm, {DIGITS.format(x='s.ref')} ref_digits,
             coalesce(cm.customer_id, ci.customer_id) party_id
      from {src_table} s
      left join (select sepa_mandate_id, any_value(customer_id) customer_id from customers where sepa_mandate_id <> '' group by 1) cm
             on cm.sepa_mandate_id = s.mandate_id
      left join (select iban, any_value(customer_id) customer_id from customers where iban <> '' group by 1) ci
             on ci.iban = s.debtor_iban
    ),
    cand as (
      select r.rid, i.item_id, i.entity = r.entity same_entity,
             (i.doc_norm = r.ref_norm or i.doc_norm = replace(r.ref_norm, ' ', '')) doc_exact,
             (r.ref_digits is not null and (i.doc_digits = r.ref_digits
               or (length(r.ref_digits) >= 6 and ends_with(i.doc_digits, r.ref_digits)))) doc_digits,
             (i.open_at_start_cents = r.amount_cents or i.total_cents = r.amount_cents) amt_ok,
             i.issue_date
      from r join items i on i.party_id = r.party_id and i.side = 'AR' and i.kind = 'ARI'
    ),
    scored as (
      select *, 4 * doc_exact::int + 3 * (doc_digits and not doc_exact)::int + 2 * amt_ok::int + same_entity::int score
      from cand where doc_exact or doc_digits or amt_ok
    )
    select rid, item_id, score, doc_exact, doc_digits, amt_ok,
           count(*) over (partition by rid, score) n_tied
    from scored
    qualify row_number() over (partition by rid order by score desc, issue_date, item_id) = 1
    """)


def step_dd(con, dirs) -> str | None:
    src = glob_csv(dirs, "direct_debit_batches")
    if not src:
        return None
    con.sql(f"""create or replace table dd as
      select row_number() over () rid, batch_id, scheme, collection_date::date collection_date, account_id, currency,
             round(batch_total::double * 100)::bigint batch_total_cents, end_to_end_id, mandate_id, debtor_name,
             upper(replace(debtor_iban, ' ', '')) debtor_iban, round(amount::double * 100)::bigint amount_cents,
             remittance_info ref, re_presentation
      from (select distinct * from {src})""")
    # batch credit line: IN on the batch account, batch_id among the references of the text
    con.sql("""create or replace table dd_line as
      with b as (select batch_id, any_value(account_id) account_id, any_value(batch_total_cents) total, count(*) n,
                        min(collection_date) d from dd group by 1)
      select b.batch_id, l.bank_txn_id, l.amount_cents = b.total total_ok, b.n
      from b join lines l on l.account_id = b.account_id and l.direction = 'IN' and list_contains(l.batch_refs, b.batch_id)
      qualify row_number() over (partition by b.batch_id order by (l.amount_cents = b.total) desc, abs(l.booking_date - b.d)) = 1""")
    con.sql("""create or replace table dd_src as
      select d.rid, a.entity, d.mandate_id, d.debtor_iban, d.ref, d.amount_cents
      from dd d join dd_line x using (batch_id) join (select account_id, any_value(entity) entity from lines group by 1) a using (account_id)""")
    resolve_receipts(con, "dd_src", "dd_item")
    con.sql("""create or replace table links_dd as
      select x.bank_txn_id, r.item_id, d.amount_cents, '' exception, 'T2.dd_batch' rule, d.batch_id link_key,
             round(case when x.total_ok then 1.0 else 0.8 end
                   * case when r.doc_exact or r.doc_digits then 1.0 when r.n_tied = 1 then 0.85 else 0.6 end, 3) confidence,
             concat('batch_id=', d.batch_id, '; total_ok=', x.total_ok, '; mandate=', d.mandate_id, '; ref=', d.ref,
                    '; doc=', (r.doc_exact or r.doc_digits), '; amt=', r.amt_ok, case when d.re_presentation = 'Y' then '; re_presentation' else '' end) evidence
      from dd d join dd_line x using (batch_id) join dd_item r using (rid)""")
    return "links_dd"


def build(phase: str, data: str | None, steps: list[str]) -> dict:
    con = duckdb.connect()
    setup(con, phase, data)
    dirs = detail_dirs(phase, data)
    tables = []
    for s in steps:
        t = {"dd": step_dd}[s](con, dirs)
        if t:
            tables.append(t)
    union = " union all ".join(f"select {COLUMNS} from {t}" for t in tables) or \
        f"select null::varchar bank_txn_id, null::varchar item_id, null::bigint amount_cents, null::varchar exception, null::varchar rule, null::varchar link_key, null::double confidence, null::varchar evidence where false"
    # one row per (line, item): keep the most confident rule
    con.sql(f"""create or replace table side_links as select * from ({union})
               qualify row_number() over (partition by bank_txn_id, item_id order by confidence desc, rule) = 1""")
    out = os.path.join(REPO, "work", phase, "side_links.parquet").replace(os.sep, "/")
    con.sql(f"copy side_links to '{out}' (format parquet)")
    return {r[0]: (r[1], r[2]) for r in con.sql("select rule, count(*), count(distinct bank_txn_id) from side_links group by 1 order by 1").fetchall()}


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data")
    ap.add_argument("--steps", default=",".join(STEPS))
    a = ap.parse_args(argv)
    t0 = time.time()
    r = build(a.phase, a.data, [s for s in a.steps.split(",") if s])
    for rule, (n, nl) in r.items():
        print(f"  {rule:22s} pairs={n:8d} lines={nl:7d}")
    print(f"[{a.phase}] side_links -> work/{a.phase}/side_links.parquet ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
