# Tareas

Cada tarea produce **un** fichero de `contracts/schemas.md` y se puede hacer en paralelo: si necesitas la salida de otra
tarea que aún no está, usa su *mock desde golden* (descrito en el contrato). Apúntate escribiendo tu nombre en **Dueño**.

Reglas comunes: duckdb/polars, nada de bucles fila a fila sobre millones de filas; código en `src/<modulo>/`, un
comando por módulo (`python -m src.<modulo> --phase train|live|final`); salida en `work/<fase>/`.
Evaluación honesta = **ventana de TRAIN 2024-07 → 2025-06** (con raw y PDFs en train-lite), saldos iniciales calculados
del golden anterior (T9).

> Existe un prototipo monolítico `src/recon.py` escrito antes de este plan (≈ 51,6 en `sample`, cifra no comparable con
> la ventana). Sirve como referencia de reglas, no como base de código.

Horario: datos FINAL a las **18:00**, cierre de envíos **19:30**, entrega del proyecto **19:45**. Todo lo que esté listo a
las 17:30 entra en el pipeline de FINAL.

---

### T1 · Ingesta y normalización de movimientos
- **Dueño:**
- **Produce:** `work/<fase>/lines.parquet` (contrato §1).
- **Hecho cuando:** existe para `train`, `live` y `sample`; 1 fila por `bank_txn_id`; extrae `cp_name_norm`, `ibans`,
  `mandate_ids`, `batch_refs`, `doc_tokens`, `month_hint` en los tres formatos (N43, MT940, camt) incluido el cambio de
  Banco del Moncayo a camt en 2025-10.
- **Verificar:** `count(*) = count(distinct bank_txn_id)` = nº de líneas de `bank_transactions`; sobre la ventana TRAIN,
  % de pares del golden cuyo `doc_norm`/`doc_digits` aparece en `doc_tokens` ≥ el 22–25 % medido con búsqueda literal;
  `cp_name_norm` no vacío en ≥ 90 % de transferencias recibidas; LIVE en < 2 min.

### T2 · Partidas, saldos iniciales y arnés de ventana
- **Dueño:**
- **Produce:** `work/<fase>/items.parquet` (contrato §2) y `work/train_window/items.parquet` con saldos al 2024-07-01.
- **Hecho cuando:** une AR, AP y abonos (+ `open_items_at_start` en LIVE/FINAL, + `masters_final` en FINAL); trae
  condiciones de pago con % y días de descuento; saldo de ventana = total − asignado por el golden antes de 2024-07-01
  (sumando `RETURNED`, a 0 si la excepción liquida entera).
- **Verificar:** en LIVE, `sum(open_at_start)` por tipo = `open_items_at_start` (ARI 1.116,1 M, API 560,2 M,
  ARC 57,3 M, APC 6,3 M); en la ventana, ≥ 99 % de los pares del golden apuntan a partidas con saldo > 0 en el momento.

### T3 · Ficheros de detalle: remesas SEPA, devoluciones, tarjetas, cheques, órdenes de pago
- **Dueño:**
- **Produce:** `work/<fase>/side_links.parquet` (contrato §3).
- **Hecho cuando:** enlaza cada movimiento de lote con todas sus partidas (remesa SEPA por `batch_id`, devoluciones por
  mandato + remesa, tarjeta por comercio/fecha/neto, cheques por `deposit_id`, `payment_runs` por e2e / run / fecha de
  vencimiento de confirming, incluido payment factory ES00 → ES01/ES02/PT01); importes de tarjeta en **bruto**.
- **Verificar:** en la ventana, precisión ≥ 98 % y recall ≥ 95 % de pares en los segmentos `ar_dd_batch`,
  `ar_dd_returned`, `ar_card_settlement`, `ar_cheque_deposit`, `ap_payment_multi`, `ap_payment_single`, `ap_confirming`,
  `ap_paid_by_holding`, `ap_credit_note_netted`, `ap_direct_debit`. Estos segmentos son ≈ 37 % de los pares del golden.

### T4 · Identificación de tercero
- **Dueño:**
- **Produce:** `work/<fase>/line_party.parquet` (contrato §4).
- **Hecho cuando:** combina IBAN (maestro + **IBAN aprendidos en TRAIN** + avisos de cambio de IBAN), mandato, nombre
  (truncado, nombre comercial, renombrados), pagador por cuenta de otro (matriz/grupo, confirming "ORD. …", agencia
  "EXPTE …") e historial.
- **Verificar:** en la ventana, top-1 acierta el tercero del golden en ≥ 95 % de las líneas con partida y top-3 en ≥ 98 %;
  desglose por formato (N43 / MT940 / camt) y en `ar_third_party_*`, `ar_new_iban`.

### T5 · Avisos de pago (remittance advices)
- **Dueño:**
- **Produce:** `work/<fase>/remit_lines.parquet` (contrato §5).
- **Hecho cuando:** parsea los 4 formatos (pdf 16,7 k, csv 18,9 k, txt 13 k, xlsx 2,4 k en TRAIN) y el cambio de
  formato del gran cliente en sep-2025; resuelve referencias del cliente a `item_id`; captura deducciones y su motivo.
- **Verificar:** ≥ 95 % de líneas de aviso con `item_id` resuelto en la ventana; con los avisos, recall de pares
  ≥ 95 % en `ar_1toN_remittance` y `ar_short_dispute` (≈ 20 % de los pares del golden) y excepción correcta en
  `SHORT_DISPUTE`; prueba en LIVE de que el nuevo formato sale (nº de avisos por mes sin caída en sep-2025).

### T6 · Clasificador de movimientos sin partida
- **Dueño:**
- **Produce:** `work/<fase>/line_category.parquet` (contrato §6).
- **Hecho cuando:** predice `p_item` y categoría (21 clases) con texto, códigos bancarios y signo; trata a `BANK_REVERSAL`
  (ambas patas), `CASH_POOL`, `INTERNAL_TRANSFER`, `INTERCOMPANY`, `FX_DEAL`; sabe que `UNIDENTIFIED` sólo se decide
  después del matcher.
- **Verificar:** en la ventana, exactitud de categoría ≥ 98 % sobre líneas sin partida (excluido `UNIDENTIFIED`) y
  AUC de `p_item` ≥ 0,99; matriz de confusión por categoría en el PR.

### T7 · Hechos de los PDFs
- **Dueño:**
- **Produce:** `work/<fase>/doc_facts.parquet` (contrato §7).
- **Hecho cuando:** extrae plazos, retención de garantía, anticipo a deducir, "anula y sustituye", mes de servicio,
  rappel compensado/pagado, abono reembolsado/compensado, factoring, nuevo IBAN (ES/FR/DE/EN…).
- **Verificar:** en la ventana, las partidas de `ar_instalment`, `ar_retention`, `ar_advance_deduction`,
  `ar_replaced_invoice`, `ar_same_amount_month`, `ar_new_iban` tienen el hecho correspondiente en ≥ 90 % de los casos;
  muestra de 20 PDFs revisados a mano sin falsos positivos.

### T8 · Matcher, asignación y excepciones
- **Dueño:**
- **Produce:** `work/<fase>/allocations.parquet` (contrato §8).
- **Hecho cuando:** recorre los movimientos **en orden de fecha** llevando saldos; prioriza `side_links` → avisos →
  referencias → tercero + importe (FIFO, subset-sum con abonos, mes de servicio) → `UNIDENTIFIED`/categoría; aplica
  `POLICIES.md` §2–3 (importe y excepción por fila, incluidas FX y duplicados); funciona con mocks de T3–T7.
- **Verificar:** en la ventana con **todos los mocks del golden**, score ≥ 90 (techo del matcher); con los módulos reales,
  mejora sobre la heurística de referencia (≈ 61). Tabla por segmento en el PR.

### T9 · Escritor, validador, evaluación y envío
- **Dueño:**
- **Produce:** `out/<fase>_….csv.gz` + `score.json`; script `make submit-check`.
- **Hecho cuando:** serializa `allocations` con el formato de `FORMAT.md`, valida todas las reglas (líneas cubiertas, sin
  pares repetidos, categoría/excepción en lista, ≤ 60 MB y ≤ 3 M filas), puntúa la ventana con
  `starter/bankrec_score.py` y desglosa por segmento y por métrica; checklist de FINAL (un solo envío).
- **Verificar:** el fichero del starter puntúa igual que con `starter/score_train.py`; un fichero roto a propósito falla
  con nº de fila; ejecución completa LIVE de punta a punta en < 10 min.

---

## Orden recomendado si hay poca gente

T9 y T2 primero (sin arnés no se puede medir), luego T3 (pares baratos y seguros) y T8; T4, T5 y T6 en paralelo; T7 al
final. Cada mejora se mide en la ventana antes de gastar un envío LIVE.
