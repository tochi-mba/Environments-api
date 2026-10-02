# Operations

Running environments-api: what to configure, what it needs from the host, and what each
failure means. Read [docs/security.md](security.md) before exposing it to anything.

## What the host must provide

This service runs commands chosen by remote callers. Isolation is tiered and probed at
startup (see [ADR-0001](adr/0001-sandbox-tiers.md)); `/ready` reports the tier in force.

- **Linux.** The sandbox needs `unshare`, `setpriv`, `useradd` and `/proc`. There is no
  Windows or macOS deployment, and the full test suite runs on Linux only.
- **A writable data root** (`ENVAPI_ROOT`) with room for the disk quotas you configure.
  `ENVAPI_MAX_DISK_BYTES` is enforced by a periodic scan, not by filesystem quotas, so any
  filesystem works and a single command can overshoot between scans.
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
| `ENVAPI_API_KEYS` | empty | Optional comma-separated keys accepted in `X-API-Key`, a gate in front of every `/v1` route (the probes stay open) for a deployment that wants a second lock. |
| `ENVAPI_OPERATOR_ACCOUNTS` | empty | Accounts allowed to see the operator surface. Keyring puts no roles in tokens, so this is the documented workaround. |

### Per-person settings

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVAPI_SETTINGS_API_BASE_URL` | unset | Where settings-api is. Unset, everybody gets this configuration as it stands. |
| `ENVAPI_SETTINGS_API_TOKEN` | unset | This service's entry in `SETTINGS_API_SERVICES`, at least 32 characters, with `audience_prefix` `environments-api`. |

Both or neither: half a configuration is a startup error. With them set, creating an
environment reads that person's `environments` namespace — idle lifetimes and the
per-profile cap — clamped to the ceilings below. A person can lower those three for
themselves and never raise them; every ceiling, and every other quota, stays the
operator's. Opening a shell reads `default_shell`: `sh` starts `ENVAPI_SH_BINARY`, and
`bash`, the default, starts `ENVAPI_SHELL_BINARY`. A person picks one of those two
names, never a path.

### Sandbox and shells

| Variable | Default | Notes |
| --- | --- | --- |
| `ENVAPI_ROOT` | `./data` | Where environments live. |
| `ENVAPI_MIN_SANDBOX_TIER` | `directory` | The weakest tier this deployment accepts. |
| `ENVAPI_ALLOW_NETWORK` | `true` | Whether sandboxed commands may reach the network. Enforced only at the namespace tier; below it, `false` is recorded but not enforced. |
| `ENVAPI_SHELL_BINARY` | `/bin/bash` | The shell started in an environment, and what `environments.default_shell: bash` means. |
| `ENVAPI_SH_BINARY` | `/bin/sh` | What a person who chose `environments.default_shell: sh` gets. Blank turns the choice off. A chosen shell this host does not have falls back to `ENVAPI_SHELL_BINARY` and logs `chosen_shell_not_installed`. |
| `ENVAPI_SHELL_IDLE_TTL_SECONDS` | `3600` | How long an idle shell is kept. |
| `ENVAPI_ENVIRONMENT_IDLE_TTL_SECONDS` | `86400` | How long an idle environment is kept. |
| `ENVAPI_REAPER_INTERVAL_SECONDS` | `60` | How often idleness is checked. |
| `ENVAPI_SHELL_CLOSE_GRACE_SECONDS` | `5` | How long a closing shell gets before it is killed. |

### Quotas

All operator-owned, all per the unit named. Each can be overridden per account through
`PUT /v1/admin/quotas/{account}` except the two file-API caps.

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
| `ENVAPI_MAX_FILE_READ_BYTES` / `_WRITE_BYTES` | 1 MiB / 16 MiB (deployment-wide; not overridable per account) |
| `ENVAPI_MAX_OUTPUT_BUFFER_BYTES` | 1 MiB |
| `ENVAPI_MAX_COMMAND_LOG_BYTES` | 32 MiB |

### Logging

| Variable | Default |
| --- | --- |
| `ENVAPI_LOG_JSON` | `true` |
| `ENVAPI_LOG_LEVEL` | `INFO` |

## Running it

```bash
cp .env.example .env
make run                                   # uvicorn on :8008, every interface
curl localhost:8008/ready                  # the sandbox tier and whether tokens verify
```

In a container, the sandbox needs privileges the default profile does not grant:

```bash
make docker
docker run --privileged --init -p 8008:8008 -v envapi:/var/lib/envapi \
  -e ENVAPI_KEYRING_BASE_URL=http://keyring:8001 \
  -e ENVAPI_KEYRING_ISSUER=http://127.0.0.1:8001 \
  -e ENVAPI_KEYRING_SERVICE_TOKEN=... \
  environments-api:local
```

The image runs as root (the user and namespace tiers need it to create per-environment
users) and sets `ENVAPI_ROOT=/var/lib/envapi`, a `VOLUME` there, and
`ENVAPI_MIN_SANDBOX_TIER=user`. Do not hand it the `.env` copied from `.env.example` with
`--env-file`: its `ENVAPI_ROOT=./data` and `ENVAPI_MIN_SANDBOX_TIER=directory` override the
image's, which puts every environment outside the volume and lets the container start at
the weakest tier. Pass the keyring settings on their own, as above.

`--init` matters as much as `--privileged`. The children a sandboxed command leaves behind
are reparented to PID 1, which is uvicorn, and uvicorn never reaps them; without an init
process they accumulate as zombies until the sandbox answers every command with
`fork: Resource temporarily unavailable`. The meta-repo's compose file sets `init: true`.

The image declares no `HEALTHCHECK`; point the orchestrator's liveness check at `/healthy`
(the meta-repo's compose file does).

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
| Startup fails on the sandbox tier | The host cannot reach `ENVAPI_MIN_SANDBOX_TIER` | Grant the container the privileges, or lower the floor deliberately. |
| Every request 401 | `ENVAPI_KEYRING_ISSUER` or `ENVAPI_KEYRING_SERVICE_NAME` disagrees with keyring, or `ENVAPI_API_KEYS` is set and the caller sends no `X-API-Key` | Make the issuer equal keyring's `KEYRING_ISSUER` and mint tokens for the service name; send the API key. |
| `/ready` says keyring unreachable | The key document cannot be fetched | Check the URL is reachable from this host; held keys keep working for a bounded grace. |
| A command cannot get its credential | No `ENVAPI_KEYRING_SERVICE_TOKEN`, or the profile has no such connection (listed in `credentials_missing`) | Register this service in keyring and connect the service on that profile. |
| Exec output looks corrupted | Redaction replaced a value that also appears in ordinary output | Expected: a credential's value is scrubbed wherever it appears. |
| Commands fail with `fork: Resource temporarily unavailable` after hours of use | Zombie processes under PID 1 in a container started without an init | Run with `--init` (compose: `init: true`). |
| Environments vanish after a container restart | `ENVAPI_ROOT` points outside the volume, often `./data` from a copied `.env` | Set `ENVAPI_ROOT=/var/lib/envapi` (the image default) and mount a volume there. |
