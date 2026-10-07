import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import anyio
import pytest
from resolution_support import T0
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    AuthSession,
    ContextVersion,
    Inquiry,
    ResolutionRun,
    SourceFetch,
    User,
)
from app.integrations.errors import (
    ExternalConflict,
    ExternalInvalidResponse,
    ExternalNotFound,
    ExternalRejected,
    ExternalTimeout,
    ExternalUnavailable,
)
from app.services.context_models import FreshnessPolicy
from app.services.resolution import ResolutionService

pytestmark = pytest.mark.database


def login(client, password, username="agent.a"):
    result = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["token"]}


def resolve(client, inquiry_id, headers, *, key="key-1", version=1, body=None):
    return client.post(
        f"/api/v1/inquiries/{inquiry_id}/resolve-context",
        headers={**headers, "Idempotency-Key": key},
        json=body if body is not None else {"expected_lock_version": version},
    )


def context(client, inquiry_id, headers, response):
    result = client.get(
        f"/api/v1/inquiries/{inquiry_id}/contexts/{response.json()['context_id']}", headers=headers
    )
    assert result.status_code == 200, result.text
    return result.json()


def counts(engine):
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ResolutionRun, SourceFetch, ContextVersion)
        )


def test_success_persists_atomic_chain_and_order_read_preserves_original_times(resolution_app):
    client, app, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    response = resolve(client, inquiry, headers)
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "SUCCEEDED" and response.json()["quality"] == "COMPLETE"
    assert response.json()["lock_version"] == 3
    body = context(client, inquiry, headers, response)
    assert body["is_current"] is True and body["context"]["context_version"] == 1
    before = list(fake.calls)
    order_url = f"/api/v1/inquiries/{inquiry}/order"
    order = client.get(order_url, headers=headers)
    assert order.status_code == 200 and set(order.json()["freshness"].values()) == {"FRESH"}
    app.state.clock = type(app.state.clock)(T0 + timedelta(hours=1))
    stale = client.get(order_url, headers=headers)
    assert stale.status_code == 200 and set(stale.json()["freshness"].values()) == {"STALE"}
    assert stale.json()["order"] == order.json()["order"]
    assert fake.calls == before
    with Session(engine) as session:
        item = session.get(Inquiry, inquiry)
        run = session.get(ResolutionRun, item.latest_run_id)
        assert item.current_context_id == UUID(response.json()["context_id"])
        assert run.finished_at == T0 and run.version == 1
        assert all(
            f.request_id == response.headers["x-request-id"]
            for f in session.scalars(select(SourceFetch))
        )
        assert {a.event_type for a in session.scalars(select(AuditLog))} >= {
            "RESOLVE_STARTED",
            "RESOLVE_SUCCEEDED",
        }
    assert counts(engine) == (1, 5, 1)


def test_completed_replay_precedes_stale_version_and_terminal_state(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    first = resolve(client, inquiry, headers)
    calls = list(fake.calls)
    with Session(engine) as session, session.begin():
        record = session.get(Inquiry, inquiry)
        record.state = "ESCALATED"
        record.escalation_reason = "Synthetic terminal state for replay verification"
    replay = resolve(client, inquiry, headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert fake.calls == calls and counts(engine) == (1, 5, 1)
    assert (
        resolve(client, inquiry, headers, version=3).json()["error"]["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    assert (
        resolve(client, inquiry, headers, key="new", version=3).json()["error"]["code"]
        == "STATE_CONFLICT"
    )


@pytest.mark.parametrize(
    "username,allowed",
    [
        ("agent.a", True),
        ("supervisor", True),
        ("agent.b", False),
        ("agent.c", False),
        ("admin", False),
    ],
)
def test_role_scope_is_server_owned_on_every_resolution_read(resolution_app, username, allowed):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password, username)
    response = resolve(client, ids["INQ-DEMO-002"], headers)
    assert response.status_code == (201 if allowed else 403), response.text
    if allowed:
        context(client, ids["INQ-DEMO-002"], headers, response)
    else:
        assert fake.calls == [] and counts(engine) == (0, 0, 0)
        unknown = resolve(client, uuid4(), headers)
        assert unknown.json()["error"]["code"] == response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "body",
    [
        {"expected_lock_version": True},
        {"expected_lock_version": "1"},
        {"expected_lock_version": 0},
        {"expected_lock_version": 1, "role": "ADMIN"},
        {"expected_lock_version": 1, "external_order_id": "ORD-OTHER"},
        {"expected_lock_version": 1, "team_id": str(uuid4())},
    ],
)
def test_closed_input_rejects_forged_identity_without_calls_or_run(resolution_app, body):
    client, _, engine, password, ids, fake = resolution_app
    response = resolve(client, ids["INQ-DEMO-002"], login(client, password), body=body)
    assert response.status_code == 422, response.text
    assert fake.calls == [] and counts(engine) == (0, 0, 0)


@pytest.mark.parametrize(
    "mode", ["duplicate_json", "duplicate_key", "missing_key", "blank_key", "query", "wrong_type"]
)
def test_http_input_boundary_checks_before_providers(resolution_app, mode):
    client, _, engine, password, ids, fake = resolution_app
    url = f"/api/v1/inquiries/{ids['INQ-DEMO-002']}/resolve-context"
    headers = {
        **login(client, password),
        "Idempotency-Key": "one",
        "Content-Type": "application/json",
    }
    body = b'{"expected_lock_version":1}'
    if mode == "duplicate_json":
        body = b'{"expected_lock_version":1,"expected_lock_version":1}'
    elif mode == "duplicate_key":
        headers = list(headers.items()) + [("Idempotency-Key", "two")]
    elif mode == "missing_key":
        del headers["Idempotency-Key"]
    elif mode == "blank_key":
        headers["Idempotency-Key"] = "  "
    elif mode == "query":
        url += "?order_id=forged"
    else:
        headers["Content-Type"] = "text/plain"
    response = client.post(url, headers=headers, content=body)
    assert response.status_code == 422, response.text
    assert fake.calls == [] and counts(engine) == (0, 0, 0)


@pytest.mark.parametrize("mode", ["expired", "revoked", "inactive"])
def test_invalid_sessions_never_start_provider_reads(resolution_app, mode):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    with Session(engine) as session, session.begin():
        auth = session.scalar(select(AuthSession))
        if mode == "expired":
            auth.created_at = T0 - timedelta(seconds=1)
            auth.expires_at = T0
        elif mode == "revoked":
            auth.revoked_at = T0
        else:
            session.get(User, auth.user_id).is_active = False
    response = resolve(client, ids["INQ-DEMO-002"], headers, body={"role": "ADMIN"})
    assert response.status_code == 401
    assert fake.calls == [] and counts(engine) == (0, 0, 0)


def test_missing_binding_zero_calls_and_stale_version_conflict(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    with Session(engine) as session, session.begin():
        session.get(Inquiry, inquiry).external_order_id = None
    assert resolve(client, inquiry, headers).json()["error"]["code"] == "ORDER_REFERENCE_MISSING"
    assert (
        resolve(client, inquiry, headers, version=2).json()["error"]["code"] == "VERSION_CONFLICT"
    )
    assert client.get(f"/api/v1/inquiries/{inquiry}/order", headers=headers).status_code == 422
    assert fake.calls == [] and counts(engine) == (0, 0, 0)


@pytest.mark.parametrize("fault", ["binding", "not_found", "timeout"])
def test_required_source_failure_safe_error_replay_and_no_context(resolution_app, fault):
    client, _, engine, password, ids, fake = resolution_app
    if fault == "binding":
        fake.order_id = "ORD-WRONG"
        code, status = "SOURCE_BINDING_MISMATCH", 502
    else:
        fake.faults["get_order"] = (ExternalNotFound if fault == "not_found" else ExternalTimeout)(
            service="fake", operation="order", request_id="req-safe"
        )
        code, status = (
            ("SOURCE_NOT_FOUND", 404) if fault == "not_found" else ("SOURCE_TIMEOUT", 504)
        )
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    response = resolve(client, inquiry, headers)
    assert response.status_code == status and response.json()["error"]["code"] == code
    before = list(fake.calls)
    replay = resolve(client, inquiry, headers)
    assert replay.status_code == status and replay.json()["error"]["code"] == code
    assert fake.calls == before
    with Session(engine) as session:
        record = session.get(Inquiry, inquiry)
        run = session.get(ResolutionRun, record.latest_run_id)
        assert record.state == "OPEN" and record.current_context_id is None
        assert run.state == "FAILED" and run.error_code == code
    assert counts(engine)[2] == 0


def test_new_failed_resolution_invalidates_old_current_and_keeps_history(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    first = resolve(client, inquiry, headers)

    def observe_begin(name, target):
        if name == "get_inquiry":
            with Session(engine) as session:
                record = session.get(Inquiry, inquiry)
                assert record.current_context_id is None and record.state == "OPEN"

    fake.hook = observe_begin
    fake.faults["get_order"] = ExternalNotFound(
        service="fake", operation="order", request_id="req-safe"
    )
    second = resolve(client, inquiry, headers, key="key-2", version=3)
    assert second.status_code == 404
    historical = context(client, inquiry, headers, first)
    assert historical["is_current"] is False
    assert (
        client.get(f"/api/v1/inquiries/{inquiry}/order", headers=headers).json()["error"]["code"]
        == "CONTEXT_REQUIRED"
    )
    with Session(engine) as session:
        assert [
            r.version
            for r in session.scalars(select(ResolutionRun).order_by(ResolutionRun.version))
        ] == [1, 2]
    assert counts(engine)[2] == 1


@pytest.mark.parametrize(
    "change", ["revoke", "inactive", "reassign", "team", "binding", "version", "identity"]
)
def test_permission_and_binding_rechecked_after_network_discards_payload(resolution_app, change):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]

    def mutate(name, target):
        if name != "get_notes":
            return
        with Session(engine) as session, session.begin():
            record = session.get(Inquiry, inquiry)
            agent = session.scalar(select(User).where(User.username == "agent.a"))
            if change == "revoke":
                session.scalar(select(AuthSession)).revoked_at = T0
            elif change == "inactive":
                agent.is_active = False
            elif change == "reassign":
                record.assigned_agent_id = session.scalar(
                    select(User.id).where(User.username == "agent.b")
                )
            elif change == "team":
                record.team_id = session.scalar(
                    select(User.team_id).where(User.username == "agent.c")
                )
            elif change == "binding":
                record.external_order_id = "ORD-CHANGED"
            elif change == "identity":
                session.scalar(select(AuthSession)).user_id = session.scalar(
                    select(User.id).where(User.username == "supervisor")
                )
            else:
                record.lock_version += 1

    fake.hook = mutate
    response = resolve(client, inquiry, headers)
    expected = (
        401
        if change in {"revoke", "inactive"}
        else 403
        if change in {"reassign", "team"}
        else 502
        if change == "binding"
        else 409
    )
    assert response.status_code == expected, response.text
    assert counts(engine) == (1, 0, 0)
    with Session(engine) as session:
        assert session.scalar(select(ResolutionRun)).state == "FAILED"
        assert session.get(Inquiry, inquiry).current_context_id is None


def test_concurrent_same_key_running_replay_and_other_key_busy(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    reached, release = Event(), Event()

    async def block(name, target):
        if name == "get_order":
            reached.set()
            assert await anyio.to_thread.run_sync(release.wait, 15)

    fake.hook = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolve, client, inquiry, headers)
        try:
            assert reached.wait(10)
            replay = resolve(client, inquiry, headers)
            assert (
                replay.status_code == 202
                and replay.headers["location"] == replay.json()["query_url"]
            )
            run = client.get(replay.json()["query_url"], headers=headers)
            assert run.status_code == 200 and run.json()["state"] == "RUNNING"
            assert resolve(client, inquiry, headers, key="other").json()["error"]["code"] == "BUSY"
            assert (
                resolve(client, inquiry, headers, version=2).json()["error"]["code"]
                == "IDEMPOTENCY_CONFLICT"
            )
        finally:
            release.set()
        assert first.result(10).status_code == 201
    assert fake.calls.count(("get_order", fake.order_id)) == 1
    assert counts(engine) == (1, 5, 1)


def test_interrupted_commit_rolls_back_and_operator_recovery_releases_exact_run(
    resolution_app, monkeypatch
):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    original = ResolutionService.audit

    def failure(self, session, record, actor, event, record_id, **metadata):
        if event == "RESOLVE_SUCCEEDED":
            raise RuntimeError("Simulated commit failure")
        return original(self, session, record, actor, event, record_id, **metadata)

    monkeypatch.setattr(ResolutionService, "audit", failure)
    response = resolve(client, inquiry, headers)
    assert response.status_code == 500
    assert counts(engine) == (1, 0, 0)
    replay = resolve(client, inquiry, headers)
    assert replay.status_code == 202
    run_id = UUID(replay.json()["run_id"])
    service = ResolutionService(
        lambda: Session(engine),
        type(resolution_app[1].state.clock)(T0),
        "operator-test",
        FreshnessPolicy(),
    )
    with pytest.raises(ValueError):
        service.recover(inquiry, run_id, 1)
    service.recover(inquiry, run_id, 2)
    with pytest.raises(ValueError):
        service.recover(inquiry, run_id, 2)
    with Session(engine) as session:
        run = session.get(ResolutionRun, run_id)
        assert run.state == "FAILED" and run.error_code == "INTERRUPTED"
        assert session.get(Inquiry, inquiry).lock_version == 3
        assert session.scalar(select(AuditLog).where(AuditLog.event_type == "OPERATION_RECOVERED"))
    assert resolve(client, inquiry, headers).json()["error"]["code"] == "INTERRUPTED"
    monkeypatch.setattr(ResolutionService, "audit", original)
    assert resolve(client, inquiry, headers, key="new", version=3).status_code == 201


def test_context_foreign_parent_read_and_reauthorization(resolution_app):
    client, _, engine, password, ids, _ = resolution_app
    headers = login(client, password)
    response = resolve(client, ids["INQ-DEMO-002"], headers)
    result = client.get(
        f"/api/v1/inquiries/{ids['INQ-DEMO-003']}/contexts/{response.json()['context_id']}",
        headers=headers,
    )
    assert result.status_code == 404
    result = client.get(
        f"/api/v1/inquiries/{ids['INQ-DEMO-003']}/runs/{response.json()['run_id']}", headers=headers
    )
    assert result.status_code == 404
    other = login(client, password, "agent.b")
    url = f"/api/v1/inquiries/{ids['INQ-DEMO-002']}/contexts/{response.json()['context_id']}"
    assert client.get(url, headers=other).status_code == 403
    with Session(engine) as session, session.begin():
        session.get(Inquiry, ids["INQ-DEMO-002"]).assigned_agent_id = session.scalar(
            select(User.id).where(User.username == "agent.b")
        )
    assert client.get(url, headers=headers).status_code == 403
    assert client.get(url, headers=other).json()["is_current"] is False


def test_persisted_context_tamper_and_cross_inquiry_run_relationship_fail_closed(resolution_app):
    client, _, engine, password, ids, _ = resolution_app
    headers = login(client, password)
    response = resolve(client, ids["INQ-DEMO-002"], headers)
    context_id = UUID(response.json()["context_id"])
    with Session(engine) as session, session.begin():
        # Only this generated test schema: simulate privileged corruption of immutable records.
        session.execute(text("ALTER TABLE context_versions DISABLE TRIGGER USER"))
        record = session.get(ContextVersion, context_id)
        data = json.loads(json.dumps(record.payload))
        data["evidence"][0]["value"] = "FORGED"
        record.payload = data
    url = f"/api/v1/inquiries/{ids['INQ-DEMO-002']}/contexts/{context_id}"
    result = client.get(url, headers=headers)
    assert result.status_code == 500 and "FORGED" not in result.text


def test_idempotency_cors_preflight_and_unknown_origin_zero_reads(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    url = f"/api/v1/inquiries/{ids['INQ-DEMO-002']}/resolve-context"
    allowed = client.options(
        url,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert allowed.status_code == 200
    assert "idempotency-key" in allowed.headers["access-control-allow-headers"].lower()
    denied = client.post(
        url,
        headers={**login(client, password), "Origin": "http://untrusted.test"},
        json={"expected_lock_version": 1},
    )
    assert denied.status_code == 403 and fake.calls == [] and counts(engine) == (0, 0, 0)


@pytest.mark.parametrize(
    "fault,outcome",
    [
        (ExternalNotFound, "NOT_FOUND"),
        (ExternalTimeout, "TIMEOUT"),
        (ExternalUnavailable, "UNAVAILABLE"),
        (ExternalInvalidResponse, "INVALID_RESPONSE"),
        (ExternalRejected, "REJECTED"),
        (ExternalConflict, "CONFLICT"),
    ],
)
def test_optional_fault_classification_and_safe_audit(resolution_app, fault, outcome):
    client, _, engine, password, ids, fake = resolution_app
    fake.parcel_ids = ["P1", "P2"]
    fake.faults[("get_shipment", "P2")] = fault(
        service="fake", operation="shipment", request_id="req-safe"
    )
    headers = login(client, password)
    result = resolve(client, ids["INQ-DEMO-002"], headers)
    assert result.status_code == 201 and result.json()["state"] == "PARTIAL"
    case = context(client, ids["INQ-DEMO-002"], headers, result)["context"]
    assert [p["parcel_id"] for p in case["parcels"]] == ["P1", "P2"]
    assert case["parcels"][0]["shipment_status_evidence_id"]
    assert case["parcels"][1]["shipment_status_evidence_id"] is None
    assert any(f["outcome"] == outcome and f["target_id"] == "P2" for f in case["source_outcomes"])
    with Session(engine) as session:
        assert session.scalar(select(AuditLog).where(AuditLog.event_type == "RESOLVE_PARTIAL"))


def test_new_success_increments_context_and_historical_replay_cannot_become_current(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    headers = login(client, password)
    inquiry = ids["INQ-DEMO-002"]
    first = resolve(client, inquiry, headers)
    second = resolve(client, inquiry, headers, key="second", version=3)
    assert second.status_code == 201 and second.json()["context_version"] == 2
    assert second.json()["lock_version"] == 5
    assert context(client, inquiry, headers, first)["is_current"] is False
    assert context(client, inquiry, headers, second)["is_current"] is True
    before = list(fake.calls)
    replay = resolve(client, inquiry, headers)
    assert replay.status_code == 200 and replay.json()["context_id"] == first.json()["context_id"]
    assert replay.json()["lock_version"] == 5 and fake.calls == before
    assert context(client, inquiry, headers, first)["is_current"] is False


def test_cross_inquiry_run_corruption_rejected_even_when_foreign_keys_are_valid(resolution_app):
    client, _, engine, password, ids, _ = resolution_app
    headers = login(client, password)
    first = resolve(client, ids["INQ-DEMO-002"], headers)
    with Session(engine) as session, session.begin():
        session.get(ResolutionRun, UUID(first.json()["run_id"])).inquiry_id = ids["INQ-DEMO-003"]
    result = client.get(
        f"/api/v1/inquiries/{ids['INQ-DEMO-002']}/contexts/{first.json()['context_id']}",
        headers=headers,
    )
    assert result.status_code == 500


def test_another_authorized_actor_cannot_replay_someone_elses_operation(resolution_app):
    client, _, engine, password, ids, fake = resolution_app
    inquiry = ids["INQ-DEMO-002"]
    first = resolve(client, inquiry, login(client, password))
    assert first.status_code == 201
    calls = list(fake.calls)
    replay = resolve(client, inquiry, login(client, password, "supervisor"))
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert fake.calls == calls and counts(engine) == (1, 5, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_order_id", "ORD-CHANGED"),
        ("external_inquiry_id", "INQ-CHANGED"),
        ("source_system", "changed_support"),
    ],
)
def test_changed_binding_makes_previous_context_historical_even_if_pointer_remains(
    resolution_app, field, value
):
    client, _, engine, password, ids, fake = resolution_app
    inquiry = ids["INQ-DEMO-002"]
    headers = login(client, password)
    first = resolve(client, inquiry, headers)
    before = list(fake.calls)
    with Session(engine) as session, session.begin():
        setattr(session.get(Inquiry, inquiry), field, value)
    assert context(client, inquiry, headers, first)["is_current"] is False
    order = client.get(f"/api/v1/inquiries/{inquiry}/order", headers=headers)
    assert order.status_code == 409 and order.json()["error"]["code"] == "CONTEXT_REQUIRED"
    assert fake.calls == before
