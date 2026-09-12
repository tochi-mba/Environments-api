# Testing

This service manages real processes and real files, so the suite runs real short-lived
processes in temporary directories. Mocking `subprocess` would prove nothing: the entire
risk lives in whether the real thing behaves. It is still fast (well under ten seconds)
because it gates on events and never sleeps to wait.

```
make check   # ruff format --check, ruff check, mypy --strict, pytest with the 100% gate
```

## What is proven where

| Area | File | Proven |
|---|---|---|
| Framing | `test_shell_units.py`, `test_shell.py` | Exit codes for success, failure and signals; the frame byte in output; a spoofed frame with the wrong nonce; partial frames across reads |
| Lifecycle | `test_shell.py` | Open → exec → poll → wait → close, state after each; `cd` and variables persisting |
| Kill | `test_shell.py` | SIGTERM then SIGKILL; the whole group dies, grandchildren included; `trap '' TERM` escalates |
| Timeouts | `test_shell.py` | Children killed; a builtin loop escalates to the shell |
| Orphan reconciliation | `test_service.py` | A simulated restart kills the recorded group; a recycled pid with a different start time is left alone |
| Buffer | `test_shell_units.py`, `test_shell.py` | Cursor semantics, rollover, `dropped_bytes`, log cap |
| Path containment | `test_paths_files.py` | `../`, absolute, symlink to `/etc`, dangling symlink out, a symlink planted after the fact |
| PID ownership | `test_service.py`, `test_api.py` | Signalling a process in another environment, and one belonging to none, are both refused |
| Quotas | `test_service.py`, `test_api.py` | Every limit returns 409 naming itself; disk quota refuses new work |
| Sandbox | `test_sandbox.py`, `test_child_setup.py` | Each tier for real (skipped with a reason when the host cannot), the detector against the host, rlimits and tty setup in forked children |
| Redaction | `test_shell.py`, `test_api.py` | `echo $TOKEN` never reaches the buffer or the log; split across chunks |
| Keyring | `test_keyring.py` | Valid, expired, wrong audience, forged signature, `alg: none`, unknown kid, rotation, against real RS256 keys |
| Isolation | `test_service.py`, `test_api.py` | Account A cannot see, poll, signal or delete account B's environment, through every route |
| Concurrency | `test_shell.py`, `test_api.py` | Second exec on a busy shell → 409; parallel shells do not interfere |
| Restart | `test_service.py`, `test_api.py` | Shells return dead with a reason, environments intact, commands retrievable from logs |

## Conventions

* Dependencies come through `app/api/deps.py`; tests override with
  `app.dependency_overrides`, never by monkeypatching internals. `create_app()` takes the
  settings, an `httpx.AsyncClient` (wired to `tests/fake_keyring.py`) and host capabilities.
* `tests/fake_keyring.py` mints real tokens with a generated RSA key and serves the JWKS
  and credentials endpoints through `httpx.MockTransport`.
* Gate on events (`wait_command`, `wait_output`, `wait_exit`) and bounded polls of
  `/proc`; never `sleep` to wait for a process.

## The coverage gate and privileged tiers

`fail_under = 100`. Code that runs in the forked child before `exec` (rlimits, tty
acquisition, the privilege drop) is measured by running it inside `multiprocessing` fork
children with coverage's `multiprocessing` concurrency; those children write their data to
`.coverage-data/`, which `conftest.py` makes world-writable so a child that dropped to
`nobody` can still save.

The user tier needs root and `useradd`; the namespace tier needs a usable `unshare`. On a
host without them the tier tests skip with a reason and the gate fails on their lines,
which is intended: the gate is met on a capable host, and CI runs in a privileged
container for exactly that reason (`.github/workflows/ci.yml`).
