from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "CHF", "INR", "JPY"}
EntryType = Literal["deposit", "withdrawal", "fee", "transfer_in", "transfer_out"]

class TransactionIn(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    client_ref: str = Field(..., min_length=1, max_length=64)
    entry_type: EntryType
    amount: Decimal = Field(..., gt=Decimal("0"), max_digits=18, decimal_places=4)
    currency: str = Field(..., min_length=3, max_length=3)
    effective_at: datetime
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        code = v.upper()
        if code not in ALLOWED_CURRENCIES:
            raise ValueError(f"Unsupported currency: {v}")
        return code

    @field_validator("effective_at")
    @classmethod
    def validate_not_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        if v > datetime.now(timezone.utc):
            raise ValueError("effective_at cannot be in the future")
        return v

class ReversalRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
