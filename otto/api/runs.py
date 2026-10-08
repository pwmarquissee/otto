"""Runs: list, inspect, spawn, kill, finish, prune, acknowledge.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import activity, config, persona, verdict
from .. import daemon as _d
from ..models import iso, utcnow
from ..runners import detached

router = APIRouter()


class SpawnRequest(BaseModel):
    name: str
    prompt: str
    cwd: str
    agent: str | None = None
    mode: str = "headless"
    task_id: str | None = None
    tier: str | None = None
    skip_permissions: bool = True
    domain: str | None = None  # inferred from cwd when omitted
    # Omitted means config.DEFAULT_MODEL, not "inherit whatever settings.json says".
    # detached.spawn() has taken a model since thread notes needed one, but this
    # field did not exist, so the API and `otto spawn` could not reach it.
    model: str | None = None
    # Free-text run notes. `otto task open` sets "mode=task | mode=attended" so
    # dispatch.settle routes the attended session's outcome back onto the card.
    notes: str | None = None
    # Attended sessions run under the owner's own permission prompts, so the unattended
    # findings/outreach rules are the wrong system prompt for them. Default keeps
    # the old behaviour for every other caller.
    system_extra: str | None = None

class DoneRequest(BaseModel):
    status: str = "ok"
    exit_code: int | None = 0
    notes: str | None = None

@router.get("/api/runs")
def list_runs(limit: int = 80) -> list[dict[str, Any]]:
    return [{**r.model_dump(), "verdict": verdict.verdict(r)} for r in _d.store.runs()[:limit]]

@router.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = _d.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return {**run.model_dump(), "verdict": verdict.verdict(run),
            "plan": verdict.plan(run, _d.store)}

@router.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def get_run_log(run_id: str, lines: int = 200) -> str:
    run = _d.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return detached.tail_log(run, lines)

@router.get("/api/runs/{run_id}/activity")
def get_activity(run_id: str, limit: int = 200) -> dict[str, Any]:
    run = _d.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return activity.parse(run, limit)

@router.get("/api/live")
def get_live() -> list[dict[str, Any]]:
    """Every agent running right now, with what it is currently doing."""
    out = []
    for r in _d.store.runs():
        if r.status != "running":
            continue
        body = r.model_dump()
        body["verdict"] = verdict.verdict(r)
        try:
            body["plan"] = verdict.plan(r, _d.store)
        except Exception as e:  # noqa: BLE001 - the plan is a garnish, never a blocker
            body["plan"] = None
            body["plan_error"] = str(e)
        try:
            a = activity.parse(r, limit=40)
            body["current"] = a["current"]
            body["tool_calls"] = a["tool_calls"]
            body["last_activity"] = a["last_activity"]
            body["input_tokens"] = a["input_tokens"] or r.input_tokens
            body["output_tokens"] = a["output_tokens"] or r.output_tokens
            body["streaming"] = a["streaming"]
            body["note"] = a.get("note")
            body["subtasks_started"] = a.get("subtasks_started", 0)
            body["subtasks_done"] = a.get("subtasks_done", 0)
        except Exception as e:  # noqa: BLE001 - a bad log must not hide the run
            body["current"] = f"could not read the log: {e}"
        out.append(body)
    return out

@router.post("/api/runs/prune")
def prune_runs(older_than_hours: int = 24, dry_run: bool = True) -> dict[str, Any]:
    """Drop finished-badly runs from history.

    Only touches orphaned/killed/failed runs older than the cutoff, and never a
    running one. The board derives an "N runs vanished" card from these, so old
    test artifacts and already-resolved incidents otherwise sit there forever
    reporting work that nobody still needs to do.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    doomed, kept = [], []
    for r in _d.store.runs():
        drop = False
        if r.status in ("orphaned", "killed", "failed"):
            try:
                when = datetime.fromisoformat((r.ended or r.started).replace("Z", "+00:00"))
                drop = when < cutoff
            except ValueError:
                drop = False
        (doomed if drop else kept).append(r)

    if not dry_run and doomed:
        with _d.store.lock:
            _d.store.save_runs(kept)
        _d.store.log(f"pruned {len(doomed)} finished run(s) older than {older_than_hours}h",
                  level="warn", source="runs")
    return {
        "dry_run": dry_run,
        "pruned": len(doomed),
        "remaining": len(kept),
        "runs": [{"id": r.id[:6], "name": r.name, "status": r.status,
                  "ended": r.ended or r.started} for r in doomed],
    }

@router.post("/api/runs/spawn")
def spawn_run(req: SpawnRequest) -> dict[str, Any]:
    try:
        run = detached.spawn(
            name=req.name, prompt=req.prompt, cwd=req.cwd, agent=req.agent,
            mode=req.mode, task_id=req.task_id, tier=req.tier,
            skip_permissions=req.skip_permissions, domain=req.domain,
            model=req.model or config.DEFAULT_MODEL or None,
            system_extra=req.system_extra,
        )
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    if req.notes:
        run.notes = ((run.notes or "") + f" | {req.notes}").strip(" |")
    _d.store.upsert_run(run)
    _d.store.log(f"spawned {run.name} (pid {run.pid}, {run.domain})", source="runner",
              run_id=run.id, cwd=req.cwd, agent=req.agent)
    return run.model_dump()

@router.post("/api/runs/{run_id}/kill")
def kill_run(run_id: str) -> dict[str, Any]:
    with _d.store.lock:
        run = _d.store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run matching {run_id}")
        run = detached.kill(run)
        _d.store.upsert_run(run)
    _d.store.log(f"killed {run.name}", level="warn", source="runner", run_id=run.id)
    return run.model_dump()

@router.post("/api/runs/{run_id}/done")
def finish_run(run_id: str, req: DoneRequest) -> dict[str, Any]:
    """Self-report endpoint. A spawned agent calls this so its exit is unambiguous."""
    with _d.store.lock:
        run = _d.store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run matching {run_id}")
        run.status = req.status  # type: ignore[assignment]
        run.exit_code = req.exit_code
        run.ended = iso(utcnow())
        if req.notes:
            run.notes = (run.notes or "") + f" | {req.notes}"
        _d.store.upsert_run(run)
    _d.store.log(f"{run.name} reported {req.status}", source="runner", run_id=run.id)
    return run.model_dump()

@router.post("/api/runs/{run_id}/ack")
def ack_run(run_id: str, undo: bool = False) -> dict[str, Any]:
    """Acknowledge a failed or orphaned run, clearing its derived board card.

    The board's only verb for a past failure. A derived card cannot be dragged to
    Done because there is no stored row to write, and a failed run is history that
    never clears on its own, so without this the card is permanent and the only way
    out is `otto prune` deleting the run from the ledger. Acknowledging keeps the
    record and drops the nag, which are different things.
    """
    with _d.store.lock:
        run = _d.store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        if run.status not in ("failed", "orphaned"):
            raise HTTPException(
                400, f"run {persona.short(run.id)} is {run.status}, not a failure; "
                     "there is nothing to acknowledge")
        run.reviewed_at = None if undo else iso(utcnow())
        _d.store.upsert_run(run)
    _d.store.log(f"{'un-acknowledged' if undo else 'acknowledged'} "
              f"{run.status} run {persona.short(run.id)} ({run.name})", source="runner")
    return run.model_dump()

@router.post("/api/runs/reclassify")
def reclassify_runs() -> dict[str, Any]:
    """Backfill `error_kind` on failures recorded before the field existed.

    Without this the fix is retroactively a lie: every historical failure has
    `error_kind: None`, so `run.transient` is False for all of them and the board
    keeps showing 529s as ordinary failures needing attention. The alternative was
    telling the owner to acknowledge twelve cards, which papers over a misclassified
    history rather than correcting it, and loses the one genuine failure hiding among
    the transient ones.

    Reads each failed run's captured log and re-applies the same test `_apply_result`
    now uses. Idempotent, and only ever fills a field that is empty: a run already
    classified is left alone.
    """
    scanned = classified = 0
    with _d.store.lock:
        runs = _d.store.runs()
        for r in runs:
            if r.status not in ("failed", "orphaned") or r.error_kind:
                continue
            scanned += 1
            obj = detached._parse_result_json(r)
            if not obj:
                continue
            if (obj.get("terminal_reason") == "api_error"
                    or obj.get("api_error_status") is not None):
                r.error_kind = "api"
                classified += 1
        if classified:
            _d.store.save_runs(runs)
    _d.store.log(f"reclassified {classified} of {scanned} unclassified failure(s) as "
              f"upstream API errors", source="runner")
    return {"scanned": scanned, "classified": classified}

@router.post("/api/runs/ack-all")
def ack_all_runs(kind: str | None = None) -> dict[str, Any]:
    """Acknowledge every outstanding failure at once, optionally only one error kind.

    `kind=api` is the case this exists for: an upstream outage produces a burst of
    identical failures, and acknowledging them one at a time is busywork that teaches
    the owner to ignore the column instead.
    """
    acked: list[str] = []
    with _d.store.lock:
        runs = _d.store.runs()
        for r in runs:
            if r.status not in ("failed", "orphaned") or r.reviewed_at:
                continue
            if kind and r.error_kind != kind:
                continue
            r.reviewed_at = iso(utcnow())
            acked.append(persona.short(r.id))
        if acked:
            _d.store.save_runs(runs)
    if acked:
        _d.store.log(f"acknowledged {len(acked)} failed run(s)"
                  + (f" of kind '{kind}'" if kind else ""), source="runner")
    return {"acknowledged": acked, "count": len(acked)}
