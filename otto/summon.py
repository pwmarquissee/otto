"""Noticing that somebody asked Otto for help, and acting within the minute.

Otto is silent in the help channel unless summoned: a message carrying the summon
reaction is a request to answer it, and a message without one is not. That rule was
decided on 2026-08-12 after an automation answered a teammate 49 seconds after she
asked and 50 seconds before the owner offered to take it to a call.

TWO THINGS THIS FIXES, AND THE SECOND MATTERS MORE

Latency. Until now the summon was noticed by the four-hourly sweep, so "Otto, help"
could sit for hours. The daemon polls one channel on its tick instead, so the answer
starts within a minute.

Enforcement. Until now "only answer when summoned" was PROSE IN A COMMAND FILE. A
model read it and was trusted to obey, which made it the weakest interlock in Otto:
every other one (roster gate, forbidden subjects, rate limits, channel allowlist,
thread-required) is Python that cannot be talked out of. Now the daemon finds the
reaction and hands the session one specific thread. The session no longer decides
whether it was summoned, because it is never asked to.

WHY THE BOT TOKEN READS THIS AND NOT THE OWNER'S

`channels:history` on the owner's user token would read every public channel in the
workspace. The same scope on the bot reads only channels the bot was invited to,
because a bot cannot see a channel it is not in. So the reachable surface is bounded
by `/invite @Otto` rather than by the scope name, and it is revoked with `/kick`.
"""

from __future__ import annotations

from . import config, slack
from .models import iso, utcnow
from .store import Store

# State key under the daemon's feed-state store. Not a new file: this is a handful
# of message ids and belongs with the other per-source bookkeeping.
STATE = "help-channel-summons"

# How many message ids to remember. Comfortably more than a channel produces between
# polls, and bounded so the record cannot grow without limit.
REMEMBER = 200


def _handled(store: Store) -> dict[str, int]:
    """ts -> the summon reaction count when it was last acted on.

    A COUNT, not a bare list of ids, because "already answered" and "asked again"
    have to be tellable apart. The first version stored ids and made re-summoning
    impossible: once a thread was in the list it could never trigger again, so the
    only way to get a second pass was to edit state by hand.
    """
    raw = store.feed_state(STATE).get("handled") or {}
    if isinstance(raw, list):
        # Migrate the id-only shape from the first version. Count 1 is right: those
        # were single reactions, and it means removing and re-adding re-triggers.
        return {ts: 1 for ts in raw}
    return {str(k): int(v) for k, v in raw.items()}


def _count(message: dict, emoji: str) -> int:
    for r in (message.get("reactions") or []):
        if r.get("name") == emoji:
            return int(r.get("count") or 0)
    return 0


def summoned(messages: list[dict], emoji: str) -> list[dict]:
    """Messages carrying the summon reaction, oldest first.

    Anyone may summon, not just the owner: it is the help channel, and a member
    reacting on their own question is asking for help. Reading WHO reacted would need
    no extra scope, so this is a policy choice rather than a limitation.
    """
    out = []
    for m in messages:
        if m.get("subtype") or not m.get("ts"):
            continue
        for r in (m.get("reactions") or []):
            if r.get("name") == emoji:
                out.append(m)
                break
    out.sort(key=lambda m: float(m.get("ts") or 0))
    return out


def poll(store: Store) -> list[str]:
    """Look for new summons and dispatch one session each. Returns log notes.

    Called from the daemon tick. Every failure path returns a note rather than
    raising: a Slack blip must not take the tick down with it.
    """
    if not config.SUMMON_POLL:
        return []  # switched off on purpose: silence is the configured behavior
    channel = config.HELP_CHANNEL
    # The summon dispatches a slash command that answers one support thread. Which
    # command is a per-deployment choice; an unset value means the watcher has
    # nothing to hand a summon to and stays idle.
    prompt = config.SUMMON_PROMPT
    if not channel:
        return _once(store, "summon watcher idle: OTTO_HELP_CHANNEL is not set")
    if not prompt:
        return _once(store, "summon watcher idle: OTTO_SUMMON_PROMPT is not set")

    try:
        messages = slack.read_channel(channel, limit=config.SUMMON_SCAN)
    except slack.SlackError as e:
        # Missing scope is the expected failure until the app is reinstalled, and it
        # would otherwise repeat every tick forever. Say it once per change.
        note = f"summon poll failed: {e}"
        if store.feed_state(STATE).get("last_error") == str(e):
            return []
        store.put_feed_state(STATE, {"last_error": str(e), "checked_at": iso(utcnow())})
        return [note]

    emoji = config.SLACK_SUMMON_EMOJI
    handled = _handled(store)
    carrying = {m["ts"]: m for m in summoned(messages, emoji)}

    # Forget any thread that no longer carries the reaction, so REMOVING AND
    # RE-ADDING it is how a human asks for another pass. That gesture is the only
    # re-summon Slack makes available: one person cannot add the same emoji twice,
    # so a count bump only happens when somebody else joins in.
    #
    # Scoped to the messages this poll actually saw. Pruning anything absent from the
    # scan window would forget old threads purely because they scrolled away, and
    # then re-answer them the moment a poll reached back far enough.
    seen = {m["ts"] for m in messages if m.get("ts")}
    handled = {ts: n for ts, n in handled.items()
               if ts in carrying or ts not in seen}

    fresh: list[tuple[dict, bool]] = []
    for ts, m in carrying.items():
        was = handled.get(ts)
        now = _count(m, emoji)
        if was is None:
            fresh.append((m, False))          # never answered
        elif now > was:
            fresh.append((m, True))           # somebody else asked too
    # else: same reaction, already answered. Silence.

    state: dict = {"last_error": None, "checked_at": iso(utcnow())}
    notes: list[str] = []

    if fresh:
        # Mark handled BEFORE dispatching. A crash between the two costs one
        # unanswered summon, which somebody can see and re-react to. Marking after
        # would risk answering the same thread on every tick until it succeeded,
        # which is a loop that posts to a channel.
        for m, _ in fresh:
            handled[m["ts"]] = _count(m, emoji)
        state["handled"] = dict(list(handled.items())[-REMEMBER:])
        store.put_feed_state(STATE, state)
        for m, again in fresh:
            notes.append(_dispatch(store, channel, m, prompt, again=again))
        return notes

    state["handled"] = handled
    store.put_feed_state(STATE, state)
    return []


def _once(store: Store, note: str) -> list[str]:
    """Log a condition the first time it appears, not once a minute forever."""
    if store.feed_state(STATE).get("last_error") == note:
        return []
    store.put_feed_state(STATE, {"last_error": note, "checked_at": iso(utcnow())})
    return [note]


def _dispatch(store: Store, channel: str, message: dict, prompt: str,
              again: bool = False) -> str:
    """Run the triage against ONE thread, told plainly that it was summoned."""
    from .runners import detached

    ts = message["ts"]
    owner = config.OWNER_NAME
    extra = (
        "SUMMONED. Otto's daemon saw the summon reaction on exactly one message and "
        f"is handing you that message and nothing else.\n\n"
        f"  channel:   {channel}\n"
        f"  thread_ts: {ts}\n"
        f"  pass:      {'REPEAT - somebody asked again' if again else 'first'}\n\n"
        "Do not scan the channel and do not look for other summons: whether this "
        "message was summoned has already been decided, in Python, by the process "
        "that started you.\n\n"
        # The correction that matters. The command's scope filters exist to stop Otto
        # butting into a thread the owner is handling, and they are exactly wrong here:
        # on 2026-08-13 a summon on a live ticket was answered with "no action needed"
        # because the owner had replied six times, when they had replied six times and
        # were STILL STUCK, which is why they asked.
        f"A SUMMON OVERRIDES THE SKIP RULES. Ignore '{owner} has already replied', "
        "'a teammate already solved it', and the age of the thread. Somebody asked "
        f"for you on purpose. {owner} being in the thread is the normal case for a summon, "
        "not a reason to stand down, and 'you are already here so I did nothing' is "
        "never a useful answer.\n\n"
        "What is wanted is a SECOND PASS, not a first answer:\n"
        "  * Read the whole thread, including what has already been tried.\n"
        "  * If the problem looks resolved, say so plainly and say what you checked "
        "to believe it. Confirmation is a real answer.\n"
        "  * If it is not resolved, offer something that has NOT been tried yet. Do "
        "not restate a suggestion already in the thread.\n"
        "  * If the last thing in the thread is a person waiting on somebody, say "
        "who is waiting on what.\n"
        f"  * If you genuinely have nothing to add, `otto tell` {owner} that and say why "
        "in one line. Do not post an empty acknowledgement into the channel.\n\n"
        "The tier rules still apply: a summon is permission to SPEAK, not permission "
        f"to act. Tier-1 and tier-2 still go to {owner} via `otto tell`."
    )
    if again:
        extra += (
            "\n\nThis is a REPEAT summon: you or another automation has answered this "
            "thread before. Read your own previous reply and do not repeat it. Either "
            "the earlier answer did not work, or somebody wants it re-checked."
        )
    try:
        run = detached.spawn(
            name="help-summon",
            prompt=prompt,
            cwd=str(config.HOME),
            mode="headless",
            skip_permissions=True,
            domain=config.WORK,
            system_extra=extra,
            budget_usd=config.SUMMON_BUDGET_USD or None,
            model=config.SUMMON_MODEL or config.DEFAULT_MODEL or None,
        )
    except (ValueError, OSError) as e:
        return f"summon on {ts}: could not spawn: {e}"
    run.notes = ((run.notes or "") + f" | mode=summon | thread={ts}").strip(" |")
    store.upsert_run(run)
    return f"summoned on {ts}, dispatched run {run.id[:6]}"
