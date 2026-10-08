"""Meeting prep: who you are about to talk to, and what is open with them.

Otto already held every piece of this and joined none of them. The calendar said
"<owner> / <colleague>" at 12:00. A dossier said the colleague asks for recurring
HR work to be automated, wants to validate a workflow before the owner builds it,
and that on 2026-08-03 there was an in-progress issue the owner owed her a follow-up
on. The board had cards naming her. Nothing put those three facts in front of the
owner at 11:45, so the join happened in his head during the first two minutes of
the call, or did not happen.

WHAT THIS IS. A deterministic join across four stores Otto already has: the agenda
snapshot, the people dossiers, the board, and the decision log. No model call, for
the same reason `advisor.next_up()` has none: a brief that cost thirty cents and
ten seconds would not be opened before a call that starts in eight minutes.

MATCHING IS THE HARD PART, AND WRONG IS WORSE THAN EMPTY.

A calendar title is not an attendee list. "<owner> / <first name>" names a person
by first name only; "<org> + <vendor> | Intro Call" names a company. So the refresher now
supplies `who` per event, and this module falls back to matching names out of the
title when it does not (old snapshots, and events whose attendee list the tool did
not return).

The matching rules are deliberately conservative, because a prep brief that names
the wrong colleague is not a small error. It puts another person's role, working
style, pronouns, and private relationship notes in front of the owner labelled as
someone else, moments before he speaks to the real one.

  EXACT WINS         An email or a full name match is taken.
  UNIQUE FIRST NAME  A bare first name matches only if EXACTLY ONE person on the
                     roster has it. Two Harpers means no match, reported as
                     ambiguous rather than resolved by picking one.
  NEVER PARTIAL      No substring or fuzzy matching on names. "Ann" does not match
                     "Anna", and "Tim" does not match "Timothy" unless the roster
                     itself says Tim.
  OWNER IS SKIPPED   They are in every meeting; a dossier of themselves is noise.

An unmatched attendee is LISTED as unmatched. Silence would read as "nobody worth
prepping for", which is the opposite of what an unrecognised name means.

BLOCKS ARE NOT MEETINGS. Team Standups, Team Work Time, Social Hour, and the Reclaim
lunch and decompress habits occupy the calendar without being things to walk into
prepared. Standup's own title says to self-organise in Discord. So `next_meeting`
skips them (`config.CALENDAR_BLOCKS`, applied in `today.next_event`) and keeps
walking to the next real meeting, instead of spending the day's one well-timed
interruption on a block and then having nothing left for the 11:00 partner call.

`otto prep` with no meeting left says which blocks it skipped rather than "nothing on
the calendar", because the second one in front of a screen full of calendar entries
looks like a broken feed.

PRONOUNS ARE RENDERED FIRST, and never inferred from a name. `people.py` carries
the whole reason in its own comment: a seeded note said "her Okta profile" because
a memory file said so and nothing in the pipeline knew better, and Fran is a man.
The dossier is the only place that distinction is recorded, so a brief that shows
the name without the pronouns is a brief that invites the same mistake at the top
of a call. Unset renders as they/them, marked as the default rather than as fact.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from typing import Any, NamedTuple

from . import config, people, today
from .store import Store

# Bullets kept per dossier section. A brief is read standing up, so each section is
# a handful of lines and the whole thing fits on a screen. `Threads` gets the most
# because "what you owe them" is the only section that is actively actionable.
KEEP = {"Threads": 4, "What we work on": 3, "Working style": 3, "Who they are": 2}

# Sections rendered, in this order. `Relationship` is deliberately excluded from the
# proactive notice path (see brief_lines): it is the most sensitive material in the
# dossier and a toast is a surface the owner does not fully control.
ORDER = ("Threads", "Working style", "What we work on", "Who they are")

# Row labels. Spelled out rather than derived from the heading, because the derived
# version rendered "Working style" as "work", which reads as "what we work on" and
# put the two most-confusable sections under near-identical tags.
# "open", not "owe". The Threads section is documented as "open topics, things you
# owe them, what to pick up next time", and in practice holds all three: a real
# commitment sits next to "Intern, timezone Asia/Seoul". Labelling the whole section
# "owe" asserts a debt over half of it, which is a brief telling the owner something
# untrue about a colleague thirty seconds before he speaks to them.
TAGS = {"Threads": "open", "Working style": "style", "What we work on": "topic",
        "Who they are": "bg"}

# Words in a calendar title that are never people. Without this, "Sync", "Intro",
# and "Team" get run through the roster on every event for nothing.
_STOP = frozenset("""
sync call meeting intro chat standup review 1 1:1 one on daily weekly biweekly
monthly quarterly team all hands allhands offsite work time lunch break hold
focus block busy tentative optional recurring canceled cancelled zoom meet
google hangout huddle interview onboarding kickoff demo prep retro planning
check checkin catchup catch up touch base coffee walk social hour decompress
the and with vs for from about re fwd
""".split())


def _stop() -> frozenset[str]:
    """Stopwords plus the org's and the owner's own names, read at call time.

    "<org> + <vendor> | Intro" and "<owner> / <colleague>" both put a name in the
    title that is never an attendee to look up. Those names are configuration, so
    they are joined here rather than written into the literal above."""
    extra = {_norm(w) for w in f"{config.ORG_NAME} {config.OWNER_NAME}".split()}
    return _STOP | {w for w in extra if w}

_WORD = re.compile(r"[A-Za-z][A-Za-z'.-]+")


class Match(NamedTuple):
    person: dict[str, Any]
    how: str          # email | full-name | unique-first-name
    raw: str          # what the calendar actually said


class Prep(NamedTuple):
    event: today.Event | None
    minutes: int | None
    matched: list[Match]
    unmatched: list[str]
    ambiguous: list[tuple[str, list[str]]]   # (raw, candidate display names)


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _roster() -> list[dict[str, Any]]:
    """Dossiers, minus the owner. Loaded per call: 104 small files, and a brief that
    read a cached roster would show a dossier edited five minutes ago as stale."""
    me = _norm(config.MEETINGS_OWNER)
    my_login = _norm(config.MEETINGS_OWNER_EMAIL).split("@")[0]
    out = []
    for p in people.load():
        if _norm(p.get("display_name")) == me:
            continue
        if _norm(p.get("login")).split("@")[0] == my_login:
            continue
        out.append(p)
    return out


def match_names(raws: list[str], roster: list[dict[str, Any]] | None = None
                ) -> tuple[list[Match], list[str], list[tuple[str, list[str]]]]:
    """Resolve raw attendee strings to dossiers. See the module docstring for why
    each rule is this conservative. Returns (matched, unmatched, ambiguous)."""
    roster = roster if roster is not None else _roster()
    by_email = {_norm(p.get("email") or p.get("login")): p for p in roster}
    by_login = {_norm(p.get("login")).split("@")[0]: p for p in roster if p.get("login")}
    by_full = {_norm(p.get("display_name")): p for p in roster}

    # Monikers are names the owner recorded by hand, so they are evidence rather than a
    # guess -- but two people can still answer to the same one, and a recorded name
    # is no better a coin flip than a shared first name. Collisions are collected and
    # reported the same way.
    monikers: dict[str, list[dict[str, Any]]] = {}
    for p in roster:
        for alias in people.monikers_of(p.get("meta") or {}):
            key = _norm(alias)
            if key:
                monikers.setdefault(key, []).append(p)

    firsts: dict[str, list[dict[str, Any]]] = {}
    for p in roster:
        first = _norm(p.get("firstName") or str(p.get("display_name") or "").split(" ")[0])
        if first:
            firsts.setdefault(first, []).append(p)

    matched: list[Match] = []
    unmatched: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    def take(p: dict[str, Any], how: str, raw: str) -> None:
        if p["slug"] in seen:
            return
        seen.add(p["slug"])
        matched.append(Match(p, how, raw))

    for raw in raws:
        key = _norm(raw)
        if not key:
            continue
        if "@" in key:
            p = by_email.get(key) or by_login.get(key.split("@")[0])
            (take(p, "email", raw) if p else unmatched.append(raw))
            continue
        if key in by_full:
            take(by_full[key], "full-name", raw)
            continue
        # A bare login, before first names are considered. `jdoe` is as exact as
        # the full email and more exact than any name, but it was falling through to
        # the first-name check and matching nothing at all, so an attendee list of
        # logins produced an empty brief.
        if key in by_login:
            take(by_login[key], "login", raw)
            continue
        # Before first names, because a moniker is written down and a first name is
        # inferred. After logins and full names, because those are the identifiers
        # the rest of the system agrees on.
        alias_hits = monikers.get(key)
        if alias_hits and len(alias_hits) == 1:
            take(alias_hits[0], "moniker", raw)
            continue
        if alias_hits:
            ambiguous.append((raw, sorted(str(h.get("display_name")) for h in alias_hits)))
            continue
        hits = firsts.get(key)
        if hits and len(hits) == 1:
            take(hits[0], "unique-first-name", raw)
        elif hits:
            # Two people share this first name. Naming one would be a coin flip
            # printed as fact, so the ambiguity is reported instead.
            ambiguous.append((raw, sorted(str(h.get("display_name")) for h in hits)))
        else:
            unmatched.append(raw)
    return matched, unmatched, ambiguous


def names_in_title(title: str) -> list[str]:
    """Candidate person-words out of a calendar title.

    Only used when the event carries no attendee list. Returns candidates, never
    matches: `match_names` still applies the unique-first-name rule, so a title
    mentioning a shared first name produces an ambiguity rather than a wrong person.
    """
    out: list[str] = []
    stop = _stop()
    for w in _WORD.findall(title or ""):
        if len(w) < 3 or _norm(w) in stop:
            continue
        out.append(w)
    return out


def attendees(event: today.Event, raw_item: dict[str, Any] | None = None) -> list[str]:
    """Attendee strings for an event: the fetched list if present, else the title."""
    who = (raw_item or {}).get("who")
    if isinstance(who, str) and who.strip():
        # Model-supplied, so tolerate "a@x.test, b@x.test" and "Alex and Sam".
        parts = re.split(r"[,;/]| and | & ", who)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(who, list):
        return [str(p).strip() for p in who if str(p).strip()]
    return names_in_title(event.title)


def _raw_items(snaps: dict[str, Any], domain: str = config.WORK) -> list[dict[str, Any]]:
    return [i for i in ((snaps.get(f"{domain}/agenda") or {}).get("items") or [])
            if isinstance(i, dict)]


def _pair_raw(event: today.Event, raws: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the stored item an Event came from, to reach fields Event drops."""
    for i in raws:
        if str(i.get("title") or "").strip() == event.title:
            return i
    return None


def for_event(store: Store, event: today.Event, minutes: int | None,
              snaps: dict[str, Any] | None = None) -> Prep:
    snaps = snaps if snaps is not None else {
        k: v.model_dump() for k, v in store.snapshots().items()}
    raw = _pair_raw(event, _raw_items(snaps))
    m, u, a = match_names(attendees(event, raw))
    return Prep(event, minutes, m, u, a)


def next_meeting(store: Store, now: datetime | None = None,
                 within_minutes: int | None = None) -> Prep | None:
    """Prep for the next work MEETING, or None if there is not one worth prepping.

    Time blocks are not meetings and are skipped, by `today.next_event`. See the
    module docstring.
    """
    snaps = {k: v.model_dump() for k, v in store.snapshots().items()}
    nxt = today.next_event(snaps, now=now)
    if nxt is None:
        return None
    event, mins = nxt
    if within_minutes is not None and mins > within_minutes:
        return None
    return for_event(store, event, mins, snaps)


# ---------------------------------------------------------------------------
# what is open with a person
# ---------------------------------------------------------------------------

def _mentions(text: str, p: dict[str, Any]) -> bool:
    """Whether a blob of text names this person.

    Full name always; first name only when the roster makes it unambiguous, which
    is checked by the caller passing a pre-filtered roster. Deliberately not a
    substring match on the login: `clynch` appears inside no English word, but
    `asmith` inside a path would match a card about a directory.
    """
    hay = _norm(text)
    if not hay:
        return False
    full = _norm(p.get("display_name"))
    if full and full in hay:
        return True
    email = _norm(p.get("email") or p.get("login"))
    return bool(email and email in hay)


def open_cards(store: Store, p: dict[str, Any], limit: int = 4) -> list:
    out = [t for t in store.tasks()
           if t.status != "done" and _mentions(f"{t.title} {t.detail or ''}", p)]
    out.sort(key=lambda t: ({"urgent": 0, "high": 1, "normal": 2, "low": 3}
                            .get(t.priority, 2), t.updated))
    return out[:limit]


def open_decisions(store: Store, p: dict[str, Any], limit: int = 2) -> list:
    out = [d for d in store.decisions() if d.live
           and _mentions(f"{d.title} {d.decision} {d.why} {d.alternatives or ''}", p)]
    return out[:limit]


def _sections(p: dict[str, Any]) -> dict[str, list[str]]:
    try:
        # (path, frontmatter, body). Unpacking the frontmatter as the body here
        # silently produced briefs with no dossier content at all: parse_body found
        # no headings in the YAML and returned {}, so every section rendered empty
        # and the brief looked merely sparse rather than broken.
        _, _, body = people._read_split(p["slug"])
    except Exception:  # noqa: BLE001 - a missing or odd dossier must not kill a brief
        return {}
    sections, _ = people.parse_body(body)
    return sections


def _bullets(sections: dict[str, list[str]], heading: str) -> list[str]:
    lines = [ln.strip().lstrip("-").strip()
             for ln in sections.get(heading, []) if ln.strip()]
    return [ln for ln in lines if ln][:KEEP.get(heading, 3)]


def _last_contact(p: dict[str, Any]) -> str | None:
    raw = (p.get("meta") or {}).get("last_contact")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return None
    days = (datetime.now().astimezone().date() - d).days
    if days <= 0:
        return "last spoke today"
    if days == 1:
        return "last spoke yesterday"
    return f"last spoke {days}d ago ({d.isoformat()})"


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def person_lines(store: Store, m: Match, sensitive: bool = True) -> list[str]:
    """One person's block. `sensitive=False` drops Relationship-grade material for
    surfaces the owner does not fully control, like an OS toast."""
    p = m.person
    meta = p.get("meta") or {}
    pron = people.pronouns_of(meta)
    pron_note = "" if meta.get("pronouns") else " (default, not recorded)"
    role = " / ".join(x for x in (p.get("title"), p.get("department")) if x)

    head = f"  {p.get('display_name')}  [{pron}{pron_note}]"
    lines = [head]
    if role:
        lines.append(f"    {role}")
    bits = [x for x in (_last_contact(p),
                        f"circle: {meta['circle']}" if meta.get("circle") else None,
                        None if m.how == "email" else f"matched on {m.how}") if x]
    if bits:
        lines.append(f"    {' | '.join(bits)}")

    sections = _sections(p)
    for heading in ORDER:
        if not sensitive and heading == "Who they are":
            continue
        tag = TAGS.get(heading, heading[:5].lower())
        for b in _bullets(sections, heading):
            # Wrapped rather than cut. These bullets end in the operative detail
            # ("...pending her approval", "...untested"), so a hard slice at 96
            # removes the part that says what is actually outstanding.
            chunks = textwrap.wrap(b, width=94) or [""]
            lines.append(f"      {tag:<6}{chunks[0]}")
            for extra in chunks[1:2]:
                lines.append(f"            {extra}")

    for t in open_cards(store, p):
        lines.append(f"      card  [{t.priority}] {t.title[:80]}")
    for d in open_decisions(store, p):
        lines.append(f"      decid {d.id} {d.title[:74]}")
    return lines


def owed(store: Store, m: Match) -> list[str]:
    """Just the Threads bullets: what is open with this person, whether that is a
    commitment the owner made or a loose end worth picking up."""
    return _bullets(_sections(m.person), "Threads")


def compact_lines(store: Store, matched: list[Match]) -> list[str]:
    """Group-meeting mode: the roster, plus only what is actually open.

    Working style and background are 1:1 preparation. In a nine-person standup they
    are a hundred lines of material the owner cannot act on in the next fifteen minutes,
    so above `PREP_MAX_PEOPLE` the brief keeps the roster and the open threads and
    drops the rest. What survives is chosen by being actionable, not by being short.
    """
    lines = [f"  {len(matched)} on the invite with dossiers:"]
    names = ", ".join(str(m.person.get("display_name")) for m in matched)
    for chunk in textwrap.wrap(names, width=92):
        lines.append(f"    {chunk}")

    any_open = False
    for m in matched:
        bullets = owed(store, m)
        cards = open_cards(store, m.person, limit=2)
        if not bullets and not cards:
            continue
        any_open = True
        meta = m.person.get("meta") or {}
        pron = people.pronouns_of(meta)
        pron_note = "" if meta.get("pronouns") else ", default"
        lines.append("")
        lines.append(f"  {m.person.get('display_name')}  [{pron}{pron_note}]")
        for b in bullets[:2]:
            for i, chunk in enumerate(textwrap.wrap(b, width=90)[:2]):
                lines.append(f"      {'open  ' if i == 0 else '      '}{chunk}")
        for t in cards:
            lines.append(f"      card  [{t.priority}] {t.title[:78]}")
    if not any_open:
        lines.append("  nothing open with any of them.")
    return lines


def brief_lines(store: Store, prep: Prep, sensitive: bool = True) -> list[str]:
    ev = prep.event
    lines: list[str] = []
    if ev is not None:
        when = ev.when + (f"-{ev.ends}" if ev.ends else "")
        head = f"{when}  {ev.title}"
        if prep.minutes is not None:
            head += ("  (starting now)" if prep.minutes <= 0
                     else f"  (in {prep.minutes} min)")
        if ev.role == "optional":
            head += "  [you are optional]"
        lines += [head, ""]

    if len(prep.matched) > config.PREP_MAX_PEOPLE:
        lines += compact_lines(store, prep.matched)
        lines.append("")
    else:
        for m in prep.matched:
            lines += person_lines(store, m, sensitive=sensitive)
            lines.append("")

    if prep.ambiguous:
        for raw, cands in prep.ambiguous:
            lines.append(f"  ? '{raw}' could be: {', '.join(cands)}. Not guessing.")
    if prep.unmatched:
        # Named, not dropped. An unrecognised attendee is usually external, which
        # is worth knowing before the call rather than after.
        lines.append(f"  no dossier: {', '.join(prep.unmatched[:6])}")
    if not prep.matched and not prep.ambiguous and not prep.unmatched:
        lines.append("  nobody identifiable on this one.")
    return lines


def render(store: Store, prep: Prep | None, now: datetime | None = None) -> str:
    if prep is None:
        # Naming the blocks it skipped, rather than claiming the calendar is empty.
        # "Nothing left today" in front of a screen showing Work Time and Social Hour
        # reads as a broken calendar feed, which is the wrong thing to go and debug.
        snaps = {k: v.model_dump() for k, v in store.snapshots().items()}
        blocks = today.blocks_left(snaps, now=now)
        if blocks:
            names = ", ".join(e.title[:26] for e in blocks[:4])
            return ("meeting prep\n\n"
                    f"  no meetings left today. {len(blocks)} time block(s) ahead "
                    f"({names}),\n  which Otto does not prep for. "
                    "`otto prep <name>` preps a person directly.\n")
        return ("meeting prep\n\n"
                "  nothing left on today's calendar. `otto prep <name>` preps a "
                "person directly.\n")
    return "meeting prep\n\n" + "\n".join(brief_lines(store, prep)) + "\n"


def actionable(store: Store, prep: Prep) -> bool:
    """Whether this brief has anything the owner can act on.

    The gate for the proactive notice. A toast saying "you have a standup with eight
    people" repeats the calendar, and a toast that repeats the calendar is the one
    that gets muted, taking the useful ones with it. So the interruption is spent
    only when something is genuinely open with someone in the room.
    """
    for m in prep.matched:
        if owed(store, m) or open_cards(store, m.person, limit=1):
            return True
    return False


def for_person(store: Store, query: str) -> Prep | None:
    """Prep for one person by name, login, or slug, with no meeting attached."""
    p = people.get(query)
    if p is None:
        m, _, amb = match_names([query])
        if m:
            return Prep(None, None, m, [], [])
        if amb:
            return Prep(None, None, [], [], amb)
        return None
    return Prep(None, None, [Match(p, "email", query)], [], [])
