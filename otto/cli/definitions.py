"""The definition surface and the capability inventory: registry, scan, manifest, identity, feeds, skills, known."""

from __future__ import annotations

import json
import sys

from .. import config, known
from ..store import Store
from ..client import Client
from ._fmt import C_BOLD, C_DIM, C_RED, _c, _cpad


def cmd_registry(args, client: Client) -> int:
    entries = client.registry(args.kind)
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("  registry empty - run `otto scan`")
        return 0
    by_kind: dict[str, list[dict]] = {}
    for e in entries:
        by_kind.setdefault(e["kind"], []).append(e)
    for kind in sorted(by_kind):
        items = by_kind[kind]
        print(_c(f"\n  {kind.upper()}S ({len(items)})", C_BOLD))
        for e in items:
            scope = (e["repo"] or "global")[:28]
            flag = _c(" MISSING", C_RED) if e["missing"] else ""
            desc = (e.get("description") or "")[:60]
            print(f"    {e['name']:<26} {_cpad(scope, 28, C_DIM)} "
                  f"{_c(desc, C_DIM)}{flag}")
    return 0


def cmd_scan(args, client: Client) -> int:
    result = client.scan()
    changes = result["changes"]
    if not changes:
        print("  registry in sync, no changes")
    else:
        print(f"  {len(changes)} change(s):")
        for c in changes[:40]:
            print(f"    {c}")
        if len(changes) > 40:
            print(f"    ... and {len(changes) - 40} more")
    summ = result["summary"]
    print(f"  now tracking: {', '.join(f'{v} {k}s' for k, v in sorted(summ.items()))}")
    return 0


def cmd_known(args, client: Client) -> int:
    """Schedules and integrations the owner has marked as known failures, and the ones
    that have since healed and want their annotation removed."""
    sub = getattr(args, "known_cmd", None)
    if sub == "add":
        try:
            item = client.mark_known(args.kind, args.name, args.reason)
        except RuntimeError as e:
            print(_c(f"  {e}", C_RED), file=sys.stderr)
            return 2
        print(f"  marked {item['kind']} {item['name']} as a known failure")
        print(_c(f"  its alert drops to info and reads: known: {item['reason'][:70]}", C_DIM))
        print(_c("  Otto will tell you once when it succeeds again.", C_DIM))
        return 0
    if sub == "rm":
        r = client.clear_known(args.kind, args.name)
        print(f"  cleared {r['removed']}; its alert is back at full volume")
        return 0
    items = client.known()
    if getattr(args, "json", False):
        print(json.dumps(items, indent=2))
        return 0
    print(_c("  KNOWN FAILURES", C_BOLD)
          + _c("   demoted to info until they heal, then flagged STALE", C_DIM))
    print(known.render_items(items), end="")
    return 0


def cmd_manifest(args, client: Client) -> int:
    """Declared-vs-observed over the capability surface. Purely local: it reads
    registry keys and filenames off disk, so it works with the daemon down."""
    from .. import manifest as _manifest
    if args.json:
        print(json.dumps({
            "findings": [{"name": f.capability.name,
                          "surface": f.capability.surface,
                          "expect": f.capability.expect,
                          "state": f.state,
                          "detail": f.detail} for f in _manifest.check()],
            "undeclared": [{"surface": s, "name": n} for s, n in _manifest.undeclared()],
        }, indent=2))
    else:
        print(_manifest.render())
    bad = [f for f in _manifest.check() if not f.ok]
    return 1 if bad else 0


def cmd_identity(args, client: Client) -> int:
    """Who Otto authenticates as, per integration. Local, like `otto manifest`.

    Exit code is deliberately NOT red for the standing gap. `known-inline` and
    `human` rows are declared, accepted, and on the board; failing on them would make
    a green run impossible until the whole build lands, and a check that can never
    pass is a check nobody runs. Only drift fails.
    """
    from .. import identity as _identity
    findings = _identity.check()
    if args.json:
        print(json.dumps({
            "scoreboard": _identity.scoreboard(),
            "principals": [{"name": f.principal.name,
                            "surface": f.principal.surface,
                            "principal": f.principal.principal,
                            "credential": f.principal.credential,
                            "identifier": f.principal.identifier,
                            "audit": f.principal.audit,
                            "blocker": f.principal.blocker,
                            "state": f.state,
                            "detail": f.detail} for f in findings],
            "undeclared_inline": [{"name": n, "keys": k}
                                  for n, k in _identity.undeclared_inline()],
        }, indent=2))
    else:
        print(_identity.render(verbose=args.verbose))
    drift = [f for f in findings if f.state in _identity.DRIFT_STATES]
    return 1 if drift or _identity.undeclared_inline() else 0


def cmd_feeds(args, client: Client) -> int:
    """Declared-vs-observed over the feed directories.

    Local like `otto manifest`: it reads the feed files and the ingest ledger off
    disk, so it answers with the daemon down. That matters here because "why has
    nothing ingested my drop" is a question you ask precisely when something is
    not running.
    """
    from .. import feeds as _feeds

    store = Store()
    if getattr(args, "init", False):
        made = _feeds.scaffold()
        for path in made:
            print(f"  created {path}")
        if not made:
            print("  every declared feed directory already exists")
        if not config.FEED_SOURCES:
            print(f"  no sources declared, so only the roots exist. Feed root: "
                  f"{config.FEED_DIR}")
        return 0

    rows = _feeds.status(store)
    if args.json:
        print(json.dumps({
            "root": str(config.FEED_DIR),
            "producers": str(config.PRODUCERS_DIR),
            "file": config.FEED_FILE,
            "sources": [{"name": s.source.name, "domain": s.source.domain,
                         "trust": s.source.trust,
                         "max_age_hours": s.source.max_age_hours,
                         "state": s.state, "age_hours": s.age_hours,
                         "items": s.items, "detail": s.detail} for s in rows],
            "undeclared": _feeds.undeclared(),
            "ledger": store.feeds(),
        }, indent=2))
    else:
        print(_feeds.render(store))
    return 1 if [s for s in rows if not s.ok] else 0


def cmd_skills_audit(args, client: Client) -> int:
    """Blast-radius audit over every definition. Local: reads files, runs nothing."""
    from .. import hardening as _h
    if args.verify is not None:
        print(_h.render_verify(args.verify or None))
        failing = [a for _, cs in _h.verify() for a in cs if not a.passed]
        return 1 if failing else 0
    if args.json:
        items = _h.scan()
        print(json.dumps({
            "items": [{"name": i.name, "kind": i.kind, "scope": i.scope,
                       "tier": i.tier, "declared": i.declared,
                       "source": str(i.source), "source_exists": i.source_exists,
                       "doc_dir": str(i.doc_dir), "signals": list(i.signals),
                       "present": list(i.present), "missing": list(i.missing)}
                      for i in items
                      if args.tier is None or i.tier == args.tier],
            "gaps": _h.gaps(),
        }, indent=2))
    else:
        print(_h.render(tier=args.tier, show_ok=args.all))
    return 1 if any(not i.ok for i in _h.scan()) else 0


def add_registry(sub) -> None:
    s = sub.add_parser("registry", help="what agents/skills/commands exist")
    s.add_argument("--kind", choices=["agent", "command", "skill", "project"])
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_registry)


def add_scan(sub) -> None:
    s = sub.add_parser("scan", help="rescan disk for definitions")
    s.set_defaults(fn=cmd_scan)


def add_manifest(sub) -> None:
    s = sub.add_parser("manifest", help="declared vs observed capabilities (names only)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_manifest)


def add_identity(sub) -> None:
    s = sub.add_parser("identity", help="who Otto authenticates as, per integration")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="show the audit field and the blocker for each row")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_identity)


def add_feeds(sub) -> None:
    s = sub.add_parser("feeds", help="producer feed directories: who has dropped what")
    s.add_argument("--init", action="store_true",
                   help="create the feed root, producers/, and a dir per declared source")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_feeds)


def add_known(sub) -> None:
    kn = sub.add_parser("known", help="schedules/integrations you know are broken")
    kn.add_argument("--json", action="store_true")
    kn.set_defaults(fn=cmd_known, known_cmd=None)
    kn_sub = kn.add_subparsers(dest="known_cmd")
    a = kn_sub.add_parser("add", help="demote its alert to info, with your reason")
    a.add_argument("kind", choices=list(known.KINDS))
    a.add_argument("name")
    a.add_argument("--reason", required=True,
                   help="what you are waiting on. Replaces the alarm text")
    a.set_defaults(fn=cmd_known)
    a = kn_sub.add_parser("rm", help="clear the annotation (do this when it goes STALE)")
    a.add_argument("kind", choices=list(known.KINDS))
    a.add_argument("name")
    a.set_defaults(fn=cmd_known)


def add_skills(sub) -> None:
    sk = sub.add_parser("skills", help="the definition surface itself")
    sk_sub = sk.add_subparsers(dest="skills_cmd", required=True)
    a = sk_sub.add_parser("audit",
                          help="rank definitions by write/dispatch capability and flag "
                               "missing HARDENING.md / CONFORMANCE.md")
    a.add_argument("--tier", type=int, choices=[1, 2, 3],
                   help="only this tier")
    a.add_argument("--all", action="store_true",
                   help="include definitions that are already documented")
    a.add_argument("--verify", nargs="?", const="", metavar="NAME",
                   help="evaluate CONFORMANCE.md assertions (all, or one definition)")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_skills_audit)


PARSERS = {
    "registry": add_registry,
    "scan": add_scan,
    "manifest": add_manifest,
    "identity": add_identity,
    "feeds": add_feeds,
    "skills": add_skills,
    "known": add_known,
}
