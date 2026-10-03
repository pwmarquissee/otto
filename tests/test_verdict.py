"""verdict.py: which side broke, and the plan a run is measured against.

The shape under test is the fix for a real misread. On 2026-08-05 a burst of 529s
produced "heartbeat failed (exit 1)" alerts, three of which were actioned into
dispatched agents that each concluded the API had been overloaded. Every branch
below pins the lane assignment that would have prevented that: a harness failure
leaves the work lane at not-evaluated, never failed.
"""

from __future__ import annotations

from datetime import timedelta

from otto import verdict
from otto.models import Run, Schedule, iso, utcnow


def _run(**kw) -> Run:
    kw.setdefault("id", "r" * 32)
    kw.setdefault("name", "heartbeat")
    kw.setdefault("runner", "detached")
    return Run(**kw)


# ---- verdict: one test per status branch ------------------------------------

def test_running_is_running_on_both_lanes():
    v = verdict.verdict(_run(status="running"))
    assert v["harness"]["state"] == "running"
    assert v["work"]["state"] == "running"
    assert v["word"] == "RUNNING"


def test_ok_is_done_with_summary():
    v = verdict.verdict(_run(status="ok", result_summary="all sensors enrolled"))
    assert v["harness"]["state"] == "ok"
    assert v["work"]["state"] == "done"
    assert v["work"]["detail"] == "all sensors enrolled"
    assert v["word"] == "DONE"
    assert v["line"] == "Otto: ok · Work: done (all sensors enrolled)"


def test_api_error_is_harness_not_work():
    """The 529 case. The work lane must NOT say failed."""
    v = verdict.verdict(_run(status="failed", exit_code=1, error_kind="api",
                             notes="mode=schedule | 529"))
    assert v["harness"]["state"] == "api"
    assert v["work"]["state"] == "not-evaluated"
    assert v["word"] == "API ERROR"
    assert "nothing is broken" in v["imperative"]
    assert v["line"].startswith("Otto: api error · Work: not evaluated")


def test_budget_cut_is_harness_not_work():
    v = verdict.verdict(_run(status="failed", exit_code=1, error_kind="budget"))
    assert v["harness"]["state"] == "budget"
    assert v["work"]["state"] == "not-evaluated"
    assert v["word"] == "BUDGET CUT"


def test_plain_failure_is_work_with_harness_ok():
    """Otto did its job: spawned, ran, reported. The report said the work failed."""
    v = verdict.verdict(_run(status="failed", exit_code=1, notes="mode=task | exit 1"))
    assert v["harness"]["state"] == "ok"
    assert v["work"]["state"] == "failed"
    assert "exit 1" in v["work"]["detail"]
    assert v["word"] == "WORK FAILED"
    assert v["line"] == "Otto: ok · Work: failed (exit 1)"


def test_orphaned_is_vanished_and_not_evaluated():
    v = verdict.verdict(_run(status="orphaned"))
    assert v["harness"]["state"] == "orphaned"
    assert v["harness"]["label"] == "vanished"
    assert v["work"]["state"] == "not-evaluated"
    assert v["word"] == "OTTO LOST IT"
    assert v["line"] == "Otto: vanished · Work: not evaluated (outcome unknown, not failed)"


def test_killed_skipped_due():
    assert verdict.verdict(_run(status="killed"))["harness"]["state"] == "killed"
    assert verdict.verdict(_run(status="killed"))["work"]["state"] == "not-evaluated"
    sk = verdict.verdict(_run(status="skipped"))
    assert (sk["harness"]["state"], sk["work"]["state"]) == ("ok", "skipped")
    du = verdict.verdict(_run(status="due"))
    assert (du["harness"]["state"], du["work"]["state"]) == ("ok", "due")


def test_every_state_is_in_the_documented_set():
    harness_ok = {"running", "ok", "orphaned", "api", "budget", "killed"}
    work_ok = {"running", "done", "failed", "not-evaluated", "skipped", "due"}
    cases = [
        dict(status="running"), dict(status="ok"),
        dict(status="failed", error_kind="api"), dict(status="failed", error_kind="budget"),
        dict(status="failed"), dict(status="orphaned"), dict(status="killed"),
        dict(status="skipped"), dict(status="due"),
    ]
    for kw in cases:
        v = verdict.verdict(_run(**kw))
        assert v["harness"]["state"] in harness_ok, kw
        assert v["work"]["state"] in work_ok, kw
        assert set(v) == {"harness", "work", "word", "imperative", "line"}


# ---- plan --------------------------------------------------------------------

def _names(p):
    return [ph["name"] for ph in p["phases"]]


def _states(p):
    return {ph["name"]: ph["state"] for ph in p["phases"]}


def test_plan_running_run_is_working_with_the_rest_queued(store):
    r = _run(status="running", pid=4242)
    store.upsert_run(r)
    p = verdict.plan(r, store)
    assert _names(p) == ["spawned", "working", "reporting", "settled"]
    assert _states(p) == {"spawned": "done", "working": "current",
                          "reporting": "queued", "settled": "queued"}
    assert p["elapsed_seconds"] is not None and p["elapsed_seconds"] >= 0
    assert p["phases"][0]["note"] == "pid 4242"


def test_plan_stamped_schedule_run_is_settled(store):
    store.upsert_schedule(Schedule(name="heartbeat", command="/heartbeat"))
    start = utcnow() - timedelta(minutes=3)
    r = _run(status="ok", notes="mode=schedule", started=iso(start), ended=iso(utcnow()))
    store.upsert_run(r)
    store.stamp("heartbeat", "ok", r.id)
    p = verdict.plan(r, store)
    assert _states(p) == {"spawned": "done", "working": "done",
                          "reporting": "done", "settled": "done"}
    assert p["phases"][3]["note"] == "stamped"
    assert 170 <= p["elapsed_seconds"] <= 190


def test_plan_unstamped_schedule_run_says_so(store):
    store.upsert_schedule(Schedule(name="heartbeat", command="/heartbeat"))
    r = _run(status="ok", notes="mode=schedule", ended=iso(utcnow()))
    store.upsert_run(r)
    p = verdict.plan(r, store)
    assert _states(p)["settled"] == "queued"
    assert p["phases"][3]["note"] == "not yet stamped"


def test_plan_orphaned_run_failed_at_working_and_reporting(store):
    r = _run(status="orphaned", ended=iso(utcnow()))
    store.upsert_run(r)
    p = verdict.plan(r, store)
    assert _states(p) == {"spawned": "done", "working": "failed",
                          "reporting": "failed", "settled": "failed"}
    assert p["phases"][1]["note"] == "vanished with no result object"


def test_plan_expected_seconds_is_the_median_of_prior_ok_runs(store):
    now = utcnow()
    runs = []
    for i, secs in enumerate((60, 120, 600)):
        start = now - timedelta(hours=i + 1)
        runs.append(_run(id=f"{i:032x}", status="ok",
                         started=iso(start), ended=iso(start + timedelta(seconds=secs))))
    current = _run(id="c" * 32, status="running")
    # Newest first, as the store keeps them. One unrelated run must be ignored.
    other = _run(id="d" * 32, name="scout", status="ok",
                 started=iso(now - timedelta(hours=5)),
                 ended=iso(now - timedelta(hours=4)))
    store.save_runs([current, other] + runs)
    p = verdict.plan(current, store)
    assert p["expected_seconds"] == 120.0


def test_plan_expected_seconds_needs_two_samples(store):
    now = utcnow()
    prior = _run(id="a" * 32, status="ok", started=iso(now - timedelta(minutes=5)),
                 ended=iso(now - timedelta(minutes=4)))
    current = _run(id="c" * 32, status="running")
    store.save_runs([current, prior])
    assert verdict.plan(current, store)["expected_seconds"] is None


def test_plan_budget_for_task_run(store, monkeypatch):
    from otto import config
    monkeypatch.setattr(config, "TASK_MAX_BUDGET_USD", 5.0)
    r = _run(status="ok", notes="mode=task", task_id=None, cost_usd=1.25, ended=iso(utcnow()))
    store.upsert_run(r)
    b = verdict.plan(r, store)["budget"]
    assert b == {"usd": 5.0, "spent": 1.25, "pct": 25}


def test_plan_budget_pct_is_capped_at_100(store, monkeypatch):
    from otto import config
    monkeypatch.setattr(config, "TASK_MAX_BUDGET_USD", 1.0)
    r = _run(status="failed", error_kind="budget", notes="mode=task", cost_usd=1.4,
             ended=iso(utcnow()))
    store.upsert_run(r)
    assert verdict.plan(r, store)["budget"]["pct"] == 100


def test_plan_budget_unknown_for_a_plain_spawn(store):
    r = _run(status="ok", notes="mode=headless", ended=iso(utcnow()))
    store.upsert_run(r)
    assert verdict.plan(r, store)["budget"] == {"usd": None, "spent": None, "pct": None}
