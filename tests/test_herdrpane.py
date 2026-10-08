"""Otto's runs inside herdr panes: workspace reuse, the tee script, pid
resolution, the fallback to a plain detached process, and the keep-minutes
sweep. herdr's CLI is faked at herdr._run; the record shapes are the ones
herdr 0.9.3 printed on this machine on 2026-10-01. One test runs the generated
tee script through a real PowerShell, because the whole point of the script is
what lands in the log byte for byte.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from otto import config, herdr, launcher
from otto.models import Run, iso, utcnow
from otto.runners import detached, herdrpane


@pytest.fixture
def fake_herdr(monkeypatch):
    """A herdr server in a dict. Workspaces and panes are created on demand, the
    pane shell is pid 4242, and `answers` overrides any command by its first two
    words (a value, or an exception to raise)."""
    calls: list[list[str]] = []
    state = {"workspaces": [], "panes": [], "agents": [], "shell_pid": 4242,
             "answers": {}, "n": 0}
    monkeypatch.setattr(herdr, "binary", lambda: "herdr")
    monkeypatch.setattr(herdrpane, "_POLL", 0.0)

    def run(args, timeout=20.0):
        calls.append(list(args))
        key = " ".join(args[:2])
        if key in state["answers"]:
            a = state["answers"][key]
            if isinstance(a, Exception):
                raise a
            return a
        if key == "api snapshot":
            return {"snapshot": {"workspaces": list(state["workspaces"]),
                                 "panes": list(state["panes"]),
                                 "agents": list(state["agents"])}}
        if key == "workspace create":
            ws = f"w{len(state['workspaces']) + 1}"
            label = args[args.index("--label") + 1] if "--label" in args else None
            state["workspaces"].append({"workspace_id": ws, "label": label})
            state["panes"].append({"pane_id": f"{ws}:p1", "workspace_id": ws})
            return {"workspace": {"workspace_id": ws}, "root_pane": {"pane_id": f"{ws}:p1"}}
        if key == "tab create":
            ws = args[args.index("--workspace") + 1]
            state["n"] += 1
            pane = f"{ws}:p{state['n'] + 1}"
            state["panes"].append({"pane_id": pane, "workspace_id": ws})
            return {"root_pane": {"pane_id": pane, "workspace_id": ws},
                    "tab": {"label": args[args.index("--label") + 1]}}
        if key == "pane process-info":
            return {"process_info": {"shell_pid": state["shell_pid"],
                                     "foreground_processes": [
                                         {"pid": state["shell_pid"], "name": "powershell.exe",
                                          "cmdline": "powershell.exe -NoExit -Command ..."}]}}
        if key == "pane close":
            state["panes"] = [p for p in state["panes"] if p["pane_id"] != args[2]]
            return {}
        return {}

    monkeypatch.setattr(herdr, "_run", run)
    return calls, state


def _calls(calls, *head):
    return [c for c in calls if c[:len(head)] == list(head)]


# ---- workspace and tab ---------------------------------------------------------

def test_workspace_is_created_once_and_found_by_label(fake_herdr, monkeypatch, tmp_path):
    calls, state = fake_herdr
    monkeypatch.setattr(herdrpane, "_children_of", lambda pid: [(777, "python -c x")])
    first = herdrpane.launch_in_pane(["python", "-c", "x"], str(tmp_path), tmp_path / "a.log",
                                     {}, label="a")
    assert first.workspace_id == "w1"
    assert state["workspaces"] == [{"workspace_id": "w1", "label": "otto-runs"}]
    second = herdrpane.launch_in_pane(["python", "-c", "x"], str(tmp_path), tmp_path / "b.log",
                                      {}, label="b")
    assert second.workspace_id == "w1"
    assert len(_calls(calls, "workspace", "create")) == 1
    # A tab per run, not a split, and never focused: the run must not steal the
    # pane the owner is typing in.
    tabs = _calls(calls, "tab", "create")
    assert len(tabs) == 2
    assert all("--no-focus" in t and "--workspace" in t and t[t.index("--workspace") + 1] == "w1"
               for t in tabs)
    assert first.pane_id != second.pane_id


def test_tee_script_and_the_command_typed_into_the_pane(fake_herdr, monkeypatch, tmp_path):
    """The PowerShell pane script; the bash form is in test_launcher.py."""
    calls, state = fake_herdr
    monkeypatch.setattr(launcher, "WINDOWS", True)
    monkeypatch.setattr(herdrpane, "_children_of", lambda pid: [(777, "x it's.ps1 y")])
    log = tmp_path / "run.log"
    cmd = ["powershell", "-File", r"C:\logs\it's.ps1"]
    launched = herdrpane.launch_in_pane(cmd, str(tmp_path), log,
                                        {"OTTO_RUN_ID": "abc", "OTTO_UNATTENDED": "1"},
                                        label="ping", match="it's.ps1")
    script = tmp_path / "run.log.pane.ps1"
    body = script.read_text(encoding="utf-8")
    # Not Tee-Object: PS 5.1 writes those files as UTF-16LE. A UTF-8 StreamWriter
    # with LF endings, flushed per line so the log can be tailed while live.
    assert "Tee-Object" not in body
    assert "[IO.StreamWriter]::new('" + str(log) + "', $false, [Text.UTF8Encoding]::new($false))" in body
    assert '$__w.NewLine = "`n"' in body
    assert "$__w.Flush()" in body
    assert "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)" in body
    # The command, each argument single-quoted, embedded quotes doubled; stderr
    # merged and every pipeline object stringified.
    assert "& 'powershell' '-File' 'C:\\logs\\it''s.ps1' 2>&1 | ForEach-Object" in body
    assert "$env:OTTO_RUN_ID = 'abc'" in body and "$env:OTTO_UNATTENDED = '1'" in body
    # The pane shell is told the same variables, and is typed into only once the
    # shell exists.
    tab = _calls(calls, "tab", "create")[0]
    assert "OTTO_RUN_ID=abc" in tab and "OTTO_UNATTENDED=1" in tab
    assert tab[tab.index("--cwd") + 1] == str(tmp_path)
    order = [c[:2] for c in calls]
    assert order.index(["pane", "process-info"]) < order.index(["pane", "run"])
    assert _calls(calls, "pane", "run")[0] == ["pane", "run", launched.pane_id, f"& '{script}'"]
    assert launched.pid == 777


@pytest.mark.skipif(shutil.which("powershell") is None,
                    reason="the tee script is PowerShell; nothing to run it with here")
def test_tee_script_lands_bytes_exactly(tmp_path):
    log = tmp_path / "exact.log"
    code = ("import json,sys;print(json.dumps({'type':'ping','text':'caf\\u00e9 \\u2713'},"
            "ensure_ascii=False),flush=True);print('warn',file=sys.stderr);print('x'*600)")
    script = herdrpane._tee_script([sys.executable, "-c", code], log, {"OTTO_PROBE": "1"})
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(script)], capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    assert "otto: run exited 0" in r.stdout, r.stdout + r.stderr
    # Lines compared as a set: stdout and stderr are two pipes and PowerShell
    # drains them concurrently, so their interleaving is not a thing any tee can
    # promise. The bytes of each line, the UTF-8, and the LF endings are.
    expected = {b'{"type": "ping", "text": "caf\xc3\xa9 \xe2\x9c\x93"}', b"warn", b"x" * 600}
    raw = log.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw and b"\xff\xfe" not in raw
    assert set(raw[:-1].split(b"\n")) == expected
    # The screen got the same lines, with stderr as its bare message rather than
    # a rendered ErrorRecord.
    assert "warn" in r.stdout and "CategoryInfo" not in r.stdout


# ---- pid resolution ------------------------------------------------------------

def test_pid_is_the_shell_child_carrying_the_token(fake_herdr, monkeypatch):
    calls, state = fake_herdr
    monkeypatch.setattr(herdrpane, "_children_of",
                        lambda pid: [(10, "conhost.exe"), (11, r"powershell -Command & 'C:\l\abc.launch.ps1'")])
    assert herdrpane.resolve_pid("w1:p2", 4242, "abc.launch.ps1", timeout=0.5) == (11, None)
    # The shell itself is never the answer, even though process-info lists it.
    monkeypatch.setattr(herdrpane, "_children_of", lambda pid: [])
    pid, note = herdrpane.resolve_pid("w1:p2", 4242, "abc.launch.ps1", timeout=0.05)
    assert pid is None and "already finished" in note
    # Something is running but nothing matched: track it rather than read the
    # run as finished while it works.
    monkeypatch.setattr(herdrpane, "_children_of", lambda pid: [(12, "node something-else")])
    pid, note = herdrpane.resolve_pid("w1:p2", 4242, "abc.launch.ps1", timeout=0.05)
    assert pid == 12 and "by position" in note


# ---- detached.spawn: pane first, process as the fallback -----------------------

class _Proc:
    pid = 999


class _PsProc:
    def __init__(self, pid):
        self.pid = pid

    def create_time(self):
        return 1700000000.0


@pytest.fixture
def spawnable(monkeypatch, tmp_path):
    popen_calls: list[list[str]] = []

    def popen(cmd, **kw):
        popen_calls.append([str(c) for c in cmd])
        return _Proc()

    monkeypatch.setattr(detached.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(detached.subprocess, "Popen", popen)
    monkeypatch.setattr(detached.psutil, "Process", _PsProc)
    monkeypatch.setattr(config, "HERDR_RUNS", True)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "server_running", lambda: True)
    return popen_calls


def test_spawn_runs_in_a_pane_and_records_it(spawnable, monkeypatch, tmp_path):
    seen = {}

    def launch(cmd, cwd, log, env, label, match=None):
        seen.update(cmd=cmd, cwd=cwd, log=log, env=env, label=label, match=match)
        return herdrpane.Launched("w3:p9", 4321, "w3")

    monkeypatch.setattr(herdrpane, "launch_in_pane", launch)
    run = detached.spawn("daily", "/daily", str(tmp_path), mode="headless")
    assert (run.pane_id, run.workspace_id, run.pid, run.pid_created) == ("w3:p9", "w3", 4321, 1700000000.0)
    assert run.log and run.log.endswith(".log") and Path(run.log) == seen["log"]
    assert "mode=headless" in run.notes and "| pane" in run.notes
    assert spawnable == [], "no detached process when the pane took the run"
    # The pane runs the same launcher the detached path would, via -Command so the
    # child decodes claude's UTF-8 before the tee re-encodes it; the launcher path
    # is the token the pid is resolved by.
    assert seen["cmd"] == launcher.pane_command(Path(seen["match"]))
    if launcher.WINDOWS:
        assert seen["cmd"][4] == "-Command" and seen["match"] in seen["cmd"][5]
    assert launcher.is_launcher(seen["match"]) and Path(seen["match"]).is_file()
    assert seen["env"] == {"OTTO_RUN_NAME": "daily", "OTTO_RUN_ID": run.id, "OTTO_UNATTENDED": "1"}
    assert seen["label"] == "daily" and seen["cwd"] == str(tmp_path)
    assert run.cmd == seen["cmd"]


def test_spawn_falls_back_to_a_detached_process_when_herdr_refuses(spawnable, monkeypatch, tmp_path):
    def launch(*a, **kw):
        raise herdr.HerdrError("socket", "server went away")

    monkeypatch.setattr(herdrpane, "launch_in_pane", launch)
    run = detached.spawn("daily", "/daily", str(tmp_path), mode="headless")
    assert run.pane_id is None and run.workspace_id is None
    assert run.pid == 999 and run.pid_created == 1700000000.0
    assert "herdr unavailable, ran detached (socket)" in run.notes
    assert "| pane" not in run.notes
    assert len(spawnable) == 1 and spawnable[0] == launcher.command(Path(run.cmd[-1]))
    assert launcher.is_launcher(run.cmd[-1])


def test_spawn_without_a_server_is_the_old_path_with_a_note(spawnable, monkeypatch, tmp_path):
    monkeypatch.setattr(herdr, "server_running", lambda: False)
    called = []
    monkeypatch.setattr(herdrpane, "launch_in_pane", lambda *a, **k: called.append(a))
    run = detached.spawn("daily", "/daily", str(tmp_path), mode="headless")
    assert called == [] and run.pane_id is None and run.pid == 999
    assert "herdr unavailable, ran detached (server down)" in run.notes
    # Switched off entirely: no note, nothing herdr-shaped, exactly as before.
    monkeypatch.setattr(config, "HERDR_RUNS", False)
    run = detached.spawn("daily", "/daily", str(tmp_path), mode="headless")
    assert run.notes == "mode=headless" and run.pane_id is None


def test_attended_spawn_is_an_interactive_claude_prompted_through_herdr(spawnable, monkeypatch, tmp_path):
    seen = {}

    def launch(cwd, args, prompt_text, label, env=None, timeout=None):
        seen.update(cwd=cwd, args=args, prompt=prompt_text, label=label, env=env)
        return herdrpane.LaunchedClaude("w3:p4", 555, "w3", True)

    monkeypatch.setattr(herdrpane, "launch_claude_interactive", launch)
    run = detached.spawn("fix the thing", "do it", str(tmp_path), mode="windowed",
                         skip_permissions=False, model="opus", system_extra="context")
    assert run.pane_id == "w3:p4" and run.pid == 555
    # No -p, no stream-json: a real session the owner sits at. The system prompt file
    # the launcher wrote is passed by path, never by value.
    assert "-p" not in seen["args"]
    assert seen["args"][:2] == ["--model", "opus"]
    assert "--dangerously-skip-permissions" not in seen["args"]
    assert seen["args"][-2] == "--append-system-prompt-file" and seen["args"][-1].endswith(".sysextra.txt")
    assert Path(seen["args"][-1]).read_text(encoding="utf-8") == "context"
    assert seen["prompt"] == "do it"
    # poll() keys the transcript path off this, and there is no log to parse.
    assert "mode=windowed" in run.notes and run.log is None
    assert spawnable == []


# ---- the sweep -----------------------------------------------------------------

def _run(name, pane, status, ended_minutes_ago):
    ended = None
    if ended_minutes_ago is not None:
        ended = iso(utcnow() - timedelta(minutes=ended_minutes_ago))
    return Run(id=name * 8, name=name, runner="detached", status=status, pane_id=pane,
               workspace_id="w3", ended=ended, notes="mode=headless | pane")


def test_sweep_closes_only_panes_of_long_finished_dead_runs(fake_herdr, monkeypatch, store):
    calls, state = fake_herdr
    for p in ("w3:p2", "w3:p3", "w3:p4", "w3:p5"):
        state["panes"].append({"pane_id": p, "workspace_id": "w3"})
    runs = [
        _run("aaaa", "w3:p2", "ok", 120),        # old and dead: closed
        _run("bbbb", "w3:p3", "ok", 5),          # too fresh: scrollback kept
        _run("cccc", "w3:p4", "running", None),  # running: never
        _run("dddd", "w3:p5", "orphaned", 120),  # old, but the process is alive
        _run("eeee", "w3:p6", "failed", 120),    # old, pane already gone
    ]
    store.save_runs(runs)
    monkeypatch.setattr(detached, "_alive", lambda r: r.pane_id == "w3:p5")
    monkeypatch.setattr(config, "HERDR_RUNS_KEEP_MINUTES", 60)

    notes = herdrpane.sweep(store, force=True)
    assert _calls(calls, "pane", "close") == [["pane", "close", "w3:p2"]]
    assert any("closed pane w3:p2 of aaaa" in n for n in notes)
    by = {r.name: r for r in store.runs()}
    assert by["aaaa"].pane_id is None and "pane w3:p2 closed" in by["aaaa"].notes
    assert by["eeee"].pane_id is None, "a pane herdr no longer lists just loses the pointer"
    assert by["bbbb"].pane_id == "w3:p3" and by["cccc"].pane_id == "w3:p4"
    assert by["dddd"].pane_id == "w3:p5"
    # Throttled: the next tick does not snapshot again.
    n_before = len(calls)
    assert herdrpane.sweep(store) == [] and len(calls) == n_before


def test_sweep_is_silent_with_nothing_to_do_and_when_the_server_is_down(fake_herdr, store):
    calls, state = fake_herdr
    store.save_runs([_run("bbbb", "w3:p3", "ok", 5)])
    assert herdrpane.sweep(store, force=True) == []
    assert _calls(calls, "api", "snapshot") == [], "no candidates, no shell-out"
    store.save_runs([_run("aaaa", "w3:p2", "ok", 120)])
    state["answers"]["api snapshot"] = herdr.HerdrError("socket", "down")
    assert herdrpane.sweep(store, force=True) == []
    assert store.runs()[0].pane_id == "w3:p2", "nothing forgotten while herdr cannot be asked"


def test_run_dump_carries_the_pane_for_the_live_views():
    r = _run("aaaa", "w3:p2", "running", None)
    body = r.model_dump()
    assert body["pane_id"] == "w3:p2" and body["workspace_id"] == "w3"
    assert json.loads(r.model_dump_json())["pane_id"] == "w3:p2"
