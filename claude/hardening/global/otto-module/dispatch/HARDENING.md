# HARDENING: dispatch

**Tier** 1: spawns real Claude Code sessions with `--dangerously-skip-permissions`.
This is the sharpest edge Otto owns.
**Source** `otto/dispatch.py`

## Blast radius

Moving a card into `queued` is not a state change, it is a trigger. `dispatch_due()`
runs on the daemon tick, picks up any stored task with `status=queued` and `auto=True`,
and spawns a headless session with permissions bypassed, `system_extra` from
`findings.INSTRUCTIONS`, and a working directory of `task.cwd` or
`OTTO_TASK_CWD` (default `~`). That session inherits every credential on this
workstation: every MCP server in the registry, every connector, the cloud SSO
session.

The realized failure is on record. A task parked in `queued` from earlier testing was
swept up on the first live tick and the agent began editing a live slash-command
definition nobody had approved changing. The edit was reasonable. It was not
authorized.
That gap is the reason this document exists.

## May write

Everything not on this list is out of bounds.

| Target | What | Why it is in bounds |
|---|---|---|
| Otto store: `Task.status`, `run_id`, `attempts`, `last_error`, `cwd` | replace | Its own bookkeeping; `settle()` is the only writer of terminal state |
| Otto store: `Run` records | create | The tracked handle for the spawned process |
| `config.LOG_DIR/*` | create | Per-run captured output, via `runners.detached` |
| Board backlog, via `findings.harvest()` | create | Things a run noticed but was not asked to do |

## Must not write

| Target | Why |
|---|---|
| `Task.status = queued` from `settle()` | Would re-dispatch on the next tick and loop. `settle()` writes `done` or `needs-you`, never `queued`. |
| The task's `detail` or `title` | The dispatcher is not an editor. A spawned session may report; the dispatcher may not rewrite the instruction it dispatched. |
| Anything a spawned session writes | Out of this module's control by design. Bounding it is the spawned definition's own HARDENING.md. |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| none directly | The module holds no secret |, |
| everything, transitively | The spawned session inherits the workstation's MCP registry and AWS SSO | full operator scope |

The second row is the whole risk. `dispatch` does not need a credential because it
hands out all of them.

## Partial failure

- **Idempotent?** No. Each dispatch increments `attempts`; `TASK_MAX_ATTEMPTS` is 1,
  so a second attempt requires a human resetting the counter.
- **Retries?** None, deliberately. A failed run lands in `needs-you` with
  `last_error` attached. A retry loop is how an agent runner turns into a bill.
- **Left behind** A spawn failure (bad `cwd`, unknown agent) parks the task in
  `needs-you` and `break`s the drain loop, so one bad task cannot burn the queue.
  A killed process leaves `status=running` until `poll_runs` reconciles it.
- **Recovery** `otto autodispatch off` is the pre-flight whenever there are queued
  tasks of unknown vintage. `otto prune` clears orphaned runs.

## Guards, and what each one is for

| Guard | Value | Failure it prevents |
|---|---|---|
| `TASK_AUTODISPATCH` (`OTTO_AUTODISPATCH`) | on | Master switch; off makes `queued` inert |
| `Task.auto` | per task | Parks a task in `queued` forever without dispatching |
| `TASK_MAX_CONCURRENT` (`OTTO_TASK_CONCURRENCY`) | 2 | Simultaneous burst |
| `TASK_MAX_ATTEMPTS` (`OTTO_TASK_ATTEMPTS`) | 1 | Retry loops; also the guard that caught the parked-task case above |
| `TASK_MIN_SECONDS_BETWEEN` (`OTTO_TASK_THROTTLE`) | 20 | A bad state spawning rapidly |
| `TASK_MAX_BUDGET_USD` (`OTTO_TASK_BUDGET_USD`) | 5 | Unbounded spend per ordinary task |
| `DEEP_BUDGET_USD` (`OTTO_DEEP_BUDGET_USD`) | 15 | Unbounded spend on a `deep`-tagged task. `model_for()` pairs the premium model with a premium ceiling, so 5 is **not** the cap for every task; it is the cap for untagged ones. Both paths are capped, at different numbers. |
| stored-tasks-only | code | A derived card (a due schedule sitting in the queued column) is never auto-run |

## Out of scope

- **Deciding what may run.** Tier gating is `/orchestrate`'s job. `dispatch` enforces
  mechanical limits and nothing else; it has no opinion about risk.
- **Editing definitions.** Nothing here writes to `claude/`.
- **Retrying.** Explicitly not a feature. Do not add one.
- **Generalizing autostart.** `refresh` is the only auto-launching runner; keeping
  that list at one entry is a control, not an oversight.

## Human gate

None at dispatch time, that is the point of the queue, and it is a stated design.
The gate sits one step earlier, in `/orchestrate`'s tier promotion, and one step
sideways in `Task.auto`. Because the gate is upstream, `queued` must be treated as a
privileged column: never write `queued` from an automated definition.
