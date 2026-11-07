"""T2 steps 4–5 · card_settlements/ and cheque_deposits/ → side links.

- T2.card    settlement credit (IN, same account, settlement_id or merchant_id in the text, amount = Σ net) → every
             order of the settlement at its **gross** amount (POLICIES §4: the acquirer fee is not an exception);
             order_number = our printed invoice number.
- T2.cheque  deposit credit (IN, same account, deposit_id in the text) → every cheque; stub_reference = our invoice
             number (often without prefix / leading zeros) → resolved by doc number, digits or amount among AR items.
"""
from __future__ import annotations

NORM = "trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"
DIGITS = "nullif(ltrim(regexp_replace(coalesce({x}, ''), '[^0-9]', '', 'g'), '0'), '')"


def step_card(con, src: str | None) -> str | None:
    if not src:
        return None
    con.sql(f"""create or replace table card as
      select row_number() over () cid, settlement_id, merchant_id, settlement_date::date settlement_date, account_id, currency,
             order_number, {NORM.format(x='order_number')} ord_norm, {DIGITS.format(x='order_number')} ord_digits,
             round(gross::double * 100)::bigint gross_cents, round(net::double * 100)::bigint net_cents
      from (select distinct * from {src})""")
    con.sql("""create or replace table card_line as
      with s as (select settlement_id, any_value(merchant_id) merchant_id, any_value(account_id) account_id,
                        any_value(settlement_date) d, sum(net_cents) net, count(*) n from card group by 1)
      select s.settlement_id, l.bank_txn_id, l.amount_cents = s.net net_ok,
             list_contains(l.batch_refs, s.settlement_id) sid_in_text
      from s join lines l on l.direction = 'IN' and l.account_id = s.account_id
            and (list_contains(l.batch_refs, s.settlement_id)
                 or (list_contains(l.batch_refs, s.merchant_id) and l.amount_cents = s.net
                     and l.booking_date between s.d - 2 and s.d + 5))
      qualify row_number() over (partition by s.settlement_id order by (l.amount_cents = s.net) desc,
                                 list_contains(l.batch_refs, s.settlement_id) desc, abs(l.booking_date - s.d)) = 1""")
    con.sql("""create or replace table card_line as select * from card_line
      qualify row_number() over (partition by bank_txn_id order by net_ok desc, sid_in_text desc, settlement_id) = 1""")
    con.sql("""create or replace table card_item as
      with acc as (select account_id, any_value(entity) entity from lines group by 1),
           cand as (
             select c.cid, i.item_id, i.entity = a.entity same_entity, i.doc_norm = c.ord_norm doc_exact,
                    (c.ord_digits is not null and i.doc_digits = c.ord_digits) doc_digits,
                    (i.total_cents = c.gross_cents) amt_ok, i.issue_date
             from card c join card_line x using (settlement_id) join acc a on a.account_id = c.account_id
             join items i on i.side = 'AR' and i.kind = 'ARI' and (i.doc_norm = c.ord_norm or i.doc_digits = c.ord_digits))
      select cid, item_id, doc_exact, amt_ok from cand
      qualify row_number() over (partition by cid order by doc_exact desc, amt_ok desc, same_entity desc, issue_date desc, item_id) = 1""")
    con.sql("""create or replace table links_card as
      select x.bank_txn_id, r.item_id, c.gross_cents amount_cents, '' exception, 'T2.card' rule, c.settlement_id link_key,
             round(case when x.net_ok then 1.0 else 0.8 end * case when r.doc_exact and r.amt_ok then 1.0 when r.doc_exact then 0.9 else 0.8 end, 3) confidence,
             concat('settlement_id=', c.settlement_id, '; merchant=', c.merchant_id, '; net_ok=', x.net_ok, '; order=', c.order_number,
                    '; gross; doc_exact=', r.doc_exact, '; amt_ok=', r.amt_ok) evidence
      from card c join card_line x using (settlement_id) join card_item r using (cid)""")
    return "links_card"


def step_cheque(con, src: str | None, resolve_receipts) -> str | None:
    if not src:
        return None
    con.sql(f"""create or replace table chq as
      select row_number() over () rid, deposit_id, deposit_date::date deposit_date, account_id, currency,
             round(deposit_total::double * 100)::bigint total_cents, cheque_number, drawer, {NORM.format(x='drawer')} drawer_norm,
             round(amount::double * 100)::bigint amount_cents, stub_reference ref
      from (select distinct * from {src})""")
    con.sql("""create or replace table chq_line as
      with d as (select deposit_id, any_value(account_id) account_id, any_value(total_cents) total, any_value(deposit_date) dt from chq group by 1)
      , lr as (select bank_txn_id, account_id, amount_cents, booking_date, unnest(regexp_extract_all(text_norm, '((?:LBX|RCH)[0-9]{9,12})')) ref
               from lines where direction = 'IN')
      select d.deposit_id, l.bank_txn_id, l.amount_cents = d.total total_ok
      from d join lr l on l.account_id = d.account_id and l.ref = d.deposit_id
      qualify row_number() over (partition by d.deposit_id order by (l.amount_cents = d.total) desc, abs(l.booking_date - d.dt)) = 1""")
    # the drawer name gives the customer (exact normalised name / trade name); the stub gives the document
    con.sql(f"""create or replace table chq_src as
      with cn as (select distinct n, entity, customer_id from (
                    select {NORM.format(x='name')} n, entity, customer_id from customers
                    union all
                    select {NORM.format(x='trade_name')} n, entity, customer_id from customers where trade_name <> '')),
           acc as (select account_id, any_value(entity) entity from lines group by 1)
      -- one row per (cheque, homonymous customer): the amount / stub decides among homonyms
      select c.rid, a.entity, null mandate_id, null debtor_iban, c.ref, c.amount_cents, cn.customer_id party_hint, c.drawer_norm
      from chq c join chq_line x using (deposit_id) join acc a on a.account_id = c.account_id
      left join cn on cn.n = c.drawer_norm and cn.entity = a.entity""")
    # 1) with a known drawer: resolve among the customer's items; 2) else: by document reference within the entity
    # suffixes (5–10 digits) of AR document digits → hash-joinable "stub without prefix / leading zeros" lookup
    con.sql("""create or replace table sfx as
      select item_id, right(doc_digits, n) suffix from items, range(5, 11) t(n)
      where side = 'AR' and kind = 'ARI' and length(doc_digits) >= n""")
    con.sql("""create or replace table chq_item as
      with r as (select *, nullif(ltrim(regexp_replace(coalesce(ref, ''), '[^0-9]', '', 'g'), '0'), '') ref_digits,
                           trim(regexp_replace(strip_accents(upper(coalesce(ref, ''))), '[^A-Z0-9]+', ' ', 'g')) ref_norm from chq_src),
           cand as (
             select r.rid, i.item_id, (i.party_id = r.party_hint) party_ok,
                    (i.doc_norm = r.ref_norm) doc_exact,
                    (r.ref_digits is not null and (i.doc_digits = r.ref_digits or (length(r.ref_digits) >= 5 and ends_with(i.doc_digits, r.ref_digits)))) doc_digits,
                    (i.total_cents = r.amount_cents or i.open_at_start_cents = r.amount_cents) amt_ok, i.issue_date
             from r join items i on i.side = 'AR' and i.kind = 'ARI' and i.entity = r.entity
                   and (i.party_id = r.party_hint or i.doc_norm = r.ref_norm
                        or i.item_id in (select item_id from sfx where sfx.suffix = r.ref_digits)))
      select rid, item_id, party_ok, doc_exact, doc_digits, amt_ok,
             count(*) over (partition by rid) n_cand
      from cand where (party_ok and (doc_exact or doc_digits or amt_ok)) or ((doc_exact or doc_digits) and amt_ok)
      qualify row_number() over (partition by rid order by party_ok desc, doc_exact desc, doc_digits desc, amt_ok desc, issue_date, item_id) = 1""")
    con.sql("""create or replace table links_chq as
      select x.bank_txn_id, r.item_id, c.amount_cents, '' exception, 'T2.cheque' rule, c.deposit_id link_key,
             round(case when x.total_ok then 1.0 else 0.8 end
                   * case when r.party_ok and (r.doc_exact or r.doc_digits) then 1.0 when r.party_ok and r.n_cand = 1 then 0.85
                          when r.doc_exact or r.doc_digits then 0.8 else 0.6 end, 3) confidence,
             concat('deposit_id=', c.deposit_id, '; total_ok=', x.total_ok, '; drawer=', c.drawer, '; stub=', c.ref,
                    '; party_ok=', r.party_ok, '; doc=', (r.doc_exact or r.doc_digits), '; amt=', r.amt_ok) evidence
      from chq c join chq_line x using (deposit_id) join chq_item r using (rid)""")
    return "links_chq"
