"""T4 · identificación de tercero.

    python -m src.party --phase window     # aprende con TRAIN < 2024-07, resuelve la ventana y la mide
    python -m src.party --phase live       # aprende con todo TRAIN, resuelve LIVE
    python -m src.party --phase final      # idem para FINAL (lee final/ si existe)

Salida: work/<fase>/line_party.parquet (contracts/schemas.md §4).
"""
import argparse
import os
import time

from . import evaluate, learn, lines, resolve, truth
from .common import RAW, connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    ap.add_argument("--no-fuzzy", action="store_true")
    ap.add_argument("--relearn", action="store_true")
    a = ap.parse_args()
    os.environ["HK_PHASE"] = a.phase
    t0 = time.time()
    con = connect()
    need = ["learn", "window", a.phase] if a.phase != "window" else ["learn", "window"]
    for ph in dict.fromkeys(need):
        if not os.path.exists(f"work/{ph}/party_lines.parquet"):
            lines.build(ph, con)
    if not os.path.exists("work/train/party_truth.parquet"):
        truth.build(con)
    model = "pre_window" if a.phase == "window" else "full"
    if a.relearn or not os.path.exists(f"work/party_model/{model}/learned_keys.parquet"):
        notices = ("train",) if model == "pre_window" else tuple(
            p for p in ("train", "live", "final") if os.path.isdir(f"{RAW}/{p}")
        )
        learn.build(model, con, phases_for_notices=notices)
    con.close()
    out = resolve.run(a.phase, model, fuzzy=not a.no_fuzzy)
    print(f"{out} en {time.time() - t0:.0f}s")
    if a.phase == "window":
        from . import calibrate
        # Calibrate only from independent, pre-window predictions and labels.
        # Never fit on the window output that is subsequently evaluated.
        if not os.path.exists("work/train/line_party.parquet"):
            resolve.run("train", "pre_window", fuzzy=not a.no_fuzzy)
        calibrate.fit("work/train/line_party.parquet")
        calibrate.apply("window")
        evaluate.evaluate()


if __name__ == "__main__":
    main()
