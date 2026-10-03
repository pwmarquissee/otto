"""Runner adapters.

A runner knows how to start and/or observe one class of work:

  detached   spawned Claude Code sessions (the current blind spot)
  scheduled  cadence evaluation for loops and cron-style agents
  external   read-only status for systems Otto does not execute
"""

from . import detached, external, herdrpane, scheduled  # noqa: F401

__all__ = ["detached", "herdrpane", "scheduled", "external"]
