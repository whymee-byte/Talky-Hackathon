"""Pydantic models for the normalized bank-statement stream."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NormalizedTransaction(BaseModel):
    """One bank movement in the compact, LLM-friendly interchange format."""

    model_config = ConfigDict(str_strip_whitespace=False)

    bank_txn_id: str
    account: str
    booking_date: date
    value_date: date
    amount: Decimal = Field(...)
    currency: str
    counterparty: str
    reference: str
    concept: str
    llm_clean_text: str
    source_file: str
    raw_record: str

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> Decimal:
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"invalid amount: {value!r}") from exc
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        return amount


class MalformedRecord(BaseModel):
    """A raw record that could not be normalized without losing evidence."""

    model_config = ConfigDict(str_strip_whitespace=False)

    source_file: str
    raw_record: str
    error: str
    record_number: int | None = None
    format: str | None = None


__all__ = ["MalformedRecord", "NormalizedTransaction"]
