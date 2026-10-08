"""Assistant scope: how the owner is actually doing, and what moves their energy."""

from __future__ import annotations

import json
import sys
from datetime import datetime

from ..store import Store
from ..client import Client, DaemonDown
from ._fmt import C_BOLD, C_DIM, C_GRN, C_RED, C_YEL, _c, _cpad


def _ask_signals() -> dict:
    """The guided walk. Enter skips, so it can be abandoned halfway and still count.

    Skipping has to be the cheapest key on the keyboard. A walk that punishes you for
    not knowing is a walk you stop running, and an unanswered signal is recorded as
    unknown rather than as a no, so half a check-in is still honest data.
    """
    from .. import wellbeing as _w

    out: dict = {}
    print(_c("  enter to skip, ctrl-c to stop (what you skip stays unknown)", C_DIM))
    for s in _w.SIGNALS:
        suffix = " 1-5" if s.kind == "scale" else " y/n"
        try:
            raw = input(f"    {s.prompt}{suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if s.kind == "scale":
            if raw.isdigit() and 1 <= int(raw) <= 5:
                out[s.key] = int(raw)
            else:
                print(_c("      not 1-5, left unknown", C_DIM))
        elif raw[0] in "yn":
            out[s.key] = raw[0] == "y"
        else:
            print(_c("      not y/n, left unknown", C_DIM))
    return out


def cmd_checkin(args, client: Client) -> int:
    """Record the half of the day only the owner can report.

    The note alone is still a complete check-in. Signals are optional, and the flag
    form (`--did kayak,outside --not doom`) exists so adding them costs one flag
    rather than fifteen prompts.
    """
    from .. import wellbeing as _w

    note = " ".join(args.text).strip() if args.text else None
    try:
        signals = _w.normalize(args.did, getattr(args, "not"),
                               {"sleep": args.sleep, "waking": args.waking,
                                "energy": args.energy})
    except _w.Refused as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 2
    if args.ask:
        signals.update(_ask_signals())

    if not note and not signals:
        print("  nothing to record.\n"
              "    otto checkin \"slept badly, twins appt at 2\" --energy 2\n"
              "    otto checkin \"got on the lake\" --did kayak,outside --not doom\n"
              "    otto checkin --ask", file=sys.stderr)
        return 2

    day = args.date or datetime.now().astimezone().date().isoformat()
    energy = signals.pop("energy", None)
    try:
        rec = client.checkin(day, note=note, energy=energy, signals=signals)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    ci = rec.get("checkin") or {}
    bits = [f"energy {ci['energy']}/5"] if ci.get("energy") else []
    recorded = [k for k in ci if k in _w.BY_KEY and k != "energy"]
    if recorded:
        bits.append(f"{len(recorded)} signal(s)")
    print(f"  recorded for {day}  {' '.join(bits)}")
    return 0


def cmd_patterns(args, client: Client) -> int:
    """What the check-ins say actually moves his energy. Local, deterministic, free."""
    from .. import wellbeing as _w

    try:
        data = client.patterns(args.days)
    except DaemonDown:
        data = _w.patterns(Store(), days=args.days)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print()
    print(_c("  PATTERNS", C_BOLD)
          + f"  {data['days']} check-in(s), {data['with_energy']} with an energy score")

    if data["with_energy"] < 2:
        print()
        print(_c("  Not enough yet. This needs days where you recorded energy AND what "
                 "you did.", C_DIM))
        print(_c("  otto checkin \"...\" --energy 4 --did kayak,outside", C_DIM))
        print()
        return 0

    ranked = data["ranked"]
    if ranked:
        print()
        print(_c("  WHAT MOVES IT", C_BOLD)
              + _c(f"   energy on days you did vs days you did not", C_DIM))
        for r in ranked:
            d = r["delta"]
            color = C_GRN if (d > 0) == r["good"] else C_YEL
            sign = "+" if d > 0 else ""
            print(f"    {_cpad(sign + str(d), 7, color)}{r['label'][:30]:<32}"
                  + _c(f"{r['energy_yes']} on {r['n_yes']}d  vs  "
                       f"{r['energy_no']} on {r['n_no']}d", C_DIM))

    thin = [s for s in data["signals"] if not s["enough"] and (s["n_yes"] or s["n_no"])]
    if thin:
        print()
        print(_c(f"  NOT ENOUGH DATA YET", C_BOLD)
              + _c(f"   needs {data['min_per_side']} days on each side", C_DIM))
        for s in thin[:8]:
            print(_c(f"    {s['label'][:30]:<32}{s['n_yes']}d yes / {s['n_no']}d no",
                     C_DIM))

    print()
    print(_c("  LATELY", C_BOLD) + _c("   last 7 days", C_DIM))
    for key, rec in data["recent"].items():
        if rec["last"] is None and rec["in_window"] == 0:
            continue
        since = rec["days_since"]
        gap = "today" if since == 0 else (f"{since}d ago" if since is not None else "never")
        warn = C_YEL if (rec["good"] and since is not None and since >= 7) else C_DIM
        print(f"    {rec['label'][:30]:<32}{rec['in_window']}/7   "
              + _c(f"last {gap}", warn))

    s = data["suggestion"]
    if s:
        print()
        print(_c("  SMALLEST THING WITH EVIDENCE BEHIND IT", C_BOLD))
        gap = (f"{s['days_since']}d ago" if s["days_since"] is not None else "never recorded")
        print(f"    {s['label']}: worth {_c('+' + str(s['delta']), C_GRN)} energy "
              f"across {s['n_yes']}+{s['n_no']} days, last {gap}")
    print()
    return 0


def add_checkin(sub) -> None:
    s = sub.add_parser("checkin", help="record how you are actually doing")
    s.add_argument("text", nargs="*", help="a line or two, not a form")
    s.add_argument("--energy", type=int, choices=[1, 2, 3, 4, 5],
                   help="end of day, 1-5")
    s.add_argument("--sleep", type=int, choices=[1, 2, 3, 4, 5])
    s.add_argument("--waking", type=int, choices=[1, 2, 3, 4, 5],
                   help="energy on waking, 1-5")
    # One flag for the yes-list and one for the no-list, rather than a flag per
    # signal. Anything named in neither stays UNKNOWN, which is the whole point:
    # silence must not be recorded as a no.
    s.add_argument("--did", metavar="a,b,c",
                   help="signals that happened: " + "|".join(
                       k for k in ("outside", "moved", "kayak", "connected", "known",
                                   "made", "valued", "quiet", "doom", "obligation",
                                   "irritable", "resentful", "self")))
    s.add_argument("--not", metavar="a,b,c", help="signals that explicitly did not")
    s.add_argument("--ask", action="store_true",
                   help="walk the questions, enter to skip any")
    s.add_argument("--date", help="ISO date, default today")
    s.set_defaults(fn=cmd_checkin)


def add_patterns(sub) -> None:
    s = sub.add_parser("patterns", help="what actually moves your energy")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_patterns)


PARSERS = {
    "checkin": add_checkin,
    "patterns": add_patterns,
}
