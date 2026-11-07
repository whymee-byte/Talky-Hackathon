# Tareas

Ordenadas por **puntos esperados** (estimación a partir de `docs/SCORING.md`; T1 es requisito de casi todo y va
justo después de T0). Cada tarea vive en **su carpeta** `src/<módulo>/`, lee/escribe los ficheros de
`contracts/schemas.md` y se puede hacer en paralelo usando el *mock desde golden* del contrato que necesite.

Para coger una tarea: pon tu nombre en **Dueño**, escribe `CLAIM T<n>` en `comms/board/<tu-nombre>.md`, commit + push.
Al acabar: `DONE T<n>` con la medida de "Verificar" pegada. Reglas completas en `CLAUDE.md`.

Horario (web): datos FINAL **18:00** · **FINAL subido antes de las 18:30** (empate = gana el más temprano) · cierre de
envíos **19:30** · entrega de proyecto **19:45**. Lo que esté en `main` y verificado a las **17:30** entra en FINAL.
**T0 y T1 desbloquean a todos: cerrados antes de las 12:00.**

> **Fusión 11:00:** este plan integra la propuesta paralela de galileogUCM (8 tareas por capas). Equivalencias:
> su T1 ingesta → T1 · T2 ítems y saldos → T1 · T3 PDFs y remesas → T6 + T8 · T4 candidatos → T3 + T4 + T2 ·
> T5 excepciones y categorías → T5 + T7 · T6 saldo temporal y devoluciones → T3 + T5 · T7 evaluación → T0 ·
> T8 pipeline → T0 + T9. Sus comprobaciones de robustez se recortaron a las que dan puntos hoy (`validate.py` con las reglas de `FORMAT.md`). Su T1 incluía
> parsear los extractos brutos: no hace falta, `bank_transactions.csv.gz` ya es esa vista normalizada y sin duplicados.

> `src/recon.py` es un prototipo monolítico anterior (≈ 51,6 en `sample`, no comparable con la ventana): úsalo como
> referencia de reglas, no como base de código.

| # | Tarea | Carpeta | Puntos esperados | Depende de (mock si no está) |
|---|---|---|---|---|
| T0 | Envío válido trivial + arnés de evaluación y envío | `src/submit/`, `tools/` | 10–19 (+ desbloquea todo) | — |
| T1 | Base: movimientos, partidas, saldos de ventana | `src/base/` | habilita | — |
| T2 | Ficheros de detalle (remesas, tarjetas, cheques, órdenes de pago) | `src/sidefiles/` | +20–30 | T1 |
| T3 | Matcher con saldo vivo (tercero + referencia + importe) | `src/match/` | +15–20 | T1, T2, T4 |
| T4 | Identificación de tercero | `src/party/` | (dentro de T3) | T1 |
| T5 | Motor de excepciones e importes | `src/exceptions/` | +8–12 | `pairs_raw` (mock) |
| T6 | Avisos de pago | `src/remit/` | +8–10 | T1 |
| T7 | Movimientos sin partida (clasificador por patrón + LLM) | `src/nonitem/` | +3–5 | T1 |
| T8 | Hechos de los PDFs | `src/pdffacts/` | +2–4 | T1 |
| T9 | Drift y preparación de FINAL | `src/final/` | protege el único envío | todas |

---

### T0 · Envío válido trivial + arnés
- **Dueño:**
- **Entradas:** `bank_transactions` de la fase; `allocations.parquet` cuando exista.
- **Salidas:** `out/<fase>_<yyyymmdd-hhmm>_<sha>.csv.gz`, `out/<…>.score.json`; `tools/validate.py`, `tools/score.py`,
  `tools/submit.py` (envoltorio del script oficial de `/enviar`, token en `HK_TOKEN`).
- **Hecho cuando:** (1) existe un fichero LIVE válido con **todas** las líneas: pares exactos del starter + resto
  `UNIDENTIFIED/UNIDENTIFIED` (cobros) o `BANK_FEES` (pagos); (2) `validate.py` reproduce todas las reglas de
  `FORMAT.md` con nº de fila; (3) `score.py` puntúa `sample` y la ventana con `starter/bankrec_score.py` y desglosa por
  métrica y segmento; (4) el primer envío LIVE está hecho y apuntado en `SUBMISSIONS.md`; `submit.py` se niega a subir si falla `validate.py`.
- **Verificar:** un fichero roto a propósito falla con nº de fila; el del starter puntúa igual con `score.py` y con
  `starter/score_train.py`; la nota LIVE queda apuntada en `SUBMISSIONS.md`.

### T1 · Base: movimientos, partidas y saldos de ventana
- **Dueño:**
- **Salidas:** `work/<fase>/lines.parquet` (§1), `work/<fase>/items.parquet` (§2) para `sample`, `window`, `live`
  (y `final` cuando salga).
- **Hecho cuando:** 1 fila por `bank_txn_id`; extrae `cp_name_norm`, `ibans`, `mandate_ids`, `batch_refs`, `doc_tokens`,
  `month_hint`, `is_return_hint` en N43, MT940 y camt (incluido Moncayo camt desde 2025-10); partidas AR+AP+abonos con
  condiciones de pago, `open_items_at_start` en LIVE/FINAL; ventana con saldo = total − asignado por el golden antes de
  2024-07-01 (sumando `RETURNED`; 0 si la excepción liquida entera).
- **Verificar:** `count(*) = count(distinct bank_txn_id)` = líneas de la fase; % de pares del golden cuyo `doc_norm`
  está en `doc_tokens` ≥ 21,8 % (búsqueda literal medida); `cp_name_norm` no vacío en ≥ 90 % de cobros por
  transferencia; en LIVE `sum(open_at_start)` por tipo = `open_items_at_start`; en la ventana ≥ 99 % de pares del golden
  apuntan a partidas con saldo > 0; LIVE en < 2 min.

### T2 · Ficheros de detalle
- **Dueño:**
- **Salida:** `work/<fase>/side_links.parquet` (§3).
- **Hecho cuando:** enlaza cada movimiento de lote con todas sus partidas: remesa SEPA/BACS/ACH por `batch_id`,
  devoluciones por mandato + remesa (`RETURNED`), tarjeta por comercio/fecha/neto (importe **bruto** por pedido),
  cheques por `deposit_id`, `payment_runs` por e2e / run / vencimiento de confirming, incluida la payment factory
  ES00 → ES01/ES02/PT01 y abonos netos.
- **Verificar:** en la ventana, precisión ≥ 98 % y recall ≥ 95 % de pares en `ar_dd_batch`, `ar_dd_returned`,
  `ar_card_settlement`, `ar_cheque_deposit`, `ap_payment_multi`, `ap_payment_single`, `ap_confirming`,
  `ap_paid_by_holding`, `ap_credit_note_netted`, `ap_direct_debit` (≈ 37 % de los pares).

### T3 · Matcher con saldo vivo
- **Dueño:** javier
- **Entradas:** `lines`, `items`, `side_links`, `line_party`, `remit_lines`, `doc_facts`, `line_category` (mocks del
  golden mientras no existan).
- **Salida:** `work/<fase>/pairs_raw.parquet` (§7b).
- **Hecho cuando:** procesa por fecha de cargo llevando el saldo de cada partida (vectorizado por día/tercero, no fila a
  fila); prioridad `side_links` → avisos → referencia de documento → tercero + importe (exacto, subset-sum con abonos,
  FIFO, mes de servicio, factura sustituta) → sin partida; casa si `confidence` ≥ umbral (ajustado en la ventana, partiendo de ≈ F1/2 ≈ 0,4); si no, la
  línea queda para T7 / `UNIDENTIFIED`. Desempates deterministas; un importe igual nunca basta por sí solo.
- **Verificar:** con **todos** los mocks del golden, score de ventana ≥ 90 (techo); con módulos reales, mejora sobre
  la heurística de referencia (≈ 61); tabla por segmento en el PR; curva score de ventana vs umbral (0,2–0,8) para elegirlo.

### T4 · Identificación de tercero
- **Dueño:** javier
- **Salida:** `work/<fase>/line_party.parquet` (§4).
- **Hecho cuando:** combina IBAN (maestro + IBANs aprendidos en TRAIN + avisos de cambio de IBAN), mandato, nombre
  (truncado, comercial, renombrados), pagador por cuenta de otro (matriz/grupo, confirming "ORD. …", agencia "EXPTE …")
  e historial; hasta 3 candidatos con `rule/confidence/evidence`.
- **Verificar:** en la ventana, top-1 = tercero del golden en ≥ 95 % de las líneas con partida, top-3 ≥ 98 %; desglose
  por formato (N43/MT940/camt) y en `ar_third_party_*`, `ar_new_iban`.

### T5 · Motor de excepciones e importes
- **Dueño:**
- **Entrada:** `pairs_raw.parquet` (§7b) — empezar con el mock del golden. **Salida:** `allocations.parquet` (§8).
- **Hecho cuando:** implementa `POLICIES.md` §2–3 como reglas generales: `FX_DIFFERENCE` (divisa distinta, banda
  ≤ 3 %, reemplaza `SHORT_*`/vacío, abonos sin excepción), `SHORT_DISCOUNT` (términos + fecha ≤ emisión + días + 3 +
  importe = total − % × total), `SHORT_WITHHOLDING` (1,25/1,5/10 % y país), `SHORT_BANK_CHARGES` (no SEPA, ≤ 60 € y
  ≤ 2 %), `OVERPAYMENT`, `DUPLICATE_PAYMENT` (saldo previo 0), `PARTIAL` (incluido descuento fuera de plazo, plazos,
  retención, anticipo), `SHORT_DISPUTE` (motivo en aviso), `RETURNED`; importe = saldo completo o lo pagado según §2.
- **Verificar:** sobre el mock de la ventana, F1 por clase ≥ 0,95 en las 10 clases y transaction accuracy de las líneas
  con partida ≥ 97 %; tabla F1 por clase en el PR.

### T6 · Avisos de pago
- **Dueño:**
- **Salida:** `work/<fase>/remit_lines.parquet` (§5).
- **Hecho cuando:** parsea pdf/csv/xlsx/txt (también el cambio de formato del gran cliente en sep-2025); resuelve la
  referencia del cliente a `item_id` (sin prefijo, nº de pedido, nº antiguo); captura deducción y motivo. LLM sólo para
  inferir la **plantilla** de cada maquetación nueva (una llamada por maquetación, en caché), nunca por aviso.
- **Verificar:** ≥ 95 % de líneas de aviso con `item_id` en la ventana; con avisos, recall de pares ≥ 95 % en
  `ar_1toN_remittance` y `ar_short_dispute`; nº de avisos parseados por mes en LIVE sin caída en sep-2025.

### T7 · Movimientos sin partida
- **Dueño:**
- **Salida:** `work/<fase>/line_category.parquet` (§6).
- **Hecho cuando:** normaliza el texto a patrón (sin dígitos/ids/fechas) + `bank_code` + signo; tabla patrón →
  categoría aprendida en TRAIN; patrones nuevos (camt de Moncayo, ZA01) por LLM **una llamada por patrón** con caché;
  `BANK_REVERSAL` en ambas patas; `UNIDENTIFIED` sólo lo pone T3/T5 cuando no hay partida posible.
- **Verificar:** en la ventana, exactitud de categoría ≥ 98 % en líneas sin partida (excluido `UNIDENTIFIED`); nº de
  patrones LIVE sin categoría = 0; nº de llamadas LLM ≤ nº de patrones nuevos.

### T8 · Hechos de los PDFs
- **Dueño:**
- **Salida:** `work/<fase>/doc_facts.parquet` (§7).
- **Hecho cuando:** extrae plazos, retención, anticipo a deducir, "anula y sustituye", mes de servicio, rappel
  compensado/pagado, abono reembolsado/compensado, factoring, nuevo IBAN (ES/FR/DE/EN…); regex primero, LLM sólo por
  plantilla de PDF desconocida.
- **Verificar:** en la ventana, ≥ 90 % de las partidas de `ar_instalment`, `ar_retention`, `ar_advance_deduction`,
  `ar_replaced_invoice`, `ar_same_amount_month`, `ar_new_iban` tienen su hecho; 20 PDFs revisados a mano sin falsos
  positivos.

### T9 · Drift y preparación de FINAL
- **Dueño:** (responsable de FINAL: javier)
- **Salida:** `src/final/run_final.sh` (un comando de punta a punta), `docs/FINAL_CHECKLIST.md`.
- **Hecho cuando:** el pipeline completo corre en LIVE en < 15 min y **sin red** (sin LLM ni caché, todo patrón nuevo
  → categoría por defecto según signo; nunca un fallo); informe por mes de LIVE (cobertura de pares,
  % sin partida, % `UNIDENTIFIED`) sin caídas en 2025-09 (avisos) ni 2025-10 (Moncayo camt); maneja entidades/cuentas
  no presentes en `masters/` (ZA01 ya aparece en LIVE) y `final/masters_final/`; simulacro de FINAL con LIVE a las 17:00.
- **Verificar:** ensayo cronometrado con la red cortada; el fichero pasa `validate.py`; checklist firmado por el
  responsable de FINAL. **Objetivo: envío FINAL validado y subido antes de las 18:30** (el empate gana el más temprano).

---

## Reparto sugerido (3–4 personas)

- **Persona A:** T0 → T5 (arnés y excepciones). **T0 antes de las 12:00.**
- **Persona B:** T1 → T2 (base y lotes: el mayor bloque de pares). **T1 antes de las 12:00.**
- **javier:** T4 → T3 (tercero y matcher, el núcleo) y **responsable de FINAL** (confirma el único envío).
- **Persona D (o la primera que acabe):** T7 → T6 → T8; T9 a partir de las 16:00 con quien esté libre.
