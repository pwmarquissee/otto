"""The board: every piece of outstanding work, in one place.

Two kinds of card:

  stored     a Task the owner created, or a mirrored Notion row. Has an identity, can
             be dragged between columns, persists.
  derived    computed from live state on every request: a schedule that is due, a
             run that orphaned, an integration that is down. These deliberately
             have NO stored identity, because a card describing "daily is 25 days
             stale" must not be able to disagree with the schedule it describes.

Derived cards are `movable: false`. You do not drag "daily is stale" to Done; you
fix it, and the card disappears on the next tick because the condition cleared.
That is the whole reason for the split.

THE ONE EXCEPTION, and it was a hole rather than a design. "Fix the condition and the
card clears itself" assumes the condition CAN clear. A stale schedule can run; a down
integration can come back. A run that already failed is history: it never clears, so
a `movable: false` card describing one is permanent, and the board offered no verb for
the only thing anyone wants to do with a past failure. After the 2026-08-05 upstream
529 burst there were nine of them wedged in Needs-you with no way out short of
`otto prune` deleting the runs from the ledger.

So a failed or orphaned run card is still not draggable (there is genuinely no stored
row to write) but it IS acknowledgeable: `Run.reviewed_at`, set by `otto ack`, and the
card stops being emitted while the run stays in history. And a failure whose
`error_kind` is `api` gets no card at all, because upstream capacity is weather rather
than a defect and the schedule's own staleness alarm is the truthful signal for it:
one card about the schedule that has stopped producing, not one per failed attempt.

DONE IS A WINDOW, NOT AN ARCHIVE. Every other column drains by being worked. Done only
fills, and by 2026-08-05 it held 65 cards against 38 in Backlog, so most of the board
was history. Finished cards older than `config.BOARD_DONE_DAYS` are left off the board.
Nothing is deleted: the Task is still in the store, `otto task ls` still shows it, and
the rollups that count completions read the store rather than the board. Every surface
gets `hidden` on the column so it can say what it is not showing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config, persona, registry
from .models import BoardCard, Task
from .runners import scheduled
from .store import Store

COLUMNS: list[tuple[str, str]] = [
    ("backlog", "Backlog"),
    ("queued", "Queued"),
    ("running", "In progress"),
    ("needs-you", "Needs you"),
    ("blocked", "Blocked"),
    ("done", "Done"),
]

_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _age(ts: str | None) -> str | None:
    dt = _parse(ts)
    if dt is None:
        return None
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "now"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


def _from_task(t: Task) -> BoardCard:
    return BoardCard(
        id=t.id,
        title=t.title,
        status=t.status,
        domain=t.domain,
        priority=t.priority,
        kind="task",
        detail=t.detail,
        agent=t.agent,
        tags=list(t.tags),
        task_ref=t.task_ref,
        run_id=t.run_id,
        age=_age(t.created),
        due=t.due,
        movable=True,
        auto=t.auto,
        attempts=t.attempts,
        last_error=t.last_error,
        origin=t.origin,
        seen_count=t.seen_count,
        created=t.created,
        source=t.source,
        owner=t.owner,
        tier=t.tier,
        readiness=t.readiness,
    )


def derived_cards(store: Store) -> list[BoardCard]:
    """Cards computed from live state. Never stored, never movable."""
    cards: list[BoardCard] = []
    now = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()

    for sched in store.schedules():
        stale = scheduled.staleness(sched, now)
        due, reason = scheduled.is_due(sched, now_local)

        if stale:
            level, detail = stale
            cards.append(BoardCard(
                id=f"sched-stale:{sched.name}",
                title=f"{sched.name} has stopped running",
                status="needs-you",
                domain=sched.domain,
                priority="urgent" if level == "crit" else "high",
                kind="schedule",
                detail=detail,
                tags=["stale"],
                age=_age(sched.last_run),
                movable=False,
                command=sched.command,
            ))
        elif due:
            cards.append(BoardCard(
                id=f"sched-due:{sched.name}",
                title=sched.name,
                status="queued",
                domain=sched.domain,
                priority="normal",
                kind="schedule",
                detail=reason,
                tags=["due"],
                age=_age(sched.last_run),
                movable=False,
                command=sched.command,
            ))

    for run in store.runs()[:40]:
        if run.status == "running":
            cards.append(BoardCard(
                id=f"run:{run.id}",
                title=run.name,
                status="running",
                domain=run.domain,
                priority="normal",
                kind="run",
                detail=f"pid {run.pid}" + (f" · {persona.usage(run)}" if run.output_tokens else ""),
                run_id=run.id,
                tags=[run.runner],
                age=_age(run.started),
                movable=False,
                command=f"otto logs {persona.short(run.id)}",
            ))
        elif run.status in ("orphaned", "failed"):
            # Already acknowledged, so it is history the owner has seen. The run stays in
            # the ledger; only the card goes.
            if run.reviewed_at:
                continue
            # An upstream API error gets NO card. It is weather, not a defect: the
            # command never ran, retrying is the whole remedy, and Claude Code had
            # already retried it ten times before giving up. A high-priority
            # "needs you" card per failed attempt is what turned one bad afternoon
            # into three dispatched agents that each concluded "upstream capacity,
            # nothing broken".
            #
            # This does NOT hide a real outage. `settle` deliberately does not reset
            # the staleness clock on a transient failure, so a schedule that keeps
            # missing still surfaces as "has stopped running" -- one card about the
            # schedule, which is the true condition, instead of one per attempt.
            if run.transient:
                continue
            cards.append(BoardCard(
                id=f"run:{run.id}",
                title=f"{run.name} {'vanished' if run.status == 'orphaned' else 'failed'}",
                status="needs-you",
                domain=run.domain,
                priority="high",
                kind="run",
                detail=run.notes or run.status,
                run_id=run.id,
                tags=[run.status],
                age=_age(run.ended or run.started),
                # Still not draggable: there is no stored row behind it, so a drop
                # would have nothing to write. `otto ack` is the verb instead, and
                # `command` below is what the card tells you to run.
                movable=False,
                command=f"otto ack {persona.short(run.id)}",
            ))

    for i in store.integrations():
        if i.ok or i.mode == "mcp-only":
            continue
        cards.append(BoardCard(
            id=f"integration:{i.name}",
            title=f"{i.name} is not available",
            status="needs-you",
            domain=config.WORK,
            priority="high" if i.mode == "api" else "normal",
            kind="alert",
            detail=i.detail,
            tags=[i.mode],
            age=_age(i.checked_at),
            movable=False,
            command="otto probe",
        ))

    missing = [e for e in store.registry() if e.missing]
    if missing:
        names = ", ".join(sorted({e.name for e in missing})[:4])
        cards.append(BoardCard(
            id="registry:missing",
            title=f"{len(missing)} definition(s) no longer on disk",
            status="backlog",
            domain=config.WORK,
            priority="low",
            kind="alert",
            detail=names,
            tags=["drift"],
            movable=False,
            command="otto scan",
        ))

    for key, snap in store.snapshots().items():
        try:
            fetched = datetime.fromisoformat(snap.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - fetched).total_seconds() / 3600 > config.SNAPSHOT_STALE_HOURS:
            cards.append(BoardCard(
                id=f"snapshot:{key}",
                title=f"{snap.domain} {snap.kind} snapshot is stale",
                status="backlog",
                domain=snap.domain,
                priority="low",
                kind="alert",
                detail="Otto cannot refresh this itself, run /otto refresh",
                tags=["snapshot"],
                age=_age(snap.fetched_at),
                movable=False,
                command=f"/otto refresh --domain {snap.domain}",
            ))

    return cards


def aged_done(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """Finished tasks past `config.BOARD_DONE_DAYS`, hidden from the board.

    Uses `updated`, which is the last time anything touched the task, so re-opening
    and re-finishing a card puts it back on the board for another week. That is the
    intended reading: the column answers "what got done lately", and the last time
    it moved is the honest timestamp for that.

    A task whose `updated` will not parse is NEVER aged out. Otto does not hide work
    it cannot date.
    """
    if config.BOARD_DONE_DAYS <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.BOARD_DONE_DAYS)
    out = []
    for t in tasks:
        if t.status != "done":
            continue
        ts = _parse(t.updated)
        if ts is not None and ts < cutoff:
            out.append(t)
    return out


def build(store: Store, domain: str | None = None) -> dict:
    """Assemble the board. Columns always present, even when empty."""
    tasks = store.tasks()
    # Done off the board after a week. It is the one column nothing drains: a card
    # arrives and stays, so the board slowly became mostly history. The count is
    # carried through as `hidden` on the column rather than dropped silently,
    # because a column that quietly shows a subset is a column that lies.
    # A card closed as a duplicate of another is not a completion and not history:
    # it is the same card, shown once. Out before anything else so the Done window
    # and its `hidden` count describe real finished work.
    duplicates = [t for t in tasks if t.duplicate_of]
    tasks = [t for t in tasks if not t.duplicate_of]
    aged = aged_done(tasks)
    # Out of the columns by definition; counted, never rendered as a pile.
    faded = [t for t in tasks if t.status == "faded"]
    tasks = [t for t in tasks if t.status != "faded"]
    aged_ids = {t.id for t in aged}
    cards = [_from_task(t) for t in tasks if t.id not in aged_ids] + derived_cards(store)
    if domain:
        cards = [c for c in cards if c.domain == domain]
        aged = [t for t in aged if t.domain == domain]
        faded = [t for t in faded if t.domain == domain]
        duplicates = [t for t in duplicates if t.domain == domain]

    # Priority first, then the due date, soonest at the top. A dated card sorts ahead
    # of an undated one at the same priority: "no deadline" is genuinely weaker
    # information than "Thursday", and without this tiebreak a card due tomorrow sat
    # wherever the alphabet put it.
    cards.sort(key=lambda c: (_PRIORITY_RANK.get(c.priority, 9),
                              c.due or "9999-99-99",
                              c.title.lower()))

    columns = []
    for key, label in COLUMNS:
        col = [c for c in cards if c.status == key]
        columns.append({
            "key": key,
            "label": label,
            "count": len(col),
            "cards": [c.model_dump() for c in col],
            # Cards this column is holding back, so every surface can say so.
            "hidden": len(aged) if key == "done" else 0,
            "hidden_after_days": config.BOARD_DONE_DAYS if key == "done" else 0,
        })

    return {
        "columns": columns,
        # Not a column. A faded card is out of the columns on purpose; the count
        # is here so every surface can say "and 40 faded" instead of hiding it.
        "faded": len(faded),
        # Cards folded into another card by the dedupe sweep. Same shape as faded:
        # not a column, but a count every surface can say out loud.
        "duplicates": len(duplicates),
        "total": len(cards),
        "stored": len([c for c in cards if c.movable]),
        "derived": len([c for c in cards if not c.movable]),
        "registry": registry.summarize_by_domain(store.registry()),
    }
