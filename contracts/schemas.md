# Contratos entre módulos

Cada módulo lee las entradas oficiales (`data/bankrec-docs-and-masters/<fase>/…`) y/o ficheros intermedios de otros
módulos, y escribe **uno** de los ficheros de abajo. Mientras se respete el contrato, cada módulo se puede desarrollar,
probar y reemplazar por separado.

## Convenciones comunes

- Formato: **Parquet**, uno por fase, en `work/<fase>/<fichero>.parquet` (`<fase>` ∈ `sample`, `train`, `window`,
  `live`, `final`; `window` = TRAIN 2024-07 → 2025-06 con saldos iniciales). `work/` no se versiona.
- Caché de entradas crudas: `data_cache/<fase>/<tabla>.parquet` (lo crea `explore/profile.py`; no se versiona).
- Caché de LLM: `llm_cache/<módulo>/<sha256(prompt_version + patrón)>.json` (sí se versiona, es pequeño y evita repetir
  llamadas).
- Código: cada módulo vive en `src/<módulo>/` y se ejecuta con `python -m src.<módulo> --phase <fase>`.
- Procesamiento por columnas (duckdb/polars), sin bucles fila a fila sobre millones de filas.
- **Importes en céntimos, entero `int64`** (`*_cents`). Sólo el escritor final los pasa a texto `1234.56`.
- Fechas `date` (no texto). Ids tal cual vienen (`BT-…`, `ARI-…`, `CES01000343`, …).
- Texto normalizado (`*_norm`): mayúsculas, sin acentos, sólo `[A-Z0-9 ]`, espacios colapsados.
- **Rastro de auditoría obligatorio:** toda tabla de "propuestas" y la decisión final llevan `rule` (id estable de la
  regla que la generó, p. ej. `T2.dd_batch.batch_id`), `confidence` ∈ [0, 1] y `evidence` (texto corto legible: qué
  claves/valores la justifican, p. ej. `batch_id=…; total ok`). Sin estas tres columnas un módulo no está "hecho".
  Sirven para arbitrar entre módulos, depurar y explicar cada decisión al jurado.
- **Sin valores del dato a fuego:** nada de ids, nombres, importes o textos concretos de las líneas que se puntúan en el
  código. Las reglas son generales (patrones, umbrales, maestros) o se aprenden de TRAIN.
- **Mocks desde el golden:** para trabajar sin esperar a otro módulo, cada contrato de abajo se puede generar "perfecto"
  a partir de `train/golden_allocations.csv.gz` (ver la columna *Mock desde golden*). Así cada tarea es independiente.

## Diagrama

```
 bank_transactions ─► [T1 ingest] ─► lines ─┬─► [T4 party]     ─► line_party ─────┐
                                            ├─► [T7 nonitem]   ─► line_category ──┤
 items csvs + open_items ─► [T1 items] ─► items ─────────────────────────────────┤
 batch/run/card/cheque csvs ─► [T2 side files] ─► side_links ────────────────────┤
 remittance_advices ─► [T6 remit] ─► remit_lines ────────────────────────────────┤
 pdfs ─► [T8 pdf facts] ─► doc_facts ────────────────────────────────────────────┤
                                                                                  ▼
                                  [T3 matcher] ─► pairs_raw ─► [T5 excepciones] ─► allocations
                                                                                  ▼
                                               [T0 writer/validador + eval] ─► submission.csv.gz + score
```

---

## 1. `lines.parquet` — movimientos normalizados (T1)

Una fila por `bank_txn_id` de la fase.

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id` | str | clave |
| `account_id`, `entity`, `bank`, `statement_format` | str | como en origen |
| `booking_date`, `value_date` | date | |
| `amount_cents` | int64 | con signo (+ cobro, − pago) |
| `currency` | str | divisa de la cuenta |
| `orig_amount_cents`, `orig_currency` | int64?, str? | importe instruido si difiere |
| `direction` | str | `IN` / `OUT` |
| `text_norm` | str | `text` + `ref1` + `ref2` + `remittance_info` + `counterparty_name`, normalizado; uniendo cortes de N43 (` \| `) |
| `cp_name_norm` | str? | nombre del ordenante/beneficiario: camt directo; N43/MT940 extraído del texto |
| `ibans` | list[str] | IBAN encontrados (contraparte + texto) |
| `mandate_ids` | list[str] | `VTM-XX-nnnnnnn` |
| `batch_refs` | list[str] | ids de remesa/depósito/run/e2e/liquidación detectados (11 dígitos, `RCH…`, `PR…`, `CF…`, comercio) |
| `doc_tokens` | list[str] | candidatos a nº de documento (alfanuméricos ≥ 4, también sólo dígitos sin ceros iniciales) |
| `month_hint` | str? | mes de servicio citado (`2025-05`) si el texto dice "MAYO 25", "SERVICIOS MAYO"… |
| `is_return_hint` | bool | texto de devolución/impagado/chargeback/rechazo |

*Mock desde golden:* no aplica (es la entrada).

## 2. `items.parquet` — partidas con saldo inicial (T1)

Una fila por `item_id` visible en la fase (incluye las abiertas al inicio).

| Columna | Tipo | Descripción |
|---|---|---|
| `item_id` | str | clave |
| `kind` | str | `ARI` / `ARC` / `API` / `APC` |
| `side` | str | `AR` / `AP` |
| `entity` | str | sociedad dueña |
| `party_id` | str | cliente o proveedor |
| `group_id`, `parent_party_id` | str? | grupo de clientes |
| `doc_number` | str | nº impreso (el que citan los clientes) |
| `doc_norm`, `doc_digits` | str | normalizado / sólo dígitos sin ceros iniciales |
| `po_norm` | str? | pedido del cliente (`customer_po`) o nuestro (`our_po`) |
| `issue_date`, `due_date` | date | |
| `currency` | str | |
| `total_cents` | int64 | importe del documento (positivo) |
| `open_at_start_cents` | int64 | saldo al inicio de la fase (TRAIN: total; LIVE/FINAL: `open_items_at_start` o total si es nueva) |
| `payment_terms`, `discount_pct`, `discount_days` | str, float?, int? | desde `payment_terms.csv` |
| `payment_method` | str? | `transfer` / `dd` / `card` / `cheque` |
| `related_doc` | str? | factura que rectifica un abono |

*Mock desde golden:* no aplica (es la entrada). Para evaluar en una **ventana** de TRAIN, `open_at_start_cents` = total −
lo asignado por el golden antes de la ventana (sumando `RETURNED`, a 0 si la excepción es de liquidación completa).

## 3. `side_links.parquet` — enlaces deterministas desde ficheros de detalle (T2)

Propuestas de asignación que salen de remesas SEPA, devoluciones, liquidaciones de tarjeta, depósitos de cheques y
nuestras órdenes de pago (`payment_runs`).

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id` | str | |
| `item_id` | str | |
| `amount_cents` | int64 | en divisa de la partida, positivo |
| `exception` | str | normalmente vacío; `RETURNED` en devoluciones; `PARTIAL` si el documento paga menos que el saldo |
| `rule` | str | `dd_batch` / `dd_return` / `card` / `cheque` / `run_transfer` / `run_aggregated` / `run_confirming` |
| `link_key` | str | id del lote/e2e/depósito que justificó el enlace |
| `confidence` | float | 1.0 si casan id y total; menos si sólo casa el total |
| `evidence` | str | claves que lo justifican |

*Mock desde golden:* filas del golden con `segment` ∈ {`ar_dd_batch`, `ar_dd_returned`, `ar_card_settlement`,
`ar_cheque_deposit`, `ap_payment_*`, `ap_confirming`, …}.

## 4. `line_party.parquet` — quién paga / a quién pagamos (T4)

Hasta 3 candidatos por movimiento.

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id` | str | |
| `rank` | int | 1 = mejor |
| `party_id` | str | cliente (`C…`) o proveedor (`S…`) cuyas partidas se liquidan (no necesariamente quien envía el dinero) |
| `payer_party_id` | str? | quien manda el dinero si es distinto (matriz, confirming, agencia, ES00) |
| `method` | str | `iban` / `mandate` / `name_exact` / `name_fuzzy` / `history` / `doc_ref` |
| `confidence` | float | |
| `rule`, `evidence` | str | regla y claves usadas (IBAN, mandato, nombre…) |

*Mock desde golden:* `party_id` de las partidas asignadas a cada movimiento.

## 5. `remit_lines.parquet` — avisos de pago parseados (T6)

Una fila por documento citado en un aviso (`remittance_advices/*.{pdf,csv,xlsx,txt}`).

| Columna | Tipo | Descripción |
|---|---|---|
| `advice_file` | str | ruta relativa del aviso |
| `entity`, `payer_name`, `received_date` | str, str, date | de `index.csv` |
| `payment_ref` | str? | referencia de pago que también sale en el banco |
| `payment_total_cents` | int64? | total del pago según el aviso |
| `doc_ref_raw` | str | cómo cita el documento el cliente |
| `item_id` | str? | partida resuelta (null si no se pudo) |
| `gross_cents`, `deduction_cents`, `paid_cents` | int64 | importe del documento, deducción y lo pagado |
| `deduction_reason` | str? | texto del motivo (precio, daños, logística…) → apunta a `SHORT_DISPUTE` |
| `is_credit_note` | bool | |

*Mock desde golden:* agrupar filas del golden de `ar_1toN_remittance` / `ar_short_dispute` por movimiento.

## 6. `line_category.parquet` — movimientos sin partida (T7)

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id` | str | |
| `p_item` | float | probabilidad de que el movimiento liquide alguna partida |
| `category` | str | mejor categoría de `masters/categories.csv` si no hay partida |
| `p_category` | float | |

*Mock desde golden:* `p_item` = 1 si tiene partidas; `category` del golden.

## 7. `doc_facts.parquet` — cláusulas de los PDFs (T8)

Una fila por hecho extraído.

| Columna | Tipo | Descripción |
|---|---|---|
| `pdf_file` | str | |
| `entity`, `party_id`, `doc_number` | str | de `pdfs/index.csv` |
| `item_id` | str? | partida resuelta |
| `fact` | str | `INSTALMENTS` / `RETENTION` / `ADVANCE_DEDUCT` / `REPLACES` / `SERVICE_MONTH` / `RAPPEL_OFFSET` / `RAPPEL_PAID_OUT` / `CN_REFUND` / `CN_OFFSET` / `FACTORED` / `NEW_IBAN` |
| `value_num` | float? | nº de plazos, % de retención, importe de anticipo… |
| `value_text` | str? | documento sustituido, mes (`2025-05`), IBAN nuevo, factor… |
| `valid_from` | date? | p. ej. para `NEW_IBAN` |

*Mock desde golden:* parcial; los segmentos `ar_instalment`, `ar_retention*`, `ar_advance_deduction`,
`ar_replaced_invoice`, `ar_same_amount_month`, `ar_new_iban` indican qué partidas tienen cada cláusula.

## 7b. `pairs_raw.parquet` — emparejamientos antes de excepciones (T3 → T5)

El matcher (T3) decide **qué** partidas liquida cada movimiento y **cuánto dinero** va a cada una; el motor de
excepciones (T5, `src/exceptions/`) decide **la excepción y el importe final** según `POLICIES.md` §2–3.

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id`, `item_id` | str | |
| `booking_date` | date | del movimiento |
| `line_amount_cents`, `line_currency` | int64, str | importe del movimiento (con signo) y divisa de la cuenta |
| `item_currency` | str | |
| `open_before_cents` | int64 | saldo de la partida justo antes de este movimiento (0 si ya liquidada → candidato a duplicado) |
| `cash_cents` | int64 | dinero aplicado a esta partida, **convertido a la divisa de la partida** |
| `doc_hint` | str? | lo que dicen aviso/PDF: `DISPUTE:<motivo>`, `INSTALMENT k/n`, `RETENTION`, `ADVANCE`, `RETURN`… |
| `rule`, `confidence`, `evidence` | str, float, str | |

*Mock desde golden:* `cash_cents` = importe del golden salvo en liquidaciones completas, donde se reconstruye de la
línea; sirve para probar el motor de excepciones sin matcher.

## 8. `allocations.parquet` — decisión final (T5, a partir de `pairs_raw` + líneas sin partida de T7)

Exactamente el contenido de la entrega, antes de serializar.

| Columna | Tipo | Descripción |
|---|---|---|
| `bank_txn_id` | str | **todas** las líneas de la fase deben aparecer |
| `item_id` | str? | |
| `amount_cents` | int64? | null si no hay partida |
| `category` | str? | sólo si no hay partida |
| `exception` | str? | lista cerrada |
| `rule`, `confidence`, `evidence` | str, float, str | rastro de auditoría (no se envía; se guarda en `work/<fase>/audit.parquet`) |

Invariantes: no hay `(bank_txn_id, item_id)` repetidos; una línea tiene **o** filas con partida **o** una única fila de
categoría; `UNIDENTIFIED` lleva categoría y excepción `UNIDENTIFIED`.

## 9. Entrega y evaluación (T0)

- `out/<fase>_<yyyymmdd-hhmm>_<git-sha>.csv.gz`: columnas `bank_txn_id,item_id,amount,category,exception`, importe con
  punto y 2 decimales.
- `out/<fase>_….score.json` (sólo TRAIN/ventanas): salida de `starter/bankrec_score.py` + desglose por segmento.
