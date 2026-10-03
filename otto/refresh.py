"""Otto refreshing its own eyes on email and calendar.

The daemon has no MCP access. A headless `claude -p` session does: verified against
the claude.ai Gmail connector from a non-interactive run. So Otto refreshes itself by
spawning a short session whose only job is to fetch and report back.

This is the one place autostart is allowed. The rule elsewhere is that Otto surfaces
work as due and lets a human pull it, because silently launching agents with
skip-permissions on a timer is how a scheduler becomes a liability. A refresher is
different in kind: it reads two connectors, writes nothing but a snapshot, and every
mutating tool is denied. That is a narrow enough blast radius to run unattended.

Deliberately NOT a general "autostart any schedule" feature. Only this one runner is
auto-launchable, so adding a schedule can never accidentally arm something dangerous.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import psutil

from . import config
from .models import Run, Snapshot, iso, utcnow
from .runners import detached
from .store import Store

# Read-only: the refresher needs its MCP connectors and nothing else.
DENIED_TOOLS = ["Edit", "Write", "NotebookEdit", "Task", "KillShell"]

PROMPT = """You are Otto's refresher, fetching the %(domain)s domain. Output ONLY JSON.

%(sources)s

1. Today's calendar events.
2. Email threads from the last 24 hours that need %(owner)s's attention. Prefer unread
   or directly-addressed threads. Ignore newsletters, notifications, automated
   alerts, and marketing.

Read only. Do not send, draft, reply to, label, archive or delete anything. You are
taking a picture of the inbox, not acting on it.

Output EXACTLY this shape and nothing else, no prose, no code fence:

{"agenda": {"summary": "<one short line>",
            "items": [{"when": "HH:MM", "ends": "HH:MM", "title": "<short>",
                       "role": "organizer|required|optional",
                       "who": "<attendee emails, comma separated>"}]},
 "mail":   {"summary": "<one short line>",
            "items": [{"when": "HH:MM", "title": "<sender: subject, short>",
                       "needs": "reply|awareness", "why": "<short clause>"}]}}

Rules:
- Cap each list at 10 items, soonest or most recent first.
- Keep every title under 70 characters.
- If a source is empty, use an empty items list and say so in the summary.
- If a tool is unavailable, set that summary to "unavailable: <reason>" and items [].
- Never invent an item. An empty list is correct and useful; a fabricated one is not.

Calendar fields:
- `when` and `ends` are LOCAL 24-hour times. `ends` is what makes a double-booking
  detectable, so include it whenever the event has one. Omit `ends` for an all-day
  event and omit `when` too, rather than inventing 00:00.
- `role`: "organizer" if %(owner)s owns the invite, "optional" if he is an optional
  attendee, "required" otherwise. Omit it if the tool does not say. Do not guess
  from the title.
- `who`: the other attendees, EMAIL ADDRESSES preferred, comma separated. Omit
  %(owner)s himself. This is what meeting prep matches against the people dossiers, and
  an email is an exact match where a first name is a guess, so prefer the address
  even when the display name is nicer to read. Omit the field entirely rather than
  inventing attendees from the event title: prep falls back to reading the title
  itself and will report an ambiguity rather than name the wrong colleague.
  Skip it for events with no other attendees (focus blocks, Reclaim holds).

Mail triage (this is the part that matters most):
- `needs` is "reply" ONLY when a specific person is waiting on a response or an
  action from %(owner)s. A question addressed to him, a request, an approval, a document
  someone asked for.
- `needs` is "awareness" for everything else worth knowing: an FYI, a decision
  announced, a vendor touching base, a thread he is cc'd on that is proceeding fine
  without him.
- When you cannot tell, use "awareness". Never default to "reply". A list where
  "reply" means "maybe reply" is a list %(owner)s has to re-read from scratch every time,
  which defeats the entire point of classifying it.
- `why` is one short clause of evidence for a "reply", naming who is waiting and
  what for ("Tim asked for the kit spec, no answer yet"). Omit it for awareness
  items; the subject line is already the whole story there.
"""


def _launcher(run_id: str, prompt_file: Path, domain: str) -> Path:
    q = detached._ps_quote
    denied = DENIED_TOOLS + list(config.REFRESH_SOURCES.get(domain, {}).get("deny") or [])
    model = config.REFRESH_MODEL or config.DEFAULT_MODEL
    lines = [
        "$ErrorActionPreference = 'Continue'",
        f"Set-Location -LiteralPath {q(config.CHAT_CWD)}",
        f"$prompt = Get-Content -LiteralPath {q(prompt_file)} -Raw",
        "$claudeArgs = @('-p', '--output-format', 'stream-json', '--verbose')",
        "$claudeArgs += '--dangerously-skip-permissions'",
        f"$claudeArgs += @('--disallowed-tools', {q(' '.join(denied))})",
        *([f"$claudeArgs += @('--model', {q(model)})"] if model else []),
        "$prompt | & claude @claudeArgs",
        "exit $LASTEXITCODE",
    ]
    p = config.LOG_DIR / f"{run_id[:6]}-refresh.launch.ps1"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def start(store: Store, domain: str = config.WORK) -> Run:
    """Spawn the refresher for one domain. Returns a tracked Run; harvest() completes it.

    One run per domain, because the two inboxes sit behind different MCP servers and
    a single session fetching both would put personal mail and work mail in the same
    reply, which is where they would start getting merged.
    """
    if domain not in config.DOMAINS:
        raise ValueError(f"unknown domain {domain!r}")
    hint = (config.REFRESH_SOURCES.get(domain) or {}).get("hint") or ""
    if not hint.strip():
        raise RuntimeError(
            f"no refresh source configured for {domain}: set "
            f"config.REFRESH_SOURCES['{domain}']['hint'] once an MCP server for it "
            "is registered. Otto will not guess which connector to use."
        )

    run_id = uuid.uuid4().hex
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = config.LOG_DIR / f"{run_id[:6]}-refresh.prompt.txt"
    prompt_file.write_text(PROMPT % {"domain": domain, "sources": hint.strip(),
                                     "owner": config.OWNER_NAME},
                           encoding="utf-8")
    log = config.LOG_DIR / f"{run_id[:6]}-refresh.log"

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(_launcher(run_id, prompt_file, domain))]
    fh = log.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd, cwd=config.CHAT_CWD, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=detached._FLAGS_HEADLESS, shell=False,
        )
    finally:
        fh.close()

    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = None

    return Run(
        # domain was hardcoded to personal here while the only connector fetched the
        # work account, so every refresh run was filed under the wrong domain.
        id=run_id, name=f"refresh-{domain}", runner="detached", status="running",
        domain=domain, pid=proc.pid, pid_created=created,
        cwd=str(config.CHAT_CWD), cmd=[str(c) for c in cmd], log=str(log),
        model=config.REFRESH_MODEL or config.DEFAULT_MODEL or None,
        notes=f"mode=refresh domain={domain}",
    )


def _extract(payload: str) -> dict | None:
    """Pull the JSON object out of the model's reply.

    It is told to emit bare JSON, but a fence or a stray sentence is always possible,
    so fall back to bracket matching rather than failing the whole refresh.
    """
    payload = payload.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.split("\n", 1)[-1] if "\n" in payload else payload
    try:
        obj = json.loads(payload)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start_i, depth = payload.find("{"), 0
    if start_i < 0:
        return None
    for i, ch in enumerate(payload[start_i:], start_i):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            try:
                obj = json.loads(payload[start_i : i + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def harvest(store: Store, run: Run) -> list[str]:
    """Turn a finished refresh run into snapshots. Returns notes for the event log."""
    if "mode=refresh" not in (run.notes or ""):
        return []

    result = detached._parse_result_json(run)
    if result is None:
        return ["refresh produced no result"]
    if result.get("is_error"):
        return [f"refresh failed: {result.get('subtype') or 'error'}"]

    obj = _extract(result.get("result") or "")
    if obj is None:
        return ["refresh reply was not JSON, snapshots left unchanged"]

    notes: list[str] = []
    for kind in config.SNAPSHOT_KINDS:
        body = obj.get(kind)
        if not isinstance(body, dict):
            continue
        raw_items = body.get("items")
        items = [i for i in raw_items if isinstance(i, dict)] if isinstance(raw_items, list) else []
        # run.domain, never a default: writing a personal inbox into the work
        # snapshot key is exactly the overwrite this keying exists to prevent.
        store.put_snapshot(Snapshot(
            kind=kind,
            domain=run.domain,
            summary=str(body.get("summary") or "")[:200] or None,
            items=items[:10],
            source="otto-refresh",
        ))
        notes.append(f"{run.domain}/{kind}: {len(items)} item(s)")
    return notes or ["refresh returned nothing usable"]
