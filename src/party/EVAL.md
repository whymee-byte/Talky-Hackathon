# T4 · medida en la ventana TRAIN 2024-07 → 2025-06

Modelo aprendido sólo con TRAIN < 2024-07-01 (sin fuga). Líneas = las que liquidan partidas en el golden (359.134).
top-1 = el rank 1 es uno de los terceros del golden; top-3 = alguno de los 3 lo es. Generado por `python -m src.party --phase window`.

## total

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| all     |   359134 |        93.9 |  88.52 |  91.66 |         41218 |

## formato

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| n43     |    71584 |        80.1 |  71.2  |  72.95 |         20613 |
| camt053 |   182249 |        96.5 |  91.5  |  95.35 |         15492 |
| mt940   |   105301 |        98.7 |  95.14 |  98.01 |          5113 |

## tipo

| grupo                |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:---------------------|---------:|------------:|-------:|-------:|--------------:|
| pago_proveedor       |    98779 |        93.6 |  87.28 |  89.37 |         12569 |
| transferencia_cobro  |   214496 |        99.9 |  94.84 |  98.83 |         11069 |
| lote_cobro           |     9517 |        29.8 |  26.36 |  26.36 |          7008 |
| payment_factory      |    11022 |        47.7 |  39.18 |  42.29 |          6704 |
| confirming_factoring |     5788 |        45.6 |  39.17 |  40.32 |          3521 |
| adeudo_proveedor     |    10796 |       100   |  98.99 | 100    |           109 |
| matriz               |      360 |       100   |  70    |  88.89 |           108 |
| agencia              |      711 |        96.8 |  91    |  96.06 |            64 |
| devolucion           |     7157 |        99.9 |  99.23 |  99.66 |            55 |
| iban_nuevo           |      508 |       100   |  97.83 |  99.61 |            11 |

## direccion

| grupo   |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:--------|---------:|------------:|-------:|-------:|--------------:|
| OUT     |   127166 |        87.9 |  82.15 |  83.97 |         22705 |
| IN      |   231968 |        97.1 |  92.02 |  95.88 |         18513 |

## formato x tipo

| grupo                          |   lineas |   cobertura |   top1 |   top3 |   fallos_top1 |
|:-------------------------------|---------:|------------:|-------:|-------:|--------------:|
| n43 · pago_proveedor           |    13626 |        54.2 |  25.2  |  26.17 |         10192 |
| camt053 · transferencia_cobro  |   119495 |        99.9 |  93.74 |  98.51 |          7479 |
| n43 · payment_factory          |    11022 |        47.7 |  39.18 |  42.29 |          6704 |
| camt053 · lote_cobro           |     6537 |        15.8 |  15.76 |  15.76 |          5507 |
| mt940 · transferencia_cobro    |    57298 |       100   |  95.2  |  98.96 |          2753 |
| n43 · confirming_factoring     |     3613 |        45.9 |  40.3  |  40.6  |          2157 |
| camt053 · pago_proveedor       |    44762 |        99.9 |  96.56 |  99.16 |          1542 |
| mt940 · lote_cobro             |     1842 |        52.4 |  52.39 |  52.39 |           877 |
| n43 · transferencia_cobro      |    37703 |       100   |  97.78 |  99.6  |           837 |
| mt940 · pago_proveedor         |    40391 |       100   |  97.93 |  99.84 |           835 |
| camt053 · confirming_factoring |     1075 |        31.3 |  23.07 |  25.95 |           827 |
| n43 · lote_cobro               |     1138 |        73.9 |  45.17 |  45.17 |           624 |
| mt940 · confirming_factoring   |     1100 |        58.5 |  51.18 |  53.45 |           537 |
| camt053 · matriz               |      155 |       100   |  60.65 |  96.13 |            61 |
| n43 · adeudo_proveedor         |     2580 |       100   |  97.83 | 100    |            56 |
| camt053 · agencia              |      447 |        99.6 |  91.95 |  98.66 |            36 |
| mt940 · adeudo_proveedor       |     3093 |       100   |  98.87 | 100    |            35 |
| mt940 · agencia                |      167 |        87.4 |  83.83 |  86.83 |            27 |
| mt940 · matriz                 |      119 |       100   |  79.83 |  87.39 |            24 |
| n43 · matriz                   |       86 |       100   |  73.26 |  77.91 |            23 |
| camt053 · devolucion           |     4563 |       100   |  99.52 |  99.78 |            22 |
| camt053 · adeudo_proveedor     |     5123 |       100   |  99.65 | 100    |            18 |
| n43 · devolucion               |     1428 |        99.9 |  98.81 |  99.51 |            17 |
| mt940 · devolucion             |     1166 |        99.6 |  98.63 |  99.4  |            16 |
| mt940 · iban_nuevo             |      125 |       100   |  92.8  |  98.4  |             9 |
| n43 · iban_nuevo               |      291 |       100   |  99.31 | 100    |             2 |
| n43 · agencia                  |       97 |       100   |  98.97 | 100    |             1 |
| camt053 · iban_nuevo           |       92 |       100   | 100    | 100    |             0 |

## calibracion

|   conf_bucket |   lineas |   precision_top1 |
|--------------:|---------:|-----------------:|
|           0   |     4615 |              1.1 |
|           0.2 |     1487 |             24.5 |
|           0.3 |     2790 |             32.9 |
|           0.4 |     1620 |             40.9 |
|           0.5 |     1430 |             57   |
|           0.7 |    16862 |             74.6 |
|           0.8 |    10823 |             84.4 |
|           0.9 |   296792 |             98.6 |
|           1   |      651 |             99.5 |
