# Registro de envíos

**LIVE:** sin confirmación; se sube lo que mejora en la ventana local y se apunta aquí (commit, ventana, nota real).
**FINAL:** fila `PROPUESTO` con hipótesis y nota esperada, y confirmación del responsable de FINAL antes de subir.
Objetivo: FINAL validado y subido antes de las 18:30 (el empate gana el envío más temprano). Límites (web `/reglas`): 1 envío / 10 s por fase; pedir la URL de subida ya consume un hueco.
FINAL: un único envío válido.

| # | Fecha y hora | Fase | Commit | Fichero | Hipótesis (qué cambia y por qué) | Ventana local | LIVE esperado | LIVE real | Estado | Confirmado por |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | PENDIENTE | LIVE | 3faff91+T0 | `python -m src.submit --phase live --trivial` → `out/live_<ts>_<sha>.csv.gz` | Envío válido trivial (T0): pares exactos del starter + resto `UNIDENTIFIED`/`BANK_FEES`. Prueba el circuito. Arnés listo; falta en esta máquina `data/live/` y `HK_TOKEN` (+ endpoints de `/enviar` en `HK_API`). `sample`: 11,28 (= starter). | — (sin `train/` en local) | 10–19 | | pendiente de subir | — |
