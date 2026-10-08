"""Tracked runs: history, logs, spawn, kill, done, live, watch, prune."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .. import config, persona, verdict
from ..models import Run
from ..client import Client
from ._fmt import C_BOLD, C_CYA, C_DIM, C_GRN, C_RED, _STATUS_COLOR, _WORK_COLOR, _age, _c, _cpad


def cmd_runs(args, client: Client) -> int:
    runs = [Run.model_validate(r) for r in client.runs(args.limit)]
    if args.json:
        print(json.dumps([r.model_dump() for r in runs], indent=2))
        return 0
    if not runs:
        print("  no runs recorded")
        return 0
    # OTTO and WORK are the two lanes from verdict.py: whether Otto's side held
    # (spawned, ran, reported) and whether the work itself succeeded. A single
    # STATUS column cannot say which side a red row is about, and that ambiguity
    # has cost money (the 2026-08-05 529 burst, actioned three times as breakage).
    print(f"  {'ID':<8}{'NAME':<24}{'RUNNER':<11}{'STATUS':<10}{'OTTO':<11}{'WORK':<15}"
          f"{'STARTED':>11}  USAGE")
    for r in runs:
        v = verdict.verdict(r)
        status = _cpad(r.status, 10, _STATUS_COLOR.get(r.status, ""))
        otto_lane = _cpad(v["harness"]["label"], 11,
                          C_GRN if v["harness"]["state"] in ("ok", "running") else C_RED)
        work_lane = _cpad(v["work"]["label"], 15, _WORK_COLOR.get(v["work"]["state"], ""))
        print(f"  {persona.short(r.id):<8}{r.name[:22]:<24}{r.runner:<11}"
              f"{status}{otto_lane}{work_lane}{_age(r.started):>11}  {persona.usage(r)}")
    return 0


def cmd_logs(args, client: Client) -> int:
    print(client.log(args.run_id, args.lines))
    return 0


def cmd_spawn(args, client: Client) -> int:
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("  refusing to spawn with an empty prompt", file=sys.stderr)
        return 2
    run = client.spawn(
        name=args.name, prompt=prompt, cwd=args.cwd or str(Path.cwd()),
        agent=args.agent, mode=args.mode, task_id=args.task,
        tier=args.tier, permissions=args.permissions, domain=args.domain,
        model=args.model,
    )
    print(f"  spawned {run['name']}  run {persona.short(run['id'])}  pid {run['pid']}  "
          f"[{run.get('permissions', 'yolo')}]")
    print(f"  log: {run['log']}")
    print(f"  follow: otto logs {persona.short(run['id'])}")
    return 0


def cmd_kill(args, client: Client) -> int:
    run = client.kill(args.run_id)
    print(f"  {run['name']} -> {run['status']}")
    return 0


def cmd_done(args, client: Client) -> int:
    run = client.done(args.run_id, args.status, args.exit_code, args.notes)
    print(f"  {run['name']} -> {run['status']}")
    return 0


def cmd_live(args, client: Client) -> int:
    """Every agent running right now, and what each is doing."""
    rows = client.live()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  nothing running")
        return 0
    for r in rows:
        print()
        print(f"  {_c(persona.short(r['id']), C_CYA)}  {_c(r['name'], C_BOLD)}"
              f"  {_cpad(r.get('domain', 'work'), 9, C_DIM)}{_age(r['started'])}")
        print(f"    {_c('now:', C_DIM)} {r.get('current') or r.get('note') or 'no output yet'}")
        if r.get("pane_id"):
            print(f"    {_c('pane:', C_DIM)} {r['pane_id']}  (herdr, workspace otto-runs)")
        bits = []
        if r.get("tool_calls"):
            bits.append(f"{r['tool_calls']} tool call(s)")
        if r.get("output_tokens"):
            bits.append(f"{persona.thousands(r['output_tokens'])} out")
        if r.get("last_activity"):
            bits.append(f"last wrote {_age(r['last_activity'])}")
        if bits:
            print(f"    {_c(' | '.join(bits), C_DIM)}")
    print()
    return 0


def cmd_watch(args, client: Client) -> int:
    """Timeline for one run, optionally following until it finishes."""
    seen = 0
    while True:
        a = client.activity(args.run_id)
        if args.json:
            print(json.dumps(a, indent=2))
            return 0
        events = a["events"]
        for e in events[seen:]:
            kind = e["kind"]
            if kind == "tool":
                mark = "..." if e.get("ok") is None else ("ok " if e["ok"] else "ERR")
                color = C_CYA if e.get("ok") is None else (C_GRN if e["ok"] else C_RED)
                print(f"  {_c(mark, color)} {_c(e['label'], C_BOLD)}  {_c(e.get('detail') or '', C_DIM)}")
                if e.get("result") and args.verbose:
                    print(f"      {_c(e['result'], C_DIM)}")
            elif kind == "text":
                for line in e["label"].splitlines():
                    print(f"      {line}")
            elif kind == "result":
                print(f"  {_c('==>', C_GRN if e.get('ok') else C_RED)} {e['label']}")
                if e.get("detail"):
                    for line in e["detail"].splitlines():
                        print(f"      {line}")
            elif kind == "init":
                print(f"  {_c('---', C_DIM)} {e['label']}  {_c(e.get('detail') or '', C_DIM)}")
            else:
                print(f"      {_c(e['label'], C_DIM)}")
        seen = len(events)

        if not a["live"] or not args.follow:
            if a["live"]:
                print(_c(f"\n  still running: {a['current']}", C_DIM))
            bits = []
            if a.get("output_tokens"):
                bits.append(f"{persona.thousands(a['output_tokens'])} out")
            if a.get("cost_usd") is not None:
                bits.append(f"${a['cost_usd']:.3f}")
            if a.get("turns"):
                bits.append(f"{a['turns']} turns")
            if bits:
                print(_c(f"  {'  |  '.join(bits)}", C_DIM))
            if a.get("note"):
                print(_c(f"  {a['note']}", C_DIM))
            return 0
        time.sleep(args.interval)


def cmd_prune(args, client: Client) -> int:
    """Clear orphaned/killed/failed runs out of history. Dry run unless --yes."""
    r = client.prune_runs(args.older_than, dry_run=not args.yes)
    if not r["pruned"]:
        print(f"  nothing to prune older than {args.older_than}h")
        return 0
    verb = "would prune" if r["dry_run"] else "pruned"
    print(f"  {verb} {r['pruned']} run(s), {r['remaining']} kept")
    for x in r["runs"][:20]:
        print(f"    {x['id']}  {x['name'][:28]:<30} {x['status']:<9} {x['ended']}")
    if r["dry_run"]:
        print(_c("  re-run with --yes to actually remove them", C_DIM))
    return 0


def _wait_run(client: Client, run_id: str, timeout: int) -> bool:
    """Poll a run to completion. True if it finished inside the timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            if client.run(run_id)["status"] != "running":
                return True
        except RuntimeError:
            return False
    return False


def add_runs(sub) -> None:
    s = sub.add_parser("runs", help="run history")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_runs)


def add_logs(sub) -> None:
    s = sub.add_parser("logs", help="tail a run's captured log")
    s.add_argument("run_id")
    s.add_argument("--lines", type=int, default=200)
    s.set_defaults(fn=cmd_logs)


def add_spawn(sub) -> None:
    s = sub.add_parser("spawn", help="launch a tracked Claude Code session")
    s.add_argument("name")
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--cwd")
    s.add_argument("--agent", help="subagent definition from ~/.claude/agents")
    s.add_argument("--mode", choices=["headless", "windowed"], default="headless")
    s.add_argument("--task", help="external queue id")
    s.add_argument("--tier")
    lvl = s.add_mutually_exclusive_group()
    lvl.add_argument("--yolo", dest="permissions", action="store_const", const="yolo",
                     help="--dangerously-skip-permissions: the full operator (default)")
    lvl.add_argument("--plan", dest="permissions", action="store_const", const="plan",
                     help="--permission-mode plan: read-only investigation that writes a plan")
    # --safe was the old spelling of --plan; kept so a muscle-memory flag still works.
    lvl.add_argument("--safe", dest="permissions", action="store_const", const="plan",
                     help=argparse.SUPPRESS)
    s.set_defaults(permissions="yolo")
    s.add_argument("--domain", choices=["work", "personal"],
                   help="default: inferred from --cwd")
    s.add_argument("--model",
                   help=f"default: {config.DEFAULT_MODEL}. Pass "
                        f"{config.DEEP_MODEL} for long-horizon work")
    s.set_defaults(fn=cmd_spawn)


def add_kill(sub) -> None:
    s = sub.add_parser("kill", help="terminate a tracked run")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_kill)


def add_done(sub) -> None:
    s = sub.add_parser("done", help="mark a run finished (agents self-report here)")
    s.add_argument("run_id")
    s.add_argument("--status", default="ok", choices=["ok", "failed", "skipped"])
    s.add_argument("--exit-code", type=int, default=0)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_done)


def add_live(sub) -> None:
    s = sub.add_parser("live", help="agents running right now")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_live)


def add_watch(sub) -> None:
    s = sub.add_parser("watch", help="timeline for one run")
    s.add_argument("run_id")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--interval", type=int, default=3)
    s.add_argument("--verbose", action="store_true", help="include tool results")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_watch)


def add_prune(sub) -> None:
    s = sub.add_parser("prune", help="clear failed/orphaned runs from history")
    s.add_argument("--older-than", type=int, default=24, metavar="HOURS")
    s.add_argument("--yes", action="store_true", help="actually remove them")
    s.set_defaults(fn=cmd_prune)


PARSERS = {
    "runs": add_runs,
    "logs": add_logs,
    "spawn": add_spawn,
    "kill": add_kill,
    "done": add_done,
    "live": add_live,
    "watch": add_watch,
    "prune": add_prune,
}
