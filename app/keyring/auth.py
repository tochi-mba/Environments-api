"""Local verification of keyring service tokens.

Tokens are RS256 JWTs with ``iss``, ``sub``, ``aud``, ``iat`` and ``exp``. ``sub`` is the
opaque account id that namespaces everything this service stores; ``aud`` must be this
service's name so a token minted for a sibling service cannot be replayed here.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

from app.errors import UnauthorizedError
from app.keyring.jwks import JWKSCache


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a request is for."""

    account_id: str
    profile: str
    user_token: str


class TokenVerifier:
    """Verifies tokens against the JWKS and the expected audience."""

    def __init__(self, jwks: JWKSCache, audience: str, leeway_seconds: float = 5.0) -> None:
        """Accept tokens whose ``aud`` is ``audience`` and whose signature the JWKS confirms."""
        self._jwks = jwks
        self._audience = audience
        self._leeway = leeway_seconds

    async def verify(self, token: str) -> str:
        """Return the account id (``sub``) a valid ``token`` was minted for.

        Raises:
            UnauthorizedError: For any defect: bad signature, wrong audience, expiry,
                ``alg: none``, a missing ``sub`` or an unknown key id.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("malformed token", token_error="malformed") from exc
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise UnauthorizedError("token has no key id", token_error="missing_kid")
        key = await self._jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("token has expired", token_error="expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise UnauthorizedError(
                "token was minted for another service", token_error="wrong_audience"
            ) from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError(f"invalid token: {exc}", token_error="invalid") from exc
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UnauthorizedError("token has no subject", token_error="missing_sub")
        return sub
