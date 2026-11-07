10:51 CLAIM T4 identificación de tercero (src/party/)
11:42 DONE T4 ventana 2024-07→2025-06 (sin fuga): top-1 88,5 % · top-3 91,7 % · cobertura 93,9 %. Fallos: órdenes de pago agrupadas N43 y lotes (→ T2), homónimos (→ T3 mira los 3). Salida work/<fase>/line_party.parquet, confidence calibrada. Ver src/party/EVAL.md
11:51 CLAIM T3 matcher (src/match/): doc ref + tercero T4 + importe exacto/FIFO, luego 1:N
12:17 CLAIM T6 avisos de pago (src/remit/): ar_short_dispute + ar_1toN_remittance ≈ 20 % de los pares
12:27 CLAIM T2b payment_runs/tarjetas/cheques/confirming (src/sidefiles_ap/)
12:47 DONE T4 v2 payrun (run_id del texto + cuenta deudora + importe → beneficiario de payment_runs → tercero por doc+importe/IBAN/nombre) y filtro created≤fecha: ventana top-1 88,5 → 94,3 % · top-3 91,7 → 97,0 %; n43 pago_proveedor 25 → 99,2 %, payment_factory 39 → 100 %. Quedan: lotes (T2/T2b), homónimos US01 (T3 mira los 3).
12:50 CLAIM T7 movimientos sin partida (src/nonitem/): tabla patrón→categoría aprendida de TRAIN + Naive Bayes por tokens para patrones nuevos; sin LLM (funciona sin red). Q1: 55,03 → 56,05. Toco sólo la rama line_category del matcher (guarda de tercero para UNIDENTIFIED cuando T7 no opina).
12:55 DONE T7 ventana 52,84 → 53,87 (acc 60,3 → 62,9; macro-F1 36,5 → 37,8); nonitem_* 0 → ≈100 %, customer_refund/advance parciales (el resto los casa T3 con partida: p_item ≤ 0,5 en line_category podría vetarlos). Salida work/<fase>/line_category.parquet (sin red, sin LLM).
