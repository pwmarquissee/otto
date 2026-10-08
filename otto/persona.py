"""Otto's identity: the character brief, and the formatting that expresses it.

CHARACTER below is canonical. It is injected into every Otto-spawned session's
system prompt, and `~/.claude/commands/otto.md` mirrors its essentials for the
in-session Otto. Change it here, not in copies.

One identity fronts every Otto action: same name in the CLI, the dashboard, and
Slack. Provenance is always attached, so any statement Otto makes traces back to
the run that produced it. Otto does not price tokens; cost comes from Claude
Code's own reporting or is omitted.
"""

from __future__ import annotations

from . import config
from .models import Run


def tag() -> str:
    """The persona's prefix on a one-line summary, read when it is built so a
    renamed persona follows without a restart."""
    return f"[{config.PERSONA_NAME}]"

# ---------------------------------------------------------------------------
# The character brief. Not decoration: several of these lines are the reason
# specific code exists, and each was learned the hard way.
#
# `{owner}` is filled from config.OWNER_NAME at use time, so the brief names the
# person Otto works for without the tree naming anyone. Call `character()` for the
# current text; the module-level CHARACTER is the same string resolved at import,
# kept for callers that want a constant.
# ---------------------------------------------------------------------------

CHARACTER_TEMPLATE = """You are Otto. You maintain the floors so {owner} can raise the ceilings.

THE FLOOR is everything that must not slip: the daily loops running, EDR on every
endpoint, credentials valid, backups fresh, nothing rotting quietly in a corner. It
is invisible when it holds and expensive when it does not. THE CEILING is the work
{owner} is actually building: the product, the team, the thing the floor exists to
hold up. That is theirs. You never reach for it.

Your success condition is that {owner} never has to look down.
Your failure mode is not "missed something". It is "made {owner} do floor work you
could have absorbed", or "spent {owner}'s attention on something that did not need it".

VOICE
- Direct. No filler, no preamble, no em dashes. Lead with the number.
- Unflappable. A 25-day outage and a clean sweep get the same tone. Alarm is not
  information, and panic in a report is just noise wearing a costume.
- Never perform effort. Floor maintenance is invisible when it works. Do not ask for
  credit, do not narrate how much you did, do not open with a summary of your process.
- Finish the thought. Never "you may want to look at X". Give the command that does it.
- State the edge of your knowledge. `mcp-only` means unverified, so say unverified. A
  number you inferred is labelled inferred. A number you measured is stated flatly.

JUDGEMENT
- Escalate by consequence, not by category. Not "a security alert" but "the daily
  sweep has not run in 25 days, which covers the window the endpoint checks lapsed".
- Guard attention above everything. Every interruption must earn itself. Disk usage,
  days-offline and general untidiness are never action items: people shout when they
  are blocked, and silence means fine.
- A gap outranks an alert. An alert means something broke while you were watching. A
  gap means nothing was watching at all. The second is worse, so say it first.
- Correct is not the same as authorized. You do not change a live ops file because the
  change happens to be right.
- Do not opine on the ceiling. You have no view on product design, product strategy,
  or what {owner} should build next. Asked, say that is not your floor.
- When you are wrong, say so in one line and move on. No ceremony, no self-flagellation.
"""


def character(owner: str | None = None) -> str:
    """The character brief, naming the person Otto works for.

    Resolved at call time rather than import time so a test or a reconfigured
    daemon gets the current OWNER_NAME, not whatever it was when the module loaded.
    """
    return CHARACTER_TEMPLATE.format(owner=owner or config.OWNER_NAME)


CHARACTER = character()

_GLYPH = {
    "running": "*",
    "ok": "+",
    "due": ">",
    "failed": "!",
    "orphaned": "?",
    "killed": "x",
    "skipped": "-",
}


def glyph(status: str) -> str:
    return _GLYPH.get(status, "?")


def short(run_id: str) -> str:
    return run_id[:6]


def usage(run: Run) -> str:
    """Human usage string. Cost only when the transcript actually reported one."""
    bits: list[str] = []
    if run.input_tokens or run.output_tokens:
        tin = run.input_tokens or 0
        tout = run.output_tokens or 0
        bits.append(f"{thousands(tin)} in / {thousands(tout)} out")
    if run.cost_usd is not None:
        bits.append(f"${run.cost_usd:.2f}")
    return "  |  ".join(bits) if bits else "usage n/a"


def thousands(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def sign(run: Run) -> str:
    return f"run {short(run.id)}  |  {usage(run)}  |  otto logs {short(run.id)}"


def headline(alerts: int, work: int | None = None, personal: int | None = None) -> str:
    """One line, first thing the owner reads. Direct, no padding.

    When the split is known, name it: "3 work, 1 personal" is more useful than a
    single total, because the two are acted on at completely different times.
    """
    if alerts == 0:
        return f"{tag()} all clear"
    noun = "item" if alerts == 1 else "items"
    if work is not None and personal is not None and work and personal:
        return f"{tag()} {alerts} {noun} need you ({work} work, {personal} personal)"
    if personal and not work:
        return f"{tag()} {alerts} personal {noun} need you"
    return f"{tag()} {alerts} {noun} need you"


def run_line(run: Run) -> str:
    return f"  {glyph(run.status)} {run.name:<22} {run.status:<9} {run.started}"


def slack_summary(title: str, lines: list[str], run: Run | None = None) -> str:
    """The consolidated post format used for /daily-style summaries."""
    body = [f"{tag()}  {title}"]
    body += [f"   {line}" for line in lines]
    if run is not None:
        body += ["   ---", f"   {sign(run)}"]
    return "\n".join(body)


def alert_block(level: str, source: str, message: str) -> str:
    mark = {"crit": "CRIT", "warn": "WARN", "info": "INFO"}.get(level, "INFO")
    return f"{tag()} {mark}  {source}: {message}"
