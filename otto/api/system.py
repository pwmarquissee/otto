"""The day journal, repositories, machine facts, the definition registry.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import journal, registry, repos
from .. import daemon as _d
from ..runners import machine

router = APIRouter()


@router.get("/api/day/{day}")
def get_day(day: str, refresh: bool = False) -> dict[str, Any]:
    """A day record: the transcript rollup plus whatever the owner reported.

    `refresh=true` recomputes the rollup from transcripts. Cheap (sub-second,
    no model) but it is a write, so it goes through the daemon like everything else.
    """
    try:
        d = _dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"not an ISO date: {day!r}") from None
    rec = _d.store.get_day(day)
    if refresh or not rec.get("rollup"):
        rec = _d.store.put_day(day, {"rollup": journal.rollup(d)})
    return rec

@router.get("/api/repos")
def get_repos() -> dict[str, Any]:
    """Roots and repositories, derived from the registry, tasks and runs.

    Not a stored list. A repo appears because something actually touched it, so
    this cannot drift from the filesystem the way a hand-kept inventory would.
    """
    return repos.collect(_d.store)

@router.get("/api/machine")
def get_machine() -> list[dict[str, Any]]:
    return [m.model_dump() for m in machine.snapshot()]

@router.get("/api/registry")
def get_registry(kind: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
    entries = _d.store.registry()
    if kind:
        entries = [e for e in entries if e.kind == kind]
    if domain:
        entries = [e for e in entries if e.domain == domain]
    return [e.model_dump() for e in entries]

@router.post("/api/registry/scan")
def scan_registry() -> dict[str, Any]:
    notes = _d.refresh_registry()
    _d._scan_notes[:] = notes
    for n in notes[:20]:
        _d.store.log(n, source="registry")
    return {"changes": notes, "summary": registry.summarize(_d.store.registry())}
