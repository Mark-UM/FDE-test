"""Synthetic canonical fixtures, independent of Sandbox code and storage."""

import inspect
from datetime import UTC, datetime

from app.integrations.models import (
    OrderSnapshot,
    ParcelSnapshot,
    ShipmentEvent,
    ShipmentSnapshot,
    SupportInquiry,
    WarehouseNoteSnapshot,
)
from app.services.source_collection import Providers

T0 = datetime(2026, 9, 20, 6, tzinfo=UTC)


class FakeSources:
    def __init__(self):
        self.calls = []
        self.faults = {}
        self.hook = None
        self.order_id = "ORD-DEMO-002"
        self.inquiry_id = "INQ-DEMO-002"
        self.order_status = "SHIPPED"
        self.shipment_status = "IN_TRANSIT"
        self.note_text = "Warehouse observation only"
        self.source_time = T0
        self.fetch_time = T0
        self.parcel_ids = ["P1"]
        self.no_tracking = False
        self.duplicate_events = False
        self.event_conflict = False

    @property
    def providers(self):
        return Providers(self, self, self, self)

    async def called(self, name, target):
        self.calls.append((name, target))
        if self.hook:
            result = self.hook(name, target)
            if inspect.isawaitable(result):
                await result
        fault = self.faults.get((name, target), self.faults.get(name))
        if fault:
            raise fault

    def metadata(self, source, identity):
        return dict(
            source_system=source,
            source_record_id=identity,
            source_updated_at=self.source_time,
            fetched_at=self.fetch_time,
        )

    async def get_inquiry(self, identity, *, request_id=None):
        await self.called("get_inquiry", identity)
        return SupportInquiry(
            inquiry_id=self.inquiry_id,
            external_order_id=self.order_id,
            customer_message="Where is my order?",
            created_at=T0,
        )

    async def get_order(self, identity, *, request_id=None):
        await self.called("get_order", identity)
        return OrderSnapshot(
            **self.metadata("fake_oms", self.order_id),
            external_order_id=self.order_id,
            customer_reference=None,
            status=self.order_status,
            items=[],
            created_at=T0,
        )

    async def get_parcels(self, identity, *, request_id=None):
        await self.called("get_parcels", identity)
        return [
            ParcelSnapshot(
                **self.metadata("fake_oms", p),
                parcel_id=p,
                external_order_id=self.order_id,
                carrier="demo",
                tracking_number=None if self.no_tracking else "TRACK-" + p,
            )
            for p in self.parcel_ids
        ]

    async def get_shipment(self, parcel, *, request_id=None):
        await self.called("get_shipment", parcel.parcel_id)
        event = ShipmentEvent(
            **self.metadata("fake_logistics", parcel.parcel_id + "-event"),
            parcel_id=parcel.parcel_id,
            status=self.shipment_status,
            description="Untrusted carrier description",
            occurred_at=T0,
        )
        events = [event]
        if self.duplicate_events:
            events.append(event)
        if self.event_conflict:
            events.append(
                ShipmentEvent(
                    **self.metadata("fake_logistics", parcel.parcel_id + "-other"),
                    parcel_id=parcel.parcel_id,
                    status="DELIVERED",
                    description="Different observation",
                    occurred_at=T0,
                )
            )
        return ShipmentSnapshot(
            **self.metadata("fake_logistics", parcel.parcel_id),
            parcel_id=parcel.parcel_id,
            status=self.shipment_status,
            events=events,
        )

    async def get_notes(self, identity, *, request_id=None):
        await self.called("get_notes", identity)
        return [
            WarehouseNoteSnapshot(
                **self.metadata("fake_warehouse", "N1"),
                external_order_id=self.order_id,
                note_id="N1",
                note_text=self.note_text,
                created_at=T0,
            )
        ]

    async def send_reply(self, *args, **kwargs):
        raise AssertionError("Stage 4 must never send a reply")
