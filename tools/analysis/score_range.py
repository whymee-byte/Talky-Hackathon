"""Official scorer restricted to the window lines booked in [lo, hi] (honest out-of-sample checks).

    python tools/analysis/score_range.py work/window/allocations.parquet 2024-10-01 2025-06-30
"""
import csv
import io
import os
import sys

import duckdb

sys.path.insert(0, "data/starter")
import bankrec_score as bs  # noqa: E402

EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
       "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def score(alloc, lo, hi):
    d = f"work/range_{lo}_{hi}"
    os.makedirs(d, exist_ok=True)
    con = duckdb.connect()
    if not os.path.exists(f"{d}/golden.csv.gz"):
        con.execute(f"""COPY (SELECT * FROM 'data_cache/train/bank_transactions.parquet'
                        WHERE booking_date BETWEEN '{lo}' AND '{hi}') TO '{d}/bank_transactions.csv.gz'""")
        con.execute(f"""COPY (SELECT g.* FROM 'data_cache/train/golden_allocations.parquet' g
                        JOIN 'data_cache/train/bank_transactions.parquet' b USING (bank_txn_id)
                        WHERE b.booking_date BETWEEN '{lo}' AND '{hi}') TO '{d}/golden.csv.gz'""")
    cats = [r["category"] for r in csv.DictReader(open("data/bankrec-docs-and-masters/masters/categories.csv",
                                                       encoding="utf-8-sig"))]
    g = bs.load_golden(f"{d}/golden.csv.gz", f"{d}/bank_transactions.csv.gz", EXC, cats)
    rows = con.sql(f"""SELECT a.bank_txn_id, coalesce(a.item_id, ''),
            CASE WHEN a.item_id IS NULL THEN '' ELSE printf('%.2f', a.amount_cents / 100.0) END,
            CASE WHEN a.item_id IS NULL THEN coalesce(a.category, '') ELSE '' END, coalesce(a.exception, '')
        FROM '{alloc}' a JOIN 'data_cache/train/bank_transactions.parquet' b USING (bank_txn_id)
        WHERE b.booking_date BETWEEN '{lo}' AND '{hi}'""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
    w.writerows(rows)
    r = bs.score_submission(g, buf.getvalue().encode(), "range.csv")
    return r


if __name__ == "__main__":
    r = score(*sys.argv[1:4])
    m = r["metrics"]
    print(f"SCORE {r['score']}  F1={m['allocation_f1']:.4f} acc={m['txn_accuracy']:.4f} macro={m['exception_macro_f1']:.4f}")
