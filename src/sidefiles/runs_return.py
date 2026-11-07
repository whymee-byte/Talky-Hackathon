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
