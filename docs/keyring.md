# Keyring

Keyring is the identity provider and credential vault shared by the sibling services.
This service uses it for exactly two things, and deliberately not for two others.

## Used

**Service tokens + JWKS verification (core).** Callers present a short-lived RS256 JWT
minted by `POST /v1/auth/service-token {"audience": "environments-api"}` in the
`X-Keyring-User-Token` header. The service fetches `GET /.well-known/jwks.json`, caches it
for `ENVAPI_JWKS_CACHE_SECONDS`, refreshes once on an unknown `kid` (rotation), and
verifies locally: signature, `exp`/`iat` with a few seconds of leeway, and
`aud == ENVAPI_KEYRING_SERVICE_NAME`. `sub` is the opaque account id that namespaces
everything and is a stable cache key; tokens rotate every few minutes, account ids do not.

**Credential resolution (opt-in per environment).** An environment created with
`credentials: ["github"]` has each service resolved per command:

```
GET {ENVAPI_KEYRING_BASE_URL}/v1/internal/credentials/{profile}/{service}
Authorization: Bearer {ENVAPI_KEYRING_SERVICE_TOKEN}
X-Keyring-User-Token: {the caller's token}
```

Both credentials are required; keyring takes the account from the user's token, so a
compromised service cannot fetch a credential it was not handed a token for.

| Keyring answers | This service does |
|---|---|
| 200 | injects the credential into that command's environment only |
| 404 | the account has not connected the service: the command runs without it, and the response lists it under `credentials_missing` |
| 401 | 401 `unauthorized` |
| 503 (sealed, refresh failed) | 503 `keyring_unavailable` with keyring's own `detail` passed through, because it names the fix |
| unreachable | 503 `keyring_unavailable` naming keyring |

### Response shape assumed

Keyring returns "what to attach", not a stored secret. `app/keyring/client.py` accepts:

* an explicit `{"env": {"NAME": "value", ...}}` object, used verbatim; otherwise
* the first present of `value`, `access_token`, `token`, `api_key`, `secret`, `password`
  becomes `<SERVICE>_TOKEN`, and a `username` becomes `<SERVICE>_USERNAME`
  (service name upper-cased, non-alphanumerics to `_`).

Every injected value of four characters or more is redacted from captured output.
Confirm the exact field names against keyring's own docs when wiring a real deployment;
this is the one place to adjust.

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
* **Keyring outage.** Requests needing a fresh JWKS or a credential fail with 503 naming
  keyring. Tokens keep verifying from cached keys until they rotate. `/health` stays up;
  `/health/ready` reports `keyring.status`.
* **Local development.** `scripts/dev_keyring.py` serves a JWKS, mints tokens
  (`POST /dev/mint`) and answers the credentials endpoint from an in-memory table so the
  service can be driven end to end without a real keyring.
