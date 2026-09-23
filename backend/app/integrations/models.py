"""Canonical Product data, independent of external HTTP shapes and paths."""

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def utc_only(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be UTC")
    return value


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Value must not be blank")
    return value  # Preserve original whitespace and free text.


UtcTimestamp = Annotated[AwareDatetime, AfterValidator(utc_only)]
Nonblank = Annotated[str, AfterValidator(nonblank)]


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Snapshot(CanonicalModel):
    source_system: Nonblank
    source_record_id: Nonblank
    source_updated_at: UtcTimestamp | None
    fetched_at: UtcTimestamp


class OrderItem(CanonicalModel):
    sku: Nonblank
    product_name: Nonblank
    quantity: Annotated[int, Field(gt=0)]


class OrderSnapshot(Snapshot):
    external_order_id: Nonblank
    customer_reference: str | None
    status: Nonblank
    items: list[OrderItem]
    created_at: UtcTimestamp


class ParcelSnapshot(Snapshot):
    parcel_id: Nonblank
    external_order_id: Nonblank
    carrier: str | None
    tracking_number: str | None


class ShipmentEvent(Snapshot):
    parcel_id: Nonblank
    status: Nonblank
    description: str
    occurred_at: UtcTimestamp


class ShipmentSnapshot(Snapshot):
    parcel_id: Nonblank
    status: str | None
    events: list[ShipmentEvent]


class WarehouseNoteSnapshot(Snapshot):
    external_order_id: Nonblank
    note_id: Nonblank
    note_text: str
    created_at: UtcTimestamp


class SupportInquiry(CanonicalModel):
    inquiry_id: Nonblank
    customer_message: Nonblank
    external_order_id: str | None
    created_at: UtcTimestamp


class SupportReplyCommand(CanonicalModel):
    inquiry_id: Nonblank
    reply_text: Nonblank
    idempotency_key: Nonblank


class SupportReplyReceipt(CanonicalModel):
    reply_id: Nonblank
    status: Nonblank
    sent_at: UtcTimestamp
