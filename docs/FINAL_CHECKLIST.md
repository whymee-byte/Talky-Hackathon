# Checklist de FINAL (T9)

**Responsable del único envío FINAL: javier.** Nadie más sube a FINAL. El empate lo gana el envío más temprano:
objetivo **FINAL validado y subido antes de las 18:30**. Un único envío: un fallo tardío no se recupera (D10).

## Horario

| Hora | Qué | Quién |
|---|---|---|
| 17:00 | Simulacro: `src/final/run_final.sh live` con la red cortada (es lo que hace el script por defecto). Comparar nota LIVE del fichero con la última de `SUBMISSIONS.md`. | javier |
| 17:30 | **Congelación de `main`**: sólo arreglos de fallos. `git pull --rebase` y `git log` limpio; nadie deja ficheros sin commit en `src/`. | todos |
| 17:30–18:00 | Repetir el simulacro con el `main` congelado si hubo commits después de las 17:00. Anotar tiempos. | javier |
| 18:00 | Llegan los datos FINAL: descomprimir en `data/bankrec-docs-and-masters/final/` (`bank_transactions.csv.gz`, `open_items_at_start.csv.gz`, ficheros de detalle, `masters_final/` si viene). | javier |
| 18:02 | `src/final/run_final.sh final` (desde cero, sin red). ~X min (ver tiempos abajo). | javier |
| ~18:15 | Revisar `work/final/final_report.md` (avisos, cobertura por mes y entidad) y `work/final/final_times.tsv`. Fila `PROPUESTO` en `SUBMISSIONS.md` con hipótesis y nota esperada. | javier |
| ≤ 18:25 | Subida: `HK_TOKEN=… .venv/bin/python tools/submit.py out/final_…csv.gz --phase final --confirm javier --note '…'`. Apuntar en `SUBMISSIONS.md` (commit, fichero, id de envío). | javier |
| 18:30 | Límite objetivo. Si a las 18:25 no hay fichero completo, subir el mejor fichero **validado** que exista (ver plan B). | javier |
| 19:30 | Cierre oficial. | — |

## Antes de las 17:00 (preparación)

- [ ] `HK_TOKEN` exportado en la terminal del responsable (`tools/submit.py --dry-run` con el último LIVE lo comprueba sin gastar hueco).
- [ ] `.venv/bin/python -c "import duckdb, polars, pyarrow, pypdf"` sin error (sin red no se instala nada).
- [ ] `work/train/party_truth.parquet`, `work/party_model/full/`, `work/nonitem_model/full/` existen (si no, `run_final.sh` los reconstruye: + tiempo).
- [ ] Espacio en disco: `work/final/` necesita ~300 MB; `work/tmp/` es el `temp_directory` de duckdb.
- [ ] El simulacro LIVE de las 17:00 termina `FICHERO VALIDADO` y `final_report.md` no trae avisos nuevos.

## Pasos del pipeline (`src/final/run_final.sh <fase>`)

| Paso | Módulo | Imprescindible | Si falla |
|---|---|---|---|
| T1 | `src.base_parquet` (lines, items, saldos) | sí | aborta |
| T2 | `src.sidefiles` (remesas, devoluciones, tarjetas, cheques, runs) | sí | aborta |
| T2b | `src.sidefiles_ap` | no | sigue sin `side_links_ap.parquet` |
| T4 | `src.party` (en `final` con `--relearn`: train + live + final) | sí | aborta |
| T6 | `src.remit` (avisos) | no | sigue sin `remit_raw.parquet` |
| T7 | `src.nonitem` (categoría sin partida; sin LLM) | no | el matcher pone categoría por defecto según signo |
| T3 | `src.match` | sí | aborta |
| T5 | `src.exceptions` (sólo con `FINAL_T5=1`) | no | se queda `allocations` de T3 |
| submit | `src.submit` → `out/final_<hora>_<sha>.csv.gz` | sí | aborta |
| validate | `tools/validate.py` | sí | aborta |
| report | `src.final.report` → `work/<fase>/final_report.md` | no | — |

Sin red: el script vacía `HACKATHON_API_KEY` y pone un proxy muerto; ningún paso llama a la API. Patrón nuevo sin
caché → T7 lo clasifica por tokens y, si no opina, el matcher pone `UNIDENTIFIED` (cobro) o `BANK_FEES` (pago).

Entidades y cuentas fuera de `masters/` (ZA01, `IT01-VALD-EUR-2`): se procesan igual (D9, nunca se omiten líneas);
`final_report.md` las lista. Si llega `final/masters_final/`, el script monta `work/final/data_root/` con esos maestros
por delante y exporta `HK_DATA` (T1, T2, T5, submit y validate lo usan; T4/T6 leen las rutas fijas de
`data/bankrec-docs-and-masters/` para avisos y PDFs, que no cambian).

## Tiempos medidos (LIVE, 203 083 líneas, desde cero, sin red)

_(se rellena con `work/live/final_times.tsv` del ensayo)_

## Plan B

1. Falla un paso opcional (T2b/T6/T7): el script sigue; el fichero es válido, con menos nota. Subir igualmente.
2. Falla T3 o T4: `python -m src.submit --phase final --side-links work/final/side_links.parquet` produce el fichero
   T2 + baseline trivial (≈ 47 en LIVE). Validar y subir.
3. Falla T1/T2: `python -m src.submit --phase final --trivial` (≈ 11). Validar y subir. Nunca cerrar sin envío.
4. Falla `tools/validate.py`: leer la fila que señala; `submit.py` se niega a subir si no valida.

## Confirmación

- [ ] Simulacro LIVE 17:00 OK (tiempo: ___ min; nota LIVE: ___).
- [ ] `main` congelado a las 17:30 (sha: ___).
- [ ] `run_final.sh final` OK a las ___ (tiempo: ___ min); `final_report.md` revisado.
- [ ] Fila `PROPUESTO` en `SUBMISSIONS.md`.
- [ ] **Envío FINAL subido a las ___ por javier** (fichero: ___; id: ___). Fila `SUBMISSIONS.md` actualizada.
