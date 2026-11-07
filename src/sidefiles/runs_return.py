"""T2 · returned transfers (ap_returned_transfer): the beneficiary bank rejects one of our payment_runs/ transfers.

Needs tables `pr` and `pr_item` built by runs.step_pr. Writes table `links_pr_ret` (contract §3 columns).
"""
from __future__ import annotations


def step_pr_return(con) -> None:
    # ---------------------------------------------------------------- returned transfers (beneficiary bank rejects)
    # IN line quoting the e2e of our order ("RETURN <code> … PR… nnnn" / "DEVOLUCION TRANSFERENCIA …") → the documents of
    # that transfer, RETURNED, amount = the original allocation (POLICIES §3)
    con.sql("""create or replace table links_pr_ret as
      with e as (select distinct end_to_end_id, e2e_norm, debtor_account_id account_id, execution_date d, transfer_cents from pr),
           m as (select e.end_to_end_id, l.bank_txn_id, l.amount_cents = e.transfer_cents amt_ok
                 from e join lines l on l.direction = 'IN' and l.account_id = e.account_id
                      and contains(' ' || l.text_norm || ' ', ' ' || e.e2e_norm || ' ')
                      and l.booking_date between e.d and e.d + 60
                 qualify row_number() over (partition by l.bank_txn_id order by l.amount_cents = e.transfer_cents desc, e.d desc) = 1)
      select m.bank_txn_id, pi.item_id, abs(p.doc_cents) amount_cents, 'RETURNED' exception, 'T2.run_return' rule,
             p.end_to_end_id link_key, round(case when m.amt_ok then 1.0 else 0.8 end * case when pi.party_ok then 1.0 else 0.85 end, 3) confidence,
             concat('returned e2e=', p.end_to_end_id, '; amount_ok=', m.amt_ok, '; doc=', p.document_number) evidence
      from m join pr p using (end_to_end_id) join pr_item pi using (did)""")

    # ---------------------------------------------------------------- N43 returns without e2e
    # "DEVOLUCION TRANSFERENCIA <code> <beneficiary>" (N43 drops the e2e): same debtor account, amount = transfer amount,
    # beneficiary name (prefix, N43 truncates it) in the text, executed in the 60 days before. Each e2e is returned once.
    con.sql("""create or replace table links_pr_ret_n43 as
      with l as (select bank_txn_id, account_id, booking_date, amount_cents, ' ' || text_norm || ' ' tx from lines
                 where direction = 'IN' and regexp_matches(text_norm, '(DEVOLUCION TRANSFERENCIA|RETURN) [A-Z]{2}[0-9]{2} ')
                 and bank_txn_id not in (select bank_txn_id from links_pr_ret)),
           e as (select end_to_end_id, any_value(debtor_account_id) account_id, any_value(execution_date) d,
                        any_value(transfer_cents) cents, any_value(ben_norm) ben
                 from pr where end_to_end_id not in (select link_key from links_pr_ret) group by 1),
           c as (select l.bank_txn_id, e.end_to_end_id, e.d, count(*) over (partition by l.bank_txn_id) n_cand
                 from l join e on e.account_id = l.account_id and e.cents = l.amount_cents
                      and e.d between l.booking_date - 60 and l.booking_date
                      and length(e.ben) >= 6 and contains(l.tx, ' ' || left(e.ben, least(length(e.ben), 14))))
      select * from c
      qualify row_number() over (partition by bank_txn_id order by d desc, end_to_end_id) = 1
          and row_number() over (partition by end_to_end_id order by d desc, bank_txn_id) = 1""")
    con.sql("""insert into links_pr_ret
      select m.bank_txn_id, pi.item_id, abs(p.doc_cents) amount_cents, 'RETURNED' exception, 'T2.run_return_n43' rule,
             p.end_to_end_id link_key,
             round(case when m.n_cand = 1 then 0.9 else 0.6 end * case when pi.party_ok then 1.0 else 0.85 end, 3) confidence,
             concat('returned e2e=', p.end_to_end_id, ' (N43: account + amount + beneficiary name); candidates=', m.n_cand,
                    '; doc=', p.document_number) evidence
      from links_pr_ret_n43 m join pr p using (end_to_end_id) join pr_item pi using (did)""")
