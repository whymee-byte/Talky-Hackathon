# T4 · medida en la ventana TRAIN 2024-07 → 2025-06

Modelo aprendido sólo con TRAIN < 2024-07-01 (sin fuga). Líneas = las que liquidan partidas en el golden (359.134).
top-1 = el rank 1 es uno de los terceros del golden; top-3 = alguno de los 3 lo es. Generado por `python -m src.party --phase window`.
v2: familia `payrun` (run_id del texto + cuenta deudora + importe → beneficiario de payment_runs → tercero por nº de documento+importe, IBAN o nombre) y filtro `created ≤ booking_date`.

## total

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| all     |   359134 |        97.8 |  94.28 |  97.02 |         20547 |

## formato

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| camt053 |   182249 |        96.9 |  92.26 |  95.93 |         14110 |
| mt940   |   105301 |        99.1 |  96.35 |  98.55 |          3843 |
| n43     |    71584 |        98.3 |  96.38 |  97.57 |          2594 |

## tipo

| grupo                |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:---------------------|---------:|------------:|-------:|-------:|--------------:|
| transferencia_cobro  |   214496 |        99.9 |  94.57 |  98.84 |         11648 |
| lote_cobro           |     9517 |        29.8 |  26.36 |  26.36 |          7008 |
| confirming_factoring |     5788 |        84.2 |  83.45 |  83.66 |           958 |
| pago_proveedor       |    98779 |        99.9 |  99.42 |  99.83 |           577 |
| adeudo_proveedor     |    10796 |       100   |  99    | 100    |           108 |
| matriz               |      360 |       100   |  74.44 |  88.89 |            92 |
| agencia              |      711 |        96.8 |  87.48 |  95.92 |            89 |
| devolucion           |     7157 |        99.9 |  99.25 |  99.69 |            54 |
| iban_nuevo           |      508 |       100   |  97.83 |  99.61 |            11 |
| payment_factory      |    11022 |       100   |  99.98 | 100    |             2 |

## direccion

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| IN      |   231968 |        97.1 |  91.77 |  95.9  |         19102 |
| OUT     |   127166 |        99.1 |  98.86 |  99.07 |          1445 |

## formato x tipo

| grupo                          |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:-------------------------------|---------:|------------:|-------:|-------:|--------------:|
| camt053 · transferencia_cobro  |   119495 |        99.9 |  93.24 |  98.53 |          8083 |
| camt053 · lote_cobro           |     6537 |        15.8 |  15.76 |  15.76 |          5507 |
| mt940 · transferencia_cobro    |    57298 |       100   |  95.24 |  98.99 |          2728 |
| n43 · confirming_factoring     |     3613 |        75.3 |  74.29 |  74.56 |           929 |
| mt940 · lote_cobro             |     1842 |        52.4 |  52.39 |  52.39 |           877 |
| n43 · transferencia_cobro      |    37703 |       100   |  97.78 |  99.61 |           837 |
| n43 · lote_cobro               |     1138 |        73.9 |  45.17 |  45.17 |           624 |
| camt053 · pago_proveedor       |    44762 |        99.9 |  99.2  |  99.71 |           359 |
| mt940 · pago_proveedor         |    40391 |       100   |  99.72 |  99.98 |           113 |
| n43 · pago_proveedor           |    13626 |        99.9 |  99.23 |  99.82 |           105 |
| camt053 · agencia              |      447 |        99.6 |  85.68 |  98.43 |            64 |
| n43 · adeudo_proveedor         |     2580 |       100   |  97.87 | 100    |            55 |
| camt053 · matriz               |      155 |       100   |  70.97 |  96.13 |            45 |
| mt940 · adeudo_proveedor       |     3093 |       100   |  98.87 | 100    |            35 |
| mt940 · agencia                |      167 |        87.4 |  85.63 |  86.83 |            24 |
| mt940 · matriz                 |      119 |       100   |  79.83 |  87.39 |            24 |
| n43 · matriz                   |       86 |       100   |  73.26 |  77.91 |            23 |
| camt053 · devolucion           |     4563 |       100   |  99.52 |  99.82 |            22 |
| camt053 · adeudo_proveedor     |     5123 |       100   |  99.65 | 100    |            18 |
| mt940 · confirming_factoring   |     1100 |        98.6 |  98.45 |  98.64 |            17 |
| mt940 · devolucion             |     1166 |        99.6 |  98.63 |  99.4  |            16 |
| n43 · devolucion               |     1428 |        99.9 |  98.88 |  99.51 |            16 |
| camt053 · confirming_factoring |     1075 |        99.1 |  98.88 |  98.88 |            12 |
| mt940 · iban_nuevo             |      125 |       100   |  92.8  |  98.4  |             9 |
| n43 · iban_nuevo               |      291 |       100   |  99.31 | 100    |             2 |
| n43 · payment_factory          |    11022 |       100   |  99.98 | 100    |             2 |
| n43 · agencia                  |       97 |       100   |  98.97 | 100    |             1 |
| camt053 · iban_nuevo           |       92 |       100   | 100    | 100    |             0 |

## calibracion

|   conf_bucket |   lineas |   precision_top1 |
|--------------:|---------:|-----------------:|
|           0.1 |      202 |             12.4 |
|           0.2 |     1604 |             24.9 |
|           0.3 |     3429 |             31.1 |
|           0.4 |     2975 |             45.5 |
|           0.5 |      712 |             48.6 |
|           0.6 |     1617 |             68.3 |
|           0.7 |     6146 |             72.1 |
|           0.8 |    11489 |             84.2 |
|           0.9 |   323106 |             99.1 |
|           1   |        8 |            100   |
