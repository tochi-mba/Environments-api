# environments-api

Sandboxed environments and shells, as an API. It is a service in the
[LUCY](https://github.com/tochi-mba/LUCY-assistant) family and authenticates against
[keyring](https://github.com/tochi-mba/Keyring-api): it creates isolated workspaces, runs
shells in them, reports what is running, and lets an assistant kill or wait on it.

This service executes arbitrary commands on behalf of remote callers. It is remote code
execution as a product, and the sandbox tiers, path containment and PID-ownership checks
are the feature, not polish. Read `docs/security.md` before deploying it.

## Quick start

```
make install                      # uv sync --all-groups
cp .env.example .env              # point ENVAPI_KEYRING_* at keyring
make run                          # uvicorn on :8008, all interfaces, with reload
curl localhost:8008/ready         # the active sandbox tier, and whether tokens verify
```

`make run` listens on every interface, so on a shared network anybody who can reach the
port can try a token; bind it to `127.0.0.1` (`uv run uvicorn app.main:app --port 8008`)
when that matters. `/ready` answers 503 until keyring's keys can be fetched.

Token verification and credential resolution use `keyring-client`, and per-person settings
use `settings-client`. Both come from their owning repositories as tagged git sources
(`[tool.uv.sources]` in `pyproject.toml`), so `make install` fetches them; no sibling
checkout is needed. Where those repositories are private, git needs your GitHub
credentials: `gh auth setup-git`, which the meta-repo's bootstrap runs, makes `gh` its
helper. Leaving `ENVAPI_SETTINGS_API_BASE_URL` unset turns per-person settings off.

Without a keyring to hand, `uv run python scripts/dev_keyring.py` serves a stand-in on
`:8001` that mints tokens (`POST /dev/mint {"account_id": "me"}`) and accepts the service
token `.env.example` sets, and `DEV_KEYRING_URL=http://127.0.0.1:8001 make smoke` drives the
whole API against it (without `DEV_KEYRING_URL` the smoke script needs a `TOKEN` instead).
The stand-in is keyring's shared test fake, so it refuses what keyring refuses.

```
TOKEN=$(curl -s -XPOST localhost:8001/dev/mint -H 'content-type: application/json' \
        -d '{"account_id":"me"}' | jq -r .token)
curl -s -XPOST localhost:8008/v1/environments -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"name":"scratch"}'
curl -s -XPOST localhost:8008/v1/exec -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"environment_id":"env_…","command":"uname -a"}'
```

## What it does

* Multiple environments per account and profile, bounded by quotas (409 names the limit).
* Each environment is a folder under `ROOT/accounts/<account>/<profile>/`; environments
  survive restarts, shells do not, and the API says so (`dead_reason: service_restarted`).
* Concurrent persistent shells per environment; commands are framed with a random nonce
  so exit codes can be trusted; `exec` with `wait_ms` returns fast results in one call.
* Live PIDs with state, memory and CPU; signalling verifies the target belongs to the
  environment by ancestry.
* A files API (list, read, search, write, exact edit, patch, mkdir, delete, copy, move) with
  resolve-then-check containment, no symlink ever followed, and `ETag`/`If-Match` so a
  stale edit is refused rather than applied.
* Keyring credentials injected per command and redacted from output.
* A layered sandbox: `directory` → `user` → `namespace`, the strongest the host allows,
  reported on `/health/ready`; `ENVAPI_MIN_SANDBOX_TIER` refuses to boot below it.
* A reaper that closes idle shells, archives idle environments and prunes logs. When
  settings-api is configured, idle lifetimes are stamped on the environment at create so
  the reaper (which has no user token) can honour them.
* An audit log of every command and privileged action.

## Layout

```
app/
  main.py            app factory, lifespan, reaper task
  settings.py        ENVAPI_* configuration and quota defaults
  constants.py       headers, name patterns, on-disk names, the frame, the state enums
  errors.py          DomainError → application/problem+json
  logging.py         structlog configuration
  middleware.py      request ids and the per-request log line
  audit.py           the append-only audit log
  keyring/           header rule and one refusal over keyring-client, credential env mapping
  sandbox/           protocol, detection, directory/user/namespace tiers, setup.sh
  shells/            ring buffer, framing, redaction, the Shell process wrapper
  environments/      records, store, quotas, service, reaper logic
  preferences.py     the one place settings-api is spoken to
  procfs.py          /proc parsing
  processes.py       process listing and the ownership guard
  paths.py           resolve-then-check containment
  files.py           the files API: reads, writes, edits, copies, moves, deletes
  file_safety.py     descriptor-relative access that never follows a symlink; ETags
  file_edits.py      exact replacement and unified-patch parsing
  file_search.py     bounded literal search
  api/               deps, schemas, routes
docs/                architecture, api, operations, security, sandbox, keyring, mcp,
                     testing, adr/
scripts/             smoke.sh, dev_keyring.py
tests/               real processes, real files, 100% coverage gate
```

## Configuration

Every setting is an `ENVAPI_`-prefixed environment variable; see `.env.example` for the
full list with defaults. An `ENVAPI_` variable that names no setting stops the service
starting, so a typo cannot leave a default silently in place. The ones that matter most:

| Variable | Meaning |
|---|---|
| `ENVAPI_ROOT` | Where environments live. Not under `/tmp` in production. |
| `ENVAPI_KEYRING_BASE_URL`, `ENVAPI_KEYRING_SERVICE_TOKEN`, `ENVAPI_KEYRING_SERVICE_NAME` | How to reach keyring and who this service is: the `aud` of accepted tokens and this service's name in keyring's `KEYRING_SERVICE_TOKENS`, whose token is 32+ characters. |
| `ENVAPI_KEYRING_ISSUER` | The `iss` of accepted tokens: exactly keyring's `KEYRING_ISSUER`. |
| `ENVAPI_MIN_SANDBOX_TIER` | `directory`, `user` or `namespace`; refuse to start below it. |
| `ENVAPI_ALLOW_NETWORK` | Default egress; only enforced at the namespace tier. |
| `ENVAPI_OPERATOR_ACCOUNTS` | Comma-separated account ids allowed to use `/v1/admin`. |
| `ENVAPI_API_KEYS` | Optional front-door keys for `X-API-Key`. |
| `ENVAPI_MAX_*`, `*_IDLE_TTL_SECONDS` | Quotas; per-account overrides via `/v1/admin/quotas`, except `ENVAPI_MAX_FILE_READ_BYTES` and `ENVAPI_MAX_FILE_WRITE_BYTES`, which are deployment-wide. Person-lowerable idle TTLs and the per-profile cap are also in settings-api (`environments`); unset `ENVAPI_SETTINGS_API_BASE_URL` keeps today's behaviour. |

## Development

```
make check    # format, lint, mypy --strict, import contracts, tests with fail_under = 100
make test
make smoke    # end to end against a running service (see scripts/smoke.sh)
```

The test suite runs every sandbox tier for real. The user tier needs root and `useradd`,
the namespace tier a usable `unshare`; CI runs in a privileged container for that reason.
See `docs/testing.md`.

## Docker

```
make docker   # builds environments-api:local
docker run --privileged --init -p 8008:8008 -v envapi:/var/lib/envapi \
  -e ENVAPI_KEYRING_BASE_URL=http://keyring:8001 -e ENVAPI_KEYRING_ISSUER=… \
  -e ENVAPI_KEYRING_SERVICE_TOKEN=… \
  environments-api:local
```

`--init` is not optional in practice. A sandboxed command's children outlive the shell
that started them and are reparented to PID 1, which in this image is uvicorn and never
reaps them; without an init they pile up as zombies until the sandbox cannot fork. The
meta-repo's compose file sets `init: true` for the same reason.

The build fetches `keyring-client` and `settings-client` from their tagged git sources.
The `github_token` BuildKit secret is only needed when those repositories are private; it
exists for that one `RUN` and is never written to a layer.

`--privileged` (or at least `--cap-add SYS_ADMIN --security-opt seccomp=unconfined`) is
what lets the namespace tier come up inside a container; without it the service runs at
the user tier, and `/health/ready` says so.
