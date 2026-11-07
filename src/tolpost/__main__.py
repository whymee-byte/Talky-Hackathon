"""T3g post-steps after src.excfix (one module so run_phase.sh, run_final.sh and quick_window.sh call it alike):
  1. overfix  OVERPAYMENT whose excess is not a whole amount nor 1 % / 10 % of the item -> no exception (src.tolmatch)
  2. T3u      learned UNIDENTIFIED flag for no-item receipts left without it (src.tolmatch.unid, versioned table)

    python -m src.tolpost --phase window|live|final
"""
import argparse

from src.tolmatch.__main__ import overfix
from src.tolmatch.unid import apply

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    a = ap.parse_args()
    overfix(a.phase)
    apply(a.phase)
