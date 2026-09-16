"""Keyring: this service's side of believing a token, and credentials as environment variables.

The rules by which a token is believed belong to keyring-client and are tested there. These
tests pin that this service applies them with its own issuer and audience; that what reaches a
caller is one refusal or fixed text, never anything a forger or a log reader could learn from;
and that keyring's answer becomes the variables a command sees. Every token carries a real
RS256 signature, and nothing about verification is stubbed.

Needs no host capability and no conftest fixture, so it runs on any workstation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from keyring_client import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient, jwks_url
from keyring_client import CredentialClient as SharedCredentialClient
from keyring_client import TokenVerifier as SharedTokenVerifier
from keyring_client.testing import (
    EPOCH,
    ROTATED_KEY,
    SEALED_DETAIL,
    FakeClock,
    forge_hs256,
    forge_unsigned,
    mint,
    private_pem,
    thumbprint,
)
from structlog.testing import capture_logs

from app.errors import KeyringUnavailableError, UnauthorizedError
from app.keyring.auth import Caller, TokenVerifier, presented_token
from app.keyring.client import UNREACHABLE, CredentialClient, parse_credential
from tests.fake_keyring import AUDIENCE, BASE_URL, ISSUER, SERVICE_TOKEN, FakeKeyring, credential

INSTANCE = "/v1/environments"
REFUSAL = {
    "type": "urn:environments-api:error:unauthorized",
    "title": "Unauthorized",
    "status": 401,
    "detail": BAD_TOKEN,
    "code": "unauthorized",
    "instance": INSTANCE,
}
LEAKY = "connection refused by https://operator:hunter2@keyring.test"
"""What an HTTP client's exception says: the URL, userinfo included."""


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def verifier(keyring: FakeKeyring, clock: FakeClock) -> AsyncIterator[TokenVerifier]:
    jwks = JwksClient(url=jwks_url(BASE_URL), clock=clock, transport=keyring.transport())
    yield TokenVerifier(SharedTokenVerifier(jwks=jwks, issuer=ISSUER, clock=clock), AUDIENCE)
    await jwks.aclose()


async def refusal(verifier: TokenVerifier, token: str) -> dict[str, Any]:
    with pytest.raises(UnauthorizedError) as caught:
        await verifier.verify(token)
    return caught.value.to_problem(INSTANCE)


def token_for(**bent: Any) -> str:
    """A token signed with keyring's key for this service, with the claims in ``bent`` bent."""
    return mint(**{"audience": AUDIENCE, "issuer": ISSUER, **bent})


# ----- believing a token ---------------------------------------------------------------


async def test_a_valid_token_yields_its_account(
    verifier: TokenVerifier, keyring: FakeKeyring
) -> None:
    assert await verifier.verify(keyring.mint(account_id="acct-1")) == "acct-1"


async def test_a_token_from_another_keyring_is_refused(verifier: TokenVerifier) -> None:
    """A second keyring's signature, however good, does not speak for this deployment."""
    assert await refusal(verifier, token_for(issuer="https://another-keyring.test")) == REFUSAL


async def test_a_token_minted_for_another_service_is_refused(
    verifier: TokenVerifier, keyring: FakeKeyring
) -> None:
    assert await refusal(verifier, keyring.mint(audience="web-search-api")) == REFUSAL


async def test_a_token_without_a_key_id_is_refused_without_asking_keyring(
    verifier: TokenVerifier, keyring: FakeKeyring
) -> None:
    """Choosing a key on the sender's behalf would be doing their search for them."""
    issued = int(EPOCH.timestamp())
    claims = {"iss": ISSUER, "sub": "acct-1", "aud": AUDIENCE, "iat": issued, "exp": issued + 60}
    assert await refusal(verifier, jwt.encode(claims, private_pem(), algorithm="RS256")) == REFUSAL
    assert keyring.fetches == 0


async def test_expiry_is_judged_by_the_injected_clock(
    verifier: TokenVerifier, clock: FakeClock
) -> None:
    """Issued at the fake clock's epoch with an hour to live, so PyJWT's wall clock would
    have refused it outright; the injected clock accepts it until the hour is up."""
    token = token_for(ttl_seconds=3600)
    assert await verifier.verify(token) == "account-a"
    clock.advance(timedelta(hours=1))
    assert await refusal(verifier, token) == REFUSAL


async def test_every_refusal_is_the_same_problem(
    verifier: TokenVerifier, keyring: FakeKeyring
) -> None:
    """Nothing in the body says which rule refused a token. The log says, to an operator."""
    tokens = [
        token_for(issuer="https://another-keyring.test"),
        keyring.mint(audience="web-search-api"),
        token_for(issued_at=EPOCH - timedelta(days=1), ttl_seconds=60),
        token_for(key=ROTATED_KEY, kid=thumbprint()),
        token_for(omit="sub"),
        token_for(claims={"sub": ""}),
        forge_hs256(audience=AUDIENCE, issuer=ISSUER),
        forge_unsigned(audience=AUDIENCE, issuer=ISSUER),
        "not-a-jwt",
    ]
    problems = [await refusal(verifier, token) for token in tokens]
    for authorization, legacy in ((None, None), ("Basic dXNlcjpwYXNz", None), ("Bearer a", "b")):
        with pytest.raises(UnauthorizedError) as caught:
            presented_token(authorization, legacy)
        problems.append(caught.value.to_problem(INSTANCE))
    assert all(problem == REFUSAL for problem in problems)


async def test_an_unknown_key_id_after_a_good_fetch_is_refused_and_cannot_flood_keyring(
    verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
) -> None:
    assert await verifier.verify(keyring.mint()) == "account-a"
    stranger = token_for(key=ROTATED_KEY)
    assert await refusal(verifier, stranger) == REFUSAL
    assert keyring.fetches == 2  # one look, in case keyring had rotated
    for _ in range(20):
        assert await refusal(verifier, stranger) == REFUSAL
    assert keyring.fetches == 2  # and no more inside the window, however many arrive
    keyring.rotate(ROTATED_KEY)
    clock.advance(61)
    assert await verifier.verify(stranger) == "account-a"  # a real rotation is still seen
    assert keyring.fetches == 3


async def test_keys_held_are_served_through_an_outage_for_a_bounded_grace(
    verifier: TokenVerifier, keyring: FakeKeyring, clock: FakeClock
) -> None:
    token = keyring.mint()
    assert await verifier.verify(token) == "account-a"
    keyring.error = httpx.ConnectError(LEAKY)
    clock.advance(timedelta(hours=2))  # past the cache, well inside the grace
    assert await verifier.verify(token) == "account-a"
    clock.advance(timedelta(days=2))  # past the grace: whether the token is good is unknown
    with pytest.raises(KeyringUnavailableError):
        await verifier.verify(token)


async def test_unreachable_keys_are_503_with_fixed_text(
    verifier: TokenVerifier, keyring: FakeKeyring
) -> None:
    keyring.error = httpx.ConnectError(LEAKY)
    with pytest.raises(KeyringUnavailableError) as caught:
        await verifier.verify(keyring.mint())
    problem = caught.value.to_problem(INSTANCE)
    assert problem["status"] == 503 and problem["detail"] == KEYS_UNAVAILABLE
    shown = f"{problem} {caught.value!r}"
    assert "hunter2" not in shown and "keyring.test" not in shown and "ConnectError" not in shown


# ----- which header carries the token ---------------------------------------------------


@pytest.mark.parametrize(
    ("authorization", "legacy"),
    [
        ("Bearer tok-1", None),
        ("bearer tok-1", None),
        ("Bearer tok-1", "tok-1"),
        ("Bearer tok-1", ""),
        (None, "tok-1"),
    ],
)
def test_the_token_comes_from_a_bearer_header_or_the_legacy_one(
    authorization: str | None, legacy: str | None
) -> None:
    assert presented_token(authorization, legacy) == "tok-1"


def test_the_legacy_header_alone_is_accepted_and_logged_without_the_token() -> None:
    with capture_logs() as logs:
        assert presented_token(None, "tok-legacy-1") == "tok-legacy-1"
        assert presented_token("Bearer tok-legacy-1", "tok-legacy-1") == "tok-legacy-1"
    assert logs == [
        {
            "event": "legacy_user_token_header",
            "replacement": "Authorization: Bearer",
            "log_level": "info",
        }
    ]


@pytest.mark.parametrize(
    ("authorization", "legacy"),
    [
        (None, None),
        (None, ""),
        ("", None),
        ("", "tok-1"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic dXNlcjpwYXNz", None),
        ("tok-1", None),
        ("Bearer tok-1", "tok-2"),
    ],
)
def test_a_missing_malformed_or_contradictory_header_is_the_one_refusal(
    authorization: str | None, legacy: str | None
) -> None:
    with pytest.raises(UnauthorizedError) as caught:
        presented_token(authorization, legacy)
    assert caught.value.to_problem(INSTANCE) == REFUSAL


def test_a_caller_never_shows_its_token() -> None:
    caller = Caller("acct-1", "personal", "eyJ-the-callers-token")
    assert "eyJ-the-callers-token" not in repr(caller) and "acct-1" in repr(caller)


# ----- credentials ---------------------------------------------------------------------


@pytest.fixture
async def credentials(keyring: FakeKeyring) -> AsyncIterator[CredentialClient]:
    shared = SharedCredentialClient(
        base_url=BASE_URL, service_token=SERVICE_TOKEN, transport=keyring.transport()
    )
    client = CredentialClient(shared)
    yield client
    await client.aclose()


async def test_resolve_credential_maps_keyrings_real_response(
    credentials: CredentialClient, keyring: FakeKeyring
) -> None:
    keyring.connect(
        account_id="a",
        profile="personal",
        service="github",
        headers={"Authorization": "Bearer ghp_secret123"},
    )
    keyring.connect(
        account_id="a",
        profile="personal",
        service="tmdb",
        headers={},
        query_params={"api_key": "tmdb_secret"},
    )
    keyring.connect(
        account_id="a",
        profile="personal",
        service="odd-name",
        headers={"X-API-Key": "tok_abcdef"},
        expires_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
    )
    token = keyring.mint(account_id="a")
    github = await credentials.resolve(token, "personal", "github")
    assert github is not None
    assert github.env == {
        "GITHUB_AUTHORIZATION": "Bearer ghp_secret123",
        "GITHUB_TOKEN": "ghp_secret123",
    }
    assert github.secrets == ("Bearer ghp_secret123", "ghp_secret123")
    tmdb = await credentials.resolve(token, "personal", "tmdb")
    assert tmdb is not None
    assert tmdb.env == {"TMDB_API_KEY": "tmdb_secret", "TMDB_TOKEN": "tmdb_secret"}
    odd = await credentials.resolve(token, "personal", "odd-name")
    assert odd is not None
    assert odd.env == {"ODD_NAME_X_API_KEY": "tok_abcdef", "ODD_NAME_TOKEN": "tok_abcdef"}
    assert await credentials.resolve(token, "personal", "missing") is None
    request = keyring.internal_calls[-1]
    assert request.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert request.headers["X-Keyring-User-Token"] == token
    # Keyring takes the account from the token, so another account's token finds nothing.
    assert await credentials.resolve(keyring.mint(account_id="b"), "personal", "github") is None


def test_the_bare_token_comes_from_authorization_before_anything_else() -> None:
    headers = {"X-Trace": "trace-1", "authorization": "token abcd1234"}
    parsed = parse_credential("svc", credential("svc", headers=headers))
    assert parsed.env == {
        "SVC_X_TRACE": "trace-1",
        "SVC_AUTHORIZATION": "token abcd1234",
        "SVC_TOKEN": "abcd1234",
    }
    bare = parse_credential("svc", credential("svc", headers={"Authorization": "sk-plain-key"}))
    assert bare.env["SVC_TOKEN"] == "sk-plain-key"


def test_no_token_is_guessed_when_it_is_ambiguous_or_absent() -> None:
    several = credential("svc", headers={"X-Id": "id-1234"}, query_params={"key": "key-5678"})
    assert parse_credential("svc", several).env == {"SVC_X_ID": "id-1234", "SVC_KEY": "key-5678"}
    empty: dict[str, Any] = {"service": "svc", "headers": {}, "query_params": None}
    assert parse_credential("svc", empty).env == {}


def test_short_values_are_injected_but_not_redacted() -> None:
    parsed = parse_credential("svc", credential("svc", headers={"X-Key": "abc"}))
    assert parsed.env == {"SVC_X_KEY": "abc", "SVC_TOKEN": "abc"}
    assert parsed.secrets == ()


@pytest.mark.parametrize(
    "body",
    [
        {"env": {"GITHUB_TOKEN": "ghp_secret123"}},
        {"value": "ghp_secret123"},
        {"headers": "Bearer abcd1234"},
        {"headers": {"Authorization": 5}},
        {"headers": {}, "query_params": ["api_key"]},
    ],
)
def test_a_body_keyring_never_sends_is_refused_rather_than_injecting_nothing(
    body: dict[str, Any],
) -> None:
    with pytest.raises(KeyringUnavailableError, match="cannot read"):
        parse_credential("svc", body)


def test_a_resolved_credential_names_its_variables_but_never_shows_them() -> None:
    headers = {"Authorization": "Bearer ghp_secret123"}
    parsed = parse_credential("github", credential("github", headers=headers))
    assert repr(parsed) == (
        "ResolvedCredential(service='github', env=<GITHUB_AUTHORIZATION,GITHUB_TOKEN>)"
    )


async def test_keyrings_refusals_become_this_services_errors(
    credentials: CredentialClient, keyring: FakeKeyring
) -> None:
    keyring.connect(
        account_id="a",
        profile="personal",
        service="github",
        headers={"Authorization": "Bearer ghp_secret123"},
    )
    token = keyring.mint(account_id="a")
    # Keyring refuses a user token minted for some other service: the one refusal.
    foreign = keyring.mint(account_id="a", audience="web-search-api")
    with pytest.raises(UnauthorizedError) as refused:
        await credentials.resolve(foreign, "personal", "github")
    assert refused.value.to_problem(INSTANCE) == REFUSAL
    keyring.sealed = True
    with pytest.raises(KeyringUnavailableError) as sealed:
        await credentials.resolve(token, "personal", "github")
    assert sealed.value.detail == SEALED_DETAIL  # keyring's own words, because they name the fix
    keyring.sealed = False
    keyring.error = httpx.ConnectError(LEAKY)
    with pytest.raises(KeyringUnavailableError) as down:
        await credentials.resolve(token, "personal", "github")
    assert down.value.detail == UNREACHABLE
    shown = " ".join(
        f"{caught.value.to_problem(INSTANCE)} {caught.value!r}"
        for caught in (refused, sealed, down)
    )
    assert "hunter2" not in shown and SERVICE_TOKEN not in shown and token not in shown


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, text=f"Traceback: {LEAKY}"),
        httpx.Response(418, json={"detail": LEAKY}),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"service": "github", "headers": {"Authorization": 5}}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_anything_else_keyring_answers_is_503_with_fixed_text(answer: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: answer)
    client = CredentialClient(
        SharedCredentialClient(base_url=BASE_URL, service_token=SERVICE_TOKEN, transport=transport)
    )
    try:
        with pytest.raises(KeyringUnavailableError) as caught:
            await client.resolve("tok", "personal", "github")
    finally:
        await client.aclose()
    assert caught.value.detail == UNREACHABLE
    assert "hunter2" not in f"{caught.value.to_problem(INSTANCE)} {caught.value!r}"
