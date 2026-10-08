"""The persona chat thread.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import chat
from .. import daemon as _d

router = APIRouter()


class ChatRequest(BaseModel):
    message: str

@router.get("/api/chat")
def get_chat() -> dict[str, Any]:
    thread = _d.store.chat()
    # A turn in flight is what the UI renders as "thinking".
    pending = [
        r.id for r in _d.store.runs()
        if r.status == "running" and "mode=chat" in (r.notes or "")
    ]
    thread["pending"] = pending
    return thread

@router.post("/api/chat")
def post_chat(req: ChatRequest) -> dict[str, Any]:
    if any(r.status == "running" and "mode=chat" in (r.notes or "") for r in _d.store.runs()):
        raise HTTPException(409, "a chat turn is already in flight")
    try:
        run, thread = chat.send(_d.store, req.message)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(500, f"could not start chat turn: {e}") from e
    _d.store.upsert_run(run)
    _d.store.log("chat turn started", source="chat", run_id=run.id)
    thread["pending"] = [run.id]
    return thread

@router.delete("/api/chat")
def reset_chat() -> dict[str, Any]:
    chat.reset(_d.store)
    _d.store.log("chat thread reset", source="chat")
    return {"reset": True}
