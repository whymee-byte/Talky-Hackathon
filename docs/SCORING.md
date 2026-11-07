# Puntuación, límites y qué la mueve

## Fuentes (citas literales)

| Tema | Cita | Fuente |
|---|---|---|
| Fórmula | "Score = 100 × (0.5 · allocation F1 + 0.3 · transaction accuracy + 0.2 · exception macro-F1)" | `data/README_EN.md`, `data/FORMAT.md` §Scoring, web `/reglas` |
| Tolerancia | "the amount is within max(0.01, 0.5 % of the golden amount)" | `data/FORMAT.md` §Scoring |
| Formato | "One CSV file per phase … UTF-8, comma separated, with header, optionally gzip-compressed" · "≤ 60 MB uncompressed, ≤ 3,000,000 rows" | `data/FORMAT.md` |
| Líneas omitidas | "Lines you omit count as wrong." | `data/FORMAT.md` (tabla de columnas) |
| Límites de envío | "Un envío cada 10 s por fase" · "100000 envíos al día por fase" · "Pedir la URL de subida ya consume un hueco" | web `/reglas` (consultada 2026-11-07 10:40) |
| LIVE | "Cada envío válido se puntúa en minutos y cuenta tu mejor puntuación." | web `/reglas` |
| FINAL | "Cada equipo tiene UN solo envío FINAL: cuenta el primer fichero válido y no se puede repetir. Un fichero con errores de formato no gasta el envío." | web `/reglas` |
| Sin FINAL | "Los equipos sin envío FINAL no entran en la clasificación final." | web `/reglas` |
| Horario | 18:00 "Se publican los datos FINAL" · 19:30 "Cierre de envíos" · 19:45 entrega de proyecto (vídeo, repo, descripción) · 20:30 revelación | web `/` agenda y `/proyecto` |
| Evaluador | "`bankrec_score.py` is the exact scorer used by the server." | `data/starter/README.md` |
| Referencias | Naive ≈ 19 · Heurística "reglas bien trabajadas" ≈ 61 · golden 100 | web `/reglas` |

> Los límites de la web (1/10 s, 100k/día) **sustituyen** a los que había en versiones anteriores de este documento
> (1/3 min, 20/día). La web dice "toda la sala comparte IP": consultar el estado como mucho cada 5 s.

## Cómo se envía

Script oficial en la web `/enviar` (4 pasos: pedir URL → subir a S3 → `POST /score` → consultar cada 5 s). El token del
equipo va en la variable `HK_TOKEN`, **nunca** en el código. Lo envolvemos en `tools/submit.py` (tarea T0). LIVE: se
sube lo que mejora en la ventana y se apunta en `SUBMISSIONS.md`. FINAL: OK manual del responsable y subida antes de las
18:30 (el empate gana el envío más temprano).

## Las tres métricas (leído en `starter/bankrec_score.py`)

| Métrica | Peso | Unidad | Detalle que importa |
|---|---|---|---|
| **Allocation F1** | 50 | par `(bank_txn_id, item_id)` | TP si el par está en el golden **y** el importe está en tolerancia. La excepción **no** cuenta aquí. Precisión = TP / pares enviados → cada par inventado resta. |
| **Transaction accuracy** | 30 | movimiento | Con partidas: mismo nº de partidas, todas en tolerancia **y con la misma excepción**. Sin partida: fila de categoría con categoría **y** excepción iguales. Una línea omitida = fallo. |
| **Exception macro-F1** | 20 | fila con excepción no vacía | F1 por clase, media simple sobre las clases **presentes en el golden**. Una excepción en una fila con partida cuenta como TP si el par existe en el golden con esa excepción (**el importe no importa** aquí). Marcar una clase que el golden no tiene en esa fila = FP. |

Consecuencias:
- **Cada clase de excepción vale ≈ 2 puntos** (20 / 10 clases), tenga 100 k filas o 1 k. Una clase que nunca
  emitimos aporta F1 = 0.
- Una fila de categoría con excepción `UNIDENTIFIED` en una línea que sí tenía partidas = FP de `UNIDENTIFIED`.
- Omitir una línea no invalida el fichero, pero suma 0 en accuracy (y recall de sus pares).

## Dónde están los puntos (TRAIN, 1.489.931 movimientos, 5.260.781 pares, ver `explore/PROFILE.md`)

### Allocation F1 → mandan los lotes grandes

| Partidas por movimiento | movimientos | % mov. | pares | % pares |
|---|---|---|---|---|
| 0 (sin partida) | 142.630 | 9,6 | 0 | 0 |
| 1 | 871.295 | 58,5 | 871.295 | 16,6 |
| 2–5 | 340.299 | 22,8 | 995.607 | 18,9 |
| 6–20 | 103.183 | 6,9 | 975.321 | 18,5 |
| 21–100 | 26.676 | 1,8 | 985.531 | 18,7 |
| > 100 (máx. 1.540) | 5.848 | 0,4 | 1.433.030 | 27,2 |

Segmentos con más pares (% de pares): `ar_dd_batch` 13,6 · `ar_short_dispute` 13,1 · `ar_ref_noise` 12,9 ·
`ap_payment_multi` 11,4 · `ar_1toN_remittance` 7,4 · `ap_fx` 6,3 · `ar_1toN_text` 4,9 · `ar_1toN_credit_note` 4,4 ·
`ar_card_settlement` 2,8.

### Transaction accuracy → mandan las líneas pequeñas y numerosas

Segmentos con más movimientos (% de líneas): `ar_ref_noise` 21,0 · `ap_payment_multi` 11,7 · `ar_1to1` 9,5 ·
`ap_payment_single` 9,4 · sin partida (todas) 9,6 · `ar_same_amount_fifo` 3,3 · `ar_1toN_text` 3,1 ·
`ar_short_discount` 2,9 · `ap_direct_debit` 2,8.

### Exception macro-F1 → cada clase vale lo mismo

| Clase | Filas TRAIN | Cómo se detecta |
|---|---|---|
| `FX_DIFFERENCE` | 113.508 | divisa movimiento ≠ divisa factura (determinista) |
| `SHORT_DISCOUNT` | 100.174 | condiciones con descuento + fecha ≤ emisión + días + 3 + importe exacto |
| `PARTIAL` | 91.664 | queda saldo: plazos, "a cuenta", retención, anticipo, descuento fuera de plazo |
| `UNIDENTIFIED` | 34.653 | cobro sin partida posible (¡no forzar parecidos!) |
| `SHORT_WITHHOLDING` | 33.818 | 1,25 / 1,5 / 10 % en MX, BR, MA |
| `RETURNED` | 30.358 | devoluciones SEPA/ACH/BACS, cheques, chargebacks, transferencias rechazadas |
| `SHORT_DISPUTE` | 20.238 | deducción con motivo, casi siempre sólo en el aviso de pago |
| `DUPLICATE_PAYMENT` | 4.927 | la partida ya estaba liquidada |
| `OVERPAYMENT` | 2.838 | pago > saldo de una única factura |
| `SHORT_BANK_CHARGES` | 1.130 | transferencia no SEPA, falta ≤ 60 € y ≤ 2 % |

## Decisiones que más mueven la nota (de más a menos)

1. **Ficheros de detalle** (remesas SEPA, tarjetas, cheques, `payment_runs`): ≈ 37 % de los pares y ≈ 25 % de las
   líneas, con precisión casi perfecta.
2. **Tercero + importe + saldo vivo** para cobros con referencia ruidosa (`ar_ref_noise`, `ar_1to1`, mismo importe
   FIFO/mes): ≈ 40 % de las líneas.
3. **Motor de excepciones**: 20 puntos de macro-F1, y además cada excepción mal puesta tira la línea en accuracy.
   Las clases raras son reglas baratas.
4. **Avisos de pago**: `ar_1toN_remittance` + `ar_short_dispute` ≈ 20 % de los pares y la clase `SHORT_DISPUTE`.
5. **Movimientos sin partida**: 9,6 % de las líneas con muy pocos patrones de texto (ver DOMAIN): puntos fáciles de
   accuracy y la clase `UNIDENTIFIED`.
6. **Umbral de casación ≈ F1/2**: F1 = 2·TP/(P+G), así que un par con p de acierto compensa si p > F1/2 (~0,4).
   Abstenerse de más hunde el recall; ajustar en la ventana.
7. **Importe correcto en la divisa de la partida**: en liquidación completa (`FX_*`, `SHORT_*` salvo disputa,
   `OVERPAYMENT`) va el **saldo completo**; en `PARTIAL`/`SHORT_DISPUTE`, lo pagado. Un importe mal puesto anula el par
   (F1) y la línea (accuracy).
8. **Robustez al drift** para el único envío FINAL (ver DOMAIN §Drift).

## Primer envío seguro (T0)

Un fichero válido que cubre **todas** las líneas de la fase:
- cobros (`amount > 0`) → `,,,UNIDENTIFIED,UNIDENTIFIED`; pagos (`amount < 0`) → `,,,BANK_FEES,`;
- más los pares exactos del starter (texto contiene nuestro nº de documento **y** importe = total).

Es lo que hace `data/starter/starter.py` (≈ 10–15 según su README; "naive" ≈ 19 en LIVE). Sirve para validar el
circuito de envío, no para competir. Mejora casi gratis: clasificar las líneas sin partida por patrón de texto.

## Cómo medirnos en local

- **`sample/`** (2025-06, 8.499 líneas): humo rápido, pero todas las facturas antiguas aparecen abiertas.
- **Ventana TRAIN 2024-07 → 2025-06** con saldos iniciales calculados del golden anterior: la medida honesta.
  Nunca puntuar en local sobre los mismos meses usados para ajustar umbrales sin decirlo en el PR.
- **LIVE**: la verdad. Cuenta la mejor puntuación; usarlo para validar hipótesis concretas (ver `SUBMISSIONS.md`).
- **FINAL**: un único envío. Un fichero inválido no lo gasta; uno válido y malo, sí. Validar con T0 antes de subir.
