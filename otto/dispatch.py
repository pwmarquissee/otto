"""Running queued board tasks.

The contract: **Backlog means not ready, Queued means go.** Moving a card into
Queued dispatches it as a real Claude Code session with
`--dangerously-skip-permissions`, in a working directory, able to change things.

That makes the board a trigger rather than a view, which is worth being explicit
about. Everything here exists to bound it:

  * Only STORED tasks. A derived card (a due schedule parked in the queued column)
    is never auto-run. Queueing one task on purpose and unattended-launching
    `/daily` are different risks.
  * `auto=False` parks a task in queued forever without dispatching.
  * A failure moves the task to `needs-you`, it does NOT retry. Retry loops are
    how an agent runner turns into a bill.
  * Concurrency cap, plus a throttle so a bad state cannot spawn a burst.
  * A master switch (`TASK_AUTODISPATCH`) that makes queued inert.

Outcome detection is free: headless runs already report `is_error` and result text
through Claude Code's JSON output, so Otto does not need the task to self-report.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import config, findings
from .models import Run, Task, iso, utcnow
from .runners import detached
from .store import Store

_last_dispatch = 0.0


def model_for(task: Task) -> tuple[str | None, float | None]:
    """Pick (model, budget) for a task. Tagged `deep` gets the premium pair.

    Returned together because they are one decision, not two. The deep model is
    priced above Opus and is chosen for work expected to run long, so pairing it
    with the ordinary task budget would kill the run mid-work having already paid
    the premium for the thinking. Every caller that raises one must raise the other.
    """
    if config.DEEP_TAG and config.DEEP_TAG in task.tags:
        return (config.DEEP_MODEL or None, config.DEEP_BUDGET_USD or None)
    return (config.DEFAULT_MODEL or None, config.TASK_MAX_BUDGET_USD or None)

PROMPT_TEMPLATE = """You are running one task from the Otto board of {owner}.

TASK: {title}
{detail}{plan}
Domain: {domain}{agent_line}

Rules for this run:
- Do the task, then stop. Do not take on adjacent work you notice.
- This session is unattended, and DESTRUCTIVE actions need the live approval
  of {owner}: rotating, revoking, or creating credentials; changing anyone's
  account, identity, or access; deleting or mutating anything through an admin
  API; anything only the owner can reverse. A harness hook gates these:
  attempting one raises a yes/no dialog on the desktop of {owner}. If it is
  approved, proceed; if it is declined or unanswered, do not retry or route
  around it. Prepare the action, state the exact commands or clicks for
  {owner}, and stop. A "Needs you" card body is the owner's checklist, not
  yours, unless the dialog is approved.
  Before the dialog is raised, a gated SHELL command must carry three comment
  lines above it, one fact each, so {owner} approves a named action and not a
  string:
    # otto-gate: target=<exactly what changes, by name or id>
    # otto-gate: rollback=<how to undo it, or "none, " and why it cannot be>
    # otto-gate: authority=<the card id or quoted instruction that asked>
  Without them the call is denied and no dialog is shown. If you cannot state
  all three truthfully, do not run it; stage it for {owner} instead.
  Editing the files that decide whether checks pass (pytest.ini,
  pyproject.toml, lint and formatter config, conftest.py, CI workflows,
  CLAUDE.md, Claude Code settings and hooks) is gated the same way: the Code
  Floor says they do not get weakened to make a change pass. Fix the code, not
  the check.
  Non-destructive work (files, board updates, Gmail sends, tickets) does not
  prompt; just do it.
- Slack messages go out AS OTTO, never as {owner}. To message a colleague (a group
  DM that includes {owner}): write the text to a file, then
  `python -m otto dm <login-or-email> --text-file <file> --why "<what the task
  asked>"`. To report to {owner} alone: `python -m otto tell --text-file <file>`.
  To reply in an allowlisted channel thread: `python -m otto reply`. Do NOT call
  the Slack connector's send tools (`slack_send_message`, `slack_schedule_message`)
  for a DM: they post under the owner's own name and the hook denies them in this
  session.
  If `otto dm` refuses (person not in the roster, brake hit), say so and put the
  message text in your final paragraph instead of finding another route.
- If the task is ambiguous or you would have to guess at something that matters,
  do the part that is unambiguous and say clearly what you did not do and why.
- If it cannot be done at all, say so plainly and explain what blocked you. Do not
  invent a workaround that was not asked for.
- Finish with a short paragraph stating exactly what changed, including any file
  you wrote to. That paragraph is what gets recorded against the task.
"""


def active_task_runs(store: Store) -> list[Run]:
    return [
        r for r in store.runs()
        if r.status == "running" and "mode=task" in (r.notes or "")
    ]


def eligible(store: Store) -> list[Task]:
    """Queued tasks Otto is allowed to dispatch right now.

    Note the `attempts` check below doubles as the guard against a task that was
    queued BEFORE dispatch existed (or before it was switched on) being swept up
    the instant the daemon starts. It bit exactly once, on the first live run: a
    task parked in `queued` from earlier testing was dispatched on the first tick
    and began editing a live ops file. `otto autodispatch off` is the pre-flight
    if you ever have queued tasks of unknown vintage.
    """
    if not config.TASK_AUTODISPATCH:
        return []
    busy = {r.task_id for r in active_task_runs(store) if r.task_id}
    out = []
    for t in store.tasks():
        if t.status != "queued" or not t.auto:
            continue
        if t.id in busy:
            continue
        if t.attempts >= config.TASK_MAX_ATTEMPTS:
            continue  # already tried; it is sitting in needs-you or was reset
        out.append(t)
    # Highest priority first, then oldest, so a queue drains predictably.
    rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    out.sort(key=lambda t: (rank.get(t.priority, 9), t.created))
    return out


# The prepare run: everything a real run would need to decide, and nothing applied.
# What it writes back becomes the card's `plan`, which the owner edits and approves.
# This is the answer to "I have to pull Otto's assumptions apart afterwards": the
# assumptions are written down first, where they can be pulled apart before.
PREPARE_TEMPLATE = """You are PREPARING one task from the Otto board of {owner}, not doing it.

TASK: {title}
{detail}
Domain: {domain}{agent_line}

This card is tier-1: the work may be done but nothing may be applied without
{owner}. Your whole output is a proposal the owner will read, edit, and approve
or reject. Apply
NOTHING: no writes to production, identity, money, files outside a scratch dir,
or another person. Read-only investigation is the job.

Write the proposal as your final reply, in this shape and nothing else:

## What I found
The facts you verified, with where you looked. Not what the card says; what is true.

## Assumptions
Every assumption you would be making if you ran this, one per line. If {owner}
disagrees with one, this is where it gets crossed out.

## Plan
Numbered steps. Each step names the exact command, API call, or click, and what
it changes. Mark the irreversible ones.

## What this will not do
Adjacent work you noticed and deliberately left out.

## Open questions
Only questions whose answer changes the plan. If none, say none.

Keep it under 60 lines. A proposal that cannot be read in two minutes will not be read.
"""

ATTENDED_TEMPLATE = """You are working one card from the Otto board of {owner}, WITH {owner} AT THE KEYBOARD.

TASK: {title}
{detail}{plan}
Domain: {domain}{agent_line}

This is an attended session, opened by {owner} because this card needs a human's
hands or judgement, so work together: state what you are about to do before you do
it, ask when a choice is the owner's, and let the owner steer. Normal permission
prompts apply; do not route around one. When the work is finished, or the owner
stops, say plainly what was done and what was not, so the card can be closed on
evidence.
"""


def _plan_block(task: Task) -> str:
    if not task.plan:
        return ""
    stamp = (task.plan_approved or "")[:10]
    head = (f"\nAPPROVED PLAN ({config.OWNER_NAME} approved {stamp}). Follow it. If reality "
            "disagrees with a step, stop at that step and report; do not improvise "
            "past it:\n" if task.plan_approved else
            "\nDRAFT PLAN (not yet approved; treat as context, not instruction):\n")
    return head + task.plan.strip() + "\n"


def _prompt(task: Task) -> str:
    agent_line = f"\nYou are acting as the {task.agent} agent." if task.agent else ""
    detail = f"\n{task.detail.strip()}\n" if task.detail else ""
    if task.run_mode == "prepare":
        return PREPARE_TEMPLATE.format(
            title=task.title, detail=detail, domain=task.domain, agent_line=agent_line,
            owner=config.OWNER_NAME,
        )
    return PROMPT_TEMPLATE.format(
        title=task.title, detail=detail, plan=_plan_block(task),
        domain=task.domain, agent_line=agent_line, owner=config.OWNER_NAME,
    )


PANE_FOOTER = """
This task was handed to you by Otto's dispatcher into a herdr pane that {owner} can
see, read along with, or type into. The hand-off is recorded as card {task_id} in
`running`. When the task is complete, close it yourself so the board is true:
    python -m otto task mv {task_id} done
If it needs {owner} (a decision, an approval the gate refused, missing access), say
exactly what in your final paragraph and run:
    python -m otto task mv {task_id} needs-you
Then stop and wait; do not pick up other board cards on your own.
"""


def pane_prompt(task: Task) -> str:
    """The prompt for a logistics hand-off into a live herdr pane.

    The unattended rules apply in full in the PROMPT, because the owner being able
    to watch is not the same as the owner watching. They are not enforced by the
    guard hook here: scripts/otto_guard.py is a no-op without OTTO_UNATTENDED=1,
    and a herdr pane's claude is started by herdr.claude_command without it (and
    with --dangerously-skip-permissions by default). SECURITY.md records this. What differs is the ending: there is no Run and no JSON
    result to settle from, so the session is told to move its own card, and the
    pane watcher (logistics.watch) is the backstop if it does not.
    """
    return _prompt(task) + PANE_FOOTER.format(task_id=task.id[:8], owner=config.OWNER_NAME)


def attended_prompt(task: Task) -> str:
    """The prompt for `otto task open`: the owner is watching, so no unattended rules."""
    agent_line = f"\nYou are acting as the {task.agent} agent." if task.agent else ""
    detail = f"\n{task.detail.strip()}\n" if task.detail else ""
    return ATTENDED_TEMPLATE.format(
        title=task.title, detail=detail, plan=_plan_block(task),
        domain=task.domain, agent_line=agent_line, owner=config.OWNER_NAME,
    )


def dispatch(store: Store, task: Task, force: bool = False) -> tuple[Run | None, str]:
    """Spawn a task. Returns (run, message). Caller persists nothing else."""
    if not force and not config.TASK_AUTODISPATCH:
        return None, "auto-dispatch is off"
    if not force and task.attempts >= config.TASK_MAX_ATTEMPTS:
        return None, f"already attempted {task.attempts}x, needs a human"

    running = active_task_runs(store)
    if len(running) >= config.TASK_MAX_CONCURRENT:
        return None, f"at concurrency cap ({config.TASK_MAX_CONCURRENT})"

    cwd = task.cwd or str(config.TASK_DEFAULT_CWD)
    model, budget_usd = model_for(task)

    try:
        run = detached.spawn(
            name=task.title[:40],
            prompt=_prompt(task),
            cwd=cwd,
            agent=task.agent,
            mode="headless",
            task_id=task.id,
            tier=task.tier,
            skip_permissions=True,
            domain=task.domain,
            budget_usd=budget_usd,
            system_extra=findings.INSTRUCTIONS,
            model=model,
        )
    except (ValueError, OSError) as e:
        return None, f"could not spawn: {e}"

    # Tag it so poll_runs can route the outcome back to the task.
    run.notes = ((run.notes or "") + " | mode=task").strip(" |")

    with store.lock:
        fresh = store.get_task(task.id) or task
        fresh.status = "running"
        fresh.run_id = run.id
        fresh.attempts += 1
        fresh.last_error = None
        fresh.cwd = cwd
        fresh.touch()
        store.upsert_task(fresh)
        store.upsert_run(run)

    return run, f"dispatched {task.title[:40]} (pid {run.pid})"


def dispatch_due(store: Store) -> list[str]:
    """Tick hook: drain the queued column, within limits."""
    global _last_dispatch
    if not config.TASK_AUTODISPATCH:
        return []
    if time.time() - _last_dispatch < config.TASK_MIN_SECONDS_BETWEEN:
        return []

    notes: list[str] = []
    for task in eligible(store):
        if len(active_task_runs(store)) >= config.TASK_MAX_CONCURRENT:
            break
        run, msg = dispatch(store, task)
        notes.append(msg)
        if run is None:
            # A hard failure (bad cwd, missing agent) must not be retried on a
            # loop. Park it for a human with the reason attached.
            with store.lock:
                fresh = store.get_task(task.id)
                if fresh:
                    fresh.status = "needs-you"
                    fresh.last_error = msg
                    fresh.attempts += 1
                    fresh.touch()
                    store.upsert_task(fresh)
            break
        _last_dispatch = time.time()
    return notes


def settle(store: Store, run: Run) -> str | None:
    """A task run finished. Move the task to done or needs-you.

    Never back to queued: that would re-dispatch on the next tick and loop.
    """
    if "mode=task" not in (run.notes or "") or not run.task_id:
        return None

    attended = "mode=attended" in (run.notes or "")
    with store.lock:
        task = store.get_task(run.task_id)
        if task is None:
            return None
        if run.status == "ok" and task.run_mode == "prepare":
            # The proposal is the product. It lands where the owner will see it, as the
            # card's plan, unapproved. Approving it is what lets the card run.
            task.status = "needs-you"
            task.plan = run.result_summary or task.plan
            task.plan_approved = None
            task.run_mode = None
            task.result = None
            msg = f"{task.title[:36]} -> needs-you (proposal ready to approve)"
        elif run.status == "ok" and attended:
            # The owner closed the window. That is not evidence the work finished, so
            # the card asks rather than assumes: one click to done.
            task.status = "needs-you"
            task.result = run.result_summary
            task.run_mode = None
            msg = f"{task.title[:36]} -> needs-you (attended session ended)"
        elif run.status == "ok":
            task.status = "done"
            task.result = run.result_summary
            task.run_mode = None
            msg = f"{task.title[:36]} -> done"
        else:
            task.status = "needs-you"
            task.last_error = (
                f"run {run.id[:6]} ended {run.status}"
                + (f": {run.notes}" if run.notes else "")
            )
            task.result = run.result_summary
            msg = f"{task.title[:36]} -> needs-you ({run.status})"
        task.touch()
        store.upsert_task(task)

    # Anything the agent noticed but was not asked to do goes to the backlog.
    for note in findings.harvest(store, run, origin=task.title[:32]):
        msg += f" | {note}"
    return msg


def status(store: Store) -> dict:
    running = active_task_runs(store)
    return {
        "enabled": config.TASK_AUTODISPATCH,
        "running": len(running),
        "max_concurrent": config.TASK_MAX_CONCURRENT,
        "max_attempts": config.TASK_MAX_ATTEMPTS,
        # Reported, not left for a caller to assume. The dashboard states this cap
        # to the owner before dispatching, and a hardcoded number there would be a
        # claim about enforcement rather than a reading of it.
        "budget_usd": config.TASK_MAX_BUDGET_USD,
        "eligible": len(eligible(store)),
        "at": iso(utcnow()),
    }


def set_enabled(value: bool) -> bool:
    """Flip the master switch for this daemon's lifetime.

    Deliberately in-memory: a kill switch that survives a restart would let a
    forgotten `off` silently disable dispatch weeks later. Set the env var
    OTTO_AUTODISPATCH=0 for a persistent default.
    """
    config.TASK_AUTODISPATCH = bool(value)
    return config.TASK_AUTODISPATCH
