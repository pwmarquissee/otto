"""Conformance harness for the check-in signals and the pattern readback.

    python scripts\\wellbeing_conformance.py

WHAT THIS FILE IS FOR. This feature exists to answer "does kayaking actually help",
and the ways it can be wrong are all quiet ones. Nothing crashes. The numbers just
stop meaning what they appear to mean, and the owner acts on them anyway.

  1. UNKNOWN IS NOT NO. The single most important property. A day he did not answer
     "did you get outside" must never be counted as a day he stayed in, or every
     comparison below is poisoned by silence and the poison is invisible.
  2. IT REFUSES TO COMPARE ON THIN DATA. Nine days can produce a two-point "effect"
     that is one bad Tuesday. Below MIN_PER_SIDE on either side there is no delta at
     all, and the counts are shown so the refusal is checkable.
  3. THE NOTE STILL STANDS ALONE. The prior design decision was "a form he will not
     fill in is worth less than a sentence he will". A check-in with prose and no
     signals must remain complete, and a partial answer must be recordable.
  4. IT NEVER INVENTS ADVICE. `suggestion` returns None rather than something
     encouraging when nothing has evidence. The profile explicitly rejects generic
     encouragement, so a suggestion with no data behind it is worse than silence.
  5. MERGING, NOT REPLACING. Signals added in the evening must join the morning's
     note rather than overwrite it, or the ritual punishes doing it in two passes.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-wellbeing-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402

config.utf8_output()
config.mark_daemon()
config.ensure_dirs()

from otto import wellbeing as w  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []
TODAY = date(2026, 8, 30)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def head(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def day(offset: int) -> str:
    return (TODAY - timedelta(days=offset)).isoformat()


def record(offset: int, note=None, **signals) -> None:
    ci = {"note": note} if note else {}
    ci.update({k: v for k, v in signals.items() if v is not None})
    store.put_day(day(offset), {"checkin": ci})


# ============================================================================
head("1. unknown is not no")

record(1, energy=5, kayak=True)
record(2, energy=1)                       # kayak NOT answered
record(3, energy=1, kayak=False)

rows = w.series(store, today=TODAY)
kayak_vals = [r.get("kayak") for r in rows]
check("a signal that was never answered is absent, not False",
      None not in [r.get("kayak", "MISSING") for r in rows] or
      any("kayak" not in r for r in rows),
      str(kayak_vals))
check("the unanswered day is excluded from BOTH sides",
      len([r for r in rows if r.get("kayak") is True]) == 1
      and len([r for r in rows if r.get("kayak") is False]) == 1,
      str(kayak_vals))

cov = w.coverage(rows)
check("coverage counts only days that actually carry the signal",
      cov["kayak"] == 2, f"kayak coverage {cov['kayak']} of {len(rows)} days")
check("...and a signal nobody has ever recorded is zero, not absent",
      cov["obligation"] == 0, str(cov["obligation"]))


# ============================================================================
head("2. it refuses to compare on thin data")

p = w.patterns(store, today=TODAY)
kayak = next(s for s in p["signals"] if s["key"] == "kayak")
check("1-vs-1 does not produce a delta", kayak["delta"] is None,
      f"delta={kayak['delta']} on n_yes={kayak['n_yes']} n_no={kayak['n_no']}")
check("...and is not ranked", not any(r["key"] == "kayak" for r in p["ranked"]))
check("...but the counts are still shown, so the refusal is checkable",
      kayak["n_yes"] == 1 and kayak["n_no"] == 1, str(kayak))

# Now give it enough on both sides.
for i, e in enumerate([5, 4, 5, 4], start=10):
    record(i, energy=e, kayak=True)
for i, e in enumerate([2, 1, 2, 3], start=20):
    record(i, energy=e, kayak=False)

p = w.patterns(store, today=TODAY)
kayak = next(s for s in p["signals"] if s["key"] == "kayak")
check(f"with {w.MIN_PER_SIDE}+ on each side a delta appears",
      kayak["delta"] is not None, str(kayak))
check("...and it is the real difference of the two means",
      kayak["energy_yes"] == 4.6 and kayak["energy_no"] == 1.8
      and kayak["delta"] == 2.8, str(kayak))
check("...and it is ranked", any(r["key"] == "kayak" for r in p["ranked"]))
check("the threshold is reported, not hidden", p["min_per_side"] == w.MIN_PER_SIDE)

# A day with signals but no energy cannot contribute to an energy comparison.
record(30, kayak=True)
p2 = w.patterns(store, today=TODAY)
k2 = next(s for s in p2["signals"] if s["key"] == "kayak")
check("a day with no energy score is excluded from the comparison",
      k2["n_yes"] == kayak["n_yes"], f"{k2['n_yes']} vs {kayak['n_yes']}")
check("...though it still counts as a check-in", p2["days"] > p["days"])


# ============================================================================
head("3. the note still stands alone")

record(40, note="just a sentence, no signals at all")
rows = w.series(store, today=TODAY)
lonely = next(r for r in rows if r["date"] == day(40))
check("a prose-only check-in is a valid row", lonely["note"].startswith("just a"))
check("...and carries no signal keys",
      not any(k in lonely for k in w.BOOLS), str(lonely))

# Partial answers are the normal case, not an error.
sig = w.normalize("kayak,outside", "doom", {"sleep": 3, "energy": None})
check("a partial answer records only what was given",
      sig == {"kayak": True, "outside": True, "doom": False, "sleep": 3}, str(sig))
check("an unanswered scale is simply absent", "energy" not in sig)


# ============================================================================
head("3b. the input parser refuses rather than guessing")

def refuses(name: str, fn, expect: str = "") -> None:
    try:
        fn()
        check(name, False, "no error raised")
    except w.Refused as e:
        check(name, expect.lower() in str(e).lower() if expect else True, str(e))


refuses("a typo'd signal is refused, not dropped",
        lambda: w.normalize("kayk", None), "unknown signal")
refuses("a scale passed as a did-flag is refused",
        lambda: w.normalize("sleep", None), "1-5 scale")
refuses("the same signal as both did and not-did is refused",
        lambda: w.normalize("kayak", "kayak"), "both")
refuses("a scale out of range is refused",
        lambda: w.normalize(None, None, {"sleep": 9}), "1-5")
check("spaces work as separators too, since he will type them",
      w.normalize("kayak outside", None) == {"kayak": True, "outside": True})


# ============================================================================
head("4. it never invents advice")

empty = Store(TMP / "empty-state")
pe = w.patterns(empty, today=TODAY)
check("no data means no suggestion", pe["suggestion"] is None)
check("...and no ranking", pe["ranked"] == [])
check("...and it does not crash", pe["days"] == 0)

# kayak is NOT suggested here, and that is correct: section 1 recorded one yesterday.
# Recency is part of the rule, so a thing he just did is not handed back to him.
p = w.patterns(store, today=TODAY)
check("something done yesterday is not suggested, however strong the effect",
      (p["suggestion"] or {}).get("key") != "kayak", str(p["suggestion"]))

# Now a signal with a STRONGER effect and a real gap: last done 70 days ago.
for i, e in enumerate([5, 5, 5, 5], start=70):
    record(i, energy=e, made=True)
for i, e in enumerate([1, 1, 1, 1], start=80):
    record(i, energy=e, made=False)

p = w.patterns(store, today=TODAY)
s = p["suggestion"]
check("with evidence and a gap, it names the specific thing",
      s is not None and s["key"] == "made", str(s))
check("...and shows the evidence rather than asserting",
      s is not None and s["delta"] == 4.0 and s["n_yes"] >= w.MIN_PER_SIDE, str(s))
check("...and it outranked the weaker effect",
      p["ranked"][0]["key"] == "made", str([r["key"] for r in p["ranked"][:3]]))

# Done today: nothing to suggest, because the answer would be "keep doing it".
record(0, energy=5, made=True)
p3 = w.patterns(store, today=TODAY)
check("if he already did it today, it is not suggested back at him",
      (p3["suggestion"] or {}).get("key") != "made", str(p3["suggestion"]))

# A drain must never be suggested, however strong the effect.
for i, e in enumerate([5, 5, 4, 5], start=50):
    record(i, energy=e, doom=True)
for i, e in enumerate([1, 2, 1, 2], start=60):
    record(i, energy=e, doom=False)
p4 = w.patterns(store, today=TODAY)
doom = next(x for x in p4["signals"] if x["key"] == "doom")
check("a drain with a positive delta is still measured honestly",
      doom["delta"] is not None and doom["delta"] > 0, str(doom))
check("...but is NEVER suggested as an action",
      (p4["suggestion"] or {}).get("key") != "doom", str(p4["suggestion"]))


# ============================================================================
head("5. recency answers what an average cannot")

rec = w.recent_counts(w.series(store, today=TODAY), TODAY)
check("days_since is 0 for something done today",
      rec["made"]["days_since"] == 0, str(rec["made"]))
check("...and counts back from the last TRUE day, not the last check-in",
      rec["kayak"]["days_since"] == 1, str(rec["kayak"]))
check("a signal never recorded true reports never, not zero",
      rec["obligation"]["last"] is None
      and rec["obligation"]["days_since"] is None, str(rec["obligation"]))
check("the 7-day window counts only the window",
      rec["kayak"]["in_window"] <= 7, str(rec["kayak"]["in_window"]))


# ============================================================================
head("6. the signals are his hypotheses, not a generic mood inventory")

keys = set(w.BY_KEY)
for want in ("kayak", "outside", "made", "valued", "known", "quiet", "doom"):
    check(f"tracks '{want}' from the profile", want in keys)
check("kayak is separate from moved, because the open question is about kayaking",
      "kayak" in keys and "moved" in keys)
check("every signal declares a direction, so a drain is never read as a win",
      all(isinstance(s.good, bool) for s in w.SIGNALS))
check("energy is the outcome everything else is measured against",
      w.OUTCOME == "energy" and w.BY_KEY["energy"].kind == "scale")


print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
print("=" * 62)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
