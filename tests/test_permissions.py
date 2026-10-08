"""Permission levels: plan, yolo, scoped (runners/detached.py, dispatch.permissions_for).

The old default was --dangerously-skip-permissions everywhere, chosen by nobody.
Now every spawn names its level, a PREPARE run gets Claude Code's own plan mode,
and the one-word approval (`otto task yolo`) is the fast way back to full
permissions. These tests pin the flags, the derivation, the quick paths, and a
grep that no spawn site can drop the argument again.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otto import backlog, config, dispatch, launcher
from otto import daemon as daemon_mod
from otto.cli import board as cli_board
from otto.cli import build_parser
from otto.cli import schedules as cli_schedules
from otto.models import Run, Task, iso
from otto.runners import detached
from otto.store import Store

from conftest import make_task

OWN = f"http://127.0.0.1:{config.PORT}"
REPO = Path(__file__).resolve().parent.parent


# ---- the flags ------------------------------------------------------------------------

def test_flags_per_level():
    assert detached.permission_flags("yolo") == ["--dangerously-skip-permissions"]
    assert detached.permission_flags("plan") == ["--permission-mode", "plan",
                                                 "--permission-prompts", "none"]
    assert detached.permission_flags("plan", interactive=True) == ["--permission-mode", "plan"]
    assert detached.permission_flags("scoped") == []
    with pytest.raises(ValueError, match="permissions must be one of"):
        detached.permission_flags("sudo")


def _script(tmp_path, monkeypatch, level, windows):
    monkeypatch.setattr(launcher, "WINDOWS", windows)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.ps1" if windows else ".launch.sh")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    prompt = tmp_path / "p.txt"
    prompt.write_text("hi", encoding="utf-8")
    path = detached._write_launcher("abc123def", "job", str(tmp_path), prompt, None,
                                    tmp_path / "job.log", "headless", level)
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("windows", [True, False])
def test_plan_launcher_has_plan_mode_and_no_skip_flag(tmp_path, monkeypatch, windows):
    body = _script(tmp_path, monkeypatch, "plan", windows)
    assert "dangerously" not in body
    if windows:
        assert "'--permission-mode', 'plan', '--permission-prompts', 'none'" in body
        assert "$env:OTTO_RUN_PERMISSIONS = 'plan'" in body
        assert "$env:OTTO_UNATTENDED = '1'" in body, "plan is still unattended for the guard"
    else:
        assert "--permission-mode plan --permission-prompts none" in body
        assert "export OTTO_RUN_PERMISSIONS=plan" in body
        assert "export OTTO_UNATTENDED=1" in body


@pytest.mark.parametrize("windows", [True, False])
def test_yolo_launcher_is_what_every_run_used_to_be(tmp_path, monkeypatch, windows):
    body = _script(tmp_path, monkeypatch, "yolo", windows)
    assert "--dangerously-skip-permissions" in body
    assert "--permission-mode" not in body
    assert ("$env:OTTO_RUN_PERMISSIONS = 'yolo'" if windows else "export OTTO_RUN_PERMISSIONS=yolo") in body


def test_scoped_launcher_passes_neither(tmp_path, monkeypatch):
    body = _script(tmp_path, monkeypatch, "scoped", True)
    assert "dangerously" not in body and "--permission-mode" not in body
    assert "$env:OTTO_RUN_PERMISSIONS = 'scoped'" in body


def test_spawn_refuses_an_unknown_level_before_writing_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    with pytest.raises(ValueError, match="permissions must be one of"):
        detached.spawn("x", "do", str(tmp_path), permissions="root")
    assert not (tmp_path / "logs").exists()


def test_a_run_row_without_the_field_loads_as_yolo():
    row = {"id": "r" * 32, "name": "old", "runner": "detached", "status": "ok"}
    assert Run.model_validate(row).permissions == "yolo"


# ---- derivation -------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def card(tid="a", **kw) -> Task:
    base = {"id": tid.ljust(32, "0"), "title": f"card {tid}", "status": "backlog",
            "detail": "real context an agent could work from", "owner": "otto",
            "readiness": "ready", "tier": backlog.TIER_APPROVAL, "assessed": iso(NOW)}
    base.update(kw)
    return Task(**base)


def test_prepare_runs_plan_and_everything_else_yolo_unless_pinned():
    assert dispatch.permissions_for(card(run_mode="prepare")) == "plan"
    assert dispatch.permissions_for(card(run_mode="run")) == "yolo"
    assert dispatch.permissions_for(card()) == "yolo"
    assert dispatch.permissions_for(card(permissions="plan")) == "plan"
    # A prepare run is read-only by definition; a pin cannot make it write.
    assert dispatch.permissions_for(card(run_mode="prepare", permissions="yolo")) == "plan"


def _capture_spawn(monkeypatch):
    seen: list[dict] = []

    def fake_spawn(**kw):
        seen.append(kw)
        return Run(id="f" * 32, name=kw["name"], runner="detached", status="running",
                   pid=4242, task_id=kw.get("task_id"), permissions=kw["permissions"])

    monkeypatch.setattr(dispatch.detached, "spawn", fake_spawn)
    return seen


def test_dispatch_passes_the_level_and_records_it(store, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    t = card(run_mode="prepare", status="queued")
    store.upsert_task(t)
    run, msg = dispatch.dispatch(store, t, force=True)
    assert seen[0]["permissions"] == "plan" and run.permissions == "plan"
    assert "perm=plan" in run.notes
    t2 = card("b", status="queued", plan="do it", plan_approved=iso(NOW), run_mode="run")
    store.upsert_task(t2)
    run2, _ = dispatch.dispatch(store, t2, force=True, permissions="plan")
    assert seen[1]["permissions"] == "plan", "the explicit override wins"
    run3, _ = dispatch.dispatch(store, t2, force=True)
    assert seen[2]["permissions"] == "yolo"


# ---- the API --------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, daemon, monkeypatch):
    fresh = Store(tmp_path / "state")
    monkeypatch.setattr(daemon_mod, "store", fresh)
    monkeypatch.setattr(daemon_mod, "_state_cache", None)
    client = TestClient(daemon_mod.app, base_url=OWN)
    client.store = fresh
    return client


def test_spawn_request_level_and_the_old_alias(api, monkeypatch):
    seen = []

    def fake_spawn(**kw):
        seen.append(kw["permissions"])
        return Run(id="s" * 32, name=kw["name"], runner="detached", status="running",
                   permissions=kw["permissions"])

    monkeypatch.setattr(detached, "spawn", fake_spawn)
    base = {"name": "x", "prompt": "do", "cwd": "."}
    for body, want in ((base, "yolo"),
                       ({**base, "permissions": "plan"}, "plan"),
                       ({**base, "skip_permissions": False}, "plan"),
                       ({**base, "skip_permissions": True}, "yolo"),
                       ({**base, "skip_permissions": False, "permissions": "yolo"}, "yolo")):
        r = api.post("/api/runs/spawn", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["permissions"] == want
    assert seen == ["yolo", "plan", "plan", "yolo", "yolo"]


def test_task_patch_pins_and_clears_the_level(api):
    t = make_task(id="p" * 32)
    api.store.upsert_task(t)
    assert api.patch(f"/api/tasks/{t.id}", json={"permissions": "plan"}).json()["permissions"] == "plan"
    assert api.patch(f"/api/tasks/{t.id}", json={"permissions": ""}).json()["permissions"] is None
    assert api.patch(f"/api/tasks/{t.id}", json={"permissions": "root"}).status_code == 400


def test_dispatch_route_takes_a_level(api, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    t = card(status="queued", plan="x", plan_approved=iso(NOW), run_mode="run")
    api.store.upsert_task(t)
    r = api.post(f"/api/tasks/{t.id}/dispatch?force=true&permissions=plan")
    assert r.status_code == 200, r.text
    assert seen[0]["permissions"] == "plan" and r.json()["run"]["permissions"] == "plan"
    assert api.post(f"/api/tasks/{t.id}/dispatch?permissions=root").status_code == 400


def test_yolo_approves_the_plan_pins_the_level_and_runs(api, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    t = card(status="needs-you", plan="## Plan\n1. do the thing", plan_approved=None)
    api.store.upsert_task(t)
    r = api.post(f"/api/tasks/{t.id}/yolo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["plan_approved"] and body["task"]["permissions"] == "yolo"
    assert body["task"]["run_mode"] == "run" and body["task"]["status"] == "running"
    assert body["run"]["permissions"] == "yolo" and seen[0]["permissions"] == "yolo"
    assert "APPROVED PLAN" in seen[0]["prompt"]


def test_yolo_still_obeys_the_gate(api, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    owned = card("c", status="backlog", owner="owner", plan="x")
    api.store.upsert_task(owned)
    r = api.post(f"/api/tasks/{owned.id}/yolo")
    assert r.status_code == 409 and "owned by" in r.json()["detail"]
    assert seen == [] and api.store.get_task(owned.id).plan_approved is None
    running = card("d", status="running", run_id="r" * 32)
    api.store.upsert_task(running)
    assert api.post(f"/api/tasks/{running.id}/yolo").status_code == 409


def test_yolo_on_a_tier_zero_card_needs_no_plan(api, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    t = card("e", tier=backlog.TIER_AUTONOMOUS)
    api.store.upsert_task(t)
    r = api.post(f"/api/tasks/{t.id}/yolo")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["plan_approved"] is None and seen[0]["permissions"] == "yolo"


def test_schedule_level_round_trips(api):
    body = {"name": "nightly", "command": "/daily", "kind": "daily", "at": "08:00"}
    assert api.put("/api/schedules/nightly", json=body).json()["permissions"] == "yolo"
    assert api.put("/api/schedules/nightly", json={**body, "permissions": "plan"}).json()["permissions"] == "plan"
    row = next(s for s in api.get("/api/schedules").json() if s["name"] == "nightly")
    assert row["permissions"] == "plan"
    assert api.put("/api/schedules/nightly", json={**body, "permissions": "root"}).status_code == 422


# ---- the CLI --------------------------------------------------------------------------

def test_spawn_and_task_run_flags():
    p = build_parser()
    assert p.parse_args(["spawn", "x", "--prompt", "hi"]).permissions == "yolo"
    assert p.parse_args(["spawn", "x", "--prompt", "hi", "--plan"]).permissions == "plan"
    assert p.parse_args(["spawn", "x", "--prompt", "hi", "--safe"]).permissions == "plan"
    assert p.parse_args(["task", "run", "abc"]).permissions is None
    assert p.parse_args(["task", "run", "abc", "--yolo"]).permissions == "yolo"
    assert p.parse_args(["task", "open", "abc"]).permissions == "yolo"
    assert p.parse_args(["task", "open", "abc", "--plan"]).permissions == "plan"
    a = p.parse_args(["task", "yolo", "abc"])
    assert a.fn is cli_board.cmd_task_yolo and a.task_id == "abc"
    assert p.parse_args(["schedule", "add", "n", "--command", "/x"]).permissions == "yolo"
    s = p.parse_args(["schedule", "set", "n", "--permissions", "plan"])
    assert s.fn is cli_schedules.cmd_schedule_set and s.permissions == "plan"
    with pytest.raises(SystemExit):
        p.parse_args(["spawn", "x", "--plan", "--yolo"])


class _Client:
    def __init__(self, tasks=(), schedules=()):
        self._tasks, self._schedules, self.calls = list(tasks), list(schedules), []

    def tasks(self, domain):
        return self._tasks

    def schedules(self):
        return self._schedules

    def patch_task(self, task_id, **changes):
        self.calls.append(("patch", task_id, changes))
        return {"id": task_id, "title": "t", "status": "backlog", "priority": "normal", **changes}

    def dispatch_task(self, task_id, force=False, permissions=None):
        self.calls.append(("dispatch", task_id, force, permissions))
        return {"message": "dispatched t", "run": {"id": "r" * 32, "permissions": permissions or "yolo"}}

    def yolo_task(self, task_id):
        self.calls.append(("yolo", task_id))
        if task_id == "nope":
            raise RuntimeError("owned by the owner, so promoting it would dispatch an agent")
        return {"task": {"id": "a" * 32, "title": "card a", "plan_approved": "2026-10-07T12:00:00Z"},
                "run": {"id": "r" * 32}, "message": "dispatched card a (pid 1)"}

    def put_schedule(self, name, **payload):
        self.calls.append(("put", name, payload))
        return {"name": name, **payload}


def _args(**kw):
    return type("A", (), kw)()


def test_task_set_pins_and_derive_clears(capsys):
    c = _Client()
    base = dict(task_id="abc", title=None, priority=None, domain=None, detail=None,
                detail_file=None, agent=None, due=None, append=False)
    assert cli_board.cmd_task_set(_args(**base, permissions="plan"), c) == 0
    assert c.calls[-1] == ("patch", "abc", {"permissions": "plan"})
    assert cli_board.cmd_task_set(_args(**base, permissions="derive"), c) == 0
    assert c.calls[-1] == ("patch", "abc", {"permissions": ""})


def test_task_run_and_yolo_commands(capsys):
    c = _Client()
    assert cli_board.cmd_task_run(_args(task_id="abc", force=False, permissions="plan"), c) == 0
    assert c.calls[-1] == ("dispatch", "abc", False, "plan")
    assert "[plan]" in capsys.readouterr().out
    assert cli_board.cmd_task_yolo(_args(task_id="abc"), c) == 0
    out = capsys.readouterr().out
    assert "yolo  aaaaaa" in out and "plan approved 2026-10-07 12:00 UTC" in out
    assert cli_board.cmd_task_yolo(_args(task_id="nope"), c) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_schedule_set_keeps_everything_but_the_level(capsys):
    c = _Client(schedules=[{"name": "n", "command": "/x", "domain": "personal", "runner": "launch",
                            "description": "d", "enabled": False, "autostart": True,
                            "max_age_hours": 30, "permissions": "yolo",
                            "cadence": {"kind": "weekly", "at": "09:00", "days": ["mon"],
                                        "hours": None, "min_interval_days": 2}}])
    assert cli_schedules.cmd_schedule_set(_args(name="n", permissions="plan"), c) == 0
    _, name, payload = c.calls[-1]
    assert name == "n" and payload["permissions"] == "plan"
    assert (payload["command"], payload["domain"], payload["runner"], payload["enabled"],
            payload["autostart"], payload["max_age_hours"], payload["kind"], payload["at"],
            payload["days"], payload["min_interval_days"]) == \
        ("/x", "personal", "launch", False, True, 30, "weekly", "09:00", ["mon"], 2)
    assert cli_schedules.cmd_schedule_set(_args(name="zz", permissions="plan"), c) == 2


# ---- the guard: no spawn site may drop the argument ------------------------------------

def test_every_spawn_site_names_its_level():
    missing = []
    for path in sorted((REPO / "otto").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"detached\.spawn\(", src):
            line = src[src.rfind(chr(10), 0, m.start()) + 1:m.start()]
            if line.lstrip().startswith("#") or src[m.end():m.end() + 1] == ")":
                continue  # prose that names the function, not a call
            depth, i = 0, m.end() - 1
            while i < len(src):
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                if depth == 0:
                    break
                i += 1
            if "permissions=" not in src[m.start():i]:
                missing.append(f"{path.relative_to(REPO)}:{src.count(chr(10), 0, m.start()) + 1}")
    assert not missing, f"spawn without a level: {missing}"
