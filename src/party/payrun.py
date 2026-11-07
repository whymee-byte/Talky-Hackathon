"""Evidence family `payrun`: our own outgoing payment files (payment_runs/<entity>_<YYYYMM>_<n>.csv).

Bank lines of a payment run carry its run_id in the text ("... 000023 PR010125011017 0", "REF PR010024102402 0233").
run_id is NOT unique across entities (every entity numbers PR01...), so the join also requires
debtor_account_id = account_id of the line. For `transfers` runs (one bank debit per beneficiary) the beneficiary
row is the one with transfer_amount = |line amount| or with the end-to-end sequence written before the run id.
The beneficiary -> party through: its document numbers (= supplier invoice / credit note numbers of the file
entity), its IBAN (masters / history) and its name (master name of the file entity).
Aggregated / confirming runs (one debit for many beneficiaries) are T2's job (side_links); here they only add the
whole-run parties when the amount singles out one row.
"""
import glob
import os

from .common import CACHE, RAW, src

RUN_RE = r"\b((?:PR|CF)[0-9]{12})\b"
CONF_DOC, CONF_IBAN, CONF_NAME = 0.96, 0.95, 0.90


def run_files(phase):
    phases = {"window": ("train",), "learn": ("train",), "sample": ("train", "sample"),
              "live": ("train", "live"), "final": ("train", "live", "final")}[phase]
    files = []
    for p in phases:
        files += glob.glob(f"{RAW}/{p}/payment_runs/*.csv")
    return [f for f in files if not os.path.basename(f).startswith("._")]


def runs_source(con, phase, files):
    """read_parquet(...) over all payment-run CSVs of the phase, cached in data_cache/party/ by count + total size
    (reading ~1,000 small CSVs with union_by_name takes ~80 s; the Parquet takes under a second)."""
    sig = f"{len(files)}_{sum(os.path.getsize(f) for f in files)}"
    pq = f"{CACHE}/party/payment_runs_{phase}_{sig}.parquet"
    if not os.path.isfile(pq):
        os.makedirs(os.path.dirname(pq), exist_ok=True)
        con.execute(f"""COPY (SELECT * FROM read_csv({files!r}, union_by_name=true, filename=true, all_varchar=true))
                        TO '{pq}' (FORMAT parquet)""")
    return f"read_parquet('{pq}')"


def add(con, phase, model):
    """Insert payrun evidence rows into temp table `cand` (bank_txn_id, party_id, fam, rule, conf, evidence)."""
    files = run_files(phase)
    if not files:
        return 0
    m = f"work/party_model/{model}"
    base = "train" if phase in ("window", "learn") else phase
    con.execute(f"""CREATE OR REPLACE TEMP TABLE runs AS
        SELECT run_id, run_type, debtor_account_id, right(end_to_end_id, 4) AS seq,
               regexp_extract(filename, '([A-Z]{{2}}[0-9]{{2}})_[0-9]{{6}}', 1) AS file_entity,
               norm(beneficiary) AS ben_norm, upper(replace(beneficiary_iban, ' ', '')) AS iban,
               CAST(round(CAST(transfer_amount AS DOUBLE) * 100) AS BIGINT) AS amt,
               regexp_replace(upper(document_number), '[^A-Z0-9]', '', 'g') AS doc_key,
               abs(CAST(round(CAST(document_amount AS DOUBLE) * 100) AS BIGINT)) AS doc_amt
        FROM {runs_source(con, phase, files)}""")
    # line -> run (same debtor account) -> beneficiary row singled out by amount or by sequence
    con.execute(f"""CREATE OR REPLACE TEMP TABLE prow AS
        WITH l AS (SELECT bank_txn_id, account_id, text_norm, abs(amount_cents) AS cash,
                          regexp_extract(text_norm, '{RUN_RE}', 1) AS rid FROM ln WHERE amount_cents < 0),
             b AS (SELECT run_id, run_type, debtor_account_id, seq, file_entity, ben_norm, iban, amt
                   FROM runs GROUP BY ALL),
             j AS (SELECT l.bank_txn_id, b.*, b.amt = l.cash AS by_amt,
                          -- N43 writes the 6-digit sequence right before the run id ("000023 PR01..."); invoice
                          -- numbers can sit there too ("FRA 2024 00039 PR02..."), so the sequence is only a fallback
                          contains(l.text_norm, ' ' || lpad(b.seq, 6, '0') || ' ' || b.run_id) AS by_seq
                   FROM l JOIN b ON b.run_id = l.rid AND b.debtor_account_id = l.account_id WHERE l.rid <> ''),
             s AS (SELECT * FROM j WHERE by_amt
                   UNION ALL SELECT * FROM j WHERE by_seq AND bank_txn_id NOT IN (SELECT bank_txn_id FROM j WHERE by_amt))
        SELECT *, count(*) OVER (PARTITION BY bank_txn_id) AS n_rows FROM s""")
    # beneficiary -> party: document numbers of the file entity, IBAN, exact master name
    # document numbers repeat across suppliers of an entity (INV0000951...): the document amount must agree too
    docs = f"""SELECT supplier_id AS party_id, entity, supplier_invoice_number AS d, total_amount AS a FROM {src(base, 'ap_invoices')}
               UNION ALL SELECT party_id, entity, document_number, total_amount FROM {src(base, 'credit_notes')}"""
    if base != "train":
        docs += f" UNION ALL SELECT party_id, entity, document_number, open_amount FROM {src(base, 'open_items_at_start')}"
    names = f"""SELECT party_id, entity, name_norm FROM '{m}/parties.parquet' WHERE kind = 'S'
                UNION SELECT supplier_id, entity, norm(supplier_name) FROM {src(base, 'ap_invoices')}"""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE pr_party AS
        WITH d AS (SELECT DISTINCT party_id, entity, regexp_replace(upper(d), '[^A-Z0-9]', '', 'g') AS doc_key,
                          abs(CAST(round(a * 100) AS BIGINT)) AS doc_amt
                   FROM ({docs}) WHERE d IS NOT NULL),
             bd AS (SELECT r.run_id, r.debtor_account_id, r.seq, d.party_id, 'payrun_doc' AS rule,
                           {CONF_DOC} / count(*) OVER (PARTITION BY r.run_id, r.debtor_account_id, r.seq, r.doc_key) AS conf,
                           'run=' || r.run_id || ' doc=' || r.doc_key AS evidence
                    FROM runs r JOIN d ON d.entity = r.file_entity AND d.doc_key = r.doc_key AND d.doc_amt = r.doc_amt),
             bi AS (SELECT r.run_id, r.debtor_account_id, r.seq, ib.party_id, 'payrun_iban', {CONF_IBAN},
                           'run=' || r.run_id || ' iban=' || r.iban
                    FROM runs r JOIN '{m}/ibans.parquet' ib ON ib.iban = r.iban AND ib.purity >= 0.8),
             bn AS (SELECT r.run_id, r.debtor_account_id, r.seq, p.party_id, 'payrun_name',
                           {CONF_NAME} / count(*) OVER (PARTITION BY r.run_id, r.debtor_account_id, r.seq),
                           'run=' || r.run_id || ' name=' || r.ben_norm
                    FROM runs r JOIN ({names}) p ON p.entity = r.file_entity AND p.name_norm = r.ben_norm)
        SELECT run_id, debtor_account_id, seq, party_id, arg_max(rule, conf) AS rule, max(conf) AS conf,
               arg_max(evidence, conf) AS evidence
        FROM (SELECT * FROM bd UNION ALL SELECT * FROM bi UNION ALL SELECT * FROM bn) GROUP BY ALL""")
    n = con.execute(f"""INSERT INTO cand
        SELECT x.bank_txn_id, pp.party_id, 'payrun', pp.rule, max(pp.conf / x.n_rows), arg_max(pp.evidence, pp.conf)
        FROM prow x JOIN pr_party pp USING (run_id, debtor_account_id, seq)
        GROUP BY 1, 2, 4""").fetchall()
    cnt = con.sql("SELECT count(DISTINCT bank_txn_id) FROM cand WHERE fam = 'payrun'").fetchall()[0][0]
    con.execute("DROP TABLE runs; DROP TABLE prow; DROP TABLE pr_party")
    print(f"payrun: {cnt} lines with a payment-run beneficiary")
    return cnt
