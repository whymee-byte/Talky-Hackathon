"""T5b · exception fixes on allocations.parquet, with the item's running balance in booking order.

    python -m src.postfix --phase window|live|final

remaining = open at start − what earlier lines of the phase applied to the item (RETURNED / DUPLICATE excluded).
  fx_run      T2 payment-run row, item currency ≠ line currency, invoice, no exception → FX_DIFFERENCE   (POLICIES §3)
  partial_run T2 payment-run row on a supplier invoice that pays less than `remaining` → PARTIAL
  dup         T3/T5/T6 invoice row with nothing left to pay (remaining ≤ 0), no exception → DUPLICATE_PAYMENT
  last_part   PARTIAL row that pays the whole remaining balance → no exception (last instalment / remainder)
  over_big    OVERPAYMENT whose cash exceeds the item by > 10 % → no exception (the line pays more than this item)
Window (2024-07 → 2025-06): fx_run 2366 / 53 wrong · partial_run 5506 / 6 · dup 479 / 1 · last_part removes 7262 FP.
"""
from __future__ import annotations

import argparse
import os

import duckdb

from src.common import REPO

TOL = "greatest(1, 0.005 * abs(remaining))"


def build(phase: str) -> None:
    w = os.path.join(REPO, "work", phase)
    p = f"{w}/allocations.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    # fill_ref: lines that ended with no item but whose text quotes open items of their party exactly / by PO / by
    # an exact+truncated number (T3f candidates, window precision 97.3 %) -> those items at their open amount
    cands = f"{w}/fuzz_cands.parquet"
    if os.path.exists(cands):
        con.execute(f"""CREATE TABLE FILL AS
          SELECT c.bank_txn_id, c.item_id, c.open_c AS amount_cents, NULL::VARCHAR AS category, '' AS exception,
                 'T5b.fill_ref.' || replace(c.how, '+', '_') AS rule, 0.9 AS confidence,
                 'no item after T3; text quotes ' || c.how || ' number of a party item' AS evidence
          FROM '{cands}' c
          WHERE c.how NOT IN ('trunc', 'typo', 'typo+trunc', 'trunc+typo') AND c.kind = 'ARI'
            AND (c.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)
                 OR c.bank_txn_id IN (   -- lines whose items still fall short of the cash
                    SELECT a.bank_txn_id FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
                    WHERE l.direction = 'IN' AND i.currency = l.currency AND coalesce(a.exception, '') <> 'RETURNED'
                    GROUP BY a.bank_txn_id, l.amount_cents
                    HAVING sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END) < abs(l.amount_cents) - greatest(100, 0.005 * abs(l.amount_cents))))
            AND c.item_id NOT IN (SELECT item_id FROM A WHERE item_id IS NOT NULL)
          QUALIFY row_number() OVER (PARTITION BY c.item_id ORDER BY c.bank_txn_id) = 1""")
        # fill_weak: still no item, few truncated/typo candidates whose open amounts do not exceed the cash
        # (window precision 74-86 %, above the F1/2 break-even)
        con.execute(f"""INSERT INTO FILL
          WITH c AS (SELECT c.*, abs(l.amount_cents) cash FROM '{cands}' c JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                     WHERE c.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)
                       AND c.bank_txn_id NOT IN (SELECT bank_txn_id FROM FILL)
                       AND c.item_id NOT IN (SELECT item_id FROM A WHERE item_id IS NOT NULL)),
               s AS (SELECT bank_txn_id, count(*) nc, sum(CASE WHEN kind = 'ARC' THEN -open_c ELSE open_c END) sm, any_value(cash) cash
                     FROM c GROUP BY 1)
          SELECT c.bank_txn_id, c.item_id, c.open_c, NULL, '', 'T5b.fill_weak.' || replace(c.how, '+', '_'), 0.6,
                 'no item after T3; ' || s.nc || ' noisy candidates summing ' || s.sm || ' for cash ' || s.cash
          FROM c JOIN s USING (bank_txn_id)
          WHERE (s.nc <= 30 AND s.sm <= 1.05 * s.cash) OR (s.nc <= 3 AND s.sm <= 1.5 * s.cash)
          QUALIFY row_number() OVER (PARTITION BY c.item_id ORDER BY c.bank_txn_id) = 1""")
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM FILL)")
        con.execute("INSERT INTO A BY NAME SELECT * FROM FILL")
        print("fill_ref:", con.sql("SELECT count(DISTINCT bank_txn_id), count(*) FROM FILL").fetchall())
    # dup_amt / over_amt: receipts still without item whose likely party (T4 >= 0.5) has exactly one invoice that
    #   dup_amt  : was already fully applied by an earlier line of the phase and whose total = the cash -> DUPLICATE_PAYMENT
    #   over_amt : is still unused and open, with open < cash <= 1.10 x open                                -> OVERPAYMENT
    lp = f"{w}/line_party.parquet"
    if os.path.exists(lp):
        con.execute(f"""CREATE TABLE NI AS
          SELECT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.booking_date, p.party_id
          FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
          WHERE l.direction = 'IN' AND p.confidence >= 0.5
            AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
        # a RETURNED row re-opens what it reverses (direct-debit return, rejected transfer, chargeback)
        con.execute(f"""CREATE TABLE USED AS SELECT a.item_id, min(l.booking_date) first_bd,
                               sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END) applied
                        FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                        WHERE a.item_id IS NOT NULL AND coalesce(a.exception, '') <> 'DUPLICATE_PAYMENT' GROUP BY 1""")
        con.execute(f"""CREATE TABLE EXTRA AS
          WITH c AS (
            SELECT n.bank_txn_id, i.item_id, i.total_cents, i.open_at_start_cents open_c, n.cash,
                   CASE WHEN u.item_id IS NOT NULL AND u.first_bd < n.booking_date AND u.applied >= i.open_at_start_cents
                             AND i.total_cents = n.cash THEN 'dup_amt'
                        WHEN u.item_id IS NULL AND i.open_at_start_cents > 0 AND n.cash > i.open_at_start_cents + 1
                             AND n.cash <= 1.10 * i.open_at_start_cents THEN 'over_amt' END how
            FROM NI n JOIN '{w}/items.parquet' i ON i.party_id = n.party_id AND i.currency = n.currency AND i.kind = 'ARI'
                 AND i.issue_date <= n.booking_date AND i.issue_date >= n.booking_date - INTERVAL 400 DAY
            LEFT JOIN USED u USING (item_id))
          -- over_amt only when the excess is a whole number of currency units (customers round up): window 142 TP / 15 FP;
          -- without that condition OVERPAYMENT F1 fell 0.47 -> 0.33
          SELECT * FROM c WHERE how = 'dup_amt' OR (how = 'over_amt' AND (cash - open_c) % 100 = 0)
          QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1 AND count(*) OVER (PARTITION BY item_id, how) = 1""")
        con.execute("""CREATE TABLE EX2 AS SELECT bank_txn_id, item_id,
                         CASE how WHEN 'dup_amt' THEN cash ELSE open_c END amount_cents, NULL::VARCHAR category,
                         CASE how WHEN 'dup_amt' THEN 'DUPLICATE_PAYMENT' ELSE 'OVERPAYMENT' END exception,
                         'T5b.' || how rule, 0.6 confidence, 'unique party invoice; ' || how evidence FROM EXTRA""")
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM EX2)")
        con.execute("INSERT INTO A BY NAME SELECT * FROM EX2")
        print("dup_amt/over_amt:", con.sql("SELECT rule, count(*) FROM EX2 GROUP BY 1").fetchall())
    # one shape per line: a line with item rows never keeps a no-item (category) row
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM A WHERE item_id IS NOT NULL)")
    con.execute(f"""CREATE TABLE X AS
      SELECT a.bank_txn_id, a.item_id, i.kind, i.currency icur, l.currency lcur, l.booking_date, abs(l.amount_cents) cash,
             i.open_at_start_cents - coalesce(sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END)
                                                  FILTER (WHERE coalesce(a.exception, '') <> 'DUPLICATE_PAYMENT')
                 OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) remaining
      FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      WHERE a.item_id IS NOT NULL""")
    con.execute(f"""CREATE TABLE FIX AS
      SELECT a.bank_txn_id, a.item_id,
        CASE WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur <> x.lcur AND x.kind IN ('API', 'ARI') THEN 'FX_DIFFERENCE'
             WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur = x.lcur AND x.kind = 'API'
                  AND a.amount_cents < x.remaining - {TOL} THEN 'PARTIAL'
             WHEN coalesce(a.exception, '') = '' AND x.kind IN ('ARI', 'API') AND x.remaining <= 0
                  AND (a.rule LIKE 'T3%' OR a.rule LIKE 'T5%' OR a.rule LIKE 'T6%') THEN 'DUPLICATE_PAYMENT'
             WHEN a.exception = 'PARTIAL' AND x.remaining > 0 AND a.amount_cents >= x.remaining - {TOL} THEN ''
             WHEN a.exception = 'OVERPAYMENT' AND x.icur = x.lcur AND x.cash > 1.10 * greatest(x.remaining, 1) THEN ''
        END AS new_exc,
        CASE WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' AND x.icur <> x.lcur THEN 'fx_run'
             WHEN a.rule LIKE 'T2.run%' AND coalesce(a.exception, '') = '' THEN 'partial_run'
             WHEN coalesce(a.exception, '') = '' THEN 'dup'
             WHEN a.exception = 'OVERPAYMENT' THEN 'over_big' ELSE 'last_part' END AS why
      FROM A a JOIN X x USING (bank_txn_id, item_id)""")
    con.execute("DELETE FROM FIX WHERE new_exc IS NULL")
    print(con.sql("SELECT why, new_exc, count(*) FROM FIX GROUP BY ALL ORDER BY 3 DESC").fetchall())
    con.execute(f"""COPY (SELECT a.* REPLACE (coalesce(f.new_exc, a.exception) AS exception,
                                         CASE WHEN f.why IS NULL THEN a.rule ELSE a.rule || '+T5b.' || f.why END AS rule)
                         FROM A a LEFT JOIN FIX f ON f.bank_txn_id = a.bank_txn_id AND f.item_id IS NOT DISTINCT FROM a.item_id)
                    TO '{p}' (FORMAT parquet)""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
