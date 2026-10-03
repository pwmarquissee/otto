# HARDENING: refresh

**Tier** 1: the only auto-launching runner in Otto. Narrow by construction, which is
exactly why the narrowness needs to be written down and re-checked.
**Source** `otto/refresh.py`

## Blast radius

The daemon has no MCP access; a headless `claude -p` session does. So Otto refreshes
its own eyes on mail and calendar by spawning a short session whose only job is to
fetch and report. That session runs with `--dangerously-skip-permissions`.

That combination, unattended, on a timer, permissions bypassed, is the exact shape
this codebase otherwise refuses. It is allowed here for one reason: every mutating tool
is denied at the process boundary, so the *worst* outcome of a wrong run is a bad
snapshot, not a sent email. If the deny list ever narrows, that argument collapses and
this stops being safe to autostart.

Second-order radius: the snapshot feeds `otto next` and Otto's ranking. A fabricated
item does not damage a mailbox, it damages the owner's decision about what to do next. The
prompt therefore says *never invent an item; an empty list is correct and useful, a
fabricated one is not.*

## May write

| Target | What | Why it is in bounds |
|---|---|---|
| Otto store: `Snapshot` (agenda, mail) per domain | replace | The refresher's own output, one per domain |
| Otto store: `Run` record | create | Tracked handle for the spawned process |
| `LOG_DIR/<id>-refresh.prompt.txt` | create | The prompt handed to the session |
| `LOG_DIR/<id>-refresh.launch.ps1` | create | Generated launcher |
| `LOG_DIR/<id>-refresh.log` | create | Captured output |

## Must not write

| Target | Why |
|---|---|
| Anything in Gmail: send, draft, reply, label, archive, delete | It is taking a picture of the inbox, not acting on it. `Write`, `Edit`, `NotebookEdit`, `Task`, `KillShell` are denied via `--disallowed-tools`, plus any per-domain `deny` entries from `config.REFRESH_SOURCES`. |
| Anything in Calendar | Same. Read-only. |
| Files anywhere | `Write`/`Edit`/`NotebookEdit` are denied. |
| Another session | `Task` is denied, so it cannot fan out. |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| claude.ai Gmail + Google Calendar connectors | account-bound MCP, work domain | read (mutating tools denied) |
| `mcp-google-multi` `personal` alias | personal domain | read (mutating tools denied) |

The personal alias dies silently if its OAuth app leaves "In production" (7-day
tokens) or an npm upgrade wipes the package-root `.env`. A missing connector must
surface as `"unavailable: <reason>"` in the summary, never as an invented item.

## Partial failure

- **Idempotent?** Yes. A refresh replaces its domain's snapshot; running it twice is
  harmless.
- **Retries?** None automatic. A failed refresh leaves the previous snapshot in place,
  which is why staleness (not absence) is the thing to alarm on.
- **Left behind** Prompt, launcher and log files in `LOG_DIR` for a run that died.
  `_extract()` falls back to bracket matching rather than failing the whole refresh
  when the model wraps its JSON in a fence.
- **Recovery** `otto refresh --domain <work|personal>` re-runs it. If a connector is
  down, fix the connector; do not hand-edit the snapshot.

## Domain separation

One run per domain, always. The two inboxes sit behind different MCP servers, and a
single session fetching both would put personal mail and work mail in the same reply,
which is where they would start getting merged. `start()` raises on an unknown domain
and raises rather than guessing when `REFRESH_SOURCES[domain]['hint']` is unset ,
Otto will not guess which connector to use.

## Out of scope

- **Acting on mail.** Not triage, not replies, not labels. Read and report.
- **Becoming a general autostart feature.** This is deliberately the *only*
  auto-launchable runner, so adding a schedule can never accidentally arm something
  dangerous. Keeping that list at one entry is the control.
- **Merging domains** into one snapshot or one session.
- **Widening the deny list's exceptions.** The deny list is the entire safety argument
  for autostarting this at all.

## Human gate

None, by design and stated as such: this is the one place autostart is allowed. The
gate is replaced by the deny list. Any change that adds a tool back must re-answer
"is this still narrow enough to run unattended?" in this file first.
