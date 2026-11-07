# ouyang

11:40 CLAIM T0 envío válido trivial + arnés (validate / score / submit)
12:00 DONE T0 (arnés) `python -m src.submit --phase <fase> [--trivial] [--score]` → out/<fase>_<ts>_<sha>.csv.gz; `tools/validate.py` reproduce las 6 reglas de FORMAT.md con nº de fila (fichero roto a propósito: 5 errores en filas 3/5/7/9/11); `tools/score.py` = scorer oficial + F1 por clase de excepción + P/R de pares por segmento; starter en sample: 11,28 con score.py y con starter/score_train.py; `tools/submit.py` se niega sin validar, sin HK_TOKEN, y en FINAL sin --confirm.
12:00 BLOCKED T0 (envío LIVE) en esta máquina no hay data/live/ ni HK_TOKEN; falta el script oficial de /enviar para fijar los endpoints (HK_API, HK_UPLOAD_PATH, HK_SCORE_PATH, HK_STATUS_PATH). Quien tenga token y datos: `python -m src.submit --phase live --trivial` + `python tools/submit.py out/live_… --phase live` y apuntarlo en SUBMISSIONS.md.
11:20 DONE T0 primer envío LIVE: **11,47** (sub s_a13bdd1907376b352f85, apuntado en SUBMISSIONS.md). `tools/submit.py` ya usa el protocolo oficial de /enviar (API https://hackathon.usetalky.com/api, Bearer HK_TOKEN, S3 presigned POST). Uso: `HK_TOKEN=… python tools/submit.py out/live_….csv.gz --phase live`.
12:00 NOTE src/common/ (stdlib): data_root (HK_DATA > data/bankrec-docs-and-masters > data), fases, listas cerradas, céntimos, git_sha. `--phase window` = train filtrado 2024-07→2025-06 (caché en data_cache/window/).
11:25 CLAIM T2 ficheros de detalle (src/sidefiles/)
11:25 NOTE T1 aún no escribe work/<fase>/lines.parquet ni items.parquet (src/base saca JSONL de extractos brutos con ids propios). Para no bloquear T2 añado src/base_parquet/ (constructor interino de §1/§2 desde los CSV oficiales, duckdb, <15 s por fase). @galileo: cuando T1 escriba los Parquet del contrato, se borra.
11:36 DONE T2 paso 1 remesas: ar_dd_batch ventana P 99,79 % / R 99,79 % (185.248 pares); ventana 11,38 → 21,91; LIVE 11,47 → 21,75
11:45 NOTE T2 auto-construye lines/items Parquet contractuales si faltan mediante src/base_parquet; excluye sidecars AppleDouble ._*.csv que rompían lectura DuckDB en window
12:00 DONE T2 paso 2 devoluciones: ar_dd_returned ventana P 97,95 % / R 97,95 % (6.824 pares; MD06 llega a 21–75 d, resto 2–14 d); ventana 21,91 → 24,56; LIVE 21,75 → 24,27
