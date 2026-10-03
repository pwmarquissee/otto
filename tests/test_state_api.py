"""GET /api/state after the 2026-10-02 performance pass: version-keyed cache, ETag
and 304, gzip, and the trimmed task detail.

Runs the real route on the real daemon app, with `daemon.store` swapped for a
sandbox Store per test so a task written here lands nowhere else. The lifespan is
never started (no `with TestClient(...)`), so no tick thread and no herdr.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from otto import config
from otto import daemon as daemon_mod
from otto.store import Store

from conftest import make_task

PORT = config.PORT  # 8799 under conftest
OWN = f"http://127.0.0.1:{PORT}"
IDENTITY = {"Accept-Encoding": "identity"}
GZIP = {"Accept-Encoding": "gzip"}
LONG = "x" * 2000


@pytest.fixture
def api(tmp_path, daemon, monkeypatch):
    """The daemon app over a fresh store, with the state cache emptied."""
    fresh = Store(tmp_path / "state")
    monkeypatch.setattr(daemon_mod, "store", fresh)
    monkeypatch.setattr(daemon_mod, "_state_cache", None)
    client = TestClient(daemon_mod.app, base_url=OWN)
    client.store = fresh
    return client


def _tasks(n: int, detail: str = LONG) -> list:
    return [make_task(id=f"{i:032x}", title=f"task {i}", detail=detail) for i in range(n)]


# ---- ETag and revalidation -----------------------------------------------------

def test_state_carries_a_strong_etag_and_no_cache(api):
    r = api.get("/api/state", headers=IDENTITY)
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and len(etag) == 42  # sha1 hex, quoted
    assert r.headers["cache-control"] == "no-cache"
    assert "content-encoding" not in r.headers
    assert r.json()["persona"] == config.PERSONA_NAME


def test_matching_if_none_match_is_a_bodyless_304(api):
    first = api.get("/api/state", headers=IDENTITY)
    etag = first.headers["etag"]
    r = api.get("/api/state", headers={**IDENTITY, "If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag
    assert r.headers["cache-control"] == "no-cache"
    # The weak form and a list containing the tag both count, per RFC 7232.
    assert api.get("/api/state", headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    assert api.get("/api/state", headers={"If-None-Match": f'"stale", {etag}'}).status_code == 304
    assert api.get("/api/state", headers={"If-None-Match": "*"}).status_code == 304


def test_stale_if_none_match_gets_the_full_body(api):
    api.get("/api/state", headers=IDENTITY)
    r = api.get("/api/state", headers={**IDENTITY, "If-None-Match": '"not-the-tag"'})
    assert r.status_code == 200
    assert len(r.content) > 0


# ---- gzip -------------------------------------------------------------------------

def test_state_is_gzipped_for_a_client_that_accepts_it(api):
    api.store.save_tasks(_tasks(20))
    plain = api.get("/api/state", headers=IDENTITY)
    zipped = api.get("/api/state", headers=GZIP)
    assert zipped.status_code == 200
    assert zipped.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in zipped.headers["vary"].lower()
    # httpx has already inflated .content; the wire size is what shrank.
    assert zipped.num_bytes_downloaded < len(plain.content) / 3
    assert zipped.json() == plain.json()
    assert zipped.headers["etag"] == plain.headers["etag"]


def test_other_json_routes_are_gzipped_by_the_middleware(api):
    api.store.save_tasks(_tasks(20))
    plain = api.get("/api/tasks", headers=IDENTITY)
    zipped = api.get("/api/tasks", headers=GZIP)
    assert "content-encoding" not in plain.headers
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.num_bytes_downloaded < len(plain.content) / 3


def test_small_bodies_are_left_alone(api):
    r = api.get("/api/health", headers=GZIP)
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


# ---- the trimmed payload --------------------------------------------------------

def test_long_detail_is_cut_to_600_and_flagged_in_state_only(api):
    long_id, short_id = "a" * 32, "b" * 32
    api.store.save_tasks([make_task(id=long_id, detail=LONG),
                          make_task(id=short_id, detail="short")])
    s = api.get("/api/state", headers=IDENTITY).json()
    by_id = {t["id"]: t for t in s["tasks"]}
    assert len(by_id[long_id]["detail"]) == config.STATE_DETAIL_CHARS == 600
    assert by_id[long_id]["detail"] == LONG[:600]
    assert by_id[long_id]["detail_truncated"] is True
    assert by_id[short_id]["detail"] == "short"
    assert "detail_truncated" not in by_id[short_id]
    # The board's cards carry the same cut and the same flag.
    cards = {c["id"]: c for col in s["board"]["columns"] for c in col["cards"]}
    assert len(cards[long_id]["detail"]) == 600
    assert cards[long_id]["detail_truncated"] is True
    assert "detail_truncated" not in cards[short_id]
    # The inspector's route returns the whole thing, unflagged.
    full = api.get(f"/api/tasks/{long_id}").json()
    assert full["detail"] == LONG
    assert "detail_truncated" not in full
    # Every other task field survives the trim.
    assert set(full) - {"run"} <= set(by_id[long_id])


def test_runs_and_events_keep_their_limits(api):
    for i in range(config.STATE_EVENTS + 15):
        api.store.log(f"event {i}", source="test")
    s = api.get("/api/state", headers=IDENTITY).json()
    assert len(s["events"]) == config.STATE_EVENTS == 60
    assert config.STATE_RUNS == 80


# ---- the version-keyed cache ----------------------------------------------------

def test_a_write_shows_up_on_the_very_next_poll(api):
    before = api.get("/api/state", headers=IDENTITY)
    assert before.json()["tasks"] == []
    api.store.save_tasks([make_task(title="brand new")])
    after = api.get("/api/state", headers=IDENTITY)
    assert [t["title"] for t in after.json()["tasks"]] == ["brand new"]
    assert after.headers["etag"] != before.headers["etag"]


def test_idle_polls_are_served_from_cache(api, monkeypatch):
    monkeypatch.setattr(config, "STATE_IDLE_CACHE_SECONDS", 60.0)
    a = api.get("/api/state", headers=IDENTITY)
    built = daemon_mod._state_cache.built_at
    b = api.get("/api/state", headers=IDENTITY)
    assert daemon_mod._state_cache.built_at == built
    assert a.content == b.content
    assert a.headers["etag"] == b.headers["etag"]
    # `at` is the computation time, so identical bytes prove no recompute.
    assert json.loads(a.content)["at"] == json.loads(b.content)["at"]


def test_idle_bound_forces_a_recompute(api, monkeypatch):
    monkeypatch.setattr(config, "STATE_IDLE_CACHE_SECONDS", 0.0)
    api.get("/api/state", headers=IDENTITY)
    built = daemon_mod._state_cache.built_at
    api.get("/api/state", headers=IDENTITY)
    assert daemon_mod._state_cache.built_at > built


def test_cache_is_stamped_with_the_version_it_read(api):
    api.store.save_tasks([make_task()])
    api.get("/api/state", headers=IDENTITY)
    assert daemon_mod._state_cache.version == api.store.version
