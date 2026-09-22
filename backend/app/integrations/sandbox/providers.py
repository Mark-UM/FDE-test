"""Explicit S0-S1 mappings. No cross-provider orchestration or interpretation."""

from urllib.parse import quote

from pydantic import TypeAdapter

from app.integrations.models import (
    OrderItem,
    OrderSnapshot,
    ParcelSnapshot,
    ShipmentEvent,
    ShipmentSnapshot,
    SupportInquiry,
    SupportReplyCommand,
    SupportReplyReceipt,
    WarehouseNoteSnapshot,
)
from app.integrations.sandbox import schemas as raw
from app.integrations.sandbox.client import SandboxClient


def segment(value: str) -> str:
    if not value.strip() or value in (".", ".."):
        raise ValueError("A nonblank external identifier is required")
    return quote(value, safe="")


class SandboxOrderProvider:
    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    async def get_order(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> OrderSnapshot:
        response = await self.client.request(
            "GET",
            f"/api/oms/orders/{segment(external_order_id)}",
            TypeAdapter(raw.Order),
            service="demo_oms",
            operation="get_order",
            request_id=request_id,
        )
        data = response.data
        response.require(data.order_id == external_order_id)
        return OrderSnapshot(
            external_order_id=data.order_id,
            customer_reference=data.customer_reference,
            status=data.status,
            items=[
                OrderItem(sku=i.sku, product_name=i.product_name, quantity=i.quantity)
                for i in data.items
            ],
            created_at=data.created_at,
            source_updated_at=data.updated_at,
            fetched_at=response.fetched_at,
            source_system="demo_oms",
            source_record_id=data.order_id,
        )

    async def get_parcels(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> list[ParcelSnapshot]:
        response = await self.client.request(
            "GET",
            f"/api/oms/orders/{segment(external_order_id)}/parcels",
            TypeAdapter(list[raw.Parcel]),
            service="demo_oms",
            operation="get_parcels",
            request_id=request_id,
        )
        response.require(all(p.order_id == external_order_id for p in response.data))
        response.require(len({p.parcel_id for p in response.data}) == len(response.data))
        return [
            ParcelSnapshot(
                parcel_id=p.parcel_id,
                external_order_id=p.order_id,
                carrier=p.carrier,
                tracking_number=p.tracking_number,
                source_updated_at=p.updated_at,
                fetched_at=response.fetched_at,
                source_system="demo_oms",
                source_record_id=p.parcel_id,
            )
            for p in response.data
        ]


class SandboxLogisticsProvider:
    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    async def get_shipment(
        self, parcel: ParcelSnapshot, *, request_id: str | None = None
    ) -> ShipmentSnapshot:
        if parcel.tracking_number is None:
            raise ValueError("Shipment lookup requires a tracking number")
        path = f"/api/logistics/shipments/{segment(parcel.tracking_number)}"
        response = await self.client.request(
            "GET",
            path,
            TypeAdapter(raw.Shipment),
            service="demo_logistics",
            operation="get_shipment",
            request_id=request_id,
        )
        data = response.data
        response.require(
            data.parcel_id == parcel.parcel_id and data.tracking_number == parcel.tracking_number
        )
        events = await self.client.request(
            "GET",
            f"{path}/events",
            TypeAdapter(list[raw.Event]),
            service="demo_logistics",
            operation="get_events",
            request_id=response.request_id,
        )
        events.require(all(e.parcel_id == parcel.parcel_id for e in events.data))
        events.require(len({e.event_id for e in events.data}) == len(events.data))
        return ShipmentSnapshot(
            parcel_id=data.parcel_id,
            status=data.status,
            source_updated_at=data.updated_at,
            fetched_at=response.fetched_at,
            source_system="demo_logistics",
            source_record_id=data.parcel_id,
            events=[
                ShipmentEvent(
                    parcel_id=e.parcel_id,
                    status=e.status,
                    description=e.description,
                    occurred_at=e.occurred_at,
                    source_updated_at=e.updated_at,
                    fetched_at=events.fetched_at,
                    source_system="demo_logistics",
                    source_record_id=e.event_id,
                )
                for e in events.data
            ],
        )


class SandboxWarehouseProvider:
    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    async def get_notes(
        self, external_order_id: str, *, request_id: str | None = None
    ) -> list[WarehouseNoteSnapshot]:
        response = await self.client.request(
            "GET",
            f"/api/warehouse/orders/{segment(external_order_id)}/notes",
            TypeAdapter(list[raw.Note]),
            service="demo_warehouse",
            operation="get_notes",
            request_id=request_id,
        )
        response.require(all(n.order_id == external_order_id for n in response.data))
        response.require(len({n.note_id for n in response.data}) == len(response.data))
        return [
            WarehouseNoteSnapshot(
                external_order_id=n.order_id,
                note_id=n.note_id,
                note_text=n.text,
                created_at=n.created_at,
                source_updated_at=n.updated_at,
                fetched_at=response.fetched_at,
                source_system="demo_warehouse",
                source_record_id=n.note_id,
            )
            for n in response.data
        ]


class SandboxMessageProvider:
    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    async def get_inquiry(
        self, inquiry_id: str, *, request_id: str | None = None
    ) -> SupportInquiry:
        response = await self.client.request(
            "GET",
            f"/api/support/inquiries/{segment(inquiry_id)}",
            TypeAdapter(raw.Inquiry),
            service="demo_support",
            operation="get_inquiry",
            request_id=request_id,
        )
        data = response.data
        response.require(data.inquiry_id == inquiry_id)
        return SupportInquiry(
            inquiry_id=data.inquiry_id,
            external_order_id=data.order_id,
            customer_message=data.customer_message,
            created_at=data.created_at,
        )

    async def send_reply(
        self, command: SupportReplyCommand, *, request_id: str | None = None
    ) -> SupportReplyReceipt:
        body = raw.ReplyCommand(text=command.reply_text, idempotency_key=command.idempotency_key)
        response = await self.client.request(
            "POST",
            f"/api/support/inquiries/{segment(command.inquiry_id)}/replies",
            TypeAdapter(raw.Receipt),
            service="demo_support",
            operation="send_reply",
            request_id=request_id,
            body=body.model_dump(),
        )
        data = response.data
        return SupportReplyReceipt(reply_id=data.reply_id, status=data.status, sent_at=data.sent_at)
