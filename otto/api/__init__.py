"""The daemon's HTTP API, one router per area.

otto/daemon.py is the process: pidfile, tick loop, lifespan, the app, the state
cache, serve and stop. Everything a client calls lives here, split by what it is
about, each module an `APIRouter` the daemon includes once after `app` exists.

THE SEAM. Routers do not own a store. They read `daemon.store`, the state cache
and the tick helpers through the daemon module at call time (`from .. import
daemon as _d`, then `_d.store`), which is the same single writer the tick loop
uses and the one attribute a test swaps to run a route over a fresh store. The
assistant's routes (otto/assistant/routes.py) bind theirs through `install`
instead, because that package must stay importable without the daemon; that is
the one exception, and it predates this package.

Order matters only for the static install: its revalidate middleware goes on
last, outermost, where it always sat.
"""

from __future__ import annotations

from fastapi import FastAPI

from . import (chat, decisions, runs, schedules, sessions, setup, slack, state, static,
               system, tasks, telemetry)

ROUTERS = (state, runs, sessions, schedules, tasks, slack, setup, decisions, telemetry,
           chat, system)


def install(app: FastAPI) -> None:
    """Mount every router, then the dashboard. Called once by the daemon."""
    for mod in ROUTERS:
        app.include_router(mod.router)
    static.install(app)
