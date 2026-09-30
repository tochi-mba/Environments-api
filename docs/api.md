# API

Every error the service raises is `application/problem+json` (RFC 9457) with a stable
`code` and, where it helps, extension members such as `limit`/`current`/`maximum` on quota
errors, `errors` on request validation, or `occurrences` on an ambiguous edit. Two answers
come from the framework instead and are plain JSON (`{"detail": "Not Found"}`): a path no
route matches (404) and a method a route does not have (405).

## Headers

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer <token>` | yes | Keyring user token minted for `environments-api`. Verified locally; see `keyring.md`. |
| `X-Keyring-User-Token` | deprecated | The same token, accepted on its own for one release. Sent with `Authorization` as well, both must carry the same token. |
| `X-Keyring-Profile` | no | Which profile. When omitted: the person's `common.default_profile` if settings-api is configured, otherwise `ENVAPI_DEFAULT_PROFILE`. A configured settings-api that cannot be reached refuses rather than guessing `personal` (503 `preferences_unavailable`). |
| `X-API-Key` | when `ENVAPI_API_KEYS` is set | Optional front-door gate. `Authorization` never satisfies it. A missing or wrong key is 401 with detail `missing or invalid API key`. |
| `X-Request-ID` | no | Echoed on the response; one is generated when absent. It is bound to every log line the request produces. |
| `If-Match` | no | On file mutations: the `ETag` a read returned. A mismatch is 412 `file_changed` and nothing is written. `*` matches any existing file. |

Every token refusal is the same 401 body with `detail: "the token was not accepted"`,
whichever rule refused it. Which rule it was is only in the service's log.

## Health

| Method | Path | Notes |
|---|---|---|
| GET | `/healthy`, `/health` | Always 200 while the process is up: `{"status": "ok", "service": "environments-api"}`. |
| GET | `/ready`, `/health/ready` | `status` (`ready` or `not_ready`), `sandbox_tier`, `min_sandbox_tier`, `allow_network`, and keyring state, read from keyring's JWKS document. `keyring.status` is `ok` (keys held or just fetched), `stale` (keyring unreachable; keys already held still verify tokens) or `unreachable` (no usable key, and 503). `keyring.error` is fixed text or `null`. |

`/healthy` and `/ready` are the family's names; `/health` and `/health/ready` are the names
this service shipped with, kept as aliases for the probes and runbooks already using them.
Only the `/health` spellings appear in the OpenAPI document.

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
| POST | `/v1/shells/{sid}/exec` | Run. `{command, timeout_ms, wait_ms}`; `timeout_ms` has no default (no deadline) and is at most 24 hours, `wait_ms` at most 10 minutes |
| GET | `/v1/shells/{sid}/output` | Poll. `?cursor=&max_bytes=&wait_ms=`; `max_bytes` defaults to 64 KiB and is capped at `ENVAPI_MAX_FILE_READ_BYTES`, `wait_ms` at most 60000 |
| POST | `/v1/shells/{sid}/wait` | Block until idle or `timeout_ms` (default 30000, at most 10 minutes) |
| POST | `/v1/shells/{sid}/signal` | `{signal}` (number, `TERM` or `SIGTERM`) to the current command's processes |
| POST | `/v1/shells/{sid}/stdin` | `{data, encoding: utf-8|base64, target: stdin|tty}` |
| GET | `/v1/shells/{sid}/commands` | History for this process's lifetime |
| GET | `/v1/commands/{cid}` | One command with output from its log; `?offset=&max_bytes=` |

`exec` returns immediately with the command record. With `wait_ms` it blocks up to that
long and, if the command finished, the same response carries `output`, `exit_code` and
`state: "exited"`; otherwise `state: "running"` and the caller polls. `output` is the start
of the command's output, at most `ENVAPI_MAX_FILE_READ_BYTES` of it; `output_truncated_bytes` counts what its cap left out and
`output_dropped_bytes` what the ring buffer had already evicted. `timeout_ms` is a
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

All under `/v1/environments/{id}/files`. Paths are workspace-relative (an absolute path
inside the workspace is accepted too).

| Method | Path | Purpose |
|---|---|---|
| GET | `` | List. `?path=&glob=&depth=`; `depth` 0 to 10 (default 0, one level). Entries are `{name, path, kind, size, mtime}` with `kind` one of `file`, `directory`, `symlink`, `other`; symlinks are reported, never followed |
| GET | `/content` | Read a window. `?path=&offset=&max_bytes=`, capped at `ENVAPI_MAX_FILE_READ_BYTES`. Returns `{path, size, offset, content, encoding, truncated, is_binary, etag, next_offset}` and an `ETag` header |
| GET | `/search` | Literal text search, not a regex. `?pattern=&path=&glob=&depth=&mode=&limit=&before=&after=&max_file_bytes=` |
| PUT | `/content` | Write. `{path, content, encoding: utf-8|base64, mode: overwrite|append}`; creates parent directories |
| POST | `/edit` | Replace one exact occurrence. `{path, old_string, new_string}` |
| POST | `/patch` | Apply a single-file unified diff. `{path, patch}` |
| POST | `/directories` | Create a directory and its missing parents. `{path}`; an existing directory succeeds |
| DELETE | `/content` | Delete. `?path=&recursive=`; a non-empty directory needs `recursive=true` |
| POST | `/copy`, `/move` | `{source, destination}`: one regular file; the destination must not exist |

**Paths.** Every path is resolved and checked for containment, and then refused if any part
of it as spelled is a symbolic link, even one that points inside the workspace. Every step
below the workspace is opened by descriptor with `O_NOFOLLOW`, so a link a shell plants
between the check and the open is not followed either. Anything outside the workspace, or
through a link, is 400 `path_outside_workspace`.

**Reads.** Whether a file is text is decided over the whole file, not the window: a NUL byte
or invalid UTF-8 anywhere makes it binary, and binary content comes back base64. A UTF-8
window ends on a character boundary; an `offset` inside a character, or a `max_bytes` too
small for one, is 422. Continue from `next_offset` while `truncated` is true. The `etag` is
the SHA-256 of the whole file.

**Writes.** Every mutation that produces a file (write, edit, patch, copy, move) returns
`{path, size, etag, diff, applied_hunks, rejected_hunks}` with an `ETag` header and takes an
optional `If-Match`. Write, edit and patch replace the file atomically (a temporary file,
then a rename), so a reader never sees half of it; copy and move create the destination
exclusively and never overwrite. Content is at most `ENVAPI_MAX_FILE_WRITE_BYTES`; for an
append that is the size of the whole result. Edits, patches, copies and moves work on files
no larger than that, and edits and patches only on UTF-8 text.

* `edit`: `old_string` must occur exactly once. Otherwise nothing changes and the 422 lists
  the line numbers it was found on (`occurrences`, empty when it was not found), so the
  caller can add context and retry. `diff` is the unified diff of the change.
* `patch`: a malformed patch (bad header, miscounted hunk, hunks out of order or
  overlapping, more than one file) is 422 before anything is written. A well-formed hunk
  whose context does not match the file is **not** applied and is listed, one-indexed, in
  `rejected_hunks`, while the hunks that do match are written. Check `rejected_hunks`
  before assuming the whole patch landed.
* `copy` and `move`: an existing destination is 409 `conflict`; `If-Match` applies to the
  source.
* `DELETE`: the workspace root is refused (use `reset`), and `If-Match` is refused on a
  directory.

**Search.** `mode` is `content` (default; each match with `before`/`after` lines of context,
at most 20 each) or `files_with_matches` (one entry per file). `depth` defaults to 10,
`limit` to 100 (at most 1000), and files over `max_file_bytes` (default 1 MiB, at most
16 MiB) are skipped. The result is `{matches, total_matches, truncated, skipped_binary,
skipped_large, skipped_unavailable}`: every file it did not read is named, and `truncated`
says `total_matches` exceeded `limit`. A returned line is clipped to 2000 characters, with
its full length in `original_chars`.

## Convenience

`POST /v1/exec`
`{environment_id, command, timeout_ms, cwd, env, pty, max_output_bytes, output_window}`
(`timeout_ms` defaults to 60000 and is at most 10 minutes; `max_output_bytes` defaults to
256 KiB and is at most 8 MiB) opens an ephemeral shell, runs the command in a subshell, waits, returns output and exit
code, and closes the shell whatever happened. The single most useful shape for an MCP tool.
The response also carries `shell_state`, `credentials_injected` and `credentials_missing`
(the declared services keyring holds nothing for).
`command` in the response is the command as sent; the subshell around it appears only in
the audit log, which records what the shell ran.

At most `max_output_bytes` of output come back. `output_window` says which end: `head`
(the default) keeps the first bytes, `tail` the last, which is where a test run or a build
prints its verdict. `output_truncated_bytes` counts the bytes that cap left out and
`output_dropped_bytes` those the ring buffer had already evicted; with the bytes in `output`
they account for every byte the command wrote before the read.

## Admin (`ENVAPI_OPERATOR_ACCOUNTS` only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/admin/quotas/{account}` | Effective quotas, overrides, defaults |
| PUT | `/v1/admin/quotas/{account}` | `{overrides: {...}}`; empty clears |
| GET | `/v1/admin/environments` | Every environment on the deployment |
| GET | `/v1/admin/audit?limit=&account_id=` | Tail of the audit log |

## Error codes

| Code | Status | When |
|---|---|---|
| `unauthorized` | 401 | Any token refusal, or a missing or wrong `X-API-Key` |
| `forbidden` | 403 | `/v1/admin` for an account not in `ENVAPI_OPERATOR_ACCOUNTS` |
| `not_found` | 404 | Anything the caller does not own, or that does not exist |
| `path_outside_workspace` | 400 | A path that escapes the workspace or goes through a symlink |
| `file_changed` | 412 | `If-Match` did not match the file, or the file changed during the operation |
| `validation_error` | 422 | The request broke a rule; request-shape failures carry `errors` |
| `conflict` | 409 | Generic conflict, such as a copy or move onto an existing file |
| `shell_busy` | 409 | A command is already running in the shell (carries its `command_id`). Also, today, a command over 1 MiB (carries `limit`) and `target: "tty"` on a shell opened without `pty` |
| `shell_not_running` | 409 | The shell has exited |
| `environment_archived` | 409 | The environment was archived for idleness; `reset` revives it |
| `network_disabled` | 409 | `network: true` on a deployment with `ENVAPI_ALLOW_NETWORK=false` |
| `quota_exceeded` | 409 | A limit was hit; carries `limit`, `current`, `maximum` |
| `keyring_unavailable` | 503 | Keyring's keys or a credential could not be fetched |
| `preferences_unavailable` | 503 | settings-api is configured and could not answer |
| `sandbox_error` | 500 | The sandbox could not start the shell or create its user, or the shell stopped accepting input |
| `internal_error` | 500 | A bug; the log has the detail |
