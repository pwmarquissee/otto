"""Assistant-scope verbs (OTTO_SCOPE=assistant): messaging colleagues, dossiers, threads, meeting prep and notes; this module also lists the whole gated set."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

from .. import config, persona
from ..store import Store
from ..client import Client
from ._fmt import C_BOLD, C_CYA, C_DIM, C_GRN, C_RED, C_YEL, _age, _c, _cpad


def cmd_dm(args, client: Client) -> int:
    """Group DM a colleague and the owner, as Otto. For messages the owner asked for."""
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to send an empty message", file=sys.stderr)
        return 2
    try:
        res = client.slack_dm(args.who, text, why=args.why or "",
                              run_id=os.environ.get("OTTO_RUN_ID"),
                              task_id=args.task)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (roster, per-run brake, unattended,
        # no-send). Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  sent as Otto to {res['to']} (group DM with {config.OWNER_NAME})  id {res['id'][:8]}")
    return 0


def cmd_people_note(args, client: Client) -> int:
    """Append a note to a dossier section.

    Exists because `otto/triage.py` tells a spawned session to call it, and a verb
    a prompt depends on must be a real command rather than a shape the model has to
    guess at. Useful by hand for the same reason.
    """
    text = " ".join(args.text).strip()
    if not text:
        print(_c("  nothing to write", C_RED), file=sys.stderr)
        return 1
    try:
        client.add_person_note(args.slug, args.section, text)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    print(f"  {args.slug}: note added to {args.section}")
    return 0


def cmd_outreach_resolve(args, client: Client) -> int:
    """Record a colleague's Slack member id so outreach can address them.

    Local, not through the daemon: it spawns a lookup session and writes a dossier
    field, and it must work before the daemon is trusted to send anything.
    """
    from .. import outreach as _o
    from .. import people as _people

    target = args.target
    existing = _o.slack_id_for(target)
    if existing and not args.force:
        print(f"  {target} already resolved: {existing}")
        return 0

    print(f"  looking up {target} in Slack...")
    sid, err = _o.resolve_slack_id(target)
    if not sid:
        print(_c(f"  {err}", C_RED), file=sys.stderr)
        return 1
    p = _people.get(target)
    if not p:
        print(_c(f"  no dossier for {target}, cannot record {sid}", C_RED), file=sys.stderr)
        return 1
    _people.set_meta(p["slug"], {"slack_id": sid})
    print(f"  {target} -> {sid}  (recorded on {p['slug']}.md)")
    return 0


def cmd_outreach_compose(args, client: Client) -> int:
    """Compose a HELD message. Never sends: the hold is the whole point."""
    try:
        o = client.compose_outreach(to=args.to, body=args.body, why=args.why,
                                    source=args.source, channel=args.channel)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    mins = o.get("hold_minutes")
    print(f"  held for {o.get('to')}: {persona.short(o.get('id',''))}")
    print(_c(f"  sends in {mins}m unless killed - otto outreach --kill "
             f"{persona.short(o.get('id',''))}", C_DIM))
    return 0


def cmd_threads(args, client: Client) -> int:
    """Every dated dossier thread, the whole list.

    The threads-quiet notice lists eight and counts the rest, which is right for a
    notice and useless for deciding. This is the list you act from.
    """
    data = client.threads(args.band)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    rows, counts = data["rows"], data["counts"]
    b = data["bands"]
    print()
    print(_c("  THREADS", C_BOLD)
          + f"  {counts.get('quiet',0)} quiet ({b['stale_days']}-{b['max_days']}d)"
          + f", {counts.get('ancient',0)} over {b['max_days']}d"
          + f", {counts.get('fresh',0)} fresh")
    print()
    last = None
    for r in rows:
        if r["band"] != last:
            last = r["band"]
            print(_c(f"  {last.upper()}", C_BOLD))
        color = {"ancient": C_RED, "quiet": C_YEL}.get(r["band"], C_DIM)
        print(f"    {_cpad(str(r['days']) + 'd', 6, color)}"
              f"{_cpad(r['who'][:22], 24)}{_c(r['when'], C_DIM)}")
        print(f"          {r['text'][:96]}")
        for n in r.get("notes") or []:
            print(_c(f"          note {n['at'][:10]}: {n['note'][:80]}", C_CYA))
    print()
    return 0


def cmd_thread_note(args, client: Client) -> int:
    """Write a note against one thread and let Otto decide what to do with it."""
    note = " ".join(args.note).strip()
    if not note:
        print(_c("  nothing to send", C_RED), file=sys.stderr)
        return 1
    try:
        res = client.thread_note(args.thread_id, note)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    run = res["run"]
    print(f"  sent to Otto  run {persona.short(run['id'])}  pid {run.get('pid')}")
    print(_c(f"  otto watch {persona.short(run['id'])} -f", C_DIM))
    return 0


def cmd_thread_update(args, client: Client) -> int:
    """Rewrite a thread line in place. What actually closes a thread."""
    text = " ".join(args.text).strip()
    if not text:
        print(_c("  nothing to write", C_RED), file=sys.stderr)
        return 1
    try:
        res = client.resolve_thread(args.thread_id, text)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    print(f"  {res['slug']}: thread updated")
    return 0


def _due_label(due: str | None) -> tuple[str, str]:
    """(text, colour) for a due date. Overdue reads as overdue, not as a date."""
    if not due:
        return "", C_DIM
    try:
        d = datetime.fromisoformat(due[:10]).date()
    except ValueError:
        return str(due)[:10], C_DIM
    days = (d - datetime.now().astimezone().date()).days
    if days < 0:
        return f"OVERDUE {abs(days)}d", C_RED
    if days == 0:
        return "due today", C_RED
    if days == 1:
        return "due tomorrow", C_YEL
    if days <= 7:
        return f"due {d.isoformat()}", C_YEL
    return f"due {d.isoformat()}", C_DIM


def cmd_meetings(args, client: Client) -> int:
    """What Otto has taken off your meeting notes, and what it owes you.

    `otto meetings` reports; `otto meetings ingest` reads Notion now rather than
    waiting for the four-hourly schedule.
    """
    if args.meetings_cmd == "ingest":
        try:
            res = client.ingest_meetings()
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run = res.get("run") or {}
        print(f"  reading Notion meeting notes (run {persona.short(run.get('id', ''))}, "
              f"pid {run.get('pid')})")
        if args.no_wait:
            print(_c("  check back with: otto meetings", C_DIM))
            return 0

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(3)
            if client.run(run["id"])["status"] != "running":
                break
        else:
            print(_c(f"  still running after {args.timeout}s. Check: otto meetings", C_DIM))
            return 0
        # Fall through to the report, which is the only place the outcome is visible.

    try:
        st = client.meetings()
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    sched = st.get("schedule") or {}
    cadence = (sched.get("cadence") or {})
    every = f"every {cadence['hours']}h" if cadence.get("hours") else cadence.get("kind", "?")
    armed = "on" if sched.get("autostart") and sched.get("enabled") else _c("OFF", C_YEL)
    print()
    print(f"  {_c('meeting notes', C_BOLD)}   {every}, autostart {armed}")
    last = st.get("last_ingest")
    print(f"    last read     {_age(last) if last else _c('never', C_YEL)}"
          + (f"   {_c(st['last_summary'], C_DIM)}" if st.get("last_summary") else ""))
    wm = st.get("watermark") or "none"
    print(f"    pages read    {st.get('pages_seen', 0)} {_c(f'(watermark {wm})', C_DIM)}")
    # "taken", not "filed": a restated commitment bumps an existing card rather than
    # creating one, so this counts items processed and the open count is the board.
    print(f"    items taken   {st.get('filed_total', 0)} total, "
          f"{st.get('open_tasks', 0)} cards still open")
    if not st.get("autoqueue"):
        print(_c("    action items land in backlog and wait for /orchestrate "
                 "(autoqueue off)", C_DIM))

    pages = st.get("recent_pages") or []
    if pages:
        print()
        print(f"  {_c('MEETINGS READ', C_DIM)}")
        for p in pages:
            n = p.get("filed") or 0
            print(f"    {_c(str(p.get('at') or '?'), C_DIM)}  {str(p.get('title') or '?')[:52]:<52} "
                  f"{_c(f'{n} item(s)' if n else 'nothing to do', C_DIM if n else C_GRN)}")

    open_tasks = [t for t in (st.get("tasks") or []) if t["status"] != "done"]
    if open_tasks:
        print()
        print(f"  {_c('ON THE BOARD', C_DIM)}")
        for t in open_tasks:
            text, col = _due_label(t.get("due"))
            print(f"    {_c(t['id'][:6], C_DIM)} {_cpad(t['status'], 10, C_DIM)} "
                  f"{t['title'][:52]:<52} {_c(text or 'no date', col)}")
            print(f"           {_c(t.get('tier') or 'no tier', C_DIM)}"
                  + (_c(f"  seen {t['seen_count']}x", C_YEL) if t.get("seen_count", 1) > 1 else ""))
    print()
    return 0


def cmd_outreach(args, client: Client) -> int:
    """The ledger of messages Otto sent, or was stopped from sending, to colleagues."""
    data = client.outreach(args.state)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    s = data["summary"]
    on = s["enabled"]
    print(_c("  OUTREACH", C_BOLD)
          + _c("   own-initiative sending " + ("ON" if on else "OFF, composed and held only")
               + "; directed (otto dm) always sends",
               C_YEL if on else C_DIM))
    if on and not s["can_send"]:
        print(_c(f"  enabled but no {'OTTO_SLACK_BOT_TOKEN'}: every send will fail",
                 C_RED))
    print(_c(f"  hold {s['hold_minutes']}m   sent 24h {s['sent_24h']}/{s['max_per_day']}"
             f"   held {s['held']}", C_DIM))
    items = data["items"]
    if not items:
        print("\n  nothing. Otto has not composed a message to anybody.")
        return 0
    for o in items[: args.limit]:
        col = {"held": C_YEL, "sent": "", "failed": C_RED}.get(o["state"], C_DIM)
        print()
        print(f"  {o['id'][:6]}  " + _cpad(o["state"], 8, col)
              + f"{o['to'][:26]:<27}{o['at'][5:16]}")
        for chunk in textwrap.wrap(o["body"], width=86)[:4]:
            print(f"          {chunk}")
        print(_c(f"          why: {o['why'][:80]}", C_DIM))
        if o["state"] == "held":
            print(_c(f"          sends {o['send_after'][11:16]}Z   "
                     f"otto outreach --kill {o['id'][:6]}", C_YEL))
        if o.get("error"):
            print(_c(f"          {o['error'][:80]}", C_DIM))
    return 0


def cmd_outreach_act(args, client: Client) -> int:
    if getattr(args, "extend", None):
        o = client.extend_outreach(args.extend, args.minutes)
        print(f"  extended {args.minutes}m: to {o['to']} / {o['body'][:60]}")
        print(_c(f"  now sends {o['send_after'][11:16]}Z   "
                 f"otto outreach --kill {o['id'][:6]}", C_YEL))
        return 0
    o = (client.kill_outreach(args.kill) if args.kill
         else client.send_outreach(args.send))
    verb = "killed" if args.kill else o["state"]
    print(f"  {verb}: to {o['to']} / {o['body'][:60]}")
    if o.get("error"):
        print(_c(f"  {o['error']}", C_YEL))
    return 0


def cmd_prep(args, client: Client) -> int:
    """Who you are about to talk to, and what is open with them. Local read."""
    from .. import prep as _prep

    store = Store()
    p = _prep.for_person(store, args.who) if args.who else _prep.next_meeting(store)
    if args.who and p is None:
        print(f"  no dossier matching {args.who!r}. otto people", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({
            "event": (p.event._asdict() if p and p.event else None),
            "minutes": (p.minutes if p else None),
            "matched": [{"slug": m.person["slug"], "name": m.person.get("display_name"),
                         "how": m.how, "raw": m.raw} for m in (p.matched if p else [])],
            "unmatched": (p.unmatched if p else []),
            "ambiguous": [{"raw": r, "candidates": c}
                          for r, c in (p.ambiguous if p else [])],
        }, indent=2, default=str))
        return 0
    print(_prep.render(store, p))
    return 0


def cmd_people(args, client: Client) -> int:
    """Operational dossiers. Local files under OTTO_HOME, never in a repo."""
    from .. import people as _people
    if args.sync:
        try:
            r = _people.sync(Path(args.sync))
        except (RuntimeError, OSError, ValueError) as e:
            print(_c(f"  {e}", C_RED))
            return 1
        print(f"  created {len(r.created)}  updated {len(r.updated)}  "
              f"bodies preserved {r.kept_bodies}  service accounts skipped "
              f"{len(r.skipped_service)}")
        return 0

    if args.touch:
        spool = config.SLACK_SPOOL_DIR / "new.json"
        try:
            data = json.loads(spool.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(_c(f"  spool unreadable: {e}", C_RED))
            return 1
        notes = _people.apply_contacts(data.get("contacts") or [])
        for n in notes:
            print(f"  {n}")
        if not notes:
            print("  nothing to advance (the daemon may already have applied this "
                  "spool, which is the normal case)")
        return 0

    if args.show:
        d = _people.get(args.show)
        if d is None:
            print(_c(f"  no dossier for {args.show}", C_YEL))
            return 1
        path = _people.PEOPLE_DIR / f"{d['slug']}.md"
        print(path.read_text(encoding="utf-8"))
        return 0

    rows = _people.load()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(_c("  no dossiers yet. otto people --sync <directory-dump.json>", C_YEL))
        return 0
    noted = sum(1 for d in rows if d.get("has_notes"))
    ext = [d for d in rows if d.get("external")]
    # Colleagues first, then outside contacts. Mixing them reads as one roster and the
    # distinction matters: an external contact has no directory record to reconcile against.
    for d in sorted(rows, key=lambda x: (x.get("external", False),
                                         x.get("department") or "~", x["slug"])):
        mark = "*" if d.get("has_notes") else " "
        group = "EXTERNAL" if d.get("external") else (d.get("department") or "-")
        org = d.get("org") or ""
        loc = "/".join(x for x in (d.get("city"), d.get("countryCode")) if x)
        print(f"  {mark} {d['display_name']:<26} {group:<12} "
              f"{(d.get('title') or org or '-'):<30} {loc}")
    print(f"\n  {len(rows)} dossiers ({len(ext)} external), {noted} with notes "
          f"(* = has notes).  {_people.PEOPLE_DIR}")
    return 0


def add_dm(sub) -> None:
    s = sub.add_parser("dm", help="group DM a colleague AND the owner, as Otto, now "
                                  "(for messages the owner asked for)")
    s.add_argument("who", nargs="+", help="login, email, or people slug; the owner is always added")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.add_argument("--why", help="what the owner asked, in a line (goes in the ledger)")
    s.add_argument("--task", help="board task id this is for, if any")
    s.set_defaults(fn=cmd_dm)


def add_outreach(sub) -> None:
    s = sub.add_parser("outreach", help="messages Otto wants to send other people")
    s.add_argument("--state", choices=["held", "sent", "killed", "expired", "failed"])
    s.add_argument("--kill", metavar="ID", help="stop a held message")
    s.add_argument("--send", metavar="ID", help="send a held message now, skip the hold")
    s.add_argument("--extend", metavar="ID",
                   help="push a held message's send time back, without deciding")
    s.add_argument("--minutes", type=int, default=10,
                   help="how far --extend pushes it (default 10)")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=lambda a, c: (cmd_outreach_act(a, c)
                                    if (a.kill or a.send or a.extend)
                                    else cmd_outreach(a, c)),
                   outreach_cmd=None)
    out_sub = s.add_subparsers(dest="outreach_cmd")

    # Compose only. There is deliberately no CLI path that sends without a hold:
    # a caller that can skip the interlock is a caller with no interlock.
    a = out_sub.add_parser("compose", help="draft a HELD message (never sends now)")
    a.add_argument("--to", required=True, help="login, email, or name")
    a.add_argument("--body", required=True)
    a.add_argument("--why", required=True,
                   help="what the owner decides on in ten seconds. Required")
    a.add_argument("--channel", default="slack-dm",
                   choices=["slack-dm", "slack-channel"])
    a.add_argument("--source", default="otto")
    a.set_defaults(fn=cmd_outreach_compose, kill=None, send=None, extend=None,
                   minutes=10, state=None, limit=10, json=False)

    a = out_sub.add_parser("resolve",
                           help="record a colleague's Slack id (required before sending)")
    a.add_argument("target", help="their email / login")
    a.add_argument("--force", action="store_true", help="re-resolve an existing id")
    a.set_defaults(fn=cmd_outreach_resolve, kill=None, send=None, extend=None,
                   minutes=10, state=None, limit=10, json=False)


def add_prep(sub) -> None:
    s = sub.add_parser("prep", help="meeting prep: who you are about to talk to")
    s.add_argument("who", nargs="?",
                   help="a name, login, or slug. Defaults to your next meeting")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_prep)


def add_people(sub) -> None:
    s = sub.add_parser("people", help="operational dossiers per person")
    s.add_argument("--sync", metavar="DIRECTORY_DUMP",
                   help="regenerate frontmatter from a directory users dump (JSON); "
                        "hand-written bodies are never touched")
    s.add_argument("--show", metavar="LOGIN", help="print one dossier")
    s.add_argument("--touch", action="store_true",
                   help="apply the DM spool's contact facts to last_contact now "
                        "(the daemon does this on its own; this is for backfill "
                        "and debugging)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_people, people_cmd=None)
    ppl_sub = s.add_subparsers(dest="people_cmd")

    a = ppl_sub.add_parser("note", help="append a note to a dossier section")
    a.add_argument("slug")
    a.add_argument("text", nargs="+")
    a.add_argument("--section", default="Threads")
    a.set_defaults(fn=cmd_people_note, sync=None, show=None, json=False, touch=False)


def add_threads(sub) -> None:
    s = sub.add_parser("threads", help="every dated dossier thread, the whole list")
    s.add_argument("--band", choices=["fresh", "quiet", "ancient"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_threads)


def add_thread_note(sub) -> None:
    s = sub.add_parser("thread-note",
                       help="note one thread and let Otto decide what to do")
    s.add_argument("thread_id")
    s.add_argument("note", nargs="+")
    s.set_defaults(fn=cmd_thread_note)


def add_thread_update(sub) -> None:
    s = sub.add_parser("thread-update",
                       help="rewrite a thread line in place (what closes a thread)")
    s.add_argument("thread_id")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_thread_update)


def add_meetings(sub) -> None:
    s = sub.add_parser("meetings", help="action items Otto took off your Notion meeting notes")
    m_sub = s.add_subparsers(dest="meetings_cmd")
    s.set_defaults(fn=cmd_meetings, meetings_cmd="status", json=False,
                   no_wait=False, timeout=180)
    s.add_argument("--json", action="store_true")
    mi = m_sub.add_parser("ingest", help="read new meeting notes now, then report")
    mi.add_argument("--no-wait", action="store_true")
    mi.add_argument("--timeout", type=int, default=180)
    mi.add_argument("--json", action="store_true")
    mi.set_defaults(fn=cmd_meetings, meetings_cmd="ingest")


PARSERS = {
    "dm": add_dm,
    "outreach": add_outreach,
    "prep": add_prep,
    "people": add_people,
    "threads": add_threads,
    "thread-note": add_thread_note,
    "thread-update": add_thread_update,
    "meetings": add_meetings,
}
