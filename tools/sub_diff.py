"""Diff two submissions line by line and, optionally, assemble "base + selected changes" as a third file.

    python tools/sub_diff.py out/final_A.csv.gz out/final_B.csv.gz                       # report only
    python tools/sub_diff.py A.csv.gz B.csv.gz --alloc work/final/allocations.parquet     # + rules behind B's rows
    python tools/sub_diff.py A.csv.gz B.csv.gz --alloc … --apply-rules T15.over_single,T3g.overfix --out out/C.csv.gz
    python tools/sub_diff.py A.csv.gz B.csv.gz --apply-txns ids.txt --out out/C.csv.gz

The merged file takes, for every bank_txn_id, B's rows if the line is selected (its rows differ from A and at least one of
B's rows for that line carries a selected rule, or the id is in --apply-txns) and A's rows otherwise. Rows are copied
verbatim (same serialisation as the inputs). Validate the result with tools/validate.py before using it.
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

COLS = "bank_txn_id, item_id, amount, category, exception"


def csvq(path: str) -> str:
    return f"read_csv('{path.replace(os.sep, '/')}', all_varchar=true, header=true, quote='\"', escape='\"', delim=',')"


def table(con, sql: str, limit: int = 40) -> str:
    r = con.execute(sql)
    cols = [d[0] for d in r.description]
    rows = r.fetchall()[:limit]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in row) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("new")
    ap.add_argument("--alloc", help="allocations.parquet behind `new` (gives the rule of each row)")
    ap.add_argument("--apply-rules", help="comma-separated rule ids (substring match) whose lines are taken from `new`")
    ap.add_argument("--apply-txns", help="file with one bank_txn_id per line to take from `new`")
    ap.add_argument("--out", help="write the merged submission here (csv.gz)")
    a = ap.parse_args()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT {COLS} FROM {csvq(a.base)}")
    con.execute(f"CREATE TABLE B AS SELECT {COLS} FROM {csvq(a.new)}")
    # one row per line: the sorted multiset of its rows
    for t in ("A", "B"):
        con.execute(f"""CREATE TABLE {t}L AS
            SELECT bank_txn_id, list_sort(list(concat_ws('|', coalesce(item_id, ''), coalesce(amount, ''),
                                                   coalesce(category, ''), coalesce(exception, '')))) AS rows,
                   count(item_id) FILTER (WHERE coalesce(item_id, '') <> '') AS n_pairs
            FROM {t} GROUP BY 1""")
    con.execute("""CREATE TABLE D AS
        SELECT coalesce(a.bank_txn_id, b.bank_txn_id) AS bank_txn_id, a.rows AS a_rows, b.rows AS b_rows,
               coalesce(a.n_pairs, 0) AS a_pairs, coalesce(b.n_pairs, 0) AS b_pairs,
               a.bank_txn_id IS NULL AS only_in_new, b.bank_txn_id IS NULL AS only_in_base
        FROM AL a FULL JOIN BL b USING (bank_txn_id)
        WHERE a.rows IS DISTINCT FROM b.rows""")
    tot_a, tot_b = con.execute("SELECT (SELECT count(*) FROM AL), (SELECT count(*) FROM BL)").fetchone()
    n_diff = con.execute("SELECT count(*) FROM D").fetchone()[0]
    print(f"lines: base {tot_a:,} · new {tot_b:,} · differing {n_diff:,} ({100.0 * n_diff / max(1, tot_a):.2f} %)")
    print(table(con, """
        SELECT CASE WHEN only_in_new THEN 'only in new' WHEN only_in_base THEN 'only in base'
                    WHEN a_pairs = 0 AND b_pairs > 0 THEN 'category -> pairs'
                    WHEN a_pairs > 0 AND b_pairs = 0 THEN 'pairs -> category'
                    WHEN a_pairs = 0 AND b_pairs = 0 THEN 'category/exception changed'
                    WHEN a_pairs <> b_pairs THEN 'pair count changed'
                    ELSE 'same pairs, amount/exception changed' END AS kind,
               count(*) AS lines, sum(b_pairs - a_pairs) AS pairs_delta
        FROM D GROUP BY 1 ORDER BY 2 DESC"""))
    print("\nexception rows per class, base vs new:")
    print(table(con, """
        SELECT coalesce(nullif(e, ''), '(none)') AS exception, sum(a) AS base_rows, sum(b) AS new_rows, sum(b) - sum(a) AS delta
        FROM (SELECT exception e, 1 a, 0 b FROM A UNION ALL SELECT exception, 0, 1 FROM B) GROUP BY 1 ORDER BY 2 DESC"""))
    rules = None
    if a.alloc:
        con.execute(f"CREATE TABLE R AS SELECT bank_txn_id, item_id, coalesce(rule, '') AS rule FROM read_parquet('{a.alloc.replace(os.sep, '/')}')")
        con.execute("""CREATE TABLE DR AS
            SELECT d.bank_txn_id, string_agg(DISTINCT r.rule, ' ; ') AS rules
            FROM D d JOIN R r USING (bank_txn_id) GROUP BY 1""")
        print("\nrules behind the differing lines (new side), top 40:")
        print(table(con, """
            SELECT rule, count(*) AS lines FROM (SELECT DISTINCT d.bank_txn_id, r.rule FROM D d JOIN R r USING (bank_txn_id))
            GROUP BY 1 ORDER BY 2 DESC"""))
        if a.apply_rules:
            rules = [x.strip() for x in a.apply_rules.split(",") if x.strip()]
    if a.out:
        sel = ["FALSE"]
        if rules:
            cond = " OR ".join(f"r.rule LIKE '%{x.replace(chr(39), chr(39) * 2)}%'" for x in rules)
            sel.append(f"bank_txn_id IN (SELECT DISTINCT r.bank_txn_id FROM R r WHERE {cond})")
        if a.apply_txns:
            ids = [x.strip() for x in open(a.apply_txns, encoding="utf-8") if x.strip()]
            con.execute("CREATE TABLE T AS SELECT unnest(?::VARCHAR[]) AS bank_txn_id", [ids])
            sel.append("bank_txn_id IN (SELECT bank_txn_id FROM T)")
        con.execute(f"CREATE TABLE SEL AS SELECT bank_txn_id FROM D WHERE {' OR '.join(sel)}")
        n_sel = con.execute("SELECT count(*) FROM SEL").fetchone()[0]
        out = a.out.replace(os.sep, "/")
        con.execute(f"""COPY (
            SELECT {COLS} FROM A WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM SEL)
            UNION ALL
            SELECT {COLS} FROM B WHERE bank_txn_id IN (SELECT bank_txn_id FROM SEL)
            ORDER BY bank_txn_id, item_id
        ) TO '{out}' (HEADER, DELIMITER ',', COMPRESSION gzip)""")
        n_rows = con.execute(f"SELECT count(*) FROM {csvq(out)}").fetchone()[0]
        print(f"\nmerged: {n_sel:,} lines taken from new, rest from base -> {out} ({n_rows:,} rows). "
              f"Validate: python tools/validate.py {out} --phase final")
    return 0


if __name__ == "__main__":
    sys.exit(main())
