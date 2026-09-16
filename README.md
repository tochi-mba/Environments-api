# environments-api

Sandboxed environments and shells, as an API. The third service alongside `keyring-api`
(identity and credentials) and `web-search-api`: it creates isolated workspaces, runs
shells in them, reports what is running, and lets an assistant kill or wait on it.

This service executes arbitrary commands on behalf of remote callers. It is remote code
execution as a product, and the sandbox tiers, path containment and PID-ownership checks
are the feature, not polish. Read `docs/security.md` before deploying it.

## Quick start

```
make install                      # uv sync --all-groups
cp .env.example .env              # point ENVAPI_KEYRING_* at keyring
make run                          # uvicorn on :8008
curl localhost:8008/health/ready  # reports the active sandbox tier
```

Token verification and credential resolution use `keyring-client`, which is not yet
published: `make install` takes it from `../Keyring-api/clients/python`, so check Keyring-api
out beside this repository. Per-person settings use `settings-client` from
`../Settings-api/clients/python` the same way; unset `ENVAPI_SETTINGS_API_BASE_URL` keeps
today's behaviour.

Without a keyring to hand, `uv run python scripts/dev_keyring.py` serves a stand-in on
`:8001` that mints tokens (`POST /dev/mint {"account_id": "me"}`) and accepts the service
token `.env.example` sets, and `make smoke` drives the whole API against it. The stand-in is
keyring's shared test fake, so it refuses what keyring refuses.

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
* Files API with resolve-then-check containment.
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
  errors.py          DomainError → application/problem+json
  keyring/           header rule and one refusal over keyring-client, credential env mapping
  sandbox/           protocol, detection, directory/user/namespace tiers, setup.sh
  shells/            ring buffer, framing, redaction, the Shell process wrapper
  environments/      records, store, quotas, service, reaper logic
  preferences.py     the one place settings-api is spoken to
  processes.py       /proc listing and the ownership guard
  files.py paths.py  files API and containment
  api/               deps, schemas, routes
docs/                architecture, api, security, sandbox, keyring, testing
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
| `ENVAPI_MAX_*`, `*_IDLE_TTL_SECONDS` | Quotas; per-account overrides via `/v1/admin/quotas`. Person-lowerable idle TTLs and the per-profile cap are also in settings-api (`environments`); unset `ENVAPI_SETTINGS_API_BASE_URL` keeps today's behaviour. |

## Development

```
make check    # format, lint, mypy --strict, tests with fail_under = 100
make test
make smoke    # end to end against a running service (see scripts/smoke.sh)
```

The test suite runs every sandbox tier for real. The user tier needs root and `useradd`,
the namespace tier a usable `unshare`; CI runs in a privileged container for that reason.
See `docs/testing.md`.

## Docker

```
GITHUB_TOKEN="$(gh auth token)" docker build --secret id=github_token,env=GITHUB_TOKEN -t environments-api .
docker run --privileged -p 8008:8008 -v envapi:/var/lib/envapi \
  -e ENVAPI_KEYRING_BASE_URL=http://keyring:8001 -e ENVAPI_KEYRING_ISSUER=… \
  -e ENVAPI_KEYRING_SERVICE_TOKEN=… \
  environments-api
```

Until `keyring-client` and `settings-client` are published the image cannot be built from this
repository alone: `uv sync` resolves them from `../Keyring-api/clients/python` and
`../Settings-api/clients/python`, which are outside the build context.

`--privileged` (or at least `--cap-add SYS_ADMIN --security-opt seccomp=unconfined`) is
what lets the namespace tier come up inside a container; without it the service runs at
the user tier, and `/health/ready` says so.
