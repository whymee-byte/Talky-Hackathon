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
if [[ "$PHASE" == "final" ]]; then
  # FINAL ships updated masters (new subsidiary ZA01, renamed customers, ZAR rates): overlay them on data/masters
  # (original kept once in data/masters_pre_final) and relearn the party model with them.
  MF=$(ls -d data/final/masters_final data/final/*/masters_final 2>/dev/null | head -1 || true)
  if [[ -n "$MF" ]]; then
    [[ -d data/masters_pre_final ]] || cp -R data/masters data/masters_pre_final
    cp "$MF"/*.csv data/masters/ && echo "== final: masters overlaid from $MF ($(ls "$MF" | wc -l | tr -d ' ') files)"
    rm -rf data_cache/masters
  fi
  PARTY_ARGS="--relearn"
fi
step "base tables"      $PY -u -m src.base_parquet --phase "$PHASE"
step "side files"       $PY -u -m src.sidefiles    --phase "$PHASE"
step "side files AP"    $PY -u -m src.sidefiles_ap --phase "$PHASE"
step "merge side links" $PY -u -m src.sidemerge  --phase "$PHASE"
[[ -n "${NO_REMIT:-}" ]] || step "remittances" $PY -u -m src.remit --phase "$PHASE"
step "party"            $PY -u -m src.party        --phase "$PHASE" ${PARTY_ARGS:-}
[[ -n "${NO_FUZZ:-}" ]] || step "fuzzy refs" $PY -u -m src.fuzzref --phase "$PHASE"
step "no-item category" $PY -u -m src.nonitem      --phase "$PHASE"
step "matcher"          $PY -u -m src.match        --phase "$PHASE"
step "exceptions"       $PY -u -m src.exceptions   --phase "$PHASE"
step "exception fixes"  $PY -u -m src.postfix     --phase "$PHASE"
EXPH=$([[ "$PHASE" == "window" ]] && echo train || echo "$PHASE")
[[ -f "data_cache/$EXPH/pdf_text.parquet" ]] || step "pdf text" $PY -u -m src.pdffacts.extract --phase "$EXPH"
step "pdf facts"        $PY -u -m src.pdffacts --phase "$PHASE"
step "pdf clauses"      $PY -u -m src.docrules --phase "$PHASE"
step "exception norm"   $PY -u -m src.excnorm   --phase "$PHASE"
step "multi-ref receipts" $PY -u -m src.multiref  --phase "$PHASE"
step "unique amount (T14)" $PY -u -m src.refnoise --phase "$PHASE"
step "unidentified"     $PY -u -m src.unident     --phase "$PHASE"
[[ -n "${NO_CNFX:-}" ]] || step "credit notes / FX sets" $PY -u -m src.cnfx --phase "$PHASE"
if [[ "$PHASE" == "window" || "$PHASE" == "sample" ]]; then
  step "write + score" $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet" --score
else
  step "write"         $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet"
fi
