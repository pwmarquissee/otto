# HARDENING: /orchestrate

**Tier** 1: promotes board tasks into `queued`, which *is* the dispatch trigger. Its
whole job is deciding what runs unattended.
**Source** `claude/commands/orchestrate.md`

## Blast radius

On the Otto board `queued` means "Otto spawns a real Claude Code session at this, with
`--dangerously-skip-permissions`". Promotion is the entire act; everything downstream
is automatic. So this definition's blast radius is not what it writes, it is what it
authorises other sessions to write.

The cold-start incident is the concrete shape of getting this wrong: a task in `queued`
was dispatched and the agent edited a live ops file nobody had approved changing. The
promotion decision is where authorization is granted, and it is the only place.

Guessing low is the expensive mistake. An unset tier is treated as tier-1 and asked
about, never assumed autonomous.

## May write

| Target | What | Why it is in bounds |
|---|---|---|
| `Task.status` → `queued`, tier-0 tasks only | replace | The promotion decision itself |
| `Task.auto` → `true`, at promotion | replace | Migrated tasks are `auto=False` on purpose; promotion sets it |
| `Task.detail` on tier-2 tasks | append | Findings and a proposed plan, so they are not lost when the session ends |
| `Task.status` → `done` on a declined tier-1 task | replace | Closing what the owner said no to |
| Slack, the configured ops channel (`OTTO_SLACK_CHANNEL_ID`) | one summary message | The owner's own one-way ops feed |

## Must not write

| Target | Why |
|---|---|
| `Task.status = queued` for a tier-1 or tier-2 task | Tier-1 means changes must not be applied without the owner; tier-2 means the owner executes. Promoting either bypasses the only gate in the system. |
| More than three promotions per run | Autorun concurrency is 1 and task dispatch is 2. Fifteen promoted tier-0 tasks drain slowly and you lose the ability to react to the first failure before the fifteenth starts. |
| A task whose `detail` was not read | Cards carry real context including the evidence that produced them. Promoting unread is promoting blind. |
| Any other task store (a wiki database, a spreadsheet) | The board is the only task system. A mirror kept elsewhere goes stale and reading status from it produces wrong answers. |
| Any target system directly (identity provider, cloud, EDR, RMM) | `/orchestrate` decides and delegates. It does not execute the work. |
| Any file under `claude/` | Definitions are not board work. |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| Otto HTTP API (`127.0.0.1:8787`) | local daemon, no auth | full board read/write |
| Slack | claude.ai Slack connector | post to the configured ops channel |

## Partial failure

- **Idempotent?** Partly. A re-read of the board is safe; a second promotion of an
  already-`done` task is not, so always re-read before promoting.
- **Retries?** A failed task lands in `needs-you` with `last_error` and does **not**
  retry. That is deliberate. Read the error before promoting it again.
- **Left behind** Promotions already made stay made if the run dies mid-batch. Small
  batches keep that recoverable.
- **Verify before promoting** `python -m otto autodispatch`, if dispatch is off, work
  piles up in `queued` and never moves.
- **Recovery** `python -m otto task mv <id> backlog` demotes a wrongly-promoted task,
  but only before it dispatches. After that it is a running session; use
  `otto kill <run>`.

## Out of scope

- **Doing the work.** Otto dispatches by itself now. Do not spawn agents or write
  status transitions by hand.
- **Setting a tier.** If a task has no tier, infer, *say which tier you assumed*, and
  treat genuine ambiguity as tier-1 and ask.
- **Manufacturing work.** If nothing can be promoted and nothing needs the owner, say
  "board clear, nothing to promote" and stop.
- **Special-casing any agent type.** Set `agent` on the task; Otto passes the
  definition through. There is no separate spawn path.

## Human gate

Tier-1 tasks: present the proposed action and what it will change, then wait. Tier-2:
gather context and propose only; the owner executes. Neither is ever promoted. That gate is
the reason this definition exists, and it is not delegable to the dispatcher, which has
no opinion about risk.
