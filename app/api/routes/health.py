"""Liveness and readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import JWKSDep, ServiceDep, SettingsDep
from app.constants import SERVICE_NAME

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/healthy", include_in_schema=False)
async def health() -> dict[str, str]:
    """Always 200 while the process is up; keyring's state does not change that.

    ``/healthy`` is the name every other service in the family serves this on, and
    ``/health`` is what this one shipped with. Both answer, so neither a sibling's
    runbook nor an existing probe is wrong.
    """
    return {"status": "ok", "service": SERVICE_NAME}


@router.get("/health/ready")
@router.get("/ready", include_in_schema=False)
async def ready(
    response: Response, service: ServiceDep, jwks: JWKSDep, settings: SettingsDep
) -> dict[str, Any]:
    """The sandbox tier in use and whether tokens can currently be verified.

    Asks keyring's signing-key document, never keyring's own ``/healthy``: that answers 503
    whenever any stored connection is unusable, which says nothing about whether a token can
    be verified here. 503 only when no usable key is held and none can be fetched, because
    then no request could be authenticated. Through a keyring outage the keys already held
    keep verifying tokens for a bounded grace, and this says so.
    """
    usable, problem = await jwks.healthy()
    if not usable:
        keyring_status = "unreachable"
    elif problem is not None:
        keyring_status = "stale"
    else:
        keyring_status = "ok"
    response.status_code = 200 if usable else 503
    return {
        "status": "ready" if usable else "not_ready",
        "sandbox_tier": service.sandbox_tier,
        "min_sandbox_tier": settings.min_sandbox_tier,
        "allow_network": settings.allow_network,
        # "error" is keyring-client's fixed text, never a URL or an exception's message.
        "keyring": {"status": keyring_status, "error": problem},
    }
