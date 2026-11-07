"""T23.model · learned pair model for receipts still without item (after the exact-sum stages).

    python -m src.agecomb.model learn      # fit the weights on the window (TRAIN 2024-07 -> 2025-06) -> weights.npy
    (used by python -m src.agecomb --phase ... when weights.npy exists)

A logistic model over (receipt, open invoice of the T4 party) features that need no reference in the text: age of the
invoice at the booking date, days past due, "skips" (later invoices of the party already settled by an earlier line),
terms, amount vs cash, purchase-order grouping, duplicates of the amount, position among the party's open invoices.
Nothing hard-coded: the weights are learned from the TRAIN golden (honest check: fit on 2024-07..09, measured on
2024-10..2025-06: AUC 0.91; pairs with p >= 0.6 are right 71 % of the time, above the F1 break-even of ~0.47).
Pairs with p >= P_MIN are emitted with confidence = p.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(__file__)
WEIGHTS = os.path.join(HERE, "weights.npy")
P_MIN = float(os.environ.get("AC_MODEL_PMIN", 0.6))
AGE_BINS = [(0, 30), (30, 60), (60, 90), (90, 120), (120, 180), (180, 250), (250, 400)]
SKIP_BINS = [(0, 1), (1, 5), (5, 15), (15, 40), (40, 100), (100, 1e9)]

FEATURE_SQL = """
 WITH live AS (
   SELECT l.bank_txn_id, l.bd, l.cash, l.party_id, l.pc, i.item_id, i.v, i.issue_date, i.due_date, i.po_norm, i.total_cents
   FROM L l JOIN IT i ON i.party_id = l.party_id AND i.currency = l.currency AND i.issue_date <= l.bd AND l.bd - i.issue_date <= 400
   LEFT JOIN USED u ON u.item_id = i.item_id WHERE (u.item_id IS NULL OR u.first_bd >= l.bd)
     AND l.bank_txn_id NOT IN (SELECT bank_txn_id FROM NEW)),
 sk AS (SELECT c.bank_txn_id, c.item_id, count(s.issue_date) skips FROM live c
        LEFT JOIN SETTLED s ON s.party_id = c.party_id AND s.issue_date > c.issue_date AND s.first_bd < c.bd AND s.first_bd > c.bd - 400
        GROUP BY 1, 2),
 po AS (SELECT bank_txn_id, po_norm, count(*) po_n, sum(v) po_v FROM live WHERE po_norm IS NOT NULL GROUP BY 1, 2),
 stat AS (SELECT bank_txn_id, count(*) n_cand, max(issue_date) newest, sum(v) tot_v FROM live GROUP BY 1),
 dup AS (SELECT bank_txn_id, v, count(*) n_same FROM live GROUP BY 1, 2)
 SELECT c.bank_txn_id, c.item_id, c.v, c.cash, c.bd - c.issue_date age, c.bd - c.due_date delay, sk.skips,
   c.due_date - c.issue_date terms, st.newest - c.issue_date newest_gap, st.n_cand, st.tot_v,
   coalesce(po.po_n, 0) po_n, coalesce(po.po_v, 0) po_v, d.n_same, (c.v = c.total_cents)::int is_full,
   row_number() OVER (PARTITION BY c.bank_txn_id ORDER BY c.issue_date DESC, c.item_id) age_rank, c.pc
 FROM live c JOIN sk USING (bank_txn_id, item_id) JOIN stat st USING (bank_txn_id)
 JOIN dup d ON d.bank_txn_id = c.bank_txn_id AND d.v = c.v
 LEFT JOIN po ON po.bank_txn_id = c.bank_txn_id AND po.po_norm = c.po_norm
"""


def features(d):
    age = d["age"].astype(float)
    sk = d["skips"].astype(float)
    dl = np.clip(d["delay"].astype(float), -120, 400)
    v = d["v"].astype(float)
    cash = d["cash"].astype(float)
    cols = [np.log1p(age), np.log1p(age) ** 2, np.log1p(sk), np.log1p(sk) ** 2, dl / 100, (dl / 100) ** 2,
            (dl < 0).astype(float), (dl < -7).astype(float), d["terms"].astype(float) / 30,
            np.log1p(d["newest_gap"].astype(float)), np.log(d["n_cand"].astype(float)),
            np.log(v / cash), np.log(v / cash) ** 2, (v > cash).astype(float), np.log(d["tot_v"].astype(float) / cash),
            (d["tot_v"] < cash).astype(float), (d["po_n"] > 1).astype(float), np.log1p(d["po_v"].astype(float) / cash),
            np.log(d["n_same"].astype(float)), d["is_full"].astype(float), np.log1p(d["age_rank"].astype(float)),
            d["pc"].astype(float)]
    for lo, hi in AGE_BINS:
        cols.append(((age >= lo) & (age < hi)).astype(float))
    for lo, hi in SKIP_BINS:
        cols.append(((sk >= lo) & (sk < hi)).astype(float))
    return np.column_stack(cols)


def predict(d, params):
    X = features(d)
    k = X.shape[1]
    w, mu, sd = params[:k + 1], params[k + 1:2 * k + 1], params[2 * k + 1:]
    Xb = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    return 1 / (1 + np.exp(-Xb @ w))


def fit(X, y, iters=1500, lr=0.5, lam=1e-4):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xb = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        w -= lr * (Xb.T @ (p - y) / len(y) + lam * w)
    return np.concatenate([w, mu, sd])


def learn(con):
    """Fit on the window's candidates (needs the window pipeline tables L, IT, USED, SETTLED, NEW in `con`)."""
    con.execute("CREATE OR REPLACE TABLE G AS SELECT bank_txn_id, item_id FROM 'data_cache/train/golden_allocations.parquet' WHERE item_id IS NOT NULL")
    before = os.environ.get("AC_FIT_BEFORE")           # honest check: fit only on lines booked before this date
    cut = f"WHERE f.bank_txn_id IN (SELECT bank_txn_id FROM L WHERE bd < DATE '{before}')" if before else ""
    d = con.execute(f"SELECT f.*, (g.item_id IS NOT NULL)::int gold FROM ({FEATURE_SQL}) f "
                    f"LEFT JOIN G g USING (bank_txn_id, item_id) {cut}").fetchnumpy()
    params = fit(features(d), d["gold"].astype(float))
    np.save(WEIGHTS, params)
    p = predict(d, params)
    m = p >= P_MIN
    print(f"  model: {len(p)} candidates, weights -> {WEIGHTS}; in-sample pairs p>={P_MIN}: {m.sum()} P {d['gold'][m].mean():.3f}")


if __name__ == "__main__":
    print(__doc__)
