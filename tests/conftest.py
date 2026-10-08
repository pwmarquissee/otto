"""Shared fixtures, and the sandbox that keeps tests off real state.

READ THE ENV BLOCK BELOW BEFORE ADDING A TEST.

otto.config resolves OTTO_HOME, STATE_DIR and friends into module-level
constants AT IMPORT TIME. Setting OTTO_HOME from inside a test is therefore too
late: config has already been imported and is still pointing at
~/.claude/otto. So the environment is set here, at the top of conftest, before
the first `import otto` anywhere in the run. pytest loads conftest before any
test module, which is the only reason this ordering holds.

Belt and braces on top of that: `Store` takes an explicit state_dir and the
fixtures below always pass one. The env var is what protects a test that forgets.

OTTO_NO_TOAST is not optional. config.py records why: on 2026-08-07 a harness
with a redirected OTTO_HOME called notify.deliver_pending, which shelled out to
the real toast script and put "Otto: Fleet-wide outage" on the owner's actual screen,
then deleted its temp store so nothing in Otto explained it. Redirecting
OTTO_HOME sandboxes the store and nothing else.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# --- must precede every otto import -----------------------------------------
_SANDBOX = Path(tempfile.mkdtemp(prefix="otto-tests-"))
os.environ["OTTO_HOME"] = str(_SANDBOX)
os.environ["OTTO_NO_TOAST"] = "1"
# A port nothing real listens on, so a stray client call fails fast instead of
# reaching a live daemon.
os.environ["OTTO_PORT"] = "8799"
# The whole package, not the core scope: the suite exercises outreach, people,
# writing and the rest, and tests/test_scope.py checks the core cut from a
# subprocess of its own.
os.environ["OTTO_SCOPE"] = "assistant"

import pytest  # noqa: E402

from otto import config  # noqa: E402
from otto.models import Task  # noqa: E402
from otto.store import Store  # noqa: E402


def test_sandbox_is_active():
    """Guard test, not a fixture. If this fails, every other test is writing to
    the owner's real board and the run must stop."""
    assert config.OTTO_HOME == _SANDBOX
    assert config.STATE_DIR == _SANDBOX / "state"
    assert str(Path.home()) not in str(config.STATE_DIR.resolve())
    assert config.TOASTS_ENABLED is False


@pytest.fixture
def daemon(monkeypatch):
    """Claim the writer role for one test.

    store._write refuses unless config.is_daemon(). The real mark_daemon() also
    inspects a pidfile and can raise NotTheDaemon; tests want the flag, not the
    negotiation, so the module global is set directly and restored after.
    """
    monkeypatch.setattr(config, "_IS_DAEMON", True)
    yield
    # monkeypatch restores it, this is just the explicit statement of intent.


@pytest.fixture
def store(tmp_path, daemon):
    """A writable Store rooted in this test's own tmp_path."""
    return Store(tmp_path / "state")


@pytest.fixture
def reader_store(tmp_path):
    """A Store WITHOUT the writer role, for asserting that writes are refused."""
    return Store(tmp_path / "state")


def make_task(**kw) -> Task:
    """A valid Task with everything required filled in."""
    kw.setdefault("id", "a" * 32)
    kw.setdefault("title", "a task")
    return Task(**kw)
