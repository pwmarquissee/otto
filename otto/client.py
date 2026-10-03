"""HTTP client for the daemon.

Every mutation goes through here. Reads fall back to the state files on disk when
the daemon is down, so `otto status` still tells you something useful (including
that the daemon is down) instead of just erroring out.
"""

from __future__ import annotations

from typing import Any

import requests

from . import config


class DaemonDown(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str | None = None, timeout: int = 20) -> None:
        self.base = (base_url or config.BASE_URL).rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = requests.request(method, self._url(path), timeout=self.timeout, **kw)
        except requests.exceptions.RequestException as e:
            raise DaemonDown(
                f"{config.PERSONA_NAME} daemon not reachable at {self.base} "
                f"- start it with `otto serve`"
            ) from e
        if r.status_code >= 400:
            detail = r.text
            try:
                detail = r.json().get("detail", detail)
            except ValueError:
                pass
            raise RuntimeError(f"HTTP {r.status_code}: {detail}")
        if r.headers.get("content-type", "").startswith("text/"):
            return r.text
        return r.json()

    def alive(self) -> bool:
        try:
            self._request("GET", "/api/health")
            return True
        except (DaemonDown, RuntimeError):
            return False

    # ---- reads --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    # ---- setup (otto/setup.py) ----------------------------------------------

    def setup_view(self) -> dict[str, Any]:
        return self._request("GET", "/api/setup")

    def setup_post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One of the /api/setup/* actions, or /api/daemon/restart."""
        return self._request("POST", path, json=body or {})

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/api/state")

    def runs(self, limit: int = 80) -> list[dict[str, Any]]:
        return self._request("GET", "/api/runs", params={"limit": limit})

    def run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/runs/{run_id}")

    def slack_reply(self, channel: str, thread_ts: str, text: str) -> dict[str, Any]:
        return self._request("POST", "/api/slack/reply",
                             json={"channel": channel, "thread_ts": thread_ts,
                                   "text": text})

    def slack_post(self, channel: str, text: str) -> dict[str, Any]:
        return self._request("POST", "/api/slack/post",
                             json={"channel": channel, "text": text})

    def slack_tell(self, text: str) -> dict[str, Any]:
        return self._request("POST", "/api/slack/tell", json={"text": text})

    def slack_dm(self, to: list[str], text: str, why: str = "",
                 run_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/slack/dm",
                             json={"to": to, "text": text, "why": why,
                                   "run_id": run_id, "task_id": task_id})

    def spend(self, days: int = 7, domain: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"days": days}
        if domain:
            params["domain"] = domain
        return self._request("GET", "/api/spend", params=params)

    def ledger(self, days: int = 30, who: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"days": days}
        if who:
            params["who"] = who
        return self._request("GET", "/api/ledger", params=params)

    def ledger_session(self, sid: str) -> dict[str, Any]:
        return self._request("GET", f"/api/ledger/sessions/{sid}")

    def telemetry(self) -> dict[str, Any]:
        return self._request("GET", "/api/telemetry")

    def log(self, run_id: str, lines: int = 200) -> str:
        return self._request("GET", f"/api/runs/{run_id}/log", params={"lines": lines})

    def registry(self, kind: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("kind", kind), ("domain", domain)) if v}
        return self._request("GET", "/api/registry", params=params or None)

    def board(self, domain: str | None = None) -> dict[str, Any]:
        params = {"domain": domain} if domain else None
        return self._request("GET", "/api/board", params=params)

    def tasks(self, domain: str | None = None) -> list[dict[str, Any]]:
        params = {"domain": domain} if domain else None
        return self._request("GET", "/api/tasks", params=params)

    def add_task(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/api/tasks", json=payload)

    def patch_task(self, task_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/api/tasks/{task_id}", json=payload)

    def patch_tasks(self, ids: list[str], **payload: Any) -> dict[str, Any]:
        """One change to many cards: one lock, one write on the daemon side."""
        return self._request("POST", "/api/tasks/batch", json={"ids": ids, "patch": payload})

    def dedupe_tasks(self, dry_run: bool = False) -> dict[str, Any]:
        """Fold duplicate open cards into one, or list what would be folded."""
        return self._request("POST", "/api/tasks/dedupe", json={"dry_run": dry_run})

    def reply_task(self, task_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/reply", json={"text": text})

    def delete_task(self, task_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/tasks/{task_id}")

    def chat_thread(self) -> dict[str, Any]:
        return self._request("GET", "/api/chat")

    def chat_send(self, message: str) -> dict[str, Any]:
        return self._request("POST", "/api/chat", json={"message": message})

    def chat_reset(self) -> dict[str, Any]:
        return self._request("DELETE", "/api/chat")

    def snapshots(self) -> dict[str, Any]:
        return self._request("GET", "/api/snapshots")

    def propose(self, tasks: list[dict[str, Any]], origin: str = "agent",
                run_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/tasks/propose",
                             json={"tasks": tasks, "origin": origin, "run_id": run_id})

    def activity(self, run_id: str, limit: int = 200) -> dict[str, Any]:
        return self._request("GET", f"/api/runs/{run_id}/activity",
                             params={"limit": limit})

    def live(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/live")

    # ---- sessions -----------------------------------------------------------
    # Read-only from here. The hook posts state (scripts/otto_hook.py, which does
    # not use this client), and installing the hooks is a local file write the CLI
    # does directly, so it still works with the daemon down.

    def sessions(self, domain: str | None = None, live: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"live": str(live).lower()}
        if domain:
            params["domain"] = domain
        return self._request("GET", "/api/sessions", params=params)

    def sessions_doctor(self) -> dict[str, Any]:
        return self._request("GET", "/api/sessions/doctor")

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        return self._request("PATCH", f"/api/sessions/{session_id}", json={"title": title})

    def open_session(self, session_id: str) -> dict[str, Any]:
        """Focus the live session's window, or resume an ended one in a new tab."""
        return self._request("POST", f"/api/sessions/{session_id}/open")

    # ---- logistics dispatch and herdr --------------------------------------------

    def logistics(self) -> dict[str, Any]:
        return self._request("GET", "/api/logistics")

    def logistics_refresh(self) -> dict[str, Any]:
        return self._request("POST", "/api/logistics/refresh")

    def approve_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/logistics/proposals/{proposal_id}/approve")

    def dismiss_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/logistics/proposals/{proposal_id}/dismiss")

    def dispatch_to(self, task_id: str, target: str) -> dict[str, Any]:
        return self._request("POST", "/api/logistics/dispatch",
                             json={"task_id": task_id, "target": target})

    def herdr(self) -> dict[str, Any]:
        return self._request("GET", "/api/herdr")

    def herdr_ensure(self) -> dict[str, Any]:
        return self._request("POST", "/api/herdr/ensure")

    def herdr_open(self, cwd: str, label: str | None = None, name: str | None = None,
                   resume: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/herdr/open",
                             json={"cwd": cwd, "label": label, "name": name, "resume": resume})

    def herdr_focus(self, target: str) -> dict[str, Any]:
        return self._request("POST", f"/api/herdr/focus/{target}")

    def herdr_worktrees(self, cwd: str) -> dict[str, Any]:
        return self._request("GET", "/api/herdr/worktrees", params={"cwd": cwd})

    def herdr_worktree_create(self, cwd: str, branch: str, base: str | None = None,
                              label: str | None = None, name: str | None = None,
                              start_claude: bool = True) -> dict[str, Any]:
        return self._request("POST", "/api/herdr/worktree/create",
                             json={"cwd": cwd, "branch": branch, "base": base, "label": label,
                                   "name": name, "start_claude": start_claude})

    def herdr_worktree_open(self, cwd: str, branch: str | None = None, path: str | None = None,
                            label: str | None = None, name: str | None = None,
                            start_claude: bool = True) -> dict[str, Any]:
        return self._request("POST", "/api/herdr/worktree/open",
                             json={"cwd": cwd, "branch": branch, "path": path, "label": label,
                                   "name": name, "start_claude": start_claude})

    def repos(self) -> dict[str, Any]:
        return self._request("GET", "/api/repos")

    # ---- dossier threads ----------------------------------------------------

    def threads(self, band: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/api/threads",
                             params={"band": band} if band else None)

    def thread_note(self, thread_id: str, note: str) -> dict[str, Any]:
        return self._request("POST", "/api/threads/note",
                             json={"thread_id": thread_id, "note": note})

    def resolve_thread(self, thread_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"/api/threads/{thread_id}/resolve",
                             json={"text": text})

    def add_person_note(self, slug: str, section: str, note: str) -> dict[str, Any]:
        return self._request("PATCH", f"/api/people/{slug}",
                             json={"section": section, "note": note})

    def arm_schedule(self, name: str, armed: bool = True) -> dict[str, Any]:
        return self._request("POST", f"/api/schedules/{name}/arm",
                             params={"armed": str(armed).lower()})

    def autorun(self) -> dict[str, Any]:
        return self._request("GET", "/api/autorun")

    def set_autorun(self, enabled: bool) -> dict[str, Any]:
        return self._request("POST", "/api/autorun",
                             params={"enabled": str(enabled).lower()})

    def run_schedule(self, name: str) -> dict[str, Any]:
        return self._request("POST", f"/api/schedules/{name}/run")

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{task_id}")

    def dispatch_task(self, task_id: str, force: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/dispatch",
                             params={"force": str(force).lower()})

    def dispatch_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/dispatch")

    def set_dispatch(self, enabled: bool) -> dict[str, Any]:
        return self._request("POST", "/api/dispatch",
                             params={"enabled": str(enabled).lower()})

    def briefing(self, domain: str | None = None) -> dict[str, Any]:
        params = {"domain": domain} if domain else None
        return self._request("GET", "/api/briefing", params=params)

    def gaps(self, domain: str | None = None) -> list[dict[str, Any]]:
        params = {"domain": domain} if domain else None
        return self._request("GET", "/api/gaps", params=params)

    def refresh(self, domain: str = "all") -> dict[str, Any]:
        """Returns {"runs": [...], "skipped": [...]}. `all` fans out per domain."""
        return self._request("POST", "/api/refresh", params={"domain": domain})

    def meetings(self) -> dict[str, Any]:
        """The ingester's ledger: what it has read and what came out of it."""
        return self._request("GET", "/api/meetings")

    def ingest_meetings(self) -> dict[str, Any]:
        """Read new Notion meeting notes now. Returns {"runs": ...} on success."""
        return self._request("POST", "/api/meetings/ingest")

    def day(self, day: str, refresh: bool = False) -> dict[str, Any]:
        return self._request("GET", f"/api/day/{day}", params={"refresh": str(refresh).lower()})

    def checkin(self, day: str, note: str | None = None,
                energy: int | None = None,
                signals: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("PUT", f"/api/day/{day}",
                             json={"note": note, "energy": energy,
                                   "signals": signals or {}})

    def patterns(self, days: int = 90) -> dict[str, Any]:
        return self._request("GET", "/api/patterns", params={"days": days})

    def notices(self, unread_only: bool = False) -> list[dict[str, Any]]:
        return self._request("GET", "/api/notices",
                             params={"unread_only": str(unread_only).lower()})

    def notify(self, title: str, **kw: Any) -> dict[str, Any]:
        return self._request("POST", "/api/notices", json={"title": title, **kw})

    def read_notice(self, notice_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/notices/{notice_id}/read")

    # ---- outreach -----------------------------------------------------------
    # No "send this now, skip the hold" compose path on purpose. Sending inline would
    # make the hold a thing a caller can opt out of, and a caller that can opt out of
    # the interlock is a caller with no interlock. `send` acts on an existing held
    # record, which means the owner saw it.

    def outreach(self, state: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/api/outreach",
                             params={"state": state} if state else None)

    def compose_outreach(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/api/outreach", json=payload)

    def kill_outreach(self, oid: str) -> dict[str, Any]:
        return self._request("POST", f"/api/outreach/{oid}/kill")

    def send_outreach(self, oid: str) -> dict[str, Any]:
        return self._request("POST", f"/api/outreach/{oid}/send")

    def extend_outreach(self, oid: str, minutes: int = 10) -> dict[str, Any]:
        return self._request("POST", f"/api/outreach/{oid}/extend",
                             params={"minutes": minutes})

    # ---- known failures -----------------------------------------------------

    def known(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/known")

    def mark_known(self, kind: str, name: str, reason: str) -> dict[str, Any]:
        return self._request("PUT", f"/api/known/{kind}/{name}", json={"reason": reason})

    def clear_known(self, kind: str, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/known/{kind}/{name}")

    # ---- decisions ----------------------------------------------------------
    # No edit method and no delete method, matching the store: superseding is the
    # only way to change a decision, and it is a write of a NEW one.

    def decisions(self, domain: str | None = None, include_superseded: bool = False,
                  q: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"include_superseded": str(include_superseded).lower()}
        if domain:
            params["domain"] = domain
        if q:
            params["q"] = q
        return self._request("GET", "/api/decisions", params=params)

    def decision(self, decision_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/decisions/{decision_id}")

    def add_decision(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/api/decisions", json=payload)

    def set_revisit_by(self, decision_id: str, when: str | None) -> dict[str, Any]:
        return self._request("PATCH", f"/api/decisions/{decision_id}/revisit-by",
                             params={"when": when} if when else None)

    def supersede_decision(self, decision_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("POST", f"/api/decisions/{decision_id}/supersede",
                             json=payload)

    # ---- run failures -------------------------------------------------------

    # ---- writing -----------------------------------------------------------

    def writing(self) -> dict[str, Any]:
        return self._request("GET", "/api/writing")

    def post(self, post_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/writing/{post_id}")

    def writing_ideas(self) -> dict[str, Any]:
        return self._request("POST", "/api/writing/ideas")

    def writing_draft(self, post_id: str, note: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/writing/{post_id}/draft", json={"note": note})

    def patch_post(self, post_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/api/writing/{post_id}", json=payload)

    def ack_run(self, run_id: str, undo: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/ack",
                             params={"undo": str(undo).lower()})

    def ack_all_runs(self, kind: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/runs/ack-all",
                             params={"kind": kind} if kind else None)

    def reclassify_runs(self) -> dict[str, Any]:
        return self._request("POST", "/api/runs/reclassify")

    def retire(self) -> dict[str, Any]:
        return self._request("GET", "/api/retire")

    def config_summary(self) -> dict[str, Any]:
        return self._request("GET", "/api/config")

    def machine(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/machine")

    def schedules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/schedules")

    def integrations(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/integrations")

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/api/events", params={"limit": limit})

    def alerts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/alerts")

    # ---- writes -------------------------------------------------------------

    def spawn(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/api/runs/spawn", json=payload)

    def prune_runs(self, older_than_hours: int = 24, dry_run: bool = True) -> dict[str, Any]:
        return self._request("POST", "/api/runs/prune",
                             params={"older_than_hours": older_than_hours,
                                     "dry_run": str(dry_run).lower()})

    def kill(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/kill")

    def done(self, run_id: str, status: str = "ok", exit_code: int | None = 0,
             notes: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/done",
                             json={"status": status, "exit_code": exit_code, "notes": notes})

    def scan(self) -> dict[str, Any]:
        return self._request("POST", "/api/registry/scan")

    def stamp(self, name: str, status: str = "ok", run_id: str | None = None,
              at: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/schedules/{name}/stamp",
                             json={"status": status, "run_id": run_id, "at": at})

    def toggle(self, name: str, enabled: bool) -> dict[str, Any]:
        return self._request("POST", f"/api/schedules/{name}/toggle",
                             params={"enabled": str(enabled).lower()})

    def probe(self) -> list[dict[str, Any]]:
        return self._request("POST", "/api/integrations/probe")

    def put_schedule(self, name: str, **payload: Any) -> dict[str, Any]:
        return self._request("PUT", f"/api/schedules/{name}",
                             json={"name": name, **payload})

    def delete_schedule(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/schedules/{name}")

    def put_snapshot(self, kind: str, summary: str | None = None,
                     items: list[dict[str, Any]] | None = None,
                     source: str = "claude-session",
                     domain: str = "work") -> dict[str, Any]:
        return self._request("PUT", f"/api/snapshots/{kind}",
                             json={"kind": kind, "domain": domain,
                                   "summary": summary,
                                   "items": items or [], "source": source})
