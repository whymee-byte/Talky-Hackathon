"""T9 · informe por mes y por entidad de una fase → work/<fase>/final_report.md (sin red, sólo duckdb).

    python -m src.final.report --phase live [--submission out/live_….csv.gz] [--data <raíz>]

Para cada mes: líneas, % con partida (algún par), % sin partida, % UNIDENTIFIED, % con regla T2 (lotes),
confianza media; avisa de caídas (cobertura < 85 % de la mediana del resto de meses). Para cada entidad y cuenta:
lo mismo, marcando las que no están en masters/ (ZA01 en LIVE; en FINAL masters_final/ debería cubrirlas, D9).
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, data_root, safe_stdout

DROP_RATIO = 0.85


def _tbl(con, sql: str) -> list[dict]:
    r = con.execute(sql)
    cols = [d[0] for d in r.description]
    return [dict(zip(cols, row)) for row in r.fetchall()]


def _md(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "_(sin filas)_\n"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            cells.append("" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v)))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def build(phase: str, submission: str | None, data: str | None) -> str:
    w = os.path.join(REPO, "work", phase)
    root = data_root(data)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE B AS SELECT DISTINCT account_id FROM read_csv('{root}/masters/bank_accounts.csv', all_varchar=true)""")
    con.execute(f"""CREATE TABLE E AS SELECT entity FROM read_csv('{root}/masters/entities.csv', all_varchar=true)""")
    # una fila por línea con su estado
    con.execute("""CREATE TABLE S AS
        SELECT l.bank_txn_id, strftime(l.booking_date, '%Y-%m') AS mon, l.entity, l.account_id, l.direction, l.statement_format,
               coalesce(a.n_items, 0) > 0 has_item,
               coalesce(a.unident, false) unident,
               coalesce(a.t2, false) t2,
               a.conf,
               a.n_rows IS NULL missing,
               b.account_id IS NULL acct_unknown, e.entity IS NULL ent_unknown
        FROM L l
        LEFT JOIN (SELECT bank_txn_id, count(*) n_rows, count(item_id) n_items,
                          bool_or(exception = 'UNIDENTIFIED' OR category = 'UNIDENTIFIED') unident,
                          bool_or(rule LIKE 'T2%') t2, avg(confidence) conf
                   FROM A GROUP BY 1) a USING (bank_txn_id)
        LEFT JOIN B b USING (account_id) LEFT JOIN E e USING (entity)""")
    agg = """count(*) lines,
             100.0 * avg(has_item::INT) pct_item, 100.0 * avg((NOT has_item)::INT) pct_noitem,
             100.0 * avg(unident::INT) pct_unident, 100.0 * avg(t2::INT) pct_t2,
             100.0 * avg(CASE WHEN direction = 'IN' THEN has_item::INT END) pct_item_in,
             100.0 * avg(CASE WHEN direction = 'OUT' THEN has_item::INT END) pct_item_out,
             avg(conf) conf, sum(missing::INT) missing"""
    months = _tbl(con, f"SELECT mon AS month, string_agg(DISTINCT statement_format, ',' ORDER BY statement_format) formats, {agg} FROM S GROUP BY 1 ORDER BY 1")
    ents = _tbl(con, f"SELECT entity, bool_or(ent_unknown) unknown, {agg} FROM S GROUP BY 1 ORDER BY 1")
    accts = _tbl(con, f"SELECT entity, account_id, string_agg(DISTINCT statement_format, ',') formats, {agg} FROM S WHERE acct_unknown GROUP BY 1, 2 ORDER BY 3 DESC")
    fmts = _tbl(con, f"SELECT mon AS month, statement_format, {agg} FROM S GROUP BY 1, 2 ORDER BY 1, 2")
    tot = _tbl(con, f"SELECT {agg} FROM S")[0]

    warns = []
    if tot["missing"]:
        warns.append(f"{tot['missing']} líneas sin fila en allocations (submit las rellena con el fallback por signo)")
    import statistics
    for key in ("pct_item", "pct_item_in", "pct_item_out"):
        vals = [m[key] for m in months if m[key] is not None]
        if len(vals) >= 3:
            for m in months:
                others = [v for v in vals if v != m[key]]
                med = statistics.median(others)
                if m[key] is not None and med > 0 and m[key] < DROP_RATIO * med:
                    warns.append(f"caída en {m['month']}: {key} {m[key]:.1f} % vs mediana {med:.1f} % del resto ({m['formats']})")
    for e in ents:
        if e["unknown"]:
            warns.append(f"entidad {e['entity']} no está en masters/entities.csv ({e['lines']} líneas, {e['pct_item']:.1f} % con partida, "
                         f"{e['pct_unident']:.1f} % UNIDENTIFIED)")
    for a in accts:
        warns.append(f"cuenta {a['account_id']} ({a['entity']}, {a['formats']}) no está en masters/bank_accounts.csv: {a['lines']} líneas, "
                     f"{a['pct_item']:.1f} % con partida")
    if submission and os.path.exists(submission):
        n = con.execute(f"SELECT count(DISTINCT bank_txn_id) FROM read_csv('{submission}', all_varchar=true)").fetchone()[0]
        if n != tot["lines"]:
            warns.append(f"la entrega cubre {n} líneas y la fase tiene {tot['lines']}")

    cols = ["month", "formats", "lines", "pct_item", "pct_item_in", "pct_item_out", "pct_noitem", "pct_unident", "pct_t2", "conf"]
    md = [f"# Informe {phase} · {time.strftime('%Y-%m-%d %H:%M')}", "",
          f"Líneas {tot['lines']} · con partida {tot['pct_item']:.1f} % (cobros {tot['pct_item_in']:.1f} %, pagos {tot['pct_item_out']:.1f} %) · "
          f"sin partida {tot['pct_noitem']:.1f} % · UNIDENTIFIED {tot['pct_unident']:.1f} % · lotes T2 {tot['pct_t2']:.1f} %"
          + (f" · entrega `{os.path.basename(submission)}`" if submission else ""), ""]
    md.append("## Avisos\n")
    md += [f"- {x}" for x in warns] or ["- ninguno"]
    md += ["", "## Por mes\n", _md(months, cols), "## Por entidad\n",
           _md(ents, ["entity", "unknown", "lines", "pct_item", "pct_item_in", "pct_item_out", "pct_noitem", "pct_unident", "pct_t2", "conf"]),
           "## Por mes y formato de extracto\n", _md(fmts, ["month", "statement_format", "lines", "pct_item", "pct_noitem", "pct_unident", "conf"])]
    text = "\n".join(md)
    with open(os.path.join(w, "final_report.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def main() -> None:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--submission")
    ap.add_argument("--data")
    a = ap.parse_args()
    text = build(a.phase, a.submission, a.data)
    # por pantalla: cabecera, avisos y la tabla por mes (las de entidad/formato quedan en el .md)
    print(text.split("## Por entidad")[0])
    print(f"-> work/{a.phase}/final_report.md")


if __name__ == "__main__":
    main()
