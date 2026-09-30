# CLAUDE.md

Read [AGENTS.md](AGENTS.md). It is the operating manual for this repository — the
conventions, the testing rules, the sharp edges and how to run it locally — and it is
normative. The test-first loop is in [CONTRIBUTING.md](CONTRIBUTING.md), and the layout in
the [README](README.md). This file exists so that a tool looking for `CLAUDE.md` finds its way there.

Two things worth knowing before the first edit:

- **The full suite needs Linux.** The sandbox tiers use `unshare`, `setpriv`, `useradd` and
  `/proc`. Use WSL2, the family devcontainer, or a privileged container. `make lint` and
  `make type` work anywhere.
- **This service runs arbitrary commands for remote callers.** Isolation is the product.
  A change that weakens it needs the test that would fail without it.
