"""The per-card due nudge: one card, one due date, one question.

The owner asked (2026-09-03) for a toast they can ANSWER when a card is nearing or past its
due date. The digest notice cannot carry a Reply button that knows which card you
meant, so every pressing card gets its own notice with `task_id` set. What keeps that
from becoming a toast storm is asserted here as hard as the feature is:

  1. PHASES, NOT DAYS. A card interrupts once approaching, once on the day, once
     when it slips, then once per re-nag period while it stays late. Never daily.
  2. THE DAILY CAP HOLDS. Measured on the real board the day this was written:
     fourteen cards were due within a day of each other. Only DUE_TOASTS_PER_DAY
     get their own notice; the rest are held, and the digest still lists them.
  3. MOST PRESSING FIRST. The cap must spend itself on the late cards, not the
     ones that happen to sort first.
  4. THE DIGEST NO LONGER INTERRUPTS. It is info-level now; the per-card notice
     carries the warn. Two toasts about the same lateness is the failure.
  5. THE NOTICE KNOWS ITS CARD. `task_id` is set, and `notify.reply_url` turns it
     into the deep link the toast's Reply button opens.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from otto import config, notify, nudges
from otto.models import Task

# The real date, not a fixed one: `Notice.at` is stamped from the clock, and the daily
# cap counts notices by that stamp, so a simulated "today" a year away would never
# see its own budget spent.
TODAY = datetime.now().astimezone().date()


def card(tid: str, due: date | None, **kw) -> Task:
    kw.setdefault("title", f"card {tid}")
    return Task(id=tid.ljust(32, "0"), due=due.isoformat() if due else None, **kw)


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(config, "DUE_SOON_DAYS", 1)
    monkeypatch.setattr(config, "DUE_RENAG_DAYS", 7)
    monkeypatch.setattr(config, "DUE_TOASTS_PER_DAY", 3)


def due_notices(store):
    return [n for n in store.notices() if n.source == "due"]


# ---- 1. phases --------------------------------------------------------------------

def test_phase_boundaries():
    assert nudges.due_phase(5) is None
    assert nudges.due_phase(2) is None          # DUE_SOON_DAYS is 1
    assert nudges.due_phase(1) == "soon"
    assert nudges.due_phase(0) == "today"
    assert nudges.due_phase(-1) == "late/0"
    assert nudges.due_phase(-7) == "late/0"
    assert nudges.due_phase(-8) == "late/1"
    assert nudges.due_phase(-21) == "late/2"


def test_a_card_is_asked_about_once_per_phase_not_once_per_day(store):
    store.save_tasks([card("a", TODAY + timedelta(days=1))])
    # Approaching: one notice, and the next tick the same day is a repeat.
    nudges.due_card_notices(store, TODAY)
    nudges.due_card_notices(store, TODAY)
    assert len(due_notices(store)) == 1
    # Due day: a second, distinct notice.
    nudges.due_card_notices(store, TODAY + timedelta(days=1))
    assert len(due_notices(store)) == 2
    # Late, and late again a day later: one notice for the whole first week.
    nudges.due_card_notices(store, TODAY + timedelta(days=2))
    nudges.due_card_notices(store, TODAY + timedelta(days=3))
    nudges.due_card_notices(store, TODAY + timedelta(days=8))
    assert len(due_notices(store)) == 3
    # Second week late: the re-nag.
    nudges.due_card_notices(store, TODAY + timedelta(days=9))
    assert len(due_notices(store)) == 4
    keys = sorted(n.key for n in due_notices(store))
    assert keys == sorted([
        f"due/{'a'.ljust(32, '0')}/soon", f"due/{'a'.ljust(32, '0')}/today",
        f"due/{'a'.ljust(32, '0')}/late/0", f"due/{'a'.ljust(32, '0')}/late/1",
    ])


def test_done_and_undated_and_distant_cards_are_left_alone(store):
    store.save_tasks([
        card("done", TODAY - timedelta(days=3), status="done"),
        card("nodate", None),
        card("far", TODAY + timedelta(days=9)),
        Task(id="bad".ljust(32, "0"), title="bad date", due="not a date"),
    ])
    assert nudges.due_card_notices(store, TODAY) == []
    assert due_notices(store) == []


# ---- 2 + 3. the cap, spent on the right cards -----------------------------------

def test_daily_cap_holds_and_spends_itself_on_the_latest_cards(store):
    store.save_tasks([
        card("t1", TODAY + timedelta(days=1)),                    # soon
        card("t2", TODAY),                                        # today
        card("t3", TODAY - timedelta(days=2)),                    # late 2d
        card("t4", TODAY - timedelta(days=9), priority="low"),    # late 9d
        card("t5", TODAY, priority="urgent"),                     # today, urgent
    ])
    notes = nudges.due_card_notices(store, TODAY)
    posted = due_notices(store)
    assert len(posted) == 3
    got = {n.task_id[:2] for n in posted}
    # Most overdue first regardless of priority, then the urgent one due today.
    assert got == {"t4", "t3", "t5"}
    assert any("held for tomorrow" in n for n in notes)

    # Same day, another tick: nothing more, the budget is spent.
    nudges.due_card_notices(store, TODAY)
    assert len(due_notices(store)) == 3

    # Tomorrow the budget resets and the held cards get their turn (t2 is now late,
    # t1 is due today; both are new phases for them).
    nudges.due_card_notices(store, TODAY + timedelta(days=1))
    assert len(due_notices(store)) == 6


def test_cap_counts_only_todays_notices(store):
    store.save_tasks([card("old", TODAY - timedelta(days=1))])
    nudges.due_card_notices(store, TODAY - timedelta(days=1))   # yesterday spent 1
    [n] = due_notices(store)
    # `at` is stamped with the real clock, so back-date it to the simulated yesterday.
    n.at = (TODAY - timedelta(days=1)).isoformat() + "T08:00:00Z"
    store.put_notice(n)
    store.save_tasks([card("old", TODAY - timedelta(days=1))]
                     + [card(f"n{i}", TODAY) for i in range(3)])
    nudges.due_card_notices(store, TODAY)
    # Yesterday's one does not eat into today's three.
    assert len(due_notices(store)) == 4


# ---- 4. the digest is informational now ------------------------------------------

def test_overdue_digest_is_info_even_when_very_late(store):
    store.save_tasks([card("t", TODAY - timedelta(days=30))])
    nudges.overdue_notice(store, TODAY)
    digest = [n for n in store.notices() if n.source == "overdue"]
    assert len(digest) == 1
    assert digest[0].level == "info"
    assert digest[0].notify is False
    assert digest[0].task_id is None


# ---- 5. the notice knows its card -------------------------------------------------

def test_due_notice_carries_the_card_and_a_reply_link(store):
    t = card("abc", TODAY, assessed_note="Finance has to sign the quote first.",
             detail="long evidence " * 40)
    store.save_tasks([t])
    nudges.due_card_notices(store, TODAY)
    [n] = due_notices(store)
    assert n.task_id == t.id
    assert n.level == "warn" and n.notify is True
    assert n.title.startswith("due today: card abc")
    # The one-line assessment beats the long detail as the toast body.
    assert n.body.startswith("Finance has to sign the quote first.")
    assert "Reply" in n.body
    assert n.command == f'otto task reply {t.id[:6]} "..."'
    assert notify.reply_url(n) in (f"otto://reply/{t.id}", f"{config.BASE_URL}/#reply={t.id}")


def test_reply_url_lands_in_the_desktop_shell_when_it_owns_the_scheme(store, monkeypatch):
    """The owner, 2026-09-03: the http link opened Chrome. When the shell has registered
    otto:// the link must use it; without the shell, the browser form still works."""
    n = notify.post(store, "due today: x", level="warn", source="due",
                    task_id="c" * 32, key="due/x/today")
    monkeypatch.setattr(config, "REPLY_SCHEME", "otto")
    assert notify.reply_url(n) == f"otto://reply/{'c' * 32}"
    monkeypatch.setattr(config, "REPLY_SCHEME", "http")
    assert notify.reply_url(n) == f"{config.BASE_URL}/#reply={'c' * 32}"
    monkeypatch.setattr(config, "REPLY_SCHEME", "auto")
    monkeypatch.setattr(notify, "desktop_registered", lambda: False)
    assert notify.reply_url(n).startswith("http")


def test_reply_url_is_none_for_a_notice_about_nothing_in_particular(store):
    n = notify.post(store, "3 cards are late", level="info", source="overdue")
    assert n.task_id is None
    assert notify.reply_url(n) is None


def test_toast_gets_the_reply_url(store, monkeypatch):
    """deliver_pending hands the deep link to the toast. Toasts are disabled in the
    sandbox, so the call itself is intercepted rather than rendered."""
    seen = []
    monkeypatch.setattr(notify, "_toast",
                        lambda title, body, level, reply_url=None: seen.append(reply_url) or True)
    monkeypatch.setattr(config, "WEEKEND_PAUSES_WORK", False)
    monkeypatch.setattr(config, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(config, "REPLY_SCHEME", "otto")
    store.save_tasks([card("abc", TODAY)])
    nudges.due_card_notices(store, TODAY)
    notify.deliver_pending(store)
    assert seen == [f"otto://reply/{'abc'.ljust(32, '0')}"]


def test_tick_includes_the_due_trigger(store):
    store.save_tasks([card("abc", TODAY)])
    notes = nudges.tick(store, TODAY)
    assert any(n.startswith("due notice: abc") for n in notes)
