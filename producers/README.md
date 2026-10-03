# Producers

A producer is anything that writes a JSON file. That is the entire contract.

Otto's daemon reads `FEED_DIR/<source>/current.json` on its tick and ingests it
into state. The producer does not import Otto, does not call the API, does not
need the daemon to be up, and does not need to know Otto exists beyond the path.
A scheduled PowerShell script, a Lambda writing to a synced folder, an agent
mid-run, or a human with a text editor are all valid producers.

Feed root on this machine (`otto feeds` prints it):

    %USERPROFILE%\.claude\otto\feed\<source>\current.json

Scripts in this directory are producers the owner keeps. **Otto never executes
anything in here.** It is a home for the scripts a human or a cron entry runs,
kept next to the feed so the two are read together. Otto running them would make
a file executable just by being placed in a directory, which is the one thing
this design must not do.

## Producers that need MCP

A producer in this directory is a script, so it can only report what a script can
reach. Anything behind an MCP server (Slack, Notion, your IdP, your RMM) is invisible
to it: the daemon has no MCP access and neither does a scheduled PowerShell file.

For those, the producer is a **slash command** in `claude\commands\`, run on a
cadence by an armed Otto schedule. It gets a headless Claude session with the
connectors, and writes the same `current.json` to the same path. Nothing about the
envelope, the trust ceiling, or the declaration step changes.

Live example: `slack-dm` is produced by `/slack-sweep`, armed hourly.

    python -m otto schedules        # its cadence
    python -m otto feeds            # whether its last drop landed

This does not weaken the "Otto never executes anything in `producers/`" rule
above. That rule is about *placement* not being consent. Arming a schedule is a
human running `otto schedule arm` and being told the session will use
`--dangerously-skip-permissions`, which is consent, stated out loud.

## Why a file and not an HTTP push

Otto has a single-writer rule: only the daemon writes state. A feed directory that
arbitrary producers write to would break that rule *if feeds were state*. They are
not. **A feed is an inbox.** You write a file; the daemon reads it and does the
state write itself. One writer for state, many producers. An HTTP push endpoint
would just be the existing snapshot API again, which is what made every new source
a code change in two places.

## The file

```json
{
  "produced_at": "2026-08-04T14:02:00Z",
  "producer": "netcheck.ps1 on WORKSTATION-01",
  "summary": "one short line, this is the panel headline",
  "items": [
    {"when": "14:02", "title": "3 endpoints failed the MTU probe"}
  ]
}
```

- `produced_at` is **required** and must be RFC3339/ISO. It is the age Otto
  renders, so it must be when the data was gathered, not when the file was
  written. A stamp in the future is rejected: that would be a quiet producer
  looking permanently fresh, which is the exact failure Otto refuses to have.
- `items` is capped at 10 and is for rendering. A feed is a panel, not a log.
- `summary` is one line. Say the number, not "see items".
- `producer` is free-form provenance. Rendered, never trusted.
- Unknown fields are a **hard error**. A misspelled `itmes` gets you a loud
  rejection instead of a silently empty panel, because an ignored field means a
  producer believing it sent something it did not.

Write it atomically (temp file in the same directory, then move over the top). A
half-written file is not a disaster (Otto retries next tick and re-reads on
digest change), but it does spend a rejection.

Unchanged files are skipped by content digest, so rewriting an identical file
every minute costs nothing and re-ingests nothing.

## Getting ingested at all

A directory is not enough. The source must be **declared** in
`config.FEED_SOURCES`:

```python
FeedSource("netcheck", WORK, TRUST_DATA, max_age_hours=26,
           title="fleet netcheck",
           why="Scheduled netcheck run drops its rollup here.")
```

An undeclared directory is never read. It shows up in `otto feeds` and in
`otto gaps` as drift, and nothing in it reaches state. If merely creating a
directory got you ingested, anything that can write a file could put cards on
the owner's board. Declaring a source is the human step, and it is deliberate.

`max_age_hours` is the other half. Declare it, or the source can stop forever and
nothing will ever say so, `otto gaps` reports a declared feed with no max age for
exactly that reason.

## Trust: what a feed is allowed to do

A feed file is untrusted input. An agent may have written it. So `trust` is a
**ceiling enforced by the ingester**, never something the file can claim.

| trust      | panel | propose board cards | max notice level |
|------------|-------|---------------------|------------------|
| `data`     | yes   | no                  | `info`           |
| `advisory` | yes   | yes, `auto=False`   | `warn`           |

Default to `data`. It is what almost every producer should be.

No trust level reaches `crit`, and none can auto-dispatch anything. A feed that
could raise a crit notice or file a task with `auto=True` is a feed that can start
agents unattended, and that is the line this design exists to hold.

### `advisory`: proposing work

An `advisory` source may add a `tasks` array:

```json
{
  "produced_at": "2026-08-04T14:02:00Z",
  "summary": "2 endpoints off the VPN for 9 days",
  "tasks": [
    {"title": "Check WS-ENG-07 VPN enrollment",
     "detail": "Off the tunnel since 2026-07-26, 9 days. Device is otherwise online in the RMM.",
     "priority": "normal"}
  ]
}
```

These go through the same filer agent findings use, so they get the same
discipline: fingerprint dedup (a producer running hourly bumps `seen_count`
instead of duplicating a card), a per-run cap, overflow **logged** rather than
silently dropped, and `auto=False` always. A proposal lands in the backlog for a
human to pull. It never runs itself.

A `data` source that sends `tasks` gets them ignored *loudly*, the event log says
so, because a producer proposing work it is not trusted to propose has a wrong
idea of its own role.

`detail` carries the evidence. "Check the VPN" is useless; the example above is
actionable.

### Notices

Any source may add one `notice` to interrupt:

```json
{"notice": {"title": "signing cert expires in 9 days",
            "body": "EV code-signing cert in the signing account, expires 2026-08-13.",
            "level": "warn",
            "command": "otto task add \"renew code-signing cert\""}}
```

`level` is a **request**. It is clamped to the trust ceiling, not rejected ,
dropping the whole notice over a bad level field would lose real content. `info`
lands in the dashboard without interrupting; `warn` raises a toast.

## Debugging

    otto feeds            # per-source state, age, trust, and why it is unhappy
    otto feeds --json     # the same plus the raw ingest ledger
    otto feeds --init     # create the roots and a dir per declared source
    otto gaps             # quiet producers and undeclared directories

`otto feeds` reads the filesystem directly, so it answers with the daemon down ,
which is when you ask why nothing ingested your drop. States you will see:

| state    | meaning |
|----------|---------|
| `ok`     | ingested, inside its declared max age |
| `stale`  | ingested, but the producer has gone quiet |
| `no-drop`| declared, directory there, nothing ingested yet |
| `no-dir` | declared, directory missing, run `otto feeds --init` |
| `REJECT` | a drop is failing validation; the reason is printed |

`REJECT` is the interesting one: the producer is writing, so it believes it is
reporting, but nothing is being ingested. That is worse than a dead producer
because it looks alive. It is reported once per bad drop, not once per tick.
