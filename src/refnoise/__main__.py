"""T14 · receipts left without item → the only open item of the payer's candidates whose balance equals the cash.

    python -m src.refnoise --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Runs after every matching post-step (after src.multiref) and before src.unident; rewrites only the no-item rows of
work/<phase>/allocations.parquet. Most receipts that end without item carry no usable number (bank refs, payment ids,
the amount itself), so they cannot be matched by reference; but the payer is often among T4's candidates even when its
confidence is too low for the matcher (rank 2–3, confidence ≈ 0,1).

Rule T14.unique_amount.r<rank>: IN line whose rows all have no item; candidate items = open AR invoices
(open_at_start_cents > 0, issued on or before the booking date, same entity and currency) of T4's top-3 parties, not
already allocated to another line (an item whose payment was RETURNED counts as open again), with open balance = the
line amount (exact cents). Accepted only if the candidate is unique for the line AND the line is the only one proposing
that item. Amount = the open balance, no exception.
Window (TRAIN 2024-07 → 2025-06; designed on Q1, Q2–Q4 untouched): Q1 644 lines, P 99,5 %; Q2–Q4 2.250 lines, P 97,7 %.

Rule T14.unique_combo.k<k>.r<rank> (see combos): lines still without item → the only combination of 2–3 open unused
items of one candidate party adding up to the cash.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

RANK_MAX = 3
CONF = {1: 0.9, 2: 0.85, 3: 0.85}
COMBO_KMAX = 40                    # most recent open items per candidate party in the combination search
COMBO_CONF = {2: 0.85, 3: 0.6}


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}' WHERE rule IS NULL OR rule NOT LIKE 'T14.%'")
    con.execute(f"""CREATE TABLE E AS SELECT l.bank_txn_id, l.entity, l.currency, l.booking_date, l.amount_cents
                    FROM '{w}/lines.parquet' l WHERE l.direction = 'IN'
                    AND l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    # allocated = more non-RETURNED rows than RETURNED rows (a returned payment re-opens the item)
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN coalesce(exception, '') = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE C AS
      SELECT e.bank_txn_id, i.item_id, i.open_at_start_cents amount_cents, p.party_id, p.rank, p.confidence pconf
      FROM E e JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = e.bank_txn_id AND p.rank <= {RANK_MAX}
      JOIN '{w}/items.parquet' i ON i.party_id = p.party_id AND i.side = 'AR' AND i.kind = 'ARI'
           AND i.entity = e.entity AND i.currency = e.currency AND i.open_at_start_cents > 0
           AND i.open_at_start_cents = e.amount_cents AND i.issue_date <= e.booking_date
      WHERE i.item_id NOT IN (SELECT item_id FROM USED)""")
    con.execute("""CREATE TABLE PICK AS SELECT * FROM C
                   QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1 AND count(*) OVER (PARTITION BY item_id) = 1""")
    case = " ".join(f"WHEN {r} THEN {c}" for r, c in CONF.items())
    con.execute(f"""CREATE TABLE NEW AS
      SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category, '' exception,
             'T14.unique_amount.r' || rank rule, CASE rank {case} END confidence,
             'no item after matching; only open unused item of T4 candidates (party ' || party_id || ', rank ' || rank
               || ', conf ' || round(pconf, 2) || ') with balance = cash' evidence
      FROM PICK""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    n_amount = con.sql("SELECT count(*) FROM NEW").fetchall()[0][0]
    combos(con, w)
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] refnoise: {con.sql('SELECT count(*) FROM E').fetchall()[0][0]} receipts without item -> "
          f"unique_amount {n_amount} lines; "
          f"{con.sql('SELECT rule, count(DISTINCT bank_txn_id) FROM COMBO GROUP BY 1 ORDER BY 1').fetchall()} -> {p} "
          f"({time.time() - t0:.0f}s)")


def combos(con, w: str) -> None:
    """T14.unique_combo.k<k>.r<rank>: still no item → the only combination of 2..COMBO_MAX open unused items of ONE of
    T4's top-3 parties (its COMBO_KMAX most recent open items) whose balances add up to the cash exactly; dropped when
    another line proposes one of its items. Window: designed on Q1 (k=2 P 100 %, k=3 P 87 %); Q2–Q4 untouched: k=2
    P 97–98 %, k=3 P 79–87 % (all above F1/2; these lines had no item, so line accuracy can only go up)."""
    con.execute("""CREATE OR REPLACE TABLE E2 AS SELECT * FROM E
                   WHERE bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)""")
    con.execute("""CREATE OR REPLACE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN coalesce(exception, '') = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE OR REPLACE TABLE CI AS SELECT * FROM (
      SELECT e.bank_txn_id, e.amount_cents cash, p.party_id, p.rank, i.item_id, i.open_at_start_cents v, i.issue_date
      FROM E2 e JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = e.bank_txn_id AND p.rank <= {RANK_MAX}
      JOIN '{w}/items.parquet' i ON i.party_id = p.party_id AND i.side = 'AR' AND i.kind = 'ARI' AND i.entity = e.entity
           AND i.currency = e.currency AND i.open_at_start_cents > 0 AND i.open_at_start_cents < e.amount_cents
           AND i.issue_date <= e.booking_date
      WHERE i.item_id NOT IN (SELECT item_id FROM USED))
      QUALIFY row_number() OVER (PARTITION BY bank_txn_id, party_id ORDER BY issue_date DESC, item_id) <= {COMBO_KMAX}""")
    con.execute("""CREATE OR REPLACE TABLE CS AS
      SELECT a.bank_txn_id, a.party_id, a.rank, [a.item_id, b.item_id] items, [a.v, b.v] vals FROM CI a JOIN CI b
        ON a.bank_txn_id = b.bank_txn_id AND a.party_id = b.party_id AND a.item_id < b.item_id AND a.v + b.v = a.cash
      UNION ALL
      SELECT a.bank_txn_id, a.party_id, a.rank, [a.item_id, b.item_id, c.item_id], [a.v, b.v, c.v] FROM CI a JOIN CI b
        ON a.bank_txn_id = b.bank_txn_id AND a.party_id = b.party_id AND a.item_id < b.item_id AND a.v + b.v < a.cash
      JOIN CI c ON c.bank_txn_id = a.bank_txn_id AND c.party_id = a.party_id AND b.item_id < c.item_id
        AND a.v + b.v + c.v = a.cash""")
    con.execute("CREATE OR REPLACE TABLE CU AS SELECT * FROM CS QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1")
    con.execute("""CREATE OR REPLACE TABLE CX AS SELECT bank_txn_id, party_id, rank, len(items) k,
                     unnest(items) item_id, unnest(vals) amount_cents FROM CU""")
    con.execute("""DELETE FROM CX WHERE bank_txn_id IN (SELECT bank_txn_id FROM CX WHERE item_id IN
                     (SELECT item_id FROM CX GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    case = " ".join(f"WHEN {k} THEN {c}" for k, c in COMBO_CONF.items())
    con.execute(f"""CREATE OR REPLACE TABLE COMBO AS
      SELECT bank_txn_id, item_id, amount_cents, NULL::VARCHAR category, '' exception,
             'T14.unique_combo.k' || k || '.r' || rank rule, CASE k {case} END confidence,
             'no item after matching; only combination of ' || k || ' open unused items of T4 candidate ' || party_id
               || ' (rank ' || rank || ') adding up to the cash' evidence
      FROM CX""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM COMBO)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM COMBO")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
