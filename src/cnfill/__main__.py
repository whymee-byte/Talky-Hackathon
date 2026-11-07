"""T21 · cnfill: an AR receipt pays the oldest open items of its payer, in maturity order, net of their credit notes.

    python -m src.cnfill --phase window|live|final [--alloc work/<fase>/allocations.parquet]

Post-stage (end of the AR chain). Statement-style payments ("pay everything due up to a date") explain many receipts of
ar_1toN_credit_note / ar_n_to_m with 10-30 invoices, too many for a subset-sum search. A *unit* is an open, unused AR
invoice of the payer plus the open unused credit notes (ARC) whose related_doc is its doc_number; credit notes with no
open invoice of the payer are units of their own (negative). Units are ordered by maturity (due date, then issue date).

  T21.run.<order>  remainder r of the receipt (cash - items already on it; whole cash if it has none) equals exactly
                      the sum of the first k >= K_MIN units of ONE payer (T4 rank 1 with conf >= PARTY_MIN, or the party
                      of the items already on the line), in due-date order (or, failing that, issue-date order). Units
                      must be issued on or before booking. Each item can be claimed by one receipt only (conflicts drop).
No ids, amounts, texts or dates: only the payer, document links, maturity order and exact cents.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

PARTY_MIN = float(os.environ.get("CNFILL_PMIN", 0.5))
K_MIN = int(os.environ.get("CNFILL_KMIN", 2))
AGE_MAX = int(os.environ.get("CNFILL_AGE", 400))
ORDERS = [o for o in os.environ.get("CNFILL_ORDERS", "due,issue").split(",") if o]
KEYS = {"due": "u.due, u.iss, u.unit", "issue": "u.iss, u.due, u.unit"}


def build(phase: str, alloc: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    lp = f"{w}/line_party.parquet"
    if not os.path.exists(lp):
        print(f"[{phase}] cnfill: no line_party, skipped")
        return
    con = duckdb.connect(config={"memory_limit": "3GB", "threads": 4, "preserve_insertion_order": False})
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE I AS SELECT item_id, kind, party_id, entity, currency, doc_number, related_doc, issue_date,
                    due_date, open_at_start_cents o FROM '{w}/items.parquet' WHERE kind IN ('ARI', 'ARC') AND open_at_start_cents > 0""")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN coalesce(exception, '') = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute("DELETE FROM I WHERE item_id IN (SELECT item_id FROM USED)")
    con.execute(f"""CREATE TABLE R AS
      SELECT l.bank_txn_id, l.entity, l.currency, l.booking_date bd, count(a.item_id) nk,
             l.amount_cents - coalesce(sum(CASE WHEN a.item_id IS NULL THEN 0 WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END), 0) r
      FROM '{w}/lines.parquet' l JOIN A a USING (bank_txn_id)
      LEFT JOIN (SELECT item_id, kind FROM '{w}/items.parquet') i ON i.item_id = a.item_id
      WHERE l.direction = 'IN' AND l.amount_cents > 0
      GROUP BY l.bank_txn_id, l.entity, l.currency, l.booking_date, l.amount_cents
      HAVING bool_and(CASE WHEN a.item_id IS NULL THEN coalesce(a.category, 'UNIDENTIFIED') = 'UNIDENTIFIED'
                           ELSE i.kind IN ('ARI', 'ARC') END)
             AND bool_and(coalesce(a.exception, '') IN ('', 'UNIDENTIFIED'))""")
    con.execute("DELETE FROM R WHERE r <= 0")
    # payer: party of the items already on the line (if single), else T4 rank 1
    con.execute(f"""CREATE TABLE RP AS
      WITH ip AS (SELECT a.bank_txn_id, min(i.party_id) party_id, count(DISTINCT i.party_id) np
                  FROM A a JOIN '{w}/items.parquet' i USING (item_id) WHERE a.bank_txn_id IN (SELECT bank_txn_id FROM R) GROUP BY 1),
           t4 AS (SELECT bank_txn_id, party_id FROM '{lp}' WHERE rank = 1 AND confidence >= {PARTY_MIN})
      SELECT r.*, CASE WHEN r.nk > 0 THEN ip.party_id ELSE t4.party_id END party_id
      FROM R r LEFT JOIN ip USING (bank_txn_id) LEFT JOIN t4 USING (bank_txn_id)
      WHERE (r.nk > 0 AND ip.np = 1) OR (r.nk = 0 AND t4.party_id IS NOT NULL)""")
    # units: invoice + its open credit notes; orphan credit notes alone
    con.execute("""CREATE TABLE UM AS
      SELECT c.item_id cn, v.item_id inv FROM I c JOIN I v ON v.kind = 'ARI' AND c.related_doc = v.doc_number
        AND c.party_id = v.party_id AND c.entity = v.entity AND c.currency = v.currency WHERE c.kind = 'ARC'
      QUALIFY count(*) OVER (PARTITION BY c.item_id) = 1""")
    con.execute("""CREATE TABLE IU AS SELECT i.*, coalesce(um.inv, i.item_id) unit FROM I i LEFT JOIN UM um ON um.cn = i.item_id""")
    con.execute("""CREATE TABLE U AS
      SELECT unit, any_value(party_id) party_id, any_value(entity) entity, any_value(currency) currency,
             max(CASE WHEN item_id = unit THEN coalesce(due_date, issue_date) END) due,
             max(CASE WHEN item_id = unit THEN issue_date END) iss, max(issue_date) last_iss,
             sum(CASE WHEN kind = 'ARC' THEN -o ELSE o END) net
      FROM IU GROUP BY 1 HAVING sum(CASE WHEN kind = 'ARC' THEN -o ELSE o END) <> 0""")
    con.execute("CREATE TABLE S_ALL (bank_txn_id VARCHAR, party_id VARCHAR, unit VARCHAR, kc BIGINT, ord VARCHAR)")
    for order in ORDERS:
        key = KEYS[order]
        con.execute(f"""CREATE OR REPLACE TABLE C AS
          SELECT rp.bank_txn_id, rp.r, rp.party_id, u.unit, u.net,
                 sum(u.net) OVER (PARTITION BY rp.bank_txn_id ORDER BY {key} ROWS UNBOUNDED PRECEDING) cum,
                 row_number() OVER (PARTITION BY rp.bank_txn_id ORDER BY {key}) k
          FROM RP rp JOIN U u ON u.party_id = rp.party_id AND u.entity = rp.entity AND u.currency = rp.currency
               AND u.last_iss <= rp.bd AND u.iss >= rp.bd - {AGE_MAX}
          WHERE rp.bank_txn_id NOT IN (SELECT bank_txn_id FROM S_ALL)""")
        # contiguous run of units (i..j) in maturity order: cum[j] - cum[i-1] = r (older never-paid items may precede it)
        con.execute(f"""CREATE OR REPLACE TABLE H AS
          SELECT a.bank_txn_id, any_value(a.k) ki, any_value(b.k) kj FROM C a JOIN C b
            ON b.bank_txn_id = a.bank_txn_id AND b.cum = a.cum - a.net + a.r AND b.k >= a.k + {K_MIN - 1}
          GROUP BY 1 HAVING count(*) = 1""")
        con.execute(f"""INSERT INTO S_ALL SELECT c.bank_txn_id, c.party_id, c.unit, h.kj - h.ki + 1, '{order}'
                        FROM C c JOIN H h USING (bank_txn_id) WHERE c.k BETWEEN h.ki AND h.kj""")
    con.execute("""CREATE TABLE X AS
      SELECT s.bank_txn_id, s.party_id, s.kc, s.ord, i.item_id, i.o FROM S_ALL s JOIN IU i USING (unit)""")
    con.execute("""DELETE FROM X WHERE bank_txn_id IN (SELECT bank_txn_id FROM X WHERE item_id IN
                   (SELECT item_id FROM X GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))""")
    con.execute("""CREATE TABLE NEW AS
      SELECT bank_txn_id, item_id, o amount_cents, NULL::VARCHAR category, '' exception,
             'T21.run.' || ord rule, 0.8 confidence,
             'remainder of the receipt = run of ' || kc || ' consecutive open units (invoice net of its credit notes) of payer '
               || party_id || ' in ' || ord || '-date order' evidence
      FROM X""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(f"[{phase}] cnfill: {con.sql('SELECT count(*) FROM RP').fetchall()[0][0]} receipts with remainder -> "
          f"{con.sql('SELECT rule, count(DISTINCT bank_txn_id), count(*) FROM NEW GROUP BY 1 ORDER BY 1').fetchall()}"
          f" -> {p} ({time.time() - t0:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    a = ap.parse_args()
    build(a.phase, a.alloc)


if __name__ == "__main__":
    main()
