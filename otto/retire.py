"""Eliminate first: what Otto should stop doing.

THE BIAS THIS EXISTS TO CORRECT. Every detector in Otto adds. `/scout` hunts for
improvements and files cards. `findings.file_tasks` files what agents notice.
`feeds` lets producers propose more. `advisor.gaps()` proposes fixes. `manifest`
and `hardening` propose things to wire up. Not one of them can conclude that
something should stop existing, so the only direction state moves is up. The
evidence that this is a real bias and not a theoretical one:

  * `heartbeat.md` documents its own `LOOPS` config as dead ("changing it has no
    effect on anything anyone reads"). It is still there, and nothing found it.
  * `/slack-sweep` went from nonexistent to armed-hourly-unattended in one step,
    on the strength of a single run.
  * 87 cards accumulated in 8 days, and no mechanism exists that can remove one
    except the owner reading it and deciding.

CALIBRATION, and the honest state of it on the day this shipped. Otto's state was
8 days old: 87 cards with the oldest backlog entry untouched for 8 days, every
seen_count still at 1, every schedule having run at least once, zero missing
definitions. So every detector below correctly reports NOTHING right now, and the
thresholds are deliberately set for a mature system rather than tuned down until
they fire. A detector that fires on a fortnight-old board would be measuring the
board's age, not its rot, and the first thing it taught the owner would be to ignore it.
Expect the first real hits around week five.

Borrowed from AIS-OS (github.com/nateherkai/AIS-OS, MIT), whose EAD method insists
on running Eliminate BEFORE Automate ("if nobody would notice it disappeared, kill
it -- don't automate waste") and pairs it with a Kill Switch: an automation that
needs constant patching or costs more than it saves gets torn down, sunk cost
notwithstanding. Otto had the launch button and no counterpart.

WHAT THIS DELIBERATELY IS NOT. It does not delete anything, and it never will.
Every row here is a QUESTION, phrased as a candidate with the evidence attached,
because whether a card still matters is a fact about the owner's intentions and no
threshold can read those. A detector that closed stale cards itself would be
deciding what matters to him on a timer, which is the same class of mistake as a
feed being allowed to file `auto=True`.

It is also not a nag. Each detector groups rather than enumerating: "11 cards have
gone quiet" is one row the owner can act on, where eleven rows are a second backlog
inside the report about the first backlog.

Thresholds live in `config.RETIRE_*` so they are tunable without touching logic,
and the reasoning for each number is documented there rather than here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .models import Schedule, Task
from .store import Store

# Bands mirroring advisor.py. Nothing here is above MED on purpose: cleanup is
# never more urgent than an outage, and a retire row that outranked a real alert
# would be actively harmful.
_MED = 45
_LOW = 20


def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400


# ---------------------------------------------------------------------------
# the detectors
# ---------------------------------------------------------------------------

def stale_cards(tasks: list[Task]) -> list[Task]:
    """Backlog cards nobody has touched in RETIRE_CARD_STALE_DAYS.

    Backlog only. A `blocked` card is waiting on something external and its age
    says nothing about whether it matters; `needs-you` is already asking. Only the
    backlog can quietly accumulate things the owner has silently decided against.
    """
    out = []
    for t in tasks:
        if t.status != "backlog":
            continue
        age = _age_days(t.updated)
        if age is not None and age >= config.RETIRE_CARD_STALE_DAYS:
            out.append(t)
    out.sort(key=lambda t: t.updated)
    return out


def nagging_cards(tasks: list[Task]) -> list[Task]:
    """Findings refiled RETIRE_NAG_SEEN+ times and still not acted on.

    `seen_count` was built so a daily detector bumps a counter instead of filing a
    duplicate, which fixed the firehose. It also created a signal nothing reads: a
    card at seen_count 9 is a detector and a human disagreeing with each other
    every morning, silently, forever.
    """
    out = [t for t in tasks
           if t.status in ("backlog", "needs-you")
           and t.seen_count >= config.RETIRE_NAG_SEEN]
    out.sort(key=lambda t: -t.seen_count)
    return out


def never_ran(scheds: list[Schedule]) -> list[Schedule]:
    """Enabled schedules that have never once produced a run."""
    out = []
    for s in scheds:
        if not s.enabled or s.cadence.kind == "manual" or s.last_run:
            continue
        age = _age_days(s.created)
        if age is not None and age >= config.RETIRE_NEVER_RAN_DAYS:
            out.append(s)
    return out


def broken_brakes(scheds: list[Schedule]) -> list[Schedule]:
    """Schedules the failure brake disabled, which nobody re-armed.

    This is the Kill Switch case exactly: the circuit breaker did its job, and then
    the schedule sat there disabled. Either it is worth fixing or it is worth
    deleting, and what it must not be is a permanently dark entry that makes the
    schedule list look fuller than the system actually is.
    """
    return [s for s in scheds if s.disabled_reason]


def missing_definitions(store: Store) -> list:
    """Registry entries whose file vanished more than RETIRE_MISSING_DAYS ago."""
    out = []
    for e in store.registry():
        if not e.missing:
            continue
        age = _age_days(e.last_seen)
        if age is not None and age >= config.RETIRE_MISSING_DAYS:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------

def gaps(store: Store | None = None) -> list[dict]:
    """Prune candidates as advisor-shaped rows, wired like feeds.gaps().

    Grouped, one row per class rather than per item, for the reason in the module
    docstring: a report that lists every stale card is a backlog about the backlog.
    """
    store = store or Store()
    rows: list[dict] = []
    tasks = store.tasks()
    scheds = store.schedules()

    stale = stale_cards(tasks)
    if stale:
        oldest = ", ".join(f"{t.title[:34]} ({_age_days(t.updated):.0f}d)"
                           for t in stale[:3])
        rows.append({
            "id": "gap:retire-stale-cards",
            "kind": "retire-cards",
            "domain": config.WORK,
            "title": f"{len(stale)} backlog card(s) untouched for "
                     f"{config.RETIRE_CARD_STALE_DAYS}d+",
            "why": f"Oldest: {oldest}. A card nobody has moved in a month is usually a "
                   f"decision that was already made by not acting. Closing it is a real "
                   f"answer; leaving it makes the board something you skim instead of "
                   f"reads.",
            "command": "otto retire",
            "score": _LOW,
        })

    nags = nagging_cards(tasks)
    if nags:
        worst = nags[0]
        rows.append({
            "id": "gap:retire-nagging",
            "kind": "retire-nagging",
            "domain": worst.domain,
            "title": f"{len(nags)} finding(s) refiled {config.RETIRE_NAG_SEEN}+ times "
                     f"and still not actioned",
            "why": f"Worst is '{worst.title[:44]}' at seen_count {worst.seen_count}. A "
                   f"detector and a human have been disagreeing about this every run. "
                   f"Either schedule it or stop detecting it, because the loop itself "
                   f"costs attention every single day and settles nothing.",
            "command": "otto retire",
            "score": _MED,
        })

    for s in never_ran(scheds):
        age = _age_days(s.created) or 0
        rows.append({
            "id": f"gap:retire-never-ran:{s.name}",
            "kind": "retire-schedule",
            "domain": s.domain,
            "title": f"schedule '{s.name}' has never run in {age:.0f}d of existing",
            "why": f"Cadence is {s.cadence.kind}, so it has had chances. A schedule that "
                   f"never runs is either broken or unwanted, and either way it is "
                   f"padding the list you use to know what Otto is doing.",
            "command": f"otto launch {s.name}   # or: otto schedule rm {s.name}",
            "score": _MED,
        })

    for s in broken_brakes(scheds):
        rows.append({
            "id": f"gap:retire-disabled:{s.name}",
            "kind": "retire-schedule",
            "domain": s.domain,
            "title": f"schedule '{s.name}' was auto-disabled and never re-armed",
            "why": f"The failure brake tripped: {s.disabled_reason}. It has been dark "
                   f"since. Fix it and re-arm, or delete it. A disabled entry left in "
                   f"place reads as coverage that does not exist.",
            "command": f"otto schedule arm {s.name}   # or: otto schedule rm {s.name}",
            "score": _MED,
        })

    gone = missing_definitions(store)
    if gone:
        names = ", ".join(sorted(e.name for e in gone)[:5])
        rows.append({
            "id": "gap:retire-missing-defs",
            "kind": "retire-registry",
            "domain": config.WORK,
            "title": f"{len(gone)} definition(s) gone for "
                     f"{config.RETIRE_MISSING_DAYS}d+ but still listed",
            "why": f"{names}. The registry keeps a vanished definition visible on "
                   f"purpose, so a deletion is noticed rather than silent. Past a "
                   f"fortnight that has served its purpose and it is just a stale row.",
            "command": "otto registry --missing",
            "score": _LOW,
        })

    return rows


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def render(store: Store | None = None) -> str:
    """Human-readable report for `otto retire`. Read-only, always."""
    store = store or Store()
    tasks = store.tasks()
    scheds = store.schedules()

    stale = stale_cards(tasks)
    nags = nagging_cards(tasks)
    never = never_ran(scheds)
    dark = broken_brakes(scheds)
    gone = missing_definitions(store)

    lines = ["retire candidates  (eliminate before automate; nothing here is deleted)", ""]

    if nags:
        lines += [f"  REFILED AND IGNORED  (seen {config.RETIRE_NAG_SEEN}+ times, "
                  f"still open)"]
        for t in nags[:8]:
            lines.append(f"    x{t.seen_count:<3} {t.id[:6]}  {t.title[:58]}")
        lines += ["         decide: schedule it, or stop the detector that files it", ""]

    if never:
        lines.append("  NEVER RAN")
        for s in never:
            lines.append(f"    {s.name:<22} created {(_age_days(s.created) or 0):.0f}d ago, "
                         f"cadence {s.cadence.kind}")
        lines.append("")

    if dark:
        lines.append("  AUTO-DISABLED, NOT RE-ARMED")
        for s in dark:
            lines.append(f"    {s.name:<22} {str(s.disabled_reason)[:52]}")
        lines.append("")

    if stale:
        lines.append(f"  QUIET FOR {config.RETIRE_CARD_STALE_DAYS}D+  "
                     f"({len(stale)} card(s), oldest first)")
        for t in stale[:10]:
            lines.append(f"    {(_age_days(t.updated) or 0):5.0f}d {t.id[:6]}  "
                         f"{t.priority:<6} {t.title[:52]}")
        if len(stale) > 10:
            lines.append(f"    ... and {len(stale) - 10} more")
        lines.append("")

    if gone:
        lines.append(f"  DEFINITIONS GONE {config.RETIRE_MISSING_DAYS}D+")
        for e in gone[:8]:
            lines.append(f"    {e.kind:<8} {e.name:<24} last seen {e.last_seen}")
        lines.append("")

    total = len(stale) + len(nags) + len(never) + len(dark) + len(gone)
    if not total:
        return ("retire candidates\n\n"
                "  nothing to cut. Board, schedules, and registry are all inside "
                "their thresholds.\n")

    lines.append(f"{total} candidate(s): {len(nags)} nagging, {len(never)} never ran, "
                 f"{len(dark)} dark, {len(stale)} quiet, {len(gone)} gone")
    lines.append("Every line is a question, not an instruction. Otto deletes nothing "
                 "on a timer.")
    return "\n".join(lines)
