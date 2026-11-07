"""Deterministic parsers for Norma 43, camt.053 and STA/MT940."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

from .models import MalformedRecord, NormalizedTransaction

_CURRENCY_RE = re.compile(r"(?:^|[-_])([A-Z]{3})(?:[-_]|$)")
_N43_NON_TRANSACTION_TYPES = {"11", "24", "33", "88"}


def _local_name(tag: str) -> str:
    """Return an XML local name regardless of namespace or prefix."""

    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%y%m%d", "%d%m%y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"invalid date: {value!r}")


def _parse_n43_date(value: str) -> date:
    """Parse the YYMMDD convention used by the supplied Norma 43 files."""

    text = value.strip()
    try:
        return datetime.strptime(text, "%y%m%d").date()
    except ValueError:
        return datetime.strptime(text, "%d%m%y").date()


def _parse_amount(value: str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip().replace(" ", "")
        if not text:
            raise ValueError("empty amount")
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"invalid amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount


def _infer_currency(account: str, path: Path | None = None) -> str:
    matches = _CURRENCY_RE.findall(account.upper())
    if matches:
        return matches[-1]
    if path is not None:
        matches = _CURRENCY_RE.findall(path.stem.upper())
        if matches:
            return matches[-1]
    return ""


def clean_llm_text(*parts: str | None) -> str:
    """Join useful text while collapsing all whitespace for token efficiency."""

    return " ".join(" ".join(str(part).split()) for part in parts if part and str(part).strip())


def generate_bank_txn_id(
    account: str,
    value_date: str | date,
    amount: Decimal | str,
    raw_record: str,
) -> str:
    """Generate the required id from account, ISO value date, amount and raw data."""

    value_iso = _parse_date(value_date).isoformat()
    amount_decimal = _parse_amount(amount)
    payload = f"{account}{value_iso}{format(amount_decimal, 'f')}{raw_record}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_transaction(
    *,
    account: str,
    booking_date: str | date,
    value_date: str | date,
    amount: Decimal | str,
    currency: str,
    counterparty: str,
    reference: str,
    concept: str,
    source_file: str,
    raw_record: str,
) -> NormalizedTransaction:
    parsed_amount = _parse_amount(amount)
    value = _parse_date(value_date)
    booking = _parse_date(booking_date)
    counterparty = counterparty.strip()
    reference = reference.strip()
    concept = concept.strip()
    return NormalizedTransaction(
        bank_txn_id=generate_bank_txn_id(account, value, parsed_amount, raw_record),
        account=account,
        booking_date=booking,
        value_date=value,
        amount=parsed_amount,
        currency=currency.strip().upper(),
        counterparty=counterparty,
        reference=reference,
        concept=concept,
        llm_clean_text=clean_llm_text(counterparty, reference, concept),
        source_file=source_file,
        raw_record=raw_record,
    )


def _error(
    path: Path,
    raw_record: str,
    exc: Exception | str,
    record_number: int | None,
    format_name: str,
) -> MalformedRecord:
    return MalformedRecord(
        source_file=str(path),
        raw_record=raw_record,
        error=str(exc),
        record_number=record_number,
        format=format_name,
    )


def parse_n43(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    """Parse fixed-width Norma 43 records and retain every malformed record."""

    lines = path.read_text(encoding="latin-1", errors="replace").splitlines()
    account = path.parent.name
    currency = _infer_currency(account, path)
    index = 0
    while index < len(lines):
        line = lines[index]
        record_number = index + 1
        if line.startswith("22"):
            raw_lines = [line]
            index += 1
            detail_lines: list[str] = []
            while index < len(lines) and lines[index].startswith("23"):
                detail_lines.append(lines[index])
                raw_lines.append(lines[index])
                index += 1
            auxiliary = ""
            if index < len(lines) and lines[index].startswith("24"):
                auxiliary = lines[index]
                raw_lines.append(auxiliary)
                index += 1
            raw_record = "\n".join(raw_lines)
            try:
                if len(line) != 80:
                    raise ValueError(f"record 22 must be 80 characters, got {len(line)}")
                invalid_detail = next((detail for detail in detail_lines if len(detail) != 80), None)
                if invalid_detail is not None:
                    raise ValueError(f"record 23 must be 80 characters, got {len(invalid_detail)}")
                if auxiliary and len(auxiliary) != 80:
                    raise ValueError(f"record 24 must be 80 characters, got {len(auxiliary)}")
                booking_date = _parse_n43_date(line[10:16])
                value_date = _parse_n43_date(line[16:22])
                sign = line[27]
                if sign not in {"1", "2"}:
                    raise ValueError(f"unknown Norma 43 sign {sign!r}")
                amount_digits = line[28:42]
                if not amount_digits.isdigit():
                    raise ValueError(f"invalid Norma 43 amount {amount_digits!r}")
                amount = Decimal(amount_digits) / Decimal("100")
                if sign == "1":
                    amount = -amount
                detail_segments = [
                    segment.strip()
                    for detail in detail_lines
                    for segment in (detail[4:42], detail[42:80])
                    if segment.strip()
                ]
                counterparty = detail_segments[0] if detail_segments else ""
                concept = " | ".join(detail_segments[1:]) if len(detail_segments) > 1 else line[64:80].strip()
                references = [line[52:64].strip(), line[64:80].strip()]
                reference = " | ".join(value for value in references if value)
                yield _make_transaction(
                    account=account,
                    booking_date=booking_date,
                    value_date=value_date,
                    amount=amount,
                    currency=currency,
                    counterparty=counterparty,
                    reference=reference,
                    concept=concept,
                    source_file=str(path),
                    raw_record=raw_record,
                )
            except Exception as exc:
                yield _error(path, raw_record, exc, record_number, "n43")
            continue
        if line.startswith("23"):
            yield _error(path, line, "orphan Norma 43 record 23 without record 22", record_number, "n43")
            index += 1
            continue
        record_type = line[:2]
        if record_type in _N43_NON_TRANSACTION_TYPES:
            if len(line) != 80:
                yield _error(
                    path,
                    line,
                    f"record {record_type} must be 80 characters, got {len(line)}",
                    record_number,
                    "n43",
                )
            index += 1
            continue
        yield _error(path, line, f"unknown Norma 43 record type {record_type!r}", record_number, "n43")
        index += 1


def _xml_text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _first_descendant(element: ET.Element, names: Iterable[str]) -> ET.Element | None:
    wanted = set(names)
    return next((child for child in element.iter() if _local_name(child.tag) in wanted), None)


def _child_text(parent: ET.Element | None, name: str) -> str:
    if parent is None:
        return ""
    return _xml_text(next((child for child in parent.iter() if _local_name(child.tag) == name), None))


def _party_value(entry: ET.Element, direction: str, name: str) -> str:
    preferred = "Dbtr" if direction == "CRDT" else "Cdtr"
    fallback = "Cdtr" if preferred == "Dbtr" else "Dbtr"
    for party_name in (preferred, fallback):
        party = next((child for child in entry.iter() if _local_name(child.tag) == party_name), None)
        value = _child_text(party, name)
        if value:
            return value
    return ""


def parse_camt(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    """Parse camt.053 using local-name semantics so namespace revisions do not break it."""

    raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    account = path.parent.name
    fallback_currency = _infer_currency(account, path)
    try:
        root = ET.fromstring(raw_text)
    except Exception as exc:
        yield _error(path, raw_text, exc, None, "camt053")
        return

    entries = [element for element in root.iter() if _local_name(element.tag) == "Ntry"]
    for record_number, entry in enumerate(entries, start=1):
        raw_record = ET.tostring(entry, encoding="unicode")
        try:
            amount_element = next(
                (child for child in entry if _local_name(child.tag) == "Amt"),
                _first_descendant(entry, {"Amt"}),
            )
            if amount_element is None:
                raise ValueError("camt Ntry has no amount")
            amount = _parse_amount(_xml_text(amount_element))
            indicator = _xml_text(_first_descendant(entry, {"CdtDbtInd"})).upper()
            if indicator == "DBIT":
                amount = -amount
            elif indicator != "CRDT":
                raise ValueError(f"unknown camt credit/debit indicator {indicator!r}")

            booking_node = next((child for child in entry if _local_name(child.tag) == "BookgDt"), None)
            value_node = next((child for child in entry if _local_name(child.tag) == "ValDt"), None)
            booking_value = _child_text(booking_node, "Dt") or _child_text(booking_node, "DtTm")
            value_value = _child_text(value_node, "Dt") or _child_text(value_node, "DtTm")
            if not booking_value or not value_value:
                raise ValueError("camt Ntry is missing BookgDt or ValDt")

            ntry_ref = _child_text(entry, "NtryRef")
            account_service_ref = _child_text(entry, "AcctSvcrRef")
            end_to_end = _child_text(entry, "EndToEndId")
            reference = " | ".join(
                dict.fromkeys(value for value in (ntry_ref, end_to_end, account_service_ref) if value)
            )
            remittance = [
                _xml_text(element)
                for element in entry.iter()
                if _local_name(element.tag) in {"Ustrd", "AddtlNtryInf"} and _xml_text(element)
            ]
            concept = " | ".join(dict.fromkeys(remittance))
            counterparty = _party_value(entry, indicator, "Nm")
            currency = amount_element.attrib.get("Ccy", "").strip().upper() or fallback_currency
            yield _make_transaction(
                account=account,
                booking_date=_parse_date(booking_value),
                value_date=_parse_date(value_value),
                amount=amount,
                currency=currency,
                counterparty=counterparty,
                reference=reference,
                concept=concept,
                source_file=str(path),
                raw_record=raw_record,
            )
        except Exception as exc:
            yield _error(path, raw_record, exc, record_number, "camt053")


def _mt940_currency(lines: list[str], account: str, path: Path) -> str:
    for line in lines:
        match = re.match(r":60[FM]:[CD]\d{6}([A-Z]{3})", line)
        if match:
            return match.group(1)
    return _infer_currency(account, path)


def _mt940_booking_date(value_date: date, entry_date: str | None) -> date:
    if not entry_date:
        return value_date
    month = int(entry_date[:2])
    day = int(entry_date[2:])
    booking = date(value_date.year, month, day)
    if booking > value_date and (booking - value_date).days > 180:
        booking = date(value_date.year - 1, month, day)
    return booking


def _mt940_counterparty(text: str) -> str:
    for marker in ("/ORDP/", "/DE "):
        if marker in text:
            candidate = text.split(marker, 1)[1].split("/", 1)[0].strip()
            if candidate:
                return candidate
    return ""


def _is_mt940_metadata(line: str) -> bool:
    if not line.strip() or line.startswith(("{", "-}")):
        return True
    return line.startswith((":20:", ":21:", ":25:", ":28", ":60", ":62", ":64:", ":65:"))


def parse_mt940(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    """Parse MT940 transaction blocks, including wrapped :86: information."""

    text = path.read_text(encoding="latin-1", errors="replace")
    lines = text.splitlines()
    account = path.parent.name
    currency = _mt940_currency(lines, account, path)
    starts = [index for index, line in enumerate(lines) if line.startswith(":61:")]
    transaction_lines: set[int] = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block_lines: list[str] = []
        for line_index in range(start, end):
            line = lines[line_index]
            if line.startswith((":62", ":64", ":65", "-}")):
                break
            transaction_lines.add(line_index)
            block_lines.append(line)
        raw_record = "\n".join(block_lines)
        try:
            if not block_lines:
                raise ValueError("empty MT940 transaction block")
            statement_line = block_lines[0][4:]
            match = re.match(
                r"^(?P<value>\d{6})(?P<entry>\d{4})?(?P<sign>[DC])(?P<amount>\d[\d.,]*)(?P<rest>.*)$",
                statement_line,
            )
            if match is None:
                raise ValueError("invalid :61: record")
            value_date = _parse_date(match.group("value"))
            booking_date = _mt940_booking_date(value_date, match.group("entry"))
            amount = _parse_amount(match.group("amount"))
            if match.group("sign") == "D":
                amount = -amount
            rest = match.group("rest").strip()
            if len(rest) < 4:
                raise ValueError("MT940 :61: record has no transaction type")
            references = rest[4:]
            if "//" in references:
                customer_reference, bank_reference = references.split("//", 1)
            else:
                customer_reference, bank_reference = references, ""
            reference = " | ".join(
                value.strip() for value in (customer_reference, bank_reference) if value.strip()
            )
            text_parts: list[str] = []
            in_86 = False
            for line in block_lines[1:]:
                if line.startswith(":86:"):
                    text_parts.append(line[4:])
                    in_86 = True
                elif in_86 and not line.startswith(":"):
                    text_parts.append(line)
                else:
                    in_86 = False
            concept = " ".join(part.strip() for part in text_parts if part.strip())
            yield _make_transaction(
                account=account,
                booking_date=booking_date,
                value_date=value_date,
                amount=amount,
                currency=currency,
                counterparty=_mt940_counterparty(concept),
                reference=reference,
                concept=concept,
                source_file=str(path),
                raw_record=raw_record,
            )
        except Exception as exc:
            yield _error(path, raw_record, exc, start + 1, "mt940")
    for line_number, line in enumerate(lines):
        if line_number in transaction_lines or _is_mt940_metadata(line):
            continue
        yield _error(path, line, "orphan or unknown MT940 record", line_number + 1, "mt940")


def parse_file(path: Path) -> Iterator[NormalizedTransaction | MalformedRecord]:
    """Dispatch a statement by suffix and report unsupported files as errors."""

    suffix = path.suffix.lower()
    if suffix == ".n43":
        yield from parse_n43(path)
    elif suffix == ".xml":
        yield from parse_camt(path)
    elif suffix in {".sta", ".mt940"}:
        yield from parse_mt940(path)
    else:
        yield _error(path, "", f"unsupported statement format: {suffix or '<none>'}", None, suffix.lstrip(".") or None)


__all__ = [
    "clean_llm_text",
    "generate_bank_txn_id",
    "parse_camt",
    "parse_file",
    "parse_mt940",
    "parse_n43",
]
