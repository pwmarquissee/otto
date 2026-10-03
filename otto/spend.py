"""Where the money goes.

Otto has always recorded `cost_usd` per run, and that number was authoritative
(Claude Code reports it; Otto never prices tokens itself). What was missing was any
way to *group* it. 500 runs each carrying a dollar figure and a free-text name is a
number you can sum and cannot act on, which is how slack-sweep reached 59% of all
spend without anyone noticing.

Three groupings, because they answer three different questions:

  by_workload  what is Otto spending on          -> is this job worth its bill
  by_model     what is it spending it through    -> is this the right model for it
  by_agent     which subagent definition ran     -> which persona costs what

The unit is one run, so the arithmetic is exact rather than estimated. Two known
holes, both stated rather than papered over:

  * `model` was only recorded from 2026-08-12. Runs before that report `unrecorded`,
    NOT a guess. Inferring them from today's config would be wrong for exactly the
    runs worth studying, since the point of the view is to catch a model that was
    set badly and later changed.
  * Outreach sends never create a Run at all (otto/outreach.py runs them with
    `subprocess.run` and keeps its own state), so they are invisible here. Small
    (~$0.27 a send on Haiku) but not zero.

A subagent's cost is NOT separable from its parent. Claude Code bills the whole
session, so an Agent-tool subagent spawned inside a run is already inside that run's
`cost_usd`. `by_agent` therefore reports the cost of runs launched with `--agent`,
not the cost of nested subagent turns. There is no data for the latter.
"""

from __future__ import annotations

import collections
import re
import statistics
from datetime import timedelta

from . import config
from .models import Run, utcnow

# Schedules and modules whose runs share a name, so a prefix match is enough to
# collapse them. Ordered: the first hit wins, so put the specific ones first.
_FAMILIES = (
    "slack-sweep", "help-summon", "meeting-notes",
    "writing",
    "card-reply", "otto-dm",
    "daily-rollup", "orchestrate", "triage", "refresh",
    "chat", "observe", "otto",
)

UNRECORDED = "unrecorded"


def family(run: Run) -> str:
    """Collapse a run name into something worth grouping on.

    Board tasks each carry a unique human title, so grouping on raw names produces
    one row per run and no insight. They collapse to `board task` instead, which is
    the honest unit: the board is one workload that happens to have many subjects.
    """
    name = (run.name or "").strip().lower()
    if name.startswith("note:"):
        return "thread-note"
    for f in _FAMILIES:
        if f in name:
            return f
    if run.task_id or "mode=task" in (run.notes or ""):
        return "board task"
    return "board task" if len(name) > 24 else (name or "?")


# `agent=<name>` is how spawn recorded the agent before Run.agent existed. Reading it
# back means the view has history on day one instead of starting empty.
_AGENT_IN_NOTES = re.compile(r"\bagent=([A-Za-z0-9_-]+)")


def agent_of(run: Run) -> str | None:
    if run.agent:
        return run.agent
    m = _AGENT_IN_NOTES.search(run.notes or "")
    return m.group(1) if m else None


def model_of(run: Run) -> str:
    """The model, or `unrecorded`.

    Every spawn path now passes an explicit model, so a missing one means the run
    predates the field rather than that it chose the default. Deliberately not
    inferred from today's config: the runs most worth studying are the ones whose
    model was set badly and later changed, and a backfill would relabel exactly
    those with the answer that makes the mistake invisible.
    """
    return run.model or UNRECORDED


def _priced(runs: list[Run], days: int, domain: str | None) -> list[Run]:
    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    return [
        r for r in runs
        if isinstance(r.cost_usd, (int, float)) and r.cost_usd > 0
        and (r.started or "") >= cutoff
        and (domain is None or r.domain == domain)
    ]


def _group(runs: list[Run], key) -> list[dict]:
    buckets: dict[str, list[Run]] = collections.defaultdict(list)
    for r in runs:
        k = key(r)
        if k is not None:
            buckets[str(k)].append(r)
    total = sum(r.cost_usd or 0.0 for r in runs) or 1.0
    out = []
    for k, rs in buckets.items():
        costs = [r.cost_usd or 0.0 for r in rs]
        ins = [r.input_tokens or 0 for r in rs]
        outs = [r.output_tokens or 0 for r in rs]
        out.append({
            "key": k,
            "runs": len(rs),
            "total_usd": round(sum(costs), 4),
            "median_usd": round(statistics.median(costs), 4),
            "max_usd": round(max(costs), 4),
            "share": round(100 * sum(costs) / total, 1),
            "median_input": int(statistics.median(ins)) if ins else 0,
            "median_output": int(statistics.median(outs)) if outs else 0,
        })
    return sorted(out, key=lambda d: -d["total_usd"])


def report(runs: list[Run], days: int = 7, domain: str | None = None) -> dict:
    """Spend over the last `days`, grouped three ways.

    `days` defaults to 7 rather than 30 because the run store is capped and a
    schedule's cadence changes faster than a month: a 30-day average hides the fact
    that a workload only started last Tuesday, which is exactly the mistake that
    made slack-sweep look affordable.
    """
    priced = _priced(runs, days, domain)
    total = sum(r.cost_usd or 0.0 for r in priced)

    by_day: dict[str, float] = collections.defaultdict(float)
    for r in priced:
        by_day[(r.started or "")[:10]] += r.cost_usd or 0.0
    observed = len(by_day) or 1

    unpriced = [
        r for r in runs
        if (r.started or "") >= (utcnow() - timedelta(days=days)).isoformat()
        and not isinstance(r.cost_usd, (int, float))
        and (domain is None or r.domain == domain)
    ]

    return {
        "days": days,
        "domain": domain,
        "runs": len(priced),
        "total_usd": round(total, 2),
        # Divided by days ACTUALLY OBSERVED, not by `days`. A workload that started
        # three days into a seven-day window would otherwise report a rate less than
        # half its real one.
        "observed_days": observed,
        "per_day_usd": round(total / observed, 2),
        "per_month_usd": round(total / observed * 30, 2),
        "by_workload": _group(priced, family),
        "by_model": _group(priced, model_of),
        "by_agent": _group(priced, agent_of),
        "daily": [{"date": d, "total_usd": round(v, 2)} for d, v in sorted(by_day.items())],
        "top_runs": [
            {"id": r.id[:6], "name": (r.name or "")[:44], "cost_usd": round(r.cost_usd or 0, 3),
             "model": model_of(r), "agent": agent_of(r), "started": r.started}
            for r in sorted(priced, key=lambda r: -(r.cost_usd or 0))[:10]
        ],
        # Runs Otto cannot price: still running, killed before a result, or a shell
        # command that never had a cost. Surfaced as a count so the total is never
        # mistaken for "everything that happened".
        "unpriced_runs": len(unpriced),
    }
