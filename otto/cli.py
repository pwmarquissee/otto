"""The `otto` command line.

Reads work with the daemon down (state files are safe to read concurrently);
writes require it, and say so plainly rather than failing obscurely.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, known, persona, registry, telemetry, verdict
from . import today as _today
from .backlog import TIERS as _TIERS
from .client import Client, DaemonDown
from .models import Run
from .runners import scheduled
from .store import Store

C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RED = "\033[31m"
C_YEL = "\033[33m"
C_GRN = "\033[32m"
C_CYA = "\033[36m"

_LEVEL_COLOR = {"crit": C_RED, "warn": C_YEL, "info": C_CYA}
_STATUS_COLOR = {
    "running": C_CYA, "ok": C_GRN, "due": C_YEL,
    "failed": C_RED, "orphaned": C_RED, "killed": C_DIM, "skipped": C_DIM,
}
# The work lane. `not-evaluated` is deliberately dim, not red: a run Otto lost, or
# that the API killed, has said nothing about the work, and painting it as a failure
# is the misread verdict.py exists to prevent.
_WORK_COLOR = {
    "running": C_CYA, "done": C_GRN, "due": C_YEL,
    "failed": C_RED, "not-evaluated": C_DIM, "skipped": C_DIM,
}


def _c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{C_RESET}"


def _cpad(text: str, width: int, color: str = "") -> str:
    """Pad first, then colorize.

    Colorizing before padding would count the ANSI escape bytes toward the field
    width and break every column downstream.
    """
    padded = f"{text:<{width}}"
    return _c(padded, color) if color else padded


def _age(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    s = delta.total_seconds()
    if s < 0:
        return "just now"
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def _banner(headline: str) -> None:
    print(_c(f"  {headline}", C_BOLD))


# ---- commands ---------------------------------------------------------------

def cmd_status(args, client: Client) -> int:
    try:
        st = client.state()
    except DaemonDown as e:
        print(_c(f"  [{config.PERSONA_NAME}] daemon DOWN", C_RED))
        print(f"  {e}")
        _offline_status()
        return 1

    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    only = args.domain
    _banner(st["headline"])
    print()

    alerts = [a for a in st["alerts"] if not only or a.get("domain") == only]
    if alerts:
        print(_c("  ALERTS", C_BOLD))
        for a in alerts[: args.limit]:
            color = _LEVEL_COLOR.get(a["level"], "")
            level = _cpad(a["level"].upper(), 5, color)
            dom = _cpad(a.get("domain", "work")[:4], 5, C_DIM)
            print(f"    {level}{dom}{a['source']:<28} {a['message']}")
        print()

    # Runs and schedules are grouped by domain so work and life never blur.
    domains = [only] if only else list(st.get("domains") or ["work", "personal"])

    for domain in domains:
        runs = [r for r in st["runs"] if r.get("domain", "work") == domain]
        scheds = [s for s in st["schedules"] if s.get("domain", "work") == domain]
        if not runs and not scheds and domain != "work":
            print(_c(f"  {domain.upper()}", C_BOLD)
                  + _c("   nothing yet - otto schedule add <name> --command ...", C_DIM))
            print()
            continue

        active = len([r for r in runs if r["status"] == "running"])
        head = _c(f"  {domain.upper()}", C_BOLD)
        print(head + _c(f"   {active} active" if active else "", C_DIM))

        for s in scheds:
            if s.get("stale"):
                mark, color, detail = "STALE", C_RED, s["stale"]
            elif s["due"]:
                mark, color, detail = "DUE", C_YEL, s["due_reason"]
            elif not s["enabled"]:
                mark, color, detail = "off", C_DIM, "disabled"
            else:
                mark, color, detail = "idle", C_DIM, s["due_reason"]
            print(f"    {_cpad(mark, 6, color)} {s['name']:<18} "
                  f"{_age(s['last_run']):>11}  {_c(detail, C_DIM)}")

        for r in runs[: args.limit]:
            status = _cpad(r["status"], 9, _STATUS_COLOR.get(r["status"], ""))
            usage = ""
            if r.get("output_tokens"):
                usage = _c(f"  {persona.thousands(r['output_tokens'])} out", C_DIM)
            print(f"    {persona.short(r['id']):<8}{r['name'][:18]:<20}{status}"
                  f"{_age(r['started']):>11}{usage}")
        print()

    _print_sessions_line(st.get("sessions") or {})
    _print_snapshots(st.get("snapshots") or {})

    if not only or only == "work":
        print(_c("  INTEGRATIONS", C_BOLD))
        if not st["integrations"]:
            print(_c("    (not probed yet - otto probe)", C_DIM))
        for i in st["integrations"]:
            mark, color = _integration_mark(i)
            print(f"    {_cpad(mark, 6, color)} {i['name']:<14} {_c(i['detail'], C_DIM)}")
        print()

    by_domain = st.get("registry_by_domain") or {}
    if by_domain:
        print(_c("  REGISTRY", C_BOLD))
        for domain in domains:
            summ = by_domain.get(domain) or {}
            parts = [f"{v} {k}s" for k, v in sorted(summ.items()) if k != "missing"]
            tail = f", {summ['missing']} missing" if summ.get("missing") else ""
            print(f"    {domain:<10} {', '.join(parts) or 'none'}{tail}")
    print(_c(f"\n  dashboard: {config.BASE_URL}", C_DIM))
    return 0


def _print_sessions_line(data: dict) -> None:
    """One line, and only when there is something to say.

    Deliberately not a list. `otto status` is the one-screen view, waiting sessions
    already come through as ALERTS above, and a per-session table here would push
    the day off the bottom of the screen to report that three terminals are idle.
    `otto sessions` is the list.
    """
    summary = data.get("summary") or {}
    if not summary.get("hooks_installed"):
        # Silent when nothing has ever reported: a machine that has not opted in
        # should not be nagged on every status call. `otto gaps` carries it.
        if not summary.get("total"):
            return
        print(_c("  SESSIONS", C_BOLD)
              + _c("   hooks not installed, this view is incomplete "
                   "- otto sessions install", C_YEL))
        print()
        return

    if not summary.get("live"):
        return

    bits = [f"{summary[k]} {k}" for k in ("busy", "waiting", "idle") if summary.get(k)]
    tail = (_c(f"   {summary['unspawned']} not spawned by Otto", C_DIM)
            if summary.get("unspawned") else "")
    print(_c("  SESSIONS", C_BOLD) + f"   {', '.join(bits)}" + tail)
    print()


def _print_snapshots(snaps: dict, domain: str | None = None) -> None:
    """The day: what is left of the calendar, and which mail wants a reply.

    Rendering lives in `today.py` rather than here so `otto status`, `otto agenda`,
    and the refresh wait path all show the same day rather than three variants of
    it. This function only adds colour, which is the one thing a module that might
    be piped into JSON should not decide.
    """
    if not snaps:
        return
    print(_c("  TODAY", C_BOLD))
    stale = [k for k, s in snaps.items()
             if (_hours_since(s.get("fetched_at")) or 0) > config.SNAPSHOT_STALE_HOURS]
    for line in _today.render(snaps, domain=domain):
        # Markers earn colour; the rest stays plain so the markers still stand out.
        stripped = line.lstrip()
        if stripped.startswith("REPLY"):
            print(_c("  " + line, C_YEL))
        elif stripped.startswith("! CLASH"):
            print(_c("  " + line, C_RED))
        elif "<- NOW" in line or "<- in " in line:
            print(_c("  " + line, C_BOLD))
        elif line and not line.startswith(" "):
            print(_c("  " + line, C_DIM))
        else:
            print("  " + line)
    if stale:
        print(_c(f"    STALE: {', '.join(sorted(stale))} older than "
                 f"{config.SNAPSHOT_STALE_HOURS}h - otto refresh", C_YEL))
    print()


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def _integration_mark(i: dict) -> tuple[str, str]:
    if i["ok"] and i["mode"] == "mcp-only":
        return "mcp", C_CYA
    if i["ok"]:
        return "ok", C_GRN
    if i["mode"] == "mcp-only":
        return "MCP?", C_YEL
    if i["mode"] == "unconfigured":
        return "unset", C_YEL
    return "DOWN", C_RED


def _offline_status() -> None:
    """Best-effort read straight off disk when the daemon is unreachable."""
    store = Store()
    scheds = store.schedules()
    if not scheds:
        print(_c("\n  no state on disk yet - run `otto serve` once to initialize", C_DIM))
        return
    print(_c("\n  last known schedule state (read from disk):", C_DIM))
    for s in scheds:
        st = scheduled.staleness(s)
        flag = _cpad("STALE", 6, C_RED) if st else _cpad("ok", 6, C_DIM)
        print(f"    {flag} {s.name:<18} {_age(s.last_run):>11}")


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


def cmd_reply(args, client: Client) -> int:
    """Post a thread reply as Otto. How a triage session speaks without the token."""
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to post an empty reply", file=sys.stderr)
        return 2
    try:
        res = client.slack_reply(args.channel, args.thread, text)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (channel allowlist, thread
        # required, unattended, no-send). Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  posted as Otto in {res['channel']} thread {args.thread}  ts {res['ts']}")
    return 0


def cmd_post(args, client: Client) -> int:
    """Post top-level in an allowlisted channel, as Otto. How the daily run speaks.

    Defaults to config.SLACK_CHANNEL_ID because that is the one feed this exists for;
    naming another channel is possible but has to be deliberate, and the daemon will
    still refuse anything outside config.POST_CHANNELS.
    """
    if not args.channel:
        # An empty id is "unconfigured", never a channel. Say so here rather than
        # let the daemon turn it into a Slack API error about a blank argument.
        print("  no channel: pass --channel or set OTTO_SLACK_CHANNEL_ID", file=sys.stderr)
        return 2
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to post an empty message", file=sys.stderr)
        return 2
    try:
        res = client.slack_post(args.channel, text)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (channel allowlist, no-send).
        # Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  posted as Otto in {res['channel']}  ts {res['ts']}")
    return 0


def cmd_tell(args, client: Client) -> int:
    """DM the owner as Otto. Cannot be pointed at anybody else."""
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to send an empty message", file=sys.stderr)
        return 2
    try:
        res = client.slack_tell(text)
    except RuntimeError as e:
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  told {config.OWNER_NAME}, as Otto  ({res['channel']} ts {res['ts']})")
    return 0


def cmd_dm(args, client: Client) -> int:
    """Group DM a colleague and the owner, as Otto. For messages the owner asked for."""
    import os
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to send an empty message", file=sys.stderr)
        return 2
    try:
        res = client.slack_dm(args.who, text, why=args.why or "",
                              run_id=os.environ.get("OTTO_RUN_ID"),
                              task_id=args.task)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (roster, per-run brake, unattended,
        # no-send). Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  sent as Otto to {res['to']} (group DM with {config.OWNER_NAME})  id {res['id'][:8]}")
    return 0


def cmd_priorities(args, client: Client) -> int:
    """What matters right now. Local read: no daemon needed to answer it."""
    from . import priorities
    if args.init:
        path = config.PRIORITIES_PATH
        if path.exists() and not args.force:
            print(f"  {path} already exists; --force to overwrite", file=sys.stderr)
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            # Local date, not UTC: this is a date a human writes and reads, and a
            # UTC stamp reads as tomorrow for anyone west of Greenwich.
            priorities.TEMPLATE.format(today=datetime.now().date().isoformat()),
            encoding="utf-8")
        print(f"  wrote {path}")
        print("  edit it, then `otto priorities` to check")
        return 0
    if args.json:
        print(json.dumps(priorities.status(), indent=2))
        return 0
    print(priorities.render())
    for g in priorities.gaps():
        print()
        print(f"  {_c(g['title'], C_YEL)}")
        print(f"  {g['why']}")
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
        tier=args.tier, skip_permissions=not args.safe, domain=args.domain,
        model=args.model,
    )
    print(f"  spawned {run['name']}  run {persona.short(run['id'])}  pid {run['pid']}")
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


def cmd_schedules(args, client: Client) -> int:
    scheds = client.schedules()
    if args.json:
        print(json.dumps(scheds, indent=2))
        return 0
    for s in scheds:
        mark = _cpad("DUE", 5, C_YEL) if s["due"] else _cpad("idle", 5, C_DIM)
        state = "" if s["enabled"] else _c(" (disabled)", C_DIM)
        print(f"  {mark} {s['name']:<18} {s['command'][:30]:<31}"
              f"{_age(s['last_run']):>11}  {_c(s['due_reason'], C_DIM)}{state}")
    return 0


def cmd_due(args, client: Client) -> int:
    """Machine-readable list of what should run now. For /daily and loops."""
    due = [s for s in client.schedules() if s["due"]]
    if args.domain:
        due = [s for s in due if s.get("domain", "work") == args.domain]
    if args.json:
        print(json.dumps(due, indent=2))
        return 0
    for s in due:
        print(s["command"])
    return 0 if due else 1


_MANGLED = ("/Git/", "\\Git\\", "/usr/bin/", "Program Files/Git")


def cmd_schedule_add(args, client: Client) -> int:
    # Git Bash rewrites a leading-slash argument into a Windows path, so
    # `--command "/review"` silently arrives as "C:/Program Files/Git/review".
    # The mangling happens before Python sees it, so all Otto can do is notice
    # the signature and refuse to store the wrong thing quietly.
    if any(m in args.command for m in _MANGLED):
        print(_c(f"  that command looks path-mangled by the shell: {args.command}", C_YEL),
              file=sys.stderr)
        print("  if you meant a slash command, use PowerShell, or prefix with "
              "MSYS_NO_PATHCONV=1", file=sys.stderr)
        if not args.force:
            print("  refusing to store it. Pass --force to override.", file=sys.stderr)
            return 2

    days = [d.strip() for d in (args.days or "").split(",") if d.strip()]
    if args.kind == "weekly" and not days:
        print("  weekly needs --days (e.g. --days sun or --days mon,thu)", file=sys.stderr)
        return 2
    if args.kind == "every" and not args.hours:
        print("  every needs --hours N", file=sys.stderr)
        return 2
    sched = client.put_schedule(
        args.name,
        command=args.command,
        domain=args.domain,
        description=args.description,
        kind=args.kind,
        at=args.at,
        days=days,
        hours=args.hours,
        min_interval_days=args.min_interval_days,
        max_age_hours=args.max_age_hours,
        enabled=not args.disabled,
    )
    cad = sched["cadence"]
    when = {
        "daily": f"daily at {cad['at']}",
        "weekly": f"{','.join(cad['days'])} at {cad['at']}",
        "every": f"every {cad['hours']}h",
        "manual": "manual only",
    }.get(cad["kind"], cad["kind"])
    print(f"  {sched['name']} ({sched['domain']}) -> {when}")
    if sched.get("max_age_hours"):
        print(f"  alarms if it has not run in {sched['max_age_hours']}h")
    print(f"  command: {sched['command']}")
    return 0


def cmd_schedule_rm(args, client: Client) -> int:
    client.delete_schedule(args.name)
    print(f"  removed {args.name}")
    return 0


def cmd_agenda(args, client: Client) -> int:
    """Show, or ingest, the calendar/mail snapshots.

    Otto has no MCP access from the daemon, so a Claude session pushes these in.
    `--push` reads a JSON body on stdin.
    """
    if args.push:
        raw = sys.stdin.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  stdin is not valid JSON: {e}", file=sys.stderr)
            return 2
        snap = client.put_snapshot(
            args.push,
            summary=body.get("summary"),
            items=body.get("items") or [],
            source=body.get("source", "claude-session"),
            domain=args.domain or body.get("domain") or config.WORK,
        )
        print(f"  {snap['domain']}/{snap['kind']} updated: "
              f"{len(snap['items'])} item(s) @ {snap['fetched_at']}")
        return 0

    snaps = client.snapshots()
    if args.json:
        print(json.dumps(snaps, indent=2))
        return 0
    if not snaps:
        print("  nothing pushed yet. Run /otto in a Claude session to refresh"
              " calendar and mail.")
        return 0
    _print_snapshots(snaps, domain=args.domain)
    return 0


def cmd_machine(args, client: Client) -> int:
    stats = client.machine()
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(_c("  THIS MACHINE", C_BOLD) + _c("  (informational, not action items)", C_DIM))
    for m in stats:
        print(f"    {m['label']:<14} {m['value']:<16} {_c(m.get('detail') or '', C_DIM)}")
    return 0


def cmd_stamp(args, client: Client) -> int:
    sched = client.stamp(args.name, args.status, args.run_id)
    print(f"  stamped {sched['name']} @ {sched['last_run']} ({sched['last_status']})")
    return 0


def cmd_toggle(args, client: Client) -> int:
    sched = client.toggle(args.name, not args.off)
    print(f"  {sched['name']} enabled={sched['enabled']}")
    return 0


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


def cmd_events(args, client: Client) -> int:
    for e in client.events(args.limit):
        level = _cpad(e["level"], 5, _LEVEL_COLOR.get(e["level"], ""))
        print(f"  {e['at']}  {level} {e['source']:<12} {e['message']}")
    return 0



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


class DetailError(Exception):
    """--detail and --detail-file disagree, or the file will not read."""


def _resolve_detail(args) -> str | None:
    """Detail text, from --detail or --detail-file (- for stdin).

    Evidence-carrying detail belongs in a file. A detail string quoting a
    command line goes on *our* command line, where the EDR reads it as the
    thing it describes: on 2026-08-17 three `otto task` calls filing the
    certutil FP were themselves blocked by the EDR for containing the word
    certutil. A file path is inert, so the write lands the first time.
    """
    path = getattr(args, "detail_file", None)
    if path is None:
        return args.detail
    if args.detail is not None:
        raise DetailError("pass --detail or --detail-file, not both")
    # utf-8-sig, not utf-8: PowerShell 5.1 writes a BOM by default, and str.strip()
    # does not treat one as whitespace, so it would ride along into the detail text.
    if path == "-":
        return sys.stdin.read().lstrip("﻿").strip() or None
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    except OSError as e:
        raise DetailError(f"cannot read {path}: {e}") from e


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
        from .models import iso, utcnow
        stamp = iso(utcnow())[:16].replace("T", " ")
        detail = "\n\n".join(x for x in (
            (cur.get("detail") or "").rstrip(),
            f"--- appended {stamp} UTC ---\n{detail.strip()}",
        ) if x)
    changes = {k: v for k, v in (
        ("title", args.title), ("priority", args.priority),
        ("domain", args.domain), ("detail", detail),
        ("agent", args.agent), ("due", args.due),
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


def _read_text_arg(path: str | None, inline: str | None) -> str | None:
    if path is None:
        return inline
    if inline is not None:
        raise DetailError("pass the text or --file, not both")
    if path == "-":
        return sys.stdin.read().lstrip("\ufeff").strip() or None
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    except OSError as e:
        raise DetailError(f"cannot read {path}: {e}") from e


def cmd_task_plan(args, client: Client) -> int:
    """Read, write, approve, or withdraw a card's plan.

    The plan is the prompt a run will follow, in the owner's hands before it runs.
    `--file` replaces it (edit the file, not the flags); `--approve` stamps it;
    `--withdraw` clears the stamp so the next promote is refused again.
    """
    from .models import iso, utcnow

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
    from . import dispatch as _dispatch
    from .models import Task

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
        skip_permissions=False, domain=task.domain, model=args.model,
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


# ---- backlog triage ---------------------------------------------------------

def _tasks_as_models(client: Client) -> list:
    from .models import Task
    out = []
    for raw in client.tasks(None):
        try:
            out.append(Task.model_validate(raw))
        except ValueError:
            continue
    return out


def cmd_triage_list(args, client: Client) -> int:
    """Cards the triage pass has not judged yet, oldest first."""
    from . import backlog as _backlog

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
    from .models import iso, utcnow

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
    from . import backlog as _backlog

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
    from .models import iso, utcnow

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
    from . import backlog as _backlog

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
    from . import backlog as _backlog

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




_BAND_COLOR = {"today": C_RED, "this week": C_YEL, "sometime": C_DIM}





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



def cmd_autorun(args, client: Client) -> int:
    """Unattended cron: the master switch, what is armed, what tripped."""
    if args.state is not None:
        st = client.set_autorun(args.state == "on")
    else:
        st = client.autorun()
    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    flag = _c("ON", C_GRN) if st["enabled"] else _c("OFF", C_YEL)
    print(f"  autorun  {flag}")
    print(_c(f"  concurrency {st['max_concurrent']}  |  breaker at {st['max_failures']} "
             f"failures  |  {st['min_gap_minutes']}m floor  |  "
             f"${st['budget_usd']:.0f} per-run cap", C_DIM))
    print()
    if not st["armed"]:
        print(_c("  nothing armed. Arm one with: otto schedule arm <name>", C_DIM))
    else:
        print(_c("  ARMED", C_BOLD))
        for a in st["armed"]:
            cad = a["cadence"]
            when = {"daily": f"daily {cad['at']}",
                    "weekly": f"{','.join(cad['days'])} {cad['at']}",
                    "every": f"every {cad['hours']}h"}.get(cad["kind"], cad["kind"])
            print(f"    {a['name']:<16} {a['runner']:<8} {when:<16} "
                  f"{_c(a['command'][:34], C_DIM)}")
            if a["consecutive_failures"]:
                print(_c(f"      {a['consecutive_failures']} consecutive failure(s)", C_YEL))
    if st.get("blocked"):
        print()
        print(_c("  DUE BUT BLOCKED", C_YEL))
        for b in st["blocked"]:
            print(f"    {b['name']:<16} {b['reason']}")
    if st["tripped"]:
        print()
        print(_c("  CIRCUIT BREAKER TRIPPED", C_RED))
        for t in st["tripped"]:
            print(f"    {t['name']:<16} {t['reason']}")
        print(_c("    re-arm with: otto schedule arm <name>", C_DIM))
    if not st["enabled"]:
        print()
        print(_c("  master switch is off, so nothing will run unattended", C_DIM))
    return 0


def cmd_schedule_arm(args, client: Client) -> int:
    """Arm a schedule to run unattended on its cadence."""
    armed = not args.off
    if armed:
        scheds = {x["name"]: x for x in client.schedules()}
        sched = scheds.get(args.name)
        if sched is None:
            print(f"  no schedule named {args.name}", file=sys.stderr)
            return 1
        slash = (sched["command"] or "").strip().startswith("/")
        what = ("a Claude Code session with --dangerously-skip-permissions"
                if slash else "a shell command")
        print(f"  arming {args.name}: {sched['command']}")
        print(_c(f"  this will run unattended, on cadence, as {what}", C_YEL))
    s = client.arm_schedule(args.name, armed)
    print(f"  {s['name']}: autostart={s['autostart']} runner={s['runner']}")
    return 0


def cmd_launch(args, client: Client) -> int:
    """Run a schedule now. Slash commands get a session; shell commands run direct."""
    try:
        r = client.run_schedule(args.name)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    run = r["run"]
    print(f"  {r['message']}")
    print(f"  watch: otto watch {persona.short(run['id'])}")
    if args.watch:
        args.run_id = run["id"]
        args.follow = True
        return cmd_watch(args, client)
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


_SESSION_COLOR = {"busy": C_YEL, "waiting": C_CYA, "idle": C_GRN, "offline": C_DIM}


def cmd_sessions(args, client: Client) -> int:
    """Claude Code sessions on this box, as their own hooks report them.

    `install` and `uninstall` are local file writes and deliberately do NOT need
    the daemon: the first thing you do on a fresh machine is install the hooks, and
    requiring a running daemon to do that would be a needless ordering constraint.
    """
    from . import sessions as _sessions

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
    from . import herdr as _herdr

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
    from . import herdr as _herdr

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


def cmd_people_note(args, client: Client) -> int:
    """Append a note to a dossier section.

    Exists because `otto/triage.py` tells a spawned session to call it, and a verb
    a prompt depends on must be a real command rather than a shape the model has to
    guess at. Useful by hand for the same reason.
    """
    text = " ".join(args.text).strip()
    if not text:
        print(_c("  nothing to write", C_RED), file=sys.stderr)
        return 1
    try:
        client.add_person_note(args.slug, args.section, text)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    print(f"  {args.slug}: note added to {args.section}")
    return 0


def cmd_outreach_resolve(args, client: Client) -> int:
    """Record a colleague's Slack member id so outreach can address them.

    Local, not through the daemon: it spawns a lookup session and writes a dossier
    field, and it must work before the daemon is trusted to send anything.
    """
    from . import outreach as _o
    from . import people as _people

    target = args.target
    existing = _o.slack_id_for(target)
    if existing and not args.force:
        print(f"  {target} already resolved: {existing}")
        return 0

    print(f"  looking up {target} in Slack...")
    sid, err = _o.resolve_slack_id(target)
    if not sid:
        print(_c(f"  {err}", C_RED), file=sys.stderr)
        return 1
    p = _people.get(target)
    if not p:
        print(_c(f"  no dossier for {target}, cannot record {sid}", C_RED), file=sys.stderr)
        return 1
    _people.set_meta(p["slug"], {"slack_id": sid})
    print(f"  {target} -> {sid}  (recorded on {p['slug']}.md)")
    return 0


def cmd_outreach_compose(args, client: Client) -> int:
    """Compose a HELD message. Never sends: the hold is the whole point."""
    try:
        o = client.compose_outreach(to=args.to, body=args.body, why=args.why,
                                    source=args.source, channel=args.channel)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    mins = o.get("hold_minutes")
    print(f"  held for {o.get('to')}: {persona.short(o.get('id',''))}")
    print(_c(f"  sends in {mins}m unless killed - otto outreach --kill "
             f"{persona.short(o.get('id',''))}", C_DIM))
    return 0


def cmd_threads(args, client: Client) -> int:
    """Every dated dossier thread, the whole list.

    The threads-quiet notice lists eight and counts the rest, which is right for a
    notice and useless for deciding. This is the list you act from.
    """
    data = client.threads(args.band)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    rows, counts = data["rows"], data["counts"]
    b = data["bands"]
    print()
    print(_c("  THREADS", C_BOLD)
          + f"  {counts.get('quiet',0)} quiet ({b['stale_days']}-{b['max_days']}d)"
          + f", {counts.get('ancient',0)} over {b['max_days']}d"
          + f", {counts.get('fresh',0)} fresh")
    print()
    last = None
    for r in rows:
        if r["band"] != last:
            last = r["band"]
            print(_c(f"  {last.upper()}", C_BOLD))
        color = {"ancient": C_RED, "quiet": C_YEL}.get(r["band"], C_DIM)
        print(f"    {_cpad(str(r['days']) + 'd', 6, color)}"
              f"{_cpad(r['who'][:22], 24)}{_c(r['when'], C_DIM)}")
        print(f"          {r['text'][:96]}")
        for n in r.get("notes") or []:
            print(_c(f"          note {n['at'][:10]}: {n['note'][:80]}", C_CYA))
    print()
    return 0


def cmd_thread_note(args, client: Client) -> int:
    """Write a note against one thread and let Otto decide what to do with it."""
    note = " ".join(args.note).strip()
    if not note:
        print(_c("  nothing to send", C_RED), file=sys.stderr)
        return 1
    try:
        res = client.thread_note(args.thread_id, note)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    run = res["run"]
    print(f"  sent to Otto  run {persona.short(run['id'])}  pid {run.get('pid')}")
    print(_c(f"  otto watch {persona.short(run['id'])} -f", C_DIM))
    return 0


def cmd_thread_update(args, client: Client) -> int:
    """Rewrite a thread line in place. What actually closes a thread."""
    text = " ".join(args.text).strip()
    if not text:
        print(_c("  nothing to write", C_RED), file=sys.stderr)
        return 1
    try:
        res = client.resolve_thread(args.thread_id, text)
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    print(f"  {res['slug']}: thread updated")
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


def cmd_task_run(args, client: Client) -> int:
    """Dispatch a task now, regardless of the queue's pace."""
    try:
        r = client.dispatch_task(args.task_id, force=args.force)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    run = r["run"]
    print(f"  {r['message']}")
    print(f"  follow: otto logs {persona.short(run['id'])}")
    return 0


def cmd_autodispatch(args, client: Client) -> int:
    if args.state is None:
        st = client.dispatch_status()
    else:
        st = client.set_dispatch(args.state == "on")
    flag = _c("ON", C_GRN) if st["enabled"] else _c("OFF", C_YEL)
    print(f"  auto-dispatch  {flag}")
    print(f"  running {st['running']}/{st['max_concurrent']}"
          f"  eligible {st['eligible']}"
          f"  max attempts {st['max_attempts']}")
    if st["enabled"]:
        print(_c("  a task moved to `queued` is spawned with skip-permissions", C_DIM))
    else:
        print(_c("  `queued` is inert; nothing will be spawned", C_DIM))
    return 0


def cmd_next(args, client: Client) -> int:
    """What to do now, ranked. Deterministic, so it costs nothing and never varies."""
    b = client.briefing(args.domain)
    rows = b["next"]
    if args.json:
        print(json.dumps(b, indent=2))
        return 0
    if not rows:
        print(f"  nothing to do. {config.PERSONA_NAME} has no suggestions.")
        return 0

    band = None
    for r in rows:
        if r["band"] != band:
            band = r["band"]
            print()
            print(_c(f"  {band.upper()}", _BAND_COLOR.get(band, C_DIM)))
        dom = _cpad(r["domain"][:4], 5, C_DIM)
        print(f"    {dom}{_c(r['title'], C_BOLD)}")
        print(f"         {_c(r['why'], C_DIM)}")
        if r.get("command"):
            print(f"         {_c('$ ' + r['command'], C_CYA)}")
    print()
    return 0


def cmd_gaps(args, client: Client) -> int:
    """Blind spots: things with no watcher, as opposed to things that broke."""
    rows = client.gaps(args.domain)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  no gaps found. Everything is watched.")
        return 0
    print(_c(f"  {len(rows)} GAP(S)", C_BOLD)
          + _c("   things nothing is currently watching", C_DIM))
    for g in rows:
        print()
        print(f"    {_cpad(g['domain'][:4], 5, C_DIM)}{_c(g['title'], C_BOLD)}"
              + _c(f"  [{g['kind']}]", C_DIM))
        print(f"         {_c(g['why'], C_DIM)}")
        if g.get("command"):
            print(f"         {_c('$ ' + g['command'], C_CYA)}")
    print()
    return 0


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
        print(f"  refreshing {r['domain']} (run {persona.short(r['id'])}, pid {r['pid']})")

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


def _due_label(due: str | None) -> tuple[str, str]:
    """(text, colour) for a due date. Overdue reads as overdue, not as a date."""
    if not due:
        return "", C_DIM
    try:
        d = datetime.fromisoformat(due[:10]).date()
    except ValueError:
        return str(due)[:10], C_DIM
    days = (d - datetime.now().astimezone().date()).days
    if days < 0:
        return f"OVERDUE {abs(days)}d", C_RED
    if days == 0:
        return "due today", C_RED
    if days == 1:
        return "due tomorrow", C_YEL
    if days <= 7:
        return f"due {d.isoformat()}", C_YEL
    return f"due {d.isoformat()}", C_DIM


def cmd_meetings(args, client: Client) -> int:
    """What Otto has taken off your meeting notes, and what it owes you.

    `otto meetings` reports; `otto meetings ingest` reads Notion now rather than
    waiting for the four-hourly schedule.
    """
    if args.meetings_cmd == "ingest":
        try:
            res = client.ingest_meetings()
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run = res.get("run") or {}
        print(f"  reading Notion meeting notes (run {persona.short(run.get('id', ''))}, "
              f"pid {run.get('pid')})")
        if args.no_wait:
            print(_c("  check back with: otto meetings", C_DIM))
            return 0

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(3)
            if client.run(run["id"])["status"] != "running":
                break
        else:
            print(_c(f"  still running after {args.timeout}s. Check: otto meetings", C_DIM))
            return 0
        # Fall through to the report, which is the only place the outcome is visible.

    try:
        st = client.meetings()
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    sched = st.get("schedule") or {}
    cadence = (sched.get("cadence") or {})
    every = f"every {cadence['hours']}h" if cadence.get("hours") else cadence.get("kind", "?")
    armed = "on" if sched.get("autostart") and sched.get("enabled") else _c("OFF", C_YEL)
    print()
    print(f"  {_c('meeting notes', C_BOLD)}   {every}, autostart {armed}")
    last = st.get("last_ingest")
    print(f"    last read     {_age(last) if last else _c('never', C_YEL)}"
          + (f"   {_c(st['last_summary'], C_DIM)}" if st.get("last_summary") else ""))
    wm = st.get("watermark") or "none"
    print(f"    pages read    {st.get('pages_seen', 0)} {_c(f'(watermark {wm})', C_DIM)}")
    # "taken", not "filed": a restated commitment bumps an existing card rather than
    # creating one, so this counts items processed and the open count is the board.
    print(f"    items taken   {st.get('filed_total', 0)} total, "
          f"{st.get('open_tasks', 0)} cards still open")
    if not st.get("autoqueue"):
        print(_c("    action items land in backlog and wait for /orchestrate "
                 "(autoqueue off)", C_DIM))

    pages = st.get("recent_pages") or []
    if pages:
        print()
        print(f"  {_c('MEETINGS READ', C_DIM)}")
        for p in pages:
            n = p.get("filed") or 0
            print(f"    {_c(str(p.get('at') or '?'), C_DIM)}  {str(p.get('title') or '?')[:52]:<52} "
                  f"{_c(f'{n} item(s)' if n else 'nothing to do', C_DIM if n else C_GRN)}")

    open_tasks = [t for t in (st.get("tasks") or []) if t["status"] != "done"]
    if open_tasks:
        print()
        print(f"  {_c('ON THE BOARD', C_DIM)}")
        for t in open_tasks:
            text, col = _due_label(t.get("due"))
            print(f"    {_c(t['id'][:6], C_DIM)} {_cpad(t['status'], 10, C_DIM)} "
                  f"{t['title'][:52]:<52} {_c(text or 'no date', col)}")
            print(f"           {_c(t.get('tier') or 'no tier', C_DIM)}"
                  + (_c(f"  seen {t['seen_count']}x", C_YEL) if t.get("seen_count", 1) > 1 else ""))
    print()
    return 0


def cmd_outreach(args, client: Client) -> int:
    """The ledger of messages Otto sent, or was stopped from sending, to colleagues."""
    data = client.outreach(args.state)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    s = data["summary"]
    on = s["enabled"]
    print(_c("  OUTREACH", C_BOLD)
          + _c("   own-initiative sending " + ("ON" if on else "OFF, composed and held only")
               + "; directed (otto dm) always sends",
               C_YEL if on else C_DIM))
    if on and not s["can_send"]:
        print(_c(f"  enabled but no {'OTTO_SLACK_BOT_TOKEN'}: every send will fail",
                 C_RED))
    print(_c(f"  hold {s['hold_minutes']}m   sent 24h {s['sent_24h']}/{s['max_per_day']}"
             f"   held {s['held']}", C_DIM))
    items = data["items"]
    if not items:
        print("\n  nothing. Otto has not composed a message to anybody.")
        return 0
    for o in items[: args.limit]:
        col = {"held": C_YEL, "sent": "", "failed": C_RED}.get(o["state"], C_DIM)
        print()
        print(f"  {o['id'][:6]}  " + _cpad(o["state"], 8, col)
              + f"{o['to'][:26]:<27}{o['at'][5:16]}")
        for chunk in textwrap.wrap(o["body"], width=86)[:4]:
            print(f"          {chunk}")
        print(_c(f"          why: {o['why'][:80]}", C_DIM))
        if o["state"] == "held":
            print(_c(f"          sends {o['send_after'][11:16]}Z   "
                     f"otto outreach --kill {o['id'][:6]}", C_YEL))
        if o.get("error"):
            print(_c(f"          {o['error'][:80]}", C_DIM))
    return 0


def cmd_outreach_act(args, client: Client) -> int:
    if getattr(args, "extend", None):
        o = client.extend_outreach(args.extend, args.minutes)
        print(f"  extended {args.minutes}m: to {o['to']} / {o['body'][:60]}")
        print(_c(f"  now sends {o['send_after'][11:16]}Z   "
                 f"otto outreach --kill {o['id'][:6]}", C_YEL))
        return 0
    o = (client.kill_outreach(args.kill) if args.kill
         else client.send_outreach(args.send))
    verb = "killed" if args.kill else o["state"]
    print(f"  {verb}: to {o['to']} / {o['body'][:60]}")
    if o.get("error"):
        print(_c(f"  {o['error']}", C_YEL))
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


def cmd_notices(args, client: Client) -> int:
    """Messages Otto sent the owner. Durable, unlike the toast that announced them."""
    try:
        rows = client.notices(unread_only=args.unread)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  nothing from Otto." if args.unread else "  no notices.")
        return 0
    for n in rows[: args.limit]:
        col = {"crit": C_RED, "warn": C_YEL}.get(n["level"], C_DIM)
        mark = " " if n.get("read_at") else _c("*", col)
        seen = (_c(f" seen {n['seen_count']}x", C_YEL)
                if (n.get("seen_count") or 1) > 1 else "")
        print(f"  {mark} {_c(n['id'][:6], C_DIM)} {_c(n['level'][:4].upper(), col)} "
              f"{_c(_age(n['at']), C_DIM)}  {n['title']}{seen}")
        if n.get("body"):
            for line in str(n["body"]).splitlines():
                print(f"        {_c(line, C_DIM)}")
        if n.get("command"):
            print(f"        {_c(n['command'], C_DIM)}")
    unread = len([n for n in rows if not n.get("read_at")])
    if unread and not args.read:
        print()
        print(_c(f"  {unread} unread. otto notices --read to clear.", C_DIM))
    if args.read:
        for n in rows:
            if not n.get("read_at"):
                client.read_notice(n["id"])
        print(_c("  marked read", C_DIM))
    return 0


def cmd_notify(args, client: Client) -> int:
    """Send the owner a message. For agents and schedules, not usually typed by hand."""
    try:
        n = client.notify(" ".join(args.title), body=args.body, level=args.level,
                          domain=args.domain or config.WORK, source=args.source,
                          command=args.command)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    print(f"  sent {n['id'][:6]} [{n['level']}] {n['title'][:60]}")
    return 0


def cmd_day(args, client: Client) -> int:
    """Where the day actually went, plus whatever the owner reported about it.

    The rollup is mechanical: read from Claude Code's own transcripts, no model, no
    cost. Two numbers are reported and they are not interchangeable. `wall` is real
    elapsed engaged time (union of activity windows, idle stripped). `attention` sums
    across sessions and can exceed wall clock because concurrent sessions are real
    work, not double counting. Reporting only the larger would be flattering nonsense.
    """
    day = args.date or datetime.now().astimezone().date().isoformat()
    try:
        rec = client.day(day, refresh=args.refresh)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1

    roll = rec.get("rollup") or {}
    ci = rec.get("checkin") or {}
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0

    print(_c(f"  {day}", C_BOLD))
    if not roll.get("session_count"):
        print("    no sessions recorded")
    else:
        wall = roll.get("wall_minutes") or 0
        att = roll.get("attention_minutes") or 0
        print(f"    {roll['session_count']} sessions   "
              f"{wall // 60}h{wall % 60:02d}m engaged   "
              f"{_c(f'{att // 60}h{att % 60:02d}m attention', C_DIM)}   "
              f"{_c(str(roll.get('first_activity')) + ' - ' + str(roll.get('last_activity')), C_DIM)}")
        print()
        for proj, mins in (roll.get("by_project") or {}).items():
            if mins < 5:
                continue
            bar = "#" * min(int(mins / 10), 40)
            print(f"    {mins:>4}m  {proj:<34} {_c(bar, C_DIM)}")
        longs = roll.get("long_sessions") or []
        if longs:
            print()
            print(_c("    longest, with what you said you were doing:", C_DIM))
            for s in longs[:5]:
                # split()/join collapses newlines and runs of spaces in one go, so a
                # multi-line prompt renders as one readable line.
                intent = " ".join((s.get("intent") or "").split())[:78]
                print(f"    {s['minutes']:>4}m  {s.get('title') or '(untitled)'}")
                print(f"          {_c(intent, C_DIM)}")

    print()
    if ci:
        e = f"energy {ci['energy']}/5   " if ci.get("energy") else ""
        print(f"  {_c('check-in', C_BOLD)}  {_c(e, C_DIM)}{_c(str(ci.get('at'))[:16], C_DIM)}")
        for line in (ci.get("note") or "").splitlines():
            print(f"    {line}")
    else:
        print(_c("  no check-in yet:  otto checkin \"...\" [--energy 1-5]", C_DIM))
    return 0


def _ask_signals() -> dict:
    """The guided walk. Enter skips, so it can be abandoned halfway and still count.

    Skipping has to be the cheapest key on the keyboard. A walk that punishes you for
    not knowing is a walk you stop running, and an unanswered signal is recorded as
    unknown rather than as a no, so half a check-in is still honest data.
    """
    from . import wellbeing as _w

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
    from . import wellbeing as _w

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
    from . import wellbeing as _w

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


def cmd_config(args, client: Client) -> int:
    """Inspect and reconcile the config Otto owns.

    Runs locally: it reads the filesystem, so it works with the daemon down.
    """
    from . import configsync

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

    from .daemon import _running_daemon_pid
    stale = _running_daemon_pid()
    if stale:
        # A pidfile pointing at a live python that is not answering HTTP: wedged.
        # Say so rather than starting a second one that will fight for the port.
        print(_c(f"  pid {stale} is alive but not answering on {config.BASE_URL}", C_YEL),
              file=sys.stderr)
        print("  stop it with: otto stop", file=sys.stderr)
        return 1

    import subprocess
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
    from . import herdr as _herdr
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
    from .daemon import stop_daemon

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
    from . import sessions as _sessions
    from . import settings as _settings
    from . import setup as _setup

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
    from .daemon import serve
    host = args.host or config.HOST
    port = args.port or config.PORT
    print(f"  {config.PERSONA_NAME} serving on http://{host}:{port}")
    serve(args.host, args.port, args.force)
    return 0


def cmd_manifest(args, client: Client) -> int:
    """Declared-vs-observed over the capability surface. Purely local: it reads
    registry keys and filenames off disk, so it works with the daemon down."""
    from . import manifest as _manifest
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
    from . import identity as _identity
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
    from . import feeds as _feeds

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
    from . import decisions as _d

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
    from . import decisions as _d

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


def cmd_writing(args, client: Client) -> int:
    """Post ideas from the week, drafts in the owner's voice, and what went out.

    Reads are local so the list works with the daemon down. Everything that
    starts a session or changes a post goes through the daemon.
    """
    from . import writing as _w

    sub = args.writing_cmd

    if sub == "voice":
        p = _w.ensure_voice()
        print(f"  rules    {p}")
        if config.voice_text() == _w.VOICE_SEED.strip():
            print(_c("           still the seed. Otto reads this file before every draft; make it yours.", C_DIM))
        print(f"  samples  {config.WRITING_SAMPLES_PATH}")
        if not config.samples_text():
            print(_c("           empty. Paste in things you wrote and were happy with. This file, not the "
                     "rules, is what makes a draft sound like you.", C_DIM))
        return 0

    if sub == "ideas":
        try:
            res = client.writing_ideas()
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run = res.get("run") or {}
        print(f"  mining the last {config.WRITING_LOOKBACK_DAYS} days for post ideas "
              f"(run {persona.short(run.get('id', ''))}, pid {run.get('pid')})")
        if args.no_wait:
            print(_c("  check back with: otto writing", C_DIM))
            return 0
        if not _wait_run(client, run["id"], args.timeout):
            print(_c(f"  still running after {args.timeout}s. Check: otto writing", C_DIM))
            return 0
        print()
        print(_w.render(Store()))
        return 0

    if sub == "edit":
        # Round-trip the draft through an editor, then store it as HIS text. The
        # next `draft` run is told the previous draft was his and keeps his changes.
        import os
        import subprocess
        import tempfile

        post = Store().get_post(args.id)
        if post is None:
            print(f"  no post {args.id}", file=sys.stderr)
            return 1
        if not post.draft:
            print(_c(f"  {post.id} has no draft yet: otto writing draft {post.id}", C_YEL),
                  file=sys.stderr)
            return 1
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "notepad"
        fd, tmp = tempfile.mkstemp(prefix=f"otto-{post.id}-", suffix=".md")
        os.close(fd)
        Path(tmp).write_text(post.draft + "\n", encoding="utf-8")
        try:
            subprocess.run([*editor.split(), tmp], check=False)
            text = Path(tmp).read_text(encoding="utf-8").strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        if not text or text == post.draft.strip():
            print(_c("  unchanged", C_DIM))
            return 0
        try:
            updated = client.patch_post(post.id, draft=text)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        n = len(updated.get("flags") or [])
        print(f"  {updated['id']} saved as your edit, {n} flag(s)")
        print(_c(f"  send it back for another pass: otto writing draft {updated['id']}"
                 " --note \"...\"", C_DIM))
        return 0

    if sub == "draft":
        if args.from_file:
            # His edited text becomes the current draft first, so the run sees it
            # as his and builds on it rather than on whatever it wrote last time.
            try:
                text = Path(args.from_file).read_text(encoding="utf-8")
            except OSError as e:
                print(_c(f"  could not read {args.from_file}: {e}", C_RED), file=sys.stderr)
                return 2
            try:
                client.patch_post(args.id, draft=text)
            except RuntimeError as e:
                print(_c(f"  {e}", C_YEL), file=sys.stderr)
                return 1
        try:
            res = client.writing_draft(args.id, args.note)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run, post = res.get("run") or {}, res.get("post") or {}
        verb = "redrafting" if post.get("versions") or post.get("draft") else "drafting"
        print(f"  {verb} {post.get('id')}: {post.get('hook', '')[:70]}")
        print(_c(f"  run {persona.short(run.get('id', ''))}, model {run.get('model') or 'default'}", C_DIM))
        if args.no_wait:
            print(_c(f"  check back with: otto writing show {post.get('id')}", C_DIM))
            return 0
        if not _wait_run(client, run["id"], args.timeout):
            print(_c(f"  still running after {args.timeout}s. Check: otto writing show {post.get('id')}", C_DIM))
            return 0
        fresh = Store().get_post(post["id"])
        if fresh is None:
            print(_c("  the post vanished mid-draft", C_RED), file=sys.stderr)
            return 1
        print()
        print(_w.render_one(fresh))
        return 0 if fresh.status == "drafted" and not fresh.error else 1

    if sub == "show":
        post = Store().get_post(args.id)
        if post is None:
            print(f"  no post {args.id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(post.model_dump(), indent=2))
        else:
            print(_w.render_one(post))
        return 0

    if sub == "set":
        payload: dict = {}
        if args.status:
            payload["status"] = args.status
        if args.url is not None:
            payload["url"] = args.url
        if args.note:
            payload["note"] = " ".join(args.note)
        if args.draft_file:
            try:
                payload["draft"] = Path(args.draft_file).read_text(encoding="utf-8")
            except OSError as e:
                print(_c(f"  could not read {args.draft_file}: {e}", C_RED), file=sys.stderr)
                return 2
        if not payload:
            print(_c("  nothing to change: --status, --url, --note, or --draft-file", C_YEL),
                  file=sys.stderr)
            return 2
        try:
            post = client.patch_post(args.id, **payload)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        print(f"  {post['id']} is {post['status']}"
              + (f", {len(post.get('flags') or [])} flag(s) on the draft" if post.get("draft") else ""))
        if post["status"] == "posted" and not post.get("url"):
            print(_c(f"  no link recorded. otto writing set {post['id']} --url <post url>", C_DIM))
        return 0

    # default: the list
    if args.json:
        print(json.dumps(_w.status(Store()), indent=2))
        return 0
    print(_w.render(Store(), show_dropped=args.all))
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


def cmd_prep(args, client: Client) -> int:
    """Who you are about to talk to, and what is open with them. Local read."""
    from . import prep as _prep

    store = Store()
    p = _prep.for_person(store, args.who) if args.who else _prep.next_meeting(store)
    if args.who and p is None:
        print(f"  no dossier matching {args.who!r}. otto people", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({
            "event": (p.event._asdict() if p and p.event else None),
            "minutes": (p.minutes if p else None),
            "matched": [{"slug": m.person["slug"], "name": m.person.get("display_name"),
                         "how": m.how, "raw": m.raw} for m in (p.matched if p else [])],
            "unmatched": (p.unmatched if p else []),
            "ambiguous": [{"raw": r, "candidates": c}
                          for r, c in (p.ambiguous if p else [])],
        }, indent=2, default=str))
        return 0
    print(_prep.render(store, p))
    return 0


def cmd_retire(args, client: Client) -> int:
    """What Otto should stop doing. Read-only, local, deletes nothing."""
    from . import retire as _retire

    store = Store()
    if args.json:
        print(json.dumps({"gaps": _retire.gaps(store)}, indent=2))
        return 0
    print(_retire.render(store))
    return 0


def cmd_skills_audit(args, client: Client) -> int:
    """Blast-radius audit over every definition. Local: reads files, runs nothing."""
    from . import hardening as _h
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


def cmd_people(args, client: Client) -> int:
    """Operational dossiers. Local files under OTTO_HOME, never in a repo."""
    from . import people as _people
    if args.sync:
        try:
            r = _people.sync(Path(args.sync))
        except (RuntimeError, OSError, ValueError) as e:
            print(_c(f"  {e}", C_RED))
            return 1
        print(f"  created {len(r.created)}  updated {len(r.updated)}  "
              f"bodies preserved {r.kept_bodies}  service accounts skipped "
              f"{len(r.skipped_service)}")
        return 0

    if args.touch:
        spool = config.SLACK_SPOOL_DIR / "new.json"
        try:
            data = json.loads(spool.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(_c(f"  spool unreadable: {e}", C_RED))
            return 1
        notes = _people.apply_contacts(data.get("contacts") or [])
        for n in notes:
            print(f"  {n}")
        if not notes:
            print("  nothing to advance (the daemon may already have applied this "
                  "spool, which is the normal case)")
        return 0

    if args.show:
        d = _people.get(args.show)
        if d is None:
            print(_c(f"  no dossier for {args.show}", C_YEL))
            return 1
        path = _people.PEOPLE_DIR / f"{d['slug']}.md"
        print(path.read_text(encoding="utf-8"))
        return 0

    rows = _people.load()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(_c("  no dossiers yet. otto people --sync <directory-dump.json>", C_YEL))
        return 0
    noted = sum(1 for d in rows if d.get("has_notes"))
    ext = [d for d in rows if d.get("external")]
    # Colleagues first, then outside contacts. Mixing them reads as one roster and the
    # distinction matters: an external contact has no directory record to reconcile against.
    for d in sorted(rows, key=lambda x: (x.get("external", False),
                                         x.get("department") or "~", x["slug"])):
        mark = "*" if d.get("has_notes") else " "
        group = "EXTERNAL" if d.get("external") else (d.get("department") or "-")
        org = d.get("org") or ""
        loc = "/".join(x for x in (d.get("city"), d.get("countryCode")) if x)
        print(f"  {mark} {d['display_name']:<26} {group:<12} "
              f"{(d.get('title') or org or '-'):<30} {loc}")
    print(f"\n  {len(rows)} dossiers ({len(ext)} external), {noted} with notes "
          f"(* = has notes).  {_people.PEOPLE_DIR}")
    return 0


def cmd_doctor(args, client: Client) -> int:
    from .daemon import _running_daemon_pid
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
    from . import configsync
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


def cmd_migrate(args, client: Client) -> int:
    from .migrate import migrate
    return migrate(client, dry_run=args.dry_run)


def cmd_say(args, client: Client) -> int:
    """Render a summary in Otto's voice. Used to check formatting before posting."""
    st = client.state()
    lines = []
    for s in st["schedules"]:
        if s.get("stale"):
            lines.append(f"{s['name']}: STALE - {s['stale']}")
        elif s["due"]:
            lines.append(f"{s['name']}: due - {s['due_reason']}")
    for i in st["integrations"]:
        if not i["ok"] and i["mode"] == "api":
            lines.append(f"{i['name']}: down - {i['detail']}")
    if not lines:
        lines = ["all schedules fresh, all integrations up"]
    print(persona.slack_summary(f"status - {st['at']}", lines))
    return 0


# ---- parser -----------------------------------------------------------------

def _assistant_parsers(sub) -> None:
    """The subcommands that exist only under OTTO_SCOPE=assistant: messaging
    colleagues, dossiers and threads, meeting prep and notes, check-ins, writing.
    One function so the parser and assistant.COMMANDS cannot drift apart; the
    profile test checks `otto --help` against that list."""
    s = sub.add_parser("dm", help="group DM a colleague AND the owner, as Otto, now "
                                  "(for messages the owner asked for)")
    s.add_argument("who", nargs="+", help="login, email, or people slug; the owner is always added")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.add_argument("--why", help="what the owner asked, in a line (goes in the ledger)")
    s.add_argument("--task", help="board task id this is for, if any")
    s.set_defaults(fn=cmd_dm)

    s = sub.add_parser("outreach", help="messages Otto wants to send other people")
    s.add_argument("--state", choices=["held", "sent", "killed", "expired", "failed"])
    s.add_argument("--kill", metavar="ID", help="stop a held message")
    s.add_argument("--send", metavar="ID", help="send a held message now, skip the hold")
    s.add_argument("--extend", metavar="ID",
                   help="push a held message's send time back, without deciding")
    s.add_argument("--minutes", type=int, default=10,
                   help="how far --extend pushes it (default 10)")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=lambda a, c: (cmd_outreach_act(a, c)
                                    if (a.kill or a.send or a.extend)
                                    else cmd_outreach(a, c)),
                   outreach_cmd=None)
    out_sub = s.add_subparsers(dest="outreach_cmd")

    # Compose only. There is deliberately no CLI path that sends without a hold:
    # a caller that can skip the interlock is a caller with no interlock.
    a = out_sub.add_parser("compose", help="draft a HELD message (never sends now)")
    a.add_argument("--to", required=True, help="login, email, or name")
    a.add_argument("--body", required=True)
    a.add_argument("--why", required=True,
                   help="what the owner decides on in ten seconds. Required")
    a.add_argument("--channel", default="slack-dm",
                   choices=["slack-dm", "slack-channel"])
    a.add_argument("--source", default="otto")
    a.set_defaults(fn=cmd_outreach_compose, kill=None, send=None, extend=None,
                   minutes=10, state=None, limit=10, json=False)

    a = out_sub.add_parser("resolve",
                           help="record a colleague's Slack id (required before sending)")
    a.add_argument("target", help="their email / login")
    a.add_argument("--force", action="store_true", help="re-resolve an existing id")
    a.set_defaults(fn=cmd_outreach_resolve, kill=None, send=None, extend=None,
                   minutes=10, state=None, limit=10, json=False)

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

    s = sub.add_parser("patterns", help="what actually moves your energy")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_patterns)

    s = sub.add_parser("prep", help="meeting prep: who you are about to talk to")
    s.add_argument("who", nargs="?",
                   help="a name, login, or slug. Defaults to your next meeting")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_prep)

    s = sub.add_parser("people", help="operational dossiers per person")
    s.add_argument("--sync", metavar="DIRECTORY_DUMP",
                   help="regenerate frontmatter from a directory users dump (JSON); "
                        "hand-written bodies are never touched")
    s.add_argument("--show", metavar="LOGIN", help="print one dossier")
    s.add_argument("--touch", action="store_true",
                   help="apply the DM spool's contact facts to last_contact now "
                        "(the daemon does this on its own; this is for backfill "
                        "and debugging)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_people, people_cmd=None)
    ppl_sub = s.add_subparsers(dest="people_cmd")

    a = ppl_sub.add_parser("note", help="append a note to a dossier section")
    a.add_argument("slug")
    a.add_argument("text", nargs="+")
    a.add_argument("--section", default="Threads")
    a.set_defaults(fn=cmd_people_note, sync=None, show=None, json=False, touch=False)

    s = sub.add_parser("threads", help="every dated dossier thread, the whole list")
    s.add_argument("--band", choices=["fresh", "quiet", "ancient"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_threads)

    s = sub.add_parser("thread-note",
                       help="note one thread and let Otto decide what to do")
    s.add_argument("thread_id")
    s.add_argument("note", nargs="+")
    s.set_defaults(fn=cmd_thread_note)

    s = sub.add_parser("thread-update",
                       help="rewrite a thread line in place (what closes a thread)")
    s.add_argument("thread_id")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_thread_update)

    s = sub.add_parser("meetings", help="action items Otto took off your Notion meeting notes")
    m_sub = s.add_subparsers(dest="meetings_cmd")
    s.set_defaults(fn=cmd_meetings, meetings_cmd="status", json=False,
                   no_wait=False, timeout=180)
    s.add_argument("--json", action="store_true")
    mi = m_sub.add_parser("ingest", help="read new meeting notes now, then report")
    mi.add_argument("--no-wait", action="store_true")
    mi.add_argument("--timeout", type=int, default=180)
    mi.add_argument("--json", action="store_true")
    mi.set_defaults(fn=cmd_meetings, meetings_cmd="ingest")

    s = sub.add_parser("writing", help="post ideas from your week, drafts in your voice")
    s.add_argument("--all", action="store_true", help="include dropped ideas")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_writing, writing_cmd=None)
    wr_sub = s.add_subparsers(dest="writing_cmd")

    a = wr_sub.add_parser("ideas", help="mine the last week for post ideas now")
    a.add_argument("--no-wait", action="store_true")
    a.add_argument("--timeout", type=int, default=240)
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("draft", help="draft a post from an idea, or redraft it with a note")
    a.add_argument("id")
    a.add_argument("--note", help="what to change, in your words")
    a.add_argument("--from-file", help="your edited draft; it becomes the text the run revises")
    a.add_argument("--no-wait", action="store_true")
    a.add_argument("--timeout", type=int, default=240)
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("edit", help="open the draft in $EDITOR (or notepad); saved as your edit")
    a.add_argument("id")
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("show", help="one post in full, with what the scan flagged")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_writing, all=False)

    a = wr_sub.add_parser("set", help="mark it posted or dropped, record the link, add a note")
    a.add_argument("id")
    a.add_argument("--status", choices=["idea", "drafted", "posted", "dropped"])
    a.add_argument("--url")
    a.add_argument("--note", nargs="+")
    a.add_argument("--draft-file", help="replace the draft with this file's text (re-scanned)")
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("voice", help="where your voice file is; seeds it if missing")
    a.set_defaults(fn=cmd_writing, all=False, json=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="otto",
        description=f"{config.PERSONA_NAME} - {config.PERSONA_BLURB}",
    )
    p.add_argument("--url", default=None, help="daemon base URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="one-screen view of everything")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=12)
    s.add_argument("--domain", choices=["work", "personal"], help="show one domain only")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("reply", help="post a thread reply as Otto")
    s.add_argument("--channel", required=True, help="channel id (must be allowlisted)")
    s.add_argument("--thread", required=True, help="parent message ts")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("post", help="post top-level in an allowlisted channel as Otto")
    s.add_argument("--channel", default=config.SLACK_CHANNEL_ID or None,
                   help="channel id (must be allowlisted; default "
                        + (f"{config.SLACK_CHANNEL_ID}, #{config.SLACK_CHANNEL}"
                           if config.SLACK_CHANNEL_ID else "none: set OTTO_SLACK_CHANNEL_ID")
                        + ")")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_post)

    s = sub.add_parser("tell", help="DM the owner as Otto (recipient is fixed)")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_tell)

    s = sub.add_parser("priorities", help="what matters right now, and how stale it is")
    s.add_argument("--init", action="store_true", help="write a starter file")
    s.add_argument("--force", action="store_true", help="overwrite an existing one")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_priorities)

    s = sub.add_parser("spend", help="cost per workload, model, and agent")
    s.add_argument("--days", type=int, default=7,
                   help="window (default 7; a month hides a cadence change)")
    s.add_argument("--by", choices=["workload", "model", "agent"],
                   help="one grouping only (default: all three)")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_spend)

    s = sub.add_parser("ledger", help="every session on this machine, priced and ranked")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--who", choices=["yours", "otto", "other"],
                   help="only the owner's terminals, only Otto's runs, or anything else")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--session", metavar="ID", help="one session's story (a prefix is enough)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("telemetry", help="Claude Code's own per-request cost, exported to the daemon")
    tel_sub = s.add_subparsers(dest="telemetry_cmd")
    tel_sub.add_parser("status", help="is it on, how much has arrived")
    tel_sub.add_parser("install", help="write the env block to ~/.claude/settings.json")
    tel_sub.add_parser("uninstall", help="remove the env block")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_telemetry, telemetry_cmd="status")

    s = sub.add_parser("runs", help="run history")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("logs", help="tail a run's captured log")
    s.add_argument("run_id")
    s.add_argument("--lines", type=int, default=200)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("spawn", help="launch a tracked Claude Code session")
    s.add_argument("name")
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--cwd")
    s.add_argument("--agent", help="subagent definition from ~/.claude/agents")
    s.add_argument("--mode", choices=["headless", "windowed"], default="headless")
    s.add_argument("--task", help="external queue id")
    s.add_argument("--tier")
    s.add_argument("--safe", action="store_true",
                   help="do NOT pass --dangerously-skip-permissions")
    s.add_argument("--domain", choices=["work", "personal"],
                   help="default: inferred from --cwd")
    s.add_argument("--model",
                   help=f"default: {config.DEFAULT_MODEL}. Pass "
                        f"{config.DEEP_MODEL} for long-horizon work")
    s.set_defaults(fn=cmd_spawn)

    s = sub.add_parser("kill", help="terminate a tracked run")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("done", help="mark a run finished (agents self-report here)")
    s.add_argument("run_id")
    s.add_argument("--status", default="ok", choices=["ok", "failed", "skipped"])
    s.add_argument("--exit-code", type=int, default=0)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("registry", help="what agents/skills/commands exist")
    s.add_argument("--kind", choices=["agent", "command", "skill", "project"])
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_registry)

    s = sub.add_parser("scan", help="rescan disk for definitions")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("schedules", help="list schedules and cadence")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_schedules)

    s = sub.add_parser("due", help="print commands that are due now")
    s.add_argument("--json", action="store_true")
    s.add_argument("--domain", choices=["work", "personal"])
    s.set_defaults(fn=cmd_due)

    # ---- schedule management: how personal routines get added ----
    sch = sub.add_parser("schedule", help="add or remove a schedule")
    sch_sub = sch.add_subparsers(dest="schedule_cmd", required=True)

    a = sch_sub.add_parser("add", help="create or replace a schedule")
    a.add_argument("name")
    a.add_argument("--command", required=True,
                   help="slash command or shell command to run")
    a.add_argument("--domain", choices=["work", "personal"], default="personal")
    a.add_argument("--kind", choices=["daily", "weekly", "every", "manual"], default="daily")
    a.add_argument("--at", default="08:00", help="HH:MM local, for daily/weekly")
    a.add_argument("--days", help="weekly only, comma separated (e.g. sun or mon,thu)")
    a.add_argument("--hours", type=int, help="for --kind every")
    a.add_argument("--min-interval-days", type=int,
                   help="never run again within N days, whatever the cadence says")
    a.add_argument("--max-age-hours", type=int,
                   help="raise an alarm if it has not run in this long")
    a.add_argument("--description")
    a.add_argument("--disabled", action="store_true")
    a.add_argument("--force", action="store_true",
                   help="store the command even if it looks shell-mangled")
    a.set_defaults(fn=cmd_schedule_add)

    a = sch_sub.add_parser("arm", help="run this schedule unattended, on cadence")
    a.add_argument("name")
    a.add_argument("--off", action="store_true", help="disarm it")
    a.set_defaults(fn=cmd_schedule_arm)

    r = sch_sub.add_parser("rm", help="delete a schedule")
    r.add_argument("name")
    r.set_defaults(fn=cmd_schedule_rm)

    s = sub.add_parser("agenda", help="calendar/mail snapshots pushed in by a session")
    s.add_argument("--push", metavar="KIND",
                   help="read JSON on stdin and store it as this snapshot kind")
    s.add_argument("--domain", choices=config.DOMAINS,
                   help="which inbox/calendar this snapshot is for (default work)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_agenda)

    s = sub.add_parser("notices", help="messages Otto sent you")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--read", action="store_true", help="mark everything shown as read")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_notices)

    s = sub.add_parser("notify", help="send the owner a message (for agents/schedules)")
    s.add_argument("title", nargs="+")
    s.add_argument("--body")
    s.add_argument("--level", choices=["info", "warn", "crit"], default="info")
    s.add_argument("--domain", choices=config.DOMAINS)
    s.add_argument("--source", default="agent")
    s.add_argument("--command")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("day", help="where the day went, from your own transcripts")
    s.add_argument("--date", help="ISO date, default today")
    s.add_argument("--refresh", action="store_true", help="recompute the rollup")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_day)

    s = sub.add_parser("machine", help="this workstation, informational only")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_machine)

    s = sub.add_parser("stamp", help="record a successful schedule run")
    s.add_argument("name")
    s.add_argument("--status", default="ok")
    s.add_argument("--run-id")
    s.set_defaults(fn=cmd_stamp)

    s = sub.add_parser("toggle", help="enable/disable a schedule")
    s.add_argument("name")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_toggle)

    s = sub.add_parser("probe", help="check integration liveness")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("events", help="recent event log")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_events)


    s = sub.add_parser("board", help="all outstanding work, in columns")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--busy", action="store_true", help="hide empty columns")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_board)

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
    a.set_defaults(fn=cmd_task_open)

    a = tsk_sub.add_parser("run", help="dispatch a task now")
    a.add_argument("task_id")
    a.add_argument("--force", action="store_true",
                   help="ignore the attempt cap and the master switch")
    a.set_defaults(fn=cmd_task_run)

    a = tsk_sub.add_parser("reply", help="answer a card: update it, or give Otto context")
    a.add_argument("task_id")
    a.add_argument("text", nargs="*")
    a.add_argument("--text-file", metavar="PATH", help="read the reply from a file, or - for stdin")
    a.set_defaults(fn=cmd_task_reply)

    a = tsk_sub.add_parser("rm", help="delete a task")
    a.add_argument("task_id")
    a.set_defaults(fn=cmd_task_rm)

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

    s = sub.add_parser("chat", help="talk to Otto about the control plane")
    s.add_argument("message", nargs="*")
    s.add_argument("--show", action="store_true", help="print the thread and exit")
    s.add_argument("--reset", action="store_true", help="forget the conversation")
    s.add_argument("--no-wait", action="store_true", help="do not wait for the reply")
    s.add_argument("--timeout", type=int, default=120)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_chat)

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

    s = sub.add_parser("launch", help="run a schedule now")
    s.add_argument("name")
    s.add_argument("--watch", action="store_true", help="follow the timeline")
    s.add_argument("--interval", type=int, default=3)
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_launch)

    s = sub.add_parser("autorun", help="unattended cron: switch, armed, tripped")
    s.add_argument("state", nargs="?", choices=["on", "off"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_autorun)

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

    a = sub.add_parser("app", help="open the dashboard as its own window (Chrome or Edge app mode)")
    a.add_argument("view", nargs="?", default="dispatch",
                   choices=["today", "board", "dispatch", "terminal", "grid", "history",
                            "writing", "plane"])
    a.set_defaults(fn=cmd_app)

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

    s = sub.add_parser("live", help="agents running right now")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_live)

    s = sub.add_parser("watch", help="timeline for one run")
    s.add_argument("run_id")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--interval", type=int, default=3)
    s.add_argument("--verbose", action="store_true", help="include tool results")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("autodispatch", help="whether queued tasks are auto-run")
    s.add_argument("state", nargs="?", choices=["on", "off"])
    s.set_defaults(fn=cmd_autodispatch)

    s = sub.add_parser("next", help="what to do now, ranked")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_next)

    s = sub.add_parser("gaps", help="blind spots nothing is watching")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_gaps)

    s = sub.add_parser("manifest", help="declared vs observed capabilities (names only)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_manifest)

    s = sub.add_parser("identity", help="who Otto authenticates as, per integration")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="show the audit field and the blocker for each row")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_identity)

    s = sub.add_parser("feeds", help="producer feed directories: who has dropped what")
    s.add_argument("--init", action="store_true",
                   help="create the feed root, producers/, and a dir per declared source")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_feeds)

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

    s = sub.add_parser("decisions", help="the decision log")
    s.add_argument("--domain", choices=["work", "personal"])
    s.add_argument("--all", action="store_true", help="include superseded ones")
    s.add_argument("--search", metavar="TEXT", help="substring match across every field")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_decisions)

    s = sub.add_parser("decision", help="show one decision in full")
    s.add_argument("id")
    s.add_argument("--revisit-by", dest="revisit_by", nargs="?", const="",
                   metavar="YYYY-MM-DD",
                   help="schedule the review (pass with no value to clear it). The "
                        "only field on a recorded decision that can change")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_decision)

    s = sub.add_parser("ack", help="acknowledge a failed run and clear its card")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--all", action="store_true",
                   help="acknowledge every outstanding failure")
    s.add_argument("--transient", action="store_true",
                   help="acknowledge only upstream API failures (the 529 burst case)")
    s.add_argument("--undo", action="store_true", help="put one back")
    s.set_defaults(fn=cmd_ack)

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

    s = sub.add_parser("retire", help="what Otto should stop doing (deletes nothing)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_retire)

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

    s = sub.add_parser("refresh", help="pull calendar + mail via MCP, per domain")
    s.add_argument("--domain", choices=config.DOMAINS,
                   help="which account to refresh (default work)")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--timeout", type=int, default=150)
    s.set_defaults(fn=cmd_refresh)

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

    s = sub.add_parser("serve", help="run the daemon + dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--force", action="store_true", help="start even if a pidfile exists")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("prune", help="clear failed/orphaned runs from history")
    s.add_argument("--older-than", type=int, default=24, metavar="HOURS")
    s.add_argument("--yes", action="store_true", help="actually remove them")
    s.set_defaults(fn=cmd_prune)

    s = sub.add_parser("ensure", help="start the daemon if it is not running")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--wait-pid", type=int, metavar="PID",
                   help="wait for this pid to exit first (used by the dashboard restart)")
    s.set_defaults(fn=cmd_ensure)

    s = sub.add_parser("restart", help="stop the daemon and start it again (applies otto.env)")
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("setup", help="first-run setup: see what is configured and fill in the rest")
    s.add_argument("--status", action="store_true", help="print the steps and exit; 0 when complete")
    s.add_argument("--reset", action="store_true", help="clear completion so the Setup view opens again")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("stop", help="stop the daemon, leaving agents running")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("doctor", help="diagnose the harness itself")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("migrate", help="absorb existing orchestrator ledgers")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("say", help="render current status in Otto's voice")
    s.set_defaults(fn=cmd_say)

    if config.ASSISTANT:
        _assistant_parsers(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    config.utf8_output()
    args = build_parser().parse_args(argv)
    client = Client(args.url)
    try:
        return args.fn(args, client)
    except DaemonDown as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    except DetailError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
