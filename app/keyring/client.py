"""Resolve credentials from keyring for injection into commands.

Keyring never hands back a stored secret as such. It answers ``resolve_credential`` with what
to attach to an outgoing request::

    {"service": "github", "headers": {"Authorization": "Bearer ghp_..."},
     "query_params": {}, "expires_at": null}

and this module turns that into environment variables for one command. The call itself is
``keyring_client``'s, shared by every service in the family: both the service token and the
user's token are sent, and keyring takes the account from the user's token itself, so a
compromised service cannot fetch a credential it was not handed a token for.

A body in any other shape is refused rather than turned into an empty environment. This
service once shipped a parser for a shape keyring has never produced, built against a stand-in
that echoed whatever it was given, and every real credential silently resolved to nothing. The
mapping is documented in ``docs/keyring.md``; this is the one place to change it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from keyring_client import (
    BAD_TOKEN,
    CredentialNotFoundError,
    CredentialUnavailableError,
    KeyringRejectedError,
    KeyringUnreachableError,
)

from app.errors import KeyringUnavailableError, UnauthorizedError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from keyring_client import CredentialClient as SharedCredentialClient

log = structlog.get_logger(__name__)

MIN_SECRET_CHARS = 4
"""Shorter values are injected but not redacted: replacing every ``abc`` in captured output
would mangle it without protecting anything worth the name."""

TOKEN_SUFFIX = "TOKEN"
UNREADABLE = "keyring returned a credential this service cannot read"
UNREACHABLE = "keyring is unreachable"

_AUTHORIZATION = "authorization"
_SCHEME_AND_CREDENTIAL = re.compile(r"^[A-Za-z][A-Za-z0-9._~+/-]*\s+(\S+)$")


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedCredential:
    """A credential as it will be injected: environment variables plus what to redact."""

    service: str
    env: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Name the variables, never show them: every value is a credential."""
        return f"ResolvedCredential(service={self.service!r}, env=<{','.join(sorted(self.env))}>)"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every value that must never reach captured output, longest first."""
        values = {value for value in self.env.values() if len(value) >= MIN_SECRET_CHARS}
        return tuple(sorted(values, key=len, reverse=True))


def env_name(service: str, name: str) -> str:
    """``<SERVICE>_<NAME>``, upper-cased, with every non-alphanumeric character as ``_``."""
    return "".join(ch if ch.isalnum() else "_" for ch in f"{service}_{name}").upper()


def parse_credential(service: str, body: Mapping[str, Any]) -> ResolvedCredential:
    """Turn keyring's resolved credential into environment variables.

    * Every header becomes ``<SERVICE>_<HEADER>``, holding the header's whole value.
    * Every query parameter becomes ``<SERVICE>_<PARAMETER>``.
    * ``<SERVICE>_TOKEN`` holds the bare credential, which is what most command-line tools
      read: the part after the scheme of an ``Authorization`` header, or its whole value when
      it has no scheme; failing that, the one value when keyring returned exactly one header
      or query parameter. With several and no ``Authorization`` it is left unset, because a
      guess would inject the wrong secret under a name a tool trusts.

    Raises:
        KeyringUnavailableError: ``headers`` is missing or is not a string-to-string object,
            or ``query_params`` is present and is not one.
    """
    headers = _string_map(body.get("headers"))
    raw_params = body.get("query_params")
    params = {} if raw_params is None else _string_map(raw_params)
    return credential_env(service, headers, params)


def credential_env(
    service: str, headers: Mapping[str, str], query_params: Mapping[str, str]
) -> ResolvedCredential:
    """:func:`parse_credential`'s rules, for an answer whose shape is already known to be good.

    :class:`CredentialClient` comes straight here, because keyring-client refuses a body in
    any other shape before handing over its headers and query parameters.
    """
    env = {env_name(service, name): value for name, value in headers.items()}
    env.update({env_name(service, name): value for name, value in query_params.items()})

    token = _bare_token(headers, query_params)
    if token is not None:
        env.setdefault(env_name(service, TOKEN_SUFFIX), token)
    return ResolvedCredential(service, env)


def _string_map(value: object) -> dict[str, str]:
    """A string-to-string mapping out of a JSON value, or the one refusal."""
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise KeyringUnavailableError(UNREADABLE)
    return {str(key): str(item) for key, item in value.items()}


def _bare_token(headers: Mapping[str, str], params: Mapping[str, str]) -> str | None:
    """The credential a tool would read as ``<SERVICE>_TOKEN``, or ``None`` rather than a guess."""
    for name, value in headers.items():
        if name.lower() == _AUTHORIZATION:
            match = _SCHEME_AND_CREDENTIAL.match(value)
            return match.group(1) if match else value
    values = [*headers.values(), *params.values()]
    return values[0] if len(values) == 1 else None


class CredentialClient:
    """``resolve_credential`` through keyring-client, as environment variables and domain errors."""

    def __init__(self, client: SharedCredentialClient) -> None:
        """Resolve through ``client``, which holds this service's own keyring token."""
        self._client = client

    async def resolve(
        self, user_token: str, profile: str, service: str
    ) -> ResolvedCredential | None:
        """Resolve one credential, or ``None`` when the account has not connected the service.

        Raises:
            UnauthorizedError: keyring refused one of the two tokens. It is the refusal every
                401 from this service uses; the user token has just verified here, so the
                log's ``keyring_rejected_credentials`` points an operator at this service's
                own token, or at its name in keyring's ``KEYRING_SERVICE_TOKENS``.
            KeyringUnavailableError: keyring holds the connection and could not make it usable,
                with keyring's own detail because it names the fix; or keyring could not be
                reached, or answered in a way this service cannot read, with fixed text.
        """
        try:
            resolved = await self._client.resolve_credential(
                user_token=user_token, profile=profile, service=service
            )
        except CredentialNotFoundError:
            return None
        except KeyringRejectedError:
            log.warning("keyring_rejected_credentials", service=service)
            raise UnauthorizedError(BAD_TOKEN) from None
        except CredentialUnavailableError as exc:
            # Keyring's problem detail, not an exception's text: keyring-client builds this
            # error from the body keyring wrote to name the fix, never from a transport failure.
            raise KeyringUnavailableError(str(exc)) from None
        except KeyringUnreachableError:
            # keyring-client has logged the failure's type; its text, which carries the URL,
            # goes nowhere.
            raise KeyringUnavailableError(UNREACHABLE) from None
        return credential_env(service, resolved.headers, resolved.query_params)

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()
