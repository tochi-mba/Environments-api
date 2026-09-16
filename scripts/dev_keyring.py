"""A keyring stand-in for local development and the smoke script.

Built on ``keyring_client.testing``, the fake the whole family shares, so it refuses what
keyring refuses: an unknown service token, and a user token with the wrong signature, issuer
or audience. On top of that it mints tokens for any account (``POST /dev/mint``) and stores
what the credentials endpoint answers (``PUT /dev/credentials``). It is not keyring: it exists
so this service can be exercised end to end on a laptop without one.

Its issuer and service token default to the values ``.env.example`` gives this service.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from keyring_client import testing

SERVICE_NAME = "environments-api"
ISSUER = os.environ.get("DEV_KEYRING_ISSUER", "http://127.0.0.1:8001")
SERVICE_TOKEN = os.environ.get(
    "DEV_KEYRING_SERVICE_TOKEN", "dev-keyring-service-token-not-for-production"
)
fake = testing.FakeKeyring(issuer=ISSUER, service_tokens={SERVICE_NAME: SERVICE_TOKEN})
keyring = fake.transport()
app = FastAPI(title="dev-keyring")


async def _answer(request: Request) -> Response:
    """Whatever the shared fake answers ``request`` with."""
    forwarded = httpx.Request(request.method, str(request.url), headers=request.headers.raw)
    answer = await keyring.handle_async_request(forwarded)
    return Response(
        await answer.aread(),
        status_code=answer.status_code,
        media_type=answer.headers.get("content-type"),
    )


@app.get("/.well-known/jwks.json")
async def jwks(request: Request) -> Response:
    """The published signing keys."""
    return await _answer(request)


@app.post("/dev/mint")
def mint(body: dict[str, Any]) -> dict[str, str]:
    """Mint a token: ``{"account_id": "...", "audience": "environments-api", "ttl": 300}``."""
    return {
        "token": testing.mint(
            account_id=str(body["account_id"]),
            audience=str(body.get("audience", SERVICE_NAME)),
            issuer=ISSUER,
            issued_at=datetime.now(UTC),
            ttl_seconds=float(body.get("ttl", 300)),
            key=fake.keys[0],
        )
    }


@app.put("/dev/credentials/{profile}/{service}")
def set_credential(profile: str, service: str, body: dict[str, Any]) -> dict[str, str]:
    """Store what keyring would answer one account for ``profile``/``service``.

    Takes ``{"account_id": "...", "headers": {...}, "query_params": {...}}``: the parts of
    keyring's real response a caller gets to choose, for the account whose token will ask.
    The shared fake fills in the rest, so the stand-in can only ever serve a shape keyring
    itself produces, and only to the account it was stored for.
    """
    fake.connect(
        account_id=str(body["account_id"]),
        profile=profile,
        service=service,
        headers=dict(body.get("headers") or {}),
        query_params=dict(body.get("query_params") or {}),
    )
    return {"status": "stored"}


@app.get("/v1/internal/credentials/{profile}/{service}")
async def credentials(request: Request) -> Response:
    """Keyring's service-to-service credential resolution, answered by the shared fake."""
    return await _answer(request)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEV_KEYRING_PORT", "8001")))
