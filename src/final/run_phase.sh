#!/bin/bash
# End-to-end pipeline for one phase: inputs in data/<phase>/ -> out/<phase>_<ts>_<sha>.csv.gz (validated).
#   src/final/run_phase.sh window        # TRAIN 2024-07 -> 2025-06, scored with the official scorer
#   src/final/run_phase.sh live          # LIVE (no golden)
#   src/final/run_phase.sh final         # FINAL; never uploads (upload is manual, see SUBMISSIONS.md)
# Works offline: no step calls a network service.
set -euo pipefail
PHASE=${1:?phase: sample|window|live|final}
cd "$(dirname "$0")/../.."
# Python: $PY if it really runs, else the first candidate that does (on Windows `python3` can be the Store alias).
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }
PY_FOUND=""
for c in "${PY:-}" .venv/bin/python .venv/Scripts/python.exe python3 python py; do
  [[ -n "$c" ]] && py_ok "$c" && { PY_FOUND=$c; break; }
done
[[ -n "$PY_FOUND" ]] || { echo "ERROR: no Python >= 3.10 found (set PY=/path/to/python)"; exit 2; }
PY=$PY_FOUND
echo "== python: $PY ($($PY -c 'import sys; print(sys.version.split()[0])'))"
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
step "credit-note netting" $PY -u -m src.cnlink --phase "$PHASE"
step "stray items + rest fill (T13c)" $PY -u -m src.mixfix --phase "$PHASE"
step "repeated instalments (T17)" $PY -u -m src.instrep --phase "$PHASE"
step "discount subsets (T18)" $PY -u -m src.discombo --phase "$PHASE"
step "two receipts (T13d)" $PY -u -m src.twopay --phase "$PHASE"
step "unidentified"     $PY -u -m src.unident     --phase "$PHASE"
[[ -n "${NO_CNFX:-}" ]] || step "credit notes / FX sets" $PY -u -m src.cnfx --phase "$PHASE"
step "due invoices on long receipts (T20)" $PY -u -m src.duefill --phase "$PHASE"
[[ -n "${NO_T3G:-}" ]] || step "tolerant matches (T3g)" $PY -u -m src.tolmatch --phase "$PHASE"
step "more credit notes (T19)" $PY -u -m src.cnmore --phase "$PHASE"
step "payer subset gap (T16)" $PY -u -m src.refgap --phase "$PHASE"
step "credit-note units (T16)" $PY -u -m src.cnrest --phase "$PHASE"
step "credit-note runs (T21)" $PY -u -m src.cnfill --phase "$PHASE"
step "late discount / FX orig (T21)" $PY -u -m src.fxlate --phase "$PHASE"
step "last exception fixes (T15)" $PY -u -m src.excfix --phase "$PHASE"
if [[ "$PHASE" == "window" || "$PHASE" == "sample" ]]; then
  step "write + score" $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet" --score
else
  step "write"         $PY -u -m src.submit --phase "$PHASE" --from "work/$PHASE/allocations.parquet"
fi
