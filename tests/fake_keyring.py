"""Keyring's shared test doubles, configured for this service.

Everything real comes from ``keyring_client.testing``, the one fake the family shares: a real
RSA key, a real JWKS document, and keyring's internal endpoint refusing what keyring refuses,
including a user token whose audience is not the name of the service calling it. This module
only points that fake at environments-api, and keeps :func:`credential` for the tests of the
mapping from keyring's answer to environment variables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from keyring_client import testing

from app.constants import SERVICE_NAME

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import rsa

AUDIENCE = SERVICE_NAME
ISSUER = testing.ISSUER
BASE_URL = testing.BASE_URL
SERVICE_TOKEN = "environments-api-service-token-for-tests"


class FakeKeyring(testing.FakeKeyring):
    """The shared fake, holding this service's token and minting for its audience by default."""

    def __init__(self, *keys: rsa.RSAPrivateKey) -> None:
        super().__init__(*keys, service_tokens={AUDIENCE: SERVICE_TOKEN})

    def mint(self, *, account_id: str = "account-a", audience: str = AUDIENCE) -> str:
        """A token this keyring accepts from environments-api, unless ``audience`` says not."""
        return super().mint(account_id=account_id, audience=audience)


def credential(
    service: str,
    *,
    headers: dict[str, str],
    query_params: dict[str, str] | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """A body in the shape keyring's ``resolve_credential`` really answers with.

    Tests build credentials through this rather than writing dictionaries by hand. A
    hand-written ``{"value": "..."}`` is how this suite once passed against a response shape
    keyring has never produced, while every real credential resolved to nothing.
    """
    return {
        "service": service,
        "headers": headers,
        "query_params": query_params or {},
        "expires_at": expires_at,
    }
