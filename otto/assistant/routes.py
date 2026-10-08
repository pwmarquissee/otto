"""The assistant's API routes, mounted on the daemon app under OTTO_SCOPE=assistant.

Moved out of otto/daemon.py as they were. Each handler uses the daemon's store,
which `install` binds; nothing here imports otto.daemon, so importing this module
never pulls the daemon in and the daemon only imports this module when the profile
says so (see otto/assistant/__init__.py).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from .. import config, meetings, notify, nudges, outreach, persona, triage, wellbeing, writing
from ..models import iso, utcnow
from ..store import Store

router = APIRouter()
store: Store  # bound by install(); every handler below reads it at call time


def install(app: FastAPI, store_: Store) -> None:
    """Bind the daemon's store and mount the routes. Called once, after `app` exists."""
    global store
    store = store_
    app.include_router(router)


# ---- Slack DM as Otto -------------------------------------------------------------

class DmRequest(BaseModel):
    """Otto DMing a colleague AND the owner, as Otto, because the owner asked for it."""

    to: list[str]
    text: str
    why: str = ""
    run_id: str | None = None
    task_id: str | None = None


@router.post("/api/slack/dm")
def slack_dm(req: DmRequest) -> dict[str, Any]:
    """Group DM (Otto, the owner, the named people), sent now as Otto. The door a
    board task or otto-dm session uses when the owner told it to message someone.
    The roster gate and the per-run brake are applied in outreach.directed, in
    Python, before anything is transmitted; the bot credential stays in the
    daemon."""
    try:
        item = outreach.directed(store, to=req.to, body=req.text, why=req.why,
                                 run_id=req.run_id, task_id=req.task_id)
    except outreach.Refused as e:
        raise HTTPException(400, str(e)) from e
    if item.state != "sent":
        raise HTTPException(502, item.error or "send failed")
    return {"ok": True, "id": item.id, "to": item.to, "state": item.state}


# ---- dossier threads ----------------------------------------------------------------

@router.get("/api/threads")
def list_threads(band: str | None = None) -> dict[str, Any]:
    """Every dated dossier thread, computed live.

    Live rather than read off the threads-quiet notice, and that is the point. The
    notice froze 8 of 23 rows into a string hours ago; this is all of them as they
    are now, so acting on one and coming back shows the change.
    """
    rows = nudges.thread_rows(store)
    if band:
        rows = [r for r in rows if r["band"] == band]
    for r in rows:
        r["notes"] = triage.notes_for(store, r["id"], limit=5)
        r["runs"] = triage.runs_for(store, r["id"])
    counts: dict[str, int] = {}
    for r in nudges.thread_rows(store):
        counts[r["band"]] = counts.get(r["band"], 0) + 1
    return {
        "rows": rows,
        "counts": counts,
        "bands": {"stale_days": config.NUDGE_THREAD_STALE_DAYS,
                  "max_days": config.NUDGE_THREAD_MAX_DAYS},
        "in_flight": len(triage.active_runs(store)),
    }


class ThreadNote(BaseModel):
    thread_id: str
    note: str


class ThreadResolve(BaseModel):
    text: str


@router.post("/api/threads/{thread_id}/resolve")
def resolve_thread(thread_id: str, req: ThreadResolve) -> dict[str, Any]:
    """Rewrite a thread line in place, so it stops being reported as quiet.

    Separate from a plain dossier note because appending does not end a thread: the
    dated line that made it stale stays in the file and keeps reporting. This is the
    verb that actually closes one.
    """
    from .. import people as _people
    thread = nudges.find_thread(store, thread_id)
    if thread is None:
        raise HTTPException(404, f"no thread {thread_id}; the dossier may have changed")
    try:
        ok = _people.replace_note(thread["slug"], "Threads", thread["text"], req.text)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e)) from e
    if not ok:
        raise HTTPException(409, "the thread line no longer matches; it was edited "
                                 "since this view loaded")
    with contextlib.suppress(Exception):
        store.log(f"thread resolved for {thread['who']}: {req.text[:80]}",
                  source="thread-note")
    return {"ok": True, "slug": thread["slug"], "text": req.text}


@router.post("/api/threads/note")
def post_thread_note(req: ThreadNote) -> dict[str, Any]:
    """The owner's note on one thread. Records it, then spawns Otto's judgement.

    The verb is deliberately not chosen here (2026-08-05): the session picks from a
    closed list in `triage.PROMPT_TEMPLATE`. Everything colleague-facing in that
    list still goes through the outreach hold, which is enforced in
    `outreach.compose` rather than in the prompt.
    """
    thread = nudges.find_thread(store, req.thread_id)
    if thread is None:
        # A thread id encodes its text, so an edited dossier line becomes a new id.
        raise HTTPException(404, f"no thread {req.thread_id}; the dossier may have "
                                 f"changed since this view loaded")
    try:
        run = triage.submit(store, thread, req.note)
    except triage.Refused as e:
        raise HTTPException(409, str(e)) from e
    return {"run": run.model_dump(), "thread_id": thread["id"]}


# ---- meeting notes -----------------------------------------------------------------

@router.get("/api/meetings")
def get_meetings() -> dict[str, Any]:
    return meetings.status(store)


@router.post("/api/meetings/ingest")
def post_meetings_ingest() -> dict[str, Any]:
    """Read any new Notion meeting notes and file the action items.

    One at a time, deliberately. Two concurrent ingesters would both read the same
    pages before either updated the ledger, and page-id dedupe cannot help when
    neither run has written yet.
    """
    in_flight = next((r for r in store.runs()
                      if r.status == "running" and "mode=ingest" in (r.notes or "")), None)
    if in_flight is not None:
        raise HTTPException(409, f"an ingest is already running ({persona.short(in_flight.id)})")
    try:
        run = meetings.start(store)
    except (OSError, RuntimeError, ValueError) as e:
        raise HTTPException(500, f"could not start the meeting ingest: {e}") from e
    store.upsert_run(run)
    store.log("meeting-notes ingest started", source="meetings", run_id=run.id)
    return {"run": run.model_dump()}


# ---- check-ins and patterns (wellbeing) ------------------------------------------

class CheckinRequest(BaseModel):
    """The owner's own report. Deliberately free text plus one optional number: two
    or three lines, not a form, because a form that goes unfilled is worth less
    than a sentence that gets written."""

    note: str | None = None
    energy: int | None = None      # 1-5, optional
    # The signals from the owner's operating profile (otto/wellbeing.py). Every one
    # is optional and three-state: absent means "not asked", NOT "no". The note
    # above stays sufficient on its own, which is the constraint this had to be
    # built under.
    signals: dict[str, Any] | None = None


@router.get("/api/patterns")
def get_patterns(days: int = 90) -> dict[str, Any]:
    """What the check-ins say actually moves the owner's energy.

    Read-only and deterministic, like `otto next` and for the same reason: this is
    the thing to look at often, so it must be instant and free.
    """
    return wellbeing.patterns(store, days=days)


@router.put("/api/day/{day}")
def put_day(day: str, req: CheckinRequest) -> dict[str, Any]:
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"not an ISO date: {day!r}") from None
    if req.energy is not None and not 1 <= req.energy <= 5:
        raise HTTPException(400, "energy must be 1-5")
    checkin = {k: v for k, v in
               {"note": req.note, "energy": req.energy, "at": iso(utcnow())}.items()
               if v is not None}
    for key, value in (req.signals or {}).items():
        sig = wellbeing.BY_KEY.get(key)
        if sig is None:
            raise HTTPException(400, f"unknown signal {key!r}")
        if sig.kind == "bool" and not isinstance(value, bool):
            raise HTTPException(400, f"{key} must be true/false")
        if sig.kind == "scale" and not (isinstance(value, int) and 1 <= value <= 5):
            raise HTTPException(400, f"{key} must be 1-5")
        checkin[key] = value
    # put_day MERGES, so signals added later in the evening join the morning's note
    # rather than replacing it. That is what makes a partial check-in safe to do twice.
    rec = store.put_day(day, {"checkin": checkin})
    store.log(f"check-in recorded for {day}", source="journal")
    return rec


# ---- outreach: messages Otto wants to send other people --------------------------

class OutreachRequest(BaseModel):
    """Composing a message to a colleague. Every field except `tier` is load-bearing.

    `why` has no default on purpose. It is what the owner vetoes on, and a producer
    that cannot say why it is messaging somebody has not thought about it enough to
    be allowed to.
    """

    to: str
    body: str
    why: str
    source: str = "otto"
    tier: int = 0
    channel: Literal["slack-dm", "slack-channel"] = "slack-dm"
    hold_minutes: int | None = None
    run_id: str | None = None
    task_id: str | None = None


@router.get("/api/outreach")
def get_outreach(state: str | None = None) -> dict[str, Any]:
    items = store.outreach()
    if state:
        items = [o for o in items if o.state == state]
    return {"summary": outreach.summary(store),
            "items": [o.model_dump() for o in items[:100]]}


@router.post("/api/outreach")
def post_outreach(req: OutreachRequest) -> dict[str, Any]:
    """Compose and hold. Never sends inline, whatever the caller wants.

    A 400 here is a REFUSAL with a reason, not a validation error: it is the gate
    telling a producer that this message will not be sent and why, which is
    information the producer should act on rather than retry.
    """
    try:
        o = outreach.compose(
            store, to=req.to, body=req.body, why=req.why, source=req.source,
            tier=req.tier, channel=req.channel, hold_minutes=req.hold_minutes,
            run_id=req.run_id, task_id=req.task_id)
    except outreach.Refused as e:
        raise HTTPException(400, f"refused: {e}") from None
    # The owner is told, every time, at warn level. An outreach never seen held is
    # an outreach with no window on it, which would make the hold decorative.
    notify.post(
        store, f"Otto wants to message {o.to}",
        body=(f"{o.body}\n\nwhy: {o.why}\n\n"
              f"sends in {o.hold_minutes} min unless you stop it: "
              f"otto outreach kill {o.id[:6]}"),
        level="warn", domain=config.WORK, source="outreach",
        command=f"otto outreach kill {o.id[:6]}", notify=True)
    return o.model_dump()


@router.post("/api/outreach/{oid}/kill")
def kill_outreach(oid: str) -> dict[str, Any]:
    o = outreach.kill(store, oid)
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


@router.post("/api/outreach/{oid}/send")
def send_outreach(oid: str) -> dict[str, Any]:
    """The owner choosing not to wait out the hold."""
    o = outreach.send_now(store, oid)
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


@router.post("/api/outreach/{oid}/extend")
def extend_outreach(oid: str, minutes: int = 10) -> dict[str, Any]:
    """The owner buying time on the hold without deciding. Held messages only."""
    try:
        o = outreach.extend(store, oid, minutes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


# ---- people: operational dossiers --------------------------------------------------

@router.get("/api/people")
def get_people(q: str | None = None) -> list[dict[str, Any]]:
    """Operational dossiers. Read straight off disk rather than from state: they are
    plain markdown owned half by the directory sync and half by the owner, and
    putting them through the single-writer store would mean the daemon rewriting
    files a human edits by hand.

    `mobilePhone` is deliberately dropped from the list payload. The dashboard binds to
    127.0.0.1 so this is not an exposure, but sixty phone numbers rendered on a summary
    screen is not something any view here needs, and the detail endpoint has it.
    """
    from .. import people as _people
    rows = [{k: v for k, v in d.items() if k != "mobilePhone"} for d in _people.load()]
    if q:
        needle = q.casefold()
        rows = [d for d in rows if any(needle in str(v).casefold() for v in d.values())]
    return rows


@router.get("/api/people/{slug}")
def get_person(slug: str) -> dict[str, Any]:
    from .. import people as _people
    d = _people.get(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no dossier for {slug}")
    # Slug comes from our own listing, but it lands in a path join, so refuse anything
    # that could climb out of PEOPLE_DIR rather than trusting the caller.
    safe = _people.PEOPLE_DIR / f"{d['slug']}.md"
    if safe.parent.resolve() != _people.PEOPLE_DIR.resolve() or not safe.is_file():
        raise HTTPException(status_code=404, detail="dossier not readable")
    return {**d, "markdown": safe.read_text(encoding="utf-8", errors="replace")}


class PersonPatch(BaseModel):
    meta: dict[str, str] | None = None        # pronouns, full_name, last_contact, ...
    note: str | None = None
    section: str | None = None                # required when `note` is present


@router.patch("/api/people/{slug}")
def patch_person(slug: str, req: PersonPatch) -> dict[str, Any]:
    """Edit a dossier from the UI. The daemon is the writer, same as everywhere else.

    Validation lives in people.py rather than here so the CLI and the API cannot drift
    on what counts as an editable field or a real section.
    """
    from .. import people as _people
    if _people.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no dossier for {slug}")
    changed: list[str] = []
    try:
        if req.meta:
            _people.set_meta(slug, req.meta)
            changed.append("meta: " + ", ".join(sorted(req.meta)))
        if req.note is not None:
            if not req.section:
                raise HTTPException(status_code=422, detail="note requires a section")
            _people.add_note(slug, req.section, req.note)
            changed.append(f"note -> {req.section}")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if changed:
        # The file write has already landed. A failure to record the event must not
        # turn a successful edit into a 500, because the caller would retry and write
        # the note twice. Same ordering rule as notify.py: persist first, tell second.
        with contextlib.suppress(Exception):
            store.log(f"dossier {slug}: {'; '.join(changed)}", source="people")
    d = _people.get(slug)
    safe = _people.PEOPLE_DIR / f"{d['slug']}.md"
    return {**d, "markdown": safe.read_text(encoding="utf-8", errors="replace"),
            "changed": changed}


@router.get("/api/people/meta/schema")
def people_meta_schema() -> dict[str, Any]:
    """What the UI is allowed to edit, so the form is generated rather than hardcoded."""
    from .. import people as _people
    return {
        "fields": [{"key": k, "hint": _people.META_HINTS[k]} for k in _people.META_FIELDS],
        "sections": [h for h, _ in _people.SECTIONS],
        "dated_sections": sorted(_people.DATED_SECTIONS),
        "pronoun_suggestions": list(_people.PRONOUN_SUGGESTIONS),
    }


# ---- writing: public posts ----------------------------------------------------------

class PostPatch(BaseModel):
    status: str | None = None
    url: str | None = None
    note: str | None = None
    draft: str | None = None


class DraftRequest(BaseModel):
    note: str | None = None


@router.get("/api/writing")
def get_writing() -> dict[str, Any]:
    return writing.status(store)


@router.get("/api/writing/{post_id}")
def get_post(post_id: str) -> dict[str, Any]:
    p = store.get_post(post_id)
    if p is None:
        raise HTTPException(404, f"no post {post_id}")
    return p.model_dump()


@router.post("/api/writing/ideas")
def post_writing_ideas() -> dict[str, Any]:
    """Mine the window for post ideas now, rather than waiting for the weekly run."""
    try:
        run = writing.start_ideas(store)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, RuntimeError) as e:
        raise HTTPException(500, f"could not start the ideas run: {e}") from e
    store.upsert_run(run)
    store.log("writing: ideas run started", source="writing", run_id=run.id)
    return {"run": run.model_dump()}


@router.post("/api/writing/{post_id}/draft")
def post_writing_draft(post_id: str, req: DraftRequest | None = None) -> dict[str, Any]:
    """Draft one post, or redraft it with a note about what to change."""
    try:
        run, post = writing.start_draft(store, post_id, (req.note if req else None))
    except ValueError as e:
        code = 404 if str(e).startswith("no post") else 409
        raise HTTPException(code, str(e)) from e
    except (OSError, RuntimeError) as e:
        raise HTTPException(500, f"could not start the draft: {e}") from e
    store.upsert_run(run)
    store.log(f"writing: drafting {post.id} ({post.hook[:50]})", source="writing", run_id=run.id)
    return {"run": run.model_dump(), "post": post.model_dump()}


@router.patch("/api/writing/{post_id}")
def patch_post(post_id: str, req: PostPatch) -> dict[str, Any]:
    try:
        post = writing.set_status(store, post_id, req.status, url=req.url,
                                  note=req.note, draft=req.draft)
    except ValueError as e:
        code = 404 if str(e).startswith("no post") else 400
        raise HTTPException(code, str(e)) from e
    # Persist first, tell second; a failed event write must not 500 a landed edit.
    with contextlib.suppress(Exception):
        store.log(f"writing: {post.id} -> {post.status}", source="writing")
    return post.model_dump()
