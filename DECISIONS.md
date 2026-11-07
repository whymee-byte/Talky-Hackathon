# Decisiones abiertas

Cada una: opciones y recomendación. Quien la cierre escribe **Decidido:** con fecha, hora y motivo (idealmente una
medida de la ventana o de LIVE).

### D1 · ¿Contra qué medimos en local?
- A) `sample/` (1 mes, segundos): rápido, pero todas las facturas antiguas aparecen abiertas → infla falsos positivos.
- B) Ventana TRAIN 2024-07 → 2025-06 con saldos calculados del golden anterior: ~370 k líneas, honesto.
- C) TRAIN completo: 1,49 M líneas, lento y supera el límite de 3 M filas (sólo local).
- **Recomendación:** B para decidir, A para pruebas de humo. Un cambio sólo se envía a LIVE si sube en B.

### D2 · Cobro sin partida segura: ¿qué ponemos?
- A) `UNIDENTIFIED/UNIDENTIFIED` (abstenerse).
- B) La mejor candidata aunque sea dudosa.
- C) Mejor candidata sólo si `confidence ≥ umbral` ajustado en la ventana; si no, A.
- **Recomendación:** C con umbral alto (precisión primero). Un par falso cuesta precisión **y** la línea; la
  abstención acierta en los `UNIDENTIFIED` reales (34,6 k en TRAIN, clase de la macro-F1) y en el resto sólo pierde
  recall.

### D3 · Pago (cargo) sin partida segura
- A) Categoría del clasificador T7 aunque sea baja confianza.
- B) `BANK_FEES` por defecto (lo más frecuente).
- C) `UNIDENTIFIED` (la política lo define para **cobros**).
- **Recomendación:** A, con B como último recurso. No usar `UNIDENTIFIED` en cargos.

### D4 · Importe de `PARTIAL`/`SHORT_DISPUTE` cuando el cobro está en otra divisa
- A) Convertir con `orig_amount`/`orig_currency` si existen; si no, con el tipo BCE del día de cargo.
- B) Siempre tipo BCE.
- C) Tipo implícito de otras facturas del mismo movimiento.
- **Recomendación:** A (la tolerancia es 0,5 % y el diferencial bancario 0,1–1,5 %: el BCE solo puede quedarse fuera en
  emergentes). Medir en `ar_fx`/`ap_fx` de la ventana.

### D5 · Orden temporal para llevar saldos
- A) Por `booking_date`, luego `bank_txn_id`.
- B) Por `value_date`.
- C) Dos pasadas: deterministas (lotes, avisos, referencias) primero, luego heurísticas FIFO sobre lo que quede.
- **Recomendación:** A + C. Los ids están ordenados por fecha de cargo; los avisos que llegan días después se resuelven
  en la primera pasada porque la entrada de LIVE está completa desde el inicio.

### D6 · Uso de LLM
- A) No usar LLM.
- B) LLM por patrón único / por maquetación nueva (avisos, PDFs, textos nuevos), con caché en disco.
- C) LLM por fila.
- **Recomendación:** B. Prohibido C (1,9 M líneas, coste y no determinista). Clave en `HACKATHON_API_KEY`; modelo en
  `HACKATHON_MODEL` (por defecto el Sonnet más reciente disponible; Haiku para clasificar patrones si el volumen lo
  pide). Cada respuesta se guarda en `llm_cache/` y se versiona, para que FINAL sea reproducible sin red.

### D7 · Partidas que ya no están abiertas al inicio de LIVE/FINAL
- A) Ignorarlas (sólo casar contra abiertas).
- B) Permitir casarlas sólo como `DUPLICATE_PAYMENT` con evidencia fuerte (referencia exacta + importe).
- **Recomendación:** B. `DUPLICATE_PAYMENT` es una clase (≈ 2 puntos) y por definición apunta a partidas ya liquidadas.

### D8 · Clases raras de excepción
- A) Detectarlas sólo con reglas exactas de `POLICIES.md`.
- B) Además, modelo aprendido en TRAIN.
- **Recomendación:** A. Cada clase vale lo mismo en la macro-F1; las reglas de la política son exactas y generalizan a
  FINAL; un modelo puede sobreajustar a patrones de TRAIN.

### D9 · Entidades y cuentas fuera de `masters/` (ZA01 ya en LIVE; FINAL trae `masters_final/`)
- A) Ignorar esas líneas (no aparecen en la entrega).
- B) Tratarlas con reglas genéricas (sin partida → clasificador por patrón; proforma → `CUSTOMER_ADVANCE`).
- **Recomendación:** B, y en FINAL cargar `masters_final/` antes que `masters/`. Nunca omitir líneas.

### D10 · Congelación para FINAL
- A) Congelar `main` a las 17:30 y sólo arreglar fallos.
- B) Seguir mejorando hasta las 19:00.
- **Recomendación:** A. Simulacro con LIVE a las 17:00; a las 18:00 sólo se ejecuta y se valida. Un único envío: un
  fallo tardío no se recupera.

### D11 · Idioma de la documentación
- A) Español (como los docs existentes).
- B) Inglés.
- **Recomendación:** A para docs del equipo; nombres de código, columnas y reglas (`rule`) en inglés.
