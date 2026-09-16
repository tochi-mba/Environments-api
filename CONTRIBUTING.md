# Contributing

Start with [AGENTS.md](AGENTS.md) — it is the operating manual for this repository and it
is normative. This file is the short version.

## This service needs Linux

The sandbox tiers use `unshare`, `setpriv`, `useradd` and `/proc`. The full suite
therefore runs on Linux only — in CI, in WSL2, in the family devcontainer, or in a
privileged container:

```bash
docker run --rm --privileged -v "$PWD":/app -w /app python:3.11 bash -lc 'make install && make check'
```

On Windows or macOS you can still run the parts that do not touch the sandbox:

```bash
make lint type                                   # both work everywhere
uv run pytest --noconftest tests/test_keyring.py tests/test_settings.py -q
```

`make check` is the gate, and it is only honest on Linux. Say in the pull request which
platform you ran it on.

## Setup

```bash
make install             # venv and every dependency, from the lockfile
uv run pre-commit install
make check
```

## The loop

1. **Write the failing test first.** Run it and confirm it fails for the reason you expect.
2. Write the smallest code that makes it pass.
3. Refactor with it green.
4. `make check` — lint, strict types, the layering contracts, and the tests at 100%
   branch coverage.

## The sandbox is the product

Everything else here exists to make the sandbox safe to hand to somebody else's assistant.
Two rules govern review:

- **A change that weakens isolation needs a test that would fail without it.** New syscalls,
  new mounts, a relaxed tier, a path that escapes the environment root: each one needs the
  case that proves it is still refused.
- **Secrets are injected, never logged.** Credentials come from keyring per request and are
  scrubbed from captured output. `app/shells/redact.py` owns that, and a change to it needs
  tests for the value, the value inside a longer string, and the bare token inside a
  `Bearer` header.

## Commits

Conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`). The
subject says what changed; the body says **why**, and flags anything surprising.

Never commit a real token, an API key, or a `.env`.
