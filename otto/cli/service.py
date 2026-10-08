"""The daemon itself: serve, ensure, stop, restart, setup, doctor, probe, config."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

from .. import config, persona, registry
from ..client import Client, DaemonDown
from ._fmt import C_BOLD, C_DIM, C_GRN, C_RED, C_YEL, _c, _cpad, _integration_mark


def cmd_probe(args, client: Client) -> int:
    items = client.probe()
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    bad = 0
    for i in items:
        mark, color = _integration_mark(i)
        if mark == "DOWN":
            bad += 1
        print(f"  {_cpad(mark, 6, color)} {i['name']:<14} {i['detail']}")
    return 1 if bad else 0


def cmd_config(args, client: Client) -> int:
    """Inspect and reconcile the config Otto owns.

    Runs locally: it reads the filesystem, so it works with the daemon down.
    """
    from .. import configsync

    if args.config_cmd == "deploy":
        done = configsync.deploy(args.dry_run)
        if not done:
            print("  nothing to deploy, repo and ~/.claude agree")
        for d in done:
            print(f"  {'would deploy' if args.dry_run else 'deployed'}  {d}")
        return 0

    if args.config_cmd == "adopt":
        done = configsync.adopt(args.dry_run)
        if not done:
            print("  nothing to adopt, repo and ~/.claude agree")
        for d in done:
            print(f"  {'would adopt' if args.dry_run else 'adopted'}  {d}")
        return 0

    if args.config_cmd == "snapshot":
        done = configsync.snapshot(args.dry_run)
        for d in done:
            print(f"  {'would snapshot' if args.dry_run else 'snapshotted'}  {d}")
        return 0

    if args.config_cmd == "backup":
        dest, n = configsync.backup()
        print(f"  {n} files -> {dest}")
        return 0

    # default: status
    summ = configsync.summary()
    if args.json:
        print(json.dumps(summ, indent=2))
        return 0

    print(_c("  CANONICAL", C_BOLD) + f"  {summ['repo']}")
    print()
    print(_c("  JUNCTIONS", C_BOLD) + _c("   ~/.claude -> repo, single source of truth", C_DIM))
    for r in summ["junctions"]:
        mark = _cpad("ok", 6, C_GRN) if r["ok"] else _cpad("BROKEN", 6, C_RED)
        print(f"    {mark} {r['name']:<14} {r.get('files', 0):>4} files  "
              f"{_c(r['state'], C_DIM)}")
        if not r["ok"]:
            print(f"           {_c('expected -> ' + r['target'], C_DIM)}")

    print()
    print(_c("  COPIED FILES", C_BOLD) + _c("   a junction cannot target a single file", C_DIM))
    for r in summ["deploy"]:
        if r["state"] == "in sync":
            mark = _cpad("sync", 6, C_GRN)
        elif r["state"] == "DRIFT":
            mark = _cpad("DRIFT", 6, C_YEL)
        else:
            mark = _cpad("--", 6, C_DIM)
        extra = f" (newer: {r['newer']})" if r["state"] == "DRIFT" else ""
        print(f"    {mark} {r['name']:<20} {_c(r['state'] + extra, C_DIM)}")
    if any(r["state"] == "DRIFT" for r in summ["deploy"]):
        print(_c("    -> otto config deploy   (repo wins)", C_DIM))
        print(_c("    -> otto config adopt    (~/.claude wins)", C_DIM))

    print()
    print(_c("  SNAPSHOTS", C_BOLD) + _c("   backup only, never auto-restored", C_DIM))
    for r in summ["snapshots"]:
        mark = _cpad("ok", 6, C_GRN) if r["state"] == "current" else _cpad(r["state"][:6], 6, C_YEL)
        print(f"    {mark} {r['name']:<20} {_c(r['state'], C_DIM)}")

    print()
    print(_c(f"  -> {'CONSOLIDATED' if summ['ok'] else 'ATTENTION NEEDED'}",
             C_GRN if summ["ok"] else C_YEL))
    return 0 if summ["ok"] else 1


def cmd_ensure(args, client: Client) -> int:
    """Start the daemon if it is not already running. Safe on a tight interval.

    This is what the Windows keepalive task calls. It must be a cheap no-op when
    things are healthy, because a logon trigger alone leaves a daemon that died at
    02:00 dead until the next logon, and every armed schedule silently misses.
    """
    wait_pid = getattr(args, "wait_pid", None)
    if wait_pid:
        # Spawned by /api/daemon/restart: the old daemon is about to exit and still
        # answers for a moment. Starting now would collide on the port, so wait for
        # that pid to be gone, then proceed as the keepalive would.
        import psutil
        for _ in range(120):
            if not psutil.pid_exists(wait_pid):
                break
            time.sleep(0.25)
        else:
            print(_c(f"  pid {wait_pid} is still alive after 30s; not starting a second daemon",
                     C_YEL), file=sys.stderr)
            return 1

    if client.alive():
        if not args.quiet:
            print("  daemon already running")
        return 0

    from ..daemon import _running_daemon_pid
    stale = _running_daemon_pid()
    if stale:
        # A pidfile pointing at a live python that is not answering HTTP: wedged.
        # Say so rather than starting a second one that will fight for the port.
        print(_c(f"  pid {stale} is alive but not answering on {config.BASE_URL}", C_YEL),
              file=sys.stderr)
        print("  stop it with: otto stop", file=sys.stderr)
        return 1

    py = sys.executable
    creation = 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    log = config.LOG_DIR / "daemon.out.log"
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Rotate on start, keeping one generation. The log is uvicorn's stdout and
    # every traceback the daemon prints; left alone it grows without bound.
    try:
        if log.exists() and log.stat().st_size > 20 * 1024 * 1024:
            prev = log.with_name("daemon.out.1.log")
            prev.unlink(missing_ok=True)
            log.rename(prev)
    except OSError:
        pass  # a rotation that cannot happen is not a reason to keep the daemon down
    fh = log.open("a", encoding="utf-8", errors="replace")
    # The daemon outlives this shell and everything it spawns inherits from it.
    # Started from a Claude Code tool shell it would carry NO_COLOR and
    # CLAUDECODE into every run and every herdr pane; herdr.clean_env strips them.
    from .. import herdr as _herdr
    try:
        subprocess.Popen(
            [py, "-m", "otto", "serve"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=creation, shell=False, env=_herdr.clean_env(),
        )
    except OSError as e:
        print(_c(f"  could not start the daemon: {e}", C_RED), file=sys.stderr)
        return 1
    finally:
        fh.close()

    for _ in range(20):
        time.sleep(1)
        if client.alive():
            print(f"  started the daemon ({config.BASE_URL})")
            return 0
    print(_c("  started it but it did not answer within 20s; see "
             f"{log}", C_YEL), file=sys.stderr)
    return 1


def cmd_stop(args, client: Client) -> int:
    """Stop the daemon without killing the agents it spawned."""
    from ..daemon import stop_daemon

    alive = []
    try:
        alive = [r for r in client.live()]
    except (DaemonDown, RuntimeError):
        pass

    ok, msg = stop_daemon()
    print(f"  {msg}")
    if alive:
        print(_c(f"  {len(alive)} agent(s) still running and untouched:", C_DIM))
        for r in alive:
            print(f"    {persona.short(r['id'])} {r['name']}  (pid {r['pid']})")
        print(_c("  start the daemon again and it will re-adopt them", C_DIM))
    return 0 if ok else 1


def cmd_restart(args, client: Client) -> int:
    """Stop, then ensure. The way to apply a changed otto.env from a terminal."""
    if client.alive():
        rc = cmd_stop(args, client)
        if rc:
            return rc
        for _ in range(40):
            if not client.alive():
                break
            time.sleep(0.25)
    args.quiet = False
    args.wait_pid = None
    return cmd_ensure(args, client)


_SETUP_PILL = {"done": ("done", C_GRN), "todo": ("to do", C_YEL), "skipped": ("skipped", C_DIM),
               "restart": ("restart", C_YEL)}


def _print_setup(view: dict) -> None:
    prog = view["progress"]
    head = (f"complete since {view['completed_at'][:10]}" if view.get("complete")
            else f"{prog['done']} of {prog['total']} done")
    print()
    print(_c("  SETUP", C_BOLD) + f"   {head}"
          + (_c("   restart needed: otto restart", C_YEL) if view.get("restart_needed") else ""))
    print(_c(f"  settings file {view['settings']['path']}"
             + ("" if view["settings"]["exists"] else " (not written yet)"), C_DIM))
    print()
    for st in view["steps"]:
        label, color = _SETUP_PILL.get(st["status"], (st["status"], ""))
        req = "" if st["required"] else _c(" optional", C_DIM)
        print(f"    {_cpad(label, 8, color)} {st['title']:<26}{req}")
        print(_c(f"             {st['summary']}", C_DIM))
    print()


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        raw = input(f"  {prompt}{hint}: ").strip()
    except EOFError:
        return default
    return raw or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    raw = _ask(prompt + (" (Y/n)" if default else " (y/N)"), "")
    if not raw:
        return default
    return raw.lower().startswith("y")


def _setup_offline(args) -> int:
    """The daemon is down. Two things still work: writing otto.env and installing
    the hooks. Both are local file writes, and both are what a fresh machine does
    first anyway."""
    from .. import sessions as _sessions
    from .. import settings as _settings
    from .. import setup as _setup

    print(_c(f"  the daemon is not running ({config.BASE_URL}); start it with otto ensure", C_YEL))
    print(_c("  with it down, setup can still write the settings file and install the hooks", C_DIM))
    if args.status or args.reset:
        return 1
    if _ask_yes("Write identity settings now?", True):
        vals = {"OTTO_OWNER_NAME": _ask("Your name"), "OTTO_ORG_NAME": _ask("Organization"),
                "OTTO_WORK_ROOTS": _ask(f"Work roots ({os.pathsep!r}-separated)"),
                "OTTO_PERSONAL_ROOTS": _ask(f"Personal roots ({os.pathsep!r}-separated)")}
        try:
            clean = _setup.validate_identity({k: v for k, v in vals.items() if v})
            written, _removed = _settings.write(clean, config.SETTINGS_PATH)
            print(f"  wrote {', '.join(written) or 'nothing'} to {config.SETTINGS_PATH}")
        except (ValueError, OSError) as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 1
    if _ask_yes("Install the Claude Code hooks?", True):
        _changed, msg = _sessions.install()
        print(f"  {msg}")
    print("  start the daemon (otto ensure) and run otto setup again for the rest")
    return 0


def _setup_step(client: Client, st: dict) -> None:
    """Prompt for one step and act on the answer. Each kind maps onto the same
    endpoint the dashboard uses, so the two faces cannot drift."""
    kind = st["action"]["kind"]
    if kind == "form" and st["id"] == "identity":
        vals = {}
        for f in st["action"]["fields"]:
            cur = f["value"].replace("\n", os.pathsep) if f["kind"] == "paths" else f["value"]
            label = f["label"] + (f" ({os.pathsep!r}-separated)" if f["kind"] == "paths" else "")
            v = _ask(label, cur)
            if v:
                vals[f["name"]] = v
        if vals:
            out = client.setup_post("/api/setup/settings", {"values": vals})
            print(f"  wrote {', '.join(out['written']) or 'nothing'}")
        return
    if kind == "form" and st["id"] == "first_card":
        title = _ask("Title")
        if title:
            client.setup_post("/api/setup/first-card", {"title": title, "detail": _ask("Detail") or None})
            print("  card added")
        return
    if kind == "button":
        if _ask_yes(st["action"]["label"] + "?", True):
            out = client.setup_post(st["data"]["endpoint"])
            print(f"  {out.get('message') or 'done'}")
        return
    if kind == "choice":
        picks = []
        for o in st["action"]["options"]:
            if _ask_yes(f"{o['label']}: {o['hint'][:60]}", bool(o.get("checked"))):
                picks.append(o["value"])
        if st["data"].get("setting"):
            client.setup_post("/api/setup/settings",
                              {"values": {st["data"]["setting"]: ",".join(picks) or "none"}})
        else:
            client.setup_post(st["data"]["endpoint"], {"arm": picks})
        print(f"  saved: {', '.join(picks) or 'none'}")
        return
    # kind == "none": nothing to do here but record the choice
    if _ask_yes("Skip this step?", True):
        client.setup_post("/api/setup/skip", {"step": st["id"]})


def cmd_setup(args, client: Client) -> int:
    """The terminal face of otto/setup.py: same steps, same statuses, same file.

    Needs the daemon for most of it (statuses come from live state and the store
    is daemon-written). The two things a fresh machine does first, writing
    otto.env and installing the hooks, work with the daemon down.
    """
    if not client.alive():
        return _setup_offline(args)

    if args.reset:
        view = client.setup_post("/api/setup/reset")
        print("  setup reset; the dashboard opens on Setup again")
        _print_setup(view)
        return 0

    view = client.setup_view()
    if args.json:
        print(json.dumps(view, indent=2))
        return 0 if view["complete"] else 1
    _print_setup(view)
    if args.status:
        return 0 if view["complete"] else 1
    if view["complete"]:
        print(_c("  already complete; otto setup --reset to run it again", C_DIM))
        return 0

    for st in view["steps"]:
        if st["status"] in ("done", "skipped", "restart") or st["id"] in ("daemon", "finish"):
            continue
        print(_c(f"  {st['title']}", C_BOLD))
        print(textwrap.fill(st["detail"], 78, initial_indent="  ", subsequent_indent="  "))
        try:
            _setup_step(client, st)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
        print()

    view = client.setup_view()
    _print_setup(view)
    if view["restart_needed"] and _ask_yes("Restart the daemon now to apply the settings?", True):
        cmd_restart(args, client)
        view = client.setup_view()
    if not view["complete"] and _ask_yes("Mark setup complete?", True):
        client.setup_post("/api/setup/complete")
        print("  setup complete")
    return 0


def cmd_serve(args, client: Client) -> int:
    from ..daemon import serve
    host = args.host or config.HOST
    port = args.port or config.PORT
    print(f"  {config.PERSONA_NAME} serving on http://{host}:{port}")
    serve(args.host, args.port, args.force)
    return 0


def cmd_doctor(args, client: Client) -> int:
    from ..daemon import _running_daemon_pid
    ok = True
    print(_c(f"  {config.PERSONA_NAME} doctor", C_BOLD))
    # Which half of Otto this is. A clamped value is the one misconfiguration that
    # keeps the daemon up and quietly runs less, so it is the first line here.
    print(f"    scope            {config.SCOPE}"
          + (_c(f"  {config.SCOPE_WARNING}", C_YEL) if config.SCOPE_WARNING else ""))
    pid = _running_daemon_pid()
    print(f"    daemon pidfile   {'pid ' + str(pid) if pid else _c('not running', C_YEL)}")
    reachable = client.alive()
    print(f"    daemon http      "
          f"{_c('reachable', C_GRN) if reachable else _c('unreachable', C_RED)}")
    ok = ok and reachable
    for label, path in (("state dir", config.STATE_DIR), ("log dir", config.LOG_DIR)):
        exists = path.is_dir()
        print(f"    {label:<16} {path} {'' if exists else _c('MISSING', C_YEL)}")
    corrupt = list(config.STATE_DIR.glob("*.corrupt")) if config.STATE_DIR.is_dir() else []
    if corrupt:
        ok = False
        print(_c(f"    corrupt state    {len(corrupt)} quarantined file(s):", C_RED))
        for c in corrupt:
            print(f"                     {c.name}")
    roots = [r for r in config.REPO_ROOTS if r.is_dir()]
    print(f"    repo roots       {len(roots)}/{len(config.REPO_ROOTS)} present")
    for r in config.REPO_ROOTS:
        if not r.is_dir():
            print(_c(f"                     missing: {r}", C_DIM))
    found = registry.discover()
    print(f"    discoverable     {registry.summarize(found)}")
    from .. import configsync
    csumm = configsync.summary()
    bad_j = [r["name"] for r in csumm["junctions"] if not r["ok"]]
    drift = [r["name"] for r in csumm["deploy"] if r["state"] == "DRIFT"]
    if bad_j:
        ok = False
        print(_c(f"    config           BROKEN junction(s): {', '.join(bad_j)}", C_RED))
    elif drift:
        print(_c(f"    config           drift in {', '.join(drift)} "
                 f"(otto config status)", C_YEL))
    else:
        print(f"    config           consolidated, {csumm['managed_files']} files owned")
    print(f"\n  -> {_c('HEALTHY', C_GRN) if ok else _c('ATTENTION NEEDED', C_YEL)}")
    return 0 if ok else 1


def add_probe(sub) -> None:
    s = sub.add_parser("probe", help="check integration liveness")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_probe)


def add_config(sub) -> None:
    cf = sub.add_parser("config", help="the ~/.claude files Otto owns")
    cf_sub = cf.add_subparsers(dest="config_cmd")
    cf.set_defaults(fn=cmd_config, config_cmd="status", json=False, dry_run=False)

    a = cf_sub.add_parser("status", help="junction map and drift")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_config, dry_run=False)

    a = cf_sub.add_parser("deploy", help="push repo copies to ~/.claude (repo wins)")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_config, json=False)

    a = cf_sub.add_parser("adopt", help="pull ~/.claude edits into the repo (live wins)")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_config, json=False)

    a = cf_sub.add_parser("snapshot", help="refresh the settings.json backups")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_config, json=False)

    a = cf_sub.add_parser("backup", help="copy every authored file to a timestamped dir")
    a.set_defaults(fn=cmd_config, json=False, dry_run=False)


def add_serve(sub) -> None:
    s = sub.add_parser("serve", help="run the daemon + dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--force", action="store_true", help="start even if a pidfile exists")
    s.set_defaults(fn=cmd_serve)


def add_ensure(sub) -> None:
    s = sub.add_parser("ensure", help="start the daemon if it is not running")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--wait-pid", type=int, metavar="PID",
                   help="wait for this pid to exit first (used by the dashboard restart)")
    s.set_defaults(fn=cmd_ensure)


def add_restart(sub) -> None:
    s = sub.add_parser("restart", help="stop the daemon and start it again (applies otto.env)")
    s.set_defaults(fn=cmd_restart)


def add_setup(sub) -> None:
    s = sub.add_parser("setup", help="first-run setup: see what is configured and fill in the rest")
    s.add_argument("--status", action="store_true", help="print the steps and exit; 0 when complete")
    s.add_argument("--reset", action="store_true", help="clear completion so the Setup view opens again")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_setup)


def add_stop(sub) -> None:
    s = sub.add_parser("stop", help="stop the daemon, leaving agents running")
    s.set_defaults(fn=cmd_stop)


def add_doctor(sub) -> None:
    s = sub.add_parser("doctor", help="diagnose the harness itself")
    s.set_defaults(fn=cmd_doctor)


PARSERS = {
    "probe": add_probe,
    "config": add_config,
    "serve": add_serve,
    "ensure": add_ensure,
    "restart": add_restart,
    "setup": add_setup,
    "stop": add_stop,
    "doctor": add_doctor,
}
