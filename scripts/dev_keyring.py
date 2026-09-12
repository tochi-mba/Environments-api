"""A keyring stand-in for local development and the smoke script.

Serves a JWKS, mints tokens for any account (``POST /dev/mint``), and answers the internal
credentials endpoint from an in-memory table (``PUT /dev/credentials``). It is not keyring:
it exists so this service can be exercised end to end on a laptop without one.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_keyring import FakeKeyring

SERVICE_TOKEN = os.environ.get("DEV_KEYRING_SERVICE_TOKEN", "dev-service-token")
fake = FakeKeyring()
app = FastAPI(title="dev-keyring")


@app.get("/.well-known/jwks.json")
def jwks() -> dict[str, Any]:
    """The published signing keys."""
    return fake.jwks()


@app.post("/dev/mint")
def mint(body: dict[str, Any]) -> dict[str, str]:
    """Mint a token: ``{"account_id": "...", "audience": "environments-api", "ttl": 300}``."""
    return {
        "token": fake.mint(
            str(body["account_id"]),
            audience=str(body.get("audience", "environments-api")),
            ttl=float(body.get("ttl", 300)),
        )
    }


@app.put("/dev/credentials/{profile}/{service}")
def set_credential(profile: str, service: str, body: dict[str, Any]) -> dict[str, str]:
    """Store what the credentials endpoint returns for ``profile``/``service``."""
    fake.credentials[(profile, service)] = body
    return {"status": "stored"}


@app.get("/v1/internal/credentials/{profile}/{service}")
def credentials(
    profile: str,
    service: str,
    request: Request,
    authorization: str = Header(default=""),
    x_keyring_user_token: str = Header(default=""),
) -> JSONResponse:
    """Keyring's service-to-service credential resolution, in miniature."""
    if authorization != f"Bearer {SERVICE_TOKEN}":
        raise HTTPException(401, "bad service token")
    if not x_keyring_user_token:
        raise HTTPException(401, "missing user token")
    body = fake.credentials.get((profile, service))
    if body is None:
        raise HTTPException(404, "service not connected")
    return JSONResponse(body)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEV_KEYRING_PORT", "8000")))
