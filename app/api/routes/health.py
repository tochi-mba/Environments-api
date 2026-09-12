"""Liveness and readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import JWKSDep, ServiceDep, SettingsDep
from app.constants import SERVICE_NAME
from app.errors import KeyringUnavailableError

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Always 200 while the process is up; keyring's state does not change that."""
    return {"status": "ok", "service": SERVICE_NAME}


@router.get("/health/ready")
async def ready(
    response: Response, service: ServiceDep, jwks: JWKSDep, settings: SettingsDep
) -> dict[str, Any]:
    """The sandbox tier in use and whether tokens can currently be verified.

    503 only when keyring is unreachable and no signing key was ever cached, because then
    no request could be authenticated. With cached keys the service keeps working through
    a keyring outage until the keys rotate.
    """
    keyring_error: str | None = None
    try:
        await jwks.ensure_fresh()
    except KeyringUnavailableError as exc:
        keyring_error = exc.detail
    ready = keyring_error is None or jwks.has_keys
    response.status_code = 200 if ready else 503
    return {
        "status": "ready" if ready else "not_ready",
        "sandbox_tier": service.sandbox_tier,
        "min_sandbox_tier": settings.min_sandbox_tier,
        "allow_network": settings.allow_network,
        "keyring": {
            "reachable": keyring_error is None,
            "keys_cached": jwks.has_keys,
            "error": keyring_error,
        },
    }
