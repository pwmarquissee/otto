# Tests

```powershell
python -m pytest              # everything
python -m pytest tests/test_store.py -v
python -m pytest -k priorities
```

Nothing gates on these. They run when you ask.

## The sandbox, and why it is at the top of conftest

`otto.config` resolves `OTTO_HOME`, `STATE_DIR` and the rest into module-level
constants **at import time**. Setting `OTTO_HOME` inside a test is too late.
`tests/conftest.py` therefore sets the environment before the first `import otto`
in the run, which works only because pytest loads conftest before any test
module. Do not move that block.

Three things it sets:

- `OTTO_HOME` to a fresh temp dir, so even a test that forgets to pass an
  explicit `state_dir` cannot write to the real board.
- `OTTO_NO_TOAST=1`. Redirecting `OTTO_HOME` sandboxes the store and nothing
  else. On 2026-08-07 a harness with a redirected home called
  `notify.deliver_pending`, which shelled out to the real toast script and put
  "Otto: Fleet-wide outage" on the owner's actual screen, then deleted its temp store
  so nothing in Otto explained it.
- `OTTO_PORT` to a port nothing listens on, so a stray client call fails fast
  instead of reaching a live daemon.

`test_sandbox_is_active` in conftest asserts all of this. If it fails, stop the
run: the other tests are writing to real state.

Anything a test redirects (a people directory, a DEBT file, a decisions log) is
redirected for the same reason: the module binds the live path at import, and
without the redirect a single write would land on real state.

## What is covered

| File | Covers |
|---|---|
| `test_store.py` | single-writer enforcement, `_read` never inventing emptiness, corruption quarantine, atomic write, task CRUD, run history trimming |
| `test_models.py` | Task/Run/BoardCard validation, JSON round-trip, `safe_text` surrogate handling |
| `test_board.py` | the Done window and its `hidden` count, column assembly, priority/due/title ordering |
| `test_priorities.py` | missing / template / undated / stale / ok, age arithmetic, gap rows |
| `test_detail_file.py` | `--detail-file`, stdin, BOM stripping, conflicting flags |
| `test_guard.py` | the unattended-session guard: which shapes gate, which never do, facts before the dialog, protected config. The operator knobs (`OTTO_GUARD_CRED_COMMANDS`, `OTTO_GUARD_CRED_FILES`, `OTTO_GUARD_IDP_TOOL_PATTERN`) are set to synthetic values by a fixture |
| `test_outreach_directed.py` | the roster gate against explicit `ORG_DOMAINS` / `CONTRACTOR_DOMAINS`, the owner always in the group DM, the per-run brake, transport failure recorded not raised |
| `test_dedupe.py` | the three tiers of "same card" (fingerprint, topic, similar title), who stays, what merging carries, a duplicate is not a completion |
| `test_verdict.py` | the two-lane verdict for every run status (api error and budget cut are harness lanes, not work failures), plan phases, expected duration from prior good runs |
| `test_known.py` | known-failure annotations: kind and target validation, `healthy()`, the sweep posting exactly one `known-stale` notice and never a second |
| `test_outreach_extend.py` | pushing a held send back, the 24h-from-compose ceiling, no effect on a non-held record |
| `test_ledger.py` | pricing sessions from transcripts: one API response per `requestId` no matter how many content-block lines it left (the first pass summed them and came out 2.1x high), images sized as images not base64, rebuilds after idle gaps, subagent files folded into their session, telemetry preferred only when it covers the session |
| `test_telemetry.py` | the OTLP receiver's allowlist (user.email and account ids never reach disk), gzip and protobuf handling, per-session rollup deduped by request id, the settings.json env block installing idempotently and never rewriting broken JSON |

Most of these are regression guards on failures that actually happened, and the
test names say which. `_read` returning a default on `OSError` lost the entire
task board; the Done column grew to 65 cards against 38 in Backlog; a dated
priority outlived its milestone by two months; a lone surrogate took the
dashboard down. Those docstrings are the reason the assertions look the shape
they do, so read them before relaxing one.

## What is NOT covered

Be aware of these before trusting a green run.

- **The daemon.** `daemon.py` is the largest module and has no tests. Every HTTP
  endpoint, the tick loop, and dispatch are unexercised.
- **`board.derived_cards`.** Stubbed out in `test_board.py` so a board assertion
  cannot fail for an unrelated reason. Schedule staleness, orphaned runs, down
  integrations and the run-acknowledgement rule are all untested.
- **Anything that talks to the outside.** Slack, Notion, Google, the EDR and
  RMM consoles, and `claude -p` spawning. No fakes exist yet.
- **The operator's own scripts.** Heartbeat probes, EDR sensor checks and the
  known-patterns store live outside this tree, so their tests are not here.
- **Concurrency.** `Store` takes an `RLock` and the docstring is specific about
  which compound mutations need it, but nothing here runs two writers at once.
  The retry-on-`PermissionError` path is tested with a stub, not a real race.
- **The CLI end to end.** `_resolve_detail` is tested directly; argument parsing,
  output formatting and exit codes are not.
- **`config.py`.** 67KB of mostly environment-derived constants, unexercised.

The gap that would bite first is the daemon, since it is the only writer and
every CLI write goes through it.
