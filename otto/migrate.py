"""Absorb the stamp ledgers of a predecessor scheduler, if there is one.

Reads (never writes) the files under OTTO_ORCHESTRATOR_DIR (default
~/.claude/orchestrator), the layout of the script-based scheduler Otto replaced,
so an install that had one starts with real history instead of a blank slate. On
a machine without that directory `otto migrate` reports nothing to do. Stamps are backdated to their original
timestamps, which is the point: importing a 25-day-old `daily` stamp should
immediately surface as STALE, not look like a fresh success.

The original ledgers are left untouched, so the predecessor keeps working while you
decide whether to retire it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .client import Client

HEARTBEAT_LEDGER = config.ORCHESTRATOR_DIR / "heartbeat" / "ledger.json"
SCOUT_LEDGER = config.ORCHESTRATOR_DIR / "scout" / "scout_ledger.json"


def _load(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"    ! could not read {path.name}: {e}")
        return None


def migrate(client: Client, dry_run: bool = False) -> int:
    print(f"  migrating existing ledgers into {config.PERSONA_NAME}"
          f"{' (dry run)' if dry_run else ''}\n")

    known = {s["name"] for s in client.schedules()}
    imported = 0
    skipped = 0

    # ---- heartbeat loop freshness -------------------------------------------
    hb = _load(HEARTBEAT_LEDGER)
    if hb is None:
        print(f"    heartbeat ledger not found at {HEARTBEAT_LEDGER}")
    else:
        print("    heartbeat ledger:")
        for name, rec in sorted(hb.items()):
            last = (rec or {}).get("last_success")
            if not last:
                continue
            if name not in known:
                print(f"      - {name:<16} {last}  (no matching Otto schedule, skipped)")
                skipped += 1
                continue
            if dry_run:
                print(f"      + {name:<16} {last}  would import")
            else:
                client.stamp(name, status="ok", at=last)
                print(f"      + {name:<16} {last}  imported")
            imported += 1

    # ---- scout proposal history --------------------------------------------
    sc = _load(SCOUT_LEDGER)
    if sc is None:
        print(f"\n    scout ledger not found at {SCOUT_LEDGER}")
    else:
        proposals = sc.get("proposals") or []
        latest = max(
            (p.get("first_proposed") for p in proposals if p.get("first_proposed")),
            default=None,
        )
        by_status: dict[str, int] = {}
        for p in proposals:
            by_status[p.get("status", "unknown")] = by_status.get(p.get("status", "unknown"), 0) + 1
        print(f"\n    scout ledger: {len(proposals)} proposals "
              f"({', '.join(f'{v} {k}' for k, v in sorted(by_status.items()))})")
        if latest and "scout" in known:
            if dry_run:
                print(f"      + scout            {latest}  would import as last run")
            else:
                client.stamp("scout", status="ok", at=latest)
                print(f"      + scout            {latest}  imported as last run")
            imported += 1

    print(f"\n  {imported} stamp(s) {'to import' if dry_run else 'imported'}"
          f"{f', {skipped} skipped' if skipped else ''}")
    print(f"  original ledgers left untouched; heartbeat.py still works")
    if not dry_run:
        print(f"\n  run `otto status` - anything overdue will now show as STALE")
    return 0
