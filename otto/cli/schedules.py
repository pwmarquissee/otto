"""Schedules and the unattended switches: cadence, arming, launching, stamping."""

from __future__ import annotations

import json
import sys

from .. import persona
from ..client import Client
from ._fmt import C_BOLD, C_DIM, C_GRN, C_RED, C_YEL, _age, _c, _cpad
from .runs import cmd_watch


def cmd_schedules(args, client: Client) -> int:
    scheds = client.schedules()
    if args.json:
        print(json.dumps(scheds, indent=2))
        return 0
    for s in scheds:
        mark = _cpad("DUE", 5, C_YEL) if s["due"] else _cpad("idle", 5, C_DIM)
        state = "" if s["enabled"] else _c(" (disabled)", C_DIM)
        print(f"  {mark} {s['name']:<18} {s['command'][:30]:<31}"
              f"{_age(s['last_run']):>11}  {_c(s['due_reason'], C_DIM)}{state}")
    return 0


def cmd_due(args, client: Client) -> int:
    """Machine-readable list of what should run now. For /daily and loops."""
    due = [s for s in client.schedules() if s["due"]]
    if args.domain:
        due = [s for s in due if s.get("domain", "work") == args.domain]
    if args.json:
        print(json.dumps(due, indent=2))
        return 0
    for s in due:
        print(s["command"])
    return 0 if due else 1


_MANGLED = ("/Git/", "\\Git\\", "/usr/bin/", "Program Files/Git")


def cmd_schedule_add(args, client: Client) -> int:
    # Git Bash rewrites a leading-slash argument into a Windows path, so
    # `--command "/review"` silently arrives as "C:/Program Files/Git/review".
    # The mangling happens before Python sees it, so all Otto can do is notice
    # the signature and refuse to store the wrong thing quietly.
    if any(m in args.command for m in _MANGLED):
        print(_c(f"  that command looks path-mangled by the shell: {args.command}", C_YEL),
              file=sys.stderr)
        print("  if you meant a slash command, use PowerShell, or prefix with "
              "MSYS_NO_PATHCONV=1", file=sys.stderr)
        if not args.force:
            print("  refusing to store it. Pass --force to override.", file=sys.stderr)
            return 2

    days = [d.strip() for d in (args.days or "").split(",") if d.strip()]
    if args.kind == "weekly" and not days:
        print("  weekly needs --days (e.g. --days sun or --days mon,thu)", file=sys.stderr)
        return 2
    if args.kind == "every" and not args.hours:
        print("  every needs --hours N", file=sys.stderr)
        return 2
    sched = client.put_schedule(
        args.name,
        command=args.command,
        domain=args.domain,
        description=args.description,
        kind=args.kind,
        at=args.at,
        days=days,
        hours=args.hours,
        min_interval_days=args.min_interval_days,
        max_age_hours=args.max_age_hours,
        enabled=not args.disabled,
    )
    cad = sched["cadence"]
    when = {
        "daily": f"daily at {cad['at']}",
        "weekly": f"{','.join(cad['days'])} at {cad['at']}",
        "every": f"every {cad['hours']}h",
        "manual": "manual only",
    }.get(cad["kind"], cad["kind"])
    print(f"  {sched['name']} ({sched['domain']}) -> {when}")
    if sched.get("max_age_hours"):
        print(f"  alarms if it has not run in {sched['max_age_hours']}h")
    print(f"  command: {sched['command']}")
    return 0


def cmd_schedule_rm(args, client: Client) -> int:
    client.delete_schedule(args.name)
    print(f"  removed {args.name}")
    return 0


def cmd_stamp(args, client: Client) -> int:
    sched = client.stamp(args.name, args.status, args.run_id)
    print(f"  stamped {sched['name']} @ {sched['last_run']} ({sched['last_status']})")
    return 0


def cmd_toggle(args, client: Client) -> int:
    sched = client.toggle(args.name, not args.off)
    print(f"  {sched['name']} enabled={sched['enabled']}")
    return 0


def cmd_autorun(args, client: Client) -> int:
    """Unattended cron: the master switch, what is armed, what tripped."""
    if args.state is not None:
        st = client.set_autorun(args.state == "on")
    else:
        st = client.autorun()
    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    flag = _c("ON", C_GRN) if st["enabled"] else _c("OFF", C_YEL)
    print(f"  autorun  {flag}")
    print(_c(f"  concurrency {st['max_concurrent']}  |  breaker at {st['max_failures']} "
             f"failures  |  {st['min_gap_minutes']}m floor  |  "
             f"${st['budget_usd']:.0f} per-run cap", C_DIM))
    print()
    if not st["armed"]:
        print(_c("  nothing armed. Arm one with: otto schedule arm <name>", C_DIM))
    else:
        print(_c("  ARMED", C_BOLD))
        for a in st["armed"]:
            cad = a["cadence"]
            when = {"daily": f"daily {cad['at']}",
                    "weekly": f"{','.join(cad['days'])} {cad['at']}",
                    "every": f"every {cad['hours']}h"}.get(cad["kind"], cad["kind"])
            print(f"    {a['name']:<16} {a['runner']:<8} {when:<16} "
                  f"{_c(a['command'][:34], C_DIM)}")
            if a["consecutive_failures"]:
                print(_c(f"      {a['consecutive_failures']} consecutive failure(s)", C_YEL))
    if st.get("blocked"):
        print()
        print(_c("  DUE BUT BLOCKED", C_YEL))
        for b in st["blocked"]:
            print(f"    {b['name']:<16} {b['reason']}")
    if st["tripped"]:
        print()
        print(_c("  CIRCUIT BREAKER TRIPPED", C_RED))
        for t in st["tripped"]:
            print(f"    {t['name']:<16} {t['reason']}")
        print(_c("    re-arm with: otto schedule arm <name>", C_DIM))
    if not st["enabled"]:
        print()
        print(_c("  master switch is off, so nothing will run unattended", C_DIM))
    return 0


def cmd_schedule_arm(args, client: Client) -> int:
    """Arm a schedule to run unattended on its cadence."""
    armed = not args.off
    if armed:
        scheds = {x["name"]: x for x in client.schedules()}
        sched = scheds.get(args.name)
        if sched is None:
            print(f"  no schedule named {args.name}", file=sys.stderr)
            return 1
        slash = (sched["command"] or "").strip().startswith("/")
        what = ("a Claude Code session with --dangerously-skip-permissions"
                if slash else "a shell command")
        print(f"  arming {args.name}: {sched['command']}")
        print(_c(f"  this will run unattended, on cadence, as {what}", C_YEL))
    s = client.arm_schedule(args.name, armed)
    print(f"  {s['name']}: autostart={s['autostart']} runner={s['runner']}")
    return 0


def cmd_launch(args, client: Client) -> int:
    """Run a schedule now. Slash commands get a session; shell commands run direct."""
    try:
        r = client.run_schedule(args.name)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    run = r["run"]
    print(f"  {r['message']}")
    print(f"  watch: otto watch {persona.short(run['id'])}")
    if args.watch:
        args.run_id = run["id"]
        args.follow = True
        return cmd_watch(args, client)
    return 0


def cmd_autodispatch(args, client: Client) -> int:
    if args.state is None:
        st = client.dispatch_status()
    else:
        st = client.set_dispatch(args.state == "on")
    flag = _c("ON", C_GRN) if st["enabled"] else _c("OFF", C_YEL)
    print(f"  auto-dispatch  {flag}")
    print(f"  running {st['running']}/{st['max_concurrent']}"
          f"  eligible {st['eligible']}"
          f"  max attempts {st['max_attempts']}")
    if st["enabled"]:
        print(_c("  a task moved to `queued` is spawned with skip-permissions", C_DIM))
    else:
        print(_c("  `queued` is inert; nothing will be spawned", C_DIM))
    return 0


def add_schedules(sub) -> None:
    s = sub.add_parser("schedules", help="list schedules and cadence")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_schedules)


def add_due(sub) -> None:
    s = sub.add_parser("due", help="print commands that are due now")
    s.add_argument("--json", action="store_true")
    s.add_argument("--domain", choices=["work", "personal"])
    s.set_defaults(fn=cmd_due)


def add_schedule(sub) -> None:
    # ---- schedule management: how personal routines get added ----
    sch = sub.add_parser("schedule", help="add or remove a schedule")
    sch_sub = sch.add_subparsers(dest="schedule_cmd", required=True)

    a = sch_sub.add_parser("add", help="create or replace a schedule")
    a.add_argument("name")
    a.add_argument("--command", required=True,
                   help="slash command or shell command to run")
    a.add_argument("--domain", choices=["work", "personal"], default="personal")
    a.add_argument("--kind", choices=["daily", "weekly", "every", "manual"], default="daily")
    a.add_argument("--at", default="08:00", help="HH:MM local, for daily/weekly")
    a.add_argument("--days", help="weekly only, comma separated (e.g. sun or mon,thu)")
    a.add_argument("--hours", type=int, help="for --kind every")
    a.add_argument("--min-interval-days", type=int,
                   help="never run again within N days, whatever the cadence says")
    a.add_argument("--max-age-hours", type=int,
                   help="raise an alarm if it has not run in this long")
    a.add_argument("--description")
    a.add_argument("--disabled", action="store_true")
    a.add_argument("--force", action="store_true",
                   help="store the command even if it looks shell-mangled")
    a.set_defaults(fn=cmd_schedule_add)

    a = sch_sub.add_parser("arm", help="run this schedule unattended, on cadence")
    a.add_argument("name")
    a.add_argument("--off", action="store_true", help="disarm it")
    a.set_defaults(fn=cmd_schedule_arm)

    r = sch_sub.add_parser("rm", help="delete a schedule")
    r.add_argument("name")
    r.set_defaults(fn=cmd_schedule_rm)


def add_stamp(sub) -> None:
    s = sub.add_parser("stamp", help="record a successful schedule run")
    s.add_argument("name")
    s.add_argument("--status", default="ok")
    s.add_argument("--run-id")
    s.set_defaults(fn=cmd_stamp)


def add_toggle(sub) -> None:
    s = sub.add_parser("toggle", help="enable/disable a schedule")
    s.add_argument("name")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_toggle)


def add_launch(sub) -> None:
    s = sub.add_parser("launch", help="run a schedule now")
    s.add_argument("name")
    s.add_argument("--watch", action="store_true", help="follow the timeline")
    s.add_argument("--interval", type=int, default=3)
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_launch)


def add_autorun(sub) -> None:
    s = sub.add_parser("autorun", help="unattended cron: switch, armed, tripped")
    s.add_argument("state", nargs="?", choices=["on", "off"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_autorun)


def add_autodispatch(sub) -> None:
    s = sub.add_parser("autodispatch", help="whether queued tasks are auto-run")
    s.add_argument("state", nargs="?", choices=["on", "off"])
    s.set_defaults(fn=cmd_autodispatch)


PARSERS = {
    "schedules": add_schedules,
    "due": add_due,
    "schedule": add_schedule,
    "stamp": add_stamp,
    "toggle": add_toggle,
    "launch": add_launch,
    "autorun": add_autorun,
    "autodispatch": add_autodispatch,
}
