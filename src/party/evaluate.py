"""Measure line_party on the TRAIN window (2024-07 -> 2025-06) against the golden party set.

A line counts if the golden settles items on it. top-1 is a hit when rank 1 is one of the golden parties
(lines that pay several parties — batches — count as hit when rank 1 is any of them).
"""
from .common import WINDOW_START, connect

PANELS = [("total", "'all'"), ("formato", "statement_format"), ("tipo", "line_type"),
          ("direccion", "direction"), ("formato x tipo", "statement_format || ' · ' || line_type")]


def evaluate(path="work/window/line_party.parquet", lines="work/window/party_lines.parquet", show=True):
    con = connect()
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ev AS
      WITH t AS (SELECT * FROM 'work/train/party_truth.parquet' WHERE booking_date >= DATE '{WINDOW_START}'),
           p AS (SELECT bank_txn_id, list(party_id ORDER BY rank) AS cands, max(confidence) AS conf1
                 FROM '{path}' GROUP BY 1)
      SELECT t.bank_txn_id, t.line_type, t.n_party, l.statement_format, l.direction, p.conf1,
             coalesce(list_contains(t.parties, p.cands[1]), false) AS top1,
             coalesce(len(list_intersect(t.parties, p.cands)) > 0, false) AS top3,
             p.cands IS NOT NULL AS covered
      FROM t JOIN '{lines}' l USING (bank_txn_id) LEFT JOIN p USING (bank_txn_id)""")
    res = {}
    for name, expr in PANELS:
        df = con.sql(f"""SELECT {expr} AS grupo, count(*) AS lineas,
                round(100 * avg(covered::int), 1) AS cobertura, round(100 * avg(top1::int), 2) AS top1,
                round(100 * avg(top3::int), 2) AS top3, count(*) - sum(top1::int) AS fallos_top1
            FROM ev GROUP BY 1 ORDER BY fallos_top1 DESC""").df()
        res[name] = df
        if show:
            print(f"\n## {name}\n{df.to_string(index=False)}")
    cal = con.sql("""SELECT floor(conf1 * 10) / 10 AS conf_bucket, count(*) AS lineas,
                round(100 * avg(top1::int), 1) AS precision_top1
            FROM ev WHERE covered GROUP BY 1 ORDER BY 1""").df()
    res["calibracion"] = cal
    if show:
        print(f"\n## calibración (confianza de rank 1 vs acierto)\n{cal.to_string(index=False)}")
    return res


if __name__ == "__main__":
    evaluate()
