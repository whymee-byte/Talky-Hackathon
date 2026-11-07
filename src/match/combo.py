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

CAP = 16          # most recent invoices per line
CAP_ARC = 4       # most recent credit notes per line (negative), on top of CAP
DUE_LO, DUE_HI = -120, 14
AGE_MAX = 200
K_MIN = 2
PARTY_RANKS, PARTY_MIN = 3, 0.2   # T4 candidates tried; the line is kept only if exactly one party gives exactly one subset


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


def add_solutions(con, passes=3):
    """Append unique mitm solutions to CS (bank_txn_id, items, k, mitm) for CB lines the SQL stage did not solve.
    Repeated: items taken by a pass leave the candidate sets, which makes other lines' solutions unique."""
    for p in range(passes):
        if _pass(con, False) == 0:
            break
    for p in range(2):        # credit notes (negative) only for what is left: they add spurious sums elsewhere
        if _pass(con, True) == 0:
            break


def _pass(con, with_arc):
    kinds = "('ARI', 'ARC')" if with_arc else "('ARI')"
    rows = con.sql(f"""
        WITH taken AS (SELECT item_id FROM SL UNION SELECT item_id FROM REFP UNION SELECT item_id FROM AMTP
                       UNION SELECT unnest(items) FROM CS),
             lines AS (   -- (line, party, remainder): CB lines with a referenced base, plus every T4 candidate party
               SELECT cb.bank_txn_id, cb.party_id, cb.rem, 1 AS rank FROM CB cb WHERE cb.has_base
               UNION ALL
               SELECT lt.bank_txn_id, p.party_id, lt.cash, p.rank
               FROM LT lt JOIN P p USING (bank_txn_id)
               WHERE p.rank <= {PARTY_RANKS} AND p.confidence >= {PARTY_MIN}
                 AND lt.bank_txn_id NOT IN (SELECT bank_txn_id FROM REFP)
                 AND lt.bank_txn_id NOT IN (SELECT bank_txn_id FROM AMTP)),
             cand AS (
               SELECT ln.bank_txn_id, ln.party_id, ln.rem, i.item_id,
                      CASE WHEN i.kind = 'ARC' THEN -i.open_at_start_cents ELSE i.open_at_start_cents END AS v
               FROM lines ln JOIN LT lt USING (bank_txn_id)
               JOIN IO i ON i.party_id = ln.party_id AND i.currency = lt.currency AND i.entity = lt.entity
                    AND i.kind IN {kinds} AND i.issue_date <= lt.booking_date
                    AND lt.booking_date - i.issue_date <= {AGE_MAX}
                    AND (i.kind = 'ARC' OR i.due_date BETWEEN lt.booking_date + ({DUE_LO}) AND lt.booking_date + {DUE_HI})
               WHERE lt.direction = 'IN' AND ln.rem > 0
                 AND ln.bank_txn_id NOT IN (SELECT bank_txn_id FROM CS)
                 AND i.item_id NOT IN (SELECT item_id FROM taken)
               QUALIFY row_number() OVER (PARTITION BY ln.bank_txn_id, ln.party_id, i.kind ORDER BY i.issue_date DESC, i.item_id)
                       <= CASE WHEN i.kind = 'ARC' THEN {CAP_ARC} ELSE {CAP} END)
        SELECT bank_txn_id, party_id, any_value(rem), list(item_id ORDER BY item_id), list(v ORDER BY item_id)
        FROM cand GROUP BY 1, 2 HAVING count(*) >= {K_MIN}
        ORDER BY 1, 2""").fetchall()
    out, parties, cur, sols, bad = [], {}, None, [], False
    def flush():
        if cur is not None and not bad and len(sols) == 1 and len(sols[0][1]) >= K_MIN:
            out.append((cur, sols[0][1], len(sols[0][1])))
            parties[cur] = sols[0][0]
    for bt, party, rem, ids, vals in rows:      # ordered by line: one line's parties come together
        if bt != cur:
            flush()
            cur, sols, bad = bt, [], False
        if bad:
            continue
        k, sol = solve(np.array(vals, dtype=np.int64), int(rem))
        if k == 1:
            sols.append((party, [ids[i] for i in sol]))
        elif k > 1:
            bad = True                          # ambiguous for one candidate party: give the line up
    flush()
    if out:
        con.executemany("INSERT INTO CS VALUES (?, ?, ?, true)", out)
        con.executemany("""INSERT INTO CB SELECT ?, ?, 0, lt.cash, false FROM LT lt WHERE lt.bank_txn_id = ?
                           AND lt.bank_txn_id NOT IN (SELECT bank_txn_id FROM CB)""",
                        [(bt, parties[bt], bt) for bt, _, _ in out])
    print(f"  comb mitm{' +ARC' if with_arc else ''}: {len(rows)} lines tried, {len(out)} unique solutions, {sum(k for _, _, k in out)} pairs")
    return len(out)
