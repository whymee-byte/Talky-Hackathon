"""Run T5: python -m src.exceptions --phase window|live|final."""
from __future__ import annotations

import argparse
import os

import duckdb
import polars as pl

from src.common import REPO, data_root
from .engine import OUTPUT, decide


def _parquet(path: str) -> pl.DataFrame:
    return pl.read_parquet(path)


def _csv(path: str) -> pl.DataFrame:
    con = duckdb.connect(":memory:")
    # DuckDB's CSV reader handles quoted commas and the supplied master files robustly.
    escaped = path.replace(os.sep, "/").replace("'", "''")
    frame = con.sql(f"SELECT * FROM read_csv('{escaped}', all_varchar=true, sample_size=-1, strict_mode=false, null_padding=true)").pl()
    con.close()
    return frame


def build(phase: str, root: str | None = None) -> dict:
    w = os.path.join(REPO, "work", phase)
    required = ["pairs_raw.parquet", "lines.parquet", "items.parquet"]
    missing = [name for name in required if not os.path.exists(os.path.join(w, name))]
    if missing:
        raise FileNotFoundError(f"missing T3/T1 outputs in {w}: {', '.join(missing)}")
    pairs = _parquet(os.path.join(w, "pairs_raw.parquet"))
    lines = _parquet(os.path.join(w, "lines.parquet"))
    items = _parquet(os.path.join(w, "items.parquet"))
    data = data_root(root)
    customers = _csv(os.path.join(data, "masters", "customers.csv"))
    fx = _csv(os.path.join(data, "masters", "fx_rates.csv"))
    decided = decide(pairs, lines, items, customers, fx)

    # Keep T2 batch rows, which T3 deliberately excludes from pairs_raw. Preserve T3's
    # no-item/T7 rows from its allocation artifact, then replace its matched rows with T5.
    old_alloc = os.path.join(w, "allocations.parquet")
    parts = [decided]
    if os.path.exists(os.path.join(w, "side_links.parquet")):
        side = _parquet(os.path.join(w, "side_links.parquet"))
        side = side.with_columns(
            pl.col("amount_cents").cast(pl.Int64),
            pl.lit(None, dtype=pl.String).alias("category"),
            pl.col("exception").cast(pl.String),
            pl.col("rule").cast(pl.String),
            pl.col("confidence").cast(pl.Float64),
            pl.col("evidence").cast(pl.String),
        ).select(OUTPUT)
        parts.append(side)
    if os.path.exists(old_alloc):
        nonitems = _parquet(old_alloc).filter(pl.col("item_id").is_null())
        if nonitems.height:
            # Standardize nullable columns, retaining T3/T7 audit evidence.
            nonitems = nonitems.select(
                pl.col("bank_txn_id"), pl.col("item_id"), pl.col("amount_cents"), pl.col("category"),
                pl.col("exception"), pl.col("rule"), pl.col("confidence"), pl.col("evidence"))
            parts.append(nonitems)
    allocations = pl.concat(parts, how="vertical_relaxed").with_columns(
        pl.col("amount_cents").cast(pl.Int64, strict=False),
        pl.col("confidence").cast(pl.Float64, strict=False),
        pl.col("rule").cast(pl.String), pl.col("evidence").cast(pl.String),
    )
    # T2 and T3 can overlap if a caller supplied stale artifacts: strongest side link wins.
    allocations = allocations.unique(subset=["bank_txn_id", "item_id"], keep="first", maintain_order=True)
    if allocations.filter(pl.col("rule").is_null() | pl.col("confidence").is_null() | pl.col("evidence").is_null()).height:
        raise ValueError("audit rule/confidence/evidence must be populated on every allocation")
    if allocations.filter(pl.col("confidence").is_not_null() & ((pl.col("confidence") < 0) | (pl.col("confidence") > 1))).height:
        raise ValueError("confidence must be in [0,1]")
    allocations.write_parquet(os.path.join(w, "allocations.parquet"), compression="zstd")
    allocations.select(OUTPUT).write_parquet(os.path.join(w, "audit.parquet"), compression="zstd")
    return {"pairs": pairs.height, "allocations": allocations.height,
            "item_rows": allocations.filter(pl.col("item_id").is_not_null()).height,
            "nonitem_rows": allocations.filter(pl.col("item_id").is_null()).height}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", required=True, choices=("sample", "window", "live", "final"))
    ap.add_argument("--data", help="data root containing masters/")
    args = ap.parse_args()
    print(build(args.phase, args.data))


if __name__ == "__main__":
    main()
