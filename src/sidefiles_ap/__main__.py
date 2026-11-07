"""T2b · side_links_ap.parquet: enlaces deterministas desde payment_runs/, card_settlements/ y cheque_deposits/.

    python -m src.sidefiles_ap --phase window [--steps runs,ret,card,chq,conf]
    python -m src.sidefiles_ap --phase live

Salida: work/<fase>/side_links_ap.parquet con las columnas de contracts/schemas.md §3
(bank_txn_id, item_id, amount_cents, exception, rule, link_key, confidence, evidence).

Reglas (ids estables):
- T2b.run_transfer    cargo por beneficiario (e2e en el texto, o run_id + importe, o cuenta + importe + fecha) → documentos del e2e
- T2b.run_aggregated  cargo por fichero (run_id en el texto, o cuenta + importe + fecha) → documentos del run
- T2b.run_return      transferencia rechazada que vuelve (IN, e2e/run en el texto, importe = transferencia) → RETURNED
- T2b.run_confirming  cargo por vencimiento de una remesa de confirming (run en el texto) → e2e vencidos que suman el cargo
- T2b.card            abono de liquidación de tarjeta (cuenta + importe neto + fecha, o id/comercio en el texto) → pedidos, importe bruto
- T2b.card_chargeback contracargo (OUT, comercio + 4 últimos de la tarjeta + importe bruto) → RETURNED
- T2b.cheque          ingreso de depósito (id en el texto, o cuenta + total + fecha) → cheques por talón/sufijo + librador + importe
- T2b.cheque_return   cheque devuelto (OUT, nº de cheque en el texto + importe) → RETURNED
Fila de run → partida: nº de documento compacto + divisa + tipo (abono si importe < 0) + sociedad del fichero
(o filial pagada por la payment factory) + proveedor (beneficiario por nombre/IBAN en suppliers.csv).
Documento que paga menos que el saldo vivo → PARTIAL (importe pagado); divisa de la partida ≠ divisa de la cuenta →
FX_DIFFERENCE con el saldo completo.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import duckdb

from src.common import REPO, data_root, phase_dir, safe_stdout

STEPS = ["runs", "ret", "card", "chq", "conf"]
COLUMNS = "bank_txn_id, item_id, amount_cents, exception, rule, link_key, confidence, evidence"
# payment factory (POLICIES §5.2): la matriz paga facturas de estas filiales desde sus cuentas
FACTORY = {"ES00": ("ES01", "ES02", "PT01")}
DAYS_BEFORE = 3     # el cargo puede contabilizarse unos días antes de la fecha de ejecución
DAYS_AFTER = 10     # … o varios días después
DAYS_AMT = 5        # sin id en el texto: cuenta + importe exacto a ≤ 5 días
CONF_DAYS = 400     # vencimientos de confirming: hasta ~13 meses tras la remesa
RET_DAYS = 120      # una transferencia devuelta vuelve en semanas; un cheque/contracargo, en meses


def compact(x: str) -> str:
    return f"regexp_replace(upper(strip_accents(coalesce({x}, ''))), '[^A-Z0-9]', '', 'g')"


def detail_dirs(phase: str, data: str | None) -> list[str]:
    src = {"sample": ["sample"], "train": ["train"], "window": ["train"], "live": ["train", "live"],
           "final": ["train", "live", "final"]}[phase]
    return [phase_dir(s, data) for s in src]


def glob_csv(dirs: list[str], sub: str, phase: str, con=None) -> str | None:
    """SQL que lee los CSV de <sub> de las fases de `dirs`; cacheado en Parquet (data_cache/sidefiles_ap/) por nº y
    tamaño total de ficheros, porque leer ~1.000 CSV pequeños tarda minutos."""
    files = sorted(p.replace(os.sep, "/") for d in dirs for p in glob.glob(os.path.join(d, sub, "*.csv"))
                   if not os.path.basename(p).startswith("._"))
    if not files:
        return None
    lst = ", ".join(f"'{p}'" for p in files)
    raw = (f"read_csv([{lst}], all_varchar=true, header=true, union_by_name=true, quote='\"', escape='\"', "
           f"delim=',', filename=true)")
    if con is None:
        return raw
    sig = f"{len(files)}_{sum(os.path.getsize(p) for p in files)}"
    cdir = os.path.join(REPO, "data_cache", "sidefiles_ap")
    os.makedirs(cdir, exist_ok=True)
    pq = os.path.join(cdir, f"{phase}_{sub}_{sig}.parquet").replace(os.sep, "/")
    if not os.path.isfile(pq):
        con.sql(f"copy (select * from {raw}) to '{pq}' (format parquet)")
    return f"read_parquet('{pq}')"


def tol(x: str) -> str:
    return f"greatest(1, 0.005 * abs({x}))"


def build(phase: str, data: str | None = None, steps: list[str] | None = None, out_name: str = "side_links_ap.parquet") -> dict:
    t0 = time.time()
    steps = steps or STEPS
    work = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    os.makedirs(os.path.join(work, "tmp"), exist_ok=True)
    con = duckdb.connect()
    con.sql(f"set memory_limit='5GB'; set preserve_insertion_order=false; set temp_directory='{work}/tmp'")
    dirs = detail_dirs(phase, data)
    masters = os.path.join(data_root(data), "masters").replace(os.sep, "/")

    # ------------------------------------------------------------------ base
    con.sql(f"""create table L as select bank_txn_id, account_id, entity, booking_date, amount_cents, abs(amount_cents) abs_cents,
                abs(orig_amount_cents) orig_amount_cents, is_return_hint,
                currency, direction, text_norm, {compact('text_norm')} txt_c, statement_format
                from '{work}/lines.parquet'""")
    d_min, d_max = con.sql("select min(booking_date), max(booking_date) from L").fetchall()[0]
    con.sql(f"""create table I as select item_id, kind, side, entity, party_id, replace(doc_norm, ' ', '') doc_c, doc_digits,
                issue_date, due_date, currency, total_cents, open_at_start_cents open_c from '{work}/items.parquet'""")
    con.sql(f"""create table SUP as select supplier_id, entity, {compact('name')} name_c,
                nullif(upper(replace(iban, ' ', '')), '') iban
                from read_csv('{masters}/suppliers.csv', all_varchar=true, header=true, quote='"', escape='"', delim=',')""")
    con.sql(f"""create table CUS as select customer_id, entity, {compact('name')} name_c, {compact('trade_name')} trade_c
                from read_csv('{masters}/customers.csv', all_varchar=true, header=true, quote='"', escape='"', delim=',')""")
    con.sql("create table OUT (bank_txn_id varchar, item_id varchar, amount_cents bigint, exception varchar, rule varchar, link_key varchar, confidence double, evidence varchar)")
    stats = {}

    # ================================================================== payment runs
    pr_csv = glob_csv(dirs, "payment_runs", phase, con)
    if pr_csv and any(s in steps for s in ("runs", "ret", "conf")):
        fact = " or ".join(f"(p.fent = '{k}' and i.entity in ({', '.join(repr(x) for x in v)}))" for k, v in FACTORY.items())
        con.sql(f"""create table PR as
          select run_id, run_type, execution_date::date d, debtor_account_id acct, end_to_end_id e2e, {compact('end_to_end_id')} e2e_c,
                 {compact('beneficiary')} ben_c, nullif(upper(replace(beneficiary_iban, ' ', '')), '') iban, currency,
                 round(transfer_amount::double * 100)::bigint t_cents, document_number doc, {compact('document_number')} doc_c,
                 round(document_amount::double * 100)::bigint doc_cents,
                 split_part(split_part(filename, '/', -1), '_', 1) fent
          from {pr_csv}
          where execution_date::date between DATE '{d_min}' - {CONF_DAYS} and DATE '{d_max}' + {DAYS_BEFORE}""")
        # fila → partida: documento + divisa + tipo + sociedad (fichero o filial de la payment factory) + proveedor
        con.sql(f"""create table PRC as
            select p.*, i.item_id, i.kind, i.entity ient, i.party_id, i.total_cents, i.open_c, i.issue_date, i.due_date,
                   coalesce(s.supplier_id = i.party_id, false) sup_name,
                   coalesce(si.supplier_id = i.party_id, false) sup_iban,
                   count(*) over (partition by p.run_id, p.e2e, p.doc_c, p.doc_cents) ncand
            from PR p
            join I i on i.doc_c = p.doc_c and i.currency = p.currency and i.kind = case when p.doc_cents < 0 then 'APC' else 'API' end
            left join SUP s on s.name_c = p.ben_c and s.entity = i.entity
            left join SUP si on si.iban = p.iban and si.entity = i.entity
            where i.entity = p.fent or {fact}""")
        # proveedor del e2e: el que resuelven nombre/IBAN o, si no (factor, renombrado), el único candidato de sus filas
        con.sql("""create table E2P as
            select run_id, e2e, party_id, ient from (
              select run_id, e2e, party_id, ient, count(*) n, row_number() over (partition by run_id, e2e order by count(*) desc) rn
              from PRC where sup_name or sup_iban or ncand = 1 group by 1, 2, 3, 4) where rn = 1""")
        con.sql("""create table PRI as
          select * from (
            select c.*, coalesce(c.party_id = e.party_id and c.ient = e.ient, false) sup_e2e,
                   row_number() over (partition by c.run_id, c.e2e, c.doc_c, c.doc_cents order by
                       c.sup_name desc, c.sup_iban desc, coalesce(c.party_id = e.party_id and c.ient = e.ient, false) desc,
                       (abs(c.doc_cents) = c.total_cents) desc, (c.open_c > 0) desc, (c.issue_date <= c.d) desc, c.issue_date desc, c.item_id) rn
            from PRC c left join E2P e using (run_id, e2e))
          where rn = 1 and (sup_name or sup_iban or sup_e2e or ncand = 1 or abs(doc_cents) = total_cents)""")
        stats["pr_rows"] = con.sql("select count(*) from PR").fetchall()[0][0]
        stats["pr_rows_item"] = con.sql("select count(*) from PRI").fetchall()[0][0]

        # unidades de cargo: e2e (transfers, confirming) y run (aggregated)
        con.sql("""create table U as
          select 'e2e' ukind, run_id, e2e ukey, e2e_c key_c, acct, d, any_value(t_cents) cents, run_type, any_value(ben_c) ben_c
          from PR where run_type <> 'aggregated' group by all
          union all
          select 'run', run_id, run_id, run_id, acct, d, sum(t_cents), run_type, null
          from (select distinct run_id, acct, d, e2e, t_cents, run_type from PR where run_type = 'aggregated') group by all""")
        # enlace unidad → línea OUT de la cuenta (prioridad: id en texto > run + importe > importe + fecha)
        con.sql(f"""create table UL as
          with c as (
            select u.*, l.bank_txn_id, l.booking_date, l.currency lcur, abs(l.booking_date - u.d) dd,
                   contains(l.txt_c, u.key_c) key_in, contains(l.txt_c, u.run_id) run_in,
                   l.abs_cents = u.cents or l.orig_amount_cents = u.cents amt_eq
            from U u join L l on l.account_id = u.acct and l.direction = 'OUT'
                 and l.booking_date between u.d - {DAYS_BEFORE} and u.d + {DAYS_AFTER}
            where u.run_type <> 'confirming' and (contains(l.txt_c, u.key_c)
                   or (contains(l.txt_c, u.run_id) and (l.abs_cents = u.cents or l.orig_amount_cents = u.cents))
                   or (l.abs_cents = u.cents and abs(l.booking_date - u.d) <= {DAYS_AMT})))
          select *, case when key_in and amt_eq then 1.0 when key_in then 0.98 when run_in then 0.97 else 0.9 end conf,
                 case when key_in then 'key_in_text' when run_in then 'run_in_text+amount' else 'acct+amount+date' end how
          from c
          qualify row_number() over (partition by ukind, ukey, acct order by key_in desc, run_in desc, amt_eq desc, dd, bank_txn_id) = 1""")
        # una línea sólo paga una unidad (la mejor), y una unidad una sola línea
        con.sql("""create table UL1 as select * from UL
                   qualify row_number() over (partition by bank_txn_id order by conf desc, dd, ukey) = 1""")
        stats["units"] = con.sql("select count(*) from U where run_type <> 'confirming'").fetchall()[0][0]
        stats["units_linked"] = con.sql("select count(*) from UL1").fetchall()[0][0]

    if pr_csv and "runs" in steps:
        emit_run_pairs(con, "UL1", "'T2b.run_' || case when r.ukind = 'e2e' then 'transfer' else 'aggregated' end",
                       "u.ukind = 'e2e' and r.e2e = u.ukey or u.ukind = 'run' and r.run_id = u.ukey")
        stats["runs_pairs"] = con.sql("select count(*) from OUT where rule like 'T2b.run_%'").fetchall()[0][0]

    if pr_csv and "ret" in steps:
        # transferencia rechazada: abono (IN) con el e2e (o run + importe) en el texto y el importe de la transferencia
        con.sql(f"""create table RL as
          select u.*, l.bank_txn_id, l.booking_date, l.currency lcur, abs(l.booking_date - u.d) dd, true key_in, true run_in, true amt_eq,
                 case when contains(l.txt_c, u.key_c) then 1.0 when contains(l.txt_c, u.run_id) then 0.98 else 0.9 end conf,
                 case when contains(l.txt_c, u.key_c) then 'return:e2e_in_text+amount' when contains(l.txt_c, u.run_id) then 'return:run_in_text+amount'
                      else 'return:hint+beneficiary+amount' end how
          from U u join L l on l.account_id = u.acct and l.direction = 'IN' and l.abs_cents = u.cents
               and l.booking_date between u.d and u.d + {RET_DAYS}
          where u.ukind = 'e2e' and (contains(l.txt_c, u.key_c) or contains(l.txt_c, u.run_id)
                 or (l.is_return_hint and length(u.ben_c) >= 8 and contains(l.txt_c, left(u.ben_c, 10))))
          qualify row_number() over (partition by bank_txn_id order by contains(l.txt_c, u.key_c) desc, contains(l.txt_c, u.run_id) desc, dd) = 1
            and count(*) over (partition by bank_txn_id, contains(l.txt_c, u.key_c) or contains(l.txt_c, u.run_id)) = 1""")
        emit_run_pairs(con, "RL", "'T2b.run_return'", "r.e2e = u.ukey", returned=True)
        stats["ret_pairs"] = con.sql("select count(*) from OUT where rule = 'T2b.run_return'").fetchall()[0][0]

    if pr_csv and "conf" in steps:
        link_confirming(con)
        stats["conf_pairs"] = con.sql("select count(*) from OUT where rule = 'T2b.run_confirming'").fetchall()[0][0]

    # ================================================================== card settlements
    card_csv = glob_csv(dirs, "card_settlements", phase, con)
    if card_csv and "card" in steps:
        con.sql(f"""create table CARD as
          select settlement_id, merchant_id, settlement_date::date d, account_id acct, currency, {compact('order_number')} ref_c,
                 right(card, 4) card4, round(gross::double * 100)::bigint gross_cents, round(net::double * 100)::bigint net_cents,
                 split_part(split_part(filename, '/', -1), '_', 1) fent
          from {card_csv} where settlement_date::date between DATE '{d_min}' - {RET_DAYS} and DATE '{d_max}' + {DAYS_BEFORE}""")
        con.sql("""create table CARDI as
          select c.*, i.item_id, i.total_cents, i.open_c, i.currency icur, i.kind
          from CARD c join I i on i.entity = c.fent and i.doc_c = c.ref_c and i.kind = 'ARI'
          qualify row_number() over (partition by settlement_id, acct, ref_c, card4, gross_cents
                                     order by (i.total_cents = c.gross_cents) desc, (i.open_c > 0) desc, i.issue_date desc) = 1""")
        con.sql(f"""create table CU as
          with u as (select settlement_id, acct, d, any_value(merchant_id) mid, sum(net_cents) net, sum(gross_cents) gross from CARD group by 1, 2, 3),
          c as (select u.*, l.bank_txn_id, l.booking_date, abs(l.booking_date - u.d) dd,
                       contains(l.txt_c, u.settlement_id) key_in, contains(l.txt_c, u.mid) mid_in, l.abs_cents = u.net amt_eq
                from u join L l on l.account_id = u.acct and l.direction = 'IN'
                     and l.booking_date between u.d - {DAYS_BEFORE} and u.d + {DAYS_AFTER}
                where contains(l.txt_c, u.settlement_id) or (l.abs_cents = u.net and abs(l.booking_date - u.d) <= {DAYS_AMT}))
          select *, case when key_in and amt_eq then 1.0 when key_in then 0.97 when mid_in then 0.98 else 0.9 end conf,
                 case when key_in then 'settlement_in_text' when mid_in then 'merchant_in_text+net' else 'acct+net+date' end how
          from c
          qualify row_number() over (partition by settlement_id, acct order by key_in desc, amt_eq desc, mid_in desc, dd) = 1""")
        con.sql("create table CU1 as select * from CU qualify row_number() over (partition by bank_txn_id order by conf desc, dd) = 1")
        con.sql(f"""insert into OUT
          select u.bank_txn_id, r.item_id, r.gross_cents, '', 'T2b.card', u.settlement_id, u.conf,
                 'settlement=' || u.settlement_id || '; ' || u.how || '; order=' || r.ref_c || '; gross=' || r.gross_cents || '; net_total=' || u.net
          from CU1 u join CARDI r on r.settlement_id = u.settlement_id and r.acct = u.acct
          qualify row_number() over (partition by u.bank_txn_id, r.item_id order by r.gross_cents desc) = 1""")
        # contracargo: OUT, comercio + 4 últimos de la tarjeta en el texto + importe bruto
        con.sql(f"""insert into OUT
          select bank_txn_id, item_id, gross_cents, 'RETURNED', 'T2b.card_chargeback', settlement_id, 0.97,
                 'chargeback; mid=' || mid || '; card4=' || card4 || '; gross=' || gross_cents
          from (select l.bank_txn_id, r.item_id, r.gross_cents, r.settlement_id, r.merchant_id mid, r.card4,
                       row_number() over (partition by l.bank_txn_id order by r.d desc) rn
                from L l join CARDI r on r.acct = l.account_id and r.gross_cents = l.abs_cents
                     and r.d between l.booking_date - {RET_DAYS} and l.booking_date
                where l.direction = 'OUT' and contains(l.txt_c, r.merchant_id) and regexp_matches(l.text_norm, '\\b' || r.card4 || '\\b'))
          where rn = 1""")
        stats["card_pairs"] = con.sql("select count(*) from OUT where rule like 'T2b.card%'").fetchall()[0][0]

    # ================================================================== cheque deposits
    chq_csv = glob_csv(dirs, "cheque_deposits", phase, con)
    if chq_csv and "chq" in steps:
        con.sql(f"""create table CHQ as
          select deposit_id, deposit_date::date d, account_id acct, currency, round(deposit_total::double * 100)::bigint total,
                 cheque_number, {compact('drawer')} drawer_c, round(amount::double * 100)::bigint cents, {compact('stub_reference')} ref_c,
                 split_part(split_part(filename, '/', -1), '_', 1) fent
          from {chq_csv} where deposit_date::date between DATE '{d_min}' - {RET_DAYS} and DATE '{d_max}' + {DAYS_BEFORE}""")
        # cheque → factura: sociedad + importe (= total o saldo) + nº de documento exacto / sufijo ≥ 5 / librador = cliente
        con.sql("""create table CHQI as
          select * from (
            select c.*, i.item_id, i.total_cents, i.open_c, i.party_id,
                   case when i.doc_c = c.ref_c then 'doc_exact'
                        when length(c.ref_c) >= 5 and (suffix(i.doc_c, c.ref_c) or suffix(i.doc_digits, c.ref_c)) then 'doc_suffix'
                        when cu.name_c = c.drawer_c or cu.trade_c = c.drawer_c then 'drawer'
                        when length(c.drawer_c) >= 6 and (left(cu.name_c, 8) = left(c.drawer_c, 8) or contains(c.drawer_c, cu.name_c)
                                                          or contains(cu.name_c, c.drawer_c)) then 'drawer_fuzzy'
                        else 'amount' end how,
                   row_number() over (partition by c.deposit_id, c.acct, c.cheque_number order by
                       case when i.doc_c = c.ref_c then 0
                            when length(c.ref_c) >= 5 and (suffix(i.doc_c, c.ref_c) or suffix(i.doc_digits, c.ref_c)) then 1
                            when cu.name_c = c.drawer_c or cu.trade_c = c.drawer_c then 2
                            when length(c.drawer_c) >= 6 and (left(cu.name_c, 8) = left(c.drawer_c, 8) or contains(c.drawer_c, cu.name_c)
                                                              or contains(cu.name_c, c.drawer_c)) then 3 else 4 end,
                       (i.total_cents = c.cents) desc, (i.open_c > 0) desc, i.issue_date) rn,
                   count(*) over (partition by c.deposit_id, c.acct, c.cheque_number) ncand
            from CHQ c join (select i.* from I i where i.kind = 'ARI' and i.total_cents = i.open_c
                             union all select i.* from I i where i.kind = 'ARI' and i.total_cents <> i.open_c) i
                 on i.entity = c.fent and i.currency = c.currency and c.cents = case when i.total_cents = i.open_c then i.total_cents else i.open_c end
            left join CUS cu on cu.customer_id = i.party_id)
          where rn = 1 and (how in ('doc_exact', 'doc_suffix', 'drawer') or ncand = 1)""")
        con.sql(f"""create table QU as
          with u as (select deposit_id, acct, d, any_value(total) total from CHQ group by 1, 2, 3),
          c as (select u.*, l.bank_txn_id, l.booking_date, abs(l.booking_date - u.d) dd,
                       contains(l.txt_c, u.deposit_id) key_in, l.abs_cents = u.total amt_eq
                from u join L l on l.account_id = u.acct and l.direction = 'IN'
                     and l.booking_date between u.d - {DAYS_BEFORE} and u.d + {DAYS_AFTER}
                where contains(l.txt_c, u.deposit_id) or (l.abs_cents = u.total and abs(l.booking_date - u.d) <= {DAYS_AMT}))
          select *, case when key_in and amt_eq then 1.0 when key_in then 0.97 else 0.9 end conf,
                 case when key_in then 'deposit_in_text' else 'acct+total+date' end how
          from c qualify row_number() over (partition by deposit_id, acct order by key_in desc, amt_eq desc, dd) = 1""")
        con.sql("create table QU1 as select * from QU qualify row_number() over (partition by bank_txn_id order by conf desc, dd) = 1")
        con.sql("""insert into OUT
          select u.bank_txn_id, r.item_id, r.cents, '', 'T2b.cheque', u.deposit_id,
                 u.conf * case r.how when 'doc_exact' then 1.0 when 'doc_suffix' then 0.98 when 'drawer' then 0.95 else 0.9 end,
                 'deposit=' || u.deposit_id || '; ' || u.how || '; cheque=' || r.cheque_number || '; ' || r.how || '; stub=' || r.ref_c || '; amt=' || r.cents
          from QU1 u join CHQI r on r.deposit_id = u.deposit_id and r.acct = u.acct
          qualify row_number() over (partition by u.bank_txn_id, r.item_id order by r.cents desc) = 1""")
        # cheque devuelto: OUT, nº de cheque en el texto + importe
        con.sql(f"""insert into OUT
          select bank_txn_id, item_id, cents, 'RETURNED', 'T2b.cheque_return', deposit_id, 0.97,
                 'returned cheque=' || cheque_number || '; amt=' || cents
          from (select l.bank_txn_id, r.item_id, r.cents, r.deposit_id, r.cheque_number,
                       row_number() over (partition by l.bank_txn_id order by r.d desc) rn
                from L l join CHQI r on r.acct = l.account_id and r.cents = l.abs_cents
                     and r.d between l.booking_date - {RET_DAYS} and l.booking_date
                where l.direction = 'OUT' and regexp_matches(l.text_norm, '\\b' || r.cheque_number || '\\b'))
          where rn = 1""")
        stats["chq_pairs"] = con.sql("select count(*) from OUT where rule like 'T2b.cheque%'").fetchall()[0][0]

    # ------------------------------------------------------------------ salida: un par por (línea, partida)
    con.sql(f"""copy (select {COLUMNS} from OUT
                     qualify row_number() over (partition by bank_txn_id, item_id order by confidence desc, rule) = 1)
                to '{work}/{out_name}' (format parquet)""")
    stats["pairs"] = con.sql(f"select count(*) from '{work}/{out_name}'").fetchall()[0][0]
    stats["lines"] = con.sql(f"select count(distinct bank_txn_id) from '{work}/{out_name}'").fetchall()[0][0]
    stats["seconds"] = round(time.time() - t0)
    return stats


def emit_run_pairs(con, links: str, rule_sql: str, join_sql: str, returned: bool = False) -> None:
    """Pares de las unidades enlazadas: documento → partida, importe y excepción (POLICIES §2-3)."""
    exc = ("'RETURNED'" if returned else
           """case when r.kind = 'APC' then ''
                   -- saldo ya a 0 (devuelta y repagada, o liquidada fuera de los runs): se paga el documento, sin excepción
                   when r.remaining <= greatest(1, 0.005 * r.doc_cents) then ''
                   when r.doc_cents < r.remaining - greatest(1, 0.005 * r.remaining) then 'PARTIAL'
                   when r.currency <> r.lcur then 'FX_DIFFERENCE'
                   else '' end""")
    amt = ("abs(r.doc_cents)" if returned else
           """case when r.kind = 'APC' then abs(r.doc_cents)
                   when r.remaining <= greatest(1, 0.005 * r.doc_cents) then r.doc_cents
                   when r.doc_cents < r.remaining - greatest(1, 0.005 * r.remaining) then r.doc_cents
                   else greatest(r.doc_cents, r.remaining) end""")
    con.sql(f"""insert into OUT
      with r as (
        select p.*, u.bank_txn_id, u.booking_date, u.lcur, u.conf, u.how, u.ukind, u.ukey,
               -- saldo vivo antes de este cargo: saldo inicial menos lo ya pagado por cargos anteriores de la fase
               p.open_c - coalesce(sum(p.doc_cents) over (partition by p.item_id order by u.booking_date, u.ukey
                                                          rows between unbounded preceding and 1 preceding), 0) remaining
        from {links} u join PRI p on p.acct = u.acct and ({join_sql.replace('r.', 'p.')}))
      select r.bank_txn_id, r.item_id, {amt}, {exc}, {rule_sql}, r.ukey, r.conf,
             'run=' || r.run_id || '; e2e=' || r.e2e || '; ' || r.how || '; doc=' || r.doc || '; doc_amt=' || r.doc_cents
             || '; open=' || r.open_c || '; remaining=' || r.remaining || '; sup=' || case when r.sup_name then 'name' when r.sup_iban then 'iban' when r.sup_e2e then 'e2e' else 'none' end
      from r""")


def link_confirming(con) -> None:
    """Cargo por vencimiento de una remesa de confirming: 'REMESA <run> <k> PROVEEDORES' = k beneficiarios (e2e) cuyos
    pagos suman el cargo. Se asignan en orden cronológico: k = 1 → el e2e con ese importe aún no cargado; k > 1 →
    búsqueda de la combinación de k e2e pendientes que suma el cargo (en Python por remesa, pocas decenas de e2e)."""
    import itertools
    from datetime import timedelta

    con.sql("""create table CFU as
      select run_id, acct, e2e, any_value(cents) cents, max(due_date) mx from
        (select u.run_id, u.acct, u.ukey e2e, u.cents, p.due_date from U u join PRI p on p.e2e = u.ukey and p.acct = u.acct
         where u.run_type = 'confirming') group by 1, 2, 3""")
    con.sql(f"""create table CFL as
      select l.bank_txn_id, l.account_id acct, l.booking_date, l.abs_cents, r.run_id, l.currency lcur,
             try_cast(regexp_extract(l.text_norm, r.run_id || ' ([0-9]+)', 1) as int) k
      from L l join (select distinct run_id, acct, d from U where run_type = 'confirming') r
           on l.account_id = r.acct and contains(l.txt_c, r.run_id) and l.direction = 'OUT'
           and l.booking_date between r.d - {CONF_DAYS} and r.d + {CONF_DAYS}""")
    runs = con.sql("select run_id, acct from CFL group by 1, 2").fetchall()
    rows = []
    for run_id, acct in runs:
        units = con.sql(f"select e2e, cents, mx from CFU where run_id = '{run_id}' and acct = '{acct}'").fetchall()
        charges = con.sql(f"select bank_txn_id, booking_date, abs_cents, k from CFL where run_id = '{run_id}' and acct = '{acct}' "
                          "order by booking_date, bank_txn_id").fetchall()
        pending = {e: (c, mx) for e, c, mx in units}
        for bt, bd, amt, k in charges:
            k = k or 1
            found = None
            if k == 1:
                hits = [e for e, (c, mx) in pending.items() if c == amt]
                if len(hits) > 1:   # varios con el mismo importe: el de vencimiento más cercano al cargo
                    hits.sort(key=lambda e: abs((pending[e][1] or bd) - bd))
                    hits = hits[:1]
                if hits:
                    found = hits
            elif k <= 6:
                # vencidos (o sin vencimiento) hasta 3 días después del cargo; si quedan demasiados, los más cercanos
                cand = sorted(((e, c) for e, (c, mx) in pending.items() if mx is None or mx <= bd + timedelta(days=3)),
                              key=lambda t: abs((pending[t[0]][1] or bd) - bd))[:45]
                hits = [combo for combo in itertools.combinations(cand, k) if sum(c for _, c in combo) == amt]
                if len(hits) == 1:
                    found = [e for e, _ in hits[0]]
            if found:
                for e in found:
                    pending.pop(e)
                    rows.append((bt, e, k))
    if rows:
        con.sql("create table CFA (bank_txn_id varchar, e2e varchar, k int)")
        con.executemany("insert into CFA values (?, ?, ?)", rows)
        con.sql("""create table CFL1 as select c.bank_txn_id, a.e2e ukey, c.acct, c.booking_date, c.lcur, 'confirming:k=' || a.k how,
                   'e2e' ukind, 0.97 conf from CFA a join CFL c using (bank_txn_id)""")
        emit_run_pairs(con, "CFL1", "'T2b.run_confirming'", "r.e2e = u.ukey")


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data")
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--out", default="side_links_ap.parquet")
    a = ap.parse_args(argv)
    work = os.path.join(REPO, "work", a.phase)
    if not all(os.path.isfile(os.path.join(work, f)) for f in ("lines.parquet", "items.parquet")):
        from src.base_parquet.__main__ import build as build_base
        build_base(a.phase, a.data)
    st = build(a.phase, a.data, a.steps.split(","), a.out)
    print(f"[{a.phase}] " + " · ".join(f"{k}={v}" for k, v in st.items()) + f" -> work/{a.phase}/{a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
