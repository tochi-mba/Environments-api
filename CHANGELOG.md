# Changelog

All notable changes to this service are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- Pin the token issuer to `ENVAPI_KEYRING_ISSUER`. The verifier required an `iss` claim but
  never compared it, so a token signed by any other keyring with a published key was accepted.
- Refuse every token with one identical 401 body. Responses no longer carry `token_error`
  codes or PyJWT's exception text; which rule refused a token is logged instead.
- Rate limit key fetches: an unknown `kid` provokes at most one JWKS fetch per
  `ENVAPI_JWKS_MIN_REFETCH_SECONDS`, and a failed fetch is not retried on every request.
  Before, every request carrying an invented key id was a request to keyring.
- Judge expiry against an injected clock, with no leeway, instead of PyJWT's wall clock and
  five seconds of leeway.
- Keep keyring's URL and transport errors out of responses and logs: unreachable keys and an
  unreachable keyring are 503s with fixed text, and only the exception's type is logged.
- Keep the caller's token and injected credential values out of object representations, and
  the service token out of settings dumps and validation errors.

### Changed

- **Breaking:** `Authorization: Bearer <keyring user token>` is the canonical way to present
  a token. `X-Keyring-User-Token` is still accepted on its own for one release and logged as
  `legacy_user_token_header`. A request carrying both must carry the same token in each, and
  an `Authorization` header that is not `Bearer <token>` is refused rather than ignored.
  `X-API-Key` is unchanged and is never satisfied by `Authorization`.
- **Breaking:** every keyring-token refusal, including a missing token and keyring refusing a
  credential call, is `401 unauthorized` with `detail: "the token was not accepted"` and no
  extension members. The `token_error` member is gone, and a missing token no longer carries
  `header`.
- **Breaking:** an `ENVAPI_` environment variable that names no setting stops the service
  starting, naming every such variable and none of their values.
- **Breaking:** a non-empty `ENVAPI_KEYRING_SERVICE_TOKEN` must be at least 32 characters with
  no surrounding whitespace, and `ENVAPI_KEYRING_SERVICE_NAME` must be an audience keyring can
  mint (non-empty, trimmed, no dot). An empty service token is still allowed.
- **Breaking:** `/health/ready` reports `keyring.status` as `ok`, `stale` or `unreachable`
  (was `fresh`, `cached` or `unreachable`) and no longer has `keyring.keys_cached`. It is 503
  only when no usable signing key is held and none can be fetched.
- **Breaking:** when keyring answers a credential call with a status other than 200, 401, 404
  or 503, or with a body this service cannot read, the response is a 503 with fixed text
  instead of keyring's status and body.
- Keys held from a successful fetch now verify tokens through a keyring outage, for up to a
  day past their cache lifetime, instead of every request failing once the cache expires.
- `ENVAPI_JWKS_CACHE_SECONDS` defaults to 3600 (was 300), as in the rest of the family, and
  must be between 0 and 86400.
- Token verification and credential resolution use `keyring-client` from the sibling
  `Keyring-api/clients/python` checkout, which a clone, CI job or image build now needs
  beside this repository. The local JWKS cache (`app/keyring/jwks.py`) and
  `Settings.jwks_url` are gone, and `create_app` takes `keyring_transport` and `clock` in
  place of `http_client`.
- `scripts/dev_keyring.py` is keyring-client's shared fake and refuses what keyring refuses.
  `PUT /dev/credentials/{profile}/{service}` takes an `account_id`, and the stand-in's issuer
  and service token default to the values in `.env.example`.

### Deprecated

- `X-Keyring-User-Token` as the way to present a user token. Send `Authorization: Bearer`.

### Added

- `ENVAPI_KEYRING_ISSUER` (default `http://127.0.0.1:8001`), which must equal keyring's
  `KEYRING_ISSUER`.
- `ENVAPI_JWKS_MIN_REFETCH_SECONDS` (default 60, at most 3600).
- `ENVAPI_SETTINGS_API_BASE_URL` and `ENVAPI_SETTINGS_API_TOKEN` (both or neither). Unset,
  everybody gets this configuration. Set, each create reads that person's `environments`
  settings from settings-api; idle TTLs are stamped on the record so the reaper can honour
  them without a user token. `common.default_profile` replaces `ENVAPI_DEFAULT_PROFILE`
  when a request names no profile, and is refused rather than guessed during an outage.
