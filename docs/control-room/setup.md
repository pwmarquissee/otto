# First run: the setup flow

A fresh install has no owner name, no project roots, no hooks in Claude Code, and
every integration probe reports "not available". The Setup view opens by itself the
first time the dashboard loads on an unconfigured daemon and walks through eight steps
in order. The same engine runs in the terminal as `otto setup`.

## Where configuration lives

`otto/config.py` reads every `OTTO_*` variable from the environment at import time.
The daemon also reads one file first:

```
<OTTO_HOME>/otto.env        KEY=value lines, # comments, same format as .env.example
```

Precedence is environment, then the file, then the default in config.py. A value set
in the shell or a service definition always wins, so a test harness or a scratch
daemon is never overridden by the file. The dashboard and `otto setup` write the file.
Nothing else does. Only keys matching `^OTTO_[A-Z0-9_]+$` are read from it, so it
cannot set PATH or anything that is not Otto's.

A write reloads config in the running daemon (`config.reload()`), so everything read
at call time takes effect at once. The keys bound when the daemon is built, listed
in `config.RESTART_KEYS` (OTTO_HOME, OTTO_HOST, OTTO_PORT, OTTO_URL, the allowed
origins and hosts, OTTO_SCOPE, OTTO_TICK), take effect at the next start: a write
touching one of those reports `restart_needed: true`, the Setup view shows a banner
with a Restart button, and `otto setup` offers the same at the end. A reload drops
what the previous apply put into the environment before applying the file again, so
a removed or changed file value lands; a key the environment itself sets is never
touched.

## The steps

| id | required | done when | action |
| --- | --- | --- | --- |
| `daemon` | yes | always, if the API answers | none; shows URL, OTTO_HOME, keepalive hint |
| `identity` | yes | owner name is not the default and at least one root exists on disk | form: owner name, org name, work roots, personal roots |
| `hooks` | yes | `sessions.installed_in()` is true | button: install (merges into ~/.claude/settings.json) |
| `herdr` | no | herdr is installed and its server answers | button: start server; or skip |
| `integrations` | no | `OTTO_INTEGRATIONS` is set in the file or env, or skipped | choice: which probes to run (anthropic, aws, notion); `none` is a valid answer |
| `first_card` | no | at least one stored task exists | form: title, detail |
| `schedules` | no | any schedule is armed, or skipped | choice: arm seeded read-only schedules |
| `finish` | | `completed_at` is set | button: finish |

Statuses: `done`, `todo`, `skipped`, `restart` (the file has the value, the running
daemon does not yet: a RESTART_KEYS key, or a value the environment pins). Required steps can also be skipped. The flow never blocks, it
only records. While setup is incomplete, unconfigured-integration cards and alerts are
suppressed on the board and in Today. That information is in the Setup view.

## API

All routes are loopback and behind the same OriginGuard as the rest of the daemon.

```
GET  /api/setup
  { "complete": bool, "completed_at": iso|null, "restart_needed": bool,
    "progress": {"done": n, "total": n},
    "settings": {"path": str, "exists": bool, "values": {KEY: value}, "env_overrides": [KEY]},
    "steps": [ {"id", "title", "required": bool, "status", "summary", "detail",
                "command": str|null, "action": {...}|null, "data": {...}} ] }

  action.kind is one of:
    "form"    with "fields": [{"name","label","value","placeholder","hint","kind":"text|paths"}]
    "button"  with "label"
    "choice"  with "options": [{"value","label","hint","checked"}], "multi": true
    "none"

POST /api/setup/settings      {"values": {KEY: value|null}}   -> {"written": [KEY], "removed": [KEY], "live": [KEY], "restart": [KEY], "restart_needed": bool}
                              null removes a key; keys must be OTTO_* and, if .env.example is present, known there
POST /api/setup/hooks/install                                  -> {"changed": bool, "message": str}
POST /api/setup/herdr/up                                       -> {"ok": bool, "message": str}
POST /api/setup/first-card    {"title": str, "detail": str|null} -> the created task
POST /api/setup/schedules     {"arm": [name]}                   -> {"armed": [name]}
POST /api/setup/skip          {"step": id}                      -> GET /api/setup body
POST /api/setup/unskip        {"step": id}                      -> GET /api/setup body
POST /api/setup/complete                                        -> GET /api/setup body
POST /api/setup/reset                                           -> GET /api/setup body   (clears completed_at and skips; the file is untouched)
POST /api/daemon/restart                                        -> {"ok": true, "pid": old, "message": str}
```

`/api/state` carries only a summary:

```
"setup": {"complete": bool, "done": n, "total": n, "restart_needed": bool}
```

Secret-looking values (`TOKEN`, `SECRET`, `PASSWORD`, `_KEY`) come back masked in
`settings.values`. The file keeps the real value, and a masked value posted back is
not written.

## Restart

`POST /api/daemon/restart` spawns `python -m otto ensure --wait-pid <own pid> --quiet`
detached with a clean environment (`herdr.clean_env`, so NO_COLOR and CLAUDECODE from
a tool shell do not leak into the new daemon), clears the pidfile, and exits. The
helper waits for the old pid to be gone, then starts the daemon as the keepalive task
would. Spawned agents are not children of the helper and are re-adopted from
runs.json as on any restart. The dashboard polls `/api/health` until the pid changes,
then reloads the Setup view. `otto restart` is the terminal equivalent (stop, then
ensure).

## Terminal

`otto setup` prints the same steps with the same statuses and prompts for the ones
that are not done, one at a time, writing the same file. `otto setup --status` prints
without prompting and exits 0 when complete, 1 otherwise, so a provisioning script can
check it. `otto setup --reset` clears completion so the view opens again.

## Dashboard

- A fresh daemon (setup incomplete) opens on the Setup view instead of the cover card.
  The cover is still in the palette.
- While incomplete, the rail shows Setup at the top with `done/total`. When complete,
  the rail entry goes away and Setup is reachable from Control plane, System tab, and
  the palette.
- Each step is a card: status pill, one-line summary, why it matters, the action, the
  equivalent CLI command with a copy button, and Skip where the step is optional.
- A restart banner appears whenever `restart_needed` is true, with the button.
