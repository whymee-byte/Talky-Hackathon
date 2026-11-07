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
- **Umbral de decisión ajustado en la ventana** (óptimo teórico de F1 ≈ F1/2). Ni forzar ni abstenerse por defecto.
  Con F1 = 2·TP/(P+G), añadir un par con probabilidad p de ser correcto sube el F1 esperado si y sólo si p > F1/2
  (≈ 0,4 con F1 ≈ 0,8). Ajustes finos con el scorer oficial: en cobros, abstenerse da `UNIDENTIFIED` (clase de la
  macro-F1) y la exactitud por movimiento también cuenta. Partir de ~0,4, no de "un umbral alto".
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

- **LIVE:** sin confirmación. Filtro: sólo se sube lo que **mejora en la ventana local**. Apunta cada envío en
  `SUBMISSIONS.md` (commit, nota de ventana, nota LIVE).
- **FINAL:** confirmación manual del responsable de FINAL antes de subir (hipótesis y nota esperada en `SUBMISSIONS.md`).
- Valida siempre con `tools/validate.py` antes de subir. El token va en `HK_TOKEN` (variable de entorno).
- FINAL: **un único envío** y **el empate gana el más temprano**: objetivo, envío FINAL validado **antes de las 18:30**.
  Congelación de `main` a las 17:30; simulacro con LIVE a las 17:00; datos FINAL a las 18:00; cierre 19:30.
  `src/final/run_final.sh` debe funcionar **sin red**: patrón nuevo sin LLM disponible (o sin caché) → categoría por
  defecto, nunca un fallo. Ver `TASKS.md` T9.
