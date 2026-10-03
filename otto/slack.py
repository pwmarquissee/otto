"""Otto speaking as Otto.

Every message Otto sends to a human goes through here. Before this existed,
transmission was a spawned Claude session calling the Slack MCP connector, which is
OAuth'd as the owner: so every automated message arrived FROM THE OWNER,
indistinguishable from them typing it. This module is what makes Otto's messages say Otto.

THE CREDENTIAL NEVER LIVES IN THIS PROCESS
The bot token is checked out per send through a just-in-time credential CLI
(config.CREDENTIAL_RUN_ARGV), which injects it into a short-lived child and nowhere
else. The daemon never holds it, so a daemon memory dump does not contain it and a
daemon that runs for a month does not hold a credential for a month. That is also
the contract such a CLI offers: give a process a credential, do not read one.

THE SEND GATE THAT MOVED
A send from here is an HTTP POST, not a tool call, so no PreToolUse hook can see it.
`_refuse_if_unattended()` below is the control at this choke point: a spawned session
cannot POST as Otto directly and has to ask the daemon (`otto tell`, `otto dm`,
`otto reply`, `otto post`), where the roster gate and the allowlists run in Python
first. It is not a
formality. Without it, any spawned agent that could import this module could message
the company with nothing in the way.

The hook still has one Slack job, the other way round: `scripts/otto_guard.py` denies
the CONNECTOR's DM tools in unattended sessions, because the connector is OAuth'd as
the owner and a message through it arrives as them. Found the hard way 2026-08-25, twice.

WHY A SUBPROCESS AND NOT `requests` IN THE DAEMON
Because the token would then have to be in the daemon's environment, which means it
is in the environment of everything the daemon spawns, which means a board task
inherits the ability to message the company. The subprocess boundary is what stops
that, and it costs about a second per send against a rate limit of six a day.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://slack.com/api/"

# Set by runners/detached.py on every headless session. Its presence means nobody is
# watching, which is exactly when Otto must not be able to message a colleague.
UNATTENDED_ENV = "OTTO_UNATTENDED"

# Set by every test harness. Refuses a send at the TRANSPORT, so a test cannot reach
# a colleague no matter what it has monkeypatched or forgotten to.
#
# This exists because of a real message. On 2026-08-12 the outreach harness stubbed
# transmission by function NAME, that function was renamed, the stub silently stopped
# intercepting, and the harness -- which sets OUTREACH_ENABLED=True on purpose to
# exercise the send path -- posted its fixture text "Ready when you are." into a
# group DM with a colleague. The harness docstring said "Nothing here can send: the
# transport is monkeypatched". That was a claim about a name, and names change.
#
# A test's safety must not depend on a test remembering something. Belt and braces:
# the stub is still there, and this is the thing that holds when the stub does not.
NO_SEND_ENV = "OTTO_NO_SEND"


class SlackError(RuntimeError):
    """A send that did not happen, with a reason worth showing a human."""


# ---------------------------------------------------------------------------
# child process: holds the token, talks to Slack, prints JSON, exits
# ---------------------------------------------------------------------------

def _call(token: str, method: str, payload: dict | None = None,
          **params: Any) -> dict:
    url = API + method
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"http_{e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"network: {e.reason}"}
    except (TimeoutError, OSError) as e:
        return {"ok": False, "error": f"transport: {type(e).__name__}: {e}"}


def _resolve(token: str, emails: list[str]) -> tuple[list[str], list[str]]:
    """Emails -> Slack user ids. Returns (ids, unresolved emails)."""
    ids: list[str] = []
    missing: list[str] = []
    for email in emails:
        body = _call(token, "users.lookupByEmail", email=email)
        uid = (body.get("user") or {}).get("id") if body.get("ok") else None
        (ids.append(uid) if uid else missing.append(email))
    return ids, missing


def _child(request: dict) -> dict:
    """Run one send request. Executes in the credential child, holding the token."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN absent in the child; "
                                      "the credential CLI did not inject it"}
    # A user token here would send as the owner and SUCCEED, which is the failure
    # worth refusing rather than logging: it looks identical to working.
    if not token.startswith("xoxb-"):
        return {"ok": False, "error": "credential is not a bot token (expected xoxb-); "
                                      "sending would go out under the owner's name"}

    if request.get("verb") == "lookup":
        got = _call(token, "users.lookupByEmail", email=request["email"])
        if not got.get("ok"):
            return {"ok": False, "error": f"users.lookupByEmail: {got.get('error')}"}
        return {"ok": True, "user_id": (got.get("user") or {}).get("id")}

    if request.get("verb") == "open":
        ids, missing = _resolve(token, list(request.get("emails") or []))
        if missing:
            return {"ok": False, "error": f"no Slack user for: {', '.join(missing)}"}
        got = _call(token, "conversations.open", payload={"users": ",".join(ids)})
        if not got.get("ok"):
            return {"ok": False, "error": f"conversations.open: {got.get('error')}"}
        return {"ok": True, "channel": (got.get("channel") or {}).get("id")}

    if request.get("verb") == "read":
        # Read-only, and the ONLY read this module does. Used to notice a summon
        # reaction in a channel Otto was invited to. Returns messages with their
        # reactions; the caller decides what any of it means.
        got = _call(token, "conversations.history",
                    channel=request["channel"], limit=int(request.get("limit") or 40))
        if not got.get("ok"):
            return {"ok": False, "error": f"conversations.history: {got.get('error')}"}
        return {"ok": True, "messages": got.get("messages") or []}

    channel = request.get("channel")
    if not channel:
        # No channel: open one. `users` may be one person (a DM) or several (a group
        # DM including the owner), and conversations.open is the same call either way.
        emails = list(request.get("emails") or [])
        if not emails:
            return {"ok": False, "error": "no channel and no emails to open one with"}
        ids, missing = _resolve(token, emails)
        if missing:
            return {"ok": False, "error": f"no Slack user for: {', '.join(missing)}"}
        opened = _call(token, "conversations.open", payload={"users": ",".join(ids)})
        if not opened.get("ok"):
            return {"ok": False, "error": f"conversations.open: {opened.get('error')}"}
        channel = (opened.get("channel") or {}).get("id")
        if not channel:
            return {"ok": False, "error": "conversations.open returned no channel id"}

    text = request.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "refusing to send an empty message"}

    post: dict[str, Any] = {"channel": channel, "text": text}
    if request.get("thread_ts"):
        post["thread_ts"] = request["thread_ts"]
    # Otto never unfurls. A nudge about a ticket should not paste a preview of
    # whatever was linked in it into a conversation a third party is reading.
    post["unfurl_links"] = False
    post["unfurl_media"] = False

    sent = _call(token, "chat.postMessage", payload=post)
    if not sent.get("ok"):
        return {"ok": False, "error": f"chat.postMessage: {sent.get('error')}",
                "channel": channel}
    return {"ok": True, "channel": sent.get("channel") or channel, "ts": sent.get("ts")}


# ---------------------------------------------------------------------------
# parent process: the daemon's interface
# ---------------------------------------------------------------------------

def _cred_argv(child: list[str]) -> list[str]:
    """Argv that runs `child` with the bot token in its environment.

    config.CREDENTIAL_RUN_ARGV is the credential CLI's command prefix, with `{env}`
    and `{cred}` placeholders for the variable to inject and the credential name,
    for example `("mycred", "run", "--with", "{env}={cred}", "--")`. The child
    command is appended after it.

    Why `shutil.which` and the command interpreter: `["tool", ...]` raises
    FileNotFoundError even when the tool is on PATH and `shutil.which` finds it,
    because on Windows npm-style CLIs install as `tool.CMD` and CreateProcess neither
    resolves PATHEXT nor executes a .cmd directly. Same shape as the `claude.CMD`
    problem runners/detached.py documents, and the same class of bug: it works in a
    shell, fails from Python, and the error names the wrong cause ("not on PATH" when
    it is very much on PATH). Resolve the real path, then hand a batch file to the
    command interpreter.
    """
    from . import config  # local: keeps this module importable without state

    # Read tolerantly: the constant is the deployment's to define, and an unset one
    # must read as "no credential CLI", not as a crash in the sender.
    template = [str(a) for a in (getattr(config, "CREDENTIAL_RUN_ARGV", ()) or ()) if str(a)]
    if not template:
        raise SlackError("OTTO_CREDENTIAL_RUN is not set, so no credential can be "
                         "checked out and nothing was sent")
    prefix = [a.replace("{env}", "SLACK_BOT_TOKEN").replace("{cred}", config.SLACK_BOT_CRED)
              for a in template]
    exe = shutil.which(prefix[0])
    if not exe:
        raise SlackError(f"{prefix[0]} is not on PATH, so no credential can be checked "
                         "out and nothing was sent")
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *prefix[1:], *child]
    return [exe, *prefix[1:], *child]


def _refuse_if_unattended() -> None:
    """The send gates. See the module docstring and NO_SEND_ENV."""
    if os.environ.get(NO_SEND_ENV) == "1":
        raise SlackError(
            f"refusing to send: {NO_SEND_ENV}=1. This process is a test or a dry "
            "run, and no message reaches a real person from one."
        )
    if os.environ.get(UNATTENDED_ENV) == "1":
        raise SlackError(
            "refusing to send: this process is marked unattended "
            f"({UNATTENDED_ENV}=1). Sending as Otto is not available to a spawned "
            "session; propose the message through the outreach API instead, where "
            "the hold and the rate limits apply."
        )


def send(text: str, *, channel: str | None = None, emails: list[str] | None = None,
         thread_ts: str | None = None, timeout: int = 120) -> tuple[str, str]:
    """Post as Otto. Returns (channel_id, message_ts). Raises SlackError.

    Give it either a `channel` (reply in a known conversation) or `emails` (open a
    conversation with those people first). Several emails means a GROUP DM, which is
    how Otto talks to a colleague: Otto, the owner, and the person, so the owner sees the reply
    and can correct in place rather than reading a report afterwards.
    """
    _refuse_if_unattended()
    if not channel and not emails:
        raise SlackError("send needs either a channel or the people to open one with")
    if emails and any(not (e or "").strip() for e in emails):
        # An empty address is almost always an unset OTTO_SLACK_OWNER. Dropping it
        # would open a conversation WITHOUT the owner, so refuse rather than trim.
        raise SlackError("an empty address was given; OTTO_SLACK_OWNER is probably "
                         "not set, and nothing was sent")

    from . import config  # local: keeps this module importable without state

    request = {"channel": channel, "emails": emails or [],
               "text": text, "thread_ts": thread_ts}

    # Over stdin, never argv. Message bodies are private DM content, and argv is
    # visible to every process on the box via the process list.
    argv = _cred_argv([sys.executable, "-m", "otto.slack", "--send"])
    try:
        proc = subprocess.run(
            argv, input=json.dumps(request), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            cwd=str(config.OTTO_REPO),
        )
    except FileNotFoundError as e:
        raise SlackError(f"could not start the credential CLI ({argv[0]}); nothing "
                         "was sent") from e
    except subprocess.TimeoutExpired as e:
        # Unknown, not failed. The POST may already have landed, and saying "failed"
        # invites a resend that double-messages a colleague.
        raise SlackError(
            f"send timed out after {timeout}s; whether Slack received it is UNKNOWN. "
            "Check the conversation before resending."
        ) from e

    # The credential CLI may print its own banner to stdout, so take the last JSON
    # object rather than assuming the whole stream is ours.
    result = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
    if result is None:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
        if "not logged in" in tail or "denied" in tail:
            raise SlackError(f"the credential CLI could not check out "
                             f"{config.SLACK_BOT_CRED}: {tail}")
        raise SlackError(f"send produced no result (exit {proc.returncode}): {tail}")

    if not result.get("ok"):
        raise SlackError(str(result.get("error") or "unknown send failure"))
    return str(result.get("channel")), str(result.get("ts"))


def reply_in_thread(channel: str, thread_ts: str, text: str,
                    timeout: int = 120) -> tuple[str, str]:
    """Post a reply inside an existing thread, as Otto. Raises SlackError.

    Narrow on purpose. A DM reaches one person who can ignore it; a channel post
    reaches everyone and cannot be unsent from anybody's memory. So this refuses two
    ways that a prompt cannot be talked out of: the channel must be in
    config.REPLY_CHANNELS, and a thread_ts is mandatory, which means Otto can only
    ever join a conversation somebody else started.

    For a post that starts its own message, see `post_to_channel`, which is gated on
    a SEPARATE and much shorter allowlist. Do not relax this one into that one: a
    channel Otto may answer questions in is not a channel Otto may announce into.
    """
    from . import config

    if not thread_ts or not str(thread_ts).strip():
        raise SlackError(
            "a thread timestamp is required: Otto replies inside threads and never "
            "posts top-level into a channel")
    if not (channel or "").strip():
        raise SlackError("no channel given; the channel id is unconfigured")
    if channel not in config.REPLY_CHANNELS:
        allowed = ", ".join(sorted(config.REPLY_CHANNELS)) or "(none)"
        raise SlackError(
            f"Otto may not post in {channel}. Allowed reply channels: {allowed}")
    return send(text, channel=channel, thread_ts=str(thread_ts).strip(),
                timeout=timeout)


def post_to_channel(channel: str, text: str, timeout: int = 120) -> tuple[str, str]:
    """Post TOP-LEVEL into an allowlisted channel, as Otto. Raises SlackError.

    The capability the daily summary needed and did not have. Its summary is a
    top-level post, the only thing that could make one was the Slack connector
    (OAuth'd as the owner, gated unattended), and so a scheduled run either posted
    nothing or posted under the owner's name depending on whether they were at their
    desk. Eight days of silence in the owner's ops feed, 2026-08-12 to 2026-08-20, with every
    health signal reading green.

    The gate that remains is config.POST_CHANNELS, and it is the whole control: no
    thread requirement can help here, because starting a message is the point. Keep
    that list short and keep it to feeds whose audience is Otto's own operator.
    """
    from . import config

    if not (channel or "").strip():
        raise SlackError("no channel given; OTTO_SLACK_CHANNEL_ID (or the caller's "
                         "channel) is unconfigured, so nothing was posted")
    if channel not in config.POST_CHANNELS:
        allowed = ", ".join(sorted(config.POST_CHANNELS)) or "(none)"
        raise SlackError(
            f"Otto may not post top-level in {channel}. Allowed post channels: "
            f"{allowed}. To answer inside an existing thread instead, use "
            "reply_in_thread.")
    return send(text, channel=channel, timeout=timeout)


def _run_child(request: dict, timeout: int) -> dict:
    """Hand one request to a credential child and return its parsed reply."""
    from . import config

    argv = _cred_argv([sys.executable, "-m", "otto.slack", "--send"])
    try:
        proc = subprocess.run(
            argv, input=json.dumps(request), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            cwd=str(config.OTTO_REPO),
        )
    except FileNotFoundError as e:
        raise SlackError(f"could not start the credential CLI ({argv[0]})") from e
    except subprocess.TimeoutExpired as e:
        raise SlackError(f"slack call timed out after {timeout}s") from e

    result = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
    if result is None:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
        raise SlackError(f"no result (exit {proc.returncode}): {tail}")
    return result


def read_channel(channel: str, limit: int = 40, timeout: int = 60) -> list[dict]:
    """Recent messages in a channel Otto was invited to, with their reactions.

    Not gated by _refuse_if_unattended: reading a channel Otto is a member of is not
    an action on anybody, and the gates exist to stop Otto SPEAKING unsupervised.
    Gating this would only mean the daemon cannot notice a summon.
    """
    result = _run_child({"verb": "read", "channel": channel, "limit": limit}, timeout)
    if not result.get("ok"):
        raise SlackError(str(result.get("error") or "read failed"))
    return list(result.get("messages") or [])


def lookup_user(email: str, timeout: int = 60) -> str | None:
    """Slack user id for a work email, or None."""
    result = _run_child({"verb": "lookup", "email": email}, timeout)
    if not result.get("ok"):
        raise SlackError(str(result.get("error") or "lookup failed"))
    return result.get("user_id")


def open_dm(email: str, timeout: int = 60) -> str | None:
    """The DM channel id with one person. Opens it if needed; notifies nobody."""
    result = _run_child({"verb": "open", "emails": [email]}, timeout)
    if not result.get("ok"):
        raise SlackError(str(result.get("error") or "open failed"))
    return result.get("channel")


def tell_owner(text: str, timeout: int = 120) -> tuple[str, str]:
    """DM the owner, as Otto. Raises SlackError.

    Safe by construction rather than by permission: the recipient is
    config.SLACK_OWNER_EMAIL and there is no parameter to point it at anybody else.
    That is why this needs no allowlist and no hold, unlike outreach - the worst case
    is Otto being tedious at the one person who can turn it off.
    """
    from . import config
    if not config.SLACK_OWNER_EMAIL:
        raise SlackError("OTTO_SLACK_OWNER is not set, so Otto has nobody to tell")
    return send(text, emails=[config.SLACK_OWNER_EMAIL], timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    """Child entry point: `python -m otto.slack --send`, request JSON on stdin."""
    argv = argv if argv is not None else sys.argv[1:]
    if "--send" not in argv:
        print(json.dumps({"ok": False, "error": "expected --send"}))
        return 2
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"unreadable request: {e}"}))
        return 2
    result = _child(request)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
