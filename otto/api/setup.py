"""First-run setup, the settings file, the restart helper, integration probes.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, herdr, safeargs, sessions, setup
from .. import daemon as _d
from .schedules import arm_schedule
from .tasks import TaskRequest, create_task

router = APIRouter()


# ---- setup: first run -----------------------------------------------------------
# One engine behind the dashboard's Setup view and `otto setup` (otto/setup.py).
# Everything here is loopback and goes through OriginGuard like every other route.
# Writes go to <OTTO_HOME>/otto.env (otto/settings.py); config is import-time, so a
# write reports restart_needed and /api/daemon/restart carries the restart.

class SetupSettingsRequest(BaseModel):
    values: dict[str, str | None]

class SetupStepRequest(BaseModel):
    step: str

class SetupFirstCardRequest(BaseModel):
    title: str
    detail: str | None = None

class SetupSchedulesRequest(BaseModel):
    arm: list[str] = []

@router.get("/api/setup")
def get_setup() -> dict[str, Any]:
    return setup.view(_d.store)

@router.post("/api/setup/settings")
def post_setup_settings(req: SetupSettingsRequest) -> dict[str, Any]:
    values = dict(req.values)
    identity_keys = {"OTTO_OWNER_NAME", "OTTO_ORG_NAME", "OTTO_WORK_ROOTS", "OTTO_PERSONAL_ROOTS"}
    try:
        if identity_keys & set(values):
            ident = setup.validate_identity({k: v for k, v in values.items() if k in identity_keys})
            values = {**{k: v for k, v in values.items() if k not in identity_keys}, **ident}
        if "OTTO_INTEGRATIONS" in values and values["OTTO_INTEGRATIONS"] is not None:
            allowed = {c[0] for c in setup.INTEGRATION_CHOICES}
            picked = [x.strip().lower() for x in str(values["OTTO_INTEGRATIONS"]).split(",") if x.strip()]
            bad = [x for x in picked if x not in allowed and x != "none"]
            if bad:
                raise ValueError(f"unknown integration: {', '.join(bad)}")
            values["OTTO_INTEGRATIONS"] = ",".join(x for x in picked if x != "none") or "none"
        with _d.store.lock:
            return setup.write_settings(_d.store, values)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(500, f"could not write {config.SETTINGS_PATH}: {e}") from e

@router.post("/api/setup/hooks/install")
def post_setup_hooks() -> dict[str, Any]:
    try:
        changed, message = sessions.install()
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    if changed:
        _d.store.log(f"setup: {message}", source="setup")
    return {"changed": changed, "message": message}

@router.post("/api/setup/herdr/up")
def post_setup_herdr() -> dict[str, Any]:
    started, message = herdr.ensure_server()
    ok = started or herdr.server_running()
    if started:
        _d.store.log(f"setup: {message}", source="herdr")
    return {"ok": ok, "message": message}

@router.post("/api/setup/first-card")
def post_setup_first_card(req: SetupFirstCardRequest) -> dict[str, Any]:
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    return create_task(TaskRequest(title=title, detail=(req.detail or "").strip() or None,
                                   status="backlog", auto=False))

@router.post("/api/setup/schedules")
def post_setup_schedules(req: SetupSchedulesRequest) -> dict[str, Any]:
    armed: list[str] = []
    for name in req.arm:
        if not safeargs.is_agent_name(name):
            # Schedule names share the agent-name shape (lowercase, digits, dashes);
            # anything else never matches a seeded schedule and is not worth a 404 loop.
            raise HTTPException(400, f"bad schedule name: {name!r}")
        arm_schedule(name, armed=True)
        armed.append(name)
    return {"armed": armed}

@router.post("/api/setup/skip")
def post_setup_skip(req: SetupStepRequest) -> dict[str, Any]:
    try:
        with _d.store.lock:
            setup.skip(_d.store, req.step)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return setup.view(_d.store)

@router.post("/api/setup/unskip")
def post_setup_unskip(req: SetupStepRequest) -> dict[str, Any]:
    try:
        with _d.store.lock:
            setup.skip(_d.store, req.step, undo=True)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return setup.view(_d.store)

@router.post("/api/setup/complete")
def post_setup_complete() -> dict[str, Any]:
    with _d.store.lock:
        setup.complete(_d.store)
    return setup.view(_d.store)

@router.post("/api/setup/reset")
def post_setup_reset() -> dict[str, Any]:
    with _d.store.lock:
        setup.reset(_d.store)
    return setup.view(_d.store)

@router.post("/api/daemon/restart")
def post_daemon_restart() -> dict[str, Any]:
    import subprocess
    pid = psutil.Process().pid
    repo = str(Path(__file__).resolve().parent.parent)
    creation = (0x00000200 | 0x08000000) if os.name == "nt" else 0
    try:
        subprocess.Popen(_d._restart_helper_argv(pid), cwd=repo, env=herdr.clean_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=creation, close_fds=True)
    except OSError as e:
        raise HTTPException(500, f"could not spawn the restart helper: {e}") from e
    _d.store.log("restart requested from the dashboard; the keepalive helper brings the daemon back",
              source="daemon")
    threading.Thread(target=_d._exit_after, args=(0.4,), daemon=True, name="otto-restart").start()
    return {"ok": True, "pid": pid, "message": "restarting; poll /api/health until the pid changes"}

@router.get("/api/integrations")
def get_integrations() -> list[dict[str, Any]]:
    return [i.model_dump() for i in _d.store.integrations()]

@router.post("/api/integrations/probe")
def probe_integrations() -> list[dict[str, Any]]:
    _d.refresh_integrations()
    return [i.model_dump() for i in _d.store.integrations()]
