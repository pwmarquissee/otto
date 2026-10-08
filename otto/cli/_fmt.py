"""Terminal formatting shared by every verb: colors, padding, ages, the snapshot table."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .. import config
from .. import today as _today


C_RESET = "\033[0m"


C_DIM = "\033[2m"


C_BOLD = "\033[1m"


C_RED = "\033[31m"


C_YEL = "\033[33m"


C_GRN = "\033[32m"


C_CYA = "\033[36m"


_LEVEL_COLOR = {"crit": C_RED, "warn": C_YEL, "info": C_CYA}


_STATUS_COLOR = {
    "running": C_CYA, "ok": C_GRN, "due": C_YEL,
    "failed": C_RED, "orphaned": C_RED, "killed": C_DIM, "skipped": C_DIM,
}


# The work lane. `not-evaluated` is deliberately dim, not red: a run Otto lost, or
# that the API killed, has said nothing about the work, and painting it as a failure
# is the misread verdict.py exists to prevent.
_WORK_COLOR = {
    "running": C_CYA, "done": C_GRN, "due": C_YEL,
    "failed": C_RED, "not-evaluated": C_DIM, "skipped": C_DIM,
}


def _c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{C_RESET}"


def _cpad(text: str, width: int, color: str = "") -> str:
    """Pad first, then colorize.

    Colorizing before padding would count the ANSI escape bytes toward the field
    width and break every column downstream.
    """
    padded = f"{text:<{width}}"
    return _c(padded, color) if color else padded


def _age(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = delta.total_seconds()
    if s < 0:
        return "just now"
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def _banner(headline: str) -> None:
    print(_c(f"  {headline}", C_BOLD))


def _print_snapshots(snaps: dict, domain: str | None = None) -> None:
    """The day: what is left of the calendar, and which mail wants a reply.

    Rendering lives in `today.py` rather than here so `otto status`, `otto agenda`,
    and the refresh wait path all show the same day rather than three variants of
    it. This function only adds colour, which is the one thing a module that might
    be piped into JSON should not decide.
    """
    if not snaps:
        return
    print(_c("  TODAY", C_BOLD))
    stale = [k for k, s in snaps.items()
             if (_hours_since(s.get("fetched_at")) or 0) > config.SNAPSHOT_STALE_HOURS]
    for line in _today.render(snaps, domain=domain):
        # Markers earn colour; the rest stays plain so the markers still stand out.
        stripped = line.lstrip()
        if stripped.startswith("REPLY"):
            print(_c("  " + line, C_YEL))
        elif stripped.startswith("! CLASH"):
            print(_c("  " + line, C_RED))
        elif "<- NOW" in line or "<- in " in line:
            print(_c("  " + line, C_BOLD))
        elif line and not line.startswith(" "):
            print(_c("  " + line, C_DIM))
        else:
            print("  " + line)
    if stale:
        print(_c(f"    STALE: {', '.join(sorted(stale))} older than "
                 f"{config.SNAPSHOT_STALE_HOURS}h - otto refresh", C_YEL))
    print()


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def _integration_mark(i: dict) -> tuple[str, str]:
    if i["ok"] and i["mode"] == "mcp-only":
        return "mcp", C_CYA
    if i["ok"]:
        return "ok", C_GRN
    if i["mode"] == "mcp-only":
        return "MCP?", C_YEL
    if i["mode"] == "unconfigured":
        return "unset", C_YEL
    return "DOWN", C_RED
