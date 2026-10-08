"""The decision log: record, list, show."""

from __future__ import annotations

import json
import sys

from ..store import Store
from ..client import Client
from ._fmt import C_DIM, C_YEL, _c


def cmd_decide(args, client: Client) -> int:
    """Record a decision. Goes through the daemon: this is a state write."""
    try:
        d = client.add_decision(
            title=args.title,
            decision=args.decision,
            why=args.why,
            alternatives=args.alternatives,
            revisit=args.revisit,
            revisit_by=args.revisit_by,
            owner=args.owner,
            domain=args.domain,
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            decided=args.decided,
            task_id=args.card,
        ) if not args.supersedes else client.supersede_decision(
            args.supersedes,
            title=args.title,
            decision=args.decision,
            why=args.why,
            alternatives=args.alternatives,
            revisit=args.revisit,
            revisit_by=args.revisit_by,
            owner=args.owner,
            domain=args.domain,
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            decided=args.decided,
            task_id=args.card,
        )
    except RuntimeError as e:
        # A 400 here is decisions.build refusing an entry with no `why`, which is
        # the one validation worth failing loudly over.
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 2

    if args.supersedes:
        old, new = d["superseded"], d["decision"]
        print(f"  {new['id']} recorded, superseding {old['id']} ({old['title'][:44]})")
        print(f"  {old['id']} is now history. Both are kept.")
        return 0

    print(f"  {d['id']} recorded: {d['title']}")
    if d.get("revisit") and not d.get("revisit_by"):
        print(_c("  no revisit_by date, so nothing will check that condition. "
                 "--revisit-by <YYYY-MM-DD> makes it a gap when it comes due.", C_DIM))
    elif d.get("revisit_by"):
        print(f"  otto will raise it as a gap from {d['revisit_by']}")
    return 0


def cmd_decisions(args, client: Client) -> int:
    """List or search the decision log. Local read, so it works daemon-down."""
    from .. import decisions as _d

    store = Store()
    if args.search:
        hits = _d.search(store, args.search, limit=args.limit)
        if not hits:
            print(f"  nothing matching {args.search!r}")
            return 1
        for d in hits:
            print(f"  {d.id}  {d.decided}  {d.title[:66]}")
        return 0
    if args.json:
        items = [d.model_dump() for d in store.decisions()
                 if args.all or d.live]
        print(json.dumps(items, indent=2))
        return 0
    print(_d.render(store, domain=args.domain, include_superseded=args.all,
                    limit=args.limit))
    return 0


def cmd_decision(args, client: Client) -> int:
    """Show one decision in full, or schedule its review."""
    from .. import decisions as _d

    # The one mutation available on an existing decision, and the only one there
    # will ever be. See Store.set_revisit_by for the reasoning.
    if args.revisit_by is not None:
        try:
            d = client.set_revisit_by(args.id, args.revisit_by or None)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 2
        if d.get("revisit_by"):
            print(f"  {d['id']} comes back up on {d['revisit_by']}")
            print(_c(f"    checking: {d.get('revisit')}", C_DIM))
        else:
            print(f"  {d['id']} review date cleared; nothing will check it now")
        return 0

    d = Store().get_decision(args.id)
    if d is None:
        print(f"  no decision {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(d.model_dump(), indent=2))
    else:
        print(_d.render_one(d))
    return 0


def add_decide(sub) -> None:
    # ---- decisions: what was decided, why, and what would change it ----
    s = sub.add_parser("decide", help="record a decision (append-only)")
    s.add_argument("title")
    s.add_argument("--decision", required=True, help="what was decided")
    s.add_argument("--why", required=True,
                   help="the reasoning. Required: it is the part future-you cannot "
                        "reconstruct")
    s.add_argument("--alternatives", help="what else was on the table")
    s.add_argument("--revisit", help="what would change this decision")
    s.add_argument("--revisit-by", dest="revisit_by", metavar="YYYY-MM-DD",
                   help="date to recheck the revisit condition; Otto raises it as a "
                        "gap once past")
    s.add_argument("--owner", help="who is accountable (default: the configured owner)")
    s.add_argument("--domain", choices=["work", "personal"], default="work")
    s.add_argument("--tags", help="comma separated")
    s.add_argument("--decided", metavar="YYYY-MM-DD",
                   help="backdate it; defaults to today")
    s.add_argument("--card", help="board card id this came out of")
    s.add_argument("--supersedes", metavar="ID",
                   help="the decision this replaces. Both are kept; the old one is "
                        "marked superseded, never edited or deleted")
    s.set_defaults(fn=cmd_decide)


def add_decisions(sub) -> None:
    s = sub.add_parser("decisions", help="the decision log")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--all", action="store_true", help="include superseded ones")
    s.add_argument("--search", metavar="TEXT", help="substring match across every field")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_decisions)


def add_decision(sub) -> None:
    s = sub.add_parser("decision", help="show one decision in full")
    s.add_argument("id")
    s.add_argument("--revisit-by", dest="revisit_by", nargs="?", const="",
                   metavar="YYYY-MM-DD",
                   help="schedule the review (pass with no value to clear it). The "
                        "only field on a recorded decision that can change")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_decision)


PARSERS = {
    "decide": add_decide,
    "decisions": add_decisions,
    "decision": add_decision,
}
