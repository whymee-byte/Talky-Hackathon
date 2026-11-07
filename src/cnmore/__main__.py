"""T19 · more credit notes netted in receipts (ar_1toN_credit_note), after src.cnfx and before src.excfix.

    python -m src.cnmore --phase window|live|final [--alloc work/<fase>/allocations.parquet] [--diag path]

Rewrites work/<phase>/allocations.parquet. Exact cents only, unique solutions only, no ids / texts / amounts in code.
What src.cnlink and src.cnfx leave behind (window diagnosis, 2376 wrong lines of the segment):
  * receipts with no item whose text names the invoices (exact number, digits, or a >= 6-digit suffix) but whose cash
    is short of their open balance by one or two credit notes the text does not name (the matcher refused the amount);
  * receipts whose AR rows are short of the cash by credit notes that have no `related_doc`, or whose related
    invoice is not on the line, or several per invoice (cnlink only nets credit notes related to an invoice of the line).

  T19.ref_cn   IN line with no item; tokens of the text resolve to open, unused AR invoices of ONE party (same currency,
               issued <= booking date; the party is a T4 candidate or has >= 2 referenced invoices; each token maps to
               exactly one invoice of that party); diff = sum open(invoices) - cash > 0 = open balance of exactly one
               subset (1-2 notes, or all notes related to the invoices) of the party's open unused credit notes.
               Every invoice and credit note at its open balance, no exception.
  T19.net_cn   IN line whose rows are all AR items of one party (same currency, no RETURNED / DUPLICATE / SHORT_* /
               OVERPAYMENT), short of the cash by diff = sum open(invoices) - sum open(credit notes already there) -
               cash > 0 = the open balance of exactly one subset (1-2) of the party's other open unused credit notes.
               Existing rows go to their open balance with no exception (a PARTIAL that only reflected the netting).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO

SUF_MIN = 6        # digits of a token used as suffix of our number
CONF = {"ref_cn": 0.8, "net_cn": 0.75}


def build(phase: str, alloc: str | None = None, diag: str | None = None) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    p = (alloc or f"{w}/allocations.parquet").replace(os.sep, "/")
    con = duckdb.connect()
    con.execute(f"SET threads=8; SET memory_limit='8GB'; SET temp_directory='{REPO}/work/tmp'")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE I AS SELECT item_id, kind, party_id, currency, doc_number, replace(doc_norm, ' ', '') doc_norm,
                      doc_digits, related_doc, issue_date, open_at_start_cents open_c
                    FROM '{w}/items.parquet' WHERE kind IN ('ARI', 'ARC') AND open_at_start_cents > 0""")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute(f"""CREATE TABLE L AS SELECT bank_txn_id, abs(amount_cents) cash, currency, booking_date bd, doc_tokens
                    FROM '{w}/lines.parquet' WHERE direction = 'IN'""")
    lp = f"{w}/line_party.parquet"
    con.execute(f"CREATE TABLE LP AS SELECT DISTINCT bank_txn_id, party_id FROM '{lp}'" if os.path.exists(lp) else
                "CREATE TABLE LP (bank_txn_id VARCHAR, party_id VARCHAR)")
    con.execute("CREATE TABLE FREE AS SELECT * FROM I WHERE item_id NOT IN (SELECT item_id FROM USED)")

    # ---------------------------------------------------------------- T19.ref_cn: no-item lines with references
    con.execute("""CREATE TABLE NI AS SELECT l.* FROM L l
                   WHERE l.bank_txn_id IN (SELECT bank_txn_id FROM A GROUP BY 1 HAVING count(item_id) = 0)
                     AND len(coalesce(l.doc_tokens, [])) > 0""")
    con.execute(f"""CREATE TABLE TK AS
      SELECT DISTINCT bank_txn_id, tk, regexp_replace(tk, '[^0-9]', '', 'g') dig
      FROM (SELECT bank_txn_id, upper(unnest(doc_tokens)) tk FROM NI) WHERE length(tk) >= 5""")
    con.execute(f"""CREATE TABLE RM AS
      SELECT t.bank_txn_id, t.tk, i.item_id, i.party_id, i.open_c,
             CASE WHEN i.doc_norm = t.tk THEN 'exact' WHEN i.doc_digits = t.dig THEN 'dig' ELSE 'suf' END how
      FROM TK t JOIN NI n USING (bank_txn_id)
      JOIN FREE i ON i.kind = 'ARI' AND i.currency = n.currency AND i.issue_date <= n.bd
           AND (i.doc_norm = t.tk OR (length(t.dig) >= 5 AND i.doc_digits = t.dig)
                OR (length(t.dig) >= {SUF_MIN} AND length(i.doc_digits) > length(t.dig) AND ends_with(i.doc_digits, t.dig)))""")
    # one party per line: the one with most referenced invoices, strictly ahead of the runner-up (twice as many, or a
    # T4 candidate); a suffix token also hits other customers' numbers, so collisions are the rule
    con.execute("""CREATE TABLE RP AS
      SELECT bank_txn_id, party_id, count(DISTINCT item_id) n, count(DISTINCT tk) ntk FROM RM
      GROUP BY 1, 2
      QUALIFY row_number() OVER (PARTITION BY bank_txn_id ORDER BY n DESC, ntk DESC) = 1
         AND n > coalesce(lead(n) OVER (PARTITION BY bank_txn_id ORDER BY n DESC, ntk DESC), 0)
         AND (n >= 2 * coalesce(lead(n) OVER (PARTITION BY bank_txn_id ORDER BY n DESC, ntk DESC), 0)
              OR (bank_txn_id, party_id) IN (SELECT bank_txn_id, party_id FROM LP))
         AND (n >= 2 OR (bank_txn_id, party_id) IN (SELECT bank_txn_id, party_id FROM LP))""")
    con.execute("""CREATE TABLE RI AS
      SELECT DISTINCT rm.bank_txn_id, rm.party_id, rm.item_id, rm.open_c FROM RM rm JOIN RP USING (bank_txn_id, party_id)
      WHERE rm.bank_txn_id NOT IN (SELECT bank_txn_id FROM RM r2 JOIN RP USING (bank_txn_id, party_id)
                                   GROUP BY r2.bank_txn_id, r2.tk HAVING count(DISTINCT r2.item_id) > 1)""")
    con.execute("""CREATE TABLE RD AS
      SELECT ri.bank_txn_id, any_value(ri.party_id) party_id, count(*) n, sum(ri.open_c) - any_value(n.cash) diff,
             list(i.doc_number) docs
      FROM RI ri JOIN NI n USING (bank_txn_id) JOIN I i USING (item_id) GROUP BY 1 HAVING sum(ri.open_c) - any_value(n.cash) > 0""")
    ref = _cn_subsets(con, "RD", "RI", "NI")
    con.execute(f"""CREATE TABLE NEW AS
      SELECT ri.bank_txn_id, ri.item_id, ri.open_c amount_cents, NULL::VARCHAR category, NULL::VARCHAR exception,
             'T19.ref_cn' rule, {CONF['ref_cn']} confidence,
             'text names ' || rd.n || ' invoices of the party; cash short by ' || rd.diff || ' = ' || s.how evidence
      FROM RI ri JOIN RD rd USING (bank_txn_id) JOIN SOL s USING (bank_txn_id)
      UNION ALL
      SELECT s.bank_txn_id, x.item_id, x.open_c, NULL, NULL, 'T19.ref_cn', {CONF['ref_cn']},
             'credit note netted: text names ' || rd.n || ' invoices; cash short by ' || rd.diff || ' = ' || s.how
      FROM SOL s JOIN RD rd USING (bank_txn_id), unnest(s.items) u(item_id) JOIN I x ON x.item_id = u.item_id""")
    n_ref = con.execute("SELECT count(*), count(DISTINCT bank_txn_id) FROM NEW").fetchall()[0]
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM NEW)")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW")
    con.execute("DROP TABLE SOL")

    # ---------------------------------------------------------------- T19.net_cn: lines with AR items short by credit notes
    con.execute("DROP TABLE USED; DROP TABLE FREE")
    con.execute("""CREATE TABLE USED AS SELECT item_id FROM A WHERE item_id IS NOT NULL GROUP BY 1
                   HAVING sum(CASE WHEN exception = 'RETURNED' THEN -1 ELSE 1 END) > 0""")
    con.execute("CREATE TABLE FREE AS SELECT * FROM I WHERE item_id NOT IN (SELECT item_id FROM USED)")
    con.execute("""CREATE TABLE LI AS
      SELECT a.bank_txn_id, any_value(i.party_id) party_id, count(*) n,
             sum(CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END) - any_value(l.cash) diff, list(i.doc_number) docs
      FROM A a JOIN L l USING (bank_txn_id) JOIN I i USING (item_id)
      WHERE i.currency = l.currency
      GROUP BY a.bank_txn_id
      HAVING count(*) = (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id)
         AND count(DISTINCT i.party_id) = 1 AND count(DISTINCT a.item_id) = count(*)
         AND bool_and(coalesce(a.exception, '') IN ('', 'PARTIAL'))
         AND bool_and(coalesce(a.rule, '') NOT LIKE 'T19.%')
         AND sum(CASE WHEN i.kind = 'ARC' THEN -i.open_c ELSE i.open_c END) - any_value(l.cash) > 0""")
    con.execute("CREATE TABLE LII AS SELECT a.bank_txn_id, a.item_id FROM A a JOIN LI USING (bank_txn_id)")
    net = _cn_subsets(con, "LI", "LII", "L")
    con.execute(f"""CREATE TABLE NEW2 AS
      SELECT s.bank_txn_id, x.item_id, x.open_c amount_cents, NULL::VARCHAR category, NULL::VARCHAR exception,
             'T19.net_cn' rule, {CONF['net_cn']} confidence,
             'credit note netted: line items of the party short of the cash by ' || li.diff || ' = ' || s.how evidence
      FROM SOL s JOIN LI li USING (bank_txn_id), unnest(s.items) u(item_id) JOIN I x ON x.item_id = u.item_id""")
    con.execute("""UPDATE A SET amount_cents = i.open_c, exception = NULL,
                     rule = CASE WHEN coalesce(A.rule, '') = '' THEN 'T19.net_cn' ELSE A.rule || '+T19.net_cn' END,
                     evidence = coalesce(A.evidence, '') || '; T19 credit note netted, open balance'
                   FROM I i WHERE A.item_id = i.item_id AND A.bank_txn_id IN (SELECT bank_txn_id FROM NEW2)""")
    con.execute("INSERT INTO A BY NAME SELECT * FROM NEW2")
    n_net = con.execute("SELECT count(*), count(DISTINCT bank_txn_id) FROM NEW2").fetchall()[0]
    if diag:
        con.execute(f"COPY (SELECT * FROM NEW UNION ALL SELECT * FROM NEW2) TO '{diag}' (FORMAT parquet)")
    con.execute(f"COPY A TO '{p}.tmp' (FORMAT parquet)")
    os.replace(f"{p}.tmp", p)
    print(f"[{phase}] cnmore: ref_cn {n_ref[0]} rows on {n_ref[1]} lines ({ref}) · net_cn {n_net[0]} credit notes on "
          f"{n_net[1]} lines ({net}) ({time.time() - t0:.0f}s)")


def _cn_subsets(con, lines: str, on_line: str, lt: str) -> list:
    """SOL(bank_txn_id, items, how): the unique subset of the party's free credit notes (not on the line, same currency,
    issued <= booking date) whose open balance is `diff` (+-1 cent): one note, two notes, or all the notes related to
    the invoices of the line."""
    con.execute(f"""CREATE OR REPLACE TABLE CN AS
      SELECT d.bank_txn_id, c.item_id, c.open_c, list_contains(d.docs, c.related_doc) rel
      FROM {lines} d JOIN {lt} l USING (bank_txn_id)
      JOIN FREE c ON c.kind = 'ARC' AND c.party_id = d.party_id AND c.currency = l.currency AND c.issue_date <= l.bd
      WHERE c.item_id NOT IN (SELECT item_id FROM {on_line} o WHERE o.bank_txn_id = d.bank_txn_id)""")
    con.execute(f"""CREATE OR REPLACE TABLE SOL AS
      WITH s1 AS (SELECT c.bank_txn_id, [c.item_id] items, 'one credit note' how FROM CN c JOIN {lines} d USING (bank_txn_id)
                  WHERE abs(c.open_c - d.diff) <= 1),
           s2 AS (SELECT a.bank_txn_id, [a.item_id, b.item_id], 'two credit notes' FROM CN a JOIN CN b USING (bank_txn_id)
                  JOIN {lines} d USING (bank_txn_id) WHERE a.item_id < b.item_id AND abs(a.open_c + b.open_c - d.diff) <= 1),
           s3 AS (SELECT c.bank_txn_id, list(c.item_id), 'all related credit notes' FROM CN c JOIN {lines} d USING (bank_txn_id)
                  WHERE c.rel GROUP BY c.bank_txn_id HAVING count(*) >= 3 AND abs(sum(c.open_c) - any_value(d.diff)) <= count(*)),
           u AS (SELECT * FROM s1 UNION ALL SELECT * FROM s2 UNION ALL SELECT * FROM s3)
      SELECT * FROM u QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1""")
    # a credit note wanted by two lines goes to neither
    con.execute("""DELETE FROM SOL WHERE bank_txn_id IN (
      SELECT s.bank_txn_id FROM SOL s, unnest(s.items) u(item_id)
      WHERE u.item_id IN (SELECT u2.item_id FROM SOL s2, unnest(s2.items) u2(item_id) GROUP BY 1 HAVING count(*) > 1))""")
    return con.execute("SELECT how, count(*) FROM SOL GROUP BY 1 ORDER BY 1").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--alloc")
    ap.add_argument("--diag", help="write the added rows to this parquet (audit)")
    a = ap.parse_args()
    build(a.phase, a.alloc, a.diag)


if __name__ == "__main__":
    main()
