# Runs in panes

Otto's own runs (schedules, board tasks, card replies, `otto task open`) happen inside
herdr panes when the herdr server is up. You can watch a run live, with scrollback,
next to your own sessions, instead of reading NDJSON through `otto logs`.

Written against herdr 0.9.3. `otto/runners/herdrpane.py` is the live version.

- A workspace labeled `otto-runs` holds one tab per run, created with `--no-focus` so
  a run never steals the pane you are typing in. The Run records `pane_id` and
  `workspace_id`. `otto live` and the dashboard live card show them.
- Everything else is unchanged: the same launcher script, the same log file with
  byte-exact NDJSON, the same pid (the launcher's PowerShell, found as a child of the
  pane shell) with the create-time check, the same result object for the ledger, the
  same settle paths. The pane is a place to look, not a new runner.
- Headless runs are teed by a per-run `<log>.pane.ps1`: a UTF-8 StreamWriter with LF
  endings, flushed per line. Not `Tee-Object`, which Windows PowerShell 5.1 writes as
  UTF-16LE and which renders native stderr as a five-line ErrorRecord.
- Attended runs (`otto task open`) get an interactive `claude` in a focused pane and
  their prompt through `herdr agent prompt` once herdr detects the agent. No log, as
  before. Usage comes off the transcript.
- If the server is down or herdr refuses before the command is in a shell, the run
  goes the old detached way and its notes say `herdr unavailable, ran detached`.
  Nothing retries after the command has been typed, so a run is never launched twice.
- A finished run's pane stays `OTTO_HERDR_RUNS_KEEP_MINUTES` (60) for scrollback, then
  the tick closes it. A pane whose process is still alive is never closed.

`OTTO_HERDR_RUNS=0` turns the whole thing off.
