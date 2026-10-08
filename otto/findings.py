"""Agents filing what they found.

A run that notices something worth doing should not have that die in a log. Two
routes in, because neither alone is sufficient:

  1. A fenced block at the end of the run's reply, parsed on harvest. Reliable:
     it needs no tools, no network, and no daemon reachable from the child, so it
     works even for a session with Bash denied.
  2. `otto propose` mid-run, for a long agent that finds something early and
     should not sit on it until it exits.

The hard part is not ingestion, it is **not drowning the backlog**. A sweep that
runs daily and files "that workstation crashed again" every morning means the
board becomes noise within a week and stops being read. So every filed item carries a
fingerprint (normalised title + domain) and a repeat bumps `seen_count` on the
existing card instead of adding another. A card that says "seen 12 times" is a
stronger signal than twelve cards, and it is the difference between a backlog and a
firehose.
"""

from __future__ import annotations

import json
import re
import uuid

from datetime import datetime, timezone

from . import config, dedupe
from .models import Run, Task, iso, utcnow
from .store import Store

# Deliberately unusual so it cannot collide with prose or a code sample.
BLOCK = re.compile(r"<<<OTTO\s*(.*?)\s*OTTO>>>", re.DOTALL)

VALID_PRIORITY = {"low", "normal", "high", "urgent"}

# The convention, appended to the system prompt of every Otto-spawned run. A
# template: instructions() fills the owner and the cap in at spawn time, so a
# reload of OTTO_OWNER_NAME or OTTO_FINDINGS_MAX reaches the next run.
_INSTRUCTIONS = """
## Filing findings back to Otto

You are running under Otto, who maintains the floors so %(owner)s can raise the ceilings.
The floor is what must not slip: loops running, endpoints covered, credentials valid,
nothing rotting quietly. If you notice a crack in a floor that is NOT part of your
current task, file it instead of just mentioning it. Otto puts filed items in the
board's Backlog.

Ceiling work belongs to %(owner)s: what to build, what to design, what to prioritize. Do not file
opinions about those.

End your reply with this block (omit it entirely if you found nothing):

<<<OTTO
{"tasks": [
  {"title": "short imperative, under 80 chars",
   "detail": "why it matters and what you saw, 1-3 sentences",
   "priority": "low|normal|high|urgent",
   "domain": "work|personal",
   "about": "otto"   <- ONLY when the finding is about Otto itself, else omit}
]}
OTTO>>>

Rules:
- Only file things a human would actually want on a board. Not observations, not
  things you already fixed, not routine noise.
- A finding about OTTO ITSELF (its board, CLI, daemon, feeds, triage or stale
  loop, dedupe, this template, a schedule that misfired) gets "about": "otto".
  Those go to the repo's DEBT.md, never onto the owner's board. That board is the
  owner's work; Otto's bugs are not their to-do list.
- Do not file the same live condition twice with a different timestamp in the
  title ("refresh X before 04:07Z"). Name the condition; Otto counts repeats.
- At most %(max)d items. If you have more, file the most important ones.
- `detail` must carry the evidence. "Check the build" is useless; "the api repo
  fails to compile on main, missing symbol X since commit abc123" is actionable.
- Do not file something you were asked to do; that is your task, not a finding.
- Filing the same thing on a later run is fine, Otto counts repeats rather than
  duplicating the card.

## Unattended-session limits

Nobody is watching this session, so two things need an explicit ask from %(owner)s
that you cannot receive here, and a guard hook will refuse them: sending outreach
(Slack sends or scheduled sends, sending email) and checking out credentials from
the secrets store. Do not attempt them and do not look for another route.
If the task needs a message sent, leave a Slack draft or put the exact text and
recipient in your final paragraph; if it needs a credential, say which secret and
why. The attended half is for %(owner)s to run.
"""


def instructions() -> str:
    """The filing convention with the owner's name and the per-run cap filled in,
    read when a run is spawned so a reload reaches the next one."""
    return _INSTRUCTIONS % {"owner": config.OWNER_NAME, "max": config.FINDINGS_MAX_PER_RUN}


# The fingerprint lives in dedupe.py now, next to the two looser tiers of
# "same card" that it was never enough on its own. Re-exported so callers and
# tests that know it as findings.fingerprint keep working.
fingerprint = dedupe.fingerprint

# A finding about Otto's own machinery, when the run forgot to say so. Kept
# conservative: every alternative names a thing only Otto has. "board" alone is
# not here because the feedback board and the Notion boards exist.
_SELF = re.compile(
    r"\botto\b|/triage\b|\btriage (pass|loop|list|set|promote|status)\b"
    r"|\bdaemon\b|--detail-file|stale re-?verif|otto task"
    r"|slack-dm-fetch|slack-sweep|\bheartbeat\b|/daily\b"
    r"|known-patterns|fingerprint|dedupe|needs-you|the backlog\b",
    re.I,
)


def is_self(raw: dict) -> bool:
    """Is this finding about Otto rather than about the owner's work?"""
    if str(raw.get("about") or "").lower() == "otto":
        return True
    return bool(_SELF.search(str(raw.get("title") or "")))


def append_debt(raw: dict, origin: str, run_id: str | None, reason: str) -> str:
    """Write a finding to the repo's DEBT.md instead of the board.

    Append-only, dated, with the origin, so the file is a log that can be
    worked top to bottom in the repo where the fix happens. Returns a note.
    """
    path = config.DEBT_FILE
    title = str(raw.get("title") or "").strip()[:160]
    detail = str(raw.get("detail") or "").strip()
    stamp = iso(utcnow())[:16].replace("T", " ")
    entry = (f"\n## {title}\n\n"
             f"- filed: {stamp} UTC by {origin}"
             + (f" (run {run_id[:6]})" if run_id else "")
             + f"\n- routed here: {reason}\n"
             + (f"\n{detail}\n" if detail else ""))
    try:
        if not path.exists():
            path.write_text(
                "# Otto debt\n\nFindings about Otto itself, filed by its own runs. "
                "Not on the board on purpose: the board is the owner's work, this is "
                "Otto's. Work it top to bottom in the repo; delete an entry when "
                "the fix lands.\n",
                encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError as e:
        return f"{origin} could not write DEBT.md ({e}); DROPPED '{title[:44]}'"
    return f"{origin} filed '{title[:44]}' to DEBT.md ({reason})"


def filed_today(store: Store, now: datetime | None = None) -> int:
    """Agent-filed cards created so far this UTC day, for the daily cap."""
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    return len([t for t in store.tasks()
                if t.source == "agent" and (t.created or "")[:10] == today])


def _clean(raw: dict, origin: str, run_id: str | None) -> Task | None:
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    domain = raw.get("domain") if raw.get("domain") in config.DOMAINS else config.WORK
    priority = raw.get("priority") if raw.get("priority") in VALID_PRIORITY else "normal"
    detail = str(raw.get("detail") or "").strip() or None

    return Task(
        id=uuid.uuid4().hex,
        title=title[:160],
        detail=detail[:1200] if detail else None,
        status="backlog",
        domain=domain,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        source="agent",
        origin=origin,
        origin_run_id=run_id,
        # Filed by a machine, so never auto-dispatched: a finding is a proposal, and
        # a proposal that runs itself is just an agent starting more agents.
        auto=False,
        fingerprint=fingerprint(title, domain),
        tags=[f"from:{origin}"] if origin else [],
    )


def parse_block(text: str) -> list[dict]:
    """Pull task dicts out of the fenced block. Tolerant of a stray code fence."""
    if not text:
        return []
    m = BLOCK.search(text)
    if not m:
        return []
    body = m.group(1).strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[-1] if "\n" in body else body
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return []
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = obj.get("tasks") or []
    else:
        return []
    return [i for i in items if isinstance(i, dict)]


def file_tasks(store: Store, raws: list[dict], origin: str,
               run_id: str | None = None) -> list[str]:
    """Create or bump. Returns human notes for the event log."""
    notes: list[str] = []
    if not raws:
        return notes

    capped = raws[: config.FINDINGS_MAX_PER_RUN]
    dropped = len(raws) - len(capped)
    today_count = filed_today(store)

    for raw in capped:
        task = _clean(raw, origin, run_id)
        if task is None:
            continue
        # Otto's own bugs go to the repo, not onto the owner's board.
        if is_self(raw):
            notes.append(append_debt(raw, origin, run_id, "about Otto"))
            continue
        with store.lock:
            # Same title, same topic, or a title alike enough: dedupe.py decides,
            # and the match is bumped instead of a new card landing.
            match = dedupe.find_match(task, store.tasks())
            if match is not None:
                existing, why = match
                dedupe.absorb(existing, task, stored=False)
                store.upsert_task(existing)
                notes.append(f"{origin} refiled '{existing.title[:44]}' "
                             f"(seen {existing.seen_count}x, {why})")
                continue
            # A repeat bumps its card whatever the cap says; only NEW cards count.
            if today_count >= config.FINDINGS_MAX_PER_DAY:
                notes.append(append_debt(
                    raw, origin, run_id,
                    f"daily board cap of {config.FINDINGS_MAX_PER_DAY} reached"))
                continue
            store.upsert_task(task)
            today_count += 1
            notes.append(f"{origin} filed '{task.title[:44]}' to backlog")

    if dropped:
        # Never silently truncate: a capped finding is a finding you did not get.
        notes.append(f"{origin} proposed {len(raws)} items, kept "
                     f"{config.FINDINGS_MAX_PER_RUN}, DROPPED {dropped}")
    return notes


def harvest(store: Store, run: Run, origin: str | None = None) -> list[str]:
    """Read a finished run's reply and file whatever it proposed."""
    from .runners import detached

    obj = detached._parse_result_json(run)
    if obj is None:
        return []
    raws = parse_block(obj.get("result") or "")
    if not raws:
        return []
    return file_tasks(store, raws, origin or run.name, run.id)
