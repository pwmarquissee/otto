"""What matters right now, and whether anybody has checked lately.

WHY THIS IS NOT IN CLAUDE.md

It was, and it went stale exactly as you would expect. CLAUDE.md carried:

    The <month> publisher demo is the top priority. All work should be evaluated
    against whether it advances, protects, or unblocks that milestone.

Two months after the demo had happened the line was still there, still being read
into every session, still steering agents toward a milestone in the past. Nobody had
lied; a dated fact had simply been written somewhere with no expiry.

The rule that follows: **CLAUDE.md holds things that are true because of how the
organization works. Anything true only until a date belongs here.** Tools, account
layout, conventions, hard-won lessons: those age slowly and a human notices when
they are wrong. A quarter's priority ages on a schedule and nobody notices at all,
because a stale sentence reads exactly like a fresh one.

WHY A FILE AND NOT A BOARD CARD

Priorities are the frame you judge cards against, not cards. They are prose, they are
short, and they change a few times a year. `profile.md` solved the same problem the
same way: a file under OTTO_HOME, outside the repo, editable without a deploy and
absent without breaking anything.

WHAT ACTUALLY STOPS THE ROT

Moving the sentence does not stop it going stale, it only moves where it rots. What
stops it is Otto NOTICING. The file carries a `reviewed:` date and goes stale like
any schedule or feed, so the failure becomes visible instead of silent. That is the
same trick the rest of Otto already runs on anything that can quietly stop being
true.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import config

_REVIEWED = re.compile(r"^\s*reviewed:\s*(\d{4}-\d{2}-\d{2})\s*$", re.I | re.M)

TEMPLATE = """reviewed: {today}

# What matters right now

(Replace this. A few lines, in your own words: what the next weeks are actually
for, and what you are protecting. Otto reads this when it decides what to propose
and what to promote, so vague here means vague there.)

# What I am not doing

(Just as useful. Naming what is deliberately not happening stops Otto and everyone
else re-proposing it.)
"""


def text() -> str:
    """The priorities as written, or "" if none are recorded."""
    try:
        return config.PRIORITIES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def reviewed_on() -> str | None:
    """The `reviewed:` date, or None if absent or unparseable."""
    m = _REVIEWED.search(text())
    if not m:
        return None
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return m.group(1)


def age_days(now: datetime | None = None) -> int | None:
    """Days since the last review, or None if it was never dated."""
    on = reviewed_on()
    if not on:
        return None
    now = now or datetime.now(timezone.utc)
    then = datetime.strptime(on, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max(0, (now - then).days)


# A phrase only the unedited template contains. `--init` stamps today's date, so
# without this check a file nobody has filled in reports as freshly reviewed and
# perfectly healthy while saying nothing at all - which is precisely the failure this
# module exists to catch, reproduced by the tool meant to prevent it.
_UNFILLED = "Replace this."


def status(now: datetime | None = None) -> dict:
    """Everything a caller needs to render or judge the priorities."""
    body = text()
    age = age_days(now)
    if not body:
        state = "missing"
    elif _UNFILLED in body:
        state = "template"
    elif age is None:
        state = "undated"
    elif age > config.PRIORITIES_MAX_AGE_DAYS:
        state = "stale"
    else:
        state = "ok"
    return {
        "state": state,
        "text": body,
        "reviewed": reviewed_on(),
        "age_days": age,
        "path": str(config.PRIORITIES_PATH),
        "max_age_days": config.PRIORITIES_MAX_AGE_DAYS,
    }


def gaps() -> list[dict]:
    """Blind spots, in the shape the rest of Otto uses.

    A missing or stale priority is not cosmetic. Every agent that decides what to
    propose, promote or skip reads this, so when it is wrong they are all
    confidently wrong in the same direction, and nothing about the output looks
    unusual.
    """
    st = status()
    if st["state"] == "ok":
        return []

    # Same row shape the other detectors use, so this lands in `otto gaps` and the
    # dashboard without anything special-casing it.
    common = {"domain": config.WORK, "command": "otto priorities"}
    if st["state"] == "missing":
        return [{
            **common,
            "id": "gap:priorities-missing",
            "kind": "priorities-drift",
            "title": "Otto does not know what matters right now",
            "why": (f"No priorities recorded at {st['path']}. Scout and orchestrate "
                    f"choose work with nothing to weigh it against, so they will "
                    f"still choose confidently. Run `otto priorities --init`."),
            "score": 75,
        }]
    if st["state"] == "template":
        return [{
            **common,
            "id": "gap:priorities-template",
            "kind": "priorities-drift",
            "title": "priorities file is still the starter template",
            "why": (f"{st['path']} exists and carries a fresh review date, so nothing "
                    f"else would ever flag it, but it still says 'Replace this.' "
                    f"Write what actually matters, in your own words."),
            "score": 75,
        }]
    if st["state"] == "undated":
        return [{
            **common,
            "id": "gap:priorities-undated",
            "kind": "priorities-drift",
            "title": "priorities have no review date",
            "why": (f"{st['path']} has no `reviewed: YYYY-MM-DD` line, so it can "
                    f"never be reported stale. That is exactly how the last one "
                    f"outlived its milestone by two months."),
            "score": 45,
        }]
    return [{
        **common,
        "id": "gap:priorities-stale",
        "kind": "priorities-drift",
        "title": f"priorities last reviewed {st['age_days']} days ago",
        "why": (f"Reviewed {st['reviewed']}, past the {st['max_age_days']} day mark. "
                f"Everything Otto proposes is still being weighed against it. Re-read "
                f"it and update the `reviewed:` line even if nothing changed."),
        "score": 45,
    }]


def render() -> str:
    st = status()
    if st["state"] == "missing":
        return (f"  no priorities recorded\n"
                f"  {st['path']}\n"
                f"  run `otto priorities --init` to start one")
    head = {
        "ok": f"  reviewed {st['reviewed']} ({st['age_days']}d ago)",
        "stale": (f"  STALE - reviewed {st['reviewed']}, {st['age_days']}d ago "
                  f"(limit {st['max_age_days']}d)"),
        "template": "  UNFILLED - still the starter template",
        "undated": "  UNDATED - add a `reviewed: YYYY-MM-DD` line",
    }[st["state"]]
    body = "\n".join("  " + ln for ln in st["text"].splitlines())
    return f"{head}\n  {st['path']}\n\n{body}"
