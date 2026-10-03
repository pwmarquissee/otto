"""Store: the single-writer JSON state layer.

Weighted toward `_read`, because its docstring records that it once lost the
entire task board. It caught OSError alongside JSONDecodeError, treated both as
corruption, quarantined the file and returned the default; a later
read-modify-write then persisted that emptiness. The file had been fine. Those
are the tests that matter here, not the CRUD.
"""

from __future__ import annotations

import json

import pytest

from otto import config
from otto.models import Run, Task
from otto.store import TASKS, Store, WriteDenied, _atomic_write


# ---- writes are the daemon's alone -----------------------------------------

def test_write_refused_without_the_writer_role(reader_store):
    with pytest.raises(WriteDenied) as e:
        reader_store.save_tasks([])
    assert "only the Otto daemon writes state" in str(e.value)


def test_write_allowed_with_the_writer_role(store):
    store.save_tasks([Task(id="a" * 32, title="t")])
    assert len(store.tasks()) == 1


# ---- _read never invents emptiness ------------------------------------------

def test_missing_file_returns_the_default(store):
    assert store._read("nope.json", []) == []
    assert store._read("nope.json", {"k": 1}) == {"k": 1}


def test_unreadable_file_raises_rather_than_reporting_empty(store, monkeypatch):
    """The exact shape of the board-loss bug.

    A transient Windows sharing violation surfaces as PermissionError, which IS
    an OSError. Returning the default here is the lie that cost the board, so a
    persistent read failure must raise instead.
    """
    store.save_tasks([Task(id="a" * 32, title="survivor")])

    def always_denied(*_a, **_kw):
        raise PermissionError("sharing violation")

    monkeypatch.setattr("pathlib.Path.read_text", always_denied)

    with pytest.raises(OSError) as e:
        store._read(TASKS, [])
    assert "Refusing to treat an unreadable file as empty state" in str(e.value)


def test_transient_read_error_is_retried_then_succeeds(store, monkeypatch):
    """A sharing violation clears in milliseconds. One must not be fatal."""
    store.save_tasks([Task(id="a" * 32, title="survivor")])
    real = type(store.dir).read_text
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("sharing violation")
        return real(self, *a, **kw)

    monkeypatch.setattr("pathlib.Path.read_text", flaky)

    rows = store._read(TASKS, [])
    assert calls["n"] == 2, "should have retried exactly once"
    assert rows[0]["title"] == "survivor"


def test_corrupt_json_raises_for_a_non_daemon_reader(reader_store):
    """A CLI reader must see the parse error, not a silent empty board."""
    reader_store.dir.mkdir(parents=True, exist_ok=True)
    (reader_store.dir / TASKS).write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        reader_store.tasks()


def test_corrupt_json_is_quarantined_loudly_for_the_daemon(store, capsys):
    """Quarantine is real data loss, so it must be noisy and keep the bytes."""
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / TASKS).write_text("{not json", encoding="utf-8")

    assert store.tasks() == []

    quarantined = store.dir / (TASKS + ".corrupt")
    assert quarantined.exists(), "the unparseable bytes must be kept, not deleted"
    assert quarantined.read_text(encoding="utf-8") == "{not json"
    assert "CRITICAL" in capsys.readouterr().err


# ---- atomic write ------------------------------------------------------------

def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "s" / "x.json"
    _atomic_write(target, '{"a": 1}\n')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(target.parent.glob(".x.json.*.tmp")) == []


def test_atomic_write_cleans_up_when_the_write_fails(tmp_path, monkeypatch):
    target = tmp_path / "x.json"

    def boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(RuntimeError):
        _atomic_write(target, "{}")

    assert not target.exists()
    assert list(tmp_path.glob(".x.json.*.tmp")) == [], "temp file was orphaned"


def test_write_then_read_round_trips_unicode(store):
    """A stray character took the dashboard down once (commit 11b5a1b)."""
    store.save_tasks([Task(id="a" * 32, title="emoji \U0001f600 and accents éàü")])
    assert store.tasks()[0].title == "emoji \U0001f600 and accents éàü"


# ---- tasks -------------------------------------------------------------------

def test_a_row_from_an_older_schema_does_not_break_the_board(store):
    """One bad row must cost one card, not the whole board."""
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / TASKS).write_text(json.dumps([
        {"id": "a" * 32, "title": "good"},
        {"id": "b" * 32},                      # no title
        {"id": "c" * 32, "title": "also good", "status": "not-a-status"},
        {"id": "d" * 32, "title": "third"},
    ]), encoding="utf-8")

    titles = [t.title for t in store.tasks()]
    assert titles == ["good", "third"], "valid rows on both sides must survive"


def test_get_task_matches_on_an_id_prefix(store):
    store.save_tasks([Task(id="abcdef" + "0" * 26, title="t")])
    assert store.get_task("abcdef").title == "t"
    assert store.get_task("abcdef" + "0" * 26).title == "t"
    assert store.get_task("zzzzzz") is None


def test_upsert_task_replaces_rather_than_appends(store):
    t = Task(id="a" * 32, title="first")
    store.upsert_task(t)
    store.upsert_task(Task(id="a" * 32, title="second"))

    rows = store.tasks()
    assert len(rows) == 1
    assert rows[0].title == "second"


def test_delete_task_reports_whether_it_removed_anything(store):
    store.save_tasks([Task(id="a" * 32, title="t")])
    assert store.delete_task("a" * 32) is True
    assert store.delete_task("a" * 32) is False
    assert store.tasks() == []


# ---- runs --------------------------------------------------------------------

def _run(rid: str, started: str, status: str = "ok") -> Run:
    return Run(id=rid, name="n", runner="detached", status=status, started=started)


def test_save_runs_never_trims_an_active_run(store, monkeypatch):
    """History is bounded, but a running agent must not fall off the ledger."""
    monkeypatch.setattr(config, "RUN_HISTORY_LIMIT", 2)
    runs = [_run(f"{i:032d}", f"2026-01-{i + 1:02d}T00:00:00Z") for i in range(5)]
    runs.append(_run("f" * 32, "2020-01-01T00:00:00Z", status="running"))

    store.save_runs(runs)
    kept = store.runs()

    assert any(r.id == "f" * 32 for r in kept), "oldest but active, must survive"
    assert len([r for r in kept if not r.active]) == 2


def test_save_runs_keeps_the_newest_finished_runs(store, monkeypatch):
    monkeypatch.setattr(config, "RUN_HISTORY_LIMIT", 2)
    store.save_runs([
        _run("1" * 32, "2026-01-01T00:00:00Z"),
        _run("2" * 32, "2026-06-01T00:00:00Z"),
        _run("3" * 32, "2026-03-01T00:00:00Z"),
    ])
    assert [r.id[0] for r in store.runs()] == ["2", "3"]
