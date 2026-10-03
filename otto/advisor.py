"""What to do next, and what you cannot see.

Two jobs an assistant has that a dashboard does not:

  next_up()  rank everything Otto knows into an ordered list of what to do now,
             each entry carrying WHY it is ranked there and the command to act.
  gaps()     find the things the owner would not notice were missing. Not "this broke"
             but "nothing was ever watching this".

Both are deterministic: plain scoring over stored state, no model call, so they are
instant, free, and identical every run. Judgement lives in `otto chat`, which reads
the same state and can reason about it. The split is deliberate: a ranking that
costs money and takes ten seconds would not get looked at.

The gap detectors are the interesting half. A stale schedule is a known unknown,
already alarmed. A schedule with no `max_age_hours` is an UNKNOWN unknown: it can
stop running forever and nothing will ever say so. That class is what this finds.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import config, configsync, persona
from .runners import scheduled
from .store import Store

# Score bands. Anything >= 80 is "today", 40-79 "this week", below that "sometime".
CRIT = 100
HIGH = 75
MED = 45
LOW = 20

_PRIORITY_SCORE = {"urgent": 92, "high": 62, "normal": 32, "low": 16}

# How long a reason may be in the ranked list.
#
# `why` used to be the whole `detail` field, which on this board runs to four
# thousand characters of evidence. `otto next` printed it verbatim, so the one
# surface meant to answer "what do I start on" was itself the clutter being
# complained about. Detail is the evidence and stays on the card; this is the
# sentence that earns the card a place in the list.
_WHY_MAX = 160


def _first_line(text: str, limit: int = _WHY_MAX) -> str:
    """The opening sentence of a body of prose, trimmed on a word boundary."""
    head = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(head) <= limit:
        return head
    cut = head[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cut + "..."


def _task_why(task) -> str:
    """One line saying why this card is in the list.

    Prefers the triage note, which was written for exactly this, and falls back
    to the head of the detail so an unassessed card still says something.
    """
    if task.assessed_note:
        return _first_line(task.assessed_note)
    if task.detail:
        return _first_line(task.detail)
    return f"on the board as {task.status}"


# What the triage assessment is worth in the ranking.
#
# The point is to separate "you could start this right now" from "this is
# parked", which raw priority cannot express: 53 cards all sitting at `normal`
# scored identically, so the list was ordered by title and answered nothing.
#
# Deliberately small numbers. These reorder cards WITHIN a priority band rather
# than letting an assessment outrank something the owner marked urgent.
_READINESS_BUMP = {
    # Actionable now, so it belongs above things that are not.
    "ready": 6,
    # A person has to choose before anything can move. Ranked up because it is
    # blocking, and a decision is usually cheap next to the work it unblocks.
    "needs-decision": 4,
    # Genuinely cannot be started, so it should not sit at the top of a list of
    # things to start.
    "needs-info": -6,
}


def _triage_bump(task) -> int:
    bump = _READINESS_BUMP.get(task.readiness or "", 0)
    # Otto's own work is not what the owner is choosing between. It still appears,
    # since a stuck agent card is worth seeing, but it does not compete for the
    # top of a list whose question is "what do I pick up".
    if task.owner == "otto" and task.status == "backlog":
        bump -= 4
    return bump


def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400


def _due_pressure(due: str | None) -> tuple[int, str | None]:
    """Score bump and a human phrase for a task's due date.

    Ranking on priority alone made a `normal` card due yesterday sit below a `high`
    card with no date at all, so a deadline someone committed to in a meeting had no
    effect on what Otto told the owner to do next. An overdue item outranks everything
    short of an outage, which is the point of having agreed to a date.
    """
    if not due:
        return 0, None
    try:
        d = datetime.fromisoformat(due[:10]).date()
    except ValueError:
        return 0, None
    days = (d - datetime.now().astimezone().date()).days
    if days < 0:
        return 45, f"OVERDUE by {abs(days)}d (was due {d.isoformat()})"
    if days == 0:
        return 30, "due today"
    if days == 1:
        return 24, "due tomorrow"
    if days <= 7:
        return 12, f"due in {days}d ({d.isoformat()})"
    return 0, f"due {d.isoformat()}"


# ============================== gaps ==============================

def gaps(store: Store) -> list[dict]:
    """Blind spots. Each one is something with no watcher, not something broken."""
    out: list[dict] = []
    scheds = store.schedules()
    runs = store.runs()
    entries = store.registry()

    # 1. Schedules that can die silently. The single most important detector here:
    #    without max_age_hours there is no staleness alarm, so the schedule can
    #    stop forever and Otto will never mention it again.
    unwatched = [s for s in scheds if s.enabled and not s.max_age_hours
                 and s.cadence.kind != "manual"]
    for s in unwatched:
        out.append({
            "id": f"gap:unwatched:{s.name}",
            "kind": "unwatched-schedule",
            "domain": s.domain,
            "title": f"{s.name} has no staleness alarm",
            "why": "If it stops running, nothing will ever tell you. Cadence alone "
                   "does not detect silence.",
            "command": f"otto schedule add {s.name} --command \"{s.command}\" "
                       f"--kind {s.cadence.kind} --max-age-hours <N>",
            "score": MED,
        })

    # 2. A whole domain with no routines at all.
    for domain in config.DOMAINS:
        if not [s for s in scheds if s.domain == domain]:
            out.append({
                "id": f"gap:empty-domain:{domain}",
                "kind": "empty-domain",
                "domain": domain,
                "title": f"nothing scheduled in {domain}",
                "why": "Otto is not watching anything here, so it can never tell you "
                       "something slipped.",
                "command": "otto schedule add <name> --command \"...\" --domain " + domain,
                "score": MED if domain == config.PERSONAL else HIGH,
            })

    # 3. Blind on email/calendar. Per domain: a fresh work inbox says nothing about
    # whether Otto can see the personal one, and vice versa.
    snaps = store.snapshots()
    for expected in config.expected_snapshots():
        snap = snaps.get(expected.key)
        kind, domain = expected.kind, expected.domain
        label = f"{domain} {kind}"
        if snap is None:
            out.append({
                "id": f"gap:no-snapshot:{expected.key}",
                "kind": "no-visibility",
                "domain": domain,
                "title": f"Otto has never seen your {label}",
                "why": "The daemon cannot reach it directly. Until something pushes it, "
                       "this is invisible to every other answer Otto gives.",
                "command": f"otto refresh --domain {domain}",
                "score": HIGH,
            })
        else:
            days = _age_days(snap.fetched_at) or 0
            if days * 24 > config.SNAPSHOT_STALE_HOURS:
                out.append({
                    "id": f"gap:stale-snapshot:{expected.key}",
                    "kind": "stale-visibility",
                    "domain": domain,
                    "title": f"{label} is {days:.1f} days old",
                    "why": "Otto will answer from this as if it were current.",
                    "command": f"otto refresh --domain {domain}",
                    "score": MED,
                })

    # 4. Integrations Otto reports on but has never actually verified.
    unverified = [i for i in store.integrations() if i.mode == "mcp-only"]
    if unverified:
        out.append({
            "id": "gap:unverified-integrations",
            "kind": "unverified",
            "domain": config.WORK,
            "title": f"{len(unverified)} integration(s) are registered but never tested",
            "why": ", ".join(i.name for i in unverified) +
                   ". Otto checks only that the MCP server is configured, not that "
                   "credentials still work. Each could be dead right now.",
            "command": "check in-session via their MCP tools",
            "score": LOW,
        })

    # 5. Duplicate definitions across repos. Usually a worktree or a stale clone,
    #    and it means edits land in one copy while agents load another.
    live = [e for e in entries if not e.missing and e.kind in ("agent", "command", "skill")]
    by_name: Counter[str] = Counter(e.name for e in live)
    dupes = {n: c for n, c in by_name.items() if c > 2}
    if dupes:
        worst = sorted(dupes.items(), key=lambda kv: -kv[1])[:3]
        repos = Counter(e.repo or "global" for e in live if e.name in dupes)
        out.append({
            "id": "gap:duplicate-definitions",
            "kind": "duplication",
            "domain": config.WORK,
            "title": f"{len(dupes)} definition name(s) exist in 3+ places",
            "why": ", ".join(f"{n} x{c}" for n, c in worst) +
                   ". Concentrated in " + ", ".join(r for r, _ in repos.most_common(2)) +
                   ". Editing one copy will not affect the others.",
            "command": "otto registry --kind agent",
            "score": LOW,
        })

    # 6. Runs that ended in an unknown state and were never looked at.
    orphans = [r for r in runs if r.status == "orphaned"]
    if orphans:
        out.append({
            "id": "gap:unresolved-orphans",
            "kind": "unresolved",
            "domain": orphans[0].domain,
            "title": f"{len(orphans)} run(s) vanished without reporting",
            "why": "No exit code, so the outcome is genuinely unknown, not failed. "
                   "Whatever they were doing may be half done.",
            "command": f"otto logs {persona.short(orphans[0].id)}",
            "score": HIGH,
        })

    # 7. Config drift between the repo and what Claude Code actually loads.
    try:
        csumm = configsync.summary()
        broken = [j["name"] for j in csumm["junctions"] if not j["ok"]]
        drift = [d["name"] for d in csumm["deploy"] if d["state"] == "DRIFT"]
        if broken:
            out.append({
                "id": "gap:broken-junction",
                "kind": "config",
                "domain": config.WORK,
                "title": f"config junction broken: {', '.join(broken)}",
                "why": "Claude Code may be loading different files than the repo holds.",
                "command": "otto config",
                "score": CRIT,
            })
        if drift:
            out.append({
                "id": "gap:config-drift",
                "kind": "config",
                "domain": config.WORK,
                "title": f"{', '.join(drift)} differs between repo and ~/.claude",
                "why": "One of the two copies is not what you think it is.",
                "command": "otto config",
                "score": MED,
            })
    except Exception:  # noqa: BLE001 - gap analysis must never be the thing that breaks
        pass

    # 8. Definitions that exist but nothing has ever invoked. Only meaningful once
    #    there is enough run history to draw from, otherwise it flags everything.
    if len(runs) >= 12:
        used = {r.name for r in runs}
        never = [e for e in live if e.scope == "global" and e.kind == "command"
                 and e.name not in used]
        if len(never) > 4:
            out.append({
                "id": "gap:unused-definitions",
                "kind": "dead-config",
                "domain": config.WORK,
                "title": f"{len(never)} global command(s) have never run",
                "why": ", ".join(sorted(e.name for e in never)[:6]) +
                       ". Either they are not useful or you forgot they exist.",
                "command": "otto registry --kind command",
                "score": LOW,
            })

    # Capability manifest drift. Deliberately last and deliberately cheap: it reads
    # registry keys and filenames off disk, never a token value. This is the only
    # detector that can find something that was NEVER wired up, or something that was
    # deliberately removed and came back -- a probe can only test what is registered.
    try:
        from . import manifest as _manifest
        out.extend(_manifest.gaps())
    except Exception as e:  # noqa: BLE001 - a malformed registry must not kill `gaps`
        out.append({
            "id": "gap:manifest-unreadable",
            "kind": "manifest-drift",
            "domain": config.WORK,
            "title": "capability manifest could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Drift is now undetected, which is worse "
                   f"than any single drift it would have reported.",
            "command": "otto manifest",
            "score": MED,
        })

    # Agent identity drift. The manifest above asks whether a credential is present;
    # this asks whose it is. Regression only -- the standing "Otto runs as the owner"
    # gap is reported by `otto identity` and tracked on the board, and re-filing it
    # every morning would teach the owner to skip the one row that means something
    # new broke.
    try:
        from . import identity as _identity
        out.extend(_identity.gaps())
    except Exception as e:  # noqa: BLE001 - a bad registry must not kill `gaps`
        out.append({
            "id": "gap:identity-unreadable",
            "kind": "identity-drift",
            "domain": config.WORK,
            "title": "agent identity table could not be evaluated",
            "why": f"{type(e).__name__}: {e}. A credential appearing inline, or a new "
                   f"integration inheriting the owner's account, is now undetected.",
            "command": "otto identity",
            "score": MED,
        })

    # Feed producers. Same declare-then-diff shape as the manifest, one layer out: a
    # declared feed whose producer has gone quiet is silence where there should be
    # data, and silence is what Otto exists to remove. Also catches the inverse -- a
    # feed directory nobody declared, which Otto deliberately does NOT ingest.
    try:
        from . import feeds as _feeds
        out.extend(_feeds.gaps(store))
    except Exception as e:  # noqa: BLE001 - a bad drop must not kill `gaps`
        out.append({
            "id": "gap:feeds-unreadable",
            "kind": "feed-drift",
            "domain": config.WORK,
            "title": "feed directories could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Nothing is now checking whether the "
                   f"producers are still producing.",
            "command": "otto feeds",
            "score": MED,
        })

    # Decisions resting on assumptions nobody rechecked. A decision that recorded
    # what would change it, and a date to check that by, is the only thing in Otto
    # that can notice its own reasoning has expired. Without this the field is prose.
    try:
        from . import decisions as _decisions
        out.extend(_decisions.gaps(store))
    except Exception as e:  # noqa: BLE001 - a bad row must not kill `gaps`
        out.append({
            "id": "gap:decisions-unreadable",
            "kind": "decision-drift",
            "domain": config.WORK,
            "title": "decision log could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Revisit conditions are now unwatched.",
            "command": "otto decisions",
            "score": MED,
        })

    # What matters right now, and whether anybody has confirmed it lately. This is
    # the only detector that watches Otto's own SENSE OF PURPOSE rather than its
    # machinery: a stale priority does not break anything, it just quietly aims
    # everything else at the wrong thing, and every output still looks normal.
    try:
        from . import priorities as _priorities
        out.extend(_priorities.gaps())
    except Exception as e:  # noqa: BLE001 - a bad row must not kill `gaps`
        out.append({
            "id": "gap:priorities-unreadable",
            "kind": "priorities-drift",
            "domain": config.WORK,
            "title": "priorities could not be read",
            "why": f"{type(e).__name__}: {e}. Work is being chosen against nothing.",
            "command": "otto priorities",
            "score": MED,
        })

    # What to STOP doing. Every other detector above proposes adding something, so
    # without this the only direction state can move is up. Deliberately scored no
    # higher than MED: cleanup never outranks an outage.
    try:
        from . import retire as _retire
        out.extend(_retire.gaps(store))
    except Exception as e:  # noqa: BLE001 - a bad row must not kill `gaps`
        out.append({
            "id": "gap:retire-unreadable",
            "kind": "retire-drift",
            "domain": config.WORK,
            "title": "retire candidates could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Nothing is now watching for dead weight.",
            "command": "otto retire",
            "score": LOW,
        })

    # Definition blast radius. Same shape and same reasoning as the manifest above:
    # this is the only detector for a definition that can dispatch a session or write
    # outside its own outputs and has never said so. The cold-start incident (a queued
    # test task auto-dispatched, agent edited a live ops file) is what it exists for --
    # correct is not the same as authorised, and an unwritten boundary is unenforceable.
    try:
        from . import hardening as _hardening
        out.extend(_hardening.gaps())
    except Exception as e:  # noqa: BLE001 - a bad definition must not kill `gaps`
        out.append({
            "id": "gap:hardening-unreadable",
            "kind": "hardening-drift",
            "domain": config.WORK,
            "title": "definition hardening audit could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Nothing is now checking which definitions "
                   f"can write outside themselves.",
            "command": "otto skills audit",
            "score": MED,
        })

    # Whether Otto can see sessions at all. This one reports on the observability
    # layer rather than on the work, which is exactly why it belongs here: with the
    # hooks absent or silently broken, every session panel reads as a calm empty
    # list, and an empty list is indistinguishable from a quiet morning.
    try:
        from . import sessions as _sessions
        out.extend(_sessions.gaps(store))
    except Exception as e:  # noqa: BLE001
        out.append({
            "id": "gap:sessions-unreadable",
            "kind": "unwatched-sessions",
            "domain": config.WORK,
            "title": "session visibility could not be evaluated",
            "why": f"{type(e).__name__}: {e}. Otto cannot say whether it can see "
                   f"running Claude Code sessions.",
            "command": "otto sessions doctor",
            "score": MED,
        })

    out.sort(key=lambda g: -g["score"])
    return out


# ============================== next up ==============================

def next_up(store: Store, domain: str | None = None, limit: int = 8) -> list[dict]:
    """One ranked list of what to do now, deduped across every source."""
    items: dict[str, dict] = {}

    def add(key: str, **kw) -> None:
        # First writer wins: sources are added in descending authority so a stale
        # schedule is described as an outage, not as a generic due item.
        #
        # The key is kept ON the row as `id`, not just used for deduping. It encodes
        # what the row actually IS -- `task:<uuid>`, `sched:<name>`, `int:<name>`,
        # `gap:...` -- and without it a caller cannot tell a row that already has a
        # stored task behind it from a purely derived condition. The dashboard needs
        # that distinction: actioning the first must update that task, and actioning
        # the second must create one.
        if key not in items:
            items[key] = {"id": key, **kw}

    now = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()

    # 1. Schedules that stopped. Highest authority.
    for s in store.schedules():
        st = scheduled.staleness(s, now)
        if not st:
            continue
        level, detail = st
        days = _age_days(s.last_run)
        add(f"sched:{s.name}",
            title=f"{s.name} has stopped running",
            why=detail + (". This is silence, not a pending run." if days else ""),
            command=_run_command(s),
            domain=s.domain, kind="outage",
            score=CRIT if level == "crit" else HIGH)

    # 2. Integrations that are actually down.
    for i in store.integrations():
        if i.ok or i.mode != "api":
            continue
        add(f"int:{i.name}",
            title=f"{i.name} is unavailable",
            why=i.detail,
            command="otto probe",
            domain=config.WORK, kind="outage", score=HIGH + 5)

    # 2b. The next meeting, when it is close enough to change what you start.
    #
    # Ranked above board work on purpose, and only inside the window: a card you
    # can pick up later should not outrank a call beginning in eight minutes, but a
    # meeting four hours out is not a thing to do NOW and would just be clutter at
    # the top of the list. Skipped entirely if the snapshot is stale, since "you
    # have a call in 10 minutes" read off a day-old calendar is worse than silence.
    #
    # `next_event` skips time blocks. "Team Work Time in 20 min" outranking real board
    # work is the clutter this ranking exists to prevent, and a block is by definition
    # not a thing that changes what you should start.
    try:
        from . import today as _today
        snaps = {k: v.model_dump() for k, v in store.snapshots().items()}
        agenda_age = _age_days(
            (snaps.get(f"{config.WORK}/agenda") or {}).get("fetched_at"))
        if agenda_age is not None and agenda_age * 24 <= config.SNAPSHOT_STALE_HOURS:
            nxt = _today.next_event(snaps)
            if nxt is not None and nxt[1] <= _today.SOON_MINUTES:
                ev, mins = nxt
                when = "starting now" if mins <= 0 else f"in {mins} min"
                add("meeting:next",
                    title=f"{ev.title} {when}",
                    why=f"On the calendar at {ev.when}"
                        + (f" until {ev.ends}" if ev.ends else "")
                        + (". You are an optional attendee."
                           if ev.role == "optional" else "."),
                    command="otto agenda",
                    domain=config.WORK, kind="meeting",
                    score=CRIT - 5 if mins <= 15 else HIGH)
    except Exception:  # noqa: BLE001 - a malformed snapshot must not kill the ranking
        pass

    # 3. Board tasks you put there yourself.
    for t in store.tasks():
        # Faded is the whole point of faded: it does not compete for attention.
        if t.status in ("done", "faded"):
            continue
        bump, when = _due_pressure(t.due)
        why = _task_why(t)
        add(f"task:{t.id}",
            title=t.title,
            why=(f"{when}. {why}" if when else why),
            command=f"otto task mv {t.id[:6]} <status>",
            domain=t.domain, kind="task",
            score=_PRIORITY_SCORE.get(t.priority, 30)
                  + (8 if t.status == "queued" else 0) + bump
                  + _triage_bump(t))

    # 4. Gaps.
    for g in gaps(store):
        add(g["id"],
            title=g["title"], why=g["why"], command=g["command"],
            domain=g["domain"], kind=g["kind"], score=g["score"])

    # 5. Schedules simply due now.
    for s in store.schedules():
        due, reason = scheduled.is_due(s, now_local)
        if not due:
            continue
        add(f"sched:{s.name}",
            title=f"run {s.name}",
            why=reason,
            command=_run_command(s),
            domain=s.domain, kind="due", score=MED)

    rows = list(items.values())
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    rows.sort(key=lambda r: (-r["score"], r["title"].lower()))

    for r in rows:
        r["band"] = "today" if r["score"] >= 80 else ("this week" if r["score"] >= 40 else "sometime")
    return rows[:limit]


def _run_command(sched) -> str:
    """How to actually run a schedule's command.

    Slash commands need a Claude Code session; shell commands are just run. Getting
    this wrong is how a suggestion becomes a command that does not exist.
    """
    cmd = (sched.command or "").strip()
    if cmd.startswith("/"):
        return f'otto spawn {sched.name} --prompt "{cmd}" --cwd {config.HOME}'
    return cmd


def briefing(store: Store, domain: str | None = None) -> dict:
    """Everything the Overview needs in one shape."""
    nxt = next_up(store, domain)
    g = gaps(store)
    if domain:
        g = [x for x in g if x["domain"] == domain]
    return {
        "next": nxt,
        "gaps": g,
        "today": len([r for r in nxt if r["band"] == "today"]),
        "counts": {
            "next": len(nxt),
            "gaps": len(g),
        },
    }
