10:56 CLAIM T1 ingesta determinista de extractos N43, camt.053 y STA/MT940
11:00 DONE T1 smoke sample: 8716 normalizados, 8716 ids únicos, 0 errores; CLI verificada
11:13 NOTE T1 validación profunda: namespaces alternativos, signos, fechas MT940, malformed, deduplicación y segunda ejecución reproducible
11:16 NOTE T1 auditoría adicional: invariantes de 8716 JSONL, comparación oficial de fechas/importes, N43-24 y MT940 huérfanos trazados
11:18 NOTE T1 auditoría final: roundtrip Pydantic e IDs SHA-256 para 8716 filas; formatos no soportados conservan raw_record
11:20 NOTE T1 cobertura por fuente: N43 4085, camt 2094, MT940 2537; suma y logs = 8716 sin pérdidas
11:54 CLAIM T5 motor de excepciones e importes (src/exceptions/): pairs_raw �7b  allocations �8, mock desde golden mientras T3 no entregue 
11:51 DONE T5 motor pairs_raw+T2+no-item -; ventana real SCORE 50.39 (Allocation F1 53.52; txn acc 59.28; exception macro-F1 29.20). F1: RETURNED 0.623; SHORT_BANK_CHARGES 0.495; SHORT_DISCOUNT 0.493; UNIDENTIFIED 0.451; PARTIAL 0.347; SHORT_WITHHOLDING 0.218; OVERPAYMENT 0.163; FX_DIFFERENCE 0.128; DUPLICATE_PAYMENT 0; SHORT_DISPUTE 0. Entrada T3 medida: 416903 pares, 0 open_before=0, 0 doc_hint informado, por tanto duplicate/dispute no evaluables en pipeline real. Smoke reglas vectorizadas, py_compile y validate correctos; pendiente mock golden de la otra sesion para aislar T5. 
