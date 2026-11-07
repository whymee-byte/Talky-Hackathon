"""T2 step 3 · payment_runs/ (our outgoing payment files) → side links.

Docs → AP items by supplier (beneficiary IBAN, else unique name) + supplier document number; the entity of the item is
not required to match the debtor account (payment factory: ES00 pays ES01/ES02/PT01 items). Amount = |document_amount|
(credit notes netted are rows with their full amount and no exception, POLICIES §2).
- transfers:  one debit per end_to_end_id (e2e in the text; N43 cuts it → run_id in the text + same amount)
- aggregated: one debit per run (run_id in the text, amount = run total)
- confirming: one debit per maturity (CF run in the text; docs grouped by the item due date, amount = group total)
"""
from __future__ import annotations

from src.sidefiles import runs_return

NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"


def step_pr(con, src: str | None) -> str | None:
    if not src:
        return None
    con.sql(f"""create or replace table pr as
      -- run ids repeat across entities (same date + sequence): keys are made unique by the debtor account;
      -- run_raw / e2e_norm keep what the bank text quotes
      select row_number() over () did, debtor_account_id || '|' || run_id run_id, run_id run_raw, run_type,
             execution_date::date execution_date, debtor_account_id,
             debtor_account_id || '|' || end_to_end_id end_to_end_id, {NORM.format(x='end_to_end_id')} e2e_norm, beneficiary, {NORM.format(x='beneficiary')} ben_norm,
             upper(replace(beneficiary_iban, ' ', '')) ben_iban, currency,
             round(transfer_amount::double * 100)::bigint transfer_cents, document_number,
             {NORM.format(x='document_number')} doc_norm, document_type,
             round(document_amount::double * 100)::bigint doc_cents
      from (select distinct * from {src})""")
    # restrict to runs whose execution is near the lines of the phase (window/live) — keeps joins small
    con.sql("""delete from pr where execution_date < (select min(booking_date) - 30 from lines)
                               or execution_date > (select max(booking_date) + 5 from lines)""")

    # ---------------------------------------------------------------- docs → items
    con.sql(f"""create or replace table pr_item as
      with sup_iban as (select upper(replace(iban, ' ', '')) iban, any_value(supplier_id) sid from suppliers
                        where iban <> '' group by 1),
           sup_name as (select {NORM.format(x='name')} n, any_value(supplier_id) sid, count(distinct supplier_id) k
                        from suppliers group by 1),
           d as (select p.*, coalesce(si.sid, case when sn.k = 1 then sn.sid end) supplier_id from pr p
                 left join sup_iban si on si.iban = p.ben_iban left join sup_name sn on sn.n = p.ben_norm),
           acc as (select account_id, any_value(entity) entity from lines group by 1),
           cand as (
             select d.did, i.item_id, i.due_date, coalesce(i.party_id = d.supplier_id, false) party_ok,
                    (i.total_cents = abs(d.doc_cents) or i.open_at_start_cents = abs(d.doc_cents)) amt_ok,
                    coalesce(i.entity = a.entity, false) same_entity
             from d join items i on i.side = 'AP' and i.doc_norm = d.doc_norm
                    and i.kind = case when d.document_type = 'CREDIT_NOTE' then 'APC' else 'API' end
             left join acc a on a.account_id = d.debtor_account_id)
      select did, item_id, due_date, party_ok, amt_ok, count(*) over (partition by did) n_cand
      from cand where party_ok or amt_ok
      qualify row_number() over (partition by did order by party_ok desc, amt_ok desc, same_entity desc, item_id) = 1""")

    # ---------------------------------------------------------------- transfers: line ↔ e2e
    con.sql("""create or replace table pr_e2e as
      select run_id, any_value(run_raw) run_raw, end_to_end_id, any_value(e2e_norm) e2e_norm, any_value(debtor_account_id) account_id,
             any_value(execution_date) d, any_value(transfer_cents) cents
      from pr where run_type = 'transfers' group by run_id, end_to_end_id""")
    con.sql("""create or replace table pr_tr_line as
      with c as (
        select e.end_to_end_id, l.bank_txn_id,
               contains(' ' || l.text_norm || ' ', ' ' || e.e2e_norm || ' ') e2e_in_text,
               -l.amount_cents = e.cents amt_ok,
               abs(l.booking_date - e.d) dd
        from pr_e2e e join lines l on l.direction = 'OUT' and l.account_id = e.account_id
              and list_contains(l.batch_refs, e.run_raw) and l.booking_date between e.d - 3 and e.d + 10)
      select * from c where e2e_in_text or amt_ok
      qualify row_number() over (partition by bank_txn_id order by e2e_in_text desc, amt_ok desc, dd, end_to_end_id) = 1""")
    con.sql("""create or replace table pr_tr_line as select * from pr_tr_line
      qualify row_number() over (partition by end_to_end_id order by e2e_in_text desc, amt_ok desc, dd, bank_txn_id) = 1""")

    # ---------------------------------------------------------------- aggregated: line ↔ whole run
    con.sql("""create or replace table pr_ag_line as
      with r as (select run_id, any_value(run_raw) run_raw, any_value(debtor_account_id) account_id, any_value(execution_date) d, sum(cents) total
                 from (select distinct run_id, run_raw, end_to_end_id, debtor_account_id, execution_date, transfer_cents cents
                       from pr where run_type = 'aggregated') group by 1)
      select r.run_id, l.bank_txn_id, -l.amount_cents = r.total total_ok
      from r join lines l on l.direction = 'OUT' and l.account_id = r.account_id and list_contains(l.batch_refs, r.run_raw)
      qualify row_number() over (partition by r.run_id order by (-l.amount_cents = r.total) desc,
                                 abs(l.booking_date - r.d), l.bank_txn_id) = 1""")

    # ---------------------------------------------------------------- confirming: line ↔ (run, maturity)
    # the bank pays each confirming transfer (e2e) at its maturity = latest due date of its documents, and debits us
    # on the first run debit on/after it (weekends shift it 1–2 days); the debit amount checks the grouping.
    con.sql("""create or replace table pr_cf_e2e as
      select p.run_id, any_value(p.run_raw) run_raw, p.end_to_end_id, any_value(p.debtor_account_id) account_id, any_value(p.transfer_cents) cents,
             max(coalesce(pi.due_date, p.execution_date)) due
      from pr p left join pr_item pi using (did) where p.run_type = 'confirming' group by p.run_id, p.end_to_end_id""")
    # (an ASOF assignment by cumulative amounts was tried and measured worse: P 70,8 % vs 87,3 %)
    con.sql("""create or replace table pr_cf_e2e_line as
      select e.run_id, e.end_to_end_id, e.cents, l.bank_txn_id, -l.amount_cents line_cents
      from pr_cf_e2e e join lines l on l.direction = 'OUT' and l.account_id = e.account_id
           and list_contains(l.batch_refs, e.run_raw) and l.booking_date >= e.due and l.booking_date <= e.due + 10
      qualify row_number() over (partition by e.end_to_end_id order by l.booking_date, l.bank_txn_id) = 1""")
    con.sql("""create or replace table pr_cf_line as
      select bank_txn_id, end_to_end_id, run_id, sum(cents) over (partition by bank_txn_id) = line_cents total_ok
      from pr_cf_e2e_line""")

    con.sql("""create or replace table links_pr as
      with m as (
        select t.bank_txn_id, p.did, 'T2.run_transfer' rule, p.end_to_end_id link_key,
               case when t.e2e_in_text and t.amt_ok then 1.0 when t.e2e_in_text then 0.9 else 0.85 end c,
               concat('e2e=', p.end_to_end_id, '; e2e_in_text=', t.e2e_in_text, '; amount_ok=', t.amt_ok) ev
        from pr_tr_line t join pr p using (end_to_end_id)
        union all
        select a.bank_txn_id, p.did, 'T2.run_aggregated', p.run_id, case when a.total_ok then 1.0 else 0.7 end,
               concat('run_id=', p.run_id, '; total_ok=', a.total_ok)
        from pr_ag_line a join pr p on p.run_id = a.run_id and p.run_type = 'aggregated'
        union all
        select c.bank_txn_id, p.did, 'T2.run_confirming', p.end_to_end_id, case when c.total_ok then 1.0 else 0.6 end,
               concat('run_id=', p.run_id, '; e2e=', p.end_to_end_id, '; first debit after maturity; total_ok=', c.total_ok)
        from pr_cf_line c join pr p on p.end_to_end_id = c.end_to_end_id and p.run_type = 'confirming')
      select m.bank_txn_id, pi.item_id, abs(p.doc_cents) amount_cents, '' exception, m.rule, m.link_key,
             round(m.c * case when pi.party_ok and pi.amt_ok then 1.0 when pi.party_ok then 0.9 else 0.75 end, 3) confidence,
             concat(m.ev, '; doc=', p.document_number, '; ', p.document_type, '; party_ok=', pi.party_ok,
                    '; amt_ok=', pi.amt_ok) evidence
      from m join pr p using (did) join pr_item pi using (did)""")
    runs_return.step_pr_return(con)   # returned transfers (runs_return.py)
    con.sql("insert into links_pr select * from links_pr_ret")
    return "links_pr"
