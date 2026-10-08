"""The whole-state read and the small reads beside it: health, state, briefing, gaps, events, alerts, config.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from typing import Any

import psutil
from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import advisor, config, configsync
from .. import daemon as _d
from ..models import iso, utcnow

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "persona": config.PERSONA_NAME,
        "pid": psutil.Process().pid,
        "state_dir": str(config.STATE_DIR),
        "at": iso(utcnow()),
    }

@router.get("/api/state")
def state(request: Request) -> Response:
    cached = _d._state_payload()
    # no-cache means "revalidate every time", not "never store": the browser
    # keeps the body and sends If-None-Match, which is exactly the 304 path.
    headers = {"ETag": cached.etag, "Cache-Control": "no-cache",
               "Vary": "Accept-Encoding"}
    if _d._etag_matches(request.headers.get("if-none-match"), cached.etag):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return Response(cached.gzipped, media_type="application/json", headers=headers)
    return Response(cached.body, media_type="application/json", headers=headers)

@router.get("/api/briefing")
def get_briefing(domain: str | None = None) -> dict[str, Any]:
    return advisor.briefing(_d.store, domain)

@router.get("/api/gaps")
def get_gaps(domain: str | None = None) -> list[dict[str, Any]]:
    rows = advisor.gaps(_d.store)
    return [g for g in rows if not domain or g["domain"] == domain]

@router.get("/api/config")
def get_config() -> dict[str, Any]:
    # `home` is here so a caller that has to supply a working directory has a real
    # default instead of guessing one. Otto's own repo would be the wrong default
    # for arbitrary work.
    return {**configsync.summary(), "home": str(config.HOME)}

@router.get("/api/events")
def get_events(limit: int = 100) -> list[dict[str, Any]]:
    return [e.model_dump() for e in _d.store.events(limit)]

@router.get("/api/alerts")
def get_alerts() -> list[dict[str, Any]]:
    return [a.model_dump() for a in _d.compute_alerts()]
