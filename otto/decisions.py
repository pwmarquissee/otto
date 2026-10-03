"""The decision record: what was decided, why, and what would change it.

Otto tracks activity (`runs`, `events`, day rollups) and intent (the board). It
tracked reasoning nowhere. The reasoning did exist -- it was being written into
task detail fields, which is where it went to die:

    08fb85  "DESIGN CONSTRAINT (2026-08-03): whatever we ask the vendor..."
    eb8e52  "ROOT PROBLEM (2026-08-03): the owner has NO admin access to..."
    04bc9d  "WHY THIS IS THE STRATEGIC ONE (2026-08-03): Otto was built to..."

Every one of those is a decision with a rationale, stored on a card whose whole
purpose is to eventually be marked `done` and trimmed. So Otto could tell you
what it was doing and never why, and the answer to "didn't we already decide
this?" lived in the owner's memory, which is the thing this system exists to stop
depending on.

WHAT THIS IS NOT. Not the journal (`journal.py` rolls up what a day contained,
generated from transcripts, disposable and regenerable). Not memory (facts about
the owner and the environment). A decision is a deliberate choice with alternatives
that were rejected, and it is authored, not derived.

TWO PROPERTIES, both load-bearing.

APPEND-ONLY. No edit, no delete, at the store layer as well as here. To change a
decision you record a new one that supersedes it, and both survive. A log you can
rewrite cannot answer "what did I think in August", which is the only question it
exists to answer. Same shape as registry.reconcile marking a vanished definition
`missing` rather than dropping it: the history of having believed something is
itself the data.

WATCHED REVISIT CONDITIONS. This is the part taken structurally from AIS-OS
(github.com/nateherkai/AIS-OS, MIT), whose decision format asks for "what would
change your mind" -- a genuinely good field that most templates lack. In that kit
it is prose in a markdown file, which means it is read exactly as often as
somebody opens the file, which is never. Otto makes it a control: `revisit_by`
turns the condition into a date, `gaps()` raises it once that date passes, and a
decision resting on an assumption nobody rechecked becomes a gap rather than a
sentence. That is the same move as `FeedSource.max_age_hours` -- a declared
expectation with a watcher behind it, because silence is the failure mode Otto
exists to remove.

A decision with no `revisit` is allowed (plenty are permanent), but a decision
with a `revisit` and no `revisit_by` is reported as a gap of its own: a condition
nobody will ever check is indistinguishable from not having written one.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any

from . import config
from .models import Decision, iso, utcnow
from .store import Store

# Short, typable, stable. A decision gets referenced in commit messages and in
# conversation ("see d-4f2a1c"), so a 32-char uuid would be retyped wrong. Derived
# from title+date+decision so re-recording the identical decision twice collides
# loudly instead of quietly duplicating.
ID_LEN = 6

# Bands mirroring advisor.py so a decision gap sorts sensibly against the rest.
_HIGH = 75
_MED = 45
_LOW = 20


def make_id(title: str, decided: str, decision: str) -> str:
    raw = f"{title.strip().lower()}|{decided}|{decision.strip().lower()}"
    return "d-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:ID_LEN]


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return None


def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------

def build(title: str, decision: str, why: str, **kw: Any) -> Decision:
    """Validate and shape a Decision. Raises ValueError with a usable message.

    Refuses an empty `why` rather than storing the entry without one. The whole
    point of the log is the reasoning; an entry recording only WHAT was decided is
    the task detail field this replaces, and accepting it would let the log fill
    with rows that look complete and answer nothing.
    """
    title = (title or "").strip()
    decision = (decision or "").strip()
    why = (why or "").strip()

    if not title:
        raise ValueError("a decision needs a title")
    if not decision:
        raise ValueError("a decision needs the decision itself (--decision)")
    if not why:
        raise ValueError(
            "a decision needs a why (--why). Recording what was decided without "
            "the reasoning is what task detail fields already do badly; the why is "
            "the only part future-you cannot reconstruct."
        )

    decided = kw.pop("decided", None) or iso(utcnow())[:10]
    if _parse_day(decided) is None:
        raise ValueError(f"decided must be an ISO date (YYYY-MM-DD), got {decided!r}")

    revisit = (kw.pop("revisit", None) or "").strip() or None
    revisit_by = kw.pop("revisit_by", None) or None
    if revisit_by:
        if _parse_day(revisit_by) is None:
            raise ValueError(f"revisit_by must be an ISO date, got {revisit_by!r}")
        revisit_by = str(revisit_by)[:10]
    if revisit_by and not revisit:
        raise ValueError(
            "revisit_by needs a revisit condition (--revisit): a date with no "
            "condition tells you to think again but not what to think about"
        )

    domain = kw.pop("domain", None) or config.WORK
    if domain not in config.DOMAINS:
        raise ValueError(f"domain must be one of {config.DOMAINS}")

    tags = kw.pop("tags", None) or []
    alternatives = (kw.pop("alternatives", None) or "").strip() or None

    return Decision(
        id=make_id(title, decided, decision),
        title=title[:160],
        decision=decision,
        why=why,
        alternatives=alternatives,
        revisit=revisit,
        revisit_by=revisit_by,
        owner=(kw.pop("owner", None) or config.OWNER_NAME),
        domain=domain,  # type: ignore[arg-type]
        tags=[str(t).strip() for t in tags if str(t).strip()][:8],
        decided=str(decided)[:10],
        task_id=kw.pop("task_id", None),
        origin=kw.pop("origin", None),
    )


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------

def gaps(store: Store | None = None) -> list[dict]:
    """Decisions whose assumptions nobody has rechecked.

    Advisor-shaped rows, wired the same way `feeds.gaps()` and `manifest.gaps()`
    are. Only live decisions are considered: a superseded one has already been
    revisited, which is exactly what superseding it means.
    """
    store = store or Store()
    rows: list[dict] = []
    live = [d for d in store.decisions() if d.live]
    today = datetime.now().astimezone().date()

    for d in live:
        due = _parse_day(d.revisit_by)
        if due is not None and due <= today:
            over = (today - due).days
            rows.append({
                "id": f"gap:decision-revisit:{d.id}",
                "kind": "decision-revisit",
                "domain": d.domain,
                "title": f"decision '{d.title[:60]}' is due for a rethink"
                         + (f" ({over}d past)" if over else " (today)"),
                "why": f"Recorded {d.decided}: {d.revisit}. That condition was set as "
                       f"the thing that would change this decision, and the date for "
                       f"checking it has passed. Nothing else is watching it.",
                "command": f"otto decision {d.id}",
                "score": _HIGH if over > 14 else _MED,
            })
        elif d.revisit and not d.revisit_by:
            # The unknown-unknown class, same as a schedule with no max_age_hours:
            # the condition is written down and nothing will ever check it.
            rows.append({
                "id": f"gap:decision-unwatched:{d.id}",
                "kind": "decision-unwatched",
                "domain": d.domain,
                "title": f"decision '{d.title[:60]}' has a revisit condition nobody checks",
                "why": f"It says it should be revisited when: {d.revisit}. With no "
                       f"revisit_by date, that will never surface on its own, which "
                       f"makes it a note rather than a control.",
                "command": f"otto decision {d.id}",
                "score": _LOW,
            })

    return rows


# ---------------------------------------------------------------------------
# linking back to the board
# ---------------------------------------------------------------------------

def for_task(store: Store, task_id: str) -> list[Decision]:
    """Decisions recorded against one card. Cheap enough to call per render."""
    if not task_id:
        return []
    return [d for d in store.decisions()
            if d.task_id and (d.task_id == task_id or task_id.startswith(d.task_id)
                              or d.task_id.startswith(task_id))]


def search(store: Store, query: str, limit: int = 10) -> list[Decision]:
    """Substring match across title, decision, why, alternatives, and tags.

    Deliberately dumb. The log is authored prose in the hundreds of entries, not
    the millions, so a scan answers instantly and cannot go stale the way an index
    can. Ranking is recency, because a recent decision usually supersedes an old
    one in spirit even when nobody recorded it as a formal supersede.
    """
    terms = [t for t in re.split(r"\s+", (query or "").strip().lower()) if t]
    if not terms:
        return []
    hits: list[Decision] = []
    for d in store.decisions():
        hay = " ".join(filter(None, [
            d.title, d.decision, d.why, d.alternatives or "",
            d.revisit or "", " ".join(d.tags),
        ])).lower()
        if all(t in hay for t in terms):
            hits.append(d)
    return hits[:limit]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def render_one(d: Decision) -> str:
    lines = [
        f"{d.id}  {d.title}",
        "",
        f"  decided   {d.decided}  ({d.domain}, owner {d.owner})",
    ]
    if d.task_id:
        lines.append(f"  card      {d.task_id}")
    if d.origin:
        lines.append(f"  recorded  {d.origin}")
    lines += ["", "  DECISION", f"    {d.decision}", "", "  WHY", f"    {d.why}"]
    if d.alternatives:
        lines += ["", "  ALTERNATIVES CONSIDERED", f"    {d.alternatives}"]
    if d.revisit:
        when = f" (check by {d.revisit_by})" if d.revisit_by else " (NO date set, so nothing checks it)"
        lines += ["", f"  WOULD CHANGE THIS{when}", f"    {d.revisit}"]
    if d.tags:
        lines += ["", f"  tags      {', '.join(d.tags)}"]
    if d.supersedes:
        lines.append(f"  supersedes {d.supersedes}")
    if d.superseded_by:
        lines += ["", f"  SUPERSEDED BY {d.superseded_by}. This is history, not current "
                      f"thinking."]
    return "\n".join(lines)


def render(store: Store | None = None, domain: str | None = None,
           include_superseded: bool = False, limit: int = 30) -> str:
    """One-line-per-decision list for `otto decisions`."""
    store = store or Store()
    items = store.decisions()
    if domain:
        items = [d for d in items if d.domain == domain]
    if not include_superseded:
        items = [d for d in items if d.live]
    if not items:
        return ("decision log\n\n"
                "  nothing recorded yet.\n"
                "  otto decide \"<title>\" --decision \"...\" --why \"...\"\n")

    today = datetime.now().astimezone().date()
    lines = ["decision log  (append-only; supersede, never edit)", ""]
    for d in items[:limit]:
        flag = "  "
        due = _parse_day(d.revisit_by)
        if not d.live:
            flag = "xx"
        elif due is not None and due <= today:
            flag = "->"     # due for a rethink
        elif d.revisit and not d.revisit_by:
            flag = " ?"     # condition with no watcher
        lines.append(f"  {flag} {d.id}  {d.decided}  {d.domain:<8} {d.title[:62]}")
    lines.append("")

    live = [d for d in items if d.live]
    due_now = [d for d in live
               if (_parse_day(d.revisit_by) or date.max) <= today]
    unwatched = [d for d in live if d.revisit and not d.revisit_by]
    lines.append(f"{len(live)} live, {len(due_now)} due for a rethink, "
                 f"{len(unwatched)} with an unwatched condition")
    if len(items) > limit:
        lines.append(f"({len(items) - limit} more, --limit to see them)")
    return "\n".join(lines)
