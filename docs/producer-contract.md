# Otto producer contract

Version 1. Everything a producer author needs. If what you want fits the primitives
below, build it. If it does not, ask for a daemon change.

The daemon is the shell. Only the daemon writes state (`Store._write` raises
`WriteDenied` in any other process), and it owns dedupe, staleness, toasts, the board,
the notice store, and the alert-versus-gap distinction. A producer hands the daemon a
fact. The daemon decides what the fact becomes. That is what makes a producer safe to
add: it cannot corrupt the board, re-toast a week of notices on restart, or bypass a
rate limit, because none of those decisions are made in the producer.

Six producers exist: the feed directories (`otto/feeds.py`), agent findings
(`otto/findings.py`), the nudges (`otto/nudges.py`), the help-channel summon poller
(`otto/summon.py`), the owner's DM inbox (`otto/inbox.py` with
`claude/commands/otto-dm.md`), and the outreach composers (`otto/outreach.py`).
Section 6 lists where they still do not match this document.

## 1. Register the producer

A feed is one line in `config.FEED_SOURCES` (`otto/config.py`):

```python
class FeedSource(NamedTuple):
    name: str                       # directory under FEED_DIR, exactly
    domain: str                     # work | personal
    trust: str = TRUST_DATA         # ceiling on what this source may do
    max_age_hours: float | None = None   # declared freshness; None = never stale
    title: str = ""                 # what the panel calls it
    why: str = ""                   # what it is for, and who produces it
```

- `name` is the feed directory, the snapshot key (`feed:<name>`), and the notice
  source (`feed:<name>`). It must match `NAME_RE` in `otto/feeds.py`
  (`^[a-z0-9][a-z0-9_-]{0,39}$`). Renaming it orphans the directory and the dedupe
  history. Retitling is free.
- `trust` has two levels. `data` may become a panel and nothing else. `advisory` may
  also propose board cards. Neither may raise `crit` or auto-dispatch.
- `max_age_hours` turns a producer that dies into a gap instead of silence. Size it to
  the cadence. `slack-dm` runs hourly and declares 3: two misses alarm, one does not.
- `why` is shown by `otto feeds`. Write it for whoever finds the directory a year from
  now.

An undeclared directory under `FEED_DIR` is never read; `feeds.undeclared()` reports
it as drift. Declare the source in the same commit as the producer, never before. A
declared source with no producer reports a gap forever.

Producers that are not feeds (`summon.py`, `inbox.py`, the nudges, the findings
harvester) are registered by being called from `daemon.tick()`. They run in the
daemon's process and are trusted accordingly.

## 2. The primitives a producer may emit

Eight exist. If none fits, ask for a daemon change.

| Primitive | Where it enters | What the daemon adds | What the producer may NOT set |
|---|---|---|---|
| **Panel** (`items`, `summary`) | `FeedFile.items` in a drop | A `Snapshot` keyed `feed:<name>`. Age is the producer's `produced_at`, never ingest time. Staleness from `max_age_hours` via `feeds.gaps()`. At most `FEED_MAX_ITEMS` (10) items and `FEED_MAX_BYTES` (256 KB) per drop | A `produced_at` more than 5 minutes in the future is rejected. Unknown envelope fields reject the whole file (`extra="forbid"`) |
| **Notice** | `FeedFile.notice`; `notify.post()` from daemon code; `POST /api/notices` from a session (`otto notify`) | Level clamped to the trust ceiling (`FEED_MAX_NOTICE_LEVEL`). Dedupe on `key` or `notify.fingerprint` inside `NOTICE_DEDUPE_HOURS` (20). One toast per notice, only for `warn` and `crit`. Weekend and quiet-hours holds. Per-source daily cap | A feed may not reach `crit`. `data` trust may not exceed `info`. Nothing may set `notified_at` |
| **Card** (task proposal) | `FeedFile.tasks`; the `<<<OTTO ... OTTO>>>` block in a run's reply; `POST /api/tasks/propose` (`otto propose`) | Always through `findings.file_tasks`. `dedupe.find_match` catches the same title, topic, or a near-alike title and bumps `seen_count` instead of filing; at 3 a `normal` card becomes `high`. Capped at `FINDINGS_MAX_PER_RUN` (5) per run and `FINDINGS_MAX_PER_DAY` (12) new cards per day. A finding about Otto itself goes to `DEBT.md`, not the board. `status=backlog`, `auto=False`, `source="agent"`, `tags=["from:<origin>"]` | `auto=True`, any status other than `backlog`, a `crit`-equivalent. A `data`-trust feed proposing tasks gets them IGNORED with a logged note |
| **Thread note** | `python -m otto thread-note <person> "<text>"` from a spawned session, `POST /api/threads/note` | Appended to the dossier's Threads section with today's date. `nudges.stale_threads` and `otto prep` read it later | A slug that does not resolve. File a task and say so rather than inventing a person |
| **Contact fact** | `contacts` in the DM producer's spool, `SLACK_SPOOL_DIR/new.json`, read by `people.ingest_spool_contacts()` | Forward-only, idempotent. Counts only when both directions exist within `CONTACT_EXCHANGE_WINDOW_DAYS` (4). Backfills `slack_id` on an email match | Anything else in the spool. The sweep ignores the `contacts` key and never copies it into a drop |
| **Finding** | Same as Card, from an agent that was doing something else and noticed a problem | Same filer. `origin` is the run's schedule or task title, so the card says who noticed | Filing the thing it was asked to do. Filing opinions about what to build. Both are in `findings.INSTRUCTIONS` |
| **Outreach** (compose only) | `POST /api/outreach`, `otto outreach compose`, or `outreach.compose()` | Written in `held` state for `OUTREACH_HOLD_MINUTES` (10). Roster gate, internal recipients only, forbidden-subject filter, `OUTREACH_MAX_PER_DAY` (6) and `OUTREACH_MAX_PER_PERSON_DAY` (2). A warn notice to the owner every time. Sent by the daemon as Otto after the hold | An empty `why`. A channel post outside `OUTREACH_CHANNELS`. A person with no dossier. Any direct send. `directed()` skips the hold only for a message the owner asked for by name |
| **Report to the owner** | `otto tell` / `POST /api/slack/tell` | Sent as Otto into Otto's DM with the owner, logged. No allowlist because it cannot be aimed anywhere else | A recipient. There is no field for one |

A notice is Otto saying something to the owner, deduped by subject. A card is work,
deduped by title. Do not turn every observation into a card or every task into a
notice.

## 3. Slots the daemon fills

You do not build these. The daemon renders them the same way for every producer.

- **Dedupe.** Notices by `key`, or by `notify.fingerprint` (card id first, then the
  title with parentheticals and punctuation stripped), inside a 20-hour window. Cards
  through `dedupe.find_match`. Drops by bytes digest, so an unchanged file costs a few
  `stat()` calls per tick.
- **Staleness.** A panel's age is `produced_at`. A restart cannot make a week-old drop
  look fresh. A producer claiming tomorrow is rejected.
- **Toast once.** `notified_at` is set when the toast fires. `info` never toasts.
- **Weekend and quiet hours.** Work notices below `crit` do not buzz on Saturday or
  Sunday (`WEEKEND_PAUSES_WORK`) or between `QUIET_FROM` and `QUIET_TO`. They are
  stored and shown, and toast once the window ends.
- **Daily cap per source.** `SOURCE_DAILY_CAP` in `otto/notify.py`. `observe` gets one
  interruption a day. Over the cap is dropped with a logged note, not an error.
- **Board column.** A proposed card lands in Backlog. Only a human, or `/orchestrate`
  under the tier gates, moves it to Queued, and Queued is what dispatches an agent.
- **Alert versus gap.** `compute_alerts()` reports what broke while Otto was watching.
  `feeds.gaps()` reports a declared producer that went quiet. A producer emits
  neither. It emits data on a cadence and the daemon notices the absence.
- **Provenance.** `origin`, `origin_run_id`, `source`, and `producer` are recorded so a
  bad card can be traced to the run that filed it.

## 4. The rules

Each one exists because the failure after it happened. The incidents are documented
in the code they changed.

1. **Never write state. Write a drop, or call the API.** Producers in `producers/`
   import nothing from Otto and do not need the daemon up. *Breaks:* a producer that
   opens `state/tasks.json` because the API felt slow, until the daemon's tick lands
   a read-modify-write on the same file and the board comes back short.
2. **Never invent identity. Supply a stable key.** A model never writes the same
   sentence twice, so the digest dedupe is the wrong instrument for its prose. Put the
   Slack `ts`, the card id, or the thread id in `notice.key`. *Breaks:* one errand,
   four phrasings, six toasts overnight (`Notice.key` in `otto/models.py`).
3. **Never assume you are the only producer.** Five cards per run, twelve new cards a
   day, six outreach a day, two per person, one `observe` a day, ten panel items. File
   your top five. Filing fifty and letting the cap choose throws away the ranking only
   you could do. *Breaks:* "the same flaky workstation crashed again" every morning
   until the board is noise.
4. **Never arm anything.** No `crit` from a drop. No `auto=True` from anywhere but a
   human. No `--status queued` without a tier gate. No outreach without a `why`. No
   send that skips the hold or `directed()`. *Breaks:* a card whose detail said "Needs
   the owner: rotate both credentials" was dispatched unattended and did exactly that
   (`scripts/otto_guard.py`, WIDENED TO MUTATIONS).
5. **Never speak as the owner.** The Slack connector is OAuth'd as the owner. The
   guard denies any connector send aimed at a person and points at `otto dm`, which
   sends the same text as Otto into a group DM that includes the owner. *Breaks:* a
   colleague got two DMs "from the owner" in one day that the owner never typed
   (`scripts/otto_guard.py`, the identity carve-out).
6. **Every action names its `otto` equivalent.** A notice carries `command`. A card's
   `detail` carries the command that would fix it. A reply to the owner's DM names the
   card id. *Breaks:* "I've processed your messages," and the owner checks by hand.

## 5. Worked example: `netcheck`

`otto/config.py` carries the declaration as a comment. Uncommenting it is the human
step:

```python
FeedSource("netcheck", WORK, TRUST_DATA, max_age_hours=26,
           title="fleet netcheck",
           why="Scheduled netcheck run drops its rollup here; Otto has no "
               "way to reach the fleet itself."),
```

The producer is a PowerShell script copied from `producers/example_producer.ps1`, run
by a scheduled task at 06:00. It imports nothing from Otto. It writes atomically (temp
file in the same directory, then move) to
`%USERPROFILE%\.claude\otto\feed\netcheck\current.json`:

```json
{
  "produced_at": "2026-08-27T06:04:11Z",
  "producer": "netcheck.ps1 on WORKSTATION-01",
  "summary": "3 of 58 endpoints failed the MTU probe",
  "items": [
    {"when": "06:02", "title": "WS-ART-01 MTU 1400 (expected 1500), path via remote desktop"},
    {"when": "06:02", "title": "WS-ART-03 MTU 1400 (expected 1500)"},
    {"when": "06:03", "title": "GPU-EC2 no reply, instance stopped"}
  ],
  "notice": {
    "title": "3 endpoints failed the MTU probe",
    "level": "warn",
    "key": "netcheck:mtu:2026-08-27",
    "command": "otto agenda"
  }
}
```

On the next tick, `feeds.ingest`:

1. Validates the envelope: `produced_at` present and not in the future, no unknown
   keys, under the size cap.
2. Stores a `Snapshot` keyed `feed:netcheck`, domain `work`, `fetched_at` 06:04:11Z.
   Today and `otto agenda` show "fleet netcheck, 3 items, 2h ago".
3. Sees `tasks` is empty. Proposed cards from a `data` feed would be IGNORED with a
   logged note.
4. Sees `notice.level` is `warn`, above the `data` ceiling, and CLAMPS it to `info`
   with a note in the event log. The notice is stored as "fleet netcheck: 3 endpoints
   failed the MTU probe", source `feed:netcheck`, key
   `feed:netcheck:netcheck:mtu:2026-08-27`. It does not toast. Tomorrow's key is new
   and posts a new notice. A re-run today with the same key bumps `seen_count`.
5. Records feed state: digest, `produced_at`, `ingested_at`, item count, and the
   coverage watermark if the drop claimed one.

If the scheduled task dies, the second morning shows a gap ("fleet netcheck: no drop
for 30h, declared 26h") instead of a quiet panel that looks like a quiet fleet. To
make the notice toast, raise the source to `advisory` in `config.py`. That is a
reviewed change and it comes with the ability to propose cards.

## 6. Where today's producers break the contract

1. `findings.INSTRUCTIONS` tells every spawned session that "a guard hook will
   refuse" outreach sends and credential checkouts. The guard was narrowed to
   destructive actions and outreach is ungated (`scripts/otto_guard.py`, NARROWED TO
   DESTRUCTIVE). The prompt is stricter than the enforcement, which makes a session
   hesitate to use `otto dm`, the route it should use.
2. `POST /api/notices` has no trust ceiling. It accepts `level: crit` from anything
   that can reach the daemon, including `otto notify` from a spawned session. Feeds
   are clamped in `feeds.ingest`; the HTTP path is not. Today the only writers are
   Otto's own commands.
3. Three dedupe mechanisms. Notices by `key` or fingerprint in `notify.post`. Cards
   through `dedupe.find_match`. Nudges through `_already()` in `otto/nudges.py`, which
   checks whether a `source` posted today (or this week for threads) and passes no
   `key`. Each is right for its primitive. They are not one mechanism.
4. `summon.py` and `inbox.py` keep watermarks in `store.feed_state()` but are not in
   `FEED_SOURCES`, so `otto feeds` does not list them and `feeds.gaps()` cannot tell
   when one stops polling. Their liveness is visible only in the event log.
5. The contact spool (`SLACK_SPOOL_DIR/new.json`, written by
   `producers/slack_dm_producer.py`, read by `people.ingest_spool_contacts()`) is a
   second inbox outside `FEED_DIR` with no declaration, no `max_age_hours`, no trust
   level, and no `otto feeds` row. It is also the input to the DM sweep, so one file
   has two consumers with different rules about which keys are theirs.
6. `/otto-dm` runs `task add`, `decide`, `thread-note`, and `tell` from a spawned
   session on the owner's instruction, with `--status backlog` and `otto dm`, but no
   fingerprint and no cap. A repeated DM files a repeated card. Acceptable because the
   producer is the owner. A copy of this shape for anyone else would not be.

## 7. Checklist before you add producer N+1

- [ ] It is declared in `config.FEED_SOURCES` in the same commit as the producer, or
      it is called from `daemon.tick()`. Nothing else is read.
- [ ] `name` is lowercase, matches `NAME_RE`, and you will keep it forever.
- [ ] `trust` is `data` unless you can say which cards it should propose and why a
      human should not file them instead.
- [ ] `max_age_hours` is set from the cadence: two misses alarm, one does not.
- [ ] `produced_at` is when you gathered the data, in UTC, never in the future.
- [ ] A quiet run still writes the envelope. "0 items" and "did not run" stay
      distinguishable.
- [ ] Every notice has a `key` that survives the prose being rewritten.
- [ ] Nothing you emit sets `crit`, `auto`, `queued`, or a send. If the task needs one
      of those, the output names it and a human does it.
- [ ] Every notice and card carries the `otto` command that acts on it.
- [ ] It writes atomically: temp file in the same directory, then move.
- [ ] It never reaches for the Slack connector to message a person. `otto dm` or
      nothing.
- [ ] You ran `python -m otto feeds` after the first drop and it says `ok`, not
      `REJECTED` or `undeclared`.
