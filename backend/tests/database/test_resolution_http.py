"""Cross-system acceptance: real Sandbox HTTP and isolated Product PostgreSQL."""

from datetime import timedelta
from uuid import UUID

import pytest
from resolution_support import T0
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_resolution import context, login, resolve

from app.core.config import Settings
from app.db.models import Inquiry, ResolutionRun, SourceFetch

pytestmark = [pytest.mark.database, pytest.mark.integration]


@pytest.mark.parametrize(
    "number,state,outcome,missing",
    [
        (1, "SUCCEEDED", "EMPTY", "NO_PARCELS"),
        (2, "SUCCEEDED", None, None),
        (4, "SUCCEEDED", None, None),
        (5, "SUCCEEDED", "EMPTY", "NO_PARCELS"),
        (6, "SUCCEEDED", None, None),
        (7, "SUCCEEDED", None, None),
        (8, "PARTIAL", "TIMEOUT", "SOURCE_TIMEOUT"),
        (9, "PARTIAL", "UNAVAILABLE", "SOURCE_UNAVAILABLE"),
        (10, "PARTIAL", "NOT_FOUND", "SOURCE_NOT_FOUND"),
        (11, "SUCCEEDED", None, None),
        (12, "FAILED", "NOT_FOUND", None),
    ],
)
def test_real_resolver_scenarios(resolution_app, request, number, state, outcome, missing):
    url = request.config.getoption("--sandbox-url")
    if not url:
        pytest.skip("Explicit independent Sandbox origin required")
    client, app, engine, password, ids, fake = resolution_app
    # Use create_app's production provider factory, with real network transport.
    from app.main import create_app

    live = create_app(
        Settings(_env_file=None, app_env="test", sandbox_base_url=url),
        session_factory=app.state.session_factory,
        clock=app.state.clock,
    )
    app.state.provider_factory = live.state.provider_factory
    username = "agent.b" if number == 11 else "agent.c" if number == 12 else "agent.a"
    headers = login(client, password, username)
    inquiry = ids[f"INQ-DEMO-{number:03}"]
    response = resolve(client, inquiry, headers)
    assert fake.calls == []
    assert response.status_code == (404 if number == 12 else 201), response.text
    with Session(engine) as session:
        run = session.scalar(select(ResolutionRun))
        assert run.state == state and run.version == 1 and run.finished_at == T0
        assert session.get(Inquiry, inquiry).lock_version == 3
        sources = session.scalars(select(SourceFetch)).all()
        if outcome:
            assert any(f.outcome == outcome for f in sources)
        for f in sources:
            if f.outcome not in {"SUCCESS", "EMPTY"}:
                assert f.canonical_payload is None and f.fetched_at is None
                assert f.error_code == f.outcome
        if number == 12:
            assert run.error_code == "SOURCE_NOT_FOUND"
            assert session.get(Inquiry, inquiry).current_context_id is None
            return
    data = context(client, inquiry, headers, response)
    assert data["is_current"] is True
    case = data["context"]
    assert case["quality"] == "DEGRADED"
    assert (
        case["policy_version"] == "core-policy-v1"
        and case["freshness_policy"]["version"] == "freshness-v1"
    )
    assert all(e["fetched_at"] == "2026-09-20T06:00:00Z" for e in case["evidence"])
    assert all(e["snapshot_version"] == 1 for e in case["evidence"])
    assert {e["id"] for e in case["evidence"]} == set(case["facts"]) | set(case["source_texts"])
    assert UUID(case["run_id"]) == run.id
    if missing:
        assert any(m["code"] == missing for m in case["missing_information"])
    if number == 2:
        assert "UNKNOWN_FRESHNESS" in case["risk_flags"]
        assert any(
            e["pointer"].endswith("/events/0/status") and e["source_updated_at"] is None
            for e in case["evidence"]
        )
    if number == 4:
        assert len(case["parcels"]) == 2
        statuses = {e["id"]: e["value"] for e in case["evidence"]}
        assert [statuses[p["shipment_status_evidence_id"]] for p in case["parcels"]] == [
            "IN_TRANSIT",
            "NOT_COLLECTED",
        ]
    if number == 5:
        assert case["parcels"] == []
        assert any(
            e["kind"] == "SOURCE_TEXT" and e["source_updated_at"] is None for e in case["evidence"]
        )
    if number == 6:
        note = next(
            e
            for e in case["evidence"]
            if e["value"] == "Expected to ship today, subject to carrier collection."
        )
        assert note["kind"] == "SOURCE_TEXT" and note["id"] not in case["facts"]
    if number == 7:
        assert "STALE_DATA" in case["risk_flags"]
        assert any(
            e["source_updated_at"] == (T0 - timedelta(hours=72)).isoformat().replace("+00:00", "Z")
            and e["freshness_status"] == "STALE"
            for e in case["evidence"]
        )
    if number in {8, 10}:
        assert (
            len(case["parcels"]) == 1 and case["parcels"][0]["shipment_status_evidence_id"] is None
        )
        assert not any(e["value"] == "EXCEPTION" for e in case["evidence"])
    if number == 9:
        assert case["parcels"][0]["shipment_status_evidence_id"]
        assert not any(m["code"] == "NO_WAREHOUSE_NOTES" for m in case["missing_information"])
    if number == 11:
        assert any(
            c["code"] == "POSSIBLE_HANDOVER_CONFLICT" and len(c["evidence_ids"]) >= 2
            for c in case["conflicts"]
        )
