# Working on environments-api

Read this before changing anything. `docs/` has the long form.

## What this is

An HTTP service that creates sandboxed environments and runs shells in them for remote
callers identified by keyring tokens. It is remote code execution as a product. The
sandbox tiers, path containment and PID-ownership checks are the feature; treat any change
to `app/sandbox/`, `app/paths.py`, `app/processes.py` or `EnvironmentService._owned` as a
security change.

## Conventions

* Python 3.12, `uv`, FastAPI, httpx, structlog, pytest + pytest-asyncio, ruff,
  `mypy --strict`, and import-linter. `keyring-client` does everything that believes a
  keyring token or fetches a credential, and `settings-client` reads per-person settings;
  both come from their owning repositories as tagged git sources (`[tool.uv.sources]` in
  `pyproject.toml`), never as a vendored copy. Line length 100; `ruff format` decides
  formatting.
* Google-style docstrings on every public module, class and function.
* Comments explain *why*, never *what*. A comment that restates the code is deleted; one
  that records a constraint, a trade-off or a non-obvious failure earns its place.
* All errors are `DomainError` subclasses in `app/errors.py`, rendered as
  `application/problem+json` with a stable `code`. Routes never deal in status codes.
* Dependencies come through `app/api/deps.py` and `create_app`'s arguments (settings,
  keyring transport, clock, host capabilities, settings-api client); tests substitute them
  there. Patching a module's internals is kept for the error branches and races no real
  input can reach, most of them in `tests/test_edges.py` and
  `tests/test_file_service_edges.py`.
* Coverage gate at 100% (`fail_under = 100`). Permitted exclusions: `if TYPE_CHECKING:`,
  protocol bodies (`...`), `if __name__ == "__main__":`. Nothing else.
* `make check` = format check, lint, types, import contracts, tests with the gate. CI runs
  the same command.

## Testing rules

* Real processes (`sh -c`, `bash`) in temporary directories. Never mock `subprocess`.
* Gate on events, never sleep: `wait_command`, `wait_output`, `wait_exit`, `wait_idle`,
  or a bounded poll of `/proc` when a child must have appeared.
* Each sandbox tier is exercised for real and skipped with a reason when the host cannot
  provide it. The gate is met on a host with root and a usable `unshare`.
* Code that runs in the forked child before exec is measured through `multiprocessing`
  fork children (`tests/test_child_setup.py`); keep such code in module-level functions.

## Sharp edges

* **Framing.** Commands are `eval`'d from a single-quoted literal followed by a
  `printf` of a nonce-tagged frame. Do not change `exec_script` without re-reading
  `docs/architecture.md`; every exit code in the API depends on it.
* **Signals.** "Signal the current command" means every descendant of the shell's root
  process *except the shell lineage* (`Shell.lineage()`). Under the namespace tier the
  root is `unshare` and the shell is its child.
* **Reconciliation.** Never kill a recorded pid without checking its start time.
* **Containment.** Resolve, then check. Never the other order.
* **Redaction.** Anything that reaches the ring buffer or a log must have gone through
  `Shell._emit`.
* **`environment.json`** is written atomically; keep it that way.

## Running locally

`cp .env.example .env`, then `uv run python scripts/dev_keyring.py` (a keyring stand-in on
:8001 that accepts the service token in `.env.example`), then `make run`, then
`DEV_KEYRING_URL=http://127.0.0.1:8001 make smoke`.
