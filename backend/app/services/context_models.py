"""Closed, versioned Evidence/CaseContext wire models; no model or action inputs."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.integrations.models import CanonicalModel, Nonblank, OrderSnapshot, UtcTimestamp

Freshness = Literal["FRESH", "STALE", "UNKNOWN"]
Outcome = Literal[
    "SUCCESS",
    "EMPTY",
    "NOT_FOUND",
    "NO_TRACKING",
    "TIMEOUT",
    "UNAVAILABLE",
    "INVALID_RESPONSE",
    "REJECTED",
    "CONFLICT",
]
Positive = Annotated[int, Field(gt=0)]


class SourceAges(CanonicalModel):
    order: Positive = 86400
    parcel: Positive = 86400
    warehouse_note: Positive = 86400
    shipment: Positive = 21600
    shipment_event: Positive = 21600


class FreshnessPolicy(CanonicalModel):
    version: Nonblank = "freshness-v1"
    fetch_max_age_seconds: Positive = 1800
    source_max_age_seconds: SourceAges = Field(default_factory=SourceAges)

    @model_validator(mode="after")
    def versioned_values(self):
        if self.version == "freshness-v1" and (
            self.fetch_max_age_seconds != 1800 or self.source_max_age_seconds != SourceAges()
        ):
            raise ValueError("Changed freshness configuration requires a new version")
        return self


class Evidence(CanonicalModel):
    id: str = Field(pattern=r"^ev-[0-9a-f]{64}$")
    kind: Literal["FACT", "SOURCE_TEXT"]
    source_system: Nonblank
    source_record_id: Nonblank
    snapshot_id: UUID
    snapshot_version: Positive
    pointer: str = Field(pattern=r"^/records/")
    value: JsonValue
    source_updated_at: UtcTimestamp | None
    fetched_at: UtcTimestamp
    occurred_at: UtcTimestamp | None
    freshness_status: Freshness


class AuthorizationScope(CanonicalModel):
    inquiry_id: UUID
    external_order_id: Nonblank
    team_id: UUID
    assigned_agent_id: UUID


class Question(CanonicalModel):
    text: Nonblank
    source_system: Nonblank
    source_record_id: Nonblank
    created_at: UtcTimestamp


class OrderContext(CanonicalModel):
    external_order_id: Nonblank
    status_evidence_id: str


class ParcelContext(CanonicalModel):
    parcel_id: Nonblank
    tracking_number_evidence_id: str | None
    shipment_status_evidence_id: str | None
    event_evidence_ids: list[str]


class SourceOutcome(CanonicalModel):
    fetch_id: UUID
    operation: Literal["get_inquiry", "get_order", "get_parcels", "get_shipment", "get_notes"]
    target_id: Nonblank
    outcome: Outcome
    error_code: str | None
    fetched_at: UtcTimestamp | None

    @model_validator(mode="after")
    def outcome_shape(self):
        success = self.outcome in {"SUCCESS", "EMPTY"}
        if success != (self.fetched_at is not None):
            raise ValueError("Outcome timestamp mismatch")
        if (success and self.error_code is not None) or (
            not success and self.error_code != self.outcome
        ):
            raise ValueError("Outcome error mismatch")
        return self


class MissingInformation(CanonicalModel):
    code: Literal[
        "NO_PARCELS",
        "NO_EVENTS",
        "NO_WAREHOUSE_NOTES",
        "NO_TRACKING",
        "SOURCE_NOT_FOUND",
        "SOURCE_TIMEOUT",
        "SOURCE_UNAVAILABLE",
        "SOURCE_INVALID_RESPONSE",
        "SOURCE_REJECTED",
        "SOURCE_CONFLICT",
    ]
    scope: Nonblank
    evidence_ids: list[str]


class Unknown(CanonicalModel):
    code: Literal["UNKNOWN_SOURCE_TIME", "UNKNOWN_STATUS", "CLOCK_ANOMALY", "OLD_FETCH"]
    evidence_ids: list[str]


class Conflict(CanonicalModel):
    id: Nonblank
    code: Literal["INCOMPATIBLE_EVENT_STATUS", "POSSIBLE_HANDOVER_CONFLICT"]
    evidence_ids: list[str] = Field(min_length=2)
    explanation: Nonblank


class CaseContext(CanonicalModel):
    schema_version: Literal["core-mvp-v1"] = "core-mvp-v1"
    context_id: UUID
    inquiry_id: UUID
    run_id: UUID
    context_version: Positive
    created_at: UtcTimestamp
    policy_version: Nonblank = "core-policy-v1"
    freshness_policy: FreshnessPolicy
    authorization_scope: AuthorizationScope
    question: Question
    order: OrderContext
    parcels: list[ParcelContext]
    source_outcomes: list[SourceOutcome]
    evidence: list[Evidence]
    facts: list[str]
    source_texts: list[str]
    unknowns: list[Unknown]
    missing_information: list[MissingInformation]
    conflicts: list[Conflict]
    quality: Literal["COMPLETE", "DEGRADED"]
    risk_flags: list[
        Literal[
            "STALE_DATA",
            "UNKNOWN_FRESHNESS",
            "PARTIAL_SOURCE_FAILURE",
            "MISSING_INFORMATION",
            "CONFLICTING_SOURCES",
            "UNKNOWN_STATUS",
            "CLOCK_ANOMALY",
        ]
    ]

    @model_validator(mode="after")
    def reference_sets(self):
        by_id = {item.id: item for item in self.evidence}
        if len(by_id) != len(self.evidence):
            raise ValueError("Duplicate Evidence identity")
        for names, kind in ((self.facts, "FACT"), (self.source_texts, "SOURCE_TEXT")):
            if len(set(names)) != len(names) or any(
                name not in by_id or by_id[name].kind != kind for name in names
            ):
                raise ValueError("Invalid Evidence classification")
        if set(self.facts) | set(self.source_texts) != set(by_id):
            raise ValueError("Evidence classification must cover every record")
        references = [self.order.status_evidence_id]
        for parcel in self.parcels:
            references.extend(parcel.event_evidence_ids)
            references.extend(
                filter(
                    None,
                    [
                        parcel.tracking_number_evidence_id,
                        parcel.shipment_status_evidence_id,
                    ],
                )
            )
        for item in [*self.unknowns, *self.missing_information, *self.conflicts]:
            references.extend(item.evidence_ids)
        if any(name not in by_id for name in references):
            raise ValueError("Evidence reference outside Context")
        if self.authorization_scope.inquiry_id != self.inquiry_id:
            raise ValueError("Authorization scope mismatch")
        if self.authorization_scope.external_order_id != self.order.external_order_id:
            raise ValueError("Order scope mismatch")
        if len(set(self.risk_flags)) != len(self.risk_flags):
            raise ValueError("Duplicate quality flag")
        if len({p.parcel_id for p in self.parcels}) != len(self.parcels):
            raise ValueError("Duplicate parcel")
        snapshots = {item.fetch_id: item for item in self.source_outcomes}
        if len(snapshots) != len(self.source_outcomes):
            raise ValueError("Duplicate snapshot")
        if any(
            item.snapshot_version != self.context_version
            or item.snapshot_id not in snapshots
            or snapshots[item.snapshot_id].outcome != "SUCCESS"
            for item in self.evidence
        ):
            raise ValueError("Evidence snapshot mismatch")
        if (self.quality == "COMPLETE") != (not self.risk_flags):
            raise ValueError("Quality mismatch")
        return self


class ResolveView(CanonicalModel):
    run_id: UUID
    state: Literal["RUNNING", "SUCCEEDED", "PARTIAL"]
    context_id: UUID | None
    context_version: Positive | None
    quality: Literal["COMPLETE", "DEGRADED"] | None
    lock_version: Positive
    query_url: str | None = None


class RunView(CanonicalModel):
    id: UUID
    state: Literal["RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"]
    version: Positive
    context_id: UUID | None
    error_code: str | None
    lock_version: Positive


class ContextView(CanonicalModel):
    context: CaseContext
    is_current: bool


class OrderReadView(CanonicalModel):
    context_id: UUID
    order: OrderSnapshot
    freshness: dict[str, Freshness]
