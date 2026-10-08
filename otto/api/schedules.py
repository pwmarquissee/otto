"""Schedules, autorun, snapshots, feeds, refresh.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, feeds, launch, refresh
from .. import daemon as _d
from ..models import Cadence, Schedule, Snapshot
from ..runners import scheduled

router = APIRouter()


class ScheduleRequest(BaseModel):
    """Create or replace a schedule. This is the primitive personal routines need."""

    name: str
    command: str
    domain: str = config.PERSONAL
    # Settable because there is now more than one refresh schedule (work and personal)
    # and only the work one is seeded in code, so the personal one has to be creatable
    # through the API. Defaults to report, which is what every user-created schedule
    # was before this existed.
    runner: Literal["report", "refresh", "ingest", "launch", "writing"] = "report"
    description: str | None = None
    # The level a launch run gets; see models.Schedule.permissions.
    permissions: Literal["plan", "yolo"] = "yolo"
    enabled: bool = True
    autostart: bool = False
    max_age_hours: int | None = None
    # Cadence, flattened so the CLI can pass simple flags.
    kind: str = "daily"
    at: str = "08:00"
    days: list[str] = []
    hours: int | None = None
    min_interval_days: int | None = None

class SnapshotRequest(BaseModel):
    kind: str
    domain: str = config.WORK
    summary: str | None = None
    items: list[dict[str, Any]] = []
    source: str = "claude-session"

class StampRequest(BaseModel):
    status: str = "ok"
    run_id: str | None = None
    at: str | None = None  # backdate, for migrating existing ledgers

@router.post("/api/schedules/{name}/run")
def run_schedule(name: str) -> dict[str, Any]:
    sched = _d.store.get_schedule(name)
    if sched is None:
        raise HTTPException(404, f"no schedule named {name}")
    try:
        run, msg = launch.launch(_d.store, sched)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    _d.store.log(msg, source="launch", run_id=run.id)
    return {"message": msg, "run": run.model_dump()}

@router.put("/api/schedules/{name}")
def put_schedule(name: str, req: ScheduleRequest) -> dict[str, Any]:
    """Create or replace a schedule. How personal routines get added."""
    if req.domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    try:
        cadence = Cadence(
            kind=req.kind, at=req.at, days=[d.lower()[:3] for d in req.days],
            hours=req.hours, min_interval_days=req.min_interval_days,
        )
        sched = Schedule(
            name=name, command=req.command, domain=req.domain,  # type: ignore[arg-type]
            cadence=cadence, enabled=req.enabled, autostart=req.autostart,
            runner=req.runner, permissions=req.permissions,
            description=req.description, max_age_hours=req.max_age_hours,
        )
    except ValueError as e:
        raise HTTPException(400, f"invalid schedule: {e}") from e

    with _d.store.lock:
        existing = _d.store.get_schedule(name)
        if existing is not None:
            # Preserve run history across an edit.
            sched.last_run = existing.last_run
            sched.last_status = existing.last_status
            sched.last_run_id = existing.last_run_id
            # And everything else the request body cannot express. `runner` is not a
            # field on ScheduleRequest, so without this an edit to the built-in
            # refresh schedule silently demoted it to the default runner and turned
            # `otto refresh` into a shell command that does nothing useful.
            # Only inherit when the caller left it at the default, so an explicit
            # runner in the request still wins.
            if req.runner == "report":
                sched.runner = existing.runner
            sched.consecutive_failures = existing.consecutive_failures
            sched.disabled_reason = existing.disabled_reason
            sched.last_autorun = existing.last_autorun
        _d.store.upsert_schedule(sched)
    _d.store.log(f"{'updated' if existing else 'added'} schedule {name} ({req.domain})",
              source="schedule")
    return sched.model_dump()

@router.delete("/api/schedules/{name}")
def delete_schedule(name: str) -> dict[str, Any]:
    if not _d.store.delete_schedule(name):
        raise HTTPException(404, f"no schedule named {name}")
    _d.store.log(f"removed schedule {name}", level="warn", source="schedule")
    return {"removed": name}

@router.get("/api/snapshots")
def get_snapshots() -> dict[str, Any]:
    return {k: v.model_dump() for k, v in _d.store.snapshots().items()}

@router.put("/api/snapshots/{kind}")
def put_snapshot(kind: str, req: SnapshotRequest) -> dict[str, Any]:
    """Ingest a view Otto cannot fetch itself (calendar, mail).

    A Claude session has the MCP servers; this daemon does not. So the session
    pushes, and Otto renders it with its age attached.
    """
    snap = Snapshot(kind=kind, domain=req.domain, summary=req.summary,
                    items=req.items, source=req.source)
    _d.store.put_snapshot(snap)
    _d.store.log(f"snapshot {snap.key} refreshed ({len(req.items)} items)",
              source="snapshot")
    return snap.model_dump()

@router.delete("/api/snapshots/{domain}/{kind}")
def delete_snapshot(domain: str, kind: str) -> dict[str, Any]:
    """Drop a snapshot. For a wrong push: Otto reporting invented items is worse
    than Otto reporting nothing, so a bad push must be removable."""
    if not _d.store.delete_snapshot(domain, kind):
        raise HTTPException(404, f"no snapshot {domain}/{kind}")
    _d.store.log(f"snapshot {domain}/{kind} dropped", level="warn", source="snapshot")
    return {"removed": f"{domain}/{kind}"}

@router.get("/api/feeds")
def get_feeds() -> dict[str, Any]:
    """Declared-vs-observed over the feed directories, plus each source's age.

    Read-only, and there is deliberately no write endpoint here. A producer
    contributes by writing a FILE, which is the entire point of the design: an
    HTTP push endpoint would just be the snapshot API again, and would put us back
    to teaching Otto about every source in advance.
    """
    return {
        "root": str(config.FEED_DIR),
        "producers": str(config.PRODUCERS_DIR),
        "file": config.FEED_FILE,
        "sources": [{"name": s.source.name, "domain": s.source.domain,
                     "trust": s.source.trust, "title": s.source.title,
                     "max_age_hours": s.source.max_age_hours,
                     "state": s.state, "age_hours": s.age_hours,
                     "items": s.items, "detail": s.detail}
                    for s in feeds.status(_d.store)],
        "undeclared": feeds.undeclared(),
        "ledger": _d.store.feeds(),
    }

@router.post("/api/feeds/init")
def post_feeds_init() -> dict[str, Any]:
    """Create the feed root, `producers/`, and a directory per declared source."""
    made = feeds.scaffold()
    if made:
        _d.store.log(f"feed scaffold created {len(made)} directory(ies)", source="feed")
    return {"created": made}

@router.post("/api/schedules/{name}/arm")
def arm_schedule(name: str, armed: bool = True) -> dict[str, Any]:
    """Arm or disarm a schedule for unattended cron."""
    with _d.store.lock:
        sched = _d.store.get_schedule(name)
        if sched is None:
            raise HTTPException(404, f"no schedule named {name}")
        if armed and launch.is_slash(sched.command) is False and not sched.command:
            raise HTTPException(400, "schedule has no command")
        sched.autostart = bool(armed)
        # The built-in runners keep their own kind. Arming used to rewrite anything
        # that was not `refresh` to `launch`, which turned `otto meetings ingest`
        # into a shell command the daemon would try to execute -- the same bug the
        # refresh exemption below already exists for, one runner later.
        if armed and sched.runner not in ("refresh", "ingest"):
            sched.runner = "launch"
        if armed:
            sched.consecutive_failures = 0
            sched.disabled_reason = None
        _d.store.upsert_schedule(sched)
    _d.store.log(f"{name} {'ARMED for autorun' if armed else 'disarmed'}",
              level="warn", source="schedule")
    return sched.model_dump()

@router.get("/api/autorun")
def get_autorun() -> dict[str, Any]:
    scheds = _d.store.schedules()
    armed = [s for s in scheds if s.autostart and s.runner in ("launch", "refresh", "ingest")]
    return {
        "enabled": config.SCHEDULE_AUTORUN,
        "max_concurrent": config.SCHEDULE_MAX_CONCURRENT,
        "max_failures": config.SCHEDULE_MAX_FAILURES,
        "min_gap_minutes": config.SCHEDULE_MIN_GAP_MINUTES,
        "budget_usd": config.SCHEDULE_BUDGET_USD,
        "armed": [
            {"name": s.name, "runner": s.runner, "command": s.command,
             "cadence": s.cadence.model_dump(), "last_autorun": s.last_autorun,
             "consecutive_failures": s.consecutive_failures,
             "disabled_reason": s.disabled_reason}
            for s in armed
        ],
        "tripped": [
            {"name": s.name, "reason": s.disabled_reason}
            for s in scheds if s.disabled_reason
        ],
        # Armed, due, and NOT running: the state worth staring at.
        "blocked": [
            {"name": s.name, "reason": launch.autorun_blocked(_d.store, s)}
            for s in armed
            if s.runner == "launch"
            and scheduled.is_due(s, datetime.now().astimezone())[0]
            and launch.autorun_blocked(_d.store, s)
        ],
    }

@router.post("/api/autorun")
def set_autorun(enabled: bool) -> dict[str, Any]:
    config.SCHEDULE_AUTORUN = bool(enabled)
    _d.store.log(f"autorun {'enabled' if enabled else 'DISABLED'}",
              level="warn", source="schedule")
    return get_autorun()

@router.post("/api/refresh")
def post_refresh(domain: str = "all") -> dict[str, Any]:
    """Start a refresh. `all` (the default) fans out to every wired-up domain.

    "Refresh my email and calendar" means both inboxes, so fanning out lives here
    rather than in the dashboard: the button, the CLI and any future caller all get
    the same behaviour from one place. A named domain still refreshes just that one.

    Never partial-fails silently. A domain that is skipped says why, because a button
    that appears to refresh both while quietly doing one is worse than an error.
    """
    if domain == "all":
        wanted = [d for d, kinds in config.SNAPSHOT_SOURCES.items() if kinds]
    elif domain in config.DOMAINS:
        wanted = [domain]
    else:
        raise HTTPException(400, f"unknown domain {domain!r}")

    in_flight = {r.domain for r in _d.store.runs()
                 if r.status == "running" and "mode=refresh" in (r.notes or "")}
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for d in wanted:
        if d in in_flight:
            skipped.append({"domain": d, "reason": "already in flight"})
            continue
        try:
            run = refresh.start(_d.store, d)
        except (OSError, RuntimeError, ValueError) as e:
            skipped.append({"domain": d, "reason": str(e)})
            continue
        _d.store.upsert_run(run)
        _d.store.log(f"refresh started ({d})", source="refresh", run_id=run.id)
        started.append(run.model_dump())

    if not started:
        detail = "; ".join(f"{s['domain']}: {s['reason']}" for s in skipped) or "nothing to do"
        raise HTTPException(409 if all(s["reason"] == "already in flight" for s in skipped)
                            else 500, f"no refresh started - {detail}")
    return {"runs": started, "skipped": skipped}

@router.get("/api/schedules")
def get_schedules() -> list[dict[str, Any]]:
    now_local = datetime.now().astimezone()
    out = []
    for s in _d.store.schedules():
        due, reason = scheduled.is_due(s, now_local)
        out.append({**s.model_dump(), "due": due, "due_reason": reason})
    return out

@router.post("/api/schedules/{name}/stamp")
def stamp_schedule(name: str, req: StampRequest) -> dict[str, Any]:
    sched = _d.store.stamp(name, req.status, req.run_id, req.at)
    if sched is None:
        raise HTTPException(404, f"no schedule named {name}")
    _d.store.log(f"stamped {name} ({req.status})", source="schedule")
    return sched.model_dump()

@router.post("/api/schedules/{name}/toggle")
def toggle_schedule(name: str, enabled: bool) -> dict[str, Any]:
    with _d.store.lock:
        sched = _d.store.get_schedule(name)
        if sched is None:
            raise HTTPException(404, f"no schedule named {name}")
        sched.enabled = enabled
        _d.store.upsert_schedule(sched)
    _d.store.log(f"{name} {'enabled' if enabled else 'disabled'}", source="schedule")
    return sched.model_dump()
