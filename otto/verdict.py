"""Two-lane verdicts on a run: did OTTO work, and did THE WORK work.

THE PROBLEM THIS SEPARATES. `RunStatus` is one axis: running, ok, failed, orphaned,
killed. Three very different facts all land on the red end of it:

  * `orphaned`        Otto lost track of the process. Says nothing about the task.
  * `failed` + api    Upstream capacity died under the session. Says nothing about
                      the task, and retrying is the correct response.
  * `failed`, exit 1  The session ran to completion and the task itself did not
                      succeed. This one IS about the work.

Otto has already paid for confusing them. On 2026-08-05 a burst of 529s produced
"heartbeat failed (exit 1)" alerts that read as "your automation is broken", and
three of them were actioned into dispatched agents in one hour. Every agent
concluded the API had been overloaded. About $3.20 to re-derive a fact the run
record already held, and `compute_alerts` grew a special case for `run.transient`
to stop it happening again. That special case is the right instinct applied to one
branch. This module applies it to all of them, and gives the answer a fixed shape
so the dashboard, the alerts, and the CLI cannot disagree about which side broke.

The shape is borrowed from a game studio's test-harness dashboard, where
"the harness is wrong" must never look like "the game is wrong" because the
naive reading of two red pills cost weeks. Here the two lanes are:

  HARNESS   Otto's side. Did it spawn, run, and report? States: running, ok,
            orphaned, api, budget, killed.
  WORK      The task's side. Did the thing the run was for succeed? States:
            running, done, failed, not-evaluated, skipped, due.

`not-evaluated` is the load-bearing state. When the harness lane is anything but
ok, the work lane is not-evaluated, never failed: a run Otto lost, or that the API
killed, or that the spend cap cut, has said NOTHING about whether the work would
have succeeded. Rendering that as a failure is the exact mistake this exists to
stop.

`plan()` is the other half of the same design idea: a timeline that renders the
plan alongside the result. `inspectRun` used to show only the events a run had
already emitted, which is history, and history cannot tell you whether a
40-minute run is deep work or dead. Four coarse phases (spawned, working,
reporting, settled) with the queued ones drawn from what SHOULD happen, plus the
budget bar and the expected duration from past runs of the same name, answer that
without a model call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from . import config
from .models import Run, utcnow
from .store import Store


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def _lane(state: str, label: str, detail: str | None = None) -> dict[str, Any]:
    return {"state": state, "label": label, "detail": detail}


def _notes_tail(run: Run, limit: int = 80) -> str | None:
    if not run.notes:
        return None
    # The last pipe-separated fragment is the newest thing appended, which is
    # where detached.poll writes "exit N" or the API error status.
    tail = run.notes.split("|")[-1].strip()
    return tail[:limit] or None


def verdict(run: Run) -> dict[str, Any]:
    """Which side broke, in a fixed shape every consumer renders the same way."""
    st = run.status
    summary = (run.result_summary or "").strip()[:120] or None

    if st == "running":
        harness = _lane("running", "running")
        work = _lane("running", "running", "no verdict yet")
        word, imperative = "RUNNING", "watch"
    elif st == "ok":
        harness = _lane("ok", "ok")
        work = _lane("done", "done", summary)
        word, imperative = "DONE", "nothing to do"
    elif st == "failed" and run.error_kind == "api":
        harness = _lane("api", "api error", "upstream API error")
        work = _lane("not-evaluated", "not evaluated", "never got a fair chance")
        word, imperative = "API ERROR", "retry on cadence, nothing is broken"
    elif st == "failed" and run.error_kind == "budget":
        harness = _lane("budget", "budget cut", "stopped by the spend cap")
        work = _lane("not-evaluated", "not evaluated",
                     "the work was cut short, not judged")
        word, imperative = "BUDGET CUT", "raise the cap or use a cheaper model"
    elif st == "failed":
        # Otto did its whole job here: it spawned the session, the session ran, and
        # it reported. What it reported was that the work did not succeed.
        harness = _lane("ok", "ok")
        bits = []
        if run.exit_code is not None:
            bits.append(f"exit {run.exit_code}")
        tail = _notes_tail(run)
        if tail and tail not in bits:
            bits.append(tail)
        work = _lane("failed", "failed", " · ".join(bits) or "the run reported failure")
        word, imperative = "WORK FAILED", "read the log"
    elif st == "orphaned":
        harness = _lane("orphaned", "vanished", "vanished with no result object")
        work = _lane("not-evaluated", "not evaluated", "outcome unknown, not failed")
        word, imperative = "OTTO LOST IT", "outcome unknown, check the log"
    elif st == "killed":
        harness = _lane("killed", "killed", "terminated before it could report")
        work = _lane("not-evaluated", "not evaluated", "stopped on purpose")
        word, imperative = "KILLED", "relaunch if it still matters"
    elif st == "skipped":
        harness = _lane("ok", "ok")
        work = _lane("skipped", "skipped", _notes_tail(run))
        word, imperative = "SKIPPED", "nothing to do"
    else:  # due
        harness = _lane("ok", "ok")
        work = _lane("due", "due", "cadence says it should run")
        word, imperative = "DUE", "run it, or wait for the cadence"

    def _fmt(lane: dict[str, Any]) -> str:
        d = lane["detail"]
        # Only the work lane carries its detail into the one-liner, and only the
        # short kind: a 120-char result summary is not a line.
        return lane["label"] + (f" ({d})" if d and len(d) <= 40 else "")

    line = f"Otto: {harness['label']} · Work: {_fmt(work)}"
    return {
        "harness": harness,
        "work": work,
        "word": word,
        "imperative": imperative,
        "line": line,
    }


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

_HARNESS_BROKE = {"orphaned", "api", "budget", "killed"}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _duration(run: Run, now: datetime) -> float | None:
    start = _parse(run.started)
    if start is None:
        return None
    end = _parse(run.ended) if run.ended else now
    if end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def expected_seconds(run: Run, store: Store, sample: int = 5) -> float | None:
    """Median wall time of the last `sample` ok runs with this name, or None.

    Two is the floor. A single prior run is a data point, not an expectation, and
    an "expected 4m" drawn from one lucky run would set the budget bar up to lie.
    """
    prior = [
        r for r in store.runs()
        if r.name == run.name and r.id != run.id and r.status == "ok" and r.ended
    ][:sample]
    now = utcnow()
    durations = [d for d in (_duration(r, now) for r in prior) if d is not None]
    if len(durations) < 2:
        return None
    return float(median(durations))


def budget_for(run: Run) -> float | None:
    """The per-run dollar cap that applied, from the same constants the spawner used.

    Read from config rather than stored on the run, so this reports the cap as
    configured today. That is the honest choice for a bar drawn against a live
    number, and the alternative (a new field on Run) would leave every historical
    run without one anyway.
    """
    notes = run.notes or ""
    if "mode=task" in notes:
        return config.TASK_MAX_BUDGET_USD or None
    if "mode=ingest" in notes:
        return config.MEETINGS_BUDGET_USD or None
    if "thread-note" in notes or "thread_note" in notes:
        return config.THREAD_NOTE_BUDGET_USD or None
    if "mode=schedule" in notes and "autorun" in notes:
        return config.SCHEDULE_BUDGET_USD or None
    return None


def plan(run: Run, store: Store) -> dict[str, Any]:
    """The four phases every run passes through, with the queued ones drawn in."""
    v = verdict(run)
    hs = v["harness"]["state"]
    running = run.status == "running"
    broke = hs in _HARNESS_BROKE
    now = utcnow()

    # reporting: a result was applied. ok and plain failed both came from a result
    # object (or a shell exit code, which is the shell's result). orphaned never
    # reported. api/budget did report, in the sense that Claude Code wrote the
    # result object that said so, but the report is about the harness, so the phase
    # reads failed rather than done.
    if running:
        reporting = "queued"
    elif hs == "orphaned":
        reporting = "failed"
    elif broke:
        reporting = "failed"
    else:
        reporting = "done"

    notes = run.notes or ""
    settled = reporting
    settled_note: str | None = None
    if "mode=schedule" in notes:
        sched = store.get_schedule(run.name)
        if sched is not None and sched.last_run_id == run.id:
            settled, settled_note = "done", (
                "stamped" if sched.last_status == "ok" else f"recorded {sched.last_status}")
        elif running:
            settled = "queued"
        elif reporting == "done":
            settled, settled_note = "queued", "not yet stamped"
    elif "mode=task" in notes and run.task_id:
        task = store.get_task(run.task_id)
        if task is not None and task.status in ("done", "needs-you"):
            settled, settled_note = "done", f"card -> {task.status}"
        elif running:
            settled = "queued"
        elif reporting == "done":
            settled, settled_note = "queued", "card not yet moved"

    working = "current" if running else ("failed" if broke else "done")

    phases = [
        {"name": "spawned", "state": "done", "at": run.started,
         "note": f"pid {run.pid}" if run.pid else None},
        {"name": "working", "state": working, "at": None,
         "note": v["harness"]["detail"] if broke else None},
        {"name": "reporting", "state": reporting, "at": run.ended,
         "note": None if reporting != "failed" else v["word"].lower()},
        {"name": "settled", "state": settled, "at": None, "note": settled_note},
    ]

    usd = budget_for(run)
    spent = run.cost_usd
    pct = None
    if usd and spent is not None:
        pct = min(100, int(round(100 * spent / usd)))

    return {
        "phases": phases,
        "elapsed_seconds": _duration(run, now),
        "expected_seconds": expected_seconds(run, store),
        "budget": {"usd": usd, "spent": spent, "pct": pct},
    }
