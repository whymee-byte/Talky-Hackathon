"""T3m · multi-invoice receipts whose bank text lists many of our numbers truncated / as suffixes / with typos.

    python -m src.multiref --phase window|live|final [--alloc path] [--rules exact,over,short]

Reads work/<phase>/{allocations,lines,items,fuzz_cands}.parquet and rewrites allocations.parquet (same columns).
Only receipts that ended with no item, or whose current items fall short of the cash, get extra rows; their no-item
row is dropped. Items already allocated anywhere in the phase are never reused, and an item picked by two lines is
given to neither.

  1. candidates   fuzz_cands (T3f: open items of the line's T4 parties whose digits match a text token), each
                  (line, item, token) scored: exact > PO > item digits as suffix of the token > token as prefix of the
                  item (by token length) > token as suffix of the item > one-digit typo
  2. pool         a line with <= NMAX candidate items keeps them all; otherwise each token keeps its best item (strength,
                  then oldest issued), greedily so that no two tokens take the same item, and the line is kept if that
                  leaves <= NMAX items
  3. subset       every subset of the pool (bitmask, credit notes negative) is compared with the cash still to explain:
                  exact   |sum − cash| <= 1 cent                                 → items at their open amount
                  short   0.9 · sum <= cash < sum (one deduction, SHORT_DISPUTE-like) → the largest invoice gets the cash
                          left after the others, exception SHORT_DISPUTE
                  over    sum < cash <= 1.1 · sum (cash also pays something we do not see) → items at their open amount
                  preference: exact, then short, then over; within a fit the smallest |diff|, then more items
  4. fill         lines with no subset: the greedy token items, if they do not over-cover the cash (see FILL_MAX)
Default fits: exact + fill. Window (2024-07 → 2025-06), on top of 87.70: exact 942 pairs / 100 % precision,
fill.big 2254 / 90.8 %, fill.small 5471 / 75.7 % → SCORE 87.83 (allocation recall 86.44 → 86.92).
short (65 %, SHORT_DISPUTE never right) and over (80 %) do not raise the window SCORE: off by default.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, safe_stdout

NMAX = 14          # bitmask subset enumeration up to 2^14 subsets per line
ROUNDS = 6         # greedy token -> item assignment rounds
SHORT_MIN = 0.90   # cash >= 90 % of the items (deduction up to ~10 %)
OVER_MAX = 1.10    # cash <= 110 % of the items
FILL_MAX = 1.15
FILL_MAX_WEAK = 1.5
DEFAULT_RULES = "exact,fill"
CONF = {"exact": 0.95, "short": 0.6, "over": 0.6, "fill": 0.7}


def build(phase: str, alloc: str | None = None, rules: str = DEFAULT_RULES) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase)
    p = alloc or f"{w}/allocations.parquet"
    cands = f"{w}/fuzz_cands.parquet"
    if not os.path.exists(cands):
        print(f"[{phase}] multiref: no {cands}, nothing to do")
        return
    keep = [r for r in rules.split(",") if r]
    con = duckdb.connect()
    con.execute("SET threads=8; SET memory_limit='10GB'")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"CREATE TABLE I AS SELECT item_id, kind, currency, doc_digits dig, "
                f"ltrim(regexp_replace(coalesce(po_norm, ''), '[^0-9]', '', 'g'), '0') po_dig FROM '{w}/items.parquet'")
    con.execute("CREATE TABLE USED AS SELECT DISTINCT item_id FROM A WHERE item_id IS NOT NULL")
    # receipts with no item, or whose items (same currency) fall short of the cash
    con.execute(f"""CREATE TABLE LT AS
      WITH s AS (
        SELECT l.bank_txn_id, abs(l.amount_cents) cash, l.text_norm, count(a.item_id) nit,
               coalesce(sum(CASE WHEN i.kind IN ('ARC', 'APC') THEN -a.amount_cents ELSE a.amount_cents END)
                        FILTER (WHERE a.item_id IS NOT NULL), 0) cov,
               bool_or(coalesce(a.exception, '') = 'RETURNED') ret,
               bool_or(i.currency IS NOT NULL AND i.currency <> l.currency) xcur
        FROM '{w}/lines.parquet' l JOIN A a USING (bank_txn_id) LEFT JOIN I i ON i.item_id = a.item_id
        WHERE l.direction = 'IN' GROUP BY ALL)
      SELECT *, cash - cov rem FROM s
      WHERE NOT xcur AND NOT ret AND (nit = 0 OR cov < cash - greatest(100, 0.005 * cash))""")
    con.execute(f"""CREATE TABLE F AS SELECT f.bank_txn_id, f.item_id, f.kind, f.open_c, f.issue_date, i.dig, i.po_dig
                    FROM '{cands}' f JOIN I i USING (item_id)
                    WHERE f.bank_txn_id IN (SELECT bank_txn_id FROM LT) AND f.item_id NOT IN (SELECT item_id FROM USED)
                      AND f.open_c > 0""")
    # digit tokens of the text (1-3 adjacent tokens joined, as in T3f)
    con.execute("""CREATE TABLE TK AS
      WITH t AS (SELECT bank_txn_id, string_split(text_norm, ' ') tok FROM LT WHERE bank_txn_id IN (SELECT bank_txn_id FROM F)),
           g AS (SELECT bank_txn_id, array_to_string(tok[i:i + k - 1], '') x
                 FROM t, LATERAL (SELECT unnest(range(1, least(len(tok), 60) + 1)) i), LATERAL (SELECT unnest([1, 2, 3]) k)
                 WHERE i + k - 1 <= len(tok) AND regexp_matches(tok[i], '[0-9]'))
      SELECT DISTINCT bank_txn_id, ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0') d FROM g
      WHERE length(ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0')) BETWEEN 6 AND 14
        AND length(regexp_replace(x, '[0-9]', '', 'g')) <= 6""")
    con.execute("""CREATE TABLE FM AS
      SELECT * FROM (
        SELECT f.bank_txn_id, f.item_id, f.kind, f.open_c, f.issue_date, t.d tok,
          CASE WHEN t.d = f.dig THEN 100 + length(f.dig)
               WHEN length(f.po_dig) >= 6 AND t.d = f.po_dig THEN 90 + length(t.d)
               WHEN length(t.d) > length(f.dig) AND ends_with(t.d, f.dig) THEN 50 + length(f.dig)
               WHEN length(t.d) < length(f.dig) AND starts_with(f.dig, t.d) THEN length(t.d)
               WHEN length(t.d) < length(f.dig) AND ends_with(f.dig, t.d) THEN length(t.d) - 0.5
               WHEN length(t.d) = length(f.dig) AND damerau_levenshtein(t.d, f.dig) <= 1 THEN length(t.d) - 1
          END strength
        FROM F f JOIN TK t USING (bank_txn_id)) WHERE strength IS NOT NULL""")
    # greedy: each token takes its best free item (strongest match, oldest issued); an item goes to its strongest token
    con.execute("CREATE TABLE ASG (bank_txn_id VARCHAR, tok VARCHAR, item_id VARCHAR, strength DOUBLE)")
    for _ in range(ROUNDS):
        con.execute("""INSERT INTO ASG
          WITH c AS (SELECT * FROM FM m WHERE NOT EXISTS (SELECT 1 FROM ASG a WHERE a.bank_txn_id = m.bank_txn_id
                                                            AND (a.tok = m.tok OR a.item_id = m.item_id))),
               b AS (SELECT * FROM c QUALIFY row_number() OVER (PARTITION BY bank_txn_id, tok ORDER BY strength DESC, issue_date, item_id) = 1)
          SELECT bank_txn_id, tok, item_id, strength FROM b
          QUALIFY row_number() OVER (PARTITION BY bank_txn_id, item_id ORDER BY strength DESC, length(tok) DESC, tok) = 1""")
    con.execute("""CREATE TABLE FMI AS SELECT bank_txn_id, item_id, any_value(kind) kind, any_value(open_c) open_c,
                          max(strength) strength FROM FM GROUP BY 1, 2""")
    con.execute(f"""CREATE TABLE POOL AS
      WITH n AS (SELECT bank_txn_id, count(*) n FROM FMI GROUP BY 1)
      SELECT f.*, 'all' pool FROM FMI f JOIN n USING (bank_txn_id) WHERE n.n <= {NMAX}
      UNION ALL
      SELECT f.*, 'tok' FROM FMI f JOIN n USING (bank_txn_id)
      WHERE n.n > {NMAX} AND (f.bank_txn_id, f.item_id) IN (SELECT (bank_txn_id, item_id) FROM ASG)
        AND f.bank_txn_id IN (SELECT bank_txn_id FROM ASG GROUP BY 1 HAVING count(*) <= {NMAX})""")
    con.execute("""CREATE TABLE V AS
      SELECT bank_txn_id, any_value(pool) pool, list(item_id ORDER BY item_id) ids,
             list(CASE WHEN kind = 'ARC' THEN -open_c ELSE open_c END ORDER BY item_id) v, count(*) n
      FROM POOL GROUP BY 1""")
    con.execute(f"""CREATE TABLE SUB AS
      WITH x AS (SELECT v.bank_txn_id, v.pool, v.ids, v.v, v.n, m.mask,
                        list_sum(list_transform(range(v.n), lambda j: CASE WHEN (m.mask >> j) & 1 = 1 THEN v.v[j + 1] ELSE 0 END)) s
                 FROM V v, LATERAL (SELECT unnest(range(1, (1::BIGINT << v.n))) mask) m)
      SELECT x.*, l.rem, x.s - l.rem diff, bit_count(x.mask) k,
             CASE WHEN abs(x.s - l.rem) <= 1 THEN 'exact'
                  WHEN x.s - l.rem > 1 AND l.rem >= {SHORT_MIN} * x.s THEN 'short'
                  WHEN l.rem - x.s > 1 AND l.rem <= {OVER_MAX} * x.s THEN 'over' END fit
      FROM x JOIN LT l USING (bank_txn_id) WHERE x.s > 0""")
    con.execute("""CREATE TABLE BEST AS
      SELECT *, count(*) FILTER (WHERE fit = 'exact') OVER (PARTITION BY bank_txn_id) n_exact
      FROM SUB WHERE fit IS NOT NULL
      QUALIFY row_number() OVER (PARTITION BY bank_txn_id
                                 ORDER BY CASE fit WHEN 'exact' THEN 0 WHEN 'short' THEN 1 ELSE 2 END, abs(diff), k DESC, mask) = 1""")
    con.execute("""CREATE TABLE PICK AS
      SELECT b.bank_txn_id, b.fit, b.pool, b.n, b.k, b.n_exact, b.diff, b.rem, u.j, b.ids[u.j + 1] item_id, b.v[u.j + 1] sv
      FROM BEST b, LATERAL (SELECT unnest(range(b.n)) j) u WHERE (b.mask >> u.j) & 1 = 1""")
    # an item picked by two lines goes to neither
    con.execute("DELETE FROM PICK WHERE bank_txn_id IN (SELECT bank_txn_id FROM PICK WHERE item_id IN "
                "(SELECT item_id FROM PICK GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))")
    con.execute(f"DELETE FROM PICK WHERE fit NOT IN ({', '.join(repr(r) for r in keep) or 'NULL'})")
    if "fill" in keep:
        # lines with no accepted subset: each token's greedy item, when the assigned items do not over-cover the cash
        # (signed sum <= FILL_MAX x cash; <= FILL_MAX_WEAK x cash for 6-digit prefixes / typos, which on the window
        # land on the right item 76-99 % of the time while longer prefixes drop to 16-64 % above 1.15x)
        con.execute(f"""INSERT INTO PICK
          WITH a AS (SELECT a.bank_txn_id, a.item_id, a.strength, f.kind, f.open_c,
                            CASE WHEN f.kind = 'ARC' THEN -f.open_c ELSE f.open_c END sv,
                            count(*) OVER (PARTITION BY a.bank_txn_id) n,
                            sum(CASE WHEN f.kind = 'ARC' THEN -f.open_c ELSE f.open_c END) OVER (PARTITION BY a.bank_txn_id) s
                     FROM ASG a JOIN FMI f USING (bank_txn_id, item_id)
                     WHERE a.bank_txn_id NOT IN (SELECT bank_txn_id FROM PICK))
          SELECT a.bank_txn_id, 'fill', CASE WHEN a.n <= {NMAX} THEN 'small' ELSE 'big' END, a.n, a.n, 0, a.s - l.rem, l.rem,
                 0, a.item_id, a.sv
          FROM a JOIN LT l USING (bank_txn_id)
          WHERE a.s > 0 AND (a.s <= {FILL_MAX} * l.rem OR (a.strength < 7 AND a.s <= {FILL_MAX_WEAK} * l.rem))
            AND a.item_id NOT IN (SELECT item_id FROM PICK)""")
        con.execute("DELETE FROM PICK WHERE fit = 'fill' AND item_id IN "
                    "(SELECT item_id FROM PICK GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1)")
    # short: the largest invoice absorbs the deduction (SHORT_DISPUTE), the rest at open amount
    con.execute(f"""CREATE TABLE NEW AS
      SELECT bank_txn_id, item_id,
             CASE WHEN fit = 'short' AND big THEN sv - diff ELSE abs(sv) END amount_cents, NULL::VARCHAR category,
             CASE WHEN fit = 'short' AND big THEN 'SHORT_DISPUTE' ELSE '' END exception,
             'T3m.' || fit || '.' || pool rule,
             CASE fit WHEN 'exact' THEN {CONF['exact']} WHEN 'short' THEN {CONF['short']} WHEN 'fill' THEN {CONF['fill']}
                      ELSE {CONF['over']} END
               * CASE WHEN n_exact > 1 THEN 0.8 ELSE 1 END confidence,
             'text tokens -> ' || k || ' of ' || n || ' items, signed sum - cash left = ' || diff || ' (' || fit || ')' evidence
      FROM (SELECT *, row_number() OVER (PARTITION BY bank_txn_id ORDER BY sv DESC, item_id) = 1 big FROM PICK)""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")
    print(con.sql("SELECT rule, count(DISTINCT bank_txn_id) lines, count(*) pairs FROM NEW GROUP BY 1 ORDER BY 3 DESC").fetchall())
    print(f"[{phase}] multiref → {p} ({time.time() - t0:.0f}s)")


def main():
    safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="fits to keep: exact,short,over")
    a = ap.parse_args()
    build(a.phase, a.alloc, a.rules)


if __name__ == "__main__":
    main()
