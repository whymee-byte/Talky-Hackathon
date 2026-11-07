"""T3 comb (mitm): a subset of 2..CAP open invoices of the T4 party that adds up to the line's remainder.

Lines without any reference in the text (ar_ref_noise multi-invoice, ~8 % of the window's golden pairs) can only be
explained by party + amounts. The SQL stage covers subsets of up to 3 items; this one covers larger subsets with a
meet-in-the-middle enumeration (2^(n/2) sums per half, numpy) over a small, well-chosen candidate set:
  * invoices of the party in the line's entity and currency, issued <= booking date and at most AGE_MAX days before,
  * due between booking - DUE_LO and booking + DUE_HI days (window golden: 1st-99th percentile of due - booking),
  * not already taken by a side link, a reference match, an amount match or a smaller-subset solution,
  * the CAP most recent ones (more candidates = more spurious exact sums: cap 16 measured P 0.96, cap 20 P 0.86).
Only a unique solution is accepted. Measured on the window (sample of 800 golden multi-invoice lines): P 0.96, R 0.28
of the unexplained multi-invoice lines; 0 spurious solutions on 190 lines whose golden has no item.
"""
import numpy as np

CAP = 16          # most recent candidates per line
DUE_LO, DUE_HI = -120, 14
AGE_MAX = 200
K_MIN = 2


def _sums(vals):
    s = np.zeros(1, dtype=np.int64)
    for x in vals:
        s = np.concatenate([s, s + x])
    return s


def solve(vals, target):
    """Number of subsets of vals summing to target and, when exactly one, its indexes."""
    n = len(vals)
    h = n // 2
    sa = _sums(vals[:h])
    order = np.argsort(sa, kind="stable")
    sas = sa[order]
    sb = _sums(vals[h:])
    need = target - sb
    lo = np.searchsorted(sas, need, "left")
    hi = np.searchsorted(sas, need, "right")
    cnt = hi - lo
    k = int(cnt.sum())
    if k != 1:
        return k, None
    j = int(np.nonzero(cnt)[0][0])
    i = int(order[lo[j]])
    return 1, [idx for idx in range(h) if i >> idx & 1] + [h + idx for idx in range(n - h) if j >> idx & 1]


def add_solutions(con):
    """Append unique mitm solutions to CS (bank_txn_id, items, k, mitm) for CB lines the SQL stage did not solve."""
    rows = con.sql(f"""
        WITH taken AS (SELECT item_id FROM SL UNION SELECT item_id FROM REFP UNION SELECT item_id FROM AMTP
                       UNION SELECT unnest(items) FROM CS),
             cand AS (
               SELECT cb.bank_txn_id, cb.rem, i.item_id, i.open_at_start_cents AS v
               FROM CB cb JOIN LT lt USING (bank_txn_id)
               JOIN IO i ON i.party_id = cb.party_id AND i.currency = lt.currency AND i.entity = lt.entity
                    AND i.kind = 'ARI' AND i.issue_date <= lt.booking_date
                    AND lt.booking_date - i.issue_date <= {AGE_MAX}
                    AND i.due_date BETWEEN lt.booking_date + ({DUE_LO}) AND lt.booking_date + {DUE_HI}
               WHERE lt.direction = 'IN' AND cb.rem > 0
                 AND cb.bank_txn_id NOT IN (SELECT bank_txn_id FROM CS)
                 AND i.item_id NOT IN (SELECT item_id FROM taken)
               QUALIFY row_number() OVER (PARTITION BY cb.bank_txn_id ORDER BY i.issue_date DESC, i.item_id) <= {CAP})
        SELECT bank_txn_id, any_value(rem), list(item_id ORDER BY item_id), list(v ORDER BY item_id)
        FROM cand GROUP BY 1 HAVING count(*) >= {K_MIN}""").fetchall()
    out = []
    for bt, rem, ids, vals in rows:
        k, sol = solve(np.array(vals, dtype=np.int64), int(rem))
        if k == 1 and len(sol) >= K_MIN:
            out.append((bt, [ids[i] for i in sol], len(sol)))
    con.executemany("INSERT INTO CS VALUES (?, ?, ?, true)", out)
    print(f"  comb mitm: {len(rows)} lines tried, {len(out)} unique solutions, {sum(k for _, _, k in out)} pairs")
