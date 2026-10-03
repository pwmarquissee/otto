"""Discovery.

Walks the global ~/.claude config plus every project root and builds one list of
what agents, commands, and skills exist across the system. Checksums let Otto
notice a definition changed; `missing` marks an entry that vanished rather than
deleting the history of having seen it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import RegistryEntry, iso, utcnow

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _mtime(path: Path) -> str | None:
    try:
        return iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return None


def _description(path: Path) -> str | None:
    """Pull `description:` out of YAML frontmatter without a yaml dependency.

    Definitions here use a flat frontmatter block, so a line scan is enough and
    avoids adding PyYAML for one field.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER.match(text)
    if not m:
        # Some skills lead with a heading instead. Fall back to the first
        # non-empty, non-heading line.
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-", "---")):
                return line[:300]
        return None
    block = m.group(1)
    out: list[str] = []
    capturing = False
    for line in block.splitlines():
        if line.lower().startswith("description:"):
            out.append(line.split(":", 1)[1].strip())
            capturing = True
        elif capturing and (line.startswith((" ", "\t")) or not line.strip()):
            out.append(line.strip())  # folded multi-line description
        elif capturing:
            break
    desc = " ".join(p for p in out if p).strip().strip("\"'")
    return desc[:300] or None


def _scan_claude_dir(
    claude_dir: Path, scope: str, repo: str | None, domain: str = config.WORK
) -> list[RegistryEntry]:
    entries: list[RegistryEntry] = []
    for sub, kind in config.DEFINITION_DIRS.items():
        d = claude_dir / sub
        if not d.is_dir():
            continue
        if kind == "skill":
            # Skills are directories containing SKILL.md.
            for child in sorted(d.iterdir()):
                manifest = child / "SKILL.md"
                if child.is_dir() and manifest.is_file():
                    entries.append(
                        RegistryEntry(
                            name=child.name,
                            kind="skill",
                            scope=scope,
                            domain=domain,
                            repo=repo,
                            path=str(manifest),
                            description=_description(manifest),
                            checksum=_sha(manifest),
                            mtime=_mtime(manifest),
                        )
                    )
        else:
            for f in sorted(d.glob("*.md")):
                entries.append(
                    RegistryEntry(
                        name=f.stem,
                        kind=kind,  # type: ignore[arg-type]
                        scope=scope,
                        domain=domain,
                        repo=repo,
                        path=str(f),
                        description=_description(f),
                        checksum=_sha(f),
                        mtime=_mtime(f),
                    )
                )
    return entries


def discover() -> list[RegistryEntry]:
    """Full sweep: global config, then every project root."""
    found: list[RegistryEntry] = []

    if (config.CLAUDE_DIR).is_dir():
        found += _scan_claude_dir(config.CLAUDE_DIR, "global", None, config.WORK)

    for root, root_domain in config.DOMAIN_ROOTS.items():
        if not root.is_dir():
            continue
        for repo_dir in sorted(root.iterdir()):
            if not repo_dir.is_dir():
                continue
            claude_dir = repo_dir / ".claude"
            claude_md = repo_dir / "CLAUDE.md"
            has_cfg = claude_dir.is_dir()
            if not has_cfg and not claude_md.is_file():
                continue
            repo_name = f"{root.name}/{repo_dir.name}"
            found.append(
                RegistryEntry(
                    name=repo_dir.name,
                    kind="project",
                    scope="repo",
                    domain=root_domain,
                    repo=repo_name,
                    path=str(repo_dir),
                    description=("has .claude/" if has_cfg else "") + ("CLAUDE.md" if claude_md.is_file() else ""),
                    checksum=_sha(claude_md) if claude_md.is_file() else None,
                    mtime=_mtime(claude_md) if claude_md.is_file() else _mtime(repo_dir),
                )
            )
            if has_cfg:
                found += _scan_claude_dir(claude_dir, "repo", repo_name, root_domain)

    return found


def reconcile(previous: list[RegistryEntry], current: list[RegistryEntry]) -> tuple[list[RegistryEntry], list[str]]:
    """Merge a fresh sweep into the stored registry.

    Returns (entries, change notes). Preserves first_seen, flags drift, and
    marks vanished definitions `missing` instead of dropping them, so a deleted
    agent is visible rather than silently gone.
    """
    prev_by_key = {e.key: e for e in previous}
    cur_by_key = {e.key: e for e in current}
    notes: list[str] = []
    now = iso(utcnow())
    merged: list[RegistryEntry] = []

    for key, entry in cur_by_key.items():
        old = prev_by_key.get(key)
        if old is None:
            notes.append(f"new {entry.kind}: {entry.name}")
        else:
            entry.first_seen = old.first_seen
            if old.checksum and entry.checksum and old.checksum != entry.checksum:
                notes.append(f"changed {entry.kind}: {entry.name}")
            if old.missing:
                notes.append(f"returned {entry.kind}: {entry.name}")
        entry.last_seen = now
        entry.missing = False
        merged.append(entry)

    for key, old in prev_by_key.items():
        if key in cur_by_key:
            continue
        if not old.missing:
            notes.append(f"missing {old.kind}: {old.name}")
        old.missing = True
        merged.append(old)

    merged.sort(key=lambda e: (e.kind, e.scope, e.repo or "", e.name))
    return merged, notes


def summarize(entries: list[RegistryEntry], domain: str | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        if domain and e.domain != domain:
            continue
        if e.missing:
            out["missing"] = out.get("missing", 0) + 1
            continue
        out[e.kind] = out.get(e.kind, 0) + 1
    return out


def summarize_by_domain(entries: list[RegistryEntry]) -> dict[str, dict[str, int]]:
    return {d: summarize(entries, d) for d in config.DOMAINS}
