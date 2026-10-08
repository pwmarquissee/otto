"""Google, read-only, by hand: OAuth installed-app consent, token refresh, and the
two reads the refresh needs (Gmail metadata, primary-calendar events).

Why by hand. The whole surface Otto needs is four HTTPS endpoints, and the
`requests` dependency is already here. google-auth-oauthlib and the discovery
client would add a dependency tree larger than this package for a loopback
redirect and a token refresh, which fit in a page each.

What a token file holds. `<OTTO_HOME>/google/<alias>.json` carries the REFRESH
TOKEN for that account, scoped to gmail.readonly and calendar.readonly. It never
lives in the repo (OTTO_HOME is outside it), and nothing here prints it. Losing the
file costs one `otto google auth <alias>`; leaking it gives the holder read access
to that mailbox until the grant is revoked at myaccount.google.com. The scopes are
the real control: a readonly grant cannot send, delete or label no matter what the
code does, which is the backstop the MCP path never had for the personal account.

Every network call goes through a `requests.Session`-shaped object passed in, so
the tests drive the whole flow with a fake and no credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .. import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
ALIASES = ("work", "personal")

# Refresh a little before Google says the access token dies, so a read that starts
# on the last second of validity does not fail halfway through its pages.
_EXPIRY_SLACK = 60.0


class GoogleError(RuntimeError):
    """Something the owner has to act on, named so the notice can say what.

    code is one of: no_client_secret, no_token, consent_expired, api.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- files -----------------------------------------------------------------------

def token_dir() -> Path:
    return Path(config.OTTO_HOME) / "google"


def token_path(alias: str) -> Path:
    if alias not in ALIASES:
        raise ValueError(f"alias must be one of {', '.join(ALIASES)}, not {alias!r}")
    return token_dir() / f"{alias}.json"


def load_token(alias: str) -> dict[str, Any] | None:
    p = token_path(alias)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


def save_token(alias: str, data: dict[str, Any]) -> Path:
    p = token_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


def client_secret(path: Path | None = None) -> dict[str, str]:
    """The OAuth client from the JSON the Cloud Console downloads. Accepts the
    `installed` (desktop) and `web` shapes; only id and secret are read."""
    p = Path(path) if path else (Path(config.GOOGLE_CLIENT_SECRET_FILE)
                                 if config.GOOGLE_CLIENT_SECRET_FILE else None)
    if p is None or not p.is_file():
        raise GoogleError(
            "no_client_secret",
            "no OAuth client: set OTTO_GOOGLE_CLIENT_SECRET_FILE to the client secret JSON "
            "from the Google Cloud Console (docs/control-room/connectors.md)")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GoogleError("no_client_secret", f"could not read {p}: {e}") from e
    block = raw.get("installed") or raw.get("web") or {}
    cid, sec = block.get("client_id"), block.get("client_secret")
    if not cid or not sec:
        raise GoogleError("no_client_secret", f"{p} holds no client_id/client_secret")
    return {"client_id": str(cid), "client_secret": str(sec)}


def status() -> list[dict[str, Any]]:
    """One row per alias: whether a token is on disk and when it last moved. Never
    the token itself."""
    rows = []
    for alias in ALIASES:
        tok = load_token(alias)
        rows.append({
            "alias": alias,
            "path": str(token_path(alias)),
            "present": tok is not None,
            "email": (tok or {}).get("email"),
            "obtained_at": (tok or {}).get("obtained_at"),
            "last_refreshed": (tok or {}).get("last_refreshed"),
            "scopes": list((tok or {}).get("scopes") or []),
        })
    return rows


# ---- consent --------------------------------------------------------------------

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _Loopback:
    """A one-shot HTTP server on 127.0.0.1 that catches Google's redirect."""

    def __init__(self, port: int = 0):
        self.result: dict[str, list[str]] | None = None
        self.done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                outer.result = parse_qs(urlparse(self.path).query)
                body = (b"<html><body><p>Otto has the code. You can close this tab.</p>"
                        b"</body></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer.done.set()

            def log_message(self, *_args):  # quiet: the CLI reports, not the server
                return

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="otto-google-loopback")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def authorize(alias: str, *, secret_file: Path | None = None,
              open_browser: Callable[[str], Any] = webbrowser.open,
              session: requests.Session | None = None, port: int = 0,
              timeout: float = 300.0, now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Run the consent flow for one alias and store its refresh token.

    Opens the consent page in the owner's browser, catches the redirect on a
    loopback port, exchanges the code with PKCE, and writes the token file.
    `open_browser` and `session` are injectable so a test can play both ends.
    """
    if alias not in ALIASES:
        raise ValueError(f"alias must be one of {', '.join(ALIASES)}, not {alias!r}")
    client = client_secret(secret_file)
    sess = session or requests.Session()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    lb = _Loopback(port)
    lb.start()
    try:
        redirect = f"http://127.0.0.1:{lb.port}/"
        url = AUTH_URL + "?" + urlencode({
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            # Google only returns a refresh token on the first consent unless asked
            # again; a re-auth after losing the file would otherwise come back empty.
            "prompt": "consent",
        })
        open_browser(url)
        if not lb.done.wait(timeout):
            raise GoogleError("api", f"no redirect from the browser within {int(timeout)}s")
        got = lb.result or {}
    finally:
        lb.stop()
    if got.get("error"):
        raise GoogleError("api", f"consent refused: {got['error'][0]}")
    if got.get("state", [None])[0] != state:
        raise GoogleError("api", "redirect carried the wrong state; not exchanging it")
    code = got.get("code", [None])[0]
    if not code:
        raise GoogleError("api", "redirect carried no code")
    r = sess.post(TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }, timeout=30)
    if r.status_code != 200:
        raise GoogleError("api", f"token exchange failed: HTTP {r.status_code} {_err(r)}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GoogleError("api", "Google returned no refresh token; revoke Otto at "
                                 "myaccount.google.com/permissions and run auth again")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    data = {
        "alias": alias,
        "client_id": client["client_id"],
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token"),
        "expires_at": now() + float(tok.get("expires_in") or 0),
        "scopes": sorted(str(tok.get("scope") or " ".join(SCOPES)).split()),
        "obtained_at": stamp,
        "last_refreshed": stamp,
        "email": _profile_email(sess, tok.get("access_token")),
    }
    save_token(alias, data)
    return {k: v for k, v in data.items() if k not in ("refresh_token", "access_token")}


def _profile_email(sess: requests.Session, access_token: str | None) -> str | None:
    """Which mailbox the grant is for, so `status` can say it. Best effort."""
    if not access_token:
        return None
    try:
        r = sess.get(f"{GMAIL_API}/profile",
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        if r.status_code == 200:
            return (r.json() or {}).get("emailAddress")
    except (requests.RequestException, ValueError):
        pass
    return None


def _err(r: Any) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            e = body.get("error")
            if isinstance(e, dict):
                return str(e.get("message") or e)
            if e:
                return f"{e}: {body.get('error_description', '')}".strip(": ")
    except ValueError:
        pass
    return (getattr(r, "text", "") or "")[:200]


# ---- an account --------------------------------------------------------------------

@dataclass
class Account:
    alias: str
    session: requests.Session
    secret_file: Path | None = None
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        self.token = load_token(self.alias)
        if self.token is None:
            raise GoogleError(
                "no_token", f"no Google token for `{self.alias}`: run `otto google auth {self.alias}`")

    def access_token(self) -> str:
        tok = self.token
        if tok.get("access_token") and float(tok.get("expires_at") or 0) - _EXPIRY_SLACK > self.now():
            return str(tok["access_token"])
        return self._refresh()

    def _refresh(self) -> str:
        client = client_secret(self.secret_file)
        r = self.session.post(TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": self.token["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code != 200:
            detail = _err(r)
            if r.status_code in (400, 401) and "invalid_grant" in detail:
                raise GoogleError(
                    "consent_expired",
                    f"Google no longer honors the `{self.alias}` grant ({detail}); run "
                    f"`otto google auth {self.alias}` again")
            raise GoogleError("api", f"token refresh failed: HTTP {r.status_code} {detail}")
        body = r.json()
        self.token["access_token"] = body["access_token"]
        self.token["expires_at"] = self.now() + float(body.get("expires_in") or 0)
        self.token["last_refreshed"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        save_token(self.alias, self.token)
        return str(self.token["access_token"])

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.session.get(url, params=params or {},
                             headers={"Authorization": f"Bearer {self.access_token()}"},
                             timeout=30)
        if r.status_code == 401:
            # One stale token is normal; a second 401 is a revoked grant.
            self.token["access_token"] = None
            r = self.session.get(url, params=params or {},
                                 headers={"Authorization": f"Bearer {self.access_token()}"},
                                 timeout=30)
        if r.status_code != 200:
            raise GoogleError("api", f"GET {url.split('/v')[0]}... HTTP {r.status_code} {_err(r)}")
        body = r.json()
        return body if isinstance(body, dict) else {}


# ---- reads --------------------------------------------------------------------------

def gmail_messages(acct: Account, *, query: str, max_results: int) -> list[dict[str, Any]]:
    """Messages matching `query`, newest first, one row per thread, capped.

    Metadata only: From/To/Subject/Date headers plus Gmail's own snippet. Bodies
    never cross the wire, which keeps the model call small and the log clean.
    """
    ids: list[dict[str, str]] = []
    token: str | None = None
    while len(ids) < max_results:
        params: dict[str, Any] = {"q": query, "maxResults": min(50, max_results - len(ids))}
        if token:
            params["pageToken"] = token
        page = acct.get(f"{GMAIL_API}/messages", params)
        ids.extend(m for m in (page.get("messages") or []) if isinstance(m, dict) and m.get("id"))
        token = page.get("nextPageToken")
        if not token:
            break
    out: list[dict[str, Any]] = []
    seen_threads: set[str] = set()
    for ref in ids[:max_results]:
        if ref.get("threadId") in seen_threads:
            continue
        m = acct.get(f"{GMAIL_API}/messages/{ref['id']}", {
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date"],
        })
        hdrs = {h.get("name", "").lower(): h.get("value", "")
                for h in ((m.get("payload") or {}).get("headers") or []) if isinstance(h, dict)}
        ms = int(m.get("internalDate") or 0)
        at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None
        out.append({
            "id": str(m.get("id") or ref["id"]),
            "thread_id": str(m.get("threadId") or ref.get("threadId") or ""),
            "from": hdrs.get("from", ""),
            "to": hdrs.get("to", ""),
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""),
            "at": at.isoformat() if at else None,
            "snippet": str(m.get("snippet") or "")[:300],
            "unread": "UNREAD" in (m.get("labelIds") or []),
        })
        if out[-1]["thread_id"]:
            seen_threads.add(out[-1]["thread_id"])
    out.sort(key=lambda r: r["at"] or "", reverse=True)
    return out


def calendar_events(acct: Account, *, start: datetime, end: datetime,
                    max_results: int = 50) -> list[dict[str, Any]]:
    """Events on the primary calendar between `start` and `end`, in order, with the
    owner's role and the other attendees' addresses already worked out."""
    items: list[dict[str, Any]] = []
    token: str | None = None
    while len(items) < max_results:
        params: dict[str, Any] = {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "singleEvents": "true", "orderBy": "startTime",
            "maxResults": min(50, max_results - len(items)),
        }
        if token:
            params["pageToken"] = token
        page = acct.get(f"{CALENDAR_API}/calendars/primary/events", params)
        items.extend(e for e in (page.get("items") or []) if isinstance(e, dict))
        token = page.get("nextPageToken")
        if not token:
            break
    out = []
    for e in items[:max_results]:
        if e.get("status") == "cancelled":
            continue
        st, en = e.get("start") or {}, e.get("end") or {}
        all_day = "date" in st and "dateTime" not in st
        attendees = [a for a in (e.get("attendees") or []) if isinstance(a, dict)]
        me = next((a for a in attendees if a.get("self")), None)
        organizer = e.get("organizer") or {}
        if organizer.get("self"):
            role: str | None = "organizer"
        elif me is not None:
            role = "optional" if me.get("optional") else "required"
        else:
            role = None
        others = [str(a.get("email")) for a in attendees
                  if a.get("email") and not a.get("self") and not a.get("resource")]
        out.append({
            "title": str(e.get("summary") or "(no title)"),
            "start": st.get("dateTime") or st.get("date"),
            "end": en.get("dateTime") or en.get("date"),
            "all_day": all_day,
            "role": role,
            "who": others,
        })
    return out


def today_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Local midnight to the next one, timezone-aware, for the calendar read."""
    now = now or datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
