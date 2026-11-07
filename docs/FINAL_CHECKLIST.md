# FINAL checklist (one submission only; tie goes to the earliest)

Data at 18:00 Madrid; target upload before 18:30; submissions close 19:30.

1. Download `bankrec-final.tar.gz` from the `/datos` page and extract it so that the folder is `data/final/`
   (`cd data && tar -xzf ~/Downloads/bankrec-final.tar.gz`). Check `data/final/bank_transactions.csv.gz` exists and
   note whether `data/final/masters_final/` came with it.
2. Run everything, offline, one command:

   ```bash
   src/final/run_phase.sh final > logs/final.log 2>&1; tail -5 logs/final.log
   ```

   - overlays `masters_final/*.csv` onto `data/masters/` (original kept in `data/masters_pre_final/`) and relearns T4;
   - writes `out/final_<ts>_<sha>.csv.gz` and validates it (`valid: N/N txns covered, 0 missing`).
   - Rehearsal (LIVE copied as FINAL, 2026-11-07 14:15): every step ran end to end in ≈ 2 min.
3. Sanity: rows ≤ 3 M, file ≤ 60 MB uncompressed, 0 missing lines, share of `UNIDENTIFIED` and of lines with items
   similar to LIVE (`python -m src.diag` or a duckdb count on `work/final/allocations.parquet`).
4. **Ask the FINAL owner (javier) for the OK**, add a `PROPUESTO` row to `SUBMISSIONS.md` with the window and LIVE
   score of the commit, then upload:

   ```bash
   .venv/bin/python tools/submit.py out/final_<…>.csv.gz --phase final --confirm javier
   ```

   The token is read from the git-ignored `.env` (`TALKY_TOKEN`).
