"""Posting as Otto: thread reply, channel post, owner DM.

Moved out of otto/daemon.py as it was. `_d` is the daemon module: every
handler reads `_d.store` (and the tick helpers it needs) at call time, so a test
that swaps `daemon.store` swaps the store these routes see. The daemon includes
`router` once, after `app` exists; see otto/api/__init__.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, slack
from .. import daemon as _d

router = APIRouter()


class ReplyRequest(BaseModel):
    """Otto answering in a Slack thread, as Otto."""

    channel: str
    thread_ts: str
    text: str

@router.post("/api/slack/reply")
def slack_reply(req: ReplyRequest) -> dict[str, Any]:
    """Post a thread reply as Otto. The only channel-posting door in the API.

    The daemon does this rather than the caller because the bot credential must never
    enter a spawned session's environment: a session that held it could message the
    company with nothing in the way. So the session asks, and the limits
    (config.REPLY_CHANNELS, thread_ts required) are applied here in Python.
    """
    try:
        channel, ts = slack.reply_in_thread(req.channel, req.thread_ts, req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"replied in {channel} thread {req.thread_ts}: {req.text[:60]}",
              source="slack")
    return {"ok": True, "channel": channel, "ts": ts}

class PostRequest(BaseModel):
    """Otto starting its own message in a channel, as Otto."""

    channel: str
    text: str

@router.post("/api/slack/post")
def slack_post(req: PostRequest) -> dict[str, Any]:
    """Post top-level into an allowlisted channel, as Otto.

    Same reason as slack_reply for living in the daemon: the bot credential must
    never enter a spawned session's environment. The allowlist (config.POST_CHANNELS)
    is applied here in Python, not asked for in a prompt.

    The stamp is the second half of the fix, and the order matters. Only a post that
    Slack ACCEPTED stamps config.FEED_POST_SCHEDULE, so the freshness of that
    schedule is the freshness of the feed itself rather than of a run that believed
    it had posted. The bug this replaces was exactly that inversion: /daily stamped
    unconditionally, the summary silently did not go out, and every health surface
    read green for eight days.
    """
    if not req.channel.strip():
        # A blank id reaches here when a caller defaulted to an unconfigured
        # SLACK_CHANNEL_ID. Refuse by name rather than hand Slack an empty channel.
        raise HTTPException(400, "no channel given and OTTO_SLACK_CHANNEL_ID is not set")
    try:
        channel, ts = slack.post_to_channel(req.channel, req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"posted in {channel}: {req.text[:60]}", source="slack")
    if config.SLACK_CHANNEL_ID and channel == config.SLACK_CHANNEL_ID:
        _d.store.stamp(config.FEED_POST_SCHEDULE, "ok")
    return {"ok": True, "channel": channel, "ts": ts}

class TellRequest(BaseModel):
    """Otto telling the owner something, as Otto. No recipient field: see slack.tell_owner."""

    text: str

@router.post("/api/slack/tell")
def slack_tell(req: TellRequest) -> dict[str, Any]:
    """DM the owner as Otto. The reporting channel: no allowlist, because it cannot be
    pointed anywhere but at the owner."""
    try:
        channel, ts = slack.tell_owner(req.text)
    except slack.SlackError as e:
        raise HTTPException(400, str(e)) from e
    _d.store.log(f"told {config.OWNER_NAME}: {req.text[:60]}", source="slack")
    return {"ok": True, "channel": channel, "ts": ts}
