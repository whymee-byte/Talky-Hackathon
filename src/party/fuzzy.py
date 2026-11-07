"""Fallback for lines with no candidate: rapidfuzz on UNIQUE extracted names (never per row).

Name string = camt counterparty name, else the line text without tokens that carry digits and without the
most frequent words of the phase (bank boilerplate such as TRANSFERENCIAS, SEPA, PAYMENT...).
Compared with partial_ratio (copes with truncation) against master names of compatible entities.
"""
import numpy as np
from rapidfuzz import fuzz, process

MIN_LEN = 8
CUTOFF = 90
TOP_WORDS = 300       # most frequent words of the phase = boilerplate
CONF_MAX = 0.5        # fuzzy evidence never beats an exact one


def add(con, phase, model):
    m = f"work/party_model/{model}"
    con.execute("""CREATE OR REPLACE TEMP TABLE nocand AS
        SELECT l.bank_txn_id, l.entity, l.cp_name_norm, l.text_norm FROM ln l
        WHERE NOT EXISTS (SELECT 1 FROM cand c WHERE c.bank_txn_id = l.bank_txn_id)""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE stopw AS
        SELECT w FROM (SELECT unnest(string_split(text_norm, ' ')) AS w FROM ln)
        GROUP BY 1 ORDER BY count(*) DESC LIMIT {TOP_WORDS}""")
    con.execute("""CREATE OR REPLACE TEMP TABLE fzq AS
        WITH w AS (SELECT bank_txn_id, entity, cp_name_norm, unnest(string_split(text_norm, ' ')) AS w,
                          generate_subscripts(string_split(text_norm, ' '), 1) AS pos FROM nocand)
        SELECT bank_txn_id, entity,
               coalesce(any_value(cp_name_norm),
                        left(string_agg(w, ' ' ORDER BY pos) FILTER (WHERE NOT regexp_matches(w, '[0-9]')
                             AND w NOT IN (SELECT w FROM stopw)), 40)) AS q
        FROM w GROUP BY 1, 2""")
    qs = con.sql(f"SELECT DISTINCT entity, q FROM fzq WHERE length(q) >= {MIN_LEN}").df()
    names = con.sql(f"""SELECT po.line_entity AS entity, p.party_id, p.name_norm
        FROM '{m}/parties.parquet' p JOIN pairs_ok po ON po.party_entity = p.entity
        WHERE length(p.name_norm) >= {MIN_LEN}""").df()
    rows = []
    for ent, g in qs.groupby("entity"):
        nm = names[names.entity == ent]
        if nm.empty:
            continue
        choices = nm.name_norm.tolist()
        ids = nm.party_id.to_numpy()
        sc = process.cdist(g.q.tolist(), choices, scorer=fuzz.partial_ratio, score_cutoff=CUTOFF, workers=-1)
        for qi, row in enumerate(sc):
            best = row.max()
            if best < CUTOFF:
                continue
            hit = np.flatnonzero(row == best)[:10]
            conf = CONF_MAX * (best - 80) / 20 / len(hit)
            for h in hit:
                rows.append((ent, g.q.iloc[qi], ids[h], float(conf), f"fuzzy={choices[h]}~{int(best)}"))
    con.execute("CREATE OR REPLACE TEMP TABLE fzhits (entity VARCHAR, q VARCHAR, party_id VARCHAR, conf DOUBLE, evidence VARCHAR)")
    if rows:
        con.executemany("INSERT INTO fzhits VALUES (?, ?, ?, ?, ?)", rows)
    con.execute("""CREATE OR REPLACE TEMP TABLE fz AS
        SELECT f.bank_txn_id, h.party_id, 'fuzzy' AS fam, 'fuzzy_partial_ratio' AS rule, h.conf, h.evidence
        FROM fzq f JOIN fzhits h ON h.entity = f.entity AND h.q = f.q""")
    print(f"fuzzy: {len(qs)} unique names, {len(rows)} name hits, "
          f"{con.sql('SELECT count(DISTINCT bank_txn_id) FROM fz').fetchall()[0][0]} lines")
    return "fz"
