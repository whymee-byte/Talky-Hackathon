"""Normalized bank-statement records and parsing errors."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class NormalizedTransaction(BaseModel):
    bank_txn_id: str
    account: str
    booking_date: str
    value_date: str
    amount: Decimal
    currency: str
    counterparty: str
    reference: str
    concept: str
    llm_clean_text: str
    source_file: str
    raw_record: str


class MalformedRecord(BaseModel):
    source_file: str
    raw_record: str
    error: str
    record_number: int | None = None
