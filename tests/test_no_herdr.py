"""A machine without herdr must still get a working dashboard.

herdr is optional (README, setup step 4). Every read path the dashboard takes has
to degrade to "no panes" rather than raise. CI found GET /api/state returning 500
on a runner without herdr because logistics.view asked for the live agent list and
herdr.snapshot() raises not_installed by design.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from otto import config, herdr
from otto import daemon as daemon_mod
from otto.store import Store

OWN = f"http://127.0.0.1:{config.PORT}"


@pytest.fixture
def no_herdr(monkeypatch):
    monkeypatch.setattr(herdr, "binary", lambda: None)
    monkeypatch.setattr(herdr, "_last", (None, 0.0))
    assert herdr.available() is False


@pytest.fixture
def api(tmp_path, daemon, monkeypatch, no_herdr):
    fresh = Store(tmp_path / "state")
    monkeypatch.setattr(daemon_mod, "store", fresh)
    monkeypatch.setattr(daemon_mod, "_state_cache", None)
    client = TestClient(daemon_mod.app, base_url=OWN)
    client.store = fresh
    return client


def test_read_helpers_are_empty_not_raising(no_herdr):
    assert herdr.server_running() is False
    assert herdr.snapshot_or_none() is None
    assert herdr.agents() == []
    assert herdr.panes() == []
    assert herdr.workspaces() == []
    assert herdr.last_snapshot() is None
    with pytest.raises(herdr.HerdrError):
        herdr.snapshot()  # the action side still fails loudly


def test_state_and_logistics_serve_without_herdr(api):
    r = api.get("/api/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["logistics"]["rail"] == []
    assert body["setup"]["complete"] is False
    r = api.get("/api/logistics")
    assert r.status_code == 200, r.text


def test_setup_names_herdr_as_optional_and_absent(api):
    step = {s["id"]: s for s in api.get("/api/setup").json()["steps"]}["herdr"]
    assert step["status"] == "todo"
    assert step["required"] is False
    assert "Not installed" in step["summary"]
    assert step["action"]["kind"] == "none"
    assert step["data"]["installed"] is False
