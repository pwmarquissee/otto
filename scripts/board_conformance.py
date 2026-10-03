"""Conformance harness for the board's Done window.

Same shape and same reasoning as `decisions_conformance.py`: runs against a throwaway
OTTO_HOME, never touches live state, never needs the daemon.

    python scripts\\board_conformance.py

WHY THIS FILE EXISTS. On the day the Done window shipped, live state had 65 finished
cards and exactly ONE of them was older than seven days. So running `otto board`
against real state proves almost nothing about the rule: it would look identical if
the cutoff were 7 days, 70, or silently broken. Synthetic aged state is the only
thing that can distinguish those.

THE THREE CLAIMS.

  1. A finished card past the window leaves the board, and the count it left behind
     is reported rather than dropped. A column that quietly shows a subset is a
     column that lies, and every surface prints `hidden` on the strength of it.
  2. Hiding is not deleting. The Task is still in the store afterwards, because
     `otto task ls`, the weekly rollups and the decision log all read it there.
  3. Nothing but Done ages. A `blocked` card untouched for a year is still work,
     and a board that hid it would be hiding the worst card on it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-board-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import Task, iso, utcnow  # noqa: E402

config.mark_daemon()
config.ensure_dirs()

from otto import board  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def ago(days: float) -> str:
    return iso(utcnow() - timedelta(days=days))


def task(**kw) -> Task:
    kw.setdefault("domain", "work")
    return Task(**kw)


def column(data: dict, key: str) -> dict:
    return next(c for c in data["columns"] if c["key"] == key)


W = config.BOARD_DONE_DAYS
print(f"\n  BOARD CONFORMANCE   window {W}d   home {TMP}\n")

store.save_tasks([
    task(id="fresh1", title="done today", status="done", updated=ago(0)),
    task(id="fresh2", title="done inside the window", status="done", updated=ago(W - 1)),
    task(id="old1", title="done a fortnight ago", status="done", updated=ago(W + 7)),
    task(id="old2", title="done last month", status="done", updated=ago(60)),
    task(id="oldpers", title="done last month, personal", status="done",
         updated=ago(60), domain="personal"),
    task(id="undated", title="done, unparseable timestamp", status="done",
         updated="not a timestamp"),
    task(id="stuck", title="blocked for a year", status="blocked", updated=ago(365)),
    task(id="open1", title="backlog, ancient", status="backlog", updated=ago(400)),
])

data = board.build(store)
done = column(data, "done")
titles = {c["title"] for c in done["cards"]}

# ---- 1. the window, and the count it leaves behind --------------------------
check("a card finished today is on the board", "done today" in titles)
check(f"a card finished {W - 1}d ago is on the board",
      "done inside the window" in titles)
check("a card finished past the window is not", "done a fortnight ago" not in titles,
      sorted(titles))
check("neither is one finished 60d ago", "done last month" not in titles)
check("the hidden count is reported", done["hidden"] == 3, str(done["hidden"]))
check("so is the window it used", done["hidden_after_days"] == W)
check("count matches the cards shown", done["count"] == len(done["cards"]))
check("no other column claims to hide anything",
      all(column(data, k)["hidden"] == 0 for k in
          ("backlog", "queued", "running", "needs-you", "blocked")))

# ---- 2. hidden is not deleted -----------------------------------------------
still = {t.id for t in store.tasks()}
check("the aged-out tasks are still in the store",
      {"old1", "old2", "oldpers"} <= still, sorted(still))
check("build() did not write to the store", len(store.tasks()) == 8)

# ---- 3. only Done ages ------------------------------------------------------
blocked = {c["title"] for c in column(data, "blocked")["cards"]}
backlog = {c["title"] for c in column(data, "backlog")["cards"]}
check("a blocked card untouched for a year stays", "blocked for a year" in blocked)
check("a 400-day-old backlog card stays", "backlog, ancient" in backlog)

# ---- undated ----------------------------------------------------------------
check("a finished card Otto cannot date is never hidden",
      "done, unparseable timestamp" in titles)

# ---- the domain filter counts its own hidden cards --------------------------
work = column(board.build(store, "work"), "done")
pers = column(board.build(store, "personal"), "done")
check("hidden is per-domain, work", work["hidden"] == 2, str(work["hidden"]))
check("hidden is per-domain, personal", pers["hidden"] == 1, str(pers["hidden"]))

# ---- the escape hatch -------------------------------------------------------
config.BOARD_DONE_DAYS = 0
off = column(board.build(store), "done")
config.BOARD_DONE_DAYS = W
check("window 0 keeps every finished card", off["hidden"] == 0 and len(off["cards"]) == 6,
      f"hidden={off['hidden']} shown={len(off['cards'])}")

# ---- the helper's own contract ----------------------------------------------
aged = board.aged_done(store.tasks(), datetime.now(timezone.utc))
check("aged_done returns Tasks, not cards", all(isinstance(t, Task) for t in aged))
check("aged_done is Done-only", all(t.status == "done" for t in aged))

print()
print(f"  {len(PASS)} ok, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
