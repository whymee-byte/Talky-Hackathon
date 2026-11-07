# Puntuación y qué la mueve

```
Score = 100 × ( 0.5 · Allocation F1  +  0.3 · Transaction accuracy  +  0.2 · Exception macro-F1 )
```

Tolerancia de importe: `|enviado − golden| ≤ max(0,01 ; 0,5 % del golden)`. El evaluador es `starter/bankrec_score.py`
(el mismo que en el servidor). Referencias de la organización: ingenuo ≈ 19, heurística trabajada ≈ 61, golden = 100.

## Las tres métricas

| Métrica | Peso | Unidad que cuenta | Qué castiga |
|---|---|---|---|
| **Allocation F1** | 50 | cada par `(movimiento, partida)` con importe en tolerancia | precisión: pares inventados; recall: pares que faltan |
| **Transaction accuracy** | 30 | cada **movimiento** | cualquier fallo dentro de la línea: una partida de más o de menos, un importe, la excepción, la categoría |
| **Exception macro-F1** | 20 | cada fila, por clase de excepción **no vacía**, media simple entre clases | no detectar una clase rara o marcarla de más |

## Dónde están los puntos (TRAIN, 1,49 M movimientos, 5,26 M pares)

### Allocation F1 → los lotes grandes mandan

| Partidas por movimiento | % movimientos | % pares |
|---|---|---|
| 0 (sin partida) | 9,6 | 0 |
| 1 | 58,5 | 16,6 |
| 2–5 | 22,8 | 18,9 |
| 6–20 | 6,9 | 18,5 |
| **> 20** | **2,2** | **46,0** |

Segmentos con más pares (% del total de pares): `ar_dd_batch` 13,6 · `ar_short_dispute` 13,1 · `ar_ref_noise` 12,9 ·
`ap_payment_multi` 11,4 · `ar_1toN_remittance` 7,4 · `ap_fx` 6,3 · `ar_1toN_text` 4,9 · `ar_1toN_credit_note` 4,4 ·
`ar_card_settlement` 2,8.

→ **Remesas SEPA, órdenes de pago, avisos de pago (incluidas disputas) y pagos FX** son casi la mitad del F1 y
son en gran parte deterministas (están en ficheros de detalle). Es lo que más puntos da por hora invertida.

### Transaction accuracy → las líneas pequeñas y numerosas mandan

Segmentos con más movimientos (% de líneas): `ar_ref_noise` 21,0 · `ap_payment_multi` 11,7 · `ar_1to1` 9,5 ·
`ap_payment_single` 9,4 · sin partida (todas las categorías) 9,6 · `ar_same_amount_fifo` 3,3 · `ar_1toN_text` 3,1 ·
`ar_short_discount` 2,9 · `ap_direct_debit` 2,8.

→ **Identificar bien al pagador** con referencias ruidosas y elegir bien entre facturas del mismo importe (FIFO / mes de
servicio). Las líneas sin partida (comisiones, cash pooling…) son puntos fáciles: un clasificador de texto las acierta casi
todas.

### Exception macro-F1 → cada clase vale lo mismo

Hay 10 clases no vacías, así que **cada clase vale ≈ 2 puntos de score**, tenga 100 k filas o 1 k:

| Clase | Filas TRAIN | Comentario |
|---|---|---|
| `FX_DIFFERENCE` | 113.508 | toda fila de factura liquidada en otra divisa |
| `SHORT_DISCOUNT` | 100.174 | condiciones de pago + fecha + importe exacto |
| `PARTIAL` | 91.664 | plazos, a cuenta, retenciones, descuento fuera de plazo |
| `UNIDENTIFIED` | 34.653 | no forzar casos parecidos |
| `SHORT_WITHHOLDING` | 33.818 | 1,25 / 1,5 / 10 % en MX, BR, MA |
| `RETURNED` | 30.358 | devoluciones SEPA, cheques, chargebacks, transferencias rechazadas |
| `SHORT_DISPUTE` | 20.238 | casi siempre sólo en el aviso de pago |
| `DUPLICATE_PAYMENT` | 4.927 | la partida ya estaba liquidada |
| `OVERPAYMENT` | 2.838 | pago > saldo de una sola factura |
| `SHORT_BANK_CHARGES` | 1.130 | ≤ 60 € y ≤ 2 %, transferencias no SEPA |

→ Las clases raras (`SHORT_BANK_CHARGES`, `OVERPAYMENT`, `DUPLICATE_PAYMENT`) son baratas de detectar con reglas y valen
lo mismo que `FX_DIFFERENCE`.

## Decisiones que más mueven la nota

1. **Explotar los ficheros de detalle** (remesas, tarjetas, cheques, `payment_runs`) antes que cualquier heurística:
   ≈ 37 % de los pares, con precisión casi perfecta.
2. **Parsear los avisos de pago**: `ar_1toN_remittance` + `ar_short_dispute` ≈ 20 % de los pares y la clase `SHORT_DISPUTE`.
3. **Llevar el saldo de cada partida en el tiempo** (procesar por fecha): evita casar facturas ya cobradas, habilita
   `DUPLICATE_PAYMENT`, `PARTIAL` y `RETURNED`, y las re-presentaciones.
4. **Identificación del tercero** en N43/MT940 (texto libre, la mitad de las líneas): sin tercero no hay subset-sum ni FIFO.
5. **Importe en divisa de la partida**: en `FX_DIFFERENCE`, `SHORT_*` y `OVERPAYMENT` va el **saldo completo**, no el
   dinero convertido; en `PARTIAL`/`SHORT_DISPUTE`, lo pagado. Un importe mal puesto anula el par (50 %) y la línea (30 %).
6. **Precisión vs. recall en líneas dudosas**: en F1 un par inventado cuesta lo mismo que uno que falta, pero en
   transaction accuracy la línea entera ya está mal. Ante dudas fuertes en un cobro, `UNIDENTIFIED` es una respuesta válida
   (y es una clase de la macro-F1).
7. **Robustez al drift** para el envío FINAL único: camt de Banco del Moncayo desde oct-2025, nuevo formato de avisos
   desde sep-2025, ZA01 en ZAR con numeración "KWI…" y `masters_final`. Probar en LIVE que nada se cae a cero en esos
   cortes antes de las 18:00.

## Cómo medirnos

- **Ventana TRAIN 2024-07 → 2025-06** con saldos de partida calculados del golden anterior (T2/T9): la única medida
  honesta en local. `sample/` sirve para pruebas rápidas, pero en él todas las facturas antiguas aparecen abiertas.
- **LIVE**: hasta 20 envíos/día, 1 cada 3 min; pedir la URL de subida ya consume un hueco.
- **FINAL**: un único envío; un fichero inválido no lo gasta, pero uno válido y malo sí. Validar con T9 antes de subir.
