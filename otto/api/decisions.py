"""The append-only decision log, and retire candidates.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, decisions, retire
from .. import daemon as _d

router = APIRouter()


class DecisionRequest(BaseModel):
    """Record a decision. `decision` and `why` are required by decisions.build,
    which raises a 400 with the reason rather than storing a half-empty entry."""

    title: str
    decision: str
    why: str
    alternatives: str | None = None
    revisit: str | None = None
    revisit_by: str | None = None
    owner: str | None = None
    domain: str = config.WORK
    tags: list[str] = []
    decided: str | None = None
    task_id: str | None = None
    origin: str | None = None
    run_id: str | None = None

# ---- decisions ---------------------------------------------------------------
# Append-only, so there is deliberately no PATCH and no DELETE here. Superseding
# is the only way to change one, and it is a POST that writes a new decision. An
# endpoint that let a decision be edited would make the log unable to answer the
# one question it exists for: what did you think at the time.

@router.get("/api/decisions")
def get_decisions(domain: str | None = None, include_superseded: bool = False,
                  q: str | None = None) -> list[dict[str, Any]]:
    if q:
        return [d.model_dump() for d in decisions.search(_d.store, q)]
    items = _d.store.decisions()
    if domain:
        items = [d for d in items if d.domain == domain]
    if not include_superseded:
        items = [d for d in items if d.live]
    return [d.model_dump() for d in items]

@router.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str) -> dict[str, Any]:
    d = _d.store.get_decision(decision_id)
    if d is None:
        raise HTTPException(404, f"no decision {decision_id}")
    return d.model_dump()

@router.post("/api/decisions")
def post_decision(req: DecisionRequest) -> dict[str, Any]:
    try:
        d = decisions.build(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        _d.store.add_decision(d)
    except ValueError as e:
        # A duplicate id means the identical decision (same title, date, and text)
        # is already recorded. Say so rather than appending a second copy.
        raise HTTPException(409, str(e)) from e
    _d.store.log(f"decision {d.id} recorded: {d.title[:60]}", source="decision")
    return d.model_dump()

@router.patch("/api/decisions/{decision_id}/revisit-by")
def patch_revisit_by(decision_id: str, when: str | None = None) -> dict[str, Any]:
    """Set or clear the review date. The ONLY mutable field on a decision; see
    Store.set_revisit_by for why this one is an exception and the rest are not."""
    if when:
        try:
            datetime.fromisoformat(str(when)[:10])
        except ValueError:
            raise HTTPException(400, f"revisit_by must be an ISO date, got {when!r}")
    try:
        d = _d.store.set_revisit_by(decision_id, when)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"decision {d.id} review date "
              + (f"set to {d.revisit_by}" if d.revisit_by else "cleared"),
              source="decision")
    return d.model_dump()

@router.post("/api/decisions/{decision_id}/supersede")
def post_supersede(decision_id: str, req: DecisionRequest) -> dict[str, Any]:
    try:
        new = decisions.build(**req.model_dump())
        old, new = _d.store.supersede_decision(decision_id, new)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"decision {new.id} supersedes {old.id}: {new.title[:50]}",
              level="warn", source="decision")
    return {"superseded": old.model_dump(), "decision": new.model_dump()}

@router.get("/api/retire")
def get_retire() -> dict[str, Any]:
    """Retire candidates. Read-only by construction: there is no POST counterpart,
    because nothing in Otto deletes a card or a schedule on a timer."""
    return {"gaps": retire.gaps(_d.store), "report": retire.render(_d.store)}
