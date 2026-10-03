"""Conformance harness for the unprompted nudges.

    python scripts\\nudges_conformance.py

WHAT THIS FILE IS FOR. Otto gained three new reasons to interrupt the owner, and an
interruption that is wrong in either direction is expensive: too quiet and the fade it
was built to catch happens anyway, too loud and they mute the channel, which costs them
the notices that mattered. Neither failure shows up in a screenshot.

  1. THE BAND HAS TWO SIDES. Measured on a real set of dossiers: 38 dated
     threads past 7 days, oldest 156. A one-sided threshold makes the daily notice a
     38-item nag, which is the exact failure the owner asked to be rid of twice in one
     day. Above the ceiling the count is STATED but the items are not listed.
  2. IT NEVER SAYS THE OWNER OWES ANYBODY ANYTHING. A Threads bullet is not proof of a debt;
     prep.py refuses the same inference. The notice says "open since". This is asserted
     on the rendered text, because it is a claim about a colleague.
  3. DEDUP IS REAL. The tick loop runs every few seconds. Threads are weekly, overdue
     and milestones daily, and each must go quiet after speaking once.
  4. NOTHING FIRES ON AN EMPTY WORLD. No milestones in the horizon, no overdue cards,
     no stale threads: no notices at all. A nudge system that always has something to
     say is one that gets muted.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-nudges-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import Task, iso, utcnow  # noqa: E402

config.utf8_output()
config.mark_daemon()
config.ensure_dirs()

from otto import nudges, people, prep  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []
TODAY = date(2026, 8, 5)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


# A fake roster with fake dossiers. The real ones are the owner's colleagues and this
# harness must not depend on what is written about them today.
THREADS = {
    "tlh": [f"- {ago(2)}: fresh, inside the floor",
            f"- {ago(9)}: the owner owes real vendor quotes on the buyback proposal",
            f"- {ago(25)}: vendor appetite question has no recorded decision",
            f"- {ago(156)}: gitlfs missing from the installer args",
            "- no date on this one at all, skipped rather than guessed"],
    "ext1": [f"- {ago(30)}: an external contact's thread"],
}
ROSTER = [
    {"slug": "tlh", "login": "tlh@example.com", "display_name": "Sam Lee",
     "external": False},
    {"slug": "ext1", "login": "klee@vendor.example", "display_name": "Kim Lee",
     "external": True},
]
people.load = lambda: ROSTER  # type: ignore[assignment]
prep._sections = lambda p: {"Threads": THREADS.get(p["slug"], [])}  # type: ignore[assignment]

print(f"\n  NUDGES CONFORMANCE   band {config.NUDGE_THREAD_STALE_DAYS}"
      f"-{config.NUDGE_THREAD_MAX_DAYS}d   home {TMP}\n")

print("-- the band has two sides ---------------------------------------------")
rows = nudges.stale_threads(store, TODAY)
ages = sorted(r[2] for r in rows)
check("a thread inside the band is picked up", 9 in ages, str(ages))
check("...and one at the ceiling too", 25 in ages, str(ages))
check("a thread younger than the floor is NOT", 2 not in ages, str(ages))
check("a 156-day-old thread is NOT a nudge", 156 not in ages, str(ages))
check("an undated bullet is skipped, never guessed at", len(rows) == 2, str(rows))
check("an external contact's thread is not the owner's thread",
      all("Kim" not in r[0] for r in rows), str(rows))
check("dated_threads still sees everything, band or not",
      len(nudges.dated_threads(store, TODAY)) == 4,
      str(len(nudges.dated_threads(store, TODAY))))

print("\n-- what the notice actually says --------------------------------------")
nudges.open_threads_notice(store, TODAY)
n = next((x for x in store.notices() if x.source == "threads-quiet"), None)
check("a notice was posted", n is not None)
body = (n.body or "") if n else ""
title = (n.title or "") if n else ""
check("it says 'open since', which is true",
      "open since" in body, body[:120])
check("it never claims the owner owes anybody anything",
      "you owe" not in f"{title} {body}".lower(),
      "a nudge asserting a debt over a colleague's thread")
check("the over-ceiling count is stated", "156d" in body or "over 30d" in body, body)
check("...but the over-ceiling items are not listed",
      "gitlfs" not in body, "listed a 156-day-old thread it claimed not to list")
check("it points at the right verb for the old ones", "otto retire" in body, body)
check("it does not interrupt: threads are a review, not an alarm",
      n is not None and n.level == "info", n.level if n else "")

print("\n-- dedup -------------------------------------------------------------")
before = len(store.notices())
nudges.open_threads_notice(store, TODAY)
check("posting twice the same day does nothing", len(store.notices()) == before)
nudges.open_threads_notice(store, TODAY + timedelta(days=3))
check("...nor three days later, threads are weekly",
      len(store.notices()) == before)
# Advancing the clock has to move the NOTICES too, not just the date handed to
# nudges. There are two dedupes in play and they read different things: nudges
# compares its `today` against notice dates, while notify.post compares wall-clock
# against `at`. Passing a future date without ageing the store leaves the two
# disagreeing about what day it is, and the assertion below silently tested nothing
# once notify gained a dedupe of its own.
for _n in store.notices():
    _n.at = iso(utcnow() - timedelta(days=9))
    store.put_notice(_n)

nudges.open_threads_notice(store, TODAY + timedelta(days=8))
check("...but a week later it speaks again", len(store.notices()) == before + 1)

print("\n-- overdue cards -----------------------------------------------------")
store.save_tasks([
    Task(id="t1", title="late by a fortnight", status="backlog", due=ago(14)),
    Task(id="t2", title="late by a day", status="backlog", due=ago(1)),
    Task(id="t3", title="due tomorrow", status="backlog",
         due=(TODAY + timedelta(days=1)).isoformat()),
    Task(id="t4", title="finished and late", status="done", due=ago(30)),
    Task(id="t5", title="no due date at all", status="backlog"),
])
od = nudges.overdue(store, TODAY)
check("overdue finds the late ones", {t.id for t, _ in od} == {"t1", "t2"},
      str([t.id for t, _ in od]))
check("...worst first", od[0][0].id == "t1")
check("a done card is never overdue", all(t.id != "t4" for t, _ in od))
nudges.overdue_notice(store, TODAY)
o = next((x for x in store.notices() if x.source == "overdue"), None)
check("the overdue notice fires", o is not None)
check("...at warn, because two weeks late is not an FYI",
      o is not None and o.level == "warn", o.level if o else "")
before = len(store.notices())
nudges.overdue_notice(store, TODAY)
check("...once a day", len(store.notices()) == before)

print("\n-- milestones --------------------------------------------------------")
config.MILESTONES = [("Gamescom (Cologne)", "2026-08-25", config.WORK),
                     ("Something next year", "2027-06-01", config.WORK)]
ms = nudges.milestones(TODAY)
check("a milestone inside the horizon is counted", len(ms) == 1, str(ms))
check("...with the right distance", ms[0][2] == 20, str(ms))
check("one beyond the horizon is not", all("next year" not in m[0] for m in ms))
store.save_tasks(list(store.tasks()) + [
    Task(id="t6", title="Order the Gamescom network kit", status="backlog")])
nudges.milestone_notice(store, TODAY)
m = next((x for x in store.notices() if x.source == "milestone"), None)
check("the milestone notice fires", m is not None)
check("...and names the open cards against it",
      m is not None and "network kit" in (m.body or ""), m.body if m else "")
check("...at info at 20 days out, not warn",
      m is not None and m.level == "info", m.level if m else "")
check("a milestone a week out DOES interrupt",
      nudges.milestones(date(2026, 8, 20))[0][2] == 5)

print("\n-- an empty world is silent ------------------------------------------")
shutil.rmtree(TMP, ignore_errors=True)
config.ensure_dirs()
quiet = Store()
config.MILESTONES = []
THREADS.clear()
notes = nudges.tick(quiet, TODAY)
check("nothing to say means nothing said",
      notes == [] and quiet.notices() == [], f"{notes} / {len(quiet.notices())}")

print()
print(f"  {len(PASS)} ok, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
