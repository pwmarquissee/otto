# HARDENING: launch

**Tier** 1: runs a schedule on demand, including slash-command schedules that chain
other tier-1 definitions under skip-permissions.
**Source** `otto/launch.py`

## Blast radius

A schedule's `command` is one of two very different things and `launch` tells them
apart:

- **slash command** (`/orchestrate`), spawned as a headless Claude Code session with
  `skip_permissions=True`, cwd `~`, `findings.INSTRUCTIONS` appended. Full operator
  credentials. A daily chain command inherits every definition it chains.
- **shell command**, run directly with output captured to `LOG_DIR`.

Getting the discrimination wrong is the difference between a working button and one
that shells out to a file called `/orchestrate`.

Two entry paths with different authorization, and the distinction is load-bearing:

| Path | Authorization | Budget |
|---|---|---|
| `launch(autorun=False)` | A human clicked Run in the dashboard | uncapped, cutting a chained run off mid-chain leaves partial state, which is worse than the spend |
| `launch(autorun=True)` | `autorun_blocked()` returned None | `SCHEDULE_BUDGET_USD` ceiling, because nobody is watching |

## May write

| Target | What | Why it is in bounds |
|---|---|---|
| Otto store: `Run` records | create | The tracked handle for the launched process |
| Otto store: `Schedule.last_autorun`, `consecutive_failures`, `disabled_reason` | replace | Circuit-breaker state, via `_stamp_autorun()` |
| `config.LOG_DIR/*` | create | Captured output and the generated launcher script |

## Must not write

| Target | Why |
|---|---|
| `Schedule.command` | `launch` executes schedules, it does not author them. `otto schedule add` is the writer. |
| `Schedule.autostart` | Arming a schedule for unattended running is an explicit human act (`otto schedule arm`). A runner that could arm itself has no ceiling. |
| Anything the launched session writes | Bounded by that definition's own HARDENING.md. |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| none directly | The module holds no secret |, |
| everything, transitively | A slash-command launch inherits the workstation's MCP registry and AWS SSO | full operator scope |

## Partial failure

- **Idempotent?** No. `active_for()` refuses a second launch while one is running, so
  a double click cannot double-launch, but a completed run relaunched is a real second
  run.
- **Retries?** None. Failures accumulate into `consecutive_failures`; at
  `SCHEDULE_MAX_FAILURES` the circuit breaker trips and `autorun_blocked()` refuses.
- **Left behind** A chained command killed mid-chain leaves earlier steps applied and later
  steps not, plus missing `otto stamp` entries, which makes `otto next` re-offer work
  that partly happened. Read the run log before relaunching.
- **Recovery** `otto logs <run>` for what actually completed, then `otto stamp <step>`
  for steps that did finish, then relaunch.

## autorun refusals

Every check in `autorun_blocked()` exists because the alternative is an unattended
agent doing something expensive at 3am with nobody watching:

`SCHEDULE_AUTORUN` master switch · `enabled` · `autostart` and `runner == "launch"` ·
`disabled_reason` circuit breaker · `consecutive_failures` · concurrency cap ·
`SCHEDULE_MIN_GAP_MINUTES` floor between two autoruns of the same schedule,
independent of cadence, so a cadence bug cannot produce a tight relaunch loop.

## Out of scope

- **Being a general autostart mechanism.** Only schedules explicitly armed with
  `autostart` and `runner=launch` can autorun. Adding a schedule must never be able to
  accidentally arm something dangerous.
- **Deciding cadence.** `otto due` owns that.
- **Editing definitions.** Nothing here writes to `claude/`.

## Human gate

Clicking Run in the dashboard *is* the authorization for an uncapped
skip-permissions session; there is no second confirmation and there should not be.
The unattended path is gated instead by `autorun_blocked()`, which is why every one of
those checks must fail closed.
