"""Conformance harness for how Otto handles a failed run.

    python scripts\\failures_conformance.py

WHY THIS EXISTS. On 2026-08-05 a burst of Anthropic 529 Overloaded errors killed
nine scheduled runs. Otto recorded "529" in each run's `notes` and then treated them
exactly like a broken command: nine high-priority "needs you" cards titled
"<name> failed", and `warn` alerts saying "failed (exit 1)". Three got actioned from
the dashboard, each dispatching an agent, and all three reported back the same
finding: upstream capacity, nothing broken. About $3.20 to re-derive a fact already
in the run record. The autorun breaker counted them too, so only interleaved
successes kept it from disarming heartbeat and slack-sweep.

Every assertion below is one of the properties that were missing that day. The two
that matter most:

NOT COUNTED, NOT FORGIVEN. A transient failure must not increment the breaker (the
schedule is not broken) and must ALSO not reset the staleness clock (the work still
did not happen). Getting one of those right and the other wrong is worse than
getting both wrong, because it either disarms a working schedule or makes a dead one
read as healthy.

A PAST FAILURE MUST BE DISMISSIBLE. Derived cards are not draggable by design: the
rule is "fix the condition and the card clears itself". A failed run is history and
its condition never clears, so that rule left the card permanent. `reviewed_at` is
the verb the board was missing.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-failures-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
os.environ["OTTO_FEED_DIR"] = str(TMP / "feed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import Cadence, Run, Schedule, iso, utcnow  # noqa: E402

config.mark_daemon()
config.ensure_dirs()

from otto import board, launch  # noqa: E402
from otto.runners import detached  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def run(**kw) -> Run:
    base = dict(id=kw.pop("id", "a" * 32), name=kw.pop("name", "heartbeat"),
                runner="detached", notes="mode=headless | mode=schedule | autorun")
    return Run(**{**base, **kw})


# The real shape, copied from the 2026-08-05 log for run f003e4.
OVERLOADED = {
    "is_error": True, "subtype": "success", "terminal_reason": "api_error",
    "api_error_status": 529, "total_cost_usd": 0.001709, "num_turns": 1,
    "result": "API Error: 529 Overloaded. This is a server-side issue, usually "
              "temporary - try again in a moment.",
}

# ---------------------------------------------------------------------------
print("-- classifying the failure --------------------------------------------")

r = run()
detached._apply_result(r, dict(OVERLOADED))
check("a 529 is still a failure, not quietly an ok", r.status == "failed")
check("...classified as an upstream api error", r.error_kind == "api")
check("...and therefore transient", r.transient is True)
check("...with the status code kept in notes", "529" in (r.notes or ""))
check("...and the message preserved for a human",
      "Overloaded" in (r.result_summary or ""))

r2 = run()
detached._apply_result(r2, {"is_error": True, "subtype": "error_during_execution",
                            "result": "the command exited 2"})
check("a non-API failure is NOT classified as api", r2.error_kind is None)
check("...and is NOT transient", r2.transient is False)

r3 = run()
detached._apply_result(r3, {"is_error": False, "subtype": "success",
                            "result": "done", "total_cost_usd": 0.4})
check("a success is ok", r3.status == "ok")
check("...and carries no error kind", r3.error_kind is None)

r4 = run(error_kind="api", status="failed")
detached._apply_result(r4, {"is_error": False, "subtype": "success", "result": "done"})
check("a retry that succeeds clears the stale verdict", r4.error_kind is None)

r5 = run()
detached._apply_result(r5, {"is_error": True, "subtype": "success",
                            "api_error_status": 429})
check("a 429 is also transient (rate limited, not broken)", r5.transient is True)

check("an ok run is never transient, whatever the kind field says",
      not Run(id="b" * 32, name="x", runner="detached",
              status="ok", error_kind="api").transient)

# ---------------------------------------------------------------------------
print("\n-- the autorun breaker: not counted, not forgiven ----------------------")

sched = Schedule(name="heartbeat", command="/heartbeat", autostart=True,
                 runner="launch", cadence=Cadence(kind="every", hours=6),
                 max_age_hours=26, last_run=iso(utcnow()))
store.upsert_schedule(sched)

for i in range(config.SCHEDULE_MAX_FAILURES + 2):
    launch._record_failure(store, "heartbeat", transient=True)
after = store.get_schedule("heartbeat")
check("a transient failure never increments the breaker",
      after.consecutive_failures == 0, f"got {after.consecutive_failures}")
check(f"...so {config.SCHEDULE_MAX_FAILURES + 2} of them cannot disarm autorun",
      after.autostart is True)
check("...and leave no disabled_reason", after.disabled_reason is None)

tripped = None
for i in range(config.SCHEDULE_MAX_FAILURES):
    tripped = launch._record_failure(store, "heartbeat") or tripped
after = store.get_schedule("heartbeat")
check("a real failure DOES increment the breaker",
      after.consecutive_failures == config.SCHEDULE_MAX_FAILURES)
check("...and trips it at the limit", tripped == config.SCHEDULE_MAX_FAILURES)
check("...disarming autorun", after.autostart is False)
check("...but leaving the schedule enabled, so silence is still reported",
      after.enabled is True)
check("...with a reason a human can read", "consecutive failures" in
      (after.disabled_reason or ""))

# The other half. A transient failure must not look like success either.
store.upsert_schedule(Schedule(
    name="sweep", command="/slack-sweep", autostart=True, runner="launch",
    cadence=Cadence(kind="every", hours=1), max_age_hours=3,
    last_run="2026-08-01T00:00:00Z"))
before_stamp = store.get_schedule("sweep").last_run
fr = run(name="sweep", status="failed", error_kind="api", ended=iso(utcnow()))
launch.settle(store, fr)
after = store.get_schedule("sweep")
check("a transient failure does NOT reset the staleness clock",
      after.last_run == before_stamp,
      "the work did not happen; a dead schedule must not read as fresh")
check("...and is recorded as an attempt, not a success",
      after.last_status == "failed")
check("...still without touching the breaker", after.consecutive_failures == 0)

# ---------------------------------------------------------------------------
print("\n-- the board: what gets a card ----------------------------------------")

def cards_for(*runs):
    store.save_runs(list(runs))
    return [c for c in board.derived_cards(store) if c.kind == "run"]


genuine = run(id="c" * 32, name="daily", status="failed", exit_code=1,
              ended=iso(utcnow()))
transient = run(id="d" * 32, name="heartbeat", status="failed", exit_code=1,
                error_kind="api", ended=iso(utcnow()))

cs = cards_for(genuine)
check("a genuine failure gets a needs-you card", len(cs) == 1 and cs[0].status == "needs-you")
check("...at high priority", cs[0].priority == "high")
check("...and tells you how to dismiss it", "otto ack" in (cs[0].command or ""),
      f"got {cs[0].command!r}")

cs = cards_for(transient)
check("an upstream API failure gets NO card at all", cs == [],
      "it is weather; the schedule's own staleness alarm is the true signal")

cs = cards_for(genuine, transient)
check("...even when a real failure is on the board beside it",
      [c.title for c in cs] == ["daily failed"], f"got {[c.title for c in cs]}")

reviewed = run(id="e" * 32, name="daily", status="failed", exit_code=1,
               ended=iso(utcnow()), reviewed_at=iso(utcnow()))
check("an acknowledged failure gets no card", cards_for(reviewed) == [])
check("...and acknowledging does not delete the run",
      len(store.runs()) == 1 and store.runs()[0].status == "failed")

orphan = run(id="f" * 32, name="daily", status="orphaned", ended=iso(utcnow()))
cs = cards_for(orphan)
check("an orphaned run still gets a card (outcome genuinely unknown)",
      len(cs) == 1 and "vanished" in cs[0].title)
check("...and is also acknowledgeable",
      cards_for(run(id="0" * 32, name="daily", status="orphaned",
                    ended=iso(utcnow()), reviewed_at=iso(utcnow()))) == [])

check("no run card is draggable (there is still no stored row behind it)",
      all(not c.movable for c in cards_for(genuine, orphan)))

# ---------------------------------------------------------------------------
print("\n-- reclassifying history ----------------------------------------------")

src = Path(board.__file__).read_text(encoding="utf-8")
check("board.py explains why a run card is the exception to the derived rule",
      "never clears" in src)

import otto.daemon as daemon  # noqa: E402
from otto.api import runs as api_runs  # noqa: E402
# The reclassify endpoint moved to otto/api/runs.py; the alert text stayed in the tick.
dsrc = (Path(daemon.__file__).read_text(encoding="utf-8")
        + Path(api_runs.__file__).read_text(encoding="utf-8"))
check("the reclassify endpoint only fills an EMPTY error_kind",
      "or r.error_kind" in dsrc or "r.error_kind:" in dsrc,
      "re-running it must never overwrite a classification")
check("a transient alert is info, not warn",
      'level="info", domain=run.domain' in dsrc)
check("...and says the retry is automatic rather than asking for attention",
      "retried automatically" in dsrc)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
