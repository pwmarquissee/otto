# Producers

A producer is anything that writes a JSON file to `FEED_DIR/<source>/current.json`.
The daemon reads the file on its tick and ingests it. The producer does not import
Otto, call the API, or need the daemon to be up. A scheduled PowerShell script, a
Lambda writing to a synced folder, or a person with a text editor all qualify.

`otto feeds` prints the feed root. The default is `%USERPROFILE%\.claude\otto\feed`.

Otto never runs anything in this directory. The scripts here are run by a person or
a scheduler and live next to the feed they write.

| File | What it does |
| --- | --- |
| `example_producer.ps1` | Template. Copy it, replace `Get-FeedPayload`, keep the atomic writer. Its `example` source is undeclared, so its drops are reported and not ingested. |
| `slack_dm_producer.py` | Fetches new Slack DMs since a watermark and writes the `slack-dm` feed. No model call. |
| `slack_dm_verify.py` | Checks that a Slack user token has the scopes the DM producer needs. Run it before the producer. |
| `slack_bot_verify.py` | Checks that Otto's bot token can post as itself and reach the owner. |
| `slack_app_manifest.yaml` | The one Slack app behind both tokens. Paste it into the Slack app manifest editor. |

A source behind an MCP server (Slack, Notion, an IdP, an RMM) cannot be a plain
script. Write it as a slash command in `claude/commands/` and arm an Otto schedule
to run it. The command gets a headless Claude session with the connectors and writes
the same file to the same path. Arming is a human step, and the CLI says the session
will run with `--dangerously-skip-permissions`.

The full contract, including producers that are not feed files, is in
[docs/producer-contract.md](../docs/producer-contract.md).

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

- `produced_at` is required, RFC 3339. Use the time the data was gathered, not the
  time the file was written. A timestamp in the future is rejected.
- `summary` is one line and becomes the panel headline. Say the number.
- `items` is for rendering and is capped at 10 (`OTTO_FEED_MAX_ITEMS`).
- `producer` is free-form provenance. Rendered, never trusted.
- `coverage_since` is optional, for producers that sweep a time window. It is the
  oldest moment this run read back to. Otto holds its watermark until a run covers
  the gap.
- Unknown fields are a hard error. A misspelled `itmes` is rejected, not ignored.

Write the file atomically (temp file in the same directory, then move it over the
top). Unchanged files are skipped by content digest, so rewriting an identical file
every minute costs nothing.

## Declaring a source

An undeclared directory is never read. It shows up in `otto feeds` and `otto gaps`
as drift. Add the source to `config.FEED_SOURCES`:

```python
FeedSource("netcheck", WORK, TRUST_DATA, max_age_hours=26,
           title="fleet netcheck",
           why="Scheduled netcheck run drops its rollup here.")
```

Set `max_age_hours`, or a producer that stops is never reported as stale. `otto gaps`
flags a declared feed with no max age.

## Trust

A feed file is untrusted input. `trust` is a ceiling the ingester enforces, and the
file cannot raise it.

| trust      | panel | propose board cards | max notice level |
|------------|-------|---------------------|------------------|
| `data`     | yes   | no                  | `info`           |
| `advisory` | yes   | yes, `auto=False`   | `warn`           |

Default to `data`. No level reaches `crit`, and none can start a run.

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

Tasks go through the same filer as agent findings. That gives fingerprint dedup (an
hourly producer bumps `seen_count` instead of adding a card), a per-run cap with
overflow logged, and `auto=False` always. The card lands in the backlog for a person
to pull. A `data` source that sends `tasks` has them ignored, and the event log says
so. Put the evidence in `detail`.

Any source may add one `notice`:

```json
{"notice": {"title": "signing cert expires in 9 days",
            "body": "EV code-signing cert in the signing account, expires 2026-08-13.",
            "level": "warn",
            "command": "otto task add \"renew code-signing cert\""}}
```

`level` is clamped to the trust ceiling, not rejected. `info` lands in the dashboard
without interrupting. `warn` raises a toast.

## Debugging

```
otto feeds            # per-source state, age, trust, and the rejection reason
otto feeds --json     # the same plus the raw ingest ledger
otto feeds --init     # create the feed root and a directory per declared source
otto gaps             # quiet producers and undeclared directories
```

`otto feeds` reads the filesystem directly, so it works with the daemon down.

| state    | meaning |
|----------|---------|
| `ok`     | ingested, inside its max age |
| `stale`  | ingested, but the producer has gone quiet |
| `no-drop`| declared, directory exists, nothing ingested yet |
| `no-dir` | declared, directory missing; run `otto feeds --init` |
| `REJECT` | the current drop fails validation; the reason is printed once per bad drop |

A `REJECT` producer looks alive, but nothing it writes reaches the board. Check it
first.
