10:51 CLAIM T4 identificación de tercero (src/party/)
11:42 DONE T4 ventana 2024-07→2025-06 (sin fuga): top-1 88,5 % · top-3 91,7 % · cobertura 93,9 %. Fallos: órdenes de pago agrupadas N43 y lotes (→ T2), homónimos (→ T3 mira los 3). Salida work/<fase>/line_party.parquet, confidence calibrada. Ver src/party/EVAL.md
11:51 CLAIM T3 matcher (src/match/): doc ref + tercero T4 + importe exacto/FIFO, luego 1:N
12:17 CLAIM T6 avisos de pago (src/remit/): ar_short_dispute + ar_1toN_remittance ≈ 20 % de los pares
12:27 CLAIM T2b payment_runs/tarjetas/cheques/confirming (src/sidefiles_ap/)
