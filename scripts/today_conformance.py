"""Conformance harness for the day view.

    python scripts\\today_conformance.py

Pure functions over dicts, so this needs no OTTO_HOME, no daemon, and no state. It
is also the only thing that can check the two cases that matter most and are
hardest to see by eye:

TIME. The day view is entirely about "what is left", so every claim it makes rests
on parsing a model-produced "HH:MM" correctly and comparing it to now. A silent
parse failure does not error, it just quietly reports a full day as already over,
which reads exactly like a quiet afternoon. That failure is invisible in the output
and obvious here.

THE TRIAGE DEFAULT. "Never promote unclassified mail to `reply`" is the property the
mail split is worth anything for. If `awareness` and unknown both drifted into
`reply`, the view would still look plausible while being a flat list again.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config, today  # noqa: E402

# This harness prints calendar titles, and calendar titles carry emoji. Piped into
# anything on Windows that means cp1252 and a UnicodeEncodeError halfway down the
# results, which is a green suite reported as a crash.
config.utf8_output()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def ag(*items) -> dict:
    return {"items": list(items), "fetched_at": "2026-08-05T00:00:00Z"}


NOON = datetime(2026, 8, 5, 12, 0).astimezone()

# ---------------------------------------------------------------------------
print("-- parsing a model's idea of a time -----------------------------------")

for raw, want in [
    ("09:05", time(9, 5)), ("9:05", time(9, 5)), ("23:59", time(23, 59)),
    ("00:00", time(0, 0)), ("1:30 PM", time(13, 30)), ("12:15 AM", time(0, 15)),
    ("12:15 PM", time(12, 15)), ("2026-08-05T14:00:00Z", None),  # tz-shifted, just not None
]:
    got = today._parse_hhmm(raw)
    ok = (got == want) if want is not None else (got is not None)
    check(f"parses {raw!r}", ok, f"got {got}, wanted {want}")

for bad in ["", None, "soon", "all day", "25:00", "10:99", "tomorrow"]:
    check(f"refuses {bad!r}", today._parse_hhmm(bad) is None,
          f"got {today._parse_hhmm(bad)}")

# ---------------------------------------------------------------------------
print("\n-- ordering and what is left ------------------------------------------")

snap = ag(
    {"when": "16:00", "title": "late"},
    {"when": "09:00", "title": "early"},
    {"title": "all-day thing"},
    {"when": "13:00", "title": "after lunch"},
)
evs = today.events(snap)
check("events sort by start time", [e.title for e in evs][:3] == ["early", "after lunch", "late"])
check("untimed events sort last", evs[-1].title == "all-day thing")
check("an untimed event is kept, not dropped", len(evs) == 4)

past, remaining, untimed = today.split(evs, NOON.time())
check("past is what already finished", [e.title for e in past] == ["early"])
check("remaining is what is left", [e.title for e in remaining] == ["after lunch", "late"])
check("untimed is separated", [e.title for e in untimed] == ["all-day thing"])

inprog = today.events(ag({"when": "11:30", "ends": "12:30", "title": "in progress"}))
_, rem, _ = today.split(inprog, NOON.time())
check("an event in progress counts as remaining, not past",
      [e.title for e in rem] == ["in progress"])

mins = today.minutes_until(today.events(ag({"when": "13:30", "title": "x"}))[0], NOON)
check("minutes_until is measured from now", mins == 90, f"got {mins}")
check("minutes_until is None for an untimed event",
      today.minutes_until(today.events(ag({"title": "x"}))[0], NOON) is None)

# ---------------------------------------------------------------------------
print("\n-- clashes ------------------------------------------------------------")

both_ends = today.events(ag(
    {"when": "10:00", "ends": "11:00", "title": "A"},
    {"when": "10:30", "ends": "11:30", "title": "B"},
))
check("real interval overlap is a clash", len(today.clashes(both_ends)) == 1)

touching = today.events(ag(
    {"when": "10:00", "ends": "11:00", "title": "A"},
    {"when": "11:00", "ends": "12:00", "title": "B"},
))
check("back-to-back is NOT a clash", today.clashes(touching) == [],
      f"got {len(today.clashes(touching))}")

same_start = today.events(ag(
    {"when": "12:00", "title": "Morgan / Harper"},
    {"when": "12:00", "title": "Social Hour"},
))
check("with no end times, an identical start is a clash",
      len(today.clashes(same_start)) == 1)

no_ends_apart = today.events(ag(
    {"when": "12:00", "title": "A"},
    {"when": "12:30", "title": "B"},
))
check("with no end times, different starts are NOT assumed to clash",
      today.clashes(no_ends_apart) == [],
      "a default duration would invent clashes that are not there")

untimed_pair = today.events(ag({"title": "A"}, {"title": "B"}))
check("two untimed events do not clash", today.clashes(untimed_pair) == [])

lines = today.render({"work/agenda": ag(
    {"when": "23:00", "title": "Morgan / Harper"},
    {"when": "23:00", "title": "Social Hour"},
)}, now=NOON)
check("a future clash is called out in the render",
      any("CLASH" in ln for ln in lines))
lines = today.render({"work/agenda": ag(
    {"when": "08:00", "title": "A"}, {"when": "08:00", "title": "B"},
)}, now=NOON)
check("a clash already lived through is not reported",
      not any("CLASH" in ln for ln in lines))

# ---------------------------------------------------------------------------
print("\n-- mail triage: the default is the whole point ------------------------")

ml = {"items": [
    {"when": "09:00", "title": "Tim: kit spec?", "needs": "reply", "why": "Tim is waiting"},
    {"when": "10:00", "title": "Dell AE check-in", "needs": "awareness"},
    {"when": "11:00", "title": "legacy row, no needs field"},
    {"when": "12:00", "title": "garbage value", "needs": "URGENT!!"},
], "fetched_at": "2026-08-05T00:00:00Z"}
ms = today.mail(ml)
by = {m.title: m.needs for m in ms}
check("an explicit reply is kept", by["Tim: kit spec?"] == "reply")
check("an explicit awareness is kept", by["Dell AE check-in"] == "awareness")
check("a missing needs becomes unsorted, NOT reply",
      by["legacy row, no needs field"] == "unsorted")
check("an unrecognized needs becomes unsorted, NOT reply",
      by["garbage value"] == "unsorted")
check("exactly one item is in the reply group",
      len([m for m in ms if m.needs == "reply"]) == 1)
check("the evidence clause is carried through",
      [m.why for m in ms if m.needs == "reply"] == ["Tim is waiting"])

lines = today.render({"work/mail": ml, "work/agenda": ag()}, now=NOON)
body = "\n".join(lines)
check("reply items are marked REPLY", "REPLY" in body)
check("awareness items are marked fyi", "fyi" in body)
check("unsorted items are marked as unknown, not as either group",
      any(ln.strip().startswith("?") for ln in lines))
check("the count line separates the two groups",
      "1 want a reply, 3 worth knowing" in body, body)

# ---------------------------------------------------------------------------
print("\n-- layout: work first, empty personal hidden ---------------------------")

snaps = {
    "personal/agenda": ag(), "personal/mail": {"items": [], "summary": "nothing"},
    "work/agenda": ag({"when": "13:00", "title": "standup"}),
    "work/mail": ml,
}
lines = today.render(snaps, now=NOON)
body = "\n".join(lines)
check("work renders", "WORK" in body)
check("an empty personal domain is hidden entirely", "PERSONAL" not in body,
      "two lines of nothing is what buried the work calendar")
check("work comes before anything else", body.index("WORK") < (
      body.index("PERSONAL") if "PERSONAL" in body else len(body)))

snaps["personal/agenda"] = ag({"when": "18:00", "title": "dinner"})
check("a personal domain with something in it does render",
      "PERSONAL" in "\n".join(today.render(snaps, now=NOON)))

check("a feed panel renders after the domains",
      "\n".join(today.render({**snaps, "work/feed:slack-dm": ag(
          {"when": "15:00", "who": "Tim", "title": "asked for the kit"})},
          now=NOON)).index("SLACK-DM") > body.index("WORK"))

check("nothing fetched at all says so",
      "otto refresh" in "\n".join(today.render({}, now=NOON)))

# ---------------------------------------------------------------------------
print("\n-- no truncation of what is left --------------------------------------")

many = ag(*[{"when": f"{h:02d}:00", "title": f"event {h}"} for h in range(13, 23)])
lines = today.render({"work/agenda": many}, now=NOON)
shown = [ln for ln in lines if "event " in ln]
check("all 10 remaining events are shown, not the first 6",
      len(shown) == 10, f"showed {len(shown)}")
check("the header counts what is left, not the total",
      any("10 left of 10" in ln for ln in lines))

mixed = ag(*[{"when": f"{h:02d}:00", "title": f"event {h}"} for h in range(8, 20)])
lines = today.render({"work/agenda": mixed}, now=NOON)
check("past events collapse to a count instead of listing",
      any("8 left of 12, 4 done" in ln for ln in lines),
      [ln for ln in lines if "left of" in ln])

nxt = today.next_event({"work/agenda": mixed}, now=NOON)
check("next_event finds the next one and its distance",
      nxt is not None and nxt[0].title == "event 12" and nxt[1] == 0,
      f"got {nxt}")

# ---------------------------------------------------------------------------
print("\n-- time blocks are not meetings ----------------------------------------")

# Real titles off a real calendar on one day, lightly anonymized. Verbatim shapes, because a classifier
# checked against titles someone invented for the test proves only that the test
# agrees with itself.
BLOCKS = [
    "Team Standups (self-organize in Discord, no central meeting)",
    "Team Work Time",
    "[daily] Social Hour",
    # Both forms of the same block. The refresher's model renamed it mid-day on
    # 2026-08-05, from the first to the second, and a pattern anchored on "(Reclaim)"
    # matched only the morning version.
    "\U0001f6e1 \U0001f371 Lunch (Reclaim)",
    "Lunch (Reclaim hold)",
    "\U0001f60e Decompress (Reclaim)",
    "Decompress (Reclaim hold)",
    "Focus time",
    "OOO",
]
MEETINGS = [
    "[weekly] Townhall 2.0",
    "Morgan / Harper",
    "Acme + Vendor | Intro Call",
    "Lunch with Kim",            # a real call that happens to be at noon
    "HOLD: publisher demo dry run",
    "Milestone build review",
]
for t in BLOCKS:
    check(f"block: {t[:34]}", today.is_block(t))
for t in MEETINGS:
    check(f"meeting: {t[:34]}", not today.is_block(t))

# The whole point: keep walking past blocks to the meeting behind them.
day = ag({"when": "12:30", "title": "Team Work Time"},
         {"when": "13:00", "title": "[daily] Social Hour"},
         {"when": "14:00", "title": "[weekly] Townhall 2.0"})
nxt = today.next_event({"work/agenda": day}, now=NOON)
check("next_event walks past two blocks to the real meeting",
      nxt is not None and nxt[0].title == "[weekly] Townhall 2.0", f"got {nxt}")
check("...and reports the distance to THAT one, not to the block",
      nxt is not None and nxt[1] == 120, f"got {nxt}")
lit = today.next_event({"work/agenda": day}, now=NOON, skip_blocks=False)
check("skip_blocks=False still returns the literal next entry",
      lit is not None and lit[0].title == "Team Work Time")

only = ag({"when": "12:30", "title": "Team Work Time"},
          {"when": "13:00", "title": "\U0001f371 Lunch (Reclaim)"})
check("a day with nothing but blocks left has no next meeting",
      today.next_event({"work/agenda": only}, now=NOON) is None)
check("...and blocks_left names them, so a surface can say what it skipped",
      [e.title for e in today.blocks_left({"work/agenda": only}, now=NOON)]
      == ["Team Work Time", "\U0001f371 Lunch (Reclaim)"])
check("blocks_left is forward-looking only",
      today.blocks_left({"work/agenda": ag({"when": "09:00", "title": "Team Work Time"})},
                        now=NOON) == [])

# `daemon._clear_block_preps` matches the NOTICE title, which is the event title with
# a time in front and an attendee list behind it. Both of these were on the owner's
# Today screen when the filter shipped, so they are the cleanup's actual input.
check("a posted block brief is recognizable from its notice title",
      today.is_block("10:30 Team Work Time with Alex Rivera, Riley Kim")
      and today.is_block("10:00 Team Standups (self-organize in Discord, with Sam"))
check("...and a real meeting's brief is not",
      not today.is_block("13:00 [weekly] Townhall 2.0 with Sam Lee, Casey Park"))

# The day view still shows them. Filtering interruptions is not the same as hiding
# the day: a day with Work Time in it is a different day.
shown = "\n".join(today.render({"work/agenda": day}, now=NOON))
check("the day view still renders every block",
      all(t in shown for t in ("Team Work Time", "Social Hour")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
