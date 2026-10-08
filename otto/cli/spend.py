"""What it costs: spend by workload, the session ledger, Claude Code telemetry."""

from __future__ import annotations

import json

from .. import config, telemetry
from ..client import Client, DaemonDown
from ._fmt import C_BOLD, C_DIM, C_RED, C_YEL, _c, _cpad


def _spend_table(title: str, rows: list[dict], total: float) -> None:
    if not rows:
        return
    print(f"\n  {_c(title, C_BOLD)}")
    print(f"  {'':<20}{'runs':>5}{'total':>10}{'median':>9}{'in tok':>10}{'share':>8}")
    for row in rows:
        # A row worth acting on is one that is both big and cheap per run, so the
        # median and the input tokens sit next to the total rather than in a
        # separate view: a $50/day workload at $0.04 a run is a frequency problem,
        # the same $50 in six runs is a model problem.
        heavy = row["share"] >= 25.0
        key = _cpad(row["key"][:19], 20, C_BOLD if heavy else "")
        print(f"  {key}{row['runs']:>5}{row['total_usd']:>10.2f}"
              f"{row['median_usd']:>9.3f}{row['median_input']:>10,}{row['share']:>7.1f}%")
    shown = sum(r["total_usd"] for r in rows)
    if abs(shown - total) > 0.01:
        print(f"  {_c(f'(rows sum to ${shown:.2f} of ${total:.2f})', C_DIM)}")


def cmd_spend(args, client: Client) -> int:
    rep = client.spend(days=args.days, domain=args.domain)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    if not rep["runs"]:
        print(f"  no priced runs in the last {rep['days']} days")
        return 0

    scope = f" ({rep['domain']})" if rep["domain"] else ""
    monthly = _c(f"~${rep['per_month_usd']:,.0f}/month", C_BOLD)
    print(f"\n  {_c('SPEND' + scope, C_BOLD)}  last {rep['days']}d, "
          f"{rep['observed_days']} with activity")
    print(f"  ${rep['total_usd']:,.2f} over {rep['runs']} runs"
          f"   ${rep['per_day_usd']:,.2f}/day   {monthly}")
    if rep["unpriced_runs"]:
        note = (f"{rep['unpriced_runs']} unpriced runs not counted "
                "(running, killed, or shell)")
        print(f"  {_c(note, C_DIM)}")

    only = args.by
    if only in (None, "workload"):
        _spend_table("BY WORKLOAD", rep["by_workload"], rep["total_usd"])
    if only in (None, "model"):
        _spend_table("BY MODEL", rep["by_model"], rep["total_usd"])
    if only in (None, "agent"):
        rows = rep["by_agent"]
        if rows:
            _spend_table("BY AGENT", rows, rep["total_usd"])
        else:
            print(f"\n  {_c('BY AGENT', C_BOLD)}")
            print(f"  {_c('no run in this window was launched with --agent', C_DIM)}")

    if only is None and rep["top_runs"]:
        print(f"\n  {_c('MOST EXPENSIVE RUNS', C_BOLD)}")
        for r in rep["top_runs"][:5]:
            model = r["model"] or "-"
            print(f"  {r['id']:<8}{r['name'][:38]:<40}{r['cost_usd']:>7.2f}  {_c(model, C_DIM)}")
    return 0


def _span(hours) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def cmd_ledger(args, client: Client) -> int:
    """Every session on this machine, priced. `otto spend` is Otto's runs by
    workload; this is the whole machine ranked by session, the owner's terminals first
    because that is where the money is."""
    if args.session:
        d = client.ledger_session(args.session)
        if args.json:
            print(json.dumps(d, indent=2))
            return 0
        title = d.get("title") or d.get("run_name") or d.get("prompt") or "(untitled)"
        print(f"\n  {_c(title[:70], C_BOLD)}")
        print(f"  {d['sid'][:8]}  {d['who']}  {d.get('cwd') or d.get('project') or ''}")
        basis = {"reported": "reported by Claude Code", "estimate": "estimated from transcript",
                 "partial": "estimated (telemetry covers part)"}[d["basis"]]
        print(f"  ${d['usd']:,.2f}  {_c(basis, C_DIM)}   {d['turns']} turns"
              f" ({d['subagent_turns']} in subagents)   span {_span(d['hours'])}"
              f"   ${d['per_turn']:.3f}/turn")
        ck = d["cost_kind"]
        print(f"  cache reads ${ck['cr']:.2f} · cache writes ${ck['cw1'] + ck['cw5']:.2f}"
              f" · output ${ck['out']:.2f} · input ${ck['inp']:.2f}")
        models = ", ".join(f"{m} {n}" for m, n in sorted(d["models"].items(), key=lambda kv: -kv[1]))
        print(f"  models: {models}")
        print(f"  context: first {d['first_ctx'] or 0:,} tokens, peak {d['max_ctx']:,}")
        if d["rebuilds"]:
            print(f"\n  {_c('REBUILDS', C_BOLD)}  {len(d['rebuilds'])} idle gaps past the cache TTL"
                  f" rewrote the context, ${d['rebuild_usd']:.2f}")
            for r in d["rebuilds"][:8]:
                print(f"    {r['at'][:16]}  idle {r['idle_minutes']:>5} min  wrote {r['tokens'] / 1e3:>7.0f}K  ${r['usd']:.2f}")
        if d["tools"]:
            print(f"\n  {_c('WHAT FILLED THE CONTEXT', C_BOLD)}  injected once, re-sent every later turn")
            print(f"  {'tool':<26}{'calls':>6}{'injected':>11}{'amplified':>12}{'share':>7}")
            for t in d["tools"][:10]:
                print(f"  {t['tool'][:25]:<26}{t['calls']:>6}{t['injected'] / 1e3:>10.0f}K"
                      f"{t['amplified'] / 1e6:>11.1f}M{t['share']:>6.1f}%")
        if d["details"]:
            print(f"\n  {_c('SPECIFICALLY', C_BOLD)}")
            for x in d["details"][:8]:
                print(f"  {x['amplified'] / 1e6:>8.1f}M {x['calls']:>4}  {x['what'][:70]}")
        return 0

    rep = client.ledger(days=args.days, who=args.who)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    t = rep["totals"]
    pr = rep["pricing"]
    tel = rep["telemetry"]
    print(f"\n  {_c('LEDGER', C_BOLD)}  last {rep['days']}d  {t['sessions']} sessions, {t['turns']:,} turns")
    print(f"  ${t['usd']:,.2f} API-equivalent   yours ${t['by_who']['yours']:,.2f}"
          f" ({t['sessions_by_who']['yours']})   otto ${t['by_who']['otto']:,.2f}"
          f" ({t['sessions_by_who']['otto']})")
    k = t["by_kind"]
    tot = sum(k.values()) or 1
    print(f"  cache reads {100 * k['cr'] / tot:.0f}% · cache writes {100 * (k['cw1'] + k['cw5']) / tot:.0f}%"
          f" · output {100 * k['out'] / tot:.0f}%   rebuilds after idle gaps: {t['rebuilds_yours']}"
          f" costing ${t['rebuilds_yours_usd']:,.0f}")
    if tel["installed"]:
        note = (f"telemetry on: {tel['records']:,} requests reported, "
                f"{tel['reported_sessions']} sessions priced from it")
        print(f"  {_c(note, C_DIM)}")
    else:
        print(f"  {_c('telemetry off: every figure is an estimate. otto telemetry install', C_YEL)}")
    stale = _c(" STALE", C_RED) if pr["stale"] else ""
    rates_note = f"rates reviewed {pr['reviewed']} ({pr['age_days']}d ago)"
    print(f"  {_c(rates_note, C_DIM)}{stale}")

    print(f"\n  {_c('MOST EXPENSIVE SESSIONS', C_BOLD)}")
    print(f"  {'':<9}{'who':<6}{'cost':>9}{'turns':>7}{'span':>7}{'ctx':>7}{'rebuild':>9}  title")
    for s in rep["sessions"][:args.top]:
        title = (s["title"] or s["run_name"] or s["prompt"] or "")[:44]
        flag = "" if s["basis"] == "reported" else "~"
        print(f"  {s['sid'][:8]} {s['who']:<6}{flag}{s['usd']:>8.2f}{s['turns']:>7}{_span(s['hours']):>7}"
              f"{s['max_ctx'] // 1000:>6}K{s['rebuilds']['usd']:>9.2f}  {title}")
    print(f"  {_c('~ estimated from the transcript; otto ledger --session <id> for the story', C_DIM)}")
    return 0


def cmd_telemetry(args, client: Client) -> int:
    """Claude Code's own per-request cost, exported to the daemon."""
    if args.telemetry_cmd == "install":
        changed, msg = telemetry.install()
        print(f"  {msg}")
        if changed:
            print("  New Claude Code sessions export to the daemon from now on. Sessions"
                  " already open keep their old environment until restarted.")
        return 0
    if args.telemetry_cmd == "uninstall":
        changed, msg = telemetry.uninstall()
        print(f"  {msg}")
        return 0
    try:
        st = client.telemetry()
    except DaemonDown:
        st = {**telemetry.summary(), "endpoint": config.BASE_URL, "daemon": "down"}
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    on = st.get("installed")
    print(f"\n  {_c('TELEMETRY', C_BOLD)}  {'installed' if on else _c('not installed', C_YEL)}"
          f"  endpoint {st.get('endpoint')}" + ("  " + _c("daemon down", C_RED) if st.get("daemon") == "down" else ""))
    print(f"  {st.get('records', 0):,} API requests from {st.get('sessions', 0)} sessions"
          f"   ${st.get('cost_usd', 0):,.2f} reported"
          + (f"   since {st['since'][:16]}" if st.get("since") else ""))
    if not on:
        print(f"  {_c('otto telemetry install', C_BOLD)} writes the env block to ~/.claude/settings.json")
    return 0


def add_spend(sub) -> None:
    s = sub.add_parser("spend", help="cost per workload, model, and agent")
    s.add_argument("--days", type=int, default=7,
                   help="window (default 7; a month hides a cadence change)")
    s.add_argument("--by", choices=["workload", "model", "agent"],
                   help="one grouping only (default: all three)")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_spend)


def add_ledger(sub) -> None:
    s = sub.add_parser("ledger", help="every session on this machine, priced and ranked")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--who", choices=["yours", "otto", "other"],
                   help="only the owner's terminals, only Otto's runs, or anything else")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--session", metavar="ID", help="one session's story (a prefix is enough)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ledger)


def add_telemetry(sub) -> None:
    s = sub.add_parser("telemetry", help="Claude Code's own per-request cost, exported to the daemon")
    tel_sub = s.add_subparsers(dest="telemetry_cmd")
    tel_sub.add_parser("status", help="is it on, how much has arrived")
    tel_sub.add_parser("install", help="write the env block to ~/.claude/settings.json")
    tel_sub.add_parser("uninstall", help="remove the env block")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_telemetry, telemetry_cmd="status")


PARSERS = {
    "spend": add_spend,
    "ledger": add_ledger,
    "telemetry": add_telemetry,
}
