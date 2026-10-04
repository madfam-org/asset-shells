"""Errors as Part 2 ``Result`` objects.

Every error this service returns — on the Part 2 read API and on the MADFAM publish API — is a
``Result``: ``{"messages": [{"code", "messageType", "text", "timestamp", "correlationId"}]}``
(IDTA-01002 Part2-API-Schemas ``Result``/``Message``). Publish rejections add a ``path`` (a JSON
pointer into the request body) to each message; Part 2 clients ignore unknown members.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import DatabaseUnavailable
from .logging import describe_db_error

log = logging.getLogger(__name__)


def _timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Problem:
    code: str
    text: str
    path: str | None = None

    def message(self, correlation_id: str) -> dict:
        msg = {
            "code": self.code[:32],
            "messageType": "Error",
            "text": self.text,
            "timestamp": _timestamp(),
            "correlationId": correlation_id,
        }
        if self.path is not None:
            msg["path"] = self.path
        return msg


@dataclass
class ApiError(Exception):
    status: int
    problems: list[Problem] = field(default_factory=list)
    headers: dict[str, str] | None = None

    @classmethod
    def one(cls, status: int, code: str, text: str, path: str | None = None, headers: dict | None = None) -> ApiError:
        return cls(status, [Problem(code, text, path)], headers)


def bad_request(code: str, text: str, path: str | None = None) -> ApiError:
    return ApiError.one(400, code, text, path)


def not_found(text: str = "The requested resource does not exist or is not visible to the caller") -> ApiError:
    return ApiError.one(404, "not_found", text)


def unauthorized(code: str, text: str) -> ApiError:
    return ApiError.one(401, code, text, headers={"WWW-Authenticate": 'Bearer realm="asset-shells"'})


def forbidden(code: str, text: str) -> ApiError:
    return ApiError.one(403, code, text)


def conflict(code: str, text: str, path: str | None = None) -> ApiError:
    return ApiError.one(409, code, text, path)


def unprocessable(problems: list[Problem]) -> ApiError:
    return ApiError(422, problems)


def result_body(problems: list[Problem], correlation_id: str) -> dict:
    return {"messages": [p.message(correlation_id) for p in problems]}


def _correlation_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            result_body(exc.problems, _correlation_id(request)), status_code=exc.status, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors()[:20]:
            loc = "/".join(str(p) for p in err.get("loc", ()))
            problems.append(Problem("bad_parameter", f"{loc}: {err.get('msg', 'invalid')}"))
        return JSONResponse(result_body(problems, _correlation_id(request)), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        text = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            result_body([Problem(code, text)], _correlation_id(request)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        # Never echo or log the driver's message: it can quote parameters or connection details.
        log.error("database error on %s %s: %s", request.method, request.url.path, describe_db_error(exc))
        status = 503 if isinstance(exc, psycopg.OperationalError) else 500
        problem = Problem("database_error", "The request failed in the database layer; it was logged without detail")
        return JSONResponse(result_body([problem], _correlation_id(request)), status_code=status)

    @app.exception_handler(DatabaseUnavailable)
    async def _db_unavailable(request: Request, exc: DatabaseUnavailable) -> JSONResponse:
        log.error("database unavailable on %s %s", request.method, request.url.path)
        problem = Problem("database_unavailable", "The database is not available")
        return JSONResponse(result_body([problem], _correlation_id(request)), status_code=503)
