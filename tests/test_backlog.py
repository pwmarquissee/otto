"""The promotion gate, and the triage assessment around it.

`queued` means Otto spawns a real Claude Code session with
--dangerously-skip-permissions. The rule about what may be promoted used to live
in /orchestrate's instructions, which is to say it was enforced by asking a model
nicely, and the board's own history records how that ends: a card of unknown
vintage sitting in `queued` was dispatched on the daemon's first tick and began
editing a live ops file nobody had approved changing.

So the gate is code now, and these are the tests that make it a gate. The
important ones are the refusals. A test that only proves a good card CAN be
promoted would pass just as happily against a function that returns None
unconditionally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from otto import backlog, config
from otto.models import Task

# Task.owner for work the human does. "otto" is the other value.
HUMAN = "owner"

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def card(tid="a", **kw) -> Task:
    """A card that passes the gate, so each test can break exactly one thing."""
    base = {
        "id": tid.ljust(32, "0"),
        "title": f"card {tid}",
        "status": "backlog",
        "detail": "real context an agent could work from",
        "owner": "otto",
        "readiness": "ready",
        "tier": backlog.TIER_AUTONOMOUS,
        "assessed": iso(NOW),
    }
    base.update(kw)
    return Task(**base)


# ---- the happy path, stated once so the refusals mean something --------------

def test_a_fully_assessed_tier_0_otto_card_may_be_promoted():
    t = card()
    assert backlog.refusal(t, [t]) is None


# ---- every refusal -----------------------------------------------------------

def test_an_unassessed_card_is_refused_and_names_what_is_missing():
    t = card(assessed=None, owner=None, readiness=None, tier=None)
    why = backlog.refusal(t, [t])
    for field in ("assessed", "owner", "readiness", "tier"):
        assert field in why


def test_a_partially_assessed_card_is_refused():
    """A tier without an owner came from the old flow, which had no notion of
    who does the work. It must be re-read, not trusted."""
    t = card(owner=None)
    assert "owner" in backlog.refusal(t, [t])


def test_the_owners_own_work_is_never_promoted():
    """The majority of this board. Dispatching an agent at "follow up with
    the vendor" is wrong, not merely risky."""
    t = card(owner=HUMAN)
    assert "owned by" in backlog.refusal(t, [t])


@pytest.mark.parametrize("tier", [backlog.TIER_APPROVAL, backlog.TIER_ASSISTIVE,
                                  "tier-0", "", "made-up"])
def test_only_tier_0_autonomous_runs_unattended(tier):
    """Including near-misses. 'tier-0' is not 'tier-0-autonomous', and a gate
    that accepts a prefix is not a gate."""
    t = card(tier=tier)
    assert backlog.refusal(t, [t]) is not None


@pytest.mark.parametrize("readiness", ["needs-info", "needs-decision"])
def test_a_card_that_is_not_ready_is_refused(readiness):
    t = card(readiness=readiness)
    assert readiness in backlog.refusal(t, [t])


def test_a_card_with_no_detail_is_refused():
    """The spawned session gets the card, so a card with only a title sends an
    agent off to guess."""
    t = card(detail=None)
    assert "no detail" in backlog.refusal(t, [t])


@pytest.mark.parametrize("status", ["queued", "running", "needs-you", "blocked", "done"])
def test_only_a_backlog_card_can_be_promoted(status):
    t = card(status=status)
    assert "backlog card" in backlog.refusal(t, [t])


# ---- the in-flight cap ---------------------------------------------------------

def test_promotion_is_refused_at_the_in_flight_cap(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 2)
    t = card("a")
    board = [t, card("b", status="queued"), card("c", status="running")]
    why = backlog.refusal(t, board)
    assert "at the limit of 2" in why


def test_the_cap_counts_queued_and_running_together(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 2)
    t = card("a")
    assert backlog.refusal(t, [t, card("b", status="queued")]) is None
    assert backlog.refusal(t, [t, card("b", status="queued"),
                               card("c", status="running")]) is not None


def test_finished_work_does_not_count_against_the_cap(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 1)
    board = [card("a")] + [card(str(i), status="done") for i in range(9)]
    assert backlog.refusal(board[0], board) is None


def test_promotable_stops_at_the_cap_rather_than_listing_a_stale_tail(monkeypatch):
    """Each card promoted fills a slot, so the list must account for its own
    effect. Returning five when only three can run is a list whose tail is
    already wrong when you act on it."""
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 3)
    board = [card(c, created=f"2026-08-0{i + 1}T00:00:00Z")
             for i, c in enumerate("abcde")]
    assert len(backlog.promotable(board)) == 3


def test_promotable_takes_the_oldest_first(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 2)
    board = [
        card("new", created="2026-08-17T00:00:00Z"),
        card("old", created="2026-08-01T00:00:00Z"),
        card("mid", created="2026-08-09T00:00:00Z"),
    ]
    assert [t.id[:3] for t in backlog.promotable(board)] == ["old", "mid"]


def test_promotable_excludes_everything_the_gate_refuses(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 10)
    board = [card("a"), card("b", owner=HUMAN), card("c", tier=backlog.TIER_APPROVAL),
             card("d", readiness="needs-info"), card("e", assessed=None)]
    assert [t.id[0] for t in backlog.promotable(board)] == ["a"]


# ---- what still needs judging ---------------------------------------------------

def test_needs_assessment_finds_unjudged_backlog_cards():
    board = [card("a", assessed=None), card("b"), card("c", owner=None)]
    assert {t.id[0] for t in backlog.needs_assessment(board)} == {"a", "c"}


def test_needs_assessment_ignores_done_but_sees_needs_you():
    """A failed run parks a card in needs-you unassessed. For three weeks the list
    only scanned backlog, so those were never judged (card 2026-08-20, "triage
    cannot see unassessed cards outside the backlog")."""
    board = [card("a", status="done", assessed=None),
             card("b", status="needs-you", assessed=None),
             card("c", status="faded", assessed=None)]
    assert [t.id[:1] for t in backlog.needs_assessment(board)] == ["b"]


def test_needs_assessment_is_oldest_first():
    board = [
        card("new", assessed=None, created="2026-08-17T00:00:00Z"),
        card("old", assessed=None, created="2026-08-01T00:00:00Z"),
    ]
    assert [t.id[:3] for t in backlog.needs_assessment(board)] == ["old", "new"]


# ---- stale candidates ------------------------------------------------------------

def test_stale_candidates_are_selected_on_age(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 10)
    board = [
        card("old", created=iso(NOW - timedelta(days=14))),
        card("new", created=iso(NOW - timedelta(days=2))),
    ]
    assert [t.id[:3] for t in backlog.stale_candidates(board, NOW)] == ["old"]


def test_stale_candidates_only_looks_at_the_backlog(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 10)
    board = [card("a", status="done", created=iso(NOW - timedelta(days=99)))]
    assert backlog.stale_candidates(board, NOW) == []


def test_an_undateable_card_is_never_called_stale(monkeypatch):
    """Otto does not get to retire work it cannot date."""
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 10)
    board = [card("a", created="not a timestamp"), card("b", created="")]
    assert backlog.stale_candidates(board, NOW) == []


def test_being_stale_does_not_make_a_card_promotable(monkeypatch):
    """Age triggers a look, never an action. A stale card is still gated."""
    monkeypatch.setattr(config, "TRIAGE_STALE_DAYS", 1)
    t = card("a", owner=HUMAN, created=iso(NOW - timedelta(days=99)))
    assert t in backlog.stale_candidates([t], NOW)
    assert backlog.refusal(t, [t]) is not None


# ---- summary ---------------------------------------------------------------------

def test_summary_counts_the_board_by_assessment(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 3)
    board = [
        card("a"),
        card("b", owner=HUMAN, readiness="needs-decision"),
        card("c", owner=HUMAN, readiness="needs-info"),
        card("d", assessed=None, owner=None, readiness=None, tier=None),
        card("e", status="running"),
    ]
    s = backlog.summary(board)
    assert s["backlog"] == 4
    assert s["assessed"] == 3
    assert s["unassessed"] == 1
    assert s["in_flight"] == 1
    assert s["mine"] == 2
    assert s["otto"] == 1
    assert s["blocked_on_a_decision"] == 1
    assert s["waiting_on_information"] == 1


def test_summary_promotable_respects_the_cap(monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_IN_FLIGHT", 1)
    board = [card("a"), card("b")]
    assert backlog.summary(board)["promotable"] == 1
