"""The session harness: titles, process-backed liveness, the waiting toast, and
the way back in.

Four live sessions and 196 offline ones, each a folder name and eight hex
characters, was the list this replaces. Everything here is recovered from things
that already exist (the transcript, the hook's parent pid, the host app's window),
because Otto owns no PTY and must not pretend to.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from otto import config, sessions
from otto.models import Session, iso, utcnow

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def write_transcript(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def user(text, **kw):
    d = {"type": "user", "message": {"role": "user", "content": text},
         "sessionId": "s", "timestamp": iso(utcnow())}
    d.update(kw)
    return d


# ---- titles --------------------------------------------------------------------------

def test_title_is_the_first_real_prompt(tmp_path):
    t = tmp_path / "a.jsonl"
    write_transcript(t, [
        {"type": "mode", "mode": "normal"},
        {"type": "file-history-snapshot", "snapshot": {}},
        user("can we add in dedupe logic?  Especially for things like sso expiry?"),
        user("second prompt"),
    ])
    assert sessions.title_from_transcript(t) == (
        "can we add in dedupe logic? Especially for things like sso expiry?", "prompt")


def test_claude_codes_own_summary_outranks_the_prompt(tmp_path):
    t = tmp_path / "a.jsonl"
    write_transcript(t, [
        {"type": "summary", "summary": "Board dedupe and SSO session renewal"},
        user("can we add in dedupe logic?"),
    ])
    assert sessions.title_from_transcript(t) == ("Board dedupe and SSO session renewal", "summary")


def test_a_slash_command_titles_as_the_command(tmp_path):
    t = tmp_path / "a.jsonl"
    write_transcript(t, [
        user("<command-message>daily is running…</command-message>\n"
             "<command-name>/daily</command-name>\n<command-args></command-args>"),
    ])
    assert sessions.title_from_transcript(t) == ("/daily", "prompt")


def test_wrappers_and_meta_lines_are_skipped(tmp_path):
    t = tmp_path / "a.jsonl"
    write_transcript(t, [
        user("<local-command-stdout>whatever</local-command-stdout>"),
        user("meta", isMeta=True),
        user("side", isSidechain=True),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "the real one"}]}},
    ])
    assert sessions.title_from_transcript(t) == ("the real one", "prompt")


def test_long_titles_are_cut_to_one_rail_line(tmp_path):
    t = tmp_path / "a.jsonl"
    write_transcript(t, [user("x" * 300)])
    title, _ = sessions.title_from_transcript(t)
    assert len(title) == config.SESSION_TITLE_MAX
    assert title.endswith("…")


def test_missing_or_promptless_transcript_gives_no_title(tmp_path):
    assert sessions.title_from_transcript(tmp_path / "nope.jsonl") is None
    t = tmp_path / "a.jsonl"
    write_transcript(t, [{"type": "mode", "mode": "normal"}])
    assert sessions.title_from_transcript(t) is None


def test_record_titles_a_session_once_and_paul_outranks_it(store, tmp_path, monkeypatch):
    # record() now reads a transcript only under config.TRANSCRIPT_ROOTS (see
    # sessions.transcript_allowed), so this test's transcript lives under one.
    monkeypatch.setattr(config, "TRANSCRIPT_ROOTS", (tmp_path,))
    t = tmp_path / "a.jsonl"
    write_transcript(t, [user("first prompt")])
    s = sessions.record(store, "busy", {"session_id": "abc", "transcript_path": str(t)})
    assert (s.title, s.title_source) == ("first prompt", "prompt")
    write_transcript(t, [user("a different first line now")])
    s = sessions.record(store, "idle", {"session_id": "abc", "transcript_path": str(t)})
    assert s.title == "first prompt"           # once
    s = sessions.rename(store, "abc", "  the dedupe   work ")
    assert (s.title, s.title_source) == ("the dedupe work", "user")
    with pytest.raises(ValueError):
        sessions.rename(store, "abc", "   ")
    with pytest.raises(KeyError):
        sessions.rename(store, "zzz", "x")


# ---- process-backed liveness -----------------------------------------------------------

def test_record_resolves_the_process_from_the_hooks_parent(store, monkeypatch):
    monkeypatch.setattr(sessions, "resolve_process",
                        lambda ppid: {"pid": 4242, "pid_started": 1000.0,
                                      "host": "Windows Terminal", "host_pid": 6316}
                        if ppid == 999 else {})
    s = sessions.record(store, "busy", {"session_id": "abc", "ppid": 999})
    assert (s.pid, s.host, s.host_pid) == (4242, "Windows Terminal", 6316)
    # Resolved once: a later hook with a different parent does not rewrite it.
    s = sessions.record(store, "idle", {"session_id": "abc", "ppid": 1})
    assert s.pid == 4242


def test_sweep_marks_a_dead_process_offline_in_any_state(store, monkeypatch):
    alive = {4242: True, 5151: False}
    monkeypatch.setattr(sessions, "process_alive", lambda pid, started: alive.get(pid, False))
    store.save_sessions([
        Session(session_id="live0001", state="busy", pid=4242, pid_started=1.0,
                last_event=iso(NOW), state_since=iso(NOW)),
        Session(session_id="dead0001", state="busy", pid=5151, pid_started=1.0,
                last_event=iso(NOW), state_since=iso(NOW), title="the dead one"),
        Session(session_id="nopid001", state="busy", last_event=iso(NOW), state_since=iso(NOW)),
    ])
    notes = sessions.sweep(store, now=NOW + timedelta(minutes=1))
    by = {s.session_id: s for s in store.sessions()}
    assert by["live0001"].state == "busy"
    assert by["dead0001"].state == "offline"
    assert by["dead0001"].offline_reason == "process"
    assert by["dead0001"].offline_inferred is False
    # Without a pid, silence is still not a verdict: busy stays busy.
    assert by["nopid001"].state == "busy"
    assert any("the dead one" in n and "exited" in n for n in notes)


def test_silence_is_still_recorded_as_an_inference(store, monkeypatch):
    monkeypatch.setattr(sessions, "process_alive", lambda pid, started: True)
    store.save_sessions([
        Session(session_id="quiet001", state="idle",
                last_event=iso(NOW - timedelta(hours=config.SESSION_OFFLINE_HOURS + 1)),
                state_since=iso(NOW - timedelta(hours=20))),
    ])
    sessions.sweep(store, now=NOW)
    s = store.sessions()[0]
    assert (s.state, s.offline_reason, s.offline_inferred) == ("offline", "silence", True)


def test_a_session_end_hook_records_its_reason(store):
    sessions.record(store, "busy", {"session_id": "abc"})
    s = sessions.record(store, "offline", {"session_id": "abc"})
    assert (s.state, s.offline_reason, s.offline_inferred) == ("offline", "hook", False)


def test_process_alive_refuses_a_recycled_pid(monkeypatch):
    class P:
        def __init__(self, name, created):
            self._n, self._c = name, created

        def name(self):
            return self._n

        def create_time(self):
            return self._c

        def is_running(self):
            return True

        def status(self):
            return "running"

    import psutil
    monkeypatch.setattr(psutil, "Process", lambda pid: P("claude.exe", 1000.0))
    assert sessions.process_alive(1, 1000.5) is True
    assert sessions.process_alive(1, 1900.0) is False          # same pid, different process
    monkeypatch.setattr(psutil, "Process", lambda pid: P("notepad.exe", 1000.0))
    assert sessions.process_alive(1, 1000.0) is False


# ---- the waiting toast -----------------------------------------------------------------

def test_entering_waiting_posts_one_notice_and_leaving_retires_it(store):
    s = sessions.record(store, "waiting", {"session_id": "abcdef12", "cwd": "D:/otto",
                                           "message": "Claude needs your permission to use Bash"})
    notices = [n for n in store.notices() if n.source == "session"]
    assert len(notices) == 1
    n = notices[0]
    assert n.level == "warn"
    assert "is asking you" in n.title
    assert n.body == "Claude needs your permission to use Bash"
    assert n.command == "otto sessions open abcdef12"
    assert n.key == f"session-waiting:abcdef12:{s.state_since}"
    # A second waiting hook for the same prompt is not a second notice.
    sessions.record(store, "waiting", {"session_id": "abcdef12", "message": "still asking"})
    assert len([x for x in store.notices() if x.source == "session"]) == 1
    # Answering it at the keyboard retires the notice before it can toast later.
    sessions.record(store, "busy", {"session_id": "abcdef12"})
    n = next(x for x in store.notices() if x.source == "session")
    assert n.read_at is not None


def test_the_waiting_toast_can_be_turned_off(store, monkeypatch):
    monkeypatch.setattr(config, "SESSION_WAITING_TOAST", False)
    sessions.record(store, "waiting", {"session_id": "abc", "message": "permission?"})
    assert not [n for n in store.notices() if n.source == "session"]


def test_a_dead_process_retires_its_waiting_notice(store, monkeypatch):
    sessions.record(store, "waiting", {"session_id": "abc", "message": "permission?"})
    s = store.get_session("abc")
    s.pid, s.pid_started = 77, 1.0
    store.put_session(s)
    monkeypatch.setattr(sessions, "process_alive", lambda pid, started: False)
    sessions.sweep(store, now=NOW)
    n = next(x for x in store.notices() if x.source == "session")
    assert n.read_at is not None


# ---- the way back in -------------------------------------------------------------------

def test_open_focuses_a_live_sessions_host_window(store, monkeypatch):
    store.save_sessions([Session(session_id="abc", state="busy", host="Windows Terminal",
                                 host_pid=6316, title="dedupe work")])
    calls = []
    monkeypatch.setattr(sessions, "focus_window", lambda pid: calls.append(pid) or True)
    ok, msg = sessions.open_session(store, "abc")
    assert ok and calls == [6316] and "Windows Terminal" in msg


def test_open_resumes_an_ended_session_in_its_directory(store, monkeypatch, tmp_path):
    store.save_sessions([Session(session_id="abcdef1234", state="offline", cwd=str(tmp_path),
                                 title="the dedupe work")])
    spawned = []
    monkeypatch.setattr(sessions.shutil, "which", lambda n: "C:/wt.exe")
    monkeypatch.setattr(sessions.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    ok, msg = sessions.open_session(store, "abcdef12")
    assert ok and "resuming" in msg
    cmd = spawned[0]
    assert cmd[0] == "C:/wt.exe"
    assert cmd[cmd.index("-d") + 1] == str(tmp_path)
    assert cmd[cmd.index("--title") + 1] == "the dedupe work"
    assert cmd[-1] == "claude --resume abcdef1234"


def test_open_refuses_when_the_directory_is_gone(store, tmp_path):
    store.save_sessions([Session(session_id="abc", state="offline",
                                 cwd=str(tmp_path / "gone"))])
    ok, msg = sessions.open_session(store, "abc")
    assert not ok and "gone" in msg


def test_open_tells_the_truth_about_a_live_session_whose_process_died(store, monkeypatch):
    store.save_sessions([Session(session_id="abc", state="busy", pid=5, pid_started=1.0,
                                 host_pid=6316)])
    monkeypatch.setattr(sessions, "process_alive", lambda pid, started: False)
    monkeypatch.setattr(sessions, "focus_window", lambda pid: pytest.fail("must not focus"))
    ok, msg = sessions.open_session(store, "abc")
    assert not ok and "process is gone" in msg


def test_alerts_name_sessions_by_title(store):
    store.save_sessions([Session(
        session_id="abc", state="waiting", title="the dedupe work",
        state_since=iso(NOW - timedelta(minutes=config.SESSION_WAITING_ALERT_MINUTES + 1)),
        last_event=iso(NOW), note="permission?")])
    [a] = sessions.alerts(store, now=NOW)
    assert a.message.startswith("the dedupe work has been waiting on you")
