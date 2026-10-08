"""otto/connectors: the direct Google read, the classification call, and the refresh
path that uses them. Every test runs with a fake HTTP session and a fake model call:
no network, no credentials, no token that is anyone's.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from otto import config, refresh
from otto.connectors import classify, google
from otto.models import Schedule


# ---- a fake Google ------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Routes (method, url-prefix) to a handler of (params, data) -> (status, body)."""

    def __init__(self):
        self.routes: list[tuple[str, str, object]] = []
        self.calls: list[tuple[str, str, dict]] = []

    def route(self, method, prefix, handler):
        self.routes.append((method, prefix, handler))

    def _dispatch(self, method, url, params, data, headers):
        self.calls.append((method, url, {"params": params, "data": data, "headers": headers}))
        for m, prefix, handler in self.routes:
            if m == method and url.startswith(prefix):
                status, body = handler(params or {}, data or {})
                return _Resp(status, body)
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("GET", url, params, None, headers or {})

    def post(self, url, data=None, headers=None, timeout=None):
        return self._dispatch("POST", url, None, data, headers or {})


@pytest.fixture
def google_home(tmp_path, monkeypatch):
    """OTTO_HOME under tmp, a client secret on disk, and a `work` token that is
    already expired so the first read has to refresh it."""
    monkeypatch.setattr(config, "OTTO_HOME", tmp_path)
    secret = tmp_path / "client.json"
    secret.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}),
                      encoding="utf-8")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET_FILE", str(secret))
    google.save_token("work", {
        "alias": "work", "client_id": "cid", "refresh_token": "rt-work",
        "access_token": "stale", "expires_at": 0, "scopes": list(google.SCOPES),
        "obtained_at": "2026-10-01T00:00:00Z", "last_refreshed": "2026-10-01T00:00:00Z",
        "email": "owner@example.com",
    })
    return tmp_path


def _token_route(sess: FakeSession, status=200, body=None):
    body = body if body is not None else {"access_token": "fresh", "expires_in": 3600}
    sess.route("POST", google.TOKEN_URL, lambda p, d: (status, body))


# ---- tokens ----------------------------------------------------------------------------

def test_expired_token_is_refreshed_once_and_the_file_updated(google_home):
    sess = FakeSession()
    _token_route(sess)
    sess.route("GET", google.GMAIL_API + "/profile", lambda p, d: (200, {"emailAddress": "o@x"}))
    acct = google.Account("work", sess)
    assert acct.access_token() == "fresh"
    assert acct.access_token() == "fresh", "cached until it nears expiry"
    posts = [c for c in sess.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["data"]["grant_type"] == "refresh_token"
    assert posts[0][2]["data"]["refresh_token"] == "rt-work"
    on_disk = google.load_token("work")
    assert on_disk["access_token"] == "fresh" and on_disk["last_refreshed"] != "2026-10-01T00:00:00Z"


def test_invalid_grant_is_a_consent_expired_error_naming_the_command(google_home):
    sess = FakeSession()
    _token_route(sess, 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    acct = google.Account("work", sess)
    with pytest.raises(google.GoogleError) as e:
        acct.access_token()
    assert e.value.code == "consent_expired"
    assert "otto google auth work" in str(e.value)


def test_missing_token_and_missing_client_secret_are_named(google_home, monkeypatch):
    with pytest.raises(google.GoogleError) as e:
        google.Account("personal", FakeSession())
    assert e.value.code == "no_token" and "otto google auth personal" in str(e.value)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET_FILE", "")
    with pytest.raises(google.GoogleError) as e2:
        google.client_secret()
    assert e2.value.code == "no_client_secret"


def test_status_never_prints_a_token(google_home):
    rows = {r["alias"]: r for r in google.status()}
    assert rows["work"]["present"] and rows["work"]["email"] == "owner@example.com"
    assert not rows["personal"]["present"]
    assert "rt-work" not in json.dumps(rows) and "stale" not in json.dumps(rows)


# ---- reads -----------------------------------------------------------------------------

def test_gmail_messages_page_dedupe_by_thread_and_sort_newest_first(google_home):
    sess = FakeSession()
    _token_route(sess)
    pages = {None: {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t1"}],
                    "nextPageToken": "p2"},
             "p2": {"messages": [{"id": "m3", "threadId": "t3"}]}}
    sess.route("GET", google.GMAIL_API + "/messages/",
               lambda p, d: (200, _message(p)))
    sess.route("GET", google.GMAIL_API + "/messages",
               lambda p, d: (200, pages[p.get("pageToken")]))
    rows = google.gmail_messages(google.Account("work", sess), query="newer_than:1d", max_results=10)
    assert [r["id"] for r in rows] == ["m3", "m1"], "one per thread, newest first"
    assert rows[0]["from"] == "Tim Hsu <tim@example.com>" and rows[0]["unread"] is True
    assert len(rows[0]["snippet"]) <= 300
    list_calls = [c for c in sess.calls if c[1] == google.GMAIL_API + "/messages"]
    assert list_calls[0][2]["params"]["q"] == "newer_than:1d"
    assert list_calls[1][2]["params"]["pageToken"] == "p2"
    meta = [c for c in sess.calls if "/messages/m" in c[1]]
    assert all(c[2]["params"]["format"] == "metadata" for c in meta)


def _message(params):
    # The handler gets the GET params, not the path; the id is in the URL, so the
    # fake keys the body by call order through a closure-free trick: a counter.
    _message.n = getattr(_message, "n", 0) + 1
    ident = {1: ("m1", "t1", 1_700_000_000_000), 2: ("m3", "t3", 1_700_000_900_000)}[_message.n]
    mid, tid, ms = ident
    return {"id": mid, "threadId": tid, "internalDate": str(ms), "snippet": "x" * 400,
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [{"name": "From", "value": "Tim Hsu <tim@example.com>"},
                                    {"name": "Subject", "value": f"subject {mid}"},
                                    {"name": "Date", "value": "Tue, 07 Oct 2026 10:00:00 +0000"}]}}


def test_calendar_events_roles_attendees_and_cancelled_dropped(google_home):
    sess = FakeSession()
    _token_route(sess)
    items = [
        {"summary": "Standup", "start": {"dateTime": "2026-10-07T09:00:00-07:00"},
         "end": {"dateTime": "2026-10-07T09:15:00-07:00"},
         "organizer": {"email": "me@example.com", "self": True},
         "attendees": [{"email": "me@example.com", "self": True},
                       {"email": "ana@example.com"}, {"email": "room@resource", "resource": True}]},
        {"summary": "Optional sync", "start": {"dateTime": "2026-10-07T11:00:00-07:00"},
         "end": {"dateTime": "2026-10-07T11:30:00-07:00"},
         "attendees": [{"email": "me@example.com", "self": True, "optional": True},
                       {"email": "bo@example.com"}]},
        {"summary": "Gone", "status": "cancelled", "start": {"dateTime": "2026-10-07T12:00:00-07:00"}},
        {"summary": "Offsite", "start": {"date": "2026-10-07"}, "end": {"date": "2026-10-08"}},
    ]
    sess.route("GET", google.CALENDAR_API, lambda p, d: (200, {"items": items}))
    start, end = google.today_window(datetime(2026, 10, 7, 8, tzinfo=timezone.utc))
    rows = google.calendar_events(google.Account("work", sess), start=start, end=end)
    assert [r["title"] for r in rows] == ["Standup", "Optional sync", "Offsite"]
    assert rows[0]["role"] == "organizer" and rows[0]["who"] == ["ana@example.com"]
    assert rows[1]["role"] == "optional" and rows[1]["who"] == ["bo@example.com"]
    assert rows[2]["all_day"] is True and rows[2]["role"] is None
    q = [c for c in sess.calls if c[0] == "GET" and "calendar" in c[1]][0][2]["params"]
    assert q["singleEvents"] == "true" and q["timeMin"] == start.isoformat()
    assert end - start == timedelta(days=1)


# ---- classify --------------------------------------------------------------------------

def test_classify_uses_the_seam_fills_gaps_and_prices_haiku(monkeypatch):
    seen = {}

    def fake_call(system, user, model):
        seen.update(system=system, user=user, model=model)
        return {"data": {"summary": "1 needs a reply",
                         "items": [{"id": "a", "needs": "reply", "why": "Tim asked for the spec"},
                                   {"id": "zzz", "needs": "reply", "why": "not a given id"}]},
                "usage": {"input_tokens": 1000, "output_tokens": 100}}

    monkeypatch.setattr(classify, "call_model", fake_call)
    msgs = [{"id": "a", "from": "Tim <t@x>", "subject": "Spec?", "snippet": "where is it"},
            {"id": "b", "from": "News <n@x>", "subject": "Weekly", "snippet": "read all about it"}]
    out = classify.classify(msgs, owner="Alex", model="claude-haiku-5-5")
    assert seen["model"] == "claude-haiku-5-5"
    assert "Alex" in seen["system"] and "Never default to \"reply\"" in seen["system"]
    assert '"id": "a"' in seen["user"] and '"id": "b"' in seen["user"]
    assert out.verdicts["a"].needs == "reply" and out.verdicts["a"].why == "Tim asked for the spec"
    assert out.verdicts["b"].needs == "awareness", "a skipped message is awareness, never dropped"
    assert "zzz" not in out.verdicts, "an id the model invented is ignored"
    assert out.cost_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1_000_000)
    assert out.input_tokens == 1000 and out.summary == "1 needs a reply"


def test_classify_empty_list_makes_no_call_and_unknown_model_has_no_cost(monkeypatch):
    def boom(*a):
        raise AssertionError("must not be called")
    monkeypatch.setattr(classify, "call_model", boom)
    out = classify.classify([], owner="Alex")
    assert out.verdicts == {} and out.cost_usd is None and "no mail" in out.summary
    assert classify.cost_for("claude-nope", 10, 10) is None


def test_api_key_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "from-setting")
    assert classify.api_key() == "from-setting"
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert classify.api_key() == "from-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(config, "CLAUDE_DIR", tmp_path)
    (tmp_path / ".anthropic-env.cache").write_text(
        'export ANTHROPIC_ADMIN_API_KEY="admin"\nexport ANTHROPIC_API_KEY="from-cache"\n', encoding="utf-8")
    assert classify.api_key() == "from-cache"


# ---- the refresh path ------------------------------------------------------------------

def _wire(monkeypatch, store, sess: FakeSession, *, verdicts=None):
    monkeypatch.setattr(config, "REFRESH_SOURCES", {
        "work": {"hint": "irrelevant", "connector": {"kind": "google", "alias": "work"}},
        "personal": {"hint": "the session path"}})
    monkeypatch.setattr(config, "GOOGLE_MAIL_MAX", 10)
    store.upsert_schedule(Schedule(name="inbox-sync", command="(built in)", domain="work",
                                   runner="refresh"))
    _token_route(sess)
    sess.route("GET", google.CALENDAR_API, lambda p, d: (200, {"items": [
        {"summary": "Standup", "start": {"dateTime": "2026-10-07T09:00:00+00:00"},
         "end": {"dateTime": "2026-10-07T09:15:00+00:00"},
         "organizer": {"self": True}, "attendees": [{"email": "ana@example.com"}]}]}))
    sess.route("GET", google.GMAIL_API + "/messages/", lambda p, d: (200, {
        "id": "m1", "threadId": "t1", "internalDate": "1790000000000", "snippet": "where is the spec",
        "payload": {"headers": [{"name": "From", "value": "Tim Hsu <tim@example.com>"},
                                {"name": "Subject", "value": "Kit spec"}]}}))
    sess.route("GET", google.GMAIL_API + "/messages", lambda p, d: (200, {"messages": [{"id": "m1", "threadId": "t1"}]}))
    verdicts = verdicts if verdicts is not None else {
        "summary": "1 needs a reply", "items": [{"id": "m1", "needs": "reply", "why": "Tim asked for the spec"}]}
    monkeypatch.setattr(classify, "call_model", lambda s, u, m: {"data": verdicts, "usage": {"input_tokens": 500, "output_tokens": 50}})


def test_direct_refresh_writes_both_snapshots_stamps_and_closes_the_run(google_home, store, monkeypatch):
    sess = FakeSession()
    _wire(monkeypatch, store, sess)
    run = refresh.start_connector(store, "work", refresh.connector_for("work"), session=sess, background=False)
    assert run.runner == "inline" and run.pid is None and run.status == "ok"
    assert run.cost_usd == pytest.approx((500 * 0.10 + 50 * 0.50) / 1_000_000)
    assert "connector=google:work" in run.notes
    snaps = store.snapshots()
    agenda, mail = snaps["work/agenda"], snaps["work/mail"]
    assert agenda.source == "otto-refresh" and agenda.items[0]["title"] == "Standup"
    assert agenda.items[0]["role"] == "organizer" and agenda.items[0]["who"] == "ana@example.com"
    assert "when" in agenda.items[0] and "ends" in agenda.items[0]
    assert mail.items == [{"title": "Tim Hsu: Kit spec", "needs": "reply",
                           "when": mail.items[0]["when"], "why": "Tim asked for the spec"}]
    assert mail.summary == "1 needs a reply"
    sched = store.get_schedule("inbox-sync")
    assert sched.last_status == "ok" and sched.last_run_id == run.id
    assert store.get_run(run.id).status == "ok", "the run closes itself"
    assert "1 need a reply" in run.result_summary


def test_direct_refresh_from_start_takes_the_connector_and_a_bare_domain_the_session(google_home, store, monkeypatch):
    sess = FakeSession()
    _wire(monkeypatch, store, sess)
    monkeypatch.setattr(refresh.requests, "Session", lambda: sess)
    # background=True is the daemon's path: the Run comes back running and closes
    # itself once the caller has recorded it, which is what the daemon does next.
    run = refresh.start(store, "work")
    assert run.status == "running" and run.runner == "inline"
    store.upsert_run(run)
    deadline = datetime.now() + timedelta(seconds=5)
    while store.get_run(run.id).status == "running" and datetime.now() < deadline:
        threading.Event().wait(0.05)
    assert store.get_run(run.id).status == "ok"
    assert refresh.connector_for("personal") is None, "no connector: the session path"


def test_missing_grant_is_the_skipped_reason_and_one_notice(google_home, store, monkeypatch):
    monkeypatch.setattr(config, "REFRESH_SOURCES", {
        "personal": {"connector": {"kind": "google", "alias": "personal"}}})
    with pytest.raises(RuntimeError, match="otto google auth personal"):
        refresh.start(store, "personal")
    notes = [n for n in store.notices() if n.source == "refresh"]
    assert len(notes) == 1 and notes[0].command == "otto google auth personal"
    assert store.runs() == [], "nothing was recorded as a run"


def test_api_failure_closes_the_run_failed_and_stamps_failed(google_home, store, monkeypatch):
    sess = FakeSession()
    _wire(monkeypatch, store, sess)
    sess.routes = [r for r in sess.routes if not r[1].startswith(google.CALENDAR_API)]
    sess.route("GET", google.CALENDAR_API, lambda p, d: (503, {"error": {"message": "backend down"}}))
    run = refresh.start_connector(store, "work", refresh.connector_for("work"), session=sess, background=False)
    assert run.status == "failed" and run.error_kind == "api" and "backend down" in run.notes
    assert store.get_schedule("inbox-sync").last_status == "failed"
    assert "work/agenda" not in store.snapshots(), "a failed read writes nothing"
    assert any(n.source == "refresh" for n in store.notices())


def test_unknown_connector_kind_is_refused(google_home, store, monkeypatch):
    monkeypatch.setattr(config, "REFRESH_SOURCES", {"work": {"connector": {"kind": "exchange", "alias": "work"}}})
    with pytest.raises(RuntimeError, match="unknown connector kind"):
        refresh.start(store, "work")


# ---- consent -----------------------------------------------------------------------------

def test_authorize_runs_the_loopback_flow_and_stores_a_refresh_token(google_home):
    sess = FakeSession()
    exchanged = {}

    def token(p, d):
        exchanged.update(d)
        return 200, {"access_token": "at", "refresh_token": "rt-new", "expires_in": 3600,
                     "scope": " ".join(google.SCOPES)}
    sess.route("POST", google.TOKEN_URL, token)
    sess.route("GET", google.GMAIL_API + "/profile", lambda p, d: (200, {"emailAddress": "p@example.com"}))

    def browser(url):
        # Play Google: read the redirect and state off the consent URL, then hit the
        # loopback the way the real redirect would, from another thread.
        q = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(url.split("?", 1)[1]).items()}
        assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
        assert q["scope"].split() == list(google.SCOPES)
        target = q["redirect_uri"] + "?code=CODE123&state=" + q["state"]
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

    info = google.authorize("personal", open_browser=browser, session=sess, timeout=10)
    assert exchanged["code"] == "CODE123" and exchanged["grant_type"] == "authorization_code"
    assert exchanged["code_verifier"] and exchanged["redirect_uri"].startswith("http://127.0.0.1:")
    assert "refresh_token" not in info and "access_token" not in info
    assert info["email"] == "p@example.com"
    assert google.load_token("personal")["refresh_token"] == "rt-new"


def test_authorize_rejects_a_wrong_state(google_home):
    sess = FakeSession()
    sess.route("POST", google.TOKEN_URL, lambda p, d: (200, {"refresh_token": "never"}))

    def browser(url):
        q = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(url.split("?", 1)[1]).items()}
        target = q["redirect_uri"] + "?code=CODE&state=forged"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

    with pytest.raises(google.GoogleError, match="wrong state"):
        google.authorize("personal", open_browser=browser, session=sess, timeout=10)
    assert google.load_token("personal") is None
    assert not any(c[0] == "POST" for c in sess.calls)
