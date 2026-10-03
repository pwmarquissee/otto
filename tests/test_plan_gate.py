"""The plan gate, prepare mode, fading, and the stale loop's memory.

Before this, tier-1 was refused outright, so 39 otto-owned tier-1 cards sat
waiting for an approval the owner could not give because there was nothing to
approve. Now a tier-1 card runs first in PREPARE mode, which writes a proposal
into `plan` and applies nothing; approving the plan is what lets it run. The
tests here are the shape of that: prepare is allowed, running is not until the
plan is approved, and the prompt a run gets is the approved plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from otto import backlog, config, dispatch
from otto.models import Run, Task

# Task.owner for work the human does. "otto" is the other value.
HUMAN = "owner"

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def card(tid="a", **kw) -> Task:
    base = {
        "id": tid.ljust(32, "0"),
        "title": f"card {tid}",
        "status": "backlog",
        "detail": "real context an agent could work from",
        "owner": "otto",
        "readiness": "ready",
        "tier": backlog.TIER_APPROVAL,
        "assessed": iso(NOW),
    }
    base.update(kw)
    return Task(**base)


# ---- tier-1 through the gate --------------------------------------------------

def test_tier_1_without_an_approved_plan_is_refused_and_points_at_prepare():
    t = card()
    why = backlog.refusal(t, [t])
    assert why and "--prepare" in why and "approve" in why


def test_tier_1_may_be_prepared():
    t = card()
    assert backlog.refusal(t, [t], prepare=True) is None


def test_tier_1_with_an_approved_plan_may_run():
    t = card(plan="1. do the thing", plan_approved=iso(NOW))
    assert backlog.refusal(t, [t]) is None


def test_preparing_an_approved_card_is_refused_so_the_plan_is_not_lost():
    t = card(plan="1. do the thing", plan_approved=iso(NOW))
    assert "already approved" in backlog.refusal(t, [t], prepare=True)


def test_tier_2_never_dispatches_either_way():
    t = card(tier=backlog.TIER_ASSISTIVE)
    assert backlog.refusal(t, [t]) is not None
    assert backlog.refusal(t, [t], prepare=True) is not None


def test_tier_0_does_not_take_prepare():
    t = card(tier=backlog.TIER_AUTONOMOUS)
    assert backlog.refusal(t, [t]) is None
    assert "--prepare is for" in backlog.refusal(t, [t], prepare=True)


def test_prepare_still_needs_an_otto_owner_and_detail():
    assert "owned by" in backlog.refusal(card(owner=HUMAN), [card(owner=HUMAN)],
                                         prepare=True)
    assert "no detail" in backlog.refusal(card(detail=None), [card(detail=None)],
                                          prepare=True)


def test_an_unknown_tier_is_refused_not_guessed():
    t = card(tier="tier-9-whatever")
    assert "Re-assess" in backlog.refusal(t, [t])


def test_summary_counts_preparable_and_awaiting_approval():
    board = [
        card("p"),                                             # preparable
        card("w", status="needs-you", plan="proposal", plan_approved=None),
        card("z", tier=backlog.TIER_AUTONOMOUS),               # promotable
    ]
    s = backlog.summary(board)
    assert s["preparable"] == 1
    assert s["promotable"] == 1
    assert s["awaiting_plan_approval"] == 1


# ---- the prompt a run receives ------------------------------------------------

def test_prepare_mode_gets_the_prepare_prompt_and_is_told_to_apply_nothing():
    t = card(run_mode="prepare")
    p = dispatch._prompt(t)
    assert "PREPARING" in p and "Apply\nNOTHING" in p
    assert "## Assumptions" in p


def test_an_approved_plan_becomes_the_prompt_body():
    t = card(plan="1. rotate nothing\n2. read the log", plan_approved=iso(NOW), run_mode="run")
    p = dispatch._prompt(t)
    assert "APPROVED PLAN" in p and "2. read the log" in p
    assert "stop at that step and report" in p


def test_a_draft_plan_is_context_not_instruction():
    t = card(plan="maybe do X", plan_approved=None, run_mode="run")
    assert "DRAFT PLAN" in dispatch._prompt(t)


def test_attended_prompt_drops_the_unattended_rules():
    p = dispatch.attended_prompt(card())
    assert "AT THE KEYBOARD" in p
    assert "unattended" not in p.lower()
    assert "dangerously" not in p


# ---- settle: where a finished run puts the card -----------------------------

def _run(task: Task, status="ok", notes="mode=task", result="the proposal text") -> Run:
    return Run(id="r" * 32, name="x", runner="detached", cwd=".", pid=1, status=status,
               task_id=task.id, notes=notes, result_summary=result)


def test_a_prepare_run_lands_in_needs_you_with_the_proposal_as_the_plan(store):
    t = card(status="running", run_mode="prepare")
    store.upsert_task(t)
    dispatch.settle(store, _run(t))
    got = store.get_task(t.id)
    assert got.status == "needs-you"
    assert got.plan == "the proposal text"
    assert got.plan_approved is None
    assert got.run_mode is None


def test_an_attended_run_asks_rather_than_assuming_done(store):
    t = card(status="running")
    store.upsert_task(t)
    dispatch.settle(store, _run(t, notes="mode=task | mode=attended", result="closed it"))
    got = store.get_task(t.id)
    assert got.status == "needs-you"
    assert got.result == "closed it"


def test_an_ordinary_ok_run_is_still_done(store):
    t = card(status="running", tier=backlog.TIER_AUTONOMOUS, run_mode="run")
    store.upsert_task(t)
    dispatch.settle(store, _run(t))
    assert store.get_task(t.id).status == "done"


# ---- fading -------------------------------------------------------------------

def harvested(tid="p", **kw) -> Task:
    """A feed-filed card the owner owns, untouched long enough to fade."""
    base = dict(owner=HUMAN, tier=backlog.TIER_ASSISTIVE, source="agent",
                origin="feed:slack-dm", updated=iso(NOW - timedelta(days=20)))
    base.update(kw)
    return card(tid, **base)


def test_an_untouched_feed_card_of_the_owners_fades(monkeypatch):
    monkeypatch.setattr(config, "FADE_DAYS", 14)
    assert [t.id[:1] for t in backlog.fade_candidates([harvested()], NOW)] == ["p"]


@pytest.mark.parametrize("kw", [
    dict(owner="otto"),
    dict(source="manual"),
    dict(due="2026-09-30"),
    dict(priority="urgent"),
    dict(priority="high"),
    dict(readiness="needs-decision"),
    dict(tags=["m5"]),
    dict(tags=["punchlist"]),
    dict(updated=iso(NOW - timedelta(days=3))),
    dict(status="needs-you"),
])
def test_what_never_fades(monkeypatch, kw):
    monkeypatch.setattr(config, "FADE_DAYS", 14)
    assert backlog.fade_candidates([harvested(**kw)], NOW) == []


def test_an_unassessed_feed_card_fades_too(monkeypatch):
    """No owner yet is still the owner's harvest, not Otto's work."""
    monkeypatch.setattr(config, "FADE_DAYS", 14)
    t = harvested(owner=None, tier=None, readiness=None, assessed=None)
    assert backlog.fade_candidates([t], NOW) == [t]


# ---- the stale loop remembers -------------------------------------------------

def test_stale_age_counts_from_the_last_check(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 10)
    t = card(created=iso(NOW - timedelta(days=40)), last_checked=iso(NOW - timedelta(days=1)))
    assert backlog.stale_candidates([t], NOW) == []


def test_a_future_next_look_keeps_a_card_out_of_the_stale_list(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 10)
    t = card(created=iso(NOW - timedelta(days=40)), next_look="2026-10-01")
    assert backlog.stale_candidates([t], NOW) == []
    t2 = card("b", created=iso(NOW - timedelta(days=40)), next_look="2026-09-01")
    assert backlog.stale_candidates([t2], NOW) == [t2]


def test_needs_assessment_now_sees_needs_you_and_blocked():
    board = [card("n", status="needs-you", assessed=None, owner=None),
             card("b", status="blocked", assessed=None, owner=None),
             card("d", status="done", assessed=None, owner=None)]
    assert sorted(t.id[:1] for t in backlog.needs_assessment(board)) == ["b", "n"]
