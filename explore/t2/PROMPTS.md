# Prompts para trabajar T2 en paralelo

Base Q1 (árbol actual, `--tag base`): confirming 78,8/88,4 · dd_returned 97,85/97,85 · returned_transfer 100/94,8 ·
cheque_returned 100/85,0 · ap_fx R+exc 92,6 · ap_partial R+exc 78,5. El resto de segmentos T2 ya está ≥ 98/95.

Reglas comunes (van incluidas en cada prompt): cada chat es dueño de **sus ficheros** y no toca los de los demás. Todos
trabajan en el mismo checkout, así que **ningún chat hace commit, push ni escribe en el tablero**: eso lo hace el chat
integrador cuando cada uno avisa de que ha terminado.

---

## Chat A · ap_confirming

```
Contexto: repo Talky-Hackathon, tarea T2 (src/sidefiles/). Lee CLAUDE.md, contracts/schemas.md §3 y
src/sidefiles/TODO.md. Hay otros chats trabajando a la vez en el mismo checkout.

TU FICHERO (el único que puedes editar): src/sidefiles/runs.py. Explora en explore/t2/a_*.py.
No toques runs_return.py, deposits.py, __main__.py, eval.py ni nada fuera de src/sidefiles/runs.py.
No hagas commit, push ni escribas en comms/board: cuando termines, informa y el integrador hace el commit.

Bucle de desarrollo (~10 s):
  python -m src.sidefiles --phase window --subset q1 --steps pr --tag cf
  python -m src.sidefiles.eval --phase window --subset q1 --tag cf --segments ap_confirming,ap_payment_single,ap_payment_multi
No lances la ventana completa sin --subset ni sin --tag. Usa siempre tu tag "cf" (no machaques side_links_q1.parquet).

Tarea: ap_confirming está en P 78,8 % / R 88,4 % (Q1). Objetivo: P ≥ 98 %, R ≥ 95 %, sin bajar ap_payment_single ni
ap_payment_multi. Regla actual (bloque "confirming" de step_pr): cada e2e va al primer cargo del run (CF… en el texto)
con fecha ≥ vencimiento máximo de sus documentos. Cuando la suma del cargo cuadra, la precisión es del 100 %; cuando no
(confidence 0,6), del ~78 %. Ya probado y peor: ASOF por importes acumulados (P 70,8 %).
Propuesta: dejar fijos los cargos que cuadran y, en los que no cuadran, reasignar los e2e entre cargos vecinos del mismo
run (±1–3 días) hasta que cuadre la suma (subset-sum pequeño por run, en duckdb o en Python vectorizado por run,
nunca fila a fila sobre millones). Si no cuadra, bajar confidence o no emitir (umbral ≈ F1/2 ≈ 0,4).
Antes de programar, diagnostica en los pares que fallan por qué no cuadra (¿vencimiento mal calculado?, ¿e2e repartido
en varios cargos?, ¿due_date vs execution_date?, ¿festivos?).
Reglas generales, nada a fuego; cada enlace con rule/confidence/evidence (rule T2.run_confirming).

Al acabar: informa P/R de Q1 de ap_confirming, ap_payment_single y ap_payment_multi antes y después, y la lista de
ficheros cambiados. Borra work/window/side_links_q1_cf.parquet.
```

---

## Chat B · devoluciones (ap_returned_transfer + ar_cheque_returned)

```
Contexto: repo Talky-Hackathon, tarea T2 (src/sidefiles/). Lee CLAUDE.md, contracts/schemas.md §3, data/POLICIES.md
§3 (devoluciones) y src/sidefiles/TODO.md. Hay otros chats trabajando a la vez en el mismo checkout.

TUS FICHEROS (los únicos que puedes editar): src/sidefiles/runs_return.py y src/sidefiles/deposits.py. Explora en
explore/t2/b_*.py. No toques runs.py (es de otro chat que trabaja en confirming: si step pr falla por un cambio suyo a
medias, espera un minuto y reintenta), ni __main__.py, eval.py ni nada más.
No hagas commit, push ni escribas en comms/board: cuando termines, informa y el integrador hace el commit.

Bucle de desarrollo:
  python -m src.sidefiles --phase window --subset q1 --steps pr,chq,card --tag ret
  python -m src.sidefiles.eval --phase window --subset q1 --tag ret --segments ap_returned_transfer,ar_cheque_returned,ar_card_chargeback,ar_cheque_deposit,ar_card_settlement
Usa siempre tu tag "ret". No lances la ventana completa.

Tarea (Q1, ahora → objetivo P ≥ 98 %, R ≥ 95 %):
1. ap_returned_transfer: P 100 / R 94,8 (219 de 231). runs_return.py: línea IN que cita el e2e de nuestra orden
   ("RETURN <código> … PR<run> <nnnn>", "DEVOLUCION TRANSFERENCIA …") → mismos documentos, RETURNED. Mira las 12 que
   faltan: probablemente e2e truncado (N43) o fuera de la ventana de 60 días → fallback por run + importe + cuenta.
2. ar_cheque_returned: P 100 / R 85,0 (34 de 40). deposits.py, bloque links_chq_ret: mira qué textos de devolución o
   qué números de cheque no casan (otros idiomas o formatos, ceros, cheque depositado antes del rango de 90 días).
3. Si te sobra tiempo: rendimiento de step_card / step_cheque (joins con OR, son los pasos lentos). Mide los tiempos
   antes y después y no cambies ningún par (mismo P/R en ar_card_settlement y ar_cheque_deposit).
Reglas generales (patrones de texto, ventanas de días), nada a fuego; rule/confidence/evidence en cada enlace.

Al acabar: informa P/R de Q1 antes y después de cada segmento, los tiempos y la lista de ficheros cambiados.
Borra work/window/side_links_q1_ret.parquet.
```

---

## Chat C · ar_dd_returned

```
Contexto: repo Talky-Hackathon, tarea T2 (src/sidefiles/). Lee CLAUDE.md, contracts/schemas.md §3, data/POLICIES.md
y src/sidefiles/TODO.md. Hay otros chats trabajando a la vez en el mismo checkout.

TU ZONA (la única que puedes editar): la función step_ret de src/sidefiles/__main__.py, y si hace falta resolve_receipts
(avísalo, porque lo usan también los cheques). Puedes crear un fichero nuevo src/sidefiles/dd_return.py si quieres sacar
step_ret de __main__.py. Explora en explore/t2/c_*.py. No toques runs*.py, deposits.py, supplier_dd.py ni eval.py.
No hagas commit, push ni escribas en comms/board: cuando termines, informa y el integrador hace el commit.

Bucle de desarrollo:
  python -m src.sidefiles --phase window --subset q1 --steps dd,ret --tag ddr
  python -m src.sidefiles.eval --phase window --subset q1 --tag ddr --segments ar_dd_returned,ar_dd_batch
Usa siempre tu tag "ddr". No lances la ventana completa.

Tarea: ar_dd_returned está en P/R 97,85 % (Q1). Objetivo: ≥ 98 / ≥ 95 sin bajar ar_dd_batch. Los fallos están en las
devoluciones MD06 (reembolso, 21–75 días) con varias cuotas iguales del mismo mandato dentro del plazo. Ahora se
desempata por cercanía a 46 días (mediana aprendida en TRAIN < 2024-07, sin fuga: compruébalo). Ideas: usar la
distribución de retrasos aprendida (moda o densidad por banco/país/entidad) en vez de la mediana; preferir cuotas aún
no devueltas (una cuota no se devuelve dos veces: asignación 1:1 entre devoluciones y cuotas del mismo mandato);
referencia o batch en el texto. Todo lo aprendido, sólo con TRAIN < 2024-07.
Reglas generales, nada a fuego; rule T2.dd_return con confidence/evidence (baja la confidence en los empates).

Al acabar: informa P/R de Q1 antes y después, cómo has comprobado que no hay fuga y la lista de ficheros cambiados.
Borra work/window/side_links_q1_ddr.parquet.
```

---

## Chat D · excepciones FX_DIFFERENCE / PARTIAL (T5)

```
Contexto: repo Talky-Hackathon. Lee CLAUDE.md, docs/SCORING.md, data/POLICIES.md, contracts/schemas.md y TASKS.md T5.
PRIMERO mira comms/board/*.md y TASKS.md: si T5 (src/exceptions/) tiene otro dueño, no programes: escribe la propuesta
en explore/t2/d_propuesta.md y avisa. Hay otros chats trabajando a la vez en el mismo checkout.

TU ZONA: src/exceptions/ (nada de src/sidefiles/). No hagas commit, push ni escribas en comms/board: cuando termines,
informa y el integrador hace el commit.

Situación: T2 enlaza bien ap_fx (R 96,9 %) y ap_partial (R 96,6 %), pero con la excepción vacía (R+exc 92,6 % y 78,5 % en
Q1: python -m src.sidefiles.eval --phase window --subset q1 --tag base --segments ap_fx,ap_partial).
Tarea: que el motor de excepciones ponga, a partir de pairs_raw / side_links (contrato §3):
- FX_DIFFERENCE cuando la divisa de la línea ≠ la de la partida (umbral de diferencia según POLICIES),
- PARTIAL cuando el documento se paga por menos que su saldo abierto,
sin romper las excepciones que ya ponen otros (RETURNED de T2). Reglas generales con rule/confidence/evidence.
Mide con tools/score.py en la ventana (exactitud de excepciones y macro-F1) antes y después.

Al acabar: informa el delta por métrica y la lista de ficheros cambiados.
```

---

## Chat integrador (este chat)

1. **Antes de abrir A–D:** commitear el árbol actual como base (sdd, chargebacks, cheque_return, run_return, MD06 46 d,
   `--tag`, runs_return.py) con su medida de ventana completa.
2. Cuando cada chat avise: revisar el diff (`git diff <sus ficheros>`), medir Q1 con todos los cambios juntos y hacer el
   commit sólo de sus ficheros (`git add <ficheros>`), con su medida en el mensaje.
3. Al final: ventana completa, `explore/t2/finish.sh`, `SUBMISSIONS.md` y la línea DONE en el tablero.
