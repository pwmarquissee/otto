# Otto

A local daemon and dashboard for running a lot of Claude Code sessions at once.

It keeps a board of work, runs Claude Code on a schedule, tracks every live session
on the machine through Claude Code's hooks, and can hand a card to an idle session.
One person, one machine, nothing leaves loopback.

![Dispatch](docs/screenshots/dispatch.png)

## What you get

- **Board.** Backlog, Queued, In progress, Needs you, Blocked, Done. A card in Queued
  gets run as a headless Claude Code session in its own directory.
- **Schedules.** Cron-style runs with staleness alarms, all disarmed until you say so.
- **Sessions.** Six hooks report every Claude Code session's state: busy, waiting on
  you, idle, gone.
- **Dispatch.** With [herdr](https://herdr.dev) installed, your sessions appear as
  live terminals in the dashboard and Otto suggests which card fits which idle pane.
- **Today.** What needs you, what is running, what the schedules did overnight.

More screenshots: [Setup](docs/screenshots/setup.png), [Today](docs/screenshots/today.png),
[Board](docs/screenshots/board.png), [Terminal](docs/screenshots/terminal.png),
[Grid](docs/screenshots/grid.png).

## Install

Python 3.13. Windows is the only platform it has run on so far.

```
pip install -e .
otto serve
```

Open http://127.0.0.1:8787. A fresh daemon opens on a setup checklist: your name and
project folders, the Claude Code hooks, herdr if you want it, which services to
probe, a first card, which schedules to arm. Each step can be skipped. The same
thing in a terminal is `otto setup`.

Settings land in `~/.claude/otto/otto.env`. Environment variables override the file.
`.env.example` lists everything. Nothing talks to an outside service unless you
configure it.

What you get by default is the orchestrator. `OTTO_SCOPE=assistant` in `otto.env`
adds the assistant half (outreach, writing, people, prep, wellbeing, summon, inbox,
meetings); under the default `core` scope none of it is loaded. See
[docs/how-it-works.md](docs/how-it-works.md#scopes).

`pip install -e .` puts `otto` on PATH; `python -m otto` is the same thing.
`requirements.lock` is the pinned set CI installs from. To keep the daemon alive
across reboots on Windows, run `scripts/Install-OttoDaemon.ps1`.

## Using it

```
otto status                 # what needs you
otto task add "..."         # a card
otto task mv <id> queued    # run it
otto schedules              # what runs when
otto sessions               # live Claude Code sessions
otto dispatch               # card to pane suggestions
otto setup --status         # is this install configured
```

Every verb has `--help`. The dashboard's command palette (Ctrl+K) lists them too,
and each click that changes something prints the equivalent command in the status
bar.

## Unattended runs

Otto can run Claude Code without you watching, so the limits that matter are
enforced in Python, not in prompt text: cards go through a promotion gate with
tiers, autorun has a master switch, a budget per run, one unattended schedule at a
time, and a breaker that disarms a schedule after three failures. Inside a session
the only control is a `PreToolUse` hook that pattern-matches tool input and puts
destructive-looking commands behind a desktop dialog. That hook is a seatbelt that
catches mistakes, not a sandbox; [SECURITY.md](SECURITY.md) lists the ways around
it. Every session runs at a level recorded on the run: `plan` (read-only, writes a
proposal; what a PREPARE run gets) or `yolo` (`--dangerously-skip-permissions`;
implementation and the loops that act). Nothing elevates on its own: `otto task
yolo <id>` is the one-word approval, and `otto task run --plan|--yolo` picks a level
for one run. Details in [docs/how-it-works.md](docs/how-it-works.md).

## Limits

- Lived on Windows. The daemon and the per-run launcher it writes have a POSIX
  backend and CI gates on Linux as well as Windows, but toasts, the scheduled-task
  installer, and the desktop shell are Windows code and nothing has run a real
  Claude Code session from Otto on Linux yet.
- The desktop shell (`desktop/`, Tauri) is Windows only and not built in CI.
- Written against herdr 0.9.3.
- Slack, mail, calendar, meeting notes, and the writing tools need MCP connectors
  you configure yourself. Without them they report unconfigured and stay quiet.

## Tests

```
pip install -e .[dev]
python -m pytest
python scripts/oss_scan.py
```

The suite runs against a temporary state directory and never writes to yours. It
reads the machine's process list the way `otto status` does, so a live Claude Code
session on the box can show up in a test's view of the world.
`oss_scan.py` is the release guard; it fails on anything that looks private.

## Credits

The dispatch model (a rail of sessions, suggested pairings, approve to send the
prompt) and the Windows hook details come from [keitora](https://github.com/rudoi/keitora).
The terminal harness is [herdr](https://herdr.dev); Otto never touches a PTY itself.

## License

MIT.
