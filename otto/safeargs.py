"""safeargs: the shapes an id may have before it goes near a shell or a CLI.

WHY ONE MODULE. Session ids, pane ids and agent names arrive from places Otto
does not control: a hook payload, a herdr snapshot, a request body, a card a model
filed. Most of them end up as argv elements (safe by construction), but a few are
typed into a pane shell, written into a .ps1, or handed to wt.exe, which parses
its own argv. Each of those sinks used to trust its input. The patterns live here
so the API boundary and the sink check the same thing, and a test can pin them.

The rule for callers: validate at the boundary (a 400/422 names the bad field),
and check again at the sink (raise), because a value can reach state through a
path that has no boundary, such as an older state file.
"""

from __future__ import annotations

import re

# Claude Code session ids are UUIDs. The pattern is wider than a UUID because
# tests, herdr, and older state use short tokens ("abc", "otto-sess-1"); what it
# guarantees is the property the sinks need: no whitespace, no quote, no shell
# metacharacter, and no leading dash that a CLI would read as an option.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# herdr pane ids, as its CLI prints them: workspace and pane, "w1:p1".
PANE_ID_RE = re.compile(r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")
# herdr's own rule for agent names (see herdr.agent_name_for).
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Otto agent definitions are files under ~/.claude/agents; the name must not be
# able to climb out of that directory.
AGENT_DEF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# A git branch or ref as Otto hands it to herdr (which hands it to git). Narrower
# than git-check-ref-format on purpose: no leading dash (option injection), no
# whitespace or control characters, no "..", no "@{".
BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/+-]{1,200}$")
# An environment variable name written into a generated PowerShell script.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def is_session_id(value: object) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_RE.match(value))


def is_pane_id(value: object) -> bool:
    return isinstance(value, str) and bool(PANE_ID_RE.match(value))


def is_agent_name(value: object) -> bool:
    return isinstance(value, str) and bool(AGENT_NAME_RE.match(value))


def is_herdr_target(value: object) -> bool:
    """A terminal or agent target: a pane id or an agent name, nothing else."""
    return is_pane_id(value) or is_agent_name(value)


def is_agent_def(value: object) -> bool:
    return isinstance(value, str) and bool(AGENT_DEF_RE.match(value))


def is_branch(value: object) -> bool:
    return isinstance(value, str) and bool(BRANCH_RE.match(value))


def is_env_name(value: object) -> bool:
    return isinstance(value, str) and bool(ENV_NAME_RE.match(value))


# PowerShell treats all of these as a single quote inside a single-quoted string,
# not just the ASCII one. Doubling only "'" let a title carrying U+2019 (the
# apostrophe every phone and word processor types) close the string and run the
# rest as code. Verified on Windows PowerShell 5.1, 2026-10-02.
_PS_SINGLE_QUOTES = ("'", "‘", "’", "‚", "‛")


def ps_quote(value: object) -> str:
    """Single-quote a value for PowerShell. Every single-quote character,
    ASCII or typographic, is doubled, which is PowerShell's own escape."""
    text = str(value)
    for q in _PS_SINGLE_QUOTES:
        text = text.replace(q, q + q)
    return "'" + text + "'"


def wt_escape(value: object) -> str:
    """Escape an argument for wt.exe. Windows Terminal splits its OWN command line
    on ';' into separate subcommands, even inside what looks like one quoted
    argument, so a title or directory holding '; new-tab ...' would open a second
    tab running whatever followed. A backslash before the semicolon is wt's
    documented literal semicolon."""
    return str(value).replace(";", "\\;")
