"""Declared-vs-observed drift detection over Otto's capability surface.

Stolen from Gaia's `.env.keys`, which exists because the always-on Mac was silently
missing an entire credential set and nothing ever compared the two machines. The
failure it catches is absence of a thing nobody declared, which no liveness probe can
find: `otto probe` can tell you a registered server is broken, but it cannot tell you
a server that should exist was never registered at all.

Two rules, both load-bearing:

  NAMES ONLY   This module reads registry KEYS and filenames. It never reads a token,
               never reports a length, never hashes a value. A length is a hint and a
               hash is a confirmation oracle.
  EXPECT BOTH  A capability can be declared `absent`. S1 is decommissioned and the
               Okta Desktop entry was deliberately pulled; a presence-only manifest is
               blind to either of them coming back, which is exactly the regression
               worth alarming on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

from . import config

# Where each observable surface lives. `account` has no local artifact, so it is
# absent from this map on purpose and reports as unverifiable rather than as missing.
CODE_REGISTRY = config.HOME / ".claude.json"
DESKTOP_REGISTRY = (Path(os.environ.get("APPDATA", config.HOME / "AppData" / "Roaming"))
                    / "Claude" / "claude_desktop_config.json")
LOADER_SH_DIR = config.CLAUDE_DIR
LOADER_PS1_DIR = config.HOME / "tools"


class Finding(NamedTuple):
    capability: config.Capability
    state: str          # "ok" | "missing" | "unexpected" | "unverifiable" | "no-surface"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("ok", "unverifiable")


def _registry_servers(path: Path) -> tuple[set[str], str | None]:
    """Server NAMES from an MCP registry. Returns (names, error).

    A missing registry is not an error for Desktop (it may not be installed) but the
    caller needs to tell "no servers" apart from "no file", because declaring a
    Desktop absence is satisfied by the file not existing at all.
    """
    if not path.is_file():
        return set(), "no registry file"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return set(), f"unreadable ({type(e).__name__})"
    # Claude Code keeps a global `mcpServers` AND per-project ones under `projects`.
    # Both are real registrations, so ALWAYS union. An earlier version returned early
    # when the global dict existed, which made every project-scoped server invisible
    # and reported `notion` as missing while this session was actively calling it.
    found: set[str] = set(raw.get("mcpServers") or [])
    projects = raw.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict):
                found |= set(proj.get("mcpServers") or [])
    return found, None


def observe() -> dict[str, set[str]]:
    """What is actually present, per surface. Names only."""
    code, _ = _registry_servers(CODE_REGISTRY)
    desktop, _ = _registry_servers(DESKTOP_REGISTRY)
    return {
        config.SURFACE_CODE_MCP: code,
        config.SURFACE_DESKTOP_MCP: desktop,
        config.SURFACE_LOADER_SH: {p.name for p in LOADER_SH_DIR.glob("*.sh")}
                                  if LOADER_SH_DIR.is_dir() else set(),
        config.SURFACE_LOADER_PS1: {p.name for p in LOADER_PS1_DIR.glob("*.ps1")}
                                   if LOADER_PS1_DIR.is_dir() else set(),
        config.SURFACE_ACCOUNT: set(),          # nothing local to read
    }


def check() -> list[Finding]:
    """Diff every declared capability against reality."""
    seen = observe()
    out: list[Finding] = []
    for cap in config.CAPABILITIES:
        if cap.surface == config.SURFACE_ACCOUNT:
            out.append(Finding(cap, "unverifiable",
                               "account-bound; no local artifact to inspect"))
            continue
        present = cap.name in seen.get(cap.surface, set())
        if cap.expect == "present":
            out.append(Finding(cap, "ok" if present else "missing"))
        else:
            out.append(Finding(cap, "unexpected" if present else "ok",
                               cap.why if present else ""))
    return out


def undeclared() -> list[tuple[str, str]]:
    """Present but not declared: (surface, name).

    Drift in the other direction. An MCP server nobody wrote down is one nobody is
    accountable for, and it is how a second Okta client ends up polling in the dark.
    Loader surfaces are excluded: `~/.claude` is full of unrelated shell scripts and
    flagging each one would bury the signal.
    """
    seen = observe()
    declared = {(c.surface, c.name) for c in config.CAPABILITIES}
    out: list[tuple[str, str]] = []
    for surface in (config.SURFACE_CODE_MCP, config.SURFACE_DESKTOP_MCP):
        for name in sorted(seen.get(surface, set())):
            if (surface, name) not in declared:
                out.append((surface, name))
    return out


def gaps() -> list[dict]:
    """Manifest drift as advisor-shaped gap rows, for `otto gaps` and the board."""
    rows: list[dict] = []
    for f in check():
        if f.state == "missing":
            rows.append({
                "id": f"gap:manifest-missing:{f.capability.key}",
                "kind": "manifest-drift",
                "domain": config.WORK,
                "title": f"declared but absent: {f.capability.name} "
                         f"({f.capability.surface})",
                "why": "Declared in the capability manifest and not found. Nothing else "
                       "detects this: a probe can only test what is registered.",
                "command": "otto manifest",
                "score": 70,
            })
        elif f.state == "unexpected":
            rows.append({
                "id": f"gap:manifest-regression:{f.capability.key}",
                "kind": "manifest-regression",
                "domain": config.WORK,
                "title": f"deliberately removed, now back: {f.capability.name} "
                         f"({f.capability.surface})",
                "why": f.detail or "Declared absent in the manifest but present.",
                "command": "otto manifest",
                "score": 80,
            })
    for surface, name in undeclared():
        rows.append({
            "id": f"gap:manifest-undeclared:{surface}/{name}",
            "kind": "manifest-undeclared",
            "domain": config.WORK,
            "title": f"undeclared MCP server: {name} ({surface})",
            "why": "Registered but not in the manifest, so nobody has said what it is "
                   "for or who owns it. Declare it or remove it.",
            "command": "otto manifest",
            "score": 40,
        })
    return rows


def render() -> str:
    """Human-readable report for `otto manifest`."""
    findings = check()
    width = max((len(f.capability.name) for f in findings), default=10)
    lines: list[str] = ["capability manifest  (names only, never values)", ""]
    by_surface: dict[str, list[Finding]] = {}
    for f in findings:
        by_surface.setdefault(f.capability.surface, []).append(f)

    glyph = {"ok": "  ok", "missing": " MISS", "unexpected": " BACK",
             "unverifiable": "   ?", "no-surface": "   -"}
    for surface in config.SURFACES:
        group = by_surface.get(surface)
        if not group:
            continue
        lines.append(f"{surface}:")
        for f in group:
            exp = "" if f.capability.expect == "present" else "   [expected absent]"
            lines.append(f"  {glyph.get(f.state, '  ??')}  "
                         f"{f.capability.name:<{width}}{exp}")
            if f.state in ("unexpected",) and f.detail:
                lines.append(f"        why: {f.detail}")
        lines.append("")

    extra = undeclared()
    if extra:
        lines.append("undeclared but present:")
        lines += [f"        {s}/{n}" for s, n in extra]
        lines.append("")

    bad = [f for f in findings if not f.ok]
    lines.append(f"{len(findings)} declared, {len(bad)} disagreeing with reality, "
                 f"{len(extra)} undeclared")
    return "\n".join(lines)
