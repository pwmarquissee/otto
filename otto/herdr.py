"""herdr: the harness the sessions live in.

WHAT IT IS. herdr (herdr.dev, Rust, 41k stars, Windows GA since 0.9) is a terminal
multiplexer built for coding agents: a background server owns every pane's PTY,
panes survive the client closing, each pane's agent is read off the screen as
idle / working / blocked / done, and a local socket API can create workspaces,
start agents, prompt them, wait on them, and read what they wrote. It is the
piece keitora built for itself (keitora-ptyd) and the piece Otto deliberately is
not.

WHAT OTTO DOES WITH IT. Otto is the logistics layer on top: it keeps the board,
decides which card could go to which idle session (logistics.py), and when the
owner approves, hands the card's prompt into that pane through `herdr agent prompt`.
herdr keeps the terminals; Otto keeps the judgement. Neither pretends to be the
other.

HOW OTTO TALKS TO IT. Through the CLI, which prints JSON for every command and is
the layer herdr's own docs recommend over the raw socket for anything that is not
an event subscriber. One `herdr api snapshot` per tick (~60ms) is the whole
polling cost; everything else happens on a request. The binary is resolved
explicitly because the daemon's PATH predates the install.

THE JOIN. herdr's Claude integration (a SessionStart hook it installs next to
Otto's) reports the Claude session id into the pane record as
`agent_session.value`. That is the same id Otto's hooks key sessions on, so a
pane and a Session are the same thing by construction, not by guessing from a
directory.

ONE WINDOWS WART, WORKED AROUND. `herdr agent start --kind claude` resolves
`claude` through PowerShell's Get-Command, which on an npm install returns the
extensionless shim before `claude.cmd`, and Start-Process then fails with "%1 is
not a valid Win32 application". Typing `claude` into the pane shell resolves it
the way the owner's own terminals do, and herdr detects a manually launched agent
anyway. So Otto launches with `pane run` and waits for detection itself.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config, safeargs
from .models import Session, iso, utcnow
from .store import Store


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---- the binary --------------------------------------------------------------

def binary() -> str | None:
    """Where herdr is, or None. Checked in this order: the explicit config, PATH,
    the installer's stable alias, the installer's versioned releases (newest)."""
    if config.HERDR_BIN and Path(config.HERDR_BIN).is_file():
        return config.HERDR_BIN
    found = shutil.which("herdr") or shutil.which("herdr.exe")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        alias = Path(local) / "Programs" / "Herdr" / "bin" / "herdr.exe"
        if alias.is_file():
            return str(alias)
    releases = sorted(glob.glob(str(Path.home() / ".herdr" / "packages" / "standalone"
                                    / "releases" / "*" / "herdr.exe")))
    return releases[-1] if releases else None


def available() -> bool:
    return binary() is not None


# The daemon has no console. Without this flag every herdr.exe call from the tick
# would open a console window that takes focus (runners/external.py records the
# AWS probe doing exactly that during a full-screen game). Harmless from a terminal.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(args: list[str], timeout: float = 20.0) -> dict[str, Any]:
    """Run one herdr command and return its JSON result body."""
    exe = binary()
    if exe is None:
        raise HerdrError("not_installed", "herdr is not installed (herdr.dev)")
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise HerdrError("timeout", f"herdr {' '.join(args[:2])} took over {timeout:.0f}s") from e
    except OSError as e:
        raise HerdrError("exec", str(e)) from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        try:
            body = json.loads(err.splitlines()[-1]) if err else {}
            e = body.get("error") or {}
            raise HerdrError(str(e.get("code") or "error"), str(e.get("message") or err))
        except (ValueError, IndexError):
            raise HerdrError("cli", err[:300] or f"exit {r.returncode}") from None
    out = (r.stdout or "").strip()
    if not out:
        return {}
    try:
        body = json.loads(out.splitlines()[-1])
    except ValueError:
        return {"text": out}
    return body.get("result", body)


# ---- the server ----------------------------------------------------------------

def snapshot() -> dict[str, Any] | None:
    """The live session: agents, panes, workspaces. None when the server is down."""
    try:
        return _run(["api", "snapshot"], timeout=10).get("snapshot")
    except HerdrError as e:
        if e.code in ("not_installed",):
            raise
        return None


def server_running() -> bool:
    return available() and snapshot() is not None


def snapshot_or_none() -> dict[str, Any] | None:
    """snapshot() for read paths that must work on a machine without herdr.

    snapshot() raises not_installed on purpose so a caller that needs herdr
    (start a pane, prompt an agent) fails loudly. The read side of the dashboard
    is the opposite case: no herdr means an empty rail, not a 500. Found by CI on
    a runner without herdr, 2026-10-02: GET /api/state raised from logistics.view.
    """
    if not available():
        return None
    return snapshot()


# Variables a Claude Code tool shell sets on everything it runs. A herdr server
# started from one of those shells passes them to every pane shell it ever
# opens, and the Claude in that pane then draws in monochrome (NO_COLOR) and
# believes it is nested inside another Claude Code (CLAUDECODE). Found 2026-10-02
# when a pane that should have looked like a Windows Terminal tab was all white.
_TAINT = ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "CLAUDECODE",
          "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
          "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT")


def clean_env() -> dict[str, str]:
    """The current environment minus the Claude Code tool-shell markers, for any
    long-lived process Otto starts that other sessions will inherit from."""
    return {k: v for k, v in os.environ.items() if k not in _TAINT}


# Pane shells created on an already-running server still inherit the server's
# environment; these overrides make a new pane color-capable regardless. An
# empty NO_COLOR counts as unset (the spec says "present and not empty").
PANE_ENV_ARGS = ["--env", "NO_COLOR=", "--env", "CLAUDECODE=", "--env", "CLAUDE_CODE_ENTRYPOINT="]


def ensure_server(wait: float = 8.0) -> tuple[bool, str]:
    """Start the background server if it is not up. Returns (started, message)."""
    if not available():
        return False, "herdr is not installed"
    if server_running():
        return False, "herdr server already running"
    exe = binary()
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe, "server"], creationflags=flags, close_fds=True,
                         env=clean_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, f"could not start herdr server: {e}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if server_running():
            return True, "herdr server started"
        time.sleep(0.5)
    return False, "herdr server did not answer within the wait"


# ---- reads -------------------------------------------------------------------

def agents(snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    snap = snap if snap is not None else snapshot_or_none()
    return list((snap or {}).get("agents") or [])


def panes(snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    snap = snap if snap is not None else snapshot_or_none()
    return list((snap or {}).get("panes") or [])


def workspaces(snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    snap = snap if snap is not None else snapshot_or_none()
    return list((snap or {}).get("workspaces") or [])


def session_id_of(agent: dict[str, Any]) -> str | None:
    """The Claude session id herdr's integration reported for this agent."""
    ref = agent.get("agent_session") or {}
    if agent.get("agent") == "claude" and ref.get("kind") == "id" and ref.get("value"):
        return str(ref["value"])
    return None


def target_of(agent: dict[str, Any]) -> str:
    """How to address this agent on the CLI: its name when it has one, else pane."""
    return str(agent.get("name") or agent.get("pane_id"))


def agent_for_pane(pane_id: str, snap: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for a in agents(snap):
        if a.get("pane_id") == pane_id:
            return a
    return None


def read(target: str, lines: int = 120) -> str:
    """Recent screen text of an agent, unwrapped, ANSI stripped."""
    body = _run(["agent", "read", target, "--source", "recent-unwrapped",
                 "--lines", str(lines)], timeout=20)
    if "text" in body:
        return str(body["text"])
    return str((body.get("read") or {}).get("text") or "")


# Lines a Claude Code screen always ends with and that never say anything: the
# input box's frame, the bare prompt, and the shortcut footer. The rail's preview
# and the blocked toast both want the last line ABOVE that chrome.
_CHROME_CHARS = set("─│╭╮╰╯┃━═┌┐└┘├┤┬┴┼╌╍ ·>•❯›⏵")
_FOOTER_WORDS = ("? for shortcuts", "esc to interrupt", "esc to cancel", "ctrl+c to",
                 "bypass permissions", "shift+tab to", "accept edits")


def last_line(text: str, limit: int = 200) -> str:
    """The last screen line that says something, or "" when none does."""
    for raw in reversed((text or "").splitlines()):
        line = raw.strip().strip("│┃").strip()
        if not line or set(line) <= _CHROME_CHARS:
            continue
        low = line.lower()
        if any(w in low for w in _FOOTER_WORDS):
            continue
        return line[:limit]
    return ""


def _ask_text(agent: dict[str, Any]) -> str | None:
    """What a blocked pane is asking, read off its screen. None when unreadable:
    the toast still fires, it just cannot say what the question was."""
    try:
        return last_line(read(target_of(agent), lines=12))[:240] or None
    except HerdrError:
        return None


# ---- writes ------------------------------------------------------------------

def _workspace_ids(r: dict[str, Any]) -> tuple[str, str]:
    """(workspace_id, root pane id) out of a workspace or worktree create/open
    result. Both print `.result.workspace.workspace_id` and
    `.result.root_pane.pane_id`; the flat spellings are accepted so a future
    herdr that trims the nesting does not turn into a KeyError in the daemon."""
    ws = r.get("workspace") or {}
    pane = r.get("root_pane") or {}
    wid = ws.get("workspace_id") or r.get("workspace_id")
    pid = pane.get("pane_id") or r.get("root_pane_id") or r.get("pane_id")
    if not wid or not pid:
        raise HerdrError("bad_result", f"herdr returned no workspace/pane ids (keys: {sorted(r)})")
    return str(wid), str(pid)


def create_workspace(cwd: str, label: str | None = None) -> tuple[str, str]:
    """A new workspace rooted at cwd. Returns (workspace_id, root pane id)."""
    args = ["workspace", "create", "--cwd", cwd, "--no-focus", *PANE_ENV_ARGS]
    if label:
        args += ["--label", label]
    return _workspace_ids(_run(args))


# ---- worktrees -----------------------------------------------------------------
# A worktree in herdr is a workspace with git provenance: `worktree create` runs
# `git worktree add`, opens the checkout as a workspace grouped under the repo's
# own workspace, and hands back the same ids `workspace create` does. That is why
# a card that needs its own branch can get a pane the same way a repo does.

def worktree_list(cwd: str) -> dict[str, Any]:
    """The worktrees of the repo containing cwd, with which are open in herdr.
    Returns the result body: `source` (repo root, its workspace) and
    `worktrees` (path, branch, label, open_workspace_id or null)."""
    r = _run(["worktree", "list", "--cwd", cwd], timeout=30)
    return {"source": r.get("source") or {}, "worktrees": list(r.get("worktrees") or [])}


def worktree_create(cwd: str, branch: str, base: str | None = None,
                    label: str | None = None) -> tuple[str, str]:
    """Create a git worktree for `branch` off the repo at cwd and open it as a
    workspace. An existing local branch is checked out; a new one is created
    from `base` (or HEAD). Returns (workspace_id, root pane id). Never focused:
    Otto opens panes behind whatever the owner is looking at."""
    if not branch:
        raise HerdrError("bad_branch", "a worktree needs a branch name")
    args = ["worktree", "create", "--cwd", cwd, "--branch", branch, "--no-focus"]
    if base:
        args += ["--base", base]
    if label:
        args += ["--label", label]
    return _workspace_ids(_run(args, timeout=90))


def worktree_open(cwd: str, branch: str | None = None, path: str | None = None,
                  label: str | None = None) -> tuple[str, str]:
    """Open an existing worktree of the repo at cwd as a workspace, named by its
    branch or its checkout path (one, not both). Returns (workspace_id, pane id)."""
    if bool(branch) == bool(path):
        raise HerdrError("bad_target", "name the worktree by --branch or --path, exactly one")
    args = ["worktree", "open", "--cwd", cwd, "--no-focus"]
    args += ["--branch", branch] if branch else ["--path", str(path)]
    if label:
        args += ["--label", label]
    return _workspace_ids(_run(args, timeout=60))


def run_in_pane(pane_id: str, command: str) -> None:
    _run(["pane", "run", pane_id, command])


def prompt(target: str, text: str) -> dict[str, Any]:
    """Submit a prompt to an agent. Refused by herdr if the agent is blocked."""
    return _run(["agent", "prompt", target, text], timeout=30)


def focus(target: str) -> None:
    _run(["agent", "focus", target])


def rename(target: str, name: str) -> dict[str, Any]:
    return _run(["agent", "rename", target, name])


def wait(target: str, until: list[str], timeout_ms: int) -> dict[str, Any]:
    args = ["agent", "wait", target, "--timeout", str(timeout_ms)]
    for u in until:
        args += ["--until", u]
    return _run(args, timeout=timeout_ms / 1000 + 5)


def notify(title: str, body: str | None = None, sound: str = "none") -> None:
    """A toast inside the herdr TUI (and the OS, per its config)."""
    args = ["notification", "show", title, "--sound", sound]
    if body:
        args += ["--body", body]
    try:
        _run(args, timeout=10)
    except HerdrError:
        pass


def claude_command(resume: str | None = None, extra: list[str] | None = None) -> str:
    """The line typed into a pane shell to start claude.

    This is a SHELL LINE, not argv: `pane run` types it into PowerShell. So the
    one value that can come from outside (a session id from a request or a hook)
    is checked against the session-id shape and refused otherwise; an id like
    `x; Start-Process calc` would otherwise run as a second command. The config
    args are the owner's own and are passed through as written."""
    parts = ["claude", *config.HERDR_CLAUDE_ARGS, *(extra or [])]
    if resume:
        if not safeargs.is_session_id(resume):
            raise HerdrError("bad_session_id", f"not a session id: {resume[:64]!r}")
        parts += ["--resume", resume]
    return " ".join(parts)


def start_claude(cwd: str, label: str | None = None, name: str | None = None,
                 resume: str | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Open a workspace at cwd and get a Claude session running in its pane.

    Launches by typing the command into the pane shell (see the module docstring
    for why not `agent start`) and polls the snapshot until herdr has detected the
    agent and it is ready (idle, or blocked on a first-run dialog). Returns the
    agent record.
    """
    if not Path(cwd).is_dir():
        raise HerdrError("bad_cwd", f"{cwd} is not a directory")
    label = label or Path(cwd).name
    _ws, pane_id = create_workspace(cwd, label)
    return launch_claude_in_pane(pane_id, name=name, resume=resume, timeout=timeout)


def launch_claude_in_pane(pane_id: str, name: str | None = None, resume: str | None = None,
                          timeout: float | None = None) -> dict[str, Any]:
    """Get a Claude session running in a pane that already exists (a fresh
    workspace, a worktree's root pane) and wait until herdr has detected it.
    Split out of start_claude so a worktree workspace, which herdr creates
    itself, gets the exact same launch and readiness check as a plain one."""
    run_in_pane(pane_id, claude_command(resume))
    deadline = time.time() + (timeout or config.HERDR_START_TIMEOUT)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = agent_for_pane(pane_id)
        if last and last.get("agent") == "claude" and \
                last.get("agent_status") in ("idle", "done", "blocked"):
            break
        time.sleep(1.0)
    else:
        raise HerdrError("agent_not_ready",
                         f"claude did not come up in {pane_id} within the timeout"
                         + (f" (last status {last.get('agent_status')})" if last else ""))
    if name and safeargs.is_agent_name(name):
        # A name herdr would refuse is skipped the same way a taken one is: the
        # session is up, and a label is not worth failing the launch over.
        try:
            last = rename(pane_id, name).get("agent", last)
        except HerdrError:
            pass  # a taken name is not a failed launch
    return last or {}


# ---- the window title -------------------------------------------------------------
# `herdr terminal title set` names the outer terminal window the herdr client is
# attached to. One glance at the taskbar then says whether anything is blocked,
# which is the whole point of a harness the owner can look away from. Set once per
# change, not per tick: the server would accept a rewrite every 15s, but a title
# that flickers is a title the eye learns to ignore.

_last_title: str | None = None


def _rows(snap: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    """Rows of a snapshot ALREADY taken. Unlike agents()/panes(), None here means
    "no server", not "go and ask": the planners below must never shell out."""
    return list((snap or {}).get(key) or [])


def window_title(snap: dict[str, Any] | None) -> str:
    counts = {"idle": 0, "working": 0, "blocked": 0}
    for a in _rows(snap, "agents"):
        status = a.get("agent_status")
        key = "idle" if status == "done" else status
        if key in counts:
            counts[key] += 1
    return (f"Otto · {counts['idle']} idle · {counts['working']} working · "
            f"{counts['blocked']} blocked")


def set_window_title(title: str) -> None:
    _run(["terminal", "title", "set", title], timeout=10)


def _refresh_window_title(snap: dict[str, Any] | None) -> bool:
    """Push the title when it changed. Returns whether a set was sent. A failure
    (no client attached, old herdr) is swallowed and the title is not remembered,
    so the next change tries again rather than going quiet for good."""
    global _last_title
    if not config.HERDR_WINDOW_TITLE or snap is None:
        return False
    title = window_title(snap)
    if title == _last_title:
        return False
    try:
        set_window_title(title)
    except HerdrError:
        return False
    _last_title = title
    return True


# ---- migration: what is not in herdr yet ----------------------------------------
# Pure planning, so the CLI prints exactly what it is about to do and the tests
# can pin the selection without a daemon. The hand-off itself goes through the
# daemon's /api/herdr/open so the session rows are synced in the same call.

def norm_path(p: str | None) -> str:
    """One spelling for a directory so D:\\otto, d:/otto/ and D:/otto compare equal."""
    if not p:
        return ""
    return str(p).replace("\\", "/").rstrip("/").lower()


def open_cwds(snap: dict[str, Any] | None) -> set[str]:
    """Directories herdr already has a pane in. Workspaces do not carry a cwd in
    the snapshot; panes do, and a workspace is open iff one of its panes is."""
    out = set()
    for p in _rows(snap, "panes"):
        out.add(norm_path(p.get("cwd")))
    for a in _rows(snap, "agents"):
        out.add(norm_path(a.get("foreground_cwd") or a.get("cwd")))
    out.discard("")
    return out


def agent_name_for(path: str) -> str:
    """A herdr agent name from a directory name: [a-z][a-z0-9_-]{0,31}."""
    raw = Path(path).name.lower()
    cleaned = "".join(c if (c.isascii() and (c.isalnum() or c in "_-")) else "-" for c in raw)
    cleaned = cleaned.lstrip("0123456789_-")
    return (cleaned or "repo")[:32]


def plan_adopt(sessions: list[dict[str, Any]], snap: dict[str, Any] | None,
               limit: int = 10) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Which OFFLINE sessions to resume into panes: newest first, capped, skipping
    ones herdr already holds (by session id) and ones whose directory is gone.
    Returns (chosen, skipped-with-reason). Live sessions are not candidates: they
    are open in some terminal already and resuming would open them twice."""
    in_herdr = {session_id_of(a) for a in _rows(snap, "agents")} - {None}
    chosen: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], str]] = []
    ordered = sorted(sessions, key=lambda s: s.get("last_event") or "", reverse=True)
    for s in ordered:
        if s.get("state") != "offline":
            continue
        if s.get("session_id") in in_herdr:
            skipped.append((s, "already in herdr"))
            continue
        cwd = s.get("cwd")
        if not cwd or not Path(cwd).is_dir():
            skipped.append((s, f"directory gone: {cwd}"))
            continue
        if len(chosen) >= limit:
            skipped.append((s, f"over the --limit of {limit}"))
            continue
        chosen.append(s)
    return chosen, skipped


def plan_open_repos(repos: list[dict[str, Any]], snap: dict[str, Any] | None
                    ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Which repo roots to open as workspaces: one per repo Otto knows, skipping
    any directory herdr already has a pane in and any that is not on disk.
    Returns (to-open with the agent name filled in, skipped-with-reason)."""
    already = open_cwds(snap)
    to_open: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for r in repos:
        path = r.get("path") or ""
        key = norm_path(path)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in already:
            skipped.append((r, "already open in herdr"))
            continue
        if not Path(path).is_dir():
            skipped.append((r, "not on disk"))
            continue
        to_open.append({**r, "name": agent_name_for(path)})
    return to_open, skipped


def client_host_pid() -> int | None:
    """The pid of the app (Windows Terminal, Cursor...) hosting a herdr TUI client,
    so a focus can bring that window forward. None when no client is attached."""
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return None
    hosts = {"windowsterminal.exe", "cursor.exe", "code.exe", "wezterm-gui.exe",
             "alacritty.exe", "conhost.exe"}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() not in ("herdr.exe", "herdr"):
                continue
            argv = [a.lower() for a in (p.info["cmdline"] or [])[1:]]
            if argv[:1] == ["server"]:
                continue
            q = p.parent()
            for _ in range(8):
                if q is None:
                    break
                if q.name().lower() in hosts:
                    return q.pid
                q = q.parent()
        except Exception:  # noqa: BLE001
            continue
    return None


# ---- the sync into Otto's sessions ---------------------------------------------

_STATE = {"working": "busy", "blocked": "waiting", "idle": "idle", "done": "idle"}

# The last snapshot the tick took, for request handlers that want the rail
# without shelling out again. (snapshot, taken_at).
_last: tuple[dict[str, Any] | None, float] = (None, 0.0)


def _signature(snap: dict[str, Any] | None) -> tuple:
    """What the rail is made of. Changes when a pane appears, leaves, or changes
    state; stays put while agents sit idle, so the watcher can tell a real
    transition from a tick with nothing to say."""
    if snap is None:
        return ("down",)
    return tuple(sorted(
        (str(a.get("pane_id")), str(a.get("agent_status")), int(a.get("state_change_seq") or 0),
         session_id_of(a) or "")
        for a in agents(snap)))


def watch(store: Store, stop: "threading.Event", every: float | None = None) -> None:
    """Poll herdr's snapshot every couple of seconds and sync on change.

    The tick runs every 15s and the dashboard read the snapshot through a 30s
    cache, so a pane going blocked could take most of a minute to show in the
    rail. One `herdr api snapshot` is ~60ms, so polling it every 2s costs about
    3% of a core and makes the rail current within a dashboard poll. Backs off
    to ten seconds while the server is down. Runs on its own daemon thread; the
    tick keeps doing the slower work (proposals, pane watcher) off the same cache.
    """
    every = every or config.HERDR_POLL_SECONDS
    last_sig: tuple | None = None
    while not stop.is_set():
        delay = every
        try:
            if available():
                snap = snapshot()
                _remember(snap)
                sig = _signature(snap)
                if snap is None:
                    delay = max(every, 10.0)
                elif sig != last_sig:
                    for n in sync(store, snap):
                        store.log(n, source="herdr")
                last_sig = sig
            else:
                delay = 30.0
        except Exception as e:  # noqa: BLE001 - the watcher must outlive any one bad poll
            try:
                store.log(f"herdr watch error: {e}", level="warn", source="herdr")
            except Exception:  # noqa: BLE001
                pass
            delay = max(every, 10.0)
        stop.wait(delay)


def last_snapshot(max_age: float = 30.0) -> dict[str, Any] | None:
    snap, at = _last
    if snap is not None and time.time() - at <= max_age:
        return snap
    snap = snapshot_or_none()
    _remember(snap)
    return snap


def _remember(snap: dict[str, Any] | None) -> None:
    global _last
    _last = (snap, time.time())


def sync(store: Store, snap: dict[str, Any] | None = None) -> list[str]:
    """Fold herdr's agent view into the session rows. Tick hook.

    Every Claude agent herdr sees gets its pane, name and status written onto the
    Session with the same id, or a new Session when the hooks have not reported
    that one yet. Sessions that pointed at a pane herdr no longer lists lose the
    pointer, so the rail never shows a pane that is gone. `snap` lets the watcher
    hand in the snapshot it already took instead of paying for a second one.
    """
    if not config.HERDR_SYNC or not available():
        return []
    if snap is None:
        snap = snapshot()
        _remember(snap)
    if snap is None:
        return []
    _refresh_window_title(snap)
    now = iso(utcnow())
    notes: list[str] = []
    seen_panes: dict[str, dict[str, Any]] = {}
    for a in agents(snap):
        sid = session_id_of(a)
        if sid:
            seen_panes[sid] = a

    # Sessions whose pane just entered or left `blocked` on this tick, posted after
    # the lock is released so a notice write never sits inside the session lock.
    blocked_now: list[Session] = []
    unblocked: list[str] = []
    with store.session_lock:
        items = store.sessions()
        by_id = {s.session_id: s for s in items}
        changed = False
        for sid, a in seen_panes.items():
            s = by_id.get(sid)
            status = a.get("agent_status")
            prev = s.herdr_status if s is not None else None
            if s is None:
                s = Session(session_id=sid, state=_STATE.get(status, "idle"),  # type: ignore[arg-type]
                            cwd=a.get("foreground_cwd") or a.get("cwd"),
                            first_seen=now, last_event=now, state_since=now)
                if s.cwd:
                    s.domain = config.domain_for_path(s.cwd)
                items.append(s)
                by_id[sid] = s
                notes.append(f"herdr pane {a.get('pane_id')} is session {sid[:8]}")
                changed = True
            if (s.herdr_pane, s.herdr_agent, s.herdr_status) != (
                    a.get("pane_id"), a.get("name") or a.get("agent"), status):
                s.herdr_pane = a.get("pane_id")
                s.herdr_agent = a.get("name") or a.get("agent")
                s.herdr_status = status if status in _STATE or status == "unknown" else None
                changed = True
            # The nudge the hooks cannot give. An AskUserQuestion fires no hook, so
            # the hooks still say busy while the pane sits on a question; herdr
            # reads the screen and says blocked. When the hooks already said waiting
            # the toast is already up (sessions.record posted it), so nothing is
            # repeated. state_since moves with the effective state: it is what the
            # notice key and "blocked for N minutes" are both built on.
            if status == "blocked" and prev != "blocked" and s.state != "waiting":
                s.state_since = now
                s.note = _ask_text(a)
                blocked_now.append(s)
                changed = True
            elif status != "blocked" and prev == "blocked" and s.state != "waiting":
                s.note = None
                unblocked.append(sid)
                changed = True
            if s.state == "offline":
                # herdr can see it, so it is not offline, whatever the sweep concluded.
                s.state = _STATE.get(status, "idle")  # type: ignore[assignment]
                s.state_since = now
                s.offline_inferred = False
                s.offline_reason = None
                changed = True
            s.herdr_seen = now
        for s in items:
            if s.herdr_pane and s.session_id not in seen_panes:
                # A blocked pane that closed is no longer asking anything.
                if s.herdr_status == "blocked" and s.state != "waiting":
                    s.note = None
                    unblocked.append(s.session_id)
                s.herdr_pane = s.herdr_agent = s.herdr_status = None
                changed = True
        if changed:
            store.save_sessions(items)
    if blocked_now or unblocked:
        # Imported here, not at the top: sessions imports herdr lazily for the same
        # reason, and importing late on both sides is what keeps the cycle open.
        from . import sessions  # noqa: PLC0415 - see the comment above
        for s in blocked_now:
            sessions._post_waiting_notice(store, s)
            notes.append(f"{sessions.label(s)} is blocked in {s.herdr_pane}"
                         + (f": {s.note}" if s.note else ""))
        for sid in unblocked:
            sessions._retire_waiting_notices(store, sid)
    return notes
