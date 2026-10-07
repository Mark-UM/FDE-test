"""Deterministic facts and quality from immutable canonical snapshots only."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from app.integrations.models import (
    OrderSnapshot,
    ParcelSnapshot,
    ShipmentSnapshot,
    SupportInquiry,
    WarehouseNoteSnapshot,
)
from app.services.context_models import (
    AuthorizationScope,
    CaseContext,
    Conflict,
    Evidence,
    FreshnessPolicy,
    MissingInformation,
    OrderContext,
    ParcelContext,
    Question,
    SourceOutcome,
    Unknown,
)
from app.services.source_collection import Fetch

ORDER_STATUSES = {
    "PAID",
    "WAITING_STOCK",
    "PACKED",
    "SHIPPED",
    "PARTIALLY_SHIPPED",
    "PROCESSING",
    "DELIVERED",
}
SHIPMENT_STATUSES = {
    "LABEL_CREATED",
    "NOT_COLLECTED",
    "PICKED_UP",
    "IN_TRANSIT",
    "DELIVERED",
    "EXCEPTION",
}
HANDOVER_PHRASES = (
    "not handed to carrier today",
    "not been handed to carrier today",
    "not handed to the carrier today",
    "not been handed to the carrier today",
)


def timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None


def freshness(record: dict, category: str, evaluated_at: datetime, policy: FreshnessPolicy):
    fetched, source = timestamp(record["fetched_at"]), timestamp(record["source_updated_at"])
    old_fetch = (evaluated_at - fetched).total_seconds() > policy.fetch_max_age_seconds
    reasons = []
    if fetched > evaluated_at or (source is not None and source > evaluated_at):
        state, reasons = "UNKNOWN", ["CLOCK_ANOMALY"]
    elif source is None:
        state, reasons = "UNKNOWN", ["UNKNOWN_SOURCE_TIME"]
    elif old_fetch or (evaluated_at - source).total_seconds() > getattr(
        policy.source_max_age_seconds, category
    ):
        state = "STALE"
    else:
        state = "FRESH"
    if state == "UNKNOWN" and old_fetch:
        reasons.append("OLD_FETCH")
    return state, reasons


def evidence_id(snapshot_id: UUID, pointer: str) -> str:
    return "ev-" + hashlib.sha256((str(snapshot_id) + "\n" + pointer).encode()).hexdigest()


def build_context(
    *,
    context_id: UUID,
    inquiry_id: UUID,
    run_id: UUID,
    version: int,
    created_at: datetime,
    scope: AuthorizationScope,
    fetches: list[Fetch],
    policy: FreshnessPolicy,
    policy_version: str = "core-policy-v1",
) -> CaseContext:
    priority = {
        name: n
        for n, name in enumerate(
            ("get_inquiry", "get_order", "get_parcels", "get_shipment", "get_notes")
        )
    }
    fetches = sorted(fetches, key=lambda item: (priority[item.operation], item.target_id))
    models = {
        "get_inquiry": SupportInquiry,
        "get_order": OrderSnapshot,
        "get_parcels": ParcelSnapshot,
        "get_shipment": ShipmentSnapshot,
        "get_notes": WarehouseNoteSnapshot,
    }
    for item in fetches:
        SourceOutcome(
            fetch_id=item.id,
            operation=item.operation,
            target_id=item.target_id,
            outcome=item.outcome,
            error_code=item.error_code,
            fetched_at=item.fetched_at,
        )
        if item.outcome in {"SUCCESS", "EMPTY"}:
            if not isinstance(item.canonical_payload, dict) or set(item.canonical_payload) != {
                "records"
            }:
                raise ValueError("Invalid snapshot envelope")
            records = item.canonical_payload["records"]
            if not isinstance(records, list) or (item.outcome == "EMPTY") != (not records):
                raise ValueError("Invalid snapshot cardinality")
            if item.operation in {"get_inquiry", "get_order", "get_shipment"} and len(records) != 1:
                raise ValueError("Expected one snapshot record")
            for record in records:
                models[item.operation].model_validate_json(json.dumps(record))
        elif item.canonical_payload is not None:
            raise ValueError("Failure contains source facts")
    evidence = []
    unknowns = defaultdict(set)
    missing = []
    conflicts = []
    paths = {}
    parcel_views = []
    status_records = []
    note_records = []
    event_statuses = defaultdict(list)
    by_operation = {(item.operation, item.target_id): item for item in fetches}
    if len(by_operation) != len(fetches):
        raise ValueError("Duplicate Provider operation")
    if len({item.id for item in fetches}) != len(fetches):
        raise ValueError("Duplicate snapshot identity")

    def add(fetch, pointer, record, category, value, *, text=False, occurred_at=None):
        state, reasons = freshness(record, category, created_at, policy)
        item = Evidence(
            id=evidence_id(fetch.id, pointer),
            kind="SOURCE_TEXT" if text else "FACT",
            source_system=record["source_system"],
            source_record_id=record["source_record_id"],
            snapshot_id=fetch.id,
            snapshot_version=version,
            pointer=pointer,
            value=value,
            source_updated_at=timestamp(record["source_updated_at"]),
            fetched_at=timestamp(record["fetched_at"]),
            occurred_at=occurred_at,
            freshness_status=state,
        )
        evidence.append(item)
        paths[(fetch.id, pointer)] = item.id
        for reason in reasons:
            unknowns[reason].add(item.id)
        return item.id

    def missing_item(code, target, refs=()):
        missing.append(MissingInformation(code=code, scope=target, evidence_ids=sorted(refs)))

    def conflict_item(code, refs, explanation):
        refs = sorted(set(refs))
        identity = hashlib.sha256((code + "\n" + "\n".join(refs)).encode()).hexdigest()
        conflicts.append(
            Conflict(id="cf-" + identity, code=code, evidence_ids=refs, explanation=explanation)
        )

    inquiry_fetch = next(item for item in fetches if item.operation == "get_inquiry")
    inquiry = inquiry_fetch.canonical_payload["records"][0]
    order_id = scope.external_order_id
    if inquiry["external_order_id"] != order_id:
        raise ValueError("Question binding mismatch")
    required = {
        ("get_inquiry", inquiry["inquiry_id"]),
        ("get_order", order_id),
        ("get_parcels", order_id),
        ("get_notes", order_id),
    }
    order_fetch = by_operation[("get_order", order_id)]
    order = order_fetch.canonical_payload["records"][0]
    if order["external_order_id"] != order_id:
        raise ValueError("Order binding mismatch")
    for name in ("external_order_id", "status", "customer_reference", "created_at"):
        reference = add(order_fetch, "/records/0/" + name, order, "order", order[name])
        if name == "status" and order[name] not in ORDER_STATUSES:
            unknowns["UNKNOWN_STATUS"].add(reference)
    order_status = paths[(order_fetch.id, "/records/0/status")]
    for n, product in enumerate(order["items"]):
        for name in ("sku", "product_name", "quantity"):
            add(order_fetch, f"/records/0/items/{n}/{name}", order, "order", product[name])

    parcels_fetch = by_operation[("get_parcels", order_id)]
    parcels = parcels_fetch.canonical_payload["records"]
    if len({p["parcel_id"] for p in parcels}) != len(parcels):
        raise ValueError("Duplicate parcel")
    if not parcels:
        missing_item("NO_PARCELS", order_id)
    for n, parcel in enumerate(parcels):
        parcel_id = parcel["parcel_id"]
        required.add(("get_shipment", parcel_id))
        if parcel["external_order_id"] != order_id:
            raise ValueError("Parcel binding mismatch")
        refs = {}
        for name in ("parcel_id", "external_order_id", "carrier", "tracking_number"):
            refs[name] = add(parcels_fetch, f"/records/{n}/{name}", parcel, "parcel", parcel[name])
        shipment_fetch = by_operation[("get_shipment", parcel_id)]
        shipment_status = None
        event_refs = []
        if shipment_fetch.outcome == "SUCCESS":
            shipment = shipment_fetch.canonical_payload["records"][0]
            if shipment["parcel_id"] != parcel_id:
                raise ValueError("Shipment binding mismatch")
            if len({event["source_record_id"] for event in shipment["events"]}) != len(
                shipment["events"]
            ):
                raise ValueError("Duplicate event identity")
            shipment_status = add(
                shipment_fetch, "/records/0/status", shipment, "shipment", shipment["status"]
            )
            status_records.append((shipment, shipment_status))
            if shipment["status"] not in SHIPMENT_STATUSES:
                unknowns["UNKNOWN_STATUS"].add(shipment_status)
            if not shipment["events"]:
                missing_item("NO_EVENTS", parcel_id, [shipment_status])
            for k, event in enumerate(shipment["events"]):
                if event["parcel_id"] != parcel_id:
                    raise ValueError("Event binding mismatch")
                occurred = timestamp(event["occurred_at"])
                prefix = f"/records/0/events/{k}/"
                status_ref = add(
                    shipment_fetch,
                    prefix + "status",
                    event,
                    "shipment_event",
                    event["status"],
                    occurred_at=occurred,
                )
                event_refs.append(status_ref)
                event_refs.append(
                    add(
                        shipment_fetch,
                        prefix + "description",
                        event,
                        "shipment_event",
                        event["description"],
                        text=True,
                        occurred_at=occurred,
                    )
                )
                if event["status"] not in SHIPMENT_STATUSES:
                    unknowns["UNKNOWN_STATUS"].add(status_ref)
                event_statuses[(parcel_id, occurred)].append((event["status"], status_ref))
        elif shipment_fetch.outcome == "NO_TRACKING":
            missing_item("NO_TRACKING", parcel_id, [refs["tracking_number"]])
        else:
            missing_item("SOURCE_" + shipment_fetch.outcome, parcel_id)
        parcel_views.append(
            ParcelContext(
                parcel_id=parcel_id,
                tracking_number_evidence_id=refs["tracking_number"],
                shipment_status_evidence_id=shipment_status,
                event_evidence_ids=event_refs,
            )
        )

    notes_fetch = by_operation[("get_notes", order_id)]
    if set(by_operation) != required:
        raise ValueError("Unexpected Provider operation")
    if notes_fetch.outcome in ("SUCCESS", "EMPTY"):
        notes = notes_fetch.canonical_payload["records"]
        if len({note["note_id"] for note in notes}) != len(notes):
            raise ValueError("Duplicate note identity")
        if not notes:
            missing_item("NO_WAREHOUSE_NOTES", order_id)
        for n, note in enumerate(notes):
            if note["external_order_id"] != order_id:
                raise ValueError("Note binding mismatch")
            reference = add(
                notes_fetch,
                f"/records/{n}/note_text",
                note,
                "warehouse_note",
                note["note_text"],
                text=True,
            )
            note_records.append((note, reference))
    else:
        missing_item("SOURCE_" + notes_fetch.outcome, order_id)

    for values in event_statuses.values():
        for n, (status, reference) in enumerate(values):
            for other, other_reference in values[n + 1 :]:
                if status != other:
                    conflict_item(
                        "INCOMPATIBLE_EVENT_STATUS",
                        [reference, other_reference],
                        "同一包裹在同一事件时间有不同状态。",
                    )
    for note, note_ref in note_records:
        if any(phrase in note["note_text"].lower() for phrase in HANDOVER_PHRASES):
            for shipment, status_ref in status_records:
                updated = timestamp(shipment["source_updated_at"])
                if (
                    shipment["status"] in {"PICKED_UP", "IN_TRANSIT", "DELIVERED"}
                    and updated is not None
                    and updated.date() == timestamp(note["created_at"]).date()
                ):
                    conflict_item(
                        "POSSIBLE_HANDOVER_CONFLICT",
                        [note_ref, status_ref],
                        "同日仓库交接备注与物流状态可能不一致，需人工核对。",
                    )

    flags = set()
    if any(item.freshness_status == "STALE" for item in evidence):
        flags.add("STALE_DATA")
    if any(item.freshness_status == "UNKNOWN" for item in evidence):
        flags.add("UNKNOWN_FRESHNESS")
    if any(item.outcome not in {"SUCCESS", "EMPTY"} for item in fetches):
        flags.add("PARTIAL_SOURCE_FAILURE")
    if missing:
        flags.add("MISSING_INFORMATION")
    if conflicts:
        flags.add("CONFLICTING_SOURCES")
    if unknowns["UNKNOWN_STATUS"]:
        flags.add("UNKNOWN_STATUS")
    if unknowns["CLOCK_ANOMALY"]:
        flags.add("CLOCK_ANOMALY")
    return CaseContext(
        context_id=context_id,
        inquiry_id=inquiry_id,
        run_id=run_id,
        context_version=version,
        created_at=created_at,
        policy_version=policy_version,
        freshness_policy=policy,
        authorization_scope=scope,
        question=Question(
            text=inquiry["customer_message"],
            source_system=inquiry_fetch.source_system,
            source_record_id=inquiry["inquiry_id"],
            created_at=timestamp(inquiry["created_at"]),
        ),
        order=OrderContext(external_order_id=order_id, status_evidence_id=order_status),
        parcels=parcel_views,
        source_outcomes=[
            SourceOutcome(
                fetch_id=item.id,
                operation=item.operation,
                target_id=item.target_id,
                outcome=item.outcome,
                error_code=item.error_code,
                fetched_at=item.fetched_at,
            )
            for item in fetches
        ],
        evidence=evidence,
        facts=[item.id for item in evidence if item.kind == "FACT"],
        source_texts=[item.id for item in evidence if item.kind == "SOURCE_TEXT"],
        unknowns=[
            Unknown(code=code, evidence_ids=sorted(refs))
            for code, refs in sorted(unknowns.items())
            if refs
        ],
        missing_information=sorted(
            missing, key=lambda item: (item.code, item.scope, item.evidence_ids)
        ),
        conflicts=sorted(
            {item.id: item for item in conflicts}.values(), key=lambda item: (item.code, item.id)
        ),
        quality="DEGRADED" if flags else "COMPLETE",
        risk_flags=sorted(flags),
    )


def same_json(left, right) -> bool:
    """JSON equality must not identify True, 1 and 1.0 as the same fact."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def validate_context(context: CaseContext, fetches: list[Fetch]) -> None:
    rebuilt = build_context(
        context_id=context.context_id,
        inquiry_id=context.inquiry_id,
        run_id=context.run_id,
        version=context.context_version,
        created_at=context.created_at,
        scope=context.authorization_scope,
        fetches=fetches,
        policy=context.freshness_policy,
        policy_version=context.policy_version,
    )
    if not same_json(context.model_dump(mode="json"), rebuilt.model_dump(mode="json")):
        raise ValueError("Context does not match immutable source records")
    by_id = {item.id: item for item in fetches}
    for evidence in context.evidence:
        value = by_id[evidence.snapshot_id].canonical_payload
        for part in evidence.pointer.split("/")[1:]:
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        if not same_json(evidence.value, value):
            raise ValueError("Evidence value does not match its JSON Pointer")
