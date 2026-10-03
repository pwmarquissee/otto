"""Session transcript usage rollup.

Claude Code writes one JSONL transcript per session under
~/.claude/projects/<slug>/<session-id>.jsonl, where slug is the working
directory with every non-alphanumeric character replaced by '-'
(D:\\repos\\myapp -> D--repos-myapp).

Otto reads those transcripts to attribute token usage to a run. It reports
tokens always and a dollar cost ONLY when the transcript itself carries one:
pricing is not hardcoded here, so these numbers never drift out of date.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config


def slug_for(cwd: str | Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir(cwd: str | Path) -> Path:
    return config.CLAUDE_DIR / "projects" / slug_for(cwd)


def newest_transcript(cwd: str | Path, after_epoch: float | None = None) -> Path | None:
    """Locate the transcript CREATED by a run that started at after_epoch.

    Filtering on modification time is not enough: a long-lived session in the same
    directory keeps getting touched, so an mtime filter happily returns the
    caller's own transcript and attributes its entire token history to a run that
    just started. Creation time (st_ctime on Windows) is the correct binding.

    Headless runs do not rely on this at all; they read usage off Claude Code's
    JSON result. This is only for interactive (windowed) sessions.
    """
    d = project_dir(cwd)
    if not d.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for f in d.glob("*.jsonl"):
        try:
            stat = f.stat()
        except OSError:
            continue
        if after_epoch is not None:
            born = getattr(stat, "st_birthtime", stat.st_ctime)
            if born < after_epoch - 5:
                continue  # predates the run, so it belongs to someone else
        if best is None or stat.st_mtime > best[0]:
            best = (stat.st_mtime, f)
    return best[1] if best else None


def rollup(path: Path) -> dict[str, float | int | None]:
    """Sum usage across assistant turns in a transcript.

    Cache reads and cache writes are counted into input tokens because that is
    what was actually billed as input for the turn.
    """
    tin = tout = 0
    cost: float | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                for key in ("costUSD", "total_cost_usd", "totalCostUsd"):
                    if isinstance(entry.get(key), (int, float)):
                        cost = (cost or 0.0) + float(entry[key])

                msg = entry.get("message")
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                tin += int(usage.get("input_tokens") or 0)
                tin += int(usage.get("cache_creation_input_tokens") or 0)
                tin += int(usage.get("cache_read_input_tokens") or 0)
                tout += int(usage.get("output_tokens") or 0)
    except OSError:
        return {"input_tokens": None, "output_tokens": None, "cost_usd": None}

    return {
        "input_tokens": tin or None,
        "output_tokens": tout or None,
        "cost_usd": cost,
    }


def usage_for_run(cwd: str | None, started_epoch: float | None) -> dict[str, float | int | None]:
    empty: dict[str, float | int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    if not cwd:
        return empty
    t = newest_transcript(cwd, started_epoch)
    if t is None:
        return empty
    return rollup(t)
