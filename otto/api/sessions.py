"""Sessions, the logistics strip, the herdr harness, dispatch on/off, the hook ingest.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .. import dispatch, herdr, logistics, safeargs, sessions
from .. import daemon as _d
from ..models import iso, utcnow

router = APIRouter()


@router.get("/api/sessions")
def list_sessions(domain: str | None = None, live: bool = False) -> dict[str, Any]:
    items = _d.store.sessions()
    if domain:
        items = [s for s in items if s.domain == domain]
    if live:
        items = [s for s in items if s.live]
    return {
        "summary": sessions.summary(_d.store, domain),
        "sessions": [s.model_dump() for s in items],
    }

@router.get("/api/sessions/doctor")
def sessions_doctor() -> dict[str, Any]:
    return sessions.doctor(_d.store)

class SessionPatch(BaseModel):
    title: str

@router.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, req: SessionPatch) -> dict[str, Any]:
    """The owner naming a session. The title outranks what the transcript said."""
    try:
        sess = sessions.rename(_d.store, session_id, req.title)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"session {sess.session_id[:8]} named '{sess.title}'", source="sessions")
    return sess.model_dump()

# ---- logistics dispatch and the herdr harness -----------------------------------

@router.get("/api/logistics")
def get_logistics() -> dict[str, Any]:
    return logistics.view(_d.store)

@router.post("/api/logistics/refresh")
def refresh_logistics() -> dict[str, Any]:
    """Recompute the strip now rather than on the next tick."""
    logistics.refresh(_d.store, herdr.snapshot())
    return logistics.view(_d.store)

@router.post("/api/logistics/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str) -> dict[str, Any]:
    try:
        p, msg = logistics.approve(_d.store, proposal_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, herdr.HerdrError) as e:
        raise HTTPException(409, str(e)) from e
    _d.store.log(msg, source="logistics", task_id=p.task_id)
    logistics.refresh(_d.store, herdr.snapshot())
    return {"ok": True, "message": msg, "proposal": p.model_dump()}

@router.post("/api/logistics/proposals/{proposal_id}/dismiss")
def dismiss_proposal(proposal_id: str) -> dict[str, Any]:
    try:
        p = logistics.dismiss(_d.store, proposal_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    _d.store.log(f"dismissed suggestion {p.id[:6]} ({p.task_id[:6]} -> {p.agent})",
              source="logistics", task_id=p.task_id)
    logistics.refresh(_d.store, herdr.snapshot())
    return {"ok": True, "proposal": p.model_dump()}

def _check(ok: bool, what: str, value: Any) -> None:
    """Field validators raise ValueError, which pydantic turns into a 422 that
    names the field. The values here reach a herdr argv or a pane shell line."""
    if not ok:
        raise ValueError(f"not a valid {what}: {str(value)[:64]!r}")

def _require_herdr_target(target: str) -> None:
    """Path-parameter twin of the validators: a target that cannot be a pane id
    or agent name never reaches the herdr CLI (a leading '-' would be an option)."""
    if not safeargs.is_herdr_target(target):
        raise HTTPException(422, f"not a pane id or agent name: {target[:64]!r}")

class DispatchToRequest(BaseModel):
    task_id: str
    target: str   # herdr agent name or pane id

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        _check(safeargs.is_herdr_target(v), "pane id or agent name", v)
        return v

@router.post("/api/logistics/dispatch")
def dispatch_to_pane(req: DispatchToRequest) -> dict[str, Any]:
    try:
        msg = logistics.dispatch_to(_d.store, req.task_id, req.target)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, herdr.HerdrError) as e:
        raise HTTPException(409, str(e)) from e
    _d.store.log(msg, source="logistics")
    logistics.refresh(_d.store, herdr.snapshot())
    return {"ok": True, "message": msg}

class HerdrOpenRequest(BaseModel):
    cwd: str
    label: str | None = None
    name: str | None = None
    resume: str | None = None   # a Claude session id to pick back up in the pane

    # resume is typed into a pane shell (herdr.claude_command), so its shape is
    # checked here as well as at the sink. name becomes the herdr agent name.
    @field_validator("resume")
    @classmethod
    def _resume(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_session_id(v), "session id", v)
        return v or None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_agent_name(v), "agent name ([a-z][a-z0-9_-]{0,31})", v)
        return v or None

@router.get("/api/herdr")
def get_herdr() -> dict[str, Any]:
    snap = herdr.snapshot() if herdr.available() else None
    return {"installed": herdr.available(), "binary": herdr.binary(),
            "running": snap is not None,
            "agents": herdr.agents(snap), "workspaces": herdr.workspaces(snap)}

@router.post("/api/herdr/ensure")
def ensure_herdr() -> dict[str, Any]:
    started, msg = herdr.ensure_server()
    _d.store.log(msg, source="herdr")
    return {"started": started, "message": msg}

@router.post("/api/herdr/open")
def open_in_herdr(req: HerdrOpenRequest) -> dict[str, Any]:
    """A new workspace with a Claude session in it, optionally resuming one."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    herdr.ensure_server()
    try:
        agent = herdr.start_claude(req.cwd, label=req.label, name=req.name, resume=req.resume)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    _d.store.log(f"herdr: claude up in {agent.get('pane_id')} ({req.cwd})"
              + (f", resumed {req.resume[:8]}" if req.resume else ""), source="herdr")
    herdr.sync(_d.store)
    return {"ok": True, "agent": agent}

class HerdrWorktreeRequest(BaseModel):
    cwd: str                        # a path inside the repo
    branch: str | None = None       # create: required. open: this or path
    base: str | None = None         # create only: ref a NEW branch starts from
    path: str | None = None         # open only: the checkout path instead of branch
    label: str | None = None
    name: str | None = None         # agent name once claude is up
    start_claude: bool = True

    # branch and base go through herdr to git; a leading '-' would be read as an
    # option there. path likewise must not look like a flag.
    @field_validator("branch", "base")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_branch(v), "branch name", v)
        return v or None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v:
            _check(not v.lstrip().startswith("-"), "worktree path", v)
        return v or None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_agent_name(v), "agent name ([a-z][a-z0-9_-]{0,31})", v)
        return v or None

def _worktree_response(ws_id: str, pane_id: str, req: HerdrWorktreeRequest,
                       verb: str) -> dict[str, Any]:
    """Shared tail of create and open: launch claude in the new root pane when
    asked, log it, sync the rail, and answer with the ids the dashboard needs."""
    agent: dict[str, Any] | None = None
    if req.start_claude:
        try:
            agent = herdr.launch_claude_in_pane(pane_id, name=req.name)
        except herdr.HerdrError as e:
            # The worktree exists and is open; only the launch failed. Say so with
            # the ids rather than hide a workspace the owner now has.
            _d.store.log(f"herdr: worktree {verb} {ws_id} but claude did not start: {e}",
                      level="warn", source="herdr")
            raise HTTPException(409, f"worktree open as {ws_id} ({pane_id}); {e}") from e
    _d.store.log(f"herdr: worktree {verb} {ws_id} ({req.branch or req.path}) from {req.cwd}"
              + (f", claude up in {pane_id}" if agent else ""), source="herdr")
    herdr.sync(_d.store)
    return {"ok": True, "workspace_id": ws_id, "pane_id": pane_id, "agent": agent}

@router.get("/api/herdr/worktrees")
def list_herdr_worktrees(cwd: str) -> dict[str, Any]:
    """The worktrees of the repo containing cwd, and which are open in herdr."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    try:
        return herdr.worktree_list(cwd)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e

@router.post("/api/herdr/worktree/create")
def create_herdr_worktree(req: HerdrWorktreeRequest) -> dict[str, Any]:
    """A git worktree for a branch, open as a workspace, with claude in it."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    if not req.branch:
        raise HTTPException(422, "branch is required")
    herdr.ensure_server()
    try:
        ws_id, pane_id = herdr.worktree_create(req.cwd, req.branch, base=req.base, label=req.label)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    return _worktree_response(ws_id, pane_id, req, "created")

@router.post("/api/herdr/worktree/open")
def open_herdr_worktree(req: HerdrWorktreeRequest) -> dict[str, Any]:
    """An existing worktree (by branch or path) open as a workspace, with claude."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    if bool(req.branch) == bool(req.path):
        raise HTTPException(422, "name the worktree by branch or path, exactly one")
    herdr.ensure_server()
    try:
        ws_id, pane_id = herdr.worktree_open(req.cwd, branch=req.branch, path=req.path,
                                             label=req.label)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    return _worktree_response(ws_id, pane_id, req, "opened")

@router.post("/api/herdr/focus/{target}")
def focus_in_herdr(target: str) -> dict[str, Any]:
    _require_herdr_target(target)
    try:
        herdr.focus(target)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    sessions.focus_window(herdr.client_host_pid() or 0)
    return {"ok": True}

# The rail's last-line preview. Each read is a herdr subprocess plus a screen
# scrape, and the dashboard asks for every working pane on every render, so an
# answer is held for ten seconds per target. A preview ten seconds stale is still
# a preview; a herdr call per render per pane is a load the server did not sign
# up for.
_PEEK_TTL_SECONDS = 10.0

_peek_cache: dict[str, tuple[float, dict[str, Any]]] = {}

@router.get("/api/herdr/peek/{target}")
def peek_herdr(target: str) -> dict[str, Any]:
    _require_herdr_target(target)
    now = time.time()
    hit = _peek_cache.get(target)
    if hit is not None and now - hit[0] < _PEEK_TTL_SECONDS:
        return hit[1]
    try:
        text = herdr.read(target, lines=12)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    body = {"target": target, "line": herdr.last_line(text), "at": iso(utcnow())}
    _peek_cache[target] = (now, body)
    return body

@router.post("/api/sessions/{session_id}/open")
def open_session(session_id: str) -> dict[str, Any]:
    """Bring a live session's window forward, or resume an ended one in a new
    Windows Terminal tab. The daemon runs in the owner's interactive session (the
    OttoDaemon task is LogonType Interactive), so it can touch his windows."""
    if session_id in ("busy", "waiting", "idle", "offline"):
        raise HTTPException(404, f"no session matching {session_id}")
    if _d.store.get_session(session_id) is None:
        raise HTTPException(404, f"no session matching {session_id}")
    ok, msg = sessions.open_session(_d.store, session_id)
    _d.store.log(f"session open {session_id[:8]}: {msg}", source="sessions",
              level="info" if ok else "warn")
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": ok, "message": msg}

@router.post("/api/sessions/{state}")
def record_session(state: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest one Claude Code hook event. Called by scripts/otto_hook.py.

    THIS ENDPOINT IS ON THE CRITICAL PATH OF EVERY TURN on this machine, so it does
    exactly one small read-modify-write under the dedicated session lock and
    returns. Nothing here may grow into a probe, a spawn, or anything else that can
    block: the caller is a hook, and a hook that takes a second makes every prompt
    the owner submits take a second longer.
    """
    if state not in ("busy", "waiting", "idle", "offline"):
        raise HTTPException(400, f"unknown session state {state}")
    try:
        sess = sessions.record(_d.store, state, payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return sess.model_dump()

@router.get("/api/dispatch")
def get_dispatch() -> dict[str, Any]:
    return dispatch.status(_d.store)

@router.post("/api/dispatch")
def set_dispatch(enabled: bool) -> dict[str, Any]:
    dispatch.set_enabled(enabled)
    _d.store.log(f"auto-dispatch {'enabled' if enabled else 'DISABLED'}",
              level="warn", source="dispatch")
    return dispatch.status(_d.store)
