# API

Every error is `application/problem+json` (RFC 9457) with a stable `code` and, where it
helps, extension members such as `limit`/`current`/`maximum` on quota errors or `path` on
containment errors.

## Headers

| Header | Required | Meaning |
|---|---|---|
| `X-Keyring-User-Token` | yes | Keyring service token minted for `environments-api`. Verified locally. |
| `X-Keyring-Profile` | no | Which profile; defaults to `ENVAPI_DEFAULT_PROFILE`. |
| `X-API-Key` | when `ENVAPI_API_KEYS` is set | Optional front-door gate. |

## Health

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Always 200 while the process is up. |
| GET | `/health/ready` | Active sandbox tier and keyring state. 503 only when keyring is unreachable *and* no signing key was ever cached. `keyring.status` is `fresh` (fetched now), `cached` (within TTL, keyring not contacted) or `unreachable`. |

## Environments

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/environments` | Create. `{name, labels, credentials[], network, limits}` |
| GET | `/v1/environments` | List. `?profile=&state=&label=key[=value]` |
| GET | `/v1/environments/{id}` | Detail |
| GET | `/v1/environments/{id}/summary` | Environment + shells + processes + recent commands |
| GET | `/v1/environments/{id}/usage` | Disk, shells, processes against limits (fresh disk scan) |
| DELETE | `/v1/environments/{id}` | Kill everything, remove the folder |
| POST | `/v1/environments/{id}/reset` | Wipe the workspace, keep the environment; revives an archived one |

`limits` may set `max_memory_bytes`, `max_cpu_seconds`, `max_file_size_bytes`,
`max_processes_per_shell`; each must be at or below the account's quota or the create
returns 409 naming it. `network: true` is refused with `network_disabled` when the
deployment has `ENVAPI_ALLOW_NETWORK=false`.

## Shells

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/environments/{id}/shells` | Open. `{cwd, env, pty}` |
| GET | `/v1/environments/{id}/shells` | List (shells known to this process, live or not) |
| GET | `/v1/shells/{sid}` | State, `pid` (root process), `shell_pid`, `pgid`, current command, cursor |
| DELETE | `/v1/shells/{sid}` | Close: SIGTERM the group, SIGKILL after the grace period |
| POST | `/v1/shells/{sid}/exec` | Run. `{command, timeout_ms, wait_ms}` |
| GET | `/v1/shells/{sid}/output` | Poll. `?cursor=&max_bytes=&wait_ms=` |
| POST | `/v1/shells/{sid}/wait` | Block until idle or `timeout_ms` |
| POST | `/v1/shells/{sid}/signal` | `{signal}` (number, `TERM` or `SIGTERM`) to the current command's processes |
| POST | `/v1/shells/{sid}/stdin` | `{data, encoding: utf-8|base64, target: stdin|tty}` |
| GET | `/v1/shells/{sid}/commands` | History for this process's lifetime |
| GET | `/v1/commands/{cid}` | One command with output from its log; `?offset=&max_bytes=` |

`exec` returns immediately with the command record. With `wait_ms` it blocks up to that
long and, if the command finished, the same response carries `output`, `exit_code` and
`state: "exited"`; otherwise `state: "running"` and the caller polls. `timeout_ms` is a
hard deadline: on expiry the command's processes are SIGKILLed and the state becomes
`timed_out`; if nothing below the shell could be killed (a builtin loop), the shell itself
is killed a second later.

Command `state` is one of `running`, `exited`, `timed_out`, `shell_died`. A command that
ends the shell (`exit 3`) is `shell_died` with `exit_code: 3`; a shell killed by a signal
leaves `exit_code: null`.

A second `exec` while one is running returns 409 `shell_busy` with the running
`command_id`. A shell is serial by nature; the service says so rather than queueing.

`output` returns bytes from `cursor`, the `next_cursor`, the `end` offset, the shell
state and `dropped_bytes` when the cursor is older than what the ring buffer still holds.
Output is never lost silently: the full stream is also in the command's log, retrievable
through `/v1/commands/{cid}` even after the buffer rolled or the service restarted.

`pty: true` gives commands a pseudo-terminal as stdout/stderr and controlling tty (so
`isatty(1)` is true and `/dev/tty` prompts work) while stdin stays a pipe, which keeps the
shell non-interactive and never echoes what the service feeds it. Write to the terminal
with `target: "tty"`.

## Processes

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/environments/{id}/processes` | Every live process: pid, ppid, pgid, state, rss_bytes, cpu_seconds, elapsed_seconds, cmdline, owning shell_id |
| GET | `/v1/shells/{sid}/processes` | Just that shell's tree |
| POST | `/v1/environments/{id}/processes/{pid}/signal` | Signal one process. Refused (404) unless its ancestry leads to a live shell of this environment. |

## Files

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/environments/{id}/files?path=` | List a directory (symlinks reported, not followed) |
| GET | `/v1/environments/{id}/files/content?path=&offset=&max_bytes=` | Read, capped, `truncated` reported; text as UTF-8, else base64 |
| PUT | `/v1/environments/{id}/files/content` | `{path, content, encoding, mode: overwrite|append}` |

Every path is resolved with symlinks followed and then checked for containment; anything
outside the workspace is 400 `path_outside_workspace`.

## Convenience

`POST /v1/exec` `{environment_id, command, timeout_ms, cwd, env, pty, max_output_bytes}`
opens an ephemeral shell, runs the command in a subshell, waits, returns output and exit
code, and closes the shell whatever happened. The single most useful shape for an MCP tool.

## Admin (`ENVAPI_OPERATOR_ACCOUNTS` only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/admin/quotas/{account}` | Effective quotas, overrides, defaults |
| PUT | `/v1/admin/quotas/{account}` | `{overrides: {...}}`; empty clears |
| GET | `/v1/admin/environments` | Every environment on the deployment |
| GET | `/v1/admin/audit?limit=&account_id=` | Tail of the audit log |

## Error codes

`unauthorized`, `forbidden`, `not_found`, `validation_error`, `path_outside_workspace`,
`conflict`, `shell_busy`, `shell_not_running`, `environment_archived`, `network_disabled`,
`quota_exceeded`, `keyring_unavailable`, `sandbox_error`, `internal_error`.
