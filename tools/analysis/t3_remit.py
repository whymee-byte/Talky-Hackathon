"""T3 análisis: tokens de los avisos de pago de la ventana (pdf/csv/xlsx/txt) → work/analysis/remit_tok.parquet.

Una fila por (aviso, token compacto con dígito). Sin interpretar maquetaciones: basta para medir si el nº de documento
de la partida aparece en algún aviso y si el aviso se puede enlazar a la línea por una referencia común.
"""
import re
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from t3_prep import OUT, connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
R = ROOT / "data/bankrec-docs-and-masters/train/remittance_advices"
D0, D1 = "2024-06-01", "2025-07-31"
SPLIT = re.compile(r"[^A-Z0-9/\-\.]+")


def text_of(path: Path, fmt: str) -> str:
    try:
        if fmt == "pdf":
            import pypdf
            return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(str(path)).pages)
        if fmt == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            return "\n".join(" ".join("" if v is None else str(v) for v in row)
                             for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
        return path.read_text(encoding="utf-8", errors="replace").replace(";", " ").replace(",", " ")
    except Exception as e:  # un aviso ilegible no rompe el análisis
        return f"__ERROR__ {e}"


def tokens(args):
    f, fmt = args
    t = text_of(R / f, fmt).upper()
    out = set()
    for raw in SPLIT.split(t):
        c = re.sub(r"[^A-Z0-9]", "", raw)
        if len(c) >= 4 and re.search(r"[0-9]", c):
            out.add(c)
    return [(f, x) for x in out] or [(f, "")]


def main():
    t = time.time()
    ix = pd.read_csv(R / "index.csv")
    ix = ix[(ix.received_date >= D0) & (ix.received_date <= D1)]
    with Pool(8) as p:
        rows = [r for rs in p.imap_unordered(tokens, list(zip(ix.file, ix.format)), chunksize=32) for r in rs]
    tk = pd.DataFrame(rows, columns=["file", "tok"]).merge(ix, on="file")
    tk["received_date"] = pd.to_datetime(tk.received_date).dt.date
    con = connect()
    con.register("tk", tk)
    con.sql(f"copy (select * from tk) to '{OUT}/remit_tok.parquet' (format parquet)")
    print(len(ix), "avisos;", len(tk), "tokens;", f"{time.time() - t:.0f}s")
    print(tk.groupby("format").file.nunique())


if __name__ == "__main__":
    main()
