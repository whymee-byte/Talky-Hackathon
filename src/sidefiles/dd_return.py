"""T2.dd_return · direct-debit R-transactions (OUT line quoting the mandate) → the receipt it reverses → its AR item.

    python -m src.sidefiles.dd_return --learn      # re-learn dd_return_lag.csv from TRAIN < WINDOW_START (no leakage)

Candidates: receipts of the same mandate and amount collected inside the SEPA timing window of the reason code
(MD06 = refund requested by the debtor, weeks later; any other code = within ~2 weeks). Tie-break, learned on
TRAIN < 2024-07 only (dd_return_lag.csv: true delay of unambiguous returns, per group MD06 / other):
  1. batch_id quoted in the text;
  2. MD06: closest to the learned median delay (the delay is ~flat in 21–73 d, the per-day density over-fits);
     other codes: highest learned density of the delay (weekday-shaped: e.g. 10 d ≫ 3 d);
  3. latest collection.
One receipt is reversed once: an ambiguous MD06 refund skips receipts already taken by another return line whose
pick is unambiguous (single candidate) or short-delay (non-MD06); if none is left it keeps its first choice.
Confidence is lowered on ties (n_cand > 1); evidence keeps lag, n_cand and the delay density.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

from src.common import REPO, WINDOW_START, phase_dir, safe_stdout

LAG_TABLE = os.path.join(REPO, "src", "sidefiles", "dd_return_lag.csv")
CODE_RE = r"((?:AC|AM|MD|MS|SL|BE|AG|RR|RC|FF|DT|CN|ED|FO|MI|NO|PY|RF|TM)[0-9]{2})"
NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"


def _p(path: str) -> str:
    return path.replace(os.sep, "/")


def load_lag_table(con) -> float:
    """Creates table ret_lag(md06 bool, lag int, p double); returns the learned MD06 median delay."""
    if os.path.exists(LAG_TABLE):
        con.sql(f"""create or replace table ret_lag as
          select grp = 'MD06' md06, lag::int lag, n::double / sum(n::double) over (partition by grp) p
          from read_csv('{_p(LAG_TABLE)}', header=true, columns={{'grp': 'varchar', 'lag': 'int', 'n': 'bigint'}})""")
        med = con.sql("""select min(lag) from (select lag, sum(p) over (order by lag) cum from ret_lag where md06) where cum >= 0.5""").fetchall()[0][0]
        if med is not None:
            return float(med)
    # no learned table (should not happen: it is versioned): flat density, centre of the MD06 window
    con.sql("create or replace table ret_lag (md06 boolean, lag integer, p double)")
    return 47.0


def step_ret(con, load_dd, resolve_receipts, dirs) -> str | None:
    if not load_dd(con, dirs):
        return None
    med = load_lag_table(con)
    con.sql(f"""create or replace table ret_cand as
      with rl0 as (
        select l.bank_txn_id, l.booking_date, -l.amount_cents amt,
               list_concat(l.batch_refs, regexp_extract_all(l.text_norm, '(?:DEV|REM) ?([0-9]{{11}})', 1)) batch_refs,
               unnest(l.mandate_ids) mandate_id,
               nullif(regexp_extract(' ' || l.text_norm || ' ', ' {CODE_RE} ', 1), '') code
        from lines l where l.direction = 'OUT' and len(l.mandate_ids) > 0),
      rl as (
        -- SEPA R-transaction timing: MD06 (refund requested by the debtor) comes weeks later; other codes within ~2 weeks
        select *, code is not distinct from 'MD06' md06,
                  case when code = 'MD06' then 14 else 2 end lag_min, case when code = 'MD06' or code is null then 80 else 16 end lag_max
        from rl0),
      c as (
        select rl.bank_txn_id, rl.md06, rl.code, d.rid, d.batch_id, d.mandate_id, d.amount_cents, d.collection_date,
               (rl.booking_date - d.collection_date)::int lag, list_contains(rl.batch_refs, d.batch_id) batch_in_text
        from rl join dd d on d.mandate_id = rl.mandate_id and d.amount_cents = rl.amt
                         and d.collection_date <= rl.booking_date - to_days(rl.lag_min)
                         and d.collection_date >= rl.booking_date - to_days(rl.lag_max)),
      c2 as (  -- a batch quoted in the text settles it
        select c.*, coalesce(k.p, 0) p from c left join ret_lag k using (md06, lag)
        qualify batch_in_text or not bool_or(batch_in_text) over (partition by bank_txn_id))
      select *, count(*) over (partition by bank_txn_id) n_cand, sum(p) over (partition by bank_txn_id) p_sum,
             row_number() over (partition by bank_txn_id order by case when md06 then abs(lag - {med}) else -p end,
                                collection_date desc, rid) pr
      from c2""")
    # one reversal per receipt: ambiguous MD06 refunds avoid receipts firmly taken by another line
    con.sql("""create or replace table ret_pick as
      with firm as (select distinct rid, bank_txn_id from ret_cand where pr = 1 and (not md06 or n_cand = 1))
      select c.*, (c.pr > 1) moved from ret_cand c
      qualify row_number() over (partition by c.bank_txn_id order by
              case when c.md06 and c.n_cand > 1 and exists (select 1 from firm f where f.rid = c.rid and f.bank_txn_id <> c.bank_txn_id)
                   then 1 else 0 end, c.pr) = 1""")
    con.sql("""create or replace table ret_src as
      select d.rid, a.entity, d.mandate_id, d.debtor_iban, d.ref, d.amount_cents
      from dd d join (select distinct rid from ret_pick) p using (rid)
      join (select account_id, any_value(entity) entity from lines group by 1) a using (account_id)""")
    resolve_receipts(con, "ret_src", "ret_item")
    con.sql("""create or replace table links_ret as
      select p.bank_txn_id, r.item_id, p.amount_cents, 'RETURNED' exception, 'T2.dd_return' rule, p.batch_id link_key,
             round(case when p.batch_in_text then 1.0 else 0.9 end
                   * case when r.doc_exact or r.doc_digits then 1.0 when r.n_tied = 1 then 0.85 else 0.6 end
                   * case when p.n_cand = 1 then 1.0
                          when p.md06 then 0.65
                          else greatest(0.5, least(1.0, p.p / nullif(p.p_sum, 0))) end, 3) confidence,
             concat('mandate=', p.mandate_id, '; code=', coalesce(p.code, '?'), '; batch_id=', p.batch_id, '; batch_in_text=', p.batch_in_text,
                    '; collected=', p.collection_date, '; lag=', p.lag, '; n_cand=', p.n_cand, '; lag_density=', round(p.p, 4),
                    case when p.moved then '; skipped_receipt_taken' else '' end, '; amount=receipt') evidence
      from ret_pick p join ret_item r using (rid)""")
    return "links_ret"


def learn(data: str | None = None) -> None:
    """True delay of R-transactions on TRAIN booked before WINDOW_START (golden segment ar_dd_returned), counting only
    returns with exactly one candidate receipt whose remittance equals the golden item's document number."""
    t = phase_dir("train", data)
    con = duckdb.connect()
    con.sql(f"""create table r as
      select b.bank_txn_id, b.booking_date::date booking_date, round(b.amount::double * 100)::bigint amount_cents, g.item_id,
             regexp_extract(upper(concat_ws(' ', b.text, b.ref1, b.ref2, b.remittance_info)), 'VTM-[A-Z]{{2}}-[0-9]{{7}}') mandate,
             nullif(regexp_extract(' ' || {NORM.format(x="concat_ws(' ', b.text, b.ref1, b.ref2, b.remittance_info)")} || ' ', ' {CODE_RE} ', 1), '') code
      from read_csv('{_p(t)}/bank_transactions.csv.gz', all_varchar=true) b
      join read_csv('{_p(t)}/golden_allocations.csv.gz', all_varchar=true) g using (bank_txn_id)
      where g.segment = 'ar_dd_returned' and b.amount::double < 0 and b.booking_date < '{WINDOW_START}'""")
    con.sql(f"""create table dd as select distinct batch_id, mandate_id, collection_date::date cd, round(amount::double * 100)::bigint amt,
                {NORM.format(x='remittance_info')} rnorm
                from read_csv('{_p(t)}/direct_debit_batches/[!.]*.csv', all_varchar=true, union_by_name=true)""")
    con.sql(f"""create table doc as select item_id, {NORM.format(x='document_number')} doc_norm
                from read_csv('{_p(t)}/ar_invoices.csv.gz', all_varchar=true)""")
    con.sql("""create table c as
      select r.bank_txn_id, r.code is not distinct from 'MD06' md06, (r.booking_date - d.cd)::int lag, i.item_id is not null is_gold
      from r join dd d on d.mandate_id = r.mandate and d.amt = -r.amount_cents and d.cd between r.booking_date - 90 and r.booking_date - 2
      left join doc i on i.item_id = r.item_id and i.doc_norm = d.rnorm""")
    con.sql(f"""copy (select case when md06 then 'MD06' else 'other' end grp, lag, count(*) n from c
                where is_gold and bank_txn_id in (select bank_txn_id from c group by 1 having sum(is_gold::int) = 1)
                group by all order by 1, 2) to '{_p(LAG_TABLE)}' (header, delimiter ',')""")
    print(con.sql(f"select grp, count(*) lags, sum(n) n from read_csv('{_p(LAG_TABLE)}') group by 1 order by 1").fetchall())


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--learn", action="store_true", help="re-learn dd_return_lag.csv from TRAIN < WINDOW_START")
    ap.add_argument("--data")
    a = ap.parse_args(argv)
    if a.learn:
        learn(a.data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
