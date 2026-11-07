"""T8b · invoice-PDF clauses (doc_facts) applied to allocations.parquet, after src.postfix (POLICIES §3, §5.7-5.8).

    python -m src.docrules --phase window|live|final [--skip rule,rule]

Reads work/<phase>/{allocations,lines,items,line_party,doc_facts}.parquet and rewrites allocations.parquet in place
(same columns; touched rows get '+T8b.<rule>' appended to `rule`). Rules:
  replaces   a row on an invoice that a PDF says was cancelled ("anula y sustituye a X") → moved to its replacement
  inst_exc   row on an instalment invoice: instalment k = schedule date closest to the booking date; k < n → PARTIAL
             (amount = cash), k = n → no exception
  ret_exc    retention invoice: cash = total − retention → PARTIAL; cash = retention → release, no exception
  late_exc   line cash = Σ rows − Σ early-payment discount of rows booked after discount days + 3 → those rows PARTIAL
             at total − discount (the discount is not accepted)
  add_*      UNIDENTIFIED receipt whose cash equals, for one open invoice of a candidate payer (line_party) in the line
             currency, an amount the PDF/terms imply (instalment k, total − retention, retention, total − advance,
             total − late discount, a usual partial share of the total) → that invoice
  add_rest   then: UNIDENTIFIED receipt = remaining balance of a partly paid invoice of the payer → that invoice
Partial shares are learned from golden PARTIAL rows of TRAIN lines outside the phase (never the scored lines).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

from src.common import REPO, data_root, safe_stdout

KEEP = "bank_txn_id, item_id, amount_cents, category, exception, rule, confidence, evidence"
NORM = "trim(regexp_replace(strip_accents(upper({})), '[^A-Z0-9]+', ' ', 'g'))"
CENTS = "try_cast(regexp_replace({}, '[^0-9]', '', 'g') AS BIGINT)"   # amounts in PDFs always carry 2 decimals
TOL = "greatest(1, 0.005 * {})"


def _balances(con) -> None:
    """B: per allocation row, the item's balance just before it (booking order), from the current A."""
    con.execute("""CREATE OR REPLACE TABLE B AS
      SELECT a.bank_txn_id, a.item_id, a.amount_cents, a.exception, l.booking_date,
             i.open_at_start_cents - coalesce(sum(CASE WHEN coalesce(a.exception, '') IN ('RETURNED', 'DUPLICATE_PAYMENT') THEN 0
                                                       ELSE a.amount_cents END)
               OVER (PARTITION BY a.item_id ORDER BY l.booking_date, a.bank_txn_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS open_before
      FROM A a JOIN L l USING (bank_txn_id) JOIN I i USING (item_id)""")


def build(phase: str, skip: set[str], work: str | None = None) -> dict:
    w = (work or os.path.join(REPO, "work", phase)).replace(os.sep, "/")
    out = f"{w}/allocations.parquet"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT {KEEP} FROM '{out}'")
    con.execute(f"CREATE TABLE L AS SELECT bank_txn_id, booking_date, amount_cents, currency, direction FROM '{w}/lines.parquet'")
    con.execute(f"CREATE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute(f"CREATE TABLE LP AS SELECT bank_txn_id, rank, party_id FROM '{w}/line_party.parquet' WHERE party_id IS NOT NULL")
    con.execute(f"CREATE TABLE F AS SELECT * FROM '{w}/doc_facts.parquet' WHERE item_id IS NOT NULL")
    n0 = con.execute("SELECT count(*) FROM A").fetchone()[0]
    stats: dict[str, int] = {}

    def tag(rule: str) -> str:
        return f"rule = coalesce(rule, '') || '+T8b.{rule}'"

    # ---- per-invoice clause table --------------------------------------------------------------------------------
    con.execute(f"""CREATE TABLE IF_ AS
      WITH inst AS (
        SELECT item_id, value_num::INT AS n,
               list_transform(string_split(split_part(value_text, '|', 1), '/'), lambda x: try_cast(x AS INT)) AS days,
               list_transform(string_split(regexp_replace(split_part(value_text, '|', 2), '\\s*[A-Z]{{3}}\\s*$', ''), ' / '),
                              lambda x: {CENTS.format('x')}) AS amts
        FROM F WHERE fact = 'INSTALMENTS' QUALIFY row_number() OVER (PARTITION BY item_id) = 1),
      ret AS (
        SELECT item_id, {CENTS.format("regexp_extract(evidence, '\\(([0-9][0-9.,\\s]*[0-9]) [A-Z]{3}\\)', 1)")} AS ret_c,
               try_cast(value_text AS DATE) AS release_date
        FROM F WHERE fact = 'RETENTION' QUALIFY row_number() OVER (PARTITION BY item_id) = 1),
      adv AS (SELECT item_id, round(value_num * 100)::BIGINT AS adv_c FROM F WHERE fact = 'ADVANCE_DEDUCT'
              QUALIFY row_number() OVER (PARTITION BY item_id) = 1)
      SELECT i.item_id, inst.n, inst.days, inst.amts, ret.ret_c, ret.release_date, adv.adv_c
      FROM I i LEFT JOIN inst USING (item_id) LEFT JOIN ret USING (item_id) LEFT JOIN adv USING (item_id)
      WHERE inst.n IS NOT NULL OR ret.ret_c IS NOT NULL OR adv.adv_c IS NOT NULL""")

    # ---- replaces: rows on a cancelled invoice → its replacement -------------------------------------------------
    if "replaces" not in skip:
        con.execute(f"""CREATE TABLE RP AS
          SELECT x.item_id AS old_id, f.item_id AS new_id
          FROM F f JOIN I r ON r.item_id = f.item_id
          JOIN I x ON x.entity = r.entity AND x.party_id = r.party_id AND x.kind = 'ARI' AND x.item_id <> r.item_id
                  AND x.doc_norm = {NORM.format('f.value_text')}
          WHERE f.fact = 'REPLACES'
          QUALIFY count(*) OVER (PARTITION BY x.item_id) = 1""")
        stats["replaces"] = con.execute("""UPDATE A SET item_id = RP.new_id, """ + tag("replaces") + """,
            evidence = coalesce(evidence, '') || '; PDF: replaced invoice ' || RP.old_id
          FROM RP JOIN I n ON n.item_id = RP.new_id WHERE A.item_id = RP.old_id
            AND abs(A.amount_cents - n.total_cents) <= greatest(1, 0.03 * n.total_cents)
            AND NOT EXISTS (SELECT 1 FROM A a2 WHERE a2.bank_txn_id = A.bank_txn_id AND a2.item_id = RP.new_id)""").fetchone()[0]

    # ---- exception fixes on existing rows ------------------------------------------------------------------------
    _balances(con)
    con.execute("""CREATE OR REPLACE TABLE X AS
      SELECT b.*, i.total_cents, i.issue_date, i.currency AS icur, l.currency AS lcur, l.amount_cents AS line_cents,
             f.n, f.days, f.amts, f.ret_c, f.adv_c,
             CASE WHEN f.n IS NOT NULL THEN list_position(
                list_transform(f.days, lambda d: abs(date_diff('day', i.issue_date, b.booking_date) - d)),
                list_min(list_transform(f.days, lambda d: abs(date_diff('day', i.issue_date, b.booking_date) - d)))) END AS k
      FROM B b JOIN I i USING (item_id) JOIN L l USING (bank_txn_id) JOIN IF_ f USING (item_id)
      WHERE i.kind = 'ARI' AND l.direction = 'IN' AND coalesce(b.exception, '') IN ('', 'PARTIAL', 'FX_DIFFERENCE')""")
    fixes = {
        # rule: (condition, new exception, new amount)
        "inst_exc": ("x.n IS NOT NULL AND x.k < x.n", "'PARTIAL'",
                     "CASE WHEN x.icur = x.lcur AND x.amts[x.k] IS NOT NULL THEN x.amts[x.k] ELSE A.amount_cents END"),
        "inst_last": ("x.n IS NOT NULL AND x.k = x.n", "CASE WHEN x.icur <> x.lcur THEN 'FX_DIFFERENCE' ELSE '' END",
                      "A.amount_cents"),
        "ret_exc": (f"x.ret_c IS NOT NULL AND abs(x.amount_cents - (x.total_cents - x.ret_c)) <= {TOL.format('x.total_cents')}",
                    "'PARTIAL'", "A.amount_cents"),
        "ret_release": (f"x.ret_c IS NOT NULL AND abs(x.amount_cents - x.ret_c) <= {TOL.format('x.ret_c')}",
                        "CASE WHEN x.icur <> x.lcur THEN 'FX_DIFFERENCE' ELSE '' END", "A.amount_cents"),
    }
    for rule, (cond, exc, amt) in fixes.items():
        if rule in skip:
            continue
        stats[rule] = con.execute(f"""UPDATE A SET exception = {exc}, amount_cents = {amt}, {tag(rule)},
              evidence = coalesce(evidence, '') || '; PDF clause ({rule})'
            FROM X x WHERE A.bank_txn_id = x.bank_txn_id AND A.item_id = x.item_id AND ({cond})
              AND (coalesce(A.exception, '') IS DISTINCT FROM {exc} OR A.amount_cents IS DISTINCT FROM {amt})""").fetchone()[0]

    # late discount: line cash = Σ rows − Σ discount of the late-discount rows → those rows PARTIAL at total − discount
    if "late_exc" not in skip:
        con.execute(f"""CREATE OR REPLACE TABLE LD AS
          WITH r AS (
            SELECT a.bank_txn_id, a.item_id, a.amount_cents, i.kind, l.amount_cents AS cash, i.total_cents,
                   CASE WHEN i.kind = 'ARI' AND i.discount_pct > 0 AND coalesce(a.exception, '') IN ('', 'PARTIAL')
                         AND date_diff('day', i.issue_date, l.booking_date) > i.discount_days + 3
                         AND a.amount_cents = i.total_cents
                        THEN round(i.total_cents * i.discount_pct / 100)::BIGINT ELSE 0 END AS disc
            FROM A a JOIN I i USING (item_id) JOIN L l USING (bank_txn_id)
            WHERE l.direction = 'IN' AND i.currency = l.currency AND coalesce(a.exception, '') <> 'RETURNED')
          SELECT bank_txn_id, item_id, total_cents - disc AS paid FROM r
          WHERE disc > 0 AND bank_txn_id IN (
            SELECT bank_txn_id FROM r GROUP BY 1
            HAVING sum(disc) > 0 AND abs(any_value(cash) - (sum(CASE WHEN kind = 'ARC' THEN -amount_cents ELSE amount_cents END)
                                                           - sum(disc))) <= 1)""")
        stats["late_exc"] = con.execute(f"""UPDATE A SET exception = 'PARTIAL', amount_cents = LD.paid, {tag('late_exc')},
              evidence = coalesce(evidence, '') || '; discount taken after the deadline'
            FROM LD WHERE A.bank_txn_id = LD.bank_txn_id AND A.item_id = LD.item_id""").fetchone()[0]

    # ---- new rows for UNIDENTIFIED receipts ----------------------------------------------------------------------
    shares = _learn_shares(con, w)
    con.execute("CREATE OR REPLACE TABLE SH AS SELECT unnest(?::DOUBLE[]) AS r", [shares])
    for rnd in ("add", "add_rest"):
        _balances(con)
        # open balance of each invoice now (all current rows) and the date it last moved
        con.execute("""CREATE OR REPLACE TABLE OPEN AS
          SELECT i.item_id, i.party_id, i.currency, i.total_cents, i.issue_date, i.discount_pct, i.discount_days,
                 i.open_at_start_cents - coalesce(sum(CASE WHEN coalesce(b.exception, '') IN ('RETURNED', 'DUPLICATE_PAYMENT') THEN 0
                                                          ELSE b.amount_cents END), 0) AS open_now,
                 max(b.booking_date) AS last_date, count(b.item_id) AS n_rows
          FROM I i LEFT JOIN B b USING (item_id) WHERE i.kind = 'ARI'
          GROUP BY i.item_id, i.party_id, i.currency, i.total_cents, i.issue_date, i.discount_pct, i.discount_days,
                   i.open_at_start_cents""")
        con.execute("""CREATE OR REPLACE TABLE U AS
          SELECT l.bank_txn_id, l.booking_date, l.amount_cents AS cash, l.currency, lp.party_id, lp.rank
          FROM L l JOIN LP lp USING (bank_txn_id)
          WHERE l.direction = 'IN' AND l.bank_txn_id IN (
            SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0 AND bool_and(category = 'UNIDENTIFIED'))""")
        if rnd == "add":
            exp = """
              SELECT o.item_id, 'inst' AS kind, k AS k, f.amts[k] AS want,
                     CASE WHEN k < f.n THEN 'PARTIAL' ELSE '' END AS exc, f.days[k] AS due_off
              FROM OPEN o JOIN IF_ f USING (item_id), range(1, 13) t(k) WHERE f.n IS NOT NULL AND k <= f.n
              UNION ALL SELECT o.item_id, 'ret', 0, o.total_cents - f.ret_c, 'PARTIAL', NULL FROM OPEN o JOIN IF_ f USING (item_id)
                WHERE f.ret_c IS NOT NULL AND o.open_now = o.total_cents
              UNION ALL SELECT o.item_id, 'ret_release', 0, f.ret_c, '', NULL FROM OPEN o JOIN IF_ f USING (item_id)
                WHERE f.ret_c IS NOT NULL AND o.open_now = f.ret_c
              UNION ALL SELECT o.item_id, 'adv', 0, o.total_cents - f.adv_c, 'PARTIAL', NULL FROM OPEN o JOIN IF_ f USING (item_id)
                WHERE f.adv_c IS NOT NULL AND o.open_now = o.total_cents
              UNION ALL SELECT o.item_id, 'late', 0, o.total_cents - round(o.total_cents * o.discount_pct / 100)::BIGINT, 'PARTIAL', -1
                FROM OPEN o WHERE o.discount_pct > 0 AND o.open_now = o.total_cents
              UNION ALL SELECT o.item_id, 'share', 0, round(o.total_cents * SH.r)::BIGINT, 'PARTIAL', NULL
                FROM OPEN o, SH WHERE o.open_now = o.total_cents AND o.item_id NOT IN (SELECT item_id FROM IF_)"""
        else:
            exp = """SELECT o.item_id, 'rest' AS kind, 0 AS k, o.open_now AS want, '' AS exc, NULL AS due_off
                     FROM OPEN o WHERE o.open_now > 0 AND o.open_now < o.total_cents"""
        con.execute(f"""CREATE OR REPLACE TABLE C AS
          WITH e AS ({exp})
          SELECT u.bank_txn_id, u.booking_date, u.cash, u.rank, e.item_id, e.kind, e.k, e.exc, o.open_now, o.issue_date,
                 abs(date_diff('day', o.issue_date, u.booking_date) - coalesce(e.due_off, 0)) AS ddist
          FROM U u JOIN OPEN o ON o.party_id = u.party_id AND o.currency = u.currency
          JOIN e ON e.item_id = o.item_id AND abs(u.cash - e.want) <= 1
          WHERE u.booking_date >= o.issue_date AND (o.last_date IS NULL OR u.booking_date >= o.last_date)
            AND u.cash <= o.open_now
            AND (e.kind <> 'late' OR date_diff('day', o.issue_date, u.booking_date) > o.discount_days + 3)""")
        if rnd == "add":   # an instalment is paid around its own date; drop far-off matches of equal instalments
            con.execute("DELETE FROM C WHERE kind = 'inst' AND ddist > 25")
        # one candidate per line (best payer rank, then closest date); the line must not be ambiguous at that rank;
        # one line per (item, instalment)
        con.execute("""CREATE OR REPLACE TABLE P AS
          SELECT * FROM C
          QUALIFY count(*) OVER (PARTITION BY bank_txn_id, rank) = 1
              AND rank = min(rank) OVER (PARTITION BY bank_txn_id)""")
        con.execute("""CREATE OR REPLACE TABLE P AS SELECT * FROM P
          QUALIFY row_number() OVER (PARTITION BY item_id, k ORDER BY ddist, booking_date, bank_txn_id) = 1
              AND (kind = 'inst' OR count(*) OVER (PARTITION BY item_id) = 1)""")
        for kind, n in con.sql("SELECT kind, count(*) FROM P GROUP BY 1").fetchall():
            stats[f"{rnd}.{kind}" if rnd == "add" else rnd] = n
        if (rnd == "add_rest" and "add_rest" in skip):
            continue
        kinds_skip = [k.split(".", 1)[1] for k in skip if k.startswith("add.")]
        con.execute(f"""DELETE FROM P WHERE kind IN (SELECT unnest(?::VARCHAR[]))""", [kinds_skip])
        if rnd == "add" and "add" in skip:
            continue
        con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM P)")
        con.execute(f"""INSERT INTO A SELECT bank_txn_id, item_id, cash, NULL, exc, 'T8b.' || '{rnd}.' || kind,
              CASE WHEN rank = 1 THEN 0.7 ELSE 0.55 END,
              'UNIDENTIFIED receipt; cash = ' || kind || ' amount of open invoice of payer (rank ' || rank || ')'
            FROM P""")

    n1 = con.execute("SELECT count(*) FROM A").fetchone()[0]
    con.execute(f"COPY (SELECT {KEEP} FROM A) TO '{out}.tmp' (FORMAT parquet)")
    os.replace(f"{out}.tmp", out)
    stats["rows_before"], stats["rows_after"] = n0, n1
    stats["shares"] = "/".join(f"{r:g}" for r in shares)
    return stats


def _learn_shares(con, w: str) -> list[float]:
    """Usual first-payment shares of the total (PARTIAL on a single-invoice line), from TRAIN golden rows whose lines
    are not in this phase: shares round to the percent that cover >= 2 % of such rows."""
    g = os.path.join(data_root(), "train", "golden_allocations.csv.gz").replace(os.sep, "/")
    return _shares_from_items(con, g, w) if os.path.exists(g) else []


def _shares_from_items(con, g: str, w: str) -> list[float]:
    # invoice totals from this phase's items: invoices paid in two steps across the phase start are enough to learn
    rows = con.sql(f"""
      WITH g AS (SELECT bank_txn_id, item_id, coalesce(exception, '') exc, round(amount::DOUBLE * 100)::BIGINT amt
                 FROM read_csv('{g}', all_varchar = true)
                 WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM '{w}/lines.parquet') AND item_id LIKE 'ARI%'),
           s AS (SELECT bank_txn_id FROM g GROUP BY 1 HAVING count(*) = 1),
           p AS (SELECT round(g.amt / i.total_cents, 2) r, g.amt = round(i.total_cents * round(g.amt / i.total_cents, 2)) exact
                 FROM g JOIN s USING (bank_txn_id) JOIN I i USING (item_id) WHERE g.exc = 'PARTIAL'
                   AND coalesce(i.discount_pct, 0) = 0 AND i.item_id NOT IN (SELECT item_id FROM IF_))
      SELECT r FROM p WHERE exact AND r > 0 AND r < 1 GROUP BY r HAVING count(*) >= 0.02 * (SELECT count(*) FROM p)
      ORDER BY r""").fetchall()
    return [r for (r,) in rows]


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--work", help="phase work folder (default work/<phase>); for experiments on a snapshot")
    ap.add_argument("--skip", default="", help="comma-separated rule ids to leave out")
    a = ap.parse_args(argv)
    t0 = time.time()
    st = build(a.phase, {s for s in a.skip.split(",") if s}, a.work)
    print(f"[{a.phase}] T8b docrules: " + ", ".join(f"{k}={v}" for k, v in st.items()) + f" ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
