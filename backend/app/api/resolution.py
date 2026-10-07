"""Stage 4 HTTP orchestration; deliberate failure commits are separate from rejections."""

import json
import logging

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.api.core import PATH_ID, SECURITY
from app.api.errors import ApiError
from app.core.security import bearer_token
from app.services.context_models import ContextView, OrderReadView, ResolveView, RunView
from app.services.resolution import ResolutionService, ResponseData, Started
from app.services.source_collection import collect_sources

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)


def service(request):
    return ResolutionService(
        request.app.state.session_factory,
        request.app.state.clock,
        request.state.request_id,
        request.app.state.freshness_policy,
        request.app.state.policy_version,
    )


def credentials(request):
    values = request.headers.getlist("authorization")
    header = values[0] if len(values) == 1 else None
    if bearer_token(header) is None:
        raise ApiError(401, "UNAUTHENTICATED")
    return header


def respond(result: ResponseData | ApiError):
    if isinstance(result, ApiError):
        raise result
    model = (
        ResolveView
        if "run_id" in result.payload
        else RunView
        if "id" in result.payload
        else OrderReadView
        if "order" in result.payload
        else ContextView
    )
    payload = model.model_validate_json(json.dumps(result.payload)).model_dump(
        mode="json", exclude_unset=True
    )
    return JSONResponse(
        payload,
        status_code=result.status,
        headers={"Location": result.location} if result.location else {},
    )


@router.post(
    "/inquiries/{id}/resolve-context",
    response_model=ResolveView,
    responses={201: {"model": ResolveView}, 202: {"model": ResolveView}},
    openapi_extra={
        **SECURITY,
        "parameters": [
            PATH_ID,
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["expected_lock_version"],
                        "properties": {"expected_lock_version": {"type": "integer", "minimum": 1}},
                    }
                }
            },
        },
    },
)
async def resolve(request: Request):
    header = credentials(request)
    operations = service(request)
    try:
        started = await run_in_threadpool(
            operations.begin,
            header,
            request.path_params["id"],
            await request.body(),
            request.headers.get("content-type", ""),
            list(request.query_params.multi_items()),
            request.headers.getlist("idempotency-key"),
        )
        if not isinstance(started, Started):
            return respond(started)
        try:
            async with request.app.state.provider_factory() as providers:
                collection = await collect_sources(
                    providers,
                    request.app.state.clock,
                    request.state.request_id,
                    started.external_inquiry_id,
                    started.external_order_id,
                    started.source_system,
                )
            result = await run_in_threadpool(operations.finish, header, started, collection)
        except ApiError as error:
            await run_in_threadpool(operations.abort, started, error.code)
            raise
    except ApiError:
        raise
    except Exception:
        logger.warning("INTERNAL_ERROR request_id=%s", request.state.request_id)
        raise ApiError(500, "INTERNAL_ERROR") from None
    return respond(result)


async def read(request, **resource):
    header = credentials(request)
    try:
        result = await run_in_threadpool(
            service(request).read,
            header,
            request.path_params["id"],
            await request.body(),
            list(request.query_params.multi_items()),
            **resource,
        )
    except ApiError:
        raise
    except Exception:
        logger.warning("INTERNAL_ERROR request_id=%s", request.state.request_id)
        raise ApiError(500, "INTERNAL_ERROR") from None
    return respond(result)


@router.get(
    "/inquiries/{id}/runs/{run_id}",
    response_model=RunView,
    openapi_extra={**SECURITY, "parameters": [PATH_ID, {**PATH_ID, "name": "run_id"}]},
)
async def run(request: Request):
    return await read(request, run_id=request.path_params["run_id"])


@router.get(
    "/inquiries/{id}/contexts/{context_id}",
    response_model=ContextView,
    openapi_extra={**SECURITY, "parameters": [PATH_ID, {**PATH_ID, "name": "context_id"}]},
)
async def context(request: Request):
    return await read(request, context_id=request.path_params["context_id"])


@router.get(
    "/inquiries/{id}/order",
    response_model=OrderReadView,
    openapi_extra={**SECURITY, "parameters": [PATH_ID]},
)
async def order(request: Request):
    return await read(request, order=True)
