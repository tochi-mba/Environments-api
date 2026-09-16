# Keyring

Keyring is the identity provider and credential vault shared by the sibling services.
This service uses it for exactly two things, and deliberately not for two others. Both go
through `keyring-client` (`Keyring-api/clients/python`), the library every service in the
family shares, so the rules by which a token is believed cannot differ between services.

## Used

**User tokens, verified locally (core).** A caller mints a short-lived RS256 token with
`POST /v1/auth/service-token {"audience": "environments-api"}` and sends it as
`Authorization: Bearer <token>`. `app/keyring/auth.py` hands it to keyring-client, which:

* accepts RS256 only, so `alg: none` and HS256 signed with the published key are refused;
* pins the issuer to `ENVAPI_KEYRING_ISSUER`, which must equal keyring's `KEYRING_ISSUER`
  exactly: a token signed by a staging keyring, or somebody's laptop, is not a token for
  this deployment however good its signature;
* pins the audience to exactly `ENVAPI_KEYRING_SERVICE_NAME`;
* requires every claim keyring mints (`exp`, `iat`, `iss`, `sub`, `aud`) and a key id;
* judges expiry by an injected clock, with no leeway.

`sub` is the opaque account id that namespaces everything and is a stable cache key;
tokens rotate every few minutes, account ids do not.

Keyring's public keys come from `GET /.well-known/jwks.json`. They are fetched for the first
token rather than at startup, so a keyring that is down cannot stop this service starting,
and cached for `ENVAPI_JWKS_CACHE_SECONDS` (default 3600). An unknown `kid` may provoke at
most one fetch per `ENVAPI_JWKS_MIN_REFETCH_SECONDS` (default 60): enough to pick up a
rotation, and not enough for a flood of invented key ids to become a flood of requests to
keyring. A failed fetch waits the same floor before the next attempt. While keyring cannot be
reached, keys already held keep verifying tokens for up to a day past their cache lifetime.

**Every refusal is the same 401.** A missing token, a malformed `Authorization` header, two
headers that disagree, a bad signature, another algorithm, issuer or audience, expiry, a
missing claim or key id, a key keyring does not publish: each one is

```json
{"type": "urn:environments-api:error:unauthorized", "title": "Unauthorized", "status": 401,
 "detail": "the token was not accepted", "code": "unauthorized", "instance": "/v1/..."}
```

because every difference a caller can see helps with the next forgery. The reason is logged
for an operator (`user_token_refused`, `token_rejected`, `jwks_kid_unknown`,
`jwks_refetch_suppressed`). When no usable key is held and none can be fetched the answer is
503 `keyring_unavailable` with the fixed detail "keyring's signing keys could not be
fetched": the token may be perfectly good, and neither keyring's URL nor the transport error
is repeated anywhere.

| Header | Meaning |
|---|---|
| `Authorization: Bearer <token>` | Canonical. The scheme is case-insensitive; any other shape is refused, not ignored. |
| `X-Keyring-User-Token: <token>` | Deprecated. Accepted on its own for one release and logged as `legacy_user_token_header`. Sent with `Authorization` as well, both must carry the same token. |
| `X-API-Key` | The optional front-door gate (`ENVAPI_API_KEYS`), checked first. `Authorization` never satisfies it. |

**Credential resolution (opt-in per environment).** An environment created with
`credentials: ["github"]` has each service resolved per command:

```
GET {ENVAPI_KEYRING_BASE_URL}/v1/internal/credentials/{profile}/{service}
Authorization: Bearer {ENVAPI_KEYRING_SERVICE_TOKEN}
X-Keyring-User-Token: {the caller's token}
```

Both credentials are required. Keyring takes the account from the user's token, and accepts
that token only when its `aud` is the name this service's token is registered under, which is
why `ENVAPI_KEYRING_SERVICE_NAME` must be that name:

```
KEYRING_SERVICE_TOKENS='{"environments-api":"<the value of ENVAPI_KEYRING_SERVICE_TOKEN>"}'
```

The service token must be at least 32 characters with no surrounding whitespace; that is
checked at startup, and the token never appears in a representation, a dump or an error.
Empty is allowed: tokens still verify, and an environment that declares credentials then
gets keyring's refusal.

| Keyring answers | This service does |
|---|---|
| 200 | injects the credential into that command's environment only |
| 404 | the account has not connected the service: the command runs without it, and the response lists it under `credentials_missing` |
| 401 | the same 401 refusal as any other; the user token has just verified here, so the log's `keyring_rejected_credentials` points at this service's token or its name in `KEYRING_SERVICE_TOKENS` |
| 503 (sealed, revoked, refresh failed) | 503 `keyring_unavailable` with keyring's own `detail`, because it names the fix |
| anything else, a body it cannot read, or no answer | 503 `keyring_unavailable`, "keyring is unreachable" |

### From keyring's answer to environment variables

Keyring returns what to attach to a request, not a stored secret:

```json
{"service": "github", "headers": {"Authorization": "Bearer ghp_..."},
 "query_params": {}, "expires_at": null}
```

`app/keyring/client.py` turns that into variables for one command, with the service name
upper-cased and every non-alphanumeric character as `_`:

* every header becomes `<SERVICE>_<HEADER>` holding the header's whole value
  (`GITHUB_AUTHORIZATION=Bearer ghp_...`);
* every query parameter becomes `<SERVICE>_<PARAMETER>` (`TMDB_API_KEY=...`);
* `<SERVICE>_TOKEN` holds the bare credential most tools read: the part after the scheme of
  an `Authorization` header, or its whole value when it has no scheme; failing that, the one
  value when keyring returned exactly one header or query parameter. With several and no
  `Authorization` it is left unset, because a guess would inject the wrong secret under a
  name a tool trusts.

A body in any other shape is refused with a 503 rather than injected as nothing. Every
injected value of four characters or more is redacted from captured output, longest first.

## Deliberately not used

**`resolve_form_secrets`** returns raw usernames, passwords and TOTP codes for filling
login forms. Nothing here needs it, and keyring's own documentation says never to expose
it as an assistant tool.

**RBAC roles.** The token carries no roles claim and no internal endpoint exposes one, so
this service cannot learn a caller's keyring permissions. Admin operations are gated by a
local `ENVAPI_OPERATOR_ACCOUNTS` list instead. If keyring adds roles to the token or an
introspection endpoint, `require_operator` in `app/api/deps.py` is the one place that
changes.

## Worth knowing

* **Account disable/delete.** There is no webhook, but keyring stops minting tokens for a
  disabled account and existing tokens expire in minutes, so access dies within the token
  TTL. Disk is reclaimed by the idle reaper, not by a signal.
* **Invented profiles.** The service cannot verify a profile name exists in keyring
  (reading profiles needs the user's session token, which it never sees). That is why the
  environment quota is enforced per account as well as per profile.
* **Keyring outage.** Tokens keep verifying against the keys already held, for up to a day
  past their cache lifetime; credential resolution fails with 503 naming keyring. `/health`
  stays up. `/health/ready` reads keyring's JWKS document, never keyring's own `/healthy`
  (which answers 503 whenever any stored connection is unusable), and reports
  `keyring.status` as `ok`, `stale` or `unreachable`.
* **Configuration typos.** An `ENVAPI_` variable that names no setting stops the service
  starting, with every such name listed and no value repeated.
* **Local development.** `scripts/dev_keyring.py` is keyring-client's shared fake behind a
  port: it serves a JWKS, mints tokens (`POST /dev/mint`), stores credentials per account
  (`PUT /dev/credentials/{profile}/{service}` with `account_id`, `headers` and
  `query_params`), and answers the credentials endpoint refusing what keyring refuses. Its
  issuer and service token default to the values in `.env.example`.
* **The sibling checkout.** keyring-client is unreleased and comes from
  `../Keyring-api/clients/python`, so a clone, a CI job or an image build of this service
  needs Keyring-api checked out beside it until the client is published.
