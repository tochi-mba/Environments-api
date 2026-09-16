# Architecture decision records

One file per decision that would otherwise be re-argued every few months: what was
decided, what it cost, and what would make it worth revisiting. They are dated records
rather than documentation — when a decision is reversed, the record stays and the one that
replaced it says so.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-sandbox-tiers.md) | Isolation is tiered, detected at startup, and the tier is reported | Accepted |
| [0002](0002-shells-are-processes-not-containers.md) | A shell is a process in a namespace, not a container | Accepted |
| [0003](0003-credentials-are-injected-per-exec.md) | Credentials are injected per command and scrubbed from output | Accepted |

Family-wide decisions — the shared client libraries, `Authorization: Bearer` as the
canonical header, the port assignments — live in the meta repository's `docs/adr/` and are
linked from here rather than restated.

## Writing one

Copy the shape of an existing record: context, decision, consequences, and what would
change the answer. Number it in sequence. If a decision only affects one module, a
docstring is the better home; an ADR is for the ones somebody will otherwise undo by
accident.
