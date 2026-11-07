"""Robust parsers for Norma 43, camt.053 and STA/MT940 files."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from .models import MalformedRecord, NormalizedTransaction


def _date(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%y%m%d", "%y%m%d"):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"invalid date: {value!r}")


def _id(account: str, value_date: str, amount: Decimal, raw: str) -> str:
    payload = account + value_date + format(amount, "f") + raw
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tx(account: str, booking: str, value: str, amount: str, currency: str, cp: str, ref: str, concept: str, raw: str, source: str) -> NormalizedTransaction:
    parsed = Decimal(amount.replace(".", "").replace(",", ".") if amount.count(",") == 1 and amount.count(".") > 1 else amount.replace(",", "."))
    value_iso = _date(value)
    return NormalizedTransaction(bank_txn_id=_id(account, value_iso, parsed, raw), account=account, booking_date=_date(booking), value_date=value_iso, amount=parsed, currency=currency or "", counterparty=cp.strip(), reference=ref.strip(), concept=concept.strip(), llm_clean_text=" ".join((cp + " " + ref + " " + concept).split()), source_file=source, raw_record=raw)


def parse_n43(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    lines = path.read_text(encoding="latin-1", errors="replace").splitlines()
    account = path.parent.name
    currency = next((code for code in ("EUR", "USD", "GBP", "CHF", "PLN", "MAD", "MXN", "BRL", "CNY", "ZAR", "CAD", "AUD") if f"-{code}" in account), "")
    for i, line in enumerate(lines):
        if not line.startswith("22"):
            continue
        raw = line + ("\n" + lines[i + 1] if i + 1 < len(lines) and lines[i + 1].startswith("23") else "")
        try:
            if len(line) < 34:
                raise ValueError("short record 22")
            date = line[10:16]
            sign = -1 if line[21:22] == "1" else 1
            cents = line[22:34]
            amount = str(sign * (Decimal(cents) / 100))
            detail = lines[i + 1] if i + 1 < len(lines) and lines[i + 1].startswith("23") else ""
            yield _tx(account, date, date, amount, currency, detail[4:29], line[34:54], detail[29:], raw, str(path))
        except Exception as exc:
            yield MalformedRecord(source_file=str(path), raw_record=raw, error=str(exc), record_number=i + 1)


def parse_camt(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    try:
        root = ET.fromstring(raw_text)
    except Exception as exc:
        yield MalformedRecord(source_file=str(path), raw_record=raw_text, error=str(exc)); return
    def local(e, name): return [x for x in e.iter() if x.tag.rsplit("}", 1)[-1] == name]
    account = next((x.text for x in local(root, "IBAN") if x.text), path.parent.name)
    currency = next((x.attrib.get("Ccy", "") for x in local(root, "Amt") if x.attrib.get("Ccy")), "")
    for n, entry in enumerate(local(root, "Ntry"), 1):
        try:
            amt = next(x for x in entry.iter() if x.tag.rsplit("}", 1)[-1] == "Amt")
            val = Decimal((amt.text or "").replace(",", "."))
            ind = next((x.text for x in entry.iter() if x.tag.rsplit("}", 1)[-1] == "CdtDbtInd"), "CRDT")
            if ind == "DBIT": val = -val
            dates = [x.text for x in entry.iter() if x.tag.rsplit("}", 1)[-1] == "Dt" and x.text]
            refs = [x.text for x in entry.iter() if x.tag.rsplit("}", 1)[-1] in {"Ustrd", "NtryRef", "AcctSvcrRef"} and x.text]
            yield _tx(account, dates[0], dates[-1] if len(dates) > 1 else dates[0], str(val), amt.attrib.get("Ccy", currency), "", refs[0] if refs else "", " ".join(refs), ET.tostring(entry, encoding="unicode"), str(path))
        except Exception as exc:
            yield MalformedRecord(source_file=str(path), raw_record=ET.tostring(entry, encoding="unicode"), error=str(exc), record_number=n)


def parse_mt940(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    text = path.read_text(encoding="latin-1", errors="replace")
    account = path.parent.name
    blocks = re.split(r"(?=^:61:)", text, flags=re.M)
    for n, block in enumerate(blocks, 1):
        if not block.startswith(":61:"): continue
        try:
            line = block.splitlines()[0][4:]
            m = re.match(r"(\d{6})(\d{4})?([DC])([0-9,\.]+)", line)
            if not m: raise ValueError("invalid :61: record")
            amount = Decimal(m.group(4).replace(",", ".")) * (-1 if m.group(3) == "D" else 1)
            text86 = next((x[4:] for x in block.splitlines() if x.startswith(":86:")), "")
            yield _tx(account, m.group(1), m.group(1), str(amount), "", "", m.group(2) or "", text86, block, str(path))
        except Exception as exc:
            yield MalformedRecord(source_file=str(path), raw_record=block, error=str(exc), record_number=n)


def parse_file(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    suffix = path.suffix.lower()
    if suffix == ".n43": yield from parse_n43(path)
    elif suffix == ".xml": yield from parse_camt(path)
    elif suffix in {".sta", ".mt940"}: yield from parse_mt940(path)
    else: yield MalformedRecord(source_file=str(path), raw_record="", error=f"unsupported format: {suffix}")
