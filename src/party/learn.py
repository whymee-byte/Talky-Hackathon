"""Learn party-identification tables from TRAIN history + masters + bank-account notices.

model 'pre_window': TRAIN lines booked before 2024-07-01 (no leakage into the evaluation window).
model 'full':       all TRAIN lines (used for LIVE / FINAL).

Outputs in work/party_model/<model>/:
  learned_keys.parquet   entity, L, key, party_id, n, total, purity     (name/text prefixes seen in history)
  master_keys.parquet    entity, L, key, party_id, n_parties, source    (current/trade/printed names)
  ibans.parquet          iban, party_id, n, purity, source              (history + masters + PDF notices)
  mandates.parquet       mandate_id, party_id, source
  parties.parquet        party_id, entity, kind (C/S), parent_party_id, group_id, name_norm
  entity_pairs.parquet   line_entity, party_entity, share              (e.g. payment factory ES00 -> ES01)
  type_prior.parquet     direction, kind, share
"""
import glob
import os
import re

import pypdf

from .common import KEY_LENGTHS, RAW, SHORT_MIN, WINDOW_START, connect, master, src

MAX_TOKENS = 20     # name keys start at one of the first 20 tokens of the text
MIN_SUPPORT = 2     # a learned key needs >= 2 lines
MIN_PURITY = 0.8    # ... and >= 80 % of them on the same party


def notice_ibans(phases):
    """IBANs (old and new, both stay valid) from bank_account_notice PDFs; party from pdfs/index.csv."""
    rows = []
    iban_re = re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b")
    for ph in phases:
        idx = f"{RAW}/{ph}/pdfs/index.csv"
        if not os.path.exists(idx):
            continue
        import csv
        with open(idx, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["document_type"] != "bank_account_notice":
                    continue
                path = f"{RAW}/{ph}/pdfs/{r['file']}"
                if not os.path.exists(path):
                    continue
                text = "".join(p.extract_text() or "" for p in pypdf.PdfReader(path).pages)
                for iban in set(iban_re.findall(text.upper().replace(" ", ""))):
                    rows.append((iban, r["party_id"], r["document_date"]))
    return rows


def build(model, con=None, phases_for_notices=("train",)):
    con = con or connect()
    d = f"work/party_model/{model}"
    os.makedirs(d, exist_ok=True)
    if model == "pre_window":
        lines = "read_parquet('work/learn/party_lines.parquet')"
        tcut = f"t.booking_date < DATE '{WINDOW_START}'"
    else:
        lines = "read_parquet(['work/learn/party_lines.parquet','work/window/party_lines.parquet'])"
        tcut = "TRUE"
    con.execute(f"""CREATE OR REPLACE TEMP VIEW hist AS
        SELECT l.*, t.parties[1] AS party_id
        FROM {lines} l JOIN read_parquet('work/train/party_truth.parquet') t USING (bank_txn_id)
        WHERE t.n_party = 1 AND {tcut}""")

    # parties (masters)
    con.execute(f"""CREATE OR REPLACE TABLE parties AS
        SELECT customer_id AS party_id, entity, 'C' AS kind, parent_customer_id AS parent_party_id, group_id,
               norm(name) AS name_norm, norm(trade_name) AS trade_norm, upper(replace(iban,' ','')) AS iban,
               sepa_mandate_id
        FROM {master('customers')}
        UNION ALL
        SELECT supplier_id, entity, 'S', NULL, NULL, norm(name), NULL, upper(replace(iban,' ','')), NULL
        FROM {master('suppliers')}""")
    con.execute(f"COPY (SELECT party_id, entity, kind, parent_party_id, group_id, name_norm FROM parties) TO '{d}/parties.parquet'")

    # every name form of a party: current, trade, and the names printed on its documents (catches renames). The
    # pre_window model only sees TRAIN; the full model also reads the documents of the later phases present on disk
    # (LIVE/FINAL invoices of customers created or renamed after TRAIN), never their bank lines or any golden.
    doc_phases = ["train"] if model == "pre_window" else [p for p in ("train", "live", "final") if os.path.isdir(f"{RAW}/{p}")]
    printed = " ".join(
        f"""UNION SELECT customer_id, entity, norm(customer_name), 'printed_name' FROM {src(p, 'ar_invoices')}
            UNION SELECT supplier_id, entity, norm(supplier_name), 'printed_name' FROM {src(p, 'ap_invoices')}
            UNION SELECT party_id, entity, norm(party_name), 'printed_name' FROM {src(p, 'credit_notes')}"""
        for p in doc_phases if os.path.exists(f"{RAW}/{p}/ar_invoices.csv.gz"))
    # materialised once (a view re-read every csv.gz for each key length and ran out of memory with LIVE names)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE names AS
        SELECT party_id, entity, name_norm AS nm, 'master_name' AS source FROM parties
        UNION SELECT party_id, entity, trade_norm, 'trade_name' FROM parties WHERE trade_norm IS NOT NULL
        {printed}""")
    parts = []
    for L in KEY_LENGTHS:
        parts.append(f"SELECT entity, {L} AS L, left(nm, {L}) AS key, party_id, source FROM names WHERE length(nm) >= {L}")
    # short names (< 10 chars) are matched whole (L = their length, followed by a space or end of text)
    parts.append(f"""SELECT entity, length(nm) AS L, nm AS key, party_id, source FROM names
                     WHERE length(nm) BETWEEN {SHORT_MIN} AND {min(KEY_LENGTHS) - 1}""")
    con.execute(f"""COPY (
        WITH k AS ({' UNION ALL '.join(parts)})
        SELECT *, count(*) OVER (PARTITION BY entity, L, key) AS n_parties
        FROM (SELECT entity, L, key, party_id, min(source) AS source FROM k GROUP BY ALL)
    ) TO '{d}/master_keys.parquet'""")

    # learned keys: text prefixes starting at each of the first tokens, per line entity
    con.execute("CREATE OR REPLACE TEMP TABLE sfx AS "
                f"""SELECT entity, party_id, array_to_string(tok[k:], ' ') AS s
                    FROM (SELECT entity, party_id, string_split(text_norm, ' ') AS tok FROM hist),
                         LATERAL (SELECT unnest(range(1, least(len(tok), {MAX_TOKENS}) + 1)) AS k)""")
    learned = []
    for L in KEY_LENGTHS:
        con.execute(f"""CREATE OR REPLACE TEMP TABLE lk{L} AS
            WITH c AS (SELECT entity, left(s, {L}) AS key, party_id, count(*) AS n
                       FROM sfx WHERE length(s) >= {L} GROUP BY ALL),
                 t AS (SELECT *, sum(n) OVER (PARTITION BY entity, key) AS total FROM c)
            SELECT entity, {L} AS L, key, party_id, n, total, n / total AS purity FROM t
            WHERE total >= {MIN_SUPPORT} AND n / total >= {MIN_PURITY}""")
        learned.append(f"SELECT * FROM lk{L}")
    con.execute(f"COPY ({' UNION ALL '.join(learned)}) TO '{d}/learned_keys.parquet'")
    con.execute("DROP TABLE sfx")

    # IBANs: history + masters + notices
    notices = notice_ibans(phases_for_notices)
    con.execute("CREATE OR REPLACE TEMP TABLE notice (iban VARCHAR, party_id VARCHAR, valid_from VARCHAR)")
    if notices:
        con.executemany("INSERT INTO notice VALUES (?, ?, ?)", notices)
    con.execute(f"""COPY (
        WITH h AS (SELECT unnest(ibans) AS iban, party_id FROM hist),
             c AS (SELECT iban, party_id, count(*) AS n FROM h GROUP BY ALL),
             p AS (SELECT iban, party_id, n, n / sum(n) OVER (PARTITION BY iban) AS purity, 'iban_history' AS source FROM c)
        SELECT * FROM p WHERE purity >= {MIN_PURITY}
        UNION ALL SELECT iban, party_id, 1, 1.0, 'iban_master' FROM parties WHERE iban IS NOT NULL
        UNION ALL SELECT DISTINCT iban, party_id, 1, 1.0, 'iban_notice' FROM notice
    ) TO '{d}/ibans.parquet'""")

    con.execute(f"""COPY (
        SELECT sepa_mandate_id AS mandate_id, party_id, 'mandate_master' AS source FROM parties WHERE sepa_mandate_id IS NOT NULL
        UNION
        SELECT m, party_id, 'mandate_history' FROM (SELECT unnest(mandate_ids) AS m, party_id FROM hist)
    ) TO '{d}/mandates.parquet'""")

    con.execute(f"""COPY (
        WITH x AS (SELECT h.entity AS line_entity, p.entity AS party_entity, count(*) AS n
                   FROM hist h JOIN parties p USING (party_id) GROUP BY ALL)
        SELECT *, n / sum(n) OVER (PARTITION BY line_entity) AS share FROM x
    ) TO '{d}/entity_pairs.parquet'""")
    con.execute(f"""COPY (
        WITH x AS (SELECT direction, left(party_id, 1) AS kind, count(*) AS n FROM hist GROUP BY ALL)
        SELECT *, n / sum(n) OVER (PARTITION BY direction) AS share FROM x
    ) TO '{d}/type_prior.parquet'""")
    return d


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1]))
