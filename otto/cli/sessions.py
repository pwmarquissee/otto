"""Claude Code sessions and the harness they live in: sessions, dispatch, herdr, the desktop app."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime

from .. import config
from ..store import Store
from ..client import Client, DaemonDown
from ._fmt import C_BOLD, C_CYA, C_DIM, C_GRN, C_RED, C_YEL, _age, _c, _cpad


_SESSION_COLOR = {"busy": C_YEL, "waiting": C_CYA, "idle": C_GRN, "offline": C_DIM}


def cmd_sessions(args, client: Client) -> int:
    """Claude Code sessions on this box, as their own hooks report them.

    `install` and `uninstall` are local file writes and deliberately do NOT need
    the daemon: the first thing you do on a fresh machine is install the hooks, and
    requiring a running daemon to do that would be a needless ordering constraint.
    """
    from .. import sessions as _sessions

    action = getattr(args, "action", None)

    if action == "install":
        changed, msg = _sessions.install()
        print(f"  {msg}")
        if changed:
            print(_c("  takes effect in new sessions; run /hooks in an open one to "
                     "reload it there", C_DIM))
        return 0

    if action == "uninstall":
        _changed, msg = _sessions.uninstall()
        print(f"  {msg}")
        return 0

    if action == "name":
        title = " ".join(args.title).strip()
        try:
            s = client.rename_session(args.session_id, title)
        except DaemonDown:
            s = _sessions.rename(Store(), args.session_id, title).model_dump()
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        print(f"  {s['session_id'][:8]}  {s['title']}")
        return 0

    if action == "open":
        try:
            r = client.open_session(args.session_id)
            ok, msg = True, r.get("message", "")
        except DaemonDown:
            ok, msg = _sessions.open_session(Store(), args.session_id)
        except RuntimeError as e:
            ok, msg = False, str(e)
        print(f"  {msg}" if ok else _c(f"  {msg}", C_RED))
        return 0 if ok else 1

    if action == "watch":
        # A rail in a terminal tab: the list, redrawn every few seconds, waiting
        # first. Ctrl-C leaves. The dashboard has the same view with buttons.
        import time as _time
        try:
            while True:
                print("\x1b[2J\x1b[H", end="")
                print(_c(f"  otto sessions · {datetime.now().strftime('%H:%M:%S')} · "
                         f"Ctrl-C to stop", C_DIM))
                _print_sessions(client, args, _sessions)
                sys.stdout.flush()
                _time.sleep(args.every)
        except KeyboardInterrupt:
            print()
            return 0

    if action == "doctor":
        try:
            doc = client.sessions_doctor()
        except DaemonDown:
            doc = _sessions.doctor(Store())
        if args.json:
            print(json.dumps(doc, indent=2))
            return 0
        print()
        for c in doc["checks"]:
            mark = _c("ok  ", C_GRN) if c["ok"] else _c("FAIL", C_RED)
            print(f"  {mark}  {c['name']:<26} {_c(c['detail'], C_DIM)}")
        print()
        print(f"  {_c('settings', C_DIM)}  {doc['settings']}")
        print(f"  {_c('command ', C_DIM)}  {doc['command']}")
        print()
        return 0 if doc["ok"] else 1

    # Default: list.
    return _print_sessions(client, args, _sessions)


def _print_sessions(client: Client, args, _sessions) -> int:
    try:
        data = client.sessions(domain=args.domain, live=not args.all)
    except DaemonDown:
        store = Store()
        items = [s for s in store.sessions()
                 if (not args.domain or s.domain == args.domain)
                 and (args.all or s.live)]
        data = {"summary": _sessions.summary(store, args.domain),
                "sessions": [s.model_dump() for s in items]}

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    summary, rows = data["summary"], data["sessions"]
    # Waiting first: it is the only state asking the owner for something. Then busy,
    # idle, offline, and within a state the one that has been there longest.
    order = {"waiting": 0, "busy": 1, "idle": 2, "offline": 3}
    rows.sort(key=lambda s: (order.get(s["state"], 9), s.get("state_since") or ""))

    if not summary.get("hooks_installed"):
        print()
        print(_c("  session hooks are not installed", C_YEL))
        print(_c("  Otto can only see sessions it spawned itself. "
                 "Fix: otto sessions install", C_DIM))
        print()
        if not rows:
            return 0

    print()
    print(f"  {_c('SESSIONS', C_BOLD)}  "
          f"{summary['busy']} busy, {summary['waiting']} waiting, "
          f"{summary['idle']} idle"
          + (f", {summary['offline']} offline" if args.all else "")
          + (_c(f"   ({summary['unspawned']} not spawned by Otto)", C_DIM)
             if summary["unspawned"] else ""))
    print()

    if not rows:
        print(_c("    nothing reporting", C_DIM))
        print()
        return 0

    for s in rows:
        state = s["state"]
        held = _age(s.get("state_since"))
        where = s.get("repo") or (pathlib.Path(s["cwd"]).name if s.get("cwd") else "?")
        title = s.get("title") or where
        bits = []
        if s.get("host"):
            bits.append(s["host"])
        elif s.get("run_id"):
            bits.append("Otto run")
        if state == "offline":
            bits.append({"process": "exited", "hook": "closed",
                         "silence": "presumed, no SessionEnd"}.get(s.get("offline_reason"))
                        or ("presumed" if s.get("offline_inferred") else "closed"))
        flag = _c("  " + " · ".join(bits), C_DIM) if bits else ""
        print(f"  {_cpad(state, 8, _SESSION_COLOR.get(state, ''))}"
              f"{_cpad(s['session_id'][:8], 10, C_DIM)}"
              f"{_cpad(title[:52], 54)}"
              f"{_cpad(held, 12, C_DIM)}{flag}")
        if s.get("title"):
            print(f"          {_c(where[:40], C_DIM)}", end="")
            print()
        detail = s.get("note") or s.get("last_message")
        if detail and state in ("waiting", "busy"):
            print(f"          {_c(('asking: ' if state == 'waiting' else '') + detail[:96], C_DIM)}")
    print()
    print(_c("  open one: otto sessions open <id>    name one: otto sessions name <id> \"...\"", C_DIM))
    return 0


_HERDR_COLOR = {"working": C_YEL, "blocked": C_CYA, "idle": C_GRN, "done": C_GRN,
                "unknown": C_DIM}


def cmd_dispatch(args, client: Client) -> int:
    """The logistics strip: which card could go to which idle session, and the
    verbs that act on it. Advisory until approved; the gate is never widened."""
    action = getattr(args, "dispatch_cmd", None) or "list"

    if action == "approve":
        try:
            r = client.approve_proposal(args.proposal_id)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        print(_c(f"  {r['message']}", C_GRN))
        return 0

    if action == "dismiss":
        try:
            client.dismiss_proposal(args.proposal_id)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        print("  dismissed; it will not be proposed again while that session lives")
        return 0

    if action == "to":
        try:
            r = client.dispatch_to(args.task_id, args.target)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        print(_c(f"  {r['message']}", C_GRN))
        return 0

    # list
    v = client.logistics_refresh() if getattr(args, "refresh", False) else client.logistics()
    if args.json:
        print(json.dumps(v, indent=2))
        return 0
    h = v.get("herdr") or {}
    if not h.get("installed"):
        print(_c("  herdr is not installed (herdr.dev); the dispatcher needs it for panes", C_YEL))
        return 1
    if not h.get("running"):
        print(_c("  herdr server is not running: otto herdr up", C_YEL))
        return 1
    rail = v.get("rail") or []
    counts = {}
    for r in rail:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print()
    print(f"  {_c('SESSIONS IN HERDR', C_BOLD)}  "
          + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    for r in rail:
        name = r.get("title") or r.get("task_title") or (r.get("cwd") or "").replace("\\", "/").split("/")[-1]
        print(f"  {_cpad(r['status'], 9, _HERDR_COLOR.get(r['status'], ''))}"
              f"{_cpad(r['agent'], 12, C_DIM)}{_cpad(name[:48], 50)}"
              f"{_c('card ' + r['task_id'][:6], C_DIM) if r.get('task_id') else ''}")
        if r.get("status") == "blocked" and r.get("note"):
            print(f"             {_c('asking: ' + r['note'][:80], C_DIM)}")
    props = v.get("proposals") or []
    print()
    print(f"  {_c('SUGGESTIONS', C_BOLD)}  {len(props)} ready · {len(v.get('open') or [])} card(s) may run")
    if not props:
        print(_c("    nothing to propose: no idle pane, or no card the gate would pass", C_DIM))
    for p in props:
        print(f"    {_c(p['id'][:6], C_CYA)}  {p['task_id'][:6]} -> {_c(p['agent'], C_BOLD)}"
              f"   conf {p['confidence']:.2f}   {p['task_title'][:52]}")
        print(f"            {_c(p['rationale'][:100], C_DIM)}")
    if props:
        print()
        print(_c("  otto dispatch approve <id>   otto dispatch dismiss <id>   "
                 "otto dispatch to <task> <agent>", C_DIM))
    print()
    return 0


def _cmd_herdr_worktree(args, client: Client) -> int:
    """`otto herdr worktree list|create|open`. A worktree is a workspace with git
    provenance, so create and open end with claude in the pane like `herdr open`
    does, unless --no-claude leaves the shell for the owner."""
    from .. import herdr as _herdr

    sub = getattr(args, "worktree_cmd", None) or "list"
    if sub == "list":
        cwd = str(pathlib.Path(args.path or ".").resolve())
        try:
            try:
                r = client.herdr_worktrees(cwd)
            except DaemonDown:
                r = _herdr.worktree_list(cwd)
        except (RuntimeError, _herdr.HerdrError) as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(r, indent=2))
            return 0
        src = r.get("source") or {}
        print()
        print(f"  repo    {src.get('repo_root') or cwd}"
              + (_c(f"  (workspace {src['source_workspace_id']})", C_DIM)
                 if src.get("source_workspace_id") else ""))
        for w in r.get("worktrees") or []:
            state = "open" if w.get("open_workspace_id") else "closed"
            print(f"  {_cpad(state, 8, C_GRN if state == 'open' else C_DIM)}"
                  f"{_cpad(w.get('branch') or '(detached)', 28)}"
                  f"{_cpad(w.get('path') or '', 48)}"
                  f"{_c(w.get('open_workspace_id') or '', C_DIM)}")
        if not (r.get("worktrees") or []):
            print(_c("  no worktrees", C_DIM))
        print()
        return 0

    repo = str(pathlib.Path(args.repo).resolve())
    start = not getattr(args, "no_claude", False)
    try:
        if sub == "create":
            r = client.herdr_worktree_create(repo, args.branch, base=args.base, label=args.label,
                                             name=args.name, start_claude=start)
        else:
            path = str(pathlib.Path(args.path).resolve()) if args.path else None
            r = client.herdr_worktree_open(repo, branch=args.branch, path=path, label=args.label,
                                           name=args.name, start_claude=start)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    a = r.get("agent") or {}
    what = args.branch or getattr(args, "path", None)
    print(_c(f"  worktree {what} {'created' if sub == 'create' else 'open'} as "
             f"{r.get('workspace_id')} (pane {r.get('pane_id')})"
             + (f", claude up as {a.get('name') or a.get('pane_id')}" if a else ""), C_GRN))
    print(_c("  see it: otto herdr attach   (or `herdr` in any terminal)", C_DIM))
    return 0


DESKTOP_EXE = pathlib.Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / \
    "target" / "release" / "otto-desktop.exe"


def cmd_app(args, client: Client) -> int:
    """Open Otto's window.

    The real app is the Tauri shell in desktop/ (logo, tray status light, otto://
    links, starts the daemon if it is down and never stops it). It autostarts
    hidden with Windows, so "I closed it" means it was quit from the tray and this
    is how it comes back (2026-10-01). A browser in app mode is only the fallback
    for a machine where the shell was never built.
    """
    import shutil as _shutil

    url = f"{config.BASE_URL}/#{args.view}"
    if DESKTOP_EXE.is_file():
        subprocess.Popen([str(DESKTOP_EXE)], cwd=str(DESKTOP_EXE.parents[3]),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
        print("  opened the Otto desktop app (it lives in the tray when closed; "
              "quit from the tray stops only the window)")
        return 0
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        _shutil.which("chrome"), _shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    exe = next((c for c in candidates if c and pathlib.Path(c).is_file()), None)
    if exe is None:
        import webbrowser
        webbrowser.open(url)
        print(f"  no Chrome or Edge found; opened {url} in the default browser")
        return 0
    subprocess.Popen([exe, f"--app={url}", "--window-size=1600,1000"],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
    print(f"  opened {url} as an app window")
    return 0


def cmd_herdr(args, client: Client) -> int:
    """The harness. `up` and `attach` are local; the rest go through the daemon so
    the session rows are synced in the same call."""
    from .. import herdr as _herdr

    action = getattr(args, "herdr_cmd", None) or "status"

    if action == "up":
        started, msg = _herdr.ensure_server()
        print(f"  {msg}")
        return 0

    if action == "attach":
        exe = _herdr.binary()
        if not exe:
            print(_c("  herdr is not installed", C_RED), file=sys.stderr)
            return 1
        # Hand this terminal to the herdr TUI. Returns when the owner detaches (ctrl+b q).
        return subprocess.call([exe])

    if action == "open" and getattr(args, "all_repos", False):
        # One workspace per repo Otto knows, skipping what herdr already has open.
        # The repo list comes from the daemon (repos.collect: what the registry,
        # tasks and runs actually touched), the open set from the live snapshot.
        repos = client.repos().get("repos") or []
        to_open, skipped = _herdr.plan_open_repos(repos, _herdr.snapshot())
        for r, why in skipped:
            print(_c(f"  skip  {r.get('key') or r.get('path')}: {why}", C_DIM))
        if not to_open:
            print("  nothing to open: every known repo is already in herdr")
            return 0
        failed = 0
        for r in to_open:
            print(f"  open  {r['path']} as {r['name']} ...", end="", flush=True)
            try:
                a = client.herdr_open(r["path"], name=r["name"]).get("agent") or {}
            except RuntimeError as e:
                failed += 1
                print(_c(f" failed: {e}", C_RED))
                continue
            print(_c(f" claude up in {a.get('pane_id')}", C_GRN))
        print(_c(f"  opened {len(to_open) - failed} of {len(to_open)}, "
                 f"skipped {len(skipped)}", C_DIM))
        return 1 if failed else 0

    if action == "open":
        if not args.path:
            print("  give a path, or --all-repos", file=sys.stderr)
            return 2
        cwd = str(pathlib.Path(args.path).resolve())
        try:
            r = client.herdr_open(cwd, label=args.label, name=args.name, resume=args.resume)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        a = r.get("agent") or {}
        print(_c(f"  claude up in {a.get('pane_id')} ({cwd})"
                 + (f" as {a.get('name')}" if a.get("name") else ""), C_GRN))
        print(_c("  see it: otto herdr attach   (or `herdr` in any terminal)", C_DIM))
        return 0

    if action == "adopt" and getattr(args, "all", False):
        # Every ended session whose directory still exists, newest first, capped,
        # back into a pane each. Ones herdr already holds are skipped by id.
        rows = client.sessions(live=False)["sessions"]
        chosen, skipped = _herdr.plan_adopt(rows, _herdr.snapshot(), limit=args.limit)
        for s, why in skipped:
            if why != "already in herdr" and not why.startswith("over the --limit"):
                print(_c(f"  skip  {s['session_id'][:8]}: {why}", C_DIM))
        held = sum(1 for _s, why in skipped if why == "already in herdr")
        over = sum(1 for _s, why in skipped if why.startswith("over the --limit"))
        if not chosen:
            print(f"  nothing to adopt ({held} already in herdr)")
            return 0
        failed = 0
        for s in chosen:
            title = (s.get("title") or s["session_id"][:8])[:50]
            print(f"  adopt {s['session_id'][:8]} '{title}' in {s['cwd']} ...", end="", flush=True)
            try:
                a = client.herdr_open(s["cwd"], resume=s["session_id"]).get("agent") or {}
            except RuntimeError as e:
                failed += 1
                print(_c(f" failed: {e}", C_RED))
                continue
            print(_c(f" resumed in {a.get('pane_id')}", C_GRN))
        print(_c(f"  adopted {len(chosen) - failed} of {len(chosen)}; {held} already in herdr"
                 + (f", {over} more beyond --limit {args.limit}" if over else ""), C_DIM))
        return 1 if failed else 0

    if action == "adopt":
        if not args.session_id:
            print("  give a session id, or --all", file=sys.stderr)
            return 2
        # Pick an ended session back up inside a pane: its cwd, `claude --resume`.
        s = next((x for x in client.sessions(live=False)["sessions"]
                  if x["session_id"].startswith(args.session_id)), None)
        if s is None:
            print(f"  no session matching {args.session_id}", file=sys.stderr)
            return 2
        if s["state"] != "offline":
            print(_c(f"  {s['session_id'][:8]} is still live in {s.get('host') or 'a terminal'}; "
                     "close that tab first, or the conversation would be open twice", C_YEL),
                  file=sys.stderr)
            return 1
        try:
            r = client.herdr_open(s["cwd"], label=args.label, name=args.name,
                                  resume=s["session_id"])
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        a = r.get("agent") or {}
        print(_c(f"  resumed '{(s.get('title') or s['session_id'][:8])[:50]}' in "
                 f"{a.get('pane_id')}", C_GRN))
        return 0

    if action == "worktree":
        return _cmd_herdr_worktree(args, client)

    if action == "focus":
        try:
            client.herdr_focus(args.target)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
        print(f"  focused {args.target}")
        return 0

    # status
    try:
        h = client.herdr()
    except DaemonDown:
        h = {"installed": _herdr.available(), "binary": _herdr.binary(),
             "running": _herdr.server_running(), "agents": _herdr.agents(),
             "workspaces": _herdr.workspaces()}
    if args.json:
        print(json.dumps(h, indent=2))
        return 0
    print()
    print(f"  herdr   {_c('installed', C_GRN) if h['installed'] else _c('not installed', C_RED)}"
          f"  {_c(h.get('binary') or '', C_DIM)}")
    print(f"  server  {_c('running', C_GRN) if h['running'] else _c('not running  (otto herdr up)', C_YEL)}")
    for a in h.get("agents") or []:
        sid = ((a.get("agent_session") or {}).get("value") or "")[:8]
        print(f"  {_cpad(a.get('agent_status') or '?', 9, _HERDR_COLOR.get(a.get('agent_status'), ''))}"
              f"{_cpad(a.get('name') or a.get('pane_id'), 12, C_DIM)}"
              f"{_cpad((a.get('cwd') or ''), 40)}{_c(a.get('agent', '') + ' ' + sid, C_DIM)}")
    if not (h.get("agents") or []):
        print(_c("  no agents in panes. Open one: otto herdr open <repo path>", C_DIM))
    print()
    return 0


def add_sessions(sub) -> None:
    ses = sub.add_parser("sessions", help="Claude Code sessions, live, from their hooks")
    ses.add_argument("--all", action="store_true", help="include offline sessions")
    ses.add_argument("--domain", choices=["work", "personal"])
    ses.add_argument("--json", action="store_true")
    ses.set_defaults(fn=cmd_sessions, action=None)
    ses_sub = ses.add_subparsers(dest="action")

    a = ses_sub.add_parser("install", help="write the session hooks into ~/.claude")
    a.set_defaults(fn=cmd_sessions, action="install", json=False, all=False, domain=None)

    a = ses_sub.add_parser("uninstall", help="remove Otto's session hooks")
    a.set_defaults(fn=cmd_sessions, action="uninstall", json=False, all=False, domain=None)

    a = ses_sub.add_parser("doctor", help="why the hooks might not be reporting")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_sessions, action="doctor", all=False, domain=None)

    a = ses_sub.add_parser("name", help="give a session a title (outranks the one from its transcript)")
    a.add_argument("session_id", help="id or unique prefix")
    a.add_argument("title", nargs="+")
    a.set_defaults(fn=cmd_sessions, action="name", json=False, all=False, domain=None)

    a = ses_sub.add_parser("open", help="focus a live session's window, or resume an ended one in a new tab")
    a.add_argument("session_id", help="id or unique prefix")
    a.set_defaults(fn=cmd_sessions, action="open", json=False, all=False, domain=None)

    a = ses_sub.add_parser("watch", help="the session list, redrawn every few seconds; a rail for a spare tab")
    a.add_argument("--all", action="store_true", help="include offline sessions")
    a.add_argument("--domain", choices=["work", "personal"])
    a.add_argument("--every", type=float, default=3.0, help="seconds between redraws")
    a.set_defaults(fn=cmd_sessions, action="watch", json=False)


def add_dispatch(sub) -> None:
    dsp = sub.add_parser("dispatch", help="logistics: match runnable cards to idle sessions in herdr")
    dsp.add_argument("--json", action="store_true")
    dsp.add_argument("--refresh", action="store_true", help="recompute the suggestions now")
    dsp.set_defaults(fn=cmd_dispatch, dispatch_cmd=None)
    dsp_sub = dsp.add_subparsers(dest="dispatch_cmd")
    a = dsp_sub.add_parser("approve", help="hand the suggested card to that session")
    a.add_argument("proposal_id")
    a.set_defaults(fn=cmd_dispatch, json=False)
    a = dsp_sub.add_parser("dismiss", help="not that pairing; remembered while the session lives")
    a.add_argument("proposal_id")
    a.set_defaults(fn=cmd_dispatch, json=False)
    a = dsp_sub.add_parser("to", help="hand a card to a named agent or pane (same gate as the strip)")
    a.add_argument("task_id")
    a.add_argument("target", help="herdr agent name or pane id, e.g. otto or w1:p1")
    a.set_defaults(fn=cmd_dispatch, json=False)


def add_app(sub) -> None:
    a = sub.add_parser("app", help="open the dashboard as its own window (Chrome or Edge app mode)")
    a.add_argument("view", nargs="?", default="dispatch",
                   choices=["today", "board", "dispatch", "terminal", "grid", "history",
                            "writing", "plane"])
    a.set_defaults(fn=cmd_app)


def add_herdr(sub) -> None:
    hd = sub.add_parser("herdr", help="the harness the sessions live in (herdr.dev)")
    hd.add_argument("--json", action="store_true")
    hd.set_defaults(fn=cmd_herdr, herdr_cmd=None)
    hd_sub = hd.add_subparsers(dest="herdr_cmd")
    a = hd_sub.add_parser("up", help="start the herdr server if it is not running")
    a.set_defaults(fn=cmd_herdr, json=False)
    a = hd_sub.add_parser("attach", help="hand this terminal to the herdr TUI (ctrl+b q detaches)")
    a.set_defaults(fn=cmd_herdr, json=False)
    a = hd_sub.add_parser("open", help="a new workspace at a path with claude running in it")
    a.add_argument("path", nargs="?", help="repo directory (or use --all-repos)")
    a.add_argument("--all-repos", action="store_true", dest="all_repos",
                   help="one workspace per repo Otto knows that is not already open in herdr")
    a.add_argument("--label", help="workspace label (default: the directory name)")
    a.add_argument("--name", help="agent name, e.g. backend ([a-z][a-z0-9_-]*)")
    a.add_argument("--resume", help="a Claude session id to resume in the pane")
    a.set_defaults(fn=cmd_herdr, json=False)
    a = hd_sub.add_parser("adopt", help="resume an ENDED session inside a herdr pane, in its directory")
    a.add_argument("session_id", nargs="?", help="id or unique prefix (otto sessions --all)")
    a.add_argument("--all", action="store_true",
                   help="every offline session whose directory still exists, newest first")
    a.add_argument("--limit", type=int, default=10, help="cap for --all (default 10)")
    a.add_argument("--label")
    a.add_argument("--name")
    a.set_defaults(fn=cmd_herdr, json=False)
    wt = hd_sub.add_parser("worktree", help="git worktrees as herdr workspaces")
    wt.add_argument("--json", action="store_true")
    wt.set_defaults(fn=cmd_herdr, json=False, worktree_cmd=None, path=None)
    wt_sub = wt.add_subparsers(dest="worktree_cmd")
    a = wt_sub.add_parser("list", help="the repo's worktrees and which are open in herdr")
    a.add_argument("path", nargs="?", help="a path inside the repo (default: here)")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_herdr)
    a = wt_sub.add_parser("create", help="git worktree add + open as a workspace, claude in the pane")
    a.add_argument("repo", help="a path inside the repo")
    a.add_argument("branch", help="existing local branch to check out, or a new one")
    a.add_argument("--base", help="ref a NEW branch starts from (default HEAD)")
    a.add_argument("--label", help="workspace label")
    a.add_argument("--name", help="agent name once claude is up")
    a.add_argument("--no-claude", action="store_true", dest="no_claude",
                   help="open the workspace but leave the pane as a shell")
    a.set_defaults(fn=cmd_herdr, json=False, path=None)
    a = wt_sub.add_parser("open", help="open an existing worktree as a workspace, claude in the pane")
    a.add_argument("repo", help="a path inside the repo")
    a.add_argument("--branch", help="the worktree's branch")
    a.add_argument("--path", help="the worktree's checkout path (instead of --branch)")
    a.add_argument("--label", help="workspace label")
    a.add_argument("--name", help="agent name once claude is up")
    a.add_argument("--no-claude", action="store_true", dest="no_claude",
                   help="open the workspace but leave the pane as a shell")
    a.set_defaults(fn=cmd_herdr, json=False)
    a = hd_sub.add_parser("focus", help="focus an agent's pane and bring the herdr window forward")
    a.add_argument("target", help="agent name or pane id")
    a.set_defaults(fn=cmd_herdr, json=False)


PARSERS = {
    "sessions": add_sessions,
    "dispatch": add_dispatch,
    "app": add_app,
    "herdr": add_herdr,
}
