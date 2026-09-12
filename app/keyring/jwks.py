"""Fetch and cache keyring's signing keys.

Keyring publishes a JWKS precisely so consuming services verify tokens locally instead of
calling back per request. The cache refreshes on expiry and, once per lookup, on an unknown
``kid`` so a key rotation is picked up without a restart.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import structlog

from app.errors import KeyringUnavailableError, UnauthorizedError

log = structlog.get_logger(__name__)


class JWKSCache:
    """A refreshing cache of keyring's public keys, keyed by ``kid``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Serve keys from ``url`` through ``client``, refreshing every ``ttl_seconds``."""
        self._client = client
        self._url = url
        self._ttl = ttl_seconds
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def has_keys(self) -> bool:
        """Whether any key has ever been loaded, i.e. tokens can be verified offline."""
        return bool(self._keys)

    async def refresh(self) -> None:
        """Fetch the key set now.

        Raises:
            KeyringUnavailableError: If keyring cannot be reached or returns a bad document.
        """
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document: dict[str, Any] = response.json()
            keys = [jwt.PyJWK(item) for item in document.get("keys", [])]
        except (httpx.HTTPError, ValueError, jwt.PyJWKError) as exc:
            raise KeyringUnavailableError(f"could not load keyring JWKS: {exc}") from exc
        self._keys = {key.key_id: key for key in keys if key.key_id}
        self._fetched_at = self._clock()
        log.info("jwks_refreshed", keys=sorted(self._keys))

    async def ensure_fresh(self) -> bool:
        """Refresh only if the cached set has expired; returns whether keyring was contacted."""
        async with self._lock:
            if self._stale():
                await self.refresh()
                return True
            return False

    def _stale(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= self._ttl

    async def get_key(self, kid: str) -> jwt.PyJWK:
        """Return the key for ``kid``, refreshing once if it is unknown.

        Raises:
            UnauthorizedError: If keyring does not publish a key with that id.
            KeyringUnavailableError: If a needed refresh fails.
        """
        async with self._lock:
            if self._stale() or kid not in self._keys:
                await self.refresh()
            try:
                return self._keys[kid]
            except KeyError:
                raise UnauthorizedError(
                    "token signed by an unknown key", token_error="unknown_kid"
                ) from None
