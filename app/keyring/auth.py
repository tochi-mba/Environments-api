"""Who a request is for: the keyring user token it presents, believed by the family's rules.

The rules are ``keyring_client``'s, the verifier every service in the family shares: RS256
only, issuer and audience pinned, every claim keyring mints required, expiry judged by an
injected clock, and signing keys fetched lazily, rate limited on an unknown ``kid`` and served
stale through a short outage. What lives here is this service's side of them:

* which header carries the token (:func:`presented_token`);
* the audience, which is this service's name, because keyring's internal endpoint refuses a
  user token whose ``aud`` is not the name of the service calling it; and
* one refusal. Every token this service does not accept, whichever rule refused it, is the
  same 401 body, because each difference a caller can see helps with the next forgery. The
  reason goes to the log. Keys that cannot be fetched are a 503 with fixed text instead,
  because the token may be perfectly good.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

import structlog
from keyring_client import (
    BAD_TOKEN,
    KEYS_UNAVAILABLE,
    AuthenticationError,
    ExactAudience,
    KeyringUnreachableError,
)

from app.errors import KeyringUnavailableError, UnauthorizedError

if TYPE_CHECKING:
    from keyring_client import TokenVerifier as SharedTokenVerifier

log = structlog.get_logger(__name__)

BEARER_SCHEME = "bearer"


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a request is for."""

    account_id: str
    profile: str
    user_token: str = field(repr=False)


def presented_token(authorization: str | None, legacy: str | None) -> str:
    """The user token a request presents, or the one refusal.

    ``Authorization: Bearer <token>`` is canonical. ``X-Keyring-User-Token`` on its own is
    still accepted for one release, and logged so the callers still sending it can be found.
    A request carrying both must carry the same token in each: two identities on one request
    is a client bug at best, and at worst a bet that two checks read different headers.

    Raises:
        UnauthorizedError: no token, an ``Authorization`` header that is not ``Bearer
            <token>``, or two headers naming different tokens.
    """
    token = legacy
    if authorization is not None:
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() != BEARER_SCHEME or not bearer:
            _refuse("authorization_scheme")
        if token and token != bearer:
            _refuse("headers_disagree")
        token = bearer
    elif token:
        log.info("legacy_user_token_header", replacement="Authorization: Bearer")
    if not token:
        _refuse("missing")
    return token


def _refuse(reason: str) -> NoReturn:
    """Log why, and refuse in the words every other refusal uses."""
    log.info("user_token_refused", reason=reason)
    raise UnauthorizedError(BAD_TOKEN)


class TokenVerifier:
    """Keyring's shared verifier, for this service's audience and in this service's errors."""

    def __init__(self, verifier: SharedTokenVerifier, audience: str) -> None:
        """Believe what ``verifier`` believes, of tokens minted for exactly ``audience``."""
        self._verifier = verifier
        self._audience = ExactAudience(audience)

    async def verify(self, token: str) -> str:
        """Return the account id (``sub``) a valid ``token`` was minted for.

        Raises:
            UnauthorizedError: the one refusal, for a bad signature, another algorithm,
                another issuer or audience, expiry, a missing claim or key id, or a key id
                keyring does not publish. Which of them it was is in the log.
            KeyringUnavailableError: keyring's signing keys could not be fetched and no usable
                copy is held, so whether the token is good is not known.
        """
        try:
            identity = await self._verifier.verify(token, audience=self._audience)
        except AuthenticationError:
            raise UnauthorizedError(BAD_TOKEN) from None
        except KeyringUnreachableError:
            raise KeyringUnavailableError(KEYS_UNAVAILABLE) from None
        return identity.account_id
