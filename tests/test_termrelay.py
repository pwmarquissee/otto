"""The terminal relay: herdr's stdio stream in, the dashboard's socket out.

The herdr process is faked with a pipe-shaped object; the record shapes are the
ones herdr 0.9.3 printed on this machine on 2026-10-01 (see termrelay's
docstring). Nothing here touches a real pane.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import subprocess
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from otto import termrelay, web_term
from otto.termrelay import Relay, RelayError, TermEvent


def frame(text: bytes, seq: int = 1, full: bool = False) -> str:
    return json.dumps({"type": "terminal.frame", "bytes": base64.b64encode(text).decode(),
                       "encoding": "ansi", "full": full, "seq": seq, "width": 80, "height": 24})


CLOSED = json.dumps({"type": "terminal.closed", "reason": "detached"})
NOT_FOUND = json.dumps({"type": "terminal.closed",
                        "reason": "terminal session observe failed: terminal target nope not found"})


class _Pipe:
    """A readable end that blocks like a real pipe and returns "" at EOF."""

    def __init__(self) -> None:
        self.q: queue.Queue[str | None] = queue.Queue()

    def readline(self) -> str:
        item = self.q.get()
        if item is None:
            self.q.put(None)  # keep EOF sticky for any later reader
            return ""
        return item

    def feed(self, *lines: str) -> None:
        for line in lines:
            self.q.put(line + "\n")

    def eof(self) -> None:
        self.q.put(None)


class _Stdin:
    def __init__(self, proc: "FakeProc") -> None:
        self.proc = proc

    def write(self, s: str) -> None:
        self.proc.commands.append(json.loads(s))
        self.proc.on_command(self.proc.commands[-1])

    def flush(self) -> None:
        pass


class FakeProc:
    """Enough of Popen for the relay: stdio pipes, poll/wait/terminate."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.stdout, self.stderr = _Pipe(), _Pipe()
        self.stdin = _Stdin(self)
        self.commands: list[dict] = []
        self.returncode: int | None = None
        self.terminated = threading.Event()

    # herdr answers terminal.release with a closed record and exits 0.
    def on_command(self, cmd: dict) -> None:
        if cmd.get("type") == "terminal.release":
            self.stdout.feed(CLOSED)
            self.exit(0)

    def exit(self, rc: int) -> None:
        self.returncode = rc
        self.stdout.eof()
        self.stderr.eof()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            # The real wait blocks; the relay only ever waits briefly, so an
            # unfinished fake must raise the same way Popen does.
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated.set()
        self.exit(-15)


@pytest.fixture
def spawn():
    procs: list[FakeProc] = []

    def factory(argv: list[str]) -> FakeProc:
        p = FakeProc(argv)
        procs.append(p)
        return p

    factory.procs = procs
    return factory


def spawned(spawn, timeout: float = 2.0) -> FakeProc:
    """The socket handler accepts first and spawns after the target check, so a
    test that connects and immediately looks for the process can win the race.
    Wait for it the way the relay's own threads do: briefly, then give up."""
    deadline = time.time() + timeout
    while not spawn.procs and time.time() < deadline:
        time.sleep(0.01)
    assert spawn.procs, "the handler never spawned a herdr process"
    return spawn.procs[0]


async def _collect(relay: Relay, limit: int = 20) -> list[TermEvent]:
    out: list[TermEvent] = []
    async for ev in relay.events():
        out.append(ev)
        if len(out) >= limit:
            break
    return out


# ---- argv and parsing ---------------------------------------------------------

def test_argv_matches_the_cli_herdr_documents():
    assert termrelay.argv_for("otto", "observe", 100, 30) == \
        ["terminal", "session", "observe", "otto", "--cols", "100", "--rows", "30"]
    assert termrelay.argv_for("w1:p1", "control", 80, 24, takeover=True) == \
        ["terminal", "session", "control", "w1:p1", "--takeover", "--cols", "80", "--rows", "24"]
    # --takeover is a control-only flag; observe never gets it.
    assert "--takeover" not in termrelay.argv_for("otto", "observe", 80, 24, takeover=True)
    with pytest.raises(RelayError) as e:
        termrelay.argv_for("otto", "attach", 80, 24)
    assert e.value.code == "bad_mode"


def test_records_parse_to_events_and_not_found_is_an_error():
    ev = termrelay.parse_record(json.loads(frame(b"\x1b[2Jhi", seq=7, full=True)))
    assert ev.kind == "frame" and ev.data == b"\x1b[2Jhi" and ev.meta["seq"] == 7
    assert termrelay.parse_record(json.loads(CLOSED)).kind == "closed"
    nf = termrelay.parse_record(json.loads(NOT_FOUND))
    assert nf.kind == "error" and nf.fatal and "not found" in nf.message
    assert termrelay.parse_record({"type": "something.else"}) is None


def test_stderr_lines_lose_the_herdr_prefix():
    line = "herdr: terminal session control input ignored: invalid json command: missing field `direction`"
    assert termrelay.stderr_message(line).startswith("input ignored:")
    assert termrelay.stderr_message('{"error":{"code":"x","message":"y"}}') == "x: y"


# ---- the relay ------------------------------------------------------------------

def test_frames_flow_and_commands_map_onto_herdr_stdin(spawn):
    async def run():
        relay = Relay("w1:p1", mode="control", cols=80, rows=24, spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        assert proc.argv == relay.argv
        proc.stdout.feed(frame(b"PS D:\\otto> ", full=True))
        first = await asyncio.wait_for(relay.events().__anext__(), 2)
        assert first.kind == "frame" and first.data == b"PS D:\\otto> "

        relay.input("echo hi\r")
        relay.resize(100, 30)
        relay.scroll(-3)
        relay.scroll(2)
        relay.scroll(0)
        assert proc.commands == [
            {"type": "terminal.input", "text": "echo hi\r"},
            {"type": "terminal.resize", "cols": 100, "rows": 30},
            {"type": "terminal.scroll", "direction": "up", "lines": 3},
            {"type": "terminal.scroll", "direction": "down", "lines": 2},
        ]
        relay.close()
        return proc
    proc = asyncio.run(run())
    assert proc.commands[-1] == {"type": "terminal.release"}
    assert not proc.terminated.is_set(), "a cooperative exit must not be terminated"


def test_close_terminates_a_process_that_ignores_release(spawn, monkeypatch):
    monkeypatch.setattr(termrelay, "RELEASE_WAIT_S", 0.05)

    async def run():
        relay = Relay("w1:p1", spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        proc.on_command = lambda cmd: None  # a herdr that never answers release
        relay.close()
        events = await asyncio.wait_for(_collect(relay), 2)
        return proc, events
    proc, events = asyncio.run(run())
    assert proc.terminated.is_set()
    assert events[-1].kind == "closed"


def test_bursts_of_frames_are_coalesced_into_one(spawn):
    """Frames that are already queued when the consumer takes one leave as a single
    message. "Already queued" is the whole rule: the relay never waits on a timer
    for a frame that might follow, because on Windows a small asyncio timeout
    rounds up to the 15.6 ms scheduler quantum and that wait made the relay
    slower than herdr itself (2026-10-02, measured 47 ms vs 29 ms raw). So the
    burst is fed, given a moment to land on the queue, and then read."""
    async def run():
        relay = Relay("otto", mode="observe", spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        proc.stdout.feed(frame(b"a", 1, True), frame(b"b", 2), frame(b"c", 3))
        await asyncio.sleep(0.05)
        first = await asyncio.wait_for(relay.events().__anext__(), 2)
        return first
    ev = asyncio.run(run())
    assert ev.kind == "frame"
    assert ev.data == b"abc"
    assert ev.meta["seq"] == 3, "the merged frame carries the last seq"


def test_frames_separated_by_a_pause_stay_separate(spawn):
    async def run():
        relay = Relay("otto", mode="observe", spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        gen = relay.events()
        proc.stdout.feed(frame(b"one", 1))
        a = await asyncio.wait_for(gen.__anext__(), 2)
        await asyncio.sleep(termrelay.COALESCE_S * 4)
        proc.stdout.feed(frame(b"two", 2))
        b = await asyncio.wait_for(gen.__anext__(), 2)
        return a, b
    a, b = asyncio.run(run())
    assert (a.data, b.data) == (b"one", b"two")


def test_observe_mode_refuses_input_but_can_still_release(spawn):
    async def run():
        relay = Relay("otto", mode="observe", spawn=spawn)
        await relay.start()
        with pytest.raises(RelayError) as e:
            relay.input("x")
        assert e.value.code == "read_only"
        relay.close()
        return spawn.procs[0]
    proc = asyncio.run(run())
    # Observers hold nothing to release, so close() just lets the process go.
    assert proc.commands == []
    assert proc.returncode is not None


def test_unknown_target_surfaces_as_a_fatal_error(spawn):
    async def run():
        relay = Relay("nope", mode="observe", spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        proc.stdout.feed(NOT_FOUND)
        proc.exit(0)
        return await asyncio.wait_for(_collect(relay), 2)
    events = asyncio.run(run())
    assert [e.kind for e in events] == ["error"]
    assert events[0].fatal and "nope not found" in events[0].message


def test_process_death_without_a_closed_record_is_reported(spawn):
    async def run():
        relay = Relay("otto", mode="observe", spawn=spawn)
        await relay.start()
        proc = spawn.procs[0]
        proc.stderr.feed("herdr: terminal session observe input ignored: bad thing")
        proc.exit(3)
        return await asyncio.wait_for(_collect(relay), 2)
    events = asyncio.run(run())
    kinds = [e.kind for e in events]
    assert "error" in kinds
    assert any("input ignored" in e.message for e in events)
    assert any(e.fatal and "status 3" in e.message for e in events)


def test_send_after_exit_is_a_relay_error(spawn):
    """send() after death is a clean RelayError, not a broken pipe."""
    async def run():
        relay = Relay("otto", spawn=spawn)
        await relay.start()
        spawn.procs[0].exit(0)
        with pytest.raises(RelayError) as e:
            relay.input("x")
        return e.value.code
    assert asyncio.run(run()) == "gone"


# ---- the picker ----------------------------------------------------------------

SNAP = {
    "agents": [{"agent": "claude", "agent_status": "idle", "cwd": "D:\\otto", "name": "otto",
                "pane_id": "w1:p1", "workspace_id": "w1",
                "agent_session": {"agent": "claude", "kind": "id", "value": "660a3192-x"}}],
    "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "cwd": "D:\\otto",
               "agent": "claude", "agent_status": "idle", "focused": True,
               "terminal_title": "\u2733 Claude Code", "terminal_title_stripped": "Claude Code"},
              {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1", "cwd": "D:\\otto",
               "focused": False, "terminal_title": "PowerShell"}],
    "workspaces": [{"workspace_id": "w1", "label": "otto"},
                   {"workspace_id": "w2", "label": "relay-test"}],
}


def test_list_panes_joins_panes_with_agents_and_workspaces():
    panes = termrelay.list_panes(SNAP)
    assert [p["pane_id"] for p in panes] == ["w1:p1", "w2:p1"]
    otto = panes[0]
    assert otto["label"] == "otto" and otto["name"] == "otto" and otto["target"] == "otto"
    assert otto["agent"] == "claude" and otto["agent_status"] == "idle"
    assert otto["terminal_title"] == "Claude Code" and otto["session_id"] == "660a3192-x"
    plain = panes[1]
    assert plain["label"] == "relay-test" and plain["name"] is None
    assert plain["target"] == "w2:p1" and plain["terminal_title"] == "PowerShell"
    assert termrelay.known_targets(SNAP) == {"w1:p1", "w2:p1", "otto"}
    assert termrelay.list_panes({}) == []


def test_a_down_server_means_no_panes_and_unknown_targets(monkeypatch):
    monkeypatch.setattr(termrelay.herdr, "snapshot", lambda: None)
    assert termrelay.list_panes() == []
    assert termrelay.known_targets() is None


# ---- the websocket --------------------------------------------------------------

class FakeBackend:
    def __init__(self, spawn) -> None:
        self.spawn = spawn
        self.opened: list[tuple] = []

    def open(self, target, mode, cols, rows, takeover):
        self.opened.append((target, mode, cols, rows, takeover))
        return Relay(target, mode=mode, cols=cols, rows=rows, takeover=takeover, spawn=self.spawn)

    def list_panes(self):
        return termrelay.list_panes(SNAP)

    def known_targets(self):
        return termrelay.known_targets(SNAP)


@pytest.fixture
def client(spawn):
    app = FastAPI()
    backend = FakeBackend(spawn)
    web_term.register(app, backend=backend)
    with TestClient(app) as c:
        c.backend = backend
        yield c


def test_panes_endpoint_returns_the_picker_list(client):
    r = client.get("/api/term/panes")
    assert r.status_code == 200
    assert [p["target"] for p in r.json()] == ["otto", "w2:p1"]


def test_socket_round_trip_frames_in_commands_out(client, spawn):
    with client.websocket_connect("/ws/term/w2:p1?cols=90&rows=25&takeover=1") as ws:
        proc = spawned(spawn)
        assert client.backend.opened == [("w2:p1", "control", 90, 25, True)]
        proc.stdout.feed(frame(b"\x1b[2JPS> ", 1, True))
        msg = ws.receive_json()
        assert msg["t"] == "frame" and base64.b64decode(msg["b"]) == b"\x1b[2JPS> "

        ws.send_json({"t": "input", "d": "echo relay-ok\r"})
        ws.send_json({"t": "resize", "cols": 100, "rows": 30})
        ws.send_json({"t": "scroll", "delta": -4})
        ws.send_json({"t": "bogus"})
        err = ws.receive_json()
        assert err["t"] == "error" and "bogus" in err["m"]
        # The input went through as text with the carriage return intact.
        assert {"type": "terminal.input", "text": "echo relay-ok\r"} in proc.commands
        assert {"type": "terminal.resize", "cols": 100, "rows": 30} in proc.commands
        assert {"type": "terminal.scroll", "direction": "up", "lines": 4} in proc.commands

        ws.send_json({"t": "release"})
        closed = ws.receive_json()
        assert closed == {"t": "closed", "reason": "detached"}
    assert proc.commands[-1] == {"type": "terminal.release"}


def test_socket_disconnect_releases_the_pane(client, spawn):
    with client.websocket_connect("/ws/term/otto?mode=observe") as ws:
        proc = spawned(spawn)
        proc.stdout.feed(frame(b"x"))
        ws.receive_json()
    # Leaving the context is the browser going away; the relay must let go.
    for _ in range(40):
        if proc.returncode is not None:
            break
        time.sleep(0.05)
    assert proc.returncode is not None
    assert client.backend.opened[0][1] == "observe"


def test_socket_refuses_unknown_targets_and_bad_modes(client, spawn):
    with client.websocket_connect("/ws/term/nope") as ws:
        msg = ws.receive_json()
        assert msg["t"] == "error" and "nope" in msg["m"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert spawn.procs == [], "no herdr process for a target the snapshot does not list"

    with client.websocket_connect("/ws/term/otto?mode=attach") as ws:
        msg = ws.receive_json()
        assert msg["t"] == "error" and "mode" in msg["m"]


def test_socket_relays_a_stream_that_herdr_ends(client, spawn):
    with client.websocket_connect("/ws/term/otto?mode=observe") as ws:
        proc = spawned(spawn)
        proc.stdout.feed(CLOSED)
        proc.exit(0)
        assert ws.receive_json() == {"t": "closed", "reason": "detached"}
