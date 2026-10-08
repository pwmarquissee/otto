# Tests

```
pip install -e .[dev]
python -m pytest                          # everything
python -m pytest tests/test_store.py -v   # one file
python -m pytest -k priorities            # by name
```

CI runs the suite and `scripts/oss_scan.py` on Windows. The Linux job is
informational and may fail without failing the run.

## The sandbox

`otto.config` resolves `OTTO_HOME`, `STATE_DIR`, and the rest into module-level
constants at import time, so `tests/conftest.py` sets the environment before the
first `import otto`. Do not move that block or add an `otto` import above it. It sets
three things.

- `OTTO_HOME` to a fresh temp directory, so a test that forgets to pass `state_dir`
  still cannot write to the real board.
- `OTTO_NO_TOAST=1`, so no test can shell out to the real toast script. Redirecting
  `OTTO_HOME` does not prevent that on its own; a test once put a fake outage toast
  on the owner's screen.
- `OTTO_PORT` to a port nothing listens on, so a stray client call fails fast.

`test_sandbox_is_active` in conftest asserts all three. If it fails, stop the run.
The other tests are writing to real state. A test that redirects anything else (a
people directory, a DEBT file, a decisions log) does so for the same reason.

## What is covered

One file per module, named for it. Most tests guard a failure that happened, and the
docstring names it. Read the docstring before relaxing an assertion.

The `test_guard.py` fixture sets the operator knobs (`OTTO_GUARD_CRED_COMMANDS`,
`OTTO_GUARD_CRED_FILES`, `OTTO_GUARD_IDP_TOOL_PATTERN`) to synthetic values.

## What is not covered

- Most of `daemon.py`. `test_state_api.py` and `test_web_events.py` exercise a few
  routes over a fresh store. The tick loop and dispatch have no tests.
- `board.derived_cards`, stubbed out in `test_board.py`. Schedule staleness, orphaned
  runs, down integrations, and the run-acknowledgement rule are untested.
- Anything that talks to the outside. Slack posting runs against a stubbed sender in
  `test_slack_post.py`. Notion, Google, the EDR and RMM consoles, and `claude -p`
  spawning have no fakes.
- Concurrency. `Store` takes an `RLock`, but nothing runs two writers at once. The
  retry-on-`PermissionError` path is tested with a stub.
- The CLI end to end. Argument parsing, output formatting, and exit codes are untested.
- `config.py`, which is mostly environment-derived constants.

The daemon is the only writer, so that gap bites first.
