# Registro de envíos

**LIVE:** sin confirmación; se sube lo que mejora en la ventana local y se apunta aquí (commit, ventana, nota real).
**FINAL:** fila `PROPUESTO` con hipótesis y nota esperada, y confirmación del responsable de FINAL antes de subir.
Objetivo: FINAL validado y subido antes de las 18:30 (el empate gana el envío más temprano). Límites (web `/reglas`): 1 envío / 10 s por fase; pedir la URL de subida ya consume un hueco.
FINAL: un único envío válido.

| # | Fecha y hora | Fase | Commit | Fichero | Hipótesis (qué cambia y por qué) | Ventana local | LIVE esperado | LIVE real | Estado | Confirmado por |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2026-11-07 11:19 | LIVE | fb9197d | `live_20261107-1111_fb9197d.csv.gz` (sub `s_a13bdd1907376b352f85`) | Envío válido trivial (T0): pares exactos del starter + resto `UNIDENTIFIED`/`BANK_FEES`. Prueba el circuito. `sample`: 11,28 (= starter). | — (sin `train/` en local) | 10–19 | **11,47** (F1 8,42 · P 99,31 · R 4,39 · macro-F1 0,88) | scored | — |
