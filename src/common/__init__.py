"""Shared helpers for all modules (stdlib only): data root, phase folders, closed lists, csv helpers."""
from __future__ import annotations

import csv
import gzip
import io
import os
import subprocess
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PHASES = ("sample", "train", "window", "live", "final")
# items visible in a phase = everything issued up to and including it
PHASE_ITEM_SOURCES = {
    "sample": ["sample"],
    "train": ["train"],
    "window": ["train"],
    "live": ["train", "live"],
    "final": ["train", "live", "final"],
}
# the TRAIN window used for honest local measurement (DECISIONS D1)
WINDOW_START, WINDOW_END = date(2024, 7, 1), date(2025, 6, 30)

EXCEPTIONS = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
              "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]
SUBMISSION_COLUMNS = ["bank_txn_id", "item_id", "amount", "category", "exception"]


def data_root(explicit: str | None = None) -> str:
    """Folder that contains masters/, sample/, train/, live/… (env HK_DATA > data/bankrec-docs-and-masters > data)."""
    for cand in (explicit, os.environ.get("HK_DATA"), os.path.join(REPO, "data", "bankrec-docs-and-masters"),
                 os.path.join(REPO, "data")):
        if cand and os.path.isdir(os.path.join(cand, "masters")):
            return cand
    raise FileNotFoundError("data root not found: set HK_DATA or pass --data (folder containing masters/)")


def phase_dir(phase: str, root: str | None = None) -> str:
    """Folder with the phase inputs. `window` reads from train/."""
    return os.path.join(data_root(root), "train" if phase == "window" else phase)


def categories(root: str | None = None) -> list[str]:
    with open(os.path.join(data_root(root), "masters", "categories.csv"), encoding="utf-8-sig", newline="") as fh:
        return [r["category"] for r in csv.DictReader(fh)]


def open_text(path: str, mode: str = "rt"):
    if path.endswith(".gz"):
        return gzip.open(path, mode, encoding="utf-8-sig" if "r" in mode else "utf-8", newline="")
    return open(path, mode, encoding="utf-8-sig" if "r" in mode else "utf-8", newline="")


def read_csv(path: str):
    """Yield dict rows from a (gzipped) csv."""
    with open_text(path) as fh:
        yield from csv.DictReader(fh)


def cents(s: str | None) -> int:
    return int(round(float(s) * 100)) if s else 0


def cents_to_text(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(int(c))
    return f"{sign}{c // 100}.{c % 100:02d}"


def git_sha(short: bool = True) -> str:
    try:
        cmd = ["git", "rev-parse"] + (["--short"] if short else []) + ["HEAD"]
        out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def safe_stdout() -> None:
    """Windows consoles may be cp1252: never crash on a non-ASCII character in a report."""
    import sys
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def in_window(d: str) -> bool:
    return WINDOW_START.isoformat() <= d[:10] <= WINDOW_END.isoformat()
