"""Looking at the whole: status, what to do next, the day, notices, priorities."""

from __future__ import annotations

import json
import sys
from datetime import datetime

from .. import config, persona
from ..runners import scheduled
from ..store import Store
from ..client import Client, DaemonDown
from ._fmt import C_BOLD, C_CYA, C_DIM, C_RED, C_YEL, _LEVEL_COLOR, _STATUS_COLOR, _age, _banner, _c, _cpad, _integration_mark, _print_snapshots


# ---- commands ---------------------------------------------------------------

def cmd_status(args, client: Client) -> int:
    try:
        st = client.state()
    except DaemonDown as e:
        print(_c(f"  [{config.PERSONA_NAME}] daemon DOWN", C_RED))
        print(f"  {e}")
        _offline_status()
        return 1

    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    only = args.domain
    _banner(st["headline"])
    print()

    alerts = [a for a in st["alerts"] if not only or a.get("domain") == only]
    if alerts:
        print(_c("  ALERTS", C_BOLD))
        for a in alerts[: args.limit]:
            color = _LEVEL_COLOR.get(a["level"], "")
            level = _cpad(a["level"].upper(), 5, color)
            dom = _cpad(a.get("domain", "work")[:4], 5, C_DIM)
            print(f"    {level}{dom}{a['source']:<28} {a['message']}")
        print()

    # Runs and schedules are grouped by domain so work and life never blur.
    domains = [only] if only else list(st.get("domains") or ["work", "personal"])

    for domain in domains:
        runs = [r for r in st["runs"] if r.get("domain", "work") == domain]
        scheds = [s for s in st["schedules"] if s.get("domain", "work") == domain]
        if not runs and not scheds and domain != "work":
            print(_c(f"  {domain.upper()}", C_BOLD)
                  + _c("   nothing yet - otto schedule add <name> --command ...", C_DIM))
            print()
            continue

        active = len([r for r in runs if r["status"] == "running"])
        head = _c(f"  {domain.upper()}", C_BOLD)
        print(head + _c(f"   {active} active" if active else "", C_DIM))

        for s in scheds:
            if s.get("stale"):
                mark, color, detail = "STALE", C_RED, s["stale"]
            elif s["due"]:
                mark, color, detail = "DUE", C_YEL, s["due_reason"]
            elif not s["enabled"]:
                mark, color, detail = "off", C_DIM, "disabled"
            else:
                mark, color, detail = "idle", C_DIM, s["due_reason"]
            print(f"    {_cpad(mark, 6, color)} {s['name']:<18} "
                  f"{_age(s['last_run']):>11}  {_c(detail, C_DIM)}")

        for r in runs[: args.limit]:
            status = _cpad(r["status"], 9, _STATUS_COLOR.get(r["status"], ""))
            usage = ""
            if r.get("output_tokens"):
                usage = _c(f"  {persona.thousands(r['output_tokens'])} out", C_DIM)
            print(f"    {persona.short(r['id']):<8}{r['name'][:18]:<20}{status}"
                  f"{_age(r['started']):>11}{usage}")
        print()

    _print_sessions_line(st.get("sessions") or {})
    _print_snapshots(st.get("snapshots") or {})

    if not only or only == "work":
        print(_c("  INTEGRATIONS", C_BOLD))
        if not st["integrations"]:
            print(_c("    (not probed yet - otto probe)", C_DIM))
        for i in st["integrations"]:
            mark, color = _integration_mark(i)
            print(f"    {_cpad(mark, 6, color)} {i['name']:<14} {_c(i['detail'], C_DIM)}")
        print()

    by_domain = st.get("registry_by_domain") or {}
    if by_domain:
        print(_c("  REGISTRY", C_BOLD))
        for domain in domains:
            summ = by_domain.get(domain) or {}
            parts = [f"{v} {k}s" for k, v in sorted(summ.items()) if k != "missing"]
            tail = f", {summ['missing']} missing" if summ.get("missing") else ""
            print(f"    {domain:<10} {', '.join(parts) or 'none'}{tail}")
    print(_c(f"\n  dashboard: {config.BASE_URL}", C_DIM))
    return 0


def _print_sessions_line(data: dict) -> None:
    """One line, and only when there is something to say.

    Deliberately not a list. `otto status` is the one-screen view, waiting sessions
    already come through as ALERTS above, and a per-session table here would push
    the day off the bottom of the screen to report that three terminals are idle.
    `otto sessions` is the list.
    """
    summary = data.get("summary") or {}
    if not summary.get("hooks_installed"):
        # Silent when nothing has ever reported: a machine that has not opted in
        # should not be nagged on every status call. `otto gaps` carries it.
        if not summary.get("total"):
            return
        print(_c("  SESSIONS", C_BOLD)
              + _c("   hooks not installed, this view is incomplete "
                   "- otto sessions install", C_YEL))
        print()
        return

    if not summary.get("live"):
        return

    bits = [f"{summary[k]} {k}" for k in ("busy", "waiting", "idle") if summary.get(k)]
    tail = (_c(f"   {summary['unspawned']} not spawned by Otto", C_DIM)
            if summary.get("unspawned") else "")
    print(_c("  SESSIONS", C_BOLD) + f"   {', '.join(bits)}" + tail)
    print()


def _offline_status() -> None:
    """Best-effort read straight off disk when the daemon is unreachable."""
    store = Store()
    scheds = store.schedules()
    if not scheds:
        print(_c("\n  no state on disk yet - run `otto serve` once to initialize", C_DIM))
        return
    print(_c("\n  last known schedule state (read from disk):", C_DIM))
    for s in scheds:
        st = scheduled.staleness(s)
        flag = _cpad("STALE", 6, C_RED) if st else _cpad("ok", 6, C_DIM)
        print(f"    {flag} {s.name:<18} {_age(s.last_run):>11}")


def cmd_priorities(args, client: Client) -> int:
    """What matters right now. Local read: no daemon needed to answer it."""
    from .. import priorities
    if args.init:
        path = config.PRIORITIES_PATH
        if path.exists() and not args.force:
            print(f"  {path} already exists; --force to overwrite", file=sys.stderr)
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            # Local date, not UTC: this is a date a human writes and reads, and a
            # UTC stamp reads as tomorrow for anyone west of Greenwich.
            priorities.TEMPLATE.format(today=datetime.now().date().isoformat()),
            encoding="utf-8")
        print(f"  wrote {path}")
        print("  edit it, then `otto priorities` to check")
        return 0
    if args.json:
        print(json.dumps(priorities.status(), indent=2))
        return 0
    print(priorities.render())
    for g in priorities.gaps():
        print()
        print(f"  {_c(g['title'], C_YEL)}")
        print(f"  {g['why']}")
    return 0


def cmd_agenda(args, client: Client) -> int:
    """Show, or ingest, the calendar/mail snapshots.

    Otto has no MCP access from the daemon, so a Claude session pushes these in.
    `--push` reads a JSON body on stdin.
    """
    if args.push:
        raw = sys.stdin.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  stdin is not valid JSON: {e}", file=sys.stderr)
            return 2
        snap = client.put_snapshot(
            args.push,
            summary=body.get("summary"),
            items=body.get("items") or [],
            source=body.get("source", "claude-session"),
            domain=args.domain or body.get("domain") or config.WORK,
        )
        print(f"  {snap['domain']}/{snap['kind']} updated: "
              f"{len(snap['items'])} item(s) @ {snap['fetched_at']}")
        return 0

    snaps = client.snapshots()
    if args.json:
        print(json.dumps(snaps, indent=2))
        return 0
    if not snaps:
        print("  nothing pushed yet. Run /otto in a Claude session to refresh"
              " calendar and mail.")
        return 0
    _print_snapshots(snaps, domain=args.domain)
    return 0


def cmd_machine(args, client: Client) -> int:
    stats = client.machine()
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(_c("  THIS MACHINE", C_BOLD) + _c("  (informational, not action items)", C_DIM))
    for m in stats:
        print(f"    {m['label']:<14} {m['value']:<16} {_c(m.get('detail') or '', C_DIM)}")
    return 0


def cmd_events(args, client: Client) -> int:
    for e in client.events(args.limit):
        level = _cpad(e["level"], 5, _LEVEL_COLOR.get(e["level"], ""))
        print(f"  {e['at']}  {level} {e['source']:<12} {e['message']}")
    return 0


_BAND_COLOR = {"today": C_RED, "this week": C_YEL, "sometime": C_DIM}


def cmd_next(args, client: Client) -> int:
    """What to do now, ranked. Deterministic, so it costs nothing and never varies."""
    b = client.briefing(args.domain)
    rows = b["next"]
    if args.json:
        print(json.dumps(b, indent=2))
        return 0
    if not rows:
        print(f"  nothing to do. {config.PERSONA_NAME} has no suggestions.")
        return 0

    band = None
    for r in rows:
        if r["band"] != band:
            band = r["band"]
            print()
            print(_c(f"  {band.upper()}", _BAND_COLOR.get(band, C_DIM)))
        dom = _cpad(r["domain"][:4], 5, C_DIM)
        print(f"    {dom}{_c(r['title'], C_BOLD)}")
        print(f"         {_c(r['why'], C_DIM)}")
        if r.get("command"):
            print(f"         {_c('$ ' + r['command'], C_CYA)}")
    print()
    return 0


def cmd_gaps(args, client: Client) -> int:
    """Blind spots: things with no watcher, as opposed to things that broke."""
    rows = client.gaps(args.domain)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  no gaps found. Everything is watched.")
        return 0
    print(_c(f"  {len(rows)} GAP(S)", C_BOLD)
          + _c("   things nothing is currently watching", C_DIM))
    for g in rows:
        print()
        print(f"    {_cpad(g['domain'][:4], 5, C_DIM)}{_c(g['title'], C_BOLD)}"
              + _c(f"  [{g['kind']}]", C_DIM))
        print(f"         {_c(g['why'], C_DIM)}")
        if g.get("command"):
            print(f"         {_c('$ ' + g['command'], C_CYA)}")
    print()
    return 0


def cmd_notices(args, client: Client) -> int:
    """Messages Otto sent the owner. Durable, unlike the toast that announced them."""
    try:
        rows = client.notices(unread_only=args.unread)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  nothing from Otto." if args.unread else "  no notices.")
        return 0
    for n in rows[: args.limit]:
        col = {"crit": C_RED, "warn": C_YEL}.get(n["level"], C_DIM)
        mark = " " if n.get("read_at") else _c("*", col)
        seen = (_c(f" seen {n['seen_count']}x", C_YEL)
                if (n.get("seen_count") or 1) > 1 else "")
        print(f"  {mark} {_c(n['id'][:6], C_DIM)} {_c(n['level'][:4].upper(), col)} "
              f"{_c(_age(n['at']), C_DIM)}  {n['title']}{seen}")
        if n.get("body"):
            for line in str(n["body"]).splitlines():
                print(f"        {_c(line, C_DIM)}")
        if n.get("command"):
            print(f"        {_c(n['command'], C_DIM)}")
    unread = len([n for n in rows if not n.get("read_at")])
    if unread and not args.read:
        print()
        print(_c(f"  {unread} unread. otto notices --read to clear.", C_DIM))
    if args.read:
        for n in rows:
            if not n.get("read_at"):
                client.read_notice(n["id"])
        print(_c("  marked read", C_DIM))
    return 0


def cmd_day(args, client: Client) -> int:
    """Where the day actually went, plus whatever the owner reported about it.

    The rollup is mechanical: read from Claude Code's own transcripts, no model, no
    cost. Two numbers are reported and they are not interchangeable. `wall` is real
    elapsed engaged time (union of activity windows, idle stripped). `attention` sums
    across sessions and can exceed wall clock because concurrent sessions are real
    work, not double counting. Reporting only the larger would be flattering nonsense.
    """
    day = args.date or datetime.now().astimezone().date().isoformat()
    try:
        rec = client.day(day, refresh=args.refresh)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1

    roll = rec.get("rollup") or {}
    ci = rec.get("checkin") or {}
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0

    print(_c(f"  {day}", C_BOLD))
    if not roll.get("session_count"):
        print("    no sessions recorded")
    else:
        wall = roll.get("wall_minutes") or 0
        att = roll.get("attention_minutes") or 0
        print(f"    {roll['session_count']} sessions   "
              f"{wall // 60}h{wall % 60:02d}m engaged   "
              f"{_c(f'{att // 60}h{att % 60:02d}m attention', C_DIM)}   "
              f"{_c(str(roll.get('first_activity')) + ' - ' + str(roll.get('last_activity')), C_DIM)}")
        print()
        for proj, mins in (roll.get("by_project") or {}).items():
            if mins < 5:
                continue
            bar = "#" * min(int(mins / 10), 40)
            print(f"    {mins:>4}m  {proj:<34} {_c(bar, C_DIM)}")
        longs = roll.get("long_sessions") or []
        if longs:
            print()
            print(_c("    longest, with what you said you were doing:", C_DIM))
            for s in longs[:5]:
                # split()/join collapses newlines and runs of spaces in one go, so a
                # multi-line prompt renders as one readable line.
                intent = " ".join((s.get("intent") or "").split())[:78]
                print(f"    {s['minutes']:>4}m  {s.get('title') or '(untitled)'}")
                print(f"          {_c(intent, C_DIM)}")

    print()
    if ci:
        e = f"energy {ci['energy']}/5   " if ci.get("energy") else ""
        print(f"  {_c('check-in', C_BOLD)}  {_c(e, C_DIM)}{_c(str(ci.get('at'))[:16], C_DIM)}")
        for line in (ci.get("note") or "").splitlines():
            print(f"    {line}")
    else:
        print(_c("  no check-in yet:  otto checkin \"...\" [--energy 1-5]", C_DIM))
    return 0


def cmd_say(args, client: Client) -> int:
    """Render a summary in Otto's voice. Used to check formatting before posting."""
    st = client.state()
    lines = []
    for s in st["schedules"]:
        if s.get("stale"):
            lines.append(f"{s['name']}: STALE - {s['stale']}")
        elif s["due"]:
            lines.append(f"{s['name']}: due - {s['due_reason']}")
    for i in st["integrations"]:
        if not i["ok"] and i["mode"] == "api":
            lines.append(f"{i['name']}: down - {i['detail']}")
    if not lines:
        lines = ["all schedules fresh, all integrations up"]
    print(persona.slack_summary(f"status - {st['at']}", lines))
    return 0


def add_status(sub) -> None:
    s = sub.add_parser("status", help="one-screen view of everything")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=12)
    s.add_argument("--domain", choices=["work", "personal"], help="show one domain only")
    s.set_defaults(fn=cmd_status)


def add_priorities(sub) -> None:
    s = sub.add_parser("priorities", help="what matters right now, and how stale it is")
    s.add_argument("--init", action="store_true", help="write a starter file")
    s.add_argument("--force", action="store_true", help="overwrite an existing one")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_priorities)


def add_agenda(sub) -> None:
    s = sub.add_parser("agenda", help="calendar/mail snapshots pushed in by a session")
    s.add_argument("--push", metavar="KIND",
                   help="read JSON on stdin and store it as this snapshot kind")
    s.add_argument("--domain", choices=config.DOMAINS,
                   help="which inbox/calendar this snapshot is for (default work)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_agenda)


def add_notices(sub) -> None:
    s = sub.add_parser("notices", help="messages Otto sent you")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--read", action="store_true", help="mark everything shown as read")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_notices)


def add_day(sub) -> None:
    s = sub.add_parser("day", help="where the day went, from your own transcripts")
    s.add_argument("--date", help="ISO date, default today")
    s.add_argument("--refresh", action="store_true", help="recompute the rollup")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_day)


def add_machine(sub) -> None:
    s = sub.add_parser("machine", help="this workstation, informational only")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_machine)


def add_events(sub) -> None:
    s = sub.add_parser("events", help="recent event log")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_events)


def add_next(sub) -> None:
    s = sub.add_parser("next", help="what to do now, ranked")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_next)


def add_gaps(sub) -> None:
    s = sub.add_parser("gaps", help="blind spots nothing is watching")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_gaps)


def add_say(sub) -> None:
    s = sub.add_parser("say", help="render current status in Otto's voice")
    s.set_defaults(fn=cmd_say)


PARSERS = {
    "status": add_status,
    "priorities": add_priorities,
    "agenda": add_agenda,
    "notices": add_notices,
    "day": add_day,
    "machine": add_machine,
    "events": add_events,
    "next": add_next,
    "gaps": add_gaps,
    "say": add_say,
}
