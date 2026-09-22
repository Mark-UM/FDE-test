"""Raw S0-S1 wire schemas, owned by this adapter (not Product domain models)."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.models import Nonblank, UtcTimestamp


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Item(SourceModel):
    sku: Nonblank
    product_name: Nonblank
    quantity: Annotated[int, Field(gt=0)]


class Order(SourceModel):
    order_id: Nonblank
    customer_reference: str | None
    status: Nonblank
    items: list[Item]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp | None


class Parcel(SourceModel):
    parcel_id: Nonblank
    order_id: Nonblank
    carrier: str | None
    tracking_number: str | None
    updated_at: UtcTimestamp | None


class Shipment(SourceModel):
    tracking_number: Nonblank
    parcel_id: Nonblank
    status: str | None
    updated_at: UtcTimestamp | None


class Event(SourceModel):
    event_id: Nonblank
    parcel_id: Nonblank
    status: Nonblank
    description: str
    occurred_at: UtcTimestamp
    updated_at: UtcTimestamp | None


class Note(SourceModel):
    note_id: Nonblank
    order_id: Nonblank
    text: str
    created_at: UtcTimestamp
    updated_at: UtcTimestamp | None


class Inquiry(SourceModel):
    inquiry_id: Nonblank
    order_id: str | None
    customer_message: Nonblank
    created_at: UtcTimestamp


class ReplyCommand(SourceModel):
    text: Annotated[Nonblank, Field(max_length=10000)]
    idempotency_key: Annotated[Nonblank, Field(max_length=200)]


class Receipt(SourceModel):
    reply_id: Nonblank
    status: Literal["SENT"]
    sent_at: UtcTimestamp


class ErrorDetail(SourceModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(SourceModel):
    error: ErrorDetail
