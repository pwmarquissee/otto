"""known.py: a broken thing the owner has decided to tolerate, and the one notice that
fires when it stops being broken.

The load-bearing assertions are the two negatives: a schedule whose last ok stamp
PREDATES the mark is not evidence of healing, and a second sweep after the stale
notice posts nothing. Get either wrong and the feature becomes a nag, which is the
failure mode retire.py's docstring warns every new detector about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from otto import known
from otto.models import Integration, Schedule, iso, utcnow


def _sched(store, name="heartbeat", **kw):
    s = Schedule(name=name, command=f"/{name}", **kw)
    store.upsert_schedule(s)
    return s


# ---- mark / clear ------------------------------------------------------------

def test_mark_rejects_unknown_kind(store):
    _sched(store)
    with pytest.raises(ValueError, match="kind must be one of"):
        known.mark(store, "run", "heartbeat", "whatever")


def test_mark_rejects_missing_target(store):
    with pytest.raises(ValueError, match="no schedule named"):
        known.mark(store, "schedule", "ghost", "a reason")
    with pytest.raises(ValueError, match="no integration named"):
        known.mark(store, "integration", "ghost", "a reason")


def test_mark_requires_a_reason(store):
    _sched(store)
    with pytest.raises(ValueError, match="needs a reason"):
        known.mark(store, "schedule", "heartbeat", "   ")


def test_mark_and_clear_round_trip(store):
    _sched(store)
    item = known.mark(store, "schedule", "heartbeat", "waiting on the vendor-a key")
    assert item["kind"] == "schedule" and item["name"] == "heartbeat"
    assert item["stale_noticed_at"] is None
    assert known.lookup(store) == {"schedule/heartbeat": item}
    assert known.clear(store, "schedule", "heartbeat") is True
    assert known.clear(store, "schedule", "heartbeat") is False
    assert known.lookup(store) == {}


def test_remark_replaces_and_rearms(store):
    _sched(store)
    first = known.mark(store, "schedule", "heartbeat", "first reason")
    first["stale_noticed_at"] = iso(utcnow())
    store.put_known(first)
    second = known.mark(store, "schedule", "heartbeat", "second reason")
    items = store.known()
    assert len(items) == 1
    assert items[0]["reason"] == "second reason"
    assert second["stale_noticed_at"] is None


# ---- healthy -------------------------------------------------------------------

def test_a_stamp_before_the_mark_is_not_healing(store):
    _sched(store)
    store.stamp("heartbeat", "ok", "r" * 32, at=iso(utcnow() - timedelta(hours=1)))
    item = known.mark(store, "schedule", "heartbeat", "reason")
    assert known.healthy(store, item) is False


def test_a_failed_run_after_the_mark_is_not_healing(store):
    _sched(store)
    item = known.mark(store, "schedule", "heartbeat", "reason")
    store.mark_attempt("heartbeat", "failed", "r" * 32)
    assert known.healthy(store, item) is False


def test_an_ok_stamp_after_the_mark_is_healing(store):
    _sched(store)
    item = known.mark(store, "schedule", "heartbeat", "reason")
    store.stamp("heartbeat", "ok", "r" * 32, at=iso(utcnow() + timedelta(seconds=1)))
    assert known.healthy(store, item) is True


def test_integration_heals_when_a_later_probe_is_ok(store):
    store.save_integrations([Integration(name="notion", ok=False, detail="401",
                                         checked_at=iso(utcnow() - timedelta(hours=1)))])
    item = known.mark(store, "integration", "notion", "token pending in the vault")
    assert known.healthy(store, item) is False
    store.save_integrations([Integration(name="notion", ok=True, detail="ok",
                                         checked_at=iso(utcnow() + timedelta(seconds=1)))])
    assert known.healthy(store, item) is True


# ---- sweep ---------------------------------------------------------------------

def _stale_notices(store):
    return [n for n in store.notices() if n.source == "known-stale"]


def test_sweep_posts_exactly_one_notice_then_goes_quiet(store):
    _sched(store, domain="work")
    known.mark(store, "schedule", "heartbeat", "waiting on the vendor-a key")

    assert known.sweep(store) == []            # still broken, nothing to say
    assert _stale_notices(store) == []

    store.stamp("heartbeat", "ok", "r" * 32, at=iso(utcnow() + timedelta(seconds=1)))
    notes = known.sweep(store)
    assert len(notes) == 1 and "STALE" in notes[0]
    posted = _stale_notices(store)
    assert len(posted) == 1
    n = posted[0]
    assert n.level == "warn"
    assert n.title == "Known failure is now stale: heartbeat"
    assert n.command == "otto known rm schedule heartbeat"
    assert "waiting on the vendor-a key" in (n.body or "")
    assert store.get_known("schedule", "heartbeat")["stale_noticed_at"]

    # The second and third sweeps are silent. The annotation is the owner's to remove.
    assert known.sweep(store) == []
    assert known.sweep(store) == []
    assert len(_stale_notices(store)) == 1
    assert _stale_notices(store)[0].seen_count == 1


def test_sweep_leaves_the_annotation_in_place(store):
    """Reports, never deletes. Whether it is really fixed is the owner's call."""
    _sched(store)
    known.mark(store, "schedule", "heartbeat", "reason")
    store.stamp("heartbeat", "ok", "r" * 32, at=iso(utcnow() + timedelta(seconds=1)))
    known.sweep(store)
    assert known.lookup(store).keys() == {"schedule/heartbeat"}


def test_render_flags_stale_items(store):
    _sched(store)
    known.mark(store, "schedule", "heartbeat", "reason")
    out = known.render(store)
    assert "heartbeat" in out and "STALE" not in out
    store.stamp("heartbeat", "ok", "r" * 32, at=iso(utcnow() + timedelta(seconds=1)))
    known.sweep(store)
    assert "STALE, clear it" in known.render(store)


def test_render_empty_says_how_to_add(store):
    assert "otto known add" in known.render(store)
