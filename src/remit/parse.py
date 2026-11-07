"""T6 · parse every remittance advice of a phase (csv, xlsx, pdf, e-mail txt) into one flat table.

    python -m src.remit.parse --phase train|live|final|sample      # → data_cache/<phase>/remit_raw.parquet

One row per advice line: file, entity, payer_name, received_date, fmt, payment_ref, payment_date, adv_amount,
adv_currency, line_no, doc, voucher, po, doc_date, gross, deduction, reason, paid. Layouts are recognised by their
header words, not by customer: the Sept-2025 layout (Voucher | Invoice | PO | Inv. date | Amount | Deduction | Net
paid) is just another header. A file that cannot be parsed is counted and skipped (never a crash).
Parsing is per file (≈ 51 k files in TRAIN), in a process pool; everything downstream is columnar.
"""
from __future__ import annotations

import argparse
import csv
import unicodedata
import io
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from src.common import REPO, data_root, phase_dir

COLS = ["file", "entity", "payer_name", "received_date", "fmt", "payment_ref", "payment_date", "adv_amount",
        "adv_currency", "line_no", "doc", "voucher", "po", "doc_date", "gross", "deduction", "reason", "paid", "error"]

DATE_RE = re.compile(r"^\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}$")
AMT_RE = re.compile(r"^-?[\d.,' ]*\d$")
CUR_RE = re.compile(r"\b([A-Z]{3})\b")
# header words → canonical column (any language seen in TRAIN/LIVE; unknown words are ignored)
HEAD = {"document": "doc", "documento": "doc", "invoice": "doc", "factura": "doc", "rechnung": "doc", "facture": "doc",
        "voucher": "voucher", "po": "po", "your_order": "po", "pedido": "po", "order": "po",
        "doc_date": "doc_date", "date": "doc_date", "inv. date": "doc_date", "fecha": "doc_date",
        "gross": "gross", "amount": "gross", "bruto": "gross", "importe": "gross",
        "discount_or_deduction": "deduction", "deduction": "deduction", "dto./ded.": "deduction",
        "reason": "reason", "motivo": "reason", "paid": "paid", "net paid": "paid", "pagado": "paid"}


# fallback when a header is not an exact key (new layouts): words of the normalised header → column, first rule wins
HEAD_WORDS = [("doc_date", {"date", "fecha", "datum", "data"}),
              ("paid", {"paid", "pagado", "bezahlt", "paye", "pago", "pagato", "net", "neto", "netto"}),
              ("deduction", {"deduction", "deduccion", "descuento", "discount", "dto", "abzug", "skonto", "remise"}),
              ("reason", {"reason", "motivo", "grund", "motif", "causale", "comment", "concepto"}),
              ("po", {"po", "order", "pedido", "bestellung", "commande", "ordine"}),
              ("gross", {"gross", "bruto", "brutto", "amount", "importe", "betrag", "montant", "importo", "total"}),
              ("voucher", {"voucher", "vale"}),
              ("doc", {"document", "documento", "doc", "invoice", "inv", "factura", "rechnung", "facture", "fattura", "nr", "no"})]


def _norm_head(h) -> str:
    s = unicodedata.normalize("NFKD", str(h or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def head_col(h) -> str | None:
    """Canonical column of a header cell: exact key first, else by its words (any language)."""
    k = str(h or "").strip().lower()
    if k in HEAD:
        return HEAD[k]
    words = set(_norm_head(h).split())
    return next((c for c, ws in HEAD_WORDS if words & ws), None)


def amount(s) -> float | None:
    """'1,295.52' · '1.651,54' · '-4.86' · '39703.28' · 39703.28 → float (None if not an amount)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not s or not AMT_RE.match(s):
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) and s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s) and not re.search(r"\.\d{1,2}$", s):
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def iso_date(s, us: bool = False) -> str | None:
    """dd/mm/yy(yy), yyyy-mm-dd, or mm/dd/yyyy when `us` → yyyy-mm-dd."""
    if s is None:
        return None
    if hasattr(s, "strftime"):
        return s.strftime("%Y-%m-%d")
    s = str(s).strip()[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})$", s)
    if not m:
        return None
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    d, mo = (b, a) if us else (a, b)
    if mo > 12:
        d, mo = mo, d
    y = y + 2000 if y < 100 else y
    try:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def split_deduction(s) -> tuple[float | None, str | None]:
    """'-4.86 COOP MKT' → (-4.86, 'COOP MKT'); '-67.22' → (-67.22, None)."""
    if s is None or s == "":
        return None, None
    if isinstance(s, (int, float)):
        return float(s), None
    m = re.match(r"^\s*(-?[\d.,' ]*\d)\s*(.*)$", str(s))
    if not m:
        return None, str(s).strip() or None
    return amount(m.group(1)), (m.group(2).strip() or None)


def _rows_from_table(rows: list[list], start: int) -> tuple[list[dict], dict]:
    """rows[start] is the header row; returns (line dicts, column map)."""
    head = [str(x or "").strip().lower() for x in rows[start]]
    cmap = {}
    for j, h in enumerate(head):  # exact keys first, word fallback only for columns still unmapped
        c = HEAD.get(h)
        if c and c not in cmap:
            cmap[c] = j
    for j, h in enumerate(head):
        c = None if h in HEAD else head_col(h)
        if c and c not in cmap and j not in cmap.values():
            cmap[c] = j
    out = []
    for r in rows[start + 1:]:
        if not r or all(x in (None, "") for x in r):
            continue
        first = str(r[0] or "").strip()
        if first.upper().startswith("TOTAL"):
            break
        get = lambda c: r[cmap[c]] if c in cmap and cmap[c] < len(r) else None  # noqa: E731
        ded, why = split_deduction(get("deduction"))
        rs = get("reason")
        out.append({"doc": _s(get("doc")), "voucher": _s(get("voucher")), "po": _s(get("po")),
                    "doc_date": iso_date(get("doc_date")), "gross": amount(get("gross")), "deduction": ded,
                    "reason": _s(rs) or why, "paid": amount(get("paid"))})
    return out, cmap


def _s(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    s = str(x).strip()
    return s or None


def parse_csv(text: str) -> tuple[dict, list[dict]]:
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    h = rows[0]
    kv = {str(h[i]).strip().lower(): h[i + 1] for i in range(0, len(h) - 1, 2)}
    head = {"payment_ref": _s(kv.get("payment_ref")), "payment_date": iso_date(kv.get("payment_date")),
            "adv_currency": _s(kv.get("currency"))}
    if not all(head.values()):  # unknown key names: infer each field from the shape of its value
        for k, v in kv.items():
            v = _s(v)
            if not v:
                continue
            if not head["adv_currency"] and re.fullmatch(r"[A-Z]{3}", v):
                head["adv_currency"] = v
            elif not head["payment_date"] and iso_date(v):
                head["payment_date"] = iso_date(v)
            elif not head["payment_ref"] and re.search(r"\d", v) and ("ref" in _norm_head(k) or "pay" in _norm_head(k)):
                head["payment_ref"] = v
        if not head["payment_ref"]:  # last resort: the value left that carries digits and is neither a date nor a currency
            rest = [_s(v) for v in kv.values() if _s(v) and re.search(r"\d", _s(v)) and not iso_date(_s(v)) and _s(v) != head["adv_currency"]]
            if rest:
                head["payment_ref"] = rest[0]
    hdr = next(i for i, r in enumerate(rows) if r and (str(r[0]).strip().lower() in HEAD or i > 0 and head_col(r[0])))
    lines, _ = _rows_from_table(rows, hdr)
    tot = [r for r in rows if r and str(r[0]).strip().upper().startswith("TOTAL")]
    if tot:
        vals = [amount(x) for x in tot[0][1:] if amount(x) is not None]
        head["adv_amount"] = vals[-1] if vals else None
    return head, lines


def parse_xlsx(path: str) -> tuple[dict, list[dict]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    head = {}
    for r in rows[:6]:
        vals = [x for x in r if x not in (None, "")]
        if vals and str(vals[0]).strip().lower() in ("payment advice", "aviso de pago", "remittance advice") and len(vals) >= 2:
            head["payment_ref"] = _s(vals[1])
            head["payment_date"] = iso_date(vals[2]) if len(vals) > 2 else None
    hdr = next(i for i, r in enumerate(rows) if r and str(r[0] or "").strip().lower() in HEAD)
    lines, cmap = _rows_from_table(rows, hdr)
    tot = [r for r in rows if r and str(r[0] or "").strip().upper().startswith("TOTAL")]
    if tot:
        vals = [amount(x) for x in tot[0][1:] if amount(x) is not None]
        head["adv_amount"] = vals[-1] if vals else None
    return head, lines


TXT_LINE = re.compile(r"^\s{1,}(\S+)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(-?[\d.,]+)(?:\s+(-[\d.,]+)\s*(.*?))?\s*$")
TXT_PAY = re.compile(r"(?:payment of|importe de|Betrag von|montant de|pagamento de|importo di)\s+(-?[\d.,]+)\s+([A-Z]{3})"
                     r"(?:.*?\b(?:on|con fecha|am|le|em|del?)\s+(\d{1,2}/\d{1,2}/\d{2,4}))?", re.I)


def parse_txt(text: str) -> tuple[dict, list[dict]]:
    head = {}
    m = re.search(r"^Subject:\s*.*?([A-Z0-9][A-Z0-9/\-]{5,})\s*$", text, re.M)
    if m:
        head["payment_ref"] = m.group(1)
    m = TXT_PAY.search(text)
    if m:
        head["adv_amount"], head["adv_currency"] = amount(m.group(1)), m.group(2)
        if m.group(3):
            # English body writes mm/dd/yyyy, the rest dd/mm/yyyy
            head["payment_date"] = iso_date(m.group(3), us=bool(re.search(r"payment of", m.group(0), re.I)))
    lines = []
    for ln in text.splitlines():
        t = TXT_LINE.match(ln)
        if not t:
            continue
        gross = amount(t.group(3))
        ded = amount(t.group(4)) if t.group(4) else None
        lines.append({"doc": t.group(1), "voucher": None, "po": None, "doc_date": iso_date(t.group(2)), "gross": gross,
                      "deduction": ded, "reason": (t.group(5) or None) if ded else None,
                      "paid": None if gross is None else round(gross + (ded or 0), 2)})
    # e-mails sometimes state a total below the listed lines without saying why (withholding / discount on every
    # line): spread the gap proportionally so the deduction rules see it per line
    tot = sum(ln["paid"] or 0 for ln in lines)
    adv = head.get("adv_amount")
    if lines and adv and tot and 0.8 < adv / tot < 0.9999 and abs(adv - tot) > 0.02:
        r = adv / tot
        for ln in lines:
            if ln["paid"] is not None and ln["paid"] > 0:
                ln["paid"] = round(ln["paid"] * r, 2)
                ln["deduction"] = round(ln["paid"] - ln["gross"], 2)
    return head, lines


def parse_pdf(path: str) -> tuple[dict, list[dict]]:
    import pypdf
    text = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(path).pages)
    L = [x.strip() for x in text.splitlines() if x.strip()]
    head = {}
    low = [x.lower() for x in L]
    for key in ("payment ref.", "ref."):
        if key in low:
            head["payment_ref"] = L[low.index(key) + 1]
            break
    for key in ("date", "fecha"):
        if key in low:
            head["payment_date"] = iso_date(L[low.index(key) + 1])
            break
    for key in ("amount", "importe"):
        if key in low:
            v = L[low.index(key) + 1]
            m = re.match(r"^(-?[\d.,]+)\s*([A-Z]{3})?$", v)
            if m:
                head["adv_amount"], head["adv_currency"] = amount(m.group(1)), m.group(2)
            break
    # table body starts after the last header word ("Paid"/"Pagado"/"Net paid")
    start = max((i for i, x in enumerate(low) if x in ("paid", "pagado", "net paid")), default=None)
    lines = []
    if start is None:
        return head, lines
    toks = L[start + 1:]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.upper().startswith("TOTAL") or t.startswith("Documento sint"):
            break
        if DATE_RE.match(t) or amount(t) is not None and not re.search(r"[A-Za-z]", t) and i + 1 < len(toks) \
                and not DATE_RE.match(toks[i + 1]):
            i += 1
            continue
        # doc, date, gross, [deduction (+reason)], paid
        if i + 3 < len(toks) and DATE_RE.match(toks[i + 1]) and amount(toks[i + 2]) is not None:
            doc, dd, gross = t, toks[i + 1], amount(toks[i + 2])
            ded, why = None, None
            j = i + 3
            if toks[j].startswith("-") and (re.search(r"[A-Za-z]", toks[j]) or j + 1 < len(toks) and amount(toks[j + 1]) is not None
                                            and not DATE_RE.match(toks[j + 1]) and (j + 2 >= len(toks) or DATE_RE.match(toks[j + 2]) is None)):
                ded, why = split_deduction(toks[j])
                j += 1
            paid = amount(toks[j]) if j < len(toks) else None
            lines.append({"doc": doc, "voucher": None, "po": None, "doc_date": iso_date(dd), "gross": gross,
                          "deduction": ded, "reason": why, "paid": paid})
            i = j + 1
        else:
            i += 1
    return head, lines


def parse_file(args) -> list[dict]:
    path, meta = args
    base = {c: None for c in COLS} | meta
    try:
        fmt = meta["fmt"]
        if fmt == "csv":
            head, lines = parse_csv(open(path, encoding="utf-8-sig", errors="replace").read())
        elif fmt == "txt":
            head, lines = parse_txt(open(path, encoding="utf-8-sig", errors="replace").read())
        elif fmt == "xlsx":
            head, lines = parse_xlsx(path)
        elif fmt == "pdf":
            head, lines = parse_pdf(path)
        else:
            return [base | {"error": f"unknown format {fmt}"}]
    except Exception as e:  # never fail the phase on one bad file
        return [base | {"error": f"{type(e).__name__}: {e}"[:200]}]
    if not lines:
        return [base | head | {"error": "no lines"}]
    return [base | head | ln | {"line_no": k} for k, ln in enumerate(lines)]


def build(phase: str, root: str | None = None, workers: int | None = None) -> str:
    import polars as pl
    t0 = time.time()
    d = os.path.join(phase_dir(phase, root), "remittance_advices")
    out_dir = os.path.join(REPO, "data_cache", phase)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "remit_raw.parquet")
    idx = os.path.join(d, "index.csv")
    jobs = []
    if os.path.exists(idx):
        with open(idx, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if os.path.basename(r["file"]).startswith("._"):
                    continue
                jobs.append((os.path.join(d, r["file"]), {"file": r["file"], "entity": r.get("entity"),
                             "payer_name": r.get("payer_name"), "received_date": r.get("received_date"),
                             "fmt": (r.get("format") or os.path.splitext(r["file"])[1].lstrip(".")).lower()}))
    rows = []
    with ProcessPoolExecutor(max_workers=workers or max(1, (os.cpu_count() or 2) - 2)) as ex:
        for part in ex.map(parse_file, jobs, chunksize=64):
            rows.extend(part)
    schema = {c: pl.String for c in COLS} | {c: pl.Float64 for c in ("adv_amount", "gross", "deduction", "paid")} | {"line_no": pl.Int64}
    df = pl.DataFrame(rows or None, schema=schema, orient="row") if rows else pl.DataFrame(schema=schema)
    df.write_parquet(out)
    files = df["file"].n_unique() if df.height else 0
    errs = df.filter(pl.col("error").is_not_null())
    print(f"[{phase}] remittance advices: {len(jobs)} files, {files} parsed, {df.height - errs.height} lines, "
          f"{errs.height} files with errors -> {out} ({time.time() - t0:.0f}s)")
    if errs.height:
        print(errs.group_by("fmt", "error").len().sort("len", descending=True).head(8))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "train", "window", "live", "final"])
    ap.add_argument("--data")
    a = ap.parse_args()
    build("train" if a.phase == "window" else a.phase, a.data)


if __name__ == "__main__":
    sys.exit(main())
