"""herdr lifecycle: worktrees, migration planning, the window title, and the
probe row. The CLI is faked at otto.herdr._run so every test pins the exact
argv Otto hands herdr and the exact result shapes it reads back; nothing here
touches a real server, a real git repo, or a real terminal title.
"""

from __future__ import annotations

import pytest

from otto import config, herdr
from otto.models import Session
from otto.runners import external

SID = "660a3192-0517-4eeb-9d21-866ea74682f6"
AGENT = {"agent": "claude", "agent_status": "idle", "cwd": "D:\\otto", "name": "otto",
         "pane_id": "w1:p1", "state_change_seq": 1, "workspace_id": "w1",
         "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude",
                           "value": SID}}
PANE = {"agent": "claude", "agent_status": "idle", "cwd": "D:\\otto", "pane_id": "w1:p1",
        "workspace_id": "w1"}
CREATED = {"type": "worktree_create",
           "workspace": {"workspace_id": "w4", "label": "feat-x"},
           "tab": {"tab_id": "w4:t1"},
           "root_pane": {"pane_id": "w4:p1"}}


@pytest.fixture
def fake_run(monkeypatch):
    """(calls, answers). `answers` maps the first two argv words to a result body,
    an exception to raise, or a callable taking argv."""
    calls: list[list[str]] = []
    answers: dict[str, object] = {}

    def run(args, timeout=20.0):
        calls.append(list(args))
        ans = answers.get(" ".join(args[:2]))
        if isinstance(ans, Exception):
            raise ans
        if callable(ans):
            return ans(args)
        return ans if ans is not None else {}

    monkeypatch.setattr(herdr, "_run", run)
    monkeypatch.setattr(herdr, "binary", lambda: "herdr")
    monkeypatch.setattr(herdr, "_last_title", None)
    return calls, answers


# ---- worktrees ---------------------------------------------------------------------

def test_worktree_create_builds_the_command_and_returns_the_ids(fake_run):
    calls, answers = fake_run
    answers["worktree create"] = CREATED
    assert herdr.worktree_create("D:\\otto", "feat/x", base="main", label="feat-x") == ("w4", "w4:p1")
    assert calls == [["worktree", "create", "--cwd", "D:\\otto", "--branch", "feat/x",
                      "--no-focus", "--base", "main", "--label", "feat-x"]]


def test_worktree_create_without_base_or_label_sends_neither(fake_run):
    calls, answers = fake_run
    answers["worktree create"] = CREATED
    herdr.worktree_create("D:\\otto", "feat/x")
    assert calls[0] == ["worktree", "create", "--cwd", "D:\\otto", "--branch", "feat/x", "--no-focus"]
    with pytest.raises(herdr.HerdrError) as e:
        herdr.worktree_create("D:\\otto", "")
    assert e.value.code == "bad_branch"
    assert len(calls) == 1   # refused before any CLI call


def test_worktree_open_takes_branch_or_path_but_not_both(fake_run):
    calls, answers = fake_run
    answers["worktree open"] = CREATED
    assert herdr.worktree_open("D:\\otto", branch="feat/x") == ("w4", "w4:p1")
    assert calls[-1] == ["worktree", "open", "--cwd", "D:\\otto", "--no-focus", "--branch", "feat/x"]
    herdr.worktree_open("D:\\otto", path="D:\\wt\\feat-x", label="x")
    assert calls[-1] == ["worktree", "open", "--cwd", "D:\\otto", "--no-focus",
                         "--path", "D:\\wt\\feat-x", "--label", "x"]
    for kw in ({}, {"branch": "a", "path": "b"}):
        with pytest.raises(herdr.HerdrError) as e:
            herdr.worktree_open("D:\\otto", **kw)
        assert e.value.code == "bad_target"
    assert len(calls) == 2


def test_worktree_list_returns_source_and_rows(fake_run):
    calls, answers = fake_run
    answers["worktree list"] = {
        "type": "worktree_list",
        "source": {"repo_root": "D:\\otto", "source_workspace_id": "w1"},
        "worktrees": [{"branch": "master", "path": "D:/otto", "open_workspace_id": "w1"}]}
    r = herdr.worktree_list("D:\\otto\\otto")
    assert calls == [["worktree", "list", "--cwd", "D:\\otto\\otto"]]
    assert r["source"]["source_workspace_id"] == "w1"
    assert r["worktrees"][0]["branch"] == "master"
    answers["worktree list"] = {"type": "worktree_list"}
    assert herdr.worktree_list("D:\\otto") == {"source": {}, "worktrees": []}


def test_a_result_without_ids_is_a_herdr_error_not_a_keyerror(fake_run):
    _calls, answers = fake_run
    answers["worktree create"] = {"type": "ok"}
    with pytest.raises(herdr.HerdrError) as e:
        herdr.worktree_create("D:\\otto", "x")
    assert e.value.code == "bad_result"
    # The flat spelling is accepted too, so a trimmed response does not break the daemon.
    answers["worktree create"] = {"workspace_id": "w9", "root_pane_id": "w9:p1"}
    assert herdr.worktree_create("D:\\otto", "x") == ("w9", "w9:p1")


# ---- the launch into an existing pane --------------------------------------------------

def test_launch_claude_in_pane_types_the_command_waits_and_names(fake_run, monkeypatch):
    calls, answers = fake_run
    monkeypatch.setattr(config, "HERDR_CLAUDE_ARGS", ["--dangerously-skip-permissions"])
    ready = {**AGENT, "pane_id": "w4:p1", "name": None}
    answers["api snapshot"] = {"snapshot": {"agents": [ready]}}
    answers["agent rename"] = {"agent": {**ready, "name": "feat-x"}}
    a = herdr.launch_claude_in_pane("w4:p1", name="feat-x", resume="abc")
    assert a["name"] == "feat-x"
    assert calls[0] == ["pane", "run", "w4:p1", "claude --dangerously-skip-permissions --resume abc"]
    assert ["agent", "rename", "w4:p1", "feat-x"] in calls


def test_start_claude_is_create_workspace_plus_the_same_launch(fake_run, monkeypatch, tmp_path):
    calls, answers = fake_run
    answers["workspace create"] = {"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}}
    answers["api snapshot"] = {"snapshot": {"agents": [{**AGENT, "pane_id": "w5:p1"}]}}
    a = herdr.start_claude(str(tmp_path))
    assert a["pane_id"] == "w5:p1"
    assert calls[0][:3] == ["workspace", "create", "--cwd"]
    assert calls[1][:3] == ["pane", "run", "w5:p1"]


def test_launch_times_out_with_the_last_status(fake_run, monkeypatch):
    _calls, answers = fake_run
    answers["api snapshot"] = {"snapshot": {"agents": [{**AGENT, "pane_id": "w4:p1",
                                                        "agent_status": "working"}]}}
    monkeypatch.setattr(herdr.time, "sleep", lambda _s: None)
    with pytest.raises(herdr.HerdrError) as e:
        herdr.launch_claude_in_pane("w4:p1", timeout=0.01)
    assert e.value.code == "agent_not_ready" and "working" in e.value.message


# ---- adopt --all --------------------------------------------------------------------

def _sess(sid, state, cwd, last_event):
    return Session(session_id=sid, state=state, cwd=cwd, last_event=last_event).model_dump()


def test_plan_adopt_picks_offline_sessions_newest_first_and_skips_the_rest(tmp_path):
    snap = {"agents": [AGENT]}
    rows = [
        _sess(SID, "offline", str(tmp_path), "2026-10-01T10:00:00Z"),     # herdr has it
        _sess("b" * 32, "offline", str(tmp_path), "2026-10-01T09:00:00Z"),
        _sess("c" * 32, "offline", str(tmp_path / "gone"), "2026-10-01T08:00:00Z"),
        _sess("d" * 32, "idle", str(tmp_path), "2026-10-01T07:00:00Z"),     # live, not a candidate
        _sess("e" * 32, "offline", str(tmp_path), "2026-10-01T11:00:00Z"),
    ]
    chosen, skipped = herdr.plan_adopt(rows, snap, limit=10)
    assert [s["session_id"][0] for s in chosen] == ["e", "b"]
    reasons = {s["session_id"][0]: why for s, why in skipped}
    assert reasons["6"] == "already in herdr"
    assert reasons["c"].startswith("directory gone")
    assert "d" not in reasons


def test_plan_adopt_caps_at_the_limit_after_the_skips(tmp_path):
    rows = [_sess(ch * 32, "offline", str(tmp_path), f"2026-10-01T0{i}:00:00Z")
            for i, ch in enumerate("abc")]
    chosen, skipped = herdr.plan_adopt(rows, {"agents": []}, limit=2)
    assert [s["session_id"][0] for s in chosen] == ["c", "b"]
    assert [(s["session_id"][0], why) for s, why in skipped] == [("a", "over the --limit of 2")]


def test_plan_adopt_with_no_server_still_plans(tmp_path):
    rows = [_sess("a" * 32, "offline", str(tmp_path), "2026-10-01T01:00:00Z")]
    chosen, _skipped = herdr.plan_adopt(rows, None)
    assert len(chosen) == 1


# ---- open --all-repos ---------------------------------------------------------------

def test_plan_open_repos_dedupes_by_cwd_against_the_panes(tmp_path):
    (tmp_path / "Personal Projects").mkdir()
    repos = [
        {"key": "otto", "path": "D:/otto"},                       # pane open, other spelling
        {"key": "pp", "path": str(tmp_path / "Personal Projects")},
        {"key": "pp2", "path": str(tmp_path / "Personal Projects") + "\\"},   # same dir
        {"key": "nope", "path": str(tmp_path / "missing")},
    ]
    snap = {"panes": [PANE], "agents": [AGENT]}
    to_open, skipped = herdr.plan_open_repos(repos, snap)
    assert [r["key"] for r in to_open] == ["pp"]
    assert to_open[0]["name"] == "personal-projects"
    assert {r["key"]: why for r, why in skipped} == {"otto": "already open in herdr",
                                                     "nope": "not on disk"}


def test_agent_names_fit_herdrs_grammar():
    assert herdr.agent_name_for("D:\\work\\help-bot") == "help-bot"
    assert herdr.agent_name_for("D:/Personal Projects/My App!") == "my-app-"
    assert herdr.agent_name_for("D:/x/123abc") == "abc"
    assert herdr.agent_name_for("D:/x/---") == "repo"
    assert len(herdr.agent_name_for("D:/x/" + "a" * 50)) == 32


def test_open_cwds_reads_panes_not_workspaces():
    snap = {"workspaces": [{"workspace_id": "w1", "label": "otto"}],
            "panes": [{"pane_id": "w1:p1", "cwd": "D:\\Otto\\"}], "agents": []}
    assert herdr.open_cwds(snap) == {"d:/otto"}
    assert herdr.open_cwds(None) == set()


# ---- the window title ---------------------------------------------------------------

def test_window_title_counts_done_as_idle():
    snap = {"agents": [AGENT, {**AGENT, "agent_status": "done"},
                       {**AGENT, "agent_status": "working"},
                       {**AGENT, "agent_status": "blocked"},
                       {**AGENT, "agent_status": "unknown"}]}
    assert herdr.window_title(snap) == "Otto · 2 idle · 1 working · 1 blocked"
    assert herdr.window_title(None) == "Otto · 0 idle · 0 working · 0 blocked"


def test_title_is_set_once_per_change_and_retried_after_a_failure(fake_run, monkeypatch):
    calls, answers = fake_run
    monkeypatch.setattr(config, "HERDR_WINDOW_TITLE", True)
    snap = {"agents": [AGENT]}
    assert herdr._refresh_window_title(snap) is True
    assert herdr._refresh_window_title(snap) is False
    assert calls == [["terminal", "title", "set", "Otto · 1 idle · 0 working · 0 blocked"]]
    busy = {"agents": [{**AGENT, "agent_status": "working"}]}
    answers["terminal title"] = herdr.HerdrError("cli", "no client attached")
    assert herdr._refresh_window_title(busy) is False
    del answers["terminal title"]
    assert herdr._refresh_window_title(busy) is True    # not remembered, so tried again
    assert calls[-1][-1] == "Otto · 0 idle · 1 working · 0 blocked"
    monkeypatch.setattr(config, "HERDR_WINDOW_TITLE", False)
    assert herdr._refresh_window_title({"agents": []}) is False
    assert len(calls) == 3


def test_sync_pushes_the_title(store, fake_run, monkeypatch):
    calls, answers = fake_run
    monkeypatch.setattr(config, "HERDR_WINDOW_TITLE", True)
    monkeypatch.setattr(herdr, "available", lambda: True)
    answers["api snapshot"] = {"snapshot": {"agents": [AGENT]}}
    herdr.sync(store)
    assert ["terminal", "title", "set", "Otto · 1 idle · 0 working · 0 blocked"] in calls
    herdr.sync(store)
    assert sum(1 for c in calls if c[:2] == ["terminal", "title"]) == 1


# ---- the probe row ------------------------------------------------------------------

def test_probe_herdr_not_installed_is_unconfigured_not_an_outage(monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: False)
    i = external.probe_herdr()
    assert (i.name, i.ok, i.mode) == ("herdr", False, "unconfigured")
    assert "not installed" in i.detail and i.checked_at


def test_probe_herdr_dead_server_is_a_real_failure(monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: None)
    i = external.probe_herdr()
    assert (i.ok, i.mode) == (False, "api")
    assert "otto herdr up" in i.detail


def test_probe_herdr_live_server_counts_agents(monkeypatch):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "snapshot", lambda: {
        "agents": [AGENT, {**AGENT, "pane_id": "w2:p1", "agent_status": "blocked"}],
        "workspaces": [{"workspace_id": "w1"}, {"workspace_id": "w2"}]})
    i = external.probe_herdr()
    assert i.ok and i.mode == "api"
    assert i.detail == "server up, 2 agent(s) in 2 workspace(s), 1 blocked"
    assert (i.metric, i.metric_label) == (2.0, "agents")


def test_probe_herdr_is_in_the_sweep_and_a_raise_does_not_stop_it(monkeypatch):
    assert external.probe_herdr in external.PROBES

    def boom():
        raise RuntimeError("binary vanished")

    monkeypatch.setattr(external, "PROBES", [boom, external.probe_herdr])
    monkeypatch.setattr(config, "INTEGRATIONS", ("boom", "herdr"))
    monkeypatch.setattr(herdr, "available", lambda: False)
    rows = external.probe_all()
    assert [r.name for r in rows] == ["boom", "herdr"]
    assert "probe raised" in rows[0].detail
