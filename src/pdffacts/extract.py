"""T8 · PDF text cache: data_cache/<phase>/pdf_text.parquet (file, document_type, document_number, entity, party_id,
document_date, text). One pass per phase, parallel; later steps only read the Parquet.

    python -m src.pdffacts.extract --phase sample|train|live|final [--workers 8]
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from src.common import REPO, phase_dir, safe_stdout

logging.getLogger("pypdf").setLevel(logging.ERROR)


def _text(path: str) -> str:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(path).pages)
    except Exception as ex:  # a broken PDF must never stop the pipeline
        return f"__ERROR__ {type(ex).__name__}"


def _chunk(paths: list[str]) -> list[str]:
    return [_text(p) for p in paths]


def extract(phase: str, data: str | None = None, workers: int = 8) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq
    base = os.path.join(phase_dir(phase, data), "pdfs")
    rows = [r for r in csv.DictReader(open(os.path.join(base, "index.csv"), encoding="utf-8-sig"))
            if not os.path.basename(r["file"]).startswith("._")]
    paths = [os.path.join(base, r["file"]) for r in rows]
    size = 200
    chunks = [paths[i:i + size] for i in range(0, len(paths), size)]
    texts: list[str] = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, out in enumerate(ex.map(_chunk, chunks)):
            texts.extend(out)
            if i % 20 == 0:
                print(f"  {len(texts)}/{len(paths)} pdfs ({time.time() - t0:.0f}s)", flush=True)
    cols = {k: [r[k] for r in rows] for k in rows[0]}
    cols["text"] = texts
    out_dir = os.path.join(REPO, "data_cache", phase)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "pdf_text.parquet")
    pq.write_table(pa.table(cols), out)
    return out


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "live", "final"])
    ap.add_argument("--data")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    a = ap.parse_args(argv)
    t0 = time.time()
    out = extract(a.phase, a.data, a.workers)
    print(f"[{a.phase}] -> {out} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
