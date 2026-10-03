"""Reasons for Otto to speak up that are not "something broke".

Otto's voice had four triggers before this module, and all four fire on a thing that is
BROKEN OR DUE: the morning check-in (since removed), a meeting brief, a feed producer asking for a
notice, and dated commitments lifted out of meeting notes. Nothing noticed a thread
going quietly stale, and nothing knew a date was coming. Those are the two ways work
actually goes wrong for one person covering CIO, CISO and helpdesk at once: not a
failure, a slow fade.

Three deterministic triggers here (owner decision, 2026-08-05). The open-ended fourth,
model-composed observations, deliberately does NOT live here: it needs a model, the
daemon makes no model calls, and a budget written in a prompt is not a budget. It runs
as `/observe` on a schedule with its ceiling enforced in Python.

WHAT THIS MODULE IS NOT ALLOWED TO DO.

  NO DEBT CLAIMS       A dated bullet in a dossier's Threads section is not proof the
                       owner owes anybody anything. prep.py documents the same trap and for
                       the same reason: the section holds real commitments next to
                       "Intern, timezone Asia/Seoul". So the wording is "open since",
                       never "you owe". Getting that wrong tells the owner something untrue
                       about a colleague, which is the one error class prep refuses.
  DEDUPED, PER TRIGGER Every trigger checks the notice store before speaking, the way
                       daily-rollup and prep already do. A tick loop runs every few
                       seconds; without it each of these is an interruption every tick.
                       Overdue cards and milestones are about today and go daily.
                       Threads go WEEKLY: "what went quiet" is a review, and a review
                       that arrives every morning is one nobody reads twice.
  BOUNDED ON BOTH SIDES  The threads trigger has a ceiling as well as a floor. The real
                       dossiers held 38 threads past 7 days with the oldest at 156, and
                       a daily notice about 38 things is the failure mode the owner has
                       already asked to be rid of twice. Past the ceiling the honest
                       verb is `otto retire`, not a nudge, and the count is stated
                       without being listed.
  NO MODEL, NO NETWORK Same reason advisor.next_up has none. A nudge that costs thirty
                       cents and ten seconds is a nudge that gets disabled.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

from . import config, notify, people, prep
from .store import Store

# A Threads bullet as written by hand at the point of contact: "2026-07-27: I owe
# real vendor quotes and a call on the buyback proposal". The leading date is the
# convention this reads; a bullet without one is skipped rather than guessed at.
_DATED = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:\-]\s*(.+)$")


def _already(store: Store, source: str, today: date, within_days: int = 1) -> bool:
    """Whether this trigger has spoken recently enough to stay quiet.

    `within_days=1` is same-day, which is what the existing daily triggers do. Larger
    windows make a trigger periodic without needing a schedule of its own.
    """
    floor = today - timedelta(days=within_days - 1)
    for n in store.notices():
        if n.source != source:
            continue
        try:
            when = date.fromisoformat((n.at or "")[:10])
        except ValueError:
            continue
        if when >= floor:
            return True
    return False


# ---------------------------------------------------------------------------
# 1. open threads going quiet
# ---------------------------------------------------------------------------

def dated_threads(store: Store, today: date | None = None) -> list[tuple]:
    """Every dated dossier thread, as (who, iso date, days, text). Oldest first."""
    today = today or datetime.now().astimezone().date()
    out: list[tuple] = []
    for p in people.load():
        if p.get("external"):
            continue  # a dossier note about an external contact is not the owner's thread
        sections = prep._sections(p)
        for raw in sections.get("Threads", []):
            m = _DATED.match(str(raw).lstrip("- ").strip())
            if not m:
                continue
            try:
                when = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            out.append((str(p.get("display_name") or p.get("login")),
                        m.group(1), (today - when).days, m.group(2).strip()))
    out.sort(key=lambda t: -t[2])
    return out


def stale_threads(store: Store, today: date | None = None) -> list[tuple]:
    """Threads inside the recoverable band: quiet a while, not yet archaeology."""
    return [t for t in dated_threads(store, today)
            if config.NUDGE_THREAD_STALE_DAYS <= t[2] <= config.NUDGE_THREAD_MAX_DAYS]


def thread_id(slug: str, when: str, text: str) -> str:
    """A stable handle for one thread line.

    Dossier threads are markdown lines, not records, so there is no id to borrow.
    This derives one from the three things that identify a line: whose file it is
    in, its date, and its text. Stable across reloads, and it changes when the text
    changes, which is correct: an edited thread is a different thread to act on.
    """
    digest = hashlib.sha1(f"{slug}|{when}|{text}".encode("utf-8")).hexdigest()[:8]
    return f"{slug}:{when}:{digest}"


def thread_rows(store: Store, today: date | None = None) -> list[dict]:
    """Every dated thread as a dict, with the slug and band the tuple form drops.

    A parallel view rather than a change to `dated_threads`: three callers and the
    conformance suite depend on that tuple shape. What the tuple cannot carry is
    the slug, and a slug is what any write-back needs, because a display name is
    not an identity.

    Bands are named here rather than at the call site so the notice, the dashboard
    and the CLI cannot disagree about what "quiet" means.
    """
    today = today or datetime.now().astimezone().date()
    rows: list[dict] = []
    for p in people.load():
        if p.get("external"):
            continue
        slug = str(p.get("slug") or p.get("login") or "")
        who = str(p.get("display_name") or p.get("login") or slug)
        for raw in prep._sections(p).get("Threads", []):
            m = _DATED.match(str(raw).lstrip("- ").strip())
            if not m:
                continue
            try:
                date.fromisoformat(m.group(1))
            except ValueError:
                continue
            when, text = m.group(1), m.group(2).strip()
            days = (today - date.fromisoformat(when)).days
            if days < config.NUDGE_THREAD_STALE_DAYS:
                band = "fresh"
            elif days <= config.NUDGE_THREAD_MAX_DAYS:
                band = "quiet"
            else:
                band = "ancient"
            rows.append({
                "id": thread_id(slug, when, text),
                "slug": slug, "who": who, "when": when, "days": days,
                "text": text, "band": band,
                "login": str(p.get("login") or ""),
            })
    rows.sort(key=lambda r: -r["days"])
    return rows


def find_thread(store: Store, tid: str) -> dict | None:
    return next((r for r in thread_rows(store) if r["id"] == tid), None)


def open_threads_notice(store: Store, today: date | None = None) -> list[str]:
    today = today or datetime.now().astimezone().date()
    if _already(store, "threads-quiet", today, config.NUDGE_THREAD_EVERY_DAYS):
        return []
    rows = stale_threads(store, today)
    if not rows:
        return []
    body_lines = [
        # "Open since", never "you owe". See the module docstring.
        f"- {who}, open since {when} ({days}d): {text[:96]}"
        for who, when, days, text in rows[:8]
    ]
    if len(rows) > 8:
        body_lines.append(f"...and {len(rows) - 8} more in the same band.")
    # The count past the ceiling, stated and NOT listed. It is real, it is not news,
    # and hiding the number entirely would make this notice look like the whole truth.
    ancient = [t for t in dated_threads(store, today)
               if t[2] > config.NUDGE_THREAD_MAX_DAYS]
    if ancient:
        body_lines.append(
            f"\n{len(ancient)} more have been quiet over "
            f"{config.NUDGE_THREAD_MAX_DAYS}d (oldest {ancient[0][2]}d). Those are a "
            "keep-or-drop call rather than a reply: otto retire")
    notify.post(
        store,
        f"{len(rows)} thread(s) went quiet: {config.NUDGE_THREAD_STALE_DAYS}"
        f"-{config.NUDGE_THREAD_MAX_DAYS} days without contact",
        body="\n".join(body_lines),
        level="info", domain=config.WORK, source="threads-quiet",
        command="otto prep",
    )
    return [f"posted threads-quiet notice ({len(rows)} in band, {len(ancient)} older)"]


# ---------------------------------------------------------------------------
# 2. milestones coming
# ---------------------------------------------------------------------------

def milestones(today: date | None = None) -> list[tuple[str, str, int, str]]:
    """(label, iso date, days out, domain) for milestones inside the horizon."""
    today = today or datetime.now().astimezone().date()
    out = []
    for label, iso_date, domain in config.MILESTONES:
        try:
            when = date.fromisoformat(iso_date)
        except ValueError:
            continue
        days = (when - today).days
        if 0 <= days <= config.MILESTONE_HORIZON_DAYS:
            out.append((label, iso_date, days, domain))
    out.sort(key=lambda m: m[2])
    return out


def milestone_notice(store: Store, today: date | None = None) -> list[str]:
    today = today or datetime.now().astimezone().date()
    if _already(store, "milestone", today):
        return []
    rows = milestones(today)
    if not rows:
        return []
    lines = []
    for label, iso_date, days, _domain in rows:
        lines.append(f"- {label}: {days} day(s), {iso_date}")
        # What is still open against it, matched by name. Deliberately a plain
        # substring match on the label's first word rather than anything clever: a
        # countdown that silently mis-attributes cards is worse than a bare countdown.
        tag = label.split()[0].lower()
        hits = [t for t in store.tasks()
                if t.status != "done"
                and tag in f"{t.title} {t.detail or ''}".lower()]
        if hits:
            lines.append(f"    {len(hits)} open card(s) mention '{tag}':")
            for t in hits[:3]:
                lines.append(f"      [{t.priority}] {t.title[:72]}")
    soonest = rows[0]
    notify.post(
        store,
        f"{soonest[0].split('(')[0].strip()} in {soonest[2]} day(s)",
        body="\n".join(lines),
        level="warn" if soonest[2] <= 7 else "info",
        domain=config.WORK, source="milestone",
        command="otto board",
    )
    return [f"posted milestone notice ({len(rows)} inside horizon)"]


# ---------------------------------------------------------------------------
# 3. overdue board cards
# ---------------------------------------------------------------------------

def overdue(store: Store, today: date | None = None) -> list:
    today = today or datetime.now().astimezone().date()
    out = []
    for t in store.tasks():
        if t.status == "done" or not t.due:
            continue
        try:
            when = date.fromisoformat(str(t.due)[:10])
        except ValueError:
            continue
        if when < today:
            out.append((t, (today - when).days))
    out.sort(key=lambda r: -r[1])
    return out


def overdue_notice(store: Store, today: date | None = None) -> list[str]:
    today = today or datetime.now().astimezone().date()
    if _already(store, "overdue", today):
        return []
    rows = overdue(store, today)
    if not rows:
        return []
    body = "\n".join(f"- {days}d late [{t.priority}] {t.title[:80]}"
                     for t, days in rows[:8])
    worst = rows[0][1]
    notify.post(
        store,
        f"{len(rows)} card(s) are past their due date",
        body=body,
        # Informational since the per-card due notices below took over the
        # interrupting. This is the digest you read in the dashboard; the toast that
        # buzzes carries one card and a Reply button, because a digest cannot.
        level="info",
        domain=config.WORK, source="overdue",
        command="otto board",
    )
    return [f"posted overdue notice ({len(rows)} card(s), worst {worst}d)"]


# ---------------------------------------------------------------------------
# 4. one card, one due date, one question
# ---------------------------------------------------------------------------
# Owner request (2026-09-03): a card nearing or past its due date should push a toast he can
# ANSWER, not just read. The answer either updates the card (done, push it, drop the
# date) or gives Otto context and lets it decide what follows. The digest above
# cannot do that: eight cards in one notice have no Reply button that knows which
# card you meant. So every pressing card gets its own notice, carrying `task_id`,
# and the toast's Reply button deep-links the dashboard to that card's reply box.
#
# Phases, not days. A card interrupts once when it comes within DUE_SOON_DAYS, once
# on the day, once when it slips past, and then every DUE_RENAG_DAYS while it stays
# late. The dedupe key carries the phase, so `notify.post` bumps a repeat within a
# phase instead of re-toasting it, and the NOTICE_DEDUPE_HOURS window does not have
# to be trusted to hold across a week.

_DUE_SOURCE = "due"


def due_phase(days_left: int) -> str | None:
    """Which phase a card with `days_left` until due is in, or None if not pressing.

    `late/<n>` includes the re-nag period, so a card three weeks overdue has been
    asked about three times: not twenty-one, and not once."""
    if days_left < 0:
        period = max(1, config.DUE_RENAG_DAYS)
        return f"late/{(-days_left - 1) // period}"
    if days_left == 0:
        return "today"
    if days_left <= config.DUE_SOON_DAYS:
        return "soon"
    return None


def due_cards(store: Store, today: date | None = None) -> list[tuple]:
    """Live cards that are pressing: (task, days_left, phase), most pressing first.

    Most pressing means most overdue first, then highest priority. A card two weeks
    late outranks an urgent one due tomorrow, because the late one is the one the
    board has already stopped telling the truth about."""
    today = today or datetime.now().astimezone().date()
    rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    out = []
    for t in store.tasks():
        if t.status == "done" or not t.due:
            continue
        try:
            when = date.fromisoformat(str(t.due)[:10])
        except ValueError:
            continue
        left = (when - today).days
        phase = due_phase(left)
        if phase is None:
            continue
        out.append((t, left, phase))
    out.sort(key=lambda r: (r[1], rank.get(r[0].priority, 9), r[0].created))
    return out


def due_key(task_id: str, phase: str) -> str:
    return f"{_DUE_SOURCE}/{task_id}/{phase}"


def _due_posted_today(store: Store, today: date) -> int:
    """Due toasts already posted on `today`, a LOCAL date.

    `Notice.at` is a UTC stamp. Comparing its first ten characters to a local
    date made the cap reset at 17:00 Pacific instead of midnight: every evening
    the count read zero and the budget was spent twice. Convert the stamp to the
    local day before comparing, which is also the only way the test that pins
    `today` passes after UTC midnight.
    """
    count = 0
    for n in store.notices():
        if n.source != _DUE_SOURCE or not n.at:
            continue
        try:
            posted = datetime.fromisoformat(n.at.replace("Z", "+00:00")).astimezone().date()
        except ValueError:
            continue
        if posted == today:
            count += 1
    return count


def due_line(days_left: int) -> str:
    if days_left < 0:
        return f"overdue {-days_left}d"
    if days_left == 0:
        return "due today"
    if days_left == 1:
        return "due tomorrow"
    return f"due in {days_left}d"


def due_card_notices(store: Store, today: date | None = None) -> list[str]:
    """One notice per pressing card, once per phase, at most DUE_TOASTS_PER_DAY new
    ones a day. The rest wait for tomorrow's budget; the digest still lists them."""
    today = today or datetime.now().astimezone().date()
    rows = due_cards(store, today)
    if not rows:
        return []
    budget = max(0, config.DUE_TOASTS_PER_DAY - _due_posted_today(store, today))
    already = {n.key for n in store.notices() if n.source == _DUE_SOURCE and n.key}
    notes: list[str] = []
    held = 0
    for t, left, phase in rows:
        key = due_key(t.id, phase)
        if key in already:
            continue
        if budget <= 0:
            held += 1
            continue
        title = f"{due_line(left)}: {t.title[:90]}"
        body = ((t.assessed_note or "").strip() or (t.detail or "").strip()[:240]
                or f"on the board as {t.status}")
        body += ("\n\nReply to update the card (done, push it, drop the date) or tell "
                 "Otto what changed and it will work out what follows.")
        notify.post(
            store, title, body=body,
            # warn so it toasts. Phase dedupe, not level, is what keeps this from
            # nagging: the same card in the same phase bumps the existing notice.
            level="warn", domain=t.domain, source=_DUE_SOURCE,
            command=f'otto task reply {t.id[:6]} "..."',
            key=key, task_id=t.id,
        )
        budget -= 1
        notes.append(f"due notice: {t.id[:6]} {phase} ({due_line(left)})")
    # Said once, alongside the notices it explains, not on every fifteen-second tick
    # for the rest of the day.
    if held and notes:
        notes.append(f"{held} pressing card(s) held for tomorrow's toast budget "
                     f"({config.DUE_TOASTS_PER_DAY}/day); the digest lists them")
    return notes


# ---------------------------------------------------------------------------

def tick(store: Store, today: date | None = None) -> list[str]:
    """Every deterministic nudge, deduped per day. Called from the daemon tick."""
    notes: list[str] = []
    for fn in (open_threads_notice, milestone_notice, overdue_notice, due_card_notices):
        try:
            notes += fn(store, today)
        except Exception as e:  # noqa: BLE001 - one bad dossier must not stop the rest
            store.log(f"nudge {fn.__name__} failed: {e}", level="warn", source="nudges")
    return notes
