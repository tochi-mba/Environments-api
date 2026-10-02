# Changelog

All notable changes to this service are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **A person's settings are read for the profile the request runs as.** The idle
  lifetimes, the default shell and the command timeout are profile-scoped, and settings-api
  returns a profile's values only to a request that names the profile. This service named
  none, so each reached it as the catalogue default. The request's `X-Keyring-Profile` is
  now named, or with none the person's `common.default_profile`, read first.
  settings-client moves to 0.4.0, whose test fake keeps profiles apart; the old one ignored
  them, which is why no test caught this.

### Security

- Scope injected credentials to their command in any POSIX shell. Commands ran
  through `eval`, a special built-in, so `sh` -- and `bash` after `set -o posix` --
  kept the variables written in front of it: every later command in that shell could
  read the token, unredacted once it carried no credentials of its own. Under `sh` a
  syntax error also ended the shell. Commands now run through `command eval`, which
  keeps the assignments temporary and the error survivable in both shells.
- Take a caller's `X-Request-ID` only when it is 1 to 64 characters of `A-Z a-z 0-9 . _ : -`,
  and generate a fresh one otherwise. Any value of any length was echoed on the response
  and bound to every log line the request wrote.
- Decode a file path once. The files API URL-decoded every path again after the framework
  had, and decoded JSON bodies that are never encoded, so `%2e%2e` and `%2F` became `..`
  and `/` after the spelling was chosen, and `%2Fetc%2Fpasswd` was an absolute path.
  Containment still refused every escape, but a path now means what it says: a `%` is part
  of the name, and a file whose name contains one can be read, written and moved.
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

- `PUT /files/content` writes atomically through a temporary file and returns `{path, size,
  etag, diff, applied_hunks, rejected_hunks}` with an `ETag` header. `path` is the
  workspace-relative path written; it was the path as sent.
- **Breaking:** the files API refuses a path if any part of it as spelled is a symbolic
  link, even one pointing inside the workspace (400 `path_outside_workspace`), and opens
  every step by descriptor with `O_NOFOLLOW`. It used to follow links that stayed inside.
- **Breaking:** the floor is now **Python 3.12**, which CI gates; 3.13 is declared supported.
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- Keep shell scripts LF-terminated on Windows checkouts so Linux image builds can run
  the namespace sandbox without shell parsing failures.
- CI gets a short-lived family token from the OIDC broker (`id-token: write`) instead of
  inheriting a shared secret; image builds accept a BuildKit `github_token`
  secret so tagged client packages can be fetched from private family repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
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
- Token verification and credential resolution use `keyring-client` from
  `Keyring-api/clients/python`, fetched as a tagged git source. The local JWKS cache
  (`app/keyring/jwks.py`) and `Settings.jwks_url` are gone, and `create_app` takes
  `keyring_transport` and `clock` in place of `http_client`.
- `scripts/dev_keyring.py` is keyring-client's shared fake and refuses what keyring refuses.
  `PUT /dev/credentials/{profile}/{service}` takes an `account_id`, and the stand-in's issuer
  and service token default to the values in `.env.example`.

### Deprecated

- `X-Keyring-User-Token` as the way to present a user token. Send `Authorization: Bearer`.

### Added

- A GitHub Pages site at <https://tochi-mba.github.io/Environments-api/>, in the REX ink/signal style: what Environments-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- File routes under `/v1/environments/{id}/files`, none of which needs a shell:
  `GET /search` (literal text search, bounded and reporting what it skipped),
  `POST /edit` (replace exactly one occurrence, returning a unified `diff`),
  `POST /patch` (apply a single-file unified diff, naming `applied_hunks` and
  `rejected_hunks`), `POST /directories` (create a directory and its parents),
  `DELETE /content` (delete a file, or a directory with `recursive=true`; never the
  workspace root), and `POST /copy` and `POST /move` (one regular file, never onto an
  existing destination).
- `GET /files` takes `glob` and `depth` (0 to 10) and lists recursively without following
  symbolic links.
- Optimistic concurrency for files: reads and every mutation that produces a file return a
  strong `ETag` (the SHA-256 of the whole file), and write, edit, patch, copy, move and
  delete honour `If-Match`. A mismatch, or a file that changes during the operation, is
  `412 file_changed`.
- `GET /files/content` returns `is_binary`, `etag` and `next_offset`; binary is decided over
  the whole file, and a UTF-8 window ends on a character boundary.
- `ENVAPI_KEYRING_ISSUER` (default `http://127.0.0.1:8001`), which must equal keyring's
  `KEYRING_ISSUER`.
- `ENVAPI_JWKS_MIN_REFETCH_SECONDS` (default 60, at most 3600).
- `ENVAPI_SETTINGS_API_BASE_URL` and `ENVAPI_SETTINGS_API_TOKEN` (both or neither). Unset,
  everybody gets this configuration. Set, each create reads that person's `environments`
  settings from settings-api; idle TTLs are stamped on the record so the reaper can honour
  them without a user token. `common.default_profile` replaces `ENVAPI_DEFAULT_PROFILE`
  when a request names no profile, and is refused rather than guessed during an outage.
- `environments.default_shell` is read when a shell opens, through
  `POST /v1/environments/{id}/shells` and `POST /v1/exec`. A person who chose `sh` gets
  `ENVAPI_SH_BINARY` (new, default `/bin/sh`; blank turns the choice off). `bash`, the
  catalogue's default, means `ENVAPI_SHELL_BINARY` whatever that names, so nobody who
  chose nothing sees a change, and neither does anybody without settings-api. A chosen
  shell this host does not have falls back to `ENVAPI_SHELL_BINARY` and is logged as
  `chosen_shell_not_installed`. Shell views carry `shell_binary`, the binary that
  started. `environments.persist_history` is not read: these shells are not
  interactive and keep no history to persist.
- `output_window` on `POST /v1/exec`: `head` (the default, and the old behaviour) or `tail`,
  which returns the last `max_output_bytes` of the output instead of the first.
- `output_truncated_bytes` on command results from `POST /v1/exec` and
  `POST /v1/shells/{sid}/exec`: the output bytes the `max_output_bytes` cap left out.
  `output_dropped_bytes` still counts only ring-buffer evictions, and before this the cap's
  cut could only be worked out from `output_end` and `output_cursor`.

### Fixed

- A path no route serves (404) and a method a route does not take (405) are
  `application/problem+json` with `code` `not_found` and `method_not_allowed`, like every
  other error. They were FastAPI's plain `{"detail": ...}` JSON. The 405 keeps its `Allow`
  header.
- `shell_busy` means only that a command is already running. A command over 1 MiB is now
  `422 validation_error` (still carrying `limit`), and `target: "tty"` on a shell opened
  without `pty` is `409 conflict`; both were reported as `shell_busy`, so a client that
  waited for the shell to go idle and retried would never succeed.
- Deleting a file or directory and creating a directory through the files API are audited,
  as `file.delete` (with `recursive`) and `file.mkdir`, like every other file mutation.
- The files API checks `max_disk_bytes` before it writes. The check ran after the file was
  on disk and ignored the bytes just written, so a write could take an environment past its
  quota, and a write that was refused with `quota_exceeded` had already been made. Write,
  edit, patch and copy now count the bytes they add first and are refused with nothing
  created, not even a parent directory; a move or a write that adds nothing is admitted.
- `POST /v1/exec` echoes `command` as the caller sent it. It echoed the subshell the route
  wraps it in, `( <command>\n)`, which a model was then shown as the command it had run.
