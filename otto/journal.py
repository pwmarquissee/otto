"""What the day actually contained.

Otto could see 23 of its own runs and no idea the owner had spent the day importing a
design and reverse-engineering a kernel dump. It was blind to the work, which makes
any retrospective it offers worthless. This module closes that: it reads Claude Code's
own session transcripts and reduces them to a durable record of where a day went.

Two deliberate choices:

  1. NO MODEL. Everything here is mechanical extraction. Claude Code already writes an
     `ai-title` line per session, which is a better one-line summary than anything a
     re-read would produce, and the first typed prompt is the stated intent. Summarising
     33 MB of transcript daily would cost real money to learn things already on disk.
  2. Stated intent is kept next to elapsed time. "Import this design" that ran nine
     hours is the single most useful signal in here, and it only exists if the intent
     is recorded verbatim rather than summarised into agreement with the outcome.

Work and personal live in one store on purpose. The owner works from home; home life is an
input to the work, so splitting them would reproduce the blindness this module fixes.
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config

# Turns Claude Code injects rather than the owner typing them. Counting these as intent
# would make every session look like it began with a system reminder.
_SYNTHETIC_SOURCES = {"hook", "system", "command_output", "compact"}

# A gap longer than this between two events in one session is idle, not work.
IDLE_GAP_S = 10 * 60


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_of(message: Any) -> str:
    """Flatten a message's content to plain text, ignoring tool blocks."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _scan(path: Path, day: _date) -> dict[str, Any] | None:
    """Reduce one transcript to facts about `day`, or None if it was idle then."""
    stamps: list[datetime] = []
    title: str | None = None
    cwd: str | None = None
    branch: str | None = None
    intent: str | None = None
    typed = 0
    assistant_turns = 0
    tools = 0
    edited: set[str] = set()

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = o.get("type")
            # ai-title / last-prompt lines carry no timestamp, so take them whenever
            # they appear: a session has one title and it describes the whole thing.
            if kind == "ai-title" and o.get("aiTitle"):
                title = str(o["aiTitle"])
                continue

            ts = _parse_ts(o.get("timestamp"))
            if ts is None:
                continue
            if ts.astimezone().date() != day:
                continue

            stamps.append(ts)
            cwd = cwd or o.get("cwd")
            branch = branch or o.get("gitBranch")

            if o.get("isSidechain"):
                continue  # subagent chatter, not the owner and not the main thread

            if kind == "user":
                if o.get("promptSource") in _SYNTHETIC_SOURCES:
                    continue
                text = _text_of(o.get("message")).strip()
                if not text or text.startswith("<"):
                    continue
                typed += 1
                if intent is None:
                    intent = text
            elif kind == "assistant":
                assistant_turns += 1
                msg = o.get("message") or {}
                for block in (msg.get("content") or []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tools += 1
                    name = block.get("name")
                    inp = block.get("input") or {}
                    if name in ("Edit", "Write", "NotebookEdit") and isinstance(inp, dict):
                        p = inp.get("file_path")
                        if p:
                            edited.add(str(p))

    if not stamps or typed == 0:
        # No activity that day, or nothing the owner actually typed. A session that only
        # received injected turns is not a session he worked in.
        return None

    stamps.sort()
    first, last = stamps[0], stamps[-1]
    # Engaged time, not span. A session left open all afternoon with two prompts in it
    # is not an afternoon of work, and reporting it as one would make every number
    # here untrustworthy. Consecutive events more than IDLE_GAP apart are idle.
    windows: list[tuple[datetime, datetime]] = []
    run_start = stamps[0]
    prev = stamps[0]
    for ts in stamps[1:]:
        if (ts - prev).total_seconds() > IDLE_GAP_S:
            windows.append((run_start, prev))
            run_start = ts
        prev = ts
    windows.append((run_start, prev))
    engaged = round(sum((b - a).total_seconds() for a, b in windows) / 60)

    return {
        "session": path.stem,
        "title": title,
        "cwd": cwd,
        "branch": branch,
        "started": first.astimezone().strftime("%H:%M"),
        "ended": last.astimezone().strftime("%H:%M"),
        "minutes": engaged,
        "span_minutes": round((last - first).total_seconds() / 60),
        "_windows": [(a.isoformat(), b.isoformat()) for a, b in windows],
        "intent": intent[:400] if intent else None,
        "typed_turns": typed,
        "assistant_turns": assistant_turns,
        "tool_calls": tools,
        "files_touched": sorted(edited)[:40],
        "files_touched_n": len(edited),
    }


def sessions_for(day: _date) -> list[dict[str, Any]]:
    """Every session the owner typed in on `day`, newest last."""
    root = config.CLAUDE_DIR / "projects"
    if not root.is_dir():
        return []
    # A transcript last modified before the day started cannot hold that day.
    floor = datetime.combine(day, datetime.min.time()).timestamp()
    out: list[dict[str, Any]] = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < floor:
                    continue
            except OSError:
                continue
            rec = _scan(f, day)
            if rec:
                rec["project"] = proj.name
                out.append(rec)
    out.sort(key=lambda r: r["started"])
    return out


def rollup(day: _date) -> dict[str, Any]:
    """The retrospective half of a day record. Deterministic, no model, no cost."""
    ss = sessions_for(day)
    # Summed engaged minutes can exceed the day: the owner runs sessions concurrently, and
    # that is real attention, not a bug. But it is NOT wall clock, so report both and
    # never let the bigger number masquerade as time elapsed.
    attention = sum(s["minutes"] for s in ss)
    # Flatten every engaged window, sort, then coalesce ONCE. An earlier version
    # merged against the previous entry before sorting, which collapsed unrelated
    # intervals and reported 74 minutes for a day spanning 00:35 to 23:18.
    flat: list[list[datetime]] = []
    for s in ss:
        for a_raw, b_raw in s.get("_windows") or []:
            flat.append([datetime.fromisoformat(a_raw), datetime.fromisoformat(b_raw)])
    flat.sort(key=lambda w: w[0])
    coalesced: list[list[datetime]] = []
    for a, b in flat:
        if coalesced and a <= coalesced[-1][1]:
            coalesced[-1][1] = max(coalesced[-1][1], b)
        else:
            coalesced.append([a, b])
    wall = round(sum((b - a).total_seconds() for a, b in coalesced) / 60)
    by_project: dict[str, int] = {}
    for s in ss:
        by_project[s.get("project") or "?"] = by_project.get(s.get("project") or "?", 0) + s["minutes"]

    # Sessions that ran long. Not a judgement, just the pair of numbers that lets a
    # judgement be made: what you said you were doing, and how long it actually took.
    long_runs = [
        {"intent": s["intent"], "minutes": s["minutes"], "title": s["title"]}
        for s in ss if s["minutes"] >= 60
    ]
    long_runs.sort(key=lambda r: -r["minutes"])

    out = {
        "date": day.isoformat(),
        "sessions": ss,
        "session_count": len(ss),
        "attention_minutes": attention,   # summed across sessions; may exceed wall clock
        "wall_minutes": wall,             # union of engaged windows; real elapsed
        "first_activity": min((s["started"] for s in ss), default=None),
        "last_activity": max((s["ended"] for s in ss), default=None),
        "by_project": dict(sorted(by_project.items(), key=lambda kv: -kv[1])),
        "long_sessions": long_runs,
        "files_touched": sorted({p for s in ss for p in s["files_touched"]})[:60],
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _strip_internal(out)


def _strip_internal(rec: dict[str, Any]) -> dict[str, Any]:
    """Drop the interval scratch data before anything stores or renders a rollup."""
    for s in rec.get("sessions") or []:
        s.pop("_windows", None)
    return rec


def yesterday() -> _date:
    return (datetime.now().astimezone() - timedelta(days=1)).date()
