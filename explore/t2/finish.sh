#!/bin/bash
# T2 close-out: full window build + eval + score, then LIVE build + submission + upload. Needs HK_TOKEN.
set -e
cd "$(dirname "$0")/../.."
echo "== window side_links"; python -u -m src.sidefiles --phase window
echo "== window eval";       python -u -m src.sidefiles.eval --phase window
echo "== window score";      python -u -m src.submit --phase window --score --out out/window_t2_final.csv.gz 2>&1 | grep -E "SCORE|allocation F1|txn accuracy|exc macro"
echo "== live side_links";   python -u -m src.sidefiles --phase live
echo "== live submission"
OUT=$(python -u -m src.submit --phase live 2>&1 | tee /dev/stderr | grep -oE 'out[\\/][^ ]+\.csv\.gz' | head -1)
echo "== upload $OUT"
python -u tools/submit.py "$OUT" --phase live --note "T2 completo" | grep -E '"sub_id"|"status"|"score"|allocation_f1|REFUSED|HTTP' | head -5
echo "== done"
