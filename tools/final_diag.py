"""Unsupervised drift diagnostics of a phase against a reference phase (no golden needed).

    python tools/final_diag.py --phase final --ref window [--out work/final/diag.md]

Reads work/<phase>/{lines,allocations,line_party,line_category}.parquet (and the same for --ref) and prints:
  1. coverage by entity / month / direction (lines with items, category lines, UNIDENTIFIED)
  2. rule frequency per 1,000 lines, phase vs ref (largest deltas)
  3. exception class rows per 1,000 lines, phase vs ref
  4. receipts (IN) left UNIDENTIFIED: by entity, and the most frequent masked text patterns (digits -> #)
  5. T4 coverage: lines with a rank-1 party at confidence >= 0.5, by entity
  6. T7: share of category lines decided by the token model (pattern never seen) by entity / format
  7. pairs per line distribution
Everything is columnar duckdb; output is small (top-N tables).
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PATTERN_SQL = r"""regexp_replace(regexp_replace(regexp_replace(upper(coalesce({t}, '')),
    '[A-Z]*[0-9][A-Z0-9/#*.:-]*', '#', 'g'), '[^A-Z#]+', ' ', 'g'), '\s+', ' ', 'g')"""


def tbl(con, sql: str, limit: int | None = None) -> str:
    r = con.execute(sql)
    cols = [d[0] for d in r.description]
    rows = r.fetchall()
    if limit:
        rows = rows[:limit]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in row) + " |")
    return "\n".join(out) + "\n"


def load(con, name: str, phase: str, required: bool = True) -> bool:
    p = os.path.join(REPO, "work", phase, f"{name}.parquet").replace(os.sep, "/")
    if not os.path.exists(p):
        if required:
            raise SystemExit(f"missing {p}")
        return False
    con.execute(f"CREATE OR REPLACE VIEW {name}_{phase} AS SELECT * FROM read_parquet('{p}')")
    return True


def base(con, ph: str) -> None:
    """S_<ph>: one row per line with its allocation summary."""
    con.execute(f"""CREATE OR REPLACE TABLE S_{ph} AS
        SELECT l.bank_txn_id, l.entity, strftime(l.booking_date, '%Y-%m') AS mon, l.direction, l.statement_format,
               l.currency, l.text_norm, abs(l.amount_cents) AS cash,
               coalesce(a.n_items, 0) AS n_items, coalesce(a.n_items, 0) > 0 AS has_item,
               coalesce(a.unident, false) AS unident, a.category, a.exc, a.rules
        FROM lines_{ph} l
        LEFT JOIN (SELECT bank_txn_id, count(item_id) AS n_items,
                          bool_or(coalesce(exception, '') = 'UNIDENTIFIED' OR coalesce(category, '') = 'UNIDENTIFIED') AS unident,
                          max(category) AS category, string_agg(DISTINCT nullif(exception, ''), ',') AS exc,
                          string_agg(DISTINCT coalesce(rule, ''), ',') AS rules
                   FROM allocations_{ph} GROUP BY 1) a USING (bank_txn_id)""")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="final")
    ap.add_argument("--ref", default="window")
    ap.add_argument("--out")
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    P, R = a.phase, a.ref
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    for ph in (P, R):
        load(con, "lines", ph)
        load(con, "allocations", ph)
        base(con, ph)
    has_lp = {ph: load(con, "line_party", ph, required=False) for ph in (P, R)}
    has_lc = {ph: load(con, "line_category", ph, required=False) for ph in (P, R)}
    md = [f"# Diagnóstico {P} frente a {R}\n"]

    md.append(f"## 0. Totales\n")
    md.append(tbl(con, f"""
        SELECT '{P}' AS phase, count(*) AS lines, round(100.0 * avg(has_item::INT), 2) AS pct_item,
               round(100.0 * avg((direction = 'IN' AND has_item)::INT) / nullif(avg((direction = 'IN')::INT), 0), 2) AS pct_item_in,
               round(100.0 * avg((direction = 'OUT' AND has_item)::INT) / nullif(avg((direction = 'OUT')::INT), 0), 2) AS pct_item_out,
               round(100.0 * avg(unident::INT), 2) AS pct_unident, sum(n_items) AS pairs,
               round(sum(n_items) * 1.0 / count(*), 3) AS pairs_per_line
        FROM S_{P}
        UNION ALL
        SELECT '{R}', count(*), round(100.0 * avg(has_item::INT), 2),
               round(100.0 * avg((direction = 'IN' AND has_item)::INT) / nullif(avg((direction = 'IN')::INT), 0), 2),
               round(100.0 * avg((direction = 'OUT' AND has_item)::INT) / nullif(avg((direction = 'OUT')::INT), 0), 2),
               round(100.0 * avg(unident::INT), 2), sum(n_items), round(sum(n_items) * 1.0 / count(*), 3)
        FROM S_{R}"""))

    md.append("## 1. Cobertura por entidad (fase | referencia)\n")
    md.append(tbl(con, f"""
        WITH p AS (SELECT entity, count(*) n, 100.0 * avg(has_item::INT) pi,
                          100.0 * avg(CASE WHEN direction = 'IN' THEN has_item::INT END) pin,
                          100.0 * avg(CASE WHEN direction = 'OUT' THEN has_item::INT END) pout,
                          100.0 * avg(unident::INT) pu FROM S_{P} GROUP BY 1),
             r AS (SELECT entity, count(*) n, 100.0 * avg(has_item::INT) pi,
                          100.0 * avg(CASE WHEN direction = 'IN' THEN has_item::INT END) pin,
                          100.0 * avg(CASE WHEN direction = 'OUT' THEN has_item::INT END) pout,
                          100.0 * avg(unident::INT) pu FROM S_{R} GROUP BY 1)
        SELECT coalesce(p.entity, r.entity) entity, p.n AS lines, round(p.pi, 1) pct_item, round(r.pi, 1) ref_pct_item,
               round(p.pin, 1) pct_item_in, round(r.pin, 1) ref_in, round(p.pout, 1) pct_item_out, round(r.pout, 1) ref_out,
               round(p.pu, 1) pct_unident, round(r.pu, 1) ref_unident
        FROM p FULL JOIN r USING (entity) ORDER BY 1"""))

    md.append("## 2. Cobertura por mes y dirección (fase)\n")
    md.append(tbl(con, f"""
        SELECT mon, direction, count(*) lines, round(100.0 * avg(has_item::INT), 1) pct_item,
               round(100.0 * avg(unident::INT), 1) pct_unident
        FROM S_{P} GROUP BY 1, 2 ORDER BY 1, 2"""))

    md.append("## 3. Frecuencia de reglas por 1.000 líneas (fase vs referencia, mayores diferencias)\n")
    md.append(tbl(con, f"""
        WITH p AS (SELECT coalesce(rule, '') rule, count(*) * 1000.0 / (SELECT count(*) FROM S_{P}) f FROM allocations_{P} GROUP BY 1),
             r AS (SELECT coalesce(rule, '') rule, count(*) * 1000.0 / (SELECT count(*) FROM S_{R}) f FROM allocations_{R} GROUP BY 1)
        SELECT coalesce(p.rule, r.rule) rule, round(coalesce(p.f, 0), 2) per_1000, round(coalesce(r.f, 0), 2) ref_per_1000,
               round(coalesce(p.f, 0) - coalesce(r.f, 0), 2) delta,
               round(coalesce(p.f, 0) / nullif(coalesce(r.f, 0), 0), 2) ratio
        FROM p FULL JOIN r USING (rule) ORDER BY abs(coalesce(p.f, 0) - coalesce(r.f, 0)) DESC""", a.top))

    md.append("## 4. Excepciones por 1.000 líneas (fase vs referencia)\n")
    md.append(tbl(con, f"""
        WITH p AS (SELECT coalesce(nullif(exception, ''), '(none)') e, count(*) * 1000.0 / (SELECT count(*) FROM S_{P}) f FROM allocations_{P} GROUP BY 1),
             r AS (SELECT coalesce(nullif(exception, ''), '(none)') e, count(*) * 1000.0 / (SELECT count(*) FROM S_{R}) f FROM allocations_{R} GROUP BY 1)
        SELECT coalesce(p.e, r.e) exception, round(coalesce(p.f, 0), 2) per_1000, round(coalesce(r.f, 0), 2) ref_per_1000,
               round(coalesce(p.f, 0) / nullif(coalesce(r.f, 0), 0), 2) ratio
        FROM p FULL JOIN r USING (e) ORDER BY 2 DESC"""))

    md.append("## 5. Cobros (IN) que quedan UNIDENTIFIED en la fase: por entidad\n")
    md.append(tbl(con, f"""
        SELECT entity, count(*) FILTER (WHERE direction = 'IN') receipts,
               count(*) FILTER (WHERE direction = 'IN' AND unident) unident,
               round(100.0 * count(*) FILTER (WHERE direction = 'IN' AND unident) / nullif(count(*) FILTER (WHERE direction = 'IN'), 0), 1) pct,
               round(sum(cash) FILTER (WHERE direction = 'IN' AND unident) / 100.0) cash_unident
        FROM S_{P} GROUP BY 1 ORDER BY 3 DESC"""))

    md.append(f"## 6. Patrones de texto más frecuentes entre los cobros UNIDENTIFIED (dígitos → #), top {a.top}\n")
    md.append(tbl(con, f"""
        SELECT entity, left({PATTERN_SQL.format(t='text_norm')}, 80) pattern, count(*) n, round(sum(cash) / 100.0) cash
        FROM S_{P} WHERE direction = 'IN' AND unident GROUP BY 1, 2 ORDER BY 3 DESC""", a.top))

    md.append(f"## 7. Pagos (OUT) sin partida: categorías por entidad (fase)\n")
    md.append(tbl(con, f"""
        SELECT entity, category, count(*) n FROM S_{P} WHERE direction = 'OUT' AND NOT has_item
        GROUP BY 1, 2 ORDER BY 1, 3 DESC"""))

    if has_lp[P] and has_lp[R]:
        md.append("## 8. T4: líneas con tercero rank-1 a confianza ≥ 0,5, por entidad (fase | referencia)\n")
        md.append(tbl(con, f"""
            WITH p AS (SELECT s.entity, count(*) n, 100.0 * avg(coalesce(lp.confidence >= 0.5, false)::INT) c5,
                              100.0 * avg((lp.bank_txn_id IS NOT NULL)::INT) anyc
                       FROM S_{P} s LEFT JOIN (SELECT bank_txn_id, max(confidence) confidence FROM line_party_{P} GROUP BY 1) lp USING (bank_txn_id)
                       GROUP BY 1),
                 r AS (SELECT s.entity, count(*) n, 100.0 * avg(coalesce(lp.confidence >= 0.5, false)::INT) c5,
                              100.0 * avg((lp.bank_txn_id IS NOT NULL)::INT) anyc
                       FROM S_{R} s LEFT JOIN (SELECT bank_txn_id, max(confidence) confidence FROM line_party_{R} GROUP BY 1) lp USING (bank_txn_id)
                       GROUP BY 1)
            SELECT coalesce(p.entity, r.entity) entity, p.n lines, round(p.anyc, 1) pct_candidate, round(r.anyc, 1) ref_candidate,
                   round(p.c5, 1) pct_conf05, round(r.c5, 1) ref_conf05
            FROM p FULL JOIN r USING (entity) ORDER BY 1"""))

    if has_lc[P]:
        md.append("## 9. T7: líneas de categoría decididas por el modelo de tokens (patrón nunca visto), por entidad y formato (fase)\n")
        md.append(tbl(con, f"""
            SELECT s.entity, s.statement_format, count(*) cat_lines,
                   round(100.0 * avg((lc.rule = 'T7.tokens')::INT), 1) pct_tokens,
                   round(100.0 * avg((lc.bank_txn_id IS NULL)::INT), 1) pct_no_t7
            FROM S_{P} s LEFT JOIN line_category_{P} lc USING (bank_txn_id)
            WHERE NOT s.has_item GROUP BY 1, 2 ORDER BY 3 DESC"""))

    md.append("## 10. Partidas por línea (fase vs referencia, % de líneas)\n")
    md.append(tbl(con, f"""
        WITH b AS (SELECT CASE WHEN n_items = 0 THEN '0' WHEN n_items = 1 THEN '1' WHEN n_items <= 5 THEN '2-5'
                               WHEN n_items <= 20 THEN '6-20' WHEN n_items <= 100 THEN '21-100' ELSE '>100' END k, 'p' ph FROM S_{P}
                   UNION ALL
                   SELECT CASE WHEN n_items = 0 THEN '0' WHEN n_items = 1 THEN '1' WHEN n_items <= 5 THEN '2-5'
                               WHEN n_items <= 20 THEN '6-20' WHEN n_items <= 100 THEN '21-100' ELSE '>100' END, 'r' FROM S_{R})
        SELECT k AS items_per_line,
               round(100.0 * count(*) FILTER (WHERE ph = 'p') / (SELECT count(*) FROM S_{P}), 2) pct,
               round(100.0 * count(*) FILTER (WHERE ph = 'r') / (SELECT count(*) FROM S_{R}), 2) ref_pct
        FROM b GROUP BY 1 ORDER BY 1"""))

    text = "\n".join(md)
    out = a.out or os.path.join(REPO, "work", P, "diag.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
