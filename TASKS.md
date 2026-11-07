# Plan de trabajo de conciliación bancaria

## Propósito y criterios transversales

Este documento descompone el reto en ocho tareas independientes y verificables. Cada tarea deja explícitamente vacío el campo de dueño para asignarlo después. No prescribe una implementación concreta: define resultados, límites y comprobaciones.

Las entregas deben respetar `data/FORMAT.md`: una fila por asignación, cinco columnas, IDs válidos, importes en moneda del ítem, excepciones y categorías de listas cerradas, sin mezclar filas de ítems con una fila de categoría para el mismo movimiento. La referencia funcional es `data/POLICIES.md`, y `data/sample/` sirve como conjunto reproducible de validación.

## Tareas

### T1. Ingestión y normalización de extractos bancarios

- **Dueño:**
- **Prioridad sugerida:** P0, bloqueante para el resto.
- **Objetivo:** Convertir los extractos de todos los formatos previstos en movimientos bancarios normalizados, conservando trazabilidad hasta el fichero y registro de origen.
- **Alcance:** Norma 43, camt.053/XML y STA/formatos presentes en `sample/bank_statements`; fechas de valor y contabilización, importe, divisa, cuenta, texto, referencia, contraparte y `bank_txn_id`; deduplicación e identificación de errores de parseo. Incluir compatibilidad con el cambio de formato del 1 de octubre de 2025.
- **Fuera de alcance:** Matching con facturas, clasificación contable y OCR de documentos.
- **Dependencias inevitables:** Ninguna funcional. Debe consumir el diccionario de datos y las reglas de identidad de la entrega.
- **Hecho cuando:** Todos los movimientos de muestra se cargan sin pérdida ni duplicación, cada uno conserva sus campos de origen y los IDs generados son estables y pertenecen a su fase.
- **Cómo verificarlo:** Comparar conteos y sumas por cuenta, fecha y divisa contra los ficheros fuente; ejecutar una prueba con cada formato de `sample/bank_statements`; revisar que una segunda ejecución sea idempotente.
- **Riesgos y decisiones clave:** Resolver explícitamente signo, separadores, fechas y colisiones de IDs. No descartar silenciosamente líneas malformadas: deben quedar diagnosticadas.

### T2. Construcción de entidades, ítems y saldos abiertos

- **Dueño:**
- **Prioridad sugerida:** P0.
- **Objetivo:** Crear una vista coherente de sociedades, clientes, proveedores, cuentas y partidas conciliables junto con su saldo abierto por fecha.
- **Alcance:** Maestros de `entities`, `customers`, `suppliers`, `bank_accounts`, `ar_invoices`, `ap_invoices`, `credit_notes`, `payment_terms` y `fx_rates`; distinguir ARI/ARC/API/APC de proformas y documentos informativos; relaciones de grupo, cambios de nombre/cuenta, documentos relacionados y moneda del ítem.
- **Fuera de alcance:** Decidir qué movimiento liquida cada ítem.
- **Dependencias inevitables:** T1 solo si se desea calcular saldos aplicados sobre movimientos normalizados; los maestros son la fuente de identidad.
- **Hecho cuando:** Cada ítem tiene identidad única, entidad responsable, importe y moneda consistentes, estado abierto reproducible a una fecha y relaciones de reemplazo/crédito preservadas.
- **Cómo verificarlo:** Validar claves y dominios del diccionario; comprobar que no se incluyen proformas como ítems; reconciliar saldos iniciales y documentos de la muestra con conteos por tipo, entidad y moneda.
- **Riesgos y decisiones clave:** No confundir número visible con `item_id`; mantener importes positivos en las filas de entrega; definir la fecha de corte que determina “abierto”.

### T3. Parsing y vinculación de PDFs y remesas

- **Dueño:**
- **Prioridad sugerida:** P1.
- **Objetivo:** Extraer hechos utilizables de PDFs, avisos de pago y ficheros de remesa para enriquecer documentos y movimientos.
- **Alcance:** Índice de PDFs, texto/tablas de facturas y abonos, avisos CSV/XLSX/texto, números alternativos, documentos pagados, descuentos, retenciones, disputas, cuotas, anticipos, meses de servicio, reemplazos y relaciones de remesa con transferencias o lotes.
- **Fuera de alcance:** Elegir definitivamente el candidato ganador o asignar la puntuación final.
- **Dependencias inevitables:** T2 para enlazar identificadores extraídos con ítems; T1 para asociar avisos con movimientos cuando solo exista referencia bancaria.
- **Hecho cuando:** Para cada documento procesable de muestra se produce un registro estructurado con origen, confianza y evidencia; los casos sin texto o ambiguos quedan marcados, no inventados.
- **Cómo verificarlo:** Medir cobertura de extracción en `sample/pdfs` y `sample/remittance_advices`; revisar manualmente una muestra que incluya cuotas, retenciones, descuentos, “anula y sustituye” y deducciones; comprobar trazabilidad de cada campo a página, fila o celda.
- **Riesgos y decisiones clave:** Variación de layouts y llegada posterior al cobro. Separar “hecho extraído” de “interpretación contable” y conservar la versión del parser.

### T4. Generación y ranking de candidatos de matching

- **Dueño:**
- **Prioridad sugerida:** P0.
- **Objetivo:** Proponer y ordenar candidatos de ítems para cada movimiento, incluyendo pagos agrupados y pagadores indirectos.
- **Alcance:** Normalización de referencias y nombres; tolerancias de importe y moneda; relaciones padre/filial, confirming, factor y payment factory; lotes SEPA/BACS/ACH, tarjetas, cheques y payment runs; FIFO, mes de servicio, referencias truncadas o con errores y múltiples ítems por movimiento.
- **Fuera de alcance:** Aplicar excepciones definitivas o generar el CSV final.
- **Dependencias inevitables:** T2 para el universo de ítems; T3 cuando la evidencia decisiva está en remesas o PDFs; T1 para atributos del movimiento.
- **Hecho cuando:** Cada movimiento tiene cero o más candidatos explicables, ordenados de forma determinista, con evidencias y sin proponer automáticamente documentos excluidos por política.
- **Cómo verificarlo:** Evaluar casos anotados de la muestra: coincidencia exacta, referencia ruidosa, mismo importe, pago agrupado, entidad pagadora distinta y documento sustituido; comprobar estabilidad del ranking al repetir la ejecución.
- **Riesgos y decisiones clave:** Un importe coincidente no basta para forzar un match. Definir umbrales, desempates y cuándo devolver “sin candidato”.

### T5. Aplicación de reglas de excepciones y categorías

- **Dueño:**
- **Prioridad sugerida:** P1.
- **Objetivo:** Traducir evidencia de movimiento e ítems a las excepciones y categorías cerradas que exige la política.
- **Alcance:** `PARTIAL`, descuentos dentro/fuera de plazo, cargos bancarios, retenciones, disputas, sobrepagos, duplicados, devoluciones, diferencias FX y `UNIDENTIFIED`; categorías sin ítem como comisiones, nómina, impuestos, cash pool, transferencias, anticipos, reembolsos y reversos bancarios; importes según la sección 2 de `POLICIES.md`.
- **Fuera de alcance:** Descubrir nuevos códigos o modificar la taxonomía de entrega.
- **Dependencias inevitables:** T2 y T4; T3 para razones que solo figuran en documentos.
- **Hecho cuando:** Cada decisión usa una regla trazable, respeta la precedencia de políticas y nunca asigna categoría en una fila con ítem ni excepción fuera de la lista cerrada.
- **Cómo verificarlo:** Crear una matriz de casos de política y compararla con `sample/golden_allocations.csv.gz`; probar especialmente FX, descuentos con gracia de tres días, devoluciones, duplicados y recibos no identificados.
- **Riesgos y decisiones clave:** Las excepciones dependen del orden temporal y de la evidencia. Documentar conflictos y preferir no identificar antes que forzar una asignación.

### T6. Reconciliación temporal de pagos, retornos y reaperturas

- **Dueño:**
- **Prioridad sugerida:** P1.
- **Objetivo:** Resolver la historia de liquidación de cada ítem a través del tiempo, incluidos pagos parciales, cuotas, re-presentaciones y devoluciones.
- **Alcance:** Orden por fechas de contabilización/valor, saldo progresivo, pagos posteriores, planes de cuotas, retenciones liberadas, pagos duplicados, retornos SEPA/BACS/ACH, cheques devueltos, chargebacks, transferencias rechazadas y re-presentaciones marcadas en lotes.
- **Fuera de alcance:** Parsing primario de extractos o PDFs.
- **Dependencias inevitables:** T1 y T2; T4 para identificar operaciones relacionadas; T5 para codificar el resultado.
- **Hecho cuando:** La secuencia de eventos produce saldos no negativos salvo estados explícitamente explicados, y cada reapertura o cierre se refleja con el importe y excepción requeridos por la política.
- **Cómo verificarlo:** Ejecutar escenarios sintéticos y casos de la muestra con pago parcial, devolución y re-presentación; comparar el estado final de ítems y el conjunto de filas contra el golden, incluyendo el “later line” de duplicados.
- **Riesgos y decisiones clave:** Desfase entre fecha de cobro y fecha de aviso. Definir orden temporal, ventanas de asociación y tratamiento de eventos tardíos sin reescribir evidencia histórica.

### T7. Evaluación, scoring y diagnóstico de calidad

- **Dueño:**
- **Prioridad sugerida:** P1.
- **Objetivo:** Medir la calidad de las decisiones antes de entregar y localizar errores por tipo, entidad, cuenta, divisa y segmento.
- **Alcance:** Reproducción de F1 de asignaciones, exactitud por movimiento, macro-F1 de excepciones, tolerancia `max(0.01, 0.5 %)`, métricas por segmentos, cobertura, errores de validación y análisis de falsos positivos/negativos.
- **Fuera de alcance:** Optimizar un modelo concreto o alterar etiquetas de referencia.
- **Dependencias inevitables:** T4-T6 para evaluar decisiones completas; `starter/bankrec_score.py` es la referencia disponible.
- **Hecho cuando:** La evaluación reproduce el score del referente en el conjunto de muestra/entrenamiento dentro de una tolerancia documentada y genera un informe accionable por causa.
- **Cómo verificarlo:** Ejecutar el scorer sobre una entrega válida de TRAIN, comparar métricas globales y por segmento, y probar mutaciones inválidas del CSV para confirmar que los fallos se detectan.
- **Riesgos y decisiones clave:** No optimizar solo F1 ignorando exactitud y excepciones. Separar validación estructural de evaluación contra golden y fijar semillas/configuración cuando aplique.

### T8. Pipeline de entrega, validación y operación por fase

- **Dueño:**
- **Prioridad sugerida:** P0, bloqueante para LIVE/FINAL.
- **Objetivo:** Encadenar ingestión, enriquecimiento, matching, reglas, scoring y exportación en una entrega reproducible y segura.
- **Alcance:** Ejecución por fase TRAIN/LIVE/FINAL, configuración de fechas y fuentes, control de versiones, logging, artefactos intermedios, exportación UTF-8 CSV o gzip, límites de 60 MB/3.000.000 filas, cobertura de todas las líneas y validaciones de `FORMAT.md`.
- **Fuera de alcance:** Automatización de la plataforma externa de subida.
- **Dependencias inevitables:** T1-T7 como interfaces de entrada y salida; el pipeline debe poder ejecutar validaciones aunque falten enriquecimientos opcionales.
- **Hecho cuando:** Una ejecución limpia produce una única entrega válida, determinista y auditable para cada fase, sin líneas omitidas, duplicados incompatibles ni mezcla de fila de categoría e ítems.
- **Cómo verificarlo:** Ejecutar de extremo a extremo sobre `sample`, pasar el validador y `starter/score_train.py`, repetir en entorno limpio y probar límites, gzip, delimitadores aceptados, IDs inválidos y errores de esquema.
- **Riesgos y decisiones clave:** FINAL permite un único envío puntuado. Incorporar un bloqueo previo que impida entregar si falla cualquier validación y conservar manifiesto de versión, configuración y conteos.

## Dependencias mínimas entre tareas

- **T1 → T4/T6/T8:** los movimientos normalizados son la base de asociación y entrega.
- **T2 → T4/T5/T6:** el universo de ítems y saldos determina qué puede liquidarse.
- **T3 → T4/T5:** PDFs y remesas aportan evidencia que no aparece en el banco.
- **T4 → T5/T6:** los candidatos preceden a la decisión de excepción y a la secuencia temporal.
- **T5/T6 → T7/T8:** scoring y entrega requieren decisiones completas y codificadas.

Estas dependencias son de datos y contratos, no implican que las tareas deban ejecutarse como un único componente ni que una persona sea dueña de varias.
