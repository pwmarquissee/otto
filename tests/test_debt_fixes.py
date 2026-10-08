"""Three DEBT.md items with a root cause already found, pinned so they stay fixed.

1. An event append that fails (a Windows sharing violation on events.jsonl) used
   to surface as a 500 on a board edit that had already landed, so the caller
   retried and wrote twice. The store now retries and then drops the line.
2. A run's final text was cut to 400 characters, and dispatch copies it into the
   card's plan for a PREPARE run, so every tier-1 proposal arrived mid-sentence.
3. There was no read-only way to see one card whole from the terminal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otto import config
from otto import daemon as daemon_mod
from otto.cli import board as cli_board
from otto.models import Event, Run
from otto.runners import detached
from otto.store import Store

from conftest import make_task

OWN = f"http://127.0.0.1:{config.PORT}"


@pytest.fixture
def api(tmp_path, daemon, monkeypatch):
    fresh = Store(tmp_path / "state")
    monkeypatch.setattr(daemon_mod, "store", fresh)
    monkeypatch.setattr(daemon_mod, "_state_cache", None)
    client = TestClient(daemon_mod.app, base_url=OWN)
    client.store = fresh
    return client


def _break_events_file(monkeypatch, store: Store, failures: int) -> list[int]:
    """Make the next `failures` opens of events.jsonl raise the Windows sharing
    violation; count every attempt so the retry is visible."""
    events_path = store._path("events.jsonl")
    attempts: list[int] = []
    real_open = Path.open

    def flaky_open(self, *a, **kw):
        if self == events_path and "a" in (a[0] if a else kw.get("mode", "")):
            attempts.append(1)
            if len(attempts) <= failures:
                raise PermissionError(32, "The process cannot access the file because "
                                          "it is being used by another process")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", flaky_open)
    return attempts


# ---- 1. the post-write log -----------------------------------------------------------

def test_a_transient_sharing_violation_is_retried_and_the_line_lands(store, monkeypatch):
    attempts = _break_events_file(monkeypatch, store, failures=2)
    store.log("after a write", source="test")
    assert len(attempts) == 3, "two refusals, then the append"
    assert store.log_failures == 0
    assert [e.message for e in store.events()] == ["after a write"]


def test_an_append_that_never_lands_is_counted_not_raised(store, monkeypatch):
    _break_events_file(monkeypatch, store, failures=99)
    store.log("lost line", source="test")
    assert store.log_failures == 1
    assert "PermissionError" in (store.last_log_error or "")
    assert store.events() == []


def test_a_board_edit_still_lands_and_returns_200_when_the_event_cannot(api, monkeypatch):
    t = make_task(id="b" * 32, title="edit me", status="backlog")
    api.store.upsert_task(t)
    _break_events_file(monkeypatch, api.store, failures=99)
    r = api.patch(f"/api/tasks/{t.id}", json={"status": "queued"})
    assert r.status_code == 200, r.text
    assert api.store.get_task(t.id).status == "queued"
    assert api.store.log_failures == 1


def test_write_denied_is_still_an_error(reader_store):
    from otto.store import WriteDenied
    with pytest.raises(WriteDenied):
        reader_store.append_event(Event(level="info", source="t", message="nope"))


# ---- 2. the result cap -----------------------------------------------------------------

def _run(**kw) -> Run:
    base = dict(id="c" * 32, name="prepare", runner="detached", status="running",
                domain=config.WORK, cwd=".")
    base.update(kw)
    return Run(**base)


def test_a_long_proposal_survives_the_harvest_whole():
    run = _run()
    plan = "## Plan\n" + ("- a step that matters\n" * 300)
    assert len(plan) > 400
    detached._apply_result(run, {"type": "result", "subtype": "success", "result": plan})
    assert run.result_summary == plan.strip()
    assert run.status == "ok"


def test_the_harvest_still_caps_at_result_chars():
    run = _run()
    detached._apply_result(run, {"type": "result", "subtype": "success",
                                 "result": "x" * (detached.RESULT_CHARS + 5)})
    assert len(run.result_summary) == detached.RESULT_CHARS


def test_the_state_payload_trims_a_long_result_and_flags_it(api):
    long = "y" * (config.STATE_DETAIL_CHARS + 50)
    api.store.upsert_run(_run(status="ok", result_summary=long,
                              started_at=datetime.now(timezone.utc).isoformat()))
    state = api.get("/api/state").json()
    row = next(r for r in state["runs"] if r["id"] == "c" * 32)
    assert len(row["result_summary"]) == config.STATE_DETAIL_CHARS
    assert row["result_truncated"] is True
    whole = api.get(f"/api/runs/{'c' * 32}").json()
    assert whole["result_summary"] == long
    assert "result_truncated" not in whole


def test_a_short_result_is_not_flagged(api):
    api.store.upsert_run(_run(status="ok", result_summary="short",
                              started_at=datetime.now(timezone.utc).isoformat()))
    row = next(r for r in api.get("/api/state").json()["runs"] if r["id"] == "c" * 32)
    assert row["result_summary"] == "short" and "result_truncated" not in row


# ---- 3. otto task show -------------------------------------------------------------------

class _Client:
    def __init__(self, body):
        self.body = body

    def task(self, task_id):
        if task_id != self.body["id"][: len(task_id)]:
            raise RuntimeError(f"no task matching {task_id}")
        return self.body


class _Args:
    def __init__(self, id, json=False):
        self.id, self.json = id, json


def _body():
    t = make_task(id="d" * 32, title="Rotate the signing cert", status="needs-you",
                  detail="line one\nline two", plan="1. check expiry\n2. rotate",
                  result=None, tags=["security"], tier="tier-1-approval")
    body = t.model_dump()
    body["run"] = {"id": "e" * 32, "status": "ok", "name": "prepare", "cost_usd": 0.42}
    return body


def test_task_show_prints_the_card_whole(capsys):
    rc = cli_board.cmd_task_show(_Args("dddddd"), _Client(_body()))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Rotate the signing cert" in out
    assert "needs-you" in out and "tier-1-approval" in out and "security" in out
    assert "line one" in out and "line two" in out
    assert "PLAN" in out and "2. rotate" in out
    assert "RESULT" not in out, "an empty section is not printed"
    assert "eeeeee ok prepare cost $0.42" in out


def test_task_show_json_is_the_api_body(capsys):
    body = _body()
    assert cli_board.cmd_task_show(_Args("dddddd", json=True), _Client(body)) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_task_show_unknown_id_is_an_error(capsys):
    rc = cli_board.cmd_task_show(_Args("zzzzzz"), _Client(_body()))
    assert rc == 1
    assert "no task matching" in capsys.readouterr().err


def test_task_show_is_registered():
    from otto.cli import build_parser
    args = build_parser().parse_args(["task", "show", "abc123", "--json"])
    assert args.fn is cli_board.cmd_task_show and args.id == "abc123" and args.json

