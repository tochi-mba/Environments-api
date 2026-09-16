# ADR-0003: Credentials are injected per command and scrubbed from output

**Status:** Accepted

## Context

Commands run here often need a credential: a token for an API, a key for a service the
person has connected in keyring. Three places could hold it. The environment could be
created with the credential baked into it, the service could hold credentials in its own
configuration, or each command could be given what it needs at the moment it runs.

Baking it into the environment means a credential sits in a long-lived directory and in
every process started there, long after the work that needed it. Holding credentials in
this service's configuration means every caller shares one identity, which defeats the
point of the family's per-person model.

## Decision

Credentials are resolved from keyring **per command**, for the person whose verified token
the request carried, and injected as environment variables for that process only. Nothing
is stored in the environment, on disk, or in this service's configuration.

What keyring returns — headers and query parameters — is mapped to environment variables
by one function, and the values are recorded as secrets for that command's output.
`app/shells/redact.py` scrubs them from everything the process prints before it reaches
the caller or a log record.

## Consequences

- A credential's lifetime is one command. An environment that outlives the work holds
  nothing worth stealing.
- Rotation works without touching environments: the next command resolves the new value.
- The person's own token is what authorises the resolution, so this service cannot ask for
  a credential it was not handed a token for.
- Redaction is a correctness requirement with tests of its own: the value, the value
  inside a longer line, and the bare token inside a `Bearer` header all have to be scrubbed,
  because a program that echoes its environment prints all three shapes.
- Short values are deliberately not redacted: replacing a two-character string everywhere
  it appears would corrupt ordinary output and teach people to turn redaction off.

## What would change the answer

A credential kind that cannot be expressed as an environment variable — a file-backed
certificate, say — which would need a per-command mount with the same lifetime rules
rather than a different storage decision.
