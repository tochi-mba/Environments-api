# Operations

Running environments-api: what to configure, what it needs from the host, and what each
failure means. Read [docs/security.md](security.md) before exposing it to anything.

## What the host must provide

This service runs commands chosen by remote callers. Isolation is tiered and probed at
startup (see [ADR-0001](adr/0001-sandbox-tiers.md)); `/ready` reports the tier in force.

- **Linux.** The sandbox needs `unshare`, `setpriv`, `useradd` and `/proc`. There is no
  Windows or macOS deployment, and the full test suite runs on Linux only.
- **A writable data root** (`ENVAPI_ROOT`), on a filesystem that supports the quotas you
  configure.
- Set `ENVAPI_MIN_SANDBOX_TIER` to the weakest tier the deployment may run at. A host that
  cannot reach it fails startup, which is the point: a weakly isolated deployment must not
  be indistinguishable from a strong one.

## Configuration

Every variable is prefixed `ENVAPI_`. A prefixed variable that names no setting is a
**startup error** naming every offender, so a typo fails loudly instead of leaving a
default in place. Values are never repeated in the message — the value a typo was aimed at
is as likely as not the service token. `ENVAPI_ENVIRONMENT_ID` and `ENVAPI_URL` are
exceptions: this service sets the first in every shell it starts, and `scripts/smoke.sh`
reads the second.

### Identity

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVAPI_KEYRING_BASE_URL` | `http://127.0.0.1:8001` | Where keyring is. |
| `ENVAPI_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Who signs the tokens, which behind a proxy is not where the keys are fetched. Must equal keyring's `KEYRING_ISSUER` exactly, or every token is refused. |
| `ENVAPI_KEYRING_SERVICE_NAME` | `environments-api` | The audience every user token must carry. Keyring's internal endpoint requires it to be this service's name in `KEYRING_SERVICE_TOKENS`, so change both or neither. |
| `ENVAPI_KEYRING_SERVICE_TOKEN` | empty | This service's entry in keyring's `KEYRING_SERVICE_TOKENS`. At least 32 characters. Empty is allowed: tokens still verify, and only an environment that declares credentials calls keyring's internal endpoint. |
| `ENVAPI_KEYRING_TIMEOUT_SECONDS` | `5` | Per call to keyring. |
| `ENVAPI_JWKS_CACHE_SECONDS` | `3600` | How long keyring's public keys are held before being read again. Held keys keep verifying tokens through a bounded outage. |
| `ENVAPI_JWKS_MIN_REFETCH_SECONDS` | `60` | Floor between the key fetches an unknown key id may provoke, and between a failed fetch and the next. Not a tuning knob. |
| `ENVAPI_DEFAULT_PROFILE` | `personal` | Which credential profile is meant when a request names none. |
| `ENVAPI_API_KEYS` | empty | Optional comma-separated gate in front of everything, for a deployment that wants a second lock. |
| `ENVAPI_OPERATOR_ACCOUNTS` | empty | Accounts allowed to see the operator surface. Keyring puts no roles in tokens, so this is the documented workaround. |

### Per-person settings

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVAPI_SETTINGS_API_BASE_URL` | unset | Where settings-api is. Unset, everybody gets this configuration as it stands. |
| `ENVAPI_SETTINGS_API_TOKEN` | unset | This service's entry in `SETTINGS_API_SERVICES`, at least 32 characters, with `audience_prefix` `environments-api`. |

Both or neither: half a configuration is a startup error. With them set, creating an
environment reads that person's `environments` namespace — idle lifetimes and the
per-profile cap — clamped to the ceilings below. Every quota stays operator-only.

### Sandbox and shells

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVAPI_ROOT` | `./data` | Where environments live. |
| `ENVAPI_MIN_SANDBOX_TIER` | `directory` | The weakest tier this deployment accepts. |
| `ENVAPI_ALLOW_NETWORK` | `true` | Whether sandboxed commands may reach the network. |
| `ENVAPI_SHELL_BINARY` | `/bin/bash` | The shell started in an environment. |
| `ENVAPI_SHELL_IDLE_TTL_SECONDS` | `3600` | How long an idle shell is kept. |
| `ENVAPI_ENVIRONMENT_IDLE_TTL_SECONDS` | `86400` | How long an idle environment is kept. |
| `ENVAPI_REAPER_INTERVAL_SECONDS` | `60` | How often idleness is checked. |
| `ENVAPI_SHELL_CLOSE_GRACE_SECONDS` | `5` | How long a closing shell gets before it is killed. |

### Quotas

All operator-owned, all per the unit named.

| Variable | Default |
| --- | --- |
| `ENVAPI_MAX_ENVIRONMENTS_PER_PROFILE` | `5` |
| `ENVAPI_MAX_ENVIRONMENTS_PER_ACCOUNT` | `20` |
| `ENVAPI_MAX_SHELLS_PER_ENVIRONMENT` | `8` |
| `ENVAPI_MAX_PROCESSES_PER_SHELL` | `256` |
| `ENVAPI_MAX_MEMORY_BYTES` | 2 GiB |
| `ENVAPI_MAX_DISK_BYTES` | 5 GiB |
| `ENVAPI_MAX_CPU_SECONDS` | `900` |
| `ENVAPI_MAX_FILE_SIZE_BYTES` | 512 MiB |
| `ENVAPI_MAX_FILE_READ_BYTES` / `_WRITE_BYTES` | 1 MiB / 16 MiB |
| `ENVAPI_MAX_OUTPUT_BUFFER_BYTES` | 1 MiB |
| `ENVAPI_MAX_COMMAND_LOG_BYTES` | 32 MiB |

### Logging

| Variable | Default |
| --- | --- |
| `ENVAPI_LOG_JSON` | `true` |
| `ENVAPI_LOG_LEVEL` | `INFO` |

## Running it

```bash
make run                                   # uvicorn on :8008
curl localhost:8008/ready                  # the sandbox tier and whether tokens verify
```

In a container, the sandbox needs privileges the default profile does not grant:

```bash
make docker
docker run --privileged -p 8008:8008 -v envapi:/var/lib/envapi --env-file .env environments-api:local
```

`--privileged` is why this service documents its own deployment rather than inheriting the
family's: every sibling's image runs unprivileged, and this one cannot.

## Probes

| Route | What it means |
| --- | --- |
| `GET /health`, `GET /healthy` | The process is up. No I/O, never fails. This is what a container healthcheck should call. |
| `GET /health/ready`, `GET /ready` | The sandbox tier in force, and whether a token could be verified right now. 503 when no usable signing key is held. This is what a load balancer should call. |

Readiness asks keyring's **key document**, never keyring's own `/healthy`: that answers 503
whenever any stored credential is unusable, which says nothing about whether a token can be
verified here.

## What each failure means

| Symptom | Cause | Fix |
| --- | --- | --- |
| Startup fails naming variables | A typo, or a setting that does not exist | The message names every offender. |
| Startup fails on the sandbox tier | The host cannot reach `MIN_SANDBOX_TIER` | Grant the container the privileges, or lower the floor deliberately. |
| Every request 401 | `KEYRING_ISSUER` or `KEYRING_SERVICE_NAME` disagrees with keyring | Make both match keyring's configuration. |
| `/ready` says keyring unreachable | The key document cannot be fetched | Check the URL is reachable from this host; held keys keep working for a bounded grace. |
| A command cannot get its credential | No `KEYRING_SERVICE_TOKEN`, or the profile has no such connection | Register this service in keyring and connect the service on that profile. |
| Exec output looks corrupted | Redaction replaced a value that also appears in ordinary output | Expected: a credential's value is scrubbed wherever it appears. |
