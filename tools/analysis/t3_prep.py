"""T3 análisis (sólo lectura): tablas base de la ventana TRAIN 2024-07 → 2025-06.

Escribe work/analysis/<tabla>.parquet (DuckDB en memoria; sólo se guarda lo recortado a la ventana):
  bt      líneas TRAIN (seq por fecha, importe en céntimos, texto compacto)
  items   partidas AR/AP/abonos (tercero, documento, importe en céntimos)
  g       golden TRAIN con seq de la línea
  ivl     intervalos de saldo vivo por partida reconstruidos del golden (open_cents válido para seq in (from_seq, to_seq])
  gw      pares golden de la ventana con open_before_cents
  tok     tokens alfanuméricos (con dígito) del texto de las líneas de la ventana

Uso: .venv/bin/python tools/analysis/t3_prep.py
"""
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
C = ROOT / "data_cache"
OUT = ROOT / "work/analysis"
W0, W1 = "2024-07-01", "2025-06-30"


def connect():
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.sql("set memory_limit='5GB'; set threads=8; set preserve_insertion_order=false")
    con.sql(f"set temp_directory='{OUT / 'tmp'}'")
    return con


def load(con, names=("bt_w", "items_w", "gw", "ivl_w", "tok", "g")):
    """Vistas sobre los Parquet de work/analysis para los scripts de análisis."""
    for n in names:
        con.sql(f"create or replace view {n} as select * from '{OUT}/{n}.parquet'")


# compacta: mayúsculas, sin acentos, sólo [A-Z0-9]
def compact(x):
    return f"regexp_replace(upper(strip_accents(coalesce({x},''))), '[^A-Z0-9]', '', 'g')"


def main():
    t = time.time()
    con = connect()
    con.sql(f"""
    create or replace table bt as
    select bank_txn_id, row_number() over (order by booking_date, bank_txn_id) seq,
           account_id, entity, statement_format fmt, booking_date, value_date,
           cast(round(amount*100) as bigint) amt_cents, currency, bank_code,
           cast(round(orig_amount*100) as bigint) orig_cents, orig_currency,
           upper(strip_accents(concat_ws(' ~ ', text, remittance_info, ref1, ref2, end_to_end_id, counterparty_name))) txt,
           counterparty_account cp_iban
    from '{C}/train/bank_transactions.parquet'""")

    con.sql(f"""
    create or replace table items as
    select item_id, 'ARI' kind, entity, customer_id party_id, document_number doc, customer_po po,
           null::varchar related_doc, null::varchar reason, currency,
           cast(round(total_amount*100) as bigint) total_cents, issue_date, due_date, payment_terms, payment_method
    from '{C}/train/ar_invoices.parquet'
    union all
    select item_id, 'API', entity, supplier_id, supplier_invoice_number, cast(our_po as varchar), null, null, currency,
           cast(round(total_amount*100) as bigint), issue_date, due_date, payment_terms, payment_method
    from '{C}/train/ap_invoices.parquet'
    union all
    select item_id, case side when 'AR' then 'ARC' else 'APC' end, entity, party_id, document_number, null,
           related_document, reason, currency, cast(round(total_amount*100) as bigint), issue_date, null, null, null
    from '{C}/train/credit_notes.parquet'""")
    con.sql(f"alter table items add column doc_c varchar; update items set doc_c = {compact('doc')}")

    con.sql(f"""
    create or replace table g as
    select g.bank_txn_id, g.item_id, cast(round(g.amount*100) as bigint) amt_cents, g.category, g.exception, g.segment,
           g.difficulty, b.seq, b.booking_date
    from '{C}/train/golden_allocations.parquet' g join bt b using (bank_txn_id)""")

    # Saldo vivo: RETURNED reabre (+), DUPLICATE_PAYMENT no mueve (la partida ya estaba a 0), el resto resta.
    con.sql("""
    create or replace table ev as
    select item_id, seq,
           case when exception='RETURNED' then amt_cents when exception='DUPLICATE_PAYMENT' then 0 else -amt_cents end eff
    from g where item_id is not null""")
    con.sql("""
    create or replace table ivl as
    with e as (
      select i.item_id, e.seq, greatest(0, i.total_cents + sum(e.eff) over w) bal,
             lead(e.seq) over (partition by e.item_id order by e.seq) nxt,
             row_number() over (partition by e.item_id order by e.seq) rn
      from ev e join items i using (item_id)
      window w as (partition by e.item_id order by e.seq rows between unbounded preceding and current row)),
    first_ev as (select item_id, min(seq) s from ev group by 1)
    select i.item_id, -1::bigint from_seq, coalesce(f.s, 9223372036854775807) to_seq, i.total_cents open_cents
    from items i left join first_ev f using (item_id)
    union all
    select item_id, seq, coalesce(nxt, 9223372036854775807), bal from e""")
    # sólo intervalos que tocan la ventana y con saldo > 0
    s0, s1 = con.sql(f"select min(seq), max(seq) from bt where booking_date between '{W0}' and '{W1}'").fetchall()[0]
    con.sql(f"""
    create or replace table ivl_w as
    select v.*, i.kind, i.party_id, i.entity, i.currency, i.issue_date, i.due_date, i.doc_c, i.total_cents
    from ivl v join items i using (item_id)
    where v.to_seq >= {s0} and v.from_seq < {s1} and v.open_cents > 0 and i.issue_date <= '{W1}'""")

    con.sql(f"""
    create or replace table gw as
    with ob as (
      select item_id, seq, greatest(0, i.total_cents + coalesce(sum(eff) over (partition by item_id order by seq
             rows between unbounded preceding and 1 preceding), 0)) open_before_cents
      from ev join items i using (item_id))
    select g.*, ob.open_before_cents
    from g left join ob using (item_id, seq)
    where g.booking_date between '{W0}' and '{W1}'""")

    # tokens del texto: separadores no alfanuméricos salvo - / . ; también versión con ' | ' (corte N43) pegado
    con.sql(f"""
    create or replace table tok as
    with src as (
      select bank_txn_id, txt from bt where booking_date between '{W0}' and '{W1}'
      union all
      select bank_txn_id, replace(txt, ' | ', '') from bt where booking_date between '{W0}' and '{W1}' and txt like '% | %'),
    t as (select bank_txn_id, unnest(regexp_split_to_array(txt, '[^A-Z0-9/\\-\\.]+')) raw from src)
    select distinct bank_txn_id, regexp_replace(raw, '[^A-Z0-9]', '', 'g') tok from t
    where regexp_matches(raw, '[0-9]') and length(regexp_replace(raw, '[^A-Z0-9]', '', 'g')) >= 3""")

    con.sql(f"create or replace table bt_w as select * from bt where booking_date between '{W0}' and '{W1}'")
    con.sql("""create or replace table items_w as select * from items
               where item_id in (select item_id from ivl_w union select item_id from gw)""")
    for tb in ["bt_w", "items_w", "g", "ivl_w", "gw", "tok"]:
        con.sql(f"copy {tb} to '{OUT}/{tb}.parquet' (format parquet, compression zstd)")
        print(tb, con.sql(f"select count(*) from {tb}").fetchall()[0][0])
    print("pares ventana con open_before=0:", con.sql(
        "select count(*) filter (where open_before_cents=0), count(*) from gw where item_id is not null").fetchall()[0])
    print(f"ok {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
