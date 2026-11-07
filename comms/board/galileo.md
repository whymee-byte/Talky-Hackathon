10:56 CLAIM T1 ingesta determinista de extractos N43, camt.053 y STA/MT940
11:00 DONE T1 smoke sample: 8716 normalizados, 8716 ids únicos, 0 errores; CLI verificada
11:13 NOTE T1 validación profunda: namespaces alternativos, signos, fechas MT940, malformed, deduplicación y segunda ejecución reproducible
11:16 NOTE T1 auditoría adicional: invariantes de 8716 JSONL, comparación oficial de fechas/importes, N43-24 y MT940 huérfanos trazados
11:18 NOTE T1 auditoría final: roundtrip Pydantic e IDs SHA-256 para 8716 filas; formatos no soportados conservan raw_record
11:20 NOTE T1 cobertura por fuente: N43 4085, camt 2094, MT940 2537; suma y logs = 8716 sin pérdidas
11:59 NOTE T5 §2: importes PARTIAL/SHORT_DISPUTE toman cash_cents por partida; ventana SCORE 50.39→50.55, allocation F1 53.52→54.02, txn accuracy 59.28→59.01; validación OK
12:04 CLAIM T5 motor de excepciones e importes; revisar e implementar según POLICIES §2–3
12:07 NOTE T5 medición ventana con mock golden: F1 FX 0,971; descuento 0,901; parcial 0,882; retención 0,582; resto ≥0,965 salvo UNIDENTIFIED sin mock. CLI genera 751.520 filas y valida formato, pero cubre 396.957/1.489.931 líneas; no declarar DONE hasta cerrar F1/coverage
