# Traspaso — rama javier/t3-next sobre main 3fd558e (worktree /Users/javier/talky-me)

## 1. T3g · `src/tolmatch/` (módulo nuevo) + 1 línea en `src/final/run_phase.sh`
Paso nuevo justo **antes de "last exception fixes (T15)"**, tras cnfx (desactivable con `NO_T3G=1`):
```
[[ -n "${NO_T3G:-}" ]] || step "tolerant matches (T3g)" $PY -u -m src.tolmatch --phase "$PHASE"
```
Sólo líneas que acaban sin ninguna partida y con tercero T4 rank 1 ≥ 0,5; exige una única factura del tercero:
- `dup` (sólo pagos, `OUT`): factura liquidada por otra línea en los 60 días anteriores, pago = total → DUPLICATE_PAYMENT
- `over`: saldo < cobro ≤ saldo·1,10 y exceso entero o del 1 % / 10 % (patrón de TRAIN < 2024-07) → OVERPAYMENT
- `reopen`: partida devuelta antes y cobro = saldo → sin excepción (hoy main ya lo cubre: 0 casos)
- `bank` (líneas sin partida): falta ≤ 60 € eq. y ≤ 2 % **y** el texto indica transferencia internacional o gastos
  (`XB_RE`: SHA/BEN/OUR/CHGS, WIRE IN, TT IN, SWIFT, TRANSFER EN DIV…) → SHORT_BANK_CHARGES con importe completo.
  En TRAIN < 2024-07 esa señal da 78-81 % de acierto frente al 1,4 % sin ella. (Sin señal sigue desactivado.)
- `complete`: cobros que ya liquidan partidas de UN tercero pero les sobra dinero → el resto = subconjunto único de
  1-4 facturas/abonos abiertos del tercero (meet-in-the-middle con parejas). Precisión 100/86/96/80 % para 1/2/3/4.
  5-6 partidas (`T3G_COMPLETE_DEEP=1`) apenas suma y acierta 50-60 %: desactivado. `T3G_NO_COMPLETE=1` lo apaga.
- Reetiquetar como comisión líneas que otras etapas ya casaron: desactivado por defecto (`T3G_BANKXB=1`), restaba
  (ventana 92,01 → 91,98).

Medidas (A/B con el mismo pipeline):
| base | sin T3g | con T3g |
|---|---|---|
| ventana main 7857736 | 91,04 | 91,31 |
| LIVE main a811cd9 (+T14) | 91,05 | 91,30 (s_2148a4f1c6a5d5290ed0) |
| LIVE main 7c335da | 91,67 (s_8b5910bb4d8d478fe715) | 91,93 (s_e53eabb106d5ac4155fc) |
| ventana main 7c335da | 91,53 | 91,78 sin comisión · **92,01** con comisión (SHORT_BANK_CHARGES F1 0,677 → 0,786) |
| LIVE main 7c335da + comisión | — | 92,12 (s_3c36e38e6f91c9a8be46, `out/live_AB3_t3gbank_7c335da.csv.gz`) |
| ventana main 7c335da + completar | — | **92,08** (+0,07 sobre 92,01) |
| LIVE main 7c335da + comisión + completar | — | **92,17** (s_3746b33afb054c1e30b9, `out/live_AB4_complete_7c335da.csv.gz`) |

## 2. Arreglo de calibración T4 · `src/party/__main__.py`
Causa del −2,2 de la ejecución limpia (#21): la calibración (`work/party_model/calibration.parquet`) sólo se ajustaba
en `--phase window`; en un checkout limpio live/final seguía sin calibrar en silencio (rank-1 conf ≥ 0,5: 346 k → 199 k
líneas en la ventana). Ahora, si falta en live/final, se ajusta en el momento con las predicciones pre-ventana
(sólo TRAIN) y se avisa con `WARNING`. Probado simulando checkout limpio en LIVE: misma distribución que la buena y
mismo tercero en las 183.013 líneas (+168 s).

## Ficheros
- `src/tolmatch/__init__.py`, `src/tolmatch/__main__.py`
- diff de `src/final/run_phase.sh` y `src/party/__main__.py`: `git -C /Users/javier/talky-me diff`
- ficheros LIVE: `out/live_AB2_base_7c335da.csv.gz`, `out/live_AB2_t3g_7c335da.csv.gz`

## 3. Sobre main 3fd558e (93,33 en LIVE): T3g casi redundante
`excfix` (T15) ya añade comisión (`T15.bc_add`), duplicados (`T15.dup_add`) y sobrepagos (`T15.over_add`).
Ventana con el mismo pipeline: base **93,32** → con T3g antes de excfix **93,37 (+0,05)**. Sigue sumando algo,
pero poco; lo importante de esta rama ahora es el **arreglo de calibración** (sección 2).

Informe de fallos de la base 93,32: `FAIL_REPORT_933.md` (generado con `tools/analysis/fail_report.py`).
Hallazgo: `ar_short_dispute` pierde 31.838 pares (~1 punto) en 378 cobros grandes **sin aviso de pago en los datos**
(comprobado por nº de factura, importe y fechas); pagan ~56 de ~728 facturas abiertas sin bloque por emisión ni
vencimiento → no recuperable sin el aviso.

## 4. `--overfix` (paso final, después de excfix)
Un OVERPAYMENT cuyo exceso sobre la partida no es un importe entero ni exactamente el 1 %/10 % es un cobro que paga
más partidas → sin excepción (se conserva el par). Ventana 3fd558e: 93,37 → **93,50**; OVERPAYMENT F1 0,737 → 0,834
(FP 210 → 94; en T5.overpayment conserva 320/320 aciertos y quita 125 de 126 falsos). Total de esta rama sobre la
base 93,32: **+0,18**. Paso en run_phase.sh: `step "overpayment shape (T3g)" ... src.tolmatch --overfix`.
