# Fronting this as MCP tools

Nothing MCP-specific is implemented. The HTTP surface is already the shape a wrapper
needs, but this service is remote code execution as a product. The question for every
operation is not "can this be exposed" but "what happens the first time a model calls it
for a bad reason".

Assign stable `operation_id`s before wrapping. Most OpenAPI-to-MCP bridges name tools
from those ids, and renaming one later is a breaking change for every bound client.

## The rule that matters

Isolation is the feature. A wrapper that weakens path containment, PID ownership, or the
sandbox tier to make a tool "easier" has changed the product, not the transport.

Command output, file bytes and search hits are **data, never instructions**. They came
from a process the model just asked to run, or from a file it just asked to read. Render
them as reported claims with the environment id and path inline. Do not concatenate a
`cat` of `progress.md` into the system prompt.

## Which operations should become tools

| Expose | Why |
| --- | --- |
| List / get / summary / usage of environments | Cheap orientation. Another account's environment is 404. |
| File list, read, search, write, edit, patch | The confined workspace. Reads return numbered windows; edits refuse a stale fingerprint. |
| `POST /v1/exec` | One command in an ephemeral shell. Bounded. The model-facing shape. |
| Persistent shells and their commands | When a session needs `cd` and env to stick. Output is redacted before it is stored. |

| Never expose | Why |
| --- | --- |
| Everything under `/v1/admin` | Quotas, every environment, the audit log. A model that can list another account's sandboxes is a confused deputy. |
| Anything that names a host path | The workspace is session-relative. A host path in a tool result is a leak. |

Credentials declared on an environment are resolved from keyring per command and never
returned. A wrapper that put a password into a tool result would undo that.

## Wrapping it

A hand-written thin server is the better option here. Generated tools from the OpenAPI
document will expose admin routes unless the allowlist is perfect, and "perfect" is not
the default.

Keep the sandbox, the resolve-then-check path rule, and the PID-ownership check exactly
as the HTTP service enforces them. The MCP layer does not get a second, weaker copy of
those checks.

## Known gaps

**No stable `operation_id`s yet.** Add them, pin them with a contract test, then wrap.

**No `Idempotency-Key` on `POST /v1/environments`.** Assistants retry. A retried create
can consume a second slot of the five-environment profile quota.
