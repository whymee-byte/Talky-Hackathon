"""Failure report of a window allocations file against the TRAIN golden (2024-07 -> 2025-06).

    python tools/analysis/fail_report.py work/window/allocations_base.parquet out/window_<...>.score.json > FAIL_REPORT.md

Sections: points lost per metric · exception classes · segments ranked by lost points (lines and pairs) ·
failure type per segment (no item / missing items / extra items / wrong items) and whether T4 had the right party.
"""
import json
import sys

import duckdb

alloc, score = sys.argv[1], sys.argv[2]
d = json.load(open(score))
m = d["metrics"]
c = duckdb.connect()
c.execute("SET preserve_insertion_order=false")
c.execute(f"CREATE VIEW a AS SELECT * FROM '{alloc}'")
c.execute("""CREATE TABLE g AS SELECT * FROM 'data_cache/train/golden_allocations.parquet'
             WHERE bank_txn_id IN (SELECT bank_txn_id FROM 'work/window/lines.parquet')""")
out = []
p = out.append
p(f"# Fallos de la ventana — score {d['score']}\n")
f1, acc, mac = m["allocation_f1"], m["txn_accuracy"], m["exception_macro_f1"]
p("| métrica | valor | puntos perdidos |\n|---|---|---|")
p(f"| allocation F1 (×50) | {f1:.4f} (P {m['precision']:.4f} · R {m['recall']:.4f}) | {50 * (1 - f1):.2f} |")
p(f"| txn accuracy (×30) | {acc:.4f} | {30 * (1 - acc):.2f} |")
p(f"| exception macro-F1 (×20) | {mac:.4f} | {20 * (1 - mac):.2f} |\n")

p("## Clases de excepción (cada una ≈ 2 puntos)\n")
p("| clase | gold | tp | fp | P | R | F1 | pts perdidos |\n|---|---|---|---|---|---|---|---|")
n_cls = len(d["exception_f1"])
for k, v in sorted(d["exception_f1"].items(), key=lambda kv: kv[1]["f1"]):
    p(f"| {k} | {v['gold']} | {v['tp']} | {v['fp']} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1']:.3f} | "
      f"{20 * (1 - v['f1']) / n_cls:.2f} |")

seg, sp = d["segments"], d.get("segment_pairs", {})
tot_lines = sum(v["n"] for v in seg.values())
gold_pairs = sum(v.get("gold_pairs", 0) for v in sp.values()) or 1
rows = []
for k, v in seg.items():
    lost_l = v["n"] * (1 - v["accuracy"])
    q = sp.get(k, {})
    lost_p = q.get("gold_pairs", 0) - q.get("tp", 0)
    pts = 30 * lost_l / tot_lines + 50 * lost_p / gold_pairs * 0.9   # rough: recall share of F1
    rows.append((pts, k, v["n"], v["accuracy"], int(lost_l), q.get("gold_pairs", 0), lost_p, q.get("precision")))
rows.sort(reverse=True)
p("\n## Segmentos por puntos perdidos (aprox.: 30·líneas + 50·pares de recall)\n")
p("| segmento | líneas | acc | líneas perdidas | pares gold | pares perdidos | P pares | pts ≈ |\n|---|---|---|---|---|---|---|---|")
for pts, k, n, a_, ll, gp, lp, pr in rows[:30]:
    p(f"| {k} | {n} | {a_:.3f} | {ll} | {gp} | {lp} | {'' if pr is None else f'{pr:.3f}'} | {pts:.2f} |")

top = [r[1] for r in rows[:12]]
p("\n## Tipo de fallo en los 12 segmentos que más pierden (líneas con partidas en el golden)\n")
df = c.sql(f"""
  WITH gs AS (SELECT bank_txn_id, any_value(segment) seg, count(item_id) ng FROM g GROUP BY 1),
       ss AS (SELECT bank_txn_id, count(item_id) ns FROM a GROUP BY 1),
       ok AS (SELECT g.bank_txn_id, count(*) FILTER (WHERE a.item_id IS NOT NULL) hit
              FROM g LEFT JOIN a USING (bank_txn_id, item_id) WHERE g.item_id IS NOT NULL GROUP BY 1),
       gp AS (SELECT g.bank_txn_id, any_value(i.party_id) gparty FROM g JOIN 'work/window/items.parquet' i USING (item_id) GROUP BY 1),
       t4 AS (SELECT bank_txn_id, list(party_id ORDER BY rank) c FROM 'work/window/line_party.parquet' GROUP BY 1),
       x AS (SELECT gs.*, coalesce(ss.ns, 0) ns, coalesce(ok.hit, 0) hit, list_contains(t4.c, gp.gparty) t4ok
             FROM gs LEFT JOIN ss USING (bank_txn_id) LEFT JOIN ok USING (bank_txn_id)
             LEFT JOIN gp USING (bank_txn_id) LEFT JOIN t4 USING (bank_txn_id)
             WHERE gs.ng > 0 AND gs.seg IN ({", ".join("'" + t + "'" for t in top)}))
  SELECT seg, count(*) lineas,
         count(*) FILTER (WHERE ns = 0) sin_partida,
         count(*) FILTER (WHERE ns = 0 AND t4ok) sin_partida_t4_ok,
         count(*) FILTER (WHERE ns > 0 AND hit = 0) todo_mal,
         count(*) FILTER (WHERE hit > 0 AND hit < ng) le_faltan,
         count(*) FILTER (WHERE hit = ng AND ns > ng) le_sobran,
         count(*) FILTER (WHERE hit = ng AND ns = ng) pares_bien,
         sum(ng - hit) pares_perdidos
  FROM x GROUP BY 1 ORDER BY pares_perdidos DESC""").df()
p(df.to_markdown(index=False))
p("\n(`pares_bien` = mismas partidas que el golden; la línea aún puede fallar por importe o excepción.)")
print("\n".join(out))
