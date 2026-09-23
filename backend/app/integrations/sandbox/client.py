"""HTTP lifecycle, wire validation and error mapping; no retry or business policy."""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.integrations.errors import (
    ExternalConflict,
    ExternalInvalidResponse,
    ExternalNotFound,
    ExternalRejected,
    ExternalTimeout,
    ExternalUnavailable,
)
from app.integrations.sandbox.schemas import ErrorResponse


@dataclass(frozen=True)
class SourceResponse[T]:
    data: T
    fetched_at: datetime
    service: str
    operation: str
    request_id: str

    def require(self, condition: bool) -> None:
        if not condition:
            raise ExternalInvalidResponse(
                service=self.service,
                operation=self.operation,
                request_id=self.request_id,
                status_code=200,
            )


class SandboxClient:
    def __init__(
        self,
        settings: Settings,
        clock: Clock | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.sandbox_base_url is None:
            raise ValueError("SANDBOX_BASE_URL is required to create a SandboxClient")
        self.clock = clock if clock is not None else SystemClock()
        self.http = httpx.AsyncClient(
            base_url=str(settings.sandbox_base_url),
            timeout=settings.sandbox_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> "SandboxClient":
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        await self.http.__aexit__(*args)

    async def request[T](
        self,
        method: str,
        path: str,
        schema: TypeAdapter[T],
        *,
        service: str,
        operation: str,
        request_id: str | None = None,
        body: dict[str, str] | None = None,
    ) -> SourceResponse[T]:
        correlation = request_id or str(uuid4())
        context = {"service": service, "operation": operation, "request_id": correlation}
        try:
            response = await self.http.request(
                method,
                path,
                headers={"X-Request-Id": correlation},
                json=body,
            )
        except httpx.TimeoutException:
            raise ExternalTimeout(**context) from None
        except httpx.RequestError:
            raise ExternalUnavailable(**context) from None

        status = response.status_code
        context["request_id"] = response.headers.get("X-Request-Id") or correlation
        if status != 200:
            code = None
            try:
                error = ErrorResponse.model_validate_json(response.content)
                code = error.error.code
            except ValidationError:
                pass  # HTTP failure semantics survive malformed/non-JSON error bodies.
            if status == 404:
                error_type = ExternalNotFound
            elif status == 409:
                error_type = ExternalConflict
            elif status in (408, 504):
                error_type = ExternalTimeout
            elif status == 429 or status >= 500:
                error_type = ExternalUnavailable
            elif 400 <= status < 500:
                error_type = ExternalRejected
            else:
                error_type = ExternalInvalidResponse
            raise error_type(**context, status_code=status, source_code=code)

        fetched_at = self.clock.now()
        try:
            data = schema.validate_json(response.content, strict=True)
        except ValidationError:
            raise ExternalInvalidResponse(**context, status_code=status) from None
        return SourceResponse(data=data, fetched_at=fetched_at, **context)
