"""T3 análisis (sólo lectura): ¿cómo se habría podido encontrar cada par del golden en la ventana 2024-07 → 2025-06?

Cascada por par: a) ficheros de detalle → b) aviso de pago → c) nº de documento en el texto; el resto se clasifica por
línea: d) única partida abierta del tercero con importe exacto · e) importe exacto con varias iguales (FIFO / mes) ·
f) combinación de varias partidas · g) importe ajustado por política · h) resto.
El tercero se toma del golden (mock de T4): mide el techo alcanzable con un tercero bien identificado.

Requiere t3_prep.py, t3_side.py y t3_remit.py. Escribe work/analysis/t3_*.md (tablas) y work/analysis/t3_pairs.parquet.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from t3_prep import OUT, compact, connect, load  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MONTHS = {
    1: "ENERO JANUARY JANVIER JANUAR GENNAIO JANEIRO JANUARI STYCZEN",
    2: "FEBRERO FEBRUARY FEVRIER FEBRUAR FEBBRAIO FEVEREIRO FEBRUARI LUTY",
    3: "MARZO MARCH MARS MAERZ MARZ MARCO MAART MARZEC",
    4: "ABRIL APRIL AVRIL APRILE KWIECIEN",
    5: "MAYO MAY MAI MAGGIO MAIO MEI MAJ",
    6: "JUNIO JUNE JUIN JUNI GIUGNO JUNHO CZERWIEC",
    7: "JULIO JULY JUILLET JULI LUGLIO JULHO LIPIEC",
    8: "AGOSTO AUGUST AOUT AUGUSTUS SIERPIEN",
    9: "SEPTIEMBRE SETIEMBRE SEPTEMBER SEPTEMBRE SETTEMBRE SETEMBRO WRZESIEN",
    10: "OCTUBRE OCTOBER OCTOBRE OKTOBER OTTOBRE OUTUBRO PAZDZIERNIK",
    11: "NOVIEMBRE NOVEMBER NOVEMBRE NOVEMBRO LISTOPAD",
    12: "DICIEMBRE DECEMBER DECEMBRE DEZEMBER DICEMBRE DEZEMBRO GRUDZIEN",
}
FULL_CLEAR = "('SHORT_DISCOUNT','SHORT_WITHHOLDING','FX_DIFFERENCE','SHORT_BANK_CHARGES','OVERPAYMENT')"
STAGES = ["a", "b", "c", "d", "e", "f", "g", "h"]


def md(con, sql, path=None):
    df = con.sql(sql).df()
    s = df.to_markdown(index=False, floatfmt=".1f")
    if path:
        with open(path, "a") as f:
            f.write(s + "\n\n")
    return s


def main():
    t = time.time()
    con = connect()
    load(con, ["bt_w", "items_w", "gw", "ivl_w", "tok"])
    con.sql(f"create or replace view side_pairs as select * from '{OUT}/side_pairs.parquet'")
    con.sql(f"create or replace view remit_tok as select * from '{OUT}/remit_tok.parquet'")
    rep = OUT / ("t3_report.md" if not os.environ.get("T3_SAMPLE") else "t3_report_sample.md")
    rep.write_text("")

    def section(title):
        with open(rep, "a") as f:
            f.write(f"## {title}\n\n")

    print(f"{time.time() - t:6.0f}s  pares base", flush=True)
    # ---------------- pares base
    n = int(os.environ.get("T3_SAMPLE", "0"))  # T3_SAMPLE=50 → 1 de cada 50 líneas (prueba rápida)
    SAMPLE = f"and hash(g.bank_txn_id) % {n} = 0" if n else ""
    con.sql(f"""
    create temp table P as
    select g.bank_txn_id, g.item_id, g.amt_cents, g.exception, g.segment, g.seq, g.booking_date, g.open_before_cents,
           i.kind, i.party_id, i.entity item_entity, i.doc_c, {compact('i.po')} po_c, i.currency item_ccy,
           i.total_cents, i.issue_date, i.due_date, i.payment_terms,
           b.amt_cents line_cents, abs(b.amt_cents) abs_cents, b.currency line_ccy, b.entity line_entity, b.account_id,
           count(*) over (partition by g.bank_txn_id) n_pairs
    from gw g join items_w i using (item_id) join bt_w b using (bank_txn_id)
    where g.item_id is not null {SAMPLE}""")
    con.sql("""
    alter table P add column core varchar; alter table P add column core_nz varchar;
    alter table P add column lastg varchar; alter table P add column lastg_nz varchar;
    update P set core = regexp_replace(doc_c, '^[A-Z]+', ''), lastg = regexp_extract(doc_c, '([0-9]+)$', 1);
    update P set core_nz = ltrim(core, '0'), lastg_nz = ltrim(lastg, '0')""")

    print(f"{time.time() - t:6.0f}s  a) ficheros de detalle", flush=True)
    # ---------------- a) ficheros de detalle
    con.sql("""create temp table A as select p.bank_txn_id, p.item_id, s.src a_src, s.link a_link
               from P p join side_pairs s using (bank_txn_id, item_id)""")

    print(f"{time.time() - t:6.0f}s  b) avisos de pago (mismo entity, recibid", flush=True)
    # ---------------- b) avisos de pago (mismo entity, recibido entre -10 y +30 días de la fecha de cargo)
    con.sql("""
    create temp table Bc as
    with k as (select bank_txn_id, item_id, item_entity, booking_date, doc_c key from P where kind in ('ARI','ARC')
               union select bank_txn_id, item_id, item_entity, booking_date, core from P
               where kind in ('ARI','ARC') and length(core) >= 5)
    select distinct k.bank_txn_id, k.item_id, r.file
    from k join remit_tok r on r.entity = k.item_entity and r.tok = k.key
    where r.received_date between k.booking_date - 10 and k.booking_date + 30""")
    # enlace aviso ↔ línea por una referencia común (token ≥ 6 del aviso presente en el texto bancario)
    con.sql("""
    create temp table Blink as
    select distinct t.bank_txn_id, r.file from tok t join remit_tok r on r.tok = t.tok and length(t.tok) >= 6
    where (t.bank_txn_id, r.file) in (select bank_txn_id, file from Bc)""")
    con.sql("""
    create temp table B as
    select c.bank_txn_id, c.item_id, bool_or(l.file is not null) b_ref
    from Bc c left join Blink l using (bank_txn_id, file) group by 1, 2""")

    print(f"{time.time() - t:6.0f}s  c) nº de documento en el texto", flush=True)
    # ---------------- c) nº de documento en el texto
    con.sql("""
    create temp table Csfx as
    select bank_txn_id, item_id, right(lastg, k) key, k from P, range(4, 13) r(k) where length(lastg) > k""")
    con.sql("""
    create temp table Cpfx as
    select bank_txn_id, item_id, left(doc_c, k) key from P, range(6, 20) r(k)
    where length(doc_c) > k and k >= length(doc_c) - 4""")
    con.sql("""
    create temp table Chits as
    select p.bank_txn_id, p.item_id, 1 lvl, 'c1_exact' v, t.tok from P p join tok t on t.bank_txn_id = p.bank_txn_id and t.tok = p.doc_c
    union all
    select p.bank_txn_id, p.item_id, 2, 'c2_sin_prefijo_o_ceros', t.tok from P p join tok t on t.bank_txn_id = p.bank_txn_id
      and length(t.tok) >= 4 and t.tok in (p.core, p.core_nz, p.lastg, p.lastg_nz) and t.tok <> p.doc_c
    union all
    select p.bank_txn_id, p.item_id, 3, 'c3_po_cliente', t.tok from P p join tok t on t.bank_txn_id = p.bank_txn_id
      and length(p.po_c) >= 4 and t.tok = p.po_c
    union all
    select s.bank_txn_id, s.item_id, case when s.k >= 6 then 4 else 7 end,
           case when s.k >= 6 then 'c4_sufijo_6+' else 'c7_sufijo_4_5' end, t.tok
    from Csfx s join tok t on t.bank_txn_id = s.bank_txn_id and t.tok = s.key
    union all
    select s.bank_txn_id, s.item_id, 5, 'c5_truncado', t.tok from Cpfx s join tok t on t.bank_txn_id = s.bank_txn_id and t.tok = s.key""")
    # a 1 edición (Damerau) sólo para los pares aún sin acierto
    con.sql("""
    insert into Chits
    select p.bank_txn_id, p.item_id, 6, 'c6_1_edicion', t.tok
    from (select * from P where (bank_txn_id, item_id) not in (select bank_txn_id, item_id from Chits) and length(doc_c) >= 6) p
    join tok t on t.bank_txn_id = p.bank_txn_id and abs(length(t.tok) - length(p.doc_c)) <= 1
    where damerau_levenshtein(t.tok, p.doc_c) = 1
       or (length(p.core) >= 6 and abs(length(t.tok) - length(p.core)) <= 1 and damerau_levenshtein(t.tok, p.core) = 1)""")
    # factura anulada y sustituida: el texto cita el nº de otra factura del mismo tercero anulada por un abono
    con.sql("""
    create temp table annulled as
    select x.item_id, x.party_id, x.doc_c, regexp_replace(x.doc_c, '^[A-Z]+', '') core
    from items_w x join items_w cn on cn.kind = 'ARC' and cn.entity = x.entity
      and regexp_replace(upper(strip_accents(cn.related_doc)), '[^A-Z0-9]', '', 'g') = x.doc_c
      and upper(strip_accents(cn.reason)) like 'ANULACION%'
    where x.kind = 'ARI'""")
    con.sql("""
    insert into Chits
    select p.bank_txn_id, p.item_id, 8, 'c8_factura_sustituida', t.tok
    from P p join (select item_id, party_id, doc_c key from annulled
                   union select item_id, party_id, core from annulled where length(core) >= 5) x
      on x.party_id = p.party_id and x.item_id <> p.item_id
    join tok t on t.bank_txn_id = p.bank_txn_id and t.tok = x.key
    where (p.bank_txn_id, p.item_id) not in (select bank_txn_id, item_id from Chits)""")
    con.sql("""create temp table C as select bank_txn_id, item_id, arg_min(v, lvl) c_var, arg_min(tok, lvl) c_tok
               from Chits group by 1, 2""")

    # ambigüedad de c: nº de partidas abiertas del mismo tercero (al cargar la línea) que encajan con el mismo token
    con.sql("""
    create temp table Ckeys as
    with toks as (select distinct c_tok from C)
    select v.item_id, v.party_id, v.from_seq, v.to_seq, k.key
    from ivl_w v, lateral (select unnest([v.doc_c, regexp_replace(v.doc_c, '^[A-Z]+', ''),
                                          ltrim(regexp_replace(v.doc_c, '^[A-Z]+', ''), '0'),
                                          regexp_extract(v.doc_c, '([0-9]+)$', 1)]
                                         || list_transform(range(4, 13), lambda k: right(regexp_extract(v.doc_c, '([0-9]+)$', 1), k))
                                         || list_transform(range(6, 20), lambda k: left(v.doc_c, k))) key) k
    where k.key in (select c_tok from toks)""")
    con.sql("""
    create temp table Camb as
    select c.bank_txn_id, c.item_id, count(distinct k.item_id) n_amb
    from C c join P p using (bank_txn_id, item_id)
    join Ckeys k on k.key = c.c_tok and k.party_id = p.party_id and k.from_seq < p.seq and p.seq <= k.to_seq
    group by 1, 2""")

    print(f"{time.time() - t:6.0f}s  d/e) una partida: candidatos abiertos de", flush=True)
    # ---------------- d/e) una partida: candidatos abiertos del tercero con importe exacto
    con.sql("""
    create temp table L1 as
    select * from P where n_pairs = 1""")
    con.sql("""
    create temp table cand1 as
    select l.bank_txn_id, v.item_id cand, v.issue_date, v.doc_c
    from L1 l join ivl_w v on v.party_id = l.party_id and v.currency = l.line_ccy and v.open_cents = l.abs_cents
      and v.from_seq < l.seq and l.seq <= v.to_seq and v.issue_date <= l.booking_date
      and ((l.line_cents > 0 and v.kind in ('ARI','APC')) or (l.line_cents < 0 and v.kind in ('API','ARC')))""")
    month_cases = " ".join(f"when mw in ({', '.join(repr(w) for w in ws.split())}) then {m}" for m, ws in MONTHS.items())
    alts = "|".join(w for ws in MONTHS.values() for w in ws.split())
    con.sql(f"""
    create temp table hint as
    with x as (select bank_txn_id, regexp_extract(txt, '(?:^|[^A-Z])({alts})(?:[^A-Z0-9]{{1,3}}([0-9]{{4}}|[0-9]{{2}}))?(?:[^0-9A-Z]|$)',
                      ['mw', 'yr']) r from bt_w where bank_txn_id in (select bank_txn_id from L1))
    select bank_txn_id, case {month_cases} end m,
           case when length(yr) = 4 then yr::int when length(yr) = 2 then 2000 + yr::int end y
    from (select bank_txn_id, r.mw mw, nullif(r.yr, '') yr from x) where mw <> ''""")
    con.sql("""
    create temp table DE as
    with c0 as (select *, min(issue_date) over (partition by bank_txn_id) min_issue from cand1),
    c as (
      select l.bank_txn_id, l.item_id gold, count(c.cand) n_cand, coalesce(bool_or(c.cand = l.item_id), false) gold_in,
             arg_min(c.cand, (c.issue_date, c.doc_c)) fifo_pick,
             count(*) filter (where c.issue_date = c.min_issue) n_oldest
      from L1 l left join c0 c using (bank_txn_id) group by 1, 2),
    m as (
      select c.bank_txn_id, count(*) n_m, any_value(c.cand) m_pick
      from cand1 c join hint h using (bank_txn_id)
      join items_w i on i.item_id = c.cand
      where month(i.issue_date - interval 1 month) = h.m and (h.y is null or year(i.issue_date - interval 1 month) = h.y)
      group by 1)
    select c.*, h.m is not null has_month, m.n_m, m.m_pick from c left join hint h using (bank_txn_id) left join m using (bank_txn_id)""")

    print(f"{time.time() - t:6.0f}s  f/g) composición de la línea", flush=True)
    # ---------------- f/g) composición de la línea
    con.sql(f"""
    create temp table LN as
    select bank_txn_id, any_value(segment) segment, count(*) n_pairs, any_value(abs_cents) abs_cents,
           any_value(line_ccy) line_ccy, any_value(booking_date) booking_date, any_value(seq) seq, any_value(line_cents) line_cents,
           bool_or(kind in ('ARC','APC')) has_cn, bool_and(item_ccy = line_ccy) same_ccy,
           count(distinct party_id) n_party, list(distinct party_id) parties,
           coalesce(list(distinct exception) filter (where exception is not null), []::varchar[]) excs,
           sum(case when kind in ('ARC','APC') then -amt_cents else amt_cents end) signed_sum,
           sum(case when exception = 'SHORT_DISCOUNT' then round(total_cents * t.early_payment_discount_pct / 100) else 0 end) disc_cents,
           sum(case when exception = 'SHORT_WITHHOLDING' then total_cents else 0 end) wh_base,
           any_value(item_ccy) item_ccy
    from P left join 'data_cache/masters/payment_terms.parquet' t on t.code = P.payment_terms
    group by 1""")
    # FX: tipo de referencia (último disponible ≤ fecha de cargo), unidades por EUR
    con.sql("""
    create temp table fx as select date::date d, currency, units_per_eur upe from 'data_cache/masters/fx_rates.parquet'
    union all select distinct date::date, 'EUR', 1.0 from 'data_cache/masters/fx_rates.parquet'""")
    con.sql("""
    create temp table LNfx as
    select l.bank_txn_id, l.signed_sum / fi.upe * fl.upe conv_cents
    from LN l asof join fx fi on fi.currency = l.item_ccy and fi.d <= l.booking_date
              asof join fx fl on fl.currency = l.line_ccy and fl.d <= l.booking_date
    where not l.same_ccy""")
    # todas las partidas abiertas del/los tercero(s) en ese momento (búsqueda de combinación)
    con.sql("""
    create temp table popen as
    with lp as (select distinct p.bank_txn_id, p.party_id, l.seq, l.booking_date, l.item_ccy
                from P p join LN l using (bank_txn_id) where l.n_pairs >= 2)
    select lp.bank_txn_id, v.item_id, v.kind, v.issue_date,
           row_number() over (partition by lp.bank_txn_id, v.kind in ('ARC','APC') order by v.issue_date, v.doc_c) rk
    from lp join ivl_w v on v.party_id = lp.party_id and v.currency = lp.item_ccy
    where v.from_seq < lp.seq and lp.seq <= v.to_seq and v.issue_date <= lp.booking_date""")
    con.sql("""
    create temp table F as
    with o as (select o.*, p.item_id is not null gold from popen o left join P p using (bank_txn_id, item_id))
    select l.bank_txn_id, count(o.item_id) n_open,
           count(*) filter (where o.gold) n_gold_open,
           max(o.rk) filter (where o.kind in ('ARI','API') and o.gold) max_rk_inv,
           count(*) filter (where o.kind in ('ARI','API') and o.gold) n_gold_inv
    from LN l left join o using (bank_txn_id) where l.n_pairs >= 2 group by 1""")

    con.sql(f"""
    create temp table LC as
    select l.*, f.n_open, f.n_gold_open, f.max_rk_inv, f.n_gold_inv, x.conv_cents,
      l.abs_cents - l.signed_sum resid,
      case
        when len(list_filter(l.excs, lambda e: e not in {FULL_CLEAR})) > 0 then null
        when list_contains(l.excs, 'FX_DIFFERENCE') or not l.same_ccy then 'g3_fx'
        when list_contains(l.excs, 'SHORT_WITHHOLDING') then 'g2_retencion'
        when list_contains(l.excs, 'SHORT_DISCOUNT') then 'g1_descuento'
        when list_contains(l.excs, 'SHORT_BANK_CHARGES') then 'g4_comisiones'
        when list_contains(l.excs, 'OVERPAYMENT') then 'g5_sobrepago'
      end g_kind
    from LN l left join F f using (bank_txn_id) left join LNfx x using (bank_txn_id)""")
    con.sql("""
    alter table LC add column g_ok boolean;
    update LC set g_ok = case g_kind
      when 'g1_descuento' then abs(resid + disc_cents) <= n_pairs
      when 'g2_retencion' then list_bool_or(list_transform([0.0125, 0.015, 0.10], lambda r: abs(resid + round(wh_base * r)) <= n_pairs + 1))
      when 'g3_fx' then conv_cents is not null and abs(abs_cents / conv_cents - 1) <= 0.03
      when 'g4_comisiones' then resid < 0 and -resid <= least(0.02 * signed_sum, 6000 * 1.25)
      when 'g5_sobrepago' then resid > 0 end""")

    # clase de línea para lo que no resuelven a–c
    con.sql("""
    create temp table LK as
    select l.bank_txn_id,
      case
        when l.n_pairs = 1 and d.n_cand = 1 and d.gold_in then 'd'
        when l.n_pairs = 1 and d.n_cand >= 2 and d.gold_in then 'e'
        when l.n_pairs >= 2 and l.same_ccy and len(l.excs) = 0 and l.signed_sum = l.abs_cents then 'f'
        when l.g_kind is not null and l.g_ok then 'g'
        else 'h' end k,
      case
        when l.n_pairs = 1 and d.n_cand >= 2 and d.gold_in then
          case when d.fifo_pick = l_gold and d.n_oldest = 1 then 'e1_fifo' else 'e3_no_fifo' end
        else null end e_kind
    from LC l left join (select *, gold l_gold from DE) d using (bank_txn_id)""")

    print(f"{time.time() - t:6.0f}s  cascada por par y por línea", flush=True)
    # ---------------- cascada por par y por línea
    con.sql("""
    create temp table PS as
    select p.bank_txn_id, p.item_id, p.segment, p.kind, p.exception, p.n_pairs,
           a.a_src, b.b_ref, c.c_var, ca.n_amb, k.k,
           case when a.a_src is not null then 'a' when b.item_id is not null then 'b' when c.c_var is not null then 'c' else k.k end stage
    from P p left join A a using (bank_txn_id, item_id) left join B b using (bank_txn_id, item_id)
    left join C c using (bank_txn_id, item_id) left join Camb ca using (bank_txn_id, item_id)
    left join LK k using (bank_txn_id)""")
    con.sql(f"copy PS to '{OUT}/t3_pairs.parquet' (format parquet)")
    con.sql("""
    create temp table LS as
    select bank_txn_id, any_value(segment) segment, count(*) n_pairs, any_value(k) k,
      case when bool_and(stage = 'a') then 'a'
           when bool_and(stage in ('a','b')) then 'b'
           when bool_and(stage in ('a','b','c')) then 'c'
           else any_value(k) end stage,
      bool_or(stage in ('a','b','c')) and not bool_and(stage in ('a','b','c')) partial_ref
    from PS group by 1""")

    n_lines_all = con.sql("select count(*) from bt_w").fetchall()[0][0]
    n_pairs = con.sql("select count(*) from PS").fetchall()[0][0]
    n_lines_items = con.sql("select count(*) from LS").fetchall()[0][0]
    with open(rep, "a") as f:
        f.write(f"Ventana: {n_lines_all:,} líneas, {n_lines_items:,} con partida, {n_pairs:,} pares.\n\n")

    piv = ", ".join(f"round(100.0 * count(*) filter (where stage = '{s}') / count(*), 1) {s}" for s in STAGES)
    section("Cascada por segmento — % de pares")
    md(con, f"""select segment, count(*) pares, {piv} from PS group by 1
               union all select '**TOTAL**', count(*), {piv} from PS order by pares desc""", rep)
    section("Cascada por segmento — % de líneas (con partida)")
    md(con, f"""select segment, count(*) lineas, {piv}, round(100.0 * avg(partial_ref::int), 1) ref_parcial from LS group by 1
               union all select '**TOTAL**', count(*), {piv}, round(100.0 * avg(partial_ref::int), 1) from LS order by lineas desc""", rep)

    section("a) Ficheros de detalle: pares por fuente y enlace (y precisión frente al golden)")
    md(con, f"""
    with s as (select s.*, (s.bank_txn_id, s.item_id) in (select bank_txn_id, item_id from P) ok from side_pairs s)
    select src, link, count(*) pares_propuestos, count(*) filter (where ok) en_golden,
           round(100.0 * avg(ok::int), 2) pct_precision from s group by all order by pares_propuestos desc""", rep)

    section("b) Avisos: pares encontrados en un aviso, con/sin referencia común aviso↔texto")
    md(con, """select p.segment, count(*) pares, count(b.item_id) en_aviso, round(100.0 * count(b.item_id) / count(*), 1) pct,
               round(100.0 * count(*) filter (where b.b_ref) / count(*), 1) pct_con_ref
               from P p left join B b using (bank_txn_id, item_id) group by 1 having count(b.item_id) > 0 order by en_aviso desc""", rep)

    section("c) Nº de documento en el texto: variante (mejor acierto por par) y ambigüedad dentro del tercero")
    md(con, """select c_var, count(*) pares, round(100.0 * count(*) / sum(count(*)) over (), 1) pct,
               round(100.0 * avg((n_amb = 1)::int), 1) pct_unico_tercero, round(avg(n_amb), 2) cand_medio
               from PS where c_var is not null and stage = 'c' group by 1 order by 1""", rep)
    md(con, """select segment, count(*) pares, """ + ", ".join(
        f"round(100.0 * count(*) filter (where c_var = '{v}') / count(*), 1) \"{v[:2]}\""
        for v in ["c1_exact", "c2_sin_prefijo_o_ceros", "c3_po_cliente", "c4_sufijo_6+", "c5_truncado", "c6_1_edicion",
                  "c7_sufijo_4_5", "c8_factura_sustituida"]) +
       """ from PS where stage = 'c' group by 1 having count(*) > 500 order by pares desc""", rep)

    section("d/e) Líneas de una partida: candidatos con importe exacto del tercero (golden incluido)")
    md(con, """
    select case when n_cand = 0 then '0 (importe ≠ saldo)' when n_cand = 1 then '1' when n_cand <= 3 then '2-3'
                when n_cand <= 10 then '4-10' else '>10' end candidatos,
           count(*) lineas, count(*) filter (where gold_in) golden_dentro,
           count(*) filter (where gold_in and n_cand >= 2 and fifo_pick = gold and n_oldest = 1) fifo_ok,
           count(*) filter (where gold_in and n_cand >= 2 and has_month) con_mes,
           count(*) filter (where gold_in and n_cand >= 2 and n_m = 1 and m_pick = gold) mes_ok,
           count(*) filter (where gold_in and n_cand >= 2 and n_m = 1 and m_pick = gold and not (fifo_pick = gold and n_oldest = 1)) mes_ok_fifo_ko
    from DE group by 1 order by min(n_cand)""", rep)
    section("e) Sólo líneas en etapa e (tras a–c): FIFO / mes por segmento")
    md(con, """
    select l.segment, count(*) lineas,
      round(100.0 * avg((d.fifo_pick = d.gold and d.n_oldest = 1)::int), 1) pct_fifo,
      round(100.0 * avg((d.n_m = 1 and d.m_pick = d.gold)::int), 1) pct_mes,
      round(100.0 * avg(((d.fifo_pick = d.gold and d.n_oldest = 1) or (d.n_m = 1 and d.m_pick = d.gold))::int), 1) pct_mes_o_fifo,
      round(avg(d.n_cand), 1) cand_medio
    from LS l join DE d using (bank_txn_id) where l.stage = 'e' group by 1 order by lineas desc""", rep)

    section("f) Combinaciones (líneas ≥2 partidas sin excepción, suma exacta) que no resuelven a–c")
    md(con, """
    select case when n_pairs = 2 then '2' when n_pairs <= 5 then '3-5' when n_pairs <= 20 then '6-20' else '>20' end tam,
      count(*) lineas, sum(n_pairs) pares, round(100.0 * avg(has_cn::int), 1) pct_con_abono,
      round(100.0 * avg((n_gold_open = n_pairs and n_open = n_pairs)::int), 1) pct_todo_lo_abierto,
      round(100.0 * avg((max_rk_inv = n_gold_inv and n_gold_open = n_pairs)::int), 1) pct_fifo_prefijo,
      round(median(n_open), 0) med_abiertas, round(quantile_cont(n_open, 0.9), 0) p90_abiertas
    from LC join (select bank_txn_id, stage from LS) LS using (bank_txn_id) where LS.stage = 'f' group by 1 order by min(LC.n_pairs)""", rep)
    md(con, """
    select LC.segment, count(*) lineas, round(avg(LC.n_pairs), 1) tam_medio, round(100.0 * avg(has_cn::int), 1) pct_abono,
      round(100.0 * avg((n_gold_open = LC.n_pairs and n_open = LC.n_pairs)::int), 1) pct_todo_abierto,
      round(100.0 * avg((max_rk_inv = n_gold_inv and n_gold_open = LC.n_pairs)::int), 1) pct_fifo_prefijo,
      round(median(n_open), 0) med_abiertas
    from LC join (select bank_txn_id, stage from LS) LS using (bank_txn_id) where LS.stage = 'f' group by 1 order by lineas desc""", rep)

    section("g) Importe ajustado por política: reproducible con la regla de POLICIES (todas las líneas con esa excepción)")
    md(con, """
    select g_kind, count(*) lineas, sum(n_pairs) pares, round(100.0 * avg(g_ok::int), 1) pct_reproducible,
           round(100.0 * avg((n_pairs = 1)::int), 1) pct_1_partida
    from LC where g_kind is not null group by 1 order by 1""", rep)

    section("h) Resto (no encontrado por a–g): por excepción y por segmento")
    md(con, """select coalesce(exception, '(vacía)') exception, count(*) pares, count(distinct bank_txn_id) lineas
               from PS where stage = 'h' group by 1 order by pares desc""", rep)
    md(con, """select segment, count(*) pares, count(distinct bank_txn_id) lineas from PS where stage = 'h'
               group by 1 order by pares desc limit 25""", rep)

    print(f"{time.time() - t:6.0f}s  puntos esperados por etapa (50·F1 por pa", flush=True)
    # ---------------- puntos esperados por etapa (50·F1 por pares + 30·accuracy por líneas)
    section("Puntos por etapa (≈ 50 × % pares + 30 × % líneas de la ventana)")
    md(con, f"""
    with p as (select stage, count(*) n from PS group by 1), l as (select stage, count(*) n from LS group by 1)
    select stage, p.n pares, round(100.0 * p.n / {n_pairs}, 1) pct_pares, l.n lineas,
           round(100.0 * l.n / {n_lines_all}, 1) pct_lineas_total,
           round(50.0 * p.n / {n_pairs} + 30.0 * l.n / {n_lines_all}, 1) puntos
    from p full join l using (stage) order by puntos desc""", rep)
    section("Puntos por segmento × etapa (top 40)")
    md(con, f"""
    with p as (select segment, stage, count(*) n from PS group by 1, 2), l as (select segment, stage, count(*) n from LS group by 1, 2)
    select segment, stage, p.n pares, round(100.0 * p.n / {n_pairs}, 1) pct_pares, l.n lineas,
           round(50.0 * p.n / {n_pairs} + 30.0 * coalesce(l.n, 0) / {n_lines_all}, 2) puntos
    from p left join l using (segment, stage) order by puntos desc limit 40""", rep)

    print(f"{time.time() - t:6.0f}s  días factura → cobro por tercero", flush=True)
    # ---------------- días factura → cobro por tercero
    section("Días factura → cargo (facturas ARI/API, ventana)")
    con.sql("""
    create temp table DY as
    select p.kind, p.party_id, coalesce(c.customer_type, 'PROVEEDOR') ptype, p.segment,
           p.booking_date - p.issue_date d_issue, p.booking_date - p.due_date d_due, p.payment_terms
    from P p left join 'data_cache/masters/customers.parquet' c on c.customer_id = p.party_id
    where p.kind in ('ARI','API') and coalesce(p.exception, '') not in ('RETURNED','DUPLICATE_PAYMENT')""")
    md(con, """
    select kind, ptype, count(*) pares, count(distinct party_id) terceros,
      quantile_cont(d_issue, 0.1) p10, median(d_issue) p50, quantile_cont(d_issue, 0.9) p90,
      median(d_due) p50_vs_venc, round(100.0 * avg((d_issue < 0)::int), 2) pct_antes_emision
    from DY group by 1, 2 order by pares desc""", rep)
    md(con, """
    with t as (select kind, party_id, count(*) n, median(d_issue) med, quantile_cont(d_issue, 0.9) - quantile_cont(d_issue, 0.1) spread
               from DY group by 1, 2 having count(*) >= 5),
    w as (select d.kind, abs(d.d_issue - t.med) <= 15 near from DY d join t using (kind, party_id))
    select t.kind, count(*) terceros_5plus, quantile_cont(med, 0.1) med_p10, median(med) med_p50, quantile_cont(med, 0.9) med_p90,
           median(spread) dispersion_p10_p90_mediana, quantile_cont(spread, 0.9) dispersion_p90,
           (select round(100.0 * avg(near::int), 1) from w where w.kind = t.kind) pct_pares_a_15d_de_su_mediana
    from t group by 1""", rep)
    md(con, """
    select segment, count(*) pares, median(d_issue) p50, quantile_cont(d_issue, 0.9) p90, max(d_issue) max_d
    from DY group by 1 having count(*) > 3000 order by pares desc""", rep)

    print(f"{time.time() - t:6.0f}s  UNIDENTIFIED que se parecen a una partid", flush=True)
    # ---------------- UNIDENTIFIED que se parecen a una partida abierta
    section("UNIDENTIFIED con importe igual a alguna partida abierta (misma entidad y divisa, al cargar la línea)")
    con.sql("""
    create temp table U as
    select g.bank_txn_id, g.segment, b.seq, b.entity, b.currency, b.amt_cents, b.booking_date
    from gw g join bt_w b using (bank_txn_id) where g.category = 'UNIDENTIFIED'""")
    con.sql("""
    create temp table Um as
    select u.bank_txn_id, count(v.item_id) n_match, count(distinct v.party_id) n_party
    from U u left join ivl_w v on v.entity = u.entity and v.currency = u.currency and v.open_cents = u.amt_cents
      and v.from_seq < u.seq and u.seq <= v.to_seq and v.issue_date <= u.booking_date and v.kind in ('ARI','APC')
    group by 1""")
    md(con, """
    select u.segment, count(*) lineas, count(*) filter (where n_match >= 1) con_alguna,
           round(100.0 * count(*) filter (where n_match >= 1) / count(*), 1) pct,
           count(*) filter (where n_match = 1) exactamente_una, count(*) filter (where n_match >= 2) varias
    from U u join Um using (bank_txn_id) group by rollup(1) order by lineas""", rep)
    # comparación: cobros reales de 1 partida con match único por importe en la entidad (riesgo de casar sólo por importe)
    md(con, """
    with t as (select l.bank_txn_id, count(v.item_id) n
               from (select * from L1 where line_cents > 0 and kind = 'ARI' and coalesce(exception,'') = '') l
               join ivl_w v on v.entity = l.line_entity and v.currency = l.line_ccy and v.open_cents = l.abs_cents
                 and v.from_seq < l.seq and l.seq <= v.to_seq and v.issue_date <= l.booking_date and v.kind in ('ARI','APC')
               group by 1)
    select 'cobro real 1 partida' tipo, count(*) filter (where n = 1) match_unico_entidad, count(*) filter (where n >= 2) varios from t
    union all
    select 'UNIDENTIFIED', count(*) filter (where n_match = 1), count(*) filter (where n_match >= 2) from Um""", rep)

    print(rep.read_text()[:3000])
    print(f"ok {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
