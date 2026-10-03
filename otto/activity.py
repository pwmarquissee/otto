"""Turning a running agent's stream into something you can watch.

Spawned sessions log NDJSON (`--output-format stream-json`), one event per step, so
the file grows live rather than appearing all at once at exit. This module reads it
into an ordered timeline plus a one-line "what is it doing right now".

Why parse server-side instead of in the browser: the log is the raw wire format with
full tool inputs and results, which can be enormous (a Read of a big file, a Bash
dump). The dashboard wants "Bash: ls -la /d/otto" and a token count, not 40KB of
tool_result. Summarising here keeps the payload small and the browser simple.

Events carry no timestamps, so ordering comes from line order and "is it stuck" comes
from the log file's mtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Run, iso

# Tool inputs worth showing, in preference order per tool.
_ARG_HINTS = (
    "command", "file_path", "path", "pattern", "query", "url", "prompt",
    "description", "skill", "notebook_path", "task_id", "name",
)

MAX_LABEL = 160


def _summarise_input(tool: str, data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)[:MAX_LABEL]
    for key in _ARG_HINTS:
        if key in data and isinstance(data[key], (str, int, float)):
            val = str(data[key]).replace("\n", " ; ").strip()
            if val:
                return val[:MAX_LABEL]
    # Nothing recognisable: show the keys so it is at least identifiable.
    return ", ".join(sorted(data)[:6])[:MAX_LABEL]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def parse(run: Run, limit: int = 200) -> dict:
    """Read a run's log into a timeline. Safe on empty, partial, and non-stream logs."""
    out: dict[str, Any] = {
        "run_id": run.id,
        "status": run.status,
        "live": run.status == "running",
        "events": [],
        "current": None,
        "tool_calls": 0,
        "turns": None,
        "cost_usd": run.cost_usd,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "model": None,
        "last_activity": None,
        "streaming": False,
        "note": None,
        "subtasks_started": 0,
        "subtasks_done": 0,
        "subtask_current": None,
    }

    if not run.log:
        out["note"] = "this run has no log"
        return out
    p = Path(run.log)
    if not p.is_file():
        out["note"] = f"log file is missing: {run.log}"
        return out

    try:
        stat = p.stat()
        out["last_activity"] = iso(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["note"] = f"log unreadable: {e}"
        return out

    if not raw.strip():
        out["note"] = ("no output yet, the agent is still starting up"
                       if run.status == "running" else "the log is empty")
        return out

    events: list[dict] = []
    pending: dict[str, dict] = {}   # tool_use_id -> event, so results attach to calls
    subtasks: dict[str, dict] = {}  # task_id -> event, for subagent sub-steps

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            # stderr or a plain-text log from an older run; keep it visible.
            events.append({"kind": "log", "label": line[:MAX_LABEL]})
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        out["streaming"] = True
        kind = ev.get("type")

        if kind == "system" and ev.get("subtype", "").startswith("task_"):
            # A subagent (Agent/Task tool) reports its own sub-steps through these.
            # They were being dropped, which is why a run doing 59 sub-tasks looked
            # frozen: the parent's only visible activity was the Agent call itself,
            # whose prompt never changes.
            sub = ev.get("subtype")
            desc = (ev.get("description") or ev.get("summary") or "").strip()
            if sub == "task_started" and desc:
                e = {"kind": "subtask", "label": desc[:MAX_LABEL], "ok": None}
                if ev.get("task_id"):
                    subtasks[ev["task_id"]] = e
                events.append(e)
                out["subtasks_started"] += 1
            elif sub == "task_notification":
                target = subtasks.pop(ev.get("task_id"), None)
                done = (ev.get("status") or "").lower() == "completed"
                if target is not None:
                    target["ok"] = done
                if done:
                    out["subtasks_done"] += 1
            elif sub == "task_progress" and desc:
                out["subtask_current"] = desc[:MAX_LABEL]
            continue

        if kind == "system" and ev.get("subtype") == "init":
            out["model"] = ev.get("model")
            tools = ev.get("tools") or []
            events.append({"kind": "init", "label": f"session started",
                           "detail": f"{ev.get('model') or 'model'}, {len(tools)} tools"})

        elif kind == "assistant":
            msg = ev.get("message") or {}
            for c in (msg.get("content") or []):
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    out["tool_calls"] += 1
                    e = {"kind": "tool", "label": c.get("name") or "tool",
                         "detail": _summarise_input(c.get("name") or "", c.get("input")),
                         "ok": None}
                    if c.get("id"):
                        pending[c["id"]] = e
                    events.append(e)
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    events.append({"kind": "text", "label": c["text"].strip()[:600]})
            usage = msg.get("usage") or {}
            if usage:
                # Output tokens ACCUMULATE across turns, so they must be summed.
                # Overwriting per message showed "2 out" for a run that had
                # produced thousands, which read as a stalled agent.
                out["output_tokens"] = (out["output_tokens"] or 0) + int(
                    usage.get("output_tokens") or 0)
                # Input is different: each turn re-sends the whole context, most of
                # it a cache read, so summing would wildly overcount. The max is the
                # context high-water mark, which is the useful in-flight number. The
                # final result object overwrites both with authoritative totals.
                tin = (int(usage.get("input_tokens") or 0)
                       + int(usage.get("cache_creation_input_tokens") or 0)
                       + int(usage.get("cache_read_input_tokens") or 0))
                if tin > (out["input_tokens"] or 0):
                    out["input_tokens"] = tin

        elif kind == "user":
            msg = ev.get("message") or {}
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    target = pending.pop(c.get("tool_use_id"), None)
                    body = _text_of(c.get("content")).strip().replace("\n", " ; ")
                    if target is not None:
                        target["ok"] = not c.get("is_error")
                        target["result"] = body[:MAX_LABEL] or "(no output)"

        elif kind == "result":
            out["turns"] = ev.get("num_turns")
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                out["cost_usd"] = float(ev["total_cost_usd"])
            events.append({
                "kind": "result",
                "label": "finished with an error" if ev.get("is_error") else "finished",
                "detail": (ev.get("result") or "")[:600] or None,
                "ok": not ev.get("is_error"),
            })

    # A tool call with no result yet is what the agent is doing right now.
    if run.status == "running":
        # A pending Agent call means a subagent is working. Its prompt is static, so
        # showing that reads as frozen; the subagent's latest sub-step is the real
        # answer to "what is it doing right now".
        waiting_on_agent = any(
            e["kind"] == "tool" and e.get("ok") is None and e["label"] in ("Agent", "Task")
            for e in events
        )
        if waiting_on_agent and out["subtask_current"]:
            done, started = out["subtasks_done"], out["subtasks_started"]
            out["current"] = f"subagent: {out['subtask_current']} ({done}/{started} sub-tasks done)"
        for e in reversed(events):
            if out["current"]:
                break
            if e["kind"] == "tool" and e.get("ok") is None:
                out["current"] = f"{e['label']}: {e['detail']}" if e.get("detail") else e["label"]
                break
        if out["current"] is None:
            last = next((e for e in reversed(events) if e["kind"] in ("text", "init")), None)
            out["current"] = "thinking" if last and last["kind"] == "text" else "starting up"

    out["events"] = events[-limit:]
    out["truncated"] = len(events) > limit
    return out


def summary_line(run: Run) -> str | None:
    """One line for a list view. Cheap enough to call per running run."""
    a = parse(run, limit=30)
    if a["current"]:
        return a["current"]
    if a.get("note"):
        return a["note"]
    return None
