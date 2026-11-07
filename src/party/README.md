# T4 · Identificación de tercero (`src/party/`)

```
python -m src.party --phase window    # aprende con TRAIN < 2024-07 (sin fuga), resuelve la ventana y la mide
python -m src.party --phase live      # aprende con todo TRAIN, resuelve LIVE
python -m src.party --phase final
```

Salida: `work/<fase>/line_party.parquet` (contrato §4): hasta 3 candidatos por línea con
`rank, party_id, payer_party_id, method, confidence, rule, evidence`.
Mientras T1 no publique `lines.parquet`, `lines.py` escribe `work/<fase>/party_lines.parquet` con los mismos nombres
de columna del contrato §1.

## Familias de evidencia (todas vectorizadas en duckdb; se combinan con 1 − ∏(1 − c))

| Familia | Qué hace | Confianza |
|---|---|---|
| `iban` | IBAN de contraparte o del texto → tercero: historial (pureza ≥ 0,8), maestros y **avisos PDF de cambio de cuenta** (IBAN viejo y nuevo valen) | 0,97 maestro/aviso; pureza·n/(n+1) historial |
| `mandate` | `VTM-XX-nnnnnnn` → cliente (maestro e historial) | 0,98 |
| `learned` | prefijos de 10/16/24 caracteres que empiezan en cada uno de los 20 primeros tokens del texto, vistos en el historial siempre con el mismo tercero (por sociedad de la cuenta) | pureza·n/(n+1) |
| `master` | prefijos del nombre actual, comercial e **impreso en facturas** (renombrados) de cada tercero; cubre "ORD. X" (confirming) y "EXPTE … X" (agencias) porque se buscan en cualquier posición | 0,70–0,90 / nº de homónimos |
| `doc_ref` | tokens del texto (también 2–3 tokens pegados: "FV25 0109359") = nº impreso, pedido o nº de proveedor de una partida | 0,85 alfanumérico / 0,70 dígitos, ÷ nº de terceros; si > 5 terceros sólo confirma a un candidato ya propuesto |
| `amount` | el candidato tiene una partida emitida ≤ 400 días antes por exactamente el importe (desempata homónimos) | 0,6, nunca sola |
| `fuzzy` | rapidfuzz `partial_ratio` por **nombre único** (no por fila) sólo para líneas sin candidato | ≤ 0,5 |

Sociedad: un tercero sólo es candidato si su sociedad es la de la cuenta o un par aprendido (payment factory ES00 →
ES01/ES02/PT01).

## Lo que hay que saber

- **Los nombres no son únicos**: en US01 el 64 % de los clientes comparte nombre con otro (hasta 28 por prefijo). El
  nombre sólo da el grupo de homónimos; desempatan `doc_ref`, `amount`, IBAN e historial. T3 debe mirar los 3
  candidatos, no sólo el primero.
- Las líneas de lote (remesas, órdenes de pago agregadas "PAGO TRANSFERENCIAS …", confirming) no traen nombre: su
  tercero sale de los ficheros de detalle (T2).
