# Reglas del equipo (humanos y agentes)

Hackathon de conciliación bancaria a escala (Grupo Valtermia, ficticio): ~1,9 M movimientos, LIVE con clasificación en
directo y un **único** envío FINAL. Este fichero y `AGENTS.md` son idénticos: si cambias uno, copia el otro.

## Antes de escribir código

1. Lee `docs/DOMAIN.md`, `docs/SCORING.md`, `data/POLICIES.md` y **`contracts/schemas.md`**.
2. Coge una tarea de `TASKS.md`: pon tu nombre en **Dueño** y escribe `CLAIM T<n>` en `comms/board/<tu-nombre>.md`.
3. Trabaja sólo en **tu carpeta** (`src/<módulo>/`). Si necesitas cambiar un contrato, propónlo en
   `comms/board/<tu-nombre>.md` y avisa: los contratos son compartidos.

## Cómo resolvemos el problema

- **Reglas generales, nunca casos a fuego.** Prohibido escribir en el código ids, nombres, importes, textos o fechas
  concretas de las líneas que se puntúan (LIVE/FINAL) ni "parches" para filas sueltas de TRAIN: hay un benchmark oculto.
  Sí valen patrones, umbrales y tablas **aprendidas** de TRAIN o leídas de los maestros y de `POLICIES.md`.
- **Precisión primero.** Si no estás seguro, abstente: cobro sin partida segura → `UNIDENTIFIED`; nada de forzar un
  parecido. Un par falso cuesta más que uno que falta.
- **Volumen en código determinista.** Todo lo masivo (casar, saldos, lotes, excepciones) con duckdb/polars en columnas;
  nunca bucles Python fila a fila sobre millones de filas; nunca imprimir salidas grandes.
- **LLM sólo sobre patrones únicos.** Una llamada por patrón (texto normalizado, maquetación de aviso, plantilla de PDF),
  con caché en disco en `llm_cache/<módulo>/` y versión de prompt en la clave. **Nunca** una llamada por fila. La clave
  se lee de la variable de entorno `HACKATHON_API_KEY` (**no** uses `ANTHROPIC_API_KEY`); el modelo de
  `HACKATHON_MODEL`. Ninguna clave ni token en el código ni en git.
- **Rastro de auditoría.** Toda decisión automática guarda `rule` (id estable de la regla), `confidence` ∈ [0, 1] y
  `evidence` (claves y valores que la justifican). Sin esas columnas el módulo no está terminado.
- **Mide antes de afirmar.** Cada PR/commit relevante dice cuánto cambia la ventana TRAIN 2024-07 → 2025-06
  (`tools/score.py`), con desglose por métrica y segmento.

## Cómo colaboramos

- Commits y push **pequeños y frecuentes** a `main` (`git pull --rebase` antes de `push`). Mensajes en presente:
  `T2: enlaza remesas SEPA por batch_id (+11,3 ventana)`.
- Tablero: `comms/board/<tu-nombre>.md`, una línea por evento:
  `HH:MM CLAIM T<n> …` · `HH:MM DONE T<n> <medida de Verificar>` · `HH:MM BLOCKED …` · `HH:MM NOTE …`.
- Datos crudos en `data/` (no versionado), caché Parquet en `data_cache/`, intermedios en `work/<fase>/`, entregas en
  `out/` (ninguno se versiona). `llm_cache/` sí se versiona.
- Decisiones abiertas en `DECISIONS.md`: si cierras una, escribe **Decidido:** con el motivo.

## Envíos a la clasificación

- **Antes de cualquier envío** (LIVE o FINAL) escribe en `SUBMISSIONS.md` la hipótesis, el commit, la nota de ventana y
  la nota LIVE esperada, y **pide confirmación al responsable del equipo**. Sin su OK no se envía.
- Valida siempre con `tools/validate.py` antes de subir. El token va en `HK_TOKEN` (variable de entorno).
- FINAL: **un único envío**. Congelación de `main` a las 17:30; simulacro con LIVE a las 17:00; datos FINAL a las 18:00;
  cierre 19:30. Ver `TASKS.md` T9.
