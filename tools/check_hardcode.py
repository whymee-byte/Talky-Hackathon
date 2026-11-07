"""Anti-hardcode check: no string literal in src/ may equal a concrete value of the data.

    python tools/check_hardcode.py [--phases train,live]

Collects every string literal of src/**/*.py (ast) and the concrete values of the phases' data: bank_txn_id,
item / document numbers, mandates, IBANs, batch / run / deposit / settlement ids, customer and supplier names.
Any literal (>= 5 chars) equal to one of those values — or any literal that contains one of the ids — is reported.
Exit 1 if something is found. Patterns, thresholds and column names never match: they are not data values.
"""
from __future__ import annotations

import argparse
import ast
import glob
import os
import sys

import duckdb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from src.common import data_root, phase_dir, safe_stdout  # noqa: E402

# (file, columns) of concrete values; files are globbed under the phase folder
SOURCES = [
    ("bank_transactions.csv.gz", ["bank_txn_id"]),
    ("ar_invoices.csv.gz", ["item_id", "document_number", "customer_po"]),
    ("ap_invoices.csv.gz", ["item_id", "supplier_invoice_number"]),
    ("credit_notes.csv.gz", ["item_id", "document_number"]),
    ("direct_debit_batches/[!.]*.csv", ["batch_id", "mandate_id", "end_to_end_id"]),
    ("payment_runs/[!.]*.csv", ["run_id", "end_to_end_id", "document_number"]),
    ("card_settlements/[!.]*.csv", ["settlement_id", "order_number"]),
    ("cheque_deposits/[!.]*.csv", ["deposit_id", "cheque_number"]),
]
CSV = "quote='\"', escape='\"'"   # masters have quoted names with commas
MASTERS = [("customers.csv", ["customer_id", "name", "trade_name", "iban", "sepa_mandate_id"]),
           ("suppliers.csv", ["supplier_id", "name", "iban"])]


def literals() -> list[tuple[str, int, str, str]]:
    """(file, line, literal, kind); kind = 'doc' for module/class/function docstrings, 'code' otherwise."""
    out = []
    for path in glob.glob(os.path.join(REPO, "src", "**", "*.py"), recursive=True):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body                     and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                docs.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value.strip()) >= 5:
                out.append((os.path.relpath(path, REPO), node.lineno, node.value.strip(), "doc" if id(node) in docs else "code"))
    return out


def values(con, phases: list[str]) -> None:
    con.execute("CREATE TABLE V (v VARCHAR, src VARCHAR)")
    def add(path: str, cols: list[str], tag: str) -> None:
        files = [p.replace(os.sep, "/") for p in glob.glob(path) if not os.path.basename(p).startswith("._")]
        if not files:
            return
        have = {r[0] for r in con.sql(f"DESCRIBE SELECT * FROM read_csv({files}, all_varchar=true, union_by_name=true, {CSV})").fetchall()}
        for c in cols:
            if c in have:
                con.execute(f"""INSERT INTO V SELECT DISTINCT trim("{c}"), '{tag}.{c}'
                                FROM read_csv({files}, all_varchar=true, union_by_name=true, {CSV}) WHERE length(trim("{c}")) >= 5""")
    for ph in phases:
        base = phase_dir(ph)
        if os.path.isdir(base):
            for f, cols in SOURCES:
                add(os.path.join(base, f), cols, f"{ph}/{f.split('/')[0]}")
    for f, cols in MASTERS:
        add(os.path.join(data_root(), "masters", f), cols, f"masters/{f}")


def main() -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phases", default="train,live")
    a = ap.parse_args()
    con = duckdb.connect()
    values(con, a.phases.split(","))
    lits = literals()
    con.execute("CREATE TABLE L (file VARCHAR, line INT, lit VARCHAR, kind VARCHAR)")
    con.executemany("INSERT INTO L VALUES (?, ?, ?, ?)", lits)
    nv = con.sql("SELECT count(*) FROM V").fetchall()[0][0]
    # exact equality (any value), or an id-like value (has a digit, >= 6 chars) contained in a literal
    hits = con.sql("""SELECT DISTINCT l.file, l.line, left(l.lit, 80), v.v, v.src, l.kind FROM L l JOIN V v ON l.lit = v.v
                      UNION
                      SELECT DISTINCT l.file, l.line, left(l.lit, 80), v.v, v.src, l.kind FROM L l JOIN
                        (SELECT * FROM V WHERE length(v) >= 6 AND regexp_matches(v, '[0-9]')) v ON contains(l.lit, v.v)
                      ORDER BY 1, 2""").fetchall()
    print(f"{len(lits)} string literals in src/ checked against {nv} data values ({a.phases} + masters)")
    code = [h for h in hits if h[5] == "code"]
    for h in hits:
        tag = "HARDCODE?" if h[5] == "code" else "doc example"
        print(f"  {tag:11s} {h[0]}:{h[1]}  value={h[3]!r} ({h[4]})")
    print("OK: no data value in the code" if not code else f"FAIL: {len(code)} data value(s) in code literals",
          f"({len(hits) - len(code)} in docstrings, informative)" if len(hits) > len(code) else "")
    return 1 if code else 0


if __name__ == "__main__":
    sys.exit(main())
