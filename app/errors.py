"""Domain errors and their RFC 9457 rendering.

Routes never deal in status codes: they raise a ``DomainError`` subclass and the handler
installed by :func:`install_error_handlers` turns it into ``application/problem+json`` with a
stable ``code`` that clients can switch on.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.constants import PROBLEM_JSON

log = structlog.get_logger(__name__)


class DomainError(Exception):
    """Base class for every error the service reports to a caller."""

    status: int = 500
    code: str = "internal_error"
    title: str = "Internal error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        """Create an error with a human-readable ``detail`` and optional extension members."""
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        """Render as a problem-details document."""
        body: dict[str, Any] = {
            "type": f"urn:environments-api:error:{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }
        if instance is not None:
            body["instance"] = instance
        body.update(self.extra)
        return body


class UnauthorizedError(DomainError):
    """The caller presented no usable identity."""

    status = 401
    code = "unauthorized"
    title = "Unauthorized"


class ForbiddenError(DomainError):
    """The caller is known but not allowed to do this."""

    status = 403
    code = "forbidden"
    title = "Forbidden"


class NotFoundError(DomainError):
    """The resource does not exist for this caller.

    Deliberately also used for resources that exist but belong to someone else: a 403 there
    would confirm the resource's existence to a caller who should not know it.
    """

    status = 404
    code = "not_found"
    title = "Not found"


class ConflictError(DomainError):
    """The request is valid but the resource's current state refuses it."""

    status = 409
    code = "conflict"
    title = "Conflict"


class ShellBusyError(ConflictError):
    """A second exec was attempted while a command is still running."""

    code = "shell_busy"
    title = "Shell is busy"


class ShellNotRunningError(ConflictError):
    """The shell process has exited; nothing more can be run in it."""

    code = "shell_not_running"
    title = "Shell is not running"


class EnvironmentArchivedError(ConflictError):
    """The environment was archived by the reaper and must be reset before use."""

    code = "environment_archived"
    title = "Environment is archived"


class QuotaExceededError(ConflictError):
    """A quota would be exceeded; names the limit and the values."""

    code = "quota_exceeded"
    title = "Quota exceeded"

    def __init__(self, limit: str, current: int, maximum: int) -> None:
        """Describe which ``limit`` blocked the request and where it stands."""
        super().__init__(
            f"{limit} exceeded: current {current}, maximum {maximum}",
            limit=limit,
            current=current,
            maximum=maximum,
        )


class ValidationError(DomainError):
    """The request body or parameters are malformed."""

    status = 422
    code = "validation_error"
    title = "Validation error"


class PreconditionError(DomainError):
    """A file changed after the caller read its ETag."""

    status = 412
    code = "file_changed"
    title = "File changed"


class PathEscapeError(DomainError):
    """A file path resolved to somewhere outside the workspace."""

    status = 400
    code = "path_outside_workspace"
    title = "Path is outside the workspace"


class KeyringUnavailableError(DomainError):
    """Keyring could not be reached or refused to serve; its own detail is passed through."""

    status = 503
    code = "keyring_unavailable"
    title = "Keyring unavailable"


class PreferencesUnavailableError(DomainError):
    """A person's settings were needed and could not be read honestly.

    Either settings-api refused this service -- a grant it was not given, a token it does
    not recognise -- or it cannot be reached and the setting in question is one that must
    not be guessed at. Neither is the caller's doing, so it is not a 4xx. The body is
    fixed text: settings-api's own detail names grants and must not reach the caller.
    """

    status = 503
    code = "preferences_unavailable"
    title = "Preferences unavailable"


class SandboxError(DomainError):
    """The sandbox could not prepare or spawn a process."""

    status = 500
    code = "sandbox_error"
    title = "Sandbox error"


def _problem_response(problem: dict[str, Any]) -> JSONResponse:
    return JSONResponse(problem, status_code=int(problem["status"]), media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    """Render every error the app raises as problem+json."""

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return _problem_response(exc.to_problem(str(request.url.path)))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # pydantic's error dicts can carry the raising exception object in ``ctx``; keep
        # only the parts that serialise.
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        err = ValidationError("Request validation failed", errors=errors)
        return _problem_response(err.to_problem(str(request.url.path)))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=str(request.url.path))
        return _problem_response(DomainError("Unexpected error").to_problem(str(request.url.path)))
