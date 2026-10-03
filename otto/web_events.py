"""web_events: one WebSocket that tells the dashboard when the store changed.

THE CONTRACT (shared with otto/web/app.js, do not change shapes, add fields only):

  WebSocket /ws/events

  server -> client (JSON text):
    {"t":"hello","v":<int>}      on connect: the store version right now
    {"t":"changed","v":<int>}    store.version moved; GET /api/state to see what
    {"t":"ping"}                 every WS_EVENTS_PING_SECONDS, keeps the socket open

  client -> server: nothing. Anything the client sends is read and ignored; the
  read exists so a closed socket is noticed.

  `v` is Store.version: a process-local counter bumped by every state write and
  every event append (otto/store.py). It restarts at 0 with the daemon, so a
  client compares `v` against the value from its own hello, never against a
  value it remembered across a reconnect.

WHY POLL THE COUNTER. The store is written from the tick thread and from
FastAPI's sync-route threadpool; the socket lives on the asyncio loop. A
Condition would need a loop-safe bridge per connection. Reading an int every
250 ms costs nothing and is the same latency the dashboard sees, so the simple
thing is the correct thing here.

WHY A SEPARATE MODULE. Same reason as web_term: daemon.py stays a two-line hook
(`web_events.register(app, store)`), and a test can mount the route on a bare
FastAPI app over a sandbox Store.

The OriginGuard middleware on the daemon app sees WebSocket handshakes too, so a
foreign page cannot subscribe (tests/test_web_events.py checks that on the real
app). A refused handshake closes before accept, which the browser sees as 403.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, WebSocket

from . import config
from .store import Store


def _versioned(store: Store) -> int:
    return int(store.version)


async def serve_events(ws: WebSocket, store: Store) -> None:
    """Run one subscriber until it disconnects."""
    await ws.accept()
    last = _versioned(store)
    await ws.send_json({"t": "hello", "v": last})
    # The only reason to read from the client: a disconnect arrives as a
    # "websocket.disconnect" message, and without a pending receive the loop
    # would learn of it only when a later send blew up.
    pending: asyncio.Future[Any] = asyncio.ensure_future(ws.receive())
    last_ping = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({pending},
                                         timeout=config.WS_EVENTS_POLL_SECONDS)
            if pending in done:
                message = pending.result()
                if message.get("type") == "websocket.disconnect":
                    return
                pending = asyncio.ensure_future(ws.receive())
            now = time.monotonic()
            v = _versioned(store)
            if v != last:
                last = v
                await ws.send_json({"t": "changed", "v": v})
            if now - last_ping >= config.WS_EVENTS_PING_SECONDS:
                last_ping = now
                await ws.send_json({"t": "ping"})
    except (RuntimeError, OSError):
        # Starlette raises RuntimeError on a send after the peer went away; the
        # socket is gone either way and there is nothing to report to.
        return
    finally:
        pending.cancel()


def register(app: FastAPI, store: Store) -> None:
    """Mount /ws/events on `app`, watching `store`."""

    @app.websocket("/ws/events")
    async def events_socket(ws: WebSocket) -> None:
        await serve_events(ws, store)
