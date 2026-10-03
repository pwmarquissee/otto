"""Conformance harness for meeting prep, aimed almost entirely at name matching.

    python scripts\\prep_conformance.py

WHY THIS FILE IS MOSTLY ABOUT ONE FUNCTION. Everything else in prep.py is a join
over stores that already have their own harnesses. `match_names` is the part that
can be confidently, invisibly wrong: it takes a string off a calendar and decides
which colleague's role, working style, pronouns, and private notes to put in front
of the owner. A wrong match does not error. It produces a complete, plausible, correctly
formatted brief about the wrong person, moments before they speak to the real one.

So the assertions here are mostly about what it must REFUSE to do. Two people
sharing a first name must produce an ambiguity, not a coin flip. A partial name
must not match. An unrecognized attendee must be reported rather than dropped, so
"no dossier" never renders as "nobody worth prepping for".

Runs against a synthetic roster, not the live one, so the assertions stay true when
someone joins or leaves the company.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# names_in_title() drops the org's and the owner's own names at call time, read from
# config. Pin both here so the title fixtures below mean the same thing on every
# machine, whatever the operator's .env says.
os.environ["OTTO_OWNER_NAME"] = "Morgan Reyes"
os.environ["OTTO_ORG_NAME"] = "Acme"

from otto import prep  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def person(slug, first, last, **kw) -> dict:
    return {"slug": slug, "firstName": first, "lastName": last,
            "display_name": f"{first} {last}", "login": f"{slug}@example.com",
            "email": f"{slug}@example.com", "meta": {}, **kw}


# Everyone here is invented.
ROSTER = [
    person("hlane", "Harper", "Lane", title="Head of People"),
    person("tsato", "Tim", "Sato", title="CFO"),
    person("arivera", "Avery", "Rivera"),
    person("adiaz", "Anna", "Diaz"),
    # Two Chrises on purpose: the ambiguity case is the whole point.
    person("clynch", "Chris", "Lynch"),
    person("cmoore", "Chris", "Moore"),
]


def m(*raws):
    return prep.match_names(list(raws), roster=ROSTER)


# ---------------------------------------------------------------------------
print("-- exact matching -----------------------------------------------------")

got, un, amb = m("hlane@example.com")
check("an email matches", [x.person["slug"] for x in got] == ["hlane"])
check("...and is recorded as an email match", got[0].how == "email")

got, _, _ = m("HLane@Example.COM")
check("email matching is case insensitive", [x.person["slug"] for x in got] == ["hlane"])

got, _, _ = m("Harper Lane")
check("a full name matches", [x.person["slug"] for x in got] == ["hlane"])
check("...and is recorded as a full-name match", got[0].how == "full-name")

got, _, _ = m("  harper   lane  ")
check("whitespace and case in a full name are tolerated",
      [x.person["slug"] for x in got] == ["hlane"])

got, _, _ = m("hlane")
check("a bare login matches", [x.person["slug"] for x in got] == ["hlane"])

# ---------------------------------------------------------------------------
print("\n-- the unique-first-name rule -----------------------------------------")

got, un, amb = m("Harper")
check("a first name matches when only one person has it",
      [x.person["slug"] for x in got] == ["hlane"])
check("...and says so, so the brief can be read skeptically",
      got[0].how == "unique-first-name")

got, un, amb = m("Chris")
check("a shared first name matches NOBODY", got == [], f"matched {got}")
check("...and is reported as ambiguous", len(amb) == 1)
check("...naming both candidates",
      amb[0][1] == ["Chris Lynch", "Chris Moore"], f"got {amb}")
check("...and is not silently dropped into unmatched", un == [])

# ---------------------------------------------------------------------------
print("\n-- what it must refuse --------------------------------------------------")

for partial in ["Ann", "Han", "Ti", "Lan", "Riv"]:
    got, un, amb = m(partial)
    check(f"{partial!r} does not partially match anyone",
          got == [] and amb == [], f"matched {[x.person['slug'] for x in got]}")

got, un, _ = m("Anna")
check("'Anna' matches Anna, not by prefix from Avery",
      [x.person["slug"] for x in got] == ["adiaz"])

got, un, _ = m("simone@vendor.example")
check("an external attendee is unmatched, not forced onto someone",
      got == [] and un == ["simone@vendor.example"])

got, un, _ = m("hlane@example.com", "Harper Lane", "Harper")
check("the same person named three ways appears once",
      [x.person["slug"] for x in got] == ["hlane"], f"got {got}")

got, un, amb = m("", "   ")
check("empty attendee strings are ignored entirely",
      got == [] and un == [] and amb == [])

# ---------------------------------------------------------------------------
print("\n-- reading names out of a calendar title -------------------------------")

check("a slash-separated title yields the person",
      "Harper" in prep.names_in_title("Morgan / Harper"))
check("the owner is not a candidate",
      "Morgan" not in prep.names_in_title("Morgan / Harper"))
for noise in ["Sync: VO Lines Naming & Generation", "Team Work Time",
              "[daily] Social Hour", "Decompress (Reclaim)", "Lunch (Reclaim)"]:
    cands = prep.names_in_title(noise)
    got, _, amb = prep.match_names(cands, roster=ROSTER)
    check(f"{noise[:30]!r} matches nobody", got == [] and amb == [],
          f"candidates {cands} matched {[x.person['slug'] for x in got]}")

got, un, _ = prep.match_names(
    prep.names_in_title("Acme + Vendor | Intro Call"), roster=ROSTER)
check("a company name in a title matches no person", got == [])

title_hits, _, _ = prep.match_names(
    prep.names_in_title("Morgan / Harper"), roster=ROSTER)
check("the title fallback finds the right person",
      [x.person["slug"] for x in title_hits] == ["hlane"])

# ---------------------------------------------------------------------------
print("\n-- the fetched attendee list wins over the title -----------------------")

ev = prep.today.events({"items": [
    {"when": "12:00", "title": "Morgan / Harper", "who": "tsato@example.com"}]})[0]
raw = {"title": "Morgan / Harper", "who": "tsato@example.com"}
check("`who` is used instead of the title when present",
      prep.attendees(ev, raw) == ["tsato@example.com"])
check("a comma-separated who is split",
      prep.attendees(ev, {"who": "a@x.test, b@x.test"}) == ["a@x.test", "b@x.test"])
check("an 'and'-joined who is split",
      prep.attendees(ev, {"who": "Harper and Tim"}) == ["Harper", "Tim"])
check("a list-valued who works too",
      prep.attendees(ev, {"who": ["a@x.test", "b@x.test"]}) == ["a@x.test", "b@x.test"])
check("no who at all falls back to the title",
      prep.attendees(ev, {"title": "Morgan / Harper"}) == prep.names_in_title("Morgan / Harper"))
check("an empty who falls back rather than yielding nothing",
      prep.attendees(ev, {"title": "Morgan / Harper", "who": "  "})
      == prep.names_in_title("Morgan / Harper"))

# ---------------------------------------------------------------------------
print("\n-- pronouns are never inferred -----------------------------------------")

from otto import people  # noqa: E402

check("an unrecorded pronoun defaults to they/them",
      people.pronouns_of({}) == "they/them")
check("a recorded pronoun is used verbatim",
      people.pronouns_of({"pronouns": "he/him"}) == "he/him")
src = Path(prep.__file__).read_text(encoding="utf-8")

# The property that matters is about the RENDERED BRIEF, not about prose in the
# source. An earlier version of this assertion scanned the whole file for the words
# she/her/him/his, which failed on the docstring that explains why pronouns must not
# be inferred. It was measuring the wrong thing: what must never happen is a pronoun
# Otto chose appearing in output about a real person.
class _FakeStore:
    def tasks(self): return []
    def decisions(self): return []


nopron = person("xtest", "Sam", "Taylor", title="Engineer")
_orig = prep._sections
prep._sections = lambda p: {"Threads": ["- owes a reply on the migration"]}
try:
    out = "\n".join(prep.person_lines(_FakeStore(), prep.Match(nopron, "email", "x")))
finally:
    prep._sections = _orig

check("a brief for someone with no recorded pronouns says they/them",
      "they/them" in out, out)
check("...and marks it as the default rather than as fact", "not recorded" in out)
check("...and contains no pronoun Otto picked",
      not any(f" {w} " in f" {out.lower()} "
              for w in ("she", "her", "hers", "he", "him", "his")),
      out)

# ---------------------------------------------------------------------------
print("\n-- group meetings collapse -------------------------------------------")

class _Store:
    def __init__(self, tasks=()): self._t = list(tasks)
    def tasks(self): return self._t
    def decisions(self): return []


class _Task:
    def __init__(self, title, priority="normal"):
        self.title, self.priority, self.status = title, priority, "backlog"
        self.detail, self.updated, self.id = None, "2026-08-05T00:00:00Z", "abc123"


roster8 = [person(f"p{i}", f"First{i}", f"Last{i}") for i in range(8)]
matched8 = [prep.Match(p, "email", p["email"]) for p in roster8]
few = matched8[:2]

prep._sections = lambda p: {"Threads": ["- an open thread"],
                            "Working style": ["- likes async"],
                            "What we work on": ["- the thing"]}
try:
    big = "\n".join(prep.brief_lines(_Store(), prep.Prep(None, None, matched8, [], [])))
    small = "\n".join(prep.brief_lines(_Store(), prep.Prep(None, None, few, [], [])))
    check(f"above {prep.config.PREP_MAX_PEOPLE} people the brief collapses",
          "on the invite with dossiers" in big)
    check("...dropping working style, which is 1:1 preparation",
          "likes async" not in big, big[:200])
    check("...but keeping what is open", "an open thread" in big)
    check("at or below the cap, the full brief renders",
          "likes async" in small and "on the invite with dossiers" not in small)

    check("a brief with something open is actionable",
          prep.actionable(_Store(), prep.Prep(None, None, few, [], [])))

    prep._sections = lambda p: {}
    check("a brief with nobody owed anything is NOT actionable",
          not prep.actionable(_Store(), prep.Prep(None, None, few, [], [])),
          "a toast here would only restate the calendar")
    check("...unless a board card names them",
          prep.actionable(_Store([_Task("ping First0 Last0 about it")]),
                          prep.Prep(None, None, few, [], [])))
    check("an empty match list is never actionable",
          not prep.actionable(_Store(), prep.Prep(None, None, [], ["x@y.test"], [])))
finally:
    prep._sections = _orig

print("\n-- nothing to prep for --------------------------------------------------")


class _Snap:
    """Minimal stand-in for a stored snapshot: prep.render calls model_dump()."""

    def __init__(self, items): self._items = items
    def model_dump(self): return {"items": self._items}


class _CalStore(_Store):
    def __init__(self, items): super().__init__(); self._items = items
    def snapshots(self): return {f"{prep.config.WORK}/agenda": _Snap(self._items)}


# The distinction that matters here is between "your calendar is empty" and "your
# calendar is full of things Otto refuses to prep for". Printing the first when the
# second is true sends the owner to debug a working calendar feed.
# A fixed clock, passed in. Fixture times of "23:30" against the real one would make
# this harness pass all day and fail between 23:30 and midnight.
NOON = datetime(2026, 8, 5, 12, 0).astimezone()
blocked = prep.render(_CalStore([
    {"when": "12:30", "title": "Team Work Time"},
    {"when": "13:00", "title": "[daily] Social Hour"},
]), None, now=NOON)
check("with only blocks left, prep says so instead of 'nothing on the calendar'",
      "no meetings left today" in blocked and "nothing left on today's" not in blocked,
      blocked)
check("...and names the blocks it skipped",
      "Team Work Time" in blocked and "Social Hour" in blocked, blocked)
check("a genuinely empty calendar still says the calendar is empty",
      "nothing left on today's calendar" in prep.render(_CalStore([]), None, now=NOON))

print("\n-- read-only ----------------------------------------------------------")

check("prep.py never writes state", "_write" not in src and "put_" not in src)
check("prep.py never deletes anything", "delete" not in src)
check("prep.py makes no model call", "subprocess" not in src and "spawn" not in src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
