"""The owner's note on a quiet thread, and Otto deciding what to do with it.

THE ASK (2026-08-05). The threads-quiet notice listed 8 of 23 and said "15
more in the same band", which is unactionable: you cannot decide about what you
cannot see. Seeing them is `nudges.thread_rows` plus the dashboard. This module is
the other half: write a note against one thread, send it, and let Otto pick the
verb.

WHY A SPAWNED SESSION AND NOT A FUNCTION. Choosing the verb needs judgement, which
needs a model, and the daemon makes no model calls (same rule `advisor.next_up`
and `nudges` are built on). So the daemon records the note and spawns a tracked
headless session, exactly like `refresh` and `meetings ingest` already do. The
note is stored BEFORE the spawn, so a session that dies never takes the owner's
typing with it.

WHAT BOUNDS IT. The owner chose open judgement over a fixed verb, and was told the
trade: you cannot predict the action before it happens. That is the point of the
feature, so the bounding is not "restrict the choice", it is "make every outcome
cheap to undo and impossible to miss":

  * THE VERBS ARE A CLOSED LIST. Four, all reversible or held. The prompt states
    it, and three of the four are ordinary Otto writes the owner already reviews.
  * NOTHING REACHES A COLLEAGUE UNSEEN. The DM verb goes through
    `outreach.compose`, which records the message, requires a `why`, and holds it
    for OUTREACH_HOLD_MINUTES before sending. That interlock is in code, not in
    the prompt, so a session cannot talk its way past it.
  * IT CANNOT FAN OUT. No dispatching further sessions, no queueing tasks (a
    `queued` card is a dispatch trigger, so cards are filed to `backlog`).
  * IT REPORTS. Every run ends with what it did, and the run is tracked like any
    other, with its log captured.

ONE THREAD PER RUN. Batching would be cheaper and is deliberately not offered: a
single run acting on twelve threads makes twelve decisions the owner reviews as one
paragraph, and the outreach hold would fire on all of them together.
"""

from __future__ import annotations

import sys
import time

from . import config
from .models import Event, Run
from .runners import detached
from .store import Store

_last_submit = 0.0

# A note is a sentence, not a document. The cap is here rather than in the UI so a
# direct API caller cannot hand a session a 40KB prompt.
MAX_NOTE = 2000

# How a spawned session invokes the CLI.
#
# The daemon's own interpreter, by absolute path, with `-m otto`. Not the bare word
# `otto`: the console script exists only once the package is installed, and these
# sessions deliberately run in OTTO_HOME rather than in the repo, so whatever is on
# the session's PATH is not something this module can assume. The interpreter that
# is running the daemon can import otto by construction (it just did), so
# `<that python> -m otto` works from any working directory on any platform.
# Verified from an unrelated cwd the first time this was a PowerShell shim.
OTTO_CMD = f'"{sys.executable}" -m otto'

PROMPT_TEMPLATE = """You are Otto, deciding what to do about ONE quiet thread on
behalf of {owner}, who has just written a note about it and sent it to you.

THREAD
  Person:      {who} ({login})
  Open since:  {when} ({days} days ago, band: {band})
  Recorded as: {text}

THE NOTE FROM {owner}
  {note}

The note is the instruction. The thread is the context for it. If the note says
what to do, do that; do not second-guess it. If the note is ambiguous, do the part
that is unambiguous and say what you did not do.

YOU MAY DO ANY OF THESE, AND NOTHING ELSE:

1. Update this thread, when the note is a fact about it ("he's on it", "done",
   "waiting on legal"). This is usually the right verb, and it is the only one
   that stops the thread being reported as quiet, because it REPLACES the dated
   line rather than adding a second one about the same thing:
     {otto} thread-update {tid} "<what is true now>"
   Do NOT write a date into the text. The date is added for you, and a line that
   carries its own becomes "2026-08-05: 2026-08-05: ...".
   Prefer this whenever the note records what happened rather than asking for
   something new. Use it even when the thread is finished: say so in the text
   ("...confirmed complete"), which both closes the loop and leaves the history.

1b. Only if the note is about something OTHER than this thread, add a separate
   dossier line instead, same no-date rule:
     {otto} people note {slug} --section Threads "<the new thing>"

2. File a board card, when the note implies work {owner} has to do:
     {otto} task add "<title>" --detail "<why, and what done looks like>" --domain work
   Backlog only. Never --status queued: queued is a dispatch trigger and this run
   does not get to start other agents.

3. Draft a Slack DM to that person, when the note asks for them to be chased and
   a message from {owner} is the actual next step:
     {otto} outreach compose --to {login} --body "<the message>" --why "<why now>"
   This does NOT send. It records the message and holds it for
   {hold} minutes so {owner} can kill it; silence sends it. Write the body as {owner}
   would: short, direct, no preamble, no em dashes. The `why` is what {owner} reads
   to decide in ten seconds whether to stop it, so make it a reason, not a
   restatement of the message.

4. Record a decision, when the note settles something worth remembering:
     {otto} decide "<title>" --decision "<what>" --why "<reasoning>"

Or do nothing at all, if the note does not call for anything. That is a real
answer and is better than inventing work.

RULES
- Usually ONE verb. Two only if the note genuinely contains two things. Never all
  four.
- Do not dispatch agents, edit any file directly, or run anything not listed here.
- Do not send the DM yourself. Compose it and let the hold do its job.
- If a command fails, say so plainly. Do not retry it more than once and do not
  work around it.

Finish with one short paragraph: what you did, and why that verb rather than
another. That paragraph is what {owner} sees against this thread.
"""


class Refused(RuntimeError):
    """The note was not accepted, and no session was spawned."""


def active_runs(store: Store) -> list[Run]:
    return [r for r in store.runs()
            if r.status == "running" and "mode=thread-note" in (r.notes or "")]


def submit(store: Store, thread: dict, note: str) -> Run:
    """Record the note, then spawn the session that decides what to do with it.

    Order matters. The event is appended first so the note survives a spawn
    failure, a daemon restart, or a session that dies before reporting. The owner typed
    it; losing it because a subprocess did not start would be the one unforgivable
    failure here.
    """
    global _last_submit

    note = (note or "").strip()
    if not note:
        raise Refused("empty note")
    if len(note) > MAX_NOTE:
        raise Refused(f"note is {len(note)} characters, the cap is {MAX_NOTE}")

    running = active_runs(store)
    if len(running) >= config.THREAD_NOTE_MAX_CONCURRENT:
        raise Refused(
            f"{len(running)} thread note(s) already being worked; "
            "wait for one to finish"
        )
    since = time.time() - _last_submit
    if since < config.THREAD_NOTE_THROTTLE_SECONDS:
        raise Refused(
            f"one note every {config.THREAD_NOTE_THROTTLE_SECONDS}s; "
            f"{config.THREAD_NOTE_THROTTLE_SECONDS - since:.0f}s to go"
        )

    # Persist first, spawn second.
    store.append_event(Event(
        level="info", source="thread-note",
        message=f"note on {thread['who']}'s thread from {thread['when']}: {note}",
        data={"thread_id": thread["id"], "slug": thread["slug"],
              "when": thread["when"], "days": thread["days"],
              "text": thread["text"], "note": note},
    ))

    prompt = PROMPT_TEMPLATE.format(
        who=thread["who"], login=thread.get("login") or thread["slug"],
        slug=thread["slug"], when=thread["when"], days=thread["days"],
        band=thread["band"], text=thread["text"], note=note,
        hold=config.OUTREACH_HOLD_MINUTES, otto=OTTO_CMD, tid=thread["id"],
        owner=config.OWNER_NAME,
    )

    try:
        run = detached.spawn(
            name=f"note:{thread['slug']}"[:40],
            prompt=prompt,
            cwd=str(config.OTTO_HOME),
            mode="headless",
            skip_permissions=True,
            domain=config.WORK,
            budget_usd=config.THREAD_NOTE_BUDGET_USD or None,
            model=config.THREAD_NOTE_MODEL or None,
        )
    except (ValueError, OSError) as e:
        raise Refused(f"could not spawn: {e}") from e

    # Tagged so poll_runs, `active_runs` and `runs_for` can all find it, and so the
    # thread it belongs to survives in the run record rather than only in the log.
    run.notes = ((run.notes or "")
                 + f" | mode=thread-note thread={thread['id']}").strip(" |")
    store.upsert_run(run)

    _last_submit = time.time()
    store.log(f"thread note sent to Otto for {thread['who']} ({thread['when']})",
              source="thread-note", run_id=run.id)
    return run


def notes_for(store: Store, thread_id: str, limit: int = 20) -> list[dict]:
    """Notes already written against a thread, newest first.

    Read back out of the event log rather than a table of their own. A note is a
    thing that happened, it is never edited, and the event log is already the
    append-only record of things that happened.
    """
    out: list[dict] = []
    for e in store.events(600):
        if e.source != "thread-note":
            continue
        data = e.data or {}
        if data.get("thread_id") != thread_id or not data.get("note"):
            continue
        out.append({"at": e.at, "note": data["note"]})
        if len(out) >= limit:
            break
    return out


def runs_for(store: Store, thread_id: str) -> list[dict]:
    """Judgement runs against a thread, with what each one concluded."""
    tag = f"thread={thread_id}"
    out = []
    for r in store.runs():
        if tag not in (r.notes or ""):
            continue
        out.append({
            "id": r.id, "status": r.status, "started": r.started,
            "result": r.result_summary, "cost_usd": r.cost_usd,
        })
    return out
