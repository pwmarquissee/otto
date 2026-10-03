"""The owner talking to Otto.

Otto could speak in Slack and could not listen. That made it a thing you configure
from a terminal, which is the wrong shape for the job: the moments worth capturing
happen away from the desk. On 2026-08-12 a version-control problem was resolved on a
voice call and nothing about it reached Otto, because reaching Otto meant opening a
laptop and typing `otto task add`.

So: DM Otto in Slack and it acts. From a phone, mid-call, walking.

    "remind me to check the EDR exclusion after the demo"
    "decided: no Rippling integration this quarter, cost"
    "michael owes me the network retest, thursday"

Otto turns it into a card, a decision record, or a note on somebody's dossier, and
REPLIES SAYING WHICH. A capture system whose results you cannot see is one you stop
trusting, and an assistant you do not trust you re-check by hand, which costs more
than typing it yourself would have.

WHY THE BOT READS THIS AND NOT THE OWNER'S USER TOKEN

`im:history` on the owner's token reads every DM they have with anybody. On the bot it reads
only conversations the bot is IN, which is its own DMs. The scope name is the same;
the reachable surface is not close.

ONLY THE OWNER, FOR NOW

Enforced here in Python rather than asked for in a prompt. Otto is new, outreach is
off, and a bot that acts on instructions from anybody who can find it is a different
security proposition than one that acts on instructions from its owner.
"""

from __future__ import annotations

from . import config, slack
from .models import iso, utcnow
from .store import Store

STATE = "otto-dm"

# Messages read per poll. Otto's own DM is not a busy channel, and anything older
# than this is being ignored on purpose rather than missed.
SCAN = 30


def _state(store: Store) -> dict:
    return store.feed_state(STATE)


def _owner_id(store: Store) -> str | None:
    """The owner's Slack id, resolved once and remembered.

    Cached because it never changes and resolving costs an API call on every poll
    otherwise. Re-resolved automatically if the cache is ever empty.
    """
    known = _state(store).get("owner_id")
    if known:
        return str(known)
    try:
        uid = slack.lookup_user(config.SLACK_OWNER_EMAIL)
    except slack.SlackError:
        return None
    if uid:
        store.put_feed_state(STATE, {"owner_id": uid})
    return uid


def _channel(store: Store) -> str | None:
    """The DM channel between Otto and the owner, resolved once and remembered."""
    known = _state(store).get("channel")
    if known:
        return str(known)
    try:
        cid = slack.open_dm(config.SLACK_OWNER_EMAIL)
    except slack.SlackError:
        return None
    if cid:
        store.put_feed_state(STATE, {"channel": cid})
    return cid


def incoming(messages: list[dict], owner_id: str, after_ts: str | None) -> list[dict]:
    """Messages from the owner that Otto has not acted on, oldest first.

    Filters out Otto's own replies by author rather than by absence of a user field:
    a bot message carries `bot_id`, and treating those as input would make Otto's
    confirmation ("filed that as a card") its own next instruction.
    """
    out = []
    for m in messages:
        if m.get("subtype") or m.get("bot_id"):
            continue
        if m.get("user") != owner_id:
            continue
        ts = m.get("ts")
        if not ts:
            continue
        if after_ts and float(ts) <= float(after_ts):
            continue
        out.append(m)
    out.sort(key=lambda m: float(m["ts"]))
    return out


def poll(store: Store) -> list[str]:
    """Check Otto's DM for instructions. Returns log notes.

    Called from the daemon tick. Every failure returns a note rather than raising: a
    Slack blip must not take the tick down.
    """
    if not config.DM_POLL:
        return []
    if not config.SLACK_OWNER_EMAIL:
        # Nobody to listen to. Resolving an empty address would be a Slack call made
        # for nothing on every tick, so stay idle and say so once.
        return _once(store, "DM inbox idle: OTTO_SLACK_OWNER is not set")

    channel = _channel(store)
    owner = _owner_id(store)
    if not channel or not owner:
        return _once(store, "cannot resolve Otto's DM with the owner yet")

    try:
        messages = slack.read_channel(channel, limit=SCAN)
    except slack.SlackError as e:
        return _once(store, f"DM poll failed: {e}")

    st = _state(store)
    patch: dict = {"last_error": None, "checked_at": iso(utcnow())}

    # FIRST POLL EVER: adopt the newest message as the watermark and act on nothing.
    #
    # Without this, the first run after the scope lands treats the whole scan window
    # as instructions and files a card for every DM in it, including months-old ones
    # the owner long since dealt with. The rule is that Otto acts on what you say to it
    # AFTER it could hear, not on everything it can now see. Same bootstrap the DM
    # producer needed, for the same reason.
    if "watermark" not in st:
        newest = max((m.get("ts") or "0" for m in messages), default="0")
        patch["watermark"] = newest
        store.put_feed_state(STATE, patch)
        return [f"DM inbox armed at {newest}; existing history left alone"]

    fresh = incoming(messages, owner, st.get("watermark"))
    if not fresh:
        store.put_feed_state(STATE, patch)
        return []

    # Watermark FIRST, for the same reason the summon poller marks handled first: a
    # crash between marking and acting costs one dropped instruction, which the owner
    # can see went unanswered and can repeat. The other order re-runs the instruction on
    # every tick until it succeeds, and these instructions change state.
    patch["watermark"] = fresh[-1]["ts"]
    store.put_feed_state(STATE, patch)

    # One session for the batch, not one per message. Three messages typed in a row
    # are usually one thought, and three sessions would pay the floor cost three
    # times to answer it in pieces.
    return [_dispatch(store, channel, fresh)]


def _once(store: Store, note: str) -> list[str]:
    """Log a failure the first time it appears, not once a minute forever."""
    if _state(store).get("last_error") == note:
        return []
    store.put_feed_state(STATE, {"last_error": note, "checked_at": iso(utcnow())})
    return [note]


def _dispatch(store: Store, channel: str, messages: list[dict]) -> str:
    from .runners import detached

    lines = "\n".join(f"  [{m['ts']}] {m.get('text') or ''}" for m in messages)
    owner = config.OWNER_NAME
    extra = (
        f"{owner.upper()} SENT THESE TO YOU IN SLACK. They are probably away from their "
        "desk, so they typed the shortest thing that would carry the meaning. Read them as one "
        "thought unless they are clearly separate.\n\n"
        f"{lines}\n\n"
        f"If they ask you to DM someone, do it AS OTTO: `python -m otto dm <login> "
        f"--text-file <file> --why <what they asked>` (a group DM that includes them). "
        f"Never the Slack connector's send tool, which posts as {owner}.\n\n"
        f"Reply in this DM when you are done: `python -m otto tell --text-file <file>`. "
        f"Say what you did in one line per item, naming the card or record you made, "
        f"so they can tell capture from silence without opening anything.\n\n"
        "If an instruction is genuinely ambiguous, do the part you are sure of, then "
        "ask ONE question about the rest. Do not ask them to restate something you "
        "could reasonably interpret: they are on a phone."
    )
    try:
        run = detached.spawn(
            name="otto-dm",
            prompt="/otto-dm",
            cwd=str(config.HOME),
            mode="headless",
            skip_permissions=True,
            domain=config.WORK,
            system_extra=extra,
            budget_usd=config.DM_BUDGET_USD or None,
            model=config.DM_MODEL or config.DEFAULT_MODEL or None,
        )
    except (ValueError, OSError) as e:
        return f"DM from {owner}: could not spawn: {e}"
    run.notes = ((run.notes or "") + f" | mode=dm | msgs={len(messages)}").strip(" |")
    store.upsert_run(run)
    return f"DM from {owner} ({len(messages)} msg), dispatched run {run.id[:6]}"
