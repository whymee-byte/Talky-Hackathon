#!/bin/bash
# Re-score the window re-running only the stages downstream of what changed; the rest reuses work/window/*.parquet.
#   tools/quick_window.sh <from> [tag]
#     from = sidefiles   T2 changed              (reuses party, sidefiles_ap, nonitem)
#            sidefiles_ap T2b changed            (reuses party, nonitem)
#            remit       T6 changed
#            fuzzref     T3f changed
#            match       T3 changed
#            exceptions  T5/T5b changed
#            post        a post-matcher step changed (postfix … refnoise … unident)
#     tag  = name of the output (out/window_quick_<tag>.csv.gz + .score.json), default: <from>
# Needs one full run before (src/final/run_phase.sh window), and compares against a baseline measured in the same
# state: the 1st run from scratch scores lower than the following ones (see TASKS.md T9).
set -euo pipefail
FROM=${1:?from: sidefiles|sidefiles_ap|remit|fuzzref|match|exceptions|post}
TAG=${2:-$FROM}
cd "$(dirname "$0")/.."
PY=${PY:-python}
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
for f in work/window/line_party.parquet work/window/line_category.parquet work/window/side_links_ap.parquet; do
  [[ -f $f ]] || { echo "missing $f: run src/final/run_phase.sh window once"; exit 1; }
done
# post-matcher steps, same order as src/final/run_phase.sh (refnoise = T14)
POST="postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix"
case $FROM in
  sidefiles)    STAGES="sidefiles sidemerge remit fuzzref match exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  sidefiles_ap) STAGES="sidefiles sidefiles_ap sidemerge remit fuzzref match exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  remit)        STAGES="remit fuzzref match exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  fuzzref)      STAGES="fuzzref match exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  match)        STAGES="match exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  exceptions)   STAGES="exceptions postfix pdffacts docrules excnorm multiref refnoise cnlink mixfix instrep discombo twopay unident cnfx duefill tolmatch cnmore refgap cnrest cnfill fxlate excfix" ;;
  post)         STAGES="$POST" ;;
  *) echo "unknown stage $FROM"; exit 1 ;;
esac
T0=$SECONDS
for s in $STAGES; do
  [[ $s == refnoise && -n ${NO_REFNOISE:-} ]] && continue   # NO_REFNOISE=1: baseline without T14
  t=$SECONDS
  $PY -u -m src.$s --phase window > work/window/quick_$s.log 2>&1 || { echo "FAIL $s"; tail -15 work/window/quick_$s.log; exit 1; }
  printf "  %-12s %4d s\n" "$s" $((SECONDS - t))
done
t=$SECONDS
$PY -u -m src.submit --phase window --from work/window/allocations.parquet --score --out "out/window_quick_$TAG.csv.gz" 2>&1 \
  | grep -E "SCORE|allocation F1|txn accuracy|exc macro"
printf "  %-12s %4d s\n  total        %4d s\n" "score" $((SECONDS - t)) $((SECONDS - T0))
