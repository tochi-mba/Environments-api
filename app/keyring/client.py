"""Resolve credentials from keyring for injection into commands.

Keyring never hands back a stored secret as such: it returns what to attach. Both the
service token and the user's token are required, and the account is taken from the user's
token by keyring itself, so a compromised service cannot fetch a credential it was not given
a token for. Nothing here erodes that.

The response shape this client understands is documented in ``docs/keyring.md``; it is
deliberately tolerant so that a small change on keyring's side does not break injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.constants import HEADER_USER_TOKEN
from app.errors import KeyringUnavailableError, UnauthorizedError

log = structlog.get_logger(__name__)

_SECRET_KEYS = ("value", "access_token", "token", "api_key", "secret", "password")


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """A credential as it will be injected: environment variables plus what to redact."""

    service: str
    env: dict[str, str] = field(default_factory=dict)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every value that must never reach captured output."""
        return tuple(sorted({v for v in self.env.values() if len(v) >= 4}, key=len, reverse=True))


def _env_name(service: str, suffix: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in service).upper() + "_" + suffix


def parse_credential(service: str, body: dict[str, Any]) -> ResolvedCredential:
    """Turn keyring's response into environment variables.

    An explicit ``env`` object wins. Otherwise the first present secret-like field becomes
    ``<SERVICE>_TOKEN`` and a ``username`` becomes ``<SERVICE>_USERNAME``.
    """
    explicit = body.get("env")
    if isinstance(explicit, dict) and explicit:
        return ResolvedCredential(service, {str(k): str(v) for k, v in explicit.items()})
    env: dict[str, str] = {}
    for key in _SECRET_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value:
            env[_env_name(service, "TOKEN")] = value
            break
    username = body.get("username")
    if isinstance(username, str) and username:
        env[_env_name(service, "USERNAME")] = username
    return ResolvedCredential(service, env)


class CredentialClient:
    """``GET /v1/internal/credentials/{profile}/{service}`` with both credentials attached."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, service_token: str) -> None:
        """Talk to keyring at ``base_url`` as the service identified by ``service_token``."""
        self._client = client
        self._base = base_url.rstrip("/")
        self._service_token = service_token

    async def resolve(
        self, user_token: str, profile: str, service: str
    ) -> ResolvedCredential | None:
        """Resolve one credential, or ``None`` when the account has not connected the service.

        Raises:
            UnauthorizedError: Keyring rejected one of the tokens.
            KeyringUnavailableError: Keyring is down, sealed, or failed a refresh; its own
                detail is passed through because it names the fix.
        """
        url = f"{self._base}/v1/internal/credentials/{profile}/{service}"
        headers = {
            "Authorization": f"Bearer {self._service_token}",
            HEADER_USER_TOKEN: user_token,
        }
        try:
            response = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise KeyringUnavailableError(f"keyring unreachable: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise UnauthorizedError("keyring rejected the token", token_error="rejected")
        if response.status_code >= 500 or response.status_code == 503:
            raise KeyringUnavailableError(_detail_of(response))
        if response.status_code != 200:
            raise KeyringUnavailableError(
                f"unexpected keyring response {response.status_code}: {_detail_of(response)}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise KeyringUnavailableError("keyring returned a non-object credential")
        return parse_credential(service, body)


def _detail_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"keyring returned {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return str(body["detail"])
    return response.text
