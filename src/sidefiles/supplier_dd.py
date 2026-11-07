"""T2 step 6 · supplier direct debits (ap_direct_debit): no detail file — the supplier collects from our account.

OUT line (`SEPA LASTSCHRIFT MREF … CRED …`, `PRLV SEPA … RUM …`, `DIRECT DEBIT <supplier> …`, N43 `<supplier> … FRA …`)
→ AP invoice paid by direct debit (`payment_method = dd`) of the same entity whose amount equals the debit, preferring
(1) its number in the bank text, (2) the supplier name in the text, then the oldest issue date. Rule `T2.supplier_dd`.
"""
from __future__ import annotations

NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"


def step_supplier_dd(con) -> str:
    # entity pairs (paying account entity → item entity) learned from the payment runs already linked: the payment
    # factory (POLICIES §5.2) pays subsidiaries' invoices; the same pairs are allowed for direct debits
    has_pr = con.sql("select count(*) from information_schema.tables where table_name = 'links_pr'").fetchall()[0][0]
    con.sql("create or replace table pay_pairs as " + (
        """select distinct l.entity payer, i.entity owner from links_pr x join lines l using (bank_txn_id)
           join items i using (item_id) where l.entity <> i.entity""" if has_pr else
        "select null::varchar payer, null::varchar owner where false"))
    con.sql(f"""create or replace table sdd_cand as
      with l as (select bank_txn_id, entity, booking_date, -amount_cents amt, ' ' || text_norm || ' ' tx
                 from lines where direction = 'OUT'
                 and bank_txn_id not in (select bank_txn_id from side_taken)),
           i as (select i.item_id, i.entity, i.party_id, i.open_at_start_cents, i.total_cents, i.issue_date, i.doc_norm,
                        left({NORM.format(x='s.name')}, 12) sname
                 from items i left join suppliers s on s.supplier_id = i.party_id
                 where i.side = 'AP' and i.kind = 'API' and i.payment_method = 'dd')
      select l.bank_txn_id, i.item_id, i.issue_date, l.booking_date,
             length(i.doc_norm) >= 4 and contains(l.tx, ' ' || i.doc_norm || ' ') doc_in_text,
             length(i.sname) >= 6 and contains(l.tx, ' ' || i.sname) name_in_text
      from l join i on (i.total_cents = l.amt or i.open_at_start_cents = l.amt)
           and (i.entity = l.entity or exists (select 1 from pay_pairs p where p.payer = l.entity and p.owner = i.entity))
           and i.issue_date <= l.booking_date and i.issue_date >= l.booking_date - 400""")
    con.sql("""create or replace table links_sdd as
      with c as (select * from sdd_cand where doc_in_text or name_in_text
                 qualify row_number() over (partition by bank_txn_id order by doc_in_text desc, name_in_text desc, issue_date, item_id) = 1),
           -- an invoice is collected once: keep the earliest debit for each item
           u as (select * from c qualify row_number() over (partition by item_id order by booking_date, bank_txn_id) = 1)
      select u.bank_txn_id, u.item_id, l.amount_cents * -1 amount_cents, '' exception, 'T2.supplier_dd' rule,
             u.item_id link_key,
             case when u.doc_in_text and u.name_in_text then 0.95 when u.doc_in_text then 0.9 else 0.7 end confidence,
             concat('supplier direct debit; amount = invoice; doc_in_text=', u.doc_in_text, '; name_in_text=', u.name_in_text) evidence
      from u join lines l using (bank_txn_id)""")
    return "links_sdd"
