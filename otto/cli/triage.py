"""Assessing the backlog: list, set, promote, checked, fade, status."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config
from ..backlog import TIERS as _TIERS
from ..client import Client
from ._fmt import C_BOLD, C_DIM, C_GRN, C_YEL, _age, _c, _cpad
from ._text import DetailError, _read_text_arg


# ---- backlog triage ---------------------------------------------------------

def _tasks_as_models(client: Client) -> list:
    from ..models import Task
    out = []
    for raw in client.tasks(None):
        try:
            out.append(Task.model_validate(raw))
        except ValueError:
            continue
    return out


def cmd_triage_list(args, client: Client) -> int:
    """Cards the triage pass has not judged yet, oldest first."""
    from .. import backlog as _backlog

    tasks = _tasks_as_models(client)
    rows = _backlog.needs_assessment(tasks)
    if args.stale:
        rows = _backlog.stale_candidates(tasks)
    rows = rows[: args.limit]

    if args.json:
        print(json.dumps([t.model_dump() for t in rows], indent=2))
        return 0

    if not rows:
        print("  nothing to assess. Every backlog card has been judged.")
        return 0

    label = "STALE CANDIDATES" if args.stale else "UNASSESSED"
    print(_c(f"  {len(rows)} {label}", C_BOLD))
    for t in rows:
        print(f"    {t.id[:6]}  {_cpad(_age(t.created), 12, C_DIM)}"
              f"{_c(t.title[:58], C_BOLD)}")
        if t.origin:
            print(f"            {_c('from ' + t.origin, C_DIM)}")
    print()
    return 0


def cmd_triage_set(args, client: Client) -> int:
    """Record an assessment against a card."""
    from ..models import iso, utcnow

    changes = {k: v for k, v in (
        ("owner", args.owner), ("readiness", args.readiness),
        ("tier", args.tier), ("agent", args.agent),
        ("assessed_note", _resolve_note(args)),
    ) if v is not None}
    if not changes:
        print("  nothing to record", file=sys.stderr)
        return 2
    changes["assessed"] = iso(utcnow())

    task = client.patch_task(args.task_id, **changes)
    stamp = " / ".join(str(task.get(k) or "-")
                       for k in ("owner", "tier", "readiness"))
    print(f"  {task['id'][:6]}  {task['title'][:50]}")
    print(f"  {_c(stamp, C_DIM)}")
    return 0


def _resolve_note(args) -> str | None:
    """The one-line note, from --note or --note-file.

    Same reason `--detail-file` exists: an assessment note quotes the card it
    describes, and a card can quote a command line.
    """
    path = getattr(args, "note_file", None)
    if path is None:
        return args.note
    if args.note is not None:
        raise DetailError("pass --note or --note-file, not both")
    if path == "-":
        return sys.stdin.read().lstrip("﻿").strip() or None
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    except OSError as e:
        raise DetailError(f"cannot read {path}: {e}") from e


def cmd_triage_promote(args, client: Client) -> int:
    """Promote an assessed card to `queued`. The gate, not a shortcut for mv.

    Refuses anything the gate refuses, and says why in a sentence. This is the
    only promotion path the triage pass is given, so the safety property is
    enforced by code rather than by a prompt asking a model to be careful.
    """
    from .. import backlog as _backlog

    tasks = _tasks_as_models(client)
    task = next((t for t in tasks if t.id.startswith(args.task_id)), None)
    if task is None:
        print(f"  no task matching {args.task_id}", file=sys.stderr)
        return 2

    prepare = bool(getattr(args, "prepare", False))
    why = _backlog.refusal(task, tasks, prepare=prepare)
    if why:
        print(_c(f"  REFUSED  {task.id[:6]}  {task.title[:50]}", C_YEL))
        print(f"  {why}.")
        return 1

    # auto=True alongside the status: a card promoted into `queued` with
    # auto=False sits there forever and never runs, which reads as a silent
    # success and is the exact failure the refusals above exist to avoid.
    # run_mode is set explicitly both ways so a card prepared last week and
    # approved today does not run in prepare mode again.
    updated = client.patch_task(task.id, status="queued", auto=True,
                                run_mode="prepare" if prepare else "run")
    verb = "preparing" if prepare else "promoted"
    print(_c(f"  {verb}  {updated['id'][:6]}  {updated['title'][:50]}", C_GRN))
    if prepare:
        print(_c("  a run will write the proposal into the card's plan and stop; "
                 "approve it with otto task plan <id> --approve", C_DIM))
    return 0


def cmd_triage_checked(args, client: Client) -> int:
    """Record that a stale card was looked at, and when to look again.

    Replaces the daily essay. One dated line goes on the detail, `last_checked`
    moves, and `--next-look` keeps it out of the stale list until that date.
    """
    from ..models import iso, utcnow

    tasks = client.tasks(None)
    cur = next((t for t in tasks if t["id"].startswith(args.task_id)), None)
    if cur is None:
        print(f"  no task matching {args.task_id}", file=sys.stderr)
        return 2
    note = _read_text_arg(getattr(args, "note_file", None), args.note)
    changes: dict = {"last_checked": iso(utcnow())}
    if args.next_look:
        changes["next_look"] = args.next_look
    if note:
        stamp = iso(utcnow())[:10]
        changes["detail"] = "\n".join(x for x in (
            (cur.get("detail") or "").rstrip(),
            f"--- checked {stamp}: {note.strip().splitlines()[0][:200]}",
        ) if x)
    updated = client.patch_task(cur["id"], **changes)
    nl = f", next look {updated['next_look']}" if updated.get("next_look") else ""
    print(f"  {updated['id'][:6]}  {updated['title'][:50]}  (checked{nl})")
    return 0


def cmd_triage_fade(args, client: Client) -> int:
    """Move the owner's untouched, undated, feed-filed cards out of the columns.

    Prints every card it fades so the run's report can carry the list. Nothing is
    closed and nothing is deleted; `otto task mv <id> backlog` brings one back.
    """
    from .. import backlog as _backlog

    tasks = _tasks_as_models(client)
    rows = _backlog.fade_candidates(tasks)
    if args.json:
        print(json.dumps([t.model_dump() for t in rows], indent=2))
        return 0
    if not rows:
        print(f"  nothing to fade. No backlog card of yours has sat untouched "
              f"for {config.FADE_DAYS} days.")
        return 0
    label = "WOULD FADE" if args.dry_run else "FADED"
    print(_c(f"  {len(rows)} {label}  (untouched {config.FADE_DAYS}d+, undated, "
             f"filed by a feed)", C_BOLD))
    for t in rows:
        if not args.dry_run:
            client.patch_task(t.id, status="faded")
        print(f"    {t.id[:6]}  {_cpad(_age(t.updated or t.created), 12, C_DIM)}"
              f"{t.title[:58]}")
        if t.origin:
            print(f"            {_c('from ' + t.origin, C_DIM)}")
    if not args.dry_run:
        print(_c("  back: otto task mv <id> backlog   list: otto task ls --faded", C_DIM))
    print()
    return 0


def cmd_triage_status(args, client: Client) -> int:
    from .. import backlog as _backlog

    s = _backlog.summary(_tasks_as_models(client))
    if args.json:
        print(json.dumps(s, indent=2))
        return 0

    print()
    print(f"  backlog {_c(str(s['backlog']), C_BOLD)}"
          f"   assessed {s['assessed']}   unassessed "
          f"{_c(str(s['unassessed']), C_YEL if s['unassessed'] else C_DIM)}")
    print(_c(f"  in flight {s['in_flight']}/{s['max_in_flight']}"
             f"   promotable now {s['promotable']}"
             f"   preparable {s['preparable']}"
             f"   proposals awaiting approval {s['awaiting_plan_approval']}", C_DIM))
    print()
    print(f"  {_c('yours', C_BOLD)} {s['mine']}"
          f"   otto {s['otto']}"
          f"   waiting on a decision {s['blocked_on_a_decision']}"
          f"   waiting on information {s['waiting_on_information']}"
          f"   faded {s['faded']}")
    print()
    return 0


def add_triage(sub) -> None:
    tri = sub.add_parser("triage", help="assess the backlog and promote what may run")
    tri_sub = tri.add_subparsers(dest="triage_cmd", required=True)

    a = tri_sub.add_parser("list", help="cards not yet assessed, oldest first")
    a.add_argument("--limit", type=int, default=20)
    a.add_argument("--stale", action="store_true",
                   help="instead list old cards worth checking for 'already done'")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_triage_list)

    a = tri_sub.add_parser("set", help="record an assessment against a card")
    a.add_argument("task_id")
    a.add_argument("--owner", choices=["otto", "owner"])
    a.add_argument("--readiness", choices=["ready", "needs-info", "needs-decision"])
    a.add_argument("--tier", choices=list(_TIERS))
    a.add_argument("--agent", help="subagent type, when the owner is otto")
    a.add_argument("--note", help="one line for the ranked list, not the evidence")
    a.add_argument("--note-file", metavar="PATH",
                   help="read the note from a file, or - for stdin")
    a.set_defaults(fn=cmd_triage_set)

    a = tri_sub.add_parser("promote", help="move an assessed card to queued, if the gate allows")
    a.add_argument("task_id")
    a.add_argument("--prepare", action="store_true",
                   help="tier-1 only: run in prepare mode, which writes a proposal into "
                        "the card's plan and applies nothing")
    a.set_defaults(fn=cmd_triage_promote)

    a = tri_sub.add_parser("checked", help="record that a stale card was verified, in one line")
    a.add_argument("task_id")
    a.add_argument("--note", help="one line: what you verified and where")
    a.add_argument("--note-file", metavar="PATH", help="read the note from a file, or - for stdin")
    a.add_argument("--next-look", metavar="YYYY-MM-DD",
                   help="skip stale checks on this card until then")
    a.set_defaults(fn=cmd_triage_checked)

    a = tri_sub.add_parser("fade", help="take the owner's untouched, undated, feed-filed cards out of the columns")
    a.add_argument("--dry-run", action="store_true", help="list what would fade, change nothing")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_triage_fade)

    a = tri_sub.add_parser("status", help="counts: assessed, in flight, promotable")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_triage_status)


PARSERS = {
    "triage": add_triage,
}
