# Contratos de ficheros y esquemas

## 1. Alcance y vocabulario

Este documento define una arquitectura de **artefactos intermedios** para una futura solución de conciliación bancaria. No implementa código ni cambia los ficheros del reto.

- **Input oficial**: fichero entregado por el reto bajo `train/`, `live/` o `final/`, según `DATA_DICTIONARY.md`.
- **Artefacto intermedio propuesto**: fichero generado por la solución para desacoplar módulos. No se sube al evaluador.
- **Partida abierta / item**: sólo facturas AR/AP y abonos AR/AP (`ARI-*`, `API-*`, `ARC-*`, `APC-*`). Proformas, avisos de remesa, payment runs y notices no son items.
- **Asignación**: relación entre un movimiento bancario y un item, con importe aplicado.
- **Fase**: `train`, `live` o `final`. Los inputs y el universo de `bank_txn_id` se limitan a una fase.

Los datos son sintéticos. Las fechas se expresan como `YYYY-MM-DD`; los importes usan punto decimal y dos decimales, en la moneda indicada. Para tablas grandes se recomienda CSV UTF-8 con cabecera y gzip, pero los artefactos de trabajo pueden materializarse como Parquet.

## 2. Inputs oficiales, sin reinterpretación

Los siguientes ficheros son oficiales y no deben confundirse con contratos nuevos:

| Input oficial | Uso | Observaciones contractuales |
|---|---|---|
| `bank_transactions.csv.gz` | Movimientos bancarios normalizados | Una fila por línea bancaria, sin duplicados. `bank_txn_id` es estable y único en todo el dataset. |
| `ar_invoices.csv.gz`, `ap_invoices.csv.gz` | Facturas AR/AP | Incluyen importes, fechas, contraparte y método de pago. |
| `credit_notes.csv.gz` | Abonos AR/AP | `total_amount` es positivo; puede referenciar una factura. |
| `open_items_at_start.csv.gz` | Saldos abiertos iniciales | Sólo LIVE/FINAL; contiene el importe abierto al inicio de la fase. |
| `remittance_advices/...` e `index.csv` | Avisos de pago | PDF/CSV/XLSX/TXT; pueden llegar después del efectivo. |
| `pdfs/...` e `index.csv` | PDFs documentales | Texto libre con contexto no presente en CSV. |
| `direct_debit_batches/...` | Remesas de domiciliación | Una fila por recibo y un total de lote. |
| `card_settlements/...` | Liquidaciones de tarjetas | Una fila por pedido, con `gross`, `fee` y `net`. |
| `cheque_deposits/...` | Depósitos de cheques | Una fila por cheque y total de depósito. |
| `payment_runs/...` | Pagos salientes | Una fila por documento pagado, con tipos de ejecución distintos. |
| `masters/...` | Maestros y catálogos | Entidades, cuentas, clientes, proveedores, categorías, términos y FX. |
| `train/golden_allocations.csv.gz` | Etiquetas TRAIN | Sólo TRAIN; no es un input disponible para LIVE/FINAL. |

Los nombres y columnas de estos inputs se conservan conforme al diccionario oficial. Un pipeline puede añadir metadatos internos, pero no debe sobrescribir ni atribuir esos campos a la fuente si son derivados.

## 3. Convenciones comunes de los artefactos propuestos

### 3.1 Formato físico recomendado

- **Parquet**, comprimido con Zstandard, para intercambio entre módulos y ejecución con DuckDB/Polars.
- CSV UTF-8 con cabecera y `.csv.gz` sólo cuando se necesite interoperabilidad o inspección manual.
- Tipos físicos recomendados: fechas `DATE`, timestamps `TIMESTAMP` sólo para metadatos de ejecución, importes `DECIMAL(20, 6)`, códigos y referencias `VARCHAR`, indicadores `BOOLEAN`.
- No usar `float32/float64` para importes. Redondear únicamente cuando lo exijan los datos oficiales o la regla de negocio.
- Cada artefacto debe incluir `phase` y `schema_version` si se persiste fuera de una ejecución aislada. Ambos son metadatos **derivados**.

### 3.2 Claves y trazabilidad

Cada tabla debe declarar una clave primaria lógica y mantener referencias a los inputs mediante sus identificadores oficiales y, cuando proceda, `source_file`. No se deben inventar identificadores oficiales. Los IDs internos pueden tener un prefijo propio, por ejemplo `CAND-*`, pero son derivados y no sustituyen a `bank_txn_id` ni `item_id`.

### 3.3 Escala

- Leer por lotes o por particiones de `phase`, `booking_year`, `entity` y, cuando sea útil, `booking_month`.
- En DuckDB, consultar Parquet con `read_parquet(...)`, proyectar sólo columnas necesarias y explotar filtros sobre particiones.
- En Polars, usar `scan_parquet`, joins perezosos, expresiones vectorizadas y `maintain_order=False` salvo que el orden sea contractual.
- Evitar una explosión movimiento × todos los items. Generar candidatos por claves, contraparte, moneda, ventana temporal, lote o referencia y conservar sólo los top-N por movimiento.
- Mantener importes en la moneda del item en las decisiones de aplicación. Las conversiones a EUR son auxiliares derivadas para análisis, no sustituyen `amount` de la entrega.

## 4. Contrato A: ingestión normalizada de movimientos bancarios

**Fichero propuesto:** `artifacts/<phase>/01_bank_transactions.parquet`  
**Propósito:** copia canónica de `bank_transactions.csv.gz`, con tipos normalizados, deduplicación y trazabilidad al fichero bruto.  
**Productor:** módulo de ingestión. **Consumidores:** extracción de evidencias, generación de candidatos, decisión y auditoría.  
**Granularidad:** una fila por línea bancaria única.

### Campos mínimos

| Campo | Tipo semántico | Estado |
|---|---|---|
| `bank_txn_id` | ID oficial, `BT-<year>-<7 dígitos>` | oficial |
| `account_id` | ID de cuenta oficial | oficial |
| `entity` | código de entidad | oficial |
| `bank` | código de banco | oficial |
| `statement_format` | enum `n43`, `mt940`, `camt053` | oficial |
| `statement_file` | ruta/nombre de origen | oficial |
| `booking_date`, `value_date` | fecha | oficial |
| `amount` | decimal con signo, moneda de cuenta | oficial |
| `currency` | código de moneda | oficial |
| `bank_code`, `bank_subcode` | texto/código bancario | oficial |
| `ref1`, `ref2`, `text` | texto original normalizado | oficial |
| `counterparty_name`, `counterparty_account` | texto/IBAN cuando exista | oficial |
| `end_to_end_id`, `remittance_info` | texto nullable | oficial |
| `orig_amount`, `orig_currency` | decimal y moneda nullable | oficial |
| `phase`, `schema_version` | texto | derivado |
| `source_row_hash` | hash técnico de origen | derivado |

**Clave:** `bank_txn_id`.  
**Invariantes:** unicidad global del ID, pertenencia a la fase, no deduplicar por importe o texto, conservar el signo, conservar nulos cuando la fuente no proporciona el dato. Las repeticiones de extractos de Banca Ribagorza se resuelven en esta capa usando el ID/identidad de línea estable, no eliminando movimientos legítimos.

## 5. Contrato B: catálogo de items y open items

**Ficheros propuestos:**

- `artifacts/<phase>/02_items.parquet`
- `artifacts/<phase>/02_open_items.parquet`

**Propósito:** unificar facturas, abonos y saldos iniciales sin convertir documentos auxiliares en partidas conciliables.  
**Productor:** módulo de catálogo. **Consumidores:** evidencias, candidatos y decisiones.  
**Granularidad:** una fila por `item_id` en `items`; una fila por item abierto al inicio de la fase en `open_items`.

### Campos mínimos de `items`

| Campo | Tipo semántico | Estado |
|---|---|---|
| `item_id` | ID oficial `ARI/API/ARC/APC-*` | oficial |
| `item_type` | enum derivado de la fuente: `ARI`, `API`, `ARC`, `APC` | derivado |
| `side` | `AR`/`AP` | derivado u oficial según fuente |
| `entity` | código de entidad | oficial |
| `party_id`, `party_name` | ID y nombre de cliente/proveedor | oficial o normalizado desde fuente |
| `document_number` | número impreso | oficial |
| `issue_date`, `due_date` | fecha | oficial |
| `currency` | moneda del documento | oficial |
| `total_amount` | decimal positivo | oficial |
| `related_document` | documento relacionado nullable | oficial para abonos |
| `payment_terms`, `customer_po`, `our_po`, `payment_method` | texto nullable | oficial cuando aplique |
| `source_file`, `phase`, `schema_version` | texto | derivado |

### Campos mínimos de `open_items`

| Campo | Tipo semántico | Estado |
|---|---|---|
| `item_id`, `item_type`, `entity`, `party_id`, `document_number` | identificadores/texto | oficial |
| `issue_date`, `due_date` | fecha | oficial |
| `currency` | moneda | oficial |
| `total_amount`, `open_amount` | decimal positivo | oficial |
| `phase`, `as_of_date` | texto/fecha | derivado, salvo fecha definida por input |

**Clave:** `item_id`. **Invariantes:** IDs únicos, sólo los cuatro tipos oficiales, importes positivos, `open_amount <= total_amount` salvo que una política explícita indique otra cosa, moneda no nula. Un proforma o un aviso de remesa nunca se incorpora como item.

## 6. Contrato C: evidencias extraídas de remesas y PDFs

**Fichero propuesto:** `artifacts/<phase>/03_evidence.parquet`  
**Propósito:** representar observaciones extraídas de documentos y textos sin convertirlas automáticamente en verdad contable.  
**Productor:** módulo de extracción/OCR/parser. **Consumidores:** generación de candidatos y explicación de decisiones.  
**Granularidad:** una fila por observación atómica, no una fila por documento completo.

### Campos mínimos

| Campo | Tipo semántico | Estado |
|---|---|---|
| `evidence_id` | ID interno estable | derivado |
| `source_file` | ruta oficial del documento | oficial/derivado de índice |
| `source_type` | `remittance_advice`, `pdf`, `bank_text`, `batch`, `settlement`, `payment_run` | derivado |
| `source_document_id` | ID/número del índice si existe | oficial o derivado |
| `bank_txn_id` | ID bancario nullable | derivado/enlace |
| `observed_type` | `invoice_number`, `party_name`, `amount`, `currency`, `service_month`, `deduction`, `reason`, `iban`, `related_document`, etc. | derivado |
| `observed_value` | texto canónico | derivado |
| `observed_amount`, `observed_currency` | decimal/moneda nullable | derivado |
| `normalized_value` | texto o valor normalizado nullable | derivado |
| `page_or_row` | entero/texto nullable | derivado |
| `extractor`, `extractor_version`, `confidence` | metadatos | derivado |

**Clave:** `evidence_id`; unicidad recomendada de (`source_file`, `page_or_row`, `observed_type`, `observed_value`, `bank_txn_id`).  
**Invariantes:** no afirmar que una evidencia es una asignación; conservar el valor bruto y su normalización; `confidence` no reemplaza una regla de decisión; permitir múltiples layouts y múltiples observaciones por documento. Los campos sobre cuotas, retenciones, anticipos, anulaciones/sustituciones, descuentos, devoluciones, factoring o cambios de IBAN sólo pueden aparecer como evidencias si realmente se extraen del documento.

## 7. Contrato D: candidatos de matching

**Fichero propuesto:** `artifacts/<phase>/04_match_candidates.parquet`  
**Propósito:** enumerar hipótesis de relación movimiento-item antes de decidir.  
**Productor:** módulo de recuperación/candidate generation. **Consumidores:** ranking, reglas de políticas y auditoría.  
**Granularidad:** una fila por par candidato (`bank_txn_id`, `item_id`), pudiendo existir varias señales agregadas en la misma fila.

### Campos mínimos

| Campo | Tipo semántico | Estado |
|---|---|---|
| `bank_txn_id`, `item_id` | claves oficiales | oficial/enlace |
| `candidate_id` | ID técnico | derivado |
| `candidate_source` | `reference`, `name`, `iban`, `amount`, `batch`, `card`, `cheque`, `payment_run`, `remittance`, `pdf`, `fifo`, etc. | derivado |
| `evidence_ids` | lista de IDs internos | derivado |
| `score` | decimal normalizado | derivado |
| `amount_fit`, `date_fit`, `party_fit`, `reference_fit` | métricas derivadas | derivado |
| `rank` | entero por movimiento | derivado |
| `currency_relation` | enum derivado | derivado |
| `eligible` | booleano | derivado |
| `rejection_reason` | texto nullable | derivado |

**Clave lógica:** (`bank_txn_id`, `item_id`), salvo que se almacenen varias versiones, en cuyo caso añadir `run_id`/`model_version`. **Invariantes:** no presentar como decisión final; no incluir items fuera del universo de la fase; `score` no puede ser negativo; la generación debe permitir candidatos de importe igual que no son válidos, porque `UNIDENTIFIED` no debe forzarse a una factura coincidente.

## 8. Contrato E: decisiones de asignación

**Fichero propuesto:** `artifacts/<phase>/05_allocation_decisions.parquet`  
**Propósito:** resultado contable interno, todavía auditable antes de serializar la entrega oficial.  
**Productor:** módulo de decisión/políticas. **Consumidores:** validador, exportador y métricas.  
**Granularidad:** una fila por asignación oficial futura: una fila por item liquidado o exactamente una fila sin item cuando el movimiento no liquida item.

### Campos mínimos

| Campo | Tipo semántico | Estado |
|---|---|---|
| `bank_txn_id` | ID oficial | oficial/enlace |
| `item_id` | ID oficial nullable | oficial/enlace |
| `amount` | decimal positivo, en moneda del item | derivado por decisión |
| `category` | código oficial nullable | decisión |
| `exception` | código cerrado nullable | decisión |
| `decision_status` | `accepted`, `rejected`, `needs_review` | derivado |
| `decision_reason` | explicación técnica | derivado |
| `policy_version`, `run_id` | metadatos | derivado |
| `source_candidate_ids`, `source_evidence_ids` | listas internas | derivado |

**Clave:** (`bank_txn_id`, `item_id`), tratando `item_id` vacío como una única clave sin item.  
**Invariantes:** un movimiento con items no puede tener fila de categoría; un movimiento sin item tiene exactamente una fila con `item_id` y `amount` vacíos; `category` sólo se usa sin item; `amount` sólo se usa con item y siempre es positivo. `exception` debe pertenecer a la lista oficial. Los importes siguen `POLICIES.md`: por ejemplo, `PARTIAL` aplica lo cobrado, mientras que `SHORT_DISCOUNT`, `SHORT_BANK_CHARGES`, `SHORT_WITHHOLDING`, `FX_DIFFERENCE` y `OVERPAYMENT` liquidan el importe abierto completo del item según la regla correspondiente.

## 9. Contrato F: salida final oficial

**Fichero:** el fichero que se carga al reto para `LIVE` o `FINAL`. No es un artefacto enriquecido.

### Formato exacto

- Un único CSV por fase, UTF-8, separado por comas, con cabecera exacta de cinco columnas, en cualquier orden: `bank_txn_id,item_id,amount,category,exception`.
- Se admite opcionalmente gzip (`.csv.gz`). BOM, CRLF y espacios exteriores se toleran. El separador `;` también se acepta, con coma decimal en ese caso.
- Una fila por asignación. Límite: **60 MB sin comprimir y 3.000.000 de filas**.
- `bank_txn_id` debe pertenecer a la fase (`live/bank_transactions.csv.gz` o `final/...`). Omitir líneas cuenta como incorrecto.
- `item_id` debe ser una partida abierta oficial (`ARI-*`, `ARC-*`, `API-*`, `APC-*`) o vacío.
- `amount` es positivo, con punto decimal, en la moneda del item, y es obligatorio si hay `item_id`; vacío si no lo hay.
- `category` sólo se informa en filas sin item y debe ser uno de `masters/categories.csv`; debe estar vacío en filas con item.
- `exception` es vacío o uno de: `PARTIAL`, `OVERPAYMENT`, `SHORT_DISCOUNT`, `SHORT_BANK_CHARGES`, `SHORT_WITHHOLDING`, `SHORT_DISPUTE`, `DUPLICATE_PAYMENT`, `UNIDENTIFIED`, `RETURNED`, `FX_DIFFERENCE`.

### Reglas de validación del servidor

1. Cabecera con exactamente cinco columnas.
2. Cada `bank_txn_id` pertenece a la fase.
3. No se duplica el par (`bank_txn_id`, `item_id`), y hay como máximo una fila sin item por movimiento.
4. Un movimiento no puede mezclar filas con item y una fila de categoría.
5. `amount` es obligatorio y no negativo cuando hay item, y vacío sin item.
6. `category` y `exception` pertenecen a sus listas cerradas.

### Reglas de contenido que debe respetar el exportador

- Si una línea liquida varios items, emitir una fila por item.
- Si no liquida ningún item, emitir exactamente una fila sin item y con categoría.
- Un abono aplicado se emite con importe positivo.
- No emitir proformas, avisos, payment runs, PDFs o evidencias como `item_id`.
- No inventar `item_id`, categorías ni excepciones fuera de los catálogos oficiales.

En `FINAL` sólo existe un envío puntuado por equipo. Un fichero rechazado por validación no cuenta, aunque puede corregirse y volver a cargarlo; el resultado puntuado final es el único envío válido aceptado.

## 10. Validaciones recomendadas antes de exportar

1. Comparar el conjunto de `bank_txn_id` de decisiones contra el input de la fase y reportar faltantes, extras y duplicados.
2. Validar tipos, nulos, signos, monedas e IDs contra los maestros y tablas oficiales.
3. Validar invariantes por movimiento con una agregación, especialmente mezcla item/categoría y multiplicidad de filas sin item.
4. Validar que todo `item_id` existe y es conciliable, sin convertir proformas o documentos auxiliares.
5. Validar las listas cerradas desde `masters/categories.csv` y `FORMAT.md`.
6. Serializar una copia CSV y comprobar cabecera, tamaño descomprimido, número de filas y lectura posterior con DuckDB/Polars.
7. En TRAIN, comparar con `golden_allocations.csv.gz` sólo para evaluación local; no incluir etiquetas, `segment` ni `difficulty` en LIVE/FINAL.

Este contrato separa deliberadamente los datos oficiales de los artefactos derivados: los primeros son la autoridad del reto; los segundos permiten escalar, depurar y explicar una solución futura, pero nunca alteran el formato de entrega ni sus reglas.
