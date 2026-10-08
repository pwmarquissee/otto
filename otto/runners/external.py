"""External system probes (read-only).

One place that answers "is this integration actually alive" for the systems
Otto itself depends on: the Anthropic admin key it prices spend with, the AWS
SSO session its runs assume, the MCP servers its sessions reach through, and
the herdr harness the sessions live in.

Honesty rule: anything Otto cannot verify from Python is reported as
`mcp-only` or `unconfigured`, never as green. Integrations that run through MCP
servers have no Python-side credentials, so Otto reports their mode rather than
pretending to have checked them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from ..models import Integration, iso, utcnow

TIMEOUT = 12


def _http(url: str, headers: dict[str, str], method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 - DNS/TLS/timeout all mean "down"
        return None, str(e).encode()


def _read_env_cache(path: Path, *names: str) -> dict[str, str]:
    """Pull export NAME="value" pairs out of a bash env-loader cache."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    for name in names:
        m = re.search(rf'export {name}="([^"]*)"', text)
        if m:
            out[name] = m.group(1)
    return out


# Windows creates a console window for a console application launched from a process
# that has none, and that window TAKES FOCUS. The daemon probes AWS every 5 minutes, so
# this yanked the owner out of a full-screen game twice an hour. Every shell-out from
# the daemon goes through _run so the flag can never be forgotten at a new call site.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=_NO_WINDOW)


def _stamp(i: Integration) -> Integration:
    i.checked_at = iso(utcnow())
    return i


# ---- API-verifiable ---------------------------------------------------------
#
# Only products that are actually in use get a probe. Probing a product you no
# longer own manufactures a red row that can never go green, which trains you to
# ignore the whole panel.

def probe_anthropic() -> Integration:
    creds = _read_env_cache(config.CLAUDE_DIR / ".anthropic-env.cache", "ANTHROPIC_ADMIN_API_KEY")
    key = creds.get("ANTHROPIC_ADMIN_API_KEY")
    if not key:
        return _stamp(Integration(
            name="anthropic", ok=False, mode="unconfigured",
            detail="no live cache - source ~/.claude/anthropic-env.sh && anthropic-env"))
    status, _ = _http("https://api.anthropic.com/v1/organizations/users?limit=1",
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    if status == 200:
        return _stamp(Integration(name="anthropic", ok=True, detail="admin key valid"))
    if status in (401, 403):
        return _stamp(Integration(name="anthropic", ok=False, detail=f"key rejected ({status})"))
    return _stamp(Integration(name="anthropic", ok=False, detail=f"HTTP {status}"))


def probe_aws() -> Integration:
    """Is the AWS SSO session live for the configured profile?

    An SSO-backed setup typically has no default profile, so the probe names one
    (config.AWS_PROFILE). An expired SSO session is the common failure and gets a
    specific remedy rather than a generic error.
    """
    if not shutil.which("aws"):
        return _stamp(Integration(name="aws", ok=False, mode="unconfigured",
                                  detail="aws CLI not on PATH"))
    profile = config.AWS_PROFILE
    base = ["aws", "--profile", profile]
    try:
        who = _run(base + ["sts", "get-caller-identity", "--output", "json"], 25)
    except (subprocess.TimeoutExpired, OSError) as e:
        return _stamp(Integration(name="aws", ok=False, detail=f"sts failed: {e}"))

    if who.returncode != 0:
        err = (who.stderr or "").strip()
        low = err.lower()
        if "sso" in low or "token" in low or "expired" in low:
            detail = f"SSO session expired - aws sso login --profile {profile}"
        elif "could not be found" in low or "invalid choice" in low:
            detail = f"profile '{profile}' not configured - set OTTO_AWS_PROFILE"
        else:
            first = err.splitlines()
            detail = f"[{profile}] {first[0] if first else 'sts error'}"
        return _stamp(Integration(name="aws", ok=False, mode="unconfigured", detail=detail))

    try:
        acct = json.loads(who.stdout).get("Account", "?")
    except json.JSONDecodeError:
        acct = "?"
    detail = f"[{profile}] session ok (acct {acct})"
    try:
        fns = _run(
            base + ["lambda", "list-functions", "--query", "length(Functions)", "--output", "text"],
            30)
        if fns.returncode == 0 and fns.stdout.strip().isdigit():
            detail += f", {fns.stdout.strip()} lambdas"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return _stamp(Integration(name="aws", ok=True, detail=detail))


# ---- MCP-only (no Python credentials; Otto does not fake these) ------------

def _mcp_registrations() -> dict[str, str]:
    """Map MCP server name -> where it is registered.

    Servers live in two places in ~/.claude.json: top-level `mcpServers` (global)
    and per-project `projects.<path>.mcpServers`. Checking only the former
    reports project-scoped servers as missing, which is wrong.
    """
    found: dict[str, str] = {}
    try:
        cfg = json.loads((config.HOME / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return found
    for name in cfg.get("mcpServers") or {}:
        found[name] = "global"
    for proj, body in (cfg.get("projects") or {}).items():
        if not isinstance(body, dict):
            continue
        for name in body.get("mcpServers") or {}:
            found.setdefault(name, f"project:{Path(proj).name}")
    return found


def _mcp_only(name: str, server: str) -> Integration:
    regs = _mcp_registrations()
    where = regs.get(server)
    if where:
        detail = (f"MCP server '{server}' registered ({where}) "
                  "- liveness verified in-session, not by Otto")
    else:
        detail = f"MCP server '{server}' NOT registered in ~/.claude.json"
    return _stamp(Integration(name=name, ok=bool(where), mode="mcp-only", detail=detail))


def probe_notion() -> Integration:
    """Notion is the one MCP-only integration Otto itself leans on: meeting notes
    are ingested from it (otto/meetings.py), so a missing registration means the
    meeting-notes schedule can never succeed."""
    return _mcp_only("notion", "notion")


# ---- local harness -----------------------------------------------------------

def probe_herdr() -> Integration:
    """Is the herdr server answering? herdr is the harness the sessions live in
    (otto/herdr.py), so a dead server means no pane view, no dispatch, and a rail
    that silently stops updating. Not installed is `unconfigured` (a warn row, like
    a missing CLI), because a machine without herdr is a choice, not an outage.
    Installed but down is a real failure and reports like any other integration."""
    from .. import herdr  # noqa: PLC0415 - herdr pulls in the store; keep the probe import light

    if not herdr.available():
        return _stamp(Integration(name="herdr", ok=False, mode="unconfigured",
                                  detail="herdr is not installed (herdr.dev); sessions run "
                                         "outside the harness"))
    try:
        snap = herdr.snapshot()
    except herdr.HerdrError as e:
        return _stamp(Integration(name="herdr", ok=False, detail=f"snapshot failed: {e}"))
    if snap is None:
        return _stamp(Integration(name="herdr", ok=False,
                                  detail="server not running - otto herdr up"))
    agents = herdr.agents(snap)
    blocked = sum(1 for a in agents if a.get("agent_status") == "blocked")
    detail = (f"server up, {len(agents)} agent(s) in {len(herdr.workspaces(snap))} "
              f"workspace(s)" + (f", {blocked} blocked" if blocked else ""))
    return _stamp(Integration(name="herdr", ok=True, detail=detail,
                              metric=float(len(agents)), metric_label="agents"))


PROBES = [
    probe_anthropic,
    probe_aws,
    probe_notion,
    probe_herdr,
]


def probe_names() -> list[str]:
    """Every probe this build knows, by the name OTTO_INTEGRATIONS uses."""
    return [fn.__name__.replace("probe_", "") for fn in PROBES]


def enabled_probes() -> list:
    """PROBES filtered by config.INTEGRATIONS: exactly the named ones, so a disabled
    product has no row at all rather than a permanent red one, and an unconfigured
    install (nothing named) talks to nothing."""
    want = set(config.INTEGRATIONS)
    return [fn for fn in PROBES if fn.__name__.replace("probe_", "") in want]


def probe_all() -> list[Integration]:
    out: list[Integration] = []
    for fn in enabled_probes():
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001 - a broken probe must not stop the sweep
            out.append(_stamp(Integration(
                name=getattr(fn, "__name__", "probe").replace("probe_", ""),
                ok=False, detail=f"probe raised: {e}")))
    return out
