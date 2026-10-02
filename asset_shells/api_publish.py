"""MADFAM publish API at /madfam/v1 — writes are validated, scoped and tenant-bound.

| Route | Scope | Tenant |
|---|---|---|
| ``PUT /type-environments/{commons}/{sha}`` | ``asset-shells:publish-types`` | none (type rows are tenant-less) |
| ``POST /instances`` | ``asset-shells:publish-instances`` | ``tenant_id`` claim required |
| ``POST /instances/{uuid}/passport-events`` | ``asset-shells:publish-instances`` | ``tenant_id`` claim required |
| ``GET /instances/{uuid}/passport-events`` | ``asset-shells:read`` | ``tenant_id`` claim required |

Rejections are Part 2 ``Result`` objects whose messages carry a ``path`` (JSON pointer) into the body.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import db, publish, repository
from .auth import (
    SCOPE_PUBLISH_INSTANCES,
    SCOPE_PUBLISH_TYPES,
    SCOPE_READ,
    Principal,
    require_principal,
    require_scope,
    require_tenant,
)
from .errors import ApiError, bad_request, not_found
from .ids import parse_instance_shell_id
from .settings import get_settings

router = APIRouter(prefix="/madfam/v1", tags=["MADFAM publish API"])


async def _read_json(request: Request) -> object:
    limit = get_settings().max_publish_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError.one(413, "too_large", f"the body exceeds {limit} bytes")
    content_type = request.headers.get("content-type", "")
    if not content_type.split(";")[0].strip().lower() == "application/json":
        raise ApiError.one(415, "unsupported_media_type", "send Content-Type: application/json")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError.one(413, "too_large", f"the body exceeds {limit} bytes")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise bad_request("bad_json", "the body is not valid JSON") from None


def _instance_id(uuid: str):
    isid = parse_instance_shell_id(f"https://id.madfam.io/aas/instance/{uuid}")
    if isid is None:
        raise bad_request("bad_identifier", "the path carries the instance's lower-case UUID")
    return isid


@router.put("/type-environments/{commons}/{sha}", operation_id="PutTypeEnvironments")
async def put_type_environments(
    commons: str, sha: str, request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    require_scope(principal, SCOPE_PUBLISH_TYPES)
    body = await _read_json(request)
    summary = await run_in_threadpool(publish.publish_type_release, commons, sha, body)
    return JSONResponse(summary.body(), status_code=201 if summary.created else 200)


@router.post("/instances", operation_id="PostInstance")
async def post_instance(request: Request, principal: Principal = Depends(require_principal)) -> JSONResponse:
    require_scope(principal, SCOPE_PUBLISH_INSTANCES)
    tenant = require_tenant(principal)
    body = await _read_json(request)
    created, result = await run_in_threadpool(publish.publish_instance, tenant, body)
    headers = {"Location": f"/api/v3.1/shells/{_b64(result['id'])}"}
    return JSONResponse(result, status_code=201 if created else 200, headers=headers)


@router.post("/instances/{uuid}/passport-events", operation_id="PostPassportEvent")
async def post_passport_event(
    uuid: str, request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    require_scope(principal, SCOPE_PUBLISH_INSTANCES)
    tenant = require_tenant(principal)
    isid = _instance_id(uuid)
    body = await _read_json(request)
    created, result = await run_in_threadpool(publish.append_passport_event, tenant, isid, body)
    return JSONResponse(result, status_code=201 if created else 200)


@router.get("/instances/{uuid}/passport-events", operation_id="GetPassportEvents")
def get_passport_events(
    uuid: str,
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    require_scope(principal, SCOPE_READ)
    tenant = require_tenant(principal)
    isid = _instance_id(uuid)
    shell_id = f"https://id.madfam.io/aas/instance/{isid.uuid}"
    s = get_settings()
    with db.transaction(tenant) as cur:
        if repository.get_shell(cur, shell_id) is None:
            raise not_found()
        page = repository.list_passport_events(cur, shell_id, cursor, min(limit or s.default_page_limit,
                                                                          s.max_page_limit))
    return JSONResponse(page.body())


def _b64(text: str) -> str:
    from .ids import b64url_encode

    return b64url_encode(text)
