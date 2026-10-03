"""The Otto daemon: HTTP API, dashboard host, and the tick loop.

This process is the single writer. It owns state and exposes every mutation as
an HTTP endpoint so the CLI, spawned agents, hooks, and cron never touch JSON
on disk directly.

The tick loop runs on a plain background thread rather than an asyncio task
because probes and process inspection are blocking calls, and a slow OAuth round
trip to an external API must not stall the dashboard.
"""

from __future__ import annotations

import contextlib
import os
import gzip
import hashlib
import json
import threading
import time
import uuid
import datetime as _dt
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.middleware.gzip import GZipMiddleware

from . import (activity, advisor, board, chat, config, configsync, decisions, dedupe,
               dispatch, feeds, findings, herdr, journal, known, launch, ledger, logistics,
               meetings, safeargs,
               notify, nudges, outreach, people, persona, prep, refresh, registry,
               repos, retire, inbox, sessions, slack, spend, summon, telemetry,
               triage, verdict, wellbeing, writing)
# Aliased, matching advisor.py: several functions in here bind a local `today` to a
# date string, and a module of the same name shadowed by a local is a trap.
from . import settings, setup
from . import today as _today
from .models import Alert, Cadence, Run, Schedule, Snapshot, Task, iso, utcnow
from .originguard import OriginGuard, default_origins
from .runners import detached, external, machine, scheduled
from .runners import herdrpane
from .store import Store

WEB_DIR = Path(__file__).parent / "web"

store = Store()

# Cadences for the tick loop's own periodic work, in seconds.
POLL_RUNS_EVERY = config.TICK_SECONDS
# Summons are polled far slower than the tick: see config.SUMMON_EVERY_SECONDS.
_last_summon = 0.0
_last_dm = 0.0
SCAN_REGISTRY_EVERY = 600
PROBE_EVERY = 300

_state_lock = threading.Lock()
_last_scan = 0.0
_last_probe = 0.0
_stop = threading.Event()
_scan_notes: list[str] = []
# schedule name -> last logged block reason, so a persistent block is
# reported once instead of every tick.
_blocked_reason: dict[str, str] = {}


# ---- pidfile ----------------------------------------------------------------

# Set by serve(--force), read by lifespan: the only sanctioned way to take the
# writer role away from a daemon that still holds the pidfile.
_FORCED_START = False


def _running_daemon_pid() -> int | None:
    if not config.PID_FILE.exists():
        return None
    try:
        pid = int(config.PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    try:
        p = psutil.Process(pid)
        if p.is_running() and "python" in (p.name() or "").lower():
            return pid
    except psutil.Error:
        return None
    return None


def _write_pidfile() -> None:
    config.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PID_FILE.write_text(str(psutil.Process().pid), encoding="utf-8")


def _clear_pidfile() -> None:
    with contextlib.suppress(OSError):
        config.PID_FILE.unlink(missing_ok=True)


# ---- tick -------------------------------------------------------------------

def poll_runs() -> list[str]:
    notes: list[str] = []
    finished_chats: list[Run] = []
    finished_refresh: list[Run] = []
    finished_ingest: list[Run] = []
    finished_tasks: list[Run] = []
    finished_schedules: list[Run] = []
    finished_writing: list[Run] = []
    with store.lock:
        runs = store.runs()
        changed = False
        for i, run in enumerate(runs):
            if not run.active:
                continue
            before = run.status
            if run.runner == "detached":
                runs[i] = detached.poll(run)
            if runs[i].status != before:
                changed = True
                notes.append(f"{run.name} {before} -> {runs[i].status}")
                if "mode=chat" in (runs[i].notes or ""):
                    finished_chats.append(runs[i])
                elif "mode=refresh" in (runs[i].notes or ""):
                    finished_refresh.append(runs[i])
                elif "mode=ingest" in (runs[i].notes or ""):
                    finished_ingest.append(runs[i])
                elif "mode=writing" in (runs[i].notes or ""):
                    finished_writing.append(runs[i])
                elif "mode=task" in (runs[i].notes or ""):
                    finished_tasks.append(runs[i])
                elif "mode=schedule" in (runs[i].notes or ""):
                    finished_schedules.append(runs[i])
        if changed:
            store.save_runs(runs)

    # Harvest outside the store lock: chat.harvest takes it again itself.
    for run in finished_chats:
        try:
            if chat.harvest(store, run):
                notes.append("chat reply ready")
        except Exception as e:  # noqa: BLE001 - a bad turn must not stall the loop
            notes.append(f"chat harvest failed: {e}")

    for run in finished_refresh:
        try:
            notes.extend(refresh.harvest(store, run))
            # Whichever refresh schedule owns this domain. Hardcoding "inbox-sync"
            # stamped the personal ledger for a work fetch.
            owner = next((sc.name for sc in store.schedules()
                          if sc.runner == "refresh" and sc.domain == run.domain), None)
            if owner:
                store.stamp(owner, "ok" if run.status == "ok" else "failed", run.id)
        except Exception as e:  # noqa: BLE001
            notes.append(f"refresh harvest failed: {e}")

    for run in finished_ingest:
        try:
            notes.extend(meetings.harvest(store, run))
            owner = next((sc.name for sc in store.schedules()
                          if sc.runner == "ingest"), None)
            if owner:
                # Only a run that actually returned usable JSON counts as a success.
                # Stamping a failed fetch would make an ingester that has been dead
                # for a week read as perfectly fresh, which is the blindness the
                # staleness clock exists to remove.
                if run.status == "ok":
                    store.stamp(owner, "ok", run.id)
                else:
                    store.mark_attempt(owner, run.status, run.id)
        except Exception as e:  # noqa: BLE001
            notes.append(f"meeting ingest harvest failed: {e}")

    for run in finished_writing:
        try:
            notes.extend(writing.harvest(store, run))
            if "kind=ideas" in (run.notes or ""):
                owner = next((sc.name for sc in store.schedules()
                              if sc.runner == "writing"), None)
                if owner:
                    # Same rule as ingest: only a run that returned usable JSON
                    # counts, or a miner dead for a month reads as fresh.
                    if run.status == "ok":
                        store.stamp(owner, "ok", run.id)
                    else:
                        store.mark_attempt(owner, run.status, run.id)
        except Exception as e:  # noqa: BLE001
            notes.append(f"writing harvest failed: {e}")

    for run in finished_tasks:
        try:
            msg = dispatch.settle(store, run)
            if msg:
                notes.append(msg)
        except Exception as e:  # noqa: BLE001
            notes.append(f"task settle failed: {e}")

    for run in finished_schedules:
        try:
            msg = launch.settle(store, run)
            if msg:
                notes.append(msg)
        except Exception as e:  # noqa: BLE001
            notes.append(f"schedule settle failed: {e}")
    return notes


def refresh_registry() -> list[str]:
    current = registry.discover()
    with store.lock:
        merged, notes = registry.reconcile(store.registry(), current)
        store.save_registry(merged)
    return notes


def autostart_due() -> list[str]:
    """Run due schedules that are armed for it. This is the cron.

    Two runners are auto-launchable, and they are treated differently on purpose:

      refresh  the built-in read-only email/calendar fetch. Cheap, bounded, no
               mutation, so it needs little more than a duplicate check.
      ingest   the built-in read-only Notion meeting-notes parse. Same risk class
               as refresh: an allow-listed set of read tools, and the only thing it
               writes is Otto's own board and ledger.
      writing  the built-in post-ideas miner. Material is injected, every tool
               is denied and the MCP config is empty, so it can touch nothing.
      launch   the schedule's real command, unattended. For a slash command that
               is a Claude Code session holding skip-permissions with nobody
               watching, so every guard in launch.autorun_blocked() applies.

    A schedule that is merely `enabled` is never auto-run. Arming is explicit.
    """
    notes: list[str] = []
    now_local = datetime.now().astimezone()

    # Per domain, not global: a work refresh in flight must not suppress the
    # personal one, they are different servers and different inboxes.
    refresh_running = {
        r.domain for r in store.runs()
        if r.status == "running" and "mode=refresh" in (r.notes or "")
    }
    # Not per-domain: there is one Notion workspace, so one ingester at a time. Two
    # in flight would both read the same pages before either wrote the ledger.
    ingest_running = any(r.status == "running" and "mode=ingest" in (r.notes or "")
                         for r in store.runs())

    for sched in store.schedules():
        if not (sched.enabled and sched.autostart):
            continue
        due, _ = scheduled.is_due(sched, now_local)
        if not due:
            continue

        if sched.runner == "refresh":
            if sched.domain in refresh_running:
                continue
            try:
                run = refresh.start(store, sched.domain)
                store.upsert_run(run)
                refresh_running.add(sched.domain)
                notes.append(f"autostarted {sched.name} (pid {run.pid})")
            except Exception as e:  # noqa: BLE001
                notes.append(f"could not autostart {sched.name}: {e}")

        elif sched.runner == "ingest":
            if ingest_running:
                continue
            try:
                run = meetings.start(store)
                store.upsert_run(run)
                ingest_running = True
                notes.append(f"autostarted {sched.name} (pid {run.pid})")
            except Exception as e:  # noqa: BLE001
                notes.append(f"could not autostart {sched.name}: {e}")

        elif sched.runner == "writing":
            # Reads Otto's own state into a prompt, no tools, no MCP, budget-capped.
            # The cheapest risk class Otto has, so it autostarts like refresh does.
            try:
                run = writing.start_ideas(store)
                store.upsert_run(run)
                notes.append(f"autostarted {sched.name} (pid {run.pid})")
            except ValueError:
                continue  # one already in flight
            except Exception as e:  # noqa: BLE001
                notes.append(f"could not autostart {sched.name}: {e}")

        elif sched.runner == "launch":
            blocked = launch.autorun_blocked(store, sched)
            if blocked:
                # autorun_blocked is only consulted for schedules that are ALREADY
                # due, so every block means armed work is not happening. Silence
                # here is the exact failure Otto exists to remove: a schedule can
                # sit blocked all day with no trace. Log on state CHANGE, so it is
                # one event per transition rather than one per 15s tick.
                if _blocked_reason.get(sched.name) != blocked:
                    _blocked_reason[sched.name] = blocked
                    notes.append(f"{sched.name} is due but blocked: {blocked}")
                continue
            _blocked_reason.pop(sched.name, None)
            try:
                run, msg = launch.launch(store, sched, autorun=True)
                notes.append(msg)
            except ValueError as e:
                notes.append(f"could not autorun {sched.name}: {e}")
            except Exception as e:  # noqa: BLE001
                notes.append(f"autorun error on {sched.name}: {e}")
    return notes


def refresh_integrations() -> None:
    """Re-probe, and say so when a watched number moves.

    The probe already runs every 5 minutes, so the count is fresh either way. What
    was missing was noticing: a rollout starting, or sensors disappearing, would
    otherwise just be a different string in a panel nobody was staring at.
    """
    results = external.probe_all()
    with store.lock:
        before = {i.name: i.metric for i in store.integrations() if i.metric is not None}
        store.save_integrations(results)

    for i in results:
        if i.metric is None:
            continue
        was = before.get(i.name)
        if was is None or was == i.metric:
            continue
        delta = i.metric - was
        unit = i.metric_label or "units"
        # A drop matters more than a rise: coverage going backwards is a problem.
        store.log(
            f"{i.name} {unit}: {was:g} -> {i.metric:g} ({delta:+g})",
            level="warn" if delta < 0 else "info",
            source="probe",
        )


def seed_schedules() -> None:
    """Add any missing default schedule without clobbering user edits.

    Always writes back, even with nothing to add. Fields introduced after a state
    file was created (such as `created`, which staleness uses as its baseline for
    a never-run schedule) default at load time, so persisting once at startup
    pins them instead of letting them drift on every restart.
    """
    with store.lock:
        items = store.schedules()
        existing = {s.name for s in items}
        for sched in scheduled.default_schedules():
            if sched.name not in existing:
                items.append(sched)
        store.save_schedules(items)


def compute_alerts() -> list[Alert]:
    alerts: list[Alert] = []
    now_utc = datetime.now(timezone.utc)

    # Things the owner has said are broken and knows about. Their alerts still exist
    # (the condition is real) but drop to info and carry the reason, so the morning
    # read distinguishes "waiting on the EDR key" from "something new is wrong". An
    # annotation that has already gone stale is not consulted: the thing is healthy,
    # so the alert it would have demoted does not exist, and the notice known.sweep
    # posted is the thing asking the owner to clean up.
    kn = {k: v for k, v in known.lookup(store).items() if not v.get("stale_noticed_at")}

    for sched in store.schedules():
        st = scheduled.staleness(sched, now_utc)
        if st:
            level, detail = st
            k = kn.get(known.key("schedule", sched.name))
            if k:
                level, detail = "info", f"known: {k['reason']} · {detail}"
            alerts.append(Alert(level=level, domain=sched.domain,
                                source=f"schedule/{sched.name}", message=detail))

    for run in store.runs()[:60]:
        # Two lanes, not one word. "heartbeat failed (exit 1)" reads as "your
        # automation is broken" and got actioned into a dispatched agent three
        # times in one hour on 2026-08-05; every one of them reported back that the
        # API had been overloaded. The message now says which side broke, so the
        # row answers its own question. See verdict.py.
        if run.status == "orphaned":
            v = verdict.verdict(run)
            alerts.append(Alert(level="warn", domain=run.domain,
                                source=f"run/{persona.short(run.id)}",
                                message=f"{run.name}: {v['line']}"))
        elif run.status == "failed":
            if run.reviewed_at:
                continue
            v = verdict.verdict(run)
            if run.transient:
                alerts.append(Alert(
                    level="info", domain=run.domain,
                    source=f"run/{persona.short(run.id)}",
                    message=f"{run.name}: {v['line']} "
                            f"(retried automatically, next attempt on cadence)"))
            else:
                alerts.append(Alert(level="warn", domain=run.domain,
                                    source=f"run/{persona.short(run.id)}",
                                    message=f"{run.name}: {v['line']}"))

    setup_done = setup.is_complete(store)
    for i in store.integrations():
        if i.ok:
            continue
        if i.mode == "unconfigured" and not setup_done:
            continue  # the Setup view carries these until setup is finished (board.py says why)
        level = "warn" if i.mode in ("unconfigured", "mcp-only") else "crit"
        detail = i.detail
        k = kn.get(known.key("integration", i.name))
        if k:
            level, detail = "info", f"known: {k['reason']} · {detail}"
        alerts.append(Alert(level=level, source=f"integration/{i.name}", message=detail))

    # Sessions blocked on the owner, and turns that have run long enough to be either
    # deep work or dead. A bad session row must not take the whole alert list with
    # it: every other alert here is load-bearing.
    try:
        alerts.extend(sessions.alerts(store))
    except Exception as e:  # noqa: BLE001
        store.log(f"session alerts failed: {e}", level="warn", source="sessions")

    # A snapshot Otto cannot refresh itself goes stale silently otherwise.
    for kind, snap in store.snapshots().items():
        # Feed-backed snapshots are excluded here and alarmed by feeds.gaps()
        # instead: this loop applies one global SNAPSHOT_STALE_HOURS, whereas a
        # feed declares its OWN max_age_hours (a daily producer is not stale at
        # 13h).
        if snap.kind.startswith("feed:"):
            continue
        try:
            fetched = datetime.fromisoformat(snap.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        age_h = (now_utc - fetched).total_seconds() / 3600
        if age_h > config.SNAPSHOT_STALE_HOURS:
            alerts.append(Alert(level="info", domain=snap.domain,
                                source=f"snapshot/{kind}",
                                message=f"{age_h / 24:.1f}d old - refresh with /otto"))

    missing = [e for e in store.registry() if e.missing]
    if missing:
        alerts.append(Alert(level="info", source="registry",
                            message=f"{len(missing)} definition(s) no longer on disk"))

    rank = {"crit": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: rank.get(a.level, 3))
    return alerts


def daily_journal() -> list[str]:
    """Roll up yesterday once per day. Silent: it no longer asks the owner anything.

    Deliberately NOT a spawned session. The rollup is mechanical extraction from
    transcripts already on disk: no MCP, no model, ~0.3s. Launching Claude Code to
    do it would cost tokens to learn nothing new.

    Idempotent by construction: it checks whether the record already carries a
    rollup, so a restart, a second daemon, or a mid-day tick cannot double-post.
    """
    notes: list[str] = []
    now_local = datetime.now().astimezone()

    # The rollup runs EVERY day including weekends. It costs nothing, needs no
    # session, and the coach is worthless with holes in its history: a gap every
    # Sat/Sun would make "what do you keep doing on weekends" unanswerable. It is
    # the interruption that pauses, not the observation.
    y = journal.yesterday()
    ykey = y.isoformat()
    roll = store.get_day(ykey).get("rollup")
    if not roll:
        roll = journal.rollup(y)
        store.put_day(ykey, {"rollup": roll})
        wall = roll.get("wall_minutes") or 0
        notes.append(f"rolled up {ykey}: {roll.get('session_count')} sessions, "
                     f"{wall // 60}h{wall % 60:02d}m engaged")

    # The morning check-in prompt used to be posted here ("How did you sleep, and what
    # actually matters today?"). Removed 2026-09-03 at the owner's request. The note it
    # asked for was write-only: stored, shown in the day tile, and read by nothing
    # that decides work (not chat, not `otto next`, not triage). He answered it ten
    # times in the first two weeks of August and never after, while the notice kept
    # firing every weekday. A form he has to fill in with no return is a nag, not a
    # feature. `otto checkin` and the web box remain, unprompted, for days he has
    # something to say. If Otto ever needs "context for the day" it should come from
    # what he already produces (calendar, first prompts, Slack), not from a question.
    sched = store.get_schedule("daily-rollup")
    if sched is None or not sched.enabled:
        return notes
    due, _ = scheduled.is_due(sched, now_local)
    if not due:
        return notes
    store.stamp("daily-rollup", "ok")
    return notes


def _clear_block_preps() -> list[str]:
    """Remove prep briefs posted for a time block.

    Two cases, both real. The filter shipped after the briefs did, so a "Team Work
    Time with Hermann, Jean-Eric" brief carrying fifteen lines of dossier was already
    sitting at the top of Today. And `config.CALENDAR_BLOCKS` can grow later, at which
    point yesterday's decision applies retroactively or it does not really hold.

    Deleted rather than marked read, because the dashboard renders the last twelve
    notices whatever their read state: marking it read leaves the same wall of text on
    the screen, one shade dimmer. Nothing is lost either. A prep brief is a joined view
    of state Otto still holds, and `otto prep` rebuilds it on demand.

    Scoped to `source == "prep"`. This must never touch a run failure or an outage
    notice that happens to have the word "standup" in its title.
    """
    notes: list[str] = []
    for n in store.notices():
        if n.source != "prep" or not _today.is_block(n.title):
            continue
        if store.delete_notice(n.id):
            notes.append(f"cleared block prep notice: {n.title[:48]}")
    return notes


def meeting_prep() -> list[str]:
    """Post the brief for a meeting starting within PREP_LEAD_MINUTES.

    This is the only proactive half of prep, and the constraints on it are all about
    not becoming an interruption the owner mutes.

    ONCE PER MEETING. Deduped on the event title within the day, so a 15-second tick
    does not deliver sixty identical toasts across the lead window. The dedup key is
    the title rather than a hash of the whole event, because a meeting whose end time
    got edited is still the same meeting.

    ONLY IF THERE IS SOMETHING TO SAY. A brief with no matched attendee is a toast
    that says there is a meeting, which the calendar already did. It is skipped,
    not sent empty.

    NEVER OFF A STALE CALENDAR. `advisor` suppresses the same way: a confident "in 10
    minutes" read off a day-old snapshot is worse than silence, because it is wrong
    in the direction of being acted on.

    RELATIONSHIP NOTES ARE WITHHELD. `sensitive=False` drops the background section
    from the toast. A toast can render on a shared screen, and the dossiers carry
    private material about colleagues; the full brief stays behind `otto prep`, which
    the owner runs deliberately.

    NOT FOR TIME BLOCKS. `prep.next_meeting` skips them. This function also clears any
    block brief already posted, see `_clear_block_preps`.
    """
    notes: list[str] = []
    notes += _clear_block_preps()
    if not config.PREP_NOTIFY:
        return notes
    snaps = {k: v.model_dump() for k, v in store.snapshots().items()}
    agenda = snaps.get(f"{config.WORK}/agenda")
    if not agenda:
        return notes
    age_h = journal_age_hours(agenda.get("fetched_at"))
    if age_h is None or age_h > config.SNAPSHOT_STALE_HOURS:
        return notes

    p = prep.next_meeting(store, within_minutes=config.PREP_LEAD_MINUTES)
    if p is None or p.event is None or not p.matched:
        return notes
    if not prep.actionable(store, p):
        # Matched people, but nothing open with any of them. A toast here would only
        # restate the calendar entry, and the toast that restates the calendar is the
        # one that teaches the owner to dismiss the rest unread.
        return notes

    today_key = datetime.now().astimezone().date().isoformat()
    key = p.event.title[:40]
    if any(n.source == "prep" and (n.at or "")[:10] == today_key and key in n.title
           for n in store.notices()):
        return notes

    body = "\n".join(prep.brief_lines(store, p, sensitive=False))
    who = ", ".join(str(m.person.get("display_name")) for m in p.matched)
    notify.post(store,
                f"{p.event.when} {p.event.title[:40]} with {who[:60]}",
                body=body, level="info", domain=config.WORK, source="prep",
                command="otto prep", notify=True)
    notes.append(f"posted meeting prep for '{key}' ({p.minutes}m out, "
                 f"{len(p.matched)} matched)")
    return notes


def journal_age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (utcnow() - dt.astimezone(timezone.utc)).total_seconds() / 3600


def tick() -> None:
    global _last_scan, _last_probe
    notes = poll_runs()
    for n in notes:
        store.log(n, source="runner")
    for n in herdrpane.sweep(store):  # panes of long-finished runs; swallows its own errors
        store.log(n, source="herdr")

    for n in autostart_due():
        store.log(n, source="schedule")

    try:
        for n in dispatch.dispatch_due(store):
            store.log(n, source="dispatch")
    except Exception as e:  # noqa: BLE001
        store.log(f"dispatch error: {e}", level="warn", source="dispatch")

    # Sessions that stopped reporting. Cheap (a small file, no process work) and
    # placed early so the rest of the tick, and any alert computed after it, sees
    # a session list that is not claiming a dead terminal is still busy.
    try:
        for n in sessions.sweep(store):
            store.log(n, source="sessions")
    except Exception as e:  # noqa: BLE001
        store.log(f"session sweep error: {e}", level="warn", source="sessions")

    # The harness and the dispatcher: one `herdr api snapshot` (about 60ms) folds
    # herdr's pane view into the session rows, refreshes the suggestion strip, and
    # settles cards that were handed to panes. Nothing here moves a card out of
    # backlog on its own; approve is a click or a verb.
    try:
        for n in logistics.tick(store):
            store.log(n, source="logistics")
    except Exception as e:  # noqa: BLE001
        store.log(f"logistics error: {e}", level="warn", source="logistics")

    # Known failures that have healed. A few string compares per annotated thing,
    # and the only detector in Otto whose output is "delete something".
    try:
        for n in known.sweep(store):
            store.log(n, source="known")
    except Exception as e:  # noqa: BLE001
        store.log(f"known sweep error: {e}", level="warn", source="known")

    # Duplicate cards folded into one. Gated on a digest of the open board, so a
    # tick where nothing filed and nothing moved costs one hash. Placed after
    # dispatch so a card that just went to `queued` is protected by the time the
    # sweep looks, and before feed ingest so the feeds' own filing lands on an
    # already-folded board.
    try:
        for n in dedupe.tick(store):
            store.log(n, source="dedupe")
    except Exception as e:  # noqa: BLE001
        store.log(f"dedupe sweep error: {e}", level="warn", source="dedupe")

    try:
        for n in daily_journal():
            store.log(n, source="journal")
    except Exception as e:  # noqa: BLE001
        store.log(f"journal error: {e}", level="warn", source="journal")

    try:
        for n in meeting_prep():
            store.log(n, source="prep")
    except Exception as e:  # noqa: BLE001 - a bad snapshot must not stall the loop
        store.log(f"prep error: {e}", level="warn", source="prep")

    # The unprompted nudges: threads gone quiet, a milestone approaching, cards past
    # their date. Each dedupes against the notice store, so this is a few list
    # comprehensions on a normal tick.
    try:
        for n in nudges.tick(store):
            store.log(n, source="nudges")
    except Exception as e:  # noqa: BLE001
        store.log(f"nudges error: {e}", level="warn", source="nudges")

    # Outreach holds expiring. Placed AFTER nudges and before notify so that a message
    # Otto decides to send is recorded and toasted in the same tick it is composed,
    # and BEFORE deliver_pending so the "held, sends in 10 min" toast is not a tick
    # behind the hold it describes.
    try:
        for n in outreach.settle(store):
            store.log(n, source="outreach")
    except Exception as e:  # noqa: BLE001
        store.log(f"outreach error: {e}", level="warn", source="outreach")

    # Feed ingestion. Before notify.deliver_pending, so a notice a producer just
    # dropped gets its toast this tick instead of waiting for the next one.
    #
    # This is the ONLY consumer of the feed directory, and it runs here because the
    # daemon is the only writer of state. Producers write files; this turns them
    # into state. Unchanged drops are skipped by digest, so the common case is a
    # few stat() calls and costs nothing at the tick rate.
    try:
        for n in feeds.ingest(store):
            store.log(n, source="feed")
    except Exception as e:  # noqa: BLE001
        store.log(f"feed ingest error: {e}", level="warn", source="feed")

    # Dossier last_contact from the DM producer's direction facts. Deterministic and
    # forward-only, and gated on the spool file's mtime, so a normal tick costs one
    # stat(). This is the writer that keeps "last spoke Nd ago" true: before it
    # existed the field was written once at seeding and never advanced again.
    try:
        for n in people.ingest_spool_contacts():
            store.log(n, source="contacts")
    except Exception as e:  # noqa: BLE001
        store.log(f"contact ingest error: {e}", level="warn", source="contacts")

    # Somebody reacting :otto-help: in the support channel is a request for an answer, and it
    # is answered within the minute rather than at the next four-hourly sweep. This
    # is also where the summon gate stops being prose and starts being code: the
    # session is handed one thread, so it never decides whether it was summoned.
    global _last_summon
    if time.time() - _last_summon > config.SUMMON_EVERY_SECONDS:
        _last_summon = time.time()
        try:
            for n in summon.poll(store):
                store.log(n, source="summon")
        except Exception as e:  # noqa: BLE001
            store.log(f"summon poll error: {e}", level="warn", source="summon")

    # The owner DMing Otto from a phone. Checked more often than the summon poll because
    # somebody is waiting on the other end of it.
    global _last_dm
    if time.time() - _last_dm > config.DM_EVERY_SECONDS:
        _last_dm = time.time()
        try:
            for n in inbox.poll(store):
                store.log(n, source="dm")
        except Exception as e:  # noqa: BLE001
            store.log(f"DM poll error: {e}", level="warn", source="dm")

    try:
        for n in notify.deliver_pending(store):
            store.log(n, source="notify")
    except Exception as e:  # noqa: BLE001
        store.log(f"notify error: {e}", level="warn", source="notify")

    now = time.time()
    if now - _last_scan > SCAN_REGISTRY_EVERY:
        _last_scan = now
        try:
            changes = refresh_registry()
            _scan_notes[:] = changes
            for c in changes[:20]:
                store.log(c, source="registry")
        except Exception as e:  # noqa: BLE001
            store.log(f"registry scan failed: {e}", level="warn", source="registry")

    if now - _last_probe > PROBE_EVERY:
        _last_probe = now
        try:
            refresh_integrations()
        except Exception as e:  # noqa: BLE001
            store.log(f"probe sweep failed: {e}", level="warn", source="probe")

    with contextlib.suppress(Exception):
        store.trim_events()


def _tick_forever() -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:  # noqa: BLE001 - the loop must never die
            with contextlib.suppress(Exception):
                store.log(f"tick error: {e}", level="crit", source="daemon")
        _stop.wait(POLL_RUNS_EVERY)


# ---- app --------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.mark_daemon(force=_FORCED_START)
    config.ensure_dirs()
    _write_pidfile()
    seed_schedules()
    with store.lock:
        setup.migrate(store)
    store.log(f"{config.PERSONA_NAME} daemon up on {config.BASE_URL}", source="daemon")
    if config.HERDR_AUTOSTART:
        # The harness before the first tick looks for panes. Nothing here may stop
        # the daemon: a missing or broken herdr is a warn line and a probe row.
        try:
            if herdr.available():
                _started, msg = herdr.ensure_server()
                store.log(f"herdr autostart: {msg}", source="herdr")
            else:
                store.log("herdr autostart: herdr is not installed (herdr.dev)",
                          level="warn", source="herdr")
        except Exception as e:  # noqa: BLE001 - startup must not die on the harness
            store.log(f"herdr autostart failed: {e}", level="warn", source="herdr")
    thread = threading.Thread(target=_tick_forever, name="otto-tick", daemon=True)
    thread.start()
    # The herdr watcher: a 2s snapshot poll on its own thread so the session rail
    # reflects a pane going blocked within one dashboard poll, not one tick.
    if config.HERDR_SYNC:
        threading.Thread(target=herdr.watch, args=(store, _stop), name="otto-herdr-watch",
                         daemon=True).start()
    try:
        yield
    finally:
        _stop.set()
        with contextlib.suppress(Exception):
            # Spawned agents are CHILD processes of this daemon. A normal exit
            # leaves them running, but `taskkill /T` walks the tree and kills them
            # with it. Naming them on the way out makes that visible instead of
            # silent, and `otto stop` exists so nobody reaches for /T.
            alive = [r for r in store.runs() if r.status == "running"]
            if alive:
                names = ", ".join(f"{r.name}({r.id[:6]})" for r in alive[:5])
                store.log(
                    f"shutting down with {len(alive)} agent(s) still running: {names}. "
                    "They survive a clean stop; they do NOT survive taskkill /T.",
                    level="warn", source="daemon")
            else:
                store.log("daemon shutting down", source="daemon")
        _clear_pidfile()


app = FastAPI(title=f"{config.PERSONA_NAME} - {config.PERSONA_BLURB}", lifespan=lifespan)
# Compress JSON bodies over 1 KB for clients that accept gzip. Added before the
# guard on purpose: add_middleware wraps outward, so this stays INSIDE OriginGuard
# and a refused request is still answered by the guard alone. Level 6 is within
# half a percent of level 9 on this payload at two thirds the CPU. /api/state
# bypasses this by compressing once per cache fill and sending Content-Encoding
# itself; the middleware passes an already-encoded response through untouched.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
# First thing on the stack, so a refused browser request never reaches a route or
# a WebSocket handler. See otto/originguard.py for what it stops and why requests
# without an Origin (hook, CLI) still pass. Starlette builds the stack on the
# first request, so serve() can still add a non-default port's origins below.
app.add_middleware(OriginGuard, allowed_origins=config.ALLOWED_ORIGINS,
                   allowed_hosts=config.ALLOWED_HOSTS)
from . import web_term  # noqa: E402  # mounted here so the terminal routes live on the one app
web_term.register(app)
from . import web_events  # noqa: E402  # same reason: the change socket lives on the one app
web_events.register(app, store)


class SpawnRequest(BaseModel):
    name: str
    prompt: str
    cwd: str
    agent: str | None = None
    mode: str = "headless"
    task_id: str | None = None
    tier: str | None = None
    skip_permissions: bool = True
    domain: str | None = None  # inferred from cwd when omitted
    # Omitted means config.DEFAULT_MODEL, not "inherit whatever settings.json says".
    # detached.spawn() has taken a model since thread notes needed one, but this
    # field did not exist, so the API and `otto spawn` could not reach it.
    model: str | None = None
    # Free-text run notes. `otto task open` sets "mode=task | mode=attended" so
    # dispatch.settle routes the attended session's outcome back onto the card.
    notes: str | None = None
    # Attended sessions run under the owner's own permission prompts, so the unattended
    # findings/outreach rules are the wrong system prompt for them. Default keeps
    # the old behaviour for every other caller.
    system_extra: str | None = None


class DoneRequest(BaseModel):
    status: str = "ok"
    exit_code: int | None = 0
    notes: str | None = None


class ScheduleRequest(BaseModel):
    """Create or replace a schedule. This is the primitive personal routines need."""

    name: str
    command: str
    domain: str = config.PERSONAL
    # Settable because there is now more than one refresh schedule (work and personal)
    # and only the work one is seeded in code, so the personal one has to be creatable
    # through the API. Defaults to report, which is what every user-created schedule
    # was before this existed.
    runner: Literal["report", "refresh", "ingest", "launch", "writing"] = "report"
    description: str | None = None
    enabled: bool = True
    autostart: bool = False
    max_age_hours: int | None = None
    # Cadence, flattened so the CLI can pass simple flags.
    kind: str = "daily"
    at: str = "08:00"
    days: list[str] = []
    hours: int | None = None
    min_interval_days: int | None = None


class TaskRequest(BaseModel):
    title: str
    status: str = "backlog"
    domain: str = config.WORK
    priority: str = "normal"
    detail: str | None = None
    agent: str | None = None
    tags: list[str] = []
    task_ref: str | None = None
    tier: str | None = None
    due: str | None = None
    cwd: str | None = None
    auto: bool = True
    origin: str | None = None   # who filed it, when not the owner
    # False files the card even when an open card matches it. The door for "I
    # know it looks like that one, it is not": the default is to merge.
    dedupe: bool = True


class CardReplyRequest(BaseModel):
    """The owner answering a card: from a due toast's Reply button, the notice sheet, the
    card menu, or `otto task reply`. Free text only. The structured answers (done,
    push a day) are PATCHes and do not need a model."""

    text: str


class TaskPatch(BaseModel):
    """Partial update. Only the fields present are applied."""

    title: str | None = None
    status: str | None = None
    domain: str | None = None
    priority: str | None = None
    detail: str | None = None
    agent: str | None = None
    tags: list[str] | None = None
    due: str | None = None
    cwd: str | None = None
    auto: bool | None = None
    tier: str | None = None
    # Triage assessment. Settable here so the pass writes them the same way
    # anything else edits a card, rather than through a side channel.
    owner: str | None = None
    readiness: str | None = None
    assessed: str | None = None
    assessed_note: str | None = None
    # Both settable so a human re-sending a failed card with new information gets a
    # real retry. dispatch.eligible() skips anything with attempts >= max_attempts,
    # so WITHOUT this a re-queued card sits in `queued` forever and never runs --
    # a silent no-op, which is worse than refusing. Resetting is not a brake bypass:
    # the brake exists to stop UNATTENDED relaunch loops, and this path requires a
    # person to type context and press send. Pass last_error="" to clear it, so the
    # card does not keep showing an error from an attempt that has been superseded.
    attempts: int | None = None
    last_error: str | None = None
    # Plan gate and stale loop; see models.Task. `plan_approved` and `next_look`
    # take "" to clear, the same convention as last_error.
    plan: str | None = None
    plan_approved: str | None = None
    run_mode: str | None = None
    last_checked: str | None = None
    next_look: str | None = None
    run_id: str | None = None
    # Settable so a hand-judged merge (two cards the title heuristics could not see
    # as one) is recorded the way the sweep records it: closed INTO a keeper, not
    # counted as finished work. Pass "" to clear, same convention as last_error.
    duplicate_of: str | None = None


class ProposeRequest(BaseModel):
    """A running agent filing work it noticed.

    `run_id` is not decoration: it is how a filed card points back at the session
    that noticed it, and `propose_tasks` has always read `req.run_id`. It was
    missing from this model, so every call raised AttributeError and returned 500 —
    which means mid-run filing has been dead on the HTTP path, silently, for as
    long as the field has been read. Found 2026-08-05 filing a card by hand.

    The post-run path (`findings.harvest`) calls `file_tasks` in-process and never
    touched this, so findings still arrived at the END of a run. Only the "do not
    sit on it, file it now" path was broken, and a dropped proposal looks exactly
    like an agent that did not find anything.
    """

    tasks: list[dict[str, Any]] = []
    origin: str = "agent"
    run_id: str | None = None


class DecisionRequest(BaseModel):
    """Record a decision. `decision` and `why` are required by decisions.build,
    which raises a 400 with the reason rather than storing a half-empty entry."""

    title: str
    decision: str
    why: str
    alternatives: str | None = None
    revisit: str | None = None
    revisit_by: str | None = None
    owner: str | None = None
    domain: str = config.WORK
    tags: list[str] = []
    decided: str | None = None
    task_id: str | None = None
    origin: str | None = None
    run_id: str | None = None


class ChatRequest(BaseModel):
    message: str


class SnapshotRequest(BaseModel):
    kind: str
    domain: str = config.WORK
    summary: str | None = None
    items: list[dict[str, Any]] = []
    source: str = "claude-session"


class StampRequest(BaseModel):
    status: str = "ok"
    run_id: str | None = None
    at: str | None = None  # backdate, for migrating existing ledgers


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "persona": config.PERSONA_NAME,
        "pid": psutil.Process().pid,
        "state_dir": str(config.STATE_DIR),
        "at": iso(utcnow()),
    }


# The whole-state payload is the most expensive thing the daemon computes and the
# most frequently requested: the dashboard polls it every 2-3s, the desktop tray
# every 20s, and a second dashboard doubles it. On 2026-10-02 that alone held the
# daemon near 60% CPU with 90% spikes, /api/state took 1.5-2s, and the tray's 3s
# health timeout called a busy daemon "unreachable".
#
# The cache is keyed on store.version (bumped by every write, see otto/store.py):
# a write invalidates it at once, so the poll after an action sees the action,
# and while nothing is written the same bytes serve every client for up to
# config.STATE_IDLE_CACHE_SECONDS (the clock-driven fields, due flags and the
# machine snapshot, are what that bound is for). The payload is serialized and
# gzipped ONCE per fill: serializing 2 MB of JSON per request was itself a cost,
# and compressing it per request would have been a bigger one. The ETag is the
# sha1 of the JSON bytes; a poll that already holds them gets a bodyless 304.
BRIEFING_CACHE_SECONDS = 15.0
_briefing_cache: tuple[float, dict[str, Any]] | None = None
_state_lock = threading.Lock()


class _StateCache:
    """One computed /api/state payload, in the forms it is served in."""

    __slots__ = ("built_at", "version", "body", "gzipped", "etag")

    def __init__(self, version: int, payload: dict[str, Any]) -> None:
        self.built_at = time.time()
        self.version = version
        self.body = json.dumps(jsonable_encoder(payload), ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
        self.gzipped = gzip.compress(self.body, compresslevel=6)
        self.etag = f'"{hashlib.sha1(self.body).hexdigest()}"'


_state_cache: _StateCache | None = None


def _slim_detail(row: dict[str, Any]) -> None:
    """Cut a task-shaped row's `detail` for the state payload, in place.

    The card draws three lines of it; the inspector fetches GET /api/tasks/{id}
    for the whole text. `detail_truncated` is present only when something was cut,
    so a row without the flag carries its full detail.
    """
    detail = row.get("detail")
    if isinstance(detail, str) and len(detail) > config.STATE_DETAIL_CHARS:
        row["detail"] = detail[: config.STATE_DETAIL_CHARS]
        row["detail_truncated"] = True


def _slim_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the fields the dashboard never draws in full. Everything else is kept."""
    for row in payload.get("tasks") or []:
        _slim_detail(row)
    for col in (payload.get("board") or {}).get("columns") or []:
        for card in col.get("cards") or []:
            _slim_detail(card)
    return payload


def _state_payload() -> _StateCache:
    """The current payload, computed only when the store moved or the idle bound
    passed. Serialized under the lock so a second poller waits for the bytes
    rather than computing its own."""
    global _state_cache
    with _state_lock:
        version = store.version
        cached = _state_cache
        if (cached is not None and cached.version == version
                and time.time() - cached.built_at < config.STATE_IDLE_CACHE_SECONDS):
            return cached
        # Read the version BEFORE computing: a write that lands mid-computation
        # then shows as a newer version on the next poll and forces a recompute,
        # instead of being masked by a cache stamped after it.
        version = store.version
        cached = _StateCache(version, _slim_state(_compute_state()))
        _state_cache = cached
        return cached


def _etag_matches(header: str | None, etag: str) -> bool:
    """RFC 7232 If-None-Match: a comma list of quoted tags, maybe W/-prefixed, or *.
    Weak comparison is fine here: the tag is a digest of the exact bytes."""
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _cached_briefing() -> dict[str, Any]:
    """advisor.briefing is half a second of ranking that changes on the order of
    minutes. Fifteen seconds stale is invisible; recomputing it per poll was not."""
    global _briefing_cache
    now = time.time()
    if _briefing_cache and now - _briefing_cache[0] < BRIEFING_CACHE_SECONDS:
        return _briefing_cache[1]
    b = advisor.briefing(store)
    _briefing_cache = (now, b)
    return b


@app.get("/api/state")
def state(request: Request) -> Response:
    cached = _state_payload()
    # no-cache means "revalidate every time", not "never store": the browser
    # keeps the body and sends If-None-Match, which is exactly the 304 path.
    headers = {"ETag": cached.etag, "Cache-Control": "no-cache",
               "Vary": "Accept-Encoding"}
    if _etag_matches(request.headers.get("if-none-match"), cached.etag):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return Response(cached.gzipped, media_type="application/json", headers=headers)
    return Response(cached.body, media_type="application/json", headers=headers)


def _compute_state() -> dict[str, Any]:
    runs = store.runs()
    scheds = store.schedules()
    now_local = datetime.now().astimezone()
    known_items = store.known()
    kn = {known.key(k["kind"], k["name"]): k for k in known_items}
    sched_rows = []
    for s in scheds:
        due, reason = scheduled.is_due(s, now_local)
        st = scheduled.staleness(s)
        k = kn.get(known.key("schedule", s.name))
        sched_rows.append({
            **s.model_dump(),
            "due": due,
            "due_reason": reason,
            "stale": st[1] if st else None,
            "stale_level": st[0] if st else None,
            "known_reason": k["reason"] if k else None,
        })
    integ_rows = []
    for i in store.integrations():
        k = kn.get(known.key("integration", i.name))
        integ_rows.append({**i.model_dump(), "known_reason": k["reason"] if k else None})
    alerts = compute_alerts()
    entries = store.registry()
    return {
        "persona": config.PERSONA_NAME,
        "blurb": config.PERSONA_BLURB,
        "headline": persona.headline(
            len([a for a in alerts if a.level != "info"]),
            work=len([a for a in alerts if a.level != "info" and a.domain == config.WORK]),
            personal=len([a for a in alerts if a.level != "info" and a.domain == config.PERSONAL]),
        ),
        "at": iso(utcnow()),
        "domains": list(config.DOMAINS),
        "alerts": [a.model_dump() for a in alerts],
        "runs": [{**r.model_dump(), "verdict": verdict.verdict(r)}
                 for r in runs[:config.STATE_RUNS]],
        "active_runs": len([r for r in runs if r.active]),
        "schedules": sched_rows,
        "integrations": integ_rows,
        "setup": setup.summary(store),
        "known": known_items,
        "registry_summary": registry.summarize(entries),
        "registry_by_domain": registry.summarize_by_domain(entries),
        "registry_notes": list(_scan_notes),
        "tasks": [t.model_dump() for t in store.tasks()],
        "board": board.build(store),
        "snapshots": {k: v.model_dump() for k, v in store.snapshots().items()},
        "notices": [n.model_dump() for n in store.notices()[:40]],
        # Held messages first: they are the only thing in this payload with a deadline
        # that the owner can still act on.
        "outreach": {"summary": outreach.summary(store),
                     "items": [o.model_dump() for o in store.outreach()[:20]]},
        "day": store.get_day(datetime.now().astimezone().date().isoformat()),
        "day_prev": store.get_day(journal.yesterday().isoformat()),
        "machine": [m.model_dump() for m in machine.snapshot()] if config.MACHINE_PANEL else [],
        "config": configsync.summary(),
        "briefing": _cached_briefing(),
        "dispatch": dispatch.status(store),
        "logistics": logistics.view(store),
        "writing": writing.summary(store),
        "autorun": get_autorun(),
        "live": get_live(),
        "sessions": list_sessions(),
        "events": [e.model_dump() for e in store.events(config.STATE_EVENTS)],
    }


@app.get("/api/runs")
def list_runs(limit: int = 80) -> list[dict[str, Any]]:
    return [{**r.model_dump(), "verdict": verdict.verdict(r)} for r in store.runs()[:limit]]


class ReplyRequest(BaseModel):
    """Otto answering in a Slack thread, as Otto."""

    channel: str
    thread_ts: str
    text: str


@app.post("/api/slack/reply")
def slack_reply(req: ReplyRequest) -> dict[str, Any]:
    """Post a thread reply as Otto. The only channel-posting door in the API.

    The daemon does this rather than the caller because the bot credential must never
    enter a spawned session's environment: a session that held it could message the
    company with nothing in the way. So the session asks, and the limits
    (config.REPLY_CHANNELS, thread_ts required) are applied here in Python.
    """
    try:
        channel, ts = slack.reply_in_thread(req.channel, req.thread_ts, req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"replied in {channel} thread {req.thread_ts}: {req.text[:60]}",
              source="slack")
    return {"ok": True, "channel": channel, "ts": ts}


class PostRequest(BaseModel):
    """Otto starting its own message in a channel, as Otto."""

    channel: str
    text: str


@app.post("/api/slack/post")
def slack_post(req: PostRequest) -> dict[str, Any]:
    """Post top-level into an allowlisted channel, as Otto.

    Same reason as slack_reply for living in the daemon: the bot credential must
    never enter a spawned session's environment. The allowlist (config.POST_CHANNELS)
    is applied here in Python, not asked for in a prompt.

    The stamp is the second half of the fix, and the order matters. Only a post that
    Slack ACCEPTED stamps config.FEED_POST_SCHEDULE, so the freshness of that
    schedule is the freshness of the feed itself rather than of a run that believed
    it had posted. The bug this replaces was exactly that inversion: /daily stamped
    unconditionally, the summary silently did not go out, and every health surface
    read green for eight days.
    """
    if not req.channel.strip():
        # A blank id reaches here when a caller defaulted to an unconfigured
        # SLACK_CHANNEL_ID. Refuse by name rather than hand Slack an empty channel.
        raise HTTPException(400, "no channel given and OTTO_SLACK_CHANNEL_ID is not set")
    try:
        channel, ts = slack.post_to_channel(req.channel, req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"posted in {channel}: {req.text[:60]}", source="slack")
    if config.SLACK_CHANNEL_ID and channel == config.SLACK_CHANNEL_ID:
        store.stamp(config.FEED_POST_SCHEDULE, "ok")
    return {"ok": True, "channel": channel, "ts": ts}


class TellRequest(BaseModel):
    """Otto telling the owner something, as Otto. No recipient field: see slack.tell_owner."""

    text: str


@app.post("/api/slack/tell")
def slack_tell(req: TellRequest) -> dict[str, Any]:
    """DM the owner as Otto. The reporting channel: no allowlist, because it cannot be
    pointed anywhere but at the owner."""
    try:
        channel, ts = slack.tell_owner(req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"told {config.OWNER_NAME}: {req.text[:60]}", source="slack")
    return {"ok": True, "channel": channel, "ts": ts}


class DmRequest(BaseModel):
    """Otto DMing a colleague AND the owner, as Otto, because the owner asked for it."""

    to: list[str]
    text: str
    why: str = ""
    run_id: str | None = None
    task_id: str | None = None


@app.post("/api/slack/dm")
def slack_dm(req: DmRequest) -> dict[str, Any]:
    """Group DM (Otto, the owner, the named people), sent now as Otto. The door a board
    task or otto-dm session uses when the owner told it to message someone. The roster
    gate and the per-run brake are applied in outreach.directed, in Python, before
    anything is transmitted; the bot credential never leaves the daemon."""
    try:
        item = outreach.directed(store, to=req.to, body=req.text, why=req.why,
                                 run_id=req.run_id, task_id=req.task_id)
    except outreach.Refused as e:
        raise HTTPException(400, str(e)) from e
    if item.state != "sent":
        raise HTTPException(502, item.error or "send failed")
    return {"ok": True, "id": item.id, "to": item.to, "state": item.state}


@app.get("/api/spend")
def get_spend(days: int = 7, domain: str | None = None) -> dict[str, Any]:
    """Cost per workload, per model, per agent. Read-only, derived, no model call."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    if domain is not None and domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    return spend.report(store.runs(), days=days, domain=domain)


# ---- ledger: every session, priced ---------------------------------------------
#
# spend.report is Otto's runs, priced by Claude Code's result JSON. The ledger is
# every Claude Code session on the machine, the owner's terminals included, priced from
# telemetry where it exists and the transcripts where it does not. Both views stay:
# spend answers "what is Otto costing per workload", the ledger answers "where did
# the money go", and the second question was 84% unanswerable before this.

_LEDGER_MEMO: dict[str, tuple[float, dict[str, Any]]] = {}
_LEDGER_MEMO_SECONDS = 60.0
_ledger_lock = threading.Lock()


@app.get("/api/ledger")
def get_ledger(days: int = 30, who: str | None = None) -> dict[str, Any]:
    """The session ledger. Memoised for a minute: a poll-driven UI would otherwise
    re-read a gigabyte of transcripts every few seconds, and the numbers do not
    move faster than that."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    if who is not None and who not in ("yours", "otto", "other"):
        raise HTTPException(400, "who must be yours, otto, or other")
    key = f"{days}"
    now = time.time()
    with _ledger_lock:
        hit = _LEDGER_MEMO.get(key)
        if hit and now - hit[0] < _LEDGER_MEMO_SECONDS:
            rep = hit[1]
        else:
            rep = ledger.build(days=days, runs=store.runs())
            _LEDGER_MEMO[key] = (now, rep)
    if who:
        rep = {**rep, "sessions": [s for s in rep["sessions"] if s["who"] == who]}
    return rep


@app.get("/api/ledger/sessions/{sid}")
def get_ledger_session(sid: str) -> dict[str, Any]:
    """One session parsed live: timeline, rebuilds, and what filled its context."""
    d = ledger.detail(sid, runs=store.runs())
    if d is None:
        raise HTTPException(404, f"no transcript for session {sid}")
    if "ambiguous" in d:
        raise HTTPException(409, f"prefix matches {len(d['ambiguous'])} sessions")
    return d


@app.get("/api/telemetry")
def get_telemetry() -> dict[str, Any]:
    return {**telemetry.summary(), "endpoint": config.BASE_URL, "env": telemetry.env()}


# The OTLP receiver. Claude Code posts here every few seconds from every live
# session once the env block is installed. Three rules: answer fast (the exporter
# retries and buffers, but a slow collector is still a slow collector), never
# raise (a 500 here teaches the exporter to back off and we lose data), and write
# only the allowlisted fields (telemetry.KEEP is the whole contract).

async def _otlp(request: Request, kind: str) -> JSONResponse:
    raw = await request.body()
    if kind != "logs":
        return JSONResponse({"partialSuccess": {}})   # accepted, discarded
    payload = telemetry.decode_body(raw, request.headers.get("content-encoding"))
    if payload is None:
        # Not JSON: protocol is grpc or protobuf. Say so once in the event log.
        if not _otlp.warned:  # type: ignore[attr-defined]
            _otlp.warned = True  # type: ignore[attr-defined]
            store.log("telemetry: non-JSON OTLP payload received; set "
                      "OTEL_EXPORTER_OTLP_PROTOCOL=http/json", level="warn", source="telemetry")
        return JSONResponse({"partialSuccess": {"rejectedLogRecords": 0}})
    try:
        rows = telemetry.flatten(payload)
        if rows:
            store.append_telemetry(rows)
    except Exception as e:  # noqa: BLE001 - the exporter must not see our failure
        store.log(f"telemetry: could not record batch: {e}", level="warn", source="telemetry")
    return JSONResponse({"partialSuccess": {}})


_otlp.warned = False  # type: ignore[attr-defined]


@app.post("/v1/logs")
async def otlp_logs(request: Request) -> JSONResponse:
    return await _otlp(request, "logs")


@app.post("/v1/metrics")
async def otlp_metrics(request: Request) -> JSONResponse:
    return await _otlp(request, "metrics")


@app.post("/v1/traces")
async def otlp_traces(request: Request) -> JSONResponse:
    return await _otlp(request, "traces")


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return {**run.model_dump(), "verdict": verdict.verdict(run),
            "plan": verdict.plan(run, store)}


@app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def get_run_log(run_id: str, lines: int = 200) -> str:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return detached.tail_log(run, lines)


@app.get("/api/runs/{run_id}/activity")
def get_activity(run_id: str, limit: int = 200) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"no run matching {run_id}")
    return activity.parse(run, limit)


@app.get("/api/live")
def get_live() -> list[dict[str, Any]]:
    """Every agent running right now, with what it is currently doing."""
    out = []
    for r in store.runs():
        if r.status != "running":
            continue
        body = r.model_dump()
        body["verdict"] = verdict.verdict(r)
        try:
            body["plan"] = verdict.plan(r, store)
        except Exception as e:  # noqa: BLE001 - the plan is a garnish, never a blocker
            body["plan"] = None
            body["plan_error"] = str(e)
        try:
            a = activity.parse(r, limit=40)
            body["current"] = a["current"]
            body["tool_calls"] = a["tool_calls"]
            body["last_activity"] = a["last_activity"]
            body["input_tokens"] = a["input_tokens"] or r.input_tokens
            body["output_tokens"] = a["output_tokens"] or r.output_tokens
            body["streaming"] = a["streaming"]
            body["note"] = a.get("note")
            body["subtasks_started"] = a.get("subtasks_started", 0)
            body["subtasks_done"] = a.get("subtasks_done", 0)
        except Exception as e:  # noqa: BLE001 - a bad log must not hide the run
            body["current"] = f"could not read the log: {e}"
        out.append(body)
    return out


@app.get("/api/sessions")
def list_sessions(domain: str | None = None, live: bool = False) -> dict[str, Any]:
    items = store.sessions()
    if domain:
        items = [s for s in items if s.domain == domain]
    if live:
        items = [s for s in items if s.live]
    return {
        "summary": sessions.summary(store, domain),
        "sessions": [s.model_dump() for s in items],
    }


@app.get("/api/sessions/doctor")
def sessions_doctor() -> dict[str, Any]:
    return sessions.doctor(store)


class SessionPatch(BaseModel):
    title: str


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, req: SessionPatch) -> dict[str, Any]:
    """The owner naming a session. The title outranks what the transcript said."""
    try:
        sess = sessions.rename(store, session_id, req.title)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"session {sess.session_id[:8]} named '{sess.title}'", source="sessions")
    return sess.model_dump()


# ---- logistics dispatch and the herdr harness -----------------------------------

@app.get("/api/logistics")
def get_logistics() -> dict[str, Any]:
    return logistics.view(store)


@app.post("/api/logistics/refresh")
def refresh_logistics() -> dict[str, Any]:
    """Recompute the strip now rather than on the next tick."""
    logistics.refresh(store, herdr.snapshot())
    return logistics.view(store)


@app.post("/api/logistics/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str) -> dict[str, Any]:
    try:
        p, msg = logistics.approve(store, proposal_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, herdr.HerdrError) as e:
        raise HTTPException(409, str(e)) from e
    store.log(msg, source="logistics", task_id=p.task_id)
    logistics.refresh(store, herdr.snapshot())
    return {"ok": True, "message": msg, "proposal": p.model_dump()}


@app.post("/api/logistics/proposals/{proposal_id}/dismiss")
def dismiss_proposal(proposal_id: str) -> dict[str, Any]:
    try:
        p = logistics.dismiss(store, proposal_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    store.log(f"dismissed suggestion {p.id[:6]} ({p.task_id[:6]} -> {p.agent})",
              source="logistics", task_id=p.task_id)
    logistics.refresh(store, herdr.snapshot())
    return {"ok": True, "proposal": p.model_dump()}


def _check(ok: bool, what: str, value: Any) -> None:
    """Field validators raise ValueError, which pydantic turns into a 422 that
    names the field. The values here reach a herdr argv or a pane shell line."""
    if not ok:
        raise ValueError(f"not a valid {what}: {str(value)[:64]!r}")


def _require_herdr_target(target: str) -> None:
    """Path-parameter twin of the validators: a target that cannot be a pane id
    or agent name never reaches the herdr CLI (a leading '-' would be an option)."""
    if not safeargs.is_herdr_target(target):
        raise HTTPException(422, f"not a pane id or agent name: {target[:64]!r}")


class DispatchToRequest(BaseModel):
    task_id: str
    target: str   # herdr agent name or pane id

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        _check(safeargs.is_herdr_target(v), "pane id or agent name", v)
        return v


@app.post("/api/logistics/dispatch")
def dispatch_to_pane(req: DispatchToRequest) -> dict[str, Any]:
    try:
        msg = logistics.dispatch_to(store, req.task_id, req.target)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, herdr.HerdrError) as e:
        raise HTTPException(409, str(e)) from e
    store.log(msg, source="logistics")
    logistics.refresh(store, herdr.snapshot())
    return {"ok": True, "message": msg}


class HerdrOpenRequest(BaseModel):
    cwd: str
    label: str | None = None
    name: str | None = None
    resume: str | None = None   # a Claude session id to pick back up in the pane

    # resume is typed into a pane shell (herdr.claude_command), so its shape is
    # checked here as well as at the sink. name becomes the herdr agent name.
    @field_validator("resume")
    @classmethod
    def _resume(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_session_id(v), "session id", v)
        return v or None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_agent_name(v), "agent name ([a-z][a-z0-9_-]{0,31})", v)
        return v or None


@app.get("/api/herdr")
def get_herdr() -> dict[str, Any]:
    snap = herdr.snapshot() if herdr.available() else None
    return {"installed": herdr.available(), "binary": herdr.binary(),
            "running": snap is not None,
            "agents": herdr.agents(snap), "workspaces": herdr.workspaces(snap)}


@app.post("/api/herdr/ensure")
def ensure_herdr() -> dict[str, Any]:
    started, msg = herdr.ensure_server()
    store.log(msg, source="herdr")
    return {"started": started, "message": msg}


@app.post("/api/herdr/open")
def open_in_herdr(req: HerdrOpenRequest) -> dict[str, Any]:
    """A new workspace with a Claude session in it, optionally resuming one."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    herdr.ensure_server()
    try:
        agent = herdr.start_claude(req.cwd, label=req.label, name=req.name, resume=req.resume)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    store.log(f"herdr: claude up in {agent.get('pane_id')} ({req.cwd})"
              + (f", resumed {req.resume[:8]}" if req.resume else ""), source="herdr")
    herdr.sync(store)
    return {"ok": True, "agent": agent}


class HerdrWorktreeRequest(BaseModel):
    cwd: str                        # a path inside the repo
    branch: str | None = None       # create: required. open: this or path
    base: str | None = None         # create only: ref a NEW branch starts from
    path: str | None = None         # open only: the checkout path instead of branch
    label: str | None = None
    name: str | None = None         # agent name once claude is up
    start_claude: bool = True

    # branch and base go through herdr to git; a leading '-' would be read as an
    # option there. path likewise must not look like a flag.
    @field_validator("branch", "base")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_branch(v), "branch name", v)
        return v or None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v:
            _check(not v.lstrip().startswith("-"), "worktree path", v)
        return v or None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v:
            _check(safeargs.is_agent_name(v), "agent name ([a-z][a-z0-9_-]{0,31})", v)
        return v or None


def _worktree_response(ws_id: str, pane_id: str, req: HerdrWorktreeRequest,
                       verb: str) -> dict[str, Any]:
    """Shared tail of create and open: launch claude in the new root pane when
    asked, log it, sync the rail, and answer with the ids the dashboard needs."""
    agent: dict[str, Any] | None = None
    if req.start_claude:
        try:
            agent = herdr.launch_claude_in_pane(pane_id, name=req.name)
        except herdr.HerdrError as e:
            # The worktree exists and is open; only the launch failed. Say so with
            # the ids rather than hide a workspace the owner now has.
            store.log(f"herdr: worktree {verb} {ws_id} but claude did not start: {e}",
                      level="warn", source="herdr")
            raise HTTPException(409, f"worktree open as {ws_id} ({pane_id}); {e}") from e
    store.log(f"herdr: worktree {verb} {ws_id} ({req.branch or req.path}) from {req.cwd}"
              + (f", claude up in {pane_id}" if agent else ""), source="herdr")
    herdr.sync(store)
    return {"ok": True, "workspace_id": ws_id, "pane_id": pane_id, "agent": agent}


@app.get("/api/herdr/worktrees")
def list_herdr_worktrees(cwd: str) -> dict[str, Any]:
    """The worktrees of the repo containing cwd, and which are open in herdr."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    try:
        return herdr.worktree_list(cwd)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/herdr/worktree/create")
def create_herdr_worktree(req: HerdrWorktreeRequest) -> dict[str, Any]:
    """A git worktree for a branch, open as a workspace, with claude in it."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    if not req.branch:
        raise HTTPException(422, "branch is required")
    herdr.ensure_server()
    try:
        ws_id, pane_id = herdr.worktree_create(req.cwd, req.branch, base=req.base, label=req.label)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    return _worktree_response(ws_id, pane_id, req, "created")


@app.post("/api/herdr/worktree/open")
def open_herdr_worktree(req: HerdrWorktreeRequest) -> dict[str, Any]:
    """An existing worktree (by branch or path) open as a workspace, with claude."""
    if not herdr.available():
        raise HTTPException(409, "herdr is not installed")
    if bool(req.branch) == bool(req.path):
        raise HTTPException(422, "name the worktree by branch or path, exactly one")
    herdr.ensure_server()
    try:
        ws_id, pane_id = herdr.worktree_open(req.cwd, branch=req.branch, path=req.path,
                                             label=req.label)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    return _worktree_response(ws_id, pane_id, req, "opened")


@app.post("/api/herdr/focus/{target}")
def focus_in_herdr(target: str) -> dict[str, Any]:
    _require_herdr_target(target)
    try:
        herdr.focus(target)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    sessions.focus_window(herdr.client_host_pid() or 0)
    return {"ok": True}


# The rail's last-line preview. Each read is a herdr subprocess plus a screen
# scrape, and the dashboard asks for every working pane on every render, so an
# answer is held for ten seconds per target. A preview ten seconds stale is still
# a preview; a herdr call per render per pane is a load the server did not sign
# up for.
_PEEK_TTL_SECONDS = 10.0
_peek_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@app.get("/api/herdr/peek/{target}")
def peek_herdr(target: str) -> dict[str, Any]:
    _require_herdr_target(target)
    now = time.time()
    hit = _peek_cache.get(target)
    if hit is not None and now - hit[0] < _PEEK_TTL_SECONDS:
        return hit[1]
    try:
        text = herdr.read(target, lines=12)
    except herdr.HerdrError as e:
        raise HTTPException(409, str(e)) from e
    body = {"target": target, "line": herdr.last_line(text), "at": iso(utcnow())}
    _peek_cache[target] = (now, body)
    return body


@app.post("/api/sessions/{session_id}/open")
def open_session(session_id: str) -> dict[str, Any]:
    """Bring a live session's window forward, or resume an ended one in a new
    Windows Terminal tab. The daemon runs in the owner's interactive session (the
    OttoDaemon task is LogonType Interactive), so it can touch his windows."""
    if session_id in ("busy", "waiting", "idle", "offline"):
        raise HTTPException(404, f"no session matching {session_id}")
    if store.get_session(session_id) is None:
        raise HTTPException(404, f"no session matching {session_id}")
    ok, msg = sessions.open_session(store, session_id)
    store.log(f"session open {session_id[:8]}: {msg}", source="sessions",
              level="info" if ok else "warn")
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": ok, "message": msg}


@app.get("/api/threads")
def list_threads(band: str | None = None) -> dict[str, Any]:
    """Every dated dossier thread, computed live.

    Live rather than read off the threads-quiet notice, and that is the point. The
    notice froze 8 of 23 rows into a string hours ago; this is all of them as they
    are now, so acting on one and coming back shows the change.
    """
    rows = nudges.thread_rows(store)
    if band:
        rows = [r for r in rows if r["band"] == band]
    for r in rows:
        r["notes"] = triage.notes_for(store, r["id"], limit=5)
        r["runs"] = triage.runs_for(store, r["id"])
    counts: dict[str, int] = {}
    for r in nudges.thread_rows(store):
        counts[r["band"]] = counts.get(r["band"], 0) + 1
    return {
        "rows": rows,
        "counts": counts,
        "bands": {"stale_days": config.NUDGE_THREAD_STALE_DAYS,
                  "max_days": config.NUDGE_THREAD_MAX_DAYS},
        "in_flight": len(triage.active_runs(store)),
    }


class ThreadNote(BaseModel):
    thread_id: str
    note: str


class ThreadResolve(BaseModel):
    text: str


@app.post("/api/threads/{thread_id}/resolve")
def resolve_thread(thread_id: str, req: ThreadResolve) -> dict[str, Any]:
    """Rewrite a thread line in place, so it stops being reported as quiet.

    Separate from a plain dossier note because appending does not end a thread: the
    dated line that made it stale stays in the file and keeps reporting. This is the
    verb that actually closes one.
    """
    from . import people as _people
    thread = nudges.find_thread(store, thread_id)
    if thread is None:
        raise HTTPException(404, f"no thread {thread_id}; the dossier may have changed")
    try:
        ok = _people.replace_note(thread["slug"], "Threads", thread["text"], req.text)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(422, str(e)) from e
    if not ok:
        raise HTTPException(409, "the thread line no longer matches; it was edited "
                                 "since this view loaded")
    with contextlib.suppress(Exception):
        store.log(f"thread resolved for {thread['who']}: {req.text[:80]}",
                  source="thread-note")
    return {"ok": True, "slug": thread["slug"], "text": req.text}


@app.post("/api/threads/note")
def post_thread_note(req: ThreadNote) -> dict[str, Any]:
    """The owner's note on one thread. Records it, then spawns Otto's judgement.

    The verb is deliberately not chosen here (decided 2026-08-05): the session picks
    from a closed list in `triage.PROMPT_TEMPLATE`. Everything colleague-facing in
    that list still goes through the outreach hold, which is enforced in
    `outreach.compose` rather than in the prompt.
    """
    thread = nudges.find_thread(store, req.thread_id)
    if thread is None:
        # A thread id encodes its text, so an edited dossier line becomes a new id.
        raise HTTPException(404, f"no thread {req.thread_id}; the dossier may have "
                                 f"changed since this view loaded")
    try:
        run = triage.submit(store, thread, req.note)
    except triage.Refused as e:
        raise HTTPException(409, str(e)) from e
    return {"run": run.model_dump(), "thread_id": thread["id"]}


@app.post("/api/sessions/{state}")
def record_session(state: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest one Claude Code hook event. Called by scripts/otto_hook.py.

    THIS ENDPOINT IS ON THE CRITICAL PATH OF EVERY TURN on this machine, so it does
    exactly one small read-modify-write under the dedicated session lock and
    returns. Nothing here may grow into a probe, a spawn, or anything else that can
    block: the caller is a hook, and a hook that takes a second makes every prompt
    the owner submits take a second longer.
    """
    if state not in ("busy", "waiting", "idle", "offline"):
        raise HTTPException(400, f"unknown session state {state}")
    try:
        sess = sessions.record(store, state, payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return sess.model_dump()


@app.post("/api/schedules/{name}/run")
def run_schedule(name: str) -> dict[str, Any]:
    sched = store.get_schedule(name)
    if sched is None:
        raise HTTPException(404, f"no schedule named {name}")
    try:
        run, msg = launch.launch(store, sched)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    store.log(msg, source="launch", run_id=run.id)
    return {"message": msg, "run": run.model_dump()}


@app.post("/api/runs/prune")
def prune_runs(older_than_hours: int = 24, dry_run: bool = True) -> dict[str, Any]:
    """Drop finished-badly runs from history.

    Only touches orphaned/killed/failed runs older than the cutoff, and never a
    running one. The board derives an "N runs vanished" card from these, so old
    test artifacts and already-resolved incidents otherwise sit there forever
    reporting work that nobody still needs to do.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    doomed, kept = [], []
    for r in store.runs():
        drop = False
        if r.status in ("orphaned", "killed", "failed"):
            try:
                when = datetime.fromisoformat((r.ended or r.started).replace("Z", "+00:00"))
                drop = when < cutoff
            except ValueError:
                drop = False
        (doomed if drop else kept).append(r)

    if not dry_run and doomed:
        with store.lock:
            store.save_runs(kept)
        store.log(f"pruned {len(doomed)} finished run(s) older than {older_than_hours}h",
                  level="warn", source="runs")
    return {
        "dry_run": dry_run,
        "pruned": len(doomed),
        "remaining": len(kept),
        "runs": [{"id": r.id[:6], "name": r.name, "status": r.status,
                  "ended": r.ended or r.started} for r in doomed],
    }


@app.post("/api/runs/spawn")
def spawn_run(req: SpawnRequest) -> dict[str, Any]:
    try:
        run = detached.spawn(
            name=req.name, prompt=req.prompt, cwd=req.cwd, agent=req.agent,
            mode=req.mode, task_id=req.task_id, tier=req.tier,
            skip_permissions=req.skip_permissions, domain=req.domain,
            model=req.model or config.DEFAULT_MODEL or None,
            system_extra=req.system_extra,
        )
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    if req.notes:
        run.notes = ((run.notes or "") + f" | {req.notes}").strip(" |")
    store.upsert_run(run)
    store.log(f"spawned {run.name} (pid {run.pid}, {run.domain})", source="runner",
              run_id=run.id, cwd=req.cwd, agent=req.agent)
    return run.model_dump()


@app.post("/api/runs/{run_id}/kill")
def kill_run(run_id: str) -> dict[str, Any]:
    with store.lock:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run matching {run_id}")
        run = detached.kill(run)
        store.upsert_run(run)
    store.log(f"killed {run.name}", level="warn", source="runner", run_id=run.id)
    return run.model_dump()


@app.post("/api/runs/{run_id}/done")
def finish_run(run_id: str, req: DoneRequest) -> dict[str, Any]:
    """Self-report endpoint. A spawned agent calls this so its exit is unambiguous."""
    with store.lock:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run matching {run_id}")
        run.status = req.status  # type: ignore[assignment]
        run.exit_code = req.exit_code
        run.ended = iso(utcnow())
        if req.notes:
            run.notes = (run.notes or "") + f" | {req.notes}"
        store.upsert_run(run)
    store.log(f"{run.name} reported {req.status}", source="runner", run_id=run.id)
    return run.model_dump()


@app.put("/api/schedules/{name}")
def put_schedule(name: str, req: ScheduleRequest) -> dict[str, Any]:
    """Create or replace a schedule. How personal routines get added."""
    if req.domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    try:
        cadence = Cadence(
            kind=req.kind, at=req.at, days=[d.lower()[:3] for d in req.days],
            hours=req.hours, min_interval_days=req.min_interval_days,
        )
        sched = Schedule(
            name=name, command=req.command, domain=req.domain,  # type: ignore[arg-type]
            cadence=cadence, enabled=req.enabled, autostart=req.autostart,
            runner=req.runner,
            description=req.description, max_age_hours=req.max_age_hours,
        )
    except ValueError as e:
        raise HTTPException(400, f"invalid schedule: {e}") from e

    with store.lock:
        existing = store.get_schedule(name)
        if existing is not None:
            # Preserve run history across an edit.
            sched.last_run = existing.last_run
            sched.last_status = existing.last_status
            sched.last_run_id = existing.last_run_id
            # And everything else the request body cannot express. `runner` is not a
            # field on ScheduleRequest, so without this an edit to the built-in
            # refresh schedule silently demoted it to the default runner and turned
            # `otto refresh` into a shell command that does nothing useful.
            # Only inherit when the caller left it at the default, so an explicit
            # runner in the request still wins.
            if req.runner == "report":
                sched.runner = existing.runner
            sched.consecutive_failures = existing.consecutive_failures
            sched.disabled_reason = existing.disabled_reason
            sched.last_autorun = existing.last_autorun
        store.upsert_schedule(sched)
    store.log(f"{'updated' if existing else 'added'} schedule {name} ({req.domain})",
              source="schedule")
    return sched.model_dump()


@app.delete("/api/schedules/{name}")
def delete_schedule(name: str) -> dict[str, Any]:
    if not store.delete_schedule(name):
        raise HTTPException(404, f"no schedule named {name}")
    store.log(f"removed schedule {name}", level="warn", source="schedule")
    return {"removed": name}


@app.get("/api/snapshots")
def get_snapshots() -> dict[str, Any]:
    return {k: v.model_dump() for k, v in store.snapshots().items()}


@app.put("/api/snapshots/{kind}")
def put_snapshot(kind: str, req: SnapshotRequest) -> dict[str, Any]:
    """Ingest a view Otto cannot fetch itself (calendar, mail).

    A Claude session has the MCP servers; this daemon does not. So the session
    pushes, and Otto renders it with its age attached.
    """
    snap = Snapshot(kind=kind, domain=req.domain, summary=req.summary,
                    items=req.items, source=req.source)
    store.put_snapshot(snap)
    store.log(f"snapshot {snap.key} refreshed ({len(req.items)} items)",
              source="snapshot")
    return snap.model_dump()


@app.delete("/api/snapshots/{domain}/{kind}")
def delete_snapshot(domain: str, kind: str) -> dict[str, Any]:
    """Drop a snapshot. For a wrong push: Otto reporting invented items is worse
    than Otto reporting nothing, so a bad push must be removable."""
    if not store.delete_snapshot(domain, kind):
        raise HTTPException(404, f"no snapshot {domain}/{kind}")
    store.log(f"snapshot {domain}/{kind} dropped", level="warn", source="snapshot")
    return {"removed": f"{domain}/{kind}"}


@app.get("/api/feeds")
def get_feeds() -> dict[str, Any]:
    """Declared-vs-observed over the feed directories, plus each source's age.

    Read-only, and there is deliberately no write endpoint here. A producer
    contributes by writing a FILE, which is the entire point of the design: an
    HTTP push endpoint would just be the snapshot API again, and would put us back
    to teaching Otto about every source in advance.
    """
    return {
        "root": str(config.FEED_DIR),
        "producers": str(config.PRODUCERS_DIR),
        "file": config.FEED_FILE,
        "sources": [{"name": s.source.name, "domain": s.source.domain,
                     "trust": s.source.trust, "title": s.source.title,
                     "max_age_hours": s.source.max_age_hours,
                     "state": s.state, "age_hours": s.age_hours,
                     "items": s.items, "detail": s.detail}
                    for s in feeds.status(store)],
        "undeclared": feeds.undeclared(),
        "ledger": store.feeds(),
    }


@app.post("/api/feeds/init")
def post_feeds_init() -> dict[str, Any]:
    """Create the feed root, `producers/`, and a directory per declared source."""
    made = feeds.scaffold()
    if made:
        store.log(f"feed scaffold created {len(made)} directory(ies)", source="feed")
    return {"created": made}


@app.post("/api/tasks/propose")
def propose_tasks(req: ProposeRequest) -> dict[str, Any]:
    """Mid-run filing. Agents that find something early should not sit on it."""
    notes = findings.file_tasks(store, req.tasks, req.origin, req.run_id)
    for n in notes:
        store.log(n, source="findings")
    return {"filed": len(notes), "notes": notes}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"no task matching {task_id}")
    body = task.model_dump()
    if task.run_id:
        run = store.get_run(task.run_id)
        if run:
            body["run"] = run.model_dump()
    return body


@app.post("/api/tasks/{task_id}/dispatch")
def dispatch_task(task_id: str, force: bool = False) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"no task matching {task_id}")
    run, msg = dispatch.dispatch(store, task, force=force)
    if run is None:
        raise HTTPException(409, msg)
    store.log(msg, source="dispatch", task_id=task.id, run_id=run.id)
    return {"message": msg, "run": run.model_dump()}


@app.post("/api/schedules/{name}/arm")
def arm_schedule(name: str, armed: bool = True) -> dict[str, Any]:
    """Arm or disarm a schedule for unattended cron."""
    with store.lock:
        sched = store.get_schedule(name)
        if sched is None:
            raise HTTPException(404, f"no schedule named {name}")
        if armed and launch.is_slash(sched.command) is False and not sched.command:
            raise HTTPException(400, "schedule has no command")
        sched.autostart = bool(armed)
        # The built-in runners keep their own kind. Arming used to rewrite anything
        # that was not `refresh` to `launch`, which turned `otto meetings ingest`
        # into a shell command the daemon would try to execute -- the same bug the
        # refresh exemption below already exists for, one runner later.
        if armed and sched.runner not in ("refresh", "ingest"):
            sched.runner = "launch"
        if armed:
            sched.consecutive_failures = 0
            sched.disabled_reason = None
        store.upsert_schedule(sched)
    store.log(f"{name} {'ARMED for autorun' if armed else 'disarmed'}",
              level="warn", source="schedule")
    return sched.model_dump()


@app.get("/api/autorun")
def get_autorun() -> dict[str, Any]:
    scheds = store.schedules()
    armed = [s for s in scheds if s.autostart and s.runner in ("launch", "refresh", "ingest")]
    return {
        "enabled": config.SCHEDULE_AUTORUN,
        "max_concurrent": config.SCHEDULE_MAX_CONCURRENT,
        "max_failures": config.SCHEDULE_MAX_FAILURES,
        "min_gap_minutes": config.SCHEDULE_MIN_GAP_MINUTES,
        "budget_usd": config.SCHEDULE_BUDGET_USD,
        "armed": [
            {"name": s.name, "runner": s.runner, "command": s.command,
             "cadence": s.cadence.model_dump(), "last_autorun": s.last_autorun,
             "consecutive_failures": s.consecutive_failures,
             "disabled_reason": s.disabled_reason}
            for s in armed
        ],
        "tripped": [
            {"name": s.name, "reason": s.disabled_reason}
            for s in scheds if s.disabled_reason
        ],
        # Armed, due, and NOT running: the state worth staring at.
        "blocked": [
            {"name": s.name, "reason": launch.autorun_blocked(store, s)}
            for s in armed
            if s.runner == "launch"
            and scheduled.is_due(s, datetime.now().astimezone())[0]
            and launch.autorun_blocked(store, s)
        ],
    }


@app.post("/api/autorun")
def set_autorun(enabled: bool) -> dict[str, Any]:
    config.SCHEDULE_AUTORUN = bool(enabled)
    store.log(f"autorun {'enabled' if enabled else 'DISABLED'}",
              level="warn", source="schedule")
    return get_autorun()


@app.get("/api/dispatch")
def get_dispatch() -> dict[str, Any]:
    return dispatch.status(store)


@app.post("/api/dispatch")
def set_dispatch(enabled: bool) -> dict[str, Any]:
    dispatch.set_enabled(enabled)
    store.log(f"auto-dispatch {'enabled' if enabled else 'DISABLED'}",
              level="warn", source="dispatch")
    return dispatch.status(store)


@app.get("/api/briefing")
def get_briefing(domain: str | None = None) -> dict[str, Any]:
    return advisor.briefing(store, domain)


@app.get("/api/gaps")
def get_gaps(domain: str | None = None) -> list[dict[str, Any]]:
    rows = advisor.gaps(store)
    return [g for g in rows if not domain or g["domain"] == domain]


@app.post("/api/refresh")
def post_refresh(domain: str = "all") -> dict[str, Any]:
    """Start a refresh. `all` (the default) fans out to every wired-up domain.

    "Refresh my email and calendar" means both inboxes, so fanning out lives here
    rather than in the dashboard: the button, the CLI and any future caller all get
    the same behaviour from one place. A named domain still refreshes just that one.

    Never partial-fails silently. A domain that is skipped says why, because a button
    that appears to refresh both while quietly doing one is worse than an error.
    """
    if domain == "all":
        wanted = [d for d, kinds in config.SNAPSHOT_SOURCES.items() if kinds]
    elif domain in config.DOMAINS:
        wanted = [domain]
    else:
        raise HTTPException(400, f"unknown domain {domain!r}")

    in_flight = {r.domain for r in store.runs()
                 if r.status == "running" and "mode=refresh" in (r.notes or "")}
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for d in wanted:
        if d in in_flight:
            skipped.append({"domain": d, "reason": "already in flight"})
            continue
        try:
            run = refresh.start(store, d)
        except (OSError, RuntimeError, ValueError) as e:
            skipped.append({"domain": d, "reason": str(e)})
            continue
        store.upsert_run(run)
        store.log(f"refresh started ({d})", source="refresh", run_id=run.id)
        started.append(run.model_dump())

    if not started:
        detail = "; ".join(f"{s['domain']}: {s['reason']}" for s in skipped) or "nothing to do"
        raise HTTPException(409 if all(s["reason"] == "already in flight" for s in skipped)
                            else 500, f"no refresh started - {detail}")
    return {"runs": started, "skipped": skipped}


@app.get("/api/meetings")
def get_meetings() -> dict[str, Any]:
    return meetings.status(store)


@app.post("/api/meetings/ingest")
def post_meetings_ingest() -> dict[str, Any]:
    """Read any new Notion meeting notes and file the action items.

    One at a time, deliberately. Two concurrent ingesters would both read the same
    pages before either updated the ledger, and page-id dedupe cannot help when
    neither run has written yet.
    """
    in_flight = next((r for r in store.runs()
                      if r.status == "running" and "mode=ingest" in (r.notes or "")), None)
    if in_flight is not None:
        raise HTTPException(409, f"an ingest is already running ({persona.short(in_flight.id)})")
    try:
        run = meetings.start(store)
    except (OSError, RuntimeError, ValueError) as e:
        raise HTTPException(500, f"could not start the meeting ingest: {e}") from e
    store.upsert_run(run)
    store.log("meeting-notes ingest started", source="meetings", run_id=run.id)
    return {"run": run.model_dump()}


# ---- setup: first run -----------------------------------------------------------
# One engine behind the dashboard's Setup view and `otto setup` (otto/setup.py).
# Everything here is loopback and goes through OriginGuard like every other route.
# Writes go to <OTTO_HOME>/otto.env (otto/settings.py); config is import-time, so a
# write reports restart_needed and /api/daemon/restart carries the restart.

class SetupSettingsRequest(BaseModel):
    values: dict[str, str | None]


class SetupStepRequest(BaseModel):
    step: str


class SetupFirstCardRequest(BaseModel):
    title: str
    detail: str | None = None


class SetupSchedulesRequest(BaseModel):
    arm: list[str] = []


@app.get("/api/setup")
def get_setup() -> dict[str, Any]:
    return setup.view(store)


@app.post("/api/setup/settings")
def post_setup_settings(req: SetupSettingsRequest) -> dict[str, Any]:
    values = dict(req.values)
    identity_keys = {"OTTO_OWNER_NAME", "OTTO_ORG_NAME", "OTTO_WORK_ROOTS", "OTTO_PERSONAL_ROOTS"}
    try:
        if identity_keys & set(values):
            ident = setup.validate_identity({k: v for k, v in values.items() if k in identity_keys})
            values = {**{k: v for k, v in values.items() if k not in identity_keys}, **ident}
        if "OTTO_INTEGRATIONS" in values and values["OTTO_INTEGRATIONS"] is not None:
            allowed = {c[0] for c in setup.INTEGRATION_CHOICES}
            picked = [x.strip().lower() for x in str(values["OTTO_INTEGRATIONS"]).split(",") if x.strip()]
            bad = [x for x in picked if x not in allowed and x != "none"]
            if bad:
                raise ValueError(f"unknown integration: {', '.join(bad)}")
            values["OTTO_INTEGRATIONS"] = ",".join(x for x in picked if x != "none") or "none"
        with store.lock:
            return setup.write_settings(store, values)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(500, f"could not write {config.SETTINGS_PATH}: {e}") from e


@app.post("/api/setup/hooks/install")
def post_setup_hooks() -> dict[str, Any]:
    try:
        changed, message = sessions.install()
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    if changed:
        store.log(f"setup: {message}", source="setup")
    return {"changed": changed, "message": message}


@app.post("/api/setup/herdr/up")
def post_setup_herdr() -> dict[str, Any]:
    started, message = herdr.ensure_server()
    ok = started or herdr.server_running()
    if started:
        store.log(f"setup: {message}", source="herdr")
    return {"ok": ok, "message": message}


@app.post("/api/setup/first-card")
def post_setup_first_card(req: SetupFirstCardRequest) -> dict[str, Any]:
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    return create_task(TaskRequest(title=title, detail=(req.detail or "").strip() or None,
                                   status="backlog", auto=False))


@app.post("/api/setup/schedules")
def post_setup_schedules(req: SetupSchedulesRequest) -> dict[str, Any]:
    armed: list[str] = []
    for name in req.arm:
        if not safeargs.is_agent_name(name):
            # Schedule names share the agent-name shape (lowercase, digits, dashes);
            # anything else never matches a seeded schedule and is not worth a 404 loop.
            raise HTTPException(400, f"bad schedule name: {name!r}")
        arm_schedule(name, armed=True)
        armed.append(name)
    return {"armed": armed}


@app.post("/api/setup/skip")
def post_setup_skip(req: SetupStepRequest) -> dict[str, Any]:
    try:
        with store.lock:
            setup.skip(store, req.step)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return setup.view(store)


@app.post("/api/setup/unskip")
def post_setup_unskip(req: SetupStepRequest) -> dict[str, Any]:
    try:
        with store.lock:
            setup.skip(store, req.step, undo=True)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return setup.view(store)


@app.post("/api/setup/complete")
def post_setup_complete() -> dict[str, Any]:
    with store.lock:
        setup.complete(store)
    return setup.view(store)


@app.post("/api/setup/reset")
def post_setup_reset() -> dict[str, Any]:
    with store.lock:
        setup.reset(store)
    return setup.view(store)


def _restart_helper_argv(pid: int) -> list[str]:
    """What the restart spawns: the keepalive entry point, told to wait for this
    pid to be gone first. Kept as a function so a test can check the argv
    without a daemon dying."""
    import sys as _sys
    return [_sys.executable, "-m", "otto", "ensure", "--wait-pid", str(pid), "--quiet"]


def _exit_after(delay: float) -> None:
    """Leave the way `otto stop` would: pidfile cleared, this process only. Spawned
    agents are not touched and the next daemon re-adopts them from runs.json."""
    import os as _os
    time.sleep(delay)
    _clear_pidfile()
    _os._exit(0)


@app.post("/api/daemon/restart")
def post_daemon_restart() -> dict[str, Any]:
    import subprocess
    pid = psutil.Process().pid
    repo = str(Path(__file__).resolve().parent.parent)
    creation = (0x00000200 | 0x08000000) if os.name == "nt" else 0
    try:
        subprocess.Popen(_restart_helper_argv(pid), cwd=repo, env=herdr.clean_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=creation, close_fds=True)
    except OSError as e:
        raise HTTPException(500, f"could not spawn the restart helper: {e}") from e
    store.log("restart requested from the dashboard; the keepalive helper brings the daemon back",
              source="daemon")
    threading.Thread(target=_exit_after, args=(0.4,), daemon=True, name="otto-restart").start()
    return {"ok": True, "pid": pid, "message": "restarting; poll /api/health until the pid changes"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    # `home` is here so a caller that has to supply a working directory has a real
    # default instead of guessing one. Otto's own repo would be the wrong default
    # for arbitrary work.
    return {**configsync.summary(), "home": str(config.HOME)}


class CheckinRequest(BaseModel):
    """The owner's own report. Deliberately free text plus one optional number: the
    ask was for two or three lines, not a form, and a form that will not get filled
    in is worth less than a sentence that will."""

    note: str | None = None
    energy: int | None = None      # 1-5, optional
    # The signals from the owner's operating profile (otto/wellbeing.py). Every one is
    # optional and three-state: absent means "not asked", NOT "no". The note above
    # stays sufficient on its own, which is the constraint this had to be built under.
    signals: dict[str, Any] | None = None


class NoticeRequest(BaseModel):
    title: str
    body: str | None = None
    level: Literal["info", "warn", "crit"] = "info"
    domain: str = config.WORK
    source: str = "otto"
    command: str | None = None
    notify: bool | None = None      # None = decide from level


@app.get("/api/notices")
def get_notices(unread_only: bool = False, domain: str | None = None) -> list[dict[str, Any]]:
    out = store.notices()
    if unread_only:
        out = [n for n in out if n.read_at is None]
    if domain:
        out = [n for n in out if n.domain == domain]
    return [n.model_dump() for n in out]


@app.post("/api/notices")
def post_notice(req: NoticeRequest) -> dict[str, Any]:
    """How Otto, an agent, or a schedule sends the owner a message."""
    if req.domain not in config.DOMAINS:
        raise HTTPException(400, f"domain must be one of {config.DOMAINS}")
    n = notify.post(store, req.title, body=req.body, level=req.level,
                    domain=req.domain, source=req.source, command=req.command,
                    notify=req.notify)
    return n.model_dump()


@app.post("/api/notices/{notice_id}/read")
def read_notice(notice_id: str) -> dict[str, Any]:
    n = notify.mark_read(store, notice_id)
    if n is None:
        raise HTTPException(404, f"no notice matching {notice_id}")
    return n.model_dump()


@app.post("/api/notices/read-all")
def read_all_notices() -> dict[str, Any]:
    n = 0
    for notice in store.notices():
        if notice.read_at is None:
            notify.mark_read(store, notice.id)
            n += 1
    return {"marked_read": n}


@app.delete("/api/notices/{notice_id}")
def remove_notice(notice_id: str) -> dict[str, Any]:
    if not store.delete_notice(notice_id):
        raise HTTPException(404, f"no notice matching {notice_id}")
    return {"removed": notice_id}


class OutreachRequest(BaseModel):
    """Composing a message to a colleague. Every field except `tier` is load-bearing.

    `why` has no default on purpose. It is what the owner vetoes on, and a producer that
    cannot say why it is messaging somebody has not thought about it enough to be
    allowed to.
    """

    to: str
    body: str
    why: str
    source: str = "otto"
    tier: int = 0
    channel: Literal["slack-dm", "slack-channel"] = "slack-dm"
    hold_minutes: int | None = None
    run_id: str | None = None
    task_id: str | None = None


@app.get("/api/outreach")
def get_outreach(state: str | None = None) -> dict[str, Any]:
    items = store.outreach()
    if state:
        items = [o for o in items if o.state == state]
    return {"summary": outreach.summary(store),
            "items": [o.model_dump() for o in items[:100]]}


@app.post("/api/outreach")
def post_outreach(req: OutreachRequest) -> dict[str, Any]:
    """Compose and hold. Never sends inline, whatever the caller wants.

    A 400 here is a REFUSAL with a reason, not a validation error: it is the gate
    telling a producer that this message will not be sent and why, which is
    information the producer should act on rather than retry.
    """
    try:
        o = outreach.compose(
            store, to=req.to, body=req.body, why=req.why, source=req.source,
            tier=req.tier, channel=req.channel, hold_minutes=req.hold_minutes,
            run_id=req.run_id, task_id=req.task_id)
    except outreach.Refused as e:
        raise HTTPException(400, f"refused: {e}") from None
    # The owner is told, every time, at warn level. An outreach never seen held is an
    # outreach he had no window on, which would make the hold decorative.
    notify.post(
        store, f"Otto wants to message {o.to}",
        body=(f"{o.body}\n\nwhy: {o.why}\n\n"
              f"sends in {o.hold_minutes} min unless you stop it: "
              f"otto outreach kill {o.id[:6]}"),
        level="warn", domain=config.WORK, source="outreach",
        command=f"otto outreach kill {o.id[:6]}", notify=True)
    return o.model_dump()


@app.post("/api/outreach/{oid}/kill")
def kill_outreach(oid: str) -> dict[str, Any]:
    o = outreach.kill(store, oid)
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


@app.post("/api/outreach/{oid}/send")
def send_outreach(oid: str) -> dict[str, Any]:
    """The owner choosing not to wait out the hold."""
    o = outreach.send_now(store, oid)
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


@app.post("/api/outreach/{oid}/extend")
def extend_outreach(oid: str, minutes: int = 10) -> dict[str, Any]:
    """The owner buying time on the hold without deciding. Held messages only."""
    try:
        o = outreach.extend(store, oid, minutes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if o is None:
        raise HTTPException(404, f"no held outreach matching {oid}")
    return o.model_dump()


# ---- known failures ---------------------------------------------------------
# A schedule or integration the owner has said is broken and knows why. See known.py.

class KnownRequest(BaseModel):
    reason: str


@app.get("/api/known")
def get_known() -> list[dict[str, Any]]:
    return store.known()


@app.put("/api/known/{kind}/{name}")
def put_known(kind: str, name: str, req: KnownRequest) -> dict[str, Any]:
    try:
        return known.mark(store, kind, name, req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.delete("/api/known/{kind}/{name}")
def delete_known(kind: str, name: str) -> dict[str, Any]:
    if not known.clear(store, kind, name):
        raise HTTPException(404, f"nothing marked known for {known.key(kind, name)}")
    return {"removed": known.key(kind, name)}


@app.get("/api/patterns")
def get_patterns(days: int = 90) -> dict[str, Any]:
    """What his check-ins say actually moves his energy.

    Read-only and deterministic, like `otto next` and for the same reason: this is the
    thing he should be able to look at often, so it must be instant and free.
    """
    return wellbeing.patterns(store, days=days)


@app.get("/api/day/{day}")
def get_day(day: str, refresh: bool = False) -> dict[str, Any]:
    """A day record: the transcript rollup plus whatever the owner reported.

    `refresh=true` recomputes the rollup from transcripts. Cheap (sub-second,
    no model) but it is a write, so it goes through the daemon like everything else.
    """
    try:
        d = _dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"not an ISO date: {day!r}") from None
    rec = store.get_day(day)
    if refresh or not rec.get("rollup"):
        rec = store.put_day(day, {"rollup": journal.rollup(d)})
    return rec


@app.put("/api/day/{day}")
def put_day(day: str, req: CheckinRequest) -> dict[str, Any]:
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, f"not an ISO date: {day!r}") from None
    if req.energy is not None and not 1 <= req.energy <= 5:
        raise HTTPException(400, "energy must be 1-5")
    checkin = {k: v for k, v in
               {"note": req.note, "energy": req.energy, "at": iso(utcnow())}.items()
               if v is not None}
    for key, value in (req.signals or {}).items():
        sig = wellbeing.BY_KEY.get(key)
        if sig is None:
            raise HTTPException(400, f"unknown signal {key!r}")
        if sig.kind == "bool" and not isinstance(value, bool):
            raise HTTPException(400, f"{key} must be true/false")
        if sig.kind == "scale" and not (isinstance(value, int) and 1 <= value <= 5):
            raise HTTPException(400, f"{key} must be 1-5")
        checkin[key] = value
    # put_day MERGES, so signals added later in the evening join the morning's note
    # rather than replacing it. That is what makes a partial check-in safe to do twice.
    rec = store.put_day(day, {"checkin": checkin})
    store.log(f"check-in recorded for {day}", source="journal")
    return rec


@app.get("/api/repos")
def get_repos() -> dict[str, Any]:
    """Roots and repositories, derived from the registry, tasks and runs.

    Not a stored list. A repo appears because something actually touched it, so
    this cannot drift from the filesystem the way a hand-kept inventory would.
    """
    return repos.collect(store)


@app.get("/api/machine")
def get_machine() -> list[dict[str, Any]]:
    return [m.model_dump() for m in machine.snapshot()]


@app.get("/api/board")
def get_board(domain: str | None = None) -> dict[str, Any]:
    return board.build(store, domain)


@app.get("/api/tasks")
def list_tasks(domain: str | None = None) -> list[dict[str, Any]]:
    items = store.tasks()
    if domain:
        items = [t for t in items if t.domain == domain]
    return [t.model_dump() for t in items]


@app.post("/api/tasks")
def create_task(req: TaskRequest) -> dict[str, Any]:
    try:
        body = req.model_dump()
        # An explicit origin means something other than the owner authored this, which is
        # what `source` records. Keeps board provenance honest without a second flag.
        if body.get("origin"):
            body["source"] = "agent"
        want_dedupe = body.pop("dedupe", True)
        task = Task(id=uuid.uuid4().hex, **body)
        task.fingerprint = dedupe.fingerprint(task.title, task.domain)
        if not want_dedupe and dedupe.DISTINCT_TAG not in task.tags:
            # Durable, not just a skip here: the tick's sweep would otherwise fold
            # the card on its next pass and the flag would have meant nothing.
            task.tags.append(dedupe.DISTINCT_TAG)
    except ValueError as e:
        raise HTTPException(400, f"invalid task: {e}") from e
    with store.lock:
        match = dedupe.find_match(task, store.tasks()) if want_dedupe else None
        if match is not None:
            # The card already exists under another wording. Bump it and hand it
            # back; the caller sees `merged` and the title it actually landed on.
            existing, why = match
            dedupe.absorb(existing, task, stored=False)
            store.upsert_task(existing)
            store.log(f"task merged into existing: '{task.title[:60]}' -> "
                      f"{existing.id[:6]} ({why})", source="board",
                      task_id=existing.id, domain=existing.domain)
            body = existing.model_dump()
            body["merged"] = why
            return body
        store.upsert_task(task)
    store.log(f"task added: {task.title}", source="board",
              task_id=task.id, domain=task.domain)
    return task.model_dump()


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: str, req: TaskPatch) -> dict[str, Any]:
    with store.lock:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        changes = req.model_dump(exclude_none=True)
        if not changes:
            return task.model_dump()
        # "" means clear for the optional strings a human re-sets: an empty plan
        # approval is "not approved", not an approval stamped "".
        for k in ("plan_approved", "next_look", "last_checked", "run_mode", "run_id",
                  "plan", "last_error", "duplicate_of"):
            if changes.get(k) == "":
                changes[k] = None
        try:
            updated = task.model_copy(update=changes)
            # Re-validate so a bad status or domain is rejected rather than stored.
            updated = Task.model_validate(updated.model_dump())
        except ValueError as e:
            raise HTTPException(400, f"invalid update: {e}") from e
        updated.touch()
        store.upsert_task(updated)
    store.log(f"task updated: {updated.title} -> {updated.status}", source="board",
              task_id=updated.id)
    return updated.model_dump()


class TaskBatchRequest(BaseModel):
    """One change applied to many cards under one lock and one write.

    The board's per-card PATCH is 30ms; what made moving cards slow was the full
    state refresh the UI waited on after each one. With a batch verb the UI can
    select twenty cards and issue one call, and tasks.json (1.4 MB) is rewritten
    once instead of twenty times. `queued` is refused here on purpose: promotion
    goes through the triage gate one card at a time, and a batch verb that could
    dispatch twenty unattended sessions in one click is the wrong shape for it.
    """

    ids: list[str]
    patch: TaskPatch


class DedupeRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/tasks/dedupe")
def dedupe_tasks(req: DedupeRequest) -> dict[str, Any]:
    """Fold every duplicate on the open board into its keeper, or say what would.

    The tick does this on its own whenever the open board changes; the verb exists
    so the owner can see the plan (`--dry-run`) and so a run's report can carry it.
    """
    rows = dedupe.sweep(store, dry_run=req.dry_run)
    if not req.dry_run:
        for r in rows:
            store.log(f"merged '{r['dupe_title'][:44]}' into {r['keeper_id'][:6]} "
                      f"'{r['keeper_title'][:44]}' ({r['why']})",
                      source="dedupe", task_id=r["keeper_id"])
    return {"dry_run": req.dry_run, "merged": rows}


@app.post("/api/tasks/batch")
def batch_tasks(req: TaskBatchRequest) -> dict[str, Any]:
    changes = req.patch.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "empty patch")
    if changes.get("status") == "queued":
        raise HTTPException(400, "batch cannot move cards to queued; promote each "
                                 "through `otto triage promote`")
    if not req.ids:
        raise HTTPException(400, "no ids")
    for k in ("plan_approved", "next_look", "last_checked", "run_mode", "run_id",
              "plan", "last_error"):
        if changes.get(k) == "":
            changes[k] = None
    updated: list[Task] = []
    missing: list[str] = []
    with store.lock:
        items = store.tasks()
        wanted = list(dict.fromkeys(req.ids))
        for pid in wanted:
            hits = [t for t in items if t.id == pid or t.id.startswith(pid)]
            if len(hits) != 1:
                missing.append(pid)
                continue
            t = hits[0]
            try:
                new = Task.model_validate(t.model_copy(update=changes).model_dump())
            except ValueError as e:
                raise HTTPException(400, f"invalid update: {e}") from e
            new.touch()
            items[items.index(t)] = new
            updated.append(new)
        if updated:
            store.save_tasks(items)
    for t in updated:
        store.log(f"task updated: {t.title} -> {t.status}", source="board", task_id=t.id)
    return {"updated": [t.model_dump() for t in updated], "missing": missing}


@app.post("/api/tasks/{task_id}/reply")
def reply_task(task_id: str, req: CardReplyRequest) -> dict[str, Any]:
    """The owner said something about a card. Record it, then let a small session decide.

    Two things happen, in this order. His words are appended to the card's detail
    FIRST, dated and attributed, so the record exists even if the session never
    starts: his own sentence is the thing that will still be right in six weeks.
    Then a /otto-card session is spawned with the card and the reply in its system
    context, on the DM model and the DM budget, to update the card, file what
    follows, or answer him. It is linked to the card by task_id, so the card's log
    shows what Otto did with the reply.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "reply is empty")
    with store.lock:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        stamp = iso(utcnow())[:16].replace("T", " ")
        task.detail = "\n\n".join(x for x in (
            (task.detail or "").rstrip(),
            f"--- {config.OWNER_NAME} replied {stamp} UTC ---\n{text}",
        ) if x)
        task.touch()
        store.upsert_task(task)
    store.log(f"reply on card: {task.title[:60]}", source="board", task_id=task.id)

    # Plain lines, not JSON. The model reads them as easily, and the command line
    # they travel on (see detached._write_launcher) is happier with fewer quotes.
    fields = [(k, getattr(task, k)) for k in
              ("id", "title", "status", "domain", "priority", "due", "tier", "owner",
               "readiness", "assessed_note", "created")]
    card_lines = [f"  {k}: {v}" for k, v in fields if v not in (None, "", [])]
    if task.tags:
        card_lines.append(f"  tags: {', '.join(task.tags)}")
    detail = "\n".join("    " + ln for ln in (task.detail or "").splitlines())
    extra = (
        f"{config.OWNER_NAME.upper()} REPLIED TO A BOARD CARD. The card, as it stands "
        "(the reply is already appended to its detail):\n\n"
        + "\n".join(card_lines) + "\n  detail:\n" + detail + "\n\n"
        f"WHAT {config.OWNER_NAME.upper()} SAID:\n  {text}\n\n"
        f"Today is {datetime.now().astimezone().date().isoformat()}. "
        "Act on the card with `python -m otto task set/mv {id}` and file anything new "
        "with `python -m otto task add`. Then send the receipt with `python -m otto "
        "notify` so the owner can see what changed without opening the board."
    )
    try:
        run = detached.spawn(
            name="card-reply",
            prompt="/otto-card",
            cwd=str(config.HOME),
            mode="headless",
            skip_permissions=True,
            domain=task.domain,
            task_id=task.id,
            system_extra=extra,
            budget_usd=config.CARD_REPLY_BUDGET_USD or None,
            model=config.CARD_REPLY_MODEL or config.DEFAULT_MODEL or None,
        )
    except (ValueError, OSError) as e:
        # The reply is on the card already. Say the session did not start rather
        # than pretending the text was lost.
        store.log(f"card reply recorded but no session: {e}", level="warn",
                  source="board", task_id=task.id)
        return {"task": task.model_dump(), "run": None, "error": str(e)}
    run.notes = ((run.notes or "") + f" | mode=card-reply | card={task.id[:6]}").strip(" |")
    store.upsert_run(run)
    store.log(f"card-reply session for {task.title[:50]}", source="runner",
              run_id=run.id, task_id=task.id)
    return {"task": task.model_dump(), "run": run.model_dump(), "error": None}


@app.delete("/api/tasks/{task_id}")
def remove_task(task_id: str) -> dict[str, Any]:
    """Delete a task, and kill its run if one is still in flight.

    Without this the agent keeps working on a task that no longer exists: it would
    burn tokens, possibly change files, and have nowhere to report back to.
    """
    killed = None
    with store.lock:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"no task matching {task_id}")
        if task.run_id:
            run = store.get_run(task.run_id)
            if run is not None and run.status == "running":
                run = detached.kill(run)
                run.notes = ((run.notes or "") + " | task deleted").strip(" |")
                store.upsert_run(run)
                killed = run.id
        store.delete_task(task.id)
    store.log(f"task removed: {task.title}"
              + (f" (killed run {persona.short(killed)})" if killed else ""),
              level="warn", source="board")
    return {"removed": task.id, "killed_run": killed}


@app.get("/api/chat")
def get_chat() -> dict[str, Any]:
    thread = store.chat()
    # A turn in flight is what the UI renders as "thinking".
    pending = [
        r.id for r in store.runs()
        if r.status == "running" and "mode=chat" in (r.notes or "")
    ]
    thread["pending"] = pending
    return thread


@app.post("/api/chat")
def post_chat(req: ChatRequest) -> dict[str, Any]:
    if any(r.status == "running" and "mode=chat" in (r.notes or "") for r in store.runs()):
        raise HTTPException(409, "a chat turn is already in flight")
    try:
        run, thread = chat.send(store, req.message)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(500, f"could not start chat turn: {e}") from e
    store.upsert_run(run)
    store.log("chat turn started", source="chat", run_id=run.id)
    thread["pending"] = [run.id]
    return thread


@app.delete("/api/chat")
def reset_chat() -> dict[str, Any]:
    chat.reset(store)
    store.log("chat thread reset", source="chat")
    return {"reset": True}


@app.get("/api/registry")
def get_registry(kind: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
    entries = store.registry()
    if kind:
        entries = [e for e in entries if e.kind == kind]
    if domain:
        entries = [e for e in entries if e.domain == domain]
    return [e.model_dump() for e in entries]


@app.get("/api/people")
def get_people(q: str | None = None) -> list[dict[str, Any]]:
    """Operational dossiers. Read straight off disk rather than from state: they are
    plain markdown owned half by the directory sync and half by the owner, and putting them through the
    single-writer store would mean the daemon rewriting files a human edits by hand.

    `mobilePhone` is deliberately dropped from the list payload. The dashboard binds to
    127.0.0.1 so this is not an exposure, but sixty phone numbers rendered on a summary
    screen is not something any view here needs, and the detail endpoint has it.
    """
    from . import people as _people
    rows = [{k: v for k, v in d.items() if k != "mobilePhone"} for d in _people.load()]
    if q:
        needle = q.casefold()
        rows = [d for d in rows if any(needle in str(v).casefold() for v in d.values())]
    return rows


@app.get("/api/people/{slug}")
def get_person(slug: str) -> dict[str, Any]:
    from . import people as _people
    d = _people.get(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no dossier for {slug}")
    # Slug comes from our own listing, but it lands in a path join, so refuse anything
    # that could climb out of PEOPLE_DIR rather than trusting the caller.
    safe = _people.PEOPLE_DIR / f"{d['slug']}.md"
    if safe.parent.resolve() != _people.PEOPLE_DIR.resolve() or not safe.is_file():
        raise HTTPException(status_code=404, detail="dossier not readable")
    return {**d, "markdown": safe.read_text(encoding="utf-8", errors="replace")}


class PersonPatch(BaseModel):
    meta: dict[str, str] | None = None        # pronouns, full_name, last_contact, ...
    note: str | None = None
    section: str | None = None                # required when `note` is present


@app.patch("/api/people/{slug}")
def patch_person(slug: str, req: PersonPatch) -> dict[str, Any]:
    """Edit a dossier from the UI. The daemon is the writer, same as everywhere else.

    Validation lives in people.py rather than here so the CLI and the API cannot drift
    on what counts as an editable field or a real section.
    """
    from . import people as _people
    if _people.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no dossier for {slug}")
    changed: list[str] = []
    try:
        if req.meta:
            _people.set_meta(slug, req.meta)
            changed.append("meta: " + ", ".join(sorted(req.meta)))
        if req.note is not None:
            if not req.section:
                raise HTTPException(status_code=422, detail="note requires a section")
            _people.add_note(slug, req.section, req.note)
            changed.append(f"note -> {req.section}")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if changed:
        # The file write has already landed. A failure to record the event must not
        # turn a successful edit into a 500, because the caller would retry and write
        # the note twice. Same ordering rule as notify.py: persist first, tell second.
        with contextlib.suppress(Exception):
            store.log(f"dossier {slug}: {'; '.join(changed)}", source="people")
    d = _people.get(slug)
    safe = _people.PEOPLE_DIR / f"{d['slug']}.md"
    return {**d, "markdown": safe.read_text(encoding="utf-8", errors="replace"),
            "changed": changed}


@app.get("/api/people/meta/schema")
def people_meta_schema() -> dict[str, Any]:
    """What the UI is allowed to edit, so the form is generated rather than hardcoded."""
    from . import people as _people
    return {
        "fields": [{"key": k, "hint": _people.META_HINTS[k]} for k in _people.META_FIELDS],
        "sections": [h for h, _ in _people.SECTIONS],
        "dated_sections": sorted(_people.DATED_SECTIONS),
        "pronoun_suggestions": list(_people.PRONOUN_SUGGESTIONS),
    }


@app.post("/api/registry/scan")
def scan_registry() -> dict[str, Any]:
    notes = refresh_registry()
    _scan_notes[:] = notes
    for n in notes[:20]:
        store.log(n, source="registry")
    return {"changes": notes, "summary": registry.summarize(store.registry())}


@app.get("/api/schedules")
def get_schedules() -> list[dict[str, Any]]:
    now_local = datetime.now().astimezone()
    out = []
    for s in store.schedules():
        due, reason = scheduled.is_due(s, now_local)
        out.append({**s.model_dump(), "due": due, "due_reason": reason})
    return out


@app.post("/api/schedules/{name}/stamp")
def stamp_schedule(name: str, req: StampRequest) -> dict[str, Any]:
    sched = store.stamp(name, req.status, req.run_id, req.at)
    if sched is None:
        raise HTTPException(404, f"no schedule named {name}")
    store.log(f"stamped {name} ({req.status})", source="schedule")
    return sched.model_dump()


@app.post("/api/schedules/{name}/toggle")
def toggle_schedule(name: str, enabled: bool) -> dict[str, Any]:
    with store.lock:
        sched = store.get_schedule(name)
        if sched is None:
            raise HTTPException(404, f"no schedule named {name}")
        sched.enabled = enabled
        store.upsert_schedule(sched)
    store.log(f"{name} {'enabled' if enabled else 'disabled'}", source="schedule")
    return sched.model_dump()


@app.get("/api/integrations")
def get_integrations() -> list[dict[str, Any]]:
    return [i.model_dump() for i in store.integrations()]


@app.post("/api/integrations/probe")
def probe_integrations() -> list[dict[str, Any]]:
    refresh_integrations()
    return [i.model_dump() for i in store.integrations()]


@app.get("/api/events")
def get_events(limit: int = 100) -> list[dict[str, Any]]:
    return [e.model_dump() for e in store.events(limit)]


@app.post("/api/runs/{run_id}/ack")
def ack_run(run_id: str, undo: bool = False) -> dict[str, Any]:
    """Acknowledge a failed or orphaned run, clearing its derived board card.

    The board's only verb for a past failure. A derived card cannot be dragged to
    Done because there is no stored row to write, and a failed run is history that
    never clears on its own, so without this the card is permanent and the only way
    out is `otto prune` deleting the run from the ledger. Acknowledging keeps the
    record and drops the nag, which are different things.
    """
    with store.lock:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        if run.status not in ("failed", "orphaned"):
            raise HTTPException(
                400, f"run {persona.short(run.id)} is {run.status}, not a failure; "
                     "there is nothing to acknowledge")
        run.reviewed_at = None if undo else iso(utcnow())
        store.upsert_run(run)
    store.log(f"{'un-acknowledged' if undo else 'acknowledged'} "
              f"{run.status} run {persona.short(run.id)} ({run.name})", source="runner")
    return run.model_dump()


@app.post("/api/runs/reclassify")
def reclassify_runs() -> dict[str, Any]:
    """Backfill `error_kind` on failures recorded before the field existed.

    Without this the fix is retroactively a lie: every historical failure has
    `error_kind: None`, so `run.transient` is False for all of them and the board
    keeps showing 529s as ordinary failures needing attention. The alternative was
    telling the owner to acknowledge twelve cards, which papers over a misclassified
    history rather than correcting it, and loses the one genuine failure hiding among
    the transient ones.

    Reads each failed run's captured log and re-applies the same test `_apply_result`
    now uses. Idempotent, and only ever fills a field that is empty: a run already
    classified is left alone.
    """
    scanned = classified = 0
    with store.lock:
        runs = store.runs()
        for r in runs:
            if r.status not in ("failed", "orphaned") or r.error_kind:
                continue
            scanned += 1
            obj = detached._parse_result_json(r)
            if not obj:
                continue
            if (obj.get("terminal_reason") == "api_error"
                    or obj.get("api_error_status") is not None):
                r.error_kind = "api"
                classified += 1
        if classified:
            store.save_runs(runs)
    store.log(f"reclassified {classified} of {scanned} unclassified failure(s) as "
              f"upstream API errors", source="runner")
    return {"scanned": scanned, "classified": classified}


@app.post("/api/runs/ack-all")
def ack_all_runs(kind: str | None = None) -> dict[str, Any]:
    """Acknowledge every outstanding failure at once, optionally only one error kind.

    `kind=api` is the case this exists for: an upstream outage produces a burst of
    identical failures, and acknowledging them one at a time is busywork that teaches
    the owner to ignore the column instead.
    """
    acked: list[str] = []
    with store.lock:
        runs = store.runs()
        for r in runs:
            if r.status not in ("failed", "orphaned") or r.reviewed_at:
                continue
            if kind and r.error_kind != kind:
                continue
            r.reviewed_at = iso(utcnow())
            acked.append(persona.short(r.id))
        if acked:
            store.save_runs(runs)
    if acked:
        store.log(f"acknowledged {len(acked)} failed run(s)"
                  + (f" of kind '{kind}'" if kind else ""), source="runner")
    return {"acknowledged": acked, "count": len(acked)}


@app.get("/api/alerts")
def get_alerts() -> list[dict[str, Any]]:
    return [a.model_dump() for a in compute_alerts()]


# ---- decisions ---------------------------------------------------------------
# Append-only, so there is deliberately no PATCH and no DELETE here. Superseding
# is the only way to change one, and it is a POST that writes a new decision. An
# endpoint that let a decision be edited would make the log unable to answer the
# one question it exists for: what did you think at the time.

@app.get("/api/decisions")
def get_decisions(domain: str | None = None, include_superseded: bool = False,
                  q: str | None = None) -> list[dict[str, Any]]:
    if q:
        return [d.model_dump() for d in decisions.search(store, q)]
    items = store.decisions()
    if domain:
        items = [d for d in items if d.domain == domain]
    if not include_superseded:
        items = [d for d in items if d.live]
    return [d.model_dump() for d in items]


@app.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str) -> dict[str, Any]:
    d = store.get_decision(decision_id)
    if d is None:
        raise HTTPException(404, f"no decision {decision_id}")
    return d.model_dump()


@app.post("/api/decisions")
def post_decision(req: DecisionRequest) -> dict[str, Any]:
    try:
        d = decisions.build(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        store.add_decision(d)
    except ValueError as e:
        # A duplicate id means the identical decision (same title, date, and text)
        # is already recorded. Say so rather than appending a second copy.
        raise HTTPException(409, str(e)) from e
    store.log(f"decision {d.id} recorded: {d.title[:60]}", source="decision")
    return d.model_dump()


@app.patch("/api/decisions/{decision_id}/revisit-by")
def patch_revisit_by(decision_id: str, when: str | None = None) -> dict[str, Any]:
    """Set or clear the review date. The ONLY mutable field on a decision; see
    Store.set_revisit_by for why this one is an exception and the rest are not."""
    if when:
        try:
            datetime.fromisoformat(str(when)[:10])
        except ValueError:
            raise HTTPException(400, f"revisit_by must be an ISO date, got {when!r}")
    try:
        d = store.set_revisit_by(decision_id, when)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"decision {d.id} review date "
              + (f"set to {d.revisit_by}" if d.revisit_by else "cleared"),
              source="decision")
    return d.model_dump()


@app.post("/api/decisions/{decision_id}/supersede")
def post_supersede(decision_id: str, req: DecisionRequest) -> dict[str, Any]:
    try:
        new = decisions.build(**req.model_dump())
        old, new = store.supersede_decision(decision_id, new)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    store.log(f"decision {new.id} supersedes {old.id}: {new.title[:50]}",
              level="warn", source="decision")
    return {"superseded": old.model_dump(), "decision": new.model_dump()}


class PostPatch(BaseModel):
    status: str | None = None
    url: str | None = None
    note: str | None = None
    draft: str | None = None


class DraftRequest(BaseModel):
    note: str | None = None


@app.get("/api/writing")
def get_writing() -> dict[str, Any]:
    return writing.status(store)


@app.get("/api/writing/{post_id}")
def get_post(post_id: str) -> dict[str, Any]:
    p = store.get_post(post_id)
    if p is None:
        raise HTTPException(404, f"no post {post_id}")
    return p.model_dump()


@app.post("/api/writing/ideas")
def post_writing_ideas() -> dict[str, Any]:
    """Mine the window for post ideas now, rather than waiting for the weekly run."""
    try:
        run = writing.start_ideas(store)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, RuntimeError) as e:
        raise HTTPException(500, f"could not start the ideas run: {e}") from e
    store.upsert_run(run)
    store.log("writing: ideas run started", source="writing", run_id=run.id)
    return {"run": run.model_dump()}


@app.post("/api/writing/{post_id}/draft")
def post_writing_draft(post_id: str, req: DraftRequest | None = None) -> dict[str, Any]:
    """Draft one post, or redraft it with a note about what to change."""
    try:
        run, post = writing.start_draft(store, post_id, (req.note if req else None))
    except ValueError as e:
        code = 404 if str(e).startswith("no post") else 409
        raise HTTPException(code, str(e)) from e
    except (OSError, RuntimeError) as e:
        raise HTTPException(500, f"could not start the draft: {e}") from e
    store.upsert_run(run)
    store.log(f"writing: drafting {post.id} ({post.hook[:50]})", source="writing", run_id=run.id)
    return {"run": run.model_dump(), "post": post.model_dump()}


@app.patch("/api/writing/{post_id}")
def patch_post(post_id: str, req: PostPatch) -> dict[str, Any]:
    try:
        post = writing.set_status(store, post_id, req.status, url=req.url,
                                  note=req.note, draft=req.draft)
    except ValueError as e:
        code = 404 if str(e).startswith("no post") else 400
        raise HTTPException(code, str(e)) from e
    # Persist first, tell second; a failed event write must not 500 a landed edit.
    with contextlib.suppress(Exception):
        store.log(f"writing: {post.id} -> {post.status}", source="writing")
    return post.model_dump()


@app.get("/api/retire")
def get_retire() -> dict[str, Any]:
    """Retire candidates. Read-only by construction: there is no POST counterpart,
    because nothing in Otto deletes a card or a schedule on a timer."""
    return {"gaps": retire.gaps(store), "report": retire.render(store)}


# ---- dashboard --------------------------------------------------------------

if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def dashboard():
    index = WEB_DIR / "index.html"
    if not index.is_file():
        return JSONResponse({"error": "dashboard assets missing", "expected": str(index)}, 500)
    # no-cache means "revalidate every time", not "do not store": the ETag makes
    # that one cheap loopback round trip. Without it WebView2 applied heuristic
    # freshness to a file that had not changed in weeks and kept serving an
    # index.html from before term.js existed, while the freshly modified app.js
    # DID revalidate. The page then died on `OttoTerm is not defined` and the
    # desktop app blamed the daemon (2026-10-02).
    return FileResponse(str(index), headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    """Same rule for everything under /static: the files change whenever the repo
    does, and a dashboard half on new code and half on old is worse than a 304."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---- entrypoint -------------------------------------------------------------

def stop_daemon() -> tuple[bool, str]:
    """Stop the daemon WITHOUT touching the agents it spawned.

    This exists because `taskkill /PID <daemon> /T` kills the whole process tree,
    and every tracked agent is a child of the daemon. That destroyed a 27-minute
    AWS forensics run mid-flight. Terminating only the daemon pid leaves the
    children running, and the next daemon re-adopts them from runs.json via
    pid + create_time.
    """
    pid = _running_daemon_pid()
    if pid is None:
        return False, "daemon is not running"
    try:
        proc = psutil.Process(pid)
        proc.terminate()          # this pid only, never proc.children()
        proc.wait(timeout=10)
    except psutil.TimeoutExpired:
        return False, f"daemon {pid} did not exit within 10s"
    except psutil.Error as e:
        return False, f"could not stop daemon {pid}: {e}"
    _clear_pidfile()
    return True, f"stopped daemon {pid}; spawned agents left running"


def serve(host: str | None = None, port: int | None = None, force: bool = False) -> None:
    global _FORCED_START
    existing = _running_daemon_pid()
    if existing and not force:
        raise SystemExit(
            f"{config.PERSONA_NAME} daemon already running (pid {existing}). "
            f"Use --force to start anyway, or stop it first."
        )
    # lifespan runs inside uvicorn, so the decision has to travel via module state.
    _FORCED_START = force
    # A --port or --host other than config's would otherwise refuse the dashboard
    # it serves. Mutating the list in place works because the guard reads it when
    # Starlette builds the middleware stack, on the first request.
    for origin in default_origins(port or config.PORT, host or config.HOST):
        if origin not in config.ALLOWED_ORIGINS:
            config.ALLOWED_ORIGINS.append(origin)
    if host and host not in config.ALLOWED_HOSTS and host not in ("0.0.0.0", "::"):
        config.ALLOWED_HOSTS.append(host)
    uvicorn.run(
        app,
        host=host or config.HOST,
        port=port or config.PORT,
        log_level="warning",
        access_log=False,
    )
