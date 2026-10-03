"""Claude Code PreToolUse hook. Gates DESTRUCTIVE actions in unattended Otto
sessions behind a live yes/no dialog on the owner's desktop.

    stdin: Claude Code's PreToolUse JSON (tool_name, tool_input, ...)
    stdout: an allow/deny decision, or nothing (= allow untouched)

WHY THIS EXISTS. Otto's scheduled sessions run with --dangerously-skip-permissions,
so every rule about what they may do lived in a prompt, and "a model that has been
told not to do something is not the same as a system that cannot". This hook is the
harness-level version of the rule.

WIDENED TO MUTATIONS after an early unattended run. A board card whose body said
"Needs the owner: rotate both credentials" was dispatched unattended, and the
session revoked a colleague's GitHub token via `gh api -X DELETE` and archived
their API key via a raw `curl -X POST` to an admin API. The guard allowed all of
it, because its threat model said unattended sessions hold no tokens. False: `gh`
is keyring-authenticated, a credential helper can cache an admin key on disk, and a
cloud SSO session can fetch secrets. Ambient credentials exist, so the gate sits on
the MUTATION, not only the checkout.

ASK, DON'T DENY. If the gate is a human saying yes or no, always ask the human. A
gated action raises a topmost yes/no dialog naming the run and showing the exact
call. Yes allows that one call. No, an unanswered dialog (default 120s), or any
failure to ask DENIES, with a reason telling the session to stage the action for
the owner instead. Fail-closed on the gated set, untouched everywhere else.

NARROWED TO DESTRUCTIVE after the first unattended morning under a wider net
raised three dialogs and all three timed out: a LOCAL findings file whose prose
mentioned the credential CLI, an `otto task add` whose tags included its name, and
the daily summary post to the owner's own ops channel. Two were pattern bugs (word
matching instead of invocation matching) and one was a policy miss. The owner
chose sends-flow-freely for Slack and Gmail. So:

  * Outreach is NOT gated here. Slack and Gmail sends in unattended sessions are
    allowed ("send freely" was chosen explicitly over prompt-per-DM and
    draft-only). outreach.py's roster/forbidden-subject interlocks still apply on
    the daemon path; this hook just stopped being a second gate on transmission.

    ONE CARVE-OUT, ABOUT IDENTITY NOT PERMISSION. The Slack connector is OAuth'd
    as the owner. With nothing in the way, board tasks once DM'd a colleague twice
    in one day through `slack_send_message`, and both arrived as the owner. So a
    connector send aimed at a PERSON (channel_id U.../D.../W..., or an mpim G...)
    is denied flat, no dialog, with the reason pointing at `otto dm`, which sends
    the same message as Otto through the daemon. Channel posts (C...) are
    untouched: the daily summary to the owner's own ops channel is the case the
    narrowing was about. Nothing here asks the owner; the message still flows, it
    just leaves under the right name.
  * Patterns match INVOCATIONS, not words. Quoted strings, bash heredocs, and
    PowerShell here-strings are stripped before scanning, so a session can write
    "run `gh api -X DELETE ...`" into a findings file, a task detail, or a message
    for the owner (which the dispatch rules TELL it to do) without raising a
    dialog. Only actually running one prompts.

WHAT MAKES A SESSION UNATTENDED. runners/detached.py sets OTTO_UNATTENDED=1 (and
OTTO_RUN_NAME, for the dialog title) in the launcher for every headless spawn.
Windowed sessions and the owner's own interactive sessions never have it, so this
hook is a no-op there.

WHAT IS GATED when the marker is present:

  CRED CHECKOUT    invoking a credential-checkout command (OTTO_GUARD_CRED_COMMANDS,
                   path-prefixed forms included), reading a cached credential file
                   (OTTO_GUARD_CRED_FILES), or Secrets Manager get-secret-value.
                   Not destructive by itself, but a checked-out admin token walks
                   around every other gate here. Word mentions (tags, prose, staged
                   instructions) do not match.
  RAW API WRITES   `gh api` with a non-GET method or field/input flags, and
                   curl/wget/Invoke-RestMethod/Invoke-WebRequest/irm/iwr with a
                   mutating method or a request body. Localhost is exempt: the
                   Otto board's own API is the session's job to write to. This
                   is the gate that would have stopped the rotation run twice over.
  IDP MUTATIONS    create/update/delete/activate/deactivate/add/remove/confirm
                   tools on any MCP server whose name matches
                   OTTO_GUARD_IDP_TOOL_PATTERN (your identity provider). Identity
                   lifecycle is the credential plane with a different name. Reads
                   (get/list) pass.

  PROTECTED CONFIG editing the files that decide whether checks pass: test and
                   lint config (pytest.ini, pyproject.toml, ruff/mypy/eslint/biome/
                   tsconfig and friends), conftest.py, CI workflows, CLAUDE.md,
                   Claude Code settings.json and hooks, and this guard and
                   otto_hook.py themselves. Agents edit these to get to green; a
                   code floor that says "tests do not get weakened to make a change
                   pass" needs a harness-level half, and this is it. Edit/Write/
                   MultiEdit/NotebookEdit on such a path gate, as do shell write
                   shapes (redirect, sed -i, Set-Content, rm, mv, git checkout ...)
                   naming one. Write to a path that does not exist yet passes:
                   scaffolding a new project's pyproject.toml is not weakening
                   anything. Prose mentions inside quoted strings pass; only a
                   quoted string that IS a bare path counts.

FACTS BEFORE THE DIALOG. A yes/no dialog that shows a command string asks the
owner to parse the command and infer the blast radius themselves. And a model
asked "are you sure" always says yes, so the session's own confidence is worth
nothing. For shell calls the gate requires the session to state, in comment lines
above the command:

    # otto-gate: target=<exactly what changes, by name or id>
    # otto-gate: rollback=<how to undo it, or "none, " and why>
    # otto-gate: authority=<the card id or quoted instruction that asked>

A gated shell call without all three (or with placeholders) is DENIED without
raising a dialog, with a reason that spells out the format. The owner's attention
is not spent on a call the session could not justify. When the facts are present,
the dialog leads with them and shows the command underneath, so the click
approves a named action with a stated rollback, not a string. The header lines
are stripped before gate scanning and are ordinary comments to bash and
PowerShell, so the executed command is unchanged. MCP tools and the file tools
carry no comment channel; their dialogs show the target from the input itself
(user id, file path and the exact old/new text).

WHAT IS DELIBERATELY NOT GATED: Slack/Gmail sends (above), ordinary local file
writes, `otto` CLI calls, EDR and RMM actions (board cards the owner queues on
purpose close stale detections unattended, intended behavior), and gh porcelain
(`gh pr create` is reviewable and reversible).

KNOBS.
  OTTO_GUARD_ASK=0 turns the dialog off and restores flat deny (for a headless
    host with no interactive desktop, docs/otto-on-ec2.md).
  OTTO_GUARD_ASK_TIMEOUT overrides the wait in seconds. The settings.json hook
    entry carries a matching harness timeout with headroom; if you raise the
    wait, raise that too or the harness will kill the dialog first.
  OTTO_GUARD_CRED_COMMANDS, comma-separated command names whose INVOCATION is a
    credential checkout (your secrets CLI, your API-key helper). Default empty,
    which disables this gate: set it, or every unattended session can check out
    whatever those tools hold.
  OTTO_GUARD_CRED_FILES, comma-separated file-name fragments whose appearance
    anywhere in a command is a checkout with extra steps (a cached admin key on
    disk). Default empty.
  OTTO_GUARD_IDP_TOOL_PATTERN, a case-insensitive regex searched against the full
    MCP tool name. Mutating verbs on a matching tool gate. Default empty, which
    disables the IdP gate.
  The knobs are read on every call, so a test can set them with monkeypatch
  without re-importing the module.

Same rules as otto_hook.py (this runs inside tool calls): stdlib only, always
exit 0, and nothing on stdout except the decision. Every asked decision is also
appended to ~/.claude/otto/logs/guard-decisions.jsonl, best-effort, so there is
an audit line for what the owner approved. A parse failure of stdin allows rather
than denies: stdin comes from Claude Code itself, not from the model, so a
malformed payload is a harness bug and failing every tool call over it would
take the whole session down. A failure to ASK, by contrast, denies: at that
point we already know the call is one the owner gates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

_SHELL_TOOLS = ("Bash", "PowerShell")
_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# ---- facts header ----------------------------------------------------------------
# One fact per comment line, above the command. Parsed from the RAW command
# (the values are prose and may contain quotes), then removed before the gate
# regexes see the command, so a header can never itself trip a gate.
_FACTS_LINE = re.compile(r"^[ \t]*#[ \t]*otto-gate:[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
_FACT_KEYS = ("target", "rollback", "authority")
_FACT_MIN_LEN = 8
_FACT_PLACEHOLDER = re.compile(
    r"^(?:\?+|n/?a|none|null|unknown|tbd|todo|-+|\.+|see above|as above|same|x+)$",
    re.IGNORECASE,
)


def _facts(raw_cmd: str) -> dict:
    """The otto-gate header, as {key: value}. Later lines win over earlier."""
    out: dict = {}
    for m in _FACTS_LINE.finditer(raw_cmd):
        body = m.group(1)
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        key = key.strip().lower()
        if key in _FACT_KEYS:
            out[key] = value.strip()
    return out


def _facts_missing(facts: dict) -> list[str]:
    """Which of the three facts are absent, too thin, or a placeholder.

    A rollback of "none" alone is refused on purpose: irreversibility is the
    fact the owner most needs, so the header has to say "none, <why>"."""
    missing = []
    for key in _FACT_KEYS:
        value = str(facts.get(key) or "").strip()
        if len(value) < _FACT_MIN_LEN or _FACT_PLACEHOLDER.match(value):
            missing.append(key)
    return missing


# ---- text stripping ----------------------------------------------------------
# Everything below scans STRIPPED shell commands: heredoc/here-string bodies and
# quoted strings are replaced with a placeholder first, so prose and staged
# command text cannot trip a gate. The placeholder is a bare word ("Q") rather
# than nothing so that `curl -X "POST"` still presents a non-GET token to the
# method regex instead of vanishing.
_BASH_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[\s\S]*?\n\1(?=[\r\n;]|\s*$)")
_PS_HERESTRING = re.compile(r"@['\"][\s\S]*?['\"]@")
_DQ_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_SQ_STRING = re.compile(r"'[^']*'")


def _strip(cmd: str) -> str:
    cmd = _FACTS_LINE.sub(" ", cmd)
    cmd = _BASH_HEREDOC.sub(" Q ", cmd)
    cmd = _PS_HERESTRING.sub(" Q ", cmd)
    cmd = _DQ_STRING.sub("Q", cmd)
    cmd = _SQ_STRING.sub("Q", cmd)
    return cmd


# ---- credential checkout -----------------------------------------------------
# The checkout commands are the operator's own tools, so they come from the
# environment. They gate only when INVOKED: at the start of the command or after a
# shell separator / subshell / source, with an optional path prefix and extension
# (a path-prefixed `secrets-cli.exe` counts, `--tags secrets,secrets-cli` does not).
# Compiled per call from the current environment, cached on the raw value, so a
# test can set the knob with monkeypatch and the hook never pays twice for it.
_ENV_CRED_COMMANDS = "OTTO_GUARD_CRED_COMMANDS"
_ENV_CRED_FILES = "OTTO_GUARD_CRED_FILES"
_ENV_IDP_PATTERN = "OTTO_GUARD_IDP_TOOL_PATTERN"


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _compile_invoke(names: tuple[str, ...]) -> re.Pattern | None:
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(
        r"(?:^|[;&|(\n]|\$\(|`|\bsource\s+|\bexec\s+|\bcommand\s+)"
        r"\s*(?:\S*[\\/])?(?:" + alts + r")(?:\.\w+)?(?=[\s)]|$)",
        re.IGNORECASE,
    )


def _compile_files(names: tuple[str, ...]) -> re.Pattern | None:
    if not names:
        return None
    return re.compile("|".join(re.escape(n) for n in names), re.IGNORECASE)


def _compile_idp(pattern: str) -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        # A broken pattern must not take the hook down with it. Treat it as a
        # literal so a plain server name still works, which is the common case.
        return re.compile(re.escape(pattern), re.IGNORECASE)


_cache: dict[str, tuple[str, re.Pattern | None]] = {}


def _cached(name: str, raw: str, build) -> re.Pattern | None:
    hit = _cache.get(name)
    if hit is not None and hit[0] == raw:
        return hit[1]
    rx = build(raw)
    _cache[name] = (raw, rx)
    return rx


def _cred_invoke_re() -> re.Pattern | None:
    raw = os.environ.get(_ENV_CRED_COMMANDS, "")
    return _cached(_ENV_CRED_COMMANDS, raw,
                   lambda r: _compile_invoke(tuple(_env_list(_ENV_CRED_COMMANDS))))


def _cred_files_re() -> re.Pattern | None:
    raw = os.environ.get(_ENV_CRED_FILES, "")
    return _cached(_ENV_CRED_FILES, raw,
                   lambda r: _compile_files(tuple(_env_list(_ENV_CRED_FILES))))


def _idp_re() -> re.Pattern | None:
    raw = os.environ.get(_ENV_IDP_PATTERN, "").strip()
    return _cached(_ENV_IDP_PATTERN, raw, _compile_idp)


# The AWS CLI verb, whatever profile it rides on. Not operator-specific, so fixed.
_CRED_AWS = re.compile(r"(?<![\w-])get-secret-value(?![\w-])", re.IGNORECASE)

# ---- raw HTTP writes ----------------------------------------------------------
_HTTP_CLIENT = re.compile(
    r"(?<![\w-])(curl|wget|Invoke-RestMethod|Invoke-WebRequest|irm|iwr)(?![\w-])",
    re.IGNORECASE,
)
# An explicit method: curl -X/--request, PowerShell -Method. Group 1 is the verb.
_HTTP_METHOD = re.compile(
    r"(?:^|[\s'\";(])(?:-X|--request|-Method)[\s=:]*['\"]?([A-Za-z]+)",
    re.IGNORECASE,
)
# A request body with no explicit method, which the clients turn into a POST.
_DATA_FLAGS = re.compile(
    r"(?:^|\s)(?:"
    r"--data(?:-\w+)?|-d|--json|--form|-F|--upload-file|-T"
    r"|--post-data|--post-file|-Body|-InFile"
    r")(?=[\s='\"]|$)",
    re.IGNORECASE,
)
_HTTP_GETIFY = re.compile(r"(?:^|\s)(?:-G|--get)(?=\s|$)")
_LOCALHOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])", re.IGNORECASE)

_GH_API = re.compile(r"(?<![\w-])gh(?:\.exe)?\s+api(?![\w-])", re.IGNORECASE)
_GH_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)[\s=]*['\"]?([A-Za-z]+)")
# gh api's field/input flags default the request to POST when no method is given.
_GH_BODY = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?=[\s=])")

# ---- identity provider ---------------------------------------------------------
# Verbs that change identity state. The tool's final segment is matched, so this
# survives a server rename (mcp__idp-mcp-server__deactivate_user and
# mcp__idp_prod__deactivate_user gate the same). Which servers count is
# OTTO_GUARD_IDP_TOOL_PATTERN, searched against the full tool name.
_IDP_MUTATE = re.compile(
    r"^(create|update|delete|activate|deactivate|add|remove|confirm)_"
)

# ---- protected config -----------------------------------------------------------
# The files that decide whether checks pass, plus the harness that enforces the
# gates. Matched on the normalized (forward-slash, lowercased) path.
_PROTECTED_NAMES = frozenset({
    # python
    "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py",
    "ruff.toml", ".ruff.toml", "mypy.ini", ".flake8", ".pylintrc",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    # js / ts
    "biome.json", "biome.jsonc", "tsconfig.json", ".npmrc",
    # go / rust
    ".golangci.yml", ".golangci.yaml", "clippy.toml", "rustfmt.toml", "deny.toml",
})
_PROTECTED_NAME_RE = re.compile(
    r"^(?:\.eslintrc(?:\..+)?|eslint\.config\..+|\.prettierrc(?:\..+)?"
    r"|jest\.config\..+|vitest\.config\..+|playwright\.config\..+|tsconfig\..+\.json)$"
)
# Paths whose tail (not just basename) identifies them: CI, Claude Code settings
# and hooks, every CLAUDE.md, and the two scripts that make this gate exist.
_PROTECTED_TAIL_RE = re.compile(
    r"(?:^|/)(?:"
    r"\.github/workflows/[^/]+"
    r"|\.gitlab-ci\.ya?ml"
    r"|\.claude/settings(?:\.local)?\.json"
    r"|\.claude/hooks/[^/]+"
    r"|claude\.md"
    r"|scripts/otto_guard\.py"
    r"|scripts/otto_hook\.py"
    r")$"
)
# A shell command shape that writes, moves, or deletes a file. Redirects exclude
# stderr/dev-null forms (2>, &>, >/dev/null, >$null, >nul). Coarse on purpose.
_SHELL_WRITE_SHAPE = re.compile(
    r"(?:(?<![\d&<])>{1,2}(?![>&]|\s*(?:/dev/null|\$null|nul(?![\w.])))"
    r"|(?:^|[\s;&|(])(?:"
    r"sed\s+(?:-\w+\s+)*-[a-zA-Z]*i|tee|rm|mv|cp|del|erase|truncate|install"
    r"|Set-Content|Out-File|Add-Content|Clear-Content|Remove-Item|Move-Item|Copy-Item"
    r"|New-Item|ni|sc|ri|mi|cpi"
    r"|git\s+(?:checkout|restore|rm|mv|clean)"
    r"|python3?\s+-c|open\s*\("
    r")(?![\w-]))",
    re.IGNORECASE,
)
# Path-looking tokens in a stripped command: anything with a dot or slash that
# is not a flag. Quoted strings are handled separately (see _shell_writes_protected).
_PATH_TOKEN = re.compile(r"(?<![\w-])(?!-)[\w.~$:{}\\/-]*[\w.]+(?![\w-])")
# Scanned independently, not as one alternation, so a single-quoted path nested
# inside a double-quoted `python -c "..."` body is still seen.
_QUOTED = (re.compile(r"\"([^\"\n]*)\""), re.compile(r"'([^'\n]*)'"))


def _norm_path(path: str) -> str:
    return str(path or "").strip().strip("\"'").replace("\\", "/").lower()


def _protected_path(path: str) -> bool:
    p = _norm_path(path)
    if not p or p.startswith("-"):
        return False
    name = p.rsplit("/", 1)[-1]
    if name in _PROTECTED_NAMES or _PROTECTED_NAME_RE.match(name):
        return True
    return bool(_PROTECTED_TAIL_RE.search(p))


def _file_tool_edits_protected(tool: str, tool_input: dict) -> bool:
    """Edit/Write/MultiEdit/NotebookEdit aimed at protected config.

    Write to a path that does not exist yet passes: a new project's
    pyproject.toml is scaffolding, not weakening. Every other file tool needs
    the file to exist already, so the exists check only matters for Write."""
    if tool not in _FILE_TOOLS:
        return False
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not _protected_path(path):
        return False
    if tool == "Write" and not os.path.exists(path):
        return False
    return True


def _shell_writes_protected(raw_cmd: str, stripped_cmd: str) -> bool:
    """A shell command with a write shape naming a protected path.

    The path may be a bare token in the stripped command, or a quoted string
    whose WHOLE content is a path. A findings file whose prose says "edit
    pytest.ini" has the words inside a longer string and does not count; that
    is the prose-versus-invocation lesson applied to this gate."""
    if not _SHELL_WRITE_SHAPE.search(stripped_cmd):
        return False
    for tok in _PATH_TOKEN.findall(stripped_cmd):
        if ("/" in tok or "." in tok) and _protected_path(tok):
            return True
    for rx in _QUOTED:
        for s in rx.findall(raw_cmd):
            if s and " " not in s and _protected_path(s):
                return True
    return False


_DECISIONS_LOG = os.path.join(
    os.path.expanduser("~"), ".claude", "otto", "logs", "guard-decisions.jsonl")

_STAGE_INSTEAD = (
    "Do not retry it and do not look for another way to apply this. Instead, "
    "state the exact command or API call the owner should run, and why, in your "
    "final paragraph. The owner will run the attended half themselves."
)

_DENY_REASONS = {
    "no": (
        "The owner saw the approval dialog for this action and clicked No. "
        + _STAGE_INSTEAD
    ),
    "timeout": (
        "This session is unattended (Otto-spawned) and this action "
        "(destructive API write, credential checkout, or identity mutation) is "
        "gated on the owner's live approval. The dialog went unanswered, so they "
        "are likely away from the desk. " + _STAGE_INSTEAD
    ),
    "off": (
        "This session is unattended (Otto-spawned) and this action "
        "(destructive API write, credential checkout, or identity mutation) is "
        "gated on the owner's live approval, which is switched off on this host "
        "(OTTO_GUARD_ASK=0). " + _STAGE_INSTEAD
    ),
}
_DENY_REASONS["error"] = _DENY_REASONS["timeout"]

_FACTS_REASON = (
    "This session is unattended (Otto-spawned) and this action (destructive API "
    "write, credential checkout, identity mutation, or edit to protected config) "
    "is gated on the owner's live approval. Before they are asked, the call must "
    "carry the facts they would be approving. Missing or too thin: {missing}. Re-issue "
    "the IDENTICAL command with these three comment lines above it, one fact per "
    "line, each a real statement of at least a few words and never a placeholder:\n"
    "  # otto-gate: target=<exactly what changes, by name or id: which key, which "
    "user, which resource, which file>\n"
    "  # otto-gate: rollback=<how to undo it, or 'none, ' followed by why it cannot "
    "be undone>\n"
    "  # otto-gate: authority=<the board card id, or the quoted instruction, that "
    "asked for this>\n"
    "The lines are ordinary comments and do not change what runs. If you cannot "
    "state all three truthfully, do not run it. " + _STAGE_INSTEAD
)

_ALLOW_REASON = "The owner approved this action live via the unattended-guard dialog."

# ---- slack identity ------------------------------------------------------------
# The connector's send tools, by final segment, so a server rename survives.
_SLACK_CONNECTOR_SEND = re.compile(r"^slack_(send_message|schedule_message)$")
# A person or private conversation, as opposed to a channel: Slack user ids (U/W),
# DM channels (D), and legacy mpim ids (G). Anything else is a channel post.
_SLACK_PERSON_ID = re.compile(r"^[UWDG][A-Z0-9]+$")

_MISROUTED_DM_REASON = (
    "This session is unattended (Otto-spawned) and this tool sends through the "
    "Slack connector, which is OAuth'd as THE OWNER: the message would arrive from "
    "them, not from Otto. Do not retry and do not look for another Slack tool. Write "
    "the text to a file and send it as Otto: `python -m otto dm <login-or-email> "
    "--text-file <file> --why \"<what was asked>\"` opens a group DM with the owner "
    "and that person; `python -m otto tell --text-file <file>` reaches the owner "
    "alone. If "
    "that refuses, put the message text in your final paragraph and stop."
)


def _misrouted_dm(tool: str, tool_input: dict) -> bool:
    """A connector send aimed at a person rather than a channel."""
    if "slack" not in tool.lower():
        return False
    if not _SLACK_CONNECTOR_SEND.match(tool.rsplit("__", 1)[-1]):
        return False
    target = str(tool_input.get("channel_id") or tool_input.get("channel") or "").strip()
    return bool(_SLACK_PERSON_ID.match(target))


def _http_mutates(cmd: str) -> bool:
    """A stripped shell command that would write remote state over HTTP.

    Localhost is exempt wholesale: the board API is the one thing an unattended
    session legitimately writes to, and a command mixing localhost with a remote
    mutation is not a shape worth a parser. Coarse, like everything here."""
    if not _HTTP_CLIENT.search(cmd):
        return False
    if _LOCALHOST.search(cmd):
        return False
    m = _HTTP_METHOD.search(cmd)
    if m:
        return m.group(1).upper() not in ("GET", "HEAD")
    if _HTTP_GETIFY.search(cmd):
        return False  # curl -G turns data flags into a query string
    return bool(_DATA_FLAGS.search(cmd))


def _gh_api_mutates(cmd: str) -> bool:
    """`gh api` with a non-GET method, or body flags that default it to POST.

    Only the raw-API subcommand is gated. `gh pr create` and friends stay open:
    a PR is reviewable and reversible, a DELETE against an org endpoint is not."""
    if not _GH_API.search(cmd):
        return False
    m = _GH_METHOD.search(cmd)
    if m:
        return m.group(1).upper() != "GET"
    return bool(_GH_BODY.search(cmd))


def _gated(tool: str, tool_input: dict) -> bool:
    # Identity lifecycle on any configured IdP server. Reads pass, writes are gated.
    idp = _idp_re()
    if idp and idp.search(tool) and _IDP_MUTATE.match(tool.rsplit("__", 1)[-1]):
        return True

    # Shell commands: credential checkout and raw HTTP writes, scanned with
    # prose (quotes, heredocs) stripped out.
    if tool in _SHELL_TOOLS:
        raw = str(tool_input.get("command") or "")
        cmd = _strip(raw)
        invoke = _cred_invoke_re()
        files = _cred_files_re()
        if (invoke and invoke.search(cmd)) or (files and files.search(cmd)) \
                or _CRED_AWS.search(cmd):
            return True
        if _gh_api_mutates(cmd):
            return True
        if _http_mutates(cmd):
            return True
        if _shell_writes_protected(raw, cmd):
            return True

    if _file_tool_edits_protected(tool, tool_input):
        return True

    return False


def _clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + " ...[truncated]"


def _dialog_text(tool: str, tool_input: dict) -> str:
    run = os.environ.get("OTTO_RUN_NAME") or "unnamed run"
    facts_block = ""
    if tool in _SHELL_TOOLS:
        raw = str(tool_input.get("command") or "")
        facts = _facts(raw)
        if facts:
            facts_block = (
                f"TARGET:    {_clip(facts.get('target', ''), 300)}\n"
                f"ROLLBACK:  {_clip(facts.get('rollback', ''), 300)}\n"
                f"AUTHORITY: {_clip(facts.get('authority', ''), 300)}\n\n"
            )
        detail = _clip(_FACTS_LINE.sub("", raw).strip(), 900)
    elif tool in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if tool == "Write":
            content = str(tool_input.get("content") or "")
            detail = (
                f"PROTECTED CONFIG, full rewrite: {path}\n"
                f"({len(content)} chars)\n\n{_clip(content, 500)}"
            )
        elif tool == "MultiEdit":
            edits = tool_input.get("edits") or []
            parts = [f"PROTECTED CONFIG, {len(edits)} edits: {path}"]
            for e in edits[:3] if isinstance(edits, list) else []:
                parts.append(
                    f"--- old\n{_clip((e or {}).get('old_string', ''), 200)}\n"
                    f"+++ new\n{_clip((e or {}).get('new_string', ''), 200)}"
                )
            detail = "\n\n".join(parts)
        else:
            detail = (
                f"PROTECTED CONFIG: {path}\n\n"
                f"--- old\n{_clip(tool_input.get('old_string', ''), 350)}\n\n"
                f"+++ new\n{_clip(tool_input.get('new_string', ''), 350)}"
            )
        detail = _clip(detail, 900)
    else:
        try:
            detail = json.dumps(tool_input, indent=1)
        except (TypeError, ValueError):
            detail = str(tool_input)
        detail = _clip(detail, 900)
    return (
        f"Otto run '{run}' (unattended) wants to run a gated action:\n\n"
        f"{facts_block}"
        f"Tool: {tool}\n\n{detail}\n\n"
        "Yes allows this ONE call. No (or closing this) makes the session "
        "stage it for you instead."
    )


def _ask_owner(tool: str, tool_input: dict) -> str:
    """Raise a topmost yes/no dialog and wait. Returns yes|no|timeout|error|off.

    A MessageBox rather than a toast: a toast can be missed and cannot carry a
    binding answer back without an action server, and the whole point is that
    the owner's click IS the authorization. DefaultDesktopOnly makes
    it topmost on the interactive desktop even from a background process, and
    the default button is No so a stray Enter refuses. On timeout the powershell
    process is killed, which tears the dialog down with it."""
    if os.environ.get("OTTO_GUARD_ASK", "1") == "0":
        return "off"
    try:
        wait = int(os.environ.get("OTTO_GUARD_ASK_TIMEOUT", "120"))
    except ValueError:
        wait = 120
    msg = _dialog_text(tool, tool_input).replace("'", "''")
    title = "Otto: approve gated action?"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        f"$r = [System.Windows.Forms.MessageBox]::Show('{msg}', '{title}', "
        "[System.Windows.Forms.MessageBoxButtons]::YesNo, "
        "[System.Windows.Forms.MessageBoxIcon]::Warning, "
        "[System.Windows.Forms.MessageBoxDefaultButton]::Button2, "
        "[System.Windows.Forms.MessageBoxOptions]::DefaultDesktopOnly); "
        "[Console]::Write(\"$r\")"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=wait,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "error"
    answer = (proc.stdout or "").strip().lower()
    if answer == "yes":
        return "yes"
    if answer == "no":
        return "no"
    return "error"


def _audit(tool: str, tool_input: dict, answer: str) -> None:
    """Best-effort JSONL audit line. Never allowed to fail the hook."""
    try:
        raw = str(tool_input.get("command") or "")
        detail = (raw or json.dumps(tool_input))[:400]
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run": os.environ.get("OTTO_RUN_NAME") or "",
            "tool": tool,
            "detail": detail,
            "answer": answer,
        }
        if tool in _FILE_TOOLS:
            record["path"] = str(
                tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        facts = _facts(raw) if raw else {}
        if facts:
            record["facts"] = {k: v[:300] for k, v in facts.items()}
        line = json.dumps(record)
        os.makedirs(os.path.dirname(_DECISIONS_LOG), exist_ok=True)
        with open(_DECISIONS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _resolve(tool: str, tool_input: dict) -> dict | None:
    """None = not gated, let the call proceed untouched. Otherwise ask the owner
    and return the decision payload their answer (or absence) earns."""
    if _misrouted_dm(tool, tool_input):
        # Not a permission question, so no dialog: the same message is allowed,
        # through the door that sends it as Otto.
        _audit(tool, tool_input, "misrouted-dm")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _MISROUTED_DM_REASON,
            }
        }
    if not _gated(tool, tool_input):
        return None
    if tool in _SHELL_TOOLS:
        # Facts first. A call the session cannot justify never reaches the owner.
        missing = _facts_missing(_facts(str(tool_input.get("command") or "")))
        if missing:
            _audit(tool, tool_input, "facts-missing")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _FACTS_REASON.format(
                        missing=", ".join(missing)),
                }
            }
    answer = _ask_owner(tool, tool_input)
    _audit(tool, tool_input, answer)
    if answer == "yes":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": _ALLOW_REASON,
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _DENY_REASONS.get(
                answer, _DENY_REASONS["error"]),
        }
    }


def main() -> int:
    if os.environ.get("OTTO_UNATTENDED") != "1":
        return 0
    try:
        payload = json.load(sys.stdin)
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    decision = _resolve(tool, tool_input)
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
