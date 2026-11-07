"""Write a valid submission for a phase.

    python -m src.submit --phase sample                 # trivial file (or from work/sample/allocations.parquet if present)
    python -m src.submit --phase live --trivial         # force the trivial baseline
    python -m src.submit --phase window --from work/window/allocations.parquet --score

Output: out/<phase>_<yyyymmdd-hhmm>_<sha>.csv.gz (+ .score.json with --score, phases with golden only).
The file is always validated with tools/validate.py before returning (exit 1 if invalid).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict

from src.common import (PHASE_ITEM_SOURCES, REPO, SUBMISSION_COLUMNS, cents, cents_to_text, data_root, git_sha,
                        in_window, open_text, phase_dir, read_csv, safe_stdout)

RULE_EXACT = "T0.exact_docnum_total"
RULE_FALLBACK_IN = "T0.fallback_unidentified"
RULE_FALLBACK_OUT = "T0.fallback_bank_fees"

ITEM_TABLES = (("ar_invoices", "document_number"), ("ap_invoices", "supplier_invoice_number"), ("credit_notes", "document_number"))


def norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def iter_lines(phase: str, data: str | None):
    """Bank lines of the phase (window = train filtered by booking_date)."""
    for r in read_csv(os.path.join(phase_dir(phase, data), "bank_transactions.csv.gz")):
        if phase != "window" or in_window(r["booking_date"]):
            yield r


def load_items(phase: str, data: str | None):
    """item_id -> total_cents and normalised doc number -> [item_id] for all items visible in the phase."""
    root = data_root(data)
    totals, by_number = {}, defaultdict(list)
    for src in PHASE_ITEM_SOURCES[phase]:
        for name, numcol in ITEM_TABLES:
            p = os.path.join(root, src, f"{name}.csv.gz")
            if not os.path.exists(p):
                continue
            for r in read_csv(p):
                totals[r["item_id"]] = cents(r["total_amount"])
                by_number[norm(r[numcol])].append(r["item_id"])
    oi = os.path.join(phase_dir(phase, data), "open_items_at_start.csv.gz")
    if os.path.exists(oi):
        for r in read_csv(oi):
            totals.setdefault(r["item_id"], cents(r["total_amount"]))
            by_number[norm(r["document_number"])].append(r["item_id"])
    return totals, by_number


def trivial_rows(phase: str, data: str | None):
    """Yield (bank_txn_id, item_id, amount_text, category, exception, rule) for every line of the phase."""
    totals, by_number = load_items(phase, data)
    used = set()
    for t in iter_lines(phase, data):
        amt = cents(t["amount"])
        text = " ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]]).upper()
        hit = None
        for tok in re.split(r"[\s|,;/]+", text):
            if len(tok) < 5:
                continue
            for iid in by_number.get(norm(tok), ()):
                if iid not in used and totals[iid] == abs(amt):
                    hit = iid
                    break
            if hit:
                break
        if hit:
            used.add(hit)
            yield t["bank_txn_id"], hit, cents_to_text(abs(amt)), "", "", RULE_EXACT
        elif amt > 0:
            yield t["bank_txn_id"], "", "", "UNIDENTIFIED", "UNIDENTIFIED", RULE_FALLBACK_IN
        else:
            yield t["bank_txn_id"], "", "", "BANK_FEES", "", RULE_FALLBACK_OUT


def allocation_rows(path: str):
    """Yield submission tuples from an allocations.parquet (§8). Needs polars or pyarrow."""
    try:
        import polars as pl
        df = pl.read_parquet(path)
        cols = {c: df[c].to_list() for c in ("bank_txn_id", "item_id", "amount_cents", "category", "exception") if c in df.columns}
    except ImportError:
        import pyarrow.parquet as pq
        tb = pq.read_table(path)
        cols = {c: tb.column(c).to_pylist() for c in ("bank_txn_id", "item_id", "amount_cents", "category", "exception") if c in tb.column_names}
    n = len(cols["bank_txn_id"])
    get = lambda c, i: (cols[c][i] if c in cols else None)  # noqa: E731
    for i in range(n):
        item = get("item_id", i) or ""
        amt = get("amount_cents", i)
        yield (cols["bank_txn_id"][i], item, cents_to_text(amt) if item and amt is not None else "",
               (get("category", i) or "") if not item else "", get("exception", i) or "", "allocations")


def write_submission(rows, out_path: str) -> dict:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    n = n_pairs = 0
    rules = defaultdict(int)
    with open_text(out_path, "wt") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SUBMISSION_COLUMNS)
        for t, item, amt, cat, exc, rule in rows:
            w.writerow([t, item, amt, cat, exc])
            n += 1
            n_pairs += bool(item)
            rules[rule] += 1
    return {"rows": n, "pairs": n_pairs, "rules": dict(rules)}


def default_out(phase: str) -> str:
    return os.path.join(REPO, "out", f"{phase}_{time.strftime('%Y%m%d-%H%M')}_{git_sha()}.csv.gz")


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data", help="data root (folder with masters/)")
    ap.add_argument("--from", dest="src", help="allocations.parquet to serialise (default: work/<phase>/allocations.parquet if it exists)")
    ap.add_argument("--trivial", action="store_true", help="ignore allocations.parquet and write the trivial baseline")
    ap.add_argument("--out", help="output file (default out/<phase>_<timestamp>_<sha>.csv.gz)")
    ap.add_argument("--score", action="store_true", help="also run tools/score.py (sample/train/window)")
    a = ap.parse_args(argv)

    src = a.src or os.path.join(REPO, "work", a.phase, "allocations.parquet")
    use_alloc = not a.trivial and os.path.exists(src)
    out = a.out or default_out(a.phase)
    t0 = time.time()
    stats = write_submission(allocation_rows(src) if use_alloc else trivial_rows(a.phase, a.data), out)
    print(f"{'allocations ' + src if use_alloc else 'trivial baseline'} -> {out}")
    print(f"  {stats['rows']} rows, {stats['pairs']} pairs, rules {stats['rules']}  ({time.time() - t0:.1f}s)")

    sys.path.insert(0, os.path.join(REPO, "tools"))
    import validate  # noqa: E402
    res = validate.validate_file(out, a.phase, a.data)
    if not res["ok"]:
        print("INVALID submission:")
        for e in res["errors"]:
            print(f"  row {e['row']}: {e['message']}")
        return 1
    print(f"  valid: {res['txns_covered']}/{res['txns_total']} txns covered, {res['txns_missing']} missing")
    if a.score:
        if a.phase not in ("sample", "train", "window"):
            print("  (no golden for this phase: skipping score)")
        else:
            import score  # noqa: E402
            r = score.score_file(out, a.phase, a.data)
            sj = out[:-len(".csv.gz")] + ".score.json"
            import json
            json.dump(r, open(sj, "w", encoding="utf-8"), indent=1)
            score.print_report(r)
            print(f"  -> {sj}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
