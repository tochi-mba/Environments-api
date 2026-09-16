# Architecture

`environments-api` creates isolated environments, runs shells in them, and reports what is
running. It is remote code execution as a product, and every design choice below follows
from that.

## Modules

| Module | Responsibility |
|---|---|
| `app/settings.py` | `ENVAPI_`-prefixed configuration, every quota default. |
| `app/errors.py` | `DomainError` subclasses and their RFC 9457 `application/problem+json` rendering. Routes never touch status codes. |
| `app/keyring/` | This service's side of `keyring-client`: which header carries the user token, one refusal for every rejected token, and keyring's credential answer as environment variables. |
| `app/sandbox/` | The `Sandbox` protocol, capability detection, and the three tiers. |
| `app/paths.py`, `app/files.py` | Resolve-then-check path containment and the files API. |
| `app/shells/` | Ring buffer, command framing, secret redaction, and the `Shell` process wrapper. |
| `app/procfs.py`, `app/processes.py` | `/proc` inspection and the process-ownership guard. |
| `app/preferences.py` | The one place settings-api is spoken to. Per-person idle TTLs, the per-profile cap, and `common.default_profile`. |
| `app/environments/` | Durable records, the on-disk store, quotas, the orchestrating service, and the reaper's work. |
| `app/audit.py` | Append-only JSON-lines log of every privileged action. |
| `app/api/` | Dependencies, request schemas, routes. |
| `app/main.py` | App factory and lifespan: probe the host, build the sandbox, load and reconcile, start the reaper. |

## The model

```
Account (keyring `sub`)
└── Profile (X-Keyring-Profile header, or `common.default_profile` / `ENVAPI_DEFAULT_PROFILE`)
    └── Environment  — durable folder, labels, declared credentials, limits, state
        ├── workspace/           the only writable place a shell gets
        ├── logs/<cmd>.log|.json full output and metadata per command
        ├── environment.json     the record, written atomically
        └── Shell (0..n, concurrent)  — a bash process; not durable
            └── Command (serial)      — one at a time per shell
```

Environments survive a restart; shells do not. After a restart every shell that was
running comes back `dead` with `dead_reason: "service_restarted"`, and the process tree it
left behind is killed during startup reconciliation (see below).

## Request flow

1. `get_caller` checks the optional `X-API-Key` gate, then verifies the user token from
   `Authorization: Bearer` (or, for one release, `X-Keyring-User-Token`) locally against
   keyring's JWKS with keyring-client. `sub` becomes the account id; `iss` must equal
   `ENVAPI_KEYRING_ISSUER` and `aud` must equal `ENVAPI_KEYRING_SERVICE_NAME`. Every
   refusal is the same 401.
2. Every service method takes the `Caller` and refuses, with a 404, anything the account
   does not own. That one check is the isolation between accounts.
3. Blocking work (spawning, waiting, killing with a grace period, disk scans) runs in a
   worker thread via `asyncio.to_thread`; the shell core is synchronous and thread-safe.

## Running a command

A persistent shell does not say when a command finishes, so the service makes it say so.
For each command it writes to the shell's stdin:

```
[VAR=value ...] eval '<command, single-quoted>'; printf '\036%s:%d\036' <nonce> $?
```

* `eval` of a quoted literal keeps the caller's quoting intact, lets `cd` and variables
  persist, and turns an unterminated quote into a syntax error with a frame rather than a
  shell that hangs.
* The frame is delimited by the ASCII record separator, a byte that essentially never
  appears in real output. The reader (`FrameParser`) scans for it across chunk boundaries.
* The nonce is random per command. A frame with the wrong nonce is ordinary output, which
  is what makes the reported exit code trustworthy.
* Temporary assignments before `eval` scope injected credentials to that one command.

Stdout and stderr are merged at the OS level (one pipe or one pty), which preserves
ordering. Output flows through the redactor, then into a bounded ring buffer and the
command's log file.

## Signals and process trees

Every shell runs in its own session and process group. Closing a shell SIGTERMs the group,
waits `ENVAPI_SHELL_CLOSE_GRACE_SECONDS`, then SIGKILLs; anything that escaped the group via
`setsid()` is caught by a `/proc` descendant walk. Signalling the *current command* sends
the signal to every descendant of the shell except the shell lineage itself (the shell and,
under the namespace tier, the `unshare` wrapper above it), so `SIGTERM` interrupts the
command and the shell survives to run the next one.

## Startup reconciliation

`environment.json` records each shell's `pid`, `pgid` and the process start time from
`/proc/<pid>/stat`. On startup, for each shell recorded as running: if the pid still
exists *and* its start time matches, it is ours and its group is killed; otherwise it is
left alone, because a pid alone proves nothing once the kernel recycles it. Either way the
record becomes `dead` / `service_restarted`.

## The reaper

A background task runs `EnvironmentService.reap()` every `ENVAPI_REAPER_INTERVAL_SECONDS`.
It closes shells idle longer than the environment's stamped `shell_idle_ttl_seconds` (or
the account's QuotaStore / deployment value when the record has none -- records created
while settings-api was off), archives environments idle longer than the matching
environment TTL (workspace and logs wiped, record kept; `reset` revives), prunes logs
oldest-first to `max_command_log_bytes`, and refreshes the per-environment disk usage that
`max_disk_bytes` is enforced against. The reaper has no user token, so it never calls
settings-api: idle lifetimes are resolved at create and stored on the record. Without it
the service would leak processes and disk until the box fell over.
