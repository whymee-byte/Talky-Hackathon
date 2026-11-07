"""T15 · last exception fixes on the final allocations (runs after src.unident / src.cnfx), POLICIES §2–3.

    python -m src.excfix --phase window|live|final [--alloc work/<fase>/allocations.parquet] [--dst …]

remaining = open at start − what earlier lines of the phase applied to the item (RETURNED rows re-open, DUPLICATE rows
excluded), ordered by booking date. All rules below act on single-row receipts unless stated.

Step 1 · pairs re-pointed or dropped (the item was wrong):
  settle_switch  text says the receipt settles a dispute ("SETTLEMENT", "REGULARIZACION": in TRAIN only dispute
                 settlements and card settlements use the word), our item is not cited in the text and exactly one open
                 invoice of the same customer cited in the text has open > cash → that invoice, amount = cash, no
                 exception (the customer pays the disputed remainder; the dispute line itself was missed upstream)
  over_switch    OVERPAYMENT whose excess is neither a whole number of currency units nor exactly 1 % / 10 % (TRAIN
                 overpayments are always one of those) and exactly one untouched open invoice of the customer has
                 total = cash → that invoice, paid in full, no exception (typically the replacement of a cancelled invoice)
  dead_fifo      T5 partial / overpayment on an item our own earlier rows had already settled → the oldest untouched
                 open invoice of the customer with total = cash, if any (FIFO, POLICIES §5.6)
  dead_drop      same, without such an invoice → the pair is dropped (category row, no exception: TRAIN 97 % wrong pairs)
  tiny_drop      T5 partial found only through a PO number or a suffix of the reference, cash < 10 % of the balance →
                 dropped (TRAIN: 0 of 82 right; real small payments on a cited invoice are dispute settlements)
  odd_drop       OVERPAYMENT found by amount only (T5b.over_amt) with a whole-unit excess above 1.1 % that is not a
                 multiple of 10 units → dropped (TRAIN: 8 right / 30 wrong; real round-ups are tens or hundreds)
  cancel_drop    3-row receipt: an invoice, its own credit note for the same amount (related_doc) and a third invoice
                 equal to the cash → the pair is removed (cancelled invoice and replacement; TRAIN drops it 273 / 290)
Step 2 · item-row relabels, with the running balance:
  inst_not_last  instalment row marked "last" (no exception) that leaves a balance and the item is paid again by a later
                 line → PARTIAL (amount = applied)
  over_single    single-invoice receipt (same currency) with cash > remaining and an excess that is a whole number of
                 currency units (≤ 50 %) or exactly 1 % / 10 % of the balance → OVERPAYMENT at the remaining amount
  over_frac      OVERPAYMENT whose excess is none of those shapes → no exception (the pair may still be right: 1:N
                 receipts where the rest went elsewhere)
  settle_noexc   PARTIAL / OVERPAYMENT on a dispute-settlement receipt (text, see settle_switch) → no exception
  rest_noexc     PARTIAL on a receipt whose text says it pays the remainder ("BALANCE", "RESTO", "SOLDE", "REST",
                 "RESTANTE": TRAIN 1 695 / 1 695 are the closing payment of a partially paid invoice) → no exception
  dup_fuzzy      DUPLICATE_PAYMENT set only because an earlier fuzzy-reference row (T3f / T5b.fill) consumed the item
                 → no exception: the earlier fuzzy allocation is the doubtful one, the window shows these rows are normal
  over_replaced  OVERPAYMENT on the replacement of a cancelled invoice ("anula y sustituye") → no exception (the
                 customer pays the old document; the replacement is cleared as documented)
  remit_rest     remittance receipt whose advice rows were all prorated as PARTIAL (T6 short of the cash on every
                 document): TRAIN pays every document in full except one that takes the whole shortfall → full amounts,
                 no exception, and the newest invoice keeps PARTIAL at open − shortfall (newest: 55 % in TRAIN)
Rows added on receipts that ended with no item (all rows category), using T4's top-ranked party:
  bc_add         exactly one unused open invoice of the party, same currency, with 0 < open − cash ≤ 2 % of open and
                 ≤ 60 EUR equivalent (EUR lines: a multiple of 5 EUR), on a non-SEPA receipt (XBCT / N43 13 / NTRF /
                 SHA·BEN·CHGS), not in a withholding entity → SHORT_BANK_CHARGES at the open amount
  over_add       exactly one unused open invoice with an overpayment-shaped excess (see over_single) → OVERPAYMENT at
                 the open amount
  disc_add       exactly one unused untouched invoice whose discounted total (terms %, booked ≤ issue + days + 3) = cash
                 → SHORT_DISCOUNT at the open amount
  dup_add        exactly one invoice of the party already cleared (before the phase or by an earlier line) whose total
                 = the cash → DUPLICATE_PAYMENT (amount = cash)
Labels on rows whose pair and amount are already right: fx_flag, wht_multi (see _label_fixes).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, data_root
from src.common.profile import sql_wht_min_diff, sql_wht_parties, withholding

TOL = "greatest(1, 0.005 * abs(remaining))"
NONSEPA = ("({t}bank_code LIKE '%XBCT' OR ({t}statement_format = 'n43' AND {t}bank_code = '13') OR {t}bank_code = 'NTRF'"
           " OR regexp_matches(coalesce({t}text_norm, ''), '\bCHGS\b|\b(SHA|BEN)\b'))")
BC_EUR_MAX = 6000          # POLICIES §3 SHORT_BANK_CHARGES: shortfall ≤ 60 EUR equivalent …
BC_PCT = 0.02              # … and ≤ 2 % of the item
BC_EUR_STEP = 500          # bc_add on EUR lines: the fee is a whole multiple of 5 EUR (TRAIN shortfall distribution)
OVER_MAX = 1.5             # whole-unit excess up to 50 % (TRAIN overpayments: whole units ≤ 2.1×, or exactly 1 % / 10 %)
OVER_RATES = (1.01, 1.10)  # the two percentage overpayments seen in TRAIN (rounding of the customer's own total)
TINY_SHARE = 0.10          # tiny_drop: cash below 10 % of the balance
OVER_WHOLE_SMALL = 1.011   # odd_drop: an amount-only overpayment above 1.1 % …
OVER_ODD_STEP = 1000       # … whose excess is not a multiple of 10 units is a wrong invoice (TRAIN 8 right / 30 wrong)
# dispute settlement / closing payment of a partially paid invoice: words of the bank text (TRAIN: no other class uses
# them on single-invoice receipts); a few synonyms added for other languages (harmless in TRAIN)
KW_SETTLE = r"\b(SETTLEMENT|REGULARIZACION|REGULARISATION)\b"
KW_REST = r"\b(BALANCE|RESTO|RESTANTE|REST|SOLDE|REMAINDER|REMAINING|RESTANT|RESTBETRAG|RESTZAHLUNG|RESZTA)\b"
# SHORT_WITHHOLDING customers: learned from TRAIN + POLICIES §3 by customer country (src.common.profile), not entity ids


def _over_shape(cash: str, base: str) -> str:
    """SQL: the excess of cash over base has the shape of a TRAIN overpayment."""
    rates = " OR ".join(f"abs({cash} - round({base} * {r})) <= 1" for r in OVER_RATES)
    return f"((({cash} - {base}) % 100 = 0 AND {cash} <= {OVER_MAX} * {base}) OR {rates})"


def _cited(text: str, t: str) -> str:
    """SQL: the document number of item alias t appears in the bank text."""
    return f"(contains({text}, {t}.doc_norm) OR (length({t}.doc_digits) >= 6 AND contains({text}, {t}.doc_digits)))"


def _build_x(con) -> None:
    """X: every item row with its line, item and running balance (remaining / later)."""
    con.execute("DROP TABLE IF EXISTS X")
    con.execute(f"""CREATE TABLE X AS
      SELECT a.bank_txn_id, a.item_id, a.amount_cents amt, coalesce(a.exception, '') exc, a.rule, coalesce(a.evidence, '') evidence,
             i.kind, i.currency icur, i.party_id, i.issue_date, i.doc_norm, i.doc_digits, i.open_at_start_cents open0,
             l.currency lcur, l.direction, l.booking_date bd, abs(l.amount_cents) cash, coalesce(l.text_norm, '') text_norm,
             l.entity, l.bank_code, l.statement_format, fx.u,
             regexp_matches(coalesce(l.text_norm, ''), '{KW_SETTLE}') kw_settle,
             regexp_matches(coalesce(l.text_norm, ''), '{KW_REST}') kw_rest,
             count(*) OVER (PARTITION BY a.bank_txn_id) nrow,
             i.open_at_start_cents - coalesce(sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END)
                 FILTER (WHERE coalesce(a.exception, '') <> 'DUPLICATE_PAYMENT')
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) remaining,
             count(*) FILTER (WHERE coalesce(a.exception, '') NOT IN ('DUPLICATE_PAYMENT', 'RETURNED'))
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) later
      FROM A a JOIN I i USING (item_id) JOIN L l USING (bank_txn_id)
      ASOF LEFT JOIN FX fx ON fx.currency = upper(l.currency) AND fx.d <= l.booking_date
      WHERE a.item_id IS NOT NULL""")


def _repoint(con) -> list:
    """Step 1: single-row receipts whose item is wrong → another invoice of the customer, or no item at all."""
    over = _over_shape("s.cash", "s.remaining")
    con.execute(f"""CREATE TABLE SW AS
      WITH s AS (SELECT * FROM X WHERE nrow = 1 AND direction = 'IN' AND kind = 'ARI' AND icur = lcur),
      settle AS (
        SELECT s.bank_txn_id, s.item_id old_item, i2.item_id new_item, s.cash, 'settle_switch' why
        FROM s JOIN I i2 ON i2.party_id = s.party_id AND i2.kind = 'ARI' AND i2.currency = s.lcur AND i2.item_id <> s.item_id
             AND i2.issue_date <= s.bd AND i2.open_at_start_cents > s.cash AND {_cited('s.text_norm', 'i2')}
        WHERE s.kw_settle AND s.rule NOT LIKE 'T2b.%' AND NOT {_cited('s.text_norm', 's')}
        QUALIFY count(*) OVER (PARTITION BY s.bank_txn_id) = 1),
      over AS (
        SELECT s.bank_txn_id, s.item_id, i2.item_id, s.cash, 'over_switch'
        FROM s JOIN I i2 ON i2.party_id = s.party_id AND i2.kind = 'ARI' AND i2.currency = s.lcur AND i2.item_id <> s.item_id
             AND i2.issue_date <= s.bd AND i2.open_at_start_cents = i2.total_cents AND i2.total_cents = s.cash
        WHERE s.exc = 'OVERPAYMENT' AND s.remaining > 0 AND s.cash > s.remaining AND NOT {over}
          AND s.bank_txn_id NOT IN (SELECT bank_txn_id FROM settle)
        QUALIFY count(*) OVER (PARTITION BY s.bank_txn_id) = 1),
      dead AS (
        SELECT s.bank_txn_id, s.item_id, i2.item_id, s.cash, 'dead_fifo'
        FROM s JOIN I i2 ON i2.party_id = s.party_id AND i2.kind = 'ARI' AND i2.currency = s.lcur AND i2.item_id <> s.item_id
             AND i2.issue_date <= s.bd AND i2.open_at_start_cents = i2.total_cents AND i2.total_cents = s.cash
             AND NOT EXISTS (SELECT 1 FROM A a2 WHERE a2.item_id = i2.item_id)
        WHERE s.remaining <= 0 AND s.exc IN ('PARTIAL', '') AND (s.rule LIKE 'T5.partial%' OR s.rule LIKE 'T5.overpayment%')
          AND s.bank_txn_id NOT IN (SELECT bank_txn_id FROM settle)
        QUALIFY row_number() OVER (PARTITION BY s.bank_txn_id ORDER BY i2.issue_date, i2.item_id) = 1)
      SELECT * FROM settle UNION ALL SELECT * FROM over UNION ALL SELECT * FROM dead""")
    con.execute(f"""CREATE TABLE DROPPED AS
      SELECT bank_txn_id, item_id, remaining, cash, rule old_rule,
        CASE WHEN remaining <= 0 AND exc IN ('PARTIAL', '') AND (rule LIKE 'T5.partial%' OR rule LIKE 'T5.overpayment%') THEN 'dead_drop'
             WHEN exc = 'OVERPAYMENT' AND rule LIKE 'T5b.over_amt%' AND remaining > 0 AND (cash - remaining) % 100 = 0
                  AND cash > {OVER_WHOLE_SMALL} * remaining AND (cash - remaining) % {OVER_ODD_STEP} <> 0 THEN 'odd_drop'
             WHEN exc = 'PARTIAL' AND rule LIKE 'T5.partial%' AND remaining > 0 AND cash < {TINY_SHARE} * remaining
                  AND (evidence LIKE '%po=%' OR evidence LIKE '%suf=%') AND evidence NOT LIKE '%alnum=%' AND evidence NOT LIKE '%dig=%'
                  AND NOT kw_settle THEN 'tiny_drop' END why
      FROM X WHERE nrow = 1 AND direction = 'IN' AND kind = 'ARI' AND icur = lcur
        AND bank_txn_id NOT IN (SELECT bank_txn_id FROM SW)""")
    con.execute("DELETE FROM DROPPED WHERE why IS NULL")
    con.execute("""UPDATE A SET item_id = sw.new_item, amount_cents = sw.cash, exception = NULL,
                     rule = A.rule || '+T15.' || sw.why, confidence = 0.7,
                     evidence = coalesce(A.evidence, '') || '; T15 ' || sw.why || ' from=' || sw.old_item
                   FROM SW sw WHERE A.bank_txn_id = sw.bank_txn_id AND A.item_id = sw.old_item""")
    con.execute("DELETE FROM A WHERE (bank_txn_id, item_id) IN (SELECT (bank_txn_id, item_id) FROM DROPPED)")
    con.execute("""INSERT INTO A BY NAME
                   SELECT bank_txn_id, NULL::VARCHAR item_id, NULL::BIGINT amount_cents, 'UNIDENTIFIED' category, NULL::VARCHAR exception,
                          'T15.' || why rule, 0.5 confidence,
                          'pair dropped: ' || why || ' item=' || item_id || ' remaining=' || remaining || ' cash=' || cash || '; was ' || old_rule evidence
                   FROM DROPPED""")
    # cancelled invoice + its own credit note (same amount) netted into a receipt that a third invoice explains alone:
    # the replacement invoice is what the customer pays ("anula y sustituye", DOMAIN §6); TRAIN drops the pair 273 / 290
    con.execute("""CREATE TABLE CDROP AS
      SELECT a.bank_txn_id, a.item_id ari, c.item_id arc, o.item_id keep
      FROM X a JOIN X c ON c.bank_txn_id = a.bank_txn_id AND c.kind = 'ARC' AND c.amt = a.amt
      JOIN I ia ON ia.item_id = a.item_id JOIN I ic ON ic.item_id = c.item_id
      JOIN X o ON o.bank_txn_id = a.bank_txn_id AND o.kind = 'ARI' AND o.item_id <> a.item_id AND abs(o.amt - a.cash) <= 1
      WHERE a.nrow = 3 AND a.kind = 'ARI' AND a.direction = 'IN' AND ic.related_doc IN (ia.doc_number, ia.item_id)
      QUALIFY count(*) OVER (PARTITION BY a.bank_txn_id) = 1""")
    con.execute("DELETE FROM A WHERE (bank_txn_id, item_id) IN (SELECT (bank_txn_id, ari) FROM CDROP UNION ALL SELECT (bank_txn_id, arc) FROM CDROP)")
    con.execute("""UPDATE A SET rule = A.rule || '+T15.cancel_drop',
                     evidence = coalesce(A.evidence, '') || '; T15 cancel_drop: ' || d.ari || ' + ' || d.arc || ' removed'
                   FROM CDROP d WHERE A.bank_txn_id = d.bank_txn_id AND A.item_id = d.keep""")
    return (con.sql("SELECT why, count(*) FROM SW GROUP BY 1 ORDER BY 1").fetchall()
            + con.sql("SELECT why, count(*) FROM DROPPED GROUP BY 1 ORDER BY 1").fetchall()
            + con.sql("SELECT 'cancel_drop', count(*) FROM CDROP").fetchall())


def _relabel(con) -> list:
    """Step 2: exception / amount relabels on item rows, with the running balance."""
    over = _over_shape("cash", "remaining")
    con.execute(f"""CREATE TABLE FIX AS
      SELECT bank_txn_id, item_id, remaining,
        CASE WHEN exc = '' AND rule LIKE '%inst_last%' AND kind IN ('ARI', 'API') AND remaining > 0
                  AND amt < remaining - {TOL} AND later > 0 THEN 'inst_not_last'
             WHEN exc = '' AND nrow = 1 AND kind = 'ARI' AND direction = 'IN' AND icur = lcur AND remaining > 0
                  AND cash > remaining + {TOL} AND {over} THEN 'over_single'
             WHEN exc = 'OVERPAYMENT' AND rule LIKE '%T8b.replaces%' THEN 'over_replaced'
             WHEN exc = 'DUPLICATE_PAYMENT' AND rule LIKE '%+T5b.dup' AND (rule LIKE 'T3f.%' OR rule LIKE 'T5b.fill_%') THEN 'dup_fuzzy'
             WHEN exc IN ('PARTIAL', 'OVERPAYMENT') AND nrow = 1 AND kind = 'ARI' AND direction = 'IN' AND kw_settle
                  AND rule NOT LIKE 'T2b.%' THEN 'settle_noexc'
             WHEN exc = 'PARTIAL' AND nrow = 1 AND kind = 'ARI' AND direction = 'IN' AND kw_rest THEN 'rest_noexc'
             WHEN exc = 'OVERPAYMENT' AND nrow = 1 AND kind = 'ARI' AND direction = 'IN' AND icur = lcur AND remaining > 0
                  AND cash > remaining AND NOT {over} THEN 'over_frac'
        END why
      FROM X""")
    con.execute("DELETE FROM FIX WHERE why IS NULL")
    con.execute("""UPDATE A SET
        exception = CASE f.why WHEN 'inst_not_last' THEN 'PARTIAL' WHEN 'over_single' THEN 'OVERPAYMENT'
                               ELSE NULL END,  -- over_replaced, dup_fuzzy, settle_noexc, rest_noexc, over_frac → none
        amount_cents = CASE WHEN f.why IN ('over_single', 'over_replaced') THEN greatest(f.remaining, 0) ELSE A.amount_cents END,
        rule = A.rule || '+T15.' || f.why,
        confidence = CASE f.why WHEN 'inst_not_last' THEN 0.7 WHEN 'over_single' THEN 0.8 WHEN 'over_frac' THEN 0.6 ELSE 0.8 END,
        evidence = coalesce(A.evidence, '') || '; T15 ' || f.why || ' remaining=' || f.remaining
      FROM FIX f WHERE A.bank_txn_id = f.bank_txn_id AND A.item_id = f.item_id""")
    fixes = con.sql("SELECT why, count(*) FROM FIX GROUP BY 1 ORDER BY 1").fetchall()

    # remittance receipts prorated as PARTIAL on every document (T6 short of the cash everywhere)
    con.execute("""CREATE TABLE RPL AS
      SELECT bank_txn_id, any_value(cash) cash FROM X
      GROUP BY 1
      HAVING bool_and(rule LIKE 'T6.remit.%' AND direction = 'IN' AND icur = lcur AND kind IN ('ARI', 'ARC'))
         AND count(*) FILTER (WHERE exc = 'PARTIAL' AND rule LIKE 'T6.remit.partial%') >= 2
         AND bool_and(CASE WHEN exc = 'PARTIAL' THEN amt < remaining ELSE true END)
         AND abs(sum(CASE WHEN kind = 'ARC' THEN -amt ELSE amt END) - any_value(cash)) <= 1""")
    con.execute("""CREATE TABLE RPF0 AS
      SELECT bank_txn_id, item_id, remaining, amt,
             sum(remaining - amt) OVER (PARTITION BY bank_txn_id) short,
             row_number() OVER (PARTITION BY bank_txn_id ORDER BY issue_date DESC, item_id DESC) rn
      FROM X WHERE bank_txn_id IN (SELECT bank_txn_id FROM RPL) AND exc = 'PARTIAL' AND rule LIKE 'T6.remit.partial%'""")
    con.execute("""CREATE TABLE RPF AS SELECT * FROM RPF0
      WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM RPF0 WHERE rn = 1 AND remaining - short <= 0)""")
    con.execute("""UPDATE A SET
        amount_cents = CASE WHEN r.rn = 1 THEN r.remaining - r.short ELSE r.remaining END,
        exception = CASE WHEN r.rn = 1 THEN 'PARTIAL' ELSE NULL END,
        rule = A.rule || '+T15.' || CASE WHEN r.rn = 1 THEN 'remit_rest' ELSE 'remit_full' END,
        confidence = 0.5,
        evidence = coalesce(A.evidence, '') || '; T15 remit prorated: short=' || r.short || ' remaining=' || r.remaining
      FROM RPF r WHERE A.bank_txn_id = r.bank_txn_id AND A.item_id = r.item_id""")
    fixes += con.sql("SELECT CASE WHEN rn = 1 THEN 'remit_rest' ELSE 'remit_full' END, count(*) FROM RPF GROUP BY 1 ORDER BY 1").fetchall()
    return fixes


def _adds(con, w: str, phase: str) -> list:
    """Rows added on receipts that ended with no item, using T4's top-ranked party."""
    lp = f"{w}/line_party.parquet"
    if not os.path.exists(lp):
        return []
    con.execute(f"""CREATE TABLE NI AS
      SELECT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.booking_date bd, l.direction, l.text_norm,
             l.bank_code, l.statement_format, l.entity, p.party_id, fx.u
      FROM L l JOIN '{lp}' p USING (bank_txn_id)
      ASOF LEFT JOIN FX fx ON fx.currency = upper(l.currency) AND fx.d <= l.booking_date
      WHERE p.rank = 1
        AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    con.execute("""CREATE TABLE USED AS SELECT a.item_id, min(l.booking_date) first_bd,
                     sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END) applied
                   FROM A a JOIN L l USING (bank_txn_id)
                   WHERE a.item_id IS NOT NULL AND coalesce(a.exception, '') <> 'DUPLICATE_PAYMENT' GROUP BY 1""")
    whtp = sql_wht_parties(con, withholding(phase))
    rates = " OR ".join(f"abs(c.cash - round(c.open_c * {r})) <= 1" for r in OVER_RATES)
    con.execute("""CREATE TABLE C0 AS
      SELECT n.bank_txn_id, n.direction, n.cash, n.currency, n.bd, n.party_id, n.u, n.text_norm, n.bank_code, n.statement_format,
             i.item_id, i.kind, i.total_cents, i.open_at_start_cents open_c, i.issue_date, i.discount_pct, i.discount_days,
             u.item_id used_item, u.first_bd, u.applied,
             count(*) FILTER (WHERE i.kind = 'ARI' AND u.item_id IS NULL AND i.open_at_start_cents > 0)
                 OVER (PARTITION BY n.bank_txn_id) nopen     -- unused open invoices of the party (same currency)
      FROM NI n JOIN I i ON i.party_id = n.party_id AND i.currency = n.currency AND i.kind IN ('ARI', 'API')
           AND i.issue_date <= n.bd AND i.issue_date >= n.bd - INTERVAL 400 DAY
      LEFT JOIN USED u USING (item_id)""")
    nonsepa = NONSEPA.format(t="c.")
    con.execute(f"""CREATE TABLE C AS
      SELECT c.bank_txn_id, c.direction, c.item_id, c.kind, c.total_cents, c.open_c, c.cash,
        CASE WHEN c.direction = 'IN' AND c.kind = 'ARI' AND c.used_item IS NULL AND c.open_c > 0
                  AND c.discount_pct IS NOT NULL AND c.bd <= c.issue_date + c.discount_days + 3
                  AND abs(c.cash - round(c.total_cents * (1 - c.discount_pct / 100))) <= 1
                  AND c.open_c = c.total_cents THEN 'disc_add'
             WHEN c.direction = 'IN' AND c.kind = 'ARI' AND c.used_item IS NULL AND c.open_c > 0
                  AND {nonsepa} AND c.party_id NOT IN {whtp}
                  AND c.open_c - c.cash > 0 AND c.open_c - c.cash <= {BC_PCT} * c.open_c
                  AND (c.open_c - c.cash) / coalesce(c.u, 1.0) <= {BC_EUR_MAX}
                  AND (c.currency <> 'EUR' OR (c.open_c - c.cash) % {BC_EUR_STEP} = 0) THEN 'bc_add'
             WHEN c.direction = 'IN' AND c.kind = 'ARI' AND c.used_item IS NULL AND c.open_c > 0
                  AND c.cash > c.open_c + 1
                  AND ({rates} OR (c.nopen = 1 AND (c.cash - c.open_c) % 100 = 0 AND c.cash <= {OVER_MAX} * c.open_c)) THEN 'over_add'
             WHEN c.total_cents = c.cash AND ((c.direction = 'IN' AND c.kind = 'ARI') OR (c.direction = 'OUT' AND c.kind = 'API'))
                  AND (c.open_c <= 0 OR (c.used_item IS NOT NULL AND c.first_bd < c.bd AND c.applied >= c.open_c))
                  THEN 'dup_add'
        END how
      FROM C0 c""")
    con.execute("DELETE FROM C WHERE how IS NULL")
    if os.environ.get("T15_DEBUG"):
        con.execute(f"COPY (SELECT C.*, n.text_norm, n.bank_code, n.statement_format, n.u, n.bd, n.currency FROM C JOIN NI n USING (bank_txn_id)) TO '{os.environ['T15_DEBUG']}' (FORMAT parquet)")
    con.execute("""CREATE TABLE ADD AS SELECT * FROM C
      QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1 AND count(*) OVER (PARTITION BY item_id) = 1""")
    con.execute("""CREATE TABLE ADDR AS SELECT a.bank_txn_id, a.item_id,
          CASE a.how WHEN 'dup_add' THEN a.cash ELSE a.open_c END amount_cents, NULL::VARCHAR category,
          CASE a.how WHEN 'bc_add' THEN 'SHORT_BANK_CHARGES' WHEN 'over_add' THEN 'OVERPAYMENT' WHEN 'disc_add' THEN 'SHORT_DISCOUNT' ELSE 'DUPLICATE_PAYMENT' END exception,
          'T15.' || a.how rule, 0.6 confidence,
          'no item; unique invoice of rank-1 party; open=' || a.open_c || '; total=' || a.total_cents || '; cash=' || a.cash ||
          '; replaced=' || coalesce(o.category, '') || '/' || coalesce(o.exception, '') evidence
        FROM ADD a LEFT JOIN (SELECT bank_txn_id, any_value(category) category, any_value(exception) exception FROM A GROUP BY 1) o USING (bank_txn_id)""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM ADDR)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM ADDR")
    return con.sql("SELECT rule, count(*) FROM ADDR GROUP BY 1 ORDER BY 1").fetchall()


def build(phase: str, alloc: str | None = None, dst: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    out = (dst or p).replace(os.sep, "/")
    fx = os.path.join(data_root(), "masters", "fx_rates.csv").replace(os.sep, "/")   # same masters as the rest (HK_DATA root)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"CREATE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    if os.path.exists(fx):
        con.execute(f"""CREATE TABLE FX AS SELECT CAST(date AS DATE) d, upper(currency) currency, CAST(units_per_eur AS DOUBLE) u
                        FROM read_csv('{fx}', header=true) WHERE units_per_eur IS NOT NULL""")
    else:   # no rates: the 60 EUR cap falls back to the line currency, the 2 % cap still applies
        con.execute("CREATE TABLE FX (d DATE, currency VARCHAR, u DOUBLE)")
    n0 = con.execute("SELECT count(*) FROM A").fetchall()[0][0]

    _build_x(con)
    moves = _repoint(con)
    _build_x(con)                       # balances again: items were re-pointed / dropped
    fixes = _relabel(con)
    adds = _adds(con, w, phase) + _label_fixes(con, w, phase)
    n1 = con.execute("SELECT count(*) FROM A").fetchall()[0][0]
    con.execute(f"COPY A TO '{out}' (FORMAT parquet)")
    print(f"[{phase}] excfix: repoint {moves} relabels {fixes} adds {adds} rows {n0} -> {n1} ({time.time() - t0:.0f}s)")


def _label_fixes(con, w: str, phase: str) -> list:
    """Labels on rows whose pair and amount are already right (POLICIES §3):
      fx_flag    invoice (ARI/API) in a currency other than the line's, no exception → FX_DIFFERENCE (full open amount
                 already; credit notes keep none). Window: 450 of ~490 such rows are FX_DIFFERENCE in the golden.
      wht_multi  receipt whose AR rows are all of customers that withhold (country, src.common.profile), same-currency, at full amounts,
                 without exception, and exceed the cash by exactly r × Σ invoice totals,
                 r ∈ {1.25 %, 1.5 %, 10 %} (± 1 cent per row) → its invoices SHORT_WITHHOLDING. Window: 134 / 139."""
    prof = withholding(phase)
    whtp = sql_wht_parties(con, prof, table='WHTP2')
    wht_min = sql_wht_min_diff([1 - r for r in prof['rates']], '(s.s_amt - s.cash)', 's.s_tot')
    con.execute(f"""CREATE OR REPLACE TABLE LF AS
      SELECT a.bank_txn_id, a.item_id, 'fx_flag' how FROM A a
      JOIN I i USING (item_id) JOIN L l USING (bank_txn_id)
      WHERE i.kind IN ('ARI', 'API') AND i.currency <> l.currency AND coalesce(a.exception, '') = ''""")
    con.execute(f"""INSERT INTO LF
      WITH s AS (SELECT a.bank_txn_id, any_value(abs(l.amount_cents)) cash, count(*) n,
                        sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END) s_amt,
                        sum(CASE WHEN i.kind = 'ARI' THEN i.total_cents ELSE 0 END) s_tot
                 FROM A a JOIN I i USING (item_id) JOIN L l USING (bank_txn_id)
                 WHERE l.direction = 'IN'
                 GROUP BY a.bank_txn_id
                 HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
                    AND bool_and(i.side = 'AR' AND i.currency = l.currency AND coalesce(a.exception, '') = ''
                                 AND i.party_id IN {whtp}
                                 AND a.amount_cents = i.open_at_start_cents))
      SELECT a.bank_txn_id, a.item_id, 'wht_multi' FROM s JOIN A a USING (bank_txn_id) JOIN I i USING (item_id)
      WHERE i.kind = 'ARI' AND s.s_amt > s.cash + 1
        AND {wht_min} <= s.n""")
    con.execute("""UPDATE A SET exception = CASE f.how WHEN 'fx_flag' THEN 'FX_DIFFERENCE' ELSE 'SHORT_WITHHOLDING' END,
                     rule = A.rule || '+T15.' || f.how
                   FROM LF f WHERE A.bank_txn_id = f.bank_txn_id AND A.item_id = f.item_id""")
    return con.sql("SELECT 'T15.' || how, count(*) FROM LF GROUP BY 1 ORDER BY 1").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    ap.add_argument("--dst")
    a = ap.parse_args()
    build(a.phase, a.alloc, a.dst)


if __name__ == "__main__":
    main()
