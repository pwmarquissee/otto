"""Claude Code session-state hook. Reports one turn boundary to the Otto daemon.

    python scripts/otto_hook.py <busy|waiting|idle|offline>

Installed into ~/.claude/settings.local.json by `otto sessions install`. See
otto/sessions.py for the event map and the reasoning; this file is only the
courier.

THREE RULES, ALL FOR THE SAME REASON. This runs inside every turn the owner takes and
blocks it until it returns:

  1. STDLIB ONLY, AND BARELY THAT. No `import otto`, no pydantic, no requests.
     Not even urllib: measured on this machine, `import urllib.request` costs
     ~62ms of the ~134ms total, because it drags in tempfile, shutil, lzma and
     random to send one POST to loopback. A hand-rolled socket write and `-S` on
     the interpreter halve the per-turn cost. The request is a fixed, tiny POST to
     127.0.0.1 with a body this script produced itself, so there is no HTTP
     subtlety being skipped here, only import weight.
  2. FAIL OPEN, ALWAYS EXIT 0. A daemon that is down, a timeout, a malformed
     payload: all of them mean "no telemetry this turn", never "fail the turn".
     Observability that can break the thing it observes is a bad trade.
  3. SILENT. Nothing on stdout or stderr. Claude Code reads hook output, and a
     stray print is a protocol violation.

Claude Code hands the hook its context as JSON on stdin. Confirmed from the real
payloads this machine has logged: session_id, cwd, transcript_path,
permission_mode, hook_event_name, and last_assistant_message on the Stop family.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

# Mirrors otto.config, which this deliberately does not import.
HOST = os.environ.get("OTTO_HOST", "127.0.0.1")
PORT = os.environ.get("OTTO_PORT", "8787")
BASE = os.environ.get("OTTO_URL", f"http://{HOST}:{PORT}").rstrip("/")

# TWO timeouts, and the split is the whole point.
#
# The obvious design is one timeout, generous enough to survive the daemon being
# mid-tick. That is a trap on this machine. Connecting to a closed port on loopback
# does NOT get refused here: the SYN is dropped, so the connect hangs for the full
# timeout instead of failing in microseconds. With a single 2s timeout, every turn
# taken while the daemon was down paid 2s per hook, 4s per turn, machine-wide.
# Measured, after being missed entirely by a first round of benchmarks that all ran
# against a live daemon.
#
# So: CONNECT is tight, because a live local daemon accepts in well under a
# millisecond (measured 0.3-0.6ms) and no honest loopback connect needs 350ms. READ
# stays generous, because once connected the daemon may genuinely be finishing a
# tick.
CONNECT_TIMEOUT = 0.35
TIMEOUT = 2.0

# Having paid one connect timeout, do not pay it again on every turn. A daemon that
# is down tends to be down for a while (a restart, a reboot, a code change), and the
# breaker turns "350ms on every turn until the owner notices" into "350ms once".
#
# Short on purpose. The cost of a stale breaker is dropped events, and a dropped
# event is cheap here because the next hook re-reports the state from scratch. 15s
# is long enough to cover a burst of turns and short enough that a daemon coming
# back is picked up almost immediately.
BREAKER_SECONDS = 15
_TEMP = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "."
BREAKER = os.path.join(_TEMP, "otto-hook-daemon-down")

STATES = ("busy", "waiting", "idle", "offline")

# Claude Code sends two kinds of Notification. One means "blocked, asking
# permission" and is genuinely `waiting`. The other is the "you have been idle"
# nudge, where the session is free and ready, and reporting THAT as waiting is the
# noisiest possible way to be wrong: a session with nothing to do would sit in the
# panel claiming it needs the owner.
#
# The installed matcher already filters to permission prompts, so this is defense
# in depth against that filter changing. It is a DENY list rather than an allow
# list on purpose: an unrecognized permission phrasing should still be reported
# (the matcher passed it), whereas the idle nudge is a specific known string. An
# allow list got this backwards and flipped an idle session to waiting, caught in
# end-to-end testing rather than by the unit assertions, which is why
# `sessions_conformance.py` now asserts on the real message texts.
_IDLE_NUDGE = ("waiting for your input", "is idle", "has been idle")


def _debug(msg: str) -> None:
    """Opt-in breadcrumb for `otto sessions doctor`. Off unless asked for."""
    path = os.environ.get("OTTO_HOOK_DEBUG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def _is_permission_prompt(payload: dict) -> bool:
    message = str(payload.get("message") or "").lower()
    if not message:
        return True  # nothing to judge on; trust the installed matcher
    if "permission" in message:
        return True  # explicit, and outranks any other phrase in the message
    if any(sig in message for sig in _IDLE_NUDGE):
        return False
    return True


def _breaker_open() -> bool:
    """True if a recent connect failed and we should not try again yet."""
    try:
        return (time.time() - os.path.getmtime(BREAKER)) < BREAKER_SECONDS
    except OSError:
        return False


def _trip_breaker() -> None:
    try:
        with open(BREAKER, "w") as fh:
            fh.write("")
    except OSError:
        pass


def _clear_breaker() -> None:
    try:
        os.remove(BREAKER)
    except OSError:
        pass


def _post(path: str, body: bytes) -> None:
    """One POST, on a raw socket, to the loopback daemon.

    Falls back to urllib for anything that is not plain http, which in practice
    means somebody pointed OTTO_URL at a real host. Paying the import cost is
    correct there and costs nothing in the normal case, because the import is
    inside the branch.
    """
    if not BASE.startswith("http://"):
        import urllib.request  # noqa: PLC0415 - deliberately lazy, see docstring
        req = urllib.request.Request(
            f"{BASE}{path}", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _debug(f"{path} -> {r.status}")
        return

    if _breaker_open():
        _debug(f"{path} -> skipped, daemon marked down")
        return

    hostport = BASE[len("http://"):]
    host, _, port = hostport.partition(":")
    addr = (host or "127.0.0.1", int(port or 80))

    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8") + body

    try:
        sock = socket.create_connection(addr, timeout=CONNECT_TIMEOUT)
    except OSError:
        # Refused, dropped, or timed out: all mean "not there right now".
        _trip_breaker()
        raise

    with sock:
        # Connected, so the daemon is up. Any breaker from an earlier outage is
        # stale and must go, or a daemon that came back would stay unreported for
        # the rest of the breaker window.
        _clear_breaker()
        sock.settimeout(TIMEOUT)
        sock.sendall(request)
        # Read the status line and drop the rest. Not for the result (there is
        # nothing to do with it either way) but so the daemon completes its write
        # instead of logging a reset connection on every single turn.
        status = sock.recv(64).split(b"\r\n")[0].decode("latin-1", "replace")
        _debug(f"{path} -> {status}")


def main(argv: list[str]) -> int:
    state = (argv[1] if len(argv) > 1 else "").strip().lower()
    if state not in STATES:
        _debug(f"bad state {state!r}")
        return 0

    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 - no stdin is not an error worth failing a turn
        pass

    payload = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            _debug(f"unparseable stdin for {state}")

    if not payload.get("session_id"):
        # Nothing to key on. Recording it would create an anonymous row that can
        # never be updated or closed.
        _debug(f"no session_id for {state}")
        return 0

    if state == "waiting" and not _is_permission_prompt(payload):
        _debug(f"skipping idle-nudge notification: {payload.get('message')!r}")
        return 0

    # The one thing the payload lacks that the daemon cannot get later: which
    # process this is. Our parent is the shell Claude Code ran the hook in, and
    # that shell's parent is Claude Code itself. The shell is alive for exactly as
    # long as this POST takes, so the daemon resolves the chain during the request
    # (otto/sessions.py, resolve_process). Two syscalls; no import.
    try:
        payload["ppid"] = os.getppid()
    except OSError:
        pass

    try:
        _post(f"/api/sessions/{state}", json.dumps(payload).encode("utf-8"))
    except OSError as e:
        # Daemon down is the ordinary case, not a problem: refused on loopback
        # returns instantly, and the next hook will report the state anyway.
        _debug(f"{state} -> unreachable: {e}")
    except Exception as e:  # noqa: BLE001 - see rule 2
        _debug(f"{state} -> {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 - rule 2, without exception
        sys.exit(0)
