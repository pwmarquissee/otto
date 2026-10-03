"""The change socket (otto/web_events.py) and the counter it watches (Store.version).

The contract under test, shared with otto/web/app.js:
  on connect  {"t":"hello","v":N}
  on a write  {"t":"changed","v":N}
  on idle     {"t":"ping"}
and the OriginGuard refuses a foreign page's handshake on this socket like any other.

The bare-app tests mount the route over a sandbox Store so a write here cannot
touch anything real; the daemon-app tests only open the socket and never start
the lifespan (no `with TestClient(...)`), so no tick thread and no herdr.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from otto import config, daemon, web_events
from otto.models import Event
from otto.originguard import OriginGuard
from otto.store import Store

from conftest import make_task

PORT = config.PORT  # 8799 under conftest
OWN = f"http://127.0.0.1:{PORT}"
WS_OWN = f"ws://127.0.0.1:{PORT}"
EVIL = "https://evil.example"


# ---- the counter ---------------------------------------------------------------

def test_fresh_store_starts_at_version_zero(store):
    assert store.version == 0
    assert store.version_at == 0.0


def test_every_state_write_bumps_the_version(store):
    before = store.version
    store.save_tasks([make_task()])
    assert store.version == before + 1
    assert store.version_at > 0
    at = store.version_at
    store.save_tasks([make_task(), make_task(id="b" * 32)])
    assert store.version == before + 2
    assert store.version_at >= at


def test_an_event_append_bumps_the_version(store):
    before = store.version
    store.log("something happened", source="test")
    assert store.version == before + 1


def test_telemetry_does_not_bump_the_version(store):
    # Nothing in /api/state reads telemetry, and a bump per export batch from
    # every live session would invalidate the state cache for no visible change.
    before = store.version
    assert store.append_telemetry([{"k": 1}, {"k": 2}]) == 2
    assert store.version == before


def test_a_refused_write_leaves_the_version_alone(reader_store):
    from otto.store import WriteDenied

    with pytest.raises(WriteDenied):
        reader_store.save_tasks([make_task()])
    assert reader_store.version == 0


# ---- the socket, on a bare app over a sandbox store ----------------------------

@pytest.fixture
def ws_app(store):
    app = FastAPI()
    app.add_middleware(OriginGuard, allowed_origins=config.ALLOWED_ORIGINS,
                       allowed_hosts=config.ALLOWED_HOSTS)
    web_events.register(app, store)
    return TestClient(app, base_url=OWN)


def test_hello_carries_the_current_version(ws_app, store):
    store.save_tasks([make_task()])
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        assert ws.receive_json() == {"t": "hello", "v": store.version}


def test_a_write_is_announced_as_changed(ws_app, store):
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        hello = ws.receive_json()
        store.save_tasks([make_task()])
        msg = ws.receive_json()
        assert msg == {"t": "changed", "v": store.version}
        assert msg["v"] > hello["v"]


def test_an_event_append_is_announced_too(ws_app, store):
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        ws.receive_json()
        store.append_event(Event(level="info", source="test", message="hi"))
        assert ws.receive_json() == {"t": "changed", "v": store.version}


def test_nothing_is_sent_while_nothing_changes(ws_app, store, monkeypatch):
    # Pings are the only idle traffic; with the ping interval far away, a short
    # wait must produce no message at all.
    monkeypatch.setattr(config, "WS_EVENTS_PING_SECONDS", 3600.0)
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        ws.receive_json()
        store.save_tasks([make_task()])
        assert ws.receive_json()["t"] == "changed"
        # A second read now would block: prove the queue is empty by writing
        # again and checking the very next message is that write, not a stale one.
        store.save_tasks([make_task(), make_task(id="c" * 32)])
        assert ws.receive_json() == {"t": "changed", "v": store.version}


def test_idle_socket_gets_pings(ws_app, monkeypatch):
    monkeypatch.setattr(config, "WS_EVENTS_PING_SECONDS", 0.05)
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        assert ws.receive_json()["t"] == "hello"
        assert ws.receive_json() == {"t": "ping"}
        assert ws.receive_json() == {"t": "ping"}


def test_client_messages_are_ignored_and_the_socket_stays_up(ws_app, store):
    with ws_app.websocket_connect(f"{WS_OWN}/ws/events") as ws:
        ws.receive_json()
        ws.send_text("not part of the contract")
        ws.send_json({"t": "whatever"})
        store.save_tasks([make_task()])
        assert ws.receive_json() == {"t": "changed", "v": store.version}


def test_foreign_origin_is_refused_on_the_bare_app(ws_app):
    with pytest.raises(WebSocketDisconnect):
        with ws_app.websocket_connect(f"{WS_OWN}/ws/events", headers={"Origin": EVIL}):
            pass


# ---- the socket, on the real daemon app ----------------------------------------

def test_daemon_app_mounts_the_socket_and_refuses_a_foreign_origin():
    c = TestClient(daemon.app, base_url=OWN)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"{WS_OWN}/ws/events", headers={"Origin": EVIL}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"{WS_OWN}/ws/events", headers={"Origin": "null"}):
            pass


def test_daemon_app_greets_its_own_origin_with_the_daemon_stores_version():
    c = TestClient(daemon.app, base_url=OWN)
    with c.websocket_connect(f"{WS_OWN}/ws/events", headers={"Origin": OWN}) as ws:
        assert ws.receive_json() == {"t": "hello", "v": daemon.store.version}
