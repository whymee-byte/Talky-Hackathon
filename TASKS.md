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

> **Actualización 13:58:** javier lleva la ventana a **82,18** y LIVE a **71,55** con `side_links_ap` + T3 v3 + T2 1-2 + T6 + T7
> (`aff5707`, en su máquina). Pero **en `main` no es reproducible**: `src/match` no lee `side_links_ap.parquet` y
> `src/remit/` no está en git. T10 sigue siendo urgente, ahora para *subir a `main`* lo que ya funciona. Además:
> **ventana 82,18 frente a LIVE 71,55 = 10,6 puntos de brecha**. Hay que explicarlo antes de ajustar nada más (ver
> T16): posible fuga en la ventana, saldos iniciales distintos (`open_items_at_start`) o drift de LIVE.
> Las cifras de recall de la revisión de abajo son las del 52,62. Según javier, `side_links_ap` ya cubre `ap_fx` (R 99,9),
> `ap_partial`, `ap_paid_by_holding` y `ap_credit_note_netted` (R 100), tarjetas y cheques. **Cada dueño vuelve a medir su
> segmento sobre el pipeline de T10 antes de empezar** y cierra la tarea si ya cumple el *Verificar*.
>
> **Revisión 13:55 (diagnóstico de la ventana 52,62, `out/window_20261107-1328_c5a5ffe.score.json`):** la precisión ya
> está bien (P 92 %). Lo que falta es el **recall** (R 38,7 %: no aparece el 61 % de los pares). Fallos encontrados, de más
> a menos puntos (en % de pares golden de la ventana):
> 1. **Integración rota (T10):** `work/window/side_links.parquet` es una versión antigua (sólo `dd_batch` y `dd_return`), y
>    `side_links_ap.parquet` (T2b) no lo lee nadie: `src/match` sólo abre `side_links.parquet`. Por eso
>    `ar_card_settlement`, `ar_cheque_deposit`, `ap_confirming` y `ap_returned_transfer` tienen R = 0, y
>    `ap_payment_multi` se queda en R 39 %, aunque T2/T2b ya los resuelven con P/R ≈ 99 %. Estimado **+8 a +12**.
> 2. **Avisos de pago sin hacer (T6):** `ar_short_dispute` + `ar_1toN_remittance` = **21,9 %** de los pares, con R ≈ 0.
>    `src/remit/` no existe. Además, la clase `SHORT_DISPUTE` tiene F1 = 0 (≈ 2 puntos de macro-F1).
> 3. **FX y pagos AP (T12):** `ap_fx` R 1,6 %, `ar_fx` R 5 %, `FX_DIFFERENCE` R 6,8 %; `ap_partial`, `ap_paid_by_holding` y
>    `ap_credit_note_netted` siguen por debajo del 30 % (≈ 11 % de los pares).
> 4. **Abonos neteados y 1:N (T13):** `ar_1toN_credit_note` R 28 %, `ar_n_to_m` R 21 % (≈ 4 %).
> 5. **Referencias ruidosas (T14):** `ar_ref_noise` R 49 % (6,5 % de los pares y el 21 % de las líneas, por lo que pesa
>    mucho en accuracy).
> 6. **Clases de excepción a cero (T15):** `DUPLICATE_PAYMENT` F1 0; `OVERPAYMENT` P 10 % (2.823 FP); `PARTIAL`
>    P 29 % (24.669 FP).
> 7. **LLM (T11):** no hay módulo común. Se usa sólo por patrón o maquetación y como red ante el drift de FINAL
>    (avisos de sep-2025, camt de Moncayo, ZA01). Presupuesto: 100 $ de API, tope duro de 40 $ en código.
>
> **Objetivo realista con el orden de abajo:** de 62 a 65 con T10; unos 75 con T6; de 85 a 90 con T12–T15. Para pasar
> de 95 haría falta casi perfección en todos los segmentos. Se intenta, pero **nunca a costa de sobreajustar**: ver
> *Reglas anti-sobreajuste* al final.

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
| **T10** | **Integración: un solo pipeline con todos los side_links** | `src/pipeline/`, `src/match/` (sólo la lectura) | **+8–12** (urgente, 14:30) | T2, T2b, T3 |
| **T11** | **Cliente LLM común (caché, presupuesto, sin red)** | `src/llm/` | habilita T6/T7/T8 ante el drift | — |
| T12 | FX y pagos AP: importe en divisa de la partida | `src/match/` (etapa fx) + `src/exceptions/` | +4–6 | T10 |
| T13 | Abonos neteados y N:M | `src/match/` (etapa comb) | +2–3 | T10 |
| T14 | Referencias ruidosas (`ar_ref_noise`) | `src/match/` (etapa ref) | +3–4 | T4 |
| T15 | Clases de excepción a cero o con mucho FP | `src/exceptions/` | +2–4 | T10, T6 |
| **T16** | **Explicar la brecha ventana 82 frente a LIVE 72** | `tools/`, `src/final/` | protege FINAL (±10) | T10 |

---

### T0 · Envío válido trivial + arnés
- **Dueño:** ouyang
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
- **Dueño: galileo**
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
- **Dueño:** ouyang
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
- **Dueño:** galileo
- **Entrada:** `pairs_raw.parquet` (§7b) — empezar con el mock del golden. **Salida:** `allocations.parquet` (§8).
- **Hecho cuando:** implementa `POLICIES.md` §2–3 como reglas generales: `FX_DIFFERENCE` (divisa distinta, banda
  ≤ 3 %, reemplaza `SHORT_*`/vacío, abonos sin excepción), `SHORT_DISCOUNT` (términos + fecha ≤ emisión + días + 3 +
  importe = total − % × total), `SHORT_WITHHOLDING` (1,25/1,5/10 % y país), `SHORT_BANK_CHARGES` (no SEPA, ≤ 60 € y
  ≤ 2 %), `OVERPAYMENT`, `DUPLICATE_PAYMENT` (saldo previo 0), `PARTIAL` (incluido descuento fuera de plazo, plazos,
  retención, anticipo), `SHORT_DISPUTE` (motivo en aviso), `RETURNED`; importe = saldo completo o lo pagado según §2.
- **Verificar:** sobre el mock de la ventana, F1 por clase ≥ 0,95 en las 10 clases y transaction accuracy de las líneas
  con partida ≥ 97 %; tabla F1 por clase en el PR.

### T6 · Avisos de pago ⚠ PRIORIDAD 1 tras T10 (21,9 % de los pares, R ≈ 0)
- **Dueño:**
- **Estado 13:55:** javier la reclamó a las 12:17, pero `src/remit/` todavía no existe. Si a las 14:30 no hay commit,
  que la coja otra persona.
- **Salida:** `work/<fase>/remit_lines.parquet` (§5), y que T3 la consuma como etapa `remit` entre `side` y `ref`.
- **Hallazgos (muestra de 1.500 avisos TRAIN):** hay unas **6 familias de maquetación**, todas deterministas:
  - CSV con `,` y CSV con `;`: cabecera `payment_ref,<ref>,payment_date,<d>,currency,<c>`, columnas
    `document,doc_date,gross,discount_or_deduction,reason,paid,your_order`, fila `TOTAL`;
  - XLSX: `Payment advice,<ref>,<date>`, columnas `Document,Date,Gross,Deduction,Reason,Paid`;
  - PDF "REMITTANCE ADVICE" (EN) y "AVISO DE PAGO" (ES): con `pypdf`, una celda por línea, la deducción va como
    `-249.51 PRICE DIFF`;
  - TXT tipo email ES/EN: `Subject: … <ref>`, una línea por documento con `doc  fecha  importe  [-deducción MOTIVO]`.
  - Motivos vistos: PRICE DIFF, DAMAGED GOODS, LOGISTIC FEE, SHORT DELIVERY, PENAL. RETRASO, COOP MKT.
  - Formatos numéricos `1,234.56` y `1.234,56`, fechas `dd/mm/yy`, `dd/mm/yyyy`, `mm/dd/yyyy` (email EN) e ISO.
- **Enlace con el banco:** (1) `payment_ref` normalizado contenido en `text_norm` y misma entidad; (2) si no aparece,
  mismo pagador (T4) + importe = total del aviso ± 1 céntimo + fecha del aviso entre −5 y +10 días desde el cargo. Medido
  sólo con CSV+TXT de la ventana: la referencia aparece en el banco en 931/2.123 líneas de `ar_short_dispute` y
  1.344/2.394 de `ar_1toN_remittance`. Con PDF+XLSX y el enlace por total debería pasar del 90 %.
- **Documento → partida:** `doc_norm` exacto → dígitos sin ceros → `po_norm` ("your_order") → número antiguo ("anula y
  sustituye" de T8); siempre dentro de las partidas del tercero/grupo.
- **Excepción e importe:** con deducción y motivo → `SHORT_DISPUTE`, importe = `paid`; si `paid` = total − % de
  descuento en plazo → `SHORT_DISCOUNT` con el importe completo (lo decide T5 con las reglas de POLICIES §3).
- **LLM (vía T11):** sólo cuando un aviso **no encaja en ninguna de las familias** (firma de maquetación desconocida,
  como el cambio de sep-2025). Una llamada por firma, que devuelve un *mapa de columnas o regex*, nunca los datos del
  aviso. El mapa se valida sobre el propio aviso: la suma de `paid` debe coincidir con el total; si no, se descarta.
- **Hecho cuando:** parsea pdf/csv/xlsx/txt (también el cambio de formato del gran cliente en sep-2025); resuelve la
  referencia del cliente a `item_id` (sin prefijo, nº de pedido, nº antiguo); captura deducción y motivo. LLM sólo para
  inferir la **plantilla** de cada maquetación nueva (una llamada por maquetación, en caché), nunca por aviso.
- **Verificar:** en la ventana, recall de pares ≥ 90 % y P ≥ 97 % en `ar_1toN_remittance` y `ar_short_dispute`; F1 de
  `SHORT_DISPUTE` ≥ 0,8; ≥ 95 % de líneas de aviso con `item_id`; nº de avisos parseados por mes en LIVE sin caída en
  sep-2025; tiempo < 5 min por fase (hay ≈ 51 k avisos en TRAIN, así que hay que paralelizar la lectura de PDFs como en
  `src/pdffacts/extract.py` y cachear el texto en `data_cache/<fase>/remit_text.parquet`).

### T7 · Movimientos sin partida
- **Dueño:**
- **Salida:** `work/<fase>/line_category.parquet` (§6).
- **Hecho cuando:** normaliza el texto a patrón (sin dígitos/ids/fechas) + `bank_code` + signo; tabla patrón →
  categoría aprendida en TRAIN; patrones nuevos (camt de Moncayo, ZA01) por LLM **una llamada por patrón** con caché;
  `BANK_REVERSAL` en ambas patas; `UNIDENTIFIED` sólo lo pone T3/T5 cuando no hay partida posible.
- **Verificar:** en la ventana, exactitud de categoría ≥ 98 % en líneas sin partida (excluido `UNIDENTIFIED`); nº de
  patrones LIVE sin categoría = 0; nº de llamadas LLM ≤ nº de patrones nuevos.

### T8 · Hechos de los PDFs
- **Dueño:** ouyang
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

### T10 · Integración: un solo pipeline con todos los side_links ⚠ URGENTE (antes de las 14:30)
- **Dueño:**
- **Problema:** cada módulo se ejecuta a mano y los ficheros de `work/<fase>/` se desfasan. La ventana 52,62 se puntuó
  con un `side_links.parquet` antiguo (sólo `T2.dd_batch` + `T2.dd_return`, de las 12:29) y sin `side_links_ap.parquet`.
  El 82,18 de javier (13:54) depende de ficheros y código locales que no están en `main`: `src/remit/` no está y
  `src/match` no lee `side_links_ap`. **Primer paso: javier sube su código de T6 y la lectura de `side_links_ap` en T3.**
  Hay riesgo de pares duplicados: `src/sidefiles` (ouyang) también genera `pr`/`card`/`chq`. Si se regeneran los dos,
  T3 leería dos veces la misma línea (sin deduplicar en `SL`).
- **Salida:** `src/pipeline/__main__.py`: `python -m src.pipeline --phase <fase> [--from <etapa>]` ejecuta en orden
  base_parquet → sidefiles (+ sidefiles_ap) → party → pdffacts → remit → nonitem → match → exceptions → submit
  (→ score en `window`/`sample`). Antes de cada etapa comprueba que sus entradas son más nuevas que su salida y escribe
  `work/<fase>/manifest.json` (etapa, commit, filas, segundos, sha de entradas). Es la base de `src/final/run_final.sh`.
- **Fusión de side_links:** T3 lee `side_links.parquet` **y** `side_links_ap.parquet`, deduplicando por
  `(bank_txn_id, item_id)`. Si los dos proponen pares distintos para el mismo `bank_txn_id`, gana el conjunto con mayor
  `confidence` media, y a igualdad el de mayor P medida en la ventana. Nunca se mezclan los pares de dos fuentes en la
  misma línea. **Propuesta:** `payment_runs`/tarjetas/cheques salen sólo de `src/sidefiles_ap` (P 99,98 %) y
  `src/sidefiles` se queda con remesas, devoluciones de recibos y `supplier_dd`. ouyang y javier lo confirman en el
  tablero.
- **Hecho cuando:** un único comando reproduce la nota de la ventana desde cero; `manifest.json` lista todas las etapas.
- **Verificar:** en la ventana, `ar_card_settlement`, `ar_cheque_deposit`, `ap_confirming`, `ap_payment_multi` y
  `ap_payment_single` con R ≥ 95 % y P ≥ 98 % en el informe de segmentos de `tools/score.py`; score de la ventana
  ≥ 80 en un clon limpio de `main`; LIVE subido y apuntado en `SUBMISSIONS.md`.

### T11 · Cliente LLM común
- **Dueño:**
- **Salida:** `src/llm/__init__.py` con `ask_json(module, prompt_version, key_text, prompt, schema) -> dict | None`.
- **Reglas:**
  - Clave en `HACKATHON_API_KEY` y modelo en `HACKATHON_MODEL` (por defecto el Sonnet más reciente; Haiku para
    clasificar patrones de texto). SDK `anthropic`, `temperature=0` y salida JSON validada contra `schema`.
  - Caché en `llm_cache/<module>/<sha256(prompt_version + key_text)>.json` con prompt, respuesta, modelo, tokens y
    coste. Se versiona en git: **FINAL debe correr sólo con caché**.
  - **Presupuesto:** gasto acumulado en `llm_cache/_spend.json`. Tope duro de 40 $ (`HK_LLM_BUDGET_USD`) y como mucho
    500 llamadas por ejecución. Al llegar al tope devuelve `None` y no lanza excepciones.
  - **Sin red:** `HK_OFFLINE=1`, sin clave o con error de red, devuelve `None` y el módulo usa su fallback
    determinista. Nunca rompe el pipeline.
  - **Nunca por fila:** el llamador agrupa antes por patrón o firma; `ask_json` registra cuántas claves distintas
    pidió cada módulo.
- **Usos previstos (por orden de valor):**
  1. T6: firma de maquetación desconocida → mapa de columnas o regex (validado por suma = total);
  2. T7: patrón de texto nuevo sin categoría aprendida (camt de Moncayo desde 2025-10, ZA01) → categoría de
     `categories.csv`, con los 30 patrones TRAIN más parecidos como ejemplos;
  3. T8: plantilla de PDF desconocida → hechos (plazos, retención, anticipo, sustituye, mes, IBAN);
  4. T4: sólo por nombre de pagador sin resolver (normalizado y agrupado), para elegir entre los 5 clientes más
     parecidos del maestro o decir "ninguno".
- **Hecho cuando:** test con red y sin red; una segunda ejecución hace 0 llamadas; `_spend.json` cuadra.
- **Verificar:** ensayo de LIVE con `HK_OFFLINE=1` y caché vacía: misma estructura de salida, sin errores; coste total
  de LIVE+FINAL < 15 $.

### T12 · FX y pagos AP: importe en divisa de la partida
- **Dueño:**
- **Problema:** `ap_fx` R 1,6 %, `ar_fx` R 5 %, `FX_DIFFERENCE` R 6,8 %. El enlace de T2 existe (R 96 %), pero se pierde
  o se le pone mal el importe al cruzar divisas. `ap_partial` R 19 %, `ap_paid_by_holding` R 16 % (P 56 %),
  `ap_credit_note_netted` R 29 %.
- **Hecho cuando:**
  - en líneas con divisa ≠ divisa de la partida, el importe es el saldo completo en divisa de la partida con
    `FX_DIFFERENCE` si la diferencia contra `fx_rates.csv` (ASOF último día hábil) es ≤ 3 %, y `PARTIAL` si es mayor;
  - los abonos van sin excepción;
  - `orig_amount`/`orig_currency` tienen prioridad sobre el BCE (D4);
  - payment factory ES00 → partidas de ES01/ES02/PT01 por `payment_runs`, no por tercero.
- **Verificar:** en la ventana, R ≥ 90 % y P ≥ 97 % en `ap_fx`, `ar_fx`, `ap_paid_by_holding`, `ap_partial` y
  `ap_credit_note_netted`; F1 de `FX_DIFFERENCE` ≥ 0,9.

### T13 · Abonos neteados y N:M
- **Dueño:**
- **Problema:** `ar_1toN_credit_note` R 28 %, `ar_n_to_m` R 21 % (P 65 %). El subset-sum actual sólo admite 1–3 partidas.
- **Hecho cuando:** completar con abonos del tercero (`related_doc` primero) cuando las facturas citadas superan el
  efectivo; subset-sum acotado de hasta 6 partidas con poda por fecha y solución única; N:M = varias líneas del
  mismo tercero en ≤ 5 días que juntas cierran un conjunto de facturas (se reparte por orden de fecha).
- **Verificar:** en la ventana, R ≥ 80 % y P ≥ 95 % en `ar_1toN_credit_note`, `ar_1toN_text` y `ar_n_to_m`; la
  precisión global de pares no baja.

### T14 · Referencias ruidosas
- **Dueño:**
- **Problema:** `ar_ref_noise` R 49 % (184 k pares, el 21 % de las líneas).
- **Hecho cuando:** casa números con 1 dígito cambiado o 2 transpuestos (distancia de Damerau 1 sobre `doc_digits`
  del tercero), truncados a 16/35/38 caracteres (prefijo ≥ 6 dígitos), sin prefijo o sin ceros; siempre restringido al
  tercero T4 (top-3) y a partidas abiertas; desempate por importe y después FIFO. Confianza calibrada en TRAIN < 2024-07.
- **Verificar:** en la ventana, R ≥ 85 % y P ≥ 95 % en `ar_ref_noise`; curva de score frente al umbral de confianza
  (0,2–0,8) en el PR.

### T15 · Clases de excepción a cero o con mucho FP
- **Dueño:** galileo (dentro de T5)
- **Problema (ventana):** `DUPLICATE_PAYMENT` F1 0 (1.306 en golden); `OVERPAYMENT` 321 TP frente a 2.823 FP;
  `PARTIAL` 10.150 TP frente a 24.669 FP; `SHORT_WITHHOLDING` R 12 %; `UNIDENTIFIED` P 45 %.
- **Hecho cuando:**
  - `DUPLICATE_PAYMENT`: partida con saldo 0 al inicio (D7) o liquidada antes en la fase + referencia exacta + importe;
  - `OVERPAYMENT` sólo con una única factura citada y exceso ≤ 10 %; si el exceso cuadra con otra partida, es 1:N;
  - `PARTIAL` sólo si queda saldo y no hay otra explicación (descuento en plazo, retención, comisión, FX);
  - `UNIDENTIFIED` sólo si ningún candidato del tercero pasa el umbral.
- **Verificar:** F1 ≥ 0,8 en las 10 clases sobre la ventana (con el pipeline real, no con el mock); macro-F1 ≥ 0,8.

### T16 · Explicar la brecha ventana frente a LIVE (82,18 frente a 71,55)
- **Dueño:**
- **Por qué importa:** FINAL se parece a LIVE, no a la ventana. Si la ventana sobreestima 10 puntos, todos los ajustes
  hechos con ella son sospechosos y el único envío FINAL puede salir mucho peor de lo esperado.
- **Hipótesis a comprobar, en orden:**
  1. **Fuga en la ventana:** tablas, densidades o umbrales aprendidos con TRAIN completo (incluida la ventana) en vez
     de TRAIN < 2024-07. Revisar `work/party_model/` (`pre_window` frente a `full`), `src/nonitem` (¿patrones aprendidos
     con la ventana?), `dd_return_lag.csv` y cualquier tabla en `work/learn/`.
  2. **Saldos iniciales:** en la ventana, `open_at_start_cents` sale del golden (perfecto); en LIVE sale de
     `open_items_at_start.csv.gz`. Comparar el % de pares que apuntan a partidas "cerradas" o que no existen.
  3. **Cobertura de ficheros de detalle en LIVE:** pares de `side_links_ap` por línea y mes en LIVE frente a la
     ventana (¿hay runs/lotes de LIVE cuyo detalle cae fuera de la fase?).
  4. **Drift:** informe por mes de LIVE (% de líneas con partida, % `UNIDENTIFIED`, % de cada etapa `rule`) frente a
     la ventana; buscar caídas en 2025-09 (avisos), 2025-10 (Moncayo camt) y en ZA01.
  5. **Mezcla de segmentos:** si LIVE tiene más `ar_short_dispute`/`ar_1toN_remittance` (T6 débil), la brecha es
     estructural y no se debe a una fuga.
- **Hecho cuando:** brecha explicada por hipótesis con cifras; las fugas corregidas; la ventana *honesta* recalculada.
- **Verificar:** tras corregir, |ventana − LIVE| ≤ 3 puntos, o la diferencia queda justificada por segmentos en
  `SUBMISSIONS.md`. A partir de ahí, cada envío LIVE apunta la brecha.

---

## Reglas anti-sobreajuste (obligatorias para T6–T16)

1. **Aprender sólo con TRAIN < 2024-07** (`WINDOW_START`) y medir en la ventana. Todo umbral, tabla o densidad
   aprendida con datos de la ventana se marca en el PR como *in-sample* y no cuenta como mejora.
2. **Hold-out temporal:** antes de subir un cambio de umbral, comprobar que mejora en **los 4 trimestres** de la ventana
   por separado (`--subset q1..q4`), no sólo en el total.
3. **Nada a fuego:** ningún id, nombre de cliente, importe, referencia o fecha concreta en el código ni en los
   prompts. Las familias de maquetación se describen por su estructura, no por el nombre del pagador.
4. **LLM sin etiquetas:** el LLM nunca ve el golden ni líneas de LIVE/FINAL con su respuesta esperada. Los ejemplos
   *few-shot* salen sólo de TRAIN < 2024-07, y su salida se valida con una regla determinista (suma = total, categoría
   dentro de la lista cerrada).
5. **LIVE como test, no como entrenamiento:** se sube sólo lo que mejora en la ventana. Si la nota LIVE se separa
   más de 3 puntos de la ventana, se investiga antes de seguir ajustando.
6. **Drift:** cada módulo tiene un fallback determinista para lo que no ha visto (maquetación, patrón, entidad nueva)
   y un informe por mes de LIVE sin caídas en sep-2025, oct-2025 ni en ZA01.

---

## Reparto sugerido (3–4 personas)

- **Persona A:** T0 → T5 (arnés y excepciones). **T0 antes de las 12:00.**
- **Persona B:** T1 → T2 (base y lotes: el mayor bloque de pares). **T1 antes de las 12:00.**
- **javier:** T4 → T3 (tercero y matcher, el núcleo) y **responsable de FINAL** (confirma el único envío).
- **Persona D (o la primera que acabe):** T7 → T6 → T8; T9 a partir de las 16:00 con quien esté libre.

### Reparto desde las 14:00 (revisión 13:55)

| Hora | ouyang | javier | galileo | libre / agente |
|---|---|---|---|---|
| 14:00–14:30 | **T10** fusión side_links + pipeline | **subir T6 + lectura de side_links_ap a `main`** | T15 sobre pipeline real | **T16** brecha + **T11** `src/llm/` |
| 14:30–16:00 | T12 FX / AP | T6 enlace banco + T3 etapa `remit` | T15 + T13 | T14 |
| 16:00–17:00 | T9 ensayo sin red | T9 ensayo + LIVE | ajuste de umbrales (4 trimestres) | T11 en T7/T6 para el drift |
| 17:00 | simulacro FINAL con LIVE | | | |
| 17:30 | **congelación de `main`** | | | |
