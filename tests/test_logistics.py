"""Logistics dispatch: who may go, where they may go, how they are paired, what
approve does, and how a pane settles. herdr is faked at the module boundary so
nothing here needs a server, a binary, or a terminal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from otto import backlog, config, dispatch, herdr, logistics
from otto.models import Session, Task, iso, utcnow

# Task.owner for work the human does. "otto" is the other value.
HUMAN = "owner"

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)


def agent(pane, status="idle", cwd="D:/otto", sid="sess-otto", name=None, seq=1):
    a = {"agent": "claude", "pane_id": pane, "agent_status": status, "cwd": cwd,
         "state_change_seq": seq, "workspace_id": pane.split(":")[0],
         "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude",
                           "value": sid} if sid else None}
    if name:
        a["name"] = name
    return a


def runnable(title, **kw):
    kw.setdefault("owner", "otto")
    kw.setdefault("tier", backlog.TIER_AUTONOMOUS)
    kw.setdefault("readiness", "ready")
    kw.setdefault("assessed", iso(utcnow()))
    kw.setdefault("detail", "enough detail to run")
    kw.setdefault("status", "backlog")
    return Task(id=kw.pop("id", None) or ("t" + str(abs(hash(title)))).ljust(32, "0")[:32],
                title=title, **kw)


@pytest.fixture
def fake_herdr(monkeypatch):
    """A herdr with a fixed snapshot and a recorder for prompts."""
    state = {"snap": {"agents": [], "panes": [], "workspaces": []}, "prompts": [],
             "reads": "the agent wrote this\nand finished", "focus": []}
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "binary", lambda: "herdr")
    monkeypatch.setattr(herdr, "snapshot", lambda: state["snap"])
    monkeypatch.setattr(herdr, "last_snapshot", lambda max_age=30.0: state["snap"])
    monkeypatch.setattr(herdr, "prompt", lambda target, text: state["prompts"].append((target, text)) or {})
    monkeypatch.setattr(herdr, "read", lambda target, lines=120: state["reads"])
    monkeypatch.setattr(herdr, "focus", lambda target: state["focus"].append(target))
    return state


# ---- who and where -----------------------------------------------------------------

def test_targets_are_idle_claude_panes_without_a_running_card(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [
        agent("w1:p1", "idle", sid="a"),
        agent("w2:p1", "working", sid="b"),
        agent("w3:p1", "blocked", sid="c"),
        agent("w4:p1", "done", sid="d"),
        agent("w5:p1", "idle", sid="e"),
        {**agent("w6:p1", "idle", sid="f"), "agent": "codex"},
    ]
    store.save_tasks([runnable("already there", status="running", pane_id="w5:p1")])
    panes = {t["pane_id"] for t in logistics.targets(store, fake_herdr["snap"])}
    assert panes == {"w1:p1", "w4:p1"}


def test_candidates_are_exactly_what_the_gate_would_promote(store):
    ok = runnable("may run")
    theirs = runnable("the owner's", owner=HUMAN)
    unassessed = Task(id="u" * 32, title="not assessed")
    store.save_tasks([ok, theirs, unassessed])
    assert [t.id for t in logistics.candidates(store)] == [ok.id]


# ---- pairing -------------------------------------------------------------------------

def test_a_card_that_runs_in_the_panes_repo_scores_highest():
    pane = {"pane_id": "w1:p1", "cwd": "D:/work/example/shipyard", "status": "idle"}
    same = runnable("fix the shader", cwd="D:/work/example/shipyard/Source")
    tagged = runnable("fix the shader", tags=["repo:shipyard"])
    named = runnable("shipyard: fix the shader")
    other = runnable("otto: dedupe the board")
    neutral = runnable("rotate the vendor token")
    scores = {k: logistics.score(t, pane)[0] for k, t in
              [("same", same), ("tagged", tagged), ("named", named), ("other", other),
               ("neutral", neutral)]}
    assert scores["same"] > scores["tagged"] > scores["named"] > scores["neutral"] > scores["other"]
    assert "shipyard" in logistics.score(same, pane)[1]
    assert scores["other"] < config.LOGISTICS_MIN_CONFIDENCE


def test_priority_and_tier_nudge_but_do_not_decide():
    pane = {"pane_id": "w1:p1", "cwd": "D:/otto", "status": "idle"}
    base = runnable("rotate the vendor token")
    hot = runnable("rotate the vendor token", priority="urgent")
    approved = runnable("rotate the vendor token", tier=backlog.TIER_APPROVAL,
                        plan="do it", plan_approved=iso(utcnow()))
    assert logistics.score(hot, pane)[0] > logistics.score(base, pane)[0]
    assert logistics.score(base, pane)[0] > logistics.score(approved, pane)[0]


def test_refresh_pairs_each_card_and_pane_once_best_first(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [
        agent("w1:p1", cwd="D:/otto", sid="s-otto", name="otto"),
        agent("w2:p1", cwd="D:/work/example/shipyard", sid="s-ship", name="shipyard"),
    ]
    a = runnable("otto: fix the board", id="a" * 32, cwd="D:/otto")
    b = runnable("shipyard: shader crash", id="b" * 32, cwd="D:/work/example/shipyard")
    c = runnable("otto: second thing", id="c" * 32, cwd="D:/otto")
    store.save_tasks([a, b, c])
    props = logistics.refresh(store, fake_herdr["snap"])
    pairs = {(p.task_id, p.agent) for p in props}
    assert pairs == {(a.id, "otto"), (b.id, "shipyard")}      # c lost the otto pane
    assert all(p.confidence >= config.LOGISTICS_MIN_CONFIDENCE for p in props)
    # Stable ids across refreshes, so an approve by id is unambiguous.
    again = logistics.refresh(store, fake_herdr["snap"])
    assert {p.id for p in again} == {p.id for p in props}


def test_a_dismissed_pair_stays_dismissed_while_the_session_lives(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1", name="otto")]
    store.save_sessions([Session(session_id="s1", state="idle")])
    t = runnable("otto: fix the board", id="a" * 32, cwd="D:/otto")
    store.save_tasks([t])
    [p] = logistics.refresh(store, fake_herdr["snap"])
    logistics.dismiss(store, p.id)
    assert logistics.refresh(store, fake_herdr["snap"]) == []
    # The same pane holding a NEW conversation is a fresh question.
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s2", name="otto")]
    assert len(logistics.refresh(store, fake_herdr["snap"])) == 1


def test_a_proposal_goes_stale_when_its_card_or_pane_no_longer_qualifies(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1")]
    t = runnable("otto: fix the board", id="a" * 32, cwd="D:/otto")
    store.save_tasks([t])
    [p] = logistics.refresh(store, fake_herdr["snap"])
    fake_herdr["snap"]["agents"] = [agent("w1:p1", "working", cwd="D:/otto", sid="s1")]
    assert logistics.refresh(store, fake_herdr["snap"]) == []
    assert store.get_proposal(p.id).status == "stale"


# ---- approve ---------------------------------------------------------------------------

def test_approve_prompts_the_pane_and_marks_the_card_running_there(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1", name="otto", seq=7)]
    t = runnable("otto: fix the board", id="a" * 32, cwd="D:/otto")
    store.save_tasks([t])
    [p] = logistics.refresh(store, fake_herdr["snap"])
    _p, msg = logistics.approve(store, p.id)
    assert "handed" in msg
    [(target, text)] = fake_herdr["prompts"]
    assert target == "otto"
    assert "otto: fix the board" in text
    assert f"python -m otto task mv {t.id[:8]} done" in text
    fresh = store.get_task(t.id)
    assert (fresh.status, fresh.pane_id, fresh.session_id, fresh.pane_seq, fresh.run_id) == \
        ("running", "w1:p1", "s1", 7, None)
    assert fresh.attempts == 1
    assert store.get_proposal(p.id).status == "approved"


def test_approve_rechecks_both_gates(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1", name="otto")]
    t = runnable("otto: fix the board", id="a" * 32, cwd="D:/otto")
    store.save_tasks([t])
    [p] = logistics.refresh(store, fake_herdr["snap"])
    # The pane started working in between.
    fake_herdr["snap"]["agents"] = [agent("w1:p1", "working", cwd="D:/otto", sid="s1", name="otto")]
    with pytest.raises(ValueError, match="not idle"):
        logistics.approve(store, p.id)
    assert fake_herdr["prompts"] == []
    # Or the card was moved by hand.
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1", name="otto")]
    t2 = store.get_task(t.id); t2.status = "needs-you"; store.upsert_task(t2)
    with pytest.raises(ValueError, match="backlog"):
        logistics.approve(store, p.id)


def test_dispatch_to_bypasses_the_strip_but_not_the_gate(store, fake_herdr):
    fake_herdr["snap"]["agents"] = [agent("w1:p1", cwd="D:/otto", sid="s1", name="otto")]
    ok = runnable("fine", id="a" * 32)
    theirs = runnable("the owner's", id="b" * 32, owner=HUMAN)
    store.save_tasks([ok, theirs])
    assert "handed" in logistics.dispatch_to(store, "a" * 8, "otto")
    with pytest.raises(ValueError, match="owned by"):
        logistics.dispatch_to(store, "b" * 8, "w1:p1")
    with pytest.raises(KeyError):
        logistics.dispatch_to(store, "a" * 8, "nobody")


# ---- the watcher ---------------------------------------------------------------------------

def test_a_settled_pane_parks_the_card_with_what_it_wrote(store, fake_herdr):
    t = runnable("otto: fix the board", id="a" * 32, status="running", pane_id="w1:p1",
                 pane_seq=3, updated=iso(NOW - timedelta(minutes=5)))
    store.save_tasks([t])
    fake_herdr["snap"]["agents"] = [agent("w1:p1", "done", sid="s1", name="otto", seq=5)]
    notes = logistics.watch(store, fake_herdr["snap"])
    fresh = store.get_task(t.id)
    assert fresh.status == "needs-you"
    assert fresh.result == "the agent wrote this\nand finished"
    assert notes and "settled" in notes[0]


def test_the_watcher_waits_out_the_settle_window_and_a_stale_seq(store, fake_herdr):
    just_now = runnable("x", id="a" * 32, status="running", pane_id="w1:p1", pane_seq=3,
                        updated=iso(utcnow()))
    old_seq = runnable("y", id="b" * 32, status="running", pane_id="w2:p1", pane_seq=9,
                       updated=iso(NOW - timedelta(minutes=5)))
    store.save_tasks([just_now, old_seq])
    fake_herdr["snap"]["agents"] = [agent("w1:p1", "idle", sid="s1", seq=5),
                                   agent("w2:p1", "idle", sid="s2", seq=9)]
    assert logistics.watch(store, fake_herdr["snap"]) == []
    assert all(t.status == "running" for t in store.tasks())


def test_a_closed_pane_parks_the_card_with_the_reason(store, fake_herdr):
    t = runnable("x", id="a" * 32, status="running", pane_id="w9:p1", pane_seq=1)
    store.save_tasks([t])
    fake_herdr["snap"]["agents"] = []
    logistics.watch(store, fake_herdr["snap"])
    fresh = store.get_task(t.id)
    assert fresh.status == "needs-you" and "closed" in fresh.last_error


def test_a_card_the_session_closed_itself_is_left_alone(store, fake_herdr):
    t = runnable("x", id="a" * 32, status="done", pane_id="w1:p1", pane_seq=1)
    store.save_tasks([t])
    fake_herdr["snap"]["agents"] = [agent("w1:p1", "idle", seq=9)]
    assert logistics.watch(store, fake_herdr["snap"]) == []
    assert store.get_task(t.id).status == "done"


def test_headless_runs_are_not_the_watchers_business(store, fake_herdr):
    t = runnable("x", id="a" * 32, status="running", pane_id="w1:p1", run_id="r" * 32,
                 updated=iso(NOW - timedelta(hours=1)))
    store.save_tasks([t])
    fake_herdr["snap"]["agents"] = []
    assert logistics.watch(store, fake_herdr["snap"]) == []


# ---- the prompt -----------------------------------------------------------------------------

def test_pane_prompt_keeps_the_unattended_rules_and_adds_the_footer():
    t = runnable("otto: fix the board", id="a" * 32)
    text = dispatch.pane_prompt(t)
    assert "otto-gate" in text                      # the guard contract travels with it
    assert "python -m otto task mv aaaaaaaa done" in text
    assert "needs-you" in text
