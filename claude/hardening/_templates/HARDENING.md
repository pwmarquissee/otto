# HARDENING: <definition name>

> Template. Copy to the definition's doc directory and replace every angle-bracket
> placeholder. Delete this blockquote. `otto skills audit` tells you where the doc
> belongs: skills co-locate next to `SKILL.md`, commands and agents live under
> `claude/hardening/<scope>/<kind>/<name>/`.
>
> Write this for the agent that will read it mid-run with none of today's context.
> The **Out of scope** section is the load-bearing one. Everything else describes
> what the definition does; that section is the only part that describes what it is
> not allowed to do, and it is the part the cold-start incident had no way to read.

**Tier** <1 | 2>: <one line: why it lands in that tier>
**Source** `<repo-relative path to the definition>`

## Blast radius

<What is the worst thing a run of this can do if it is wrong, not malicious? Name the
systems and the people affected. One paragraph, concrete. "Could break things" is not
an answer; "posts as the owner into a company-wide channel" is.>

## May write

Everything not on this list is out of bounds.

| Target | What | Why it is in bounds |
|---|---|---|
| `<path or system>` | <create/append/replace> | <the reason this is its own output> |

## Must not write

| Target | Why |
|---|---|
| `<path or system>` | <the specific damage> |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| `<name>` | <loader, Secrets Manager ARN, MCP server, credential-store item> | <read / write, which tenant> |

Never inline a secret in a definition, a doc, or a task detail. If a credential is
missing or expired, that is a stop condition, not something to work around.

## Partial failure

<What state can a half-finished run leave behind, and what does a human do about it?
Answer all four:>

- **Idempotent?** <Is a re-run safe, or does it double-apply?>
- **Retries?** <Does anything retry automatically? If not, say so, that is usually
  the right answer and it should be deliberate.>
- **Left behind** <Partial writes, an open thread, a task in the wrong column.>
- **Recovery** <The exact command or manual step that puts it right.>

## Out of scope

Explicit non-goals. A run that finds itself doing one of these should stop and say so
rather than proceed, even when the change would be correct.

- <thing it must not take on, and why it is somebody else's call>

## Human gate

<Which actions require the owner before they happen, and how the definition asks. If there
is no gate at all, say that plainly, an ungated definition is a legitimate design,
but it should be a stated one.>
