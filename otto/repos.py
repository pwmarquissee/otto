"""Repositories, derived from state Otto already has.

The dashboard can scope every view to one directory. That needs a list of
repositories, and Otto deliberately does not keep one: inventing a registry of
repos would be a second source of truth that drifts from the filesystem.

So this derives them. A repository is a directory one level under a configured
DOMAIN_ROOT that at least one registry definition, task, or run actually touches.
Nothing is guessed from a name, and a directory nobody has ever worked in does not
appear. Domain comes from the root, never from the repo, so a repo can never
disagree with its own domain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config
from .store import Store


def _root_of(p: Path) -> tuple[Path, str] | None:
    """The DOMAIN_ROOT containing p, and that root's domain."""
    for root, domain in config.DOMAIN_ROOTS.items():
        try:
            p.relative_to(root)
        except ValueError:
            continue
        return root, domain
    return None


def _repo_of(raw: str | None) -> tuple[str, Path, str] | None:
    """(key, path, domain) for a path inside a root, or None if outside every root.

    The repo is the FIRST segment under the root. Deeper nesting collapses upward:
    a worktree at work/api/sub belongs to work/api, because a
    run in a subdirectory is still work on that repo.
    """
    if not raw:
        return None
    try:
        p = Path(raw)
    except (TypeError, ValueError):
        return None
    hit = _root_of(p)
    if hit is None:
        return None
    root, domain = hit
    rel = p.relative_to(root).parts
    if not rel:
        return None
    return f"{root.name}/{rel[0]}", root / rel[0], domain


def collect(store: Store) -> dict[str, Any]:
    """Roots and repos, with the counts the scope picker shows."""
    repos: dict[str, dict[str, Any]] = {}

    def slot(key: str, path: Path, domain: str) -> dict[str, Any]:
        if key not in repos:
            repos[key] = {
                "key": key, "name": path.name, "path": str(path),
                "root": str(path.parent), "domain": domain,
                "defs": 0, "missing": 0, "tasks": 0, "running": 0,
                "stale": 0, "drift": 0, "last": None, "note": "",
            }
        return repos[key]

    # Definitions. registry.repo is already `root-name/repo-name`, but derive from
    # the real path so the two can never disagree.
    for e in store.registry():
        hit = _repo_of(e.path)
        if hit is None:
            continue
        r = slot(*hit)
        r["defs"] += 1
        if e.missing:
            r["missing"] += 1

    # Open work and live runs, by the directory they were spawned in.
    for t in store.tasks():
        hit = _repo_of(getattr(t, "cwd", None))
        if hit is None or t.status == "done":
            continue
        slot(*hit)["tasks"] += 1

    for run in store.runs():
        hit = _repo_of(run.cwd)
        if hit is None:
            continue
        r = slot(*hit)
        if run.status == "running":
            r["running"] += 1
        if run.started and (r["last"] is None or run.started > r["last"]):
            r["last"] = run.started

    for r in repos.values():
        bits = [f"{r['defs']} definition" + ("s" if r["defs"] != 1 else "")]
        if r["missing"]:
            bits.append(f"{r['missing']} missing from disk")
        r["note"] = ", ".join(bits)

    roots = [{"path": str(p), "name": p.name, "domain": d}
             for p, d in config.DOMAIN_ROOTS.items()]
    return {
        "roots": roots,
        "repos": sorted(repos.values(), key=lambda r: (r["root"], r["key"])),
    }


def key_for(rec_cwd: str | None) -> str | None:
    """The repo key a cwd belongs to, for server-side filtering."""
    hit = _repo_of(rec_cwd)
    return hit[0] if hit else None
