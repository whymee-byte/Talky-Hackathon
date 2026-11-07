# Fallos de la ventana — score 93.32

| métrica | valor | puntos perdidos |
|---|---|---|
| allocation F1 (×50) | 0.9471 (P 0.9872 · R 0.9101) | 2.64 |
| txn accuracy (×30) | 0.9380 | 1.86 |
| exception macro-F1 (×20) | 0.8912 | 2.18 |

## Clases de excepción (cada una ≈ 2 puntos)

| clase | gold | tp | fp | P | R | F1 | pts perdidos |
|---|---|---|---|---|---|---|---|
| OVERPAYMENT | 786 | 581 | 210 | 0.735 | 0.739 | 0.737 | 0.53 |
| PARTIAL | 24978 | 20587 | 3428 | 0.857 | 0.824 | 0.840 | 0.32 |
| UNIDENTIFIED | 9186 | 7245 | 456 | 0.941 | 0.789 | 0.858 | 0.28 |
| DUPLICATE_PAYMENT | 1306 | 1162 | 179 | 0.867 | 0.890 | 0.878 | 0.24 |
| SHORT_BANK_CHARGES | 330 | 283 | 29 | 0.907 | 0.858 | 0.882 | 0.24 |
| SHORT_DISPUTE | 6032 | 5040 | 0 | 1.000 | 0.836 | 0.910 | 0.18 |
| SHORT_WITHHOLDING | 8963 | 7633 | 51 | 0.993 | 0.852 | 0.917 | 0.17 |
| FX_DIFFERENCE | 31334 | 27507 | 95 | 0.997 | 0.878 | 0.933 | 0.13 |
| SHORT_DISCOUNT | 29125 | 27666 | 18 | 0.999 | 0.950 | 0.974 | 0.05 |
| RETURNED | 7963 | 7838 | 163 | 0.980 | 0.984 | 0.982 | 0.04 |

## Segmentos por puntos perdidos (aprox.: 30·líneas + 50·pares de recall)

| segmento | líneas | acc | líneas perdidas | pares gold | pares perdidos | P pares | pts ≈ |
|---|---|---|---|---|---|---|---|
| ar_ref_noise | 83717 | 0.934 | 5513 | 184668 | 34604 | 0.973 | 1.49 |
| ar_1toN_credit_note | 6374 | 0.664 | 2140 | 65243 | 30129 | 0.959 | 1.10 |
| ar_short_dispute | 2123 | 0.821 | 379 | 209969 | 31916 | 0.995 | 1.02 |
| ar_n_to_m | 3721 | 0.105 | 3329 | 13893 | 8452 | 0.711 | 0.51 |
| ar_1toN_text | 12345 | 0.928 | 890 | 72373 | 9133 | 0.983 | 0.35 |
| ar_same_amount_fifo | 12405 | 0.858 | 1762 | 12405 | 1754 | 0.884 | 0.19 |
| ar_fx | 1486 | 0.753 | 367 | 27756 | 3737 | 0.997 | 0.14 |
| ar_discount_late | 1534 | 0.576 | 650 | 5656 | 2478 | 0.825 | 0.13 |
| ar_partial | 7454 | 0.831 | 1256 | 7454 | 847 | 0.927 | 0.12 |
| ar_instalment | 9003 | 0.867 | 1193 | 9003 | 660 | 0.961 | 0.11 |
| unidentified | 6447 | 0.791 | 1347 | 0 | 0 | 0.000 | 0.10 |
| ar_short_discount | 11613 | 0.937 | 734 | 13153 | 914 | 0.955 | 0.08 |
| ar_short_withholding | 2729 | 0.842 | 432 | 9329 | 1179 | 0.990 | 0.07 |
| ar_dispute_settlement | 2309 | 0.751 | 576 | 2309 | 292 | 0.899 | 0.05 |
| ap_supplier_refund | 4917 | 0.898 | 500 | 4917 | 406 | 0.972 | 0.05 |
| ar_same_amount_month | 8562 | 0.947 | 452 | 8562 | 453 | 0.968 | 0.05 |
| ar_1toN_remittance | 2394 | 0.995 | 12 | 108471 | 1440 | 1.000 | 0.05 |
| unidentified_lookalike | 2739 | 0.783 | 592 | 0 | 0 | 0.000 | 0.04 |
| ar_retention | 1868 | 0.816 | 343 | 1868 | 292 | 0.876 | 0.04 |
| ar_replaced_invoice | 507 | 0.333 | 338 | 1069 | 143 | 0.597 | 0.03 |
| ar_1to1 | 37894 | 0.992 | 285 | 37894 | 35 | 0.992 | 0.02 |
| ar_overpayment | 784 | 0.740 | 203 | 784 | 162 | 0.934 | 0.02 |
| customer_advance | 540 | 0.569 | 232 | 0 | 0 | 0.000 | 0.02 |
| ar_third_party_agency | 711 | 0.889 | 78 | 1598 | 369 | 0.965 | 0.02 |
| ap_factored | 2760 | 0.943 | 156 | 7068 | 148 | 1.000 | 0.02 |
| ar_duplicate_payment | 1183 | 0.867 | 156 | 1183 | 61 | 0.883 | 0.01 |
| ap_confirming | 1610 | 0.952 | 77 | 16312 | 228 | 0.964 | 0.01 |
| ar_dd_returned | 6824 | 0.983 | 116 | 6824 | 117 | 0.983 | 0.01 |
| ar_advance_deduction | 541 | 0.880 | 65 | 541 | 47 | 0.890 | 0.01 |
| ar_retention_release | 1410 | 0.950 | 71 | 1410 | 26 | 0.955 | 0.01 |

## Tipo de fallo en los 12 segmentos que más pierden (líneas con partidas en el golden)

| seg                 |   lineas |   sin_partida |   sin_partida_t4_ok |   todo_mal |   le_faltan |   le_sobran |   pares_bien |   pares_perdidos |
|:--------------------|---------:|--------------:|--------------------:|-----------:|------------:|------------:|-------------:|-----------------:|
| ar_ref_noise        |    83717 |          3149 |                2471 |       1195 |         904 |         227 |        78242 |            34604 |
| ar_short_dispute    |     2123 |            45 |                  39 |          4 |         329 |           1 |         1744 |            31838 |
| ar_1toN_credit_note |     6374 |           997 |                 815 |         62 |         963 |          46 |         4306 |            29957 |
| ar_1toN_text        |    12345 |            37 |                  31 |          9 |         785 |          58 |        11456 |             9126 |
| ar_n_to_m           |     3721 |          1331 |                1262 |         77 |        1118 |          66 |         1129 |             6913 |
| ar_fx               |     1486 |           256 |                 253 |         14 |          88 |           0 |         1128 |             3722 |
| ar_discount_late    |     1534 |           254 |                 208 |         16 |          74 |          33 |         1157 |             2014 |
| ar_same_amount_fifo |    12405 |           386 |                 369 |       1368 |           0 |           1 |        10650 |             1754 |
| ar_short_discount   |    11613 |           199 |                 188 |        145 |          90 |          91 |        11088 |              770 |
| ar_partial          |     7454 |           592 |                 326 |        120 |           0 |         214 |         6528 |              712 |
| ar_instalment       |     9003 |           566 |                 476 |         77 |           0 |         192 |         8168 |              643 |

(`pares_bien` = mismas partidas que el golden; la línea aún puede fallar por importe o excepción.)
