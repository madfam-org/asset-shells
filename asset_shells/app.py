"""The FastAPI application: Part 2 read API, MADFAM publish API, health and readiness.

* ``/health`` — liveness: the process answers. No dependencies.
* ``/ready`` — readiness: a real database round-trip through the pool, and the schema revision the
  code expects. 503 otherwise.
* ``/openapi.json`` — the service's own OpenAPI document (an MCP surface can be generated from it).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, db
from .api_publish import router as publish_router
from .api_read import router as read_router
from .errors import install_handlers
from .logging import configure_logging, describe_db_error
from .settings import get_settings

log = logging.getLogger("asset_shells")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    await run_in_threadpool(db.open_pool)
    await run_in_threadpool(db.check_role_posture)
    log.info("asset-shells %s started (env=%s, pool max=%s)", __version__, s.asset_shells_env, s.db_pool_max)
    try:
        yield
    finally:
        await run_in_threadpool(db.close_pool)


def _ready() -> tuple[bool, str]:
    try:
        revision = db.schema_revision()
    except db.DatabaseUnavailable:
        return False, "database pool not open"
    except psycopg.Error as exc:
        log.warning("readiness check failed: %s", describe_db_error(exc))
        return False, "database round-trip failed"
    if revision != db.EXPECTED_SCHEMA_REVISION:
        return False, "schema revision mismatch"
    return True, "ready"


def create_app() -> FastAPI:
    app = FastAPI(
        title="asset-shells",
        version=__version__,
        description=(
            "MADFAM asset-shell service. Read API: a subset of the IDTA-01002 v3.1 AAS Repository, Submodel "
            "Repository and Discovery read operations at /api/v3.1. Publish API: /madfam/v1. Type shells are "
            "public; instance shells and passports require a Janua service token and are tenant-scoped."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_handlers(app)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        if request.url.path not in ("/health", "/ready"):
            log.info(
                "request",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        return response

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        ok, reason = _ready()
        return JSONResponse({"status": reason}, status_code=200 if ok else 503)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "asset-shells",
            "version": __version__,
            "readApi": "/api/v3.1",
            "publishApi": "/madfam/v1",
            "openapi": "/openapi.json",
        }

    app.include_router(read_router)
    app.include_router(publish_router)
    return app


app = create_app()
