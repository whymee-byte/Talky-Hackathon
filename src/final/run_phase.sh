#!/bin/bash
# End-to-end pipeline for one phase: inputs in data/<phase>/ -> out/<phase>_<ts>_<sha>.csv.gz (validated).
#   src/final/run_phase.sh window        # TRAIN 2024-07 -> 2025-06, scored with the official scorer
#   src/final/run_phase.sh live          # LIVE (no golden)
#   src/final/run_phase.sh final         # FINAL; never uploads (upload is manual, see SUBMISSIONS.md)
# Works offline: no step calls a network service.
set -euo pipefail
PHASE=${1:?phase: sample|window|live|final}
cd "$(dirname "$0")/../.."
PY=${PY:-.venv/bin/python}
export PYTHONDONTWRITEBYTECODE=1
step() { local t=$SECONDS; echo "== $PHASE: $1"; shift; "$@"; echo "   ($((SECONDS - t)) s)"; }
step "base tables"      $PY -u -m src.base_parquet --phase "$PHASE"
step "side files"       $PY -u -m src.sidefiles    --phase "$PHASE"
step "side files AP"    $PY -u -m src.sidefiles_ap --phase "$PHASE"
step "merge side links" $PY -u -m src.sidemerge  --phase "$PHASE"
[[ -n "${NO_REMIT:-}" ]] || step "remittances" $PY -u -m src.remit --phase "$PHASE"
step "party"            $PY -u -m src.party        --phase "$PHASE"
[[ -n "${NO_FUZZ:-}" ]] || step "fuzzy refs" $PY -u -m src.fuzzref --phase "$PHASE"
step "no-item category" $PY -u -m src.nonitem      --phase "$PHASE"
step "matcher"          $PY -u -m src.match        --phase "$PHASE"
step "exceptions"       $PY -u -m src.exceptions   --phase "$PHASE"
step "exception fixes"  $PY -u -m src.postfix     --phase "$PHASE"
if [[ "$PHASE" == "window" || "$PHASE" == "sample" ]]; then
  step "write + score" $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet" --score
else
  step "write"         $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet"
fi
