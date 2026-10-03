"""First-run setup: what a fresh install needs, computed from live state.

One engine, two faces. The dashboard's Setup view and `otto setup` both call
`view(store)` and act through the same functions below, so they can never
disagree about what is done. Nothing here is a wizard that remembers where you
were: every status is recomputed from the daemon, the filesystem, and the
settings file each time, which is what makes "done" mean done rather than
"clicked through".

Statuses:
  done      the condition holds in the running daemon
  restart   the settings file has the value, the running daemon does not yet
  todo      not done, not skipped
  skipped   the owner said not now (store setup.json records when)

The flow never blocks. A required step can be skipped too; required only
decides what the summary says and what the rail counts.

See docs/control-room/setup.md for the API shapes this backs.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, settings
from .store import Store

STEP_IDS = ("daemon", "identity", "hooks", "herdr", "integrations", "first_card",
            "schedules", "finish")
REQUIRED = {"daemon", "identity", "hooks"}

# Probes the integrations step offers. herdr has its own step; a probe that is
# not listed here has no setup story and stays governed by OTTO_INTEGRATIONS alone.
INTEGRATION_CHOICES = (
    ("anthropic", "Anthropic admin key",
     "Checks the admin API key cached by your anthropic-env script; shows org usage"),
    ("aws", "AWS SSO session",
     "Runs sts get-caller-identity on OTTO_AWS_PROFILE so an expired session is a card, not a surprise"),
    ("notion", "Notion (through MCP)",
     "Meeting notes are read from Notion; the probe only checks the MCP server is registered"),
)

DEFAULT_OWNER = "the owner"
DEFAULT_ORG = "your organization"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_complete(store: Store) -> bool:
    return bool(store.setup().get("completed_at"))


def migrate(store: Store) -> bool:
    """An install that predates the setup flow is not a first run. On the first
    start with this code, a store that already holds cards or runs and has no
    setup record gets marked complete, so the Setup view does not greet someone
    who configured Otto by hand months ago and the unconfigured-integration cards
    they are used to stay where they were. A fresh install has neither and goes
    through the flow. Returns True when it wrote."""
    if store.setup():
        return False
    if not (store.tasks() or store.runs()):
        return False
    store.put_setup({"completed_at": _now(), "migrated": True})
    store.log("setup: existing install marked complete (migrated); otto setup --reset to run the flow",
              source="setup")
    return True


def skipped(store: Store) -> dict[str, str]:
    data = store.setup().get("skipped") or {}
    return data if isinstance(data, dict) else {}


def restart_needed(store: Store) -> bool:
    """True once a settings write happened in this daemon's lifetime and no
    restart followed. Recorded against the daemon pid so a restart clears it by
    construction: the new daemon has a new pid."""
    data = store.setup()
    return bool(data.get("restart_pid")) and data.get("restart_pid") == os.getpid()


def _mark_restart(store: Store) -> None:
    store.put_setup({"restart_pid": os.getpid(), "settings_written_at": _now()})


def _file() -> dict[str, str]:
    return settings.read(config.SETTINGS_PATH)


def _paths_field(name: str, label: str, live: tuple[Path, ...], filed: str | None, hint: str) -> dict[str, Any]:
    value = filed if filed is not None else os.pathsep.join(str(p) for p in live)
    return {"name": name, "label": label, "kind": "paths",
            "value": "\n".join(x for x in value.split(os.pathsep) if x.strip()),
            "placeholder": str(Path.home() / "work"), "hint": hint}


def split_paths(raw: str) -> list[str]:
    """A paths field arrives newline-separated from the dashboard and
    pathsep-separated from the terminal; accept both."""
    out: list[str] = []
    for chunk in raw.replace("\r", "\n").split("\n"):
        for part in chunk.split(os.pathsep):
            part = part.strip().strip('"')
            if part:
                out.append(part)
    return out


def validate_identity(values: dict[str, Any]) -> dict[str, str | None]:
    """The identity form to settings-file writes. Roots must exist on disk:
    a root that does not resolve is a typo, and a typo here means every session
    under it is filed as unscoped forever with nothing saying why."""
    out: dict[str, str | None] = {}
    name = str(values.get("OTTO_OWNER_NAME", "")).strip()
    org = str(values.get("OTTO_ORG_NAME", "")).strip()
    if "OTTO_OWNER_NAME" in values:
        out["OTTO_OWNER_NAME"] = name or None
    if "OTTO_ORG_NAME" in values:
        out["OTTO_ORG_NAME"] = org or None
    for key in ("OTTO_WORK_ROOTS", "OTTO_PERSONAL_ROOTS"):
        if key not in values:
            continue
        roots = split_paths(str(values.get(key) or ""))
        bad = [r for r in roots if not Path(r).expanduser().is_dir()]
        if bad:
            raise ValueError(f"{key}: not a directory: {', '.join(bad)}")
        out[key] = os.pathsep.join(str(Path(r).expanduser()) for r in roots) or None
    return out


def write_settings(store: Store, values: dict[str, str | None]) -> dict[str, Any]:
    """Validate against .env.example when it is present, write, and flag the
    restart. Secrets masked by describe() must not round-trip: a masked value
    coming back from a form means "unchanged", not "set it to asterisks"."""
    known = settings.known_keys()
    clean: dict[str, str | None] = {}
    for key, value in values.items():
        if not settings.KEY_RE.match(key):
            raise ValueError(f"not an Otto setting: {key}")
        if known and key not in known:
            raise ValueError(f"unknown setting: {key} (not in .env.example)")
        if value == settings.MASK:
            continue
        clean[key] = None if value is None else str(value)
    written, removed = settings.write(clean, config.SETTINGS_PATH)
    if written or removed:
        _mark_restart(store)
        store.log(f"setup: wrote {', '.join(written) or 'nothing'}"
                  + (f", removed {', '.join(removed)}" if removed else "")
                  + f" to {config.SETTINGS_PATH.name}; restart to apply", source="setup")
    return {"written": written, "removed": removed, "restart_needed": True}


def skip(store: Store, step: str, undo: bool = False) -> None:
    if step not in STEP_IDS or step in ("daemon", "finish"):
        raise ValueError(f"cannot skip {step}")
    data = skipped(store)
    if undo:
        data.pop(step, None)
    else:
        data[step] = _now()
    store.put_setup({"skipped": data})


def complete(store: Store) -> None:
    store.put_setup({"completed_at": _now()})
    store.log("setup: marked complete", source="setup")


def reset(store: Store) -> None:
    store.put_setup({"completed_at": None, "skipped": {}})
    store.log("setup: reset, the Setup view opens again", source="setup")


# ---- steps -------------------------------------------------------------------

def _step(sid: str, title: str, status: str, summary: str, detail: str,
          command: str | None = None, action: dict[str, Any] | None = None,
          data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": sid, "title": title, "required": sid in REQUIRED, "status": status,
            "summary": summary, "detail": detail, "command": command,
            "action": action or {"kind": "none"}, "data": data or {}}


def _status(done: bool, pending: bool, skip_at: str | None) -> str:
    if done:
        return "done"
    if pending:
        return "restart"
    if skip_at:
        return "skipped"
    return "todo"


def step_daemon(store: Store) -> dict[str, Any]:
    keep = "scripts/Install-OttoDaemon.ps1" if os.name == "nt" else "a user service that runs `otto ensure`"
    return _step(
        "daemon", "Daemon", "done",
        f"Answering on {config.BASE_URL}; state in {config.OTTO_HOME}",
        "Otto is one process: the API, the dashboard, and a tick thread that runs "
        f"schedules and watches sessions. Nothing here leaves loopback. To have it come "
        f"back after a reboot or a crash, register {keep}.",
        command="otto status",
        data={"url": config.BASE_URL, "home": str(config.OTTO_HOME),
              "settings_path": str(config.SETTINGS_PATH), "pid": os.getpid()},
    )


def step_identity(store: Store) -> dict[str, Any]:
    filed = _file()
    live_ok = (config.OWNER_NAME != DEFAULT_OWNER
               and any(p.is_dir() for p in config.WORK_ROOTS + config.PERSONAL_ROOTS))
    filed_ok = bool(filed.get("OTTO_OWNER_NAME")) and bool(
        filed.get("OTTO_WORK_ROOTS") or filed.get("OTTO_PERSONAL_ROOTS"))
    status = _status(live_ok, filed_ok and not live_ok, skipped(store).get("identity"))
    roots = len([p for p in config.WORK_ROOTS + config.PERSONAL_ROOTS if p.is_dir()])
    summary = (f"{config.OWNER_NAME}, {roots} project root{'s' if roots != 1 else ''}"
               if live_ok else "No owner name or project roots yet")
    fields = [
        {"name": "OTTO_OWNER_NAME", "label": "Your name", "kind": "text",
         "value": filed.get("OTTO_OWNER_NAME", "" if config.OWNER_NAME == DEFAULT_OWNER else config.OWNER_NAME),
         "placeholder": "Alex Example",
         "hint": "How prompts refer to you, in the third person"},
        {"name": "OTTO_ORG_NAME", "label": "Organization", "kind": "text",
         "value": filed.get("OTTO_ORG_NAME", "" if config.ORG_NAME == DEFAULT_ORG else config.ORG_NAME),
         "placeholder": "Example Studio",
         "hint": "Used in prompts and the writing privacy scan; leave blank if there is none"},
        _paths_field("OTTO_WORK_ROOTS", "Work project roots", config.WORK_ROOTS,
                     filed.get("OTTO_WORK_ROOTS"),
                     "One directory per line. A session whose cwd is under one of these is work"),
        _paths_field("OTTO_PERSONAL_ROOTS", "Personal project roots", config.PERSONAL_ROOTS,
                     filed.get("OTTO_PERSONAL_ROOTS"),
                     "Same, for personal. The work/personal filter in the rail runs on these"),
    ]
    return _step(
        "identity", "Who and where", status, summary,
        "Otto sorts every session, card, and run into work or personal by the directory "
        "it runs in, and writes prompts that refer to you by name. Without roots, nothing "
        "has a domain and the filters do nothing.",
        command="otto setup",
        action={"kind": "form", "fields": fields, "submit": "Save"},
        data={"setting": True, "endpoint": "/api/setup/settings"},
    )


def step_hooks(store: Store) -> dict[str, Any]:
    from . import sessions
    installed = sessions.installed_in()
    claude = shutil.which("claude") is not None
    status = _status(installed, False, skipped(store).get("hooks"))
    if installed:
        summary = f"Installed in {sessions.GLOBAL_SETTINGS}"
    elif not claude:
        summary = "Claude Code is not on PATH; install it first, then come back"
    else:
        summary = "Not installed; live sessions are invisible until they are"
    return _step(
        "hooks", "Claude Code hooks", status, summary,
        "Six hooks merged into ~/.claude/settings.json under a marker tell Otto when a "
        "session starts, stops, waits on a permission prompt, or ends. That is how the "
        "rail, the dispatch control room, and the Today view know what is live. "
        "`otto sessions uninstall` removes exactly those entries and nothing else.",
        command="otto sessions install",
        action={"kind": "button", "label": "Install hooks"},
        data={"endpoint": "/api/setup/hooks/install", "claude_on_path": claude,
              "settings_file": str(sessions.GLOBAL_SETTINGS)},
    )


def step_herdr(store: Store) -> dict[str, Any]:
    from . import herdr
    avail = herdr.available()
    running = avail and herdr.server_running()
    status = _status(running, False, skipped(store).get("herdr"))
    if running:
        summary = "Installed and the server is answering"
    elif avail:
        summary = "Installed, server not running"
    else:
        summary = "Not installed (herdr.dev). Optional: sessions run fine outside it"
    action = ({"kind": "button", "label": "Start herdr server"} if avail and not running
              else {"kind": "none"})
    return _step(
        "herdr", "herdr terminal harness", status, summary,
        "herdr is a terminal multiplexer that owns the PTY of every pane. With it, the "
        "Dispatch, Terminal, and Grid views show your sessions live and Otto can hand a "
        "card to an idle pane. Without it, Otto still tracks sessions through the hooks; "
        "you just do not get the embedded terminals.",
        command="otto herdr up",
        action=action,
        data={"endpoint": "/api/setup/herdr/up", "installed": avail,
              "install_hint": "winget install herdr" if os.name == "nt" else "see herdr.dev"},
    )


def step_integrations(store: Store) -> dict[str, Any]:
    filed = _file()
    filed_set = "OTTO_INTEGRATIONS" in filed
    live_set = config.INTEGRATIONS_SET
    status = _status(live_set, filed_set and not live_set, skipped(store).get("integrations"))
    if live_set:
        on = config.INTEGRATIONS or ()
        summary = (f"Probing {', '.join(on)}" if on else "No integration probes")
    else:
        summary = "Every probe runs; unused ones report red forever"
    chosen = (set(x.strip().lower() for x in filed["OTTO_INTEGRATIONS"].split(","))
              if filed_set else set(config.INTEGRATIONS or [c[0] for c in INTEGRATION_CHOICES]))
    options = [{"value": v, "label": label, "hint": hint, "checked": v in chosen}
               for v, label, hint in INTEGRATION_CHOICES]
    return _step(
        "integrations", "Integrations to watch", status, summary,
        "Otto probes a few outside services every five minutes and files a card when "
        "one is down. Pick only the ones you use; a probe for a product you do not have "
        "is a red card that can never go green. You can change this later in otto.env.",
        command="otto setup",
        action={"kind": "choice", "multi": True, "options": options, "submit": "Save",
                "none_label": "None of these"},
        data={"setting": "OTTO_INTEGRATIONS", "endpoint": "/api/setup/settings"},
    )


def step_first_card(store: Store) -> dict[str, Any]:
    n = len([t for t in store.tasks() if t.status != "done"])
    total = len(store.tasks())
    status = _status(total > 0, False, skipped(store).get("first_card"))
    summary = (f"{total} card{'s' if total != 1 else ''} on the board, {n} open"
               if total else "The board is empty")
    return _step(
        "first_card", "Your first card", status, summary,
        "Cards are the unit of work. Backlog means not ready; Queued means Otto may run "
        "it in a headless Claude Code session when a schedule or you say so; Needs you "
        "is the column to read first every morning. Add one real thing you want done.",
        command='otto task add "Rotate the staging database password" --priority high',
        action={"kind": "form", "submit": "Add card", "fields": [
            {"name": "title", "label": "Title", "kind": "text", "value": "",
             "placeholder": "Rotate the staging database password", "hint": ""},
            {"name": "detail", "label": "Detail", "kind": "text", "value": "",
             "placeholder": "What done looks like, in a sentence or two", "hint": "Optional"},
        ]},
        data={"endpoint": "/api/setup/first-card"},
    )


def _armable(sched) -> bool:
    """The seeded schedules a fresh install can arm safely: the built-in runners
    whose only writes are to Otto's own state. Slash-command schedules need the
    command to exist in claude/commands first."""
    return sched.runner in ("refresh", "ingest", "writing", "report") or \
        str(sched.command).startswith("(built in)")


def step_schedules(store: Store) -> dict[str, Any]:
    scheds = store.schedules()
    armed = [s.name for s in scheds if s.autostart and s.enabled]
    status = _status(bool(armed), False, skipped(store).get("schedules"))
    summary = (f"{len(armed)} armed: {', '.join(armed[:4])}" if armed
               else f"{len(scheds)} seeded, none armed; nothing runs unattended")
    options = [{"value": s.name, "label": s.name, "hint": s.description or "",
                "checked": s.autostart, "cadence": _cadence_text(s)}
               for s in scheds if _armable(s)]
    return _step(
        "schedules", "Unattended schedules", status, summary,
        "A fresh daemon starts with every schedule disarmed. The ones offered here are "
        "the read-only class: they pull your calendar and mail through MCP, fold "
        "yesterday's transcripts into a day record, and mine post ideas. Each writes "
        "only to Otto's own board and ledger. Arm what you want; the rest wait for "
        "`otto schedule arm <name>`.",
        command="otto schedule arm inbox-sync",
        action={"kind": "choice", "multi": True, "options": options, "submit": "Arm selected"},
        data={"endpoint": "/api/setup/schedules"},
    )


def _cadence_text(s) -> str:
    c = s.cadence
    try:
        if c.kind == "daily":
            return f"daily at {c.at}"
        if c.kind == "weekly":
            return f"{','.join(c.days or [])} at {c.at}"
        if c.kind == "every":
            return f"every {c.hours}h"
    except AttributeError:
        pass
    return c.kind if c else ""


def step_finish(store: Store, steps: list[dict[str, Any]]) -> dict[str, Any]:
    data = store.setup()
    done_at = data.get("completed_at")
    todo = [s["title"] for s in steps if s["status"] == "todo"]
    if done_at:
        summary = f"Finished {done_at[:10]}"
    elif todo:
        summary = f"Still open: {', '.join(todo)}"
    else:
        summary = "Everything is done or skipped"
    return _step(
        "finish", "Finish", "done" if done_at else "todo", summary,
        "Finishing hides this view from the rail (it stays under Control plane, "
        "System) and lets unconfigured-integration cards onto the board again. "
        "Nothing you skipped is lost: run `otto setup` any time.",
        command="otto setup --status",
        action={"kind": "button", "label": "Finish setup"},
        data={"endpoint": "/api/setup/complete", "completed_at": done_at},
    )


def steps(store: Store) -> list[dict[str, Any]]:
    out = [step_daemon(store), step_identity(store), step_hooks(store), step_herdr(store),
           step_integrations(store), step_first_card(store), step_schedules(store)]
    out.append(step_finish(store, out))
    return out


def summary(store: Store) -> dict[str, Any]:
    """The slice /api/state carries: enough for the rail badge and the first-open
    decision, nothing that needs a filesystem walk the dashboard will not use."""
    data = store.setup()
    items = [s for s in steps(store) if s["id"] != "finish"]
    done = len([s for s in items if s["status"] in ("done", "skipped", "restart")])
    return {"complete": bool(data.get("completed_at")), "done": done, "total": len(items),
            "restart_needed": restart_needed(store)}


def view(store: Store) -> dict[str, Any]:
    data = store.setup()
    items = steps(store)
    countable = [s for s in items if s["id"] != "finish"]
    done = len([s for s in countable if s["status"] in ("done", "skipped", "restart")])
    return {
        "complete": bool(data.get("completed_at")),
        "completed_at": data.get("completed_at"),
        "restart_needed": restart_needed(store),
        "progress": {"done": done, "total": len(countable)},
        "settings": settings.describe(config.SETTINGS_PATH),
        "steps": items,
    }
