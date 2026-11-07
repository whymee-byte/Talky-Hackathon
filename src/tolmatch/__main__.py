"""T3g · tolerant single-invoice matches for lines that still settle no item (runs after src.postfix).

    python -m src.tolmatch --phase window|live|final        # rewrites work/<fase>/allocations.parquet in place

Only lines whose allocation is a single no-item row and whose T4 party (rank 1) has confidence >= PARTY_MIN.
Among that party's invoices (ARI for receipts, API for payments, same currency, issued before the line) exactly one
must fit one of these POLICIES §3 cases, else the line is left untouched:
  dup    the invoice was fully settled by another line of the phase in the previous DUP_MAX_DAYS days, the cash
         equals its total, and the party has no open invoice of that amount  -> DUPLICATE_PAYMENT, amount = cash
  over   open > 0 and open < cash <= open * (1 + OVER_MAX)          -> OVERPAYMENT, amount = open
  bank   open - cash <= min(60 EUR equivalent, 2 % of open)          -> SHORT_BANK_CHARGES, amount = open
         (disabled: too imprecise when measured on the window; see BANK_ENABLED)
  reopen the item was returned (RETURNED) by an earlier line and the cash equals its open amount -> '' (re-presented)
  over additionally needs a whole-unit excess or exactly 1 % / 10 % of the invoice (pattern learned on TRAIN < 2024-07)
"""
import argparse
import os
import time

import duckdb

from src.common import data_root

PARTY_MIN = 0.5
OVER_MAX = 0.10
DUP_DIRECTIONS = ('OUT',)  # receipts' duplicates are already flagged by src.postfix (dup_amt); here only payments
DUP_MAX_DAYS = 60   # TRAIN < 2024-07: a duplicate arrives 2-14 days after the first payment (p99 49 days)
BANK_ENABLED = False   # without a text signal: 149 of 1722 right (8.7 %), far below the F1/2 threshold
# cross-border signal in the bank text (SWIFT charge codes, international wire wording). TRAIN < 2024-07, receipts of a
# single invoice short by <= 60 EUR and <= 2 %: SHA/BEN/CHGS -> 78 % SHORT_BANK_CHARGES, international wire -> 81 %,
# no signal -> 1.4 %
XB_RE = r"\b(SHA|BEN|OUR|CHGS|CHG)\b|WIRE IN|\bSWIFT\b|TRANSFER EN DIV|\bTT IN\b|INTERNATIONAL|AUSLAND|ETRANGER|ESTERO"
BANK_EUR_CENTS = 6000
BANK_PCT = 0.02
FX = os.path.join(data_root(), "masters", "fx_rates.csv").replace(os.sep, "/")   # same masters as the rest (HK_DATA root)


def tol(x):
    return f"greatest(1, 0.005 * abs({x}))"


def build(phase):
    t0 = time.time()
    w = f"work/{phase}"
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE U AS   -- unmatched lines with a sure party
      SELECT l.bank_txn_id, l.booking_date, l.direction, l.currency, abs(l.amount_cents) AS cash, p.party_id, p.confidence pconf,
             l.text_norm
      FROM '{w}/lines.parquet' l
      JOIN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0 AND count(*) = 1) n USING (bank_txn_id)
      JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = l.bank_txn_id AND p.rank = 1 AND p.confidence >= {PARTY_MIN}""")
    # how each item is settled by the other lines of the phase: amount and date of the line that cleared it
    con.execute(f"""CREATE TABLE S AS
      -- RETURNED re-opens the item (POLICIES §3): it counts negative
      SELECT a.item_id, sum(CASE WHEN a.exception = 'RETURNED' THEN -a.amount_cents ELSE a.amount_cents END) paid,
             min(l.booking_date) first_date, max(l.booking_date) last_date,
             bool_or(a.exception = 'RETURNED') was_returned
      FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) WHERE a.item_id IS NOT NULL GROUP BY 1""")
    con.execute(f"""CREATE TABLE FXR AS SELECT date::DATE d, currency, units_per_eur::DOUBLE u FROM read_csv('{FX}', all_varchar=true)
                    UNION ALL SELECT DISTINCT date::DATE, 'EUR', 1.0 FROM read_csv('{FX}', all_varchar=true)""")
    con.execute(f"""CREATE TABLE C AS
      WITH c AS (
        SELECT u.*, i.item_id, i.total_cents, i.open_at_start_cents open0,
               coalesce(s.paid, 0) paid_other, s.last_date, coalesce(s.was_returned, false) was_returned,
               -- open just before this line (other lines that booked earlier)
               i.open_at_start_cents - coalesce(s.paid, 0) * coalesce((s.last_date < u.booking_date)::int, 0) AS open_now
        FROM U u JOIN '{w}/items.parquet' i ON i.party_id = u.party_id AND i.currency = u.currency
             AND i.kind = CASE u.direction WHEN 'IN' THEN 'ARI' ELSE 'API' END AND i.issue_date <= u.booking_date
        LEFT JOIN S s ON s.item_id = i.item_id),
      r AS (SELECT c.*, coalesce(fx.u, 1.0) u_line FROM c ASOF LEFT JOIN FXR fx ON fx.currency = c.currency AND fx.d <= c.booking_date)
      SELECT *,
        -- TRAIN < 2024-07: the excess of an overpayment is a whole amount (61 %), 1 % (18 %) or 10 % (20 %) of the invoice
        open_now > 0 AND paid_other = 0 AND cash > open_now + 1 AND cash <= open_now * (1 + {OVER_MAX})
          AND ((cash - open_now) % 100 = 0 OR abs((cash - open_now) - round(open_now * 0.01)) <= 1
               OR abs((cash - open_now) - round(open_now * 0.10)) <= 1) AS is_over,
        ({BANK_ENABLED} OR (regexp_matches(text_norm, '{XB_RE}') AND {not bool(os.environ.get('T3G_NO_XB'))})) AND open_now > 0 AND paid_other = 0 AND open_now - cash > 1
          AND open_now - cash <= least({BANK_EUR_CENTS} * u_line, {BANK_PCT} * open_now) AS is_bank,
        abs(cash - open_now) * 1.0 / nullif(open_now, 0) AS rel_diff,
        direction IN {DUP_DIRECTIONS if len(DUP_DIRECTIONS) > 1 else "('" + DUP_DIRECTIONS[0] + "')"}
          AND open0 > 0 AND paid_other >= open0 - {tol('open0')} AND last_date < booking_date
          AND booking_date - last_date <= {DUP_MAX_DAYS} AND abs(cash - total_cents) <= {tol('total_cents')} AS is_dup,
        open_now > 0 AND abs(cash - open_now) <= {tol('open_now')} AS same_amount_open,
        -- a returned direct debit / cheque re-opens the item; a later transfer for its amount pays it (no exception)
        was_returned AND last_date < booking_date AND open_now > 0 AND abs(cash - open_now) <= {tol('open_now')} AS is_reopen
      FROM r""")
    con.execute("""CREATE TABLE M AS
      WITH o AS (   -- best-fitting open invoice per line; the runner-up must fit at least twice as badly
             SELECT *, lead(rel_diff) OVER (PARTITION BY bank_txn_id ORDER BY rel_diff, item_id) AS d2,
                    row_number() OVER (PARTITION BY bank_txn_id ORDER BY rel_diff, item_id) AS rk
             FROM C WHERE is_over OR is_bank),
           op AS (SELECT *, CASE WHEN is_over THEN 'over' ELSE 'bank' END how FROM o
                  WHERE rk = 1 AND (d2 IS NULL OR d2 >= 2 * rel_diff + 0.0005)),
           dp AS (SELECT *, 'dup' how FROM C c
                  WHERE is_dup AND bank_txn_id NOT IN (SELECT bank_txn_id FROM o)
                  QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1
                      AND NOT bool_or(same_amount_open) OVER (PARTITION BY bank_txn_id)),
           rp AS (SELECT *, 'reopen' how FROM C WHERE is_reopen QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1)
      SELECT * FROM (SELECT * FROM rp
                     UNION ALL BY NAME SELECT * EXCLUDE (d2, rk) FROM op WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM rp)
                     UNION ALL BY NAME SELECT * FROM dp WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM rp))
      QUALIFY count(*) OVER (PARTITION BY item_id, how) = 1""")
    con.execute(f"""COPY (
      SELECT * FROM A WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM M)
      UNION ALL BY NAME
      SELECT bank_txn_id, item_id,
             CASE how WHEN 'dup' THEN cash ELSE open_now END AS amount_cents,  -- reopen: open_now
             NULL::VARCHAR AS category,
             CASE how WHEN 'dup' THEN 'DUPLICATE_PAYMENT' WHEN 'over' THEN 'OVERPAYMENT' WHEN 'reopen' THEN ''
                      ELSE 'SHORT_BANK_CHARGES' END AS exception,
             'T3g.' || how AS rule, round(pconf * 0.9, 4) AS confidence,
             'party=' || party_id || ' conf=' || round(pconf, 2) || '; cash=' || cash || ' total=' || total_cents
               || ' open=' || open_now AS evidence
      FROM M
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    # bank charges: a receipt of exactly one invoice, short by <= 60 EUR eq. and <= 2 %, whose text shows a cross-border
    # transfer -> SHORT_BANK_CHARGES with the full open amount (POLICIES §3), whatever stage matched it (or none)
    # relabelling lines other stages already matched: window 92.01 -> 91.98 (24 lines, mostly wrong) -> off by default
    if not os.environ.get("T3G_BANKXB"):
        print(f"[{phase}] T3g:", con.sql("SELECT how, count(*) FROM M GROUP BY 1 ORDER BY 1").fetchall())
        if not os.environ.get("T3G_NO_COMPLETE"):
            complete(con, w, phase)
        return
    con.execute(f"CREATE OR REPLACE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE BK AS
      WITH one AS (SELECT bank_txn_id, any_value(item_id) item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING count(*) = 1 AND count(*) FILTER (WHERE item_id IS NULL) = 0),
           x AS (SELECT o.bank_txn_id, o.item_id, a.exception, abs(l.amount_cents) cash, l.currency, l.booking_date,
                        i.open_at_start_cents - coalesce(s.paid, 0) * coalesce((s.last_date < l.booking_date)::int, 0) AS open_now
                 FROM one o JOIN A a USING (bank_txn_id, item_id)
                 JOIN '{w}/lines.parquet' l ON l.bank_txn_id = o.bank_txn_id
                 JOIN '{w}/items.parquet' i ON i.item_id = o.item_id
                 LEFT JOIN (SELECT a2.item_id, sum(CASE WHEN a2.exception = 'RETURNED' THEN -a2.amount_cents ELSE a2.amount_cents END) paid,
                                   max(l2.booking_date) last_date
                            FROM A a2 JOIN '{w}/lines.parquet' l2 USING (bank_txn_id)
                            WHERE a2.item_id IS NOT NULL GROUP BY 1) s ON s.item_id = o.item_id
                 WHERE l.direction = 'IN' AND i.kind = 'ARI' AND i.currency = l.currency
                   AND coalesce(a.exception, '') IN ('', 'PARTIAL')
                   AND regexp_matches(l.text_norm, '{XB_RE}')),
           r AS (SELECT x.*, coalesce(fx.u, 1.0) u_line FROM x ASOF LEFT JOIN FXR fx ON fx.currency = x.currency AND fx.d <= x.booking_date)
      SELECT * FROM r WHERE open_now > cash + 1 AND open_now - cash <= least({BANK_EUR_CENTS} * u_line, {BANK_PCT} * open_now)""")
    con.execute(f"""COPY (
      SELECT a.* REPLACE (
               CASE WHEN b.item_id IS NOT NULL THEN b.open_now ELSE a.amount_cents END AS amount_cents,
               CASE WHEN b.item_id IS NOT NULL THEN 'SHORT_BANK_CHARGES' ELSE a.exception END AS exception,
               CASE WHEN b.item_id IS NOT NULL THEN coalesce(a.rule, '') || '+T3g.bank_xb' ELSE a.rule END AS rule,
               CASE WHEN b.item_id IS NOT NULL THEN coalesce(a.evidence, '') || '; cross-border text, short ' || (b.open_now - b.cash)
                    ELSE a.evidence END AS evidence)
      FROM A a LEFT JOIN BK b ON b.bank_txn_id = a.bank_txn_id AND b.item_id = a.item_id
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T3g bank_xb:", con.sql("SELECT coalesce(exception, '') prev, count(*) FROM BK GROUP BY 1").fetchall())
    print(f"[{phase}] T3g:", con.sql("SELECT how, count(*) FROM M GROUP BY 1 ORDER BY 1").fetchall(),
          f"({time.time() - t0:.0f}s)")
    if not os.environ.get("T3G_NO_COMPLETE"):
        complete(con, w, phase)


def complete(con, w, phase):
    """Receipts that already settle some items of ONE party but whose cash exceeds them: the remainder must be exactly a
    subset (1-4 items) of that party's other open invoices / credit notes (credit notes negative), unique at the smallest
    size; items used by any other line are excluded and an item claimed by two lines is given to neither."""
    t0 = time.time()
    con.execute(f"CREATE OR REPLACE TABLE A2 AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE OR REPLACE TABLE LS AS
      WITH r AS (SELECT a.bank_txn_id, count(*) n_items, count(DISTINCT i.party_id) n_party, any_value(i.party_id) party_id,
                        sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END) cur_sum,
                        bool_and(i.kind IN ('ARI', 'ARC')) ar_only, bool_and(i.currency = l.currency) same_ccy,
                        any_value(abs(l.amount_cents)) cash, any_value(l.booking_date) booking_date,
                        any_value(l.direction) direction
                 FROM A2 a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                 WHERE a.item_id IS NOT NULL GROUP BY 1)
      SELECT *, cash - cur_sum AS rem FROM r
      WHERE n_party = 1 AND ar_only AND same_ccy AND direction = 'IN' AND cash - cur_sum > 1
        AND bank_txn_id NOT IN (SELECT bank_txn_id FROM A2 WHERE item_id IS NULL)""")
    con.execute(f"""CREATE OR REPLACE TABLE CI2 AS
      SELECT ls.bank_txn_id, i.item_id, CASE WHEN i.kind = 'ARC' THEN -i.open_at_start_cents ELSE i.open_at_start_cents END v
      FROM LS ls JOIN '{w}/items.parquet' i ON i.party_id = ls.party_id AND i.kind IN ('ARI', 'ARC')
           AND i.open_at_start_cents > 0 AND i.issue_date <= ls.booking_date AND i.issue_date >= ls.booking_date - INTERVAL 400 DAY
      WHERE i.item_id NOT IN (SELECT item_id FROM A2 WHERE item_id IS NOT NULL)
      QUALIFY row_number() OVER (PARTITION BY ls.bank_txn_id ORDER BY i.issue_date DESC, i.item_id) <= 40""")
    con.execute("""CREATE OR REPLACE TABLE P2 AS
      SELECT a.bank_txn_id, a.item_id ia, b.item_id ib, a.v + b.v sab FROM CI2 a JOIN CI2 b
        ON b.bank_txn_id = a.bank_txn_id AND b.item_id > a.item_id""")
    deep = "true" if os.environ.get("T3G_COMPLETE_DEEP", "0") == "1" else "false"
    con.execute("""CREATE OR REPLACE TABLE SOL AS
      WITH s1 AS (SELECT c.bank_txn_id, [c.item_id] items FROM CI2 c JOIN LS USING (bank_txn_id) WHERE c.v = LS.rem),
           s2 AS (SELECT p.bank_txn_id, [p.ia, p.ib] items FROM P2 p JOIN LS USING (bank_txn_id) WHERE p.sab = LS.rem),
           s3 AS (SELECT p.bank_txn_id, [p.ia, p.ib, c.item_id] items FROM P2 p JOIN LS USING (bank_txn_id)
                  JOIN CI2 c ON c.bank_txn_id = p.bank_txn_id AND c.item_id > p.ib AND c.v = LS.rem - p.sab),
           s4 AS (SELECT x.bank_txn_id, [x.ia, x.ib, y.ia, y.ib] items FROM P2 x JOIN LS USING (bank_txn_id)
                  JOIN P2 y ON y.bank_txn_id = x.bank_txn_id AND y.ia > x.ib AND y.sab = LS.rem - x.sab),
           c24 AS (SELECT * FROM CI2 QUALIFY row_number() OVER (PARTITION BY bank_txn_id ORDER BY item_id) <= 24),
           t3 AS (SELECT a.bank_txn_id, a.item_id i1, b.item_id i2, c.item_id i3, a.v + b.v + c.v s FROM c24 a
                  JOIN c24 b ON b.bank_txn_id = a.bank_txn_id AND b.item_id > a.item_id
                  JOIN c24 c ON c.bank_txn_id = a.bank_txn_id AND c.item_id > b.item_id
                  WHERE {DEEP}),
           s5 AS (SELECT t.bank_txn_id, [t.i1, t.i2, t.i3, p.ia, p.ib] items FROM t3 t JOIN LS USING (bank_txn_id)
                  JOIN P2 p ON p.bank_txn_id = t.bank_txn_id AND p.ia > t.i3 AND p.sab = LS.rem - t.s),
           s6 AS (SELECT x.bank_txn_id, [x.i1, x.i2, x.i3, y.i1, y.i2, y.i3] items FROM t3 x JOIN LS USING (bank_txn_id)
                  JOIN t3 y ON y.bank_txn_id = x.bank_txn_id AND y.i1 > x.i3 AND y.s = LS.rem - x.s),
           allsol AS (SELECT * FROM s1 UNION ALL SELECT * FROM s2 UNION ALL SELECT * FROM s3 UNION ALL SELECT * FROM s4
                      UNION ALL SELECT * FROM s5 UNION ALL SELECT * FROM s6),
           best AS (SELECT *, len(items) k FROM allsol QUALIFY len(items) = min(len(items)) OVER (PARTITION BY bank_txn_id))
      SELECT bank_txn_id, any_value(items) items, any_value(k) k FROM best GROUP BY 1 HAVING count(*) = 1""".replace("{DEEP}", deep))
    con.execute("""CREATE OR REPLACE TABLE ADD AS
      SELECT s.bank_txn_id, unnest(s.items) item_id, s.k FROM SOL s""")
    con.execute("""CREATE OR REPLACE TABLE ADD AS SELECT * FROM ADD QUALIFY count(*) OVER (PARTITION BY item_id) = 1""")
    con.execute("""CREATE OR REPLACE TABLE ADD AS   -- keep a line only if all its chosen items survived
      SELECT a.* FROM ADD a JOIN SOL s USING (bank_txn_id) WHERE (SELECT count(*) FROM ADD b WHERE b.bank_txn_id = a.bank_txn_id) = s.k""")
    con.execute(f"""COPY (
      SELECT * FROM A2
      UNION ALL BY NAME
      SELECT d.bank_txn_id, d.item_id, i.open_at_start_cents amount_cents, NULL::VARCHAR category, '' exception,
             'T3g.complete_' || d.k rule, 0.7 confidence,
             'remainder of the cash = subset of ' || d.k || ' open item(s) of the same party' evidence
      FROM ADD d JOIN '{w}/items.parquet' i USING (item_id)
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T3g complete:", con.sql("SELECT k, count(DISTINCT bank_txn_id) lines, count(*) items FROM ADD GROUP BY 1 ORDER BY 1").fetchall(),
          f"(candidate lines {con.sql('SELECT count(*) FROM LS').fetchall()[0][0]}, {time.time() - t0:.0f}s)")


def overfix(phase):
    """Last step (after src.excfix): an OVERPAYMENT whose excess over the item is neither a whole number of currency
    units nor exactly 1 % / 10 % of it is a line that pays more than this item -> no exception (the pair is kept).
    TRAIN < 2024-07: genuine overpayments have that excess shape; window: keeps 320/320 T5.overpayment hits and drops
    125 of its 126 false positives."""
    w = f"work/{phase}"
    con = duckdb.connect()
    con.execute(f"""CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'""")
    con.execute(f"""CREATE TABLE X AS
      SELECT a.bank_txn_id, a.item_id FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      JOIN '{w}/items.parquet' i USING (item_id)
      WHERE a.exception = 'OVERPAYMENT' AND i.currency = l.currency
        AND NOT ((abs(l.amount_cents) - a.amount_cents) % 100 = 0
                 OR abs((abs(l.amount_cents) - a.amount_cents) - round(a.amount_cents * 0.01)) <= 1
                 OR abs((abs(l.amount_cents) - a.amount_cents) - round(a.amount_cents * 0.10)) <= 1)""")
    con.execute(f"""COPY (
      SELECT a.* REPLACE (
        CASE WHEN x.item_id IS NOT NULL THEN '' ELSE a.exception END AS exception,
        CASE WHEN x.item_id IS NOT NULL THEN coalesce(a.rule, '') || '+T3g.overfix' ELSE a.rule END AS rule)
      FROM A a LEFT JOIN X x ON x.bank_txn_id = a.bank_txn_id AND x.item_id = a.item_id
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T3g overfix: {con.sql('SELECT count(*) FROM X').fetchall()[0][0]} OVERPAYMENT -> no exception")




if __name__ == "__main__":
    import argparse as _ap
    ap = _ap.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--overfix", action="store_true", help="only the final OVERPAYMENT shape check (after excfix)")
    a = ap.parse_args()
    if a.overfix:
        overfix(a.phase)
    else:
        build(a.phase)
