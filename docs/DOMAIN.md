# Dominio de la conciliación bancaria

## 1. Contexto: Grupo Valtermia

Grupo Valtermia es un grupo industrial español ficticio, de tamaño parecido al de una empresa del IBEX-35. Diseña, fabrica y distribuye equipos, recambios, productos químicos y servicios para instalaciones de agua, como piscinas, wellness y riego.

El grupo tiene 17 sociedades en 15 países, 12 divisas, 13 bancos y aproximadamente 50 cuentas bancarias. Trabaja con decenas de miles de clientes: desde grandes cadenas que pagan muchas facturas en una sola transferencia hasta instaladores que pagan mediante recibos domiciliados. Tesorería recibe cientos de miles de movimientos bancarios por semestre.

Los datos son sintéticos. No representan a empresas, bancos, personas, NIF ni IBAN reales. Los IBAN tienen dígitos de control válidos, pero los códigos de entidad son inventados. La excepción son los tipos de cambio de [`data/masters/fx_rates.csv`](../data/masters/fx_rates.csv), basados en referencias reales del BCE; el MAD se deriva de una cesta de EUR y USD.

El reto de conciliación consiste en explicar cada línea bancaria:

1. qué facturas o abonos liquida;
2. cuánto de cada documento se considera pagado;
3. qué tipo de movimiento es cuando no liquida ningún documento;
4. qué excepción explica una diferencia entre el dinero y los documentos.

Las reglas exactas que siguen las etiquetas están en [`data/POLICIES.md`](../data/POLICIES.md). La estructura de archivos y columnas está en [`data/DATA_DICTIONARY.md`](../data/DATA_DICTIONARY.md), y el formato de entrega y puntuación en [`data/FORMAT.md`](../data/FORMAT.md).

## 2. Qué es una conciliación bancaria

Una conciliación bancaria es comparar lo que aparece en una cuenta bancaria con los documentos que deberían explicar esos movimientos.

### Ejemplo sencillo

Supongamos que Valtermia tiene una factura de cliente de **1.000 EUR**. El banco registra un ingreso de **1.000 EUR** con una referencia que permite reconocer al cliente. La conciliación dice:

- este movimiento bancario corresponde a esa factura;
- se aplican 1.000 EUR a la factura;
- no hay excepción.

Ahora supongamos que el cliente paga 980 EUR dentro del plazo que permite un descuento por pronto pago del 2 %. La factura queda totalmente liquidada porque los 20 EUR restantes son un descuento válido. La asignación sigue siendo por el importe completo de la factura, 1.000 EUR, y lleva la excepción `SHORT_DISCOUNT`.

La conciliación no consiste solamente en buscar importes iguales. Un movimiento puede pagar varias facturas, una factura puede pagarse en varios movimientos, y el texto bancario puede estar incompleto o usar el nombre comercial del cliente en vez del número de factura.

### Qué se observa en `sample/`

La muestra contiene un mes de TRAIN. Una exploración vectorizada con DuckDB y Polars encontró **8.499 movimientos bancarios** y **8.499 `bank_txn_id` distintos**, con fechas de contabilización del **2 al 30 de junio de 2025**, 5.945 abonos, 2.554 cargos, 10 cuentas, 3 formatos de extracto y 2 divisas. Las etiquetas contienen **36.574 filas** para esos movimientos: 915 no liquidan ninguna partida y el número de partidas por movimiento tiene mediana 1, percentil 90 igual a 7, percentil 99 aproximadamente 51 y máximo 585.

Esto no es una peculiaridad de la muestra: el README anuncia aproximadamente 210.000 movimientos y 750.000 asignaciones en medio año LIVE. La consecuencia práctica es que el diseño debe usar lecturas perezosas, joins y agregaciones vectorizadas, no recorrer filas en Python ni comparar cada movimiento contra todas las partidas.

## 3. Entidades y tipos de partidas

### Entidades principales

- **Sociedades**: empresas del grupo, cada una con país y moneda funcional.
- **Cuentas bancarias**: pertenecen a una sociedad y pueden tener monedas, bancos y formatos de extracto distintos.
- **Clientes**: compran a Valtermia y generan facturas de cuentas por cobrar. Una matriz puede pagar facturas de sus filiales.
- **Proveedores**: reciben pagos de Valtermia y generan facturas de cuentas por pagar. Una sociedad del grupo puede pagar facturas de otra mediante una fábrica de pagos.
- **Bancos, adquirentes y terceros**: bancos, procesadores de tarjetas, agencias de cobro, bancos confirmadores y otras contrapartes que pueden aparecer en el movimiento.

Los datos maestros se describen en [`data/masters/entities.csv`](../data/masters/entities.csv), [`data/masters/bank_accounts.csv`](../data/masters/bank_accounts.csv), [`data/masters/customers.csv`](../data/masters/customers.csv) y [`data/masters/suppliers.csv`](../data/masters/suppliers.csv). Los nombres maestros son una fotografía del estado disponible, por lo que un cliente puede aparecer con su nombre actual aunque el banco use un nombre antiguo.

### Partidas que sí se pueden liquidar

Una **partida abierta** es un documento que todavía tiene saldo pendiente:

- `ARI-...`: factura de cliente, es decir, dinero que un cliente debe a Valtermia.
- `ARC-...`: abono o nota de crédito de cliente.
- `API-...`: factura de proveedor, es decir, dinero que Valtermia debe pagar.
- `APC-...`: abono o nota de crédito de proveedor.

Las proformas, avisos de pago, remesas, ficheros de pagos y notificaciones no son partidas. Son evidencia auxiliar para interpretar el movimiento.

### Movimientos sin partida

Cuando una línea no liquida una factura o un abono, debe tener una categoría, por ejemplo:

- `BANK_FEES`: comisiones y gastos bancarios;
- `PAYROLL` o `SOCIAL_SECURITY`: nóminas o seguros sociales;
- `TAX_PAYMENT` o `TAX_REFUND`: pagos o devoluciones de impuestos;
- `CASH_POOL`: barridos de tesorería entre cuentas;
- `INTERNAL_TRANSFER`: transferencia entre cuentas de la misma sociedad;
- `INTERCOMPANY`: financiación entre sociedades del grupo;
- `FX_DEAL`: operación de cambio de divisa;
- `CUSTOMER_ADVANCE`: anticipo pagado contra una proforma;
- `CUSTOMER_REFUND` y `SUPPLIER_REFUND`: devoluciones;
- `BANK_REVERSAL`: error bancario y su anulación;
- `UNIDENTIFIED`: cobro que no se puede asociar a una partida abierta.

La lista completa y válida está en [`data/masters/categories.csv`](../data/masters/categories.csv) y en [`data/FORMAT.md`](../data/FORMAT.md).

## 4. Cómo se representa una conciliación

Cada línea bancaria se identifica con `bank_txn_id`. La vista normalizada está en `bank_transactions.csv.gz` y se explica en [`data/DATA_DICTIONARY.md`](../data/DATA_DICTIONARY.md).

Una fila de entrega tiene cinco campos:

```text
bank_txn_id,item_id,amount,category,exception
```

- Si la línea liquida partidas, hay una fila por partida. `item_id` identifica el documento, `amount` es el importe aplicado en la moneda del documento y `category` queda vacío.
- Si no liquida ninguna partida, hay exactamente una fila, con `item_id` y `amount` vacíos, y una `category`.
- `exception` explica la diferencia. Vacío significa que no hay excepción.

El importe de una fila es positivo, incluso para abonos. En una liquidación completa con descuento, gastos bancarios tolerados, retención fiscal, diferencia de cambio o sobrepago, se informa el importe abierto completo del documento. En una liquidación parcial o disputa, se informa lo realmente aplicado. La tolerancia del evaluador es:

> un importe es correcto si está dentro de `max(0.01, 0.5 % × importe de referencia)`.

## 5. Flujo de datos

1. **Extractos bancarios**: llegan en Norma 43, MT940 o camt.053. Se normalizan a una fila por línea bancaria y se eliminan duplicados de extractos solapados. **Ese trabajo ya viene hecho**: `bank_transactions.csv.gz` es la vista normalizada y sin duplicados de los mismos extractos; los ficheros brutos sólo hacen falta si queremos un campo que la vista no trae.
2. **Documentos contables operativos**: facturas y abonos de clientes y proveedores forman las partidas abiertas.
3. **Saldos iniciales**: en LIVE y FINAL, `open_items_at_start.csv.gz` incorpora partidas que quedaron abiertas en la fase anterior.
4. **Evidencia auxiliar**: avisos de pago, remesas de domiciliación, liquidaciones de tarjetas, depósitos de cheques, ficheros de pagos y PDFs aportan referencias que no siempre aparecen en el extracto.
5. **Datos maestros**: relacionan sociedades, cuentas, clientes, proveedores, monedas, condiciones de pago y tipos de cambio.
6. **Decisión por movimiento**: se identifica la contraparte, se buscan documentos candidatos, se decide si hay una o varias asignaciones, se calcula el importe aplicado y se marca la excepción o categoría.
7. **Entrega y evaluación**: el resultado es un CSV con una o varias filas por `bank_txn_id`.

Un aviso de pago puede llegar varios días después del dinero. Los PDFs pueden contener información decisiva que no aparece en los CSV, como cuotas, retenciones, anticipos, sustituciones de factura o el mes del servicio.

## 6. Casos difíciles y decisiones importantes

### Referencias y nombres imperfectos

El número de factura puede faltar, estar sin prefijo, truncado, con ceros eliminados o con dígitos cambiados. Puede aparecer el pedido del cliente en vez de la factura. El nombre puede estar truncado, ser un nombre comercial antiguo o corresponder a la matriz que paga por una filial.

La decisión debe usar conjuntamente texto, nombres, cuentas, fechas, divisa, relaciones de grupo y documentos auxiliares. No se debe asumir que quien paga es siempre la sociedad que emitió la factura.

### Una línea para muchas partidas

Una remesa SEPA, una liquidación de tarjetas, un depósito de cheques o un pago agrupado puede aparecer como un solo movimiento bancario y tener el detalle en otro fichero. En ese caso se asignan todas las partidas documentadas por el lote.

### Una partida en varios pagos

Una factura puede pagarse en cuotas, con un primer pago parcial y un último pago que cierra el saldo. Las cuotas no finales suelen llevar `PARTIAL`; el último pago normalmente no lleva excepción.

### Descuentos, retenciones y disputas

- `SHORT_DISCOUNT`: descuento por pronto pago tomado dentro del plazo, incluyendo hasta tres días de gracia.
- `SHORT_WITHHOLDING`: retención fiscal documentada. La factura queda liquidada.
- `SHORT_DISPUTE`: el cliente descuenta una cantidad y explica el motivo. Solo se aplica lo pagado y lo disputado queda abierto.
- `SHORT_BANK_CHARGES`: comisión de banco intermediario en una transferencia internacional fuera de SEPA, dentro de los límites de 60 EUR equivalentes y 2 % del documento.
- `PARTIAL`: queda saldo abierto sin una explicación más específica.

Un descuento tomado fuera de plazo no es un descuento válido: normalmente se etiqueta como `PARTIAL` y la diferencia queda abierta.

### Divisas

Si la moneda del banco y la del documento son distintas, la entidad financiera puede aplicar un tipo propio. Una factura totalmente liquidada en esta situación lleva `FX_DIFFERENCE` y el importe completo del documento en la moneda de la factura. No se calcula una ganancia o pérdida contable: solo se marca la diferencia.

### Devoluciones y duplicados

Un recibo domiciliado, cheque, tarjeta o transferencia puede devolverse y reabrir la partida: cada asignación revertida lleva `RETURNED`. Si un documento ya pagado se vuelve a pagar, el segundo pago lleva `DUPLICATE_PAYMENT`. Una devolución posterior se clasifica como devolución de cliente o proveedor, según corresponda.

### Cobros no identificados

Si no existe ninguna partida abierta que explique un cobro, se usa una sola fila con categoría y excepción `UNIDENTIFIED`. No se debe forzar una coincidencia solo porque el importe sea igual al de una factura distinta.

### Facturas sustituidas y abonos

Si un PDF dice “anula y sustituye”, el pago que menciona la factura antigua debe asignarse a la factura sustituta. La factura anulada y su abono no se liquidan por banco. Los abonos pueden compensarse en un pago posterior o pagarse mediante una transferencia independiente, según la evidencia.

## 7. TRAIN, LIVE, FINAL y drift

| Fase | Periodo | Qué está disponible |
|---|---|---|
| TRAIN | 1 de julio de 2021 a 30 de junio de 2025 | Entradas y etiquetas doradas en `golden_allocations.csv.gz`. |
| LIVE | 1 de julio de 2025 a 31 de diciembre de 2025 | Entradas y partidas iniciales, sin etiquetas doradas; 1 envío cada 10 s, cuenta la mejor nota (web `/reglas`). |
| FINAL | 1 de enero de 2026 a 30 de junio de 2026 | Entradas y partidas iniciales; los maestros finales se publican al cierre; solo hay un envío puntuado. |

**Drift** significa que el comportamiento o el formato cambia con el tiempo. Se espera, entre otros cambios:

- Banco del Moncayo pasa de Norma 43 a camt.053 el 1 de octubre de 2025.
- Un cliente grande cambia el formato de sus avisos de pago en septiembre de 2025.
- El 1 de enero de 2026 se incorpora ZA01 (aunque **ya hay 2 cobros de ZA01 en LIVE**, 29–30 dic 2025, en una cuenta que no está en `masters/`; ver §11), una filial de Sudáfrica con moneda ZAR y numeración de facturas `KWI...`; se renombra el 1 de abril de 2026 y sus maestros finales aparecen en `final/masters_final/`.
- Los clientes pueden renombrarse, cambiar de banco, desaparecer o pagar más tarde.

Por eso, una regla aprendida solo del texto o de los nombres históricos puede dejar de funcionar. La fecha, la fase, los maestros vigentes y los documentos de soporte son parte del contexto.

## 8. Qué está fuera de alcance

Este reto no pide:

- generar asientos contables;
- calcular ganancias o pérdidas de cambio;
- modificar facturas, saldos maestros o datos bancarios;
- decidir un plan contable distinto del proporcionado;
- conciliar movimientos fuera de la fase correspondiente;
- tratar proformas, avisos o ficheros de pagos como partidas liquidables.

Cada línea bancaria pertenece exactamente a una fase según su fecha de contabilización. El objetivo es producir asignaciones, categorías y excepciones correctas.

## 9. Fórmula exacta de puntuación

> Resumen; el análisis de dónde están los puntos, con cifras de TRAIN, está en [`SCORING.md`](SCORING.md).

La puntuación final es, redondeada a dos decimales:

```text
puntuación = 100 × (0.5 × F1_asignaciones
                   + 0.3 × acierto_por_movimiento
                   + 0.2 × macro-F1_excepciones)
```

### 9.1 F1 de asignaciones: 50 %

Se comparan los pares `(bank_txn_id, item_id)`.

- Un par enviado es verdadero positivo si el par aparece en la referencia y su importe está dentro de `max(0.01, 0.5 % del importe de referencia)`.
- `precision = TP / pares enviados`.
- `recall = TP / pares de referencia`.
- `F1 = 2 × precision × recall / (precision + recall)` cuando ambas métricas tienen sentido.

Este componente premia encontrar las partidas correctas sin inventar partidas adicionales y sin omitir las verdaderas.

### 9.2 Acierto por movimiento: 30 %

Cada línea bancaria cuenta como correcta solo si su conjunto completo coincide con la referencia:

- todas las partidas, importes dentro de tolerancia y excepciones correctas; o
- si no hay partida, categoría y excepción correctas.

Un movimiento con tres asignaciones y una excepción incorrecta no es completamente correcto para este componente, aunque alguna de sus filas sí contribuya al F1 de asignaciones.

### 9.3 Macro-F1 de excepciones: 20 %

La unidad es la clave `(bank_txn_id, item_id)`; para una línea sin partida, el `item_id` está vacío. Se calcula el F1 de cada clase de excepción no vacía que aparece en la referencia y después se hace la media simple entre esas clases:

```text
macro-F1 = media(F1_PARTIAL, F1_SHORT_DISCOUNT, ...)
```

Solo se promedian las clases no vacías presentes en el conjunto dorado. Confundir `PARTIAL` con `SHORT_DISCOUNT`, `RETURNED`, `UNIDENTIFIED` u otra excepción perjudica este componente.

El resultado también puede mostrar métricas auxiliares como precisión, recall, exactitud ponderada por importe en EUR y exactitud por segmento, pero la fórmula oficial es la de los tres componentes anteriores.

## 10. Qué decisiones mueven más la puntuación

1. **No perder partidas verdaderas**: el F1 de asignaciones pesa la mitad. Omitir asignaciones reduce el recall; añadir coincidencias dudosas reduce la precisión.
2. **Acertar el conjunto completo de cada movimiento**: el 30 % de acierto por movimiento exige que no falte ni sobre una fila y que importe y excepción sean correctos.
3. **Distinguir excepciones parecidas**: el 20 % restante usa macro-F1. Es especialmente importante diferenciar parcial, descuento válido, disputa, devolución, duplicado, diferencia de cambio y no identificado.
4. **Usar la unidad correcta del importe**: las filas de partidas se expresan en la moneda del documento, no necesariamente en la moneda de la cuenta bancaria.
5. **Reconocer lotes y relaciones de grupo**: una transferencia de una matriz, una fábrica de pagos o una remesa puede explicar varias partidas de otra sociedad.
6. **No forzar coincidencias**: un cobro no identificado bien clasificado es mejor que una factura incorrecta con el mismo importe.
7. **Respetar la tolerancia**: un importe fuera de `max(0.01, 0.5 % del importe de referencia)` no cuenta como la asignación correcta aunque el documento elegido sea correcto.

Para conocer las reglas operativas completas, consulta [`data/POLICIES.md`](../data/POLICIES.md). Para las restricciones de CSV, validación y envío, consulta [`data/FORMAT.md`](../data/FORMAT.md).

## 11. Datos medidos (TRAIN y LIVE completos)

Cifras de `explore/profile.py` → [`explore/PROFILE.md`](../explore/PROFILE.md).

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

### Las formas en que un pago se relaciona con facturas

| Forma | Ejemplo | Frecuencia (TRAIN, % de movimientos) |
|---|---|---|
| **1:1** | Un cliente paga una factura. | ~59 % de las líneas tienen exactamente 1 partida |
| **1:N** | Una transferencia paga 14 facturas y descuenta un abono. | 2–5 partidas: 23 %; 6–20: 7 %; >20: 2 % |
| **N:1** | Una factura se paga en 3 plazos (sólo lo dice el PDF). | `ar_instalment` 2,2 % |
| **Lote** | Una remesa SEPA de 95 recibos entra como **un único apunte**; el detalle está en `direct_debit_batches/`. Igual con tarjetas, cheques y nuestros pagos agrupados. | pocas líneas, **muchísimos pares** |
| **Sin partida** | Comisiones, nóminas, impuestos, barridos de cash pooling, préstamos… | ~9,6 % |

## 12. Errores que cuestan puntos

| Error | Qué pierde |
|---|---|
| Casar un cobro a una factura parecida que no es | 1 par falso (precisión F1) + la línea mal + quizás un FP de excepción. Peor que no casar. |
| Casar la factura correcta con importe mal (p. ej. lo cobrado en vez del saldo completo en `SHORT_DISCOUNT`/`FX_DIFFERENCE`) | el par no cuenta (F1) y la línea está mal |
| Poner la excepción equivocada o ninguna | la línea está mal (accuracy) y la clase pierde F1 |
| Dejar sin marcar una clase entera (p. ej. nunca emitir `OVERPAYMENT`) | ≈ 2 puntos |
| Olvidar líneas de la fase | cada una cuenta como fallo |
| Casar una partida ya liquidada sin marcar `DUPLICATE_PAYMENT` | par probablemente falso; hay que llevar saldos |
| Usar un `item_id` de una factura anulada ("anula y sustituye") | par falso: se liquida la sustituta |

## 13. Glosario rápido

- **Norma 43 / MT940 / camt.053**: tres formatos de extracto bancario (español, SWIFT, ISO 20022 XML).
- **SEPA Direct Debit / recibo domiciliado**: cobramos nosotros de la cuenta del cliente con su mandato (`VTM-ES-…`).
- **R-transaction**: devolución de un recibo SEPA (códigos AC06, MS02…).
- **Confirming**: un banco paga a nuestros proveedores (o nos paga a nosotros) por cuenta de un cliente.
- **Cash pooling**: barridos automáticos de saldo entre cuentas del grupo; no son facturas.
- **Rappel**: abono trimestral por volumen de compra.
- **Payment factory**: Valtermia Industrial (ES00) paga facturas de proveedores de ES01, ES02 y PT01.
