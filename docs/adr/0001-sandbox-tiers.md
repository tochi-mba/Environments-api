# ADR-0001: Isolation is tiered, detected at startup, and reported

**Status:** Accepted

## Context

This service runs commands that somebody else's assistant chose. The isolation it can
actually provide depends on what the host allows: unprivileged user namespaces, the
ability to create users, a writable `/proc`, the kernel's own configuration. Those differ
between a developer's laptop, a container without extra capabilities, and a VM the
operator controls.

Two failure modes were available and both are bad. Refusing to start unless the strongest
isolation is available makes the service undevelopable and pushes people towards running
it with the sandbox switched off. Quietly running with whatever isolation happens to be
available makes a weakly isolated deployment indistinguishable from a strong one, which
is the more dangerous of the two: the operator believes something that is not true.

## Decision

Isolation is expressed as **tiers**. At startup the service probes the host
(`app/sandbox/detect.py`), settles on the strongest tier the host supports, and reports it
— in the startup log and in the readiness probe.

An operator sets `ENVAPI_MIN_SANDBOX_TIER` to the weakest tier their deployment is willing
to accept. A host that cannot reach it fails startup rather than serving weakly isolated
work.

## Consequences

- A deployment's real isolation is a fact anybody can read from `/ready`, rather than an
  assumption. This is why the readiness body names the tier.
- The tier is decided once, at startup, not per request: a probe per exec would be a
  syscall storm, and a tier that changed under running work would be worse than either
  answer.
- The test suite needs Linux, and the suite is honest about which tier it exercised. Tests
  that assert isolation run against the real mechanism; they are skipped nowhere and
  faked nowhere, because a faked namespace proves nothing at all.
- Development on Windows or macOS means running the tests in WSL2, the devcontainer, or a
  privileged container. That cost is accepted deliberately: the alternative is a sandbox
  whose tests pass on a machine where the sandbox does not exist.

## What would change the answer

A host mechanism that is both universally available and strong enough on its own — at
which point the tiers collapse into one and the probe becomes a version check.
