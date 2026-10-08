"""Config consolidation: Otto owning the files Claude Code reads.

Layout after migration (<checkout> is wherever this repo lives):

  <checkout>/claude/{agents,commands,skills}   the only copy; ~/.claude junctions here
  <checkout>/claude/CLAUDE.md + *.sh            canonical, but COPIED to ~/.claude
  <checkout>/claude/snapshots/                  backup only, never auto-restored

Only directories the checkout actually has are expected as junctions (JUNCTIONS
below); a fresh clone ships claude/commands and nothing else.

Why two mechanisms. A Windows directory junction is transparent to Claude Code
(verified: a headless session with all file tools denied still resolved
~/.claude/commands/otto.md), which makes junctioned directories a true single
source of truth with no drift and no deploy step.

Single files cannot be junctioned. Symlinks need admin or Developer Mode, neither
of which is available here, and a hardlink silently decouples the moment an editor
does a replace-write, which is exactly how Claude Code's Edit tool saves. So loose
files are real files in ~/.claude with the repo holding the source, and drift is
something Otto reports rather than something it prevents.

settings.json is deliberately NOT canonical here: Claude Code rewrites it itself
(plugin toggles, permission grants), so the repo only ever holds a snapshot.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config

REPO = Path(__file__).resolve().parent.parent
CFG_DIR = REPO / "claude"
SNAP_DIR = CFG_DIR / "snapshots"

# name -> where the real content lives
# Only directories the checkout actually has are expected as junctions. A fresh
# clone ships claude/commands and nothing else; listing agents, skills or an
# orchestrator tree it does not have made every new install show a permanent
# "config junction broken" gap for directories that were never meant to exist.
_CANDIDATE_JUNCTIONS: dict[str, Path] = {
    "agents": CFG_DIR / "agents",
    "commands": CFG_DIR / "commands",
    "skills": CFG_DIR / "skills",
    "orchestrator": REPO / "orchestrator",
}
JUNCTIONS: dict[str, Path] = {k: v for k, v in _CANDIDATE_JUNCTIONS.items() if v.is_dir()}

# Repo is the source; ~/.claude gets a copy. Only files present in the checkout.
DEPLOY_FILES = [f for f in ["CLAUDE.md", "anthropic-env.sh"] if (REPO / "claude" / f).is_file()]

# Claude Code owns these. Repo holds a backup only.
SNAPSHOT_FILES = ["settings.json", "settings.local.json", "config.json"]

# Never copied anywhere, for the avoidance of doubt.
NEVER = [
    ".credentials.json", ".s1-env.cache", ".anthropic-env.cache", ".cs-token.cache",
    "projects", "file-history", "plugins", "cache", "paste-cache", "shell-snapshots",
    "otto", "backups", "history.jsonl", "stats-cache.json",
]


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def junction_status() -> list[dict]:
    """Is each managed directory actually a junction pointing where we expect?"""
    rows = []
    for name, target in JUNCTIONS.items():
        live = config.CLAUDE_DIR / name
        row: dict = {"name": name, "live": str(live), "target": str(target)}
        if not live.exists():
            row.update(ok=False, state="missing", files=0)
        else:
            # readlink() tells us IF it is a reparse point, but on Windows it
            # returns the extended-length form (\\?\D:\...) which resolve() does
            # not normalize, so comparing that string always fails. live.resolve()
            # gives the plain path, so correctness is checked with that instead.
            try:
                linked = live.readlink()
                row["points_at"] = str(linked).replace("\\\\?\\", "")
                row["linked"] = True
            except (OSError, ValueError):
                row["linked"] = False

            try:
                same = live.resolve() == target.resolve()
            except OSError:
                same = False

            if row["linked"] and same:
                row["ok"], row["state"] = True, "junction"
            elif row["linked"]:
                row["ok"], row["state"] = False, "junction -> wrong target"
            else:
                row["ok"], row["state"] = False, "real directory, not a junction"
            try:
                row["files"] = sum(1 for _ in live.rglob("*") if _.is_file())
            except OSError:
                row["files"] = 0
        row["canonical_exists"] = target.is_dir()
        rows.append(row)
    return rows


def deploy_status() -> list[dict]:
    """Drift between the repo copy and the live file, and which side is newer."""
    rows = []
    for name in DEPLOY_FILES:
        repo_p = CFG_DIR / name
        live_p = config.CLAUDE_DIR / name
        rh, lh = _sha(repo_p), _sha(live_p)
        rm, lm = _mtime(repo_p), _mtime(live_p)
        if rh is None and lh is None:
            state, newer = "absent both", None
        elif rh is None:
            state, newer = "only in ~/.claude", "live"
        elif lh is None:
            state, newer = "only in repo", "repo"
        elif rh == lh:
            state, newer = "in sync", None
        else:
            state = "DRIFT"
            newer = "live" if (lm and rm and lm > rm) else "repo"
        rows.append({
            "name": name, "state": state, "newer": newer,
            "repo": str(repo_p), "live": str(live_p),
            "repo_sha": rh, "live_sha": lh,
            "repo_mtime": rm.isoformat() if rm else None,
            "live_mtime": lm.isoformat() if lm else None,
        })
    return rows


def snapshot_status() -> list[dict]:
    rows = []
    for name in SNAPSHOT_FILES:
        snap_p = SNAP_DIR / name
        live_p = config.CLAUDE_DIR / name
        sh, lh = _sha(snap_p), _sha(live_p)
        rows.append({
            "name": name,
            "state": "no snapshot" if sh is None else ("current" if sh == lh else "outdated"),
            "snapshot_mtime": (lambda m: m.isoformat() if m else None)(_mtime(snap_p)),
        })
    return rows


def deploy(dry_run: bool = False) -> list[str]:
    """Push repo copies out to ~/.claude. Repo wins."""
    done = []
    for row in deploy_status():
        if row["state"] in ("in sync", "absent both", "only in ~/.claude"):
            continue
        src, dst = Path(row["repo"]), Path(row["live"])
        if not src.is_file():
            continue
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        done.append(f"{row['name']}: repo -> ~/.claude")
    return done


def adopt(dry_run: bool = False) -> list[str]:
    """Pull live files back into the repo. The live file wins.

    Needed because ~/.claude/CLAUDE.md is the path a human naturally edits, so the
    repo copy goes stale unless there is a way to take the edit back.
    """
    done = []
    for row in deploy_status():
        if row["state"] in ("in sync", "absent both", "only in repo"):
            continue
        src, dst = Path(row["live"]), Path(row["repo"])
        if not src.is_file():
            continue
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        done.append(f"{row['name']}: ~/.claude -> repo")
    return done


def snapshot(dry_run: bool = False) -> list[str]:
    done = []
    for name in SNAPSHOT_FILES:
        live_p = config.CLAUDE_DIR / name
        if not live_p.is_file():
            continue
        if not dry_run:
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live_p, SNAP_DIR / name)
        done.append(name)
    return done


def backup(dest: Path | None = None) -> tuple[Path, int]:
    """Copy every authored file to a timestamped directory outside both trees."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest or (config.HOME / f"otto-config-backup-{stamp}")
    dest.mkdir(parents=True, exist_ok=True)

    for name, target in JUNCTIONS.items():
        if target.is_dir():
            shutil.copytree(target, dest / name, dirs_exist_ok=True)
    for name in DEPLOY_FILES:
        for src in (CFG_DIR / name, config.CLAUDE_DIR / name):
            if src.is_file():
                shutil.copy2(src, dest / name)
                break
    for name in SNAPSHOT_FILES:
        src = config.CLAUDE_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)

    count = sum(1 for _ in dest.rglob("*") if _.is_file())
    return dest, count


def summary() -> dict:
    j = junction_status()
    d = deploy_status()
    s = snapshot_status()
    return {
        "repo": str(REPO),
        "junctions": j,
        "deploy": d,
        "snapshots": s,
        "ok": all(r["ok"] for r in j) and all(r["state"] != "DRIFT" for r in d),
        "managed_files": sum(r.get("files", 0) for r in j) + len(DEPLOY_FILES),
    }
