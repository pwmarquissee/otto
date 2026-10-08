# How Otto works

The longer version of the README, for anyone who wants to know what is actually
going on before they arm a schedule.

## Architecture

```
  browser (dashboard) ──▶ ┌────────────────────────────────────┐
  otto CLI ─────────────▶ │  daemon  127.0.0.1:8787            │
  Claude Code hooks ────▶ │  FastAPI + a tick thread           │
  agents (HTTP) ────────▶ │  single writer, atomic JSON writes │
                          └───────────────┬────────────────────┘
                                          │
                              ~/.claude/otto/state/*.json
                              ~/.claude/otto/logs/*.log

  api/       one router per area (state, runs, sessions, schedules, tasks,
             slack, setup, decisions, telemetry, chat, system, static);
             daemon.py holds the process, the tick loop and the app
  cli/       one module per verb group; cli/__init__.py assembles the parser
  web/js/    the dashboard as ordered classic scripts (00-shell.js states
             the load order); no build step
  runners/   detached    spawn and track headless Claude Code runs
             scheduled   cadence and staleness evaluation
             external    read-only liveness probes for integrations
             machine     host facts, informational only

  herdr (optional)       terminal multiplexer that owns every pane's PTY;
                         Otto reads its snapshot and dispatches into its panes
```

State is plain JSON under `~/.claude/otto` (override with `OTTO_HOME`), never in the
repo. Only the daemon writes. The CLI, the dashboard, the hooks, and agents all go
through the HTTP API. Writes are atomic (temp file, then `os.replace`), so a reader
sees the whole old file or the whole new one.

## Configuration

Every `OTTO_*` variable is read by `otto/config.py` when the daemon starts: the
environment first, then `<OTTO_HOME>/otto.env` (what the setup flow writes), then the
default. `.env.example` documents all of them. The ones most people touch:

| Variable | What it does |
| --- | --- |
| `OTTO_HOME` | state and log directory, default `~/.claude/otto` |
| `OTTO_HOST`, `OTTO_PORT` | where the daemon listens, default `127.0.0.1:8787` |
| `OTTO_TICK` | seconds between daemon ticks, default 15 |
| `OTTO_DEFAULT_MODEL`, `OTTO_DEEP_MODEL` | which Claude Code model a dispatched card runs on; the `deep` tag selects the second |
| `OTTO_TASK_BUDGET_USD` | per-card spend cap passed to Claude Code, default 5 |
| `OTTO_TASK_CONCURRENCY` | dispatched cards at once, default 2 |
| `OTTO_TASK_ATTEMPTS` | dispatch attempts per card, default 1 |
| `OTTO_TRIAGE_MAX_IN_FLIGHT` | cap on cards queued or running, default 3 |
| `OTTO_AUTODISPATCH` | `0` makes the Queued column inert |
| `OTTO_INTEGRATIONS` | which outside services to probe; `none` for no probes |
| `OTTO_UNATTENDED` | set by Otto on headless runs; turns the guard hook on |
| `OTTO_GUARD_ASK`, `OTTO_GUARD_ASK_TIMEOUT` | whether a gated action raises a dialog, and how long it waits |
| `OTTO_HERDR_AUTOSTART`, `OTTO_HERDR_RUNS` | start the herdr server with the daemon; put Otto's own runs in herdr panes |
| `OTTO_NO_TOAST`, `OTTO_NO_SEND` | suppress desktop toasts and outbound messages (tests set both) |

## The dashboard

A left rail of views, a persistent inspector on the right, and a work/personal
filter that applies everywhere.

| View | What it is |
| --- | --- |
| Setup | first-run checklist, hidden once complete |
| Today | what needs you now, your day, what is in flight, blind spots |
| Board | outstanding work in columns: Backlog, Queued, In progress, Needs you, Blocked, Done |
| Dispatch | live sessions, suggested card-to-session pairings, an Open lane to drag from, a terminal for the selected pane |
| Terminal | one herdr pane at full fidelity under a strip of tabs |
| Grid | every pane tiled live, observe mode |
| History | every Claude Code session on the machine priced and ranked, and every tracked run with its verdict |
| Writing | drafts in your voice mined from the week, with a privacy scan per line |
| Control plane | schedules, registry, people, integrations, config, events |

Two kinds of card live on the board. Stored cards are the ones you or an agent
created; they drag between columns. Derived cards are computed from live state on
every request (a stale schedule, an orphaned run, a down integration) and are not
draggable. Fix the condition and the card disappears.

The dashboard is push-driven: one WebSocket announces when state changed, the client
refetches with an ETag, and an unchanged state is a bodyless 304. The payload is
computed once per store version and gzipped once. Numbers and the contract are in
[control-room/performance.md](control-room/performance.md).

## Runs, sessions, and hooks

A run is a Claude Code session Otto spawned: a headless `claude -p` with a captured
NDJSON log, a tracked pid, and usage read back from Claude Code's own output. A
session is anything Claude Code reports through the hooks, whether or not Otto
started it. The two are joined when a run's session id shows up.

Runs come from three places:

- Schedules. The daemon is the scheduler. Each tick it evaluates every cadence and
  launches what is armed. Staleness alarms are independent of cadence, so a schedule
  that stops producing is reported whatever the reason.
- The board. A stored card in Queued is dispatched as a Claude Code session in its
  working directory with permission prompts skipped. A failed card lands in Needs you
  and stops.
- Dispatch into a live session. With herdr running, the control room pairs
  promotable cards with idle Claude panes, scored with the reason in words (same
  repository is the strong signal). Approve submits the card's prompt into that pane
  and marks the card running. When the pane goes idle again, its recent screen is
  read into the card's result and the card lands in Needs you.

With herdr up, Otto's own runs launch as tabs in an `otto-runs` workspace, so they
sit in the same rail as your sessions with scrollback. If herdr is down they fall
back to a detached launch and the run says so.

The six hooks map turn boundaries onto a state: `UserPromptSubmit` to busy,
`Notification` (permission prompts only) to waiting, `Stop` and `StopFailure` to
idle, `SessionStart` and `SessionEnd` to up and down. The hook script is stdlib only,
writes nothing to stdout, always exits 0, and trips a short breaker when the daemon
is down, so it cannot break a turn. Otto owns no PTY and never infers a state it did
not observe: a session silent for twelve hours is `offline_inferred`, not dead.

`otto sessions install` merges the hooks into `~/.claude/settings.json` under a
marker, alongside whatever is already there; `sessions uninstall` removes exactly
those. `~/.claude/settings.local.json` does not load hooks, which is why the
user-level file is the target. `herdr integration install claude` adds herdr's own
SessionStart hook, which reports the Claude session id into the pane record. That id
is what Otto's hooks key sessions on, so a pane and a session are the same thing.

## Unattended runs

- The promotion gate. `otto triage promote` is the sanctioned way into Queued. It
  refuses a card that is unassessed, owned by a human, `tier-2-assistive`, not
  `ready`, carrying no detail, or over the in-flight cap, and every refusal is a
  sentence.
- Tiers gate autonomy, not difficulty. `tier-0-autonomous` may be promoted directly.
  `tier-1-approval` goes through two steps: `promote --prepare` runs a session that
  investigates and writes a proposal into the card's plan, applying nothing;
  `task plan --approve` stamps it; then `promote` runs with the approved plan as the
  prompt, told to stop at the first step where reality disagrees. `tier-2-assistive`
  is never promoted.
- Brakes on autorun. A master switch, one unattended schedule at a time, a breaker
  that disarms a schedule after three consecutive failures (it stays enabled so it
  still reports as stale), a minimum gap between launches, and a per-run budget.
  Upstream API errors (429, 529) do not count toward the breaker.
- Brakes on dispatch. `otto autodispatch off` makes Queued inert. Derived cards are
  never auto-run. One attempt per card. A throttle between dispatches. Per-card
  budget through Claude Code's `--max-budget-usd`.
- The guard hook. `scripts/otto_guard.py` is a Claude Code `PreToolUse` hook that is
  a no-op unless `OTTO_UNATTENDED=1`. In an unattended session it gates destructive
  shapes (credential checkouts, raw mutating API calls, identity-provider mutations,
  edits to the files that decide whether checks pass) behind a desktop yes/no dialog.
  The session has to state target, rollback, and authority in comment lines above a
  gated shell command, or the call is denied without a dialog. Patterns match
  invocations, not words. Fail-closed on the gated set, untouched everywhere else.
- The daemon outlives the window. Closing the desktop window hides it; Quit from the
  tray leaves the daemon running. `otto stop` is a separate act.

Every session runs at a permission level recorded on the run: `plan`
(`--permission-mode plan`, read-only, writes a proposal; what a PREPARE run gets),
`yolo` (`--dangerously-skip-permissions`, the full operator; implementation and the
loops that act on the board), or `scoped` (a caller's own allow list). Nothing
elevates on its own: `otto task yolo <id>` approves the card's plan and runs it at
yolo through the gate; `otto task run --plan|--yolo` and the dashboard's Run (plan) /
Run (yolo) pick a level for one run; `otto schedule set <name> --permissions` sets a
schedule's.

## Scopes

`OTTO_SCOPE=core` (the default) is the orchestrator this document describes: board,
sessions, dispatch, schedules, hooks, ledger, runs, feeds, decisions, journal,
nudges, priorities, retire, chat, refresh, today. `OTTO_SCOPE=assistant` adds the
owner's assistant modules (`outreach`, `writing`, `people`, `prep`, `wellbeing`,
`summon`, `inbox`, `meetings`): their routes, their tick hooks, their schedules and
their subcommands. Under core none of them is imported, mounted or seeded; the
boundary is `otto/assistant/` and `tests/test_scope.py` holds it from a subprocess.
It is a switch rather than a pip extra because the assistant needs no package the
core lacks; what differs is what the daemon loads.

## CLI

`otto <verb>` (`python -m otto` is the same). Grouped:

- Look: `status`, `next`, `gaps`, `board`, `live`, `runs`, `logs`, `watch`, `events`,
  `agenda`, `notices`, `day`, `spend`, `ledger`, `priorities`, `machine`, `say`
- Board and triage: `task add|ls|mv|set|plan|open|run|reply|dedupe|rm`,
  `triage list|set|promote|checked|fade|status`, `propose`, `ack`, `known add|rm`,
  `decide`, `decisions`, `decision`, `retire`
- Runs and schedules: `spawn`, `kill`, `done`, `launch`, `schedules`,
  `schedule add|arm|rm`, `due`, `stamp`, `toggle`, `autorun`, `autodispatch`, `prune`
- Sessions, dispatch, herdr: `sessions [install|uninstall|doctor|name|open|watch]`,
  `dispatch [approve|dismiss|to]`, `herdr [up|attach|open|adopt|focus|worktree ...]`,
  `app`
- Daemon and config: `serve`, `ensure`, `stop`, `restart`, `setup`, `doctor`,
  `probe`, `identity`, `manifest`, `registry`, `scan`, `feeds`,
  `config status|deploy|adopt|snapshot|backup`, `telemetry status|install|uninstall`
- Core, needing an integration configured: `chat`, `refresh`, `notify`, `reply`,
  `post`, `tell`, `skills audit`
- Assistant scope only (`OTTO_SCOPE=assistant`): `meetings ingest`, `prep`,
  `people [note]`, `threads`, `thread-note`, `thread-update`,
  `outreach [compose|resolve]`, `dm`, `checkin`, `patterns`,
  `writing ideas|draft|edit|show|set|voice`

## More

- [control-room/setup.md](control-room/setup.md): the first-run flow and its API
- [control-room/dispatch-parity.md](control-room/dispatch-parity.md): the dispatch model
- [control-room/terminal-views.md](control-room/terminal-views.md) and
  [control-room/termrelay.md](control-room/termrelay.md): embedded terminals
- [control-room/performance.md](control-room/performance.md): payload and push design
- [../SECURITY.md](../SECURITY.md): threat model and what is enforced
