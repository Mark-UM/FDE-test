"""Real HTTP only: a running, independent DemoCommerce S0-S1 is required."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.core.clock import FixedClock
from app.core.config import Settings
from app.integrations.errors import (
    ExternalConflict,
    ExternalNotFound,
    ExternalTimeout,
    ExternalUnavailable,
)
from app.integrations.models import SupportReplyCommand
from app.integrations.sandbox.client import SandboxClient
from app.integrations.sandbox.providers import (
    SandboxLogisticsProvider,
    SandboxMessageProvider,
    SandboxOrderProvider,
    SandboxWarehouseProvider,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
T0 = datetime(2026, 9, 20, 6, tzinfo=UTC)


@pytest.fixture
async def client(request):
    url = request.config.getoption("--sandbox-url")
    if not url:
        pytest.skip("Supply --sandbox-url to run real HTTP integration tests")
    # No fallback to mocks, imports, auto-reset or direct database access.
    settings = Settings(_env_file=None, sandbox_base_url=url)
    async with SandboxClient(settings, FixedClock(T0)) as client:
        response = await client.http.get("/health")
        response.raise_for_status()
        assert response.json()["database"] == "ok"
        yield client


async def shipment(client, number):
    parcels = await SandboxOrderProvider(client).get_parcels(f"ORD-DEMO-{number:03}")
    return await SandboxLogisticsProvider(client).get_shipment(parcels[0])


async def test_s02_in_transit_and_source_timestamps(client):
    orders = SandboxOrderProvider(client)
    order = await orders.get_order("ORD-DEMO-002")
    raw = (await client.http.get("/api/oms/orders/ORD-DEMO-002")).json()
    assert order.external_order_id == raw["order_id"]
    assert order.customer_reference == raw["customer_reference"]
    assert order.status == raw["status"] == "SHIPPED"
    assert order.items[0].model_dump() == raw["items"][0]
    assert order.source_updated_at == T0 - timedelta(minutes=30)
    assert order.model_dump(mode="json")["source_updated_at"] == raw["updated_at"]
    assert order.created_at == T0 - timedelta(days=5)
    assert order.fetched_at == T0
    result = await shipment(client, 2)
    assert result.status == "IN_TRANSIT"
    assert [e.status for e in result.events] == ["PICKED_UP", "IN_TRANSIT"]
    assert result.source_updated_at == T0 - timedelta(minutes=10)
    assert result.fetched_at == T0
    assert all(e.fetched_at == T0 and e.source_updated_at is None for e in result.events)
    assert result.events[0].source_record_id == "EVT-DEMO-002-1-1"


async def test_s04_all_parcels_map_independently(client):
    parcels = await SandboxOrderProvider(client).get_parcels("ORD-DEMO-004")
    assert [p.parcel_id for p in parcels] == ["PAR-DEMO-004-1", "PAR-DEMO-004-2"]
    assert all(p.external_order_id == "ORD-DEMO-004" for p in parcels)
    assert all(p.fetched_at == T0 and p.source_updated_at is None for p in parcels)
    shipments = [await SandboxLogisticsProvider(client).get_shipment(p) for p in parcels]
    assert [s.parcel_id for s in shipments] == [p.parcel_id for p in parcels]
    assert [s.status for s in shipments] == ["IN_TRANSIT", "NOT_COLLECTED"]


async def test_s06_tentative_warehouse_wording_is_unchanged(client):
    notes = await SandboxWarehouseProvider(client).get_notes("ORD-DEMO-006")
    result = await shipment(client, 6)
    assert notes[0].note_text == "Expected to ship today, subject to carrier collection."
    assert notes[0].fetched_at == T0
    assert notes[0].source_updated_at == T0 - timedelta(minutes=20)
    assert result.status == "NOT_COLLECTED"
    assert [e.status for e in result.events] == ["LABEL_CREATED"]


async def test_s07_old_timestamp_is_not_replaced_by_fetch_time(client):
    result = await shipment(client, 7)
    assert result.source_updated_at == datetime(2026, 9, 17, 6, tzinfo=UTC)
    assert result.fetched_at == T0
    assert result.events[-1].occurred_at == result.source_updated_at


async def test_s08_504_is_timeout_other_sources_work(client):
    parcels = await SandboxOrderProvider(client).get_parcels("ORD-DEMO-008")
    with pytest.raises(ExternalTimeout) as failure:
        await SandboxLogisticsProvider(client).get_shipment(parcels[0], request_id="product-s08")
    assert failure.value.status_code == 504
    assert failure.value.source_code == "LOGISTICS_TEMPORARILY_UNAVAILABLE"
    assert failure.value.request_id == "product-s08"
    assert failure.value.service == "demo_logistics"
    assert (await SandboxOrderProvider(client).get_order("ORD-DEMO-008")).status == "SHIPPED"
    assert await SandboxWarehouseProvider(client).get_notes("ORD-DEMO-008")


async def test_s09_503_is_unavailable_logistics_still_works(client):
    with pytest.raises(ExternalUnavailable) as failure:
        await SandboxWarehouseProvider(client).get_notes("ORD-DEMO-009")
    assert failure.value.status_code == 503
    assert failure.value.source_code == "WAREHOUSE_TEMPORARILY_UNAVAILABLE"
    assert failure.value.request_id
    assert (await shipment(client, 9)).status == "IN_TRANSIT"


async def test_s10_missing_logistics_is_404_not_empty_success(client):
    # Actual S0-S1 S10 semantics differ from the original task's empty-result label.
    parcels = await SandboxOrderProvider(client).get_parcels("ORD-DEMO-010")
    assert len(parcels) == 1
    with pytest.raises(ExternalNotFound) as failure:
        await SandboxLogisticsProvider(client).get_shipment(parcels[0])
    assert failure.value.status_code == 404
    assert failure.value.source_code == "SHIPMENT_NOT_FOUND"


async def test_s01_successful_empty_parcels_remain_empty(client):
    assert await SandboxOrderProvider(client).get_parcels("ORD-DEMO-001") == []


async def test_s05_unknown_note_update_remains_null(client):
    notes = await SandboxWarehouseProvider(client).get_notes("ORD-DEMO-005")
    assert notes[0].source_updated_at is None
    assert notes[0].fetched_at == T0


async def test_s11_both_conflicting_source_records_survive(client):
    notes = await SandboxWarehouseProvider(client).get_notes("ORD-DEMO-011")
    result = await shipment(client, 11)
    assert notes[0].note_text == "Parcel has not been handed to the carrier today."
    assert result.status == "PICKED_UP"
    assert result.events[0].description == "Collected by carrier."


async def test_s12_unknown_order_but_existing_inquiry(client):
    with pytest.raises(ExternalNotFound) as failure:
        await SandboxOrderProvider(client).get_order("ORD-DEMO-012")
    assert failure.value.source_code == "ORDER_NOT_FOUND"
    assert failure.value.status_code == 404
    inquiry = await SandboxMessageProvider(client).get_inquiry("INQ-DEMO-012")
    assert inquiry.external_order_id == "ORD-DEMO-012"
    assert inquiry.created_at == T0


async def test_message_idempotency_through_provider(client):
    provider = SandboxMessageProvider(client)
    key = f"product-test-{uuid4()}"
    command = SupportReplyCommand(
        inquiry_id="INQ-DEMO-002",
        reply_text="  Integration test reply.  ",
        idempotency_key=key,
    )
    first = await provider.send_reply(command)
    assert await provider.send_reply(command) == first
    assert first.status == "SENT"
    assert first.sent_at.utcoffset() == timedelta(0)
    with pytest.raises(ExternalConflict) as failure:
        await provider.send_reply(command.model_copy(update={"reply_text": "Different text"}))
    assert failure.value.status_code == 409
    assert failure.value.source_code == "IDEMPOTENCY_CONFLICT"
    second = await provider.send_reply(command.model_copy(update={"idempotency_key": key + "-new"}))
    assert second.reply_id != first.reply_id
    # Idempotency scope includes the inquiry, not just the key.
    third = await provider.send_reply(command.model_copy(update={"inquiry_id": "INQ-DEMO-003"}))
    assert third.reply_id not in (first.reply_id, second.reply_id)


async def test_live_openapi_matches_inspected_routes(client):
    # Detect using a different service/contract without importing external source code.
    async with httpx.AsyncClient(base_url=str(client.http.base_url), trust_env=False) as http:
        response = await http.get("/openapi.json")
    response.raise_for_status()
    paths = response.json()["paths"]
    assert "/api/oms/orders/{order_id}/parcels" in paths
    assert "/api/logistics/shipments/{tracking_number}/events" in paths
    assert "/api/support/inquiries/{inquiry_id}/replies" in paths
