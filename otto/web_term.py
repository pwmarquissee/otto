"""web_term: the dashboard's terminal socket, one herdr pane per connection.

THE CONTRACT (shared with otto/web/, do not change shapes, add fields only):

  WebSocket /ws/term/{target}?cols=N&rows=M&mode=control|observe&takeover=0|1
    target is an agent name ("otto") or a pane id ("w1:p1"). mode defaults to
    control; takeover=1 replaces whoever controls the pane now.

  server -> client (JSON text):
    {"t":"frame","b":"<base64 ANSI bytes>"}     write straight into xterm
    {"t":"closed","reason":"detached"|...}       the stream ended; socket closes
    {"t":"error","m":"<message>"}                non-fatal unless followed by close

  client -> server (JSON text):
    {"t":"input","d":"<text>"}                   keystrokes, "\\r" is Enter
    {"t":"resize","cols":N,"rows":M}
    {"t":"scroll","delta":<int>}                 rows; negative is up into history
    {"t":"release"}                              hand the pane back and hang up

  GET /api/term/panes -> [ {pane_id, workspace_id, tab_id, label, cwd, agent,
                            name, target, agent_status, terminal_title, focused,
                            session_id}, ... ]

WHY A SEPARATE MODULE. daemon.py is shared by everything; this keeps the terminal
feature to a two-line hook there (`web_term.register(app)`) and lets a test mount
the same routes on a bare FastAPI app with a fake backend underneath.

WHY VALIDATE THE TARGET FIRST. herdr reports an unknown target as a normal close
with "not found" in the reason, after spawning a process. Checking the snapshot
first costs one CLI call and gives the viewer a clear error before any process
exists. When herdr cannot be asked at all the check is skipped and herdr's own
verdict is relayed instead.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from typing import Any, Callable, Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from . import termrelay
from .termrelay import MODES, Relay, RelayError

COLS = (20, 500)
ROWS = (5, 300)
DEFAULT_COLS, DEFAULT_ROWS = 120, 36
# 1008 is the websocket "policy violation" code: the request was well formed but
# refused (unknown target, bad mode). Viewers can tell it from a dropped link.
WS_REFUSED = 1008


class Backend(Protocol):
    """What the routes need from the relay layer. termrelay provides the real one."""

    def open(self, target: str, mode: str, cols: int, rows: int, takeover: bool) -> Relay: ...
    def list_panes(self) -> list[dict[str, Any]]: ...
    def known_targets(self) -> set[str] | None: ...


class HerdrBackend:
    def open(self, target: str, mode: str, cols: int, rows: int, takeover: bool) -> Relay:
        return Relay(target, mode=mode, cols=cols, rows=rows, takeover=takeover)

    def list_panes(self) -> list[dict[str, Any]]:
        return termrelay.list_panes()

    def known_targets(self) -> set[str] | None:
        return termrelay.known_targets()


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    return max(lo, min(hi, int(value)))


def frame_message(data: bytes) -> dict[str, Any]:
    return {"t": "frame", "b": base64.b64encode(data).decode("ascii")}


def event_message(ev: termrelay.TermEvent) -> dict[str, Any]:
    if ev.kind == "frame":
        return frame_message(ev.data)
    if ev.kind == "closed":
        return {"t": "closed", "reason": ev.reason}
    return {"t": "error", "m": ev.message}


def apply_client_message(relay: Relay, msg: dict[str, Any]) -> bool:
    """Map one client message onto the relay. Returns True when the client asked
    to end the session. Raises RelayError for anything the relay refuses."""
    t = msg.get("t")
    if t == "input":
        relay.input(str(msg.get("d", "")))
    elif t == "resize":
        relay.resize(_clamp(msg.get("cols", relay.cols), COLS),
                     _clamp(msg.get("rows", relay.rows), ROWS))
    elif t == "scroll":
        relay.scroll(int(msg.get("delta", 0)))
    elif t == "release":
        return True
    else:
        raise RelayError("bad_message", f"unknown message type {t!r}")
    return False


def register(app: FastAPI, backend: Backend | None = None,
             run_blocking: Callable[..., Any] | None = None) -> None:
    be: Backend = backend or HerdrBackend()
    # The snapshot shells out; it runs off the loop so a slow herdr never stalls
    # the rest of the daemon's handlers.
    blocking = run_blocking or asyncio.to_thread

    @app.get("/api/term/panes")
    def term_panes() -> list[dict[str, Any]]:
        return be.list_panes()

    @app.websocket("/ws/term/{target}")
    async def term_socket(ws: WebSocket, target: str, cols: int = DEFAULT_COLS,
                          rows: int = DEFAULT_ROWS, mode: str = "control",
                          takeover: int = 0) -> None:
        await ws.accept()

        async def refuse(message: str) -> None:
            await ws.send_json({"t": "error", "m": message})
            await ws.close(code=WS_REFUSED)

        if mode not in MODES:
            await refuse(f"mode must be control or observe, not {mode!r}")
            return
        try:
            known = await blocking(be.known_targets)
        except RelayError as e:
            await refuse(e.message)
            return
        if known is not None and target not in known:
            await refuse(f"unknown terminal target {target!r}")
            return

        try:
            relay = be.open(target, mode, _clamp(cols, COLS), _clamp(rows, ROWS), bool(takeover))
            await relay.start()
        except RelayError as e:
            await refuse(e.message)
            return

        ended = {"sent": False}

        async def pump() -> None:
            """relay -> socket, until the stream ends."""
            async for ev in relay.events():
                await ws.send_json(event_message(ev))
                if ev.kind == "closed" or ev.fatal:
                    ended["sent"] = True
                    return

        async def drive() -> None:
            """socket -> relay, until the client hangs up or asks to release."""
            while True:
                try:
                    raw = await ws.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    return
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                except ValueError as e:
                    await ws.send_json({"t": "error", "m": f"bad message: {e}"})
                    continue
                try:
                    if apply_client_message(relay, msg):
                        return
                except RelayError as e:
                    await ws.send_json({"t": "error", "m": e.message})
                    if e.code == "gone":
                        return

        pumping, driving = asyncio.create_task(pump()), asyncio.create_task(drive())
        try:
            await asyncio.wait({pumping, driving}, return_when=asyncio.FIRST_COMPLETED)
            if driving.done() and not pumping.done() and relay.mode == "control":
                # The client released or hung up. Ask herdr to let go and give
                # it a moment to answer with its own closed record, so the
                # viewer sees the real reason rather than a bare socket close.
                try:
                    relay.release()
                except RelayError:
                    pass  # already gone; the close below covers it
                await asyncio.wait({pumping}, timeout=termrelay.RELEASE_WAIT_S)
        finally:
            for t in (pumping, driving):
                t.cancel()
            # close() blocks on the pipe, and it must happen even when this task
            # is cancelled under us (a server shutdown, or Starlette's TestClient
            # on disconnect), where every await below would re-raise. A thread
            # started synchronously survives that; nothing needs its result.
            threading.Thread(target=relay.close, name=f"termrelay-close-{target}",
                             daemon=True).start()
            try:
                # asyncio.wait, not gather: when this task is cancelled while
                # gathering just-cancelled children, the cancel count comes out
                # wrong and the CancelledError escapes the server's scope
                # (reproduced under Starlette's TestClient, anyio 4.10).
                await asyncio.wait({pumping, driving})
                if not ended["sent"]:
                    await ws.send_json({"t": "closed", "reason": "released"})
                await ws.close()
            except (RuntimeError, WebSocketDisconnect, OSError):
                pass  # the client already went away; nothing to close
