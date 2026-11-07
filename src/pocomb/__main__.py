"""T22 · receipts still without item: the customer pays COMPLETE purchase orders -> unique subset of PO units = cash.

    python -m src.pocomb --phase window|live|final

Measured on TRAIN golden (multi-invoice receipts, single party): a customer never settles part of a purchase order
(customer_po) in 95-96 % of the receipts, stable in every half-year 2021H2..2025H1 (shared pattern, no drift).
So the choice is not among ~40 open invoices but among PO units (all open invoices sharing customer_po, plus each
invoice without PO as its own unit), fewer and with more distinctive amounts.
Candidates: T4 rank-1 party (conf >= PARTY_MIN), open AR invoices issued before the line, same currency, not
settled by an earlier line of the phase. If exactly ONE subset of units sums to the cash (meet-in-the-middle,
<= MAX_UNITS units) the line settles those invoices with no exception. Window measurement on lines no other stage
matched: 211 unique-and-correct vs 25 unique-but-wrong (89 %).
"""
import argparse
import collections
import time

import duckdb
import pyarrow as pa

MAX_UNITS = 26
PARTY_MIN = 0.5
RULE = "T22.pocomb"


def solve(vs, target):
    """Subsets of vs summing to target (stops at 2), as masks over the two halves."""
    h = len(vs) // 2
    A, B = vs[:h], vs[h:]
    sa = collections.defaultdict(list)
    for m in range(1 << len(A)):
        sa[sum(A[i] for i in range(len(A)) if m >> i & 1)].append(m)
    out = []
    for m in range(1 << len(B)):
        s = sum(B[i] for i in range(len(B)) if m >> i & 1)
        for ma in sa.get(target - s, ()):
            if ma or m:
                out.append((ma, m))
                if len(out) > 1:
                    return out
    return out


def build(phase):
    t0 = time.time()
    w = f"work/{phase}"
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE L AS
      SELECT l.bank_txn_id, l.booking_date bd, abs(l.amount_cents) cash, l.currency, p.party_id, p.confidence pc
      FROM '{w}/lines.parquet' l
      JOIN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0) n USING (bank_txn_id)
      JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = l.bank_txn_id AND p.rank = 1 AND p.confidence >= {PARTY_MIN}
      WHERE l.direction = 'IN'""")
    con.execute(f"""CREATE TABLE USED AS SELECT a.item_id, min(l.booking_date) first_bd FROM A a
                   JOIN '{w}/lines.parquet' l USING (bank_txn_id)
                   WHERE a.item_id IS NOT NULL AND coalesce(a.exception, '') NOT IN ('PARTIAL', 'SHORT_DISPUTE', 'RETURNED')
                   GROUP BY 1""")
    rows = con.execute(f"""
      WITH pool AS (
        SELECT l.bank_txn_id, l.cash, i.item_id, coalesce(i.po_norm, i.item_id) unit, i.open_at_start_cents v
        FROM L l JOIN '{w}/items.parquet' i ON i.party_id = l.party_id AND i.kind = 'ARI' AND i.currency = l.currency
             AND i.open_at_start_cents > 0 AND i.issue_date <= l.bd
        LEFT JOIN USED u ON u.item_id = i.item_id
        WHERE u.item_id IS NULL OR u.first_bd >= l.bd),
      units AS (SELECT bank_txn_id, any_value(cash) cash, unit, list(item_id) items, sum(v) v FROM pool GROUP BY 1, 3)
      SELECT bank_txn_id, any_value(cash), list(items ORDER BY unit), list(v ORDER BY unit)
      FROM units GROUP BY 1 HAVING count(*) BETWEEN 2 AND {MAX_UNITS}""").fetchall()
    out, n_multi, n_none = [], 0, 0
    for bt, cash, units, vs in rows:
        r = solve(list(vs), cash)
        if len(r) == 1:
            ma, mb = r[0]
            h = len(vs) // 2
            chosen = [it for i in range(h) if ma >> i & 1 for it in units[i]] + \
                     [it for i in range(len(vs) - h) if mb >> i & 1 for it in units[h + i]]
            out.extend((bt, it) for it in chosen)
        elif r:
            n_multi += 1
        else:
            n_none += 1
    cnt = collections.Counter(it for _, it in out)        # an item claimed by two lines goes to neither
    bad = {bt for bt, it in out if cnt[it] > 1}
    out = [(bt, it) for bt, it in out if bt not in bad]
    con.execute("CREATE TABLE NEW (bank_txn_id VARCHAR, item_id VARCHAR)")
    if out:
        con.register("newdf", pa.table({"bank_txn_id": [b for b, _ in out], "item_id": [i for _, i in out]}))
        con.execute("INSERT INTO NEW SELECT * FROM newdf")
    con.execute(f"""COPY (
      SELECT * FROM A WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM NEW)
      UNION ALL BY NAME
      SELECT n.bank_txn_id, n.item_id, i.open_at_start_cents amount_cents, NULL::VARCHAR category, '' exception,
             '{RULE}' rule, 0.75 confidence,
             'unique subset of complete purchase orders of the party = cash' evidence
      FROM NEW n JOIN '{w}/items.parquet' i USING (item_id)
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T22 pocomb: {len(rows)} candidate lines -> {len({b for b, _ in out})} lines, {len(out)} pairs "
          f"(multi {n_multi}, none {n_none}, conflicts {len(bad)}) ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)
