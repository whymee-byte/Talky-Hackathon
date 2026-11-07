10:56 CLAIM T1 ingesta determinista de extractos N43, camt.053 y STA/MT940
11:00 DONE T1 smoke sample: 8716 normalizados, 8716 ids únicos, 0 errores; CLI verificada
11:13 NOTE T1 validación profunda: namespaces alternativos, signos, fechas MT940, malformed, deduplicación y segunda ejecución reproducible
11:16 NOTE T1 auditoría adicional: invariantes de 8716 JSONL, comparación oficial de fechas/importes, N43-24 y MT940 huérfanos trazados
11:18 NOTE T1 auditoría final: roundtrip Pydantic e IDs SHA-256 para 8716 filas; formatos no soportados conservan raw_record
11:20 NOTE T1 cobertura por fuente: N43 4085, camt 2094, MT940 2537; suma y logs = 8716 sin pérdidas
11:59 NOTE T5 §2: importes PARTIAL/SHORT_DISPUTE toman cash_cents por partida; ventana SCORE 50.39→50.55, allocation F1 53.52→54.02, txn accuracy 59.28→59.01; validación OK
12:04 CLAIM T5 motor de excepciones e importes; revisar e implementar según POLICIES §2–3
12:07 NOTE T5 medición ventana con mock golden: F1 FX 0,971; descuento 0,901; parcial 0,882; retención 0,582; resto ≥0,965 salvo UNIDENTIFIED sin mock. CLI genera 751.520 filas y valida formato, pero cubre 396.957/1.489.931 líneas; no declarar DONE hasta cerrar F1/coverage
12:19 NOTE T5: corrige falsos RETURNED/PARTIAL en abonos neteados y prioridad T2 ante solapes obsoletos; 3 tests pasan. Pipeline público T7→T3→T5→submit: SCORE ventana 51,59→52,61; 396.957/396.957 ids de ventana cubiertos, validate.py --txns OK. Mock T5 sigue bajo objetivo en descuento 0,901, parcial 0,882 y retención 0,582; no DONE
12:28 NOTE T5: regla general de retención por factura en pagos mixtos; 4 tests + compileall + validate.py por ids de ventana OK. Mock F1 retención 0,5816→0,5853 (+38 TP, +11 FP); score público 52,61→52,62. Descuento/parcial/retención siguen bajo 0,95; no DONE
13:37 NOTE T5 diagnóstico de alcance: al restringir el mock a claves existentes en work/window/pairs_raw, F1 de descuento 0,9915, parcial 0,9625, retención 0,9718 y demás clases con positivos ≥0,95; SHORT_DISPUTE solo tiene 27 positivos en ese input y RETURNED ninguno. En el mock completo, 2.641 SHORT_DISCOUNT de ar_1toN_remittance + 1.910 de ar_short_dispute (4.551 total) no aparecen en pairs_raw; además los retornos llegan por side_links, fuera de decide(). No se añade regla T5 para inventar pares: se requiere revisar recall/ruta de T3/T6/T2 antes de declarar Hecho global; full pipeline score aún 52,62.
15:49 CLAIM T13 abonos neteados + N:M (ar_1toN_credit_note, ar_n_to_m, ar_1toN_text) — sigo sobre comb mitm de javier; @javier avísame si estás tocando src/match/combo.py
