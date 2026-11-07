"""Fast dev loop: score allocations on the first quarter of the window (2024-07-01 → 2024-09-30) with the official scorer.

    python -m src.match.q1 work/window/allocations_q1.parquet
"""
import csv
import io
import os
import sys

import duckdb

from src.common import data_root

sys.path.insert(0, "data/starter")
import bankrec_score as bs  # noqa: E402

Q1 = ("2024-07-01", "2024-09-30")
D = "work/q1"
EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
       "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def ensure():
    os.makedirs(D, exist_ok=True)
    if not os.path.exists(f"{D}/golden.csv.gz"):
        con = duckdb.connect()
        con.execute(f"""COPY (SELECT * FROM 'data_cache/train/bank_transactions.parquet'
                        WHERE booking_date BETWEEN '{Q1[0]}' AND '{Q1[1]}') TO '{D}/bank_transactions.csv.gz'""")
        con.execute(f"""COPY (SELECT g.* FROM 'data_cache/train/golden_allocations.parquet' g
                        JOIN 'data_cache/train/bank_transactions.parquet' b USING (bank_txn_id)
                        WHERE b.booking_date BETWEEN '{Q1[0]}' AND '{Q1[1]}') TO '{D}/golden.csv.gz'""")


def score(alloc, show=True):
    ensure()
    cats_path = os.path.join(data_root(), "masters", "categories.csv")
    cats = [r["category"] for r in csv.DictReader(open(cats_path, encoding="utf-8-sig", newline=""))]
    g = bs.load_golden(f"{D}/golden.csv.gz", f"{D}/bank_transactions.csv.gz", EXC, cats)
    rows = duckdb.sql(f"""SELECT a.bank_txn_id, coalesce(a.item_id, ''),
            CASE WHEN a.item_id IS NULL THEN '' ELSE printf('%.2f', a.amount_cents / 100.0) END,
            CASE WHEN a.item_id IS NULL THEN coalesce(a.category, '') ELSE '' END, coalesce(a.exception, '')
        FROM '{alloc}' a JOIN 'data_cache/train/bank_transactions.parquet' b USING (bank_txn_id)
        WHERE b.booking_date BETWEEN '{Q1[0]}' AND '{Q1[1]}'""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
    w.writerows(rows)
    r = bs.score_submission(g, buf.getvalue().encode(), "q1.csv")
    if show:
        if not r["ok"]:
            print("INVALID", r["errors"][:5])
            return r
        m = r["metrics"]
        print(f"Q1 SCORE {r['score']}  " + "  ".join(f"{k}={v}" for k, v in m.items() if not isinstance(v, (dict, list))))
        seg = sorted(r["segments"].items(), key=lambda kv: -kv[1]["n"] * (1 - kv[1]["accuracy"]))[:15]
        print("peores segmentos (líneas perdidas):", ", ".join(f"{k} {v['accuracy']:.2f}/{v['n']}" for k, v in seg))
    return r


if __name__ == "__main__":
    score(sys.argv[1])
