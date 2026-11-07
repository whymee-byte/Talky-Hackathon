"""Score a submission locally with the official scorer (data/starter/bankrec_score.py), with breakdowns.

    python tools/score.py out/sample_….csv.gz --phase sample
    python tools/score.py out/window_….csv.gz --phase window          # TRAIN 2024-07 → 2025-06 (DECISIONS D1)
    python tools/score.py sub.csv.gz --phase train --baseline out/prev.score.json

Writes `<submission>.score.json` next to the file (or --out) with: official result (score, metrics, segments), plus
exception F1 per class and allocation precision/recall per segment. `window` filters train/ by booking_date and caches
the filtered golden + ids in data_cache/window/.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from src.common import safe_stdout, EXCEPTIONS, categories, data_root, in_window, open_text, phase_dir, read_csv  # noqa: E402

sys.path.insert(0, os.path.join(data_root(), "starter"))
import bankrec_score as S  # noqa: E402


def phase_files(phase: str, data: str | None) -> tuple[str, str]:
    """(golden_path, txns_path) for sample/train/window. window → filtered copies in data_cache/window/."""
    d = phase_dir(phase, data)
    golden, txns = os.path.join(d, "golden_allocations.csv.gz"), os.path.join(d, "bank_transactions.csv.gz")
    if phase != "window":
        return golden, txns
    cache = os.path.join(REPO, "data_cache", "window")
    os.makedirs(cache, exist_ok=True)
    g_out, t_out = os.path.join(cache, "golden_allocations.csv.gz"), os.path.join(cache, "bank_txn_ids.csv.gz")
    if not (os.path.exists(g_out) and os.path.exists(t_out)):
        ids = set()
        with open_text(t_out, "wt") as out:
            out.write("bank_txn_id\n")
            for r in read_csv(txns):
                if in_window(r["booking_date"]):
                    ids.add(r["bank_txn_id"])
                    out.write(r["bank_txn_id"] + "\n")
        with open_text(golden) as fh, open_text(g_out, "wt") as out:
            rd = csv.reader(fh)
            w = csv.writer(out, lineterminator="\n")
            h = next(rd)
            w.writerow(h)
            it = h.index("bank_txn_id")
            w.writerows(r for r in rd if r and r[it] in ids)
    return g_out, t_out


def extra_breakdown(golden: S.Golden, data: bytes) -> dict:
    """Per-exception-class F1 and per-segment pair precision/recall (same TP definition as the scorer)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    stream = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
    first = stream.readline()
    delim = ";" if (first.count(";") >= 4 and first.count(",") < 4) else ","
    header = [h.strip().lower() for h in next(csv.reader([first], delimiter=delim))]
    it, ii, ia, ic, ie = (header.index(c) for c in S.REQUIRED)
    tp = {c: 0 for c in golden.exceptions}
    fp = {c: 0 for c in golden.exceptions}
    gold = {c: 0 for c in golden.exceptions}
    seg_sub, seg_tp, seg_gold = {}, {}, {}
    sub_cat_of = {}     # txn -> (cat, exc) for no-item rows
    for row in csv.reader(stream, delimiter=delim):
        if not row or len(row) != len(header):
            continue
        t, item, amt_s, exc = row[it].strip(), row[ii].strip(), row[ia].strip().replace(",", "."), row[ie].strip().upper()
        g = golden.txns.get(t)
        if g is None:
            continue
        seg = g[3]
        if item:
            seg_sub[seg] = seg_sub.get(seg, 0) + 1
            gi = g[0].get(item)
            if gi is not None and amt_s and abs(float(amt_s) - gi[0]) <= S._tol(gi[0]):
                seg_tp[seg] = seg_tp.get(seg, 0) + 1
            if exc:
                if gi is not None and exc == gi[1]:
                    tp[exc] += 1
                else:
                    fp[exc] += 1
        else:
            sub_cat_of[t] = (row[ic].strip().upper(), exc)
            if exc:
                if not g[0] and exc == g[2]:
                    tp[exc] += 1
                else:
                    fp[exc] += 1
    cat_tp, cat_n = {}, {}
    for t, (gitems, gcat, gexc, seg) in golden.txns.items():
        for _, ge in gitems.values():
            if ge in gold:
                gold[ge] += 1
        if gitems:
            seg_gold[seg] = seg_gold.get(seg, 0) + len(gitems)
        else:
            if gexc in gold:
                gold[gexc] += 1
            cat_n[gcat] = cat_n.get(gcat, 0) + 1
            if sub_cat_of.get(t) == (gcat, gexc):
                cat_tp[gcat] = cat_tp.get(gcat, 0) + 1
    exc_f1 = {}
    for c in golden.exceptions:
        if not gold[c]:
            continue
        p = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        r = tp[c] / gold[c]
        exc_f1[c] = {"gold": gold[c], "tp": tp[c], "fp": fp[c], "precision": round(p, 4), "recall": round(r, 4),
                     "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}
    segs = {}
    for seg in sorted(set(seg_gold) | set(seg_sub)):
        sub, hit, gl = seg_sub.get(seg, 0), seg_tp.get(seg, 0), seg_gold.get(seg, 0)
        segs[seg] = {"gold_pairs": gl, "sub_pairs": sub, "tp": hit,
                     "precision": round(hit / sub, 4) if sub else None, "recall": round(hit / gl, 4) if gl else None}
    cats = {c: {"n": n, "accuracy": round(cat_tp.get(c, 0) / n, 4)} for c, n in sorted(cat_n.items(), key=lambda x: -x[1])}
    return {"exception_f1": exc_f1, "segment_pairs": segs, "noitem_categories": cats}


def score_file(path: str, phase: str, data: str | None = None) -> dict:
    golden_p, txns_p = phase_files(phase, data)
    g = S.load_golden(golden_p, txns_p, EXCEPTIONS, categories(data))
    raw = open(path, "rb").read()
    res = S.score_submission(g, raw, path)
    res["phase"] = phase
    res["submission"] = os.path.relpath(path, REPO) if os.path.isabs(path) else path
    if res["ok"]:
        res.update(extra_breakdown(g, raw))
    return res


def print_report(res: dict, baseline: dict | None = None, top: int = 25):
    if not res["ok"]:
        print("INVALID:")
        for e in res["errors"]:
            print(f"  row {e['row']}: {e['message']}")
        return
    m = res["metrics"]
    b = (baseline or {}).get("metrics", {})

    def d(key, scale=100):
        return f" ({(m[key] - b[key]) * scale:+.2f})" if key in b else ""

    bs = f" (baseline {baseline['score']:.2f}, {res['score'] - baseline['score']:+.2f})" if baseline and baseline.get("score") is not None else ""
    print(f"[{res['phase']}] SCORE {res['score']:.2f}{bs}")
    print(f"  allocation F1 {m['allocation_f1'] * 100:6.2f}{d('allocation_f1')}  (precision {m['precision'] * 100:.2f}, recall {m['recall'] * 100:.2f})")
    print(f"  txn accuracy  {m['txn_accuracy'] * 100:6.2f}{d('txn_accuracy')}  (amount-weighted {m['amount_weighted_accuracy'] * 100:.2f})")
    print(f"  exc macro-F1  {m['exception_macro_f1'] * 100:6.2f}{d('exception_macro_f1')}")
    print(f"  rows {m['rows']}, txns covered {m['txns_covered']}/{m['txns_total']}")
    print("  exception F1 per class:")
    for c, v in sorted(res["exception_f1"].items(), key=lambda x: x[1]["f1"]):
        print(f"    {c:19s} gold={v['gold']:7d} tp={v['tp']:7d} fp={v['fp']:7d}  P={v['precision']:.3f} R={v['recall']:.3f} F1={v['f1']:.3f}")
    print(f"  segments (by lines, top {top}): n, txn accuracy, pair P/R")
    sp = res["segment_pairs"]
    for k, v in sorted(res["segments"].items(), key=lambda x: -x[1]["n"])[:top]:
        s = sp.get(k, {})
        p = f"{s['precision']:.3f}" if s.get("precision") is not None else "  -  "
        r = f"{s['recall']:.3f}" if s.get("recall") is not None else "  -  "
        print(f"    {k:28s} n={v['n']:7d} acc={v['accuracy']:.3f}  pairs gold={s.get('gold_pairs', 0):7d} P={p} R={r}")


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("submission")
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window"])
    ap.add_argument("--data", help="data root (folder with masters/)")
    ap.add_argument("--out", help="where to write the score json (default: <submission>.score.json)")
    ap.add_argument("--baseline", help="previous .score.json to show deltas against")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args(argv)
    res = score_file(a.submission, a.phase, a.data)
    out = a.out or (a.submission[:-len(".csv.gz")] if a.submission.endswith(".csv.gz") else os.path.splitext(a.submission)[0]) + ".score.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    baseline = json.load(open(a.baseline, encoding="utf-8")) if a.baseline else None
    print_report(res, baseline, a.top)
    print(f"  -> {out}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
