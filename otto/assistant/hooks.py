"""The assistant's share of the daemon tick, called only under OTTO_SCOPE=assistant.

Moved out of otto/daemon.py as it was, in the order it ran there. Every hook logs
through the daemon's store the way the tick loop does and swallows its own errors
for the same reason: one bad dossier or a dead Slack token must not stall the loop
that polls runs and delivers notices. `install` binds the store; nothing here
imports otto.daemon.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .. import config, inbox, meetings, notify, outreach, people, prep, summon, writing
from .. import today as _today
from ..models import Run, Schedule
from ..store import Store

store: Store                                   # bound by install()
_journal_age_hours: Callable[[str | None], float | None]

# Summons are polled far slower than the tick: see config.SUMMON_EVERY_SECONDS.
_last_summon = 0.0
_last_dm = 0.0


def install(store_: Store, *, journal_age_hours: Callable[[str | None], float | None]) -> None:
    """Bind the daemon's store and its snapshot-age helper. Called once at import."""
    global store, _journal_age_hours
    store = store_
    _journal_age_hours = journal_age_hours


# ---- finished runs -----------------------------------------------------------------

def owns(run: Run) -> bool:
    """True for a run one of the assistant's runners started (meeting ingest, writing)."""
    notes = run.notes or ""
    return "mode=ingest" in notes or "mode=writing" in notes


def harvest(run: Run) -> list[str]:
    """Settle one finished assistant run: read its output, stamp its schedule."""
    notes: list[str] = []
    if "mode=ingest" in (run.notes or ""):
        try:
            notes.extend(meetings.harvest(store, run))
            owner = next((sc.name for sc in store.schedules()
                          if sc.runner == "ingest"), None)
            if owner:
                # Only a run that actually returned usable JSON counts as a success.
                # Stamping a failed fetch would make an ingester that has been dead
                # for a week read as perfectly fresh, which is the blindness the
                # staleness clock exists to remove.
                if run.status == "ok":
                    store.stamp(owner, "ok", run.id)
                else:
                    store.mark_attempt(owner, run.status, run.id)
        except Exception as e:  # noqa: BLE001 - one bad harvest must not stall the loop
            notes.append(f"meeting ingest harvest failed: {e}")
    elif "mode=writing" in (run.notes or ""):
        try:
            notes.extend(writing.harvest(store, run))
            if "kind=ideas" in (run.notes or ""):
                owner = next((sc.name for sc in store.schedules()
                              if sc.runner == "writing"), None)
                if owner:
                    # Same rule as ingest: only a run that returned usable JSON
                    # counts, or a miner dead for a month reads as fresh.
                    if run.status == "ok":
                        store.stamp(owner, "ok", run.id)
                    else:
                        store.mark_attempt(owner, run.status, run.id)
        except Exception as e:  # noqa: BLE001 - one bad harvest must not stall the loop
            notes.append(f"writing harvest failed: {e}")
    return notes


# ---- due schedules -----------------------------------------------------------------

class Autostart:
    """One tick's view of the assistant runners that may autostart.

    ingest   the built-in read-only Notion meeting-notes parse. Same risk class as
             refresh: an allow-listed set of read tools, and the only thing it
             writes is Otto's own board and ledger. Not per-domain: there is one
             Notion workspace, so one ingester at a time. Two in flight would both
             read the same pages before either wrote the ledger.
    writing  the built-in post-ideas miner. Material is injected, every tool is
             denied and the MCP config is empty, so it can touch nothing.
    """

    def __init__(self, runs: list[Run]) -> None:
        self.ingest_running = any(r.status == "running" and "mode=ingest" in (r.notes or "")
                                  for r in runs)

    def start(self, sched: Schedule) -> str | None:
        """Start a due assistant schedule. Returns the log line, or None for a skip."""
        if sched.runner == "ingest":
            if self.ingest_running:
                return None
            try:
                run = meetings.start(store)
                store.upsert_run(run)
                self.ingest_running = True
                return f"autostarted {sched.name} (pid {run.pid})"
            except Exception as e:  # noqa: BLE001 - a failed start is a log line, not a crash
                return f"could not autostart {sched.name}: {e}"
        if sched.runner == "writing":
            # Reads Otto's own state into a prompt, no tools, no MCP, budget-capped.
            # The cheapest risk class Otto has, so it autostarts like refresh does.
            try:
                run = writing.start_ideas(store)
                store.upsert_run(run)
                return f"autostarted {sched.name} (pid {run.pid})"
            except ValueError:
                return None  # one already in flight
            except Exception as e:  # noqa: BLE001 - a failed start is a log line, not a crash
                return f"could not autostart {sched.name}: {e}"
        return None


# ---- the tick ------------------------------------------------------------------------

def _clear_block_preps() -> list[str]:
    """Remove prep briefs posted for a time block.

    Two cases, both real. The filter shipped after the briefs did, so a "Team Work
    Time with ..." brief carrying fifteen lines of dossier was already sitting at
    the top of Today. And `config.CALENDAR_BLOCKS` can grow later, at which point
    yesterday's decision applies retroactively or it does not really hold.

    Deleted rather than marked read, because the dashboard renders the last twelve
    notices whatever their read state: marking it read leaves the same wall of text on
    the screen, one shade dimmer. Nothing is lost either. A prep brief is a joined view
    of state Otto still holds, and `otto prep` rebuilds it on demand.

    Scoped to `source == "prep"`. This must never touch a run failure or an outage
    notice that happens to have the word "standup" in its title.
    """
    notes: list[str] = []
    for n in store.notices():
        if n.source != "prep" or not _today.is_block(n.title):
            continue
        if store.delete_notice(n.id):
            notes.append(f"cleared block prep notice: {n.title[:48]}")
    return notes


def meeting_prep() -> list[str]:
    """Post the brief for a meeting starting within PREP_LEAD_MINUTES.

    This is the only proactive half of prep, and the constraints on it are all about
    not becoming an interruption the owner mutes.

    ONCE PER MEETING. Deduped on the event title within the day, so a 15-second tick
    does not deliver sixty identical toasts across the lead window. The dedup key is
    the title rather than a hash of the whole event, because a meeting whose end time
    got edited is still the same meeting.

    ONLY IF THERE IS SOMETHING TO SAY. A brief with no matched attendee is a toast
    that says there is a meeting, which the calendar already did. It is skipped,
    not sent empty.

    NEVER OFF A STALE CALENDAR. `advisor` suppresses the same way: a confident "in 10
    minutes" read off a day-old snapshot is worse than silence, because it is wrong
    in the direction of being acted on.

    RELATIONSHIP NOTES ARE WITHHELD. `sensitive=False` drops the background section
    from the toast. A toast can render on a shared screen, and the dossiers carry
    private material about colleagues; the full brief stays behind `otto prep`, which
    the owner runs deliberately.

    NOT FOR TIME BLOCKS. `prep.next_meeting` skips them. This function also clears any
    block brief already posted, see `_clear_block_preps`.
    """
    notes: list[str] = []
    notes += _clear_block_preps()
    if not config.PREP_NOTIFY:
        return notes
    snaps = {k: v.model_dump() for k, v in store.snapshots().items()}
    agenda = snaps.get(f"{config.WORK}/agenda")
    if not agenda:
        return notes
    age_h = _journal_age_hours(agenda.get("fetched_at"))
    if age_h is None or age_h > config.SNAPSHOT_STALE_HOURS:
        return notes

    p = prep.next_meeting(store, within_minutes=config.PREP_LEAD_MINUTES)
    if p is None or p.event is None or not p.matched:
        return notes
    if not prep.actionable(store, p):
        # Matched people, but nothing open with any of them. A toast here would only
        # restate the calendar entry, and the toast that restates the calendar is the
        # one that teaches the owner to dismiss the rest unread.
        return notes

    today_key = datetime.now().astimezone().date().isoformat()
    key = p.event.title[:40]
    if any(n.source == "prep" and (n.at or "")[:10] == today_key and key in n.title
           for n in store.notices()):
        return notes

    body = "\n".join(prep.brief_lines(store, p, sensitive=False))
    who = ", ".join(str(m.person.get("display_name")) for m in p.matched)
    notify.post(store,
                f"{p.event.when} {p.event.title[:40]} with {who[:60]}",
                body=body, level="info", domain=config.WORK, source="prep",
                command="otto prep", notify=True)
    notes.append(f"posted meeting prep for '{key}' ({p.minutes}m out, "
                 f"{len(p.matched)} matched)")
    return notes


def tick_prep() -> None:
    """After the journal, before the nudges: the meeting-prep toast."""
    try:
        for n in meeting_prep():
            store.log(n, source="prep")
    except Exception as e:  # noqa: BLE001 - a bad snapshot must not stall the loop
        store.log(f"prep error: {e}", level="warn", source="prep")


def tick_settle() -> None:
    """After the nudges, before feed ingest: outreach holds expiring.

    Placed AFTER nudges and before notify so that a message Otto decides to send is
    recorded and toasted in the same tick it is composed, and BEFORE deliver_pending
    so the "held, sends in 10 min" toast is not a tick behind the hold it describes.
    """
    try:
        for n in outreach.settle(store):
            store.log(n, source="outreach")
    except Exception as e:  # noqa: BLE001 - a dead token must not stall the loop
        store.log(f"outreach error: {e}", level="warn", source="outreach")


def tick_ingest() -> None:
    """After feed ingest, before notify: dossier contacts, summons, the DM inbox."""
    global _last_summon, _last_dm
    # Dossier last_contact from the DM producer's direction facts. Deterministic and
    # forward-only, and gated on the spool file's mtime, so a normal tick costs one
    # stat(). This is the writer that keeps "last spoke Nd ago" true: before it
    # existed the field was written once at seeding and never advanced again.
    try:
        for n in people.ingest_spool_contacts():
            store.log(n, source="contacts")
    except Exception as e:  # noqa: BLE001 - one bad spool line must not stall the loop
        store.log(f"contact ingest error: {e}", level="warn", source="contacts")

    # Somebody reacting with the help emoji in the help channel is a request for an
    # answer, and it is answered within the minute rather than at the next
    # four-hourly sweep. This is also where the summon gate stops being prose and
    # starts being code: the session is handed one thread, so it never decides
    # whether it was summoned.
    if time.time() - _last_summon > config.SUMMON_EVERY_SECONDS:
        _last_summon = time.time()
        try:
            for n in summon.poll(store):
                store.log(n, source="summon")
        except Exception as e:  # noqa: BLE001 - a failed poll must not stall the loop
            store.log(f"summon poll error: {e}", level="warn", source="summon")

    # The owner DMing Otto from a phone. Checked more often than the summon poll
    # because somebody is waiting on the other end of it.
    if time.time() - _last_dm > config.DM_EVERY_SECONDS:
        _last_dm = time.time()
        try:
            for n in inbox.poll(store):
                store.log(n, source="dm")
        except Exception as e:  # noqa: BLE001 - a failed poll must not stall the loop
            store.log(f"DM poll error: {e}", level="warn", source="dm")


# ---- /api/state ----------------------------------------------------------------------

def state_extra() -> dict[str, Any]:
    """The assistant's keys in the state payload (see assistant.STATE_EMPTY)."""
    return {
        # Held messages first: they are the only thing in this payload with a deadline
        # the owner can still act on.
        "outreach": {"summary": outreach.summary(store),
                     "items": [o.model_dump() for o in store.outreach()[:20]]},
        "writing": writing.summary(store),
    }
