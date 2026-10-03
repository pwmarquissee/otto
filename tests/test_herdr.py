"""The herdr client: result parsing, the session join, and the sync into Otto's
session rows. The CLI is faked; the record shapes are the ones herdr 0.9.3
printed on this machine on 2026-10-01.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from otto import herdr
from otto.models import Session

AGENT = {"agent": "claude", "agent_status": "idle", "cwd": "D:\\otto", "focused": True,
         "name": "otto", "pane_id": "w1:p1", "revision": 2, "state_change_seq": 1,
         "tab_id": "w1:t1", "terminal_id": "term_65cd1bae0bf141",
         "terminal_title": "✳ Claude Code", "workspace_id": "w1",
         "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude",
                           "value": "660a3192-0517-4eeb-9d21-866ea74682f6"}}


class R:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []
    answers = {}
    monkeypatch.setattr(herdr, "binary", lambda: "herdr")

    def run(argv, **kw):
        calls.append(argv[1:])
        key = " ".join(argv[1:3])
        return answers.get(key, R(0, json.dumps({"id": "x", "result": {"type": "ok"}})))

    monkeypatch.setattr(subprocess, "run", run)
    return calls, answers


def test_results_are_unwrapped_and_errors_carry_their_code(fake_cli):
    calls, answers = fake_cli
    answers["agent list"] = R(0, json.dumps({"id": "cli:agent:list",
                                             "result": {"agents": [AGENT], "type": "agent_list"}}))
    assert herdr._run(["agent", "list"])["agents"][0]["name"] == "otto"
    answers["agent prompt"] = R(1, "", json.dumps({"error": {"code": "agent_blocked",
                                                              "message": "agent is blocked"},
                                                   "id": "cli:agent:prompt"}))
    with pytest.raises(herdr.HerdrError) as e:
        herdr.prompt("otto", "hi")
    assert e.value.code == "agent_blocked"


def test_a_down_server_is_none_not_an_exception(fake_cli):
    calls, answers = fake_cli
    answers["api snapshot"] = R(1, "", "could not connect to the herdr server")
    assert herdr.snapshot() is None
    assert herdr.server_running() is False


def test_not_installed_is_an_error_everywhere(monkeypatch):
    monkeypatch.setattr(herdr, "binary", lambda: None)
    with pytest.raises(herdr.HerdrError) as e:
        herdr.snapshot()
    assert e.value.code == "not_installed"


def test_the_join_is_the_integrations_session_id():
    assert herdr.session_id_of(AGENT) == "660a3192-0517-4eeb-9d21-866ea74682f6"
    assert herdr.session_id_of({**AGENT, "agent": "codex"}) is None
    assert herdr.session_id_of({**AGENT, "agent_session": None}) is None
    assert herdr.target_of(AGENT) == "otto"
    assert herdr.target_of({k: v for k, v in AGENT.items() if k != "name"}) == "w1:p1"


def test_claude_command_resumes_with_the_configured_args(monkeypatch):
    monkeypatch.setattr(herdr.config, "HERDR_CLAUDE_ARGS", ["--dangerously-skip-permissions"])
    assert herdr.claude_command() == "claude --dangerously-skip-permissions"
    assert herdr.claude_command("abc") == "claude --dangerously-skip-permissions --resume abc"


# ---- sync -------------------------------------------------------------------------------

def test_sync_writes_the_pane_onto_the_matching_session(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [AGENT]})
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="idle", title="the otto pane")])
    herdr.sync(store)
    s = store.get_session(sid)
    assert (s.herdr_pane, s.herdr_agent, s.herdr_status) == ("w1:p1", "otto", "idle")
    assert s.effective_state == "idle"


def test_sync_creates_a_session_the_hooks_have_not_reported(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "working"}]})
    notes = herdr.sync(store)
    s = store.get_session(AGENT["agent_session"]["value"])
    assert s is not None and s.state == "busy" and s.cwd == "D:\\otto"
    assert notes and "is session" in notes[0]


def test_herdrs_blocked_outranks_the_hooks_busy(store, monkeypatch):
    """An AskUserQuestion fires no hook, so the hooks say busy. herdr reads the
    screen and says blocked. The rail must show blocked."""
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "blocked"}]})
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="busy")])
    herdr.sync(store)
    s = store.get_session(sid)
    assert s.state == "busy" and s.effective_state == "waiting"


def test_sync_revives_a_session_the_sweep_called_offline(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [AGENT]})
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="offline", offline_inferred=True,
                                 offline_reason="silence")])
    herdr.sync(store)
    s = store.get_session(sid)
    assert s.state == "idle" and s.offline_inferred is False and s.offline_reason is None


def test_sync_clears_the_pane_when_herdr_no_longer_lists_it(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": []})
    store.save_sessions([Session(session_id="gone", state="idle", herdr_pane="w1:p1",
                                 herdr_agent="otto", herdr_status="idle")])
    herdr.sync(store)
    s = store.get_session("gone")
    assert (s.herdr_pane, s.herdr_agent, s.herdr_status) == (None, None, None)


def test_sync_is_silent_when_the_server_is_down(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: None)
    store.save_sessions([Session(session_id="x", state="idle", herdr_pane="w1:p1")])
    assert herdr.sync(store) == []
    assert store.get_session("x").herdr_pane == "w1:p1"   # not cleared on a blind tick


# ---- the blocked toast ------------------------------------------------------------------

SCREEN = "\n".join([
    "I can take either route.",
    "Which branch should the fix land on?",
    "  1. main",
    "  2. release/0.9",
    "",
    "╭──────────────────────────────╮",
    "│ >                            │",
    "╰──────────────────────────────╯",
    "  ? for shortcuts",
])


def test_last_line_skips_the_input_box_and_the_footer():
    assert herdr.last_line(SCREEN) == "2. release/0.9"
    assert herdr.last_line("") == ""
    assert herdr.last_line("│ > │\n? for shortcuts\n") == ""


def test_a_pane_turning_blocked_posts_the_waiting_notice_and_leaving_retires_it(store, monkeypatch):
    """An AskUserQuestion fires no hook, so the hooks still say busy. herdr sees
    the question on the screen; that transition must raise the same toast the
    hooks would have, saying what was asked, and take it down once answered."""
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "read", lambda target, lines=120: SCREEN)
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="busy", title="the fix")])

    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "blocked"}]})
    notes = herdr.sync(store)
    s = store.get_session(sid)
    assert s.effective_state == "waiting" and s.note == "2. release/0.9"
    posted = [n for n in store.notices() if n.source == "session"]
    assert len(posted) == 1
    assert posted[0].title == "the fix is asking you"
    assert posted[0].body == "2. release/0.9"
    assert posted[0].key == f"session-waiting:{sid}:{s.state_since}"
    assert any("is blocked in w1:p1" in n for n in notes)

    # Still blocked on the next tick: the same question is not a second toast.
    herdr.sync(store)
    assert len([n for n in store.notices() if n.source == "session"]) == 1

    # Answered at the keyboard: the pane goes back to working, the notice retires.
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "working"}]})
    herdr.sync(store)
    s = store.get_session(sid)
    assert s.note is None and s.effective_state == "busy"
    assert all(n.read_at is not None for n in store.notices() if n.source == "session")


def test_a_pane_blocked_while_the_hooks_already_said_waiting_is_not_a_second_toast(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "read", lambda target, lines=120: SCREEN)
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="waiting", note="permission for Bash?")])
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "blocked"}]})
    herdr.sync(store)
    assert not [n for n in store.notices() if n.source == "session"]
    assert store.get_session(sid).note == "permission for Bash?"   # the hook's text stands


def test_a_blocked_pane_that_closes_retires_its_notice(store, monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "read", lambda target, lines=120: SCREEN)
    sid = AGENT["agent_session"]["value"]
    store.save_sessions([Session(session_id=sid, state="busy")])
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": [{**AGENT, "agent_status": "blocked"}]})
    herdr.sync(store)
    monkeypatch.setattr(herdr, "snapshot", lambda: {"agents": []})
    herdr.sync(store)
    assert all(n.read_at is not None for n in store.notices() if n.source == "session")
    assert store.get_session(sid).note is None
