"""config is a settings object: resolved from the environment and the settings file,
re-resolvable with config.reload(), readable as plain `config.X` everywhere.

What a reload cannot do is rebuild the objects other modules made from the old
values (the daemon's Store, the app's origin guard); those keys are RESTART_KEYS
and setup.write_settings reports them. The `settings` fixture and the autouse
settle in conftest are the test-side contract.
"""

from __future__ import annotations

import os
from pathlib import Path

from otto import config, setup
from otto import settings as settings_mod


def test_reload_picks_up_a_changed_environment(settings):
    assert config.TICK_SECONDS == 15, "the suite's own environment sets no tick"
    s = settings(OTTO_TICK="42")
    assert config.TICK_SECONDS == 42
    assert s is config.current() and s.TICK_SECONDS == 42


def test_the_previous_test_was_settled():
    """Order-independent: every test starts from the suite's environment, because the
    autouse fixture reloads after one that reloaded."""
    assert config.TICK_SECONDS == 15
    assert "OTTO_TICK" not in os.environ


def test_a_changed_file_value_lands_on_reload(tmp_path, monkeypatch):
    monkeypatch.delenv("OTTO_TICK", raising=False)
    env_file = tmp_path / "otto.env"
    env_file.write_text("OTTO_TICK=20\n", encoding="utf-8")
    config.reload(path=env_file)
    assert config.TICK_SECONDS == 20
    assert config.SETTINGS_PATH == env_file
    assert os.environ["OTTO_TICK"] == "20", "applied into the environment, so children inherit it"
    assert "OTTO_TICK" in config.SETTINGS_APPLIED

    env_file.write_text("OTTO_TICK=30\n", encoding="utf-8")
    config.reload()  # no arguments: the file Otto writes is the file it reads back
    assert config.TICK_SECONDS == 30 and os.environ["OTTO_TICK"] == "30"

    env_file.write_text("", encoding="utf-8")
    config.reload()
    assert config.TICK_SECONDS == 15, "a removed file value falls back to the default"
    assert "OTTO_TICK" not in os.environ, "and the previous apply is taken back out"


def test_a_real_environment_variable_beats_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_TICK", "7")
    env_file = tmp_path / "otto.env"
    env_file.write_text("OTTO_TICK=20\n", encoding="utf-8")
    config.reload(path=env_file)
    assert config.TICK_SECONDS == 7
    assert "OTTO_TICK" not in config.SETTINGS_APPLIED
    assert os.environ["OTTO_TICK"] == "7", "never overwritten by a file it did not write"


def test_resolve_with_a_mapping_touches_nothing(tmp_path):
    before = config.PORT
    s = config.resolve({"OTTO_HOME": str(tmp_path), "OTTO_PORT": "9999", "OTTO_TICK": "3"})
    assert (s.PORT, s.TICK_SECONDS, s.OTTO_HOME) == (9999, 3, tmp_path)
    assert s.SETTINGS_PATH == tmp_path / settings_mod.FILE_NAME
    assert s.SETTINGS_APPLIED == (), "a mapping is the whole truth; no file is applied"
    assert config.PORT == before and config.current() is not s
    assert "OTTO_PORT" not in os.environ or os.environ["OTTO_PORT"] != "9999"


def test_current_is_what_the_globals_hold():
    live = config.current()
    for name, value in vars(live).items():
        assert getattr(config, name) == value, name
    assert "OTTO_HOME" in vars(live) and "PORT" in vars(live)
    assert "_IS_DAEMON" not in vars(live), "process state is not a setting"


def test_restart_keys_are_real_keys():
    known = settings_mod.known_keys()
    if known:
        assert config.RESTART_KEYS <= known, config.RESTART_KEYS - known
    assert {"OTTO_HOME", "OTTO_PORT", "OTTO_SCOPE", "OTTO_TICK"} <= config.RESTART_KEYS


def test_write_settings_classifies_live_and_restart(store, tmp_path, monkeypatch):
    monkeypatch.delenv("OTTO_TICK", raising=False)
    monkeypatch.delenv("OTTO_OWNER_NAME", raising=False)
    config.reload(path=tmp_path / "otto.env")
    out = setup.write_settings(store, {"OTTO_OWNER_NAME": "Alex"})
    assert out == {"written": ["OTTO_OWNER_NAME"], "removed": [], "live": ["OTTO_OWNER_NAME"],
                   "restart": [], "restart_needed": False}
    assert config.OWNER_NAME == "Alex" and setup.restart_needed(store) is False

    out = setup.write_settings(store, {"OTTO_TICK": "9"})
    assert out["restart"] == ["OTTO_TICK"] and out["restart_needed"] is True
    assert config.TICK_SECONDS == 9, "resolved all the same; what waits is the object built from it"
    assert setup.restart_needed(store) is True

    out = setup.write_settings(store, {"OTTO_OWNER_NAME": None})
    assert out["removed"] == ["OTTO_OWNER_NAME"] and out["live"] == ["OTTO_OWNER_NAME"]
    assert config.OWNER_NAME == config._DEFAULT_OWNER_NAME


def test_helpers_read_the_live_values(settings, tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("work with me like this\n", encoding="utf-8")
    assert config.profile_text() != "work with me like this"
    settings(OTTO_PROFILE=str(profile))
    assert config.PROFILE_PATH == Path(profile)
    assert config.profile_text() == "work with me like this"
