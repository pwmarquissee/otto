"""Host facts for the owner's own workstation.

Deliberately informational. The owner's standing guidance is that drive/disk alerts
and multi-day-offline signals are never action items and never warrant outreach,
so nothing in this module produces an Alert. It answers "what is the state of this
machine" as context, and stops there.

Backup freshness is the one thing here that can be genuinely useful, and it only
reports when OTTO's config names a path to watch. An unconfigured backup path
reports as unconfigured rather than as healthy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psutil

from .. import config
from ..models import MachineStat


def _gb(n: float) -> str:
    """Human size that does not collapse small volumes to '0G'."""
    tb = 1024 ** 4
    gb = 1024 ** 3
    mb = 1024 ** 2
    if n >= tb:
        return f"{n / tb:.1f}T"
    if n >= 10 * gb:
        return f"{n / gb:.0f}G"
    if n >= gb:
        return f"{n / gb:.1f}G"
    if n >= mb:
        return f"{n / mb:.0f}M"
    return f"{n}B"


def _fmt_age(seconds: float) -> str:
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def disks() -> list[MachineStat]:
    out: list[MachineStat] = []
    for part in psutil.disk_partitions(all=False):
        # Skip empty optical/removable drives, which raise on Windows.
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append(
            MachineStat(
                label=part.mountpoint.rstrip("\\/") or part.device,
                value=f"{usage.percent:.0f}% used",
                detail=f"{_gb(usage.free)} free of {_gb(usage.total)}",
            )
        )
    return out


def uptime() -> MachineStat:
    booted = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    age = (datetime.now(timezone.utc) - booted).total_seconds()
    return MachineStat(
        label="uptime",
        value=_fmt_age(age),
        detail=f"booted {booted.strftime('%Y-%m-%d %H:%M')}Z",
    )


def load() -> list[MachineStat]:
    mem = psutil.virtual_memory()
    return [
        MachineStat(label="memory", value=f"{mem.percent:.0f}% used",
                    detail=f"{_gb(mem.available)} available of {_gb(mem.total)}"),
        # interval=None reads the delta since the last call instead of sleeping
        # 300ms inside every /api/state. With two dashboards polling every 2-3s
        # that sleep was a third of the request time, and the tray's 3s health
        # timeout tripped on a daemon that was merely busy, not down.
        MachineStat(label="cpu", value=f"{psutil.cpu_percent(interval=None):.0f}%",
                    detail=f"{psutil.cpu_count(logical=True)} logical cores"),
    ]


def backups() -> list[MachineStat]:
    if not config.BACKUP_PATHS:
        return [MachineStat(label="backups", value="unconfigured",
                            detail="set BACKUP_PATHS in otto/config.py to track freshness")]
    out: list[MachineStat] = []
    now = datetime.now(timezone.utc).timestamp()
    for path in config.BACKUP_PATHS:
        p = Path(path)
        if not p.exists():
            out.append(MachineStat(label=p.name or str(p), value="missing", detail=str(p)))
            continue
        newest = 0.0
        try:
            if p.is_file():
                newest = p.stat().st_mtime
            else:
                for f in p.rglob("*"):
                    if f.is_file():
                        newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
        if not newest:
            out.append(MachineStat(label=p.name or str(p), value="empty", detail=str(p)))
        else:
            out.append(MachineStat(label=p.name or str(p),
                                   value=f"{_fmt_age(now - newest)} old", detail=str(p)))
    return out


def snapshot() -> list[MachineStat]:
    """Everything, in display order. Never raises: a broken probe is omitted."""
    stats: list[MachineStat] = []
    for fn in (uptime,):
        try:
            stats.append(fn())
        except Exception:  # noqa: BLE001
            continue
    for fn in (load, disks, backups):
        try:
            stats.extend(fn())
        except Exception:  # noqa: BLE001
            continue
    return stats
