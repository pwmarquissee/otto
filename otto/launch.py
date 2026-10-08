"""Launching a schedule on demand.

A schedule's `command` is one of two very different things, and running it correctly
means telling them apart:

  slash command (`/daily`)   needs a Claude Code session. Spawned headless with
                             skip-permissions, tracked like any other run.
  shell command              run directly, output captured to a log.

Getting this wrong is the difference between a working button and one that shells out
to a file called `/daily`.

This is user-initiated only. Nothing here is on a timer: `autostart` remains limited
to the read-only refresher. Clicking Run in the dashboard is an explicit act, which is
exactly the authorization that a schedule running /triage and /orchestrate under
skip-permissions needs.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid
from datetime import datetime, timezone

import psutil

from . import config, feeds, findings, launcher
from .models import Run, Schedule, iso, utcnow
from .runners import detached
from .store import Store


def active_for(store: Store, name: str) -> Run | None:
    """An in-flight run for this schedule, so a double click cannot double-launch."""
    for r in store.runs():
        if r.status == "running" and r.name == name:
            return r
    return None


def is_slash(command: str) -> bool:
    return (command or "").strip().startswith("/")


def _frontmatter_model(command: str) -> str | None:
    """The model a slash command pins in its own frontmatter, if any.

    Otto PROMOTES this to `--model`, because a headless `claude -p '/name'` run
    ignores the frontmatter entirely. Measured from Otto's own logs on 2026-08-12:
    slack-sweep.md has declared `model: claude-sonnet-5` since it was written, and
    across 103 runs its `modelUsage` reports claude-fable-5, claude-opus-5[1m], and
    claude-sonnet-5 in the proportions of whatever the session default happened to
    be at the time. The pin never took effect once.

    That is also how Fable came to serve ~$492 of work between 2026-08-05 and
    2026-08-11 with no reference to it anywhere in Otto's config: the harness
    default drifted, and every schedule silently followed it. Passing an explicit
    `--model` on every spawn is what stops that recurring, so the rule is:

        schedule command's own pin, else config.DEFAULT_MODEL, always passed.
    """
    name = (command or "").strip().lstrip("/").split()[0] if command.strip() else ""
    if not name:
        return None
    for base in (config.OTTO_REPO / "claude" / "commands", config.CLAUDE_DIR / "commands"):
        path = base / f"{name}.md"
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        # Several command files start with a UTF-8 BOM (heartbeat.md, s1-triage.md),
        # which would make a plain startswith("---") miss the frontmatter entirely
        # and silently override their model.
        head = head.lstrip("﻿")
        if not head.startswith("---"):
            continue
        body = head[3:]
        end = body.find("\n---")
        block = body[:end] if end != -1 else body
        for ln in block.splitlines():
            if ln.strip().startswith("model:"):
                value = ln.split(":", 1)[1].strip().strip("\"'")
                if value:
                    return value
    return None


def autorun_blocked(store: Store, sched: Schedule) -> str | None:
    """Why this schedule must NOT be auto-run right now, or None if it may.

    Every check here exists because the alternative is an unattended agent doing
    something expensive at 3am with nobody watching.
    """
    if not config.SCHEDULE_AUTORUN:
        return "autorun master switch is off"
    if not sched.enabled:
        return "schedule is disabled"
    if not sched.autostart or sched.runner != "launch":
        return "not armed for autorun"
    if sched.disabled_reason:
        return f"circuit breaker tripped: {sched.disabled_reason}"
    if sched.consecutive_failures >= config.SCHEDULE_MAX_FAILURES:
        return f"{sched.consecutive_failures} consecutive failures"

    running = [r for r in store.runs()
               if r.status == "running" and "mode=schedule" in (r.notes or "")]
    if len(running) >= config.SCHEDULE_MAX_CONCURRENT:
        return f"at autorun concurrency cap ({config.SCHEDULE_MAX_CONCURRENT})"

    # Floor between two autoruns of the same schedule, independent of cadence, so a
    # cadence bug cannot produce a tight relaunch loop.
    last = sched.last_autorun
    if last:
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00"))
            gap = (datetime.now(timezone.utc) - when).total_seconds() / 60
            if gap < config.SCHEDULE_MIN_GAP_MINUTES:
                return f"last autorun {gap:.0f}m ago, floor is {config.SCHEDULE_MIN_GAP_MINUTES}m"
        except ValueError:
            pass
    return None


def launch(store: Store, sched: Schedule, autorun: bool = False) -> tuple[Run, str]:
    """Run a schedule now. Raises ValueError with a usable message on refusal."""
    existing = active_for(store, sched.name)
    if existing is not None:
        raise ValueError(
            f"{sched.name} is already running (run {existing.id[:6]}, pid {existing.pid})"
        )

    command = (sched.command or "").strip()
    if not command:
        raise ValueError(f"{sched.name} has no command to run")

    if is_slash(command):
        pinned = _frontmatter_model(command)
        cmd_name = command.strip().lstrip("/").split()[0]
        # A sweep gets told how far back it still needs to read, when Otto can prove
        # the last run covered the rest. Appended to the findings instructions rather
        # than replacing them: both are system-level context and a producer needs
        # both. None when there is no usable watermark, which leaves the command's
        # own documented window in charge.
        extra = findings.INSTRUCTIONS
        feed_name = config.SWEEP_FEED_COMMANDS.get(cmd_name)
        if feed_name:
            hint = feeds.coverage_hint(store, feed_name)
            if hint:
                extra = f"{extra}\n\n{hint}"
        run = detached.spawn(
            name=sched.name,
            prompt=command,
            cwd=str(config.HOME),
            agent=None,
            mode="headless",
            skip_permissions=True,
            domain=sched.domain,
            system_extra=extra,
            # A hand-launched run is watched, so it stays uncapped: cutting /daily
            # off mid-chain leaves partial state, which is worse than the spend.
            # An UNATTENDED run has nobody to notice it running away, so it gets a
            # ceiling. Generous enough that a normal /daily finishes.
            budget_usd=(config.SCHEDULE_BUDGET_USD or None) if autorun else None,
            # The loops (/triage, /orchestrate, /observe) are the work
            # that most needs judgement, so they get the default rather than
            # inheriting whatever settings.json happens to say. No deep-tag check:
            # a schedule is a command, not a card, so it has no tags to read.
            #
            # A command's own pin wins, but it has to be passed as a flag to mean
            # anything at all: see _frontmatter_model. Never left unset, because
            # unset means "inherit the harness default", which is the drift that
            # put Fable on slack-sweep for a week.
            model=pinned or config.DEFAULT_MODEL or None,
            # 28k of cached prefix, re-read every turn, for servers this command
            # never calls. See config.MCP_FREE_COMMANDS.
            local_mcp=cmd_name not in config.MCP_FREE_COMMANDS,
        )
        run.notes = ((run.notes or "") + " | mode=schedule"
                     + (" | autorun" if autorun else "")).strip(" |")
        store.upsert_run(run)
        if autorun:
            _stamp_autorun(store, sched.name)
        how = "autorun" if autorun else "launched"
        return run, f"{how} {sched.name} ({command}) as run {run.id[:6]}"

    # ---- shell command ----
    run_id = uuid.uuid4().hex
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = config.LOG_DIR / f"{run_id[:6]}-{sched.name}.log"

    # Wrapped so the poller has an exit code to read; launcher.write_shell says why.
    script = launcher.write_shell(config.LOG_DIR / f"{run_id[:6]}-{sched.name}",
                                  str(config.HOME), command)
    argv = launcher.command(script)

    fh = log.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(config.HOME),
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=detached._FLAGS_HEADLESS,
            shell=False,
        )
    except (OSError, ValueError) as e:
        fh.close()
        raise ValueError(f"could not start it: {e}") from e
    finally:
        if not fh.closed:
            fh.close()

    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = None

    run = Run(
        id=run_id,
        name=sched.name,
        runner="detached",
        status="running",
        domain=sched.domain,
        pid=proc.pid,
        pid_created=created,
        cwd=str(config.HOME),
        cmd=argv,
        log=str(log),
        notes="mode=schedule | shell" + (" | autorun" if autorun else ""),
    )
    store.upsert_run(run)
    if autorun:
        _stamp_autorun(store, sched.name)
    how = "autorun" if autorun else "launched"
    return run, f"{how} {sched.name} (shell) as run {run.id[:6]}"


def settle(store: Store, run: Run) -> str | None:
    """A schedule run finished: stamp the ledger so the alarm clears.

    This is the whole point of launching from the dashboard. Without the stamp the
    schedule would still read STALE after a successful run, which is the exact trap
    the daily.md ledger migration existed to close.
    """
    if "mode=schedule" not in (run.notes or ""):
        return None
    sched = store.get_schedule(run.name)
    if sched is None:
        return None
    if run.status == "ok":
        store.stamp(sched.name, "ok", run.id)
        _reset_failures(store, sched.name)
        msg = f"{sched.name} completed and stamped"
        for note in findings.harvest(store, run, origin=sched.name):
            msg += f" | {note}"
        return msg
    # A shell command that exits 0 counts, since there is no JSON result to read.
    if "shell" in (run.notes or "") and run.exit_code == 0:
        store.stamp(sched.name, "ok", run.id)
        _reset_failures(store, sched.name)
        return f"{sched.name} completed (exit 0) and stamped"
    # NOT stamp(): that would reset the staleness clock and make a schedule which
    # fails on every run look permanently fresh.
    store.mark_attempt(sched.name, run.status, run.id)
    msg = (f"{sched.name} ended {run.status}; staleness clock NOT reset, "
           f"it still reads as overdue")
    if run.transient:
        msg += " (upstream API error, not counted toward the autorun breaker)"

    tripped = _record_failure(store, sched.name, transient=run.transient)
    if tripped:
        msg += f" | AUTORUN DISABLED after {tripped} consecutive failures"
    return msg


def _stamp_autorun(store: Store, name: str) -> None:
    with store.lock:
        sched = store.get_schedule(name)
        if sched is None:
            return
        sched.last_autorun = iso(utcnow())
        store.upsert_schedule(sched)


def _reset_failures(store: Store, name: str) -> None:
    with store.lock:
        sched = store.get_schedule(name)
        if sched is None or (sched.consecutive_failures == 0 and not sched.disabled_reason):
            return
        sched.consecutive_failures = 0
        sched.disabled_reason = None
        store.upsert_schedule(sched)


def _record_failure(store: Store, name: str, transient: bool = False) -> int | None:
    """Count a failure. Returns the count if this tripped the breaker.

    A `transient` failure is NOT counted. The breaker exists to stop a BROKEN
    schedule from relaunching unattended forever; a schedule that would have worked
    if upstream had answered is not broken, and disarming it punishes the wrong
    thing. On 2026-08-05 a burst of 529 Overloaded errors put three of these on the
    board in an hour, and only interleaved successes kept the counter from reaching
    SCHEDULE_MAX_FAILURES and disarming heartbeat and slack-sweep outright. Surviving
    that by luck is not surviving it.

    The staleness clock is deliberately still NOT reset for a transient failure (see
    settle): the work genuinely did not happen, and a schedule that has not produced
    in eight hours must keep reading as overdue whatever the reason. Not counted
    toward the breaker, not forgiven on the alarm.
    """
    with store.lock:
        sched = store.get_schedule(name)
        if sched is None:
            return None
        if transient:
            return None
        sched.consecutive_failures += 1
        tripped = None
        if (sched.autostart and sched.runner == "launch"
                and sched.consecutive_failures >= config.SCHEDULE_MAX_FAILURES):
            # Disarm autorun, but leave the schedule itself enabled so it still
            # reports as due and stale. Silence would be the worse failure.
            sched.autostart = False
            sched.disabled_reason = (
                f"auto-disarmed after {sched.consecutive_failures} consecutive failures")
            tripped = sched.consecutive_failures
        store.upsert_schedule(sched)
        return tripped
