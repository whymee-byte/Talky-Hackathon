"""T8 · facts from PDFs → work/<phase>/doc_facts.parquet (contracts §7).

    python -m src.pdffacts.extract --phase train          # once per phase: PDF text cache (data_cache/<phase>/pdf_text.parquet)
    python -m src.pdffacts --phase window|live|sample     # regex facts + item resolution

`window` reads the train cache. Facts (rule ids `T8.<fact>.<lang>`):
INSTALMENTS (value_num = n, value_text = "days|amounts") · RETENTION (value_num = %, value_text = release date) ·
ADVANCE_DEDUCT (value_num = advance amount, value_text = proforma) · REPLACES (value_text = replaced document) ·
SERVICE_MONTH (value_text = YYYY-MM) · RAPPEL_OFFSET / RAPPEL_PAID_OUT · CN_OFFSET / CN_REFUND · FACTORED (value_text =
factor) · NEW_IBAN (value_text = new IBAN, valid_from; evidence keeps the old IBAN when quoted).
Regex first; documents with an unknown layout are counted (no LLM call in this version, see TODO.md).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import duckdb

from src.common import REPO, safe_stdout

MONTHS = {
    # es, en, fr, de, it, pt
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
    "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
    "januar": 1, "februar": 2, "marz": 3, "märz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "settembre": 9, "ottobre": 10,
    "dicembre": 12, "janeiro": 1, "fevereiro": 2, "março": 3, "maio": 5, "junho": 6, "julho": 7, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
NUM = r"\d[\d.,\s]*\d|\d"
DATE = r"\d{2}/\d{2}/\d{4}"
IBAN = r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}"

# (fact, lang, regex) — applied to the whitespace-collapsed text
PATTERNS = [
    ("INSTALMENTS", "es", r"(\d+) vencimientos a ([\d/]+) d[ií]as \(([^)]*)\)"),
    ("INSTALMENTS", "en", r"(\d+) instalments at ([\d/]+) days \(([^)]*)\)"),
    ("INSTALMENTS", "fr", r"(\d+) [ée]ch[ée]ances [àa] ([\d/]+) jours \(([^)]*)\)"),
    ("INSTALMENTS", "de", r"(\d+) Raten (?:zu|nach|an) ([\d/]+) Tagen \(([^)]*)\)"),
    ("RETENTION", "es", rf"Retenci[oó]n en garant[ií]a del (\d+(?:[.,]\d+)?) ?% \(({NUM}) [A-Z]{{3}}\),? liberable a la recepci[oó]n definitiva \(({DATE})\)"),
    ("RETENTION", "en", rf"Retention (\d+(?:[.,]\d+)?) ?% \(({NUM}) [A-Z]{{3}}\) withheld until final acceptance \(({DATE})\)"),
    ("ADVANCE_DEDUCT", "es", rf"A deducir anticipo recibido seg[uú]n proforma (\S+) de ({NUM}) [A-Z]{{3}}"),
    ("ADVANCE_DEDUCT", "en", rf"Less advance received against proforma (\S+?):? ({NUM}) [A-Z]{{3}}"),
    ("REPLACES", "es", r"anula y sustituye a la factura (\S+?) de"),
    ("REPLACES", "en", r"cancels and replaces invoice (\S+?) dated"),
    ("SERVICE_MONTH", "es", r"correspondiente a ([a-zA-Záéíóúñ]+) (?:de )?(\d{4})"),
    ("SERVICE_MONTH", "en", r"for the month of ([A-Za-z]+) (\d{4})"),
    ("CN_OFFSET", "es", r"(?:se compensar[aá] en sus pr[oó]ximos pagos|se descontar[aá] de su pr[oó]ximo pago)"),
    ("CN_OFFSET", "en", r"(?:to be deducted from your next payment|will be offset (?:against|in) (?:your|our) next payment)"),
    ("CN_REFUND", "es", r"(?:les ser[aá] reembolsado mediante transferencia|ser[aá] abonado mediante transferencia)"),
    ("CN_REFUND", "en", r"(?:will be refunded by bank transfer|will be paid out by (?:bank )?transfer)"),
    ("FACTORED", "es", r"ha sido cedido a (.+?)\. El pago"),
    ("FACTORED", "en", r"has been assigned to (.+?)\. Payment"),
    ("NEW_IBAN", "es", rf"a partir del ({DATE}) nuestros datos bancarios cambian\. Nuevo IBAN ({IBAN})"),
    ("NEW_IBAN", "en", rf"from ({DATE}) our bank details change\. New IBAN ({IBAN})"),
    ("NEW_IBAN", "es_notice", rf"a partir del ({DATE}) .{{0,160}}?(?:nueva )?cuenta ({IBAN}),? en sustituci[oó]n de la anterior ({IBAN})"),
    ("NEW_IBAN", "en_notice", rf"from ({DATE}) .{{0,160}}?account ({IBAN}) \(previously ({IBAN})\)"),
]
RAPPEL = re.compile(r"\b(rappel|rebate|bonificaci[oó]n por volumen|volume discount)\b", re.I)


def _num(s: str) -> float | None:
    s = re.sub(r"\s", "", s or "")
    if not s:
        return None
    if "," in s and (s.rfind(",") > s.rfind(".")):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date(s: str) -> str | None:
    d, m, y = s.split("/")
    return f"{y}-{m}-{d}"


def facts_of(doc_type: str, text: str) -> list[tuple]:
    """→ [(fact, lang, value_num, value_text, valid_from, evidence)] for one document."""
    t = re.sub(r"\s+", " ", text or "")
    out = []
    for fact, lang, rx in PATTERNS:
        for m in re.finditer(rx, t, flags=re.I):
            g = m.groups()
            vn = vt = vf = None
            if fact == "INSTALMENTS":
                vn, vt = float(g[0]), f"{g[1]}|{g[2]}"
            elif fact == "RETENTION":
                vn, vt = _num(g[0]), _date(g[2])
            elif fact == "ADVANCE_DEDUCT":
                vt, vn = g[0].rstrip(":"), _num(g[1])
            elif fact == "REPLACES":
                vt = g[0].rstrip(".,")
            elif fact == "SERVICE_MONTH":
                mo = MONTHS.get(g[0].lower())
                if not mo:
                    continue
                vt = f"{g[1]}-{mo:02d}"
            elif fact == "FACTORED":
                vt = g[0].strip()
            elif fact == "NEW_IBAN":
                vf, vt = _date(g[0]), g[1]
            if fact.startswith("CN_") and RAPPEL.search(t):
                fact = "RAPPEL_OFFSET" if fact == "CN_OFFSET" else "RAPPEL_PAID_OUT"
            out.append((fact, lang, vn, vt, vf, m.group(0)[:200]))
    return out


PRIOR_PHASES = {"live": ("train",), "final": ("train", "live")}   # earlier PDF caches for items open at start


def build(phase: str) -> dict:
    cache_phase = "train" if phase == "window" else phase
    src = os.path.join(REPO, "data_cache", cache_phase, "pdf_text.parquet").replace(os.sep, "/")
    if not os.path.exists(src):
        raise FileNotFoundError(f"{src} missing: run python -m src.pdffacts.extract --phase {cache_phase}")
    con = duckdb.connect()
    docs = con.sql(f"select file, document_type, document_number, entity, party_id, document_date, text from '{src}'").fetchall()
    # invoices open at the start of the phase were issued (and their PDF shipped) in an earlier phase: read those caches
    # too, keeping only facts that resolve to an item of this phase (below)
    prior = []
    for pp in PRIOR_PHASES.get(phase, ()):
        ps = os.path.join(REPO, "data_cache", pp, "pdf_text.parquet").replace(os.sep, "/")
        if os.path.exists(ps):
            prior += con.sql(f"""select file, document_type, document_number, entity, party_id, document_date, text
                                from '{ps}' where document_type in ('invoice', 'credit_note', 'supplier_invoice',
                                'supplier_credit_note')""").fetchall()
    rows = []
    unknown = 0
    for own, batch in ((True, docs), (False, prior)):
        for f, dt, dn, ent, pid, dd, txt in batch:      # one regex pass per document
            fs = facts_of(dt, txt)
            if own and not fs and dt in ("bank_account_notice",):
                unknown += 1
            for fact, lang, vn, vt, vf, ev in fs:
                rows.append((f, dt, ent, pid, dn, fact, vn, vt, vf, f"T8.{fact.lower()}.{lang}", ev, own))
    import pyarrow as pa
    cols = ["pdf_file", "document_type", "entity", "party_id", "doc_number", "fact", "value_num", "value_text", "valid_from", "rule", "evidence", "own"]
    tb = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)}) if rows else pa.table({c: [] for c in cols})
    con.register("f", tb)
    work = os.path.join(REPO, "work", phase).replace(os.sep, "/")
    # resolve the document to an item of the phase: same entity + party + printed number (AR ours / AP supplier's)
    con.sql(f"""create table facts as
      select f.pdf_file, f.entity, f.party_id, f.doc_number, i.item_id, f.fact, f.value_num, f.value_text,
             try_cast(f.valid_from as date) valid_from, f.rule,
             case when i.item_id is not null or f.fact = 'NEW_IBAN' then 0.95 else 0.6 end confidence, f.evidence, f.document_type
      from f left join (select item_id, entity, party_id, doc_norm from '{work}/items.parquet') i
        on i.entity = f.entity and i.party_id = f.party_id
       and i.doc_norm = trim(regexp_replace(strip_accents(upper(f.doc_number)), '[^A-Z0-9]+', ' ', 'g'))
       and f.document_type in ('invoice', 'credit_note', 'supplier_invoice', 'supplier_credit_note')
      where f.own or i.item_id is not null
      qualify f.own or count(*) filter (where f.own) over (partition by i.item_id, f.fact) = 0""")
    con.sql(f"copy facts to '{work}/doc_facts.parquet' (format parquet)")
    stats = con.sql("select fact, count(*), count(item_id) from facts group by 1 order by 1").fetchall()
    return {"docs": len(docs), "facts": stats, "notices_without_fact": unknown}


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["sample", "window", "train", "live", "final"])
    a = ap.parse_args(argv)
    t0 = time.time()
    r = build(a.phase)
    print(f"[{a.phase}] {r['docs']} pdfs, notices without fact: {r['notices_without_fact']}")
    for fact, n, ni in r["facts"]:
        print(f"  {fact:16s} {n:7d} facts, {ni:7d} resolved to an item")
    print(f"  -> work/{a.phase}/doc_facts.parquet ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
