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
    if a.phase != "window":
        # The calibration used to be fitted only by the window run. A clean checkout that runs live/final directly had
        # none, resolve.run silently skipped it and every downstream threshold (matcher 0.3, comb/T3g/unident 0.5,
        # T14 0.2) saw the raw, much lower confidences: LIVE from scratch scored 2.2 points less. Fit it here, from the
        # same independent pre-window predictions the window run uses (TRAIN only, no LIVE/FINAL labels exist).
        from . import calibrate
        if not os.path.exists(calibrate.CAL):
            print(f"WARNING: {calibrate.CAL} missing -> fitting the T4 calibration from pre-window TRAIN predictions",
                  flush=True)
            if not os.path.exists("work/party_model/pre_window/learned_keys.parquet"):
                learn.build("pre_window", con, phases_for_notices=("train",))
            con.close()
            resolve.run("learn", "pre_window", fuzzy=not a.no_fuzzy, apply_calibration=False)
            calibrate.fit("work/learn/line_party.parquet")
            con = connect()
        print(f"T4 calibration: {calibrate.CAL}", flush=True)
    con.close()
    out = resolve.run(a.phase, model, fuzzy=not a.no_fuzzy)
    print(f"{out} en {time.time() - t0:.0f}s")
    if a.phase == "window":
        from . import calibrate
        # Calibrate only from independent, pre-window predictions and labels.
        # Never fit on the window output that is subsequently evaluated.
        resolve.run("learn", "pre_window", fuzzy=not a.no_fuzzy, apply_calibration=False)
        calibrate.fit("work/learn/line_party.parquet")
        calibrate.apply("window")
        evaluate.evaluate()


if __name__ == "__main__":
    main()
