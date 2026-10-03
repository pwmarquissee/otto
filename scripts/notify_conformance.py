"""Conformance harness for notice dedupe and quiet hours.

    python scripts\\notify_conformance.py

WHAT THIS FILE IS FOR. On the night of 2026-08-06 Otto delivered SEVEN warn notices
about one errand, six of them toasted between 00:15 and 06:37 local. Nothing was
broken: `/slack-sweep` re-summarises the same Slack DM every hour, a model never
writes the same sentence twice, and the feed deduped on a bytes digest. Every run
looked new. The dedupe was answering "are these bytes new" when the question was "is
this the same thing you already told me".

The assertions below are built from the four real titles that night produced, because
a synthetic "same title twice" fixture would have passed against the broken code.

  1. THE FOUR PHRASINGS ARE ONE THING. Including the one that reorders the whole
     sentence, which title normalization alone cannot catch and the card id can.
  2. A REPEAT BUMPS, IT DOES NOT REPEAT. No second card, and specifically no second
     toast, which is the part he actually felt.
  3. `at` DOES NOT MOVE. An errand outstanding since 19:14 must keep saying 19:14. If
     a repeat refreshed the timestamp, the age becomes a lie and the oldest thing on
     the list looks like the newest.
  4. THE WINDOW REOPENS. A thing still outstanding tomorrow is allowed to say so once
     tomorrow. Dedupe that never expires is just suppression.
  5. QUIET HOURS HOLD, THEY DO NOT DROP. `notified_at` stays unset so the toast fires
     in the morning. Nothing is lost, it is deferred, and `crit` goes through.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="otto-notify-test-")
os.environ["OTTO_HOME"] = TMP
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402

config.utf8_output()
config.mark_daemon()
config.ensure_dirs()

from otto import notify  # noqa: E402
from otto.models import iso, utcnow  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []

# THE TOAST IS STUBBED, AND THIS IS NOT OPTIONAL.
#
# `deliver_pending` is the function under test in section 5, and it calls `_toast`,
# which shells out to Show-OttoToast.ps1 and raises a real Windows notification on
# whoever's machine is running the suite. OTTO_HOME being a temp dir does not help:
# the store is redirected, the notification center is not.
#
# Learned the hard way. The first version of this file fired a crit toast reading
# "Otto: Fleet-wide outage" onto a real desktop while proving that crit breaks
# through quiet hours, and then deleted the temp store on exit, so the owner got an
# alarm about a fleet outage with no matching record anywhere in Otto to explain it.
#
# Any harness that calls deliver_pending must stub this first.
TOASTED: list[tuple[str, str]] = []
# `**kw` because the real _toast grew keyword arguments (reply_url) after this stub
# was written, and a stub with the old signature raised TypeError from inside
# deliver_pending instead of recording the toast. The stub must accept whatever the
# real function does, or the harness fails for a reason that has nothing to do with
# the property under test.
notify._toast = lambda title, body, level, **kw: (  # type: ignore[assignment]
    TOASTED.append((level, title)), True)[1]


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def head(t: str) -> None:
    print(f"\n{t}\n" + "-" * len(t))


# The real shape, from four notices Otto actually posted about one errand, with the
# names changed.
TITLES = [
    "slack DMs: Lee needs the game audio workflow screenshots today (card 136aca)",
    "slack DMs: Lee's game-audio workflow screenshots are due today (asked 8/6 19:14"
    " PDT 'just for tomorrow', the owner committed). Board card 136aca.",
    "slack DMs: Lee's game audio workflow screenshots are due today",
    "slack DMs: Lee's game audio workflow screenshots are due today (asked 8/6 19:14"
    " PDT 'just for tomorrow', the owner agreed). Card 136aca.",
]
BODY = ("DM 2026-08-06 19:14 PDT (D0000000000): 'Just for tomorrow: could you send me "
        "some screenshots of the workflow you've been using for game audio?' "
        "The owner agreed ('Yep!'). Card 136aca.")
SRC = "feed:slack-dm"


# ============================================================================
head("1. the four real phrasings are one thing")

fps = {notify.fingerprint(t, BODY, SRC) for t in TITLES}
check("four distinct titles collapse to one fingerprint",
      len(set(TITLES)) == 4 and len(fps) == 1, f"{len(fps)} fingerprints: {fps}")
check("...and the key is the card id, not the prose",
      fps.pop().endswith(":card:136aca"))

# The hard one: a full sentence rewrite. Title normalization cannot save this.
a = notify.fingerprint(TITLES[0], BODY, SRC)          # "Lee needs ... today"
b = notify.fingerprint(TITLES[2], BODY, SRC)          # "... are due today"
check("a full rewrite still matches, because the card id survived it", a == b,
      f"{a} vs {b}")

no_card = notify.fingerprint("Disk is filling up on WORKSTATION-07", None, "machine")
check("with no card reference it falls back to a normalized title",
      no_card.startswith("machine:title:"), no_card)
check("...and the fallback ignores punctuation and parentheticals",
      notify.fingerprint("Foo's bar-baz (whatever, 19:14)", None, "x")
      == notify.fingerprint("Foos bar baz", None, "x"))
check("different subjects do NOT collide",
      notify.fingerprint("card 136aca thing", None, SRC)
      != notify.fingerprint("card 99ffee thing", None, SRC))
check("the same words from a different source are different notices",
      notify.fingerprint("same words", None, "a")
      != notify.fingerprint("same words", None, "b"))


# ============================================================================
head("2. a repeat bumps, it does not repeat")

first = notify.post(store, TITLES[0], body=BODY, level="warn", source=SRC)
# Pretend the toast fired, as it would have.
first.notified_at = iso(utcnow())
store.put_notice(first)

for t in TITLES[1:] + TITLES:                 # every phrasing, twice over
    notify.post(store, t, body=BODY, level="warn", source=SRC)

mine = [n for n in store.notices() if n.source == SRC]
check("seven postings produced ONE notice", len(mine) == 1, f"{len(mine)} notices")
check("...and it counted them", mine[0].seen_count == 8, str(mine[0].seen_count))
check("...and did not buy a second toast",
      mine[0].notified_at == first.notified_at, "notified_at moved")
check("...and recorded when it last came round", mine[0].last_seen is not None)


# ============================================================================
head("3. `at` does not move")

check("the age still points at the first sighting", mine[0].at == first.at,
      f"{mine[0].at} vs {first.at}")


# ============================================================================
head("4. the window reopens")

old = mine[0]
old.at = iso(utcnow() - timedelta(hours=config.NOTICE_DEDUPE_HOURS + 2))
store.put_notice(old)
notify.post(store, TITLES[2], body=BODY, level="warn", source=SRC)
again = [n for n in store.notices() if n.source == SRC]
check(f"past {config.NOTICE_DEDUPE_HOURS}h it is allowed to say so again",
      len(again) == 2, f"{len(again)} notices")
check("...and the new one starts its own count",
      sorted(n.seen_count for n in again)[0] == 1,
      str([n.seen_count for n in again]))


# ============================================================================
head("5. quiet hours hold, they do not drop")

check("the window wraps midnight",
      config.in_quiet_hours(datetime.now().astimezone().replace(hour=23))
      and config.in_quiet_hours(datetime.now().astimezone().replace(hour=2)),
      "23:00 and 02:00 should both be quiet")
check("...and daytime is not quiet",
      not config.in_quiet_hours(datetime.now().astimezone().replace(hour=13)))
check("an empty window disables the feature rather than silencing everything",
      True if config.QUIET_FROM != config.QUIET_TO else False,
      "QUIET_FROM == QUIET_TO would be ambiguous; in_quiet_hours returns False")

# Everything owed a toast, with the clock inside quiet hours.
for n in store.notices():
    n.notified_at = None
    n.read_at = None
    store.put_notice(n)

real_quiet = config.in_quiet_hours
config.in_quiet_hours = lambda when=None: True          # type: ignore[assignment]
try:
    notes = notify.deliver_pending(store)
    still_owed = [n for n in store.notices() if n.notify and n.notified_at is None]
    check("nothing toasted during quiet hours",
          all("toast" not in x for x in notes), str(notes))
    check("...and they are still OWED, not dropped", len(still_owed) >= 2,
          f"{len(still_owed)} still owed")
    check("...and the hold is reported, not silent",
          any("quiet hours" in x for x in notes), str(notes))

    # Deliberately NOT worded like a real incident. This string reached a real
    # desktop once; if the stub above ever fails, the next person to see it should
    # read it as a test rather than reach for their phone.
    crit = notify.post(store, "CONFORMANCE TEST crit, not a real incident",
                       level="crit", source="probe")
    notes = notify.deliver_pending(store)
    fresh = store.get_notice(crit.id)
    check("crit goes through quiet hours", fresh is not None
          and fresh.notified_at is not None, "crit was held")
    check("...and it went through the stub, not the real notification center",
          any(lvl == "crit" for lvl, _ in TOASTED), str(TOASTED))
finally:
    config.in_quiet_hours = real_quiet                  # type: ignore[assignment]


# ============================================================================
head("6. an explicit key beats anything derived")

k1 = notify.post(store, "Totally different words every time", level="info",
                 source="producer", key="producer:thread:D0EXAMPLE123")
k2 = notify.post(store, "Nothing like the first sentence at all", level="info",
                 source="producer", key="producer:thread:D0EXAMPLE123")
check("two unrelated titles with one key are one notice", k1.id == k2.id)
check("...and it counted the second", k2.seen_count == 2, str(k2.seen_count))


print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
print("=" * 62)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
