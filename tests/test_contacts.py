"""last_contact, kept true by facts instead of written once by seeding.

The failure this guards against was observed, not imagined: `apply_signals` was
built to carry last_contact and nothing ever called it, so every date in the
people directory was whatever the one-time seeding wrote. A colleague's dossier
read "24 days without contact" while the owner was in their DMs daily (2026-08-24).

The properties that matter, each with a test:

  * an exchange is BIDIRECTIONAL. One side talking is not contact -- an
    unanswered ping must keep the relationship reading as quiet.
  * the two directions must be CLOSE TOGETHER. A reply that lands weeks after
    the question is not a conversation either.
  * FORWARD-ONLY, same as apply_signals: nothing walks a date back.
  * matching prefers slack_id, falls back to email, and a match by email
    BACKFILLS slack_id -- outreach refuses to send without one.
  * the spool ingest is mtime-gated, so a daemon tick over an unchanged spool
    does nothing.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from otto import people


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


T_MON = _ts("2026-08-24T10:00:00")   # the exchange under test
T_MON_REPLY = _ts("2026-08-24T11:30:00")
T_OLD = _ts("2026-08-01T09:00:00")   # far outside the 4-day window


def _contact(peer="U_JWHIT", email="jwhitfield@example.com",
             t_in=T_MON, t_out=T_MON_REPLY) -> dict:
    return {"peer": peer, "who": "Jane", "email": email,
            "last_inbound_ts": t_in, "last_outbound_ts": t_out}


@pytest.fixture
def roster(monkeypatch, tmp_path):
    monkeypatch.setattr(people, "PEOPLE_DIR", tmp_path)
    p = people.Person("00u1", {"login": "jwhitfield@example.com",
                               "email": "jwhitfield@example.com",
                               "firstName": "Jane", "lastName": "Whitfield"})
    (tmp_path / "jwhitfield.md").write_text(people.render(p), encoding="utf-8")
    return tmp_path


def _meta() -> dict:
    d = people.get("jwhitfield")
    assert d is not None
    return d["meta"]


def test_bidirectional_exchange_advances_and_backfills_slack_id(roster):
    notes = people.apply_contacts([_contact()])
    meta = _meta()
    assert meta["last_contact"] == "2026-08-24"
    assert meta["slack_id"] == "U_JWHIT"
    assert len(notes) == 1 and "jwhitfield" in notes[0]


def test_one_sided_traffic_is_not_contact(roster):
    people.apply_contacts([_contact(t_out=0)])
    assert "last_contact" not in _meta()
    people.apply_contacts([_contact(t_in=None, t_out=T_MON)])
    assert "last_contact" not in _meta()


def test_a_reply_weeks_late_is_not_a_conversation(roster):
    people.apply_contacts([_contact(t_in=T_OLD, t_out=T_MON)])
    assert "last_contact" not in _meta()


def test_forward_only_never_walks_a_date_back(roster):
    people.set_meta("jwhitfield", {"last_contact": "2026-08-30"})
    people.apply_contacts([_contact()])
    assert _meta()["last_contact"] == "2026-08-30"


def test_matches_on_slack_id_when_email_is_missing(roster):
    people.set_meta("jwhitfield", {"slack_id": "U_JWHIT"})
    people.apply_contacts([_contact(email="")])
    assert _meta()["last_contact"] == "2026-08-24"


def test_unknown_peer_is_quietly_skipped(roster):
    notes = people.apply_contacts(
        [_contact(peer="U_STRANGER", email="nobody@example.com")])
    assert notes == []


def test_spool_ingest_is_mtime_gated(roster, monkeypatch, tmp_path):
    from otto import config

    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    (spool_dir / "new.json").write_text(
        json.dumps({"contacts": [_contact()]}), encoding="utf-8")
    monkeypatch.setattr(config, "SLACK_SPOOL_DIR", spool_dir)
    monkeypatch.setattr(people, "_spool_seen_mtime", 0.0)

    first = people.ingest_spool_contacts()
    assert len(first) == 1
    # Same file, same mtime: the tick path must do nothing, not re-parse.
    assert people.ingest_spool_contacts() == []


def test_missing_spool_is_silent(roster, monkeypatch, tmp_path):
    from otto import config

    monkeypatch.setattr(config, "SLACK_SPOOL_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(people, "_spool_seen_mtime", 0.0)
    assert people.ingest_spool_contacts() == []


# ---------------------------------------------------------------------------
# The producer's half: direction tracking is pure bookkeeping, tested as such.
# ---------------------------------------------------------------------------

def _producer():
    path = Path(__file__).resolve().parents[1] / "producers" / "slack_dm_producer.py"
    spec = importlib.util.spec_from_file_location("slack_dm_producer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_track_directions_splits_peer_from_paul():
    mod = _producer()
    rec: dict = {}
    mod.track_directions(rec, "U_PEER", [
        {"user": "U_PEER", "ts": "100.000100"},
        {"user": "U_PAUL", "ts": "200.000200"},
        {"user": "U_PEER", "ts": "150.000150"},   # older than the first: max wins
    ])
    assert rec["last_inbound_ts"] == pytest.approx(150.00015)
    assert rec["last_outbound_ts"] == pytest.approx(200.0002)


def test_track_directions_skips_group_dms():
    mod = _producer()
    rec: dict = {}
    mod.track_directions(rec, None, [{"user": "U_A", "ts": "1.0"}])
    assert rec == {}


# ---------------------------------------------------------------------------
# The spool is a buffer, not a mailbox slot. Overwriting it dropped up to three
# of every four hourly spools before the four-hourly sweep ever read them.
# ---------------------------------------------------------------------------

def _spool(tmp_path, messages, produced_at="2026-08-24T10:00:00Z"):
    p = tmp_path / "new.json"
    p.write_text(json.dumps({"produced_at": produced_at, "messages": messages}),
                 encoding="utf-8")
    return p


def test_carry_forward_keeps_unswept_and_expires_old(tmp_path):
    mod = _producer()
    now = _ts("2026-08-24T12:00:00")
    path = _spool(tmp_path, [
        {"conversation": "C1", "ts": "1.0", "fetched_at": now - 3600},       # 1h old
        {"conversation": "C1", "ts": "2.0", "fetched_at": now - 30 * 3600},  # 30h old
    ])
    kept, expired = mod.carry_forward(path, now)
    assert [r["ts"] for r in kept] == ["1.0"]
    assert expired == 1  # counted, so the loss is a receipt and not a silence


def test_carry_forward_missing_spool_is_empty_not_an_error(tmp_path):
    mod = _producer()
    kept, expired = mod.carry_forward(tmp_path / "absent.json", 0.0)
    assert (kept, expired) == ([], 0)


def test_carry_forward_pre_upgrade_rows_inherit_produced_at(tmp_path):
    """Rows written before fetched_at existed age from the old spool's
    produced_at -- not from zero (instant expiry) or from now (immortal)."""
    mod = _producer()
    produced = "2026-08-24T10:00:00Z"
    path = _spool(tmp_path, [{"conversation": "C1", "ts": "5.0"}], produced_at=produced)
    produced_epoch = datetime.strptime(produced, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=__import__("datetime").timezone.utc).timestamp()

    kept, expired = mod.carry_forward(path, produced_epoch + 3600)
    assert len(kept) == 1 and kept[0]["fetched_at"] == pytest.approx(produced_epoch)

    kept, expired = mod.carry_forward(path, produced_epoch + 27 * 3600)
    assert kept == [] and expired == 1


def test_merge_spool_fresh_wins_and_order_holds():
    mod = _producer()
    carried = [{"conversation": "C1", "ts": "10.0", "text": "old copy",
                "fetched_at": 1.0},
               {"conversation": "C2", "ts": "30.0", "text": "kept", "fetched_at": 1.0}]
    fresh = [{"conversation": "C1", "ts": "10.0", "text": "fresh copy"},
             {"conversation": "C1", "ts": "20.0", "text": "new"}]
    out = mod.merge_spool(carried, fresh, fetched_at=99.0)
    assert [r["text"] for r in out] == ["fresh copy", "new", "kept"]
    assert all(r["fetched_at"] == 99.0 for r in out if r["conversation"] == "C1")
