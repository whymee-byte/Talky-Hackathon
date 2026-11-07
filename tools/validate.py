"""Validate a submission file with the server rules (FORMAT.md §Validation), reporting row numbers.

    python tools/validate.py out/live_….csv.gz --phase live [--data DIR]
    python tools/validate.py sub.csv --txns path/to/bank_transactions.csv.gz   # explicit id source

Exit code 0 = valid, 1 = invalid (errors printed, max 50 like the server), 2 = usage error.
Rules replicated: header with exactly the 5 columns (any order, ';' accepted), ≤ 60 MB uncompressed, ≤ 3 M rows,
bank_txn_id of the phase, no duplicate (txn, item), ≤ 1 no-item row per txn, no mix item/no-item, amount rules,
closed lists for category/exception, item_id pattern. Extra (warning, not error): bank lines of the phase not covered.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common import safe_stdout, EXCEPTIONS, SUBMISSION_COLUMNS, categories, phase_dir, read_csv  # noqa: E402

MAX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_ROWS = 3_000_000
MAX_ERRORS = 50
ITEM_RE = re.compile(r"^(ARI|ARC|API|APC)-\d{4}-\d{7}$")
AMT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def load_phase_ids(phase: str | None, data: str | None, txns: str | None) -> set[str]:
    path = txns or os.path.join(phase_dir(phase, data), "bank_transactions.csv.gz")
    if not os.path.exists(path):
        raise SystemExit(f"phase ids not found: {path} (download the {phase} data, set HK_DATA/--data, or pass --txns)")
    return {r["bank_txn_id"].strip() for r in read_csv(path)}


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif path.lower().endswith(".gz"):
        raise ValueError("file name ends with .gz but the content is not gzip")
    return data


def validate_bytes(data: bytes, phase_ids: set[str], cats: list[str], exceptions: list[str] = EXCEPTIONS) -> dict:
    """Return {"ok", "errors": [{"row", "message"}], "rows", "txns_covered", "txns_missing", "pairs", "noitem_rows"}."""
    errors: list[dict] = []

    def err(row, msg):
        if len(errors) < MAX_ERRORS:
            errors.append({"row": row, "message": msg})

    if not data:
        return {"ok": False, "errors": [{"row": None, "message": "empty file"}]}
    if len(data) > MAX_UNCOMPRESSED:
        return {"ok": False, "errors": [{"row": None, "message": f"file too large: {len(data) / 1e6:.1f} MB uncompressed (limit 60 MB)"}]}
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "errors": [{"row": None, "message": "file is not valid UTF-8"}]}
    stream = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
    first = stream.readline()
    delim = ";" if (first.count(";") >= 4 and first.count(",") < 4) else ","
    header = [h.strip().lower() for h in next(csv.reader([first], delimiter=delim), [])]
    missing = [c for c in SUBMISSION_COLUMNS if c not in header]
    extra = [c for c in header if c and c not in SUBMISSION_COLUMNS]
    if missing or extra:
        msg = (f"missing column(s): {', '.join(missing)}; " if missing else "") + (f"unexpected column(s): {', '.join(extra)}; " if extra else "")
        return {"ok": False, "errors": [{"row": 1, "message": msg + f"expected header: {','.join(SUBMISSION_COLUMNS)}"}]}
    ix = [header.index(c) for c in SUBMISSION_COLUMNS]
    ncol = len(header)
    exc_ok = set(exceptions) | {""}
    cat_ok = set(cats) | {""}
    state: dict[str, list] = {}       # txn -> [n_item_rows, has_noitem]
    seen: set[str] = set()
    n_rows = n_pairs = n_noitem = 0
    for lineno, row in enumerate(csv.reader(stream, delimiter=delim), start=2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        n_rows += 1
        if n_rows > MAX_ROWS:
            return {"ok": False, "errors": [{"row": lineno, "message": f"too many rows (limit {MAX_ROWS:,})"}]}
        if len(row) != ncol:
            err(lineno, f"expected {ncol} fields, found {len(row)}")
            continue
        t, item, amt_s, cat, exc = (row[i].strip() for i in ix)
        cat, exc = cat.upper(), exc.upper()
        if not t:
            err(lineno, "bank_txn_id is empty")
            continue
        if t not in phase_ids:
            err(lineno, f"bank_txn_id {t} is not a bank transaction of this phase")
            continue
        if exc not in exc_ok:
            err(lineno, f"unknown exception '{exc}' (allowed: empty or {', '.join(exceptions)})")
            continue
        if cat not in cat_ok:
            err(lineno, f"unknown category '{cat}' (see categories.csv)")
            continue
        st = state.setdefault(t, [0, False])
        if item:
            if not ITEM_RE.match(item):
                err(lineno, f"item_id '{item}' is not a valid id (e.g. ARI-2025-0123456)")
                continue
            if cat:
                err(lineno, "category must be empty when item_id is given")
                continue
            if delim == ";":
                amt_s = amt_s.replace(",", ".")
            if not AMT_RE.match(amt_s):
                err(lineno, f"invalid amount '{amt_s}' (use a dot as decimal separator, e.g. 1210.00)")
                continue
            amt = float(amt_s)
            if not math.isfinite(amt) or amt < 0 or amt > 1e12:
                err(lineno, f"invalid amount '{amt_s}' (must be a non-negative number in the item currency)")
                continue
            key = t + "|" + item
            if key in seen:
                err(lineno, f"duplicate row for ({t}, {item})")
                continue
            if st[1]:
                err(lineno, f"{t} has both item rows and a no-item (category) row")
                continue
            seen.add(key)
            st[0] += 1
            n_pairs += 1
        else:
            if amt_s:
                err(lineno, "amount must be empty when item_id is empty")
                continue
            if st[1]:
                err(lineno, f"duplicate no-item row for {t}")
                continue
            if st[0]:
                err(lineno, f"{t} has both item rows and a no-item (category) row")
                continue
            st[1] = True
            n_noitem += 1
    if n_rows == 0 and not errors:
        errors.append({"row": None, "message": "no data rows"})
    return {
        "ok": not errors, "errors": errors, "rows": n_rows, "pairs": n_pairs, "noitem_rows": n_noitem,
        "txns_covered": len(state), "txns_total": len(phase_ids), "txns_missing": len(phase_ids) - len(state),
        "uncompressed_mb": round(len(data) / 1e6, 2),
    }


def validate_file(path: str, phase: str | None = None, data: str | None = None, txns: str | None = None) -> dict:
    try:
        raw = read_bytes(path)
    except Exception as ex:
        return {"ok": False, "errors": [{"row": None, "message": str(ex)}]}
    return validate_bytes(raw, load_phase_ids(phase, data, txns), categories(data))


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("submission")
    ap.add_argument("--phase", choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data", help="data root (folder with masters/); default: HK_DATA or data/")
    ap.add_argument("--txns", help="bank_transactions.csv.gz to take the phase ids from (overrides --phase)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args(argv)
    if not a.phase and not a.txns:
        ap.error("--phase or --txns is required")
    res = validate_file(a.submission, a.phase, a.data, a.txns)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        if res["ok"]:
            print(f"OK  {a.submission}: {res['rows']} rows, {res['pairs']} pairs, {res['noitem_rows']} no-item rows, "
                  f"{res['txns_covered']}/{res['txns_total']} txns covered ({res['txns_missing']} missing = scored wrong), "
                  f"{res['uncompressed_mb']} MB")
        else:
            print(f"INVALID  {a.submission}: {len(res['errors'])} error(s) shown (max {MAX_ERRORS})")
            for e in res["errors"]:
                print(f"  row {e['row']}: {e['message']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
