"""Spend, the session ledger, telemetry status, and the OTLP receiver.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, ledger, spend, telemetry
from .. import daemon as _d

router = APIRouter()


@router.get("/api/spend")
def get_spend(days: int = 7, domain: str | None = None) -> dict[str, Any]:
    """Cost per workload, per model, per agent. Read-only, derived, no model call."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    if domain is not None and domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    return spend.report(_d.store.runs(), days=days, domain=domain)

# ---- ledger: every session, priced ---------------------------------------------
#
# spend.report is Otto's runs, priced by Claude Code's result JSON. The ledger is
# every Claude Code session on the machine, the owner's terminals included, priced from
# telemetry where it exists and the transcripts where it does not. Both views stay:
# spend answers "what is Otto costing per workload", the ledger answers "where did
# the money go", and the second question was 84% unanswerable before this.

_LEDGER_MEMO: dict[str, tuple[float, dict[str, Any]]] = {}

_LEDGER_MEMO_SECONDS = 60.0

_ledger_lock = threading.Lock()

@router.get("/api/ledger")
def get_ledger(days: int = 30, who: str | None = None) -> dict[str, Any]:
    """The session ledger. Memoised for a minute: a poll-driven UI would otherwise
    re-read a gigabyte of transcripts every few seconds, and the numbers do not
    move faster than that."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    if who is not None and who not in ("yours", "otto", "other"):
        raise HTTPException(400, "who must be yours, otto, or other")
    key = f"{days}"
    now = time.time()
    with _ledger_lock:
        hit = _LEDGER_MEMO.get(key)
        if hit and now - hit[0] < _LEDGER_MEMO_SECONDS:
            rep = hit[1]
        else:
            rep = ledger.build(days=days, runs=_d.store.runs())
            _LEDGER_MEMO[key] = (now, rep)
    if who:
        rep = {**rep, "sessions": [s for s in rep["sessions"] if s["who"] == who]}
    return rep

@router.get("/api/ledger/sessions/{sid}")
def get_ledger_session(sid: str) -> dict[str, Any]:
    """One session parsed live: timeline, rebuilds, and what filled its context."""
    d = ledger.detail(sid, runs=_d.store.runs())
    if d is None:
        raise HTTPException(404, f"no transcript for session {sid}")
    if "ambiguous" in d:
        raise HTTPException(409, f"prefix matches {len(d['ambiguous'])} sessions")
    return d

@router.get("/api/telemetry")
def get_telemetry() -> dict[str, Any]:
    return {**telemetry.summary(), "endpoint": config.BASE_URL, "env": telemetry.env()}

# The OTLP receiver. Claude Code posts here every few seconds from every live
# session once the env block is installed. Three rules: answer fast (the exporter
# retries and buffers, but a slow collector is still a slow collector), never
# raise (a 500 here teaches the exporter to back off and we lose data), and write
# only the allowlisted fields (telemetry.KEEP is the whole contract).

async def _otlp(request: Request, kind: str) -> JSONResponse:
    raw = await request.body()
    if kind != "logs":
        return JSONResponse({"partialSuccess": {}})   # accepted, discarded
    payload = telemetry.decode_body(raw, request.headers.get("content-encoding"))
    if payload is None:
        # Not JSON: protocol is grpc or protobuf. Say so once in the event log.
        if not _otlp.warned:  # type: ignore[attr-defined]
            _otlp.warned = True  # type: ignore[attr-defined]
            _d.store.log("telemetry: non-JSON OTLP payload received; set "
                      "OTEL_EXPORTER_OTLP_PROTOCOL=http/json", level="warn", source="telemetry")
        return JSONResponse({"partialSuccess": {"rejectedLogRecords": 0}})
    try:
        rows = telemetry.flatten(payload)
        if rows:
            _d.store.append_telemetry(rows)
    except Exception as e:  # noqa: BLE001 - the exporter must not see our failure
        _d.store.log(f"telemetry: could not record batch: {e}", level="warn", source="telemetry")
    return JSONResponse({"partialSuccess": {}})

_otlp.warned = False  # type: ignore[attr-defined]

@router.post("/v1/logs")
async def otlp_logs(request: Request) -> JSONResponse:
    return await _otlp(request, "logs")

@router.post("/v1/metrics")
async def otlp_metrics(request: Request) -> JSONResponse:
    return await _otlp(request, "metrics")

@router.post("/v1/traces")
async def otlp_traces(request: Request) -> JSONResponse:
    return await _otlp(request, "traces")
