# Security

This service executes arbitrary commands on behalf of remote callers. A folder per
environment is organisation, not a boundary; a process in it can still `cd /` and read
whatever the service user can read. The sandbox tiers, the path containment checks and the
PID-ownership checks are the feature, not polish.

## Identity

* Only keyring-minted RS256 tokens are accepted, verified locally against keyring's JWKS by
  `keyring-client`, the verifier the whole family shares. The issuer and audience are
  pinned, every claim keyring mints is required, and expiry is judged by an injected clock.
  `alg: none`, HS256 signed with the public key, forged signatures, another issuer or
  audience, expiry, a missing key id and unknown key ids are all rejected (each is a named
  test with real signatures).
* Every refusal is one identical 401 body; which rule refused a token is only in the log,
  so a forger learns nothing from the difference. Keys that cannot be fetched are a 503
  with fixed text, and no keyring URL or transport error reaches a response or a log line.
* An unknown key id provokes at most one JWKS fetch per `ENVAPI_JWKS_MIN_REFETCH_SECONDS`,
  so invented key ids cannot turn inbound requests into requests to keyring.
* The user token travels in `Authorization: Bearer`. The legacy `X-Keyring-User-Token` is
  accepted on its own for one release; a request carrying both with different tokens, or an
  `Authorization` header in any other shape, is refused.
* `ENVAPI_KEYRING_SERVICE_TOKEN` is checked at startup (32+ characters, no surrounding
  whitespace) and never appears in a representation, dump or error; neither does a caller's
  token or an injected credential. An unknown `ENVAPI_` variable stops the service starting.
* The account id (`sub`) namespaces every environment. Every lookup checks the owner and
  answers 404 for anything else, so a caller cannot learn whether another account's
  environment exists.
* Keyring's roles are not visible in its tokens, so `/v1/admin` is gated by a local
  `ENVAPI_OPERATOR_ACCOUNTS` list. If keyring later exposes roles, `require_operator` in
  `app/api/deps.py` is the one place that changes.
* `ENVAPI_API_KEYS` adds an optional front-door key compared in constant time.

## Credentials

* An environment declares which services' credentials it wants. They are resolved per
  command with the caller's own token (keyring derives the account from it; there is no
  parameter naming an account), injected as temporary variables scoped to that one
  command, never written to disk, and redacted from captured output before it reaches the
  ring buffer or the log (`«redacted:<service>»`). Redaction works across read boundaries.
* `resolve_form_secrets` is deliberately not used.

## Sandbox

See `sandbox.md`. The active tier is reported on `/health/ready` and every environment
record, and `ENVAPI_MIN_SANDBOX_TIER` makes the service refuse to boot below it.

## Paths

Every file path is resolved (symlinks followed, including ones a shell planted after the
environment was created) and only then checked for containment within the workspace.
Check-then-resolve is the classic symlink escape and is never done. `../`, absolute paths,
symlinks to `/etc` and dangling symlinks pointing outside are table-driven tests.

## Signals

* Signalling a pid verifies it belongs to the environment by walking its ancestry in
  `/proc` to a live shell whose recorded start time still matches. Without that, the
  endpoint would be "kill any PID on the host, as a service".
* Startup reconciliation kills a recorded shell's group only when the pid *and* start
  time match; a recycled pid is left alone (named test).

## Resource exhaustion

`RLIMIT_NPROC`, `RLIMIT_AS`, `RLIMIT_FSIZE` and `RLIMIT_CPU` are applied in every child.
Note that `RLIMIT_NPROC` only bites at the user and namespace tiers, where each
environment has its own uid; as root at the directory tier it is not enforced by the
kernel. Disk is bounded by a periodic scan, so a single command can overshoot between
scans; real filesystem quotas would be exact at the cost of setup.

## Audit

Every create, delete, reset, archive, shell open/close/exec/signal/stdin, process signal,
file write and quota change is appended to `ROOT/audit.jsonl` with the account id.
Commands are logged truncated to 512 characters. Operators can tail it via
`GET /v1/admin/audit`.

## Known limits

* Network egress is only genuinely withheld at the namespace tier (`--net`). At lower
  tiers `ENVAPI_ALLOW_NETWORK=false` is a documented request, not a guarantee.
* At the rootless namespace tier there is no privilege drop: the mapped root inside the
  namespace is the service's own uid outside.
* Bash prints "Terminated" and similar job notices on stderr; they appear in output.
