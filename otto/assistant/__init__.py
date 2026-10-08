"""The assistant half of Otto, behind one switch.

Otto's README describes an orchestrator: a board, a scheduler, the Claude Code
hooks, a cost ledger, and dispatch into terminal panes. Roughly half of the package
is something else, the owner's personal assistant: messaging colleagues on a hold
timer, drafting public posts, per-person dossiers and meeting prep, energy
tracking, a Slack help-channel watcher and DM inbox, meeting notes turned into
cards. Both halves share the daemon, the store and the tick loop, which is why a
newcomer could not tell from the import graph which modules were live for them.

This package is the boundary. `OTTO_SCOPE=core` (the default) loads, registers
and ticks the orchestrator only; `OTTO_SCOPE=assistant` adds everything named in
MODULES. The modules themselves stay where they are: moving eight files would churn
every import and test for no gain, and a gate on what the daemon imports, mounts
and runs is the thing that makes the claim true. With the core scope none of
MODULES is imported by `otto.daemon` or `otto.cli`, none of their routes exist on
the app, and none of their subcommands are in `otto --help` (tests/test_scope.py
holds that line).

Why a profile switch and not pip extras. Extras gate dependencies, and the
assistant modules have none the core lacks: every one of them talks to the world
through a spawned `claude` session or the Slack bot token the core already uses.
There is nothing to install or leave out, only something to load or leave off.

Surface the daemon uses (both files import the assistant modules at module level,
so the daemon imports them only under the assistant scope):

  routes.install(app, store)   mounts the assistant's API routes on the daemon app
  hooks.install(store, ...)    binds the tick hooks to the daemon's store
  hooks.owns(run) / harvest    the finished-run harvesters (meeting ingest, writing)
  hooks.autostart(...)         the auto-launchable assistant runners
  hooks.prep / settle / ingest the three places the tick loop calls out
  STATE_EMPTY                  what /api/state carries for the assistant keys when
                               the profile is core, so the dashboard's reads stay
                               defined without the modules being loaded
"""

from __future__ import annotations

from .. import config

MODULES: tuple[str, ...] = (
    "outreach", "writing", "people", "prep", "wellbeing", "summon", "inbox", "meetings",
)

# The /api/state keys the assistant fills, in their empty shape. app.js reads
# `s.outreach.items`, `s.outreach.summary` and `s.writing.counts` defensively, but
# a key that exists with no rows is a clearer contract than one that comes and goes.
STATE_EMPTY: dict[str, dict] = {
    "outreach": {"summary": {"enabled": False, "held": 0, "sent_24h": 0, "counts": {}},
                 "items": []},
    "writing": {"counts": {}, "running": 0},
}

# CLI subcommands that exist only under the assistant scope. Listed here, next to
# MODULES, so the parser and the profile test read the same line.
COMMANDS: tuple[str, ...] = (
    "dm", "outreach", "checkin", "patterns", "prep", "people", "threads",
    "thread-note", "thread-update", "meetings", "writing",
)

# Schedule runners the assistant owns. A schedule with one of these runners is
# seeded and autostarted only under the assistant scope.
RUNNERS: tuple[str, ...] = ("ingest", "writing")


def enabled() -> bool:
    return config.ASSISTANT
