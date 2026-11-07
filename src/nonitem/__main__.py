"""T7 — lines that settle no item: text pattern -> category (contracts/schemas.md §6).

    python -m src.nonitem --phase window [--q1]      # learns from TRAIN before 2024-07-01 (no leakage)
    python -m src.nonitem --phase live               # learns from all of TRAIN

Two learned tables, nothing hard-coded:
  patterns  (bank_code, sign, pattern) -> n lines, share with items, majority category among the no-item lines.
            pattern = text in upper case with every token containing a digit replaced by '#', only letters kept.
  tokens    naive-Bayes over the pattern's tokens (+ bank_code, sign) for patterns never seen in TRAIN
            (new bank formats, free-text names): classes = no-item categories + ITEM.
Output work/<phase>/line_category.parquet: bank_txn_id, p_item, category, p_category, rule, confidence, evidence.
Works without network; a pattern nobody has seen still gets the best token-based category.
"""
import argparse
import os
import time

from src.party.common import WINDOW_START, connect, out_dir, src

PAT_MIN_N = 2        # a pattern seen fewer times falls back to the token model
ALPHA = 0.5          # Laplace smoothing of the token model
P_ITEM_MAX = 0.5     # only patterns that mostly settle no item carry a category (§6: category if no item)
PATTERN_SQL = r"""regexp_replace(regexp_replace(regexp_replace(upper(strip_accents(coalesce({t}, ''))),
    '[A-Z]*[0-9][A-Z0-9/#*.:-]*', '#', 'g'), '[^A-Z#]+', ' ', 'g'), '\s+', ' ', 'g')"""


def pat(col):
    return PATTERN_SQL.format(t=col)


def learn(con, model):
    """Pattern and token tables from TRAIN golden (lines before the window for model='pre_window')."""
    d = f"work/nonitem_model/{model}"
    os.makedirs(d, exist_ok=True)
    cut = f"AND b.booking_date < DATE '{WINDOW_START}'" if model == "pre_window" else ""
    con.execute(f"""CREATE OR REPLACE TABLE TR AS
        SELECT b.bank_txn_id, b.bank_code, sign(b.amount)::INTEGER AS sgn, {pat('b.text')} AS pattern,
               g.has_item, g.category
        FROM {src('train', 'bank_transactions')} b
        JOIN (SELECT bank_txn_id, max(CASE WHEN item_id IS NULL OR item_id = '' THEN 0 ELSE 1 END) has_item,
                     max(CASE WHEN item_id IS NULL OR item_id = '' THEN category END) category
              FROM {src('train', 'golden_allocations')} GROUP BY 1) g USING (bank_txn_id)
        WHERE true {cut}""")
    con.execute(f"""COPY (
        WITH pc AS (SELECT bank_code, sgn, pattern, category, count(*) nc FROM TR WHERE has_item = 0 GROUP BY ALL),
             best AS (SELECT * FROM pc QUALIFY row_number() OVER (PARTITION BY bank_code, sgn, pattern ORDER BY nc DESC, category) = 1)
        SELECT p.bank_code, p.sgn, p.pattern, count(*) n, avg(has_item) p_item,
               any_value(b.category) category,
               any_value(b.nc) * 1.0 / greatest(1, count(*) FILTER (WHERE has_item = 0)) p_category
        FROM TR p LEFT JOIN best b USING (bank_code, sgn, pattern) GROUP BY 1, 2, 3) TO '{d}/patterns.parquet' (FORMAT parquet)""")
    # token model: classes = categories of no-item lines + ITEM
    con.execute("""CREATE OR REPLACE TABLE TT AS
        SELECT bank_txn_id, CASE WHEN has_item = 1 THEN 'ITEM' ELSE category END cls, tok
        FROM (SELECT *, unnest(list_distinct(list_concat(string_split(pattern, ' '),
                                                         ['BC=' || coalesce(bank_code, ''), 'SGN=' || sgn]))) tok FROM TR)
        WHERE tok <> ''""")
    con.execute(f"""COPY (SELECT cls, count(DISTINCT bank_txn_id) n_lines, count(*) n_tok FROM TT GROUP BY 1)
                    TO '{d}/classes.parquet' (FORMAT parquet)""")
    con.execute(f"""COPY (SELECT cls, tok, count(*) n FROM TT GROUP BY 1, 2 HAVING count(*) >= 2)
                    TO '{d}/tokens.parquet' (FORMAT parquet)""")
    con.execute(f"""COPY (SELECT count(DISTINCT tok) v FROM TT) TO '{d}/vocab.parquet' (FORMAT parquet)""")
    n = con.execute("SELECT count(*) FROM TR").fetchone()[0]
    print(f"model {model}: {n} TRAIN lines, "
          f"{con.execute(f'''SELECT count(*) FROM '{d}/patterns.parquet' ''').fetchone()[0]} patterns, "
          f"{con.execute(f'''SELECT count(*) FROM '{d}/tokens.parquet' ''').fetchone()[0]} token/class rows")
    return d


def apply(con, phase, model_dir, q1=False):
    sfx = "_q1" if q1 else ""
    qf = "AND b.booking_date BETWEEN DATE '2024-07-01' AND DATE '2024-09-30'" if q1 else ""
    wf = f"AND b.booking_date >= DATE '{WINDOW_START}'" if phase == "window" else ""
    base = src(phase, "bank_transactions")
    con.execute(f"""CREATE OR REPLACE TABLE L AS
        SELECT b.bank_txn_id, b.bank_code, sign(b.amount)::INTEGER AS sgn, {pat('b.text')} AS pattern
        FROM {base} b WHERE true {wf} {qf}""")
    con.execute(f"""CREATE OR REPLACE TABLE SEEN AS
        SELECT l.bank_txn_id, p.n, p.p_item, p.category, p.p_category
        FROM L l JOIN '{model_dir}/patterns.parquet' p USING (bank_code, sgn, pattern) WHERE p.n >= {PAT_MIN_N}""")
    # token model for the rest
    V = con.execute(f"SELECT v FROM '{model_dir}/vocab.parquet'").fetchone()[0]
    con.execute(f"""CREATE OR REPLACE TABLE NB AS
        WITH lt AS (SELECT bank_txn_id, unnest(list_distinct(list_concat(string_split(pattern, ' '),
                                                  ['BC=' || coalesce(bank_code, ''), 'SGN=' || sgn]))) tok
                    FROM L WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM SEEN)),
             lt2 AS (SELECT * FROM lt WHERE tok IN (SELECT DISTINCT tok FROM '{model_dir}/tokens.parquet')),
             c AS (SELECT cls, n_lines, n_tok FROM '{model_dir}/classes.parquet'),
             tot AS (SELECT sum(n_lines) s FROM c),
             sc AS (SELECT lt2.bank_txn_id, c.cls,
                           ln(c.n_lines / tot.s) + sum(ln((coalesce(t.n, 0) + {ALPHA}) / (c.n_tok + {ALPHA} * {V}))) s
                    FROM lt2 CROSS JOIN c CROSS JOIN tot
                    LEFT JOIN '{model_dir}/tokens.parquet' t ON t.cls = c.cls AND t.tok = lt2.tok
                    GROUP BY 1, 2, c.n_lines, tot.s),
             post AS (SELECT bank_txn_id, cls, exp(s - max(s) OVER (PARTITION BY bank_txn_id)) e FROM sc),
             norm AS (SELECT bank_txn_id, cls, e / sum(e) OVER (PARTITION BY bank_txn_id) p FROM post)
        SELECT bank_txn_id,
               max(p) FILTER (WHERE cls = 'ITEM') p_item,
               arg_max(cls, p) FILTER (WHERE cls <> 'ITEM') category,
               max(p) FILTER (WHERE cls <> 'ITEM') / greatest(1e-9, sum(p) FILTER (WHERE cls <> 'ITEM')) p_category
        FROM norm GROUP BY 1""")
    out = f"{out_dir(phase)}/line_category{sfx}.parquet"
    con.execute(f"""COPY (
        SELECT bank_txn_id, round(p_item, 4) p_item, category, round(p_category, 4) p_category,
               'T7.pattern' rule, round((1 - p_item) * p_category, 4) confidence,
               'pattern seen ' || n || 'x in TRAIN; p_item=' || round(p_item, 2) || '; p_cat=' || round(p_category, 2) evidence
        FROM SEEN WHERE p_item <= {P_ITEM_MAX}
        UNION ALL
        SELECT bank_txn_id, round(p_item, 4), category, round(p_category, 4),
               'T7.tokens', round((1 - p_item) * p_category, 4),
               'pattern unseen; token model p_item=' || round(p_item, 2) || '; p_cat=' || round(p_category, 2)
        FROM NB WHERE p_item <= {P_ITEM_MAX}
    ) TO '{out}' (FORMAT parquet)""")
    st = con.sql(f"""SELECT rule, count(*) lines, round(avg(p_item), 3) p_item, round(avg(p_category), 3) p_cat
                     FROM '{out}' GROUP BY 1""").df()
    print(st.to_string(index=False))
    print(f"line_category → {out}  ({con.execute('SELECT count(*) FROM L').fetchone()[0]} lines of the phase)")
    return out


def evaluate(con, phase, out, q1=False):
    """Window only: category accuracy on golden no-item lines and false 'no item' calls."""
    if phase != "window":
        return
    con.execute(f"""CREATE OR REPLACE TABLE G AS
        SELECT bank_txn_id, max(CASE WHEN item_id IS NULL OR item_id = '' THEN 0 ELSE 1 END) has_item,
               max(CASE WHEN item_id IS NULL OR item_id = '' THEN category END) category
        FROM {src('train', 'golden_allocations')} GROUP BY 1""")
    print(con.sql(f"""
        SELECT c.rule, count(*) lines, sum(g.has_item = 0) gold_noitem,
               sum(g.has_item = 0 AND g.category = c.category) right_cat,
               round(sum(g.has_item = 0 AND g.category = c.category) * 1.0 / greatest(1, sum(g.has_item = 0)), 3) acc_noitem,
               sum(g.has_item = 1) gold_item
        FROM L l JOIN G g USING (bank_txn_id) LEFT JOIN '{out}' c USING (bank_txn_id) GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    print(con.sql(f"""
        SELECT g.category gold, coalesce(c.category, '(none)') pred, count(*) n
        FROM L l JOIN G g USING (bank_txn_id) LEFT JOIN '{out}' c USING (bank_txn_id)
        WHERE g.has_item = 0 AND coalesce(c.category, '') <> g.category GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""").df().to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--q1", action="store_true", help="window only: lines of 2024-07..09 (dev loop)")
    ap.add_argument("--relearn", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    con = connect("4GB")
    model = "pre_window" if phase_model(a.phase) else "full"
    d = f"work/nonitem_model/{model}"
    if a.relearn or not os.path.exists(f"{d}/tokens.parquet"):
        learn(con, model)
    out = apply(con, a.phase, d, a.q1)
    evaluate(con, a.phase, out, a.q1)
    print(f"done in {time.time() - t0:.0f}s")


def phase_model(phase):
    return phase in ("window", "sample")


if __name__ == "__main__":
    main()
