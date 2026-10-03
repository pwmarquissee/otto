# HARDENING: sessions

**Tier** 1: writes into `~/.claude/settings.json`, the file that governs every
Claude Code session on this workstation, and installs a command that then executes
inside every turn.
**Source** `otto/sessions.py`, `scripts/otto_hook.py`

## Blast radius

Two different edges, and they fail in opposite directions.

**The install edge.** `install()` merges hook entries into `~/.claude/settings.json`.
That file is shared and busy: Claude Code rewrites it itself (plugin toggles,
permission grants), and it may already hold unrelated `Stop`, `SubagentStop` and
`PostToolUse` hooks belonging to other tools. A merge that replaced rather
than merged would silently delete those, and nothing would report it, because the
failure looks exactly like "that feature was never configured". The merge is
therefore additive, marker-scoped (`otto:session-hook`), and refuses outright on a
file it cannot parse rather than starting from a fresh dict.

**Why this file and not `settings.local.json`.** The local file is the better
target on every count except the one that matters: Claude Code does not load hooks
from `~/.claude/settings.local.json`. It reads `permissions` from it and ignores
`hooks`. Verified against a control hook in `settings.json` that fired for
the same session in which the local-file hook did not. Writing to `settings.json` is
therefore not a preference, it is the only user-level option, and the marker-scoped
merge is what makes it safe to share with the file's other owners.

**The per-turn edge.** Once installed, `scripts/otto_hook.py` runs on six Claude Code
events, inside every turn, on every session on the machine, including sessions that
have nothing to do with Otto. A hook that is slow taxes all of them; a hook that
exits non-zero or writes to stdout can break the turn outright. Measured cost is
~64ms per invocation, ~128ms per full turn (`UserPromptSubmit` + `Stop`).

The ingest endpoint inherits this: `POST /api/sessions/{state}` is on the critical
path of every turn, so anything added to it is added to every prompt the owner submits.

## May write

Everything not on this list is out of bounds.

| Target | What | Why it is in bounds |
|---|---|---|
| `~/.claude/settings.json`, `hooks.*` entries carrying `otto:session-hook` | create / replace / delete | The install surface, and the only user-level file Claude Code loads hooks from. Scoped to its own marker. |
| Otto store: `sessions.json` | create / replace | Its own state, and the only writer of it |
| `Session.state`, `state_since`, `last_event`, `turns`, `note`, `last_message`, `run_id` | replace | Its own bookkeeping |
| Event log, via `store.log` from the tick sweep | create | Reporting a session presumed offline |

## Must not write

| Target | Why |
|---|---|
| Any key in `settings.json` other than `hooks` | `permissions`, `model`, `enabledPlugins` all live there and belong to Claude Code and to the owner. This module has no business near any of them. |
| `~/.claude/settings.local.json` | Not because it is unsafe, but because Claude Code does not load hooks from it. Writing there produces config that looks correct and never runs. |
| Hook entries without the marker | They belong to somebody else. `_is_ours()` gates every removal. |
| Any `Run` record | Sessions and runs are joined by reading `Run.session_id`, never by writing it. A hook must not be able to alter run history. |
| A state not reported by a hook | The one exception is `offline` after `SESSION_OFFLINE_HOURS` of silence, which sets `offline_inferred=True`. A stuck `busy` is never rewritten. |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| none | The hook posts to `127.0.0.1` with no auth | loopback only |

The endpoint is unauthenticated, which is the same posture as the rest of the Otto
API and acceptable for the same reason: the daemon binds `127.0.0.1` by default. The
consequence specific to this module is that any local process can assert a session
state. The blast radius of a forged POST is a wrong row in a panel and possibly a
spurious "waiting on you" alert. It cannot dispatch anything, and `record()` refuses
a payload with no `session_id` so it cannot create anonymous rows.

## Partial failure

- **Idempotent?** Yes, on both edges. `merged_settings()` applied twice is identical,
  and re-`record()`ing the same state does not move `state_since` or double-count a
  turn.
- **Retries?** None, deliberately. A hook that cannot reach the daemon drops the
  event and returns 0. The next event re-reports the state, so a missed one
  self-heals within a turn. Retrying inside a hook would put the daemon's downtime on
  the owner's keyboard latency.
- **Left behind** A crashed session never sends `SessionEnd`, so its row sits in its
  last reported state until the sweep presumes it offline. Sessions from a previous
  boot behave the same way. Both are visible as `offline_inferred`.
- **Recovery** `otto sessions doctor` reports which link in the chain is broken.
  `otto sessions uninstall` removes every hook entry and leaves the rest of the file
  untouched; it is a complete undo of the install edge.

## The honesty rule

Otto has no PTY and cannot ask a session whether it is alive. Every state here is
either something a hook reported or something explicitly labelled as inferred. This
mirrors `orphaned` on runs: an unknown outcome is reported as unknown rather than
resolved into a plausible one. A `busy` session that has been silent for four hours
is reported as ambiguous, at `info`, and is not converted into a death nobody
witnessed.
