"""Conformance harness for the feed ingester.

Asserts the properties the design has to hold, not the ones that are easy to
check. Runs against a throwaway OTTO_HOME so it can never touch live state, and
never needs the daemon.

    python scripts\feeds_conformance.py

The trust-ceiling cases are the point of this file. "A data feed cannot file a
task" and "no feed can raise crit" are the claims that make a producer/consumer
inbox safe, and a claim nothing re-checks is just a comment.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-feeds-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
os.environ["OTTO_FEED_DIR"] = str(TMP / "feed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import iso, utcnow  # noqa: E402

config.mark_daemon()
config.ensure_dirs()

from otto import feeds, findings  # noqa: E402
from otto.store import Store  # noqa: E402

# Declared for the test only. This is exactly the one-line declaration a real
# source gets, which is the point: nothing else had to change to add a source.
config.FEED_SOURCES = (
    config.FeedSource("panel", config.WORK, config.TRUST_DATA, max_age_hours=26,
                      title="panel feed", why="test"),
    config.FeedSource("adv", config.WORK, config.TRUST_ADVISORY, max_age_hours=26,
                      title="advisory feed", why="test"),
    config.FeedSource("unbounded", config.WORK, config.TRUST_DATA,
                      max_age_hours=None, title="no max age", why="test"),
)

store = Store()
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def drop(source: str, obj: dict | str) -> None:
    d = config.FEED_DIR / source
    d.mkdir(parents=True, exist_ok=True)
    body = obj if isinstance(obj, str) else json.dumps(obj)
    (d / config.FEED_FILE).write_text(body, encoding="utf-8")


def now_iso(delta_h: float = 0) -> str:
    return iso(utcnow() + timedelta(hours=delta_h))


feeds.scaffold()

# ---------------------------------------------------------------------------
print("\n-- the happy path -----------------------------------------------------")
drop("panel", {"produced_at": now_iso(-1), "producer": "test.ps1",
               "summary": "3 things", "items": [{"when": "10:00", "title": "a"}]})
notes = feeds.ingest(store)
snaps = store.snapshots()
check("a declared data drop becomes a snapshot", "work/feed:panel" in snaps,
      f"keys={list(snaps)}")
snap = snaps.get("work/feed:panel")
check("snapshot carries the producer's items", bool(snap and len(snap.items) == 1))
check("fetched_at is produced_at, NOT ingest time",
      bool(snap and snap.fetched_at == now_iso(-1)),
      f"got {snap.fetched_at if snap else None} want {now_iso(-1)}")
check("ingest is logged", any("feed panel" in n for n in notes), str(notes))

n2 = feeds.ingest(store)
check("an unchanged drop is not re-ingested", n2 == [], str(n2))

# ---------------------------------------------------------------------------
print("\n-- trust ceiling: the load-bearing half -------------------------------")
before = len(store.tasks())
drop("panel", {"produced_at": now_iso(), "summary": "sneaky",
               "tasks": [{"title": "do a thing", "detail": "x"}]})
notes = feeds.ingest(store)
check("a data-trust feed CANNOT file a board card", len(store.tasks()) == before,
      f"{before} -> {len(store.tasks())}")
check("...and the refusal is loud, not silent",
      any("IGNORED" in n for n in notes), str(notes))

drop("adv", {"produced_at": now_iso(), "summary": "real finding",
             "tasks": [{"title": "check the thing", "detail": "evidence here",
                        "priority": "high"}]})
feeds.ingest(store)
filed = [t for t in store.tasks() if t.origin == "feed:adv"]
check("an advisory feed CAN propose a card", len(filed) == 1, f"{len(filed)} filed")
check("a proposed card is never auto-dispatchable",
      bool(filed and filed[0].auto is False))
check("a proposed card lands in backlog",
      bool(filed and filed[0].status == "backlog"))
check("a proposed card is fingerprinted for dedup",
      bool(filed and filed[0].fingerprint))

# Same finding again, new drop (different summary so the digest changes).
drop("adv", {"produced_at": now_iso(0.01), "summary": "real finding again",
             "tasks": [{"title": "check the thing", "detail": "evidence here",
                        "priority": "high"}]})
feeds.ingest(store)
filed = [t for t in store.tasks() if t.origin == "feed:adv"]
check("a repeat bumps seen_count instead of duplicating",
      len(filed) == 1 and filed[0].seen_count == 2,
      f"{len(filed)} card(s), seen={filed[0].seen_count if filed else '-'}")

over = [{"title": f"finding {i}", "detail": "d"} for i in range(config.FINDINGS_MAX_PER_RUN + 3)]
drop("adv", {"produced_at": now_iso(0.02), "summary": "flood", "tasks": over})
notes = feeds.ingest(store)
check("overflow is capped at FINDINGS_MAX_PER_RUN",
      len([t for t in store.tasks() if t.origin == "feed:adv"])
      <= config.FINDINGS_MAX_PER_RUN + 1)
check("...and the drop is LOGGED, not silent",
      any("DROPPED" in n for n in notes), str(notes))

# ---------------------------------------------------------------------------
print("\n-- notice clamping ----------------------------------------------------")
drop("adv", {"produced_at": now_iso(0.03), "summary": "s",
             "notice": {"title": "wake up", "level": "crit"}})
notes = feeds.ingest(store)
lv = [n.level for n in store.notices() if n.source == "feed:adv"]
check("NO feed can raise a crit notice", "crit" not in lv, f"levels={lv}")
check("an advisory crit is clamped to warn", "warn" in lv, f"levels={lv}")
check("...and the clamp is reported", any("CLAMPED" in n for n in notes), str(notes))

drop("panel", {"produced_at": now_iso(0.04), "summary": "s",
               "notice": {"title": "psst", "level": "warn"}})
feeds.ingest(store)
lv = [n.level for n in store.notices() if n.source == "feed:panel"]
check("a data-trust warn is clamped to info (never toasts)",
      lv == ["info"], f"levels={lv}")

# ---------------------------------------------------------------------------
print("\n-- untrusted input ----------------------------------------------------")
drop("panel", "{not json at all")
notes = feeds.ingest(store)
check("malformed JSON is rejected", any("REJECTED" in n for n in notes), str(notes))
check("a rejected drop does not overwrite the last good snapshot",
      store.snapshots()["work/feed:panel"].summary is not None)
n2 = feeds.ingest(store)
check("a bad drop is complained about ONCE, not every tick", n2 == [], str(n2))

drop("panel", {"produced_at": now_iso(), "itmes": []})
notes = feeds.ingest(store)
check("an unknown field is a hard error (no silent ignore)",
      any("REJECTED" in n and "schema" in n for n in notes), str(notes))

drop("panel", {"produced_at": now_iso(+48), "summary": "from the future"})
notes = feeds.ingest(store)
check("a future produced_at is rejected (no permanent freshness)",
      any("future" in n for n in notes), str(notes))

drop("panel", {"summary": "no timestamp"})
notes = feeds.ingest(store)
check("produced_at is required", any("REJECTED" in n for n in notes), str(notes))

drop("panel", {"produced_at": now_iso(), "summary": "big",
               "items": [{"title": "x" * 400, "n": i} for i in range(50)]})
feeds.ingest(store)
snap = store.snapshots()["work/feed:panel"]
check("items are capped", len(snap.items) == config.FEED_MAX_ITEMS,
      f"{len(snap.items)} items")
check("item strings are bounded", len(snap.items[0]["title"]) <= 300)

big = config.FEED_DIR / "panel" / config.FEED_FILE
big.write_text('{"produced_at":"' + now_iso() + '","summary":"'
               + "x" * (config.FEED_MAX_BYTES + 10) + '"}', encoding="utf-8")
notes = feeds.ingest(store)
check("an oversized file is refused unread",
      any("exceeds" in n for n in notes), str(notes))

# ---------------------------------------------------------------------------
print("\n-- declared vs observed ----------------------------------------------")
(config.FEED_DIR / "rogue").mkdir(parents=True, exist_ok=True)
drop("rogue", {"produced_at": now_iso(), "summary": "I was never declared",
               "tasks": [{"title": "trust me"}]})
before = len(store.tasks())
feeds.ingest(store)
check("an UNDECLARED directory is never ingested",
      "work/feed:rogue" not in store.snapshots() and len(store.tasks()) == before)
check("...and it is reported as drift", "rogue" in feeds.undeclared(),
      str(feeds.undeclared()))
check("undeclared shows up in gaps",
      any(g["kind"] == "feed-undeclared" for g in feeds.gaps(store)))

for bad in ("../evil", "a/b", ".hidden", "UPPER", "x" * 41):
    try:
        feeds.source_dir(bad)
        check(f"name {bad!r} is rejected", False, "accepted")
    except ValueError:
        check(f"name {bad!r} is rejected", True)

# ---------------------------------------------------------------------------
print("\n-- staleness ----------------------------------------------------------")
drop("panel", {"produced_at": now_iso(-100), "summary": "ancient"})
feeds.ingest(store)
st = {s.source.name: s for s in feeds.status(store)}
check("a quiet producer reads as stale", st["panel"].state == "stale",
      f"state={st['panel'].state}")
check("...and becomes a GAP, not silence",
      any(g["kind"] == "feed-stale" for g in feeds.gaps(store)))
check("a declared source that never produced reads as no-drop",
      st["unbounded"].state == "no-drop", f"state={st['unbounded'].state}")
check("...which is also a gap",
      any(g["kind"] == "feed-silent" for g in feeds.gaps(store)))

drop("unbounded", {"produced_at": now_iso(-500), "summary": "no max age"})
feeds.ingest(store)
check("a source with no declared max age is an unknown-unknown gap",
      any(g["kind"] == "feed-unbounded" for g in feeds.gaps(store)),
      str([g["kind"] for g in feeds.gaps(store)]))

# ---------------------------------------------------------------------------
print("\n-- single-writer rule ------------------------------------------------")
check("feeds.py never writes state outside Store",
      "_atomic_write" not in Path(feeds.__file__).read_text(encoding="utf-8"))
src = Path(feeds.__file__).read_text(encoding="utf-8")
check("feeds.py reuses findings.file_tasks rather than its own filer",
      "findings.file_tasks" in src and "def _clean" not in src)
check("the ingester is the only consumer (no push endpoint in feeds.py)",
      "put_snapshot" in src)

# ---------------------------------------------------------------------------
print("\n-- incremental sweep watermark ----------------------------------------")
# The claim under test: a watermark advances ONLY on demonstrated coverage, and a
# producer that half-covers its window makes the next run re-read rather than
# leaving a hole. Holding costs money; advancing wrongly loses a message.

SWEEP = config.FEED_SOURCES[0].name  # any declared source; semantics are per-feed

def sweep_state() -> dict:
    return store.feed_state(SWEEP)

# Returns the produced_at it actually WROTE. Recomputing now_iso(produced_h) in the
# assertion made this suite flaky: the two calls straddle a second boundary every so
# often, the ISO strings differ by 1s, and a correct watermark reads as a failure. A
# test that fails on a clock tick teaches people to re-run until green, which is worse
# than not having it.
def sweep_drop(produced_h: float, coverage_h: float | None) -> tuple[list[str], str]:
    produced = now_iso(produced_h)
    obj = {"produced_at": produced, "summary": f"sweep {produced_h}"}
    if coverage_h is not None:
        obj["coverage_since"] = now_iso(coverage_h)
    drop(SWEEP, obj)
    return feeds.ingest(store), produced

check("coverage_since is an accepted envelope field",
      "coverage_since" in feeds.FeedFile.model_fields)

_, _p = sweep_drop(-3, None)
check("a drop with no coverage_since leaves the watermark unset",
      sweep_state().get("coverage_watermark") is None,
      str(sweep_state().get("coverage_watermark")))

# Full coverage with no prior ask: trusted, so the watermark takes produced_at.
notes, produced = sweep_drop(-2.5, -26)
check("full coverage advances the watermark to produced_at",
      sweep_state().get("coverage_watermark") == produced,
      str(sweep_state().get("coverage_watermark")))

# Now Otto asks for a specific point, and the producer falls short of it.
store.put_feed_state(SWEEP, {"coverage_asked": now_iso(-8)})
before = sweep_state().get("coverage_watermark")
notes, _ = sweep_drop(-2, -4)
check("a short answer HOLDS the watermark",
      sweep_state().get("coverage_watermark") == before,
      f"{before} -> {sweep_state().get('coverage_watermark')}")
check("holding is logged, not silent",
      any("watermark HELD" in n for n in notes), str(notes))

# Meets the ask exactly: advance.
store.put_feed_state(SWEEP, {"coverage_asked": now_iso(-6)})
notes, produced = sweep_drop(-1.5, -6)
check("coverage that reaches the requested point advances the watermark",
      sweep_state().get("coverage_watermark") == produced,
      str(sweep_state().get("coverage_watermark")))

# A coverage claim later than produced_at is nonsense and must not be trusted.
store.put_feed_state(SWEEP, {"coverage_asked": now_iso(-6)})
before = sweep_state().get("coverage_watermark")
sweep_drop(-1, +1)[0]
check("coverage_since after produced_at is rejected, watermark holds",
      sweep_state().get("coverage_watermark") == before,
      f"{before} -> {sweep_state().get('coverage_watermark')}")

# Window clamping: the bounds are what stop a stale watermark opening an
# unbounded search, and a fresh one making the sweep blind.
now = utcnow()
far = now - timedelta(hours=500)
check("a stale watermark clamps to the MAX window, not 500h",
      feeds._clamp_window(far, now) == now - timedelta(hours=feeds.SWEEP_WINDOW_MAX_HOURS))
recent = now - timedelta(minutes=10)
check("a fresh watermark clamps to the MIN window, so a sweep is never blind",
      feeds._clamp_window(recent, now) == now - timedelta(hours=feeds.SWEEP_WINDOW_MIN_HOURS))
mid = now - timedelta(hours=12)
check("a watermark inside the bounds is used as-is",
      feeds._clamp_window(mid, now) == mid)
check("no watermark means the full MAX window",
      feeds._clamp_window(None, now) == now - timedelta(hours=feeds.SWEEP_WINDOW_MAX_HOURS))
check("MIN is below MAX, or every window would be empty",
      feeds.SWEEP_WINDOW_MIN_HOURS < feeds.SWEEP_WINDOW_MAX_HOURS)

# The hint is what the producer actually reads, and it must record the ask so the
# answer can be checked against it.
store.put_feed_state(SWEEP, {"coverage_watermark": iso(now - timedelta(hours=10))})
hint = feeds.coverage_hint(store, SWEEP)
check("coverage_hint returns instruction text when a watermark exists", bool(hint))
check("the hint names coverage_since as required",
      bool(hint) and "coverage_since" in hint)
check("asking records coverage_asked, so the reply can be verified",
      sweep_state().get("coverage_asked") is not None)
store.put_feed_state(SWEEP, {"coverage_watermark": None})
check("no watermark means no hint, leaving the command's own window in charge",
      feeds.coverage_hint(store, SWEEP) is None)

print("\n-- render -------------------------------------------------------------")
out = feeds.render(store)
print("\n".join("    " + ln for ln in out.splitlines()))
check("render mentions every declared source",
      all(s.name in out for s in config.FEED_SOURCES))
check("render flags undeclared directories", "rogue" in out)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
