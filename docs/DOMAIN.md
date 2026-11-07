# El problema, explicado sin contabilidad

## La idea en una frase

Una empresa recibe y hace miles de pagos al día. El banco sólo le dice **"han entrado 3.988,80 € de un tal NORTEMAQ"**.
Contabilidad tiene una lista de **facturas pendientes de cobro y de pago**. *Conciliar* es decir, para cada movimiento del
banco, **qué facturas paga y cuánto de cada una**, y explicar por qué el dinero no cuadra cuando no cuadra.

Nuestro programa hace ese trabajo para 5 años de Grupo Valtermia (ficticio): 17 sociedades, 9+ divisas, ~1,9 M movimientos.

## Las piezas

| Pieza | Qué es | Dónde está |
|---|---|---|
| **Movimiento bancario** (`bank_txn_id`) | Una línea del extracto: fecha, importe con signo (+ entra dinero, − sale), texto libre del banco. | `bank_transactions.csv.gz` (y en bruto en `bank_statements/`) |
| **Partida abierta** (`item_id`) | Un documento que alguien nos debe o que debemos: factura emitida a cliente (`ARI`), abono a cliente (`ARC`), factura de proveedor (`API`), abono de proveedor (`APC`). | `ar_invoices`, `ap_invoices`, `credit_notes`, `open_items_at_start` |
| **Saldo abierto** | Lo que queda por pagar de una partida. Baja cuando se cobra, vuelve a subir si el cobro se devuelve. | Lo calculamos nosotros |
| **Tercero** | Cliente (`C…`) o proveedor (`S…`). Un cliente puede pertenecer a un grupo; otra empresa puede pagar por él. | `masters/customers.csv`, `suppliers.csv` |
| **Documentos de apoyo** | Avisos de pago del cliente ("os pago estas 300 facturas"), ficheros de remesas de recibos, liquidaciones de tarjeta, ingresos de cheques, nuestras órdenes de pago, PDFs de facturas con cláusulas. | `remittance_advices/`, `direct_debit_batches/`, `card_settlements/`, `cheque_deposits/`, `payment_runs/`, `pdfs/` |

**AR** (*accounts receivable*) = lo que nos deben los clientes → dinero que **entra**.
**AP** (*accounts payable*) = lo que debemos a proveedores → dinero que **sale**.
Un **abono** (credit note) es una factura en negativo: reduce lo que el otro debe.

## Qué hay que entregar

Un CSV con una fila por (movimiento, partida):

```
bank_txn_id,item_id,amount,category,exception
BT-2025-0104417,ARI-…00871,1210.00,,
BT-2025-0104417,ARC-…00112,145.20,,                 ← abono compensado: importe positivo
BT-2025-0104417,ARI-…00902,2330.00,,SHORT_DISCOUNT  ← se liquida entera aunque llegó menos dinero
BT-2025-0001240,,,BANK_FEES,                         ← movimiento sin factura: se clasifica
BT-2025-0001301,,,UNIDENTIFIED,UNIDENTIFIED          ← no sabemos de quién es: se marca, no se fuerza
```

- `amount` va **en la divisa de la factura**, siempre positivo.
- Si el movimiento no liquida ninguna partida, una sola fila con `category` (comisión, nómina, cash pooling, impuestos…).

## Las formas en que un pago se relaciona con facturas

| Forma | Ejemplo | Frecuencia (TRAIN, % de movimientos) |
|---|---|---|
| **1:1** | Un cliente paga una factura. | ~59 % de las líneas tienen exactamente 1 partida |
| **1:N** | Una transferencia paga 14 facturas y descuenta un abono. | 2–5 partidas: 23 %; 6–20: 7 %; >20: 2 % |
| **N:1** | Una factura se paga en 3 plazos (sólo lo dice el PDF). | `ar_instalment` 2,2 % |
| **Lote** | Una remesa SEPA de 95 recibos entra como **un único apunte**; el detalle está en `direct_debit_batches/`. Igual con tarjetas, cheques y nuestros pagos agrupados. | pocas líneas, **muchísimos pares** |
| **Sin partida** | Comisiones, nóminas, impuestos, barridos de cash pooling, préstamos… | ~9,6 % |

## Por qué el dinero no cuadra (las excepciones)

| Código | Qué pasó en la vida real | Importe que se pone |
|---|---|---|
| *(vacío)* | Pagó lo que decía la factura. | saldo abierto |
| `SHORT_DISCOUNT` | El cliente pagó pronto y se descontó el % que permiten sus condiciones (p. ej. "2 % a 10 días"). Sólo vale si pagó a tiempo (+3 días de gracia). | factura entera |
| `SHORT_WITHHOLDING` | El cliente (México, Brasil, Marruecos) retuvo un impuesto (1,25 %, 1,5 % o 10 %). | factura entera |
| `SHORT_BANK_CHARGES` | Un banco intermediario se quedó una comisión en una transferencia internacional (≤ 60 € y ≤ 2 %). | factura entera |
| `FX_DIFFERENCE` | La factura está en USD y el dinero llegó en EUR: el banco cambió a su tipo. | factura entera (en su divisa) |
| `OVERPAYMENT` | Pagó de más. El exceso queda a cuenta. | factura entera |
| `PARTIAL` | Pagó una parte y sigue debiendo el resto (plazos, "a cuenta", retención de garantía, descuento fuera de plazo). | lo pagado |
| `SHORT_DISPUTE` | Pagó de menos **con un motivo** (mercancía dañada, diferencia de precio…), normalmente explicado en su aviso de pago. | lo pagado |
| `DUPLICATE_PAYMENT` | Pagó dos veces una factura ya cobrada. | lo pagado otra vez |
| `RETURNED` | Un recibo domiciliado vuelve (el cliente no tenía saldo), un cheque rebota, un *chargeback* de tarjeta. La factura se **reabre**. | lo que se reabre |
| `UNIDENTIFIED` | Entra dinero que no corresponde a ninguna factura abierta. **Marcar, no forzar**, aunque el importe coincida con alguna. | — |

Las reglas exactas están en `POLICIES.md`: las etiquetas las siguen al pie de la letra.

## Por qué es difícil

1. **El número de factura casi nunca está.** Sólo en ~22 % de los pares aparece literal en el texto del banco. Lo
   normal es: nombre del pagador truncado, número sin prefijo, dígitos cambiados, el nº de pedido del cliente, o nada.
2. **Hay que saber quién paga.** En camt.053 viene el nombre y a veces el IBAN; en Norma 43 y MT940 (la mitad de las
   líneas) sólo hay texto libre de 38/65 caracteres. A veces paga la matriz, un banco de *confirming* o una agencia de
   cobros en nombre del cliente.
3. **Hay que llevar el saldo de cada factura en el tiempo.** Una factura pagada ya no se puede volver a casar (salvo
   duplicado); una devuelta vuelve a estar abierta. En LIVE/FINAL empezamos desde `open_items_at_start`.
4. **Mismo importe, varias facturas** (cuotas fijas mensuales): sin referencia, se paga la más antigua (FIFO); si el
   texto dice "MANT. MAYO", la de ese mes.
5. **Información sólo en PDF**: plazos, retenciones de garantía, anticipos a deducir, "anula y sustituye a…", cambio de IBAN.
6. **Divisas**: el banco aplica su cambio (0,1–1,5 % peor que el BCE de `fx_rates.csv`).
7. **Cambios a propósito entre fases** (*drift*): un banco pasa de Norma 43 a camt.053 (oct-2025), un gran cliente cambia el
   formato de sus avisos (sep-2025), nueva filial en Sudáfrica en FINAL (ZAR, numeración "KWI…"), clientes renombrados.

## Fases del concurso

| Fase | Periodo | Movimientos | Tenemos respuestas | Uso |
|---|---|---|---|---|
| TRAIN | 2021-07 → 2025-06 | 1,49 M | sí (`golden_allocations`) | aprender y **autoevaluarnos** |
| LIVE | 2025-07 → 2025-12 | 203 k | no | clasificación pública; 1 envío / 10 s, cuenta el mejor |
| FINAL | 2026-01 → 2026-06 | 220 k | no | **un solo envío**, sale a las 18:00, cierre 19:30 |

## Volúmenes (medidos, `explore/PROFILE.md`)

| | TRAIN | LIVE |
|---|---|---|
| movimientos | 1.489.931 | 203.083 |
| facturas AR / AP / abonos | 3,96 M / 1,53 M / 150 k | 487 k / 199 k / 19,5 k |
| partidas abiertas al inicio | — | 325.419 (1.740 M) |
| filas de remesas SEPA / tarjetas / cheques / órdenes de pago | 714 k / 148 k / 42 k / 1,42 M | 94 k / 19 k / 6 k / 206 k |
| avisos de pago / PDFs (ficheros) | 13,4 k / 33,2 k | 6,9 k / 16,7 k |
| formato del extracto (camt / MT940 / N43) | 49 / 29 / 21 % | 52 / 29 / 19 % |

Claves de unión: `bank_txn_id` (movimiento ↔ golden ↔ entrega); `item_id` (golden/entrega ↔ facturas y abonos; el
100 % de los `item_id` del golden existe en las tablas de partidas); `account_id` (movimiento ↔ `bank_accounts` ↔
ficheros de lote); `batch_id`/`end_to_end_id`/`deposit_id`/`settlement_id`/`run_id` (texto del banco ↔ ficheros de
detalle); `customer_id`/`supplier_id`/`party_id` (partida ↔ maestros); `document_number` (texto, avisos, PDFs ↔ partida).
Sólo el 2,3 % de los pares cruza divisa (FX) y el 2,1 % cruza sociedad (payment factory ES00).

**Patrones de texto** (quitando dígitos/ids): las líneas **sin partida** (excepto `UNIDENTIFIED`, `CUSTOMER_REFUND`,
`CUSTOMER_ADVANCE`, `SUPPLIER_REFUND`) caben en **< 200 patrones** (p. ej. `BANK_FEES` 70 k líneas = 14 patrones,
`CASH_POOL` 12,6 k = 5). Los cobros son muy variados (64 k–96 k patrones por formato) porque llevan el nombre del
pagador. → clasificar sin-partida por patrón es barato; los cobros se casan con tercero + importe.

**Rarezas encontradas:** 2 movimientos de **ZA01** (cuenta `ZA01-UMVC-ZAR`, ZAR) ya en LIVE (29–30 dic 2025), que no
están en `masters/` (ni la sociedad ni la cuenta): son cobros de proforma (→ `CUSTOMER_ADVANCE`). Banco del Moncayo
cambia a camt exactamente el 2025-10-01 en sus 4 cuentas. 26 líneas TRAIN / 5 LIVE sin texto. Sin duplicados de id,
ni importes 0, ni fechas valor raras.

## Errores que cuestan puntos

| Error | Qué pierde |
|---|---|
| Casar un cobro a una factura parecida que no es | 1 par falso (precisión F1) + la línea mal + quizás un FP de excepción. Peor que no casar. |
| Casar la factura correcta con importe mal (p. ej. lo cobrado en vez del saldo completo en `SHORT_DISCOUNT`/`FX_DIFFERENCE`) | el par no cuenta (F1) y la línea está mal |
| Poner la excepción equivocada o ninguna | la línea está mal (accuracy) y la clase pierde F1 |
| Dejar sin marcar una clase entera (p. ej. nunca emitir `OVERPAYMENT`) | ≈ 2 puntos |
| Olvidar líneas de la fase | cada una cuenta como fallo |
| Casar una partida ya liquidada sin marcar `DUPLICATE_PAYMENT` | par probablemente falso; hay que llevar saldos |
| Usar un `item_id` de una factura anulada ("anula y sustituye") | par falso: se liquida la sustituta |

## Glosario rápido

- **Norma 43 / MT940 / camt.053**: tres formatos de extracto bancario (español, SWIFT, ISO 20022 XML).
- **SEPA Direct Debit / recibo domiciliado**: cobramos nosotros de la cuenta del cliente con su mandato (`VTM-ES-…`).
- **R-transaction**: devolución de un recibo SEPA (códigos AC06, MS02…).
- **Confirming**: un banco paga a nuestros proveedores (o nos paga a nosotros) por cuenta de un cliente.
- **Cash pooling**: barridos automáticos de saldo entre cuentas del grupo; no son facturas.
- **Rappel**: abono trimestral por volumen de compra.
- **Payment factory**: Valtermia Industrial (ES00) paga facturas de proveedores de ES01, ES02 y PT01.
