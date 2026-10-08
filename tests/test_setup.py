"""First-run setup: the settings file (otto/settings.py), the step engine
(otto/setup.py), and the /api/setup routes.

The settings file is the one piece of configuration Otto writes for itself, so
the properties that matter are precedence (environment over file over default),
the key allowlist (OTTO_* only, and known to .env.example), round-tripping with
comments preserved, and masked secrets never being written back. The step engine
is checked on an empty store, which is what a fresh install is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otto import config, settings, setup
from otto import daemon as daemon_mod
from otto.store import Store

from conftest import make_task

OWN = f"http://127.0.0.1:{config.PORT}"


# ---- settings file -------------------------------------------------------------

def test_parse_handles_comments_quotes_and_inline_comments():
    text = (
        "# header\n"
        "\n"
        "OTTO_OWNER_NAME=Alex Example                # how prompts refer to the owner\n"
        'OTTO_ORG_NAME="Example #1 Studio"\n'
        "OTTO_WORK_ROOTS='C:\\work;D:\\repos'\n"
        "export OTTO_TICK=30\n"
        "PATH=/evil\n"
        "not a line\n"
    )
    got = settings.parse(text)
    assert got == {
        "OTTO_OWNER_NAME": "Alex Example",
        "OTTO_ORG_NAME": "Example #1 Studio",
        "OTTO_WORK_ROOTS": "C:\\work;D:\\repos",
        "OTTO_TICK": "30",
    }
    assert "PATH" not in got


def test_write_preserves_other_lines_and_appends_new_keys(tmp_path):
    f = tmp_path / "otto.env"
    f.write_text("# mine\nOTTO_TICK=15   # keep this comment line shape\nOTTO_PORT=8787\n", encoding="utf-8")
    written, removed = settings.write({"OTTO_TICK": "30", "OTTO_PORT": None, "OTTO_OWNER_NAME": "Alex"}, f)
    assert written == ["OTTO_TICK", "OTTO_OWNER_NAME"]
    assert removed == ["OTTO_PORT"]
    lines = f.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# mine"
    assert "OTTO_TICK=30" in lines
    assert not any(line.startswith("OTTO_PORT") for line in lines)
    assert settings.SECTION_HEADER in lines
    assert settings.read(f) == {"OTTO_TICK": "30", "OTTO_OWNER_NAME": "Alex"}


def test_write_round_trips_awkward_values(tmp_path):
    f = tmp_path / "otto.env"
    values = {"OTTO_ORG_NAME": 'Quote "Inc" #1', "OTTO_WORK_ROOTS": "C:\\a b;D:\\c", "OTTO_OWNER_NAME": ""}
    settings.write(values, f)
    assert settings.read(f) == values


def test_write_refuses_non_otto_keys(tmp_path):
    with pytest.raises(ValueError):
        settings.write({"PATH": "/evil"}, tmp_path / "otto.env")
    with pytest.raises(ValueError):
        settings.write({"otto_lower": "x"}, tmp_path / "otto.env")
    assert not (tmp_path / "otto.env").exists()


def test_apply_lets_the_environment_win(tmp_path, monkeypatch):
    f = tmp_path / "otto.env"
    settings.write({"OTTO_OWNER_NAME": "FromFile", "OTTO_ORG_NAME": "FileOrg"}, f)
    monkeypatch.setenv("OTTO_OWNER_NAME", "FromEnv")
    monkeypatch.delenv("OTTO_ORG_NAME", raising=False)
    applied = settings.apply(f)
    assert applied == ["OTTO_ORG_NAME"]
    assert os.environ["OTTO_OWNER_NAME"] == "FromEnv"
    assert os.environ["OTTO_ORG_NAME"] == "FileOrg"
    monkeypatch.delenv("OTTO_ORG_NAME")


def test_describe_masks_secrets_and_reports_overrides(tmp_path, monkeypatch):
    f = tmp_path / "otto.env"
    settings.write({"OTTO_SLACK_BOT_TOKEN": "xoxb-real", "OTTO_OWNER_NAME": "Alex"}, f)
    monkeypatch.setenv("OTTO_OWNER_NAME", "Someone Else")
    d = settings.describe(f)
    assert d["values"]["OTTO_SLACK_BOT_TOKEN"] == settings.MASK
    assert d["values"]["OTTO_OWNER_NAME"] == "Alex"
    assert d["env_overrides"] == ["OTTO_OWNER_NAME"]
    assert settings.read(f)["OTTO_SLACK_BOT_TOKEN"] == "xoxb-real"


def test_known_keys_come_from_env_example():
    keys = settings.known_keys()
    assert "OTTO_OWNER_NAME" in keys
    assert "OTTO_INTEGRATIONS" in keys
    assert "OTTO_WORK_ROOTS" in keys


def test_config_loaded_settings_from_the_sandbox_home():
    # conftest points OTTO_HOME at a sandbox, so the path must sit under it and
    # nothing under the real home may have been read at import.
    assert config.SETTINGS_PATH == config.OTTO_HOME / settings.FILE_NAME
    assert config.SETTINGS_PATH != Path.home() / ".claude" / "otto" / settings.FILE_NAME


def _disarmed_seeds():
    """The seeded schedules as a fresh install sees them. Whether seeds start armed
    is a config choice (SCHEDULES_ARMED_BY_DEFAULT) that differs between installs;
    these tests are about the setup step, so they fix the baseline themselves."""
    from otto.runners import scheduled
    items = scheduled.default_schedules()
    for item in items:
        item.autostart = False
    return items


# ---- step engine -------------------------------------------------------------------

@pytest.fixture
def fresh(tmp_path, daemon, monkeypatch):
    """An empty store and an unwritten settings file: a brand-new install.

    The fresh-install position is pinned through the resolver's own defaults and
    a reload, not by overwriting the resolved values: setup.write_settings
    reloads config, and a value pinned by assignment would not survive that.
    The settings file is this test's own; a no-argument reload keeps reading it."""
    store = Store(tmp_path / "state")
    monkeypatch.setattr(config, "_DEFAULT_OWNER_NAME", setup.DEFAULT_OWNER)
    monkeypatch.setattr(config, "_DEFAULT_ORG_NAME", setup.DEFAULT_ORG)
    monkeypatch.setattr(config, "_DEFAULT_WORK_ROOTS", ())
    monkeypatch.setattr(config, "_DEFAULT_PERSONAL_ROOTS", ())
    for key in ("OTTO_OWNER_NAME", "OTTO_ORG_NAME", "OTTO_WORK_ROOTS", "OTTO_PERSONAL_ROOTS",
                "OTTO_INTEGRATIONS"):
        monkeypatch.delenv(key, raising=False)
    config.reload(path=tmp_path / "otto.env")
    assert config.OWNER_NAME == setup.DEFAULT_OWNER and config.WORK_ROOTS == ()
    assert config.INTEGRATIONS == () and config.INTEGRATIONS_SET is False
    # A machine with herdr installed and running would make that step "done" and
    # hide what the skip tests check. A fresh install has neither.
    from otto import herdr
    monkeypatch.setattr(herdr, "available", lambda: False)
    monkeypatch.setattr(herdr, "server_running", lambda: False)
    return store


def test_fresh_install_view(fresh):
    v = setup.view(fresh)
    ids = [s["id"] for s in v["steps"]]
    assert ids == list(setup.STEP_IDS)
    by = {s["id"]: s for s in v["steps"]}
    assert v["complete"] is False
    assert v["restart_needed"] is False
    assert by["daemon"]["status"] == "done"
    assert by["identity"]["status"] == "todo"
    assert by["identity"]["required"] is True
    assert by["integrations"]["status"] == "todo"
    assert by["integrations"]["required"] is False
    assert by["first_card"]["status"] == "todo"
    assert by["finish"]["status"] == "todo"
    assert v["progress"]["total"] == len(setup.STEP_IDS) - 1
    assert v["settings"]["exists"] is False
    # Every step carries the fields the dashboard renders from.
    for s in v["steps"]:
        assert s["action"]["kind"] in ("form", "button", "choice", "none")
        assert s["summary"] and s["detail"]


def test_identity_validation_rejects_missing_roots(tmp_path):
    with pytest.raises(ValueError):
        setup.validate_identity({"OTTO_WORK_ROOTS": str(tmp_path / "nope")})
    good = setup.validate_identity({
        "OTTO_OWNER_NAME": " Alex ",
        "OTTO_WORK_ROOTS": f"{tmp_path}\n{tmp_path}",
    })
    assert good["OTTO_OWNER_NAME"] == "Alex"
    assert good["OTTO_WORK_ROOTS"] == os.pathsep.join([str(tmp_path), str(tmp_path)])


def test_write_settings_applies_live_and_skips_masked_values(fresh):
    """A write reloads config, so a key read at call time is live at once and the
    step reads done, not restart. (It used to flag every write for a restart,
    because config was import-time.)"""
    out = setup.write_settings(fresh, {"OTTO_OWNER_NAME": "Alex", "OTTO_SLACK_BOT_TOKEN": settings.MASK})
    assert out["written"] == ["OTTO_OWNER_NAME"]
    assert out["live"] == ["OTTO_OWNER_NAME"] and out["restart"] == []
    assert out["restart_needed"] is False
    assert setup.restart_needed(fresh) is False
    assert config.OWNER_NAME == "Alex"
    assert "OTTO_SLACK_BOT_TOKEN" not in settings.read(config.SETTINGS_PATH)
    by = {s["id"]: s for s in setup.steps(fresh)}
    assert by["identity"]["status"] == "todo"  # roots still missing
    setup.write_settings(fresh, {"OTTO_WORK_ROOTS": str(config.SETTINGS_PATH.parent)})
    assert config.WORK_ROOTS == (config.SETTINGS_PATH.parent,)
    by = {s["id"]: s for s in setup.steps(fresh)}
    assert by["identity"]["status"] == "done"


def test_write_settings_flags_restart_only_for_bound_keys(fresh, monkeypatch):
    """The tick period is bound when the daemon imports; writing it reloads config
    like anything else but reports the restart, and the flag sticks for this pid.
    (Not the port: conftest pins OTTO_PORT in the environment, which beats the file.)"""
    monkeypatch.delenv("OTTO_TICK", raising=False)
    out = setup.write_settings(fresh, {"OTTO_TICK": "9", "OTTO_OWNER_NAME": "Alex"})
    assert out["restart"] == ["OTTO_TICK"] and out["live"] == ["OTTO_OWNER_NAME"]
    assert out["restart_needed"] is True
    assert setup.restart_needed(fresh) is True
    assert config.TICK_SECONDS == 9, "resolved too; the objects built from the old value are what wait"
    assert setup.view(fresh)["restart_needed"] is True


def test_write_settings_rejects_unknown_keys(fresh):
    with pytest.raises(ValueError):
        setup.write_settings(fresh, {"OTTO_NOT_A_THING": "x"})


def test_skip_complete_reset_and_summary(fresh):
    setup.skip(fresh, "herdr")
    by = {s["id"]: s for s in setup.steps(fresh)}
    assert by["herdr"]["status"] == "skipped"
    summ = setup.summary(fresh)
    assert summ["complete"] is False
    assert summ["done"] >= 2  # daemon plus the skip
    setup.skip(fresh, "herdr", undo=True)
    assert {s["id"]: s for s in setup.steps(fresh)}["herdr"]["status"] != "skipped"
    with pytest.raises(ValueError):
        setup.skip(fresh, "daemon")
    setup.complete(fresh)
    assert setup.is_complete(fresh)
    assert setup.summary(fresh)["complete"] is True
    setup.reset(fresh)
    assert not setup.is_complete(fresh)


def test_first_card_and_schedules_steps_read_the_store(fresh):
    fresh.save_tasks([make_task(title="Rotate the staging password")])
    by = {s["id"]: s for s in setup.steps(fresh)}
    assert by["first_card"]["status"] == "done"
    assert "1 card" in by["first_card"]["summary"]
    fresh.save_schedules(_disarmed_seeds())
    by = {s["id"]: s for s in setup.steps(fresh)}
    names = [o["value"] for o in by["schedules"]["action"]["options"]]
    assert names, "the seeded read-only schedules must be offered"
    assert "inbox-sync" in names
    assert by["schedules"]["status"] == "todo"


def test_migrate_marks_a_lived_in_install_complete_and_leaves_a_fresh_one_alone(fresh):
    assert setup.migrate(fresh) is False, "an empty store is a first run"
    assert not setup.is_complete(fresh)
    fresh.save_tasks([make_task(title="old work")])
    assert setup.migrate(fresh) is True
    assert setup.is_complete(fresh)
    assert fresh.setup().get("migrated") is True
    assert setup.migrate(fresh) is False, "never rewrites an existing record"
    setup.reset(fresh)
    assert setup.migrate(fresh) is False, "a reset is a record too; the person asked for the flow"


# ---- suppression while setup is incomplete --------------------------------------------

def test_unconfigured_integrations_stay_off_the_board_until_setup_finishes(fresh):
    from otto import board
    from otto.models import Integration
    fresh.save_integrations([
        Integration(name="aws", ok=False, mode="unconfigured", detail="aws CLI not on PATH"),
        Integration(name="anthropic", ok=False, mode="api", detail="key rejected (401)"),
    ])
    ids = {c.id for c in board.derived_cards(fresh)}
    assert "integration:aws" not in ids
    assert "integration:anthropic" in ids, "a real failure is never hidden"
    setup.complete(fresh)
    ids = {c.id for c in board.derived_cards(fresh)}
    assert "integration:aws" in ids


def test_probe_filter_honors_the_integrations_setting(monkeypatch):
    """Unset is the same as none: a probe is outbound traffic, and an unconfigured
    daemon talks to nothing (it used to mean every probe)."""
    from otto.runners import external
    monkeypatch.setattr(config, "INTEGRATIONS", ())
    monkeypatch.setattr(config, "INTEGRATIONS_SET", False)
    assert external.enabled_probes() == []
    monkeypatch.setattr(config, "INTEGRATIONS", ("aws",))
    assert [fn.__name__ for fn in external.enabled_probes()] == ["probe_aws"]
    monkeypatch.setattr(config, "INTEGRATIONS", ())
    assert external.enabled_probes() == []
    assert external.probe_all() == []


def test_pin_integrations_keeps_a_pre_setting_install_probing(fresh, monkeypatch):
    """An install that predates OTTO_INTEGRATIONS ran every probe. The first start on
    this code writes that full list into otto.env for it, once, and applies it in
    process. A fresh install gets nothing written: its setup step asks."""
    from otto.runners import external
    from otto import settings
    assert setup.pin_integrations(fresh) is False, "fresh install: the step asks"
    assert "OTTO_INTEGRATIONS" not in settings.read(config.SETTINGS_PATH)

    fresh.put_setup({"completed_at": "2026-01-01T00:00:00Z", "migrated": True})
    assert setup.pin_integrations(fresh) is True
    filed = settings.read(config.SETTINGS_PATH)["OTTO_INTEGRATIONS"]
    assert filed.split(",") == external.probe_names()
    assert config.INTEGRATIONS == tuple(external.probe_names())
    assert config.INTEGRATIONS_SET is True
    assert len(external.enabled_probes()) == len(external.PROBES)
    assert setup.pin_integrations(fresh) is False, "written once"

    monkeypatch.setattr(config, "INTEGRATIONS", ("aws",))
    monkeypatch.setattr(config, "INTEGRATIONS_SET", True)
    assert setup.pin_integrations(fresh) is False, "a set value is never overwritten"


# ---- routes ----------------------------------------------------------------------

@pytest.fixture
def api(fresh, monkeypatch):
    monkeypatch.setattr(daemon_mod, "store", fresh)
    monkeypatch.setattr(daemon_mod, "_state_cache", None)
    client = TestClient(daemon_mod.app, base_url=OWN)
    client.store = fresh
    return client


def test_get_setup_and_state_summary(api):
    v = api.get("/api/setup").json()
    assert v["complete"] is False
    assert [s["id"] for s in v["steps"]] == list(setup.STEP_IDS)
    st = api.get("/api/state").json()
    assert st["setup"]["complete"] is False
    assert st["setup"]["total"] == len(setup.STEP_IDS) - 1
    assert "restart_needed" in st["setup"]


def test_settings_route_validates_and_writes(api, tmp_path):
    r = api.post("/api/setup/settings", json={"values": {"OTTO_WORK_ROOTS": str(tmp_path / "missing")}})
    assert r.status_code == 400
    assert "not a directory" in r.json()["detail"]
    r = api.post("/api/setup/settings", json={"values": {"PATH": "x"}})
    assert r.status_code == 400
    r = api.post("/api/setup/settings", json={"values": {"OTTO_INTEGRATIONS": "aws,bogus"}})
    assert r.status_code == 400
    r = api.post("/api/setup/settings", json={"values": {
        "OTTO_OWNER_NAME": "Alex", "OTTO_WORK_ROOTS": f"{tmp_path}\n", "OTTO_INTEGRATIONS": "AWS, notion"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["written"]) == {"OTTO_OWNER_NAME", "OTTO_WORK_ROOTS", "OTTO_INTEGRATIONS"}
    assert body["restart_needed"] is False, "none of these is bound at daemon build"
    assert config.INTEGRATIONS == ("aws", "notion"), "live in the process that wrote it"
    filed = settings.read(config.SETTINGS_PATH)
    assert filed["OTTO_INTEGRATIONS"] == "aws,notion"
    assert filed["OTTO_WORK_ROOTS"] == str(tmp_path)
    v = api.get("/api/setup").json()
    assert v["restart_needed"] is False
    assert v["settings"]["exists"] is True
    r = api.post("/api/setup/settings", json={"values": {"OTTO_INTEGRATIONS": "none"}})
    assert settings.read(config.SETTINGS_PATH)["OTTO_INTEGRATIONS"] == "none"


def test_skip_complete_reset_routes(api):
    r = api.post("/api/setup/skip", json={"step": "daemon"})
    assert r.status_code == 400
    v = api.post("/api/setup/skip", json={"step": "integrations"}).json()
    assert {s["id"]: s for s in v["steps"]}["integrations"]["status"] == "skipped"
    v = api.post("/api/setup/unskip", json={"step": "integrations"}).json()
    assert {s["id"]: s for s in v["steps"]}["integrations"]["status"] == "todo"
    v = api.post("/api/setup/complete").json()
    assert v["complete"] is True and v["completed_at"]
    assert api.get("/api/state").json()["setup"]["complete"] is True
    v = api.post("/api/setup/reset").json()
    assert v["complete"] is False


def test_first_card_route_files_a_backlog_card(api):
    r = api.post("/api/setup/first-card", json={"title": "  ", "detail": None})
    assert r.status_code == 400
    r = api.post("/api/setup/first-card", json={"title": "Rotate the staging password", "detail": "By Friday"})
    assert r.status_code == 200, r.text
    tasks = api.store.tasks()
    assert len(tasks) == 1
    assert tasks[0].status == "backlog"
    assert tasks[0].detail == "By Friday"
    assert {s["id"]: s for s in api.get("/api/setup").json()["steps"]}["first_card"]["status"] == "done"


def test_schedules_route_arms_only_named_schedules(api):
    api.store.save_schedules(_disarmed_seeds())
    r = api.post("/api/setup/schedules", json={"arm": ["../evil"]})
    assert r.status_code == 400
    r = api.post("/api/setup/schedules", json={"arm": ["no-such-schedule"]})
    assert r.status_code == 404
    r = api.post("/api/setup/schedules", json={"arm": ["inbox-sync"]})
    assert r.status_code == 200, r.text
    assert r.json()["armed"] == ["inbox-sync"]
    armed = {s.name for s in api.store.schedules() if s.autostart}
    assert armed == {"inbox-sync"}
    assert {s["id"]: s for s in api.get("/api/setup").json()["steps"]}["schedules"]["status"] == "done"


def test_hooks_route_writes_the_claude_settings_file(api, tmp_path, monkeypatch):
    from otto import sessions
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"permissions": {"allow": ["Bash(git status)"]}}), encoding="utf-8")
    monkeypatch.setattr(sessions, "GLOBAL_SETTINGS", target)
    r = api.post("/api/setup/hooks/install")
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(git status)"], "existing grants survive"
    assert data["hooks"]
    assert sessions.installed_in(target)
    r = api.post("/api/setup/hooks/install")
    assert r.json()["changed"] is False
    assert {s["id"]: s for s in api.get("/api/setup").json()["steps"]}["hooks"]["status"] == "done"


def test_restart_helper_argv_waits_for_this_pid():
    argv = daemon_mod._restart_helper_argv(4242)
    assert argv[1:] == ["-m", "otto", "ensure", "--wait-pid", "4242", "--quiet"]


def test_restart_route_spawns_helper_and_schedules_exit(api, monkeypatch):
    import subprocess
    import threading
    spawned = {}
    started = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            spawned["argv"] = argv
            spawned["kw"] = kw

    class FakeThread:
        def __init__(self, *a, **kw):
            started["target"] = kw.get("target")
            started["args"] = kw.get("args")

        def start(self):
            started["started"] = True

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(threading, "Thread", FakeThread)
    r = api.post("/api/daemon/restart")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["pid"] == os.getpid()
    assert spawned["argv"][-3:] == ["--wait-pid", str(os.getpid()), "--quiet"]
    assert spawned["kw"]["env"].get("NO_COLOR") in (None, "") or "NO_COLOR" not in spawned["kw"]["env"]
    assert started["started"] is True
    assert started["target"] is daemon_mod._exit_after
