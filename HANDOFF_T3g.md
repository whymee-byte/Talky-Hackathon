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

**LIVE (main 3fd558e + T3g + overfix): 93,55** (s_d7e8e717d7ed733f5480, `out/live_AB5_overfix_3fd558e.csv.gz`),
frente a 93,33 de la base → **+0,22**.

## 5. T3u · `src/tolmatch/unid.py` (tabla aprendida de no identificados, último paso)
Añade la excepción UNIDENTIFIED a cobros sin partida con categoría UNIDENTIFIED que el pipeline dejó sin ella, si
P(no identificado | importe redondo, nº de facturas abiertas del tercero T4, método y confianza de T4) ≥ 0,45.
La tabla (`src/tolmatch/unid_table.csv`) se aprende del golden de TRAIN y va versionada (FINAL no depende de una
ventana previa). Validación honesta: aprendida sólo en Q1 y medida en Q2-Q4: **93,36 → 93,44**. LIVE (tabla de toda
la ventana) sobre 93,55: **93,60** (s_821c6f4c5f567d9c0fca). Reaprender: `python -m src.tolmatch.unid learn`.

## 6. T21 · `src/delaycomb` (perfil de retraso de pago por cliente + subconjunto único), antes de cnmore
Idea medida contra el golden: el cliente NO paga facturas que aún no le "tocan" según su retraso mínimo habitual
(q05 de cargo − vencimiento, aprendido de TRAIN; −7 días). Excluyendo sólo esas, el golden cabe en el 92 % de los
cobros multi-factura que nadie casa, con ~29 candidatas en vez de 40. Con ≤ 24 candidatas y solución única del
subset-sum: 616 únicas-correctas vs 51 únicas-incorrectas en el sondeo. En pipeline (sin golden para excluir pagadas):
314 líneas, 955 pares, P 0,88; ventana 93,50 → 93,54; **fuera de muestra Q2-Q4 (perfil < 2024-07): 93,36 → 93,39**.
Ampliar a 36 candidatas + abonos + rank 2-3 baja la precisión a 0,55 (descartado, parámetros por env DC_*).

## 7. Medida sobre main 70a361c (el main que da 93,88 en LIVE)
| ventana | score | Q2-Q4 fuera de muestra |
|---|---|---|
| main + delaycomb (T21) | 93,49 | 93,36 |
| main + delaycomb + tolpost (overfix + T3u) | **93,67** | **93,53** |
`tolpost` = +0,18 (macro-F1 89,33 → 90,12), consistente fuera de muestra (+0,17). `delaycomb` ≈ +0,02 (955 pares,
P 0,88). Recomendación: integrar los tres (sin riesgo medido; NO_T3G=1 los apaga). LIVE en curso.

## 8. Rama javier/t3u-on-94 (sobre main c9259f6, el que da 94,03 en LIVE)
Ese main ya lleva mi --overfix en run_phase.sh **pero no en run_final.sh** (termina en excfix): avisado. Esta rama:
`src.tolpost` (overfix + T3u) tras excfix en run_phase.sh y run_final.sh; `src.delaycomb` antes de cnmore en ambos.
Lo nuevo frente a 94,03 es T3u (+delaycomb). Ventana en curso.

## 9. T22 · `src/pocomb` (paga pedidos completos) y por qué el bloque de +2 no cae
Patrón medido en el golden: un cliente nunca parte un pedido (`customer_po`) en el 95-96 % de los cobros
multi-factura, **estable en cada semestre 2021H2..2025H1** (patrón compartido, sin deriva → seguro en FINAL).
Subconjunto único de unidades-pedido = cobro: sondeo 211 ok / 25 mal (89 %); en pipeline 293 líneas, P 0,76,
ventana +0,03, Q2-Q4 +0,03. Complementario a delaycomb (juntos 93,54 en la ventana sobre 93,50).
Descartado con datos: bloque por emisión/vencimiento (1,8 %), quincena (12 %), consecutivos (16 %), más grandes
primero (no), referencias parciales en el texto (no hay).
Cuello real: de ~5.000 cobros multi-factura sin casar, 1.156 tienen facturas del golden "robadas" por otra línea
(2.311 facturas), 959 de ellas por T5.full_clear (P global 0,97, no se puede contestar a ciegas). Reasignación
arbitrada contra reglas débiles (T3x.win, T3m.fill, fill_weak, overpayment): 76 ok / 10 mal, sólo 76 líneas → no
compensa como etapa. El resto no tiene solución única por importe: hace falta el aviso de pago, que no existe.

## 10. Medida sobre main fef4544 (el main de LIVE 94,03), ventana completa
| ventana | score | Q2-Q4 |
|---|---|---|
| main + pocomb + delaycomb | 94,05 | 93,93 |
| + tolpost (overfix + T3u) | **94,11** | **93,99** |
| + T3g.ambig (quita full_clear ambiguo: línea multi-partida, sin referencia en texto, ≥2 facturas abiertas del mismo importe; 343 pares) | **94,12** | — |
tolpost sigue sumando (+0,06) sobre el excfix nuevo; todo consistente fuera de muestra. LIVE en curso.

## 11. Estado al cierre (FINAL enviado 18:40: main 9cb8800 → 94,22)
Esta rama (javier/t3u-on-94) está rebasada sobre el main final (ef14c90) y NO entró en FINAL. Aporta, en orden de
valor: `src.tolpost` tras excfix (overfix + T3u + T3g.ambig) > `src.pocomb` > `src.delaycomb` (ambos antes de cnmore),
todo validado fuera de muestra (Q1 → Q2-Q4) y sin red. Medida de la ventana completa sobre el main final: ver abajo.
Para FINAL habría sido lo mismo que LIVE: pasos deterministas al final del pipeline, reglas generales aprendidas de
TRAIN (retraso por cliente, pedidos completos, forma del sobrepago, tabla de no identificados), sin ids ni importes
a fuego.
