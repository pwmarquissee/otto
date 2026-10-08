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
import gzip
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from starlette.middleware.gzip import GZipMiddleware

from . import (advisor, board, chat, config, configsync, dedupe,
               dispatch, feeds, herdr, journal, known, launch, logistics,
               notify, nudges, persona, refresh, registry, sessions, verdict)
# The assistant half (outreach, writing, people, prep, wellbeing, summon, inbox,
# meetings) is imported only under OTTO_SCOPE=assistant, further down, once the
# app exists. See otto/assistant/__init__.py for the boundary and why.
from . import assistant
from . import setup
from .models import Alert, Run, iso, utcnow
from .originguard import OriginGuard, default_origins
from .runners import detached, external, machine, scheduled
from .runners import herdrpane
from .store import Store

store = Store()

# Cadences for the tick loop's own periodic work, in seconds.
POLL_RUNS_EVERY = config.TICK_SECONDS
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
    finished_tasks: list[Run] = []
    finished_schedules: list[Run] = []
    finished_assistant: list[Run] = []
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
                elif config.ASSISTANT and _ahooks.owns(runs[i]):
                    finished_assistant.append(runs[i])
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

    # Meeting ingest and writing runs: the assistant's harvesters, which stamp
    # their own schedules. Only reached under the assistant scope, because only
    # that profile can have started such a run.
    for run in finished_assistant:
        notes.extend(_ahooks.harvest(run))

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
      ingest,  the assistant's two: the read-only meeting-notes parse and the
      writing  post-ideas miner. Both are the same risk class as refresh (read
               tools only, or no tools at all; the only writes are to Otto's own
               board and ledger). See otto/assistant/hooks.py; under the core
               profile a schedule with one of these runners is skipped.
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
    assistant_starts = _ahooks.Autostart(store.runs()) if config.ASSISTANT else None

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

        elif sched.runner in assistant.RUNNERS:
            if assistant_starts is None:
                continue  # core scope: nothing can run it, and nothing seeded it
            msg = assistant_starts.start(sched)
            if msg:
                notes.append(msg)

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

    if config.ASSISTANT:
        _ahooks.tick_prep()  # the meeting-prep toast

    # The unprompted nudges: threads gone quiet, a milestone approaching, cards past
    # their date. Each dedupes against the notice store, so this is a few list
    # comprehensions on a normal tick.
    try:
        for n in nudges.tick(store):
            store.log(n, source="nudges")
    except Exception as e:  # noqa: BLE001
        store.log(f"nudges error: {e}", level="warn", source="nudges")

    if config.ASSISTANT:
        _ahooks.tick_settle()  # outreach holds expiring; the hook says why it sits here

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

    if config.ASSISTANT:
        _ahooks.tick_ingest()  # dossier contacts, summons, the DM inbox

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
        setup.pin_integrations(store)
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

if config.ASSISTANT:
    # The only place the daemon imports the assistant modules. Under the core
    # profile these never load, their routes do not exist, and the tick hooks above
    # are never called; tests/test_scope.py checks all three from a subprocess.
    from .assistant import hooks as _ahooks  # noqa: E402  # after `app`, by design
    from .assistant import routes as _aroutes  # noqa: E402
    _ahooks.install(store, journal_age_hours=journal_age_hours)
    _aroutes.install(app, store)
web_events.register(app, store)
from . import api  # noqa: E402  # the routers read this module's store at call time
api.install(app)


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

def _slim_result(row: dict[str, Any]) -> None:
    """Cut a run row's `result_summary` for the state payload, in place. A run
    keeps up to detached.RESULT_CHARS of its final text (a PREPARE proposal is
    that text); the rail draws a line of it and GET /api/runs/{id} has the
    whole thing. `result_truncated` is present only when something was cut."""
    text = row.get("result_summary")
    if isinstance(text, str) and len(text) > config.STATE_DETAIL_CHARS:
        row["result_summary"] = text[: config.STATE_DETAIL_CHARS]
        row["result_truncated"] = True

def _slim_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the fields the dashboard never draws in full. Everything else is kept."""
    for row in payload.get("tasks") or []:
        _slim_detail(row)
    for row in payload.get("runs") or []:
        _slim_result(row)
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
        "day": store.get_day(datetime.now().astimezone().date().isoformat()),
        "day_prev": store.get_day(journal.yesterday().isoformat()),
        "machine": [m.model_dump() for m in machine.snapshot()] if config.MACHINE_PANEL else [],
        "config": configsync.summary(),
        "briefing": _cached_briefing(),
        "dispatch": dispatch.status(store),
        "logistics": logistics.view(store),
        "autorun": api.schedules.get_autorun(),
        "live": api.runs.get_live(),
        "sessions": api.sessions.list_sessions(),
        "events": [e.model_dump() for e in store.events(config.STATE_EVENTS)],
        # outreach and writing: the assistant's keys, empty shapes under core so the
        # dashboard's reads stay defined.
        **(_ahooks.state_extra() if config.ASSISTANT else assistant.STATE_EMPTY),
    }

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
