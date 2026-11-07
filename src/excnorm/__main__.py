"""T5c · last exception normalisation on allocations.parquet (after src.docrules), POLICIES §3.

    python -m src.excnorm --phase window|live|final

  wht_fix   single-invoice line marked PARTIAL whose cash = total × (1 − r), r ∈ {1.25 %, 1.5 %, 10 %}, in an entity
            whose customers withhold (MX01, BR01, MA01) → SHORT_WITHHOLDING at the full open amount
  disc_fix  single-invoice line marked PARTIAL whose cash = total × (1 − discount %) booked within discount days + 3
            → SHORT_DISCOUNT at the full open amount
  wht_add   receipt with no item, likely party (T4 ≥ 0.5) with exactly one unused open invoice whose total × (1 − r)
            = the cash → that invoice, SHORT_WITHHOLDING (full open amount)
"""
from __future__ import annotations

import argparse
import os

import duckdb

from src.common import REPO

RATES = (0.0125, 0.015, 0.10)
WHT_ENTITIES = ("MX01", "BR01", "MA01")


def build(phase: str) -> None:
    w = os.path.join(REPO, "work", phase)
    p = f"{w}/allocations.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    wht = " OR ".join(f"abs(cash - round(total_cents * (1 - {r}))) <= 1" for r in RATES)
    con.execute(f"""CREATE TABLE S AS
      SELECT a.bank_txn_id, a.item_id, abs(l.amount_cents) cash, i.total_cents, i.open_at_start_cents open_c, i.entity,
             i.discount_pct, i.discount_days, i.issue_date, l.booking_date
      FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
      WHERE a.exception = 'PARTIAL' AND i.kind = 'ARI' AND i.currency = l.currency
        AND a.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 1)""")
    con.execute(f"""CREATE TABLE FIX AS
      SELECT bank_txn_id, item_id, open_c,
             CASE WHEN entity IN {WHT_ENTITIES} AND ({wht}) THEN 'SHORT_WITHHOLDING'
                  WHEN discount_pct IS NOT NULL AND booking_date <= issue_date + discount_days + 3
                       AND abs(cash - round(total_cents * (1 - discount_pct / 100))) <= 1 THEN 'SHORT_DISCOUNT' END exc
      FROM S""")
    con.execute("DELETE FROM FIX WHERE exc IS NULL")
    con.execute("""UPDATE A SET exception = f.exc, amount_cents = f.open_c,
                     rule = A.rule || CASE f.exc WHEN 'SHORT_WITHHOLDING' THEN '+T5c.wht_fix' ELSE '+T5c.disc_fix' END
                   FROM FIX f WHERE A.bank_txn_id = f.bank_txn_id AND A.item_id = f.item_id""")
    lp = f"{w}/line_party.parquet"
    n_add = 0
    if os.path.exists(lp):
        con.execute(f"""CREATE TABLE ADD AS
          WITH n AS (SELECT l.bank_txn_id, abs(l.amount_cents) cash, l.currency, l.booking_date, p.party_id
                     FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
                     WHERE l.direction = 'IN' AND p.confidence >= 0.5 AND l.entity IN {WHT_ENTITIES}
                       AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)),
               c AS (SELECT n.bank_txn_id, i.item_id, i.open_at_start_cents open_c, n.cash, i.total_cents
                     FROM n JOIN '{w}/items.parquet' i ON i.party_id = n.party_id AND i.currency = n.currency AND i.kind = 'ARI'
                          AND i.open_at_start_cents = i.total_cents AND i.issue_date <= n.booking_date
                     WHERE i.item_id NOT IN (SELECT item_id FROM A WHERE item_id IS NOT NULL) AND ({wht}))
          SELECT * FROM c QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1 AND count(*) OVER (PARTITION BY item_id) = 1""")
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM ADD)")
        con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
                         'SHORT_WITHHOLDING' exception, 'T5c.wht_add' rule, 0.7 confidence,
                         'unique party invoice; cash = total less a withholding rate' evidence FROM ADD""")
        n_add = con.execute("SELECT count(*) FROM ADD").fetchall()[0][0]
    # prefix_add: receipt with no item, likely party (rank 1, T4 >= 0.5): its oldest unused open items (credit notes
    # negative) in the instructed currency add up exactly to the instructed amount -> those items (window 2151 pairs,
    # 87 % precision). The customer clears its oldest invoices; exact cents make the cut point unambiguous.
    if os.path.exists(lp):
        con.execute(f"""CREATE TABLE PF AS
          WITH ln AS (SELECT l.bank_txn_id, l.booking_date bd, p.party_id, l.currency lcur,
                             coalesce(l.orig_currency, l.currency) cur, coalesce(abs(l.orig_amount_cents), abs(l.amount_cents)) amt
                      FROM '{w}/lines.parquet' l JOIN '{lp}' p USING (bank_txn_id)
                      WHERE l.direction = 'IN' AND p.rank = 1 AND p.confidence >= 0.5
                        AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)),
               pf AS (SELECT ln.bank_txn_id, ln.lcur, i.item_id, i.kind, i.currency, i.issue_date, i.open_at_start_cents open_c, ln.amt,
                             sum(CASE WHEN i.kind = 'ARC' THEN -i.open_at_start_cents ELSE i.open_at_start_cents END)
                                 OVER (PARTITION BY ln.bank_txn_id ORDER BY i.issue_date, i.item_id) cum
                      FROM ln JOIN '{w}/items.parquet' i ON i.party_id = ln.party_id AND i.currency = ln.cur AND i.kind IN ('ARI', 'ARC')
                           AND i.open_at_start_cents > 0 AND i.issue_date <= ln.bd AND i.issue_date >= ln.bd - INTERVAL 400 DAY
                      WHERE i.item_id NOT IN (SELECT item_id FROM A WHERE item_id IS NOT NULL)),
               cut AS (SELECT bank_txn_id, min(issue_date) FILTER (WHERE abs(cum - amt) <= 1) d FROM pf GROUP BY 1)
          SELECT pf.* FROM pf JOIN cut USING (bank_txn_id) WHERE cut.d IS NOT NULL AND pf.issue_date <= cut.d
          QUALIFY row_number() OVER (PARTITION BY item_id ORDER BY bank_txn_id) = 1""")
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM PF)")
        con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
                         CASE WHEN kind = 'ARI' AND currency <> lcur THEN 'FX_DIFFERENCE' ELSE '' END exception,
                         'T5c.prefix_add' rule, 0.6 confidence, 'oldest open items of the party sum exactly to the amount' evidence FROM PF""")
        print("prefix_add:", con.execute("SELECT count(DISTINCT bank_txn_id), count(*) FROM PF").fetchall())
    print(f"[{phase}] excnorm:", con.sql("SELECT exc, count(*) FROM FIX GROUP BY 1").fetchall(), "wht_add", n_add)
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
