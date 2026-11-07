"""Build work/<phase>/lines.parquet and items.parquet (contracts §1, §2) with duckdb, columnar.

    python -m src.base_parquet --phase window      # TRAIN 2024-07 → 2025-06, open balances from the golden before it
    python -m src.base_parquet --phase live
    python -m src.base_parquet --phase sample

Not filled yet (left null/empty, owned by T1): month_hint, doc_tokens is a simple token list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

import duckdb

from src.common import REPO, WINDOW_END, WINDOW_START, phase_dir, safe_stdout


def norm_sql(x: str) -> str:
    """contracts: upper, no accents, only [A-Z0-9 ], collapsed spaces."""
    return f"trim(regexp_replace(strip_accents(upper(coalesce({x}, ''))), '[^A-Z0-9]+', ' ', 'g'))"


def csv(path: str) -> str:
    p = path.replace(os.sep, "/")
    return "read_csv('" + p + "', all_varchar=true, header=true, quote='\"', escape='\"', delim=',')"


def input_signature(phase: str, data: str | None, pdir: str) -> str:
    """Return a cache key from phase and all input file metadata.

    Metadata is deliberately included for every CSV, including masters and the
    pre-window golden.  A changed file size or mtime invalidates the cache,
    while avoiding another full pass over multi-million-row CSVs.
    """
    roots = [phase_dir(x, data) for x in {"sample": ["sample"], "window": ["train"],
             "train": ["train"], "live": ["train", "live"],
             "final": ["train", "live", "final"]}[phase]]
    roots += [os.path.join(pdir, "..", "masters"), pdir]
    files = []
    for root in roots:
        if os.path.isdir(root):
            for base, _, names in os.walk(root):
                for name in sorted(names):
                    path = os.path.join(base, name)
                    if name.endswith((".csv", ".csv.gz")):
                        st = os.stat(path)
                        files.append((os.path.relpath(path, REPO), st.st_size, st.st_mtime_ns))
    payload = json.dumps({"phase": phase, "window": [str(WINDOW_START), str(WINDOW_END)],
                         "files": sorted(set(files))}, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def cached_result(out: str, key: str) -> dict | None:
    manifest = os.path.join(out, ".base_parquet_manifest.json")
    if not all(os.path.exists(os.path.join(out, name)) for name in ("lines.parquet", "items.parquet", manifest)):
        return None
    try:
        with open(manifest, encoding="utf-8") as fh:
            if json.load(fh).get("signature") != key:
                return None
        con = duckdb.connect()
        try:
            lines = con.sql(f"select count(*), count(distinct bank_txn_id) from read_parquet('{out}/lines.parquet')").fetchall()[0]
            items = con.sql(f"select count(*), count(distinct item_id), sum((open_at_start_cents > 0)::int) from read_parquet('{out}/items.parquet')").fetchall()[0]
        finally:
            con.close()
        print("[base_parquet] cache hit: reusing signature-matched parquet", flush=True)
        return {"lines": lines, "items": items}
    except (OSError, duckdb.Error, ValueError):
        return None


def build(phase: str, data: str | None = None) -> dict:
    out = os.path.join(REPO, "work", phase)
    os.makedirs(out, exist_ok=True)
    pdir = phase_dir(phase, data)
    signature = input_signature(phase, data, pdir)
    hit = cached_result(out, signature)
    if hit is not None:
        return hit
    con = duckdb.connect()
    con.sql("set preserve_insertion_order=false")
    print(f"[base_parquet] building {phase}; input signature {signature[:12]}", flush=True)

    # ---------------------------------------------------------------- lines (§1)
    where = (f"where booking_date between '{WINDOW_START}' and '{WINDOW_END}'" if phase == "window" else "")
    # Materialize once: the window balance query also needs the bank rows and
    # DuckDB otherwise reparses this large CSV a second time.
    con.sql(f"create table bt_raw as select * from {csv(os.path.join(pdir, 'bank_transactions.csv.gz'))}")
    print("[base_parquet] loaded bank transactions", flush=True)
    con.sql(f"""
    create table lines as
    with b as (
      select *, concat_ws(' ', "text", ref1, ref2, remittance_info, counterparty_name, end_to_end_id) as raw_all
      from bt_raw {where}
    )
    select
      bank_txn_id, account_id, entity, bank, statement_format,
      booking_date::date as booking_date, value_date::date as value_date,
      round(amount::double * 100)::bigint as amount_cents, currency,
      case when orig_amount is not null and orig_amount <> '' then round(orig_amount::double * 100)::bigint end as orig_amount_cents,
      nullif(orig_currency, '') as orig_currency,
      case when amount::double >= 0 then 'IN' else 'OUT' end as direction,
      bank_code, bank_subcode,
      {norm_sql('raw_all')} as text_norm,
      nullif({norm_sql('counterparty_name')}, '') as cp_name_norm,
      nullif({norm_sql('end_to_end_id')}, '') as e2e_norm,
      list_distinct(list_concat(
        case when counterparty_account is not null and counterparty_account <> '' then [upper(replace(counterparty_account, ' ', ''))] else [] end,
        regexp_extract_all(upper(raw_all), '\\b[A-Z]{{2}}[0-9]{{2}}[A-Z0-9]{{11,30}}\\b'))) as ibans,
      list_distinct(regexp_extract_all(upper(raw_all), 'VTM-[A-Z]{{2}}-[0-9]{{7}}')) as mandate_ids,
      list_distinct(regexp_extract_all(upper(raw_all), '\\b(?:[0-9]{{9,11}}|RCH[0-9]+|PR[0-9]+|CF[0-9]+)\\b')) as batch_refs,
      list_distinct(list_filter(string_split({norm_sql('raw_all')}, ' '), lambda t: length(t) >= 4 and regexp_matches(t, '[0-9]'))) as doc_tokens,
      null::varchar as month_hint,
      regexp_matches({norm_sql('raw_all')}, 'DEVOL|IMPAGAD|RUECKLAST|RETOUR|REJET|RETURN|CHARGEBACK|RECHAZ|UNPAID|RETURNED') as is_return_hint
    from b
    """)
    con.sql(f"copy lines to '{os.path.join(out, 'lines.parquet')}' (format parquet)")
    n_lines = con.sql("select count(*), count(distinct bank_txn_id) from lines").fetchall()[0]
    print(f"[base_parquet] wrote lines: {n_lines[0]:,}", flush=True)

    # ---------------------------------------------------------------- items (§2)
    sources = {"sample": ["sample"], "window": ["train"], "train": ["train"], "live": ["train", "live"], "final": ["train", "live", "final"]}[phase]
    parts = []
    for i, src in enumerate(sources):
        d = phase_dir(src, data)
        for name, sel in (
            ("ar_invoices", "item_id, 'ARI' kind, 'AR' side, entity, customer_id party_id, document_number doc_number, customer_po po, issue_date, due_date, currency, total_amount, payment_terms, payment_method, null related_doc"),
            ("ap_invoices", "item_id, 'API' kind, 'AP' side, entity, supplier_id party_id, supplier_invoice_number doc_number, our_po po, issue_date, due_date, currency, total_amount, payment_terms, payment_method, null related_doc"),
            ("credit_notes", "item_id, case when side = 'AR' then 'ARC' else 'APC' end kind, side, entity, party_id, document_number doc_number, null po, issue_date, null due_date, currency, total_amount, null payment_terms, null payment_method, related_document related_doc"),
        ):
            p = os.path.join(d, f"{name}.csv.gz")
            if os.path.exists(p):
                parts.append(f"select {sel}, {i} src_rank from {csv(p)}")
    oi = os.path.join(pdir, "open_items_at_start.csv.gz") if phase in ("live", "final") else None
    con.sql("create table items_raw as " + " union all by name ".join(parts))
    con.sql("""create table items0 as select * exclude (src_rank) from items_raw
               qualify row_number() over (partition by item_id order by src_rank desc) = 1""")
    if oi and os.path.exists(oi):
        # LIVE/FINAL: the phase sees items open at start (their open amount) + items issued in the phase
        con.sql(f"""create table opened as select item_id, round(open_amount::double * 100)::bigint open_c,
                    item_type, entity, party_id, document_number, issue_date, due_date, currency, total_amount from {csv(oi)}""")
        cur = sources[-1]
        cur_items = " union all ".join(
            f"select item_id from {csv(os.path.join(phase_dir(cur, data), n + '.csv.gz'))}" for n in ("ar_invoices", "ap_invoices", "credit_notes")
            if os.path.exists(os.path.join(phase_dir(cur, data), n + ".csv.gz")))
        con.sql(f"create table cur_ids as {cur_items}")
        con.sql("""create table items1 as
          select i.*, coalesce(o.open_c, round(i.total_amount::double * 100)::bigint) open_c from items0 i
          left join opened o using (item_id)
          where o.item_id is not null or i.item_id in (select item_id from cur_ids)
          union all by name
          select o.item_id, left(o.item_type, 3) kind, case when o.item_type like 'AR%' then 'AR' else 'AP' end side, o.entity, o.party_id,
                 o.document_number doc_number, null po, o.issue_date, o.due_date, o.currency, o.total_amount, null payment_terms,
                 null payment_method, null related_doc, o.open_c
          from opened o where o.item_id not in (select item_id from items0)""")
    elif phase == "window":
        # open at window start = total − golden allocated before the window (RETURNED re-opens); full-clearing → 0
        g = os.path.join(phase_dir("train", data), "golden_allocations.csv.gz")
        con.sql(f"""create table before as
          select g.item_id,
                 sum(case when g.exception = 'RETURNED' then -g.amount::double else g.amount::double end) paid,
                 bool_or(g.exception in ('SHORT_DISCOUNT','SHORT_BANK_CHARGES','SHORT_WITHHOLDING','FX_DIFFERENCE','OVERPAYMENT')) full_clear,
                 max(b.booking_date) last_date
          from {csv(g)} g join bt_raw b using (bank_txn_id)
          where g.item_id is not null and g.item_id <> '' and b.booking_date < '{WINDOW_START}' group by 1""")
        con.sql(f"""create table items1 as
          select i.*, case when b.item_id is null then round(i.total_amount::double * 100)::bigint
                           when b.full_clear and b.paid > 0 then 0
                           else greatest(0, least(round(i.total_amount::double * 100), round((i.total_amount::double - b.paid) * 100)))::bigint end open_c
          from items0 i left join before b using (item_id) where i.issue_date <= '{WINDOW_END}'""")
    else:
        con.sql("create table items1 as select *, round(total_amount::double * 100)::bigint open_c from items0")

    pt = os.path.join(phase_dir("sample", data), "..", "masters", "payment_terms.csv")
    con.sql(f"""
    create table items as
    select i.item_id, i.kind, i.side, i.entity, i.party_id,
           c.group_id, c.parent_customer_id as parent_party_id,
           i.doc_number, {norm_sql('i.doc_number')} as doc_norm,
           nullif(ltrim(regexp_replace(coalesce(i.doc_number, ''), '[^0-9]', '', 'g'), '0'), '') as doc_digits,
           nullif({norm_sql('i.po')}, '') as po_norm,
           i.issue_date::date issue_date, try_cast(i.due_date as date) due_date, i.currency,
           round(i.total_amount::double * 100)::bigint as total_cents, i.open_c as open_at_start_cents,
           i.payment_terms, t.early_payment_discount_pct::double discount_pct, t.discount_days::int discount_days,
           i.payment_method, nullif(i.related_doc, '') related_doc
    from items1 i
    left join {csv(os.path.join(os.path.dirname(pt), 'customers.csv'))} c on c.customer_id = i.party_id
    left join {csv(pt)} t on t.code = i.payment_terms
    """)
    con.sql(f"copy items to '{os.path.join(out, 'items.parquet')}' (format parquet)")
    n_items = con.sql("select count(*), count(distinct item_id), sum((open_at_start_cents > 0)::int) from items").fetchall()[0]
    with open(os.path.join(out, ".base_parquet_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"signature": signature, "phase": phase}, fh)
    print(f"[base_parquet] wrote items: {n_items[0]:,}", flush=True)
    con.close()
    return {"lines": n_lines, "items": n_items}


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data")
    a = ap.parse_args(argv)
    t0 = time.time()
    r = build(a.phase, a.data)
    print(f"[{a.phase}] lines {r['lines'][0]} ({r['lines'][1]} distinct) · items {r['items'][0]} ({r['items'][1]} distinct, "
          f"{r['items'][2]} open) -> work/{a.phase}/  ({time.time() - t0:.0f}s)")
    return 0 if r["lines"][0] == r["lines"][1] and r["items"][0] == r["items"][1] else 1


if __name__ == "__main__":
    sys.exit(main())
