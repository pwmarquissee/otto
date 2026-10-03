"""Notion AI meeting notes, turned into board cards.

Notion AI writes a page per meeting. The useful half of that page is not the
summary, it is the six lines that say who agreed to do what by when. Those lines
were only ever actioned if the owner reread the page, which means the ones that
mattered were the ones he happened to remember. This module closes that loop:
every new meeting page is read once, the commitments that are HIS are filed onto
the board with the due date the meeting actually stated, and the rest are counted
so nothing vanishes quietly.

Shaped exactly like refresh.py, for the same reason: the daemon has no MCP access,
a headless `claude -p` session does. So Otto spawns a short session whose only job
is to read Notion and hand back JSON.

Three things this deliberately does NOT do:

  * It does not queue work. An action item is a claim a model extracted from a
    transcript another model wrote; between "somebody said a thing" and "an agent
    ran with skip-permissions" there would be two inference steps and no human.
    Cards land in `backlog` with a tier, and /orchestrate is the gate.
    MEETINGS_AUTOQUEUE exists, defaults off, and should stay off.
  * It does not write to Notion. Every mutating tool is outside the allow-list AND
    on the denylist. Notes are the record of the meeting, not Otto's scratchpad.
  * It does not invent a due date. A made-up deadline is worse than no deadline,
    because the board stops being believable the first time one is wrong.

Idempotence is by PAGE ID, not by watermark. The watermark is only a query bound
to keep the search cheap; the ledger of consumed pages is what actually guarantees
a meeting is parsed once. A page is marked consumed only when every action item it
produced was filed, so a run that hits the per-run cap leaves the remainder for the
next one rather than swallowing it.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from datetime import date, timedelta
from pathlib import Path

import psutil

from . import config, dedupe, findings, notify
from .models import Run, Task, iso, utcnow
from .runners import detached
# Reused deliberately rather than copied: a model told to emit bare JSON will
# occasionally wrap it in a fence anyway, and having two implementations of that
# recovery means fixing the next surprise twice.
from .refresh import _extract as extract_json
from .store import Store

# Pages consumed per run. Bounds the cost of a first run, or of a run after the
# daemon was down for a fortnight. Whatever is left over is reported and picked up
# on the next pass, so the ceiling delays work rather than dropping it.
MAX_PAGES_PER_RUN = 6

# Both server prefixes on purpose: the connector answers as `notion` in Claude Code
# today, and `claude_ai_Notion` is what the account-level registry calls it. Denying a
# name that does not exist costs nothing; missing one that does costs a write to the
# record of a meeting.
_NOTION_WRITES = [
    "notion-create-pages", "notion-update-page", "notion-create-comment",
    "notion-create-database", "notion-create-view", "notion-update-view",
    "notion-update-data-source", "notion-move-pages", "notion-duplicate-page",
    "notion-create-attachment", "notion-create-file-upload",
    "notion-download-attachment",
]

# THE control, and the only one.
#
# `--allowed-tools` was tried first as the primary restriction and MEASURED: under
# `--dangerously-skip-permissions` it restricts nothing, and the session came up
# holding all 249 tools including every Notion write. It is not used here, because a
# control that does not control is worse than no control: it only moves where you stop
# looking. `--disallowed-tools` was verified to work in the same run, and with this
# list the session comes up with 94 tools and exactly nine Notion ones, all reads.
#
# So this list has to be RIGHT rather than well-intentioned. Widening it is not a
# settings change, it is a re-answer to "is this still narrow enough to run
# unattended?" -- see claude/hardening/global/otto-module/meetings/HARDENING.md.
DENIED_TOOLS = [
    # Local mutation.
    "Edit", "Write", "NotebookEdit", "Task", "KillShell", "Bash",
    # Local reads. The job is "read Notion, emit JSON" and needs none of them. The
    # first smoke run reached for Read on a session transcript, which has nothing to
    # do with a meeting note.
    "Read", "Glob", "Grep", "LSP",
    # Egress and side effects. WebFetch matters most: meeting notes carry unreleased
    # feature names and personnel detail, and an outbound fetch is how that leaves the
    # building. The scheduling and messaging tools are here because a read-only job
    # that can create a cron entry is not a read-only job.
    "WebFetch", "WebSearch", "CronCreate", "CronDelete", "PushNotification",
    "RemoteTrigger", "SendMessage", "DesignSync", "Artifact",
    *[f"mcp__notion__{t}" for t in _NOTION_WRITES],
    *[f"mcp__claude_ai_Notion__{t}" for t in _NOTION_WRITES],
]
# ToolSearch is deliberately NOT denied: the Notion tool schemas are deferred, and
# without it the session cannot see the tools it is supposed to use at all.

VALID_TIERS = {"tier-0-autonomous", "tier-1-approval", "tier-2-assistive"}

PROMPT = """You are Otto's meeting-notes ingester. Output ONLY JSON.

Today is %(today)s. The person you work for is %(owner)s (%(owner_email)s).

Read Notion AI meeting notes %(window)s and extract the action items that are HIS.
Use notion-query-meeting-notes first; notion-search and notion-fetch are there to
read a page body when the query result is not enough.

Already ingested, SKIP these page ids entirely:
%(seen)s

Process at most %(max_pages)d pages, OLDEST FIRST. If there are more, ignore the
rest silently, they will be picked up on the next run.

For each page, pull out every commitment, decision-with-a-consequence, and
follow-up. For each one decide:

  mine=true   %(owner)s owns it, or nobody was named and it is Enterprise
              Technology & Security work (identity, endpoints, security, AWS,
              tooling, access, licences, vendors)
  mine=false  someone else owns it AND %(owner)s has nothing to do

An item someone else owns that %(owner)s has to chase, unblock, approve, or
provide access for IS his: mine=true, and say whose it is in the detail.

Assign each a risk tier, using these exactly:
  tier-0-autonomous  read-only or trivially reversible: a log query, an audit, a
                     cost check, a lookup, gathering context
  tier-1-approval    the work is clear but it CHANGES something: a policy, an
                     account, a group, a config, a spend commitment
  tier-2-assistive   needs judgement, a decision, or a conversation with a human.
                     Anything involving people, money, or an external party.
When torn, pick the higher tier. Guessing low is the expensive mistake.

Due dates: ONLY when the meeting stated one. Resolve relative wording ("by
Friday", "before the demo", "end of month") against the meeting's own date, which
you have. Output YYYY-MM-DD. If no date was stated, use null. Never estimate one.

Output EXACTLY this shape and nothing else, no prose, no code fence:

{"meetings": [{"page_id": "<the bare 32-character page id, NOT a url>",
               "title": "<short>", "when": "YYYY-MM-DD",
               "url": "<page url or null>", "action_count": <int>}],
 "actions":  [{"page_id": "<the page it came from>",
               "title": "<short imperative, under 80 chars>",
               "detail": "<what was said and why it matters, 1-3 sentences>",
               "quote": "<the line from the notes, verbatim, under 200 chars>",
               "owner": "<who owns it, or null>",
               "mine": true,
               "priority": "low|normal|high|urgent",
               "tier": "tier-0-autonomous|tier-1-approval|tier-2-assistive",
               "due": "YYYY-MM-DD or null",
               "domain": "work|personal"}],
 "skipped":  <count of action items you saw that were NOT his>,
 "summary":  "<one short line: N meetings read, M items for him>"}

Rules:
- Read only. Do not create, update, comment on, move, or duplicate anything in
  Notion. You are reading the record of a meeting, not editing it.
- Every action MUST carry a page_id matching one in `meetings`, or it is dropped.
- `quote` is what makes a card trustworthy six weeks later. Keep it verbatim.
- Never invent an action item. A meeting that produced none is normal and
  correct: return the page in `meetings` with action_count 0.
- If no meeting notes are reachable, return empty lists and set summary to
  "unavailable: <reason>". Do not guess at what the meetings might have said.
- If a page is a draft with no content yet, skip it and do not list it in
  `meetings`, so it gets read once it is written.
"""


def _window(store: Store) -> tuple[str, str | None]:
    """The query bound, as prose for the prompt, plus the watermark it came from."""
    wm = store.meetings().get("watermark")
    if wm:
        return f"created or updated since {wm[:10]}", wm
    since = (date.today() - timedelta(days=config.MEETINGS_FIRST_RUN_DAYS)).isoformat()
    return (f"from the last {config.MEETINGS_FIRST_RUN_DAYS} days (since {since}); "
            "this is the first run, so do not go back further"), None


def _build_prompt(store: Store) -> str:
    seen = sorted(store.seen_pages())
    # Bounded: the ledger holds up to 400 ids and pasting all of them would be most
    # of the prompt. The newest are the ones a query for recent pages can actually
    # return, and harvest() re-checks the full ledger anyway, so a truncated hint
    # costs a wasted read at worst and can never cause a duplicate card.
    hint = "\n".join(f"  {p}" for p in seen[-60:]) or "  (none yet)"
    window, _ = _window(store)
    return PROMPT % {
        "today": date.today().isoformat(),
        "owner": config.MEETINGS_OWNER,
        "owner_email": config.MEETINGS_OWNER_EMAIL,
        "window": window,
        "seen": hint,
        "max_pages": MAX_PAGES_PER_RUN,
    }


def _launcher(run_id: str, prompt_file: Path) -> Path:
    q = detached._ps_quote
    lines = [
        "$ErrorActionPreference = 'Continue'",
        f"Set-Location -LiteralPath {q(config.CHAT_CWD)}",
        f"$prompt = Get-Content -LiteralPath {q(prompt_file)} -Raw",
        "$claudeArgs = @('-p', '--output-format', 'stream-json', '--verbose')",
        "$claudeArgs += '--dangerously-skip-permissions'",
        # The one control that was measured to actually restrict this session. See
        # DENIED_TOOLS above for why there is no allow-list here.
        f"$claudeArgs += @('--disallowed-tools', {q(' '.join(DENIED_TOOLS))})",
        *([f"$claudeArgs += @('--max-budget-usd', '{config.MEETINGS_BUDGET_USD}')"]
          if config.MEETINGS_BUDGET_USD else []),
        *([f"$claudeArgs += @('--model', {q(config.MEETINGS_MODEL)})"]
          if config.MEETINGS_MODEL else []),
        "$prompt | & claude @claudeArgs",
        "exit $LASTEXITCODE",
    ]
    p = config.LOG_DIR / f"{run_id[:6]}-meetings.launch.ps1"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def start(store: Store) -> Run:
    """Spawn the ingester. Returns a tracked Run; harvest() completes it."""
    run_id = uuid.uuid4().hex
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    prompt_file = config.LOG_DIR / f"{run_id[:6]}-meetings.prompt.txt"
    prompt_file.write_text(_build_prompt(store), encoding="utf-8")
    log = config.LOG_DIR / f"{run_id[:6]}-meetings.log"

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(_launcher(run_id, prompt_file))]
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
        id=run_id, name="meeting-notes", runner="detached", status="running",
        domain=config.WORK, pid=proc.pid, pid_created=created,
        cwd=str(config.CHAT_CWD), cmd=[str(c) for c in cmd], log=str(log),
        model=config.MEETINGS_MODEL or None,
        notes="mode=ingest kind=meetings",
    )


# ---- harvest ----------------------------------------------------------------

# Boundary-anchored on purpose. An unanchored search matched one character early on
# `notion.so/Finance-Review-<id>`, because stripping the hyphen let the `e` on the
# end of the slug join the id -- a near-miss key, which is worse than no key at all.
_UUID32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def _page_key(raw: object) -> str:
    """A page id that means the same thing on every run.

    Two consecutive live runs returned the SAME page as `3ab1e0e4...` and as
    `https://app.notion.com/p/3ab1e0e4...`. Both are correct answers to "the page
    id" and they are not equal, so keying the ledger on whichever arrived would let
    one meeting be consumed twice and file every commitment in it again. Normalising
    is what makes "parsed exactly once" true rather than merely intended.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    # A URL carries the id in its last path segment, behind any title slug and in
    # front of any query string.
    seg = text.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    for candidate in (seg, seg.replace("-", "")):
        m = _UUID32.search(candidate)
        if m:
            return m.group(0)
    # Fall back to the segment rather than to nothing: an id in a shape we have never
    # seen still dedupes against itself, which is most of the value.
    return (seg or text)[:120]


def _valid_due(raw: object, meeting_when: str | None) -> str | None:
    """A due date, or None. Never a guess, and never one before its own meeting."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()[:10]
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return None
    # A date earlier than the meeting it came from is an extraction error, not a
    # deadline: "by Friday" resolved against the wrong week reads as instantly
    # overdue, and an instantly-overdue card trains you to ignore the due column.
    if meeting_when:
        try:
            if d < date.fromisoformat(meeting_when[:10]):
                return None
        except ValueError:
            pass
    return d.isoformat()


def _task_from(action: dict, meeting: dict, run_id: str) -> Task | None:
    title = str(action.get("title") or "").strip()
    if not title:
        return None

    domain = action.get("domain") if action.get("domain") in config.DOMAINS else config.WORK
    priority = (action.get("priority")
                if action.get("priority") in findings.VALID_PRIORITY else "normal")
    tier = action.get("tier") if action.get("tier") in VALID_TIERS else "tier-2-assistive"
    due = _valid_due(action.get("due"), meeting.get("when"))

    m_title = str(meeting.get("title") or "a meeting").strip()
    when = str(meeting.get("when") or "")[:10]
    owner = str(action.get("owner") or "").strip()
    quote = str(action.get("quote") or "").strip()

    # The detail carries the provenance, because a card whose evidence you cannot
    # check is a card you will not act on. Meeting, date, the line as spoken, and
    # the link back to the page.
    parts = [str(action.get("detail") or "").strip()]
    if quote:
        parts.append(f'From the notes: "{quote[:200]}"')
    src = f"{m_title}, {when}" if when else m_title
    if owner and owner.lower() not in config.MEETINGS_OWNER.lower():
        parts.append(f"Owned by {owner}; on {config.OWNER_NAME} to chase.")
    parts.append(f"Source: {src}")
    if meeting.get("url"):
        parts.append(str(meeting["url"]))
    detail = "\n\n".join(p for p in parts if p)

    return Task(
        id=uuid.uuid4().hex,
        title=title[:160],
        detail=detail[:1200],
        status="backlog",
        domain=domain,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        source="notion",
        origin="meeting-notes",
        origin_run_id=run_id,
        notion_page_id=_page_key(meeting.get("page_id")) or None,
        tier=tier,
        due=due,
        # Extracted by a model, so a proposal, not an instruction. /orchestrate
        # promotes; see MEETINGS_AUTOQUEUE for why this is not negotiable by default.
        auto=config.MEETINGS_AUTOQUEUE and tier == "tier-0-autonomous",
        fingerprint=findings.fingerprint(title, domain),
        tags=["meeting", tier],
    )


def _file(store: Store, task: Task) -> tuple[str, Task]:
    """Create the card, or bump the one that is already there. Returns (verb, task)."""
    with store.lock:
        match = dedupe.find_match(task, store.tasks())
        if match is None:
            store.upsert_task(task)
            return "filed", task

        # The same commitment restated in a later meeting, or reworded by the
        # model that extracted it. Bump rather than duplicate, and let the repeat
        # carry its new information: a deadline that moved EARLIER is news, and a
        # second mention is evidence of importance, so a meeting repeat escalates
        # on the second sighting where a feed repeat waits for the third.
        existing, _why = match
        dedupe.absorb(existing, task, stored=False)
        if existing.seen_count >= 2 and existing.priority == "normal":
            existing.priority = "high"
        store.upsert_task(existing)
        return "refiled", existing


def _raise_notice(store: Store, filed: list[Task], run_id: str) -> str | None:
    """One notice for everything that cannot wait, never one per card.

    Per-card toasts would be six interruptions after a meeting-heavy morning, which
    is how a notification channel gets muted.
    """
    today = date.today()
    soon = today + timedelta(days=config.MEETINGS_DUE_SOON_DAYS)
    urgent = [t for t in filed
              if t.priority == "urgent"
              or (t.due and date.fromisoformat(t.due) <= soon)]
    if not urgent:
        return None

    def line(t: Task) -> str:
        if not t.due:
            return f"- {t.title} (urgent)"
        d = date.fromisoformat(t.due)
        when = ("OVERDUE" if d < today else
                "today" if d == today else
                "tomorrow" if d == today + timedelta(days=1) else t.due)
        return f"- {t.title} (due {when})"

    overdue = any(t.due and date.fromisoformat(t.due) < today for t in urgent)
    notify.post(
        store,
        f"{len(urgent)} thing(s) from your meetings need a date kept",
        body="\n".join(line(t) for t in urgent[:6]),
        level="crit" if overdue else "warn",
        domain=config.WORK,
        source="meeting-notes",
        command="otto board --domain work",
    )
    return f"raised a notice for {len(urgent)} dated item(s)"


def harvest(store: Store, run: Run) -> list[str]:
    """Turn a finished ingest run into board cards. Returns notes for the event log."""
    if "mode=ingest" not in (run.notes or ""):
        return []

    result = detached._parse_result_json(run)
    if result is None:
        return ["meeting ingest produced no result"]
    if result.get("is_error"):
        return [f"meeting ingest failed: {result.get('subtype') or 'error'}"]

    obj = extract_json(result.get("result") or "")
    if obj is None:
        return ["meeting ingest reply was not JSON, ledger left unchanged"]

    summary = str(obj.get("summary") or "").strip()
    if summary.startswith("unavailable"):
        # An unreachable connector must not roll the watermark forward, or the
        # meetings it could not see would be skipped forever.
        return [f"meeting ingest {summary}"]

    raw_meetings = [m for m in (obj.get("meetings") or []) if isinstance(m, dict)]
    raw_actions = [a for a in (obj.get("actions") or []) if isinstance(a, dict)]

    # Page-id dedupe is the real guarantee, so it is enforced HERE and not left to
    # the session honouring the skip list in its prompt. Normalised on both sides:
    # a ledger written before the normaliser existed holds raw ids.
    already = {_page_key(p) for p in store.seen_pages()}
    meetings = []
    for m in raw_meetings:
        key = _page_key(m.get("page_id"))
        if not key or key in already:
            continue
        m["page_id"] = key
        meetings.append(m)
    if not meetings:
        note = "no new meeting notes"
        store.put_meetings({"last_ingest": iso(utcnow()),
                            "last_summary": summary or note})
        return [f"meeting ingest: {note}"]

    by_page: dict[str, list[dict]] = {m["page_id"]: [] for m in meetings}
    orphaned = 0
    restated = 0
    for a in raw_actions:
        pid = _page_key(a.get("page_id"))
        if pid in by_page:
            if a.get("mine") is not False:
                by_page[pid].append(a)
        elif pid in already:
            # A page this run re-read even though it was told to skip it. Harmless
            # and NOT the same thing as an action with no page: reporting it as
            # "dropped, no matching page" would read as lost work when the card for
            # it is already on the board.
            restated += 1
        else:
            orphaned += 1

    # Oldest first, and a page is only consumed when ALL of its items fit under the
    # cap. Splitting a page would mark it read while half its commitments were
    # dropped, which is the one failure mode that loses work silently.
    meetings.sort(key=lambda m: str(m.get("when") or ""))

    notes: list[str] = []
    filed: list[Task] = []
    consumed: list[dict] = []
    budget = config.MEETINGS_MAX_PER_RUN
    overflow = 0

    for m in meetings:
        pid = m["page_id"]
        actions = by_page[pid]
        if consumed and len(actions) > budget:
            # Stop here rather than skipping ahead to a smaller page. Consumption
            # stays strictly chronological, which is what lets the watermark and
            # `meetings[len(consumed):]` below both mean what they say.
            break
        if not consumed and len(actions) > budget:
            # One meeting alone exceeds the cap. File all of it and say so, rather
            # than splitting it: a page marked read with half its commitments
            # dropped is the only outcome here that loses work permanently.
            overflow = len(actions) - budget

        for a in actions:
            task = _task_from(a, m, run.id)
            if task is None:
                continue
            verb, stored = _file(store, task)
            filed.append(stored)
            due = f", due {stored.due}" if stored.due else ""
            notes.append(f"{verb} '{stored.title[:44]}' ({stored.tier}{due})")

        budget -= len(actions)
        consumed.append({
            "id": pid,
            "title": str(m.get("title") or "")[:120],
            "at": str(m.get("when") or "")[:10] or None,
            "url": str(m.get("url") or "") or None,
            "filed": len(actions),
        })
        if budget <= 0:
            break

    deferred = meetings[len(consumed):]

    watermark = max((c["at"] for c in consumed if c["at"]), default=None)
    store.put_meetings(
        {"watermark": watermark, "last_ingest": iso(utcnow()),
         "last_summary": summary or None,
         "filed_total": store.meetings().get("filed_total", 0) + len(filed)},
        pages=consumed,
    )

    msg = _raise_notice(store, filed, run.id)
    if msg:
        notes.append(msg)

    notes.insert(0, f"meeting ingest: {len(consumed)} page(s) read, "
                    f"{len(filed)} card(s) on the board")
    skipped = obj.get("skipped")
    if isinstance(skipped, int) and skipped:
        notes.append(f"{skipped} action item(s) belonged to somebody else")
    if restated:
        notes.append(f"{restated} action item(s) came from pages already consumed")
    if orphaned:
        notes.append(f"{orphaned} action item(s) dropped: no matching meeting page")
    if overflow:
        notes.append(f"one meeting carried {overflow} item(s) over the per-run cap "
                     f"of {config.MEETINGS_MAX_PER_RUN}; filed all of them rather "
                     "than splitting the page")
    if deferred:
        # Never silent. A deferred page is work the owner does not have yet.
        titles = ", ".join(str(m.get("title") or m["page_id"])[:30] for m in deferred[:3])
        notes.append(f"{len(deferred)} page(s) DEFERRED to the next run "
                     f"(per-run cap {config.MEETINGS_MAX_PER_RUN}): {titles}")
    return notes


# ---- read-side --------------------------------------------------------------

def status(store: Store) -> dict:
    """What the ingester has consumed, and what came out of it."""
    led = store.meetings()
    pages = led.get("pages") or []
    tasks = [t for t in store.tasks() if t.origin == "meeting-notes"]
    running = [r for r in store.runs()
               if r.status == "running" and "mode=ingest" in (r.notes or "")]
    sched = next((s for s in store.schedules() if s.runner == "ingest"), None)

    return {
        "watermark": led.get("watermark"),
        "last_ingest": led.get("last_ingest"),
        "last_summary": led.get("last_summary"),
        "filed_total": led.get("filed_total", 0),
        "pages_seen": len(pages),
        "recent_pages": list(reversed(pages[-8:])),
        "running": [r.id for r in running],
        "schedule": ({"name": sched.name, "enabled": sched.enabled,
                      "autostart": sched.autostart, "cadence": sched.cadence.model_dump(),
                      "last_run": sched.last_run, "last_status": sched.last_status}
                     if sched else None),
        "autoqueue": config.MEETINGS_AUTOQUEUE,
        "open_tasks": len([t for t in tasks if t.status != "done"]),
        "tasks": [
            {"id": t.id, "title": t.title, "status": t.status, "tier": t.tier,
             "due": t.due, "priority": t.priority, "seen_count": t.seen_count}
            for t in sorted(tasks, key=lambda t: (t.due or "9999", t.title.lower()))
        ],
    }


def overdue(store: Store) -> list[Task]:
    """Board tasks whose due date has passed. Any source, not only meetings."""
    today = date.today().isoformat()
    return [t for t in store.tasks()
            if t.due and t.due[:10] < today and t.status != "done"]
