"""Shared helpers for T4 (party identification): paths, duckdb connection, SQL macros."""
import os

import duckdb

RAW = "data/bankrec-docs-and-masters"
CACHE = "data_cache"
WORK = "work"
WINDOW_START = "2024-07-01"     # honest evaluation window: TRAIN 2024-07 -> 2025-06
KEY_LENGTHS = (10, 16, 24)      # prefix lengths for name keys (N43 fields truncate at 16/38, MT940 at 35/65)
SHORT_MIN = 6                   # names shorter than 10 chars are matched whole, if >= 6 chars


def connect(mem="4GB"):
    con = duckdb.connect()
    os.makedirs(f"{WORK}/tmp", exist_ok=True)
    con.execute(f"SET memory_limit='{mem}'; SET temp_directory='{WORK}/tmp'; SET preserve_insertion_order=false; SET enable_progress_bar=false")
    # norm(): upper, no accents, only [A-Z0-9 ], collapsed spaces (contracts/schemas.md, *_norm)
    con.execute(r"""CREATE MACRO norm(s) AS
        trim(regexp_replace(upper(strip_accents(coalesce(s, ''))), '[^A-Z0-9]+', ' ', 'g'))""")
    return con


def src(phase, table):
    """Path to an official input table, Parquet cache first (explore/profile.py), else the raw csv.gz."""
    base = "train" if phase == "window" else phase
    pq = f"{CACHE}/{base}/{table}.parquet"
    if os.path.exists(pq):
        return f"read_parquet('{pq}')"
    return f"read_csv('{RAW}/{base}/{table}.csv.gz', sample_size=-1)"


def master(name):
    pq = f"{CACHE}/masters/{name}.parquet"
    if os.path.exists(pq):
        return f"read_parquet('{pq}')"
    return f"read_csv('{RAW}/masters/{name}.csv', sample_size=-1)"


def out_dir(phase):
    d = f"{WORK}/{phase}"
    os.makedirs(d, exist_ok=True)
    return d
