"""The daemon's route table, pinned.

Captured on 2026-10-07, before otto/daemon.py was split into otto/api/*: 114 HTTP
routes and 2 WebSocket routes under the core scope, plus the assistant's 22 under
OTTO_SCOPE=assistant (conftest runs the suite as assistant, so all of them are
expected here). A split or a later refactor that drops, renames or duplicates a
route fails this test by name rather than by a dashboard going quiet.
"""

from __future__ import annotations

from fastapi.routing import APIRoute, APIWebSocketRoute

from otto import config, daemon

CORE = {
    ("DELETE", "/api/chat"), ("DELETE", "/api/known/{kind}/{name}"),
    ("DELETE", "/api/notices/{notice_id}"), ("DELETE", "/api/schedules/{name}"),
    ("DELETE", "/api/snapshots/{domain}/{kind}"), ("DELETE", "/api/tasks/{task_id}"),
    ("GET", "/"), ("GET", "/api/alerts"), ("GET", "/api/autorun"), ("GET", "/api/board"),
    ("GET", "/api/briefing"), ("GET", "/api/chat"), ("GET", "/api/config"),
    ("GET", "/api/day/{day}"), ("GET", "/api/decisions"), ("GET", "/api/decisions/{decision_id}"),
    ("GET", "/api/dispatch"), ("GET", "/api/events"), ("GET", "/api/feeds"), ("GET", "/api/gaps"),
    ("GET", "/api/health"), ("GET", "/api/herdr"), ("GET", "/api/herdr/peek/{target}"),
    ("GET", "/api/herdr/worktrees"), ("GET", "/api/integrations"), ("GET", "/api/known"),
    ("GET", "/api/ledger"), ("GET", "/api/ledger/sessions/{sid}"), ("GET", "/api/live"),
    ("GET", "/api/logistics"), ("GET", "/api/machine"), ("GET", "/api/notices"),
    ("GET", "/api/registry"), ("GET", "/api/repos"), ("GET", "/api/retire"), ("GET", "/api/runs"),
    ("GET", "/api/runs/{run_id}"), ("GET", "/api/runs/{run_id}/activity"),
    ("GET", "/api/runs/{run_id}/log"), ("GET", "/api/schedules"), ("GET", "/api/sessions"),
    ("GET", "/api/sessions/doctor"), ("GET", "/api/setup"), ("GET", "/api/snapshots"),
    ("GET", "/api/spend"), ("GET", "/api/state"), ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"), ("GET", "/api/telemetry"), ("GET", "/api/term/panes"),
    ("PATCH", "/api/decisions/{decision_id}/revisit-by"), ("PATCH", "/api/sessions/{session_id}"),
    ("PATCH", "/api/tasks/{task_id}"), ("POST", "/api/autorun"), ("POST", "/api/chat"),
    ("POST", "/api/daemon/restart"), ("POST", "/api/decisions"),
    ("POST", "/api/decisions/{decision_id}/supersede"), ("POST", "/api/dispatch"),
    ("POST", "/api/feeds/init"), ("POST", "/api/herdr/ensure"),
    ("POST", "/api/herdr/focus/{target}"), ("POST", "/api/herdr/open"),
    ("POST", "/api/herdr/worktree/create"), ("POST", "/api/herdr/worktree/open"),
    ("POST", "/api/integrations/probe"), ("POST", "/api/logistics/dispatch"),
    ("POST", "/api/logistics/proposals/{proposal_id}/approve"),
    ("POST", "/api/logistics/proposals/{proposal_id}/dismiss"), ("POST", "/api/logistics/refresh"),
    ("POST", "/api/notices"), ("POST", "/api/notices/read-all"),
    ("POST", "/api/notices/{notice_id}/read"), ("POST", "/api/refresh"),
    ("POST", "/api/registry/scan"), ("POST", "/api/runs/ack-all"), ("POST", "/api/runs/prune"),
    ("POST", "/api/runs/reclassify"), ("POST", "/api/runs/spawn"), ("POST", "/api/runs/{run_id}/ack"),
    ("POST", "/api/runs/{run_id}/done"), ("POST", "/api/runs/{run_id}/kill"),
    ("POST", "/api/schedules/{name}/arm"), ("POST", "/api/schedules/{name}/run"),
    ("POST", "/api/schedules/{name}/stamp"), ("POST", "/api/schedules/{name}/toggle"),
    ("POST", "/api/sessions/{session_id}/open"), ("POST", "/api/sessions/{state}"),
    ("POST", "/api/setup/complete"), ("POST", "/api/setup/first-card"),
    ("POST", "/api/setup/herdr/up"), ("POST", "/api/setup/hooks/install"),
    ("POST", "/api/setup/reset"), ("POST", "/api/setup/schedules"), ("POST", "/api/setup/settings"),
    ("POST", "/api/setup/skip"), ("POST", "/api/setup/unskip"), ("POST", "/api/slack/post"),
    ("POST", "/api/slack/reply"), ("POST", "/api/slack/tell"), ("POST", "/api/tasks"),
    ("POST", "/api/tasks/batch"), ("POST", "/api/tasks/dedupe"), ("POST", "/api/tasks/propose"),
    ("POST", "/api/tasks/{task_id}/dispatch"), ("POST", "/api/tasks/{task_id}/reply"),
    ("POST", "/v1/logs"), ("POST", "/v1/metrics"), ("POST", "/v1/traces"),
    ("PUT", "/api/known/{kind}/{name}"), ("PUT", "/api/schedules/{name}"),
    ("PUT", "/api/snapshots/{kind}"),
    ("WS", "/ws/events"), ("WS", "/ws/term/{target}"),
}

ASSISTANT = {
    ("GET", "/api/meetings"), ("GET", "/api/outreach"), ("GET", "/api/patterns"),
    ("GET", "/api/people"), ("GET", "/api/people/meta/schema"), ("GET", "/api/people/{slug}"),
    ("GET", "/api/threads"), ("GET", "/api/writing"), ("GET", "/api/writing/{post_id}"),
    ("PATCH", "/api/people/{slug}"), ("PATCH", "/api/writing/{post_id}"),
    ("POST", "/api/meetings/ingest"), ("POST", "/api/outreach"),
    ("POST", "/api/outreach/{oid}/extend"), ("POST", "/api/outreach/{oid}/kill"),
    ("POST", "/api/outreach/{oid}/send"), ("POST", "/api/slack/dm"), ("POST", "/api/threads/note"),
    ("POST", "/api/threads/{thread_id}/resolve"), ("POST", "/api/writing/ideas"),
    ("POST", "/api/writing/{post_id}/draft"), ("PUT", "/api/day/{day}"),
}


def _table(routes) -> list[tuple[str, str]]:
    """Every (method, path), descending into included routers (FastAPI 0.139 keeps
    them as one wrapper holding `original_router`; older versions splice them in).
    A route is listed once per method it answers, so a duplicate registration
    shows up as a repeated pair."""
    out: list[tuple[str, str]] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out.extend(_table(inner.routes))
        elif isinstance(r, APIRoute):
            out.extend((m, r.path) for m in sorted(r.methods))
        elif isinstance(r, APIWebSocketRoute):
            out.append(("WS", r.path))
    return out


def test_every_route_is_still_there_and_none_is_doubled():
    assert config.ASSISTANT, "the suite runs as assistant so the whole table is expected"
    got = _table(daemon.app.routes)
    expected = CORE | ASSISTANT
    missing = sorted(expected - set(got))
    extra = sorted(set(got) - expected)
    doubled = sorted({p for p in got if got.count(p) > 1})
    assert not missing, f"routes gone: {missing}"
    assert not extra, f"routes nobody pinned: {extra} (add them here on purpose)"
    assert not doubled, f"routes registered twice: {doubled}"


def test_the_dashboard_and_static_files_are_served():
    """The `/` route and the /static mount are the dashboard; the mount is not an
    APIRoute, so it is checked by name."""
    assert ("GET", "/") in _table(daemon.app.routes)
    assert any(getattr(r, "name", None) == "static" for r in daemon.app.routes)
