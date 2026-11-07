"""Build side_links.parquet for a phase.

    python -m src.sidefiles --phase window [--steps dd]       # steps: dd (more added step by step)
    python -m src.sidefiles --phase live

Rules (rule ids are stable, used in the audit trail):
- T2.dd_batch      batch credit (IN, same account, batch_id in the bank text) → every receipt of the batch;
                   re-presented receipts (re_presentation=Y) carry no exception.
- T2.dd_return     R-transaction (OUT, mandate in the text, batch when quoted) → item of the reversed receipt, RETURNED.
- T2.run_transfer / T2.run_aggregated / T2.run_confirming / T2.run_return   payment_runs/ (see runs.py)
- T2.card / T2.card_chargeback / T2.cheque / T2.cheque_return             deposits.py
- T2.supplier_dd   supplier direct debits without detail file (supplier_dd.py)
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import duckdb

from src.common import REPO, data_root, phase_dir, safe_stdout
from src.sidefiles import deposits, runs, supplier_dd

STEPS = ["dd", "ret", "pr", "card", "chq", "sdd"]
COLUMNS = "bank_txn_id, item_id, amount_cents, exception, rule, link_key, confidence, evidence"


def detail_dirs(phase: str, data: str | None) -> list[str]:
    """Phase folders whose detail files may explain lines of the phase (earlier phases for returns of old batches)."""
    src = {"sample": ["sample"], "train": ["train"], "window": ["train"], "live": ["train", "live"],
           "final": ["train", "live", "final"]}[phase]
    return [phase_dir(s, data) for s in src]


def glob_csv(dirs: list[str], sub: str) -> str | None:
    # macOS can leave AppleDouble sidecars (._name.csv) beside data files.
    # They are not input tables and may not be UTF-8 encoded.
    files = [
        path.replace(os.sep, "/")
        for directory in dirs
        for path in glob.glob(os.path.join(directory, sub, "*.csv"))
        if not os.path.basename(path).startswith("._")
    ]
    if not files:
        return None
    lst = ", ".join(f"'{path}'" for path in files)
    return f"read_csv([{lst}], all_varchar=true, header=true, union_by_name=true, quote='\"', escape='\"', delim=',')"


def ensure_base_parquets(phase: str, data: str | None) -> None:
    """Ensure T2's T1-contract inputs exist, building the temporary CSV adapter if needed.

    T1 currently emits JSONL rather than the shared ``lines.parquet`` and
    ``items.parquet`` contracts. Keep this fallback local to T2 so a clean
    phase run does not depend on someone having invoked ``src.base_parquet``
    manually. Once T1 produces both contract files, this remains a no-op.
    """
    work = os.path.join(REPO, "work", phase)
    required = (os.path.join(work, "lines.parquet"), os.path.join(work, "items.parquet"))
    if all(os.path.isfile(path) for path in required):
        return

    from src.base_parquet.__main__ import build as build_base_parquets

    build_base_parquets(phase, data)
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise FileNotFoundError(
            "base Parquet builder did not produce required T2 inputs: " + ", ".join(missing)
        )


NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"
DIGITS = "nullif(ltrim(regexp_replace(coalesce({x}, ''), '[^0-9]', '', 'g'), '0'), '')"


# development subsets of the window (DECISIONS D1: iterate fast on Q1, measure the full window before each commit)
SUBSETS = {"q1": ("2024-07-01", "2024-09-30")}


def setup(con, phase: str, data: str | None, subset: str | None = None):
    ensure_base_parquets(phase, data)
    work = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    where = ""
    if subset:
        lo, hi = SUBSETS[subset]
        where = f" where booking_date between '{lo}' and '{hi}'"
    con.sql(f"create view lines as select * from '{work}/lines.parquet'{where}")
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
             nullif(regexp_extract({NORM.format(x='s.ref')}, '(?:^| )([0-9]{{4,}})$', 1), '') ref_tail,
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
               or (length(r.ref_digits) >= 6 and ends_with(i.doc_digits, r.ref_digits))
               -- last number group of the reference = last number group of the document (RECIBO 22311 ~ FT24-01-22311)
               or (r.ref_tail is not null and regexp_extract(i.doc_norm, '(?:^| )([0-9]+)$', 1) = r.ref_tail))) doc_digits,
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


def load_dd(con, dirs) -> bool:
    if con.sql("select count(*) from information_schema.tables where table_name = 'dd'").fetchall()[0][0]:
        return True
    src = glob_csv(dirs, "direct_debit_batches")
    if not src:
        return False
    con.sql(f"""create or replace table dd as
      select row_number() over () rid, batch_id, scheme, collection_date::date collection_date, account_id, currency,
             round(batch_total::double * 100)::bigint batch_total_cents, end_to_end_id, mandate_id, debtor_name,
             upper(replace(debtor_iban, ' ', '')) debtor_iban, round(amount::double * 100)::bigint amount_cents,
             remittance_info ref, re_presentation
      from (select distinct * from {src})""")
    return True


def step_dd(con, dirs) -> str | None:
    if not load_dd(con, dirs):
        return None
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


def step_ret(con, dirs) -> str | None:
    """Direct-debit return (R-transaction) → the receipt it reverses → that receipt's item, RETURNED (dd_return.py)."""
    from src.sidefiles import dd_return
    return dd_return.step_ret(con, load_dd, resolve_receipts, dirs)


def out_name(subset: str | None = None, tag: str | None = None) -> str:
    """side_links[_<subset>][_<tag>].parquet — the tag lets parallel dev sessions keep separate outputs."""
    return f"side_links{'_' + subset if subset else ''}{'_' + tag if tag else ''}.parquet"


def build(phase: str, data: str | None, steps: list[str], subset: str | None = None, tag: str | None = None) -> dict:
    con = duckdb.connect()
    setup(con, phase, data, subset)
    dirs = detail_dirs(phase, data)
    tables = []
    for s in steps:
        if s == "sdd":   # runs last: only lines no detail file explained
            taken = " union ".join(f"select bank_txn_id from {t}" for t in tables) or "select null::varchar bank_txn_id where false"
            con.sql(f"create or replace table side_taken as {taken}")
        t = {"dd": step_dd, "ret": step_ret, "sdd": lambda c, d: supplier_dd.step_supplier_dd(c),
             "pr": lambda c, d: runs.step_pr(c, glob_csv(d, "payment_runs")),
             "card": lambda c, d: deposits.step_card(c, glob_csv(d, "card_settlements")),
             "chq": lambda c, d: deposits.step_cheque(c, glob_csv(d, "cheque_deposits"), resolve_receipts)}[s](con, dirs)
        if t:
            tables.append(t)
            n = con.sql(f"select count(*) from {t}").fetchall()[0][0]
            print(f"  step {s:5s} -> {n:8d} pairs", flush=True)
    union = " union all ".join(f"select {COLUMNS} from {t}" for t in tables) or \
        f"select null::varchar bank_txn_id, null::varchar item_id, null::bigint amount_cents, null::varchar exception, null::varchar rule, null::varchar link_key, null::double confidence, null::varchar evidence where false"
    # one row per (line, item): keep the most confident rule
    con.sql(f"""create or replace table side_links as select * from ({union})
               qualify row_number() over (partition by bank_txn_id, item_id order by confidence desc, rule) = 1""")
    out = os.path.join(REPO, "work", phase, out_name(subset, tag)).replace(os.sep, "/")
    con.sql(f"copy side_links to '{out}' (format parquet)")
    return {r[0]: (r[1], r[2]) for r in con.sql("select rule, count(*), count(distinct bank_txn_id) from side_links group by 1 order by 1").fetchall()}


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data")
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--subset", choices=sorted(SUBSETS), help="window only: dev subset (writes side_links_<subset>.parquet)")
    ap.add_argument("--tag", help="dev only: writes side_links[_<subset>]_<tag>.parquet (parallel sessions)")
    a = ap.parse_args(argv)
    t0 = time.time()
    r = build(a.phase, a.data, [s for s in a.steps.split(",") if s], a.subset, a.tag)
    for rule, (n, nl) in r.items():
        print(f"  {rule:22s} pairs={n:8d} lines={nl:7d}")
    print(f"[{a.phase}{'/' + a.subset if a.subset else ''}] side_links -> work/{a.phase}/{out_name(a.subset, a.tag)} ({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
