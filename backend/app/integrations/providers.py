"""Narrow async protocols; callers orchestrate independent source reads."""

from typing import Protocol

from app.integrations.models import (
    OrderSnapshot,
    ParcelSnapshot,
    ShipmentSnapshot,
    SupportInquiry,
    SupportReplyCommand,
    SupportReplyReceipt,
    WarehouseNoteSnapshot,
)


class OrderProvider(Protocol):
    async def get_order(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> OrderSnapshot: ...

    async def get_parcels(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> list[ParcelSnapshot]: ...


class LogisticsProvider(Protocol):
    async def get_shipment(
        self, parcel: ParcelSnapshot, *, request_id: str | None = None
    ) -> ShipmentSnapshot: ...


class WarehouseProvider(Protocol):
    async def get_notes(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> list[WarehouseNoteSnapshot]: ...


class MessageProvider(Protocol):
    async def get_inquiry(
        self, inquiry_id: str, *, request_id: str | None = None
    ) -> SupportInquiry: ...

    async def send_reply(
        self, command: SupportReplyCommand, *, request_id: str | None = None
    ) -> SupportReplyReceipt: ...
