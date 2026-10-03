"""Talking to Otto directly.

Design constraint that shapes everything here: the daemon has no model of its own.
A reply comes from a real `claude -p` invocation, which takes seconds, so chat is
modelled as a tracked Run rather than a blocking HTTP call. Post a message, get a
run id, poll the thread. Same machinery that tracks spawned agents.

Safety: Otto's job is to answer about state, so the state is INJECTED into the
prompt and mutating tools are denied. A chat box that can run arbitrary Bash on a
managed endpoint is a remote shell with a friendly face; this one reads a snapshot
and hands back the command for you to run. `--dangerously-skip-permissions` is
never passed here.

Continuity comes from Claude Code itself: Otto assigns a session UUID on the first
turn (--session-id) and resumes it after (--resume), so the model keeps the real
conversation and this module only stores what the UI renders.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import config, persona, registry
from .models import Run, iso, utcnow
from .runners import detached, scheduled
from .store import Store

# Anything that could change the machine. Read/Glob/Grep are left alone: harmless,
# and occasionally useful for answering a question about a definition on disk.
DENIED_TOOLS = [
    "Bash", "Edit", "Write", "NotebookEdit", "Task", "WebFetch", "WebSearch",
    "KillShell", "BashOutput",
]

# The owner and the organization are named from config so the prompt reads
# naturally without the tree naming anyone. Resolved at import like every other
# config-derived constant; `_write_launcher` is what actually uses it.
_OWNER = config.OWNER_NAME
_ORG = config.ORG_NAME

SYSTEM_PROMPT = persona.character() + f"""
You cover two domains, work (the owner's role at {_ORG}) and personal, and never
merge them into one list. Name the domain.

You are answering from a state snapshot supplied in the user's message. That snapshot
is your only source of truth about the control plane. Two or three sentences unless
asked for more.

Rules:
- STALE means something silently stopped running. DUE means the cadence says it is
  time. Never conflate them.
- An `orphaned` run is an unknown outcome, not a failure: the session vanished
  without reporting, so there is no exit code.
- Token and cost figures come from Claude Code's own reporting. Never estimate or
  price tokens yourself.
- Integrations marked `mcp-only` are registered, not verified. Say so if it matters.
- Disk usage and days-offline are context, never action items. Do not tell {_OWNER}
  to act on them.
- You cannot run commands in this conversation. When an action is needed, give the
  exact command and stop. Do not claim to have done it.
- If the snapshot does not contain the answer, say what is missing rather than
  guessing.

COMMANDS. These are the ONLY commands that exist. Never invent one, and never
suggest a subcommand that is not listed here. If what {_OWNER} needs has no command,
say so plainly instead of inventing a plausible-looking one.

  otto status [--domain work|personal]   one-screen view
  otto board [--domain ...]              all outstanding work in columns
  otto chat "<question>"                 this conversation
  otto runs                              run history
  otto logs <run-id>                     a run's captured output
  otto spawn <name> --prompt "<task>" [--agent <type>] [--cwd <dir>] [--mode headless|windowed]
  otto kill <run-id>                     terminate a tracked run
  otto done <run-id>                     mark a run finished
  otto task add "<title>" [--domain ...] [--priority low|normal|high|urgent] [--detail ...]
  otto task ls | mv <id> <status> | set <id> --... | rm <id>
  otto schedule add <name> --command "<cmd>" --kind daily|weekly|every|manual
                    [--days sun,mon] [--at HH:MM] [--hours N]
                    [--max-age-hours N] [--min-interval-days N]
  otto schedule rm <name>
  otto schedules | otto due [--domain ...]
  otto stamp <name>                      record a successful run
  otto toggle <name> [--off]             enable/disable a schedule
  otto probe                             integration liveness
  otto agenda [--push agenda|mail]       calendar/mail snapshots
  otto machine                           host facts
  otto registry [--kind agent|skill|command|project] [--domain ...]
  otto scan | otto events | otto doctor | otto migrate | otto serve | otto say
  otto writing                           post ideas, drafts, and what was posted
  otto writing ideas                     mine the last week for post ideas now
  otto writing draft <id> [--note "..."] draft a post, or redraft it with a note
  otto writing edit <id>                 edit the draft by hand; the next draft run keeps your changes
  otto writing draft <id> --from-file f  your edited text, then another pass on it
  otto writing show <id>                 one post in full, with the scan's flags
  otto writing set <id> --status posted|dropped|idea|drafted [--url ...] [--note ...]

There is NO `otto run`. Schedules whose command starts with `/` are Claude Code
slash commands and need a session, so the way to run one is:

  otto spawn <name> --prompt "/<command>" --cwd {config.CHAT_CWD}

Schedules whose command is a shell command (a sync script, say) are run directly in
a terminal, not through otto."""


def _fmt_age(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    secs = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.1f}d ago"


def build_context(store: Store) -> str:
    """A compact state snapshot. Compact matters: this is prepended to every turn."""
    now_local = datetime.now().astimezone()
    lines: list[str] = [f"CURRENT STATE (generated {iso(utcnow())})", ""]

    lines.append("SCHEDULES")
    for s in store.schedules():
        stale = scheduled.staleness(s)
        due, reason = scheduled.is_due(s, now_local)
        flag = "STALE" if stale else ("DUE" if due else "idle")
        note = stale[1] if stale else reason
        lines.append(
            f"  [{s.domain}] {s.name}: {flag}, last {_fmt_age(s.last_run)}"
            f" ({note}), cmd={s.command}"
        )

    runs = store.runs()[:12]
    lines += ["", "RECENT RUNS"]
    if not runs:
        lines.append("  none")
    for r in runs:
        usage = ""
        if r.output_tokens:
            usage = f", {r.input_tokens or 0} in / {r.output_tokens} out"
        if r.cost_usd is not None:
            usage += f", ${r.cost_usd:.2f}"
        lines.append(
            f"  [{r.domain}] {r.id[:6]} {r.name}: {r.status},"
            f" started {_fmt_age(r.started)}{usage}"
        )

    lines += ["", "INTEGRATIONS"]
    for i in store.integrations():
        lines.append(f"  {i.name}: {'ok' if i.ok else 'NOT OK'} ({i.mode}) - {i.detail}")

    tasks = store.tasks()
    lines += ["", f"BOARD TASKS ({len(tasks)})"]
    if not tasks:
        lines.append("  none")
    for t in tasks[:25]:
        ref = f" {t.task_ref}" if t.task_ref else ""
        lines.append(f"  [{t.domain}] {t.status}/{t.priority}{ref}: {t.title}")

    snaps = store.snapshots()
    if snaps:
        lines += ["", "TODAY (pushed snapshots)"]
        # Domain is in the label, not just the key: work mail and personal mail are
        # different inboxes and must never be reported as one.
        for key in sorted(snaps):
            snap = snaps[key]
            lines.append(f"  [{snap.domain}] {snap.kind} ({_fmt_age(snap.fetched_at)}):"
                         f" {snap.summary or '-'}")
            for item in snap.items[:8]:
                when = item.get("when") or item.get("time") or ""
                title = item.get("title") or item.get("summary") or ""
                lines.append(f"    {when} {title}")

    summ = registry.summarize_by_domain(store.registry())
    lines += ["", "REGISTRY"]
    for domain, counts in summ.items():
        pretty = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"
        lines.append(f"  {domain}: {pretty}")

    return "\n".join(lines)


def _write_launcher(run_id: str, prompt_file: Path, session_id: str, resume: bool) -> Path:
    """A launcher script, for the same reason spawning agents needs one: the prompt
    and system prompt are far too long and too quoted to survive a command line."""
    # The owner's operating profile goes to CHAT and not into the persona brief, which
    # is injected into every Otto-spawned session. An EDR triage or a dossier ingest
    # has no use for how burnout presents on one person, and would pay for the tokens
    # on every run. Chat is the surface where the owner actually thinks out loud, so
    # it is the one that needs it. Appended after the state rules so the operating
    # guidance is the last thing read before the conversation starts.
    system = SYSTEM_PROMPT
    profile = config.profile_text()
    if profile:
        system += (
            f"\n\n---\n\nHOW TO WORK WITH {config.OWNER_NAME.upper()}. Written by them. "
            "This is not background color, it is instruction, and the parts telling you "
            "NOT to do something (not to take their depleted conclusions literally, not "
            "to reassure automatically, not to encourage career conclusions while they "
            "are burned out) matter more than the parts telling you what they are good "
            "at.\n\n"
            + profile
        )

    sys_file = config.LOG_DIR / f"{run_id[:6]}-chat.system.txt"
    sys_file.write_text(system, encoding="utf-8")

    q = detached._ps_quote
    model = config.CHAT_MODEL or config.DEFAULT_MODEL
    lines = [
        "$ErrorActionPreference = 'Continue'",
        f"Set-Location -LiteralPath {q(config.CHAT_CWD)}",
        f"$prompt = Get-Content -LiteralPath {q(prompt_file)} -Raw",
        "$claudeArgs = @('-p', '--output-format', 'json')",
        f"$claudeArgs += @('{'--resume' if resume else '--session-id'}', '{session_id}')",
        *([f"$claudeArgs += @('--model', {q(model)})"] if model else []),
        # By PATH, not by value: Windows PowerShell 5.1 strips or splits on embedded
        # quotes when it hands an argument to a native exe, and the profile is full
        # of them. See detached._write_launcher for the day this was found.
        f"$claudeArgs += @('--append-system-prompt-file', {q(sys_file)})",
        f"$claudeArgs += @('--disallowed-tools', {q(' '.join(DENIED_TOOLS))})",
        "$prompt | & claude @claudeArgs",
        "exit $LASTEXITCODE",
    ]
    launcher = config.LOG_DIR / f"{run_id[:6]}-chat.launch.ps1"
    launcher.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return launcher


def send(store: Store, message: str) -> tuple[Run, dict]:
    """Start a chat turn. Returns (run, thread) with the user message appended."""
    if not message.strip():
        raise ValueError("empty message")

    thread = store.chat()
    session_id = thread.get("session_id")
    resume = bool(session_id)
    if not session_id:
        session_id = str(uuid.uuid4())

    run_id = uuid.uuid4().hex
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    context = build_context(store)
    prompt_file = config.LOG_DIR / f"{run_id[:6]}-chat.prompt.txt"
    # Re-injected every turn: the resumed conversation carries the older snapshot,
    # and stale state is worse than repeated state.
    prompt_file.write_text(
        f"{context}\n\n---\n\n{config.OWNER_NAME} asks: {message.strip()}\n", encoding="utf-8"
    )

    launcher = _write_launcher(run_id, prompt_file, session_id, resume)
    log = config.LOG_DIR / f"{run_id[:6]}-chat.log"

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher)]
    fh = log.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=config.CHAT_CWD,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=detached._FLAGS_HEADLESS,
            shell=False,
        )
    finally:
        fh.close()

    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = None

    run = Run(
        id=run_id,
        name="chat",
        runner="detached",
        status="running",
        domain=config.WORK,
        pid=proc.pid,
        pid_created=created,
        cwd=str(config.CHAT_CWD),
        cmd=[str(c) for c in cmd],
        log=str(log),
        session_id=session_id,
        model=config.CHAT_MODEL or config.DEFAULT_MODEL or None,
        notes="mode=chat",
    )

    thread["session_id"] = session_id
    thread["messages"] = (thread.get("messages") or []) + [{
        "role": "user",
        "text": message.strip(),
        "at": iso(utcnow()),
        "run_id": run_id,
    }]
    store.save_chat(thread)
    return run, thread


def harvest(store: Store, run: Run) -> bool:
    """Turn a finished chat run into an assistant message.

    Called from the tick loop. Returns True if a message was appended.
    """
    if "mode=chat" not in (run.notes or ""):
        return False

    thread = store.chat()
    messages = thread.get("messages") or []
    if any(m.get("run_id") == run.id and m.get("role") == "assistant" for m in messages):
        return False  # already harvested

    obj = detached._parse_result_json(run)
    if obj is None:
        text = "That turn produced no result. The session ended without replying."
        ok = False
    else:
        text = (obj.get("result") or "").strip()
        ok = not obj.get("is_error")
        if not text:
            text = f"No reply text returned (subtype={obj.get('subtype')})."
            ok = False

    thread["messages"] = messages + [{
        "role": "assistant",
        "text": text,
        "at": iso(utcnow()),
        "run_id": run.id,
        "ok": ok,
        "cost_usd": run.cost_usd,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
    }]
    store.save_chat(thread)
    return True


def reset(store: Store) -> None:
    """Drop the thread and forget the session, starting a fresh conversation."""
    store.save_chat({"session_id": None, "messages": []})
