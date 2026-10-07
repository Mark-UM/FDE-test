"""Sequential Provider reads outside Product transactions; immutable canonical batches."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.core.clock import Clock
from app.integrations.errors import (
    ExternalConflict,
    ExternalError,
    ExternalInvalidResponse,
    ExternalNotFound,
    ExternalRejected,
    ExternalTimeout,
    ExternalUnavailable,
)
from app.integrations.models import (
    OrderSnapshot,
    ParcelSnapshot,
    ShipmentSnapshot,
    SupportInquiry,
    WarehouseNoteSnapshot,
)
from app.integrations.providers import (
    LogisticsProvider,
    MessageProvider,
    OrderProvider,
    WarehouseProvider,
)

ERROR_OUTCOMES = {
    ExternalNotFound: "NOT_FOUND",
    ExternalTimeout: "TIMEOUT",
    ExternalUnavailable: "UNAVAILABLE",
    ExternalInvalidResponse: "INVALID_RESPONSE",
    ExternalRejected: "REJECTED",
    ExternalConflict: "CONFLICT",
}
ERROR_STATUS = {
    "SOURCE_NOT_FOUND": 404,
    "SOURCE_TIMEOUT": 504,
    "SOURCE_UNAVAILABLE": 503,
    "SOURCE_INVALID_RESPONSE": 502,
    "SOURCE_REJECTED": 502,
    "SOURCE_CONFLICT": 409,
    "SOURCE_BINDING_MISMATCH": 502,
    "INTERRUPTED": 500,
}


@dataclass(frozen=True)
class Providers:
    orders: OrderProvider
    logistics: LogisticsProvider
    warehouse: WarehouseProvider
    messages: MessageProvider


@dataclass(frozen=True)
class Fetch:
    id: UUID
    operation: str
    target_id: str
    outcome: str
    request_id: str
    started_at: datetime
    completed_at: datetime
    fetched_at: datetime | None
    error_code: str | None
    source_system: str
    canonical_payload: dict | None


@dataclass(frozen=True)
class Collection:
    fetches: list[Fetch]
    error_code: str | None


class BindingMismatch(Exception):
    pass


async def collect_sources(
    providers: Providers,
    clock: Clock,
    request_id: str,
    inquiry_id: str,
    order_id: str,
    inquiry_source: str,
) -> Collection:
    fetches: list[Fetch] = []

    async def fetch(operation, target, source, call, model, *, array=False, binding=None):
        started = clock.now()
        records = None
        error = None
        try:
            result = await call()
            if array and not isinstance(result, list):
                raise ValueError("Expected canonical array")
            records = result if array else [result]
            # Validate again at the protocol boundary, including mutated nested arrays.
            records = [model.model_validate_json(record.model_dump_json()) for record in records]
            if binding is not None:
                binding(records)
            outcome = "SUCCESS" if records else "EMPTY"
        except BindingMismatch:
            outcome, error, records = "INVALID_RESPONSE", "SOURCE_BINDING_MISMATCH", None
        except ExternalError as exc:
            outcome = next((v for k, v in ERROR_OUTCOMES.items() if isinstance(exc, k)), None)
            if outcome is None:
                raise
            error, records = "SOURCE_" + outcome, None
        except (ValidationError, ValueError, AttributeError, TypeError):
            outcome, error, records = "INVALID_RESPONSE", "SOURCE_INVALID_RESPONSE", None
        completed = clock.now()
        source_name = (
            records[0].source_system if records and hasattr(records[0], "source_system") else source
        )
        fetches.append(
            Fetch(
                id=uuid4(),
                operation=operation,
                target_id=target,
                outcome=outcome,
                request_id=request_id,
                started_at=started,
                completed_at=completed,
                fetched_at=completed if records is not None else None,
                error_code=outcome if error else None,
                source_system=source_name,
                canonical_payload={"records": [item.model_dump(mode="json") for item in records]}
                if records is not None
                else None,
            )
        )
        return records, error

    def inquiry_binding(records):
        if records[0].inquiry_id != inquiry_id or records[0].external_order_id != order_id:
            raise BindingMismatch

    _, error = await fetch(
        "get_inquiry",
        inquiry_id,
        inquiry_source,
        lambda: providers.messages.get_inquiry(inquiry_id, request_id=request_id),
        SupportInquiry,
        binding=inquiry_binding,
    )
    if error:
        return Collection(fetches, error)

    def order_binding(records):
        if records[0].external_order_id != order_id:
            raise BindingMismatch

    _, error = await fetch(
        "get_order",
        order_id,
        "demo_oms",
        lambda: providers.orders.get_order(order_id, request_id=request_id),
        OrderSnapshot,
        binding=order_binding,
    )
    if error:
        return Collection(fetches, error)

    def parcel_binding(records):
        if any(item.external_order_id != order_id for item in records):
            raise BindingMismatch
        if len({item.parcel_id for item in records}) != len(records):
            raise ValueError("Duplicate parcel identity")

    parcels, error = await fetch(
        "get_parcels",
        order_id,
        "demo_oms",
        lambda: providers.orders.get_parcels(order_id, request_id=request_id),
        ParcelSnapshot,
        array=True,
        binding=parcel_binding,
    )
    if error:
        return Collection(fetches, error)
    for parcel in parcels:
        if parcel.tracking_number is None:
            now = clock.now()
            fetches.append(
                Fetch(
                    uuid4(),
                    "get_shipment",
                    parcel.parcel_id,
                    "NO_TRACKING",
                    request_id,
                    now,
                    now,
                    None,
                    "NO_TRACKING",
                    "demo_logistics",
                    None,
                )
            )
            continue

        def shipment_binding(records, expected=parcel.parcel_id):
            shipment = records[0]
            if shipment.parcel_id != expected or any(
                e.parcel_id != expected for e in shipment.events
            ):
                raise BindingMismatch
            if len({e.source_record_id for e in shipment.events}) != len(shipment.events):
                raise ValueError("Duplicate event identity")

        await fetch(
            "get_shipment",
            parcel.parcel_id,
            "demo_logistics",
            lambda parcel=parcel: providers.logistics.get_shipment(parcel, request_id=request_id),
            ShipmentSnapshot,
            binding=shipment_binding,
        )

    def note_binding(records):
        if any(item.external_order_id != order_id for item in records):
            raise BindingMismatch
        if len({item.note_id for item in records}) != len(records):
            raise ValueError("Duplicate note identity")

    await fetch(
        "get_notes",
        order_id,
        "demo_warehouse",
        lambda: providers.warehouse.get_notes(order_id, request_id=request_id),
        WarehouseNoteSnapshot,
        array=True,
        binding=note_binding,
    )
    return Collection(fetches, None)
