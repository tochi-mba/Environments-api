from __future__ import annotations

import pytest

from app.errors import KeyringUnavailableError, UnauthorizedError
from app.keyring.auth import TokenVerifier
from app.keyring.client import CredentialClient, parse_credential
from app.keyring.jwks import JWKSCache
from tests.fake_keyring import AUDIENCE, SERVICE_TOKEN, FakeKeyring, _generate_key

JWKS_URL = "http://keyring/.well-known/jwks.json"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(keyring: FakeKeyring, clock: Clock) -> TokenVerifier:
    cache = JWKSCache(keyring.client(), JWKS_URL, ttl_seconds=60, clock=clock)
    return TokenVerifier(cache, AUDIENCE)


async def test_valid_token_yields_account(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    assert await verifier.verify(keyring.mint("acct-1")) == "acct-1"


async def test_expired_token_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(keyring.mint("acct-1", ttl=-60))
    assert info.value.extra["token_error"] == "expired"


async def test_wrong_audience_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(keyring.mint("acct-1", audience="web-search-api"))
    assert info.value.extra["token_error"] == "wrong_audience"


async def test_forged_signature_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    forged = keyring.mint("acct-1", key=_generate_key())
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(forged)
    assert info.value.extra["token_error"] == "invalid"


async def test_alg_none_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(keyring.mint("acct-1", algorithm="none"))
    assert info.value.extra["token_error"] == "invalid"


async def test_unknown_kid_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    token = keyring.mint("acct-1", kid="ghost", key=_generate_key())
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(token)
    assert info.value.extra["token_error"] == "unknown_kid"


async def test_missing_kid_and_malformed(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify("not.a.token")
    assert info.value.extra["token_error"] == "malformed"
    import jwt

    pem = _pem(keyring)
    token = jwt.encode({"sub": "x", "aud": AUDIENCE, "exp": 9999999999, "iat": 1}, pem, "RS256")
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(token)
    assert info.value.extra["token_error"] == "missing_kid"


async def test_missing_sub_rejected(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    import jwt

    payload = {"aud": AUDIENCE, "exp": 9999999999, "iat": 1, "sub": ""}
    token = jwt.encode(payload, _pem(keyring), "RS256", headers={"kid": keyring.current_kid})
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(token)
    assert info.value.extra["token_error"] == "missing_sub"
    payload["sub"] = 5
    token = jwt.encode(payload, _pem(keyring), "RS256", headers={"kid": keyring.current_kid})
    with pytest.raises(UnauthorizedError) as info:
        await verifier.verify(token)
    assert info.value.extra["token_error"] == "invalid"


async def test_jwks_rotation_is_picked_up(verifier: TokenVerifier, keyring: FakeKeyring) -> None:
    assert await verifier.verify(keyring.mint("a")) == "a"
    old = keyring.mint("a")
    keyring.rotate("k2")
    assert await verifier.verify(keyring.mint("a")) == "a"
    with pytest.raises(UnauthorizedError):
        await verifier.verify(old)


async def test_jwks_cache_respects_ttl(keyring: FakeKeyring, clock: Clock) -> None:
    cache = JWKSCache(keyring.client(), JWKS_URL, ttl_seconds=60, clock=clock)
    assert not cache.has_keys
    await cache.get_key("k1")
    await cache.get_key("k1")
    assert len(keyring.requests) == 1
    clock.now += 61
    await cache.get_key("k1")
    assert len(keyring.requests) == 2
    assert cache.has_keys


async def test_jwks_unreachable_or_broken(keyring: FakeKeyring, clock: Clock) -> None:
    cache = JWKSCache(keyring.client(), JWKS_URL, ttl_seconds=60, clock=clock)
    keyring.down = True
    with pytest.raises(KeyringUnavailableError):
        await cache.get_key("k1")
    keyring.down = False
    keyring.jwks_broken = True
    with pytest.raises(KeyringUnavailableError):
        await cache.get_key("k1")


@pytest.fixture
def credentials(keyring: FakeKeyring) -> CredentialClient:
    return CredentialClient(keyring.client(), "http://keyring/", SERVICE_TOKEN)


async def test_resolve_credential_shapes(
    credentials: CredentialClient, keyring: FakeKeyring
) -> None:
    keyring.credentials[("personal", "github")] = {"kind": "api_key", "value": "ghp_secret123"}
    keyring.credentials[("personal", "npm")] = {
        "env": {"NPM_TOKEN": "npm_secret", "NPM_REGISTRY": "https://r"}
    }
    keyring.credentials[("personal", "basic")] = {"password": "pw123456", "username": "bob"}
    keyring.credentials[("personal", "odd-name")] = {"token": "tok_abcdef"}
    token = keyring.mint("a")
    github = await credentials.resolve(token, "personal", "github")
    assert github is not None
    assert github.env == {"GITHUB_TOKEN": "ghp_secret123"}
    assert github.secrets == ("ghp_secret123",)
    npm = await credentials.resolve(token, "personal", "npm")
    assert npm is not None
    assert npm.env["NPM_TOKEN"] == "npm_secret"
    assert set(npm.secrets) == {"npm_secret", "https://r"}
    basic = await credentials.resolve(token, "personal", "basic")
    assert basic is not None
    assert basic.env == {"BASIC_TOKEN": "pw123456", "BASIC_USERNAME": "bob"}
    odd = await credentials.resolve(token, "personal", "odd-name")
    assert odd is not None
    assert odd.env == {"ODD_NAME_TOKEN": "tok_abcdef"}
    assert await credentials.resolve(token, "personal", "missing") is None
    request = keyring.requests[-1]
    assert request.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert request.headers["X-Keyring-User-Token"] == token


def test_parse_credential_ignores_empty_and_short() -> None:
    parsed = parse_credential("svc", {"value": "", "token": "abc"})
    assert parsed.env == {"SVC_TOKEN": "abc"}
    assert parsed.secrets == ()
    assert parse_credential("svc", {"env": {}}).env == {}


async def test_resolve_error_mapping(credentials: CredentialClient, keyring: FakeKeyring) -> None:
    with pytest.raises(UnauthorizedError):
        await credentials.resolve("rejected", "personal", "github")
    keyring.sealed = True
    with pytest.raises(KeyringUnavailableError) as info:
        await credentials.resolve("tok", "personal", "github")
    assert "sealed" in info.value.detail
    keyring.sealed = False
    keyring.down = True
    with pytest.raises(KeyringUnavailableError) as info:
        await credentials.resolve("tok", "personal", "github")
    assert "unreachable" in info.value.detail
    keyring.down = False
    keyring.credentials[("personal", "weird")] = {"__status__": 418, "__text__": "teapot"}
    with pytest.raises(KeyringUnavailableError) as info:
        await credentials.resolve("tok", "personal", "weird")
    assert "418" in info.value.detail and "teapot" in info.value.detail
    keyring.credentials[("personal", "weird")] = {"__status__": 500, "__text__": ""}
    with pytest.raises(KeyringUnavailableError) as info:
        await credentials.resolve("tok", "personal", "weird")
    assert "500" in info.value.detail
    keyring.credentials[("personal", "weird")] = {"__status__": 502, "__text__": '{"x": 1}'}
    with pytest.raises(KeyringUnavailableError) as info:
        await credentials.resolve("tok", "personal", "weird")
    assert info.value.detail == '{"x": 1}'


async def test_resolve_non_object_body(keyring: FakeKeyring) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    client = CredentialClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://k", SERVICE_TOKEN
    )
    with pytest.raises(KeyringUnavailableError):
        await client.resolve("tok", "personal", "github")


def _pem(keyring: FakeKeyring) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return keyring.keys[keyring.current_kid].private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
