# Aportaciones de javier (Claude) — material para la descripción del proyecto

**FINAL: 94,22** (main 9cb8800, único envío, 18:40). Evolución de LIVE a lo largo del día: 11,47 → 21,75 → 52 →
71,55 → 82 → 91,07 → 93,33 → 94,03 → 94,11.

## Ideas clave en las que trabajé
- **T4 · tercero (`src/party`)**: quién paga cada línea con evidencias combinadas (IBAN histórico + maestros +
  avisos PDF de cambio de cuenta, mandato SEPA, prefijos de texto aprendidos en TRAIN, nombres actuales/comerciales/
  impresos, nº de documento, importe, rapidfuzz por nombre único). Confianza **calibrada empíricamente** para poder
  aplicar el umbral óptimo de F1 (≈ F1/2). Ventana: top-1 88,5 % → 94,3 % con órdenes de pago.
- **T3 · matcher (`src/match`)**: referencia de documento, tercero + importe, FIFO por orden, combinaciones
  (subset-sum), saldo vivo (duplicados, última cuota), reglas de POLICIES para excepciones e importes.
- **Arreglo crítico para FINAL**: la calibración de T4 sólo se generaba en la fase ventana; un checkout limpio de
  LIVE/FINAL perdía 2,2 puntos en silencio. Ahora se ajusta al vuelo con TRAIN y avisa.
- **Reglas aprendidas de los datos, validadas fuera de muestra** (aprender en Q1, medir en Q2-Q4):
  forma del sobrepago (exceso entero o 1 %/10 %), comisión bancaria sólo con señal de transferencia internacional en
  el texto, tabla de no identificados (importe redondo, facturas abiertas del tercero, evidencia de T4),
  **el cliente paga pedidos completos** (nunca parte un `customer_po`: 95 %, estable 2021-2025), retraso mínimo de
  pago por cliente.
- **Diagnóstico con el golden** (`tools/analysis/fail_report.py`): dónde se pierden los puntos por métrica, clase
  y segmento; qué huecos son recuperables y cuáles no (p. ej. disputas sin aviso en los datos).

## Stack
Python 3.14, duckdb + polars (todo vectorizado, nunca fila a fila sobre 1,9 M líneas), pyarrow, rapidfuzz, pypdf.
Sin LLM en el pipeline final: reglas generales aprendidas de TRAIN, sin ids ni importes a fuego, funciona sin red.
Pipeline reproducible (`src/final/run_final.sh`), un único Parquet de salida por etapa, rastro de auditoría
(`rule`, `confidence`, `evidence`) en cada decisión.

## Qué haríamos a continuación
- **Arbitraje global por cliente** para los cobros multi-factura sin referencia (≈ 2 puntos): resolver a la vez todas
  las líneas de un cliente en una ventana de días con sus facturas abiertas (pedidos completos + retraso mínimo),
  y recuperar facturas "robadas" por reglas débiles. Es el mayor hueco medido.
- **Autoaprendizaje dentro de la fase**: usar las decisiones casi seguras de FINAL (remesas, órdenes de pago, avisos)
  como pseudo-etiquetas para reaprender IBAN y nombres de clientes nuevos o renombrados (drift de ZA01 y renombrados).
- Abonos netos y N:M (bloques `ar_1toN_credit_note`, `ar_n_to_m`), aún con recall bajo.
