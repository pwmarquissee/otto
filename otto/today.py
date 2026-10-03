"""The day view: what is left of today, and which mail actually wants you.

The data was already being fetched. Every four hours the refresher stored a work
agenda, a work mail list, and the personal equivalents, and `otto agenda` printed
them. The problem was that it printed them as STORAGE rather than as a day:

  * Sorted by snapshot key, so `personal/agenda` and `personal/mail` came first
    and were usually both empty, pushing the work calendar to third place.
  * Every event, all day, including the six that already happened. At 19:00 the
    view still opened with a 10:00 standup.
  * Items capped at 6, so an 8-event day silently lost the last two, which are
    the ones still ahead of you.
  * Two events at 12:00 rendered as two ordinary lines. Nothing said they clash.
  * Mail was a flat list. "Needs a reply from you" and "worth knowing about" look
    identical in a flat list, so the list has to be re-triaged by eye every time
    it is read, which is the work it was supposed to save.

So this module renders a day rather than a dict. Three rules:

WHAT IS LEFT, NOT WHAT HAPPENED. Past events collapse to a count. The next event
carries the minutes until it starts. A day view read at 16:00 should be about
16:00 onward, because the part you can still act on is the only part that is
decision-relevant.

CLASHES ARE FINDINGS. Two events overlapping is a thing to be told, not a thing to
notice. Detected from real intervals when the fetch supplied end times, and from
identical start times when it did not.

MAIL IS SPLIT, NOT LISTED. `needs: reply` and `needs: awareness` are rendered as
separate groups with different markers, because the whole value of a triaged inbox
is losing the need to triage it again.

BACKWARD COMPATIBILITY IS NOT OPTIONAL HERE. Snapshots already on disk were
written by the previous prompt and carry only `when` and `title`. Every field this
module reads is therefore optional with a documented fallback: no `ends` degrades
conflict detection to same-start-time, and a missing `needs` degrades to "unsorted"
rather than guessing that unclassified mail needs a reply. Guessing would put
newsletters in the group that means "answer me", which is exactly how a triaged
list stops being trusted. The old snapshots render correctly, just with less.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, time, timedelta, timezone
from typing import Any, NamedTuple

from . import config

# How close counts as "up next" rather than "later today".
SOON_MINUTES = 90

# Events that are all-day or have no parseable time. Kept and shown, because a
# day with an all-day event in it is a different day, but never used for "next up".
NO_TIME = "--:--"


class Event(NamedTuple):
    when: str            # HH:MM local, or NO_TIME
    ends: str | None
    title: str
    start: time | None   # parsed, None for all-day/unparseable
    end: time | None
    role: str | None     # organizer | required | optional, when the fetch said

    @property
    def timed(self) -> bool:
        return self.start is not None


class Mail(NamedTuple):
    when: str
    title: str
    needs: str           # reply | awareness | unsorted
    why: str | None


def _parse_hhmm(raw: Any) -> time | None:
    s = str(raw or "").strip()
    if not s or ":" not in s:
        return None
    # Tolerate "9:05", "09:05", "09:05 AM", and a full ISO timestamp, because the
    # field is produced by a model and its exact shape is not guaranteed.
    if "T" in s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().time()
        except ValueError:
            return None
    head = s.split()[0]
    ampm = s.upper()
    try:
        hh, mm = head.split(":")[:2]
        h, m = int(hh), int(mm[:2])
    except (ValueError, IndexError):
        return None
    if "PM" in ampm and h < 12:
        h += 12
    if "AM" in ampm and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def events(snap: dict | None) -> list[Event]:
    """Parse an agenda snapshot into time-sorted events. Untimed ones sort last."""
    out: list[Event] = []
    for item in ((snap or {}).get("items") or []):
        if not isinstance(item, dict):
            continue
        when = str(item.get("when") or item.get("time") or "").strip() or NO_TIME
        ends = str(item.get("ends") or item.get("end") or "").strip() or None
        start = _parse_hhmm(when)
        out.append(Event(
            when=when if start else NO_TIME,
            ends=ends,
            title=str(item.get("title") or item.get("summary") or "").strip() or "(untitled)",
            start=start,
            end=_parse_hhmm(ends),
            role=(str(item.get("role")).strip().lower() if item.get("role") else None),
        ))
    out.sort(key=lambda e: (e.start is None, e.start or time(0, 0)))
    return out


def mail(snap: dict | None) -> list[Mail]:
    """Parse a mail snapshot, preserving the fetch's own triage.

    An item with no `needs` becomes `unsorted`, never `reply`. See the module
    docstring: promoting unclassified mail into the group that means "answer me"
    is how the distinction stops being believed.
    """
    out: list[Mail] = []
    for item in ((snap or {}).get("items") or []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("needs") or "").strip().lower()
        needs = raw if raw in ("reply", "awareness") else "unsorted"
        out.append(Mail(
            when=str(item.get("when") or item.get("time") or "").strip() or NO_TIME,
            title=str(item.get("title") or item.get("summary") or "").strip() or "(no subject)",
            needs=needs,
            why=(str(item.get("why")).strip() or None) if item.get("why") else None,
        ))
    return out


def clashes(evs: list[Event]) -> list[tuple[Event, Event]]:
    """Overlapping pairs among timed events.

    Real interval overlap when both ends are known. When they are not, only an
    identical start time counts: assuming a default duration would invent clashes
    that are not there, and a clash report that cries wolf gets ignored, which
    costs more than the feature is worth.
    """
    timed = [e for e in evs if e.timed]
    out: list[tuple[Event, Event]] = []
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if a.end and b.end:
                if a.start < b.end and b.start < a.end:  # type: ignore[operator]
                    out.append((a, b))
            elif a.start == b.start:
                out.append((a, b))
    return out


def split(evs: list[Event], now: time | None = None) -> tuple[list[Event], list[Event], list[Event]]:
    """(past, remaining, untimed). `remaining` includes anything in progress."""
    now = now or datetime.now().astimezone().time()
    past, remaining, untimed = [], [], []
    for e in evs:
        if not e.timed:
            untimed.append(e)
        elif (e.end or e.start) < now:  # type: ignore[operator]
            past.append(e)
        else:
            remaining.append(e)
    return past, remaining, untimed


def minutes_until(e: Event, now: datetime | None = None) -> int | None:
    if not e.timed:
        return None
    now = now or datetime.now().astimezone()
    target = now.replace(hour=e.start.hour, minute=e.start.minute,  # type: ignore[union-attr]
                         second=0, microsecond=0)
    return int((target - now).total_seconds() // 60)


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def _age(ts: str | None) -> str:
    h = _age_hours(ts)
    if h is None:
        return "?"
    if h < 1:
        return f"{h * 60:.0f}m ago"
    if h < 48:
        return f"{h:.1f}h ago"
    return f"{h / 24:.1f}d ago"


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def render(snaps: dict[str, Any], domain: str | None = None,
           now: datetime | None = None, plain: bool = True) -> list[str]:
    """The day, as lines. Caller prints; colour is the caller's business.

    Work comes first and unconditionally. Personal is shown only when it has
    something in it: two lines saying the personal calendar is empty is the
    clutter that pushed the work calendar off the top of the screen.
    """
    now = now or datetime.now().astimezone()
    lines: list[str] = []
    domains = [domain] if domain else list(config.DOMAINS)

    for dom in domains:
        ag = snaps.get(f"{dom}/agenda")
        ml = snaps.get(f"{dom}/mail")
        evs = events(ag)
        mails = mail(ml)
        has_content = bool(evs or mails)

        # Personal earns its heading only by having something to say. Work always
        # renders, because "no work meetings today" is itself information.
        if dom != config.WORK and not has_content:
            continue
        if ag is None and ml is None:
            continue

        lines.append(f"{dom.upper()}  ({_age(( ag or ml or {}).get('fetched_at'))})")

        # ---- calendar ----
        past, remaining, untimed = split(evs, now.time())
        clash_ids = {id(e) for pair in clashes(evs) for e in pair}

        if not evs:
            lines.append("    calendar   nothing on it")
        else:
            head = f"    calendar   {len(remaining)} left of {len(evs)}"
            if past:
                head += f", {len(past)} done"
            lines.append(head)
            for e in untimed:
                lines.append(f"      all-day  {e.title[:64]}")
            for i, e in enumerate(remaining):
                mins = minutes_until(e, now)
                if i == 0 and mins is not None:
                    if mins < 0:
                        tag = "NOW"
                    elif mins <= SOON_MINUTES:
                        tag = f"in {mins}m" if mins < 60 else f"in {mins // 60}h{mins % 60:02d}"
                    else:
                        tag = ""
                else:
                    tag = ""
                span = e.when + (f"-{e.ends}" if e.ends else "")
                bang = "!" if id(e) in clash_ids else " "
                role = ""
                if e.role == "optional":
                    role = "  (optional)"
                elif e.role == "organizer":
                    role = "  (yours)"
                lines.append(f"      {bang} {span:<12} {e.title[:58]}{role}"
                             + (f"   <- {tag}" if tag else ""))

        for a, b in clashes(evs):
            if (a.end or a.start) < now.time():  # type: ignore[operator]
                continue  # a clash you already lived through is not news
            lines.append(f"      ! CLASH {a.when} {a.title[:26]} vs {b.title[:26]}")

        # ---- mail ----
        need = [m for m in mails if m.needs == "reply"]
        aware = [m for m in mails if m.needs == "awareness"]
        unsorted_ = [m for m in mails if m.needs == "unsorted"]

        if not mails:
            summary = (ml or {}).get("summary")
            lines.append(f"    mail       {summary or 'nothing worth a look'}")
        else:
            lines.append(f"    mail       {len(need)} want a reply, "
                         f"{len(aware) + len(unsorted_)} worth knowing")
            for m in need:
                lines.append(f"      REPLY {m.when:<7} {m.title[:60]}")
                # Wrapped, not truncated. The `why` is the evidence for calling this
                # a reply, so cutting it at "no resp" costs the reader the reason and
                # makes them open the mail to find out, which is the work the triage
                # was supposed to remove.
                for chunk in textwrap.wrap(m.why or "", width=64)[:2]:
                    lines.append(f"              {chunk}")
            for m in aware:
                lines.append(f"      fyi   {m.when:<7} {m.title[:60]}")
            for m in unsorted_:
                # Untriaged, because this snapshot predates the classifier. Marked
                # as such rather than quietly filed under one of the two groups.
                lines.append(f"      ?     {m.when:<7} {m.title[:60]}")

        lines.append("")

    # Feeds are a different kind of thing (a producer's panel, not the owner's day), so
    # they render after both domains rather than interleaved by sort order.
    for key in sorted(k for k in snaps if "/feed:" in k):
        snap = snaps[key] or {}
        name = key.split("/feed:", 1)[1]
        lines.append(f"{name.upper()}  ({_age(snap.get('fetched_at'))})")
        for chunk in textwrap.wrap(str(snap.get("summary") or ""), width=104)[:2]:
            lines.append(f"    {chunk}")
        for item in (snap.get("items") or [])[:6]:
            if not isinstance(item, dict):
                continue
            when = str(item.get("when") or "")[:7]
            who = str(item.get("who") or "")
            title = str(item.get("title") or "")
            lines.append(f"      {when:<7} {(who + ': ' if who else '')[:22]}{title[:64]}")
        lines.append("")

    if not lines:
        return ["  nothing fetched yet. otto refresh"]
    return lines


_BLOCK_RE = [re.compile(p, re.I) for p in config.CALENDAR_BLOCKS]


def is_block(event: Event | str) -> bool:
    """Whether this calendar entry is a time block rather than a meeting.

    See `config.CALENDAR_BLOCKS`. Blocks are still rendered in the day view: the
    point is not to hide them, it is that nothing should INTERRUPT for one.
    """
    title = event if isinstance(event, str) else event.title
    return any(r.search(title or "") for r in _BLOCK_RE)


def next_event(snaps: dict[str, Any], now: datetime | None = None,
               skip_blocks: bool = True) -> tuple[Event, int] | None:
    """The next work MEETING and minutes until it. For `otto next` and notices.

    Skips time blocks by default, and keeps walking rather than giving up: a 10:00
    standup followed by an 11:00 partner call must return the partner call, because
    both callers use this to decide whether to interrupt the owner, and the answer for a
    block is always no. Pass `skip_blocks=False` for the literal next entry.
    """
    now = now or datetime.now().astimezone()
    _, remaining, _ = split(events(snaps.get(f"{config.WORK}/agenda")), now.time())
    for e in remaining:
        if skip_blocks and is_block(e):
            continue
        mins = minutes_until(e, now)
        if mins is not None and mins >= 0:
            return e, mins
    return None


def blocks_left(snaps: dict[str, Any], now: datetime | None = None) -> list[Event]:
    """Time blocks still ahead today. So a surface can say what it skipped."""
    now = now or datetime.now().astimezone()
    _, remaining, _ = split(events(snaps.get(f"{config.WORK}/agenda")), now.time())
    return [e for e in remaining if is_block(e)]
