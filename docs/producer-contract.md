# Otto producer contract

Version 1. This is the whole design input a producer author needs. There is no second
document and no design review: if what you want to do fits the primitives below, you
build it; if it does not, you ask for a daemon change, and every producer gets it.

The shape is borrowed from a page-extension contract in an internal web console, where
the shell owns navigation, connection, polling and every pixel of chrome, and a page
owns a descriptor, a JSON blob and a body made of named primitives. Otto's shell is the
**daemon**. It is the single writer of state, and it owns dedupe, staleness, toasting,
the board, the notice store and the alert-versus-gap distinction. A **producer** is
anything that gives the daemon something to say or file, and there are already seven
of them: the feed directories (`otto/feeds.py`), agent findings
(`otto/findings.py`), the deterministic nudges (`otto/nudges.py`), the help-channel
summon poller (`otto/summon.py`), the owner's DM inbox (`otto/inbox.py` with
`claude/commands/otto-dm.md`), the `/scout` and `/slack-sweep` commands, and the
outreach composers (`otto/outreach.py`). Each grew its own shape. This document is what
lets the eighth be added without a design conversation, and it names, in section 7,
where the first seven do not yet match it.

---

## 1. Why the daemon is the shell

Otto's one structural rule is that only the daemon writes state. `Store._write` raises
`WriteDenied` in any other process (`otto/store.py:167`), the CLI mutates over HTTP, and
agents mutate over HTTP. That rule is what makes a producer safe to add: a producer
cannot corrupt the board, cannot re-toast a week of notices on restart, cannot bypass a
rate limit, because none of those decisions are made in the producer. It hands the
daemon a fact and the daemon decides what the fact becomes.

The cost of not having a contract has already been paid three times, each documented
in the code it changed:

* `/slack-sweep` re-summarized the same DM every hour, a model never writes the same
  sentence twice, and the feed's bytes-digest dedupe saw a new drop each time. Seven
  warn notices about one errand, six of them toasted between 00:15 and 06:37 local
  (`otto/models.py`, the `Notice.key` comment; `otto/feeds.py:121`).
* A burst of 529 Overloaded errors one evening killed several scheduled runs. Each
  was raised as "failed (exit 1)", indistinguishable from a broken schedule, and three
  were actioned into dispatched agents that each concluded upstream was down. About
  $3.20 to re-derive a fact already in the run record (`otto/models.py`, the
  `Run.error_kind` comment).
* A run dispatched unattended from a board card whose body said "Needs the owner"
  revoked a colleague's GitHub token and archived their API key
  (`scripts/otto_guard.py`, the WIDENED TO MUTATIONS block). The card was filed by a
  producer that had no ceiling on what its card could do.

Every rule below exists because one of those happened.

---

## 2. Register the producer

A producer is declared before it is ever read. For a feed, the declaration is one line
in `config.FEED_SOURCES` (`otto/config.py:839` is the live one), and the type is
`FeedSource` (`otto/config.py:777`):

```python
class FeedSource(NamedTuple):
    name: str                       # directory under FEED_DIR, exactly
    domain: str                     # work | personal
    trust: str = TRUST_DATA         # ceiling on what this source may do
    max_age_hours: float | None = None   # declared freshness; None = never stale
    title: str = ""                 # what the panel calls it
    why: str = ""                   # what it is for, and who produces it
```

* `name` is a contract with the feed directory, the snapshot key (`feed:<name>`) and
  the notice source (`feed:<name>`). It is constrained by `NAME_RE`
  (`otto/feeds.py:93`) so a name cannot spell a path traversal. Renaming it orphans
  the directory and the dedupe history; retitling is free.
* `trust` is the ceiling. Two levels exist and there is no third
  (`otto/config.py:767`): `data` may become a panel and nothing else; `advisory` may
  additionally propose board cards. Neither may raise `crit` and neither may
  auto-dispatch. Those stay with detectors that are code in this repo.
* `max_age_hours` is what turns a producer that quietly dies into a **gap** instead of
  silence. Size it to the cadence: `slack-dm` runs hourly and declares 3, so two
  consecutive misses alarm and one transient failure does not.
* `why` is rendered in `otto feeds`. Write it for the person who finds the directory a
  year from now.

**DECLARED ONLY.** An undeclared directory under `FEED_DIR` is never read. It is
reported as drift by `feeds.undeclared()` (`otto/feeds.py:548`) and ignored. If
creating a directory were enough, anything that can write a file could put cards on
the owner's board. Declaring a source is the human step, and it goes in the same commit
as the producer, never before it: a declared source with no producer behind it reports a
gap forever and teaches the owner to ignore feed gaps.

Producers that are not feeds (the pollers in `summon.py` and `inbox.py`, the nudges,
the findings harvester) are registered by being called from `daemon.tick()`
(`otto/daemon.py:581`). That is a code change, which is the point: they run in the
daemon's own process and are trusted accordingly.

---

## 3. The primitives a producer may emit

Eight exist. If none fits, that is a request for a daemon change, not a license to
invent a ninth path into state.

| Primitive | Where it enters | What the daemon adds | What the producer may NOT set |
|---|---|---|---|
| **Panel** (`items`, `summary`) | `FeedFile.items` in a drop, `otto/feeds.py:154` | Stored as a `Snapshot` keyed `feed:<name>`; age is the producer's `produced_at`, never ingest time (`otto/feeds.py:433`, step 1); staleness from `max_age_hours` via `feeds.gaps()` (`otto/feeds.py:592`) | A `produced_at` more than 5 minutes in the future is rejected (`otto/feeds.py:100`); unknown envelope fields reject the whole file (`extra="forbid"`) |
| **Notice** | `FeedFile.notice` (`otto/feeds.py:121`); `notify.post()` from daemon code (`otto/notify.py:111`); `POST /api/notices` from a session (`otto/daemon.py:1667`, `otto notify`) | Level clamped to the trust ceiling (`FEED_MAX_NOTICE_LEVEL`, `otto/config.py:774`); dedupe on `key` or `notify.fingerprint` inside `NOTICE_DEDUPE_HOURS` (`otto/config.py:360`); one toast per notice, only for `warn` and `crit` (`otto/notify.py:32`); weekend and quiet-hours holds (`otto/notify.py:168`); per-source daily cap (`otto/notify.py:43`) | A feed may not reach `crit`; `data` trust may not exceed `info`. Nothing may set `notified_at` |
| **Card** (task proposal) | `FeedFile.tasks` (`otto/feeds.py:143`); the `<<<OTTO ... OTTO>>>` block in a run's reply (`otto/findings.py:32`); `POST /api/tasks/propose` (`otto/daemon.py:1434`, `otto propose`) | Always through `findings.file_tasks` (`otto/findings.py:144`): fingerprint dedupe on normalized title plus domain (`otto/findings.py:82`), a repeat bumps `seen_count` and escalates to `high` at 3, capped at `FINDINGS_MAX_PER_RUN` (`otto/config.py:587`) with overflow logged, `status=backlog`, `auto=False`, `source="agent"`, `tags=["from:<origin>"]` | `auto=True`, any status other than `backlog`, a `crit`-equivalent. A `data`-trust feed proposing tasks gets them IGNORED with a logged note (`otto/feeds.py:433`, step 2) |
| **Thread note** | `python -m otto thread-note <person> "<text>"` from a spawned session, `POST /api/threads/note` | Appended to the dossier's Threads section with today's date; the note is what `nudges.stale_threads` and `otto prep` later read | A slug that does not resolve. The rule in `otto-dm.md`: file a task and say so rather than inventing a person |
| **Contact fact** | `contacts` in the DM producer's spool, `SLACK_SPOOL_DIR/new.json`, read by `people.ingest_spool_contacts()` (`otto/people.py:816`) | Forward-only, idempotent; counts only when both directions exist within `CONTACT_EXCHANGE_WINDOW_DAYS` (`otto/config.py:262`); backfills `slack_id` on an email match (`otto/people.py:750`) | Anything else in the spool. `/slack-sweep` is told to ignore the `contacts` key and never copy it into a drop |
| **Finding** | Same as Card, from an agent that was doing something else and noticed a floor cracking | Same filer, `origin` is the run's schedule or task title so the card says who noticed | Filing the thing it was asked to do; filing opinions about ceiling work (what to build). Both are in `findings.INSTRUCTIONS` |
| **Outreach** (compose only) | `POST /api/outreach` (`otto/daemon.py:1731`), `otto outreach compose`, or `outreach.compose()` (`otto/outreach.py:214`) | Written in `held` state, held `OUTREACH_HOLD_MINUTES` (`otto/config.py:869`), roster gate, internal-recipients-only, forbidden-subject filter, 6/day and 2/person rate limits (`otto/config.py:875`), a warn notice to the owner every time, transmitted by the daemon as Otto after the hold | An empty `why` (refused, `otto/outreach.py:214`); a channel post outside `OUTREACH_CHANNELS`; a person with no dossier; any direct send. `directed()` (`otto/outreach.py:277`) skips the hold but only for a message the owner asked for by name |
| **Report to the owner** | `otto tell` / `POST /api/slack/tell` (`otto/daemon.py:1011`) | Sent as Otto into Otto's DM with the owner, logged. No allowlist because it cannot be aimed anywhere else | A recipient. There is no field for one |

The two primitives that look alike and are not: a **notice** is Otto saying something
to the owner and is deduped by subject; a **card** is work and is deduped by title. A
producer that turns every observation into a card has misunderstood the board, and a
producer that turns every task into a notice has misunderstood attention.

---

## 4. Slots the daemon fills

You never build these. Return the fact and the daemon renders it identically for every
producer, which is why a failing producer is legible in the same place as a healthy one.

* **Dedupe.** Notices by `key` (or the derived fingerprint, which prefers a card id
  and then the title with parentheticals and punctuation stripped, `otto/notify.py:68`)
  inside a 20-hour window. Cards by normalized title plus domain. Drops by bytes
  digest, so an unchanged file costs a few `stat()` calls per tick.
* **Staleness.** A feed panel's age is `produced_at`. A daemon restart cannot make a
  week-old drop look fresh, and a producer claiming tomorrow is rejected.
* **Toast once.** `notified_at` is set when the toast fires, so a restart does not
  re-toast a week of notices. `info` never toasts.
* **Weekend and quiet hours.** Work notices below `crit` do not buzz on Saturday or
  Sunday (`otto/config.py:311`) or between `QUIET_FROM` and `QUIET_TO`
  (`otto/config.py:328`). They are held, not dropped: the notice is stored and shows in
  the dashboard, and it toasts once the window ends.
* **Daily cap per source.** `SOURCE_DAILY_CAP` (`otto/notify.py:43`) is enforced at the
  one chokepoint every poster goes through. `observe` gets one interruption a day. A
  source over its cap is dropped with a logged note, not raised as an error, because
  the caller is a scheduled agent that can do nothing useful with an exception.
* **Board column.** A proposed card lands in Backlog. Only a human, or `/orchestrate`
  under the tier gates, moves it to Queued, and Queued is what dispatches an agent.
* **Alert versus gap.** `compute_alerts()` (`otto/daemon.py:320`) reports what broke
  while Otto was watching. `feeds.gaps()` reports a declared producer that has gone
  quiet. A producer never emits either; it emits data on a cadence and the daemon
  notices the absence.
* **Provenance.** `origin`, `origin_run_id`, `source`, `producer` are recorded so a bad
  card can be traced to the run that filed it. `otto/models.py` on `Outreach`: "a ledger
  full of opaque Slack ids is not one anybody audits."

---

## 5. The rules you must not break

**1. Never write state. Write a drop, or call the API.**
A feed is an inbox, not state. `Store._write` will raise at you from any process that is
not the daemon, and that is the design, not a nuisance. Producers in `producers/` import
nothing from Otto and do not need the daemon to be up.
*Breaks:* a producer that opens `state/tasks.json` because the API felt slow. It works
until the daemon's tick lands a read-modify-write on the same file, and the board comes
back short.

**2. Never invent identity. Supply a stable key.**
If a model writes your prose, the bytes are new every run and the digest dedupe is the
wrong instrument. Put the Slack `ts`, the card id, the thread id in `notice.key`
(`otto/feeds.py:121`). The fallback fingerprint catches near-repeats and will not catch a
full rewrite.
*Breaks:* the seven-notice night. Same errand, four phrasings, six toasts before 06:37.

**3. Never assume you are the only producer.**
The caps are there because you are one of seven. Five cards per run, six outreach a day,
two per person, one `observe` a day, ten panel items. A producer that files its top five
is fine; one that files fifty and lets the cap choose has thrown away the ranking it was
the only one able to do.
*Breaks:* a morning check filing "the same flaky workstation crashed again" every morning.
Within a week the board is noise and stops being read (`otto/findings.py`, module
docstring).

**4. Never arm anything.**
No `crit` from a drop. No `auto=True` from anywhere but a human. No `--status queued` from
a command that has not been through a tier gate (`claude/commands/scout.md`,
`claude/commands/otto-dm.md` both say so in bold). No outreach without a `why`, and no
send that does not pass through the hold or `directed()`.
*Breaks:* the rotation run. A card whose detail said "Needs the owner: rotate both
credentials" was dispatched unattended and did exactly that.

**5. Never speak as the owner.**
The Slack MCP connector is OAuth'd as the owner. A spawned session that reaches for
`slack_send_message` to DM a colleague posts under their name; it did, twice in one day
(`scripts/otto_guard.py`, the identity carve-out). The guard now denies any connector
send aimed at a person and points at `otto dm`, which sends the same text as Otto into
a group DM that includes the owner.
*Breaks:* a colleague receiving two DMs "from the owner" that the owner never typed.

**6. Every action names its `otto` equivalent.**
A notice carries `command`. A card's `detail` carries the command that would fix it. A
reply to the owner's DM names the card id. This is how a person graduates from reading
the dashboard to driving the CLI, and it costs one string.
*Breaks:* "I've processed your messages." They go and check by hand, which costs more
than typing it themselves would have (`claude/commands/otto-dm.md`, Always reply).

---

## 6. Worked example: `netcheck`, end to end

`otto/config.py:815` already carries the declaration as a comment. Uncommenting it is
the human step:

```python
FeedSource("netcheck", WORK, TRUST_DATA, max_age_hours=26,
           title="fleet netcheck",
           why="Scheduled netcheck run drops its rollup here; Otto has no "
               "way to reach the fleet itself."),
```

The producer is a PowerShell script copied from `producers/example_producer.ps1`, run by
a Windows scheduled task at 06:00. It imports nothing from Otto. It writes, atomically
(temp file, then move), to `%USERPROFILE%\.claude\otto\feed\netcheck\current.json`:

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

What the daemon does with it on the next tick (`otto/feeds.py:433`):

1. Validates the envelope. `produced_at` present, not in the future, no unknown keys.
2. Stores a `Snapshot` keyed `feed:netcheck`, domain `work`, `fetched_at` = 06:04:11Z.
   The dashboard and `otto agenda` render "fleet netcheck, 3 items, 2h ago".
3. Sees `tasks` is empty. Had the drop proposed cards, they would be IGNORED with a
   logged note, because `data` trust cannot file to the board.
4. Sees `notice.level` is `warn`, which is above the `data` ceiling of `info`, and
   CLAMPS it to `info` with a note in the event log. The notice is stored with source
   `feed:netcheck` and key `feed:netcheck:netcheck:mtu:2026-08-27`. It does not toast,
   because `info` never toasts. Tomorrow's run carries a new key and posts a new notice;
   a re-run today with the same key bumps `seen_count` instead.
5. Records feed state: digest, `produced_at`, `ingested_at`, item count.

What the owner sees: a panel on Today with three rows, an `info` card under From Otto with
the command to run, and, if the scheduled task dies, a **gap** on the second morning
("fleet netcheck: no drop for 30h, declared 26h") rather than a quiet panel that looks
like a quiet fleet.

If the author wants the notice to toast, that is a request to raise the source to
`advisory` in `config.py`, reviewed by a human, and it comes with the ability to propose
cards. There is no way to get a toast without that conversation.

---

## 7. Where today's producers break the contract

Named honestly, with pointers, so the next producer does not copy the wrong one.

1. **`/scout` writes to the board over HTTP, not through a drop.**
   `claude/commands/scout.md` runs `python -m otto task add ... --tags scout --origin
   scout` from a spawned session. That bypasses the trust ceiling and
   `findings.file_tasks`: no fingerprint dedupe (it uses its own ledger,
   `orchestrator/scout/scout.py check`), no per-run cap enforced in the daemon (the
   command promises "max 3"), and a card that arrives via `task add` has whatever
   fields the caller set. It does respect `--status backlog`. A feed-shaped `/scout`
   would declare `FeedSource("scout", WORK, TRUST_ADVISORY, ...)` and drop `tasks`.

2. **`/scout` carried a dated priority in its prose.**
   The command file named a specific demo milestone as the priority, and kept saying so
   for months after the date had passed. This is the exact failure a CLAUDE.md can
   describe for itself: a dated fact with no expiry, read into every run, steering work
   at a milestone in the past. Priorities live in `otto priorities`, which carries a
   review date.

3. **`findings.INSTRUCTIONS` describes a guard that no longer exists in that shape.**
   `otto/findings.py` tells every spawned session that "a guard hook will refuse"
   outreach sends and credential checkouts. The guard was later narrowed to destructive
   actions only, and outreach is explicitly ungated
   (`scripts/otto_guard.py`, NARROWED TO DESTRUCTIVE block). The prompt is now stricter
   than the enforcement, in the direction that makes a session hesitate to use `otto
   dm`, which is the route it should be using.

4. **`POST /api/notices` has no trust ceiling.**
   `otto/daemon.py:1667` accepts `level: crit` from anything that can reach the daemon,
   including any spawned session via `otto notify`. Feeds are clamped
   (`otto/feeds.py:433`, step 3); the HTTP path is not. Today the only writers are
   Otto's own commands, so this is a gap in enforcement rather than an incident, but it
   is the same gap the feed design closed on purpose.

5. **Three dedupe mechanisms, not one.**
   Notices dedupe on `key`/fingerprint in `notify.post`. Cards dedupe on title
   fingerprint in `findings.file_tasks`. The nudges dedupe a fourth way: `_already()`
   (`otto/nudges.py:54`) checks whether a `source` has posted today (or this week for
   threads) before composing, and passes no `key`, so the title fingerprint carries the
   count ("23 thread(s) went quiet") and would post again inside the window if the
   count changed and `_already` were ever bypassed. They are each correct for their
   primitive; they are not one mechanism a new producer can point at.

6. **Two pollers keep state under feed names that are not feeds.**
   `summon.py` (the help-channel summons) and `inbox.py` (`otto-dm`) store watermarks in
   `store.feed_state()`, sharing the per-source bookkeeping file with real feeds, but
   are not declared in `FEED_SOURCES`, so `otto feeds` does not list them and
   `feeds.gaps()` cannot tell when one stops polling. Their liveness is visible only in
   the event log.

7. **The contact spool is a second inbox outside `FEED_DIR`.**
   `producers/slack_dm_producer.py` writes `SLACK_SPOOL_DIR/new.json`
   (`otto/config.py:256`), which `people.ingest_spool_contacts()` (`otto/people.py:816`)
   reads directly with its own mtime-based dedupe. It is the right shape (a file the
   daemon reads and applies itself) but it has no declaration, no `max_age_hours`, no
   trust level, and no `otto feeds` row. It is also the input to `/slack-sweep`, so one
   producer file is read by two consumers with different rules about which keys are
   theirs.

8. **`/otto-dm` acts through the CLI with no cap.**
   `claude/commands/otto-dm.md` runs `task add`, `decide`, `thread-note` and `tell` from
   a spawned session on the owner's direct instruction. That is the correct route for a
   command from the owner, and it respects rule 4 (`--status backlog`, never queued) and
   rule 5 (`otto dm`, never the connector). What it lacks is any of section 4: no
   fingerprint, no cap, and a repeated DM files a repeated card. Acceptable because the
   producer is the owner; noted because a copy of this shape for anyone else would not be.

---

## 8. Checklist before you add producer N+1

- [ ] It is declared in `config.FEED_SOURCES` in the same commit as the producer, or it
      is called from `daemon.tick()`. Nothing else is read.
- [ ] `name` is lowercase, matches `NAME_RE`, and you are prepared to keep it forever.
- [ ] `trust` is `data` unless you can say which cards it should be allowed to propose
      and why a human should not file them instead.
- [ ] `max_age_hours` is set from the cadence: two misses alarm, one does not.
- [ ] `produced_at` is when you gathered the data, in UTC, never in the future.
- [ ] A quiet run still writes the envelope. "0 items" and "did not run" must stay
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
