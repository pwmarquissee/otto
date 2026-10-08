"""The board: columns, cards, proposals, acknowledgements, what to stop doing."""

from __future__ import annotations

import json
import sys

from .. import config, persona
from ..store import Store
from ..client import Client
from ._fmt import C_BOLD, C_DIM, C_GRN, C_RED, C_YEL, _c, _cpad
from ._text import _read_text_arg, _resolve_detail


_PRIO_COLOR = {"urgent": C_RED, "high": C_YEL, "normal": "", "low": C_DIM}


def cmd_board(args, client: Client) -> int:
    data = client.board(args.domain)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(_c(f"  BOARD", C_BOLD)
          + _c(f"   {data['total']} cards "
               f"({data['stored']} tasks, {data['derived']} derived)", C_DIM))
    for col in data["columns"]:
        if not col["cards"] and args.busy:
            continue
        print()
        print(_c(f"  {col['label'].upper()}", C_BOLD) + _c(f"  {col['count']}", C_DIM))
        if not col["cards"] and not col.get("hidden"):
            print(_c("    empty", C_DIM))
        if col.get("hidden"):
            print(_c(f"    +{col['hidden']} finished more than "
                     f"{col['hidden_after_days']}d ago, off the board "
                     f"(otto task ls)", C_DIM))
        for card in col["cards"]:
            prio = _cpad(card["priority"], 7, _PRIO_COLOR.get(card["priority"], ""))
            # A derived card has no stored row, so show no id to drag around.
            ident = card["id"][:6] if card["movable"] else "  --  "
            dom = _cpad(card["domain"][:4], 5, C_DIM)
            print(f"    {ident}  {prio}{dom}{card['title'][:52]}")
            if card.get("detail"):
                print(f"            {_c(str(card['detail'])[:70], C_DIM)}")
    return 0


def cmd_task_add(args, client: Client) -> int:
    task = client.add_task(
        title=args.title, status=args.status, domain=args.domain,
        priority=args.priority, detail=_resolve_detail(args), agent=args.agent,
        tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
        task_ref=args.ref, due=args.due, cwd=args.cwd, auto=not args.no_auto,
        origin=args.origin, dedupe=not args.no_dedupe,
    )
    if task.get("merged"):
        print(_c(f"  matched an open card ({task['merged']}); bumped it instead "
                 f"(seen {task.get('seen_count', 1)}x). "
                 f"--no-dedupe files it separately.", C_DIM))
    print(f"  {task['id'][:6]}  {task['title']}")
    print(f"  {task['status']} / {task['priority']} / {task['domain']}")
    return 0


def cmd_task_ls(args, client: Client) -> int:
    tasks = client.tasks(args.domain)
    # Faded cards and duplicates are out of every default listing on purpose.
    # --faded and --duplicates are the doors back to them; nothing is deleted.
    dupes = [t for t in tasks if t.get("duplicate_of")]
    tasks = [t for t in tasks if not t.get("duplicate_of")]
    faded = [t for t in tasks if t.get("status") == "faded"]
    if args.duplicates:
        tasks = dupes
    else:
        tasks = faded if args.faded else [t for t in tasks if t.get("status") != "faded"]
    if args.json:
        print(json.dumps(tasks, indent=2))
        return 0
    if not tasks:
        print("  no tasks. Add one: otto task add \"...\"")
        return 0
    print(f"  {'ID':<8}{'STATUS':<11}{'PRIO':<9}{'DOM':<10}TITLE")
    for t in tasks:
        prio = _cpad(t["priority"], 9, _PRIO_COLOR.get(t["priority"], ""))
        print(f"  {t['id'][:6]:<8}{t['status']:<11}{prio}{t['domain']:<10}{t['title'][:48]}")
        if args.duplicates and t.get("duplicate_of"):
            print(_c(f"          -> {t['duplicate_of'][:6]}", C_DIM))
    if not args.duplicates:
        if faded and not args.faded:
            print(_c(f"  and {len(faded)} faded (otto task ls --faded)", C_DIM))
        if dupes:
            print(_c(f"  and {len(dupes)} merged as duplicates (otto task ls --duplicates)",
                     C_DIM))
    return 0


def cmd_task_show(args, client: Client) -> int:
    """One card, whole, read-only: every field a session or the owner might act
    on, including the plan and the result that `task ls` has no room for."""
    try:
        t = client.task(args.id)
    except RuntimeError as e:
        print(f"  {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(t, indent=2))
        return 0
    print(_c(f"  {t['title']}", C_BOLD))
    head = [("id", t["id"]), ("status", t.get("status")), ("priority", t.get("priority")),
            ("domain", t.get("domain")), ("owner", t.get("owner")), ("tier", t.get("tier")),
            ("agent", t.get("agent")), ("tags", ", ".join(t.get("tags") or []) or None),
            ("origin", t.get("origin")), ("created", t.get("created_at")),
            ("updated", t.get("updated_at")), ("due", t.get("due")),
            ("run mode", t.get("run_mode")), ("plan approved", t.get("plan_approved")),
            ("run", (t.get("run_id") or "")[:6] or None),
            ("duplicate of", (t.get("duplicate_of") or "")[:6] or None)]
    for k, v in head:
        if v not in (None, "", []):
            print(f"  {k:<14}{v}")
    for label, key in (("DETAIL", "detail"), ("PLAN", "plan"), ("RESULT", "result"),
                       ("LAST ERROR", "last_error")):
        text = t.get(key)
        if text:
            print()
            print(_c(f"  {label}", C_DIM))
            for line in str(text).splitlines():
                print(f"    {line}")
    run = t.get("run")
    if run:
        print()
        print(_c("  RUN", C_DIM))
        print(f"    {run['id'][:6]} {run.get('status')} {run.get('name')}"
              + (f" cost ${run['cost_usd']:.2f}" if run.get("cost_usd") else ""))
    return 0


def cmd_task_dedupe(args, client: Client) -> int:
    """Fold duplicate open cards into one. The tick does this on its own; the verb
    shows the plan, and lets a run's report carry what was folded."""
    r = client.dedupe_tasks(dry_run=args.dry_run)
    rows = r.get("merged") or []
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  no duplicates on the open board.")
        return 0
    label = "WOULD MERGE" if args.dry_run else "MERGED"
    keepers = {row["keeper_id"] for row in rows}
    print(_c(f"  {len(rows)} {label} into {len(keepers)} card(s)", C_BOLD))
    for kid in sorted(keepers, key=lambda k: -len([x for x in rows if x["keeper_id"] == k])):
        mine = [x for x in rows if x["keeper_id"] == kid]
        k = mine[0]
        print(f"\n  keep  {kid[:6]}  {_cpad(k['keeper_status'], 10, C_DIM)}{k['keeper_title'][:58]}")
        for x in mine:
            print(f"    x   {x['dupe_id'][:6]}  {_cpad(x['dupe_status'], 10, C_DIM)}"
                  f"{x['dupe_title'][:58]}")
            print(f"            {_c(x['why'] + ' · from ' + str(x['dupe_origin']), C_DIM)}")
    if not args.dry_run:
        print(_c("\n  undo one: otto task mv <id> backlog   list: otto task ls --duplicates",
                 C_DIM))
    print()
    return 0


def cmd_task_mv(args, client: Client) -> int:
    ids = list(args.task_id)
    if len(ids) == 1:
        task = client.patch_task(ids[0], status=args.status)
        print(f"  {task['title']} -> {task['status']}")
        return 0
    # Many ids: one batch call, one write. `queued` is refused by the daemon for a
    # batch (promotion is one card at a time, through the gate).
    r = client.patch_tasks(ids, status=args.status)
    for t in r.get("updated", []):
        print(f"  {t['id'][:6]}  {t['title'][:60]} -> {t['status']}")
    for m in r.get("missing", []):
        print(_c(f"  no unique task matching {m}", C_YEL), file=sys.stderr)
    return 1 if r.get("missing") else 0


def cmd_task_set(args, client: Client) -> int:
    detail = _resolve_detail(args)
    if detail is not None and getattr(args, "append", False):
        # The triage pass was told `--detail-file` appends. It replaced, and on
        # 2026-08-31 wiped the evidence trail off four stale cards. Append is now a
        # flag rather than the default so an existing caller that MEANS replace
        # still gets it; the pass is told to pass --append.
        current = client.tasks(None)
        cur = next((t for t in current if t["id"].startswith(args.task_id)), None)
        if cur is None:
            print(f"  no task matching {args.task_id}", file=sys.stderr)
            return 2
        from ..models import iso, utcnow
        stamp = iso(utcnow())[:16].replace("T", " ")
        detail = "\n\n".join(x for x in (
            (cur.get("detail") or "").rstrip(),
            f"--- appended {stamp} UTC ---\n{detail.strip()}",
        ) if x)
    # "derive" clears the pin: the daemon stores "" as None (api/tasks.py).
    permissions = {None: None, "derive": ""}.get(args.permissions, args.permissions)
    changes = {k: v for k, v in (
        ("title", args.title), ("priority", args.priority),
        ("domain", args.domain), ("detail", detail),
        ("agent", args.agent), ("due", args.due),
        ("permissions", permissions),
    ) if v is not None}
    if not changes:
        print("  nothing to change", file=sys.stderr)
        return 2
    task = client.patch_task(args.task_id, **changes)
    print(f"  {task['id'][:6]}  {task['title']}  ({task['status']} / {task['priority']})")
    return 0


def cmd_task_reply(args, client: Client) -> int:
    """Answer a card in a sentence. The due toast's Reply button lands in the same
    place; this is the terminal door to it."""
    if args.text_file:
        src = sys.stdin if args.text_file == "-" else open(args.text_file, encoding="utf-8")
        with src:
            text = src.read()
    else:
        text = " ".join(args.text or [])
    if not text.strip():
        print("  nothing to say: pass the text, or --text-file PATH (or -)", file=sys.stderr)
        return 2
    r = client.reply_task(args.task_id, text.strip())
    t = r.get("task") or {}
    print(f"  {t.get('id', '')[:6]}  {t.get('title', '')}  (reply appended)")
    if r.get("run"):
        print(f"  card-reply session {r['run']['id'][:6]} is deciding what follows; "
              f"the receipt arrives as a notice")
    else:
        print(_c(f"  no session started: {r.get('error')}", C_YEL), file=sys.stderr)
        return 1
    return 0


def cmd_task_plan(args, client: Client) -> int:
    """Read, write, approve, or withdraw a card's plan.

    The plan is the prompt a run will follow, in the owner's hands before it runs.
    `--file` replaces it (edit the file, not the flags); `--approve` stamps it;
    `--withdraw` clears the stamp so the next promote is refused again.
    """
    from ..models import iso, utcnow

    tasks = client.tasks(None)
    cur = next((t for t in tasks if t["id"].startswith(args.task_id)), None)
    if cur is None:
        print(f"  no task matching {args.task_id}", file=sys.stderr)
        return 2

    text = _read_text_arg(args.file, " ".join(args.text) if args.text else None)
    changes: dict = {}
    if text:
        changes["plan"] = text
        # A new plan is not the approved plan, whatever the old stamp said.
        changes["plan_approved"] = ""
    if args.approve:
        if not (text or cur.get("plan")):
            print("  nothing to approve: the card has no plan yet. Write one with "
                  "--file, or promote it with --prepare to have a run draft one",
                  file=sys.stderr)
            return 2
        changes["plan_approved"] = iso(utcnow())
    if args.withdraw:
        changes["plan_approved"] = ""

    if changes:
        cur = client.patch_task(cur["id"], **changes)

    print(f"  {cur['id'][:6]}  {cur['title'][:60]}")
    stamp = cur.get("plan_approved")
    state = (_c(f"approved {stamp[:16].replace('T', ' ')} UTC", C_GRN) if stamp
             else _c("draft, not approved", C_YEL) if cur.get("plan")
             else _c("no plan", C_DIM))
    print(f"  plan: {state}")
    if cur.get("plan") and not args.quiet:
        print()
        for line in cur["plan"].splitlines():
            print(f"    {line}")
        print()
    if cur.get("plan") and not stamp:
        print(_c(f"  edit: write it to a file, then otto task plan {cur['id'][:6]} "
                 f"--file PATH   approve: otto task plan {cur['id'][:6]} --approve",
                 C_DIM))
    return 0


def cmd_task_open(args, client: Client) -> int:
    """Open a card as an attended Claude Code session: a visible console, the owner at
    the keyboard, normal permission prompts, and the card linked to the run so it
    stops rotting while he works on it outside Otto.

    This is the door for "I started doing it myself". The card moves to running,
    the session is seeded with the card and its plan, and when the console closes
    the card lands in needs-you with what the session reported, one click from done.
    """
    from .. import dispatch as _dispatch
    from ..models import Task

    tasks = client.tasks(None)
    raw = next((t for t in tasks if t["id"].startswith(args.task_id)), None)
    if raw is None:
        print(f"  no task matching {args.task_id}", file=sys.stderr)
        return 2
    task = Task.model_validate(raw)
    if task.status == "running" and task.run_id:
        print(f"  {task.id[:6]} already has a run ({task.run_id[:6]}). "
              f"otto logs {task.run_id[:6]} to follow it", file=sys.stderr)
        return 1

    cwd = args.cwd or task.cwd or str(config.TASK_DEFAULT_CWD)
    # In a herdr pane the daemon waits for claude to come up before answering.
    client.timeout = max(client.timeout, config.HERDR_START_TIMEOUT + 15)
    run = client.spawn(
        name=task.title[:40], prompt=_dispatch.attended_prompt(task), cwd=cwd,
        agent=task.agent, mode="windowed", task_id=task.id, tier=task.tier,
        permissions=args.permissions, domain=task.domain, model=args.model,
        notes="mode=task | mode=attended",
    )
    client.patch_task(task.id, status="running", run_id=run["id"], cwd=cwd)
    print(_c(f"  opened  {task.id[:6]}  {task.title[:50]}", C_GRN))
    print(f"  attended session {persona.short(run['id'])}  pid {run['pid']}  in {cwd}")
    print(_c("  the card is running against this window; close it and the card "
             "asks you whether it is done", C_DIM))
    return 0


def cmd_task_rm(args, client: Client) -> int:
    client.delete_task(args.task_id)
    print(f"  removed {args.task_id}")
    return 0


def cmd_propose(args, client: Client) -> int:
    """File a finding into the backlog. Agents call this mid-run."""
    if args.json_body:
        raw = sys.stdin.read() if args.json_body == "-" else args.json_body
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  not valid JSON: {e}", file=sys.stderr)
            return 2
        tasks = body if isinstance(body, list) else body.get("tasks") or []
    else:
        if not args.title:
            print("  give a title, or --json-body", file=sys.stderr)
            return 2
        tasks = [{"title": args.title, "detail": _resolve_detail(args),
                  "priority": args.priority, "domain": args.domain}]

    r = client.propose(tasks, origin=args.origin, run_id=args.run_id)
    if not r["notes"]:
        print("  nothing filed")
        return 0
    for n in r["notes"]:
        print(f"  {n}")
    return 0


def cmd_task_run(args, client: Client) -> int:
    """Dispatch a task now, regardless of the queue's pace."""
    try:
        r = client.dispatch_task(args.task_id, force=args.force, permissions=args.permissions)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    run = r["run"]
    print(f"  {r['message']}  [{run.get('permissions', 'yolo')}]")
    print(f"  follow: otto logs {persona.short(run['id'])}")
    return 0


def cmd_task_yolo(args, client: Client) -> int:
    """The one-word approval: approve the plan the card holds, pin yolo, run it now.

    Goes through the same gate `triage promote` uses, so a card Otto may not run
    (owned by the owner, assistive tier, not assessed) prints the gate's sentence
    and stops. The word answers the plan gate, nothing else.
    """
    try:
        r = client.yolo_task(args.task_id)
    except RuntimeError as e:
        print(_c(f"  REFUSED  {e}", C_YEL), file=sys.stderr)
        return 1
    t = r["task"]
    print(_c(f"  yolo  {t['id'][:6]}  {t['title'][:50]}", C_GRN))
    if t.get("plan_approved"):
        print(f"  plan approved {t['plan_approved'][:16].replace('T', ' ')} UTC, level yolo")
    if r.get("run"):
        print(f"  {r['message']}")
        print(f"  follow: otto logs {persona.short(r['run']['id'])}")
    else:
        print(_c(f"  queued, not started this instant: {r['message']}", C_DIM))
    return 0


def cmd_ack(args, client: Client) -> int:
    """Acknowledge a failed run, clearing its stuck Needs-you card."""
    try:
        if args.all or args.transient:
            # Classify before acknowledging, so --transient can actually tell an
            # upstream error from a real one on failures predating the field.
            rc = client.reclassify_runs()
            if rc["classified"]:
                print(f"  classified {rc['classified']} historical failure(s) as "
                      f"upstream API errors")
            r = client.ack_all_runs(kind="api" if args.transient else None)
            if not r["count"]:
                print("  nothing outstanding to acknowledge")
                return 0
            print(f"  acknowledged {r['count']} run(s): {', '.join(r['acknowledged'][:8])}"
                  + (" ..." if r["count"] > 8 else ""))
            return 0
        if not args.run_id:
            print("  which run? otto ack <run-id>, or --all / --transient",
                  file=sys.stderr)
            return 2
        run = client.ack_run(args.run_id, undo=args.undo)
        verb = "un-acknowledged" if args.undo else "acknowledged"
        print(f"  {verb} {run['name']} ({run['status']})")
        return 0
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1


def cmd_retire(args, client: Client) -> int:
    """What Otto should stop doing. Read-only, local, deletes nothing."""
    from .. import retire as _retire

    store = Store()
    if args.json:
        print(json.dumps({"gaps": _retire.gaps(store)}, indent=2))
        return 0
    print(_retire.render(store))
    return 0


def add_board(sub) -> None:
    s = sub.add_parser("board", help="all outstanding work, in columns")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--busy", action="store_true", help="hide empty columns")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_board)


def add_task(sub) -> None:
    tsk = sub.add_parser("task", help="create and move board tasks")
    tsk_sub = tsk.add_subparsers(dest="task_cmd", required=True)

    a = tsk_sub.add_parser("add", help="create a task")
    a.add_argument("title")
    a.add_argument("--status", default="backlog",
                   choices=["backlog", "queued", "running", "needs-you", "blocked", "done",
                            "faded"])
    a.add_argument("--domain", choices=["work", "personal"], default="work")
    a.add_argument("--priority", choices=["low", "normal", "high", "urgent"], default="normal")
    a.add_argument("--detail")
    a.add_argument("--detail-file", metavar="PATH",
                   help="read detail from a file, or - for stdin. Use this when "
                        "the text quotes a command line or detection evidence")
    a.add_argument("--agent", help="subagent type to dispatch to")
    a.add_argument("--tags", help="comma separated")
    a.add_argument("--ref", help="the external queue's id, when mirroring one")
    a.add_argument("--due")
    a.add_argument("--cwd", help="working directory for the spawned session")
    a.add_argument("--no-auto", action="store_true",
                   help="park in queued without ever auto-dispatching")
    a.add_argument("--origin", help="who filed this, if not you (e.g. a schedule name)")
    a.add_argument("--no-dedupe", action="store_true",
                   help="file it even if an open card matches it, and tag it `distinct` "
                        "so the sweep never folds it either (the default merges)")
    a.set_defaults(fn=cmd_task_add)

    a = tsk_sub.add_parser("ls", help="list tasks")
    a.add_argument("--domain", choices=["work", "personal"])
    a.add_argument("--faded", action="store_true",
                   help="list only faded cards (hidden from every other listing)")
    a.add_argument("--duplicates", action="store_true",
                   help="list only cards merged into another as duplicates")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_ls)

    a = tsk_sub.add_parser("show", help="one card, whole and read-only (detail, plan, result)")
    a.add_argument("id", help="task id or unique prefix")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_show)

    a = tsk_sub.add_parser("dedupe", help="fold duplicate open cards into one (the tick does this too)")
    a.add_argument("--dry-run", action="store_true", help="show what would merge, change nothing")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_task_dedupe)

    a = tsk_sub.add_parser("mv", help="move one or more tasks to another column")
    a.add_argument("task_id", nargs="+", help="one id, or several; the last argument is the column")
    a.add_argument("status",
                   choices=["backlog", "queued", "running", "needs-you", "blocked", "done",
                            "faded"])
    a.set_defaults(fn=cmd_task_mv)

    a = tsk_sub.add_parser("set", help="edit a task's fields")
    a.add_argument("task_id")
    a.add_argument("--title")
    a.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    a.add_argument("--domain", choices=["work", "personal"])
    a.add_argument("--detail")
    a.add_argument("--detail-file", metavar="PATH",
                   help="read detail from a file, or - for stdin. Use this when "
                        "the text quotes a command line or detection evidence")
    a.add_argument("--agent")
    a.add_argument("--due")
    a.add_argument("--append", action="store_true",
                   help="append --detail/--detail-file to the existing detail, dated, "
                        "instead of replacing it")
    a.add_argument("--permissions", choices=["plan", "yolo", "derive"],
                   help="pin the level the next run gets; derive = prepare runs plan, the rest yolo")
    a.set_defaults(fn=cmd_task_set)

    a = tsk_sub.add_parser("plan", help="read, write, or approve the plan a run will follow")
    a.add_argument("task_id")
    a.add_argument("text", nargs="*", help="the plan, inline (prefer --file)")
    a.add_argument("--file", metavar="PATH", help="read the plan from a file, or - for stdin")
    a.add_argument("--approve", action="store_true", help="approve the plan; the card may then run")
    a.add_argument("--withdraw", action="store_true", help="withdraw approval")
    a.add_argument("--quiet", action="store_true", help="do not print the plan body")
    a.set_defaults(fn=cmd_task_plan)

    a = tsk_sub.add_parser("open", help="work a card with the owner at the keyboard: a visible, "
                                        "attended Claude Code session linked to the card")
    a.add_argument("task_id")
    a.add_argument("--cwd", help="where the session runs; defaults to the card's cwd")
    a.add_argument("--model", help="default: the ordinary task model")
    lvl = a.add_mutually_exclusive_group()
    lvl.add_argument("--yolo", dest="permissions", action="store_const", const="yolo",
                     help="--dangerously-skip-permissions: the full operator (default: you are at the keyboard)")
    lvl.add_argument("--plan", dest="permissions", action="store_const", const="plan",
                     help="--permission-mode plan: read-only investigation that writes a plan")
    a.set_defaults(fn=cmd_task_open, permissions="yolo")

    a = tsk_sub.add_parser("run", help="dispatch a task now")
    a.add_argument("task_id")
    a.add_argument("--force", action="store_true",
                   help="ignore the attempt cap and the master switch")
    lvl = a.add_mutually_exclusive_group()
    lvl.add_argument("--yolo", dest="permissions", action="store_const", const="yolo",
                     help="--dangerously-skip-permissions: the full operator")
    lvl.add_argument("--plan", dest="permissions", action="store_const", const="plan",
                     help="--permission-mode plan: read-only investigation that writes a plan")
    a.set_defaults(fn=cmd_task_run, permissions=None)

    a = tsk_sub.add_parser("yolo", help="approve the card's plan and run it now at full "
                                        "permissions (the one-word approval)")
    a.add_argument("task_id")
    a.set_defaults(fn=cmd_task_yolo)

    a = tsk_sub.add_parser("reply", help="answer a card: update it, or give Otto context")
    a.add_argument("task_id")
    a.add_argument("text", nargs="*")
    a.add_argument("--text-file", metavar="PATH", help="read the reply from a file, or - for stdin")
    a.set_defaults(fn=cmd_task_reply)

    a = tsk_sub.add_parser("rm", help="delete a task")
    a.add_argument("task_id")
    a.set_defaults(fn=cmd_task_rm)


def add_propose(sub) -> None:
    s = sub.add_parser("propose", help="file a finding into the backlog")
    s.add_argument("title", nargs="?")
    s.add_argument("--detail")
    s.add_argument("--detail-file", metavar="PATH",
                   help="read detail from a file, or - for stdin. Use this when "
                        "the text quotes a command line or detection evidence")
    s.add_argument("--priority", choices=["low", "normal", "high", "urgent"], default="normal")
    s.add_argument("--domain", choices=["work", "personal"], default="work")
    s.add_argument("--origin", default="agent", help="who is filing this")
    s.add_argument("--run-id")
    s.add_argument("--json-body", metavar="JSON",
                   help='a {"tasks":[...]} payload, or - to read stdin')
    s.set_defaults(fn=cmd_propose)


def add_ack(sub) -> None:
    s = sub.add_parser("ack", help="acknowledge a failed run and clear its card")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--all", action="store_true",
                   help="acknowledge every outstanding failure")
    s.add_argument("--transient", action="store_true",
                   help="acknowledge only upstream API failures (the 529 burst case)")
    s.add_argument("--undo", action="store_true", help="put one back")
    s.set_defaults(fn=cmd_ack)


def add_retire(sub) -> None:
    s = sub.add_parser("retire", help="what Otto should stop doing (deletes nothing)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_retire)


PARSERS = {
    "board": add_board,
    "task": add_task,
    "propose": add_propose,
    "ack": add_ack,
    "retire": add_retire,
}
