"""Live Claude Code session state, reported by the sessions' own hooks.

THE GAP THIS CLOSES. Otto watches runs it spawned, from the outside: a pid it
polls and an NDJSON log it parses. That leaves two holes it could name but not
see. Every session the owner opens in a terminal by hand is invisible, and for the
ones Otto did spawn, "thinking hard" and "blocked on a permission prompt" are the
same picture, because a log that has not grown in ten minutes looks the same
either way.

Claude Code will simply tell you, if you ask. It runs a hook on each turn
boundary, so mapping those events to a state gives Otto the inside view it cannot
get from the outside:

    UserPromptSubmit  -> busy       a turn started
    Notification      -> waiting    blocked, asking permission
    Stop              -> idle       turn finished
    StopFailure       -> idle       turn errored; without this it sticks at busy
    SessionStart      -> idle       session up
    SessionEnd        -> offline    session down

The event map, the `permission_prompt` matcher, the settings.local.json target and
the marker-based idempotent merge are ported from keitora (rudoi/keitora,
`src-tauri/src/hooks.rs`), which worked this out first and paid for the Windows
details. What is Otto's own is the identity model and the honesty rule below.

KEYED BY SESSION, NOT BY DIRECTORY. Keitora keys state by worktree because its
whole model is one terminal pane per worktree. Otto's unit is a session: two of
them in one repo are two different things, session_id is what Claude Code
actually hands the hook, and it is the join key back to a Run once one has been
parsed out of the stream. A session with no matching run is precisely the class
Otto could never see before.

SILENCE IS NOT A VERDICT. Keitora can ask its PTY whether a session is alive;
Otto owns no PTY and has no equivalent. So a state is only ever set by a hook
that actually fired. The single inference is `offline` after a long gap, and it
is recorded as `offline_inferred` rather than presented as observed, for the same
reason a vanished run is `orphaned` and not `failed`. A stuck `busy` is reported
as a stuck `busy`, not converted into a death Otto did not witness.

WHY THE HOOK IS NOT THIS MODULE. Hooks block the turn, so the thing on the
critical path is `scripts/otto_hook.py`: stdlib only, no otto import, no pydantic,
one POST, fail-open. Importing this package per turn would put a chunk of startup
cost on every prompt the owner submits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, notify, repos
from .models import Alert, Session, iso, safe_text, utcnow
from .store import Store

# (claude code event, matcher, resulting state).
#
# The two subtleties are both keitora's, and both cost a wrong state if you get
# them wrong:
#
#   * `Notification` is filtered to `permission_prompt` ONLY. The other
#     notification Claude Code sends is the "you have been idle" nudge, where the
#     session is idle and ready rather than blocked mid-task. Mapping that to
#     `waiting` would flip a perfectly free session into "needs the owner".
#   * `StopFailure` mirrors `Stop`, so a turn that errored out lands on `idle`
#     instead of sticking at `busy` until the offline sweep gives up on it.
#
# `SubagentStop` is deliberately absent. It fires mid-turn, every time a subagent
# finishes, and would repeatedly report the parent session as idle while it is
# still working. Otto has extra reason to care: `/daily` and `/orchestrate` fan
# out to subagents constantly, so this would be wrong more often than right.
HOOK_MAP: tuple[tuple[str, str, str], ...] = (
    ("UserPromptSubmit", "", "busy"),
    ("Notification", "permission_prompt", "waiting"),
    ("Stop", "", "idle"),
    ("StopFailure", "", "idle"),
    ("SessionStart", "", "idle"),
    ("SessionEnd", "", "offline"),
)

# Embedded in every command Otto writes so a refresh can recognize and replace its
# own entries instead of appending a duplicate every time the path changes.
HOOK_MARKER = "otto:session-hook"

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "otto_hook.py"

# Where the hooks go. User-level rather than per-repo, which is a deliberate
# divergence from keitora: it enumerates worktrees because it manages a fixed set
# of panes, whereas the whole point here is to see sessions Otto did not start,
# including ones in directories nobody registered anywhere.
#
# IT MUST BE `settings.json`, AND THAT IS NOT THE OBVIOUS CHOICE. keitora writes
# per-project `.claude/settings.local.json` because it is gitignored, which keeps
# machine-absolute paths out of every worktree's `git status`. Generalising that to
# the user level looked strictly better and is simply broken: Claude Code reads
# `permissions` from `~/.claude/settings.local.json` but does NOT load hooks from
# it. Verified twice on 2026-08-05, once by a headless run producing no hook call
# at all, and once against a control (`robot-factory.ps1`, installed in
# `~/.claude/settings.json`, which fired for the very same session).
#
# The cost of being here is real and accepted: Claude Code rewrites this file
# itself (plugin toggles, permission grants), and configsync.py holds only a
# snapshot of it for that reason. Empirically a foreign hook survives that
# rewriting indefinitely; the control above has been in this file for months. The
# merge is marker-scoped precisely so the two can coexist.
#
# `install(path=...)` still takes a target, so a per-project install in keitora's
# style remains available for a repo that wants it.
GLOBAL_SETTINGS = config.CLAUDE_DIR / "settings.json"


def _fwd(p: Path | str) -> str:
    r"""A path Git Bash will resolve without argument.

    Claude Code runs hooks through a POSIX shell on EVERY platform: /bin/sh on
    unix, Git Bash on Windows. That is the fact the whole command format turns on,
    and getting it wrong fails in a way that looks like the hook silently not
    working. A cmd.exe-syntax command errors with `rem: command not found`, and
    `>nul` quietly creates a file called `nul`.

    So: forward slashes, because bash resolves `C:/Python313/python.exe`
    unambiguously and a backslashed path depends on quoting rules; and strip the
    `\\?\` extended-length prefix, which bash cannot execute.
    """
    s = str(p)
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\"):
        if s.startswith(prefix):
            s = ("\\\\" + s[len(prefix):]) if prefix.endswith("UNC\\") else s[len(prefix):]
            break
    return s.replace("\\", "/")


def hook_command(state: str, python: str | None = None,
                 script: Path | str | None = None) -> str:
    """The shell command installed for one state.

    Guarded, silenced, and incapable of failing, in that order. `[ -f ]` short
    circuits if the interpreter or the script has moved, output goes nowhere so a
    stray print cannot corrupt the hook protocol, and `|| true` means Claude Code
    never sees a non-zero exit from Otto. A hook that can fail a turn is worse
    than no hook.

    `-S` skips site-packages. The hook imports nothing outside the stdlib by
    design, so this is free, and measured here it takes ~9ms off an interpreter
    start that happens twice per turn.
    """
    py = _fwd(python or sys.executable)
    sc = _fwd(script or HOOK_SCRIPT)
    return (f'[ -f "{py}" ] && [ -f "{sc}" ] && "{py}" -S "{sc}" {state} '
            f'>/dev/null 2>&1 || true # {HOOK_MARKER}')


def _is_ours(entry: Any) -> bool:
    """True for a hook group Otto installed.

    Matches the marker and, as a fallback, the script name, so an entry written
    before the marker existed is still recognized and upgraded rather than left
    alongside a duplicate.
    """
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks") or []:
        cmd = (h or {}).get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and (HOOK_MARKER in cmd or "otto_hook.py" in cmd):
            return True
    return False


def merged_settings(existing: Any, python: str | None = None,
                    script: Path | str | None = None) -> dict:
    """Pure merge: `existing` with Otto's session hooks ensured.

    Idempotent and additive. Unrelated keys survive untouched, other people's
    hooks on the same event survive next to Otto's, and Otto's own prior entries
    are dropped before the new ones go in so a changed interpreter path updates
    instead of accumulating.

    That last part is not hypothetical here: this machine already runs a `Stop`
    hook (robot-factory.ps1) out of settings.json. Clobbering somebody's existing
    hooks to install telemetry would be a poor trade.
    """
    root: dict = dict(existing) if isinstance(existing, dict) else {}
    hooks = root.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}

    for event, matcher, state in HOOK_MAP:
        arr = hooks.get(event)
        arr = [e for e in arr if not _is_ours(e)] if isinstance(arr, list) else []
        arr.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": hook_command(state, python, script)}],
        })
        hooks[event] = arr

    root["hooks"] = hooks
    return root


def installed_in(path: Path | None = None) -> bool:
    """Whether Otto's hooks are present in a settings file."""
    path = path or GLOBAL_SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = (data or {}).get("hooks") or {}
    if not isinstance(hooks, dict):
        return False
    return all(
        any(_is_ours(e) for e in (hooks.get(event) or []))
        for event, _m, _s in HOOK_MAP
    )


def install(path: Path | None = None, python: str | None = None,
            script: Path | str | None = None) -> tuple[bool, str]:
    """Install or refresh the hooks. Returns (changed, message).

    Writes only when the merged content actually differs, so a re-run on an
    already-hooked machine touches nothing.
    """
    path = path or GLOBAL_SETTINGS
    if not Path(script or HOOK_SCRIPT).is_file():
        return False, f"hook script missing at {script or HOOK_SCRIPT}, nothing installed"

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        existing: Any = {}
    else:
        try:
            existing = json.loads(raw)
        except ValueError as e:
            # Refuse rather than overwrite. This file holds the owner's permissions;
            # replacing an unparseable one with a fresh dict would silently drop
            # every grant in it.
            return False, f"{path} is not valid JSON ({e}); refusing to touch it"

    merged = merged_settings(existing, python, script)
    if isinstance(existing, dict) and merged == existing:
        return False, f"already installed in {path}"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True, f"session hooks installed in {path}"


def uninstall(path: Path | None = None) -> tuple[bool, str]:
    """Remove Otto's hook entries, leaving everything else alone."""
    path = path or GLOBAL_SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"nothing to remove from {path}"
    if not isinstance(data, dict):
        return False, f"nothing to remove from {path}"

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False, f"nothing to remove from {path}"

    removed = 0
    for event in list(hooks):
        arr = hooks.get(event)
        if not isinstance(arr, list):
            continue
        keep = [e for e in arr if not _is_ours(e)]
        removed += len(arr) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event)
    if not removed:
        return False, f"no otto hooks in {path}"

    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True, f"removed {removed} otto hook entr{'y' if removed == 1 else 'ies'} from {path}"


# ---- ingest ------------------------------------------------------------------

MAX_MESSAGE = 240


def _run_id_for(store: Store, session_id: str) -> str | None:
    """The Otto run this session belongs to, when there is one.

    `runners.detached` records session_id off the stream, so a spawned run can be
    matched here. No match is the interesting case rather than an error: it means
    a session Otto did not start, which is the thing this module exists to see.
    """
    for r in store.runs():
        if r.session_id and r.session_id == session_id:
            return r.id
    return None


def record(store: Store, state: str, payload: dict[str, Any] | None = None) -> Session:
    """Apply one hook event. The only writer of session state.

    `state_since` moves only on a real transition. Bumping it every event would
    make a session that has been blocked since 09:00 read as freshly blocked each
    time anything touched it, which would defeat the one question this data is
    here to answer.
    """
    payload = payload or {}
    now = iso(utcnow())
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        raise ValueError("hook payload carried no session_id")

    cwd = payload.get("cwd") or None
    prior = store.get_session(sid)

    if prior is None:
        sess = Session(session_id=sid, state=state, first_seen=now,
                       last_event=now, state_since=now)
    else:
        sess = prior.model_copy()
        if sess.state != state:
            sess.state_since = now
        sess.state = state
        sess.last_event = now

    if cwd:
        sess.cwd = str(cwd)
        sess.domain = config.domain_for_path(cwd)
        sess.repo = repos.key_for(str(cwd))
    if payload.get("transcript_path"):
        sess.transcript = str(payload["transcript_path"])
    if payload.get("permission_mode"):
        sess.permission_mode = str(payload["permission_mode"])

    # Which process, and which app it lives in. Resolved once: the chain does not
    # change for the life of a session, and walking it costs a few process reads
    # on a path that runs inside every turn.
    if sess.pid is None and payload.get("ppid"):
        for k, v in resolve_process(payload.get("ppid")).items():
            setattr(sess, k, v)
    # What it is about. Also once, and only after the first prompt exists: on
    # SessionStart the transcript holds no user message yet.
    if sess.title is None and sess.transcript and state != "offline":
        found = title_from_transcript(sess.transcript)
        if found:
            sess.title, sess.title_source = found

    if state == "busy":
        sess.turns += 1
    if state == "offline":
        sess.offline_reason = "hook"
    # A hook that actually fired always outranks an inference the sweep made.
    sess.offline_inferred = False
    # The notification text is the only thing that says WHAT it is blocked on, and
    # it is meaningless once the session is unblocked, so it is cleared on the way
    # out rather than left to describe a prompt that was answered an hour ago.
    # safe_text on both: this text comes from a hook, which got it from whatever the
    # session was reading. A calendar title with a split emoji arrived here on
    # 2026-08-15, stored fine, and then 500'd every endpoint that served the record.
    sess.note = safe_text(str(payload.get("message"))[:MAX_MESSAGE]) if state == "waiting" else None

    msg = payload.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        sess.last_message = safe_text(" ".join(msg.split())[:MAX_MESSAGE])

    if sess.run_id is None:
        sess.run_id = _run_id_for(store, sid)

    saved = store.put_session(sess)

    # The nudge. Entering `waiting` is the one transition that is a request of
    # the owner, so it is the one that raises a toast, naming the session and what
    # it asked, with the verb that gets them to it. Leaving `waiting` retires the
    # notice, so a prompt answered at the keyboard does not buzz anyone later and
    # does not sit unread in the sheet describing a question that is gone.
    was_waiting = prior is not None and prior.state == "waiting"
    if state == "waiting" and not was_waiting:
        _post_waiting_notice(store, saved)
    elif was_waiting and state != "waiting":
        _retire_waiting_notices(store, sid)

    return saved


def _post_waiting_notice(store: Store, sess: Session) -> None:
    if not config.SESSION_WAITING_TOAST:
        return
    try:
        notify.post(
            store, f"{label(sess)} is asking you"[:160],
            body=sess.note, level="warn", domain=sess.domain, source="session",
            command=f"otto sessions open {sess.session_id[:8]}",
            key=f"session-waiting:{sess.session_id}:{sess.state_since}",
        )
    except Exception as e:  # noqa: BLE001 - a toast must never fail a turn
        store.log(f"session waiting notice failed: {e}", level="warn", source="sessions")


def _retire_waiting_notices(store: Store, session_id: str) -> None:
    prefix = f"session-waiting:{session_id}:"
    try:
        for n in store.notices():
            if (n.key or "").startswith(prefix) and n.read_at is None:
                notify.mark_read(store, n.id)
    except Exception as e:  # noqa: BLE001
        store.log(f"session notice retire failed: {e}", level="warn", source="sessions")


# ---- sweep -------------------------------------------------------------------

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _age_minutes(ts: str | None, now: datetime | None = None) -> float | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 60.0


def sweep(store: Store, now: datetime | None = None) -> list[str]:
    """Age out sessions that stopped reporting. Called on tick.

    Two jobs, and only the first one changes a state:

      1. No hook of any kind for SESSION_OFFLINE_HOURS: presume gone. Flagged
         `offline_inferred` so the panel can say it concluded this rather than
         observed it, and so a session that comes back does not look like it was
         cleanly closed.
      2. Drop long-dead sessions entirely, so the list stays a picture of now.

    A stuck `busy` is NOT rewritten on silence alone. Otto does not know whether
    that turn is deep in a long agent run or died three hours ago, and picking one
    would be inventing an outcome. `alerts()` reports the ambiguity instead.

    But where the hook told us WHICH process the session is, liveness stops being
    a guess: a session whose Claude process is gone is offline, observed, in any
    state, and that is the third job here. It is what turns "presumed offline
    after 12h" into "exited" within a tick, and it is the only honest way out of
    a `busy` that died.
    """
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []

    with store.session_lock:
        items = store.sessions()
        keep: list[Session] = []
        changed = False
        titled = 0

        for s in items:
            age_min = _age_minutes(s.last_event, now)

            if s.state != "offline" and s.pid and not process_alive(s.pid, s.pid_started):
                s.state = "offline"
                s.state_since = iso(now)
                s.offline_reason = "process"
                s.offline_inferred = False
                s.note = None
                changed = True
                notes.append(f"session {s.session_id[:8]} ({label(s)[:40]}) exited: "
                             f"pid {s.pid} is gone")
                _retire_waiting_notices(store, s.session_id)
            elif (s.state != "offline" and not s.herdr_pane and age_min is not None
                    and age_min > config.SESSION_OFFLINE_HOURS * 60):
                # A session herdr can see is not silent, whatever the hooks say: an
                # idle Claude emits no hooks for as long as it sits there. Without
                # this guard the sweep called the `otto` pane offline every tick and
                # herdr.sync revived it every tick, 250 log lines in a night.
                s.state = "offline"
                s.state_since = iso(now)
                s.offline_inferred = True
                s.offline_reason = "silence"
                s.note = None
                changed = True
                notes.append(
                    f"session {s.session_id[:8]} "
                    f"({Path(s.cwd).name if s.cwd else 'unknown dir'}) presumed offline "
                    f"after {age_min / 60:.0f}h with no hook"
                )

            # Titles for sessions that predate the harness, or whose first hook came
            # before their first prompt. Offline ones too: `otto sessions --all` is
            # the list of conversations that can be resumed, and a folder name is
            # not enough to pick one. Bounded per tick: a transcript head is cheap
            # to read but 200 of them in one tick is not.
            if s.title is None and s.transcript and titled < 10:
                found = title_from_transcript(s.transcript)
                titled += 1
                if found:
                    s.title, s.title_source = found
                    changed = True

            if (s.state == "offline" and age_min is not None
                    and age_min > config.SESSION_PRUNE_DAYS * 24 * 60):
                changed = True
                continue

            keep.append(s)

        if changed:
            store.save_sessions(keep)

    return notes


# ---- reporting ---------------------------------------------------------------

def summary(store: Store, domain: str | None = None) -> dict[str, Any]:
    """Counts by state, for the status line and the dashboard."""
    items = [s for s in store.sessions() if not domain or s.domain == domain]
    live = [s for s in items if s.live]
    return {
        "total": len(items),
        "live": len(live),
        "busy": len([s for s in live if s.state == "busy"]),
        "waiting": len([s for s in live if s.state == "waiting"]),
        "idle": len([s for s in live if s.state == "idle"]),
        "offline": len([s for s in items if s.state == "offline"]),
        # The count that justifies the whole module: live sessions with no run
        # record, which is everything Otto could not see before.
        "unspawned": len([s for s in live if not s.run_id]),
        "hooks_installed": installed_in(),
    }


def _where(s: Session) -> str:
    if s.repo:
        return s.repo
    if s.cwd:
        return Path(s.cwd).name
    return s.session_id[:8]


def label(s: Session) -> str:
    """How a session is named to the owner: its title when it has one, else its place."""
    return s.title or _where(s)


def alerts(store: Store, now: datetime | None = None) -> list[Alert]:
    """Sessions worth interrupting the owner about.

    Only two, and they are deliberately different levels. Being blocked on a
    permission prompt is a real ask of the owner and warns. A long turn is a fact
    worth knowing and stays info, because a `/daily` fan-out legitimately runs for
    an hour and an alarm that fires on normal operation trains the eye to ignore the
    row that matters.
    """
    now = now or datetime.now(timezone.utc)
    out: list[Alert] = []

    for s in store.sessions():
        if not s.live:
            continue
        held = _age_minutes(s.state_since, now)
        if held is None:
            continue

        if s.state == "waiting" and held >= config.SESSION_WAITING_ALERT_MINUTES:
            detail = f": {s.note}" if s.note else ""
            out.append(Alert(
                level="warn", domain=s.domain,
                source=f"session/{s.session_id[:8]}",
                message=(f"{label(s)} has been waiting on you for "
                         f"{_human(held)}{detail}"),
            ))
        elif s.state == "busy" and held >= config.SESSION_BUSY_ALERT_HOURS * 60:
            # With a pid on record the sweep would already have called a dead
            # process offline, so a long busy here is a live process on a long
            # turn, and the message can say so instead of shrugging.
            tail = ("the process is alive, so it is still working"
                    if s.pid else "still working or died mid-turn, Otto cannot tell")
            out.append(Alert(
                level="info", domain=s.domain,
                source=f"session/{s.session_id[:8]}",
                message=f"{label(s)} has been on one turn for {_human(held)}; {tail}",
            ))

    return out


def _human(minutes: float) -> str:
    if minutes < 90:
        return f"{int(minutes)}m"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def gaps(store: Store) -> list[dict]:
    """Blind spots in the session view itself.

    Both of these are the same shape as the detector that found `scout` had no
    staleness alarm: not something broken, something with no watcher. A hook that
    is installed but silently not firing is the worse of the two, because the
    panel looks fine and reports nothing forever.
    """
    out: list[dict] = []

    if not installed_in():
        out.append({
            "id": "gap:session-hooks",
            "kind": "unwatched-sessions",
            "domain": config.WORK,
            "title": "Otto cannot see any session it did not spawn",
            "why": "Without the session hooks, a Claude Code session opened by hand "
                   "is invisible, and a spawned one blocked on a permission "
                   "prompt is indistinguishable from one that is thinking.",
            "command": "otto sessions install",
            "score": 60,
        })
        return out

    items = store.sessions()
    if not items:
        out.append({
            "id": "gap:session-hooks-silent",
            "kind": "unwatched-sessions",
            "domain": config.WORK,
            "title": "session hooks are installed but have never fired",
            "why": "Either no session has run since they went in, or they are "
                   "broken and the empty panel will keep looking healthy forever.",
            "command": "otto sessions doctor",
            "score": 50,
        })

    return out


def doctor(store: Store) -> dict[str, Any]:
    """Why the hooks might not be reporting. Read-only.

    Exists because every failure mode here is silent. A broken hook produces an
    empty list, which is exactly what a quiet morning produces.
    """
    py = _fwd(sys.executable)
    checks = [
        {"name": "hook script present", "ok": HOOK_SCRIPT.is_file(),
         "detail": str(HOOK_SCRIPT)},
        {"name": "interpreter resolvable", "ok": Path(sys.executable).is_file(),
         "detail": py},
        {"name": "hooks installed", "ok": installed_in(),
         "detail": str(GLOBAL_SETTINGS)},
    ]

    items = store.sessions()
    last = items[0].last_event if items else None
    age = _age_minutes(last)
    checks.append({
        "name": "a hook has fired",
        "ok": bool(items),
        "detail": (f"{len(items)} session(s), last event {last}"
                   if items else "no session has ever reported"),
    })
    if items and age is not None and age > 24 * 60:
        checks.append({
            "name": "recent events",
            "ok": False,
            "detail": f"last hook was {_human(age)} ago; hooks may have stopped firing",
        })

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "settings": str(GLOBAL_SETTINGS),
        "command": hook_command("busy"),
        "summary": summary(store),
    }


# ---- the harness: which process, what it is about, how to get back to it -----
#
# Keitora can answer all three because it owns the PTY. Otto owns nothing, so each
# is recovered from what is already there: the hook's parent pid, the transcript
# Claude Code writes anyway, and the terminal app's window.

# Process names Claude Code runs as. `node` covers an npm install launched through
# node directly; `claude` the native launcher on unix.
_CLAUDE_NAMES = frozenset({"claude.exe", "claude", "node.exe", "node"})

# Apps a session can be running in, by the process name found above it. Anything
# else (python.exe for a detached Otto run, an unknown terminal) is left unnamed
# rather than guessed.
_HOSTS = {
    "windowsterminal.exe": "Windows Terminal",
    "cursor.exe": "Cursor",
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code",
    "wezterm-gui.exe": "WezTerm",
    "alacritty.exe": "Alacritty",
    "conhost.exe": "console",
    "iterm2": "iTerm",
    "terminal": "Terminal",
}


def resolve_process(ppid: Any) -> dict[str, Any]:
    """From the hook's parent (the shell Claude Code ran it in) to the Claude
    process and the app hosting it. Empty dict when it cannot be told.

    Walks up at most eight parents each way. The shell is alive for exactly as
    long as the hook's POST, which is why this runs inside the request rather
    than on the tick: by the next tick the chain has a hole in it.
    """
    try:
        import psutil  # noqa: PLC0415 - daemon-side only; the hook never imports this
        p: Any = psutil.Process(int(ppid))
    except Exception:  # noqa: BLE001 - bad pid, gone, or no psutil: no verdict
        return {}
    try:
        claude = None
        for _ in range(8):
            if p is None:
                break
            if p.name().lower() in _CLAUDE_NAMES:
                claude = p
                break
            p = p.parent()
        if claude is None:
            return {}
        out: dict[str, Any] = {"pid": claude.pid, "pid_started": claude.create_time()}
        q = claude.parent()
        for _ in range(8):
            if q is None:
                break
            name = _HOSTS.get(q.name().lower())
            if name:
                out["host"], out["host_pid"] = name, q.pid
                break
            q = q.parent()
        return out
    except Exception:  # noqa: BLE001 - a process vanished mid-walk
        return {}


def process_alive(pid: int, started: float | None) -> bool:
    """Is the Claude process the hook told us about still the same live process?

    Three checks, because a pid alone is recycled by Windows within hours: the pid
    exists, it is a Claude-shaped process, and it was created when ours was (within
    two seconds, which absorbs clock rounding in the create time).
    """
    try:
        import psutil  # noqa: PLC0415
        p = psutil.Process(int(pid))
        if p.name().lower() not in _CLAUDE_NAMES:
            return False
        if started is not None and abs(p.create_time() - float(started)) > 2.0:
            return False
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 - NoSuchProcess, AccessDenied, no psutil
        return False


_TITLE_SKIP_PREFIXES = ("<local-command", "<system-reminder", "<bash-", "<user-memory")


def title_from_transcript(path: str | Path, max_lines: int = 400) -> tuple[str, str] | None:
    """(title, source) from the head of a Claude Code transcript, or None.

    Claude Code writes a `summary` line at the top of a transcript it resumed or
    compacted, which is its own title for the conversation and the best one we can
    get. Otherwise the first real user prompt, flattened to one line. A slash
    command arrives wrapped in `<command-name>` tags and becomes just the command.
    Reads at most `max_lines` lines: the first prompt is always near the top, and a
    long transcript is exactly the file not to read whole on a hook path.
    """
    import re  # noqa: PLC0415

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            kind = d.get("type")
            if kind == "summary" and isinstance(d.get("summary"), str) and d["summary"].strip():
                return _clean_title(d["summary"]), "summary"
            if kind != "user" or d.get("isMeta") or d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content
                                   if isinstance(c, dict) and c.get("type") == "text")
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text or text.startswith(_TITLE_SKIP_PREFIXES):
                continue
            m = re.search(r"<command-name>\s*(/?[\w:-]+)\s*</command-name>", text)
            if m:
                args = re.search(r"<command-args>\s*(.*?)\s*</command-args>", text, re.S)
                extra = f" {args.group(1).strip()}" if args and args.group(1).strip() else ""
                return _clean_title(m.group(1) + extra), "prompt"
            if text.startswith("<"):
                continue  # some other wrapper; the next user line is the prompt
            return _clean_title(text), "prompt"
    return None


def _clean_title(text: str) -> str:
    one = " ".join(str(text).split())
    one = safe_text(one)
    if len(one) > config.SESSION_TITLE_MAX:
        one = one[: config.SESSION_TITLE_MAX - 1].rstrip() + "…"
    return one


def rename(store: Store, session_id: str, title: str) -> Session:
    """The owner naming a session. Outranks anything the transcript said."""
    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError(f"no session matching {session_id}")
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("a title cannot be empty")
    sess.title = _clean_title(title)
    sess.title_source = "user"
    return store.put_session(sess)


def open_session(store: Store, session_id: str) -> tuple[bool, str]:
    """Get the owner back to a session. Returns (ok, message).

    A live session is in a window somewhere: bring that app's window forward
    (Windows Terminal cannot be asked to switch to a particular tab from outside,
    so the tab is the last step he takes himself). A session that has ended is
    reopened in a new Windows Terminal tab in its directory with
    `claude --resume`, which is what keitora's "reattach with scrollback" becomes
    when nobody owns the PTY: the conversation comes back, the scrollback does not.
    """
    sess = store.get_session(session_id)
    if sess is None:
        return False, f"no session matching {session_id}"
    if sess.live and sess.herdr_pane:
        # It lives in herdr: ask herdr to focus the pane (and mark it seen), then
        # bring whatever window hosts the herdr client forward.
        from . import herdr  # noqa: PLC0415 - herdr imports Session; keep the edge one-way at import
        try:
            herdr.focus(sess.herdr_agent or sess.herdr_pane)
        except herdr.HerdrError as e:
            return False, f"herdr could not focus {sess.herdr_pane}: {e}"
        host = herdr.client_host_pid()
        focused = focus_window(host) if host else False
        return True, (f"focused {label(sess)} in herdr pane {sess.herdr_pane}"
                      + ("" if focused else "; attach a client with `herdr` to see it"))
    if sess.live:
        if sess.pid and not process_alive(sess.pid, sess.pid_started):
            return False, (f"{label(sess)} is recorded live but its process is gone; "
                           f"the next tick will mark it offline, then open it again to resume")
        if sess.host_pid and focus_window(sess.host_pid):
            return True, f"brought {sess.host} forward; {label(sess)} is one of its tabs"
        where = f" in {sess.host}" if sess.host else ""
        return False, f"{label(sess)} is live{where} but its window could not be focused"
    return resume_in_terminal(sess)


def focus_window(pid: int) -> bool:
    """Bring the first visible top-level window owned by `pid` to the foreground.

    Windows only. Windows refuses SetForegroundWindow from a process that is not
    itself in the foreground unless that process has recently sent input, and the
    daemon never has, so an Alt keypress is synthesised first: the documented
    workaround that every launcher uses, and harmless (a bare Alt press and release
    changes nothing).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except ImportError:
        return False
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER: top-level only
            found.append(hwnd)
            return False
        return True

    try:
        user32.EnumWindows(_enum, 0)
        if not found:
            return False
        hwnd = found[0]
        user32.keybd_event(0x12, 0, 0, 0)        # VK_MENU down
        user32.keybd_event(0x12, 0, 0x0002, 0)   # KEYEVENTF_KEYUP
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)           # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def resume_in_terminal(sess: Session) -> tuple[bool, str]:
    """Open a new Windows Terminal tab in the session's directory running
    `claude --resume <id>`. The tab is titled with the session's title so it reads
    the same in the terminal as it does in Otto."""
    if not sess.cwd or not Path(sess.cwd).is_dir():
        return False, f"cannot resume {label(sess)}: its directory {sess.cwd!r} is gone"
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if not wt:
        return False, ("Windows Terminal (wt.exe) is not on PATH; run by hand: "
                       f"cd {sess.cwd} && claude --resume {sess.session_id}")
    title = label(sess)[:60]
    cmd = [wt, "-w", "0", "new-tab", "-d", sess.cwd, "--title", title,
           "powershell", "-NoExit", "-Command", f"claude --resume {sess.session_id}"]
    try:
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True)
    except OSError as e:
        return False, f"could not start Windows Terminal: {e}"
    return True, f"resuming {title} in a new Windows Terminal tab ({Path(sess.cwd).name})"
