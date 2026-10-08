"""The one configuration file Otto writes: <OTTO_HOME>/otto.env.

config.py resolves every OTTO_* value from the environment (config.resolve, run
at import and again on config.reload). This module exists so a person can
configure Otto from the dashboard or from `otto setup` without editing a shell
profile or a service definition: the values land here, and resolve() pulls them
into the environment before it computes anything (settings.apply).

Precedence is environment, then this file, then the default in config.py. The
environment wins so a test harness, a scratch daemon, or a deployment that pins a
value is never overridden by a file it did not write. Only keys matching
^OTTO_[A-Z0-9_]+$ are read from the file: it lives in the owner's own OTTO_HOME and
is written by the daemon on loopback requests, and even so it must not be able to
set PATH or anything that is not Otto's.

A changed file takes effect on the next config.reload(), which setup.write_settings
runs right after writing: apply() remembers which keys it put into the environment
and drops them before applying again, so a changed or removed file value lands
and a key the environment itself sets is never touched. Keys in config.RESTART_KEYS
are bound when the daemon is built; a write to one of those reports
restart_needed, and the setup flow carries the restart.

No otto imports here. config.py imports this module, so importing config back
would be circular.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

FILE_NAME = "otto.env"
KEY_RE = re.compile(r"^OTTO_[A-Z0-9_]+$")
# Values whose key says they are a credential come back masked from describe().
# The file keeps the real value; only what leaves the daemon is masked.
SECRET_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|_KEY)(_|$)")
MASK = "********"
SECTION_HEADER = "# ---- set by otto setup --------------------------------------------------"

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def default_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the file lives. Mirrors config's OTTO_HOME resolution without
    importing config: OTTO_HOME is the one value that can only come from the
    environment, since it says where this file is."""
    home = (os.environ if environ is None else environ).get("OTTO_HOME")
    base = Path(home) if home else Path.home() / ".claude" / "otto"
    return base / FILE_NAME


def _unquote(raw: str) -> str:
    """One value as the file holds it: quotes stripped when they match, and an
    inline comment dropped from an unquoted value when whitespace precedes the
    hash (the .env.example style). A hash glued to the value is part of it."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == '"':
            # The writer escapes backslashes and double quotes inside a quoted
            # value; a single-quoted value is taken literally, shell style.
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    m = re.search(r"\s+#", s)
    if m:
        s = s[:m.start()].rstrip()
    return s


def _quote(value: str) -> str:
    """Written form. Quoted only when the plain form would not read back the
    same, so the file stays easy to edit by hand."""
    if value == "" or value != value.strip() or re.search(r"\s#|^#|[\"'\n]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def parse(text: str) -> dict[str, str]:
    """KEY=value lines to a dict. Unknown or non-OTTO keys are ignored, not
    rejected: a hand-edited file with a stray line should not take the daemon
    down at import."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if not KEY_RE.match(key):
            continue
        out[key] = _unquote(raw)
    return out


def read(path: Path | None = None) -> dict[str, str]:
    path = path or default_path()
    try:
        return parse(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


# What apply() last put into os.environ, key -> value, so the next apply can take
# it back out. Only a value still equal to what we set is removed: a key the
# process changed by hand since then is its own, not ours.
_APPLIED: dict[str, str] = {}


def unapply() -> list[str]:
    """Remove from os.environ what the last apply() put there. Returns the keys."""
    dropped: list[str] = []
    for key, value in list(_APPLIED.items()):
        if os.environ.get(key) == value:
            del os.environ[key]
            dropped.append(key)
    _APPLIED.clear()
    return dropped


def apply(path: Path | None = None) -> list[str]:
    """Pull the file into os.environ for every key the environment does not
    already set, after taking back what the previous apply() set, so a reload
    sees the file as it is now. Returns the keys applied, for the daemon log."""
    unapply()
    applied: list[str] = []
    for key, value in read(path).items():
        if key in os.environ:
            continue
        os.environ[key] = value
        _APPLIED[key] = value
        applied.append(key)
    return applied


def write(updates: dict[str, str | None], path: Path | None = None) -> tuple[list[str], list[str]]:
    """Set or remove keys, preserving every other line (comments included).

    None removes the key. New keys go under one section header at the end.
    Returns (written, removed). Atomic: temp file then os.replace, like the
    store, so a crash mid-write leaves the old file whole.
    """
    path = path or default_path()
    for key in updates:
        if not KEY_RE.match(key):
            raise ValueError(f"not an Otto setting: {key}")

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []

    written: list[str] = []
    removed: list[str] = []
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _LINE_RE.match(line)
        key = m.group(1) if m and not line.lstrip().startswith("#") else None
        if key in pending:
            value = pending.pop(key)
            if value is None:
                removed.append(key)
                continue
            out.append(f"{key}={_quote(value)}")
            written.append(key)
        else:
            out.append(line)

    new = {k: v for k, v in pending.items() if v is not None}
    if new:
        if out and out[-1].strip():
            out.append("")
        if SECTION_HEADER not in out:
            out.append(SECTION_HEADER)
        for key, value in new.items():
            out.append(f"{key}={_quote(value)}")
            written.append(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".otto.env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out).rstrip("\n") + ("\n" if out else ""))
        os.replace(tmp, path)
    except BaseException:
        with _suppress_oserror():
            os.unlink(tmp)
        raise
    return written, removed


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def known_keys(example: Path | None = None) -> set[str]:
    """Every key .env.example documents. The API refuses a key that is not here
    when the file exists: a typo in a setting name would otherwise be written,
    applied, and silently ignored by config forever. An install without the
    example file (a packaged copy) accepts any OTTO_* key."""
    example = example or Path(__file__).resolve().parent.parent / ".env.example"
    try:
        text = example.read_text(encoding="utf-8-sig")
    except OSError:
        return set()
    return {m.group(1) for m in re.finditer(r"^(OTTO_[A-Z0-9_]+)=", text, re.M)}


def is_secret(key: str) -> bool:
    return bool(SECRET_RE.search(key))


def describe(path: Path | None = None) -> dict:
    """What the dashboard shows about the file: where it is, what it holds
    (secrets masked), and which of its keys the environment overrides, since a
    value the person just saved that does not take effect after a restart is
    almost always one of those."""
    path = path or default_path()
    values = read(path)
    overrides = sorted(k for k in values if k in os.environ and os.environ[k] != values[k])
    return {
        "path": str(path),
        "exists": path.exists(),
        "values": {k: (MASK if is_secret(k) and v else v) for k, v in values.items()},
        "env_overrides": overrides,
    }
