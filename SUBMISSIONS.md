# Registro de envíos

**LIVE:** sin confirmación; se sube lo que mejora en la ventana local y se apunta aquí (commit, ventana, nota real).
**FINAL:** fila `PROPUESTO` con hipótesis y nota esperada, y confirmación del responsable de FINAL antes de subir.
Objetivo: FINAL validado y subido antes de las 18:30 (el empate gana el envío más temprano). Límites (web `/reglas`): 1 envío / 10 s por fase; pedir la URL de subida ya consume un hueco.
FINAL: un único envío válido.

| # | Fecha y hora | Fase | Commit | Fichero | Hipótesis (qué cambia y por qué) | Ventana local | LIVE esperado | LIVE real | Estado | Confirmado por |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2026-11-07 11:19 | LIVE | fb9197d | `live_20261107-1111_fb9197d.csv.gz` (sub `s_a13bdd1907376b352f85`) | Envío válido trivial (T0): pares exactos del starter + resto `UNIDENTIFIED`/`BANK_FEES`. Prueba el circuito. `sample`: 11,28 (= starter). | — (sin `train/` en local) | 10–19 | **11,47** (F1 8,42 · P 99,31 · R 4,39 · macro-F1 0,88) | scored | — |
| 2 | 2026-11-07 11:35 | LIVE | T2 paso 1 | `live_20261107-1134_e16d586.csv.gz` (sub `s_726a6aca835213dfa7aa`) | T2 paso 1: remesas SEPA/BECS/BACS/ACH por `batch_id` → todos sus recibos (side_links por delante del baseline T0). `ar_dd_batch` ventana P 99,79 % / R 99,79 %. | 11,38 → **21,91** (+10,53) | ~21 | **21,75** (F1 28,63 · P 99,61 · R 16,72 · acc 24,18) | scored | — |
| 3 | 2026-11-07 11:58 | LIVE | T2 paso 2 | `live_20261107-1158_3f83980.csv.gz` (sub `s_2fa7ffc0eb733e3b51aa`) | T2 paso 2: devoluciones de recibos (mandato + remesa `DEV…`/`REM…` + plazo por código R: MD06 14–80 d, resto 2–16 d) → partida del recibo original con `RETURNED`. `ar_dd_returned` ventana P 97,95 % / R 97,95 %. | 21,91 → **24,56** (+2,65) | ~24 | **24,27** | scored | — |
| 3 | 2026-11-07 11:59 | LIVE | 20af927 | `live_20261107-1158_20af927.csv.gz` | T3 v1: nº de documento del texto + tercero T4 + importe exacto/FIFO por orden, encima de T2 remesas. | 21,91 → **46,14** | ~45 | | pendiente de subir (falta HK_TOKEN aquí) | — |
