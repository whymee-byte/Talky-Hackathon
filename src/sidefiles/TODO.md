# ⚠ ALERTA T2 · lo que falta (estado entregado "decentemente aceptable")

T2 se cerró por tiempo con todos los pasos implementados y medidos, pero **no todos los segmentos llegan al objetivo**
(precisión ≥ 98 %, recall ≥ 95 %). Cifras de la ventana 2024-07 → 2025-06 en el último commit; ver `SUBMISSIONS.md`.

Cómo medir: `python -m src.sidefiles --phase window --subset q1` (rápido) y `python -m src.sidefiles.eval --phase window --subset q1`;
la ventana completa (sin `--subset`) sólo antes de cada commit.

## Segmentos por debajo del objetivo

| Segmento | Estado | Qué falta / idea |
|---|---|---|
| `ap_confirming` | P ≈ 87 %, R ≈ 90 % | Un cargo de confirming = pagos (e2e) que vencen ese día. Asigno cada e2e al primer cargo del run con fecha ≥ vencimiento máx. de sus documentos (93 % exacto). Cuando la suma no cuadra (conf. 0,6) la precisión baja al 78 %. Probado y **descartado**: asignación ASOF por importes acumulados (P 70,8 %). Siguiente idea: rehacer la asignación sólo en cargos que no cuadran, moviendo e2e de ±1–3 días hasta cuadrar (subset-sum pequeño por run). |
| `ap_payment_single` | P ≈ 97,2 % | Fallback N43 (`PR… 0` truncado): run + importe; con importes repetidos dentro del run elige mal. Desempatar por nombre del beneficiario en el texto. |
| ~~`ar_cheque_deposit`~~ | ✅ resuelto | ~950 cheques sin `stub_reference` (sobre todo US lockbox) y con librador homónimo (varios clientes con el mismo nombre). Arreglado (un candidato por homónimo + desempate por importe): ventana 99,97 % / 99,97 %. |
| `ar_dd_returned` | P/R ≈ 97,95 % | MD06 (reembolso, 21–75 días) con varias cuotas del mismo importe dentro del plazo: no distinguible. ⚠ La preferencia por ~49 días se midió **en la propia ventana**: recalcularla con TRAIN < 2024-07 para evitar fuga. |
| `ap_direct_debit` | R = 0 % | **No implementado.** No hay fichero de detalle: son cargos de proveedores por domiciliación (`SEPA LASTSCHRIFT MREF … CRED <creditor id>`, `PRLV SEPA … RUM …`, `RECIBO …`). Encaja mejor en T3/T4 (tercero por creditor id/RUM + nº de factura del texto). |

## No hecho (fuera del alcance pedido o de otro módulo)

- **Excepciones de los enlaces de pagos**: `ap_fx` (`FX_DIFFERENCE`) y `ap_partial` (`PARTIAL`) se enlazan bien (R 96 %) pero
  sin excepción → la excepción la pone T5 (`src/exceptions/`). Con eso suben accuracy y macro-F1.
- **Otras devoluciones**: `ar_cheque_returned`, `ar_card_chargeback`, `ap_returned_transfer` (texto `RETURN <código> … PR… e2e`)
  no implementadas; el patrón de `ap_returned_transfer` es fácil (e2e de la orden de pago en el texto → mismos documentos, `RETURNED`).
- **Entrada interina**: `src/base_parquet/` genera `lines.parquet`/`items.parquet` mientras T1 no los escribe con el contrato §1–§2.
  Cuando T1 los entregue, borrar `src/base_parquet/` y la llamada de `ensure_base_parquets`.
- **Rendimiento**: el paso de cheques es el más lento en la ventana completa (join por sufijos de dígitos); vigilar en FINAL (< 15 min en total).
- `tools/validate.py --phase window` cuenta como "missing" las líneas de TRAIN fuera de la ventana (sólo informativo, el fichero es válido).
