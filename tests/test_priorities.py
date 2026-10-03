"""Priorities: what matters now, and whether anyone has checked lately.

The module exists because a dated fact ("the June 2026 demo is the top
priority") sat in CLAUDE.md into August, steering every session at a milestone in
the past. Moving the sentence does not stop it rotting, it only moves where it
rots. What stops it is Otto NOTICING, so these tests are mostly about the
noticing: each way a priorities file can be untrustworthy has to produce a
distinct state, and none of them may report `ok`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from otto import config, priorities


@pytest.fixture
def prio(monkeypatch, tmp_path):
    """Point the module at a throwaway priorities file.

    PRIORITIES_PATH is resolved at import, so the constant is patched rather than
    the environment.
    """
    path = tmp_path / "priorities.md"
    monkeypatch.setattr(config, "PRIORITIES_PATH", path)
    monkeypatch.setattr(config, "PRIORITIES_MAX_AGE_DAYS", 45)
    return path


def _dated(days_ago: int, body: str = "Ship the thing.") -> str:
    day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return f"reviewed: {day}\n\n# What matters right now\n\n{body}\n"


# ---- the states --------------------------------------------------------------

def test_absent_file_reports_missing(prio):
    assert priorities.status()["state"] == "missing"
    assert priorities.text() == ""
    assert priorities.age_days() is None


def test_fresh_and_filled_in_reports_ok(prio):
    prio.write_text(_dated(3), encoding="utf-8")
    st = priorities.status()
    assert st["state"] == "ok"
    assert st["age_days"] == 3


def test_past_the_limit_reports_stale(prio):
    prio.write_text(_dated(46), encoding="utf-8")
    assert priorities.status()["state"] == "stale"


def test_exactly_at_the_limit_is_not_yet_stale(prio):
    """`age > max` is the rule, so the boundary day still counts as reviewed."""
    prio.write_text(_dated(45), encoding="utf-8")
    assert priorities.status()["state"] == "ok"


def test_no_reviewed_line_reports_undated(prio):
    prio.write_text("# What matters right now\n\nShip the thing.\n", encoding="utf-8")
    assert priorities.status()["state"] == "undated"
    assert priorities.reviewed_on() is None


def test_unparseable_date_reports_undated_rather_than_crashing(prio):
    prio.write_text("reviewed: 2026-13-99\n\nShip the thing.\n", encoding="utf-8")
    assert priorities.reviewed_on() is None
    assert priorities.status()["state"] == "undated"


def test_the_unedited_template_never_reports_ok(prio):
    """The trap this module was built to avoid, reproduced by its own tool.

    `--init` stamps today's date, so an untouched template is fresh by every
    other measure while saying nothing at all. The template check must beat the
    date check.
    """
    prio.write_text(
        priorities.TEMPLATE.format(today=datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        encoding="utf-8",
    )
    st = priorities.status()
    assert st["state"] == "template"
    assert st["age_days"] == 0, "it really is freshly dated, which is the point"


# ---- age -----------------------------------------------------------------------

def test_age_is_never_negative_for_a_future_date(prio):
    prio.write_text(_dated(-30), encoding="utf-8")
    assert priorities.age_days() == 0


def test_age_is_measured_against_an_injected_now(prio):
    prio.write_text("reviewed: 2026-01-01\n\nShip it.\n", encoding="utf-8")
    now = datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert priorities.age_days(now) == 60


# ---- gaps ----------------------------------------------------------------------

def test_a_healthy_file_produces_no_gap(prio):
    prio.write_text(_dated(1), encoding="utf-8")
    assert priorities.gaps() == []


@pytest.mark.parametrize("content,gap_id", [
    (None, "gap:priorities-missing"),
    (_dated(99), "gap:priorities-stale"),
    ("# What matters\n\nShip it.\n", "gap:priorities-undated"),
])
def test_each_unhealthy_state_raises_its_own_gap(prio, content, gap_id):
    if content is not None:
        prio.write_text(content, encoding="utf-8")
    rows = priorities.gaps()
    assert [g["id"] for g in rows] == [gap_id]
    assert rows[0]["command"] == "otto priorities"
    assert rows[0]["domain"] == config.WORK
    assert rows[0]["why"], "a gap with no explanation is not actionable"


def test_missing_scores_higher_than_stale(prio):
    """No priorities at all is worse than priorities nobody re-read."""
    missing = priorities.gaps()[0]["score"]
    prio.write_text(_dated(99), encoding="utf-8")
    stale = priorities.gaps()[0]["score"]
    assert missing > stale


# ---- render --------------------------------------------------------------------

def test_render_names_the_path_when_there_is_nothing_to_read(prio):
    out = priorities.render()
    assert "no priorities recorded" in out
    assert str(prio) in out


def test_render_shouts_about_a_stale_file(prio):
    prio.write_text(_dated(60), encoding="utf-8")
    out = priorities.render()
    assert "STALE" in out
    assert "Ship the thing." in out, "the body is still shown, not swallowed"
