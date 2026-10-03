"""Board assembly: columns, ordering, and the Done window.

Two behaviours carry real weight here.

Done is a window, not an archive. By 2026-08-05 it held 65 cards against 38 in
Backlog, so most of the board was history. Aged cards are left OFF the board but
never deleted, and the count is carried through as `hidden` so a surface can say
what it is not showing. A column that quietly shows a subset is a column that
lies, so `hidden` is asserted here as hard as the filtering is.

Ordering is the other one. Priority first, then due date soonest, and a dated
card must outrank an undated one at the same priority: "no deadline" is genuinely
weaker information than "Thursday".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from otto import board, config
from otto.models import Task

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def task(tid: str, **kw) -> Task:
    kw.setdefault("title", tid)
    return Task(id=tid.ljust(32, "0"), **kw)


@pytest.fixture(autouse=True)
def no_derived_cards(monkeypatch):
    """Isolate stored-card behaviour.

    derived_cards() reads schedules, runs and integrations and reaches into
    persona and registry. Those deserve their own tests; mixing them in here
    would mean a board assertion could fail for a reason that has nothing to do
    with the board.
    """
    monkeypatch.setattr(board, "derived_cards", lambda _store: [])


@pytest.fixture(autouse=True)
def quiet_registry(monkeypatch):
    monkeypatch.setattr(board.registry, "summarize_by_domain", lambda _entries: {})


# ---- the Done window ----------------------------------------------------------

def test_aged_done_only_touches_finished_cards(monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    old = iso(NOW - timedelta(days=30))
    tasks = [
        task("a", status="done", updated=old),
        task("b", status="backlog", updated=old),
        task("c", status="needs-you", updated=old),
    ]
    assert [t.id[0] for t in board.aged_done(tasks, NOW)] == ["a"]


def test_aged_done_uses_the_configured_window(monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    tasks = [
        task("a", status="done", updated=iso(NOW - timedelta(days=8))),
        task("b", status="done", updated=iso(NOW - timedelta(days=6))),
    ]
    assert [t.id[0] for t in board.aged_done(tasks, NOW)] == ["a"]


def test_a_window_of_zero_disables_ageing(monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 0)
    tasks = [task("a", status="done", updated=iso(NOW - timedelta(days=999)))]
    assert board.aged_done(tasks, NOW) == []


def test_a_card_with_an_unparseable_date_is_never_hidden(monkeypatch):
    """Otto does not hide work it cannot date."""
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    tasks = [
        task("a", status="done", updated="not a timestamp"),
        task("b", status="done", updated=""),
    ]
    assert board.aged_done(tasks, NOW) == []


def test_build_hides_aged_cards_but_says_how_many(store, monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    store.save_tasks([
        task("a", status="done", updated=iso(datetime.now(timezone.utc) - timedelta(days=30))),
        task("b", status="done", updated=iso(datetime.now(timezone.utc))),
        task("c", status="backlog"),
    ])

    done = next(c for c in board.build(store)["columns"] if c["key"] == "done")
    assert done["count"] == 1, "the aged card is off the board"
    assert done["hidden"] == 1, "...and the column admits it"
    assert done["hidden_after_days"] == 7


def test_hiding_a_card_does_not_delete_it(store, monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    store.save_tasks([
        task("a", status="done", updated=iso(datetime.now(timezone.utc) - timedelta(days=30))),
    ])
    board.build(store)
    assert len(store.tasks()) == 1, "the board is a view, never a reaper"


def test_only_the_done_column_reports_hidden(store, monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    store.save_tasks([task("a", status="backlog")])
    for col in board.build(store)["columns"]:
        if col["key"] != "done":
            assert col["hidden"] == 0


# ---- columns -------------------------------------------------------------------

def test_every_column_is_present_even_when_empty(store):
    keys = [c["key"] for c in board.build(store)["columns"]]
    assert keys == ["backlog", "queued", "running", "needs-you", "blocked", "done"]


def test_cards_land_in_the_column_matching_their_status(store):
    store.save_tasks([
        task("a", status="backlog"),
        task("b", status="queued"),
        task("c", status="needs-you"),
        task("d", status="needs-you"),
    ])
    counts = {c["key"]: c["count"] for c in board.build(store)["columns"]}
    assert counts == {"backlog": 1, "queued": 1, "running": 0,
                      "needs-you": 2, "blocked": 0, "done": 0}


def test_domain_filter_applies_to_cards_and_to_the_hidden_count(store, monkeypatch):
    monkeypatch.setattr(config, "BOARD_DONE_DAYS", 7)
    stale = iso(datetime.now(timezone.utc) - timedelta(days=30))
    store.save_tasks([
        task("a", status="backlog", domain="work"),
        task("b", status="backlog", domain="personal"),
        task("c", status="done", domain="personal", updated=stale),
    ])

    work = board.build(store, domain="work")
    assert work["total"] == 1
    done = next(c for c in work["columns"] if c["key"] == "done")
    assert done["hidden"] == 0, "a personal card must not inflate the work count"


def test_totals_split_stored_from_derived(store):
    store.save_tasks([task("a"), task("b")])
    built = board.build(store)
    assert built["total"] == 2
    assert built["stored"] == 2
    assert built["derived"] == 0


# ---- ordering --------------------------------------------------------------------

def test_cards_sort_by_priority_first(store):
    store.save_tasks([
        task("low", priority="low"),
        task("urgent", priority="urgent"),
        task("normal", priority="normal"),
        task("high", priority="high"),
    ])
    col = next(c for c in board.build(store)["columns"] if c["key"] == "backlog")
    assert [c["title"] for c in col["cards"]] == ["urgent", "high", "normal", "low"]


def test_a_due_date_breaks_ties_within_a_priority(store):
    store.save_tasks([
        task("march", priority="high", due="2026-03-01"),
        task("tomorrow", priority="high", due="2026-01-02"),
    ])
    col = next(c for c in board.build(store)["columns"] if c["key"] == "backlog")
    assert [c["title"] for c in col["cards"]] == ["tomorrow", "march"]


def test_a_dated_card_outranks_an_undated_one(store):
    """'No deadline' is weaker information than 'Thursday'."""
    store.save_tasks([
        task("undated", priority="high"),
        task("dated", priority="high", due="2026-12-31"),
    ])
    col = next(c for c in board.build(store)["columns"] if c["key"] == "backlog")
    assert [c["title"] for c in col["cards"]] == ["dated", "undated"]


def test_title_is_the_last_tiebreak_and_is_case_insensitive(store):
    store.save_tasks([
        task("beta", title="beta", priority="normal"),
        task("Alpha", title="Alpha", priority="normal"),
    ])
    col = next(c for c in board.build(store)["columns"] if c["key"] == "backlog")
    assert [c["title"] for c in col["cards"]] == ["Alpha", "beta"]
