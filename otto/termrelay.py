"""termrelay: one herdr pane as a live terminal stream the dashboard can embed.

WHAT IT IS. herdr owns every pane's PTY and exposes each one as a stream over a
child process's stdio (`herdr terminal session observe|control <target>`):
newline-delimited JSON out, newline-delimited JSON commands in. This module wraps
that one process in a `Relay` the daemon's websocket layer (web_term.py) can pump
in both directions, plus `list_panes()` so the dashboard can offer a picker.

WHY A SUBPROCESS PER VIEWER. herdr's own docs point at the CLI rather than the
raw socket for everything but event subscribers, and the stream commands are the
CLI's only way in. One process per open terminal is the price; an idle viewer
costs nothing because a quiet pane sends no frames.

THE RECORDS, AS herdr 0.9.3 PRINTED THEM ON THIS MACHINE (2026-10-01):

  frame   {"type":"terminal.frame","bytes":"<base64 ANSI>","encoding":"ansi",
           "full":true|false,"seq":N,"width":100,"height":30}
  closed  {"type":"terminal.closed","reason":"detached"}
  unknown target: exit 0 and a single closed record whose reason reads
          "terminal session observe failed: terminal target X not found"

  stdin (control mode only):
  {"type":"terminal.input","text":"<text>"}      ("bytes": base64 also works;
                                                  "data" is silently ignored)
  {"type":"terminal.resize","cols":N,"rows":N}
  {"type":"terminal.scroll","direction":"up"|"down","lines":N}   (N is a u16)
  {"type":"terminal.release"}
  A malformed command is not fatal: herdr prints
  "herdr: terminal session control input ignored: ..." on stderr and carries on.

COALESCING. A busy pane (a build, a spinner) emits many small frames in a row.
Forwarding each one as its own websocket message storms the socket for no visual
gain, so frames that are already queued when one is taken are joined into one
before they leave the relay. Already queued, never "arriving soon": the relay
does not wait on a timer for more, because on Windows asyncio rounds a small
timeout up to the 15.6 ms scheduler quantum and that one wait made the relay
slower than herdr itself. The ANSI stream is a byte stream, so concatenation is
exact.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import herdr

# Not a wait. The relay never sleeps for more frames (see COALESCING above); this
# is the gap after which two frames are considered separate bursts, kept for the
# tests that assert a pause keeps frames apart and as documentation of the scale.
COALESCE_S = 0.002
# How long close() gives herdr to honor terminal.release before it is terminated.
RELEASE_WAIT_S = 1.0
MODES = ("control", "observe")


class RelayError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class TermEvent:
    """What the relay hands upward. kind is one of frame / closed / error.

    frame:  data holds the raw ANSI bytes (already base64-decoded, joined).
    closed: reason says why the stream ended ("detached", "exit", ...).
    error:  message is human-readable; fatal says whether the stream is over.
    """
    kind: str
    data: bytes = b""
    reason: str = ""
    message: str = ""
    fatal: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class Proc(Protocol):
    """The slice of subprocess.Popen the relay touches, so tests can fake it."""
    stdin: Any
    stdout: Any
    stderr: Any
    returncode: int | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


SpawnFn = Callable[[list[str]], Proc]


def argv_for(target: str, mode: str, cols: int, rows: int, takeover: bool = False) -> list[str]:
    """The herdr command line for one stream. Pure, so tests can assert on it."""
    if mode not in MODES:
        raise RelayError("bad_mode", f"mode must be one of {MODES}, not {mode!r}")
    args = ["terminal", "session", mode, target]
    if takeover and mode == "control":
        args.append("--takeover")
    args += ["--cols", str(cols), "--rows", str(rows)]
    return args


def default_spawn(args: list[str]) -> Proc:
    exe = herdr.binary()
    if exe is None:
        raise RelayError("not_installed", "herdr is not installed (herdr.dev)")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.Popen(
            [exe, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=flags)
    except OSError as e:
        raise RelayError("exec", str(e)) from e


class Relay:
    """One herdr terminal stream, pumped onto an asyncio queue.

    Lifecycle: construct, `await start()` on the loop that will consume it, iterate
    `events()`, call `close()` from any state (it is idempotent). Reads happen on
    two daemon threads (stdout, stderr) because the pipes are blocking; everything
    the consumer sees crosses to the loop through call_soon_threadsafe.
    """

    def __init__(self, target: str, mode: str = "control", cols: int = 120, rows: int = 36,
                 takeover: bool = False, spawn: SpawnFn | None = None) -> None:
        if mode not in MODES:
            raise RelayError("bad_mode", f"mode must be one of {MODES}, not {mode!r}")
        self.target, self.mode, self.cols, self.rows = target, mode, int(cols), int(rows)
        self.takeover = takeover
        self._spawn = spawn or default_spawn
        self._proc: Proc | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[TermEvent | None] = asyncio.Queue()
        self._threads: list[threading.Thread] = []
        self._stderr_thread: threading.Thread | None = None
        # _closed_seen: the consumer has (or will) get its final closed/error.
        # _closing: close() is driving the exit, so a non-zero status is ours,
        # not a herdr failure. Only the stdout reader posts the final events,
        # so the consumer sees every record before the end marker.
        self._closed_seen = False
        self._closing = False
        self._ended = False
        self._write_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------

    @property
    def argv(self) -> list[str]:
        return argv_for(self.target, self.mode, self.cols, self.rows, self.takeover)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._proc = self._spawn(self.argv)
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"termrelay-stderr-{self.target}", daemon=True)
        self._stderr_thread.start()
        out = threading.Thread(target=self._read_stdout, name=f"termrelay-stdout-{self.target}",
                               daemon=True)
        out.start()
        self._threads = [self._stderr_thread, out]

    def close(self) -> None:
        """Release the terminal politely, then make sure the process is gone.

        A controller that just vanishes leaves herdr holding a dead controller
        until it notices; terminal.release hands the pane back immediately so the
        next viewer does not need --takeover. Observers have nothing to release.
        """
        self._closing = True
        proc = self._proc
        if proc is None:
            self._end("closed")
            return
        if proc.poll() is None:
            if self.mode == "control":
                try:
                    self.send({"type": "terminal.release"})
                except RelayError:
                    pass  # stdin already gone, the terminate below is the fallback
            try:
                proc.wait(timeout=RELEASE_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=RELEASE_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass  # the reader thread will report whatever happens next

    # ---- commands (control mode) ---------------------------------------------

    def send(self, cmd: dict[str, Any]) -> None:
        """Write one JSON command line to herdr's stdin."""
        if self.mode != "control" and cmd.get("type") != "terminal.release":
            raise RelayError("read_only", "this stream is observe-only")
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise RelayError("gone", "the herdr stream has ended")
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise RelayError("gone", f"could not write to herdr: {e}") from e

    def input(self, text: str) -> None:
        self.send({"type": "terminal.input", "text": text})

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = int(cols), int(rows)
        self.send({"type": "terminal.resize", "cols": self.cols, "rows": self.rows})

    def scroll(self, delta: int) -> None:
        """delta < 0 scrolls up into history, > 0 back toward the bottom, in rows.
        herdr wants a direction plus an unsigned count, so the sign is split off."""
        n = abs(int(delta))
        if n == 0:
            return
        self.send({"type": "terminal.scroll", "direction": "up" if delta < 0 else "down",
                   "lines": min(n, 65535)})

    def release(self) -> None:
        self.send({"type": "terminal.release"})

    # ---- the event stream ---------------------------------------------------

    async def events(self):
        """Yield TermEvents until the stream ends. Frames already queued together
        are joined into one; nothing waits for a frame that has not arrived."""
        q = self._queue
        while True:
            ev = await q.get()
            if ev is None:
                return
            if ev.kind != "frame":
                yield ev
                if ev.kind == "closed" or ev.fatal:
                    return
                continue
            chunks = [ev.data]
            meta = dict(ev.meta)
            trailing: TermEvent | None = None
            # Join only what is ALREADY queued, never wait on a timer for more. A
            # timed wait here is what made the relay slower than herdr itself: on
            # Windows asyncio's proactor rounds a small timeout up to the 15.6 ms
            # scheduler quantum, so a "2 ms" window cost a full quantum per echo
            # (measured: 29 ms raw herdr, 47 ms through the relay). Yielding to
            # the loop with sleep(0) has no timer; it lets the reader thread's
            # already-posted callbacks land, which is all a split redraw needs.
            for _ in range(3):
                while True:
                    try:
                        nxt = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is not None and nxt.kind == "frame":
                        chunks.append(nxt.data)
                        meta.update(nxt.meta)
                        continue
                    trailing = nxt
                    break
                if trailing is not None:
                    break
                await asyncio.sleep(0)
            yield TermEvent("frame", data=b"".join(chunks), meta=meta)
            if trailing is None:
                continue
            yield trailing
            if trailing.kind == "closed" or trailing.fatal:
                return

    # ---- reader threads ----------------------------------------------------

    def _post(self, ev: TermEvent | None) -> None:
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, ev)
        except RuntimeError:
            pass  # the loop is closed; nobody is listening any more

    def _end(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        if not self._closed_seen:
            self._closed_seen = True
            self._post(TermEvent("closed", reason=reason))
        self._post(None)

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    self._post(TermEvent("error", message=f"unparseable herdr record: {line[:120]}"))
                    continue
                ev = parse_record(rec)
                if ev is None:
                    continue
                if ev.kind == "closed" or ev.fatal:
                    self._closed_seen = True
                self._post(ev)
                if ev.kind == "closed" or ev.fatal:
                    break
        except (OSError, ValueError):
            pass  # the pipe died under us; the exit handling below reports it
        rc: int | None
        try:
            rc = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            rc = proc.poll()
        # Let stderr drain first so a herdr complaint lands before the end marker.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        if not self._closed_seen:
            self._closed_seen = True
            if rc not in (0, None) and not self._closing:
                self._post(TermEvent("error", message=f"herdr exited with status {rc}", fatal=True))
            else:
                self._post(TermEvent("closed", reason="closed" if self._closing else "exit"))
        self._ended = True
        self._post(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None
        if proc.stderr is None:
            return
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._post(TermEvent("error", message=stderr_message(line)))
        except (OSError, ValueError):
            pass  # same as stdout: the exit path owns the final word


def parse_record(rec: dict[str, Any]) -> TermEvent | None:
    """One herdr stdout record to a TermEvent; None for records we do not carry."""
    t = rec.get("type")
    if t == "terminal.frame":
        try:
            data = base64.b64decode(rec.get("bytes") or "")
        except (ValueError, TypeError):
            return TermEvent("error", message="frame with undecodable bytes")
        meta = {k: rec[k] for k in ("seq", "full", "width", "height") if k in rec}
        return TermEvent("frame", data=data, meta=meta)
    if t == "terminal.closed":
        reason = str(rec.get("reason") or "closed")
        # An unknown target is reported as a close, not an error. The viewer
        # needs to know the difference between "you were detached" and "that
        # pane does not exist", so it is promoted here.
        if "not found" in reason or "failed" in reason:
            return TermEvent("error", message=reason, fatal=True, reason=reason)
        return TermEvent("closed", reason=reason)
    return None


def stderr_message(line: str) -> str:
    """herdr prefixes its stderr with the command path; the dashboard wants the
    part after it. A JSON error body is unwrapped the same way herdr.py does."""
    if line.startswith("{"):
        try:
            body = json.loads(line)
            e = body.get("error") or {}
            if e:
                return f"{e.get('code', 'error')}: {e.get('message', line)}"
        except ValueError:
            pass
    prefix = "herdr: terminal session "
    if line.startswith(prefix):
        rest = line[len(prefix):]
        for head in ("control ", "observe "):
            if rest.startswith(head):
                return rest[len(head):]
        return rest
    return line


# ---- the picker -------------------------------------------------------------

def list_panes(snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every pane herdr knows, with its workspace label and agent, for a picker.

    Panes carry the agent fields themselves in the snapshot, but the agent NAME
    (the thing you can target on the CLI) lives only on the agents list, so the
    two are joined on pane_id. Fields are fixed here because the frontend reads
    them by name; add, do not rename.
    """
    snap = snap if snap is not None else herdr.snapshot()
    if snap is None:
        return []
    ws_by_id = {w.get("workspace_id"): w for w in herdr.workspaces(snap)}
    agent_by_pane = {a.get("pane_id"): a for a in herdr.agents(snap)}
    out: list[dict[str, Any]] = []
    for p in herdr.panes(snap):
        pid = p.get("pane_id")
        a = agent_by_pane.get(pid) or {}
        ws = ws_by_id.get(p.get("workspace_id")) or {}
        out.append({
            "pane_id": pid,
            "workspace_id": p.get("workspace_id"),
            "tab_id": p.get("tab_id"),
            "label": ws.get("label"),
            "cwd": p.get("cwd") or a.get("cwd"),
            "agent": p.get("agent") or a.get("agent"),
            "name": a.get("name"),
            "target": a.get("name") or pid,
            "agent_status": p.get("agent_status") or a.get("agent_status"),
            "terminal_title": p.get("terminal_title_stripped") or p.get("terminal_title"),
            "focused": bool(p.get("focused")),
            "session_id": herdr.session_id_of(a) if a else None,
        })
    return out


def known_targets(snap: dict[str, Any] | None = None) -> set[str] | None:
    """Everything a stream can be opened on: pane ids and live agent names.
    None when herdr cannot be asked, so the caller lets herdr decide."""
    snap = snap if snap is not None else herdr.snapshot()
    if snap is None:
        return None
    names = {str(p["pane_id"]) for p in herdr.panes(snap) if p.get("pane_id")}
    names |= {str(a["name"]) for a in herdr.agents(snap) if a.get("name")}
    return names
