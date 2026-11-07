"""T21 · receipts still without item: the customer's payment-delay profile + unique exact subset of its open invoices.

    python -m src.delaycomb --phase window|live|final

Rule learned on TRAIN golden (before the window for --phase window, all TRAIN otherwise), nothing hard-coded:
a customer never pays an invoice earlier than its usual minimum delay after the due date (q05 of booking - due_date over
its history, minus PAD days). Candidates = the T4 rank-1 party's open AR invoices issued before the line, excluding
those "not yet due for this customer" and those settled by an earlier line of the phase. If exactly ONE subset of them
sums to the cash (meet-in-the-middle, <= MAX_CAND candidates), the line settles it with no exception.
Window measurement on lines no other stage matched: 616 unique-and-correct vs 51 unique-but-wrong (92 %).
"""
import argparse
import collections
import os
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from src.common import data_root

PAD = 7
MIN_HISTORY = 8
MAX_CAND = int(os.environ.get("DC_MAX_CAND", 24))   # measured: 36 + credit notes drops precision 0.88 -> 0.55
RANKS = int(os.environ.get("DC_RANKS", 1))      # T4 parties tried (rank 1..RANKS); kept only if exactly one resolves
WITH_CN = os.environ.get("DC_CN", "0") == "1"   # open AR credit notes of the party as negative candidates
PARTY_MIN = 0.5
RANK2_MIN = 0.2
RULE = "T21.delaycomb"


def src(root, table):
    """TRAIN table: Parquet cache when present (typed), else the csv.gz with inferred types."""
    pq_ = f"data_cache/train/{table}.parquet"
    return f"read_parquet('{pq_}')" if os.path.exists(pq_) else f"read_csv('{root}/train/{table}.csv.gz', sample_size=-1)"


def learn(con, phase):
    cut = "AND b.booking_date < DATE '2024-07-01'" if phase == "window" else ""
    root = data_root().replace(os.sep, "/")
    con.execute(f"""CREATE TABLE D AS
      WITH g AS (SELECT g.item_id, b.booking_date::DATE bd
                 FROM {src(root, 'golden_allocations')} g JOIN {src(root, 'bank_transactions')} b USING (bank_txn_id)
                 WHERE g.item_id LIKE 'ARI%' {cut})
      SELECT i.customer_id party, quantile_cont(g.bd - i.due_date::DATE, 0.05) q05
      FROM g JOIN {src(root, 'ar_invoices')} i USING (item_id)
      GROUP BY 1 HAVING count(*) >= {MIN_HISTORY}""")


def solve(vs, target):
    """All subsets (up to 2) of vs summing to target, as index masks (a, b) over the two halves."""
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
    learn(con, phase)
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE L AS
      SELECT l.bank_txn_id, l.booking_date bd, abs(l.amount_cents) cash, l.currency, p.party_id, p.confidence pc
      FROM '{w}/lines.parquet' l
      JOIN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0) n USING (bank_txn_id)
      JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = l.bank_txn_id AND p.rank <= {RANKS}
           AND (p.rank = 1 AND p.confidence >= {PARTY_MIN} OR p.rank > 1 AND p.confidence >= {RANK2_MIN})
      WHERE l.direction = 'IN'""")
    con.execute("""CREATE TABLE USED AS SELECT a.item_id, min(l.booking_date) first_bd FROM A a
                   JOIN read_parquet('work/""" + phase + """/lines.parquet') l USING (bank_txn_id)
                   WHERE a.item_id IS NOT NULL AND coalesce(a.exception, '') NOT IN ('PARTIAL', 'SHORT_DISPUTE', 'RETURNED')
                   GROUP BY 1""")
    rows = con.execute(f"""
      SELECT l.bank_txn_id, l.party_id, l.cash,
             list(i.item_id ORDER BY i.kind, i.due_date, i.item_id),
             list(CASE WHEN i.kind = 'ARC' THEN -i.open_at_start_cents ELSE i.open_at_start_cents END
                  ORDER BY i.kind, i.due_date, i.item_id)
      FROM L l JOIN D d ON d.party = l.party_id
      JOIN '{w}/items.parquet' i ON i.party_id = l.party_id AND i.currency = l.currency
           AND i.open_at_start_cents > 0 AND i.issue_date <= l.bd
           AND (i.kind = 'ARI' AND (l.bd - i.due_date) >= d.q05 - {PAD} OR {WITH_CN} AND i.kind = 'ARC')
      LEFT JOIN USED u ON u.item_id = i.item_id
      WHERE u.item_id IS NULL OR u.first_bd >= l.bd
      GROUP BY 1, 2, 3 HAVING count(*) BETWEEN 2 AND {MAX_CAND}""").fetchall()
    out, n_multi, n_none, n_ambig = [], 0, 0, 0
    per_line = collections.defaultdict(list)          # bank_txn_id -> [(party, chosen items)]
    for bt, party, cash, ids, vs in rows:
        r = solve(list(vs), cash)
        if len(r) == 1:
            ma, mb = r[0]
            h = len(vs) // 2
            chosen = [ids[i] for i in range(h) if ma >> i & 1] + [ids[h + i] for i in range(len(vs) - h) if mb >> i & 1]
            per_line[bt].append((party, chosen))
        elif r:
            n_multi += 1
        else:
            n_none += 1
    for bt, sols_ in per_line.items():
        if len(sols_) == 1:                             # exactly one candidate party resolves the line
            out.extend((bt, it) for it in sols_[0][1])
        else:
            n_ambig += 1
    # an item claimed by two lines goes to neither
    cnt = collections.Counter(it for _, it in out)
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
             '{RULE}' rule, 0.7 confidence,
             'unique subset of the party''s invoices due for it (delay >= q05-{PAD}) = cash' evidence
      FROM NEW n JOIN '{w}/items.parquet' i USING (item_id)
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T21 delaycomb: {len(rows)} (line, party) candidates -> {len({b for b, _ in out})} lines, {len(out)} pairs "
          f"(multi {n_multi}, none {n_none}, party-ambiguous {n_ambig}, conflicts {len(bad)}) ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)
