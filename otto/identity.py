"""Whose credential is Otto using, and what will the target system's log say.

`manifest.py` asks whether a credential is present. This module asks who it belongs
to. They are different failures and only one of them is visible to a probe: an
integration can be green, verified, and still be Otto acting as the owner, in which case
the target system's audit log records a human doing something no human did.

That is the gap Otto has not closed. It has the board (enumerated work, origin
provenance, propose-not-act), stored events with timestamps, a deterministic journal
with no model in it, a liveness watchdog, and policy as a constant enforced in code
rather than prose an agent could rewrite. All of those are claims Otto makes about
itself. Attribution is the one claim a *target system* makes about Otto, which is why
it is worth more than the rest and why it must not be oversold as already solved.

Three rules, all load-bearing:

  NAMES ONLY      Same rule as `manifest.py`. This module reads registry keys and env
                  key NAMES. Never a value, never a length, never a hash. Principal
                  identifiers (a client id, a secret's path) are fine and are exactly
                  what an audit log shows; the secret behind them is not.

  SCOREBOARD IS   `otto identity` reports the standing gap every time. It does not
  NOT AN ALARM    raise it as a gap row, because the standing gap is known, accepted,
                  and already a task on the board. A detector that fires daily about
                  a thing the owner chose teaches them to ignore the detector, which costs
                  more than the row is worth.

  ALARM ON        `gaps()` fires on movement: a new integration wired to the owner's
  REGRESSION      account (count above `INHERITED_BASELINE`), a secret appearing
                  inline where the table says it should not be, or a registered
                  server carrying an inline secret nobody declared. Those are all
                  "something changed and nobody decided it", which is the only class
                  of identity finding worth interrupting a morning for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

from . import config
from .manifest import CODE_REGISTRY, DESKTOP_REGISTRY

# Findings that mean "reality and the table disagree". Kept as one tuple so the
# render, the exit code, and the gap rows cannot drift apart on what counts as bad.
DRIFT_STATES = ("inline-regression", "declaration-stale", "undeclared-inline")


class Finding(NamedTuple):
    principal: config.Principal
    state: str      # ok | known-inline | inline-regression | declaration-stale
                    # | not-registered | unverifiable
    detail: str = ""

    @property
    def ok(self) -> bool:
        # `known-inline` is NOT ok in the sense of good, but it is expected, declared,
        # and tracked. It must not turn the exit code red or a green run becomes
        # impossible until the whole build lands, and an always-red check is ignored.
        return self.state in ("ok", "known-inline", "unverifiable", "not-registered")


def _registry_env_keys(path: Path) -> dict[str, set[str]]:
    """{server name: set of env KEY names} from an MCP registry. Values never read.

    Unions the global `mcpServers` with every per-project one, for the same reason
    `manifest._registry_servers` does: both are real registrations, and an earlier
    version of that function returning early on the global dict made every
    project-scoped server invisible.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    out: dict[str, set[str]] = {}

    def absorb(servers: object) -> None:
        if not isinstance(servers, dict):
            return
        for name, spec in servers.items():
            env = spec.get("env") if isinstance(spec, dict) else None
            keys = set(env.keys()) if isinstance(env, dict) else set()
            # An http server carries its secret in `headers`, not `env`. Reading only
            # `env` made `vendor-mcp` -- a static bearer pasted straight into
            # ~/.claude.json -- look like a server with no inline credential at all,
            # which is the one thing this module exists to notice. Prefixed so the
            # render says WHERE it lives; still a key name, never a value.
            headers = spec.get("headers") if isinstance(spec, dict) else None
            if isinstance(headers, dict):
                keys |= {f"headers.{k}" for k in headers}
            out.setdefault(name, set()).update(keys)

    absorb(raw.get("mcpServers"))
    projects = raw.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict):
                absorb(proj.get("mcpServers"))
    return out


def _secretish(keys: set[str]) -> list[str]:
    """Which env key names look like a secret. Name matching only.

    `EDR_CLIENT_ID`, `RMM_REGION` and `EDR_BASE_URL` are configuration and
    must not match: three false positives per server would bury the two entries that
    genuinely carry a long-lived secret.
    """
    hits = [k for k in sorted(keys)
            if any(tok in k.upper() for tok in config.SECRETISH_ENV_KEYS)]
    return hits


def observe() -> dict[str, set[str]]:
    """Env key names per registered MCP server, both registries merged."""
    seen = _registry_env_keys(CODE_REGISTRY)
    for name, keys in _registry_env_keys(DESKTOP_REGISTRY).items():
        seen.setdefault(name, set()).update(keys)
    return seen


def check() -> list[Finding]:
    """Every declared principal against what the registries actually hold."""
    seen = observe()
    out: list[Finding] = []
    for p in config.PRINCIPALS:
        if p.surface not in (config.SURFACE_CODE_MCP, config.SURFACE_DESKTOP_MCP):
            # Account-bound and external integrations have no local artifact naming
            # the principal. Reported, never guessed at.
            out.append(Finding(p, "unverifiable",
                               "no local artifact names this principal"))
            continue
        if p.name not in seen:
            out.append(Finding(p, "not-registered",
                               "declared here but not in either MCP registry; "
                               "`otto manifest` owns that alarm"))
            continue
        hits = _secretish(seen[p.name])
        if p.credential == config.CRED_UNVERIFIED:
            # Never report `ok` for a row that admits it was not assessed. Absence of
            # an inline secret is not evidence of a scoped principal; the token could
            # be a file on disk holding the owner's OAuth grant, which is the same gap
            # wearing a better hat.
            out.append(Finding(p, "unverifiable",
                               "declared unverified: no inline secret, but the actual "
                               "credential source was not inspected. Assess it before "
                               "counting it either way."))
        elif p.credential == config.CRED_INLINE:
            if hits:
                out.append(Finding(p, "known-inline",
                                   "inline secret as declared: " + ", ".join(hits)))
            else:
                out.append(Finding(p, "declaration-stale",
                                   "declared inline, no inline secret found. If this was "
                                   "moved to a store, update the row and lower "
                                   "INHERITED_BASELINE in the same change."))
        else:
            if hits:
                out.append(Finding(p, "inline-regression",
                                   "declared " + p.credential + ", found inline: "
                                   + ", ".join(hits)))
            else:
                out.append(Finding(p, "ok"))
    return out


def undeclared_inline() -> list[tuple[str, list[str]]]:
    """Registered servers carrying an inline secret that no `Principal` declares.

    The drift that matters most, because it is access nobody is accountable for.
    A server nobody wrote down holds a long-lived credential nobody rotates, and it
    is how a second EDR API client ends up authenticating in the dark.
    """
    declared = {p.name for p in config.PRINCIPALS}
    out: list[tuple[str, list[str]]] = []
    for name, keys in sorted(observe().items()):
        if name in declared:
            continue
        hits = _secretish(keys)
        if hits:
            out.append((name, hits))
    return out


def scoreboard() -> dict:
    """The standing attribution picture. Counts, not alarms.

    This is the number the risk-acceptance doc quotes and the number that has to move
    for the build to mean anything.
    """
    counts = {kind: 0 for kind in config.PRINCIPAL_KINDS}
    for p in config.PRINCIPALS:
        counts[p.principal] = counts.get(p.principal, 0) + 1
    total = len(config.PRINCIPALS)
    inherited = counts.get(config.PRINCIPAL_HUMAN, 0)
    return {
        "total": total,
        "attributable": counts.get(config.PRINCIPAL_AGENT, 0),
        "inherited": inherited,
        "baseline": config.INHERITED_BASELINE,
        "counts": counts,
        "inline_credentials": sum(1 for p in config.PRINCIPALS
                                  if p.credential == config.CRED_INLINE),
    }


def gaps() -> list[dict]:
    """Identity drift as advisor-shaped rows. Regression only, never the known gap."""
    rows: list[dict] = []

    for f in check():
        if f.state == "inline-regression":
            rows.append({
                "id": f"gap:identity-inline:{f.principal.key}",
                "kind": "identity-drift",
                "domain": config.WORK,
                "title": f"inline secret appeared for {f.principal.name}",
                "why": f"{f.detail} A long-lived secret in ~/.claude.json is readable by "
                       f"every process that can read the owner's home and is rotatable only "
                       f"by hand. Something re-added it, or the table is wrong.",
                "command": "otto identity",
                "score": 80,
            })
        elif f.state == "declaration-stale":
            rows.append({
                "id": f"gap:identity-stale-decl:{f.principal.key}",
                "kind": "identity-drift",
                "domain": config.WORK,
                "title": f"identity table is stale for {f.principal.name}",
                "why": f.detail,
                "command": "otto identity",
                "score": 35,
            })

    for name, hits in undeclared_inline():
        rows.append({
            "id": f"gap:identity-undeclared:{name}",
            "kind": "identity-drift",
            "domain": config.WORK,
            "title": f"undeclared inline credential: {name}",
            "why": "This server holds a long-lived secret and no row in config.PRINCIPALS "
                   "says whose principal it is or what the audit log will show. "
                   f"Keys: {', '.join(hits)}. Declare it or remove it.",
            "command": "otto identity",
            "score": 70,
        })

    board = scoreboard()
    if board["inherited"] > board["baseline"]:
        rows.append({
            "id": "gap:identity-new-inheritance",
            "kind": "identity-drift",
            "domain": config.WORK,
            "title": f"{board['inherited']} integrations now authenticate as {config.OWNER_NAME}, "
                     f"baseline is {board['baseline']}",
            "why": "A new integration inherited the owner's account rather than getting "
                   "its own principal. That is the moment to decide, not months later: "
                   "actions Otto takes there will be indistinguishable from the owner's in "
                   "that system's audit log. See docs/agent-identity.md.",
            "command": "otto identity",
            "score": 75,
        })
    elif board["inherited"] < board["baseline"]:
        rows.append({
            "id": "gap:identity-baseline-stale",
            "kind": "identity-drift",
            "domain": config.WORK,
            "title": f"INHERITED_BASELINE says {board['baseline']}, reality is "
                     f"{board['inherited']}",
            "why": "Fewer integrations inherit the owner's account than the baseline admits, "
                   "so the regression detector has that much slack in it. Lower the "
                   "baseline to lock the win in.",
            "command": "otto identity",
            "score": 30,
        })

    return rows


def render(verbose: bool = False) -> str:
    """Human-readable report for `otto identity`."""
    findings = {f.principal.key: f for f in check()}
    board = scoreboard()

    lines = ["agent identity  (who Otto authenticates as; names only, never values)", ""]

    order = {config.PRINCIPAL_AGENT: 0, config.PRINCIPAL_SHARED: 1,
             config.PRINCIPAL_HUMAN: 2, config.PRINCIPAL_UNVERIFIED: 3}
    rows = sorted(config.PRINCIPALS, key=lambda p: (order.get(p.principal, 9), p.name))
    name_w = max(len(p.name) for p in rows)
    prin_w = max(len(p.principal) for p in rows)
    cred_w = max(len(p.credential) for p in rows)

    glyph = {"ok": "  ok", "known-inline": "INLINE", "inline-regression": "  BACK",
             "declaration-stale": " STALE", "not-registered": "  MISS",
             "unverifiable": "     ?"}

    lines.append(f"  {'':>6}  {'integration':<{name_w}}  {'principal':<{prin_w}}  "
                 f"{'credential':<{cred_w}}")
    for p in rows:
        f = findings.get(p.key)
        state = f.state if f else "unverifiable"
        mark = glyph.get(state, "  ??")
        if state == "ok" and not p.attributable:
            # `ok` here only ever meant "the credential is where the table says". On a
            # `human` row that reads as approval, which is the exact flattery this
            # module exists to refuse. Only a principal Otto alone holds gets a tick.
            mark = "     -"
        lines.append(f"  {mark:>6}  {p.name:<{name_w}}  "
                     f"{p.principal:<{prin_w}}  {p.credential:<{cred_w}}")
        if verbose:
            if p.identifier:
                lines.append(f"          id:      {p.identifier}")
            lines.append(f"          audit:   {p.audit}")
            if p.blocker:
                lines.append(f"          blocker: {p.blocker}")
            if f and f.detail and state in DRIFT_STATES + ("known-inline",):
                lines.append(f"          note:    {f.detail}")
            lines.append("")

    extra = undeclared_inline()
    if extra:
        lines += ["", "undeclared inline credentials:"]
        lines += [f"        {n}  ({', '.join(h)})" for n, h in extra]

    lines += [
        "",
        f"{board['attributable']} of {board['total']} integrations attributable to "
        f"{config.PERSONA_NAME}; {board['inherited']} authenticate as {config.OWNER_NAME} "
        f"(baseline {board['baseline']}); {board['inline_credentials']} hold an inline "
        f"secret.",
    ]
    if board["attributable"] == 0:
        lines.append(f"{config.PERSONA_NAME} has no principal of its own yet. Nothing "
                     f"it does is provable from any target system's audit log. "
                     f"Provisioning steps: docs/agent-identity.md")
    lines.append("mark is credential drift only: ok = attributable and where declared, "
                 "- = no drift but not attributable, INLINE = declared inline secret, "
                 "BACK = inline where the table says store, STALE = table needs "
                 "updating, ? = nothing local to inspect.")
    if not verbose:
        lines.append("`otto identity -v` for the audit field and blocker per row.")
    return "\n".join(lines)
