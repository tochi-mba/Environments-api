# ADR-0002: A shell is a process in a namespace, not a container

**Status:** Accepted

## Context

An environment needs to run commands and hold a long-lived interactive shell. The obvious
implementation is a container per environment: strong isolation, a familiar mental model,
and somebody else's code doing the hard part.

It also means this service becomes a container orchestrator. It would need a container
runtime on every host that runs it, a privileged socket or a daemon to talk to, image
management, and a second lifecycle to keep in step with its own. The service would be
unable to run inside an unprivileged container itself, which is where most deployments
would want to put it.

## Decision

An environment is a directory and a set of limits. A shell is an ordinary **process**,
started inside namespaces this service creates, with its own user, its own resource limits
and its own working directory. There is no container runtime, no daemon and no image.

`app/sandbox/` owns the isolation — namespaces, users, directories, limits — and
`app/shells/` owns what a running shell is: framing, buffering, redaction, and the
lifecycle of the process.

## Consequences

- The service can run anywhere a Linux kernel with user namespaces is available, including
  inside a container, without a runtime socket.
- Isolation is this repository's responsibility rather than a runtime's. That is why the
  sandbox tests are the ones that may never be weakened, and why a change to `app/sandbox/`
  needs the test that would fail without it.
- Process ownership has to be tracked explicitly: a PID belongs to an environment, and a
  request to signal or wait on a process is checked against that ownership. Somebody else's
  PID is reported as one that does not exist.
- Output is a stream this service frames, so redaction of injected credentials happens on
  the way out, in one place.
- Startup is fast, and an environment costs a directory rather than a filesystem layer.

## What would change the answer

A deployment target that offers containers as a primitive this service may create without
privilege, or a requirement for isolation stronger than namespaces provide — a microVM per
environment, say, at which point the process model is no longer enough.
