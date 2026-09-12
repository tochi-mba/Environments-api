"""An in-process stand-in for keyring, served through ``httpx.MockTransport``.

It signs real RS256 tokens with a generated key pair, publishes a JWKS that can be rotated,
and answers the internal credentials endpoint with whatever the test configured.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.constants import HEADER_USER_TOKEN

SERVICE_TOKEN = "service-token-for-tests"
AUDIENCE = "environments-api"
ISSUER = "keyring"


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class FakeKeyring:
    """State for the fake, mutable by tests between requests."""

    keys: dict[str, rsa.RSAPrivateKey] = field(default_factory=lambda: {"k1": _generate_key()})
    credentials: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    sealed: bool = False
    down: bool = False
    jwks_broken: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def current_kid(self) -> str:
        """The key id new tokens are signed with."""
        return next(reversed(self.keys))

    def rotate(self, kid: str) -> None:
        """Retire every key and publish a new one under ``kid``."""
        self.keys = {kid: _generate_key()}

    def mint(
        self,
        account_id: str,
        *,
        audience: str = AUDIENCE,
        ttl: float = 300,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        algorithm: str = "RS256",
        claims: dict[str, Any] | None = None,
    ) -> str:
        """Mint a token for ``account_id``; overrides exist to forge defective tokens."""
        kid = kid or self.current_kid
        signing_key = key or self.keys[kid]
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "sub": account_id,
            "aud": audience,
            "iat": now,
            "exp": now + int(ttl),
        }
        if claims:
            payload.update(claims)
        headers = {"kid": kid}
        if algorithm == "none":
            return jwt.encode(payload, key=None, algorithm="none", headers=headers)  # type: ignore[arg-type]
        pem = signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm=algorithm, headers=headers)

    def jwks(self) -> dict[str, Any]:
        """The JWKS document as keyring would publish it."""
        keys = []
        for kid, key in self.keys.items():
            jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
            jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
            keys.append(jwk)
        return {"keys": keys}

    def handle(self, request: httpx.Request) -> httpx.Response:
        """The ``httpx.MockTransport`` handler."""
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("keyring is down", request=request)
        path = request.url.path
        if path == "/.well-known/jwks.json":
            if self.jwks_broken:
                return httpx.Response(200, text="not json")
            return httpx.Response(200, json=self.jwks())
        if path.startswith("/v1/internal/credentials/"):
            return self._credentials(request, path)
        return httpx.Response(404, json={"detail": "no such route"})

    def _credentials(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {SERVICE_TOKEN}":
            return httpx.Response(401, json={"detail": "bad service token"})
        user_token = request.headers.get(HEADER_USER_TOKEN, "")
        if not user_token or user_token == "rejected":
            return httpx.Response(401, json={"detail": "bad user token"})
        if self.sealed:
            return httpx.Response(503, json={"detail": "vault is sealed; run keyring unseal"})
        _, _, _, _, profile, service = path.split("/", 5)
        body = self.credentials.get((profile, service))
        if body is None:
            return httpx.Response(404, json={"detail": "service not connected"})
        if body.get("__status__"):
            status = int(body["__status__"])
            return httpx.Response(status, text=str(body.get("__text__", "")))
        return httpx.Response(200, json=body)

    def client(self) -> httpx.AsyncClient:
        """An async client wired to this fake."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
