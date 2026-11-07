"""T15 · last exception fixes on the final allocations (runs after src.unident), POLICIES §2–3.

    python -m src.excfix --phase window|live|final [--alloc work/<fase>/allocations.parquet]

remaining = open at start − what earlier lines of the phase applied to the item (RETURNED rows re-open, DUPLICATE rows
excluded), ordered by booking date. Item-row relabels:
  inst_not_last  instalment row marked "last" (no exception) that leaves a balance and the item is paid again by a later
                 line → PARTIAL (amount = applied)
  over_single    single-invoice receipt (same currency) with cash > remaining, excess a whole number of currency units
                 and ≤ 50 %, or excess ≤ 2 % → OVERPAYMENT at the remaining amount
  dup_fuzzy      DUPLICATE_PAYMENT set only because an earlier fuzzy-reference row (T3f / T5b.fill) consumed the item
                 → no exception: the earlier fuzzy allocation is the doubtful one, the window shows these rows are normal
  over_replaced  OVERPAYMENT on the replacement of a cancelled invoice ("anula y sustituye") → no exception (the
                 customer pays the old document; the replacement is cleared as documented)
Rows added on receipts that ended with no item (all rows category), using T4's top-ranked party:
  bc_add         exactly one unused open invoice of the party, same currency, with 0 < open − cash ≤ 2 % of open and
                 ≤ 60 EUR equivalent (EUR lines: a multiple of 5 EUR), on a non-SEPA receipt (XBCT / N43 13 / NTRF /
                 SHA·BEN·CHGS), not in a withholding entity → SHORT_BANK_CHARGES at the open amount
  over_add       exactly one unused open invoice with cash = open × 1.01 (to the cent) → OVERPAYMENT at the open amount
  disc_add       exactly one unused untouched invoice whose discounted total (terms %, booked ≤ issue + days + 3) = cash
                 → SHORT_DISCOUNT at the open amount
  dup_add        exactly one invoice of the party already cleared (before the phase or by an earlier line) whose total
                 = the cash → DUPLICATE_PAYMENT (amount = cash)
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, data_root
from src.common.profile import sql_wht_parties, withholding

TOL = "greatest(1, 0.005 * abs(remaining))"
NONSEPA = ("({t}bank_code LIKE '%XBCT' OR ({t}statement_format = 'n43' AND {t}bank_code = '13') OR {t}bank_code = 'NTRF'"
           " OR regexp_matches(coalesce({t}text_norm, ''), '\bCHGS\b|\b(SHA|BEN)\b'))")
BC_EUR_MAX = 6000          # POLICIES §3 SHORT_BANK_CHARGES: shortfall ≤ 60 EUR equivalent …
BC_PCT = 0.02              # … and ≤ 2 % of the item
OVER_ADD_PCT = 0.01        # over_add: excess = exactly 1 % of the open amount (the dominant TRAIN overpayment pattern)
BC_EUR_STEP = 500          # bc_add on EUR lines: the fee is a whole multiple of 5 EUR (TRAIN shortfall distribution)
OVER_SINGLE_MAX = 1.5      # over_single: whole-unit excess up to 50 %
OVER_SMALL = 1.02          # over_single: any excess up to 2 %
# SHORT_WITHHOLDING customers: learned from TRAIN + POLICIES §3 by customer country (src.common.profile), not entity ids


def build(phase: str, alloc: str | None = None, dst: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    out = (dst or p).replace(os.sep, "/")
    fx = os.path.join(data_root(), "masters", "fx_rates.csv").replace(os.sep, "/")   # same masters as the rest (HK_DATA root)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"CREATE VIEW L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE VIEW I AS SELECT * FROM '{w}/items.parquet'")
    if os.path.exists(fx):
        con.execute(f"""CREATE TABLE FX AS SELECT CAST(date AS DATE) d, upper(currency) currency, CAST(units_per_eur AS DOUBLE) u
                        FROM read_csv('{fx}', header=true) WHERE units_per_eur IS NOT NULL""")
    else:   # no rates: the 60 EUR cap falls back to the line currency, the 2 % cap still applies
        con.execute("CREATE TABLE FX (d DATE, currency VARCHAR, u DOUBLE)")
    n0 = con.execute("SELECT count(*) FROM A").fetchall()[0][0]

    # ---------- item-row relabels, with the running balance
    con.execute("""CREATE TABLE X AS
      SELECT a.bank_txn_id, a.item_id, a.amount_cents amt, coalesce(a.exception, '') exc, a.rule, i.kind, i.currency icur,
             l.currency lcur, l.direction, l.booking_date bd, abs(l.amount_cents) cash,
             l.entity, l.bank_code, l.statement_format, l.text_norm, fx.u,
             count(*) OVER (PARTITION BY a.bank_txn_id) nrow,
             i.open_at_start_cents - coalesce(sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END)
                 FILTER (WHERE coalesce(a.exception, '') <> 'DUPLICATE_PAYMENT')
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) remaining,
             count(*) FILTER (WHERE coalesce(a.exception, '') NOT IN ('DUPLICATE_PAYMENT', 'RETURNED'))
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) later
      FROM A a JOIN I i USING (item_id) JOIN L l USING (bank_txn_id)
      ASOF LEFT JOIN FX fx ON fx.currency = upper(l.currency) AND fx.d <= l.booking_date
      WHERE a.item_id IS NOT NULL""")
    con.execute(f"""CREATE TABLE FIX AS
      SELECT bank_txn_id, item_id, remaining,
        CASE WHEN exc = '' AND rule LIKE '%inst_last%' AND kind IN ('ARI', 'API') AND remaining > 0
                  AND amt < remaining - {TOL} AND later > 0 THEN 'inst_not_last'
             WHEN exc = '' AND nrow = 1 AND kind = 'ARI' AND direction = 'IN' AND icur = lcur AND remaining > 0
                  AND cash > remaining + {TOL}
                  AND (((cash - remaining) % 100 = 0 AND cash <= {OVER_SINGLE_MAX} * remaining) OR cash <= {OVER_SMALL} * remaining)
                  THEN 'over_single'
             WHEN exc = 'OVERPAYMENT' AND rule LIKE '%T8b.replaces%' THEN 'over_replaced'
             WHEN exc = 'DUPLICATE_PAYMENT' AND rule LIKE '%+T5b.dup' AND (rule LIKE 'T3f.%' OR rule LIKE 'T5b.fill_%') THEN 'dup_fuzzy'
        END why
      FROM X""")
    con.execute("DELETE FROM FIX WHERE why IS NULL")
    con.execute("""UPDATE A SET
        exception = CASE f.why WHEN 'inst_not_last' THEN 'PARTIAL' WHEN 'over_single' THEN 'OVERPAYMENT'
                               ELSE NULL END,  -- over_replaced, dup_fuzzy → none
        amount_cents = CASE WHEN f.why IN ('over_single', 'over_replaced') THEN greatest(f.remaining, 0) ELSE A.amount_cents END,
        rule = A.rule || '+T15.' || f.why,
        confidence = CASE f.why WHEN 'inst_not_last' THEN 0.7 WHEN 'over_single' THEN 0.8 ELSE 0.8 END,
        evidence = coalesce(A.evidence, '') || '; T15 ' || f.why || ' remaining=' || f.remaining
      FROM FIX f WHERE A.bank_txn_id = f.bank_txn_id AND A.item_id = f.item_id""")
    fixes = con.sql("SELECT why, count(*) FROM FIX GROUP BY 1 ORDER BY 1").fetchall()

    # ---------- rows added on lines without item
    lp = f"{w}/line_party.parquet"
    adds = []
    if os.path.exists(lp):
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
        nonsepa = NONSEPA.format(t="n.")
        whtp = sql_wht_parties(con, withholding(phase))
        con.execute(f"""CREATE TABLE C AS
          SELECT n.bank_txn_id, n.direction, i.item_id, i.kind, i.total_cents, i.open_at_start_cents open_c, n.cash,
            CASE WHEN n.direction = 'IN' AND i.kind = 'ARI' AND u.item_id IS NULL AND i.open_at_start_cents > 0
                      AND i.discount_pct IS NOT NULL AND n.bd <= i.issue_date + i.discount_days + 3
                      AND abs(n.cash - round(i.total_cents * (1 - i.discount_pct / 100))) <= 1
                      AND i.open_at_start_cents = i.total_cents THEN 'disc_add'
                 WHEN n.direction = 'IN' AND i.kind = 'ARI' AND u.item_id IS NULL AND i.open_at_start_cents > 0
                      AND {nonsepa} AND n.party_id NOT IN {whtp}
                      AND i.open_at_start_cents - n.cash > 0 AND i.open_at_start_cents - n.cash <= {BC_PCT} * i.open_at_start_cents
                      AND (i.open_at_start_cents - n.cash) / coalesce(n.u, 1.0) <= {BC_EUR_MAX}
                      AND (n.currency <> 'EUR' OR (i.open_at_start_cents - n.cash) % {BC_EUR_STEP} = 0) THEN 'bc_add'
                 WHEN n.direction = 'IN' AND i.kind = 'ARI' AND u.item_id IS NULL AND i.open_at_start_cents > 0
                      AND n.cash > i.open_at_start_cents + 1
                      AND abs(n.cash - round(i.open_at_start_cents * (1 + {OVER_ADD_PCT}))) <= 1
                      THEN 'over_add'
                 WHEN i.total_cents = n.cash AND ((n.direction = 'IN' AND i.kind = 'ARI') OR (n.direction = 'OUT' AND i.kind = 'API'))
                      AND (i.open_at_start_cents <= 0 OR (u.item_id IS NOT NULL AND u.first_bd < n.bd AND u.applied >= i.open_at_start_cents))
                      THEN 'dup_add'
            END how
          FROM NI n JOIN I i ON i.party_id = n.party_id AND i.currency = n.currency AND i.kind IN ('ARI', 'API')
               AND i.issue_date <= n.bd AND i.issue_date >= n.bd - INTERVAL 400 DAY
          LEFT JOIN USED u USING (item_id)""")
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
        adds = con.sql("SELECT rule, count(*) FROM ADDR GROUP BY 1 ORDER BY 1").fetchall()
    lab = _label_fixes(con, w)
    adds = list(adds) + lab
    n1 = con.execute("SELECT count(*) FROM A").fetchall()[0][0]
    con.execute(f"COPY A TO '{out}' (FORMAT parquet)")
    print(f"[{phase}] excfix: relabels {fixes} adds {adds} rows {n0} -> {n1} ({time.time() - t0:.0f}s)")


def _label_fixes(con, w: str) -> list:
    """Labels on rows whose pair and amount are already right (POLICIES §3):
      fx_flag    invoice (ARI/API) in a currency other than the line's, no exception → FX_DIFFERENCE (full open amount
                 already; credit notes keep none). Window: 450 of ~490 such rows are FX_DIFFERENCE in the golden.
      wht_multi  receipt in a withholding entity (MX01/BR01/MA01) whose AR rows are all same-currency, at full amounts,
                 without exception, and exceed the cash by exactly r × Σ invoice totals,
                 r ∈ {1.25 %, 1.5 %, 10 %} (± 1 cent per row) → its invoices SHORT_WITHHOLDING. Window: 134 / 139."""
    con.execute(f"""CREATE OR REPLACE TABLE LF AS
      SELECT a.bank_txn_id, a.item_id, 'fx_flag' how FROM A a
      JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      WHERE i.kind IN ('ARI', 'API') AND i.currency <> l.currency AND coalesce(a.exception, '') = ''""")
    con.execute(f"""INSERT INTO LF
      WITH s AS (SELECT a.bank_txn_id, any_value(abs(l.amount_cents)) cash, count(*) n,
                        sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END) s_amt,
                        sum(CASE WHEN i.kind = 'ARI' THEN i.total_cents ELSE 0 END) s_tot
                 FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                 WHERE l.direction = 'IN' AND l.entity IN ('MX01', 'BR01', 'MA01')
                 GROUP BY a.bank_txn_id
                 HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
                    AND bool_and(i.side = 'AR' AND i.currency = l.currency AND coalesce(a.exception, '') = ''
                                 AND a.amount_cents = i.open_at_start_cents))
      SELECT a.bank_txn_id, a.item_id, 'wht_multi' FROM s JOIN A a USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
      WHERE i.kind = 'ARI' AND s.s_amt > s.cash + 1
        AND least(abs(s.s_amt - s.cash - round(s.s_tot * 0.0125)), abs(s.s_amt - s.cash - round(s.s_tot * 0.015)),
                  abs(s.s_amt - s.cash - round(s.s_tot * 0.10))) <= s.n""")
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
