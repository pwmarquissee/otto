"""The ranked list: why a card is there, and in what order.

Both halves of "there's so much clutter I don't even know what to start on".

`why` used to be the entire `detail` field, and `otto next` printed it verbatim.
On this board detail runs to four thousand characters of evidence, so the one
surface meant to answer "what do I start on" was itself the clutter.

And the ordering answered nothing: score was priority plus a due-date bump, so
53 cards all sitting at `normal` with no due date scored identically and were
ordered by title.
"""

from __future__ import annotations

import pytest

from otto import advisor
from otto.models import Task

# Task.owner for work the human does. "otto" is the other value.
HUMAN = "owner"


def card(**kw) -> Task:
    base = {"id": "a" * 32, "title": "a card"}
    base.update(kw)
    return Task(**base)


# ---- the reason is one line ---------------------------------------------------

def test_a_long_detail_is_cut_down_to_a_line():
    body = "First sentence that matters. " + ("padding " * 500)
    why = advisor._task_why(card(detail=body))
    assert len(why) <= advisor._WHY_MAX + 3
    assert why.startswith("First sentence")


def test_the_cut_lands_on_a_word_boundary():
    why = advisor._task_why(card(detail="alpha bravo charlie " * 40))
    assert why.endswith("...")
    assert not why.replace("...", "").endswith(" ")
    assert "charli..." not in why, "must not slice a word in half"


def test_a_short_detail_is_left_alone():
    why = advisor._task_why(card(detail="Order the RAM before prices move."))
    assert why == "Order the RAM before prices move."
    assert not why.endswith("...")


def test_the_triage_note_wins_over_the_detail():
    """The note was written for this list. The detail is evidence."""
    t = card(detail="four thousand characters of Falcon output " * 50,
             assessed_note="Waiting on the finance quote before anything can move.")
    assert advisor._task_why(t) == "Waiting on the finance quote before anything can move."


def test_a_card_with_neither_still_says_something():
    assert "backlog" in advisor._task_why(card(status="backlog"))


def test_only_the_first_non_empty_line_is_used():
    t = card(detail="\n\n  Headline that matters  \n\nA second paragraph.\n")
    assert advisor._task_why(t) == "Headline that matters"


# ---- ordering ------------------------------------------------------------------

def test_ready_work_outranks_work_that_cannot_be_started():
    ready = advisor._triage_bump(card(owner=HUMAN, readiness="ready"))
    blocked = advisor._triage_bump(card(owner=HUMAN, readiness="needs-info"))
    assert ready > blocked


def test_a_pending_decision_ranks_above_a_card_waiting_on_information():
    """A decision is usually cheap next to the work it unblocks."""
    decision = advisor._triage_bump(card(owner=HUMAN, readiness="needs-decision"))
    info = advisor._triage_bump(card(owner=HUMAN, readiness="needs-info"))
    assert decision > info


def test_ottos_own_backlog_work_does_not_compete_for_the_top():
    """The list answers "what do I pick up". Otto's queue is not that."""
    mine = advisor._triage_bump(card(owner=HUMAN, readiness="ready"))
    ottos = advisor._triage_bump(card(owner="otto", readiness="ready",
                                      status="backlog"))
    assert mine > ottos


def test_an_unassessed_card_is_not_penalised():
    """It still needs to be seen, precisely so it gets assessed."""
    assert advisor._triage_bump(card()) == 0


def test_the_bump_cannot_outrank_an_explicit_priority():
    """Assessment reorders within a band. It must not let a `normal` card that
    happens to be ready jump something the owner marked urgent."""
    spread = (advisor._PRIORITY_SCORE["urgent"] - advisor._PRIORITY_SCORE["high"])
    biggest = max(abs(v) for v in advisor._READINESS_BUMP.values()) + 4
    assert biggest < spread


@pytest.mark.parametrize("readiness", ["ready", "needs-info", "needs-decision"])
def test_every_readiness_value_has_a_defined_bump(readiness):
    """A new readiness value silently scoring 0 would be invisible."""
    assert readiness in advisor._READINESS_BUMP
