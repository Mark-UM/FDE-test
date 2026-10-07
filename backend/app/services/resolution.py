"""Stage 4 short transactions; no network, retries or model calls inside a transaction."""

import hashlib
import json
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.api.contracts import ClosedModel
from app.api.errors import ApiError
from app.core.clock import Clock
from app.db.models import (
    AuditLog,
    ContextVersion,
    GenerationAttempt,
    Inquiry,
    ResolutionRun,
    SourceFetch,
    User,
)
from app.services.context_models import AuthorizationScope, CaseContext, FreshnessPolicy
from app.services.core_access import CoreAccess, closed_query, inquiry_scope, no_input, validated
from app.services.evidence import build_context, freshness, validate_context
from app.services.source_collection import ERROR_STATUS, Collection, Fetch


class ResolveInput(ClosedModel):
    expected_lock_version: int


@dataclass(frozen=True)
class Started:
    inquiry_id: UUID
    run_id: UUID
    actor_id: UUID
    session_id: UUID
    source_system: str
    external_inquiry_id: str
    external_order_id: str
    lock_version: int


@dataclass(frozen=True)
class ResponseData:
    status: int
    payload: dict
    location: str | None = None


def identity(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ApiError(422, "INVALID_REQUEST", {"field_errors": ["id"]}) from None


def source_error(code, run_id):
    status = ERROR_STATUS.get(
        code,
        {
            "UNAUTHENTICATED": 401,
            "FORBIDDEN": 403,
            "VERSION_CONFLICT": 409,
            "STATE_CONFLICT": 409,
        }.get(code, 500),
    )
    return ApiError(status, code, {"run_id": str(run_id)})


class ResolutionService:
    def __init__(
        self,
        session_factory,
        clock: Clock,
        request_id: str,
        policy: FreshnessPolicy,
        policy_version: str = "core-policy-v1",
    ):
        self.sessions = session_factory
        self.clock = clock
        self.request_id = request_id
        self.policy = policy
        self.policy_version = policy_version

    def authorize(self, session, header, inquiry_id, *, exclusive=False):
        core = CoreAccess(session, self.clock, self.request_id)
        user, auth = core.authenticate(header)
        record = session.scalar(
            select(Inquiry)
            .where(Inquiry.id == identity(inquiry_id), inquiry_scope(user))
            .with_for_update(read=not exclusive)
        )
        if record is None:
            core.audit("ACCESS_DENIED", user.id, error_code="FORBIDDEN")
            raise ApiError(403, "FORBIDDEN")
        return user, auth, record

    def audit(self, session, inquiry, actor_id, event, record_id, **metadata):
        session.add(
            AuditLog(
                inquiry_id=inquiry.id,
                actor_id=actor_id,
                event_type=event,
                record_id=record_id,
                request_id=self.request_id,
                occurred_at=self.clock.now(),
                safe_metadata=metadata,
            )
        )

    def response(self, session, inquiry, run, *, initial=False):
        if run.inquiry_id != inquiry.id:
            raise ValueError("Run relationship mismatch")
        if run.state == "FAILED":
            return source_error(run.error_code, run.id)
        context = session.scalar(select(ContextVersion).where(ContextVersion.run_id == run.id))
        if context is not None and (
            context.inquiry_id != inquiry.id or context.version != run.version
        ):
            raise ValueError("Context relationship mismatch")
        url = f"/api/v1/inquiries/{inquiry.id}/runs/{run.id}"
        if run.state != "RUNNING" and context is None:
            raise ValueError("Completed Run lacks Context")
        if context is not None:
            self.load_context(session, inquiry, str(context.id))
        payload = {
            "run_id": str(run.id),
            "state": run.state,
            "context_id": str(context.id) if context else None,
            "context_version": context.version if context else None,
            "quality": context.quality if context else None,
            "lock_version": inquiry.lock_version,
        }
        if run.state == "RUNNING":
            payload["query_url"] = url
        return ResponseData(
            202 if run.state == "RUNNING" else 201 if initial else 200,
            payload,
            url if run.state == "RUNNING" else None,
        )

    def begin(self, header, inquiry_id, body, content_type, query, keys):
        # Only pre-mutation ApiErrors are deliberately committed, for safe denial Audit.
        with self.sessions() as session, session.begin():
            inquiry = user = None
            try:
                user, auth, inquiry = self.authorize(session, header, inquiry_id, exclusive=True)
                closed_query(query, set())
                if (
                    content_type.split(";", 1)[0].strip().lower() != "application/json"
                    or len(body) > 4096
                ):
                    raise ApiError(422, "INVALID_REQUEST")
                if len(keys) != 1 or not 1 <= len(keys[0]) <= 200 or not keys[0].strip():
                    raise ApiError(422, "INVALID_REQUEST", {"field_errors": ["Idempotency-Key"]})
                try:
                    value = json.loads(
                        body,
                        object_pairs_hook=lambda pairs: closed_query(
                            pairs, {"expected_lock_version"}
                        ),
                    )
                except (ValueError, UnicodeError, RecursionError):
                    raise ApiError(422, "INVALID_REQUEST") from None
                data = validated(ResolveInput, value)
                if data.expected_lock_version < 1:
                    raise ApiError(
                        422, "INVALID_REQUEST", {"field_errors": ["expected_lock_version"]}
                    )
                request_hash = hashlib.sha256(
                    json.dumps(
                        {"actor_id": str(user.id), "request": data.model_dump()},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                prior = session.scalar(
                    select(ResolutionRun).where(
                        ResolutionRun.inquiry_id == inquiry.id,
                        ResolutionRun.idempotency_key == keys[0],
                    )
                )
                if prior is not None:
                    if prior.request_hash != request_hash:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT")
                    return self.response(session, inquiry, prior)
                if inquiry.state in {"APPROVED", "ESCALATED"}:
                    raise ApiError(409, "STATE_CONFLICT")
                busy = session.scalar(
                    select(ResolutionRun.id).where(
                        ResolutionRun.inquiry_id == inquiry.id, ResolutionRun.state == "RUNNING"
                    )
                ) or session.scalar(
                    select(GenerationAttempt.id).where(
                        GenerationAttempt.inquiry_id == inquiry.id,
                        GenerationAttempt.state == "RUNNING",
                    )
                )
                if busy:
                    raise ApiError(409, "BUSY")
                if inquiry.lock_version != data.expected_lock_version:
                    raise ApiError(409, "VERSION_CONFLICT")
                if inquiry.external_order_id is None:
                    raise ApiError(422, "ORDER_REFERENCE_MISSING")
            except ApiError as error:
                if inquiry is not None:
                    self.audit(
                        session, inquiry, user.id, "ACCESS_DENIED", None, error_code=error.code
                    )
                result = error
            else:
                version = (
                    session.scalar(
                        select(func.max(ResolutionRun.version)).where(
                            ResolutionRun.inquiry_id == inquiry.id
                        )
                    )
                    or 0
                ) + 1
                run = ResolutionRun(
                    id=uuid4(),
                    inquiry_id=inquiry.id,
                    actor_id=user.id,
                    idempotency_key=keys[0],
                    request_hash=request_hash,
                    version=version,
                    state="RUNNING",
                    request_id=self.request_id,
                    started_at=self.clock.now(),
                )
                session.add(run)
                session.flush()
                inquiry.latest_run_id = run.id
                inquiry.current_context_id = inquiry.current_draft_id = None
                inquiry.state = "OPEN"
                inquiry.lock_version += 1
                inquiry.updated_at = self.clock.now()
                self.audit(session, inquiry, user.id, "RESOLVE_STARTED", run.id, version=version)
                result = Started(
                    inquiry.id,
                    run.id,
                    user.id,
                    auth.id,
                    inquiry.source_system,
                    inquiry.external_inquiry_id,
                    inquiry.external_order_id,
                    inquiry.lock_version,
                )
        return result

    def finish(self, header, started: Started, collection: Collection):
        with self.sessions() as session, session.begin():
            user, auth, inquiry = self.authorize(
                session, header, str(started.inquiry_id), exclusive=True
            )
            run = session.get(ResolutionRun, started.run_id)
            if (
                run is None
                or user.id != started.actor_id
                or auth.id != started.session_id
                or run.inquiry_id != inquiry.id
                or run.actor_id != started.actor_id
                or run.state != "RUNNING"
                or inquiry.latest_run_id != run.id
            ):
                raise ApiError(409, "STATE_CONFLICT")
            if inquiry.lock_version != started.lock_version:
                raise ApiError(409, "VERSION_CONFLICT")
            if (
                inquiry.external_order_id != started.external_order_id
                or inquiry.external_inquiry_id != started.external_inquiry_id
                or inquiry.source_system != started.source_system
            ):
                raise ApiError(502, "SOURCE_BINDING_MISMATCH")
            for item in collection.fetches:
                session.add(SourceFetch(run_id=run.id, **asdict(item)))
            context = None
            if collection.error_code is None:
                scope = AuthorizationScope(
                    inquiry_id=inquiry.id,
                    external_order_id=inquiry.external_order_id,
                    team_id=inquiry.team_id,
                    assigned_agent_id=inquiry.assigned_agent_id,
                )
                payload = build_context(
                    context_id=uuid4(),
                    inquiry_id=inquiry.id,
                    run_id=run.id,
                    version=run.version,
                    created_at=self.clock.now(),
                    scope=scope,
                    fetches=collection.fetches,
                    policy=self.policy,
                    policy_version=self.policy_version,
                )
                validate_context(payload, collection.fetches)
                context = ContextVersion(
                    id=payload.context_id,
                    inquiry_id=inquiry.id,
                    run_id=run.id,
                    version=run.version,
                    schema_version=payload.schema_version,
                    policy_version=payload.policy_version,
                    created_at=payload.created_at,
                    quality=payload.quality,
                    payload=payload.model_dump(mode="json"),
                )
                session.add(context)
                session.flush()
                inquiry.current_context_id = context.id
                inquiry.state = "CONTEXT_READY"
                run.state = (
                    "PARTIAL"
                    if any(f.outcome not in {"SUCCESS", "EMPTY"} for f in collection.fetches)
                    else "SUCCEEDED"
                )
            else:
                run.state, run.error_code = "FAILED", collection.error_code
            run.finished_at = self.clock.now()
            inquiry.updated_at = self.clock.now()
            inquiry.lock_version += 1
            self.audit(
                session,
                inquiry,
                user.id,
                "RESOLVE_" + run.state,
                run.id,
                version=run.version,
                state=run.state,
                error_code=run.error_code,
            )
            session.flush()
            result = self.response(session, inquiry, run, initial=True)
        return result

    def abort(self, started: Started, code: str):
        """Internal rejection cleanup only; never publish facts or overwrite recovery."""
        with self.sessions() as session, session.begin():
            session.scalar(
                select(User).where(User.id == started.actor_id).with_for_update(read=True)
            )
            inquiry = session.scalar(
                select(Inquiry).where(Inquiry.id == started.inquiry_id).with_for_update()
            )
            run = session.get(ResolutionRun, started.run_id)
            if (
                run is not None
                and inquiry is not None
                and run.inquiry_id == inquiry.id
                and run.actor_id == started.actor_id
                and run.state == "RUNNING"
                and inquiry.latest_run_id == run.id
            ):
                run.state, run.error_code, run.finished_at = "FAILED", code, self.clock.now()
                inquiry.current_context_id = inquiry.current_draft_id = None
                if inquiry.state not in {"APPROVED", "ESCALATED"}:
                    inquiry.state = "OPEN"
                inquiry.lock_version += 1
                inquiry.updated_at = self.clock.now()
                self.audit(
                    session, inquiry, started.actor_id, "RESOLVE_FAILED", run.id, error_code=code
                )

    def load_context(self, session, inquiry, context_id):
        context = session.scalar(
            select(ContextVersion).where(
                ContextVersion.id == identity(context_id), ContextVersion.inquiry_id == inquiry.id
            )
        )
        if context is None:
            raise ApiError(404, "RESOURCE_NOT_FOUND")
        run = session.get(ResolutionRun, context.run_id)
        if (
            run is None
            or run.inquiry_id != inquiry.id
            or run.version != context.version
            or run.state not in {"SUCCEEDED", "PARTIAL"}
        ):
            raise ValueError("Invalid persisted Context relationship")
        payload = CaseContext.model_validate_json(json.dumps(context.payload))
        if (
            payload.context_id != context.id
            or payload.inquiry_id != inquiry.id
            or payload.run_id != run.id
            or payload.context_version != context.version
            or payload.created_at != context.created_at
            or payload.quality != context.quality
            or payload.schema_version != context.schema_version
            or payload.policy_version != context.policy_version
        ):
            raise ValueError("Context envelope mismatch")
        rows = session.scalars(
            select(SourceFetch)
            .where(SourceFetch.run_id == run.id)
            .order_by(SourceFetch.started_at, SourceFetch.id)
        ).all()
        # Source outcomes carry the original operation sequence; PostgreSQL row order is irrelevant.
        by_id = {row.id: row for row in rows}
        if set(by_id) != {item.fetch_id for item in payload.source_outcomes}:
            raise ValueError("Context snapshot set mismatch")
        fetches = [
            Fetch(
                **{name: getattr(by_id[item.fetch_id], name) for name in Fetch.__dataclass_fields__}
            )
            for item in payload.source_outcomes
        ]
        validate_context(payload, fetches)
        current = (
            inquiry.current_context_id == context.id
            and inquiry.latest_run_id == run.id
            and inquiry.state in {"CONTEXT_READY", "DRAFTED", "APPROVED"}
            and payload.authorization_scope.external_order_id == inquiry.external_order_id
            and payload.authorization_scope.team_id == inquiry.team_id
            and payload.authorization_scope.assigned_agent_id == inquiry.assigned_agent_id
            and payload.question.source_record_id == inquiry.external_inquiry_id
            and payload.question.source_system == inquiry.source_system
        )
        return payload, fetches, current

    def read(self, header, inquiry_id, body, query, *, run_id=None, context_id=None, order=False):
        with self.sessions() as session, session.begin():
            try:
                _, _, inquiry = self.authorize(session, header, inquiry_id)
                no_input(body, query)
                if run_id is not None:
                    run = session.scalar(
                        select(ResolutionRun).where(
                            ResolutionRun.id == identity(run_id),
                            ResolutionRun.inquiry_id == inquiry.id,
                        )
                    )
                    if run is None:
                        raise ApiError(404, "RESOURCE_NOT_FOUND")
                    context = session.scalar(
                        select(ContextVersion).where(ContextVersion.run_id == run.id)
                    )
                    if context and (
                        context.inquiry_id != inquiry.id or context.version != run.version
                    ):
                        raise ValueError("Invalid run Context")
                    if context is not None:
                        self.load_context(session, inquiry, str(context.id))
                    payload = {
                        "id": str(run.id),
                        "state": run.state,
                        "version": run.version,
                        "context_id": str(context.id) if context else None,
                        "error_code": run.error_code,
                        "lock_version": inquiry.lock_version,
                    }
                elif order:
                    if inquiry.external_order_id is None:
                        raise ApiError(422, "ORDER_REFERENCE_MISSING")
                    if inquiry.current_context_id is None:
                        raise ApiError(409, "CONTEXT_REQUIRED")
                    context, fetches, current = self.load_context(
                        session, inquiry, str(inquiry.current_context_id)
                    )
                    if not current:
                        raise ApiError(409, "CONTEXT_REQUIRED")
                    source = next(f for f in fetches if f.operation == "get_order")
                    record = source.canonical_payload["records"][0]
                    state, _ = freshness(
                        record, "order", self.clock.now(), context.freshness_policy
                    )
                    payload = {
                        "context_id": str(context.context_id),
                        "order": record,
                        "freshness": {
                            item.pointer: state
                            for item in context.evidence
                            if item.snapshot_id == source.id
                        },
                    }
                else:
                    context, _, current = self.load_context(session, inquiry, context_id)
                    payload = {"context": context.model_dump(mode="json"), "is_current": current}
            except ApiError as error:
                result = error
            else:
                result = ResponseData(200, payload)
        return result

    def recover(self, inquiry_id: UUID, run_id: UUID, expected_lock_version: int):
        """Trusted operator CLI only: one exact outstanding run, no Provider execution."""
        with self.sessions() as session, session.begin():
            inquiry = session.scalar(
                select(Inquiry).where(Inquiry.id == inquiry_id).with_for_update()
            )
            run = session.get(ResolutionRun, run_id)
            if (
                inquiry is None
                or run is None
                or run.inquiry_id != inquiry.id
                or inquiry.latest_run_id != run.id
                or run.state != "RUNNING"
                or inquiry.lock_version != expected_lock_version
                or inquiry.state != "OPEN"
            ):
                raise ValueError("Recovery target is not the expected current RUNNING operation")
            run.state, run.error_code, run.finished_at = "FAILED", "INTERRUPTED", self.clock.now()
            inquiry.current_context_id = inquiry.current_draft_id = None
            inquiry.lock_version += 1
            inquiry.updated_at = self.clock.now()
            self.audit(
                session, inquiry, None, "OPERATION_RECOVERED", run.id, error_code="INTERRUPTED"
            )
