"""The board: tasks, batches, replies, dedupe, known failures, notices.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import backlog, board, config, dedupe, dispatch, findings, known, notify, persona
from .. import daemon as _d
from ..models import Task, iso, utcnow
from ..runners import detached

router = APIRouter()


class TaskRequest(BaseModel):
    title: str
    status: str = "backlog"
    domain: str = config.WORK
    priority: str = "normal"
    detail: str | None = None
    agent: str | None = None
    tags: list[str] = []
    task_ref: str | None = None
    tier: str | None = None
    due: str | None = None
    cwd: str | None = None
    auto: bool = True
    origin: str | None = None   # who filed it, when not the owner
    # False files the card even when an open card matches it. The door for "I
    # know it looks like that one, it is not": the default is to merge.
    dedupe: bool = True

class CardReplyRequest(BaseModel):
    """The owner answering a card: from a due toast's Reply button, the notice sheet, the
    card menu, or `otto task reply`. Free text only. The structured answers (done,
    push a day) are PATCHes and do not need a model."""

    text: str

class TaskPatch(BaseModel):
    """Partial update. Only the fields present are applied."""

    title: str | None = None
    status: str | None = None
    domain: str | None = None
    priority: str | None = None
    detail: str | None = None
    agent: str | None = None
    tags: list[str] | None = None
    due: str | None = None
    cwd: str | None = None
    auto: bool | None = None
    tier: str | None = None
    # Triage assessment. Settable here so the pass writes them the same way
    # anything else edits a card, rather than through a side channel.
    owner: str | None = None
    readiness: str | None = None
    assessed: str | None = None
    assessed_note: str | None = None
    # Both settable so a human re-sending a failed card with new information gets a
    # real retry. dispatch.eligible() skips anything with attempts >= max_attempts,
    # so WITHOUT this a re-queued card sits in `queued` forever and never runs --
    # a silent no-op, which is worse than refusing. Resetting is not a brake bypass:
    # the brake exists to stop UNATTENDED relaunch loops, and this path requires a
    # person to type context and press send. Pass last_error="" to clear it, so the
    # card does not keep showing an error from an attempt that has been superseded.
    attempts: int | None = None
    last_error: str | None = None
    # Plan gate and stale loop; see models.Task. `plan_approved` and `next_look`
    # take "" to clear, the same convention as last_error.
    plan: str | None = None
    plan_approved: str | None = None
    run_mode: str | None = None
    last_checked: str | None = None
    next_look: str | None = None
    run_id: str | None = None
    # Settable so a hand-judged merge (two cards the title heuristics could not see
    # as one) is recorded the way the sweep records it: closed INTO a keeper, not
    # counted as finished work. Pass "" to clear, same convention as last_error.
    duplicate_of: str | None = None
    # plan | yolo, or "" to go back to deriving it (models.Task.permissions).
    permissions: str | None = None

class ProposeRequest(BaseModel):
    """A running agent filing work it noticed.

    `run_id` is not decoration: it is how a filed card points back at the session
    that noticed it, and `propose_tasks` has always read `req.run_id`. It was
    missing from this model, so every call raised AttributeError and returned 500 —
    which means mid-run filing has been dead on the HTTP path, silently, for as
    long as the field has been read. Found 2026-08-05 filing a card by hand.

    The post-run path (`findings.harvest`) calls `file_tasks` in-process and never
    touched this, so findings still arrived at the END of a run. Only the "do not
    sit on it, file it now" path was broken, and a dropped proposal looks exactly
    like an agent that did not find anything.
    """

    tasks: list[dict[str, Any]] = []
    origin: str = "agent"
    run_id: str | None = None

@router.post("/api/tasks/propose")
def propose_tasks(req: ProposeRequest) -> dict[str, Any]:
    """Mid-run filing. Agents that find something early should not sit on it."""
    notes = findings.file_tasks(_d.store, req.tasks, req.origin, req.run_id)
    for n in notes:
        _d.store.log(n, source="findings")
    return {"filed": len(notes), "notes": notes}

@router.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = _d.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"no task matching {task_id}")
    body = task.model_dump()
    if task.run_id:
        run = _d.store.get_run(task.run_id)
        if run:
            body["run"] = run.model_dump()
    return body

@router.post("/api/tasks/{task_id}/dispatch")
def dispatch_task(task_id: str, force: bool = False,
                  permissions: str | None = None) -> dict[str, Any]:
    """Run a card now. `permissions=plan|yolo` overrides the derived level for
    this one run; omitted means dispatch.permissions_for(task)."""
    if permissions is not None and permissions not in ("plan", "yolo"):
        raise HTTPException(400, "permissions must be plan or yolo")
    task = _d.store.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"no task matching {task_id}")
    run, msg = dispatch.dispatch(_d.store, task, force=force, permissions=permissions)
    if run is None:
        raise HTTPException(409, msg)
    _d.store.log(msg, source="dispatch", task_id=task.id, run_id=run.id)
    return {"message": msg, "run": run.model_dump()}

@router.post("/api/tasks/{task_id}/yolo")
def yolo_task(task_id: str) -> dict[str, Any]:
    """The one-word approval: `otto task yolo <id>`, or "yolo" typed on a card.

    Approves the plan the card holds (a PREPARE run's proposal, or one the owner
    wrote), pins the level to yolo, and sends the card through the same gate
    `otto triage promote` uses, then dispatches it now. The plan gate is what the
    word answers; a tier or owner refusal still stands and comes back as the
    gate's own sentence, because "yolo" on a card Otto may not run is still no.
    A card already queued or in needs-you after a prepare run is treated as
    backlog for the gate: it is the same card, one step further along.
    """
    with _d.store.lock:
        task = _d.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        if task.status == "running":
            raise HTTPException(409, f"already running as {persona.short(task.run_id or '')}")
        probe = task.model_copy(update={
            "status": "backlog" if task.status in ("needs-you", "queued") else task.status,
            "plan_approved": task.plan_approved or (iso(utcnow()) if task.plan else None),
        })
        why = backlog.refusal(probe, _d.store.tasks())
        if why:
            raise HTTPException(409, why)
        task.plan_approved = probe.plan_approved
        task.permissions = "yolo"
        task.run_mode = "run"
        task.status = "queued"
        task.auto = True
        task.touch()
        _d.store.upsert_task(task)
    run, msg = dispatch.dispatch(_d.store, task, force=True, permissions="yolo")
    if run is None:
        # Queued and approved, so the next tick or `otto task run` picks it up; say
        # why it did not go this instant rather than failing the approval.
        _d.store.log(f"yolo on {task.title[:50]}: approved, not dispatched ({msg})",
                     source="board", task_id=task.id)
        return {"task": _d.store.get_task(task.id).model_dump(), "run": None, "message": msg}
    _d.store.log(f"yolo: {msg}", source="dispatch", task_id=task.id, run_id=run.id)
    return {"task": _d.store.get_task(task.id).model_dump(), "run": run.model_dump(),
            "message": msg}

class NoticeRequest(BaseModel):
    title: str
    body: str | None = None
    level: Literal["info", "warn", "crit"] = "info"
    domain: str = config.WORK
    source: str = "otto"
    command: str | None = None
    notify: bool | None = None      # None = decide from level

@router.get("/api/notices")
def get_notices(unread_only: bool = False, domain: str | None = None) -> list[dict[str, Any]]:
    out = _d.store.notices()
    if unread_only:
        out = [n for n in out if n.read_at is None]
    if domain:
        out = [n for n in out if n.domain == domain]
    return [n.model_dump() for n in out]

@router.post("/api/notices")
def post_notice(req: NoticeRequest) -> dict[str, Any]:
    """How Otto, an agent, or a schedule sends the owner a message."""
    if req.domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    n = notify.post(_d.store, req.title, body=req.body, level=req.level,
                    domain=req.domain, source=req.source, command=req.command,
                    notify=req.notify)
    return n.model_dump()

@router.post("/api/notices/{notice_id}/read")
def read_notice(notice_id: str) -> dict[str, Any]:
    n = notify.mark_read(_d.store, notice_id)
    if n is None:
        raise HTTPException(404, f"no notice matching {notice_id}")
    return n.model_dump()

@router.post("/api/notices/read-all")
def read_all_notices() -> dict[str, Any]:
    n = 0
    for notice in _d.store.notices():
        if notice.read_at is None:
            notify.mark_read(_d.store, notice.id)
            n += 1
    return {"marked_read": n}

@router.delete("/api/notices/{notice_id}")
def remove_notice(notice_id: str) -> dict[str, Any]:
    if not _d.store.delete_notice(notice_id):
        raise HTTPException(404, f"no notice matching {notice_id}")
    return {"removed": notice_id}

# ---- known failures ---------------------------------------------------------
# A schedule or integration the owner has said is broken and knows why. See known.py.

class KnownRequest(BaseModel):
    reason: str

@router.get("/api/known")
def get_known() -> list[dict[str, Any]]:
    return _d.store.known()

@router.put("/api/known/{kind}/{name}")
def put_known(kind: str, name: str, req: KnownRequest) -> dict[str, Any]:
    try:
        return known.mark(_d.store, kind, name, req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

@router.delete("/api/known/{kind}/{name}")
def delete_known(kind: str, name: str) -> dict[str, Any]:
    if not known.clear(_d.store, kind, name):
        raise HTTPException(404, f"nothing marked known for {known.key(kind, name)}")
    return {"removed": known.key(kind, name)}

@router.get("/api/board")
def get_board(domain: str | None = None) -> dict[str, Any]:
    return board.build(_d.store, domain)

@router.get("/api/tasks")
def list_tasks(domain: str | None = None) -> list[dict[str, Any]]:
    items = _d.store.tasks()
    if domain:
        items = [t for t in items if t.domain == domain]
    return [t.model_dump() for t in items]

@router.post("/api/tasks")
def create_task(req: TaskRequest) -> dict[str, Any]:
    try:
        body = req.model_dump()
        # An explicit origin means something other than the owner authored this, which is
        # what `source` records. Keeps board provenance honest without a second flag.
        if body.get("origin"):
            body["source"] = "agent"
        want_dedupe = body.pop("dedupe", True)
        task = Task(id=uuid.uuid4().hex, **body)
        task.fingerprint = dedupe.fingerprint(task.title, task.domain)
        if not want_dedupe and dedupe.DISTINCT_TAG not in task.tags:
            # Durable, not just a skip here: the tick's sweep would otherwise fold
            # the card on its next pass and the flag would have meant nothing.
            task.tags.append(dedupe.DISTINCT_TAG)
    except ValueError as e:
        raise HTTPException(400, f"invalid task: {e}") from e
    with _d.store.lock:
        match = dedupe.find_match(task, _d.store.tasks()) if want_dedupe else None
        if match is not None:
            # The card already exists under another wording. Bump it and hand it
            # back; the caller sees `merged` and the title it actually landed on.
            existing, why = match
            dedupe.absorb(existing, task, stored=False)
            _d.store.upsert_task(existing)
            _d.store.log(f"task merged into existing: '{task.title[:60]}' -> "
                      f"{existing.id[:6]} ({why})", source="board",
                      task_id=existing.id, domain=existing.domain)
            body = existing.model_dump()
            body["merged"] = why
            return body
        _d.store.upsert_task(task)
    _d.store.log(f"task added: {task.title}", source="board",
              task_id=task.id, domain=task.domain)
    return task.model_dump()

@router.patch("/api/tasks/{task_id}")
def update_task(task_id: str, req: TaskPatch) -> dict[str, Any]:
    with _d.store.lock:
        task = _d.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        changes = req.model_dump(exclude_none=True)
        if not changes:
            return task.model_dump()
        # "" means clear for the optional strings a human re-sets: an empty plan
        # approval is "not approved", not an approval stamped "".
        for k in ("plan_approved", "next_look", "last_checked", "run_mode", "run_id",
                  "plan", "last_error", "duplicate_of", "permissions"):
            if changes.get(k) == "":
                changes[k] = None
        try:
            updated = task.model_copy(update=changes)
            # Re-validate so a bad status or domain is rejected rather than stored.
            updated = Task.model_validate(updated.model_dump())
        except ValueError as e:
            raise HTTPException(400, f"invalid update: {e}") from e
        updated.touch()
        _d.store.upsert_task(updated)
    _d.store.log(f"task updated: {updated.title} -> {updated.status}", source="board",
              task_id=updated.id)
    return updated.model_dump()

class TaskBatchRequest(BaseModel):
    """One change applied to many cards under one lock and one write.

    The board's per-card PATCH is 30ms; what made moving cards slow was the full
    state refresh the UI waited on after each one. With a batch verb the UI can
    select twenty cards and issue one call, and tasks.json (1.4 MB) is rewritten
    once instead of twenty times. `queued` is refused here on purpose: promotion
    goes through the triage gate one card at a time, and a batch verb that could
    dispatch twenty unattended sessions in one click is the wrong shape for it.
    """

    ids: list[str]
    patch: TaskPatch

class DedupeRequest(BaseModel):
    dry_run: bool = False

@router.post("/api/tasks/dedupe")
def dedupe_tasks(req: DedupeRequest) -> dict[str, Any]:
    """Fold every duplicate on the open board into its keeper, or say what would.

    The tick does this on its own whenever the open board changes; the verb exists
    so the owner can see the plan (`--dry-run`) and so a run's report can carry it.
    """
    rows = dedupe.sweep(_d.store, dry_run=req.dry_run)
    if not req.dry_run:
        for r in rows:
            _d.store.log(f"merged '{r['dupe_title'][:44]}' into {r['keeper_id'][:6]} "
                      f"'{r['keeper_title'][:44]}' ({r['why']})",
                      source="dedupe", task_id=r["keeper_id"])
    return {"dry_run": req.dry_run, "merged": rows}

@router.post("/api/tasks/batch")
def batch_tasks(req: TaskBatchRequest) -> dict[str, Any]:
    changes = req.patch.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "empty patch")
    if changes.get("status") == "queued":
        raise HTTPException(400, "batch cannot move cards to queued; promote each "
                                 "through `otto triage promote`")
    if not req.ids:
        raise HTTPException(400, "no ids")
    for k in ("plan_approved", "next_look", "last_checked", "run_mode", "run_id",
              "plan", "last_error"):
        if changes.get(k) == "":
            changes[k] = None
    updated: list[Task] = []
    missing: list[str] = []
    with _d.store.lock:
        items = _d.store.tasks()
        wanted = list(dict.fromkeys(req.ids))
        for pid in wanted:
            hits = [t for t in items if t.id == pid or t.id.startswith(pid)]
            if len(hits) != 1:
                missing.append(pid)
                continue
            t = hits[0]
            try:
                new = Task.model_validate(t.model_copy(update=changes).model_dump())
            except ValueError as e:
                raise HTTPException(400, f"invalid update: {e}") from e
            new.touch()
            items[items.index(t)] = new
            updated.append(new)
        if updated:
            _d.store.save_tasks(items)
    for t in updated:
        _d.store.log(f"task updated: {t.title} -> {t.status}", source="board", task_id=t.id)
    return {"updated": [t.model_dump() for t in updated], "missing": missing}

@router.post("/api/tasks/{task_id}/reply")
def reply_task(task_id: str, req: CardReplyRequest) -> dict[str, Any]:
    """The owner said something about a card. Record it, then let a small session decide.

    Two things happen, in this order. His words are appended to the card's detail
    FIRST, dated and attributed, so the record exists even if the session never
    starts: his own sentence is the thing that will still be right in six weeks.
    Then a /otto-card session is spawned with the card and the reply in its system
    context, on the DM model and the DM budget, to update the card, file what
    follows, or answer him. It is linked to the card by task_id, so the card's log
    shows what Otto did with the reply.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "reply is empty")
    with _d.store.lock:
        task = _d.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        stamp = iso(utcnow())[:16].replace("T", " ")
        task.detail = "\n\n".join(x for x in (
            (task.detail or "").rstrip(),
            f"--- {config.OWNER_NAME} replied {stamp} UTC ---\n{text}",
        ) if x)
        task.touch()
        _d.store.upsert_task(task)
    _d.store.log(f"reply on card: {task.title[:60]}", source="board", task_id=task.id)

    # Plain lines, not JSON. The model reads them as easily, and the command line
    # they travel on (see detached._write_launcher) is happier with fewer quotes.
    fields = [(k, getattr(task, k)) for k in
              ("id", "title", "status", "domain", "priority", "due", "tier", "owner",
               "readiness", "assessed_note", "created")]
    card_lines = [f"  {k}: {v}" for k, v in fields if v not in (None, "", [])]
    if task.tags:
        card_lines.append(f"  tags: {', '.join(task.tags)}")
    detail = "\n".join("    " + ln for ln in (task.detail or "").splitlines())
    extra = (
        f"{config.OWNER_NAME.upper()} REPLIED TO A BOARD CARD. The card, as it stands "
        "(the reply is already appended to its detail):\n\n"
        + "\n".join(card_lines) + "\n  detail:\n" + detail + "\n\n"
        f"WHAT {config.OWNER_NAME.upper()} SAID:\n  {text}\n\n"
        f"Today is {datetime.now().astimezone().date().isoformat()}. "
        "Act on the card with `python -m otto task set/mv {id}` and file anything new "
        "with `python -m otto task add`. Then send the receipt with `python -m otto "
        "notify` so the owner can see what changed without opening the board."
    )
    try:
        run = detached.spawn(
            name="card-reply",
            prompt="/otto-card",
            cwd=str(config.HOME),
            mode="headless",
            # yolo: it moves cards and files new ones on the owner's word, which plan
            # mode cannot do; the guard hook still gates what it may reach.
            permissions="yolo",
            domain=task.domain,
            task_id=task.id,
            system_extra=extra,
            budget_usd=config.CARD_REPLY_BUDGET_USD or None,
            model=config.CARD_REPLY_MODEL or config.DEFAULT_MODEL or None,
        )
    except (ValueError, OSError) as e:
        # The reply is on the card already. Say the session did not start rather
        # than pretending the text was lost.
        _d.store.log(f"card reply recorded but no session: {e}", level="warn",
                  source="board", task_id=task.id)
        return {"task": task.model_dump(), "run": None, "error": str(e)}
    run.notes = ((run.notes or "") + f" | mode=card-reply | card={task.id[:6]}").strip(" |")
    _d.store.upsert_run(run)
    _d.store.log(f"card-reply session for {task.title[:50]}", source="runner",
              run_id=run.id, task_id=task.id)
    return {"task": task.model_dump(), "run": run.model_dump(), "error": None}

@router.delete("/api/tasks/{task_id}")
def remove_task(task_id: str) -> dict[str, Any]:
    """Delete a task, and kill its run if one is still in flight.

    Without this the agent keeps working on a task that no longer exists: it would
    burn tokens, possibly change files, and have nowhere to report back to.
    """
    killed = None
    with _d.store.lock:
        task = _d.store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        if task.run_id:
            run = _d.store.get_run(task.run_id)
            if run is not None and run.status == "running":
                run = detached.kill(run)
                run.notes = ((run.notes or "") + " | task deleted").strip(" |")
                _d.store.upsert_run(run)
                killed = run.id
        _d.store.delete_task(task.id)
    _d.store.log(f"task removed: {task.title}"
              + (f" (killed run {persona.short(killed)})" if killed else ""),
              level="warn", source="board")
    return {"removed": task.id, "killed_run": killed}
