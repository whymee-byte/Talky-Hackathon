"""T23 · receipts still without item: stale invoices are not candidates -> unique exact subset of the party's LIVE invoices.

    python -m src.agecomb --phase window|live|final [--eval]

Measured on TRAIN golden: a customer's open ledger is dominated by stale invoices (open for years, never paid by
bank), which make the exact-sum problem ambiguous. Two general signals separate the invoices a receipt pays from the
stale ones: age (booking - issue: golden q95 ~150 days vs median ~170 for the rest) and "skips" (number of the
party's invoices issued LATER and already settled by an earlier line: golden q95 ~30 vs median ~9 / q80 ~135).
Candidates = T4 rank-1 party's open AR invoices, same currency, issued <= booking date, age <= AGE_MAX, skips <= SKIP_MAX,
not settled by an earlier line of the phase. If exactly ONE subset sums to the cash (meet-in-the-middle, <= MAX_CAND)
the line settles it with no exception. Items solved in a pass count as settled for the next pass (fewer candidates
for the other lines). Audit: rule 'T23.agecomb', confidence, evidence.
"""
import argparse
import collections
import os
import time

import duckdb
import pyarrow as pa

AGE_MAX = int(os.environ.get("AC_AGE", 150))
SKIP_MAX = int(os.environ.get("AC_SKIP", 30))
MAX_CAND = int(os.environ.get("AC_MAX_CAND", 18))
MIN_CAND = 2
PASSES = int(os.environ.get("AC_PASSES", 2))
PARTY_MIN = 0.5
RULE = "T23.agecomb"


SOL_CAP = int(os.environ.get("AC_SOL_CAP", 64))     # solutions enumerated per line (marginal mode)
MARG = float(os.environ.get("AC_MARG", 1.0))         # 1.0 = unique solution only; <1 = items present in >= MARG of the solutions


def solve(vs, target, cap=2):
    """Subsets of vs summing to target (stops at cap), as masks over the two halves."""
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
                if len(out) >= cap:
                    return out
    return out


def build(phase, do_eval=False):
    t0 = time.time()
    w = f"work/{phase}"
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"CREATE TABLE LN AS SELECT bank_txn_id, booking_date, direction, abs(amount_cents) cash, currency FROM '{w}/lines.parquet'")
    con.execute(f"""CREATE TABLE IT AS SELECT item_id, party_id, issue_date, open_at_start_cents v, currency
                    FROM '{w}/items.parquet' WHERE kind = 'ARI' AND open_at_start_cents > 0""")
    con.execute(f"""CREATE TABLE L AS
      SELECT l.bank_txn_id, l.booking_date bd, l.cash, l.currency, p.party_id
      FROM LN l
      JOIN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0) n USING (bank_txn_id)
      JOIN '{w}/line_party.parquet' p ON p.bank_txn_id = l.bank_txn_id AND p.rank = 1 AND p.confidence >= {PARTY_MIN}
      WHERE l.direction = 'IN'""")
    con.execute("""CREATE TABLE USED AS SELECT a.item_id, min(l.booking_date) first_bd FROM A a JOIN LN l USING (bank_txn_id)
                   WHERE a.item_id IS NOT NULL AND coalesce(a.exception, '') NOT IN ('PARTIAL', 'SHORT_DISPUTE', 'RETURNED')
                   GROUP BY 1""")
    # referenced invoices of the party in the bank text: our number without prefix (>= 5 trailing digits) or a
    # fuzzy reference found by T3f (typo / po / exact; a bare truncation is too weak on its own)
    fz = f"{w}/fuzz_cands.parquet"
    con.execute(f"""CREATE TABLE REF AS
      WITH tk AS (SELECT bank_txn_id, unnest(doc_tokens) tok FROM '{w}/lines.parquet' WHERE direction = 'IN'),
           suf AS (SELECT l.bank_txn_id, i.item_id FROM L l JOIN tk USING (bank_txn_id)
                   JOIN '{w}/items.parquet' i ON i.party_id = l.party_id AND i.kind = 'ARI' AND i.currency = l.currency
                        AND i.issue_date <= l.bd AND i.open_at_start_cents > 0
                   WHERE regexp_matches(tk.tok, '^[0-9]{{5,9}}$') AND length(i.doc_digits) > length(tk.tok)
                         AND ends_with(i.doc_digits, tk.tok)),
           fz AS (SELECT f.bank_txn_id, f.item_id FROM {"'" + fz + "'" if os.path.exists(fz) else "suf"} f
                  JOIN L l USING (bank_txn_id) JOIN IT i USING (item_id) WHERE i.party_id = l.party_id
                  {"AND f.how <> 'trunc'" if os.path.exists(fz) else ""})
      SELECT DISTINCT bank_txn_id, item_id FROM (SELECT * FROM suf UNION ALL SELECT * FROM fz)""")
    con.execute("CREATE TABLE NEW (bank_txn_id VARCHAR, item_id VARCHAR, n_cand INTEGER, n_sol INTEGER)")
    total = []
    for p in range(PASSES):
        # settled invoices per party, to count the skips of each candidate
        con.execute("""CREATE OR REPLACE TABLE SETTLED AS
          SELECT i.party_id, i.issue_date, u.first_bd FROM USED u JOIN IT i USING (item_id)""")
        rows = con.execute(f"""
          WITH live AS (
            SELECT l.bank_txn_id, l.cash, i.item_id, i.v, i.issue_date, i.party_id, l.bd
            FROM L l JOIN IT i ON i.party_id = l.party_id AND i.currency = l.currency
                 AND i.issue_date <= l.bd AND l.bd - i.issue_date <= {AGE_MAX}
            LEFT JOIN USED u ON u.item_id = i.item_id
            WHERE (u.item_id IS NULL OR u.first_bd >= l.bd)
              AND l.bank_txn_id NOT IN (SELECT bank_txn_id FROM NEW)),
          sk AS (
            SELECT c.bank_txn_id, c.item_id, count(s.issue_date) skips
            FROM live c LEFT JOIN SETTLED s ON s.party_id = c.party_id AND s.issue_date > c.issue_date AND s.first_bd < c.bd AND s.first_bd > c.bd - {AGE_MAX}
            GROUP BY 1, 2),
          refs AS (
            SELECT l.bank_txn_id, l.cash, i.item_id, i.v, i.issue_date
            FROM REF r JOIN L l USING (bank_txn_id) JOIN IT i USING (item_id)
            LEFT JOIN USED u ON u.item_id = i.item_id
            WHERE (u.item_id IS NULL OR u.first_bd >= l.bd) AND l.bank_txn_id NOT IN (SELECT bank_txn_id FROM NEW)),
          cand AS (
            SELECT bank_txn_id, cash, item_id, v, issue_date, true refd FROM refs
            UNION ALL
            SELECT c.bank_txn_id, c.cash, c.item_id, c.v, c.issue_date, false
            FROM live c JOIN sk USING (bank_txn_id, item_id)
            WHERE sk.skips <= {SKIP_MAX} AND c.item_id NOT IN (SELECT item_id FROM refs r WHERE r.bank_txn_id = c.bank_txn_id))
          SELECT bank_txn_id, any_value(cash), list(item_id ORDER BY issue_date, item_id), list(v ORDER BY issue_date, item_id),
                 list(refd ORDER BY issue_date, item_id)
          FROM cand GROUP BY 1
          HAVING sum((NOT refd)::int) <= {MAX_CAND} AND (count(*) >= {MIN_CAND} OR bool_or(refd))""").fetchall()
        out, n_multi, n_none = [], 0, 0
        for bt, cash, ids, vs, refd in rows:
            forced = [i for i in range(len(vs)) if refd[i]]
            rest = cash - sum(vs[i] for i in forced)
            free = [i for i in range(len(vs)) if not refd[i]]
            if forced and rest == 0:
                r, free = [(0, 0)], []
            elif rest <= 0:
                n_none += 1
                continue
            else:
                r = solve([vs[i] for i in free], rest, 2 if MARG >= 1 else SOL_CAP)
            h = len(free) // 2
            if len(r) == 1 or (1 < len(r) < SOL_CAP):
                sets = [forced + [free[i] for i in range(h) if ma >> i & 1] + [free[h + i] for i in range(len(free) - h) if mb >> i & 1]
                        for ma, mb in r]
                cnt = collections.Counter(i for st in sets for i in st)
                chosen = [ids[i] for i, c in cnt.items() if c >= MARG * len(sets)]
                out.extend((bt, it, len(vs) if not forced else -len(forced), len(sets)) for it in chosen)
                if len(sets) > 1:
                    n_multi += 1
            elif r:
                n_multi += 1
            else:
                n_none += 1
        cnt = collections.Counter(it for _, it, _, _ in out)        # an item claimed by two lines goes to neither
        bad = {bt for bt, it, _, _ in out if cnt[it] > 1}
        out = [x for x in out if x[0] not in bad]
        print(f"  pass {p + 1}: {len(rows)} lines tried, {len({b for b, _, _, _ in out})} lines, {len(out)} pairs "
              f"(multi {n_multi}, none {n_none}, conflicts {len(bad)})")
        if not out:
            break
        con.register("newdf", pa.table({"bank_txn_id": [b for b, _, _, _ in out], "item_id": [i for _, i, _, _ in out],
                                        "n_cand": [n for _, _, n, _ in out], "n_sol": [k for _, _, _, k in out]}))
        con.execute("INSERT INTO NEW SELECT * FROM newdf")
        con.execute("""INSERT INTO USED SELECT n.item_id, l.bd FROM newdf n JOIN L l USING (bank_txn_id)
                       WHERE n.item_id NOT IN (SELECT item_id FROM USED)""")
        total.extend(out)
    con.execute(f"""COPY (
      SELECT * FROM A WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM NEW)
      UNION ALL BY NAME
      SELECT n.bank_txn_id, n.item_id, i.v amount_cents, NULL::VARCHAR category, '' exception,
             '{RULE}' || CASE WHEN n.n_cand < 0 THEN '.ref' ELSE '' END || CASE WHEN n.n_sol = 1 THEN '' ELSE '.marg' END rule,
             CASE WHEN n.n_sol = 1 THEN 0.8 ELSE {MARG} END confidence,
             CASE WHEN n.n_cand < 0 THEN (-n.n_cand) || ' invoices cited in the text (number without prefix / fuzzy) + '
                  ELSE '' END || 'unique subset of the party''s live invoices (age<={AGE_MAX}d, skips<={SKIP_MAX}, '
             || abs(n.n_cand) || ' cand, ' || n.n_sol || ' solutions) = cash' evidence
      FROM NEW n JOIN IT i USING (item_id)
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    print(f"[{phase}] T23 agecomb: {len({b for b, _, _, _ in total})} lines, {len(total)} pairs ({time.time() - t0:.0f}s)")
    if do_eval and os.path.exists("data_cache/train/golden_allocations.parquet"):
        r = con.execute("""
          WITH g AS (SELECT bank_txn_id, item_id FROM 'data_cache/train/golden_allocations.parquet' WHERE item_id IS NOT NULL),
               per AS (SELECT n.bank_txn_id, count(*) n_a, sum((g.item_id IS NOT NULL)::int) n_ok,
                              (SELECT count(*) FROM g WHERE g.bank_txn_id = n.bank_txn_id) n_g
                       FROM NEW n LEFT JOIN g USING (bank_txn_id, item_id) GROUP BY 1)
          SELECT count(*), sum((n_a = n_ok AND n_a = n_g)::int), sum(n_a), sum(n_ok) FROM per""").fetchone()
        print(f"  eval: lines {r[0]} exact {r[1]} | pairs {r[2]} ok {r[3]} (P {r[3] / max(1, r[2]):.3f})")
        for row in con.execute("""
          WITH g AS (SELECT bank_txn_id, item_id FROM 'data_cache/train/golden_allocations.parquet' WHERE item_id IS NOT NULL)
          SELECT least(n.n_sol, 9) sol, CASE WHEN n_cand < 0 THEN -1 WHEN n_cand <= 12 THEN 12 WHEN n_cand <= 18 THEN 18 WHEN n_cand <= 24 THEN 24 ELSE 99 END nc,
                 count(*), sum((g.item_id IS NOT NULL)::int) FROM NEW n LEFT JOIN g USING (bank_txn_id, item_id) GROUP BY 1, 2 ORDER BY 1, 2""").fetchall():
            print("   sol/cand/pairs/ok", row)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--eval", action="store_true", help="window only: precision against the golden")
    a = ap.parse_args()
    build(a.phase, a.eval)
