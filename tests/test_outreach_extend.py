"""outreach.extend: buying time on a held message without deciding.

Records are constructed directly and put in the store. compose() is deliberately
not used: its roster gate needs a dossier, and these tests are about the hold
arithmetic, not the gates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from otto import outreach
from otto.models import Outreach, iso, utcnow


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _held(store, **kw) -> Outreach:
    now = utcnow()
    kw.setdefault("target", "someone@example.com")
    kw.setdefault("to", "Someone")
    kw.setdefault("body", "a message")
    kw.setdefault("why", "a reason")
    kw.setdefault("at", iso(now))
    kw.setdefault("send_after", iso(now + timedelta(minutes=10)))
    kw.setdefault("hold_minutes", 10)
    o = Outreach(**kw)
    store.put_outreach(o)
    return o


def test_extend_pushes_send_after_forward(store):
    o = _held(store)
    before = _parse(o.send_after)
    after = outreach.extend(store, o.id, 10)
    assert after is not None
    assert _parse(after.send_after) - before == timedelta(minutes=10)
    assert after.hold_minutes == 20
    assert store.get_outreach(o.id).send_after == after.send_after
    # The message itself is untouched: what the owner approved by silence is what goes out.
    assert after.body == o.body and after.why == o.why


def test_extend_from_an_already_expired_hold_counts_from_now(store):
    """A hold that lapsed while the daemon was down is extended from now, not from
    a time in the past, or the extension would already be over."""
    o = _held(store, send_after=iso(utcnow() - timedelta(minutes=30)))
    after = outreach.extend(store, o.id, 5)
    remaining = _parse(after.send_after) - utcnow()
    assert timedelta(minutes=4) < remaining <= timedelta(minutes=5, seconds=5)


def test_extend_refuses_past_the_24h_cap(store):
    o = _held(store, at=iso(utcnow() - timedelta(hours=23, minutes=50)),
              send_after=iso(utcnow() + timedelta(minutes=5)))
    with pytest.raises(ValueError, match="24h"):
        outreach.extend(store, o.id, 10)
    # Nothing was written.
    assert store.get_outreach(o.id).send_after == o.send_after


def test_extend_rejects_non_positive_minutes(store):
    o = _held(store)
    with pytest.raises(ValueError):
        outreach.extend(store, o.id, 0)


def test_extend_returns_none_for_non_held_and_unknown(store):
    sent = _held(store, state="sent")
    assert outreach.extend(store, sent.id, 10) is None
    assert outreach.extend(store, "nope", 10) is None


def test_extend_accepts_a_short_id_prefix(store):
    o = _held(store)
    assert outreach.extend(store, o.id[:6], 10) is not None
