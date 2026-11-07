"""T20 · last fixes: discount taken AFTER its period → PARTIAL (ar_discount_late); T7 advances → no item (customer_advance).

    python -m src.latedisc --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs last (after src.excfix); only relabels item rows of receipts that already have their invoices. The matchers put
every invoice of such a receipt at its full balance with no exception, so the rows add up to MORE than the cash. If the
excess equals, within 1 cent, the discount (round(total × pct / 100)) of a UNIQUE subset of the receipt's invoices
whose discount period is over (booking date > issue date + discount days + 3, the term of POLICIES §3 and of
src.discombo), those invoices become PARTIAL at total − discount. Only receipts whose rows all have no exception.
No ids, amounts or texts: the invoices' own discount terms and exact cents.

Grace check on TRAIN before the window (no leakage): paid at total − discount with booking ≤ +3 days → always
SHORT_DISCOUNT; from +4 days PARTIAL is the majority on every day (SHORT_DISCOUNT 25–33 % on days 4–11), so +3 stays.
"""
from __future__ import annotations

import argparse
import itertools
import os
import time

import duckdb

from src.common import REPO

GRACE = 3          # days after issue + discount_days still inside the term (POLICIES §3)
ADV_P_ITEM = 0.05  # T7 probability that the line has an item, below which a CUSTOMER_ADVANCE call wins
MAX_ITEMS = 14     # discounted invoices per receipt for the exhaustive subset search


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    rows = con.sql(f"""
      WITH x AS (SELECT a.bank_txn_id, a.item_id, a.amount_cents::BIGINT amt, coalesce(a.exception, '') exc,
                        l.amount_cents cash,
                        (i.discount_pct IS NOT NULL AND i.discount_days IS NOT NULL AND i.kind = 'ARI'
                         AND l.booking_date > i.issue_date + i.discount_days + {GRACE}
                         AND a.amount_cents = i.total_cents) late_ok,
                        round(i.total_cents * i.discount_pct / 100)::BIGINT disc
                 FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
                 WHERE l.direction = 'IN' AND l.currency = i.currency)
      SELECT bank_txn_id, sum(amt) - any_value(cash) excess, list(item_id) FILTER (WHERE late_ok),
             list(disc) FILTER (WHERE late_ok)
      FROM x GROUP BY 1
      HAVING sum(amt) > any_value(cash) AND bool_or(late_ok) AND bool_and(exc = '')""").fetchall()
    fix = []
    for t, excess, ids, discs in rows:
        if len(ids) > MAX_ITEMS:
            continue
        sols = [c for r in range(1, len(ids) + 1) for c in itertools.combinations(range(len(ids)), r)
                if abs(sum(discs[i] for i in c) - excess) <= 1]
        if len(sols) == 1:
            fix += [(t, ids[i], discs[i]) for i in sols[0]]
    con.execute("CREATE TABLE F (bank_txn_id VARCHAR, item_id VARCHAR, disc BIGINT)")
    if fix:
        con.executemany("INSERT INTO F VALUES (?, ?, ?)", fix)
    con.execute("""UPDATE A SET amount_cents = A.amount_cents - F.disc, exception = 'PARTIAL', rule = 'T20.late_discount',
                     evidence = coalesce(A.evidence, '') || '; discount ' || F.disc || ' taken after its period → PARTIAL'
                   FROM F WHERE A.bank_txn_id = F.bank_txn_id AND A.item_id = F.item_id""")
    n = con.sql("SELECT count(DISTINCT bank_txn_id), count(*) FROM F").fetchall()[0]
    na = advance(con, w)
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] latedisc: {n[0]} receipts, {n[1]} invoices → PARTIAL at total − discount · "
          f"advance {na} receipts → CUSTOMER_ADVANCE  ({time.time() - t0:.0f}s)")


def advance(con, w: str) -> int:
    """T20.advance (customer_advance): a receipt that T7 classifies as CUSTOMER_ADVANCE with p_item <= ADV_P_ITEM
    (proforma / advance wording learned on TRAIN) but that a later matcher gave invoices → no item, category
    CUSTOMER_ADVANCE (POLICIES §4). Window: Q1 60/61, Q2–Q4 117/119 such receipts are advances in the golden (98 %);
    above 0.05 it fails almost always, so the threshold was set on Q1 and confirmed on Q2–Q4."""
    cat = f"{w}/line_category.parquet"
    if not os.path.exists(cat):
        return 0
    con.execute(f"""CREATE OR REPLACE TABLE ADV AS SELECT c.bank_txn_id FROM '{cat}' c
                    JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                    WHERE c.category = 'CUSTOMER_ADVANCE' AND c.p_item <= {ADV_P_ITEM} AND l.direction = 'IN'
                      AND c.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) > 0)""")
    con.execute("DELETE FROM A WHERE bank_txn_id IN (SELECT bank_txn_id FROM ADV)")
    con.execute(f"""INSERT INTO A BY NAME SELECT bank_txn_id, NULL::VARCHAR item_id, NULL::BIGINT amount_cents,
                     'CUSTOMER_ADVANCE' category, '' exception, 'T20.advance' rule, 0.9 confidence,
                     'T7 says CUSTOMER_ADVANCE with p_item <= {ADV_P_ITEM}: no item' evidence FROM ADV""")
    return con.sql("SELECT count(*) FROM ADV").fetchall()[0][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
