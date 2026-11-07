"""T3x · receipts that pay a set of a party's open invoices net of credit notes, or in another currency (FX).

    python -m src.cnfx --phase window|live|final [--alloc path] [--rules subset,run]

Runs after src.unident and rewrites work/<phase>/allocations.parquet (same columns). Only receipts with no item, or
whose current items fall short of the cash, get extra rows (their no-item row is dropped). Items already allocated
anywhere in the phase are never reused, and an item picked by two lines is given to neither.

  target  cash still to explain, in the currency the payer instructed: the line currency, or orig_amount/orig_currency
          when the bank shows the instructed amount in another currency (FX line, POLICIES §3 FX_DIFFERENCE)
  pool    open, unallocated AR invoices and credit notes (signed: ARC negative) of the line's parties (T4 rank 1 and
          the parties of the items already on the line), in the target currency, issued on or before the booking date
  alt     receipts with no item and no solution over that pool: each of T4's rank 2-3 parties on its own; accepted
          only if exactly one solution over all of them (rule suffix _alt)
  search  per line, over a small pool (numpy, meet-in-the-middle on <= MITM_MAX items): every subset whose signed sum
          equals the target within 1 cent; over a bigger pool: runs of consecutive items in issue-date order.
          A line is accepted only when exactly one solution exists.
  rows    every item at its full open amount; invoices of an FX line carry FX_DIFFERENCE, credit notes no exception.
Rule ids T3x.<search>.<cn|fx|plain>[.add], confidence by search kind, evidence = pool size, solution size, target.
Window 2024-07 -> 2025-06 on top of 91.04: 6.4k pairs on 1.8k lines (pair precision: plain 90-99 %, fx 100 %,
cn 66-93 %) -> SCORE 91.28 (recall 89.49 -> 89.87, txn accuracy 91.30 -> 91.69; ar_fx 50 -> 65 %, ar_1toN_credit_note
59 -> 61 %).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb
import numpy as np
import pandas as pd

from src.common import REPO

MITM_MAX = 22      # meet-in-the-middle over 2 x 2^11 subsets
RUN_MAX = 400      # pool cap for the consecutive-run search
TOL = 1            # cents
ALT_RANKS = 3      # T4 ranks tried when rank 1 has no solution (0 = off)
ALT_MIN_CONF = 0.0
AGE_MAX = {"ARI": 365, "ARC": 240}   # days between issue and receipt (TRAIN golden: 99.7 % / 99.6 % of AR pairs)
DEFAULT_RULES = "subset,run,subset_alt,run_alt"
CONF = {"subset": 0.9, "run": 0.85, "alt": 0.6}


def _sums(v: np.ndarray) -> np.ndarray:
    """All 2^len(v) subset sums (index = bitmask)."""
    s = np.zeros(1, dtype=np.int64)
    for x in v:
        s = np.concatenate([s, s + x])
    return s


def subset_solutions(v: np.ndarray, t: int, cap: int = 3) -> list[np.ndarray]:
    n = len(v)
    h = n // 2
    a, b = _sums(v[:h]), _sums(v[h:])
    order = np.argsort(b, kind="stable")
    bs = b[order]
    lo = np.searchsorted(bs, t - a - TOL, side="left")
    hi = np.searchsorted(bs, t - a + TOL, side="right")
    cnt = hi - lo
    out = []
    for ia in np.nonzero(cnt)[0]:
        for k in range(lo[ia], hi[ia]):
            ib = order[k]
            if ia == 0 and ib == 0:
                continue
            mask = np.array([(ia >> j) & 1 for j in range(h)] + [(ib >> j) & 1 for j in range(n - h)], dtype=bool)
            out.append(mask)
            if len(out) >= cap:
                return out
    return out


def run_solutions(v: np.ndarray, t: int, cap: int = 3) -> list[tuple[int, int]]:
    p = np.concatenate([[0], np.cumsum(v)])
    out = []
    order = np.argsort(p, kind="stable")
    ps = p[order]
    for j in range(1, len(p)):
        lo = np.searchsorted(ps, p[j] - t - TOL, side="left")
        hi = np.searchsorted(ps, p[j] - t + TOL, side="right")
        for k in range(lo, hi):
            i = order[k]
            if i < j:
                out.append((int(i), j))
                if len(out) >= cap:
                    return out
    return out


def build(phase: str, alloc: str | None = None, rules: str = DEFAULT_RULES, work: str | None = None) -> None:
    t0 = time.time()
    w = work or os.path.join(REPO, "work", phase)
    p = alloc or f"{w}/allocations.parquet"
    keep = set(r for r in rules.split(",") if r)
    con = duckdb.connect()
    con.execute(f"SET threads=8; SET memory_limit='10GB'; SET temp_directory='{REPO}/work/tmp'")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE I AS SELECT item_id, kind, party_id, currency, open_at_start_cents open_c, issue_date
                    FROM '{w}/items.parquet' WHERE kind IN ('ARI', 'ARC') AND open_at_start_cents > 0""")
    con.execute("CREATE TABLE USED AS SELECT DISTINCT item_id FROM A WHERE item_id IS NOT NULL")
    # target receipts: no item, or items (in the target currency) short of the instructed amount
    con.execute(f"""CREATE TABLE LT AS
      WITH l AS (SELECT bank_txn_id, booking_date bd, currency lcur, abs(amount_cents) cash,
                        CASE WHEN orig_currency IS NOT NULL AND orig_currency <> currency AND orig_amount_cents > 0
                             THEN orig_currency ELSE currency END tcur,
                        CASE WHEN orig_currency IS NOT NULL AND orig_currency <> currency AND orig_amount_cents > 0
                             THEN abs(orig_amount_cents) ELSE abs(amount_cents) END tamt
                 FROM '{w}/lines.parquet' WHERE direction = 'IN'),
      s AS (SELECT l.bank_txn_id, any_value(l.bd) bd, any_value(l.lcur) lcur, any_value(l.tcur) tcur,
                   any_value(l.tamt) tamt, count(a.item_id) nit,
                   coalesce(sum(CASE WHEN ii.kind IN ('ARC', 'APC') THEN -a.amount_cents ELSE a.amount_cents END)
                            FILTER (WHERE a.item_id IS NOT NULL), 0) cov,
                   bool_or(coalesce(a.exception, '') IN ('RETURNED', 'PARTIAL', 'SHORT_DISPUTE', 'DUPLICATE_PAYMENT')) bad
            FROM l JOIN A a USING (bank_txn_id)
            LEFT JOIN (SELECT item_id, kind, currency FROM '{w}/items.parquet') ii ON ii.item_id = a.item_id
            GROUP BY l.bank_txn_id)
      SELECT bank_txn_id, bd, lcur, tcur, tamt, nit, cov, tamt - cov rem FROM s
      WHERE NOT coalesce(bad, false) AND (nit = 0 OR cov < tamt - greatest(100, 0.005 * tamt))""")
    # a line whose items are in another currency than the target is left alone
    con.execute(f"""DELETE FROM LT WHERE bank_txn_id IN (
      SELECT a.bank_txn_id FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN LT USING (bank_txn_id)
      WHERE i.currency <> LT.tcur)""")
    lp = f"{w}/line_party.parquet"
    con.execute(f"""CREATE TABLE LP AS
      SELECT bank_txn_id, party_id, 'p1' grp FROM '{lp}' WHERE rank = 1 AND bank_txn_id IN (SELECT bank_txn_id FROM LT)
      UNION
      SELECT a.bank_txn_id, i.party_id, 'p1' FROM A a JOIN I i USING (item_id) WHERE a.bank_txn_id IN (SELECT bank_txn_id FROM LT)""")
    if ALT_RANKS:
        # receipts with no item: T4's runner-up parties, each on its own, as a fallback when rank 1 has no solution
        con.execute(f"""INSERT INTO LP SELECT p.bank_txn_id, p.party_id, 'alt' FROM '{lp}' p JOIN LT t USING (bank_txn_id)
          WHERE p.rank BETWEEN 2 AND {ALT_RANKS} AND t.nit = 0 AND p.confidence >= {ALT_MIN_CONF}
            AND p.party_id NOT IN (SELECT party_id FROM LP q WHERE q.bank_txn_id = p.bank_txn_id)""")
    con.execute(f"""CREATE TABLE POOL AS
      SELECT t.bank_txn_id, CASE LP.grp WHEN 'p1' THEN 'p1' ELSE LP.party_id END grp, i.item_id, i.kind, i.open_c,
             i.issue_date, CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END sv
      FROM LT t JOIN LP USING (bank_txn_id) JOIN I i ON i.party_id = LP.party_id AND i.currency = t.tcur
      WHERE i.issue_date <= t.bd AND t.bd - i.issue_date <= CASE i.kind WHEN 'ARC' THEN {AGE_MAX['ARC']} ELSE {AGE_MAX['ARI']} END
        AND i.item_id NOT IN (SELECT item_id FROM USED)""")
    df = con.sql(f"""SELECT p.bank_txn_id, p.grp, any_value(t.rem) rem, list(p.item_id ORDER BY p.issue_date, p.item_id) ids,
                            list(p.sv ORDER BY p.issue_date, p.item_id) v, count(*) n
                     FROM POOL p JOIN LT t USING (bank_txn_id) GROUP BY 1, 2 HAVING count(*) <= {RUN_MAX}
                     ORDER BY 1, p.grp <> 'p1', 2""").df()
    print(f"[{phase}] cnfx: {con.sql('SELECT count(*) FROM LT').fetchall()[0][0]} target lines, {len(df)} pools "
          f"({time.time() - t0:.0f}s)")
    sols = {}   # line -> list of (grp, how, n, selection)
    diag = []
    for b, grp, rem, ids, v, n in df.itertuples(index=False):
        v = np.asarray(v, dtype=np.int64)
        rem = int(rem)
        if rem <= 0:
            continue
        if n <= MITM_MAX:
            how, sel = "subset", [np.nonzero(m)[0] for m in subset_solutions(v, rem)]
        else:
            how, sel = "run", [np.arange(i, j) for i, j in run_solutions(v, rem)]
        diag.append((b, grp, int(n), how, len(sel)))
        sols.setdefault(b, []).extend((grp, how, int(n), ids, x, rem) for x in sel)
    picks = []
    for b, lst in sols.items():
        p1 = [x for x in lst if x[0] == "p1"]
        cand = p1 if p1 else [x for x in lst if x[0] != "p1"]
        if len(cand) != 1:
            continue
        grp, how, n, ids, x, rem = cand[0]
        how = how if grp == "p1" else how + "_alt"
        for k in x:
            picks.append((b, ids[k], how, n, len(x), rem))
    pd.DataFrame(diag, columns=["bank_txn_id", "grp", "n", "how", "nsol"]).to_parquet(f"{w}/cnfx_diag.parquet")
    P = pd.DataFrame(picks, columns=["bank_txn_id", "item_id", "how", "n", "k", "rem"])
    con.register("Pdf", P)
    con.execute("CREATE TABLE PICK AS SELECT * FROM Pdf")
    con.execute(f"DELETE FROM PICK WHERE how NOT IN ({', '.join(repr(r) for r in keep) or 'NULL'})")
    con.execute("DELETE FROM PICK WHERE bank_txn_id IN (SELECT bank_txn_id FROM PICK WHERE item_id IN "
                "(SELECT item_id FROM PICK GROUP BY 1 HAVING count(DISTINCT bank_txn_id) > 1))")
    con.execute(f"""CREATE TABLE NEW AS
      WITH f AS (SELECT p.*, i.kind, i.open_c, t.tcur, t.lcur, t.nit,
                        bool_or(i.kind = 'ARC') OVER (PARTITION BY p.bank_txn_id) has_cn
                 FROM PICK p JOIN I i USING (item_id) JOIN LT t USING (bank_txn_id))
      SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category,
             CASE WHEN tcur <> lcur AND kind = 'ARI' THEN 'FX_DIFFERENCE' ELSE '' END exception,
             'T3x.' || how || '.' || CASE WHEN tcur <> lcur THEN 'fx' WHEN has_cn THEN 'cn' ELSE 'plain' END
               || CASE WHEN nit > 0 THEN '.add' ELSE '' END rule,
             CASE how WHEN 'subset' THEN {CONF['subset']} WHEN 'run' THEN {CONF['run']} ELSE {CONF['alt']} END confidence,
             'party open items: ' || k || ' of ' || n || ' sum to the ' || tcur || ' amount left ' || rem
               || ' (unique ' || how || ')' evidence
      FROM f""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    # POLICIES §3 FX_DIFFERENCE: every fully cleared invoice of a line in another currency carries the flag
    # (it replaces an empty exception or a SHORT_* one); partial / disputed rows and credit notes keep theirs
    con.execute(f"""CREATE TABLE FXF AS
      SELECT a.bank_txn_id, a.item_id FROM A a JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
      WHERE i.kind IN ('ARI', 'API') AND i.currency <> l.currency AND a.amount_cents >= i.open_at_start_cents
        AND coalesce(a.exception, '') IN ('', 'SHORT_DISCOUNT', 'SHORT_WITHHOLDING', 'SHORT_BANK_CHARGES')""")
    con.execute("""UPDATE A SET exception = 'FX_DIFFERENCE', rule = A.rule || '+T3x.fx_flag',
                          evidence = A.evidence || '; T3x: invoice currency <> line currency, fully cleared -> FX_DIFFERENCE'
                   FROM FXF f WHERE A.bank_txn_id = f.bank_txn_id AND A.item_id = f.item_id""")
    con.execute(f"COPY NEW TO '{w}/cnfx_new.parquet' (FORMAT parquet)")
    con.execute(f"COPY A TO '{p}.tmp' (FORMAT parquet)")
    os.replace(f"{p}.tmp", p)
    print(con.sql("SELECT rule, count(DISTINCT bank_txn_id) lines, count(*) pairs FROM NEW GROUP BY 1 ORDER BY 3 DESC").fetchall())
    print(f"[{phase}] cnfx: FX_DIFFERENCE set on {con.sql('SELECT count(*) FROM FXF').fetchall()[0][0]} rows")
    print(f"[{phase}] cnfx → {p} ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc", help="allocations parquet to rewrite (default work/<phase>/allocations.parquet)")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="searches to keep: subset,run")
    ap.add_argument("--work", help="work folder to read (default work/<phase>)")
    a = ap.parse_args()
    build(a.phase, a.alloc, a.rules, a.work)


if __name__ == "__main__":
    main()
