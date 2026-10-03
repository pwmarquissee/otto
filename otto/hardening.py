"""Per-definition blast radius: what a definition may write, and whether it says so.

Stolen from Gaia (2026-08-03), where several skills ship HARDENING.md and
CONFORMANCE.md alongside SKILL.md. Otto had 144 definitions and zero of either.

Why Otto specifically needs this. The cold-start incident: a task left in `queued`
from testing was auto-dispatched, and the agent edited `daily.md` -- a live ops file
nobody had approved changing. The lesson recorded at the time was *correct is not the
same as authorised*. Nothing in the system was wrong; the edit was a reasonable thing
for that agent to do. What was missing was a written boundary to be outside of.

So the two documents answer two different questions, and neither substitutes:

  HARDENING.md    Prose, for a human and for the agent reading its own definition.
                  Blast radius, allowed write paths, credentials, partial-failure
                  behaviour, and what is explicitly OUT of scope. The out-of-scope
                  section is the load-bearing one -- it is the part the cold-start
                  agent had no way to read.

  CONFORMANCE.md  Machine-checkable assertions, not prose. A HARDENING.md that has
                  drifted away from its definition is worse than none, because it is
                  now a false assurance. `otto skills audit --verify` re-checks every
                  assertion against the definition on disk.

Two rules mirrored from manifest.py, which is the prior art for declared-vs-observed:

  DECLARE BOTH   Tier is DECLARED for the surfaces whose blast radius is already
                 known (`DECLARED_SURFACES`, `DECLARED_TIER1`) and INFERRED for
                 everything else. Inference alone would silently re-tier a critical
                 definition down the moment someone reworded it.
  NEVER EXECUTE  Conformance directives are evaluated in pure Python against file
                 text. There is no shell-out. A doc format that ran commands would
                 make every CONFORMANCE.md a new dispatch surface, which would be a
                 remarkable thing for a hardening tool to add.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from . import config, registry

REPO = Path(__file__).resolve().parent.parent

# Docs for file-based definitions live here rather than next to the definition.
# This is not a style choice: ~/.claude/{commands,agents} is junctioned to the repo
# and Claude Code registers EVERY .md in those trees. A `daily.HARDENING.md` sibling
# would become a slash command called `/daily.HARDENING`. Skills are directories and
# only SKILL.md is read, so skill docs co-locate the way Gaia's do.
SIDECAR_ROOT = REPO / "claude" / "hardening"
TEMPLATE_DIR = SIDECAR_ROOT / "_templates"

HARDENING_DOC = "HARDENING.md"
CONFORMANCE_DOC = "CONFORMANCE.md"

TIER_BOTH = 1        # can dispatch, write outside its own outputs, or touch prod
TIER_HARDENING = 2   # writes files at all
TIER_NONE = 3        # read-only reporting

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TOOLS_LINE = re.compile(r"^tools:\s*(.*)$", re.MULTILINE)

# ---------------------------------------------------------------------------
# declared surfaces
# ---------------------------------------------------------------------------

class Surface(NamedTuple):
    """A tier-1 blast radius that is NOT a Claude definition.

    `otto dispatch`/`launch`/`refresh` are the sharpest edges
    Otto owns, and none of them is a .md file, so a definition-only sweep would rate
    Otto's own dispatcher as out of scope. They are declared here instead.
    """
    name: str
    kind: str      # "otto-module" | "script"
    target: str    # repo-relative path of the thing being documented
    why: str


DECLARED_SURFACES: tuple[Surface, ...] = (
    Surface("dispatch", "otto-module", "otto/dispatch.py",
            "Spawns queued board tasks as real Claude Code sessions with "
            "--dangerously-skip-permissions. The cold-start incident came through here."),
    Surface("launch", "otto-module", "otto/launch.py",
            "Runs a schedule on demand, including slash-command schedules that run "
            "/triage and /orchestrate under skip-permissions."),
    Surface("refresh", "otto-module", "otto/refresh.py",
            "One of two auto-launching runners in Otto. Narrow by construction, which "
            "is exactly why the narrowness needs to be written down and re-checked."),
    Surface("meetings", "otto-module", "otto/meetings.py",
            "The second auto-launching runner: reads Notion meeting notes unattended "
            "under skip-permissions and files board cards from what it finds. Same "
            "argument as refresh, and the same dependence on a complete deny list."),
    Surface("sessions", "otto-module", "otto/sessions.py",
            "The only Otto module that writes into ~/.claude/settings.json, the file "
            "governing every Claude Code session on this workstation. It also "
            "installs a command that then runs inside every turn the owner takes."),
)

# Tier 1 by declaration, not inference. Keys are "<kind>:<name>", scope-global only.
# Add your own operator commands here as you write them: anything that promotes,
# dispatches, or writes to the board unattended belongs on this list.
DECLARED_TIER1: dict[str, str] = {
    "command:orchestrate": "Promotes board tasks into `queued`, which IS the dispatch "
                           "trigger. Its whole job is deciding what runs unattended.",
    "command:triage": "Assesses backlog cards and promotes what may run; a wrong tier "
                      "here turns a suggestion into an unattended agent run.",
}

# ---------------------------------------------------------------------------
# capability signals
# ---------------------------------------------------------------------------

# Category -> (label, pattern). Categories map to tiers below. Patterns stay specific:
# a body that merely says the word "write" is not a write capability, but a frontmatter
# `tools:` entry granting Write is, and so is Bash.
_FM_SIGNALS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "ungated": (
        ("bypassPermissions", re.compile(r"permissionMode:\s*bypassPermissions")),
    ),
}

_TOOL_SIGNALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("ungated", "tools:*",   re.compile(r"^\s*[\"']?\*[\"']?\s*$")),
    ("write",   "tools:Write", re.compile(r"\b(Write|Edit|MultiEdit|NotebookEdit)\b")),
    ("write",   "tools:Bash",  re.compile(r"\bBash\b")),
    ("dispatch", "tools:Task", re.compile(r"\b(Task|Agent)\b")),
)

_BODY_SIGNALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # dispatch: this thing can start another session that can change things
    ("dispatch", "skip-permissions",
     re.compile(r"dangerously-skip-permissions")),
    ("dispatch", "otto-dispatch",
     re.compile(r"otto\s+(?:task\s+run|spawn|launch)\b|autodispatch")),
    ("dispatch", "promote-to-queued",
     re.compile(r"task\s+mv\s+\S+\s+queued|[\"']status[\"']\s*:\s*[\"']queued")),
    ("dispatch", "subagents",
     re.compile(r"\bdispatch\s+(?:\d+[-\s]?\d*\s+)?subagents?\b|"
                r"\bsubagents?\s+in\s+parallel\b|\bUse the \w+ agent\b")),
    # prod: reaches a system outside this workstation and changes it
    ("prod", "slack-write",
     re.compile(r"slack_send_message|slack_schedule_message|slack_create_canvas")),
    ("prod", "okta-write",
     re.compile(r"okta[-_]mcp[-_]server|(?:activate|deactivate|create|update|delete)_"
                r"(?:user|group|application|policy)|add_user_to_group")),
    ("prod", "notion-write",
     re.compile(r"notion-(?:create|update|move|duplicate)")),
    # Vendor-specific write tools (an EDR console, an RMM) are declared per deployment
    # in config.PROD_WRITE_SIGNALS as (label, regex) pairs, because their MCP tool names
    # are the vendor's and belong to whoever runs them. Optional, hence getattr.
    *(("prod", label, re.compile(pat))
      for label, pat in getattr(config, "PROD_WRITE_SIGNALS", ())),
    ("prod", "gam",
     re.compile(r"\bgam(?:\.exe)?\s+(?:user|group|update|create|delete|print)\b|"
                r"GAM7[\\/]gam")),
    ("prod", "aws-write",
     re.compile(r"aws\s+\S+\s+(?:create|delete|put|update|modify|terminate|attach)")),
    # write: touches the filesystem or Otto's own state
    ("write", "otto-task-add", re.compile(r"otto\s+task\s+add\b")),
    ("write", "otto-state",    re.compile(r"otto\s+(?:stamp|propose|task\s+set)\b")),
    ("write", "http-mutate",
     re.compile(r"-X\s+(?:POST|PATCH|PUT|DELETE)")),
    ("write", "powershell-write",
     re.compile(r"Set-Content|Out-File|Add-Content|New-Item|Remove-Item")),
    ("write", "python-write",
     re.compile(r"json\.dump|write_text\(|open\([^)]*[\"'][wa]")),
)

_CATEGORY_TIER = {"ungated": TIER_BOTH, "dispatch": TIER_BOTH, "prod": TIER_BOTH,
                  "write": TIER_HARDENING}


def _frontmatter(text: str) -> str:
    m = _FRONTMATTER.match(text)
    return m.group(1) if m else ""


def classify(text: str) -> tuple[int, tuple[str, ...]]:
    """Infer tier and the signals that produced it. Text is the definition source."""
    fm = _frontmatter(text)
    body = text[len(fm):] if fm else text
    found: set[tuple[str, str]] = set()

    for cat, pats in _FM_SIGNALS.items():
        for label, pat in pats:
            if pat.search(fm):
                found.add((cat, label))

    tools = _TOOLS_LINE.search(fm)
    if tools:
        value = tools.group(1)
        for cat, label, pat in _TOOL_SIGNALS:
            if pat.search(value):
                found.add((cat, label))

    for cat, label, pat in _BODY_SIGNALS:
        if pat.search(body):
            found.add((cat, label))

    tier = min((_CATEGORY_TIER[c] for c, _ in found), default=TIER_NONE)
    return tier, tuple(sorted(f"{c}:{l}" for c, l in found))


def needs(tier: int) -> tuple[str, ...]:
    if tier == TIER_BOTH:
        return (HARDENING_DOC, CONFORMANCE_DOC)
    if tier == TIER_HARDENING:
        return (HARDENING_DOC,)
    return ()


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------

class Item(NamedTuple):
    name: str
    kind: str                    # skill | command | agent | otto-module | script
    scope: str                   # "global" or "<root>/<repo>"
    source: Path                 # the definition itself
    doc_dir: Path                # where its HARDENING/CONFORMANCE belong
    tier: int
    declared: bool               # tier came from a declaration, not inference
    signals: tuple[str, ...]
    present: tuple[str, ...]     # docs that exist
    missing: tuple[str, ...]     # docs required by tier and absent
    source_exists: bool
    why: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.kind}:{self.name}"

    @property
    def ok(self) -> bool:
        return self.source_exists and not self.missing


def _scope_slug(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", scope) or "global"


def doc_dir_for(kind: str, name: str, scope: str, source: Path) -> Path:
    """Skills co-locate; everything else goes under claude/hardening/. See SIDECAR_ROOT."""
    if kind == "skill":
        return source.parent
    return SIDECAR_ROOT / _scope_slug(scope) / kind / name


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _item(name: str, kind: str, scope: str, source: Path,
          declared_tier: int | None = None, why: str = "") -> Item:
    exists = source.is_file()
    inferred, signals = classify(_read(source)) if exists else (TIER_NONE, ())
    # A declaration only ever raises the tier. If inference finds MORE capability than
    # was declared, that is the interesting direction and it wins.
    tier = min(declared_tier, inferred) if declared_tier else inferred
    ddir = doc_dir_for(kind, name, scope, source)
    required = needs(tier)
    present = tuple(d for d in required if (ddir / d).is_file())
    return Item(name=name, kind=kind, scope=scope, source=source, doc_dir=ddir,
                tier=tier, declared=declared_tier is not None, signals=signals,
                present=present,
                missing=tuple(d for d in required if d not in present),
                source_exists=exists, why=why)


def scan() -> list[Item]:
    """Every auditable surface: declared non-definition surfaces, then definitions."""
    out: list[Item] = []

    for s in DECLARED_SURFACES:
        out.append(_item(s.name, s.kind, "global", REPO / s.target,
                         declared_tier=TIER_BOTH, why=s.why))

    for e in registry.discover():
        if e.kind not in config.DEFINITION_DIRS.values():
            continue
        scope = e.repo if e.scope == "repo" and e.repo else "global"
        declared = DECLARED_TIER1.get(f"{e.kind}:{e.name}") if scope == "global" else None
        out.append(_item(e.name, e.kind, scope, Path(e.path),
                         declared_tier=TIER_BOTH if declared else None,
                         why=declared or ""))

    out.sort(key=lambda i: (i.tier, i.kind, i.scope, i.name))
    return out


# ---------------------------------------------------------------------------
# conformance
# ---------------------------------------------------------------------------

_BLOCK = re.compile(r"<!--\s*otto-conformance\s*(.*?)-->", re.DOTALL)
_DIRECTIVE = re.compile(r"^(must-contain|must-not-contain|file-exists|file-absent)"
                        r":\s*(.+?)\s*(?:#\s*(.*))?$")


class Assertion(NamedTuple):
    kind: str
    argument: str
    note: str
    passed: bool
    detail: str


def _matches(text: str, argument: str) -> bool:
    """`/regex/` is a pattern; anything else is a literal substring."""
    if len(argument) > 2 and argument.startswith("/") and argument.endswith("/"):
        try:
            return re.search(argument[1:-1], text) is not None
        except re.error:
            return False
    return argument in text


def assertions(item: Item) -> list[Assertion]:
    """Evaluate item's CONFORMANCE.md against its source. Pure text, never executes."""
    doc = item.doc_dir / CONFORMANCE_DOC
    if not doc.is_file():
        return []
    out: list[Assertion] = []
    source = _read(item.source)
    for block in _BLOCK.findall(_read(doc)):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _DIRECTIVE.match(line)
            if not m:
                out.append(Assertion("malformed", line, "", False,
                                     "not a recognised directive"))
                continue
            kind, arg, note = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            arg = arg.strip('"').strip("'")
            if kind == "must-contain":
                ok, detail = _matches(source, arg), f"absent from {item.source.name}"
            elif kind == "must-not-contain":
                ok, detail = not _matches(source, arg), f"present in {item.source.name}"
            elif kind == "file-exists":
                ok, detail = (REPO / arg).exists(), "no such path"
            else:
                ok, detail = not (REPO / arg).exists(), "path still exists"
            out.append(Assertion(kind, arg, note, ok, "" if ok else detail))
    return out


def verify(items: list[Item] | None = None) -> list[tuple[Item, list[Assertion]]]:
    """Every item that has a CONFORMANCE.md, with its assertion results."""
    out = []
    for item in items if items is not None else scan():
        checks = assertions(item)
        if checks:
            out.append((item, checks))
    return out


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------

def gaps() -> list[dict]:
    """Audit findings as advisor-shaped rows, wired the same way manifest.gaps() is.

    Tier 1 is reported per item because each one is individually actionable and the
    set is small. Tier 2 is AGGREGATED on purpose: there are ~50 of them and fifty
    rows would bury every other gap Otto reports, which is the failure mode this
    whole module exists to avoid.
    """
    rows: list[dict] = []
    items = scan()

    for item in items:
        if not item.source_exists:
            rows.append({
                "id": f"gap:hardening-vanished:{item.key}",
                "kind": "hardening-drift",
                "domain": config.WORK,
                "title": f"declared tier-1 surface is gone: {item.name} "
                         f"({item.kind})",
                "why": f"{item.source} was declared in hardening.DECLARED_SURFACES and "
                       f"is not on disk. Either it moved and the declaration is stale, "
                       f"or a dispatch surface was deleted without anyone saying so.",
                "command": "otto skills audit",
                "score": 65,
            })

    for item in items:
        if item.tier != TIER_BOTH or not item.missing or not item.source_exists:
            continue
        both = len(item.missing) == 2
        rows.append({
            "id": f"gap:hardening-tier1:{item.key}",
            "kind": "hardening-missing",
            "domain": config.WORK,
            "title": f"tier-1 {item.kind} with no {' + '.join(item.missing)}: "
                     f"{item.name}",
            "why": (item.why or "Signals: " + ", ".join(item.signals) + ".") +
                   " Nothing states what it may write, so nothing can tell an "
                   "authorised change from a merely correct one.",
            "command": "otto skills audit",
            "score": 75 if both else 60,
        })

    tier2 = [i for i in items
             if i.tier == TIER_HARDENING and i.missing and i.source_exists]
    if tier2:
        sample = ", ".join(i.name for i in sorted(tier2, key=lambda x: x.name)[:5])
        rows.append({
            "id": "gap:hardening-tier2",
            "kind": "hardening-missing",
            "domain": config.WORK,
            "title": f"{len(tier2)} file-writing definition(s) have no {HARDENING_DOC}",
            "why": f"{sample}{'...' if len(tier2) > 5 else ''}. Each declares write "
                   f"tools and none states which paths are in bounds.",
            "command": "otto skills audit --tier 2",
            "score": 35,
        })

    for item, checks in verify(items):
        failed = [a for a in checks if not a.passed]
        if not failed:
            continue
        rows.append({
            "id": f"gap:conformance-fail:{item.key}",
            "kind": "conformance-fail",
            "domain": config.WORK,
            "title": f"{item.name} broke {len(failed)} of its own documented "
                     f"invariant(s)",
            "why": "; ".join(f"{a.kind} {a.argument!r}: {a.detail}"
                             for a in failed[:3]) +
                   ". A HARDENING.md that no longer matches its definition is a false "
                   "assurance, which is worse than having none.",
            "command": f"otto skills audit --verify {item.name}",
            "score": 85,
        })

    return rows


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_TIER_LABEL = {
    TIER_BOTH: "tier 1  dispatch / writes outside itself / touches prod  "
               f"[needs {HARDENING_DOC} + {CONFORMANCE_DOC}]",
    TIER_HARDENING: f"tier 2  writes files  [needs {HARDENING_DOC}]",
    TIER_NONE: "tier 3  read-only  [needs neither]",
}


def render(tier: int | None = None, show_ok: bool = False) -> str:
    items = scan()
    lines = ["definition hardening audit  (tier is declared where known, else inferred)",
             ""]
    for t in (TIER_BOTH, TIER_HARDENING, TIER_NONE):
        if tier and t != tier:
            continue
        group = [i for i in items if i.tier == t]
        if not group:
            continue
        bad = [i for i in group if not i.ok]
        lines.append(_TIER_LABEL[t])
        if t == TIER_NONE:
            lines.append(f"        {len(group)} definitions, nothing required")
            lines.append("")
            continue
        for i in group:
            if i.ok and not show_ok:
                continue
            h = "H" if HARDENING_DOC in i.present else "-"
            c = ("C" if CONFORMANCE_DOC in i.present
                 else ("-" if t == TIER_BOTH else " "))
            flag = "  DECLARED" if i.declared else ""
            where = "" if i.scope == "global" else f"  ({i.scope})"
            state = "  SOURCE MISSING" if not i.source_exists else ""
            lines.append(f"  [{h}{c}]  {i.kind:<11} {i.name}{where}{flag}{state}")
            if not i.source_exists:
                lines.append(f"          {i.source}")
            elif i.signals:
                lines.append(f"          {', '.join(i.signals)}")
        lines.append(f"        {len(group) - len(bad)}/{len(group)} documented")
        lines.append("")

    checks = verify(items)
    if checks:
        failed = [(i, [a for a in cs if not a.passed]) for i, cs in checks]
        failed = [(i, f) for i, f in failed if f]
        total = sum(len(cs) for _, cs in checks)
        lines.append(f"conformance: {total} assertion(s) across {len(checks)} "
                     f"definition(s), {sum(len(f) for _, f in failed)} failing")
        for i, fs in failed:
            lines.append(f"  FAIL  {i.name}")
            for a in fs:
                lines.append(f"          {a.kind}: {a.argument}  -- {a.detail}")
        lines.append("")

    undocumented = [i for i in items if not i.ok]
    lines.append(f"{len(items)} definitions, {len(undocumented)} missing a required "
                 f"document")
    return "\n".join(lines)


def render_verify(name: str | None = None) -> str:
    """Assertion-by-assertion output, for `otto skills audit --verify [name]`."""
    results = [(i, cs) for i, cs in verify()
               if name is None or i.name == name]
    if not results:
        target = f" for {name}" if name else ""
        return (f"no conformance assertions found{target}. "
                f"Add a {CONFORMANCE_DOC} with an `otto-conformance` block.")
    lines: list[str] = []
    total = failing = 0
    for item, checks in results:
        lines.append(f"{item.kind} {item.name}  ({item.doc_dir / CONFORMANCE_DOC})")
        for a in checks:
            total += 1
            if not a.passed:
                failing += 1
            mark = "ok  " if a.passed else "FAIL"
            lines.append(f"  {mark}  {a.kind}: {a.argument}")
            if a.note:
                lines.append(f"          {a.note}")
            if not a.passed:
                lines.append(f"          -> {a.detail}")
        lines.append("")
    lines.append(f"{total} assertion(s), {failing} failing")
    return "\n".join(lines)
