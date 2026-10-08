"""OTTO_SCOPE holds the line between the orchestrator and the assistant.

The README describes an orchestrator. Under the core scope that is all the
daemon loads: none of the assistant modules are imported by `otto.daemon` or
`otto.cli`, none of their routes exist on the app, none of their subcommands are
in `otto --help`, and none of their schedules are seeded. Under the assistant
profile all of it is there. Each check runs in a subprocess, because config
resolves the profile at import time and this process already imported Otto under
the suite's own profile (conftest sets assistant).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from otto import assistant

_PROBE = r"""
import json, sys
import otto.daemon, otto.cli
from otto import assistant, config
from otto.runners import scheduled
loaded = sorted(m.split(".", 1)[1] for m in sys.modules
                if m.startswith("otto.") and m.split(".", 1)[1] in assistant.MODULES)
# An included router shows up on app.routes as one wrapper object in this FastAPI
# (0.139: `_IncludedRouter`, holding `original_router`); older ones splice the
# routes in directly. Walk both so the check does not depend on the version.
flat = []
for r in otto.daemon.app.routes:
    inner = getattr(r, "original_router", None)
    flat.extend(inner.routes if inner is not None else [r])
routes = sorted(r.path for r in flat if hasattr(r, "path"))
parser = otto.cli.build_parser()
sub = next(a for a in parser._actions if getattr(a, "choices", None) and "status" in a.choices)
print(json.dumps({
    "profile": config.SCOPE,
    "warning": config.SCOPE_WARNING,
    "loaded": loaded,
    "routes": routes,
    "commands": sorted(sub.choices),
    "runners": sorted({s.runner for s in scheduled.default_schedules()}),
}))
"""

ASSISTANT_ROUTES = ("/api/outreach", "/api/people", "/api/writing", "/api/meetings",
                    "/api/patterns", "/api/threads", "/api/slack/dm", "/api/day/{day}")


def _probe(profile: str) -> dict:
    env = {k: v for k, v in os.environ.items()}
    env["OTTO_SCOPE"] = profile
    env["OTTO_HOME"] = tempfile.mkdtemp(prefix=f"otto-profile-{profile}-")
    env["OTTO_NO_TOAST"] = "1"
    env["OTTO_PORT"] = "8799"
    out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                         env=env, cwd=str(Path(__file__).resolve().parent.parent),
                         timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_core_profile_loads_only_the_orchestrator():
    got = _probe("core")
    assert got["profile"] == "core" and got["warning"] is None
    assert got["loaded"] == [], f"assistant modules imported under core: {got['loaded']}"
    for prefix in ASSISTANT_ROUTES:
        hit = [r for r in got["routes"] if r == prefix or r.startswith(prefix.rstrip("}") + "/")]
        if prefix == "/api/day/{day}":
            # GET /api/day/{day} is the journal's (core); PUT is the check-in (assistant).
            # FastAPI lists the path once per route, so the core sees exactly one.
            assert got["routes"].count("/api/day/{day}") == 1
            continue
        assert not hit, f"{prefix} mounted under core: {hit}"
    for cmd in assistant.COMMANDS:
        assert cmd not in got["commands"], f"`otto {cmd}` offered under core"
    assert "status" in got["commands"] and "serve" in got["commands"]
    assert not set(got["runners"]) & set(assistant.RUNNERS)


def test_assistant_profile_loads_everything():
    got = _probe("assistant")
    assert got["profile"] == "assistant" and got["warning"] is None
    assert got["loaded"] == sorted(assistant.MODULES)
    for prefix in ("/api/outreach", "/api/people", "/api/writing", "/api/meetings",
                   "/api/patterns", "/api/threads", "/api/slack/dm"):
        assert any(r == prefix or r.startswith(prefix + "/") for r in got["routes"]), prefix
    assert got["routes"].count("/api/day/{day}") == 2
    for cmd in assistant.COMMANDS:
        assert cmd in got["commands"], f"`otto {cmd}` missing under assistant"
    assert set(assistant.RUNNERS) <= set(got["runners"])


def test_unknown_profile_clamps_to_core_and_says_so():
    got = _probe("Butler")
    assert got["profile"] == "core"
    assert got["warning"] and "butler" in got["warning"]
    assert got["loaded"] == []


def test_the_boundary_lists_match_the_code():
    """assistant.MODULES is the import-graph contract; every name must be a module."""
    pkg = Path(assistant.__file__).resolve().parent.parent
    for name in assistant.MODULES:
        assert (pkg / f"{name}.py").is_file(), name
