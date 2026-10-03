"""originguard: keep web pages the owner happens to visit out of the daemon.

THE PROBLEM. The daemon has no authentication by design and binds loopback, which
keeps other machines out and does nothing about the owner's own browser. Any page
open in any tab can send requests to http://127.0.0.1:8787:

  * WebSockets are not subject to CORS. Without a check, a page could open
    ws://127.0.0.1:8787/ws/term/otto in control mode and type into a live
    terminal running claude with --dangerously-skip-permissions.
  * "Simple" cross-origin POSTs (no preflight) still execute server side; the
    page just cannot read the answer. Every bodyless POST route (run a schedule,
    send a held outreach message, dispatch a card) is reachable that way. JSON
    routes are refused by FastAPI's content-type check on current versions, and
    were not on older ones that parsed a body with no Content-Type as JSON.
  * DNS rebinding turns a hostile hostname into 127.0.0.1 after the page loads,
    so its requests become same-origin and every route, reads included, opens up.

THE RULE. Two checks, on every HTTP request and WebSocket handshake:

  1. Host. The Host header's hostname must be a loopback name (or one the owner
     added). This is the rebinding defense: a rebound page still sends its own
     hostname. The port is not compared, because an SSH or SSM port forward
     legitimately serves the daemon on a different local port.
  2. Origin. A request that carries an Origin header came from a browser, and
     the origin must be one of the daemon's own (or the owner's additions). A
     request with NO Origin is a non-browser client (the hook, the CLI, curl, the
     desktop shell's health poll) and is allowed: those are local processes, and
     the daemon trusts local processes (SECURITY.md).

Both lists come from config so a non-default port, an OTTO_HOST, or a forwarded
port can be added without code. Refusals are 403 for HTTP and a close before
accept (an HTTP 403 on the wire) for WebSockets.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# 1008 is the WebSocket "policy violation" close code, the same one web_term
# uses for a refused target.
WS_POLICY = 1008


def default_origins(port: int, host: str | None = None) -> list[str]:
    """The daemon's own origins on this port. The desktop shell's window loads
    http://127.0.0.1:8787 directly (desktop/src-tauri/src/main.rs, BASE), so its
    origin is already in this list."""
    out = [f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"]
    if host and host not in LOOPBACK_HOSTS and host not in ("0.0.0.0", "::"):
        out.append(f"http://{_bracket(host)}:{port}")
    return out


def default_hosts(host: str | None = None) -> list[str]:
    out = list(LOOPBACK_HOSTS)
    if host and host not in out and host not in ("0.0.0.0", "::"):
        out.append(host.lower())
    return out


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def hostname_of(host_header: str) -> str:
    """'127.0.0.1:8787' -> '127.0.0.1', '[::1]:8787' -> '::1', 'Localhost' -> 'localhost'."""
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def _norm_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class OriginGuard:
    """Pure ASGI middleware, so it sees WebSocket handshakes as well as HTTP.
    (`@app.middleware("http")` never runs for a websocket scope.)"""

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str],
                 allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.origins = {_norm_origin(o) for o in allowed_origins if o.strip()}
        self.hosts = {h.strip().lower().strip("[]") for h in allowed_hosts if h.strip()}

    def refusal(self, headers: dict[str, str]) -> str | None:
        """Why this request is refused, or None to let it through."""
        host = headers.get("host")
        if host is not None and hostname_of(host) not in self.hosts:
            return f"host {host!r} is not an allowed daemon host"
        origin = headers.get("origin")
        if origin is not None and _norm_origin(origin) not in self.origins:
            # "null" lands here too (sandboxed iframes, file:// pages): present,
            # and not ours.
            return f"origin {origin!r} is not allowed to call this daemon"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            # A repeated Host or Origin is not something a browser sends; keeping
            # the first is fine because any copy that fails still fails below.
            headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        reason = self.refusal(headers)
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": WS_POLICY, "reason": reason[:120]})
            return
        body = json.dumps({"detail": reason}).encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})
