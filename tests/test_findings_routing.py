"""Where a filed finding lands, and whether it lands at all.

The board's intake problem was not the feeds, it was Otto: in the week of
2026-09-03 the triage loop filed 41 cards about its own bugs onto the owner's board,
heartbeat filed five cards about one AWS SSO session with a different timestamp in
each title, and every spawned run could add five more. These tests hold the three
rules that stop that: findings about Otto go to DEBT.md, a timestamp is not
identity, and the board takes only so many new agent cards a day.
"""

from __future__ import annotations


import pytest

from otto import config, findings
from otto.models import Task, iso, utcnow


@pytest.fixture
def debt(tmp_path, monkeypatch):
    path = tmp_path / "DEBT.md"
    monkeypatch.setattr(config, "DEBT_FILE", path)
    return path


# ---- fingerprints -------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("Refresh AWS SSO before 2026-09-05T04:07:16Z", "Refresh AWS SSO before 2026-09-09T09:40Z"),
    ("AWS SSO auth near expiry (60min left)", "AWS SSO auth near expiry (3h left)"),
    ("Add 3 known-patterns entries from the 2026-08-24 sweep",
     "Add 3 known-patterns entries from the 2026-08-27 sweep"),
])
def test_timestamps_and_durations_are_not_identity(a, b):
    assert findings.fingerprint(a, "work") == findings.fingerprint(b, "work")


def test_a_different_device_is_a_different_finding():
    """Only time-shaped numbers are stripped. A device number is identity."""
    assert (findings.fingerprint("Archive RMM device 36", "work")
            != findings.fingerprint("Archive RMM device 37", "work"))


# ---- about: otto --------------------------------------------------------------

def test_explicit_about_otto_is_self():
    assert findings.is_self({"title": "Anything at all", "about": "otto"})


@pytest.mark.parametrize("title", [
    "otto task set --detail-file replaces detail instead of appending",
    "Stale re-verify appends a near-duplicate essay to cards daily",
    "Give triage a way to mark a card as a duplicate",   # not caught: "triage" alone
])
def test_self_heuristic_catches_ottos_own_vocabulary(title):
    expected = "triage a way" not in title
    assert findings.is_self({"title": title}) is expected


@pytest.mark.parametrize("title", [
    "Triage a designer's launcher ticket",
    "Remove WS-ART07 from the EDR console",
    "Post survey group assignment data to the feedback board",
])
def test_real_work_is_not_mistaken_for_otto_debt(title):
    assert not findings.is_self({"title": title})


def test_self_findings_go_to_debt_not_the_board(store, debt):
    notes = findings.file_tasks(store, [
        {"title": "otto task set returns HTTP 500 on short ids", "detail": "seen 3x"},
        {"title": "Install the EDR sensor on the new MacBook", "detail": "no sensor"},
    ], origin="triage")
    titles = [t.title for t in store.tasks()]
    assert titles == ["Install the EDR sensor on the new MacBook"]
    body = debt.read_text(encoding="utf-8")
    assert "## otto task set returns HTTP 500 on short ids" in body
    assert "seen 3x" in body
    assert "by triage" in body
    assert any("DEBT.md" in n for n in notes)


# ---- the daily cap ------------------------------------------------------------

def test_new_agent_cards_stop_at_the_daily_cap_and_spill_to_debt(store, debt, monkeypatch):
    monkeypatch.setattr(config, "FINDINGS_MAX_PER_RUN", 10)
    monkeypatch.setattr(config, "FINDINGS_MAX_PER_DAY", 2)
    raws = [{"title": f"Fleet finding {i}", "detail": "evidence"} for i in range(4)]
    findings.file_tasks(store, raws, origin="nightly-sweep")
    assert len(store.tasks()) == 2
    body = debt.read_text(encoding="utf-8")
    assert "Fleet finding 2" in body and "Fleet finding 3" in body
    assert "daily board cap of 2" in body


def test_a_repeat_still_bumps_its_card_when_the_cap_is_full(store, debt, monkeypatch):
    """The cap bounds NEW cards. A repeat is evidence and must keep counting."""
    monkeypatch.setattr(config, "FINDINGS_MAX_PER_DAY", 1)
    findings.file_tasks(store, [{"title": "The build machine's DDC drive is full"}], origin="a")
    findings.file_tasks(store, [{"title": "The build machine's DDC drive is full"}], origin="b")
    tasks = store.tasks()
    assert len(tasks) == 1 and tasks[0].seen_count == 2
    assert not debt.exists()


def test_a_faded_card_does_not_swallow_a_fresh_filing(store, debt):
    """Faded means the owner let it go. If a feed finds it again, that is new evidence
    and deserves a fresh card, not a silent bump on something nobody can see."""
    old = Task(id="f" * 32, title="Chase the vendor on the MCP token", status="faded",
               source="agent", fingerprint=findings.fingerprint("Chase the vendor on the MCP token", "work"))
    store.upsert_task(old)
    findings.file_tasks(store, [{"title": "Chase the vendor on the MCP token"}], origin="slack-dm")
    statuses = sorted(t.status for t in store.tasks())
    assert statuses == ["backlog", "faded"]


def test_filed_today_counts_only_agent_cards_from_today(store):
    today = iso(utcnow())
    store.upsert_task(Task(id="1" * 32, title="a", source="agent", created=today))
    store.upsert_task(Task(id="2" * 32, title="b", source="manual", created=today))
    store.upsert_task(Task(id="3" * 32, title="c", source="agent",
                           created="2026-01-01T00:00:00Z"))
    assert findings.filed_today(store) == 1
