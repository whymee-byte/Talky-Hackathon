#!/usr/bin/env bash
# T9 · pipeline de punta a punta, SIN RED:  src/final/run_final.sh <sample|window|live|final>
#
#   base_parquet (T1) → sidefiles (T2) → [sidefiles_ap T2b] → party (T4) → [remit T6] → [nonitem T7]
#   → match (T3) → [exceptions T5 si FINAL_T5=1] → submit → tools/validate.py → informe por mes (src/final/report.py)
#
# Pasos entre corchetes: opcionales. Se ejecutan si el módulo existe y, si fallan, NO abortan (el matcher funciona
# sin sus ficheros; un patrón nuevo sin caché → categoría por defecto según signo: cobro UNIDENTIFIED, pago BANK_FEES).
# Los imprescindibles (T1, T2, T4, T3, submit, validate) abortan con código ≠ 0.
#
# Variables:
#   FINAL_ALLOW_NET=1  no corta la red (por defecto: HACKATHON_API_KEY vacío + proxy muerto en 127.0.0.1:9).
#   FINAL_CLEAN=1      borra work/<fase>/*.parquet antes de empezar (por defecto sólo en final; en live/window se
#                      reutiliza lo que haya para no pisar a quien esté trabajando en ese directorio).
#   FINAL_T5=1         ejecuta src.exceptions encima de pairs_raw (apagado: en la ventana baja la nota, ver SUBMISSIONS.md).
#   FINAL_T8=1         ejecuta src.pdffacts (el matcher no lo lee todavía).
#   HK_DATA            raíz de datos (carpeta con masters/). Si existe <raíz>/final/masters_final/, se monta una raíz
#                      work/final/data_root con masters_final/ por delante de masters/ (D9) y se exporta HK_DATA.
#
# Salidas: out/<fase>_<yyyymmdd-hhmm>_<sha>.csv.gz, work/<fase>/final_times.tsv (tiempo por paso),
#          work/<fase>/final_run.log (salida completa de cada paso), work/<fase>/final_report.md (informe por mes).
# Nunca sube nada: la subida FINAL la hace a mano el responsable con tools/submit.py --confirm (docs/FINAL_CHECKLIST.md).
set -uo pipefail

PHASE=${1:?uso: src/final/run_final.sh <sample|window|live|final>}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
PY=${PY:-"$REPO/.venv/bin/python"}
[[ -x "$PY" ]] || PY=python3
W="work/$PHASE"
mkdir -p "$W" out
LOG="$W/final_run.log"
TIMES="$W/final_times.tsv"
: > "$LOG"
printf 'paso\testado\tsegundos\n' > "$TIMES"
T_ALL=$(date +%s)
FAILED_OPT=()

say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

# ---------- sin red ----------
if [[ "${FINAL_ALLOW_NET:-0}" != 1 ]]; then
  export HACKATHON_API_KEY="" HACKATHON_OFFLINE=1
  unset ANTHROPIC_API_KEY
  export http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9
  export HTTP_PROXY=$http_proxy HTTPS_PROXY=$https_proxy ALL_PROXY=$all_proxy no_proxy="" NO_PROXY=""
  say "red cortada: HACKATHON_API_KEY vacío, proxy muerto 127.0.0.1:9 (FINAL_ALLOW_NET=1 para permitirla)"
fi

# ---------- raíz de datos y masters_final (D9) ----------
DATA=${HK_DATA:-}
if [[ -z "$DATA" ]]; then
  for c in "$REPO/data/bankrec-docs-and-masters" "$REPO/data"; do [[ -d "$c/masters" ]] && { DATA=$c; break; }; done
fi
[[ -n "$DATA" && -d "$DATA/masters" ]] || { say "ERROR: no encuentro la raíz de datos (carpeta con masters/); exporta HK_DATA"; exit 2; }
PDIR="$DATA/$PHASE"; [[ "$PHASE" == window ]] && PDIR="$DATA/train"
[[ -f "$PDIR/bank_transactions.csv.gz" ]] || { say "ERROR: faltan los datos de la fase: $PDIR/bank_transactions.csv.gz"; exit 2; }
if [[ "$PHASE" == final && -d "$DATA/final/masters_final" ]]; then
  ROOT="$REPO/work/final/data_root"
  rm -rf "$ROOT"; mkdir -p "$ROOT/masters"
  for d in "$DATA"/*/; do d=${d%/}; [[ "$(basename "$d")" == masters ]] && continue; ln -s "$d" "$ROOT/$(basename "$d")"; done
  cp "$DATA"/masters/* "$ROOT/masters/"             # maestros a 2025-12-31 …
  cp "$DATA"/final/masters_final/* "$ROOT/masters/"  # … pisados por los de 2026-06-30 (ZA01, renombres)
  export HK_DATA="$ROOT"
  say "masters_final/ encontrado: HK_DATA=$ROOT (masters_final por delante de masters; $(ls "$DATA/final/masters_final" | tr '\n' ' '))"
else
  export HK_DATA="$DATA"
fi

# ---------- limpieza de intermedios ----------
CLEAN=${FINAL_CLEAN:-}; [[ -z "$CLEAN" && "$PHASE" == final ]] && CLEAN=1
if [[ "$CLEAN" == 1 ]]; then
  n=$(ls "$W"/*.parquet 2>/dev/null | wc -l | tr -d ' ')
  rm -f "$W"/*.parquet
  say "limpieza: $n parquet borrados de $W (ejecución desde cero)"
else
  say "reutilizo lo que haya en $W (FINAL_CLEAN=1 para empezar de cero)"
fi

# ---------- ejecutar un paso ----------
# step <nombre> <req|opt> <cmd…>
step() {
  local name=$1 kind=$2; shift 2
  local t0 rc dt
  say "== $name ($kind): $*"
  t0=$(date +%s)
  "$@" >> "$LOG" 2>&1; rc=$?
  dt=$(( $(date +%s) - t0 ))
  tail -n 3 "$LOG" | sed 's/^/   | /'
  if [[ $rc -eq 0 ]]; then
    printf '%s\tok\t%s\n' "$name" "$dt" >> "$TIMES"; say "   ok en ${dt}s"
  else
    printf '%s\tFAIL(%s)\t%s\n' "$name" "$rc" "$dt" >> "$TIMES"
    if [[ $kind == req ]]; then say "   FALLO (rc=$rc) en paso imprescindible $name: abortando. Log: $LOG"; exit 1; fi
    say "   FALLO (rc=$rc) en paso opcional $name: sigo sin él"
    FAILED_OPT+=("$name")
  fi
}
have() { [[ -f "src/$1/__main__.py" ]]; }

# ---------- pipeline ----------
step T1_base_parquet req "$PY" -m src.base_parquet --phase "$PHASE" --data "$HK_DATA"
step T2_sidefiles    req "$PY" -m src.sidefiles    --phase "$PHASE" --data "$HK_DATA"
if have sidefiles_ap; then step T2b_sidefiles_ap opt "$PY" -m src.sidefiles_ap --phase "$PHASE" --data "$HK_DATA"; fi
if [[ "$PHASE" == final ]]; then
  step T4_party req "$PY" -m src.party --phase "$PHASE" --relearn      # aprende con train + live + final
else
  step T4_party req "$PY" -m src.party --phase "$PHASE"
fi
if have remit;   then step T6_remit   opt "$PY" -m src.remit   --phase "$PHASE"; fi
if have nonitem; then step T7_nonitem opt "$PY" -m src.nonitem --phase "$PHASE"; fi
if [[ "${FINAL_T8:-0}" == 1 ]] && have pdffacts; then step T8_pdffacts opt "$PY" -m src.pdffacts --phase "$PHASE"; fi
step T3_match req "$PY" -m src.match --phase "$PHASE"
if [[ "${FINAL_T5:-0}" == 1 ]] && have exceptions; then step T5_exceptions opt "$PY" -m src.exceptions --phase "$PHASE" --data "$HK_DATA"; fi

SHA=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo nogit)
OUT="out/${PHASE}_$(date +%Y%m%d-%H%M)_${SHA}.csv.gz"
step submit   req "$PY" -m src.submit --phase "$PHASE" --data "$HK_DATA" --from "$W/allocations.parquet" --out "$OUT"
step validate req "$PY" tools/validate.py "$OUT" --phase "$PHASE" --data "$HK_DATA"
step report   opt "$PY" -m src.final.report --phase "$PHASE" --submission "$OUT" --data "$HK_DATA"

# ---------- resumen ----------
DT=$(( $(date +%s) - T_ALL ))
say "=============================================================="
say "FICHERO VALIDADO: $OUT   (sha $SHA, total $((DT / 60)) min $((DT % 60)) s)"
say "tiempos por paso: $TIMES"
column -t -s $'\t' "$TIMES" | sed 's/^/   /'
if [[ ${#FAILED_OPT[@]} -gt 0 ]]; then say "AVISO: pasos opcionales fallidos: ${FAILED_OPT[*]} (ver $LOG)"; fi
[[ -f "$W/final_report.md" ]] && say "informe por mes: $W/final_report.md"
if [[ "$PHASE" == final ]]; then
  say "NO SE HA SUBIDO NADA. Subida (sólo el responsable de FINAL, tras la checklist):"
  say "   HK_TOKEN=… $PY tools/submit.py $OUT --phase final --confirm javier --note '…'"
fi
