"""Talking to Otto, and refreshing what it knows from mail and calendar."""

from __future__ import annotations

import sys
import time

from .. import config, persona
from ..client import Client
from ._fmt import C_CYA, C_DIM, C_RED, C_YEL, _age, _c, _print_snapshots


def cmd_chat(args, client: Client) -> int:
    """Talk to Otto. Each turn is a real claude -p run, so it takes a few seconds."""
    if args.reset:
        client.chat_reset()
        print("  thread reset")
        return 0

    if args.show or not args.message:
        thread = client.chat_thread()
        msgs = thread.get("messages") or []
        if not msgs:
            print("  no conversation yet. Try: otto chat \"what needs me?\"")
            return 0
        for m in msgs[-args.limit :]:
            who = "you" if m["role"] == "user" else config.PERSONA_NAME
            color = C_DIM if m["role"] == "user" else C_CYA
            print(f"\n  {_c(who, color)}  {_c(m['at'], C_DIM)}")
            for line in str(m["text"]).splitlines():
                print(f"    {line}")
        if thread.get("pending"):
            print(_c("\n  (a reply is still in flight)", C_DIM))
        return 0

    message = " ".join(args.message)
    try:
        client.chat_send(message)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1

    print(_c(f"  asked. {config.PERSONA_NAME} is thinking...", C_DIM))
    if args.no_wait:
        print("  check back with: otto chat --show")
        return 0

    # Poll the thread rather than the run: the reply only exists once harvested.
    deadline = time.time() + args.timeout
    seen = None
    while time.time() < deadline:
        time.sleep(2)
        thread = client.chat_thread()
        msgs = [m for m in (thread.get("messages") or []) if m["role"] == "assistant"]
        if msgs and msgs[-1]["at"] != seen:
            last = msgs[-1]
            print(f"\n  {_c(config.PERSONA_NAME, C_CYA)}")
            for line in str(last["text"]).splitlines():
                print(f"    {line}")
            meta = []
            if last.get("output_tokens"):
                meta.append(f"{persona.thousands(last['output_tokens'])} out")
            if last.get("cost_usd") is not None:
                meta.append(f"${last['cost_usd']:.3f}")
            if meta:
                print(_c(f"\n    {'  |  '.join(meta)}", C_DIM))
            return 0 if last.get("ok", True) else 1
        if not thread.get("pending"):
            break
    print(_c(f"  no reply within {args.timeout}s. Try: otto chat --show", C_YEL))
    return 1


def cmd_refresh(args, client: Client) -> int:
    """Have Otto refresh its view of calendar and mail. Both domains unless told one.

    No --domain means every wired-up domain, because "refresh my email" means all of
    it. The two run as separate sessions against different MCP servers, so this waits
    on all of them and reports each.
    """
    try:
        res = client.refresh(args.domain or "all")
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1

    runs = res.get("runs") or []
    for s in res.get("skipped") or []:
        # A skipped domain is stated, never dropped: a refresh that silently does
        # half the job reads as a success.
        print(_c(f"  skipped {s['domain']}: {s['reason']}", C_YEL))
    for r in runs:
        how = f"pid {r['pid']}" if r.get('pid') else "direct connector"
        print(f"  refreshing {r['domain']} (run {persona.short(r['id'])}, {how})")

    if args.no_wait:
        print("  check back with: otto agenda")
        return 0

    pending = {r["id"]: r for r in runs}
    failed: list[dict] = []
    deadline = time.time() + args.timeout
    while pending and time.time() < deadline:
        time.sleep(3)
        for run_id, run in list(pending.items()):
            if client.run(run_id)["status"] == "running":
                continue
            pending.pop(run_id)
            snaps = client.snapshots()
            pushed = [k for k, v in snaps.items()
                      if v.get("source") == "otto-refresh"
                      and k.startswith(f"{run['domain']}/")
                      and _age(v.get("fetched_at")).endswith(("s ago", "m ago"))]
            if not pushed:
                failed.append(run)

    if pending:
        names = ", ".join(r["domain"] for r in pending.values())
        print(_c(f"  still running after {args.timeout}s ({names}). Check: otto agenda", C_DIM))

    _print_snapshots(client.snapshots())
    for run in failed:
        print(_c(f"  {run['domain']} finished but pushed nothing. Check the log:", C_YEL))
        print(f"    otto logs {persona.short(run['id'])}")
    return 1 if failed else 0


def add_chat(sub) -> None:
    s = sub.add_parser("chat", help="talk to Otto about the control plane")
    s.add_argument("message", nargs="*")
    s.add_argument("--show", action="store_true", help="print the thread and exit")
    s.add_argument("--reset", action="store_true", help="forget the conversation")
    s.add_argument("--no-wait", action="store_true", help="do not wait for the reply")
    s.add_argument("--timeout", type=int, default=120)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_chat)


def add_refresh(sub) -> None:
    s = sub.add_parser("refresh", help="pull calendar + mail, per domain (a session, or the direct connector)")
    s.add_argument("--domain", choices=config.DOMAINS,
                   help="which account to refresh (default work)")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--timeout", type=int, default=150)
    s.set_defaults(fn=cmd_refresh)


PARSERS = {
    "chat": add_chat,
    "refresh": add_refresh,
}
