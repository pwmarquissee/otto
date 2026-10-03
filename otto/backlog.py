"""Backlog triage: assessing cards, and the gate that decides what may run.

WHY THIS IS A MODULE AND NOT A PROMPT

`queued` means "Otto spawns a real Claude Code session with
--dangerously-skip-permissions and lets it work". The rule that only a
tier-0, Otto-owned, fully-specified card may be promoted was previously written
in `/orchestrate`'s instructions, which is to say it was enforced by asking a
model nicely. The board's own history says how that ends: a task of unknown
vintage sitting in `queued` was dispatched on the daemon's first tick and began
editing a live ops file nobody had approved changing.

So the gate lives here, in code, and `otto triage promote` is the only path the
triage pass is given. `refusal()` returns a reason or None, and every refusal is
a sentence a human can act on. A model can still be wrong about the ASSESSMENT,
but it cannot skip the gate, and it cannot promote something it never assessed.

WHY THE CAP IS ON WORK IN FLIGHT, NOT PER PASS

"Promote three at a time" only bounds anything if you know how often the pass
runs, and the whole problem being fixed here is that intake ran hourly while
assessment ran daily. A cap on how many cards are ALREADY queued or running is
self-limiting at any cadence: the backlog drains as work finishes and can never
flood, whether triage runs once a day or every twenty minutes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .models import Task

# The three tiers, spelled exactly as the board and /orchestrate use them.
TIER_AUTONOMOUS = "tier-0-autonomous"
TIER_APPROVAL = "tier-1-approval"
TIER_ASSISTIVE = "tier-2-assistive"
TIERS = (TIER_AUTONOMOUS, TIER_APPROVAL, TIER_ASSISTIVE)

# Statuses that mean "Otto is already spending money on this".
IN_FLIGHT = ("queued", "running")


def in_flight(tasks: list[Task]) -> list[Task]:
    return [t for t in tasks if t.status in IN_FLIGHT]


def is_assessed(task: Task) -> bool:
    """Whether the triage pass has judged this card.

    All four together, deliberately. A card with a tier but no owner was judged
    by the old flow, which had no concept of who does the work, and it must be
    re-read rather than trusted.
    """
    return bool(task.assessed and task.owner and task.readiness and task.tier)


# Columns a card can sit in unjudged. `needs-you` and `blocked` are included
# because a failed run lands a card in `needs-you` with no assessment, and for
# three weeks nothing ever looked at those: the list only scanned `backlog`.
ASSESSABLE = ("backlog", "needs-you", "blocked")


def needs_assessment(tasks: list[Task]) -> list[Task]:
    """Open cards the triage pass has not judged, oldest first.

    Oldest first because the complaint is clutter: the cards that have sat
    longest are the ones most likely to be stale, already done, or quietly
    blocking something, and they are the reason the list is unreadable.
    """
    rows = [t for t in tasks if t.status in ASSESSABLE and not is_assessed(t)]
    rows.sort(key=lambda t: t.created or "")
    return rows


def refusal(task: Task, tasks: list[Task], prepare: bool = False) -> str | None:
    """Why this card may NOT be auto-promoted to `queued`, or None if it may.

    Every branch returns a sentence, not a code. A refusal a human cannot read
    is a refusal that gets worked around.

    Three ways through, by tier:
      tier-0                 runs unattended, as before.
      tier-1, prepare=True   runs in PREPARE mode: investigate, write the
                             proposal into `plan`, apply nothing, land in
                             needs-you. Nothing changes; the owner gets something
                             concrete to approve instead of a title.
      tier-1, plan approved  runs for real, with the approved plan as its prompt.
    tier-1 used to be refused outright, so 39 otto-owned cards sat waiting for an
    approval nobody could give because there was nothing yet to approve.
    """
    if task.status != "backlog":
        return (f"only a backlog card can be promoted, and this one is "
                f"'{task.status}'")

    if not is_assessed(task):
        missing = [name for name, value in (
            ("assessed", task.assessed), ("owner", task.owner),
            ("readiness", task.readiness), ("tier", task.tier),
        ) if not value]
        return ("not assessed yet, missing " + ", ".join(missing)
                + ". Run the triage pass over it first")

    if task.owner != "otto":
        return (f"owned by {config.OWNER_NAME}, so promoting it would dispatch an agent "
                "at work only they can do. Rank it for them instead")

    if task.tier == TIER_ASSISTIVE:
        return (f"tier is '{task.tier}': gather-and-propose work that {config.OWNER_NAME} "
                "executes. It never dispatches; rank it for them instead")

    if task.tier == TIER_APPROVAL:
        if not prepare and not task.plan_approved:
            return (f"tier is '{task.tier}', so it may only run once {config.OWNER_NAME} has "
                    "approved a plan. Promote it with --prepare to have a run "
                    "write the proposal first, then `otto task plan <id> "
                    "--approve`")
        if prepare and task.plan_approved:
            return ("plan is already approved, so --prepare would throw it "
                    "away and write another. Promote without --prepare to run it")
    elif task.tier != TIER_AUTONOMOUS:
        return (f"tier is '{task.tier}', which is not one of {', '.join(TIERS)}. "
                "Re-assess it")
    elif prepare:
        return (f"{TIER_AUTONOMOUS} runs unattended as it is; --prepare is for "
                f"{TIER_APPROVAL} cards")

    if task.readiness != "ready":
        return (f"readiness is '{task.readiness}', so an agent would start "
                "without what it needs and fail or guess")

    if not task.detail:
        return ("no detail, so the spawned session would have none of this "
                "context and would be working from the title alone")

    running = in_flight(tasks)
    if len(running) >= config.TRIAGE_MAX_IN_FLIGHT:
        ids = ", ".join(t.id[:6] for t in running)
        return (f"{len(running)} card(s) already queued or running "
                f"({ids}), at the limit of {config.TRIAGE_MAX_IN_FLIGHT}. "
                "It will be promotable once one finishes")

    return None


def promotable(tasks: list[Task]) -> list[Task]:
    """Every backlog card that would pass the gate right now.

    Evaluated against a board that grows as cards are promoted, so this reports
    what could go in ONE pass rather than a list whose tail is already invalid
    by the time you act on it.
    """
    out: list[Task] = []
    board = list(tasks)
    for t in sorted(tasks, key=lambda t: t.created or ""):
        if refusal(t, board) is None:
            out.append(t)
            # Stand-in for the promoted card, so the in-flight cap counts it.
            board = board + [t.model_copy(update={"status": "queued"})]
    return out


def preparable(tasks: list[Task]) -> list[Task]:
    """tier-1 cards that could go out in prepare mode right now."""
    return [t for t in sorted(tasks, key=lambda t: t.created or "")
            if refusal(t, tasks, prepare=True) is None]


def summary(tasks: list[Task]) -> dict:
    """Counts for the triage surfaces. Cheap, so any caller can show it."""
    backlog = [t for t in tasks if t.status == "backlog"]
    assessed = [t for t in backlog if is_assessed(t)]
    return {
        "backlog": len(backlog),
        "assessed": len(assessed),
        "unassessed": len([t for t in tasks if t.status in ASSESSABLE
                           and not is_assessed(t)]),
        "faded": len([t for t in tasks if t.status == "faded"]),
        "in_flight": len(in_flight(tasks)),
        "max_in_flight": config.TRIAGE_MAX_IN_FLIGHT,
        "promotable": len(promotable(tasks)),
        "preparable": len(preparable(tasks)),
        "awaiting_plan_approval": len(
            [t for t in tasks if t.status == "needs-you" and t.plan
             and not t.plan_approved]),
        "mine": len([t for t in assessed if t.owner == "owner"]),
        "otto": len([t for t in assessed if t.owner == "otto"]),
        "blocked_on_a_decision": len(
            [t for t in assessed if t.readiness == "needs-decision"]),
        "waiting_on_information": len(
            [t for t in assessed if t.readiness == "needs-info"]),
    }


def stale_candidates(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """Backlog cards old enough to be worth checking for "already done".

    Age is the trigger for LOOKING, never the reason to close. Closing needs
    positive evidence that the work happened, which only the triage pass can
    establish, so this returns candidates and nothing more.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    out = []
    for t in tasks:
        if t.status != "backlog":
            continue
        # A declared next-look date is the card saying "nothing will have changed
        # before then". Re-checking it daily anyway is what produced eight identical
        # EDR console reads on one card.
        if t.next_look and t.next_look[:10] > today:
            continue
        # Age from the last check, not from birth: a card verified yesterday is
        # not stale today, whatever its created date says.
        age = _age_days(t.last_checked or t.created, now)
        if age is not None and age >= config.TRIAGE_STALE_DAYS:
            out.append(t)
    out.sort(key=lambda t: (t.last_checked or t.created or ""))
    return out


# Tags that mark a card as part of the current milestone; those never fade on
# age alone, because "nobody touched it for two weeks" is normal for a punchlist
# item that is waiting on a dependency, and losing one silently is the expensive
# mistake.
FADE_EXEMPT_TAGS = frozenset({"m5", "punchlist", "milestone"})


def fade_candidates(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """The owner's own harvested commitments that nobody has touched for FADE_DAYS.

    The rules are narrow on purpose, and every one of them is a reason this card
    is a reminder rather than a task:
      * owned by the owner (or unassessed): Otto's own work is promoted or prepared,
        never faded.
      * filed by a feed or an agent, not typed by the owner. What he wrote himself he
        can fade by hand; Otto does not decide his own notes stopped mattering.
      * no due date, priority below high, not blocked on a decision, not tagged
        for the milestone. `high` was set by someone on purpose; four of the
        first fade candidates were high (an exposed-secrets rotation among them).
      * `updated` older than FADE_DAYS. A reply, an edit, a re-file, or a triage
        note all count as touching it.
    A faded card is not done and not deleted. It stops being counted.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for t in tasks:
        if t.status != "backlog":
            continue
        if t.owner == "otto":
            continue
        if t.source == "manual":
            continue
        if t.due or t.priority in ("urgent", "high") or t.readiness == "needs-decision":
            continue
        if FADE_EXEMPT_TAGS & set(t.tags or []):
            continue
        age = _age_days(t.updated or t.created, now)
        if age is not None and age >= config.FADE_DAYS:
            out.append(t)
    out.sort(key=lambda t: t.updated or t.created or "")
    return out


def _age_days(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400
