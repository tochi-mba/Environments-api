"""Request-scoped logging context and request ids."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

from app.constants import HEADER_REQUEST_ID

log = structlog.get_logger(__name__)


def install_middleware(app: FastAPI) -> None:
    """Attach a request id to every request and log its outcome."""

    @app.middleware("http")
    async def _request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(HEADER_REQUEST_ID) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )
        started = time.perf_counter()
        response = await call_next(request)
        response.headers[HEADER_REQUEST_ID] = request_id
        log.info(
            "request",
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response
