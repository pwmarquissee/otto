"""Logistics dispatch: which card could go to which idle session, and the hand-off.

THE SHAPE, FROM KEITORA. A rail of live sessions with their state, an Open column
of cards that may run, a strip of suggestions pairing one with the other, and an
Approve button that injects the card's prompt into that session's terminal. The
human stays in the loop: nothing moves without the click. What Otto adds is the
board it already has (tiers, plans, assessment, the promotion gate) and the
sessions it already sees; what herdr adds is the terminals the prompt lands in.

WHO MAY GO. Only cards the promotion gate would pass (`backlog.promotable`):
otto-owned, assessed, ready, with detail, tier-0 or tier-1 with an approved plan,
under the in-flight cap. The dispatcher never widens that gate. It is the same
set `otto triage promote` would spawn as headless runs; the difference is that
these run in a pane the owner can watch and type into, in a session that already has
the repo open.

WHERE THEY MAY GO. A herdr pane holding a Claude agent that herdr reads as idle
or done, that is not already carrying a running card. `blocked` and `working`
panes are never targets, and herdr itself refuses to prompt a blocked one.

HOW THEY ARE PAIRED. Deterministically, with a reason in words. Same repo as the
card's working directory or `repo:` tag is the strong signal; a card that names
another repo in its title is a weak one; priority and tier nudge. The score is a
confidence in [0, 1] shown on the suggestion, not a probability of anything.
Keitora asks a headless model to rank; Otto can add that later, but the first
version should be explicable in one line, because the owner reads the line and decides.

WHAT APPROVE DOES. Re-checks both gates (the card may have been promoted by hand,
the pane may have started working), builds the same prompt a headless run gets
plus a footer telling the session to move its own card, submits it through herdr,
and marks the card `running` with the pane and session on it. There is no Run.
The pane watcher on the tick is the backstop: when the pane settles back to idle
and the card is still `running`, it reads the pane's recent screen into the
card's result and moves it to `needs-you`, where one click says done.

DISMISS IS REMEMBERED. A pair the owner dismissed stays dismissed while that pane is
the same session. Re-proposing it every tick is how a suggestion strip becomes
something to ignore.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import backlog, config, dispatch, herdr
from .models import Proposal, Task, iso, safe_text, utcnow
from .store import Store

# Directory names on this machine that are repos a card might name. Resolved at
# call time from the directories under the configured roots, so a new checkout is
# picked up without a code change; this constant is only the fallback for a
# machine with no roots configured, and it is deliberately empty: a list of repo
# names in code is a list that belongs to one person's machine.
_KNOWN_REPO_WORDS: tuple[str, ...] = ()

# Listing the roots is cheap but the scorer runs for every card x pane pair on
# every tick, so the names are cached for a minute. A checkout made in the last
# sixty seconds is not what a pairing hinges on.
_REPO_WORDS_TTL = 60.0
_repo_words_cache: tuple[float, tuple[str, ...]] = (0.0, ())


def repo_words() -> tuple[str, ...]:
    """Lowercased repo directory names under WORK_ROOTS and PERSONAL_ROOTS.

    "otto" and the name of Otto's own checkout are always included, since cards
    about Otto name it and the repo may live outside every configured root (or
    in a directory with another name). Roots that do not exist or cannot be read
    contribute nothing rather than raising: a missing root is a configuration
    choice, not a scoring failure.
    """
    global _repo_words_cache
    stamp, words = _repo_words_cache
    if words and time.monotonic() - stamp < _REPO_WORDS_TTL:
        return words
    found: list[str] = ["otto", config.OTTO_REPO.name.lower()]
    for root in (*config.WORK_ROOTS, *config.PERSONAL_ROOTS):
        try:
            children = [c for c in root.iterdir() if c.is_dir()]
        except OSError:
            continue
        found.extend(c.name.lower() for c in children if not c.name.startswith("."))
    words = tuple(dict.fromkeys(found)) or _KNOWN_REPO_WORDS
    _repo_words_cache = (time.monotonic(), words)
    return words


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---- targets -----------------------------------------------------------------

def targets(store: Store, snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Idle Claude panes with no running card on them, as rows the strip can use."""
    agents = herdr.agents(snap) if snap is not None else herdr.agents(herdr.last_snapshot())
    busy_panes = {t.pane_id for t in store.tasks() if t.status == "running" and t.pane_id}
    sessions = {s.session_id: s for s in store.sessions()}
    out = []
    for a in agents:
        if a.get("agent") != "claude":
            continue
        if a.get("agent_status") not in ("idle", "done"):
            continue
        pane = str(a.get("pane_id"))
        if pane in busy_panes:
            continue
        sid = herdr.session_id_of(a)
        sess = sessions.get(sid) if sid else None
        out.append({
            "pane_id": pane,
            "agent": herdr.target_of(a),
            "session_id": sid,
            "cwd": a.get("foreground_cwd") or a.get("cwd"),
            "status": a.get("agent_status"),
            "title": sess.title if sess else None,
            "state_change_seq": a.get("state_change_seq"),
        })
    return out


def candidates(store: Store) -> list[Task]:
    """Cards the promotion gate would let run right now. The gate is not widened."""
    return backlog.promotable(store.tasks())


# ---- scoring -------------------------------------------------------------------

def _repo_of(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).name.lower()


def score(task: Task, target: dict[str, Any]) -> tuple[float, str]:
    """(confidence, rationale) for sending `task` to `target`. Deterministic."""
    reasons: list[str] = []
    conf = 0.4
    pane_repo = _repo_of(target.get("cwd"))
    pane_cwd = (target.get("cwd") or "").replace("\\", "/").rstrip("/").lower()
    text = f"{task.title} {' '.join(task.tags)}".lower()

    card_cwd = (task.cwd or "").replace("\\", "/").rstrip("/").lower()
    tag_repo = next((t.split(":", 1)[1].lower() for t in task.tags if t.startswith("repo:")), None)

    if card_cwd and pane_cwd and (card_cwd == pane_cwd or card_cwd.startswith(pane_cwd + "/")):
        conf += 0.4
        reasons.append(f"card runs in {pane_repo}, which this pane has open")
    elif tag_repo and pane_repo and tag_repo == pane_repo:
        conf += 0.35
        reasons.append(f"tagged repo:{tag_repo}, which this pane has open")
    elif pane_repo and pane_repo in text:
        conf += 0.25
        reasons.append(f"names {pane_repo}, which this pane has open")
    else:
        other = next((w for w in repo_words() if w != pane_repo and w in text), None)
        if other:
            conf -= 0.2
            reasons.append(f"names {other}; this pane is in {pane_repo or 'an unrelated directory'}")
        elif card_cwd or tag_repo:
            conf -= 0.1
            reasons.append(f"wants {_repo_of(card_cwd) or tag_repo}; this pane is in {pane_repo}")
        else:
            reasons.append(f"no repo preference; pane is idle in {pane_repo or 'its directory'}")

    if task.tier == backlog.TIER_AUTONOMOUS:
        conf += 0.1
        reasons.append("tier-0")
    elif task.plan_approved:
        conf += 0.05
        reasons.append("tier-1 with an approved plan")
    if task.priority in ("urgent", "high"):
        conf += 0.1
        reasons.append(f"{task.priority} priority")
    if target.get("status") == "done":
        reasons.append("just finished a turn")
    return max(0.0, min(1.0, round(conf, 2))), "; ".join(reasons)


# ---- proposals -------------------------------------------------------------------

def _pair_key(p: Proposal) -> tuple[str, str, str | None]:
    return (p.task_id, p.pane_id, p.session_id)


def refresh(store: Store, snap: dict[str, Any] | None = None) -> list[Proposal]:
    """Recompute the strip. Keeps the owner's dismissals, marks what no longer applies.

    Greedy: best pair first, then each card and each pane used at most once, up
    to LOGISTICS_MAX_PROPOSALS. Stored whole so the dashboard and the CLI read
    the same list and an approve by id is unambiguous.
    """
    if not herdr.available():
        return [p for p in store.proposals() if p.status in ("approved", "dismissed")]
    cards = candidates(store)
    rows = targets(store, snap)
    prior = store.proposals()
    dismissed = {_pair_key(p) for p in prior if p.status == "dismissed"}
    kept_ids = {p.id for p in prior if p.status in ("approved", "dismissed")}

    scored = []
    for t in cards:
        for r in rows:
            conf, why = score(t, r)
            if conf < config.LOGISTICS_MIN_CONFIDENCE:
                continue
            if (t.id, r["pane_id"], r.get("session_id")) in dismissed:
                continue
            scored.append((conf, t, r, why))
    scored.sort(key=lambda x: (-x[0], x[1].created or ""))

    chosen: list[Proposal] = []
    used_tasks: set[str] = set()
    used_panes: set[str] = set()
    live_pairs = {}
    for p in prior:
        if p.status == "proposed":
            live_pairs[_pair_key(p)] = p
    for conf, t, r, why in scored:
        if len(chosen) >= config.LOGISTICS_MAX_PROPOSALS:
            break
        if t.id in used_tasks or r["pane_id"] in used_panes:
            continue
        key = (t.id, r["pane_id"], r.get("session_id"))
        existing = live_pairs.get(key)
        if existing is not None:
            existing.confidence, existing.rationale = conf, why
            existing.agent, existing.cwd = r["agent"], r.get("cwd")
            chosen.append(existing)
        else:
            chosen.append(Proposal(task_id=t.id, pane_id=r["pane_id"], agent=r["agent"],
                                   session_id=r.get("session_id"), cwd=r.get("cwd"),
                                   confidence=conf, rationale=why))
        used_tasks.add(t.id)
        used_panes.add(r["pane_id"])

    chosen_ids = {p.id for p in chosen}
    out = list(chosen)
    for p in prior:
        if p.id in chosen_ids:
            continue
        if p.id in kept_ids:
            out.append(p)
        elif p.status == "proposed":
            p.status = "stale"
            p.decided_at = iso(utcnow())
            out.append(p)
    # Trim the memory of old decisions: a dismissal only matters while that session
    # is alive, and approvals are history the card already carries.
    live_sessions = {s.session_id for s in store.sessions() if s.live}
    out = [p for p in out if p.status == "proposed"
           or (p.status == "dismissed" and p.session_id in live_sessions)
           or (p.status in ("approved", "stale") and _age_hours(p.decided_at) < 48)]
    store.save_proposals(out)
    return [p for p in out if p.status == "proposed"]


def _age_hours(ts: str | None) -> float:
    dt = _parse(ts)
    if dt is None:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


# ---- the hand-off --------------------------------------------------------------

def hand_off(store: Store, task: Task, target: dict[str, Any]) -> str:
    """Submit the card's prompt to the pane and mark the card running there."""
    why = backlog.refusal(task, store.tasks())
    if why is not None:
        raise ValueError(f"{task.title[:40]}: {why}")
    live = herdr.agent_for_pane(target["pane_id"])
    if live is None:
        raise ValueError(f"pane {target['pane_id']} no longer holds an agent")
    if live.get("agent_status") not in ("idle", "done"):
        raise ValueError(f"pane {target['pane_id']} is {live.get('agent_status')}, not idle")
    herdr.prompt(herdr.target_of(live), dispatch.pane_prompt(task))
    with store.lock:
        fresh = store.get_task(task.id) or task
        fresh.status = "running"
        fresh.run_id = None
        fresh.session_id = herdr.session_id_of(live)
        fresh.pane_id = target["pane_id"]
        fresh.pane_seq = live.get("state_change_seq")
        fresh.cwd = live.get("foreground_cwd") or live.get("cwd") or fresh.cwd
        fresh.attempts += 1
        fresh.last_error = None
        fresh.touch()
        store.upsert_task(fresh)
    label = live.get("name") or target["pane_id"]
    return f"handed '{task.title[:40]}' to {label} ({Path(fresh.cwd or '').name})"


def approve(store: Store, proposal_id: str) -> tuple[Proposal, str]:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal matching {proposal_id}")
    if p.status != "proposed":
        raise ValueError(f"proposal is {p.status}, not open")
    task = store.get_task(p.task_id)
    if task is None:
        raise ValueError("the card is gone")
    msg = hand_off(store, task, {"pane_id": p.pane_id, "agent": p.agent})
    p.status, p.decided_at = "approved", iso(utcnow())
    _save_one(store, p)
    return p, msg


def dismiss(store: Store, proposal_id: str) -> Proposal:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal matching {proposal_id}")
    p.status, p.decided_at = "dismissed", iso(utcnow())
    _save_one(store, p)
    return p


def dispatch_to(store: Store, task_id: str, target: str) -> str:
    """Manual hand-off, bypassing the strip but not the gate. `target` is an agent
    name or a pane id."""
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(f"no task matching {task_id}")
    live = next((a for a in herdr.agents(herdr.snapshot())
                 if a.get("name") == target or a.get("pane_id") == target), None)
    if live is None:
        raise KeyError(f"no herdr agent matching {target}")
    return hand_off(store, task, {"pane_id": live["pane_id"], "agent": herdr.target_of(live)})


def _save_one(store: Store, p: Proposal) -> None:
    with store.lock:
        items = store.proposals()
        for i, q in enumerate(items):
            if q.id == p.id:
                items[i] = p
                break
        else:
            items.append(p)
        store.save_proposals(items)


# ---- the pane watcher ---------------------------------------------------------------

def watch(store: Store, snap: dict[str, Any] | None = None) -> list[str]:
    """Settle cards that were handed to panes. Tick hook.

    A card the session closed itself (per the prompt's footer) is already not
    `running` and is skipped. For the rest: a pane that is gone parks the card in
    needs-you with the reason; a pane that has settled back to idle after the
    hand-off, past the settle window, has its recent screen read into the card's
    result and the card parked in needs-you, one click from done. `working` and
    `blocked` are left alone, and `blocked` is what the session rail already
    shouts about.
    """
    notes: list[str] = []
    running = [t for t in store.tasks() if t.status == "running" and t.pane_id and not t.run_id]
    if not running:
        return notes
    if snap is None:
        snap = herdr.last_snapshot()
    if snap is None:
        return notes  # server down: say nothing rather than invent an outcome
    now = datetime.now(timezone.utc)
    for t in running:
        a = herdr.agent_for_pane(t.pane_id, snap)
        with store.lock:
            fresh = store.get_task(t.id)
            if fresh is None or fresh.status != "running":
                continue
            if a is None:
                fresh.status = "needs-you"
                fresh.last_error = f"pane {t.pane_id} closed before the card was finished"
                fresh.touch()
                store.upsert_task(fresh)
                notes.append(f"{fresh.title[:36]} -> needs-you (pane gone)")
                continue
            status = a.get("agent_status")
            seq = a.get("state_change_seq") or 0
            since = _parse(fresh.updated)
            settled = since is not None and (now - since).total_seconds() >= config.LOGISTICS_SETTLE_SECONDS
            if status in ("idle", "done") and settled and seq > (fresh.pane_seq or -1):
                try:
                    tail = herdr.read(herdr.target_of(a), lines=80)
                except herdr.HerdrError as e:
                    tail = f"(could not read the pane: {e})"
                fresh.status = "needs-you"
                fresh.result = safe_text(tail.strip()[-1500:]) or None
                fresh.touch()
                store.upsert_task(fresh)
                notes.append(f"{fresh.title[:36]} -> needs-you (pane settled, result captured)")
    return notes


# ---- the view -----------------------------------------------------------------------

def view(store: Store) -> dict[str, Any]:
    """Everything the dispatch surface shows, in one read."""
    snap = herdr.last_snapshot() if herdr.available() else None
    sessions = {s.session_id: s for s in store.sessions()}
    tasks = store.tasks()
    rail = []
    for a in herdr.agents(snap):
        sid = herdr.session_id_of(a)
        s = sessions.get(sid) if sid else None
        card = next((t for t in tasks if t.status == "running" and t.pane_id == a.get("pane_id")), None)
        rail.append({
            "pane_id": a.get("pane_id"), "agent": herdr.target_of(a), "kind": a.get("agent"),
            "status": a.get("agent_status"), "cwd": a.get("foreground_cwd") or a.get("cwd"),
            "session_id": sid, "title": (s.title if s else None),
            "state_since": (s.state_since if s else None),
            "last_message": (s.last_message if s else None),
            "note": (s.note if s else None),
            "task_id": card.id if card else None, "task_title": card.title if card else None,
        })
    order = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 4}
    rail.sort(key=lambda r: (order.get(r["status"] or "", 9), r["pane_id"] or ""))
    by_id = {t.id: t for t in tasks}
    proposals = []
    for p in store.proposals():
        if p.status != "proposed":
            continue
        t = by_id.get(p.task_id)
        if t is None:
            continue
        proposals.append({**p.model_dump(), "task_title": t.title, "task_priority": t.priority,
                          "task_tier": t.tier})
    return {
        "herdr": {"installed": herdr.available(), "running": snap is not None,
                  "binary": herdr.binary()},
        "rail": rail,
        "proposals": proposals,
        "open": [t.model_dump() for t in candidates(store)],
        "running": [t.model_dump() for t in tasks if t.status == "running" and t.pane_id],
        "at": iso(utcnow()),
    }


def tick(store: Store) -> list[str]:
    """Sync herdr, refresh the strip, settle panes. One snapshot for all three."""
    if not herdr.available():
        return []
    notes = herdr.sync(store)
    snap = herdr.last_snapshot(max_age=5)
    if snap is None:
        return notes
    try:
        refresh(store, snap)
    except Exception as e:  # noqa: BLE001 - the strip is advisory; never stall the tick
        notes.append(f"logistics refresh error: {e}")
    notes.extend(watch(store, snap))
    return notes
