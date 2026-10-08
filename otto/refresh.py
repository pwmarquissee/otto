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

TWO PATHS, SAME SNAPSHOTS. A domain whose settings name a connector
(`OTTO_REFRESH_<DOMAIN>_CONNECTOR=google:<alias>`, see otto/connectors) is refreshed
in-process: a direct read of Gmail and Calendar under Otto's own read-only grant,
one small model call for the reply-or-awareness verdict, the same Snapshot shapes
written, the same schedule stamped. The Run it returns carries `runner="inline"`
and no pid; it completes itself from a worker thread, so poll_runs never has to
poll it. A domain with no connector takes the session path below, unchanged.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import requests

from . import config, launcher, notify
from .connectors import classify as _classify
from .connectors import google as _google
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
    denied = DENIED_TOOLS + list(config.REFRESH_SOURCES.get(domain, {}).get("deny") or [])
    model = config.REFRESH_MODEL or config.DEFAULT_MODEL
    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            "--disallowed-tools", " ".join(denied)]
    if model:
        args += ["--model", model]
    return launcher.write_claude(
        config.LOG_DIR / f"{run_id[:6]}-refresh",
        launcher.ClaudeSpec(cwd=str(config.CHAT_CWD), prompt_file=prompt_file, args=tuple(args)))


def start(store: Store, domain: str = config.WORK) -> Run:
    """Spawn the refresher for one domain. Returns a tracked Run; harvest() completes it.

    One run per domain, because the two inboxes sit behind different MCP servers and
    a single session fetching both would put personal mail and work mail in the same
    reply, which is where they would start getting merged.
    """
    if domain not in config.DOMAINS:
        raise ValueError(f"unknown domain {domain!r}")
    conn = connector_for(domain)
    if conn is not None:
        return start_connector(store, domain, conn)
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

    cmd = launcher.command(_launcher(run_id, prompt_file, domain))
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


# ---- the direct path -------------------------------------------------------------------

def connector_for(domain: str) -> dict[str, str] | None:
    """The connector a domain is switched to, or None for the session path."""
    conn = (config.REFRESH_SOURCES.get(domain) or {}).get("connector")
    if not isinstance(conn, dict) or not conn.get("kind"):
        return None
    return {"kind": str(conn["kind"]), "alias": str(conn.get("alias") or domain)}


def _connector_note(domain: str, conn: dict[str, str]) -> str:
    return f"mode=refresh domain={domain} connector={conn['kind']}:{conn['alias']}"


def start_connector(store: Store, domain: str, conn: dict[str, str],
                    *, session: requests.Session | None = None,
                    background: bool = True) -> Run:
    """The direct path. Preflight is synchronous so a missing grant comes back as the
    caller's "skipped" reason (and one notice) instead of a run that fails a second
    later; the read and the classification run on a thread.

    `background=False` runs the whole thing before returning, which is what the
    tests use and what a shell one-off may want.
    """
    if conn["kind"] != "google":
        raise RuntimeError(f"unknown connector kind {conn['kind']!r} for {domain}; only google is wired")
    sess = session or requests.Session()
    try:
        acct = _google.Account(conn["alias"], sess)
    except _google.GoogleError as e:
        _owner_notice(store, domain, conn, e)
        raise RuntimeError(str(e)) from e
    run = Run(
        id=uuid.uuid4().hex, name=f"refresh-{domain}", runner="inline", status="running",
        domain=domain, cwd=None, cmd=[], log=None,
        model=config.CLASSIFY_MODEL or None,
        notes=_connector_note(domain, conn),
    )
    if not background:
        return run_connector(store, run, domain, conn, acct)
    threading.Thread(target=run_connector, args=(store, run, domain, conn, acct),
                     daemon=True, name=f"otto-refresh-{domain}").start()
    return run


def _owner_notice(store: Store, domain: str, conn: dict[str, str], err: Exception) -> None:
    """One line telling the owner what to run. Deduped by notify.post, so a grant
    that stays broken for a day is one notice, not a notice per tick."""
    code = getattr(err, "code", "api")
    command = f"otto google auth {conn['alias']}" if code in ("no_token", "consent_expired") else None
    try:
        notify.post(store, f"{domain} refresh needs you",
                    body=str(err), level="warn", domain=domain, source="refresh",
                    command=command, key=f"refresh-connector-{domain}-{code}")
    except Exception:  # noqa: BLE001 - a notice failure must not mask the refresh error
        pass


def _hhmm(value: str | None) -> str | None:
    """A Google timestamp as local HH:MM; None for a date-only (all-day) value."""
    if not value or "T" not in value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return None


def agenda_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calendar rows in the snapshot shape the Today view and prep already read."""
    items = []
    for e in events[:10]:
        row: dict[str, Any] = {"title": str(e.get("title") or "")[:70]}
        if not e.get("all_day"):
            when, ends = _hhmm(e.get("start")), _hhmm(e.get("end"))
            if when:
                row["when"] = when
            if ends:
                row["ends"] = ends
        if e.get("role"):
            row["role"] = e["role"]
        if e.get("who"):
            row["who"] = ", ".join(e["who"])
        items.append(row)
    return items


def _sender(raw: str) -> str:
    """"Tim Hsu <tim@x>" -> "Tim Hsu"; a bare address stays an address."""
    raw = (raw or "").strip()
    if "<" in raw:
        name = raw.split("<", 1)[0].strip().strip('"')
        return name or raw[raw.find("<") + 1:].rstrip(">")
    return raw


def mail_items(messages: list[dict[str, Any]], verdicts: dict[str, Any]) -> list[dict[str, Any]]:
    """Mail rows in the snapshot shape, newest first, replies first within the cap."""
    rows = []
    for m in messages:
        v = verdicts.get(m["id"])
        needs = getattr(v, "needs", None) or "awareness"
        row: dict[str, Any] = {
            "title": f"{_sender(m.get('from', ''))}: {m.get('subject') or '(no subject)'}"[:70],
            "needs": needs,
        }
        when = _hhmm(m.get("at"))
        if when:
            row["when"] = when
        why = getattr(v, "why", None)
        if needs == "reply" and why:
            row["why"] = str(why)[:120]
        rows.append(row)
    rows.sort(key=lambda r: 0 if r["needs"] == "reply" else 1)
    return rows[:10]


def _wait_for_run(store: Store, run_id: str, timeout: float = 5.0) -> None:
    """The caller of start() records the Run after it returns. A worker that
    finished first would be overwritten by that record and read as running for
    ever, so the final write waits until the row exists."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.get_run(run_id) is not None:
            return
        time.sleep(0.05)


def run_connector(store: Store, run: Run, domain: str, conn: dict[str, str],
                  acct: Any, *, now: datetime | None = None) -> Run:
    """Read, classify, write the snapshots, stamp the schedule, close the run.
    Never raises: every failure becomes a failed Run plus one notice."""
    notes: list[str] = []
    try:
        start, end = _google.today_window(now)
        events = _google.calendar_events(acct, start=start, end=end)
        messages = _google.gmail_messages(acct, query=config.GOOGLE_MAIL_QUERY,
                                          max_results=config.GOOGLE_MAIL_MAX)
        verdict = _classify.classify(messages, owner=config.OWNER_NAME)
        agenda = agenda_items(events)
        mail = mail_items(messages, verdict.verdicts)
        with store.lock:
            store.put_snapshot(Snapshot(
                kind="agenda", domain=domain, source="otto-refresh",
                summary=(f"{len(agenda)} event(s) today" if agenda else "nothing on the calendar today"),
                items=agenda))
            store.put_snapshot(Snapshot(
                kind="mail", domain=domain, source="otto-refresh",
                summary=verdict.summary[:200] or None, items=mail))
            owner = next((sc.name for sc in store.schedules()
                          if sc.runner == "refresh" and sc.domain == domain), None)
            if owner:
                store.stamp(owner, "ok", run.id)
        run.status = "ok"
        run.exit_code = 0
        run.input_tokens = verdict.input_tokens
        run.output_tokens = verdict.output_tokens
        run.cost_usd = verdict.cost_usd
        replies = sum(1 for r in mail if r["needs"] == "reply")
        run.result_summary = (f"{len(agenda)} event(s), {len(messages)} thread(s), "
                              f"{replies} need a reply")[:400]
        notes.append(f"{domain}/agenda: {len(agenda)} item(s)")
        notes.append(f"{domain}/mail: {len(mail)} item(s)")
    except Exception as e:  # noqa: BLE001 - the tick must never die on a refresh
        run.status = "failed"
        run.exit_code = 1
        # Only an upstream fault is transient: a revoked grant will not heal by retrying.
        transient = isinstance(e, requests.RequestException) or (
            isinstance(e, _google.GoogleError) and e.code == "api")
        run.error_kind = "api" if transient else None
        run.notes = f"{run.notes or ''} | {type(e).__name__}: {str(e)[:200]}".strip(" |")
        run.result_summary = str(e)[:400]
        _owner_notice(store, domain, conn, e)
        with store.lock:
            owner = next((sc.name for sc in store.schedules()
                          if sc.runner == "refresh" and sc.domain == domain), None)
            if owner:
                store.stamp(owner, "failed", run.id)
    run.ended = iso(utcnow())
    _wait_for_run(store, run.id)
    with store.lock:
        store.upsert_run(run)
        for n in notes:
            store.log(n, source="refresh", run_id=run.id)
    return run


# ---- the session path's harvest ---------------------------------------------------------

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
