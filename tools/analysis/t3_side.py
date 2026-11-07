"""T3 análisis: pares (línea, partida) que se pueden sacar de los ficheros de detalle en la ventana.

Unidad de pago (remesa, liquidación de tarjeta, depósito de cheques, transferencia/fichero/remesa de confirming) →
línea bancaria: misma cuenta, fecha a ≤ 7 días y (id de la unidad dentro del texto **o** mismo importe absoluto a ≤ 5 días;
el id basta sin importe porque en ficheros FX el total en divisa de factura ≠ cargo).
Fila de detalle → partida: nº de documento compacto (y para cheques, talón con sufijo del nº).
Inversos (devolución de recibo, chargeback, cheque devuelto, transferencia rechazada): id/mandato/tarjeta en el texto +
importe de la fila. Escribe work/analysis/side_pairs.parquet (bank_txn_id, item_id, src, link).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from t3_prep import OUT, W0, W1, compact, connect, load  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "data/bankrec-docs-and-masters/train"
D0, D1 = "2024-05-15", "2025-07-15"  # margen alrededor de la ventana


def rd(folder):
    return f"read_csv('{T}/{folder}/*.csv', all_varchar=true, union_by_name=true, filename=true)"


def main():
    t = time.time()
    con = connect()
    load(con, ["bt_w", "items_w"])
    con.sql(f"create temp table L as select *, {compact('txt')} txt_c, abs(amt_cents) abs_cents from bt_w")
    con.sql("create temp table I as select item_id, kind, entity, doc_c, total_cents, due_date from items_w")

    # ---------- filas de detalle normalizadas: (src, unit_key(s), account, date, row_cents, ref_c, extra)
    con.sql(f"""
    create temp table dd as
    select batch_id, end_to_end_id, mandate_id, account_id, collection_date::date d, cast(round(batch_total::double*100) as bigint) unit_cents,
           cast(round(amount::double*100) as bigint) row_cents, {compact('remittance_info')} ref_c, re_presentation,
           split_part(split_part(filename, '/', -1), '_', 1) entity
    from {rd('direct_debit_batches')} where collection_date between '{D0}' and '{D1}'""")
    con.sql(f"""
    create temp table card as
    select settlement_id, merchant_id, account_id, settlement_date::date d, card, {compact('order_number')} ref_c,
           cast(round(gross::double*100) as bigint) gross_cents, cast(round(net::double*100) as bigint) net_cents,
           split_part(split_part(filename, '/', -1), '_', 1) entity
    from {rd('card_settlements')} where settlement_date between '{D0}' and '{D1}'""")
    con.sql(f"""
    create temp table chq as
    select deposit_id, account_id, deposit_date::date d, cast(round(deposit_total::double*100) as bigint) unit_cents,
           cheque_number, cast(round(amount::double*100) as bigint) row_cents, {compact('stub_reference')} ref_c,
           split_part(split_part(filename, '/', -1), '_', 1) entity
    from {rd('cheque_deposits')} where deposit_date between '{D0}' and '{D1}'""")
    con.sql(f"""
    create temp table pr as
    select run_id, run_type, end_to_end_id, debtor_account_id account_id, execution_date::date d,
           cast(round(transfer_amount::double*100) as bigint) transfer_cents, {compact('document_number')} ref_c,
           document_type, cast(round(document_amount::double*100) as bigint) doc_cents,
           split_part(split_part(filename, '/', -1), '_', 1) entity
    from {rd('payment_runs')} where execution_date between '{D0}' and '{D1}'""")

    # ---------- fila → partida
    con.sql("""
    create temp table dd_i as select dd.*, i.item_id from dd join I i on i.entity=dd.entity and i.doc_c=dd.ref_c and i.kind='ARI'""")
    con.sql("""
    create temp table card_i as select card.*, i.item_id from card join I i on i.entity=card.entity and i.doc_c=card.ref_c and i.kind='ARI'""")
    con.sql("""
    create temp table chq_i as
    select chq.*, i.item_id, (i.doc_c = chq.ref_c) exact from chq join I i on i.entity=chq.entity and i.kind='ARI'
      and i.total_cents = chq.row_cents
      and (i.doc_c = chq.ref_c or (length(chq.ref_c) >= 5 and regexp_matches(chq.ref_c, '^[0-9]+$') and suffix(i.doc_c, chq.ref_c)))""")
    # payment factory: ES00 paga facturas de ES01/ES02/PT01 → no se exige misma entidad, sí nº + importe
    con.sql("""
    create temp table pr_i as
    select pr.*, i.item_id, count(*) over (partition by pr.run_id, pr.end_to_end_id, pr.ref_c, pr.doc_cents) namb
    from pr join I i on i.doc_c = pr.ref_c and i.total_cents = abs(pr.doc_cents)
      and i.kind = case when pr.doc_cents < 0 then 'APC' else 'API' end
      and (i.entity = pr.entity or (pr.entity = 'ES00' and i.entity in ('ES01','ES02','PT01')))""")

    # ---------- unidades de pago
    # unidad: key = id único de la unidad, tkey = id que se busca en el texto, need_amt = el id no basta (lo comparten
    # varios cargos: confirming tiene un cargo por vencimiento dentro de la misma remesa)
    con.sql("""
    create temp table units as
    select 'dd_batch' src, batch_id key, batch_id tkey, false need_amt, account_id, d, any_value(unit_cents) cents, 1 sgn
    from dd group by all
    union all select 'card', settlement_id, settlement_id, false, account_id, d, sum(net_cents), 1 from card group by all
    union all select 'cheque', deposit_id, deposit_id, false, account_id, d, any_value(unit_cents), 1 from chq group by all
    union all select 'pr_'||run_type||'_e2e', end_to_end_id, end_to_end_id, false, account_id, d, any_value(transfer_cents), -1
    from pr group by all
    union all select 'pr_aggregated_run', run_id, run_id, false, account_id, d, sum(doc_cents), -1
    from pr where run_type = 'aggregated' group by all
""")
    # enlace por igualdad (cuenta, día): cada unidad se expande a los días d-7 … d+7; signo compatible
    con.sql(f"""
    create temp table ulink as
    with ud as (select u.*, {compact('u.tkey')} key_c, u.d + r.k::int dday, abs(r.k) dd
                from units u, range(-7, 8) r(k))
    select distinct ud.src, ud.key, ud.account_id, ud.d, l.bank_txn_id,
           case when length(ud.key_c) >= 8 and contains(l.txt_c, ud.key_c) then 'key_in_text' else 'acct_amount_date' end link
    from ud join L l on l.account_id = ud.account_id and l.booking_date = ud.dday
    where sign(l.amt_cents) = ud.sgn
      and ((length(ud.key_c) >= 8 and contains(l.txt_c, ud.key_c) and (not ud.need_amt or l.abs_cents = abs(ud.cents)))
           or (l.abs_cents = abs(ud.cents) and ud.dd <= 5))""")

    con.sql("""
    create temp table sp as
    select l.bank_txn_id, r.item_id, l.src, l.link from ulink l join dd_i r on l.src='dd_batch' and r.batch_id=l.key and r.account_id=l.account_id
    union all
    select l.bank_txn_id, r.item_id, l.src, l.link from ulink l join card_i r on l.src='card' and r.settlement_id=l.key and r.account_id=l.account_id
    union all
    select l.bank_txn_id, r.item_id, l.src||case when r.exact then '' else '_suffix' end, l.link from ulink l
      join chq_i r on l.src='cheque' and r.deposit_id=l.key and r.account_id=l.account_id
    union all
    select l.bank_txn_id, r.item_id, l.src, l.link from ulink l join pr_i r on l.src like 'pr_%_e2e' and r.end_to_end_id=l.key and r.account_id=l.account_id
    union all
    select l.bank_txn_id, r.item_id, l.src, l.link from ulink l join pr_i r on l.src = 'pr_aggregated_run' and r.run_id=l.key and r.account_id=l.account_id
""")
    # confirming: un cargo por vencimiento, a menudo meses después de la ejecución → remesa citada en el texto y, para
    # cada cargo, las filas cuyo vencimiento cae entre el cargo anterior de esa remesa y éste
    con.sql(f"""
    insert into sp
    with r as (select distinct run_id, account_id, d from pr where run_type = 'confirming'),
    lk as (select l.bank_txn_id, l.booking_date, r.run_id, r.account_id,
                  lag(l.booking_date) over (partition by r.run_id order by l.booking_date, l.bank_txn_id) prev_bd
           from r join L l on l.account_id = r.account_id and l.amt_cents < 0
           where l.booking_date between r.d and r.d + 200 and contains(l.txt_c, r.run_id))
    select lk.bank_txn_id, p.item_id, 'pr_confirming_venc', 'key_in_text'
    from lk join pr_i p on p.run_id = lk.run_id and p.account_id = lk.account_id join I i on i.item_id = p.item_id
    where i.due_date <= lk.booking_date and (lk.prev_bd is null or i.due_date > lk.prev_bd)""")

    # ---------- inversos
    con.sql(f"""
    insert into sp
    select bank_txn_id, item_id, 'dd_return', 'mandate_in_text' from (
      select l.bank_txn_id, r.item_id, row_number() over (partition by l.bank_txn_id order by r.d desc) rn
      from L l join dd_i r on r.account_id = l.account_id and r.row_cents = l.abs_cents and r.d < l.booking_date
      where l.amt_cents < 0 and contains(l.txt_c, {compact('r.mandate_id')})) where rn = 1""")
    con.sql(f"""
    insert into sp
    select bank_txn_id, item_id, 'card_chargeback', 'card_mid_in_text' from (
      select l.bank_txn_id, r.item_id, row_number() over (partition by l.bank_txn_id order by r.d desc) rn
      from L l join card_i r on r.account_id = l.account_id and r.gross_cents = l.abs_cents and r.d <= l.booking_date
      where l.amt_cents < 0 and contains(l.txt_c, r.merchant_id) and contains(l.txt, right(r.card, 4))) where rn = 1""")
    con.sql(f"""
    insert into sp
    select l.bank_txn_id, r.item_id, 'cheque_return', 'chq_in_text'
    from L l join chq_i r on r.account_id = l.account_id and r.row_cents = l.abs_cents and r.d <= l.booking_date
    where l.amt_cents < 0 and contains(l.txt_c, r.cheque_number)""")
    con.sql(f"""
    insert into sp
    select l.bank_txn_id, r.item_id, 'pr_return', 'e2e_in_text'
    from L l join pr_i r on r.account_id = l.account_id and r.transfer_cents = l.abs_cents and r.d <= l.booking_date
    where l.amt_cents > 0 and contains(l.txt_c, {compact('r.end_to_end_id')})""")

    con.sql(f"""copy (select bank_txn_id, item_id, min(src) src, min(link) link from sp group by 1, 2)
               to '{OUT}/side_pairs.parquet' (format parquet)""")
    print(con.sql(f"select src, link, count(*) pairs, count(distinct bank_txn_id) lines from '{OUT}/side_pairs.parquet' group by all order by 1,2"))
    print(f"ok {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
