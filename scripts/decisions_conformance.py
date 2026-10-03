"""Conformance harness for the decision log and the retire detectors.

Same shape and same reasoning as `feeds_conformance.py`: runs against a throwaway
OTTO_HOME, never touches live state, never needs the daemon.

    python scripts\\decisions_conformance.py

TWO CLAIMS THIS FILE EXISTS FOR.

APPEND-ONLY IS A PROPERTY, NOT A CONVENTION. "A decision cannot be edited or
deleted" is the whole basis for trusting the log to answer "what did I think in
August". A claim nothing re-checks is just a comment, so the absence of an edit
path is asserted here against the Store's actual surface rather than assumed from
the docstring.

THE RETIRE DETECTORS CANNOT FIRE ON LIVE STATE YET. Otto's state was 8 days old
when they shipped: nothing has aged past a 30-day threshold, no finding has been
refiled 4 times, every schedule has run. So live state proves nothing about them
and synthetic aged state is the only thing that can. Five detectors verified by
"the report printed no rows" would be five detectors verified by nothing.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-decisions-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
os.environ["OTTO_FEED_DIR"] = str(TMP / "feed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import Cadence, Decision, RegistryEntry, Schedule, Task, iso, utcnow  # noqa: E402

config.mark_daemon()
config.ensure_dirs()

from otto import decisions, retire  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def ago(days: float) -> str:
    return iso(utcnow() - timedelta(days=days))


def day_offset(days: int) -> str:
    return iso(utcnow() + timedelta(days=days))[:10]


# ---------------------------------------------------------------------------
print("-- recording ----------------------------------------------------------")

d = decisions.build("Ship the thing", decision="Ship it Tuesday",
                    why="The window closes Wednesday")
store.add_decision(d)
check("a decision is recorded", len(store.decisions()) == 1)
check("the id is short and typable", len(d.id) <= 10 and d.id.startswith("d-"))
check("decided defaults to a date, not a timestamp", len(d.decided) == 10)

for bad, label in [
    (dict(title="", decision="x", why="y"), "no title"),
    (dict(title="t", decision="", why="y"), "no decision"),
    (dict(title="t", decision="x", why=""), "no why"),
    (dict(title="t", decision="x", why="   "), "whitespace-only why"),
]:
    try:
        decisions.build(**bad)
        check(f"{label} is refused", False, "build accepted it")
    except ValueError:
        check(f"{label} is refused", True)

try:
    decisions.build("t", decision="x", why="y", revisit_by=day_offset(5))
    check("a revisit date with no condition is refused", False, "accepted")
except ValueError:
    check("a revisit date with no condition is refused", True)

try:
    store.add_decision(decisions.build("Ship the thing", decision="Ship it Tuesday",
                                       why="The window closes Wednesday"))
    check("recording the identical decision twice is refused", False, "accepted")
except ValueError:
    check("recording the identical decision twice is refused", True)

# ---------------------------------------------------------------------------
print("\n-- append-only: the load-bearing property -----------------------------")

surface = [m for m in dir(store) if "decision" in m]
check("there is no edit/update method for a decision",
      not any(m in surface for m in ("update_decision", "edit_decision",
                                     "upsert_decision", "put_decision")),
      f"found {surface}")
check("there is no delete method for a decision",
      "delete_decision" not in surface, f"found {surface}")

src = Path(decisions.__file__).read_text(encoding="utf-8")
check("decisions.py never writes state itself", "_atomic_write" not in src
      and "_write(" not in src)

# ---------------------------------------------------------------------------
print("\n-- supersede ----------------------------------------------------------")

new = decisions.build("Ship the thing later", decision="Ship it Thursday",
                      why="QA found a blocker on Tuesday's build")
old, new = store.supersede_decision(d.id, new)
check("the old decision still exists", store.get_decision(d.id) is not None)
check("the old one is marked superseded", store.get_decision(d.id).superseded_by == new.id)
check("the new one points back", store.get_decision(new.id).supersedes == d.id)
check("the old one is no longer live", not store.get_decision(d.id).live)
check("superseded decisions are hidden by default",
      d.id not in decisions.render(store))
check("...and visible when asked for",
      d.id in decisions.render(store, include_superseded=True))

try:
    store.supersede_decision(d.id, decisions.build("again", decision="x", why="y"))
    check("double-superseding the same decision is refused", False, "accepted")
except ValueError:
    check("double-superseding the same decision is refused", True)

# ---------------------------------------------------------------------------
print("\n-- revisit: the field that is a control, not a note --------------------")

watched = decisions.build("Use vendor X", decision="Sign with X for a year",
                          why="Cheapest that clears the security bar",
                          revisit="X raises price or misses the SLA twice")
store.add_decision(watched)
g = {r["id"]: r for r in decisions.gaps(store)}
check("a condition with no date is reported as unwatched",
      f"gap:decision-unwatched:{watched.id}" in g)
check("...and explains why that makes it a note",
      "note rather than a control" in g[f"gap:decision-unwatched:{watched.id}"]["why"])

store.set_revisit_by(watched.id, day_offset(30))
g = {r["id"]: r for r in decisions.gaps(store)}
check("a future date silences the unwatched gap",
      f"gap:decision-unwatched:{watched.id}" not in g
      and f"gap:decision-revisit:{watched.id}" not in g)

store.set_revisit_by(watched.id, day_offset(-20))
g = {r["id"]: r for r in decisions.gaps(store)}
row = g.get(f"gap:decision-revisit:{watched.id}")
check("a past date raises it for a rethink", row is not None)
check("...carrying the condition to check, not just the title",
      row is not None and "misses the SLA" in row["why"])
check("...and scores higher the longer it is overdue", row is not None and row["score"] >= 75)

try:
    store.set_revisit_by(new.id, day_offset(10))
    check("a date on a decision with no condition is refused", False, "accepted")
except ValueError:
    check("a date on a decision with no condition is refused", True)

store.set_revisit_by(watched.id, None)
check("a review date can be cleared", store.get_decision(watched.id).revisit_by is None)

superseded_id = d.id
try:
    store.set_revisit_by(superseded_id, day_offset(10))
    check("a superseded decision cannot be scheduled", False, "accepted")
except ValueError:
    check("a superseded decision cannot be scheduled", True)

check("only revisit_by is mutable: the decision text is unchanged",
      store.get_decision(watched.id).decision == "Sign with X for a year")

# ---------------------------------------------------------------------------
print("\n-- search and linkage -------------------------------------------------")

check("search finds by body text, not just title",
      [x.id for x in decisions.search(store, "security bar")] == [watched.id])
check("search is AND across terms",
      decisions.search(store, "security nonexistentword") == [])
linked = decisions.build("Card-linked call", decision="Do it in-house",
                         why="Vendor quote was 4x", task_id="abc123")
store.add_decision(linked)
check("a decision can be found from its board card",
      [x.id for x in decisions.for_task(store, "abc123")] == [linked.id])

# ---------------------------------------------------------------------------
print("\n-- retire detectors, against synthetic aged state ---------------------")

def task(**kw) -> Task:
    base = dict(id=kw.pop("id"), title=kw.pop("title", "t"), status="backlog",
                updated=kw.pop("updated", iso(utcnow())))
    return Task(**{**base, **kw})

store.save_tasks([
    task(id="fresh1", title="written today"),
    task(id="stale1", title="quiet a long time", updated=ago(45)),
    task(id="stale2", title="quiet too", updated=ago(31)),
    task(id="notstale", title="just inside the line", updated=ago(29)),
    task(id="blocked1", title="blocked and old", status="blocked", updated=ago(90)),
    task(id="done1", title="done and old", status="done", updated=ago(90)),
    task(id="nag1", title="keeps coming back", seen_count=7),
    task(id="nag2", title="also nags", seen_count=4),
    task(id="nag3", title="under the bar", seen_count=3),
])

stale_ids = {t.id for t in retire.stale_cards(store.tasks())}
check("a card quiet past the threshold is a candidate", stale_ids == {"stale1", "stale2"},
      f"got {stale_ids}")
check("a blocked card is never called stale (it is waiting on something else)",
      "blocked1" not in stale_ids)
check("a done card is never called stale", "done1" not in stale_ids)
check("stale candidates are oldest-first",
      [t.id for t in retire.stale_cards(store.tasks())][0] == "stale1")

nag_ids = [t.id for t in retire.nagging_cards(store.tasks())]
check("a finding refiled past the threshold is a candidate",
      set(nag_ids) == {"nag1", "nag2"}, f"got {nag_ids}")
check("nagging candidates are worst-first", nag_ids[0] == "nag1")

store.save_schedules([
    Schedule(name="healthy", command="/x", cadence=Cadence(kind="daily"),
             created=ago(60), last_run=iso(utcnow())),
    Schedule(name="never-ran", command="/y", cadence=Cadence(kind="daily"),
             created=ago(40)),
    Schedule(name="too-new-to-judge", command="/z", cadence=Cadence(kind="daily"),
             created=ago(3)),
    Schedule(name="manual-only", command="/m", cadence=Cadence(kind="manual"),
             created=ago(60)),
    Schedule(name="braked", command="/b", cadence=Cadence(kind="daily"),
             created=ago(60), last_run=ago(9),
             disabled_reason="3 consecutive failures"),
])

never = {s.name for s in retire.never_ran(store.schedules())}
check("a schedule that never ran is a candidate", never == {"never-ran"}, f"got {never}")
check("a brand-new schedule is not judged for never running",
      "too-new-to-judge" not in never)
check("a manual schedule is never judged for never running", "manual-only" not in never)
check("a schedule the brake disabled is a candidate",
      {s.name for s in retire.broken_brakes(store.schedules())} == {"braked"})

store.save_registry([
    RegistryEntry(name="alive", kind="command", scope="global", path="a"),
    RegistryEntry(name="long-gone", kind="command", scope="global", path="b",
                  missing=True, last_seen=ago(30)),
    RegistryEntry(name="just-went", kind="agent", scope="global", path="c",
                  missing=True, last_seen=ago(2)),
])
gone = {e.name for e in retire.missing_definitions(store)}
check("a definition gone a long time is a candidate", gone == {"long-gone"}, f"got {gone}")
check("a definition that just vanished is left alone (recent deletion is news)",
      "just-went" not in gone)

# ---------------------------------------------------------------------------
print("\n-- retire is read-only, by construction -------------------------------")

rsrc = Path(retire.__file__).read_text(encoding="utf-8")
check("retire.py never writes state", "_write" not in rsrc and "_atomic_write" not in rsrc)
check("retire.py never deletes a task", "delete_task" not in rsrc)
check("retire.py never deletes a schedule", "delete_schedule" not in rsrc)
check("retire.py never upserts anything", "upsert" not in rsrc)

before = (len(store.tasks()), len(store.schedules()), len(store.registry()))
retire.gaps(store)
retire.render(store)
after = (len(store.tasks()), len(store.schedules()), len(store.registry()))
check("running the whole report changes no state", before == after)

rows = {r["id"]: r for r in retire.gaps(store)}
check("stale cards are ONE grouped row, not one row per card",
      len([k for k in rows if k.startswith("gap:retire-stale")]) == 1)
check("nothing retire reports outranks an outage",
      all(r["score"] <= 45 for r in rows.values()),
      f"max {max((r['score'] for r in rows.values()), default=0)}")

report = retire.render(store)
check("the report says it deletes nothing", "deleted" in report or "deletes nothing" in report)
check("the report names the worst nagging card", "keeps coming back" in report)

print("\n-- render -------------------------------------------------------------")
print("\n".join("    " + ln for ln in retire.render(store).splitlines()))
print("\n".join("    " + ln for ln in decisions.render(store).splitlines()))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
