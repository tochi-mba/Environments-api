"""Dependency injection. Tests override these with ``app.dependency_overrides``."""

from __future__ import annotations

import hmac
from typing import Annotated

import structlog
from fastapi import Depends, Request

from app.audit import AuditLog
from app.constants import (
    HEADER_API_KEY,
    HEADER_AUTHORIZATION,
    HEADER_PROFILE,
    HEADER_USER_TOKEN,
    PROFILE_PATTERN,
)
from app.environments.service import EnvironmentService
from app.errors import ForbiddenError, UnauthorizedError, ValidationError
from app.files import FileService
from app.keyring import JwksClient
from app.keyring.auth import Caller, TokenVerifier, presented_token
from app.keyring.client import CredentialClient
from app.preferences import PreferenceSource
from app.settings import Settings


def get_settings(request: Request) -> Settings:
    """The app's settings."""
    settings: Settings = request.app.state.settings
    return settings


def get_service(request: Request) -> EnvironmentService:
    """The environment service."""
    service: EnvironmentService = request.app.state.service
    return service


def get_verifier(request: Request) -> TokenVerifier:
    """The token verifier."""
    verifier: TokenVerifier = request.app.state.verifier
    return verifier


def get_jwks(request: Request) -> JwksClient:
    """Keyring's signing keys, as held for verifying tokens."""
    jwks: JwksClient = request.app.state.jwks
    return jwks


def get_credentials(request: Request) -> CredentialClient:
    """The keyring credential client."""
    client: CredentialClient = request.app.state.credentials
    return client


def get_files(request: Request) -> FileService:
    """The file service."""
    files: FileService = request.app.state.files
    return files


def get_audit(request: Request) -> AuditLog:
    """The audit log."""
    audit: AuditLog = request.app.state.audit
    return audit


def get_preference_source(request: Request) -> PreferenceSource:
    """Where this request's per-person settings come from."""
    preferences: PreferenceSource = request.app.state.preferences
    return preferences


async def get_caller(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
    preferences: Annotated[PreferenceSource, Depends(get_preference_source)],
) -> Caller:
    """Authenticate the request: optional API key gate, then the keyring user token.

    The API key has its own header, and ``Authorization`` never stands in for it: a
    deployment that sets ``ENVAPI_API_KEYS`` needs both on every request.

    ``X-Keyring-Profile`` is used as named. When it is absent, ``common.default_profile``
    fills in if settings-api is in use, otherwise ``ENVAPI_DEFAULT_PROFILE``. Guessing
    ``personal`` during an outage is refused.

    Raises:
        PreferencesUnavailableError: settings-api refused this service, or the request
            named no profile and the default must not be guessed.
    """
    if settings.api_keys:
        presented = request.headers.get(HEADER_API_KEY, "")
        if not any(hmac.compare_digest(presented, key) for key in settings.api_keys):
            raise UnauthorizedError("missing or invalid API key", header=HEADER_API_KEY)
    token = presented_token(
        request.headers.get(HEADER_AUTHORIZATION), request.headers.get(HEADER_USER_TOKEN)
    )
    account_id = await verifier.verify(token)
    requested = request.headers.get(HEADER_PROFILE, "").strip() or None
    chosen = await preferences.for_token(token)
    profile = chosen.profile(requested)
    if not PROFILE_PATTERN.match(profile):
        raise ValidationError(f"invalid profile name {profile!r}", header=HEADER_PROFILE)
    structlog.contextvars.bind_contextvars(account_id=account_id, profile=profile)
    return Caller(account_id=account_id, profile=profile, user_token=token)


def require_operator(
    caller: Annotated[Caller, Depends(get_caller)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Caller:
    """Only accounts listed in ``ENVAPI_OPERATOR_ACCOUNTS`` may use ``/v1/admin``.

    Keyring's roles are not visible in its tokens, so this local list stands in for RBAC.
    """
    if caller.account_id not in settings.operator_accounts:
        raise ForbiddenError("this account is not an operator")
    return caller


CallerDep = Annotated[Caller, Depends(get_caller)]
OperatorDep = Annotated[Caller, Depends(require_operator)]
ServiceDep = Annotated[EnvironmentService, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
FilesDep = Annotated[FileService, Depends(get_files)]
CredentialsDep = Annotated[CredentialClient, Depends(get_credentials)]
AuditDep = Annotated[AuditLog, Depends(get_audit)]
JWKSDep = Annotated[JwksClient, Depends(get_jwks)]
PreferenceSourceDep = Annotated[PreferenceSource, Depends(get_preference_source)]
