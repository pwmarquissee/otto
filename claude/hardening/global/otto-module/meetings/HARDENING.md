# HARDENING: meetings

**Tier** 1: the second auto-launching runner in Otto, alongside `refresh`. Narrow by
construction, which is exactly why the narrowness needs to be written down and
re-checked.
**Source** `otto/meetings.py`

## Blast radius

The daemon has no MCP access; a headless `claude -p` session does. So Otto reads
AI-generated meeting notes in Notion by spawning a short session whose only job is to fetch, extract and
report. That session runs with `--dangerously-skip-permissions`, unattended, every four
hours.

That combination is the exact shape this codebase otherwise refuses. It is allowed here
for the same single reason it is allowed for `refresh`: every mutating tool is denied at
the process boundary, so the worst outcome of a wrong run is a bad board card, not an
edited meeting record. If the deny list narrows, that argument collapses and this stops
being safe to autostart.

Two second-order radii, and the second is the one that actually matters:

- **Confidentiality.** Meeting notes carry unreleased feature names, vendor terms and
  personnel detail. `WebFetch` and `WebSearch` are denied for that reason specifically:
  the risk here is not a bad write, it is an outbound request carrying the contents of
  a 1:1.
- **The owner's attention and their commitments.** A card that misstates what somebody
  agreed to is worse than no card, because they will act on it, and worse still if it carries a
  fabricated deadline. Two inference steps sit between the meeting and the card: Notion
  AI wrote the transcript, and this session extracted from it. That is why every card
  carries the verbatim line and a link to the page, and why nothing is ever queued.

## May write

| Target | What | Why it is in bounds |
|---|---|---|
| Otto store: `Task` (status `backlog`, `auto=False`) | create / bump | The ingester's own output. Never `queued`, never `auto=True` unless `MEETINGS_AUTOQUEUE` is explicitly turned on |
| Otto store: `meetings.json` ledger | merge | Watermark + consumed page ids. Merge only; the watermark moves forward, never back |
| Otto store: `Notice`, one per run at most | create | The dated items that cannot wait |
| Otto store: `Run` record | create | Tracked handle for the spawned process |
| `LOG_DIR/<id>-meetings.{prompt.txt,launch.ps1,log}` | create | Prompt, generated launcher, captured output |

## Must not write

| Target | Why |
|---|---|
| Anything in Notion: create, update, comment, move, duplicate, upload | Notes are the record of a meeting, not Otto's scratchpad. All 12 write tools are denied on both server prefixes via `--disallowed-tools` |
| Files anywhere | `Write`, `Edit`, `NotebookEdit` denied |
| Anywhere outside the process | `WebFetch`, `WebSearch`, `SendMessage`, `PushNotification`, `RemoteTrigger`, `DesignSync`, `Artifact` denied |
| A schedule or another session | `CronCreate`, `CronDelete`, `Task`, `Bash` denied. A read-only job that can create a cron entry is not a read-only job |
| The `queued` column | An extracted action item is a proposal. `/orchestrate` promotes; the ingester never does |

## Credentials

| Credential | Where it comes from | Scope |
|---|---|---|
| Notion connector (`notion` in Claude Code, `claude_ai_Notion` account-level) | MCP, bound to the claude.ai login | Workspace-wide **read/write**. The token is not read-scoped, so the deny list is the only thing standing between this session and a write |

That last row is the whole reason this file exists. There is no scope-level backstop.

## Partial failure

- **Idempotent?** By page id, yes. Re-running consumes nothing already in the ledger.
  Ids are normalized first: two live runs returned the same page as `3ab1e0e4…` and as
  `https://app.notion.com/p/3ab1e0e4…`, and keying on whichever arrived would have
  filed every commitment in that meeting twice.
- **Per-run cap.** `MEETINGS_MAX_PER_RUN` (8) bounds cards per run. A page is marked
  consumed only when **every** action item it produced was filed, so the cap defers
  whole pages rather than splitting one. A page whose items alone exceed the cap is
  filed in full and the overflow reported, because splitting it is the one outcome that
  loses a commitment permanently. Deferrals are always named in the event log.
- **Connector down.** The summary must start `unavailable:` and the watermark must not
  move. Rolling it forward past meetings the session could not see would skip them
  forever, silently.
- **Retries?** None. A failed run leaves the ledger untouched and calls
  `store.mark_attempt`, never `store.stamp`, so staleness keeps counting. Stamping a
  failed fetch would make a dead ingester read as fresh.
- **Left behind** Prompt, launcher and log in `LOG_DIR` for a run that died.
- **Recovery** `otto meetings ingest`. If the connector is down, fix the connector; do
  not hand-edit `meetings.json`.

## Cost

The first live run inherited the default model: $1.71 for five meetings, ~$300/month at
this cadence to reread the same notes. Extraction is mechanical, so `MEETINGS_MODEL`
defaults to Sonnet and every run carries `--max-budget-usd`
(`MEETINGS_BUDGET_USD`, 1.5). An unattended schedule needs a ceiling it cannot exceed,
not a hope that it will not.

## Out of scope

- **Acting on what it found.** Not promoting, not queueing, not dispatching. File and
  report. `MEETINGS_AUTOQUEUE` defaults off and turning it on means accepting that a
  model-extracted claim can start a skip-permissions session with no human in the loop.
- **Writing to Notion**, including "helpfully" ticking a checkbox on the page.
- **Inventing a due date.** Only a date the meeting stated. A date earlier than its own
  meeting is treated as an extraction error and dropped.
- **Filing other people's work.** Only items that are the owner's, or that they have to chase.
- **Widening the deny list.** It is the entire safety argument for autostarting this.

## Human gate

None at ingest, by design and stated as such: this is the second of exactly two places
autostart is allowed, and the gate is replaced by the deny list. The human gate sits one
step later and is real: nothing this files can run until `/orchestrate` promotes it.

Any change that adds a tool back must re-answer "is this still narrow enough to run
unattended?" in this file first.
