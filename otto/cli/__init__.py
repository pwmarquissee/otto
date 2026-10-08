"""The `otto` command line.

Reads work with the daemon down (state files are safe to read concurrently);
writes require it, and say so plainly rather than failing obscurely.

The package: one module per verb group, each holding its cmd_ functions and the
`add_<verb>(sub)` that defines that verb's parser, collected in the module's
PARSERS. This file owns the parser assembly and the verb ORDER, which is the
flat file's order kept so `otto --help` reads as it always has; the assistant
verbs are added last, only under OTTO_SCOPE=assistant (see otto/assistant).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from .. import config
from ..client import Client, DaemonDown
from . import (assistant, board, checkin, converse, decisions, definitions, google, insight,
               posts, runs, schedules, service, sessions, slack, spend, triage)
from ._fmt import C_RED, _c
from ._text import DetailError, _resolve_detail  # noqa: F401  # re-exported: tests and tools import them from otto.cli

_MODULES = (insight, spend, runs, schedules, board, triage, decisions, sessions, slack,
            converse, google, definitions, service, assistant, checkin, posts)

# The verb order of `otto --help`: the order the flat cli.py registered them in.
ORDER = ['status', 'reply', 'post', 'tell', 'priorities', 'spend', 'ledger', 'telemetry', 'runs', 'logs', 'spawn', 'kill', 'done', 'registry', 'scan', 'schedules', 'due', 'schedule', 'agenda', 'notices', 'notify', 'day', 'machine', 'stamp', 'toggle', 'probe', 'events', 'board', 'task', 'triage', 'chat', 'propose', 'launch', 'autorun', 'sessions', 'dispatch', 'app', 'herdr', 'live', 'watch', 'autodispatch', 'next', 'gaps', 'manifest', 'identity', 'feeds', 'decide', 'decisions', 'decision', 'ack', 'known', 'retire', 'skills', 'refresh', 'google', 'config', 'serve', 'prune', 'ensure', 'restart', 'setup', 'stop', 'doctor', 'say']

# Added after ORDER, and only under the assistant scope. The scope test checks
# `otto --help` against assistant.COMMANDS, so this list and that one agree.
ASSISTANT_ORDER = ['dm', 'outreach', 'checkin', 'patterns', 'prep', 'people', 'threads', 'thread-note', 'thread-update', 'meetings', 'writing']


def _parsers() -> dict[str, Callable[[argparse._SubParsersAction], None]]:
    out: dict[str, Callable] = {}
    for mod in _MODULES:
        for verb, add in mod.PARSERS.items():
            if verb in out:
                raise RuntimeError(f"verb {verb!r} is defined in two cli modules")
            out[verb] = add
    missing = [v for v in ORDER + ASSISTANT_ORDER if v not in out]
    extra = [v for v in out if v not in ORDER and v not in ASSISTANT_ORDER]
    if missing or extra:
        raise RuntimeError(f"cli verbs out of step: missing {missing}, unordered {extra}")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="otto",
        description=f"{config.PERSONA_NAME} - {config.PERSONA_BLURB}",
    )
    p.add_argument("--url", default=None, help="daemon base URL")
    sub = p.add_subparsers(dest="cmd", required=True)
    adders = _parsers()
    for verb in ORDER:
        adders[verb](sub)
    if config.ASSISTANT:
        for verb in ASSISTANT_ORDER:
            adders[verb](sub)
    return p


def main(argv: list[str] | None = None) -> int:
    config.utf8_output()
    args = build_parser().parse_args(argv)
    client = Client(args.url)
    try:
        return args.fn(args, client)
    except DaemonDown as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    except DetailError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(_c(f"  {e}", C_RED), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
