"""Cadence evaluation.

Rules like "scout only on Wednesdays and not within 5 days of the last run" or
"meeting notes every four hours" were once prose in a command file that a model
re-read and re-interpreted every run. Here they are data, evaluated the same way
every time.

Two independent questions per schedule:

  due    -> the cadence says it should run now
  stale  -> it has not succeeded within max_age_hours, so something is wrong

A schedule can be stale without being due, which is exactly the failure the
heartbeat ledger was built to catch.

Timestamps are stored in UTC. Cadence is evaluated in LOCAL time, because
"Mondays at 08:00" is a statement about the human's day, not about UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import config
from ..models import Cadence, Schedule

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _at_time(cadence: Cadence) -> tuple[int, int]:
    try:
        hh, mm = cadence.at.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return 0, 0


def is_due(sched: Schedule, now: datetime | None = None) -> tuple[bool, str]:
    """Returns (due, reason)."""
    if not sched.enabled:
        return False, "disabled"

    cad = sched.cadence
    if cad.kind == "manual":
        return False, "manual"

    now = (now or datetime.now()).astimezone()
    if (config.WEEKEND_PAUSES_WORK and sched.domain == "work"
            and config.is_weekend(now)):
        # Not "not due yet" but "not this weekend". Personal schedules are unaffected.
        return False, "work paused on the weekend"
    last = _parse(sched.last_run)
    last_local = last.astimezone() if last else None

    # A minimum interval vetoes everything else. This is the /scout guard.
    if cad.min_interval_days and last_local:
        elapsed = now - last_local
        if elapsed < timedelta(days=cad.min_interval_days):
            remaining = timedelta(days=cad.min_interval_days) - elapsed
            days = remaining.total_seconds() / 86400
            return False, f"min interval {cad.min_interval_days}d ({days:.1f}d left)"

    if cad.kind == "every":
        hours = cad.hours or 24
        if last_local is None:
            return True, "never run"
        elapsed_h = (now - last_local).total_seconds() / 3600
        if elapsed_h >= hours:
            return True, f"{elapsed_h:.1f}h since last (every {hours}h)"
        return False, f"{hours - elapsed_h:.1f}h until due"

    if cad.kind == "weekly":
        today = WEEKDAYS[now.weekday()]
        if cad.days and today not in cad.days:
            return False, f"not scheduled {today} (runs {','.join(cad.days)})"

    hh, mm = _at_time(cad)
    window_open = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < window_open:
        return False, f"before {cad.at}"

    if last_local is not None and last_local >= window_open:
        return False, f"already ran today at {last_local.strftime('%H:%M')}"

    return True, "never run" if last_local is None else f"due since {cad.at}"


def _elapsed_hours(start: datetime, end: datetime, skip_weekend: bool) -> float:
    """Hours between two instants, optionally not counting Sat/Sun.

    Without this the weekend pause would manufacture a false outage every Saturday:
    a work schedule that correctly did not run still ages, crosses max_age_hours, and
    reports itself dark. Weekday/weekend is judged in LOCAL time because that is
    the sense in which the owner has a weekend.
    """
    if not skip_weekend:
        return (end - start).total_seconds() / 3600
    a, b = start.astimezone(), end.astimezone()
    if b <= a:
        return 0.0
    total = 0.0
    cur = a
    while cur < b:
        midnight = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        chunk_end = min(b, midnight)
        if cur.weekday() < 5:          # Mon-Fri only
            total += (chunk_end - cur).total_seconds() / 3600
        cur = chunk_end
    return total


def staleness(sched: Schedule, now: datetime | None = None) -> tuple[str, str] | None:
    """Returns (level, detail) when a schedule has gone stale, else None.

    A schedule that has never run is NOT automatically stale. Staleness measures
    silence against a baseline, and for a schedule that was just added the correct
    baseline is when it was created, not the beginning of time. Otherwise every new
    routine screams the moment you add it.
    """
    if not sched.enabled or not sched.max_age_hours:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    skip = config.WEEKEND_PAUSES_WORK and sched.domain == "work"

    last = _parse(sched.last_run)
    if last is None:
        baseline = _parse(sched.created)
        if baseline is None:
            return "warn", "never run"
        waited_h = _elapsed_hours(baseline, now, skip)
        if waited_h <= sched.max_age_hours:
            return None  # added recently, give it a chance to run
        return "warn", f"never run since added {waited_h / 24:.1f}d ago"

    age_h = _elapsed_hours(last, now, skip)
    if age_h <= sched.max_age_hours:
        return None
    level = "crit" if age_h > sched.max_age_hours * 3 else "warn"
    unit = "working days" if skip else "days"
    return level, f"last success {age_h / 24:.1f} {unit} ago (limit {sched.max_age_hours}h)"


# ---- seeds ------------------------------------------------------------------
# These encode the existing /daily contract. autostart stays False: Otto
# surfaces work as due and lets a human or a loop pull it, rather than silently
# launching agents with skip-permissions on a timer.
#
# There are deliberately NO personal seeds. Otto does not get to invent what the
# owner's life looks like: `otto schedule add` exists for that, and the personal
# domain starts empty until the owner fills it.

def default_schedules() -> list[Schedule]:
    seeds = [
        Schedule(
            name="daily-rollup",
            domain=config.WORK,
            command="(built in) journal.rollup",
            description="Yesterday's transcripts reduced to a day record. Silent; the "
                        "morning check-in prompt was removed 2026-09-03",
            cadence=Cadence(kind="daily", at="06:00"),
            # Runs inside the daemon, not as a session: it is mechanical extraction
            # with no MCP and no model, so spawning Claude Code for it would be
            # pure overhead. runner stays "report" because nothing is launched.
            max_age_hours=30,
        ),
        Schedule(
            # Post ideas mined from the week. Friday afternoon, so the ideas are
            # waiting over the weekend and never compete with a workday. Autostart
            # is safe here for the reason writing.py gives: the session gets its
            # material in the prompt, has every tool denied and an empty MCP
            # config, and is budget-capped. Eight days of silence alarms.
            name="writing-ideas",
            domain=config.WORK,
            command="(built in) writing.start_ideas",
            description="Post ideas mined from the week's transcripts, decisions, "
                        "and notices. Drafts are on demand: otto writing draft <id>",
            cadence=Cadence(kind="weekly", days=["fri"], at="15:00"),
            runner="writing",
            autostart=config.SCHEDULES_ARMED_BY_DEFAULT,
            max_age_hours=8 * 24,
        ),
        # The slash-command schedules below only make sense when the command ships
        # in claude/commands/. Add your own daily sweep or heartbeat here once you
        # have written the command for it; a schedule pointing at a command that
        # does not exist is a stale alarm from day one.
        Schedule(
            name="triage",
            domain=config.WORK,
            command="/triage",
            description="Assess unjudged backlog cards, route them, and promote "
                        "what may safely run",
            cadence=Cadence(kind="daily", at="08:00"),
            max_age_hours=26,
        ),
        Schedule(
            name="orchestrate",
            domain=config.WORK,
            command="/orchestrate",
            description="Work the board: gate by risk tier, promote what may run, "
                        "present what needs a decision",
            cadence=Cadence(kind="daily", at="08:00"),
            max_age_hours=26,
        ),
        Schedule(
            # Otto's own judgement, once a day: the one thing worth saying that no
            # template would catch. notify.py caps it at one notice per day, so a
            # daily cadence is also the most it could ever usefully run.
            name="observe",
            domain=config.WORK,
            command="/observe",
            description="Look across Otto's state for the one thing worth saying "
                        "that no template would catch",
            cadence=Cadence(kind="daily", at="09:00"),
            max_age_hours=26,
        ),
        Schedule(
            name="inbox-sync",
            domain=config.PERSONAL,
            command="otto refresh",
            description="Pull today's calendar and reply-worthy mail via MCP",
            cadence=Cadence(kind="every", hours=4),
            runner="refresh",
            autostart=config.SCHEDULES_ARMED_BY_DEFAULT,
            max_age_hours=14,
        ),
        Schedule(
            name="meeting-notes",
            domain=config.WORK,
            command="otto meetings ingest",
            description="Parse new Notion AI meeting notes into board cards, with the "
                        "due dates the meetings actually stated",
            # Four-hourly rather than daily: an action item agreed at 09:30 with a
            # "by end of day" on it is worthless if it lands on the board tomorrow
            # morning. Cheap enough to do this often because a run with no new pages
            # is one Notion query and exits.
            cadence=Cadence(kind="every", hours=4),
            runner="ingest",
            # The one class of autostart Otto allows: read-only tools, and the only
            # writes are to Otto's own board and ledger. It files proposals; it does
            # not queue them. See config.MEETINGS_AUTOQUEUE.
            autostart=config.SCHEDULES_ARMED_BY_DEFAULT,
            # Generous against the 4h cadence because the weekend pause plus a
            # meeting-free stretch is normal. Two working days of total silence is
            # not, and that is what this catches.
            max_age_hours=48,
        ),
        Schedule(
            name="scout",
            domain=config.WORK,
            command="/scout",
            description="Hunt QoL improvements, write up to 3 tasks into the queue",
            cadence=Cadence(kind="weekly", days=["wed"], at="08:00", min_interval_days=5),
            # Otto's own gap detector flagged this as unwatched: with no alarm,
            # scout could stop forever and nothing would ever say so. Weekly with a
            # 5-day floor means 14 days of silence is genuinely wrong.
            max_age_hours=336,
        ),
    ]
    if config.SLACK_CHANNEL_ID:
        seeds.insert(3, Schedule(
            # Not a routine that runs: a freshness marker for the ops feed itself.
            # Stamped by daemon.slack_post when a summary is ACCEPTED by Slack, never
            # by the run that composed it, so this goes stale exactly when the
            # channel goes quiet. That is the whole point: /daily stamped itself
            # unconditionally, so between 2026-08-12 and 2026-08-20 the summary
            # silently did not post for eight days while every health surface read
            # green, and the only thing that noticed was the owner eventually scrolling
            # the channel.
            #
            # cadence manual: nothing should ever launch this, and `otto due` must
            # not list it as work. 26h matches /daily's own limit, so one missed
            # summary warns and three days of silence is crit.
            #
            # Seeded only when a feed channel is configured: with no channel the
            # marker could never be stamped and would alarm forever about a feed
            # that does not exist.
            name=config.FEED_POST_SCHEDULE,
            domain=config.WORK,
            command=f"otto post --channel {config.SLACK_CHANNEL_ID}",
            description=f"Freshness of #{config.SLACK_CHANNEL or config.SLACK_CHANNEL_ID} "
                        "itself: stamped only when a summary actually lands, so silence alarms",
            cadence=Cadence(kind="manual"),
            max_age_hours=26,
        ))
    return seeds
