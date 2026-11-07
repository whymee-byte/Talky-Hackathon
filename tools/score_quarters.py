"""Stability check: official score of the window allocations quarter by quarter (2024-Q3 … 2025-Q2).

    python tools/score_quarters.py [work/window/allocations.parquet]

An over-fitted rule (tuned on one period, or fed by a leak) shows as a quarter clearly better than the others.
Prints SCORE, F1, accuracy and macro-F1 per quarter and the spread (max − min). Uses the official scorer
(data/starter/bankrec_score.py) on the golden and bank lines of each quarter.
"""
from __future__ import annotations

import csv
import io
import os
import sys

import duckdb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "data", "starter"))
from src.common import data_root, safe_stdout  # noqa: E402
import bankrec_score as bs  # noqa: E402

QUARTERS = [("2024-Q3", "2024-07-01", "2024-09-30"), ("2024-Q4", "2024-10-01", "2024-12-31"),
            ("2025-Q1", "2025-01-01", "2025-03-31"), ("2025-Q2", "2025-04-01", "2025-06-30")]
EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
       "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]
from src.common import phase_dir  # noqa: E402
_T = phase_dir("train").replace(os.sep, "/")
BT = f"read_csv('{_T}/bank_transactions.csv.gz', all_varchar=true)"
GOLD = f"read_csv('{_T}/golden_allocations.csv.gz', all_varchar=true)"


def score_range(alloc: str, name: str, lo: str, hi: str, cats: list[str]) -> dict:
    d = os.path.join(REPO, "work", "quarters", name).replace(os.sep, "/")
    os.makedirs(d, exist_ok=True)
    if not os.path.exists(f"{d}/golden.csv.gz"):
        con = duckdb.connect()
        con.execute(f"COPY (SELECT * FROM {BT} WHERE booking_date BETWEEN '{lo}' AND '{hi}') TO '{d}/bank_transactions.csv.gz'")
        con.execute(f"""COPY (SELECT g.* FROM {GOLD} g JOIN {BT} b USING (bank_txn_id)
                        WHERE b.booking_date BETWEEN '{lo}' AND '{hi}') TO '{d}/golden.csv.gz'""")
    g = bs.load_golden(f"{d}/golden.csv.gz", f"{d}/bank_transactions.csv.gz", EXC, cats)
    rows = duckdb.sql(f"""SELECT a.bank_txn_id, coalesce(a.item_id, ''),
            CASE WHEN a.item_id IS NULL THEN '' ELSE printf('%.2f', a.amount_cents / 100.0) END,
            CASE WHEN a.item_id IS NULL THEN coalesce(a.category, '') ELSE '' END, coalesce(a.exception, '')
        FROM '{alloc}' a JOIN {BT} b USING (bank_txn_id) WHERE b.booking_date BETWEEN '{lo}' AND '{hi}'""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
    w.writerows(rows)
    return bs.score_submission(g, buf.getvalue().encode(), f"{name}.csv")


def main() -> int:
    safe_stdout()
    os.chdir(REPO)
    alloc = (sys.argv[1] if len(sys.argv) > 1 else "work/window/allocations.parquet").replace(os.sep, "/")
    cats = [r["category"] for r in csv.DictReader(open(os.path.join(data_root(), "masters", "categories.csv"),
                                                        encoding="utf-8-sig", newline=""))]
    res = []
    print(f"{'quarter':8s} {'SCORE':>6s} {'F1':>6s} {'acc':>6s} {'macroF1':>8s}")
    for name, lo, hi in QUARTERS:
        r = score_range(alloc, name, lo, hi, cats)
        if not r["ok"]:
            print(name, "INVALID", r["errors"][:3])
            return 1
        m = r["metrics"]
        res.append(r["score"])
        print(f"{name:8s} {r['score']:6.2f} {100 * m['allocation_f1']:6.2f} {100 * m['txn_accuracy']:6.2f} "
              f"{100 * m['exception_macro_f1']:8.2f}")
    print(f"spread (max - min) = {max(res) - min(res):.2f} points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
