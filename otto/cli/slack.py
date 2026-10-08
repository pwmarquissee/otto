"""Speaking as Otto on Slack: reply, post, tell, notify."""

from __future__ import annotations

import sys
from pathlib import Path

from .. import config
from ..client import Client
from ._fmt import C_YEL, _c


def cmd_reply(args, client: Client) -> int:
    """Post a thread reply as Otto. How a triage session speaks without the token."""
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to post an empty reply", file=sys.stderr)
        return 2
    try:
        res = client.slack_reply(args.channel, args.thread, text)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (channel allowlist, thread
        # required, unattended, no-send). Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  posted as Otto in {res['channel']} thread {args.thread}  ts {res['ts']}")
    return 0


def cmd_post(args, client: Client) -> int:
    """Post top-level in an allowlisted channel, as Otto. How the daily run speaks.

    Defaults to config.SLACK_CHANNEL_ID because that is the one feed this exists for;
    naming another channel is possible but has to be deliberate, and the daemon will
    still refuse anything outside config.POST_CHANNELS.
    """
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to post an empty message", file=sys.stderr)
        return 2
    if not args.channel:
        # An empty id is "unconfigured", never a channel. Say so here rather than
        # let the daemon turn it into a Slack API error about a blank argument.
        print("  no channel: pass --channel or set OTTO_SLACK_CHANNEL_ID", file=sys.stderr)
        return 2
    try:
        res = client.slack_post(args.channel, text)
    except RuntimeError as e:
        # The daemon's refusals are the interlocks (channel allowlist, no-send).
        # Show them plainly rather than as a stack.
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  posted as Otto in {res['channel']}  ts {res['ts']}")
    return 0


def cmd_tell(args, client: Client) -> int:
    """DM the owner as Otto. Cannot be pointed at anybody else."""
    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else (args.text or sys.stdin.read()))
    if not text.strip():
        print("  refusing to send an empty message", file=sys.stderr)
        return 2
    try:
        res = client.slack_tell(text)
    except RuntimeError as e:
        print(f"  refused: {e}", file=sys.stderr)
        return 1
    print(f"  told {config.OWNER_NAME}, as Otto  ({res['channel']} ts {res['ts']})")
    return 0


def cmd_notify(args, client: Client) -> int:
    """Send the owner a message. For agents and schedules, not usually typed by hand."""
    try:
        n = client.notify(" ".join(args.title), body=args.body, level=args.level,
                          domain=args.domain or config.WORK, source=args.source,
                          command=args.command)
    except RuntimeError as e:
        print(_c(f"  {e}", C_YEL), file=sys.stderr)
        return 1
    print(f"  sent {n['id'][:6]} [{n['level']}] {n['title'][:60]}")
    return 0


def add_reply(sub) -> None:
    s = sub.add_parser("reply", help="post a thread reply as Otto")
    s.add_argument("--channel", required=True, help="channel id (must be allowlisted)")
    s.add_argument("--thread", required=True, help="parent message ts")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_reply)


def add_post(sub) -> None:
    s = sub.add_parser("post", help="post top-level in an allowlisted channel as Otto")
    s.add_argument("--channel", default=config.SLACK_CHANNEL_ID or None,
                   help="channel id (must be allowlisted; default "
                        + (f"{config.SLACK_CHANNEL_ID}, #{config.SLACK_CHANNEL}"
                           if config.SLACK_CHANNEL_ID else "none: set OTTO_SLACK_CHANNEL_ID")
                        + ")")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_post)


def add_tell(sub) -> None:
    s = sub.add_parser("tell", help="DM the owner as Otto (recipient is fixed)")
    s.add_argument("--text")
    s.add_argument("--text-file", help="preferred: keeps the body off the command line")
    s.set_defaults(fn=cmd_tell)


def add_notify(sub) -> None:
    s = sub.add_parser("notify", help="send the owner a message (for agents/schedules)")
    s.add_argument("title", nargs="+")
    s.add_argument("--body")
    s.add_argument("--level", choices=["info", "warn", "crit"], default="info")
    s.add_argument("--domain", choices=config.DOMAINS)
    s.add_argument("--source", default="agent")
    s.add_argument("--command")
    s.set_defaults(fn=cmd_notify)


PARSERS = {
    "reply": add_reply,
    "post": add_post,
    "tell": add_tell,
    "notify": add_notify,
}
