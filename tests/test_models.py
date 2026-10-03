"""Models: validation and round-tripping.

The interesting cases are not "does pydantic work". They are the places where a
model has to survive hostile input, because state is JSON on disk and one row
that will not serialise takes down every surface that reads the file.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from otto.models import BoardCard, Run, Task, safe_text


# ---- Task --------------------------------------------------------------------

def test_task_needs_an_id_and_a_title():
    with pytest.raises(ValidationError):
        Task(id="a" * 32)
    with pytest.raises(ValidationError):
        Task(title="no id")


def test_task_defaults_are_the_safe_ones():
    """A task filed with nothing specified must not be dispatchable-by-surprise
    into a column that means "run this now"."""
    t = Task(id="a" * 32, title="t")
    assert t.status == "backlog"
    assert t.domain == "work"
    assert t.priority == "normal"
    assert t.source == "manual"
    assert t.seen_count == 1
    assert t.attempts == 0


@pytest.mark.parametrize("field,value", [
    ("status", "in-progress"),
    ("priority", "critical"),
    ("domain", "team"),
    ("source", "somewhere"),
])
def test_task_rejects_values_outside_the_vocabulary(field, value):
    with pytest.raises(ValidationError):
        Task(id="a" * 32, title="t", **{field: value})


def test_task_round_trips_through_json():
    t = Task(id="a" * 32, title="t", detail="why", tags=["falcon", "tier-1"])
    back = Task.model_validate(json.loads(json.dumps(t.model_dump())))
    assert back == t


def test_touch_moves_updated_but_not_created():
    t = Task(id="a" * 32, title="t", created="2020-01-01T00:00:00Z",
             updated="2020-01-01T00:00:00Z")
    t.touch()
    assert t.created == "2020-01-01T00:00:00Z"
    assert t.updated != "2020-01-01T00:00:00Z"


def test_detail_carrying_a_quoted_command_line_survives_serialisation():
    """The detail field is exactly where evidence text lands."""
    body = 'ran: bash -c "find / -type d -iname x" && echo \\"done\\"'
    t = Task(id="a" * 32, title="t", detail=body)
    assert Task.model_validate(json.loads(json.dumps(t.model_dump()))).detail == body


# ---- safe_text, the surrogate guard ------------------------------------------

def test_safe_text_neutralises_a_lone_surrogate():
    """A lone surrogate cannot be encoded to JSON, so one in external text takes
    down every reader of the file it lands in (commit 11b5a1b)."""
    hostile = "before \ud800 after"
    cleaned = safe_text(hostile)
    json.dumps(cleaned)  # must not raise
    assert "before" in cleaned and "after" in cleaned


def test_safe_text_leaves_ordinary_unicode_alone():
    for s in ["plain", "accents éàü", "emoji \U0001f600", "日本語", ""]:
        assert safe_text(s) == s


def test_a_task_built_from_hostile_text_still_serialises():
    t = Task(id="a" * 32, title=safe_text("title \ud800 here"),
             detail=safe_text("detail \udfff here"))
    json.dumps(t.model_dump())


# ---- Run ---------------------------------------------------------------------

def _run(**kw) -> Run:
    base = {"id": "a" * 32, "name": "n", "runner": "detached",
            "started": "2026-01-01T00:00:00Z"}
    base.update(kw)
    return Run(**base)


def test_run_is_active_only_while_running():
    assert _run(status="running").active is True
    for status in ("ok", "failed", "orphaned", "killed", "skipped", "due"):
        assert _run(status=status).active is False


def test_run_rejects_an_unknown_runner():
    with pytest.raises(ValidationError):
        _run(runner="machine")


def test_run_round_trips_through_json():
    r = _run(status="failed")
    assert Run.model_validate(json.loads(json.dumps(r.model_dump()))) == r


# ---- BoardCard ---------------------------------------------------------------

def test_board_card_defaults_to_a_task_kind():
    c = BoardCard(id="a" * 32, title="t", status="backlog")
    assert c.kind == "task"


def test_board_card_rejects_an_unknown_kind():
    with pytest.raises(ValidationError):
        BoardCard(id="a" * 32, title="t", status="backlog", kind="widget")
