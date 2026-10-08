"""Otto talking to somebody who is not its owner.

Every other capability in Otto acts on the owner's own state: the board, the calendar,
the machines, the ledger. This one acts on a COLLEAGUE, and that is a different kind of
thing. A wrong board card costs the owner ten seconds. A wrong Slack DM costs a teammate
their morning, or their trust in a channel that is supposed to be reliable, and no
amount of correcting it afterwards un-sends it.

So the whole module is an interlock, and every default is the closed position.

THE SHAPE (decided with the owner, 2026-08-05). Otto composes a message, records it, and
holds it for `OUTREACH_HOLD_MINUTES`. Silence sends it. One click kills it. This is
the only one of the four options that is actually autonomy AND actually reversible:

  draft-only        not autonomy. It converts a send into a task on the owner's list,
                    which is the thing they already have too many of.
  send immediately  no recall path. The failure lands on a colleague, and the owner
                    finds out when they reply.
  hold, then send   The owner does nothing in the normal case. The abnormal case has a
                    window, and the window is the whole feature.
  full autonomy     as above, minus the window.

WHAT IS ENFORCED HERE RATHER THAN IN A PROMPT. This matters more than the list itself.
Every rule below is Python that runs on the record before it is stored, because the
things composing these messages are models, and a model that has been told not to do
something is not the same as a system that cannot:

  ROSTER GATE       The recipient must be a person Otto already holds a dossier for.
                    Not "an address on the org domain" -- a row in the people
                    store. A typo'd address does not resolve, so it cannot be sent to.
  MEMBERS ONLY      `external` on the person record. Every outside thread (partners,
                    publishers, vendors) routes to the owner. Contractors, recognized
                    by the domains in CONTRACTOR_DOMAINS, are OFF by default and
                    switchable, because "internal" is genuinely ambiguous for them.
  DM BY DEFAULT     A DM reaches one person who can ignore it. A channel post reaches
                    everyone and cannot be unsent from anybody's memory. Channels
                    require an explicit allowlist.
  FORBIDDEN TOPICS  Comp, performance, offboarding, HR process, and live security
                    incidents. Not a capability question, a standing question: Otto
                    has none on any of it, and these arriving from the owner's account
                    without the owner having written them is its own harm. Checked against
                    the text, and a hit is refused rather than downgraded.
  RATE LIMITS       6 a day, 2 to any one person. Sized so a human never notices the
                    ceiling and a runaway producer cannot get past it.
  TIER 0 ONLY       A producer may mark a message tier 1+ to mean "the owner must
                    decide". Those are held and never auto-send, matching the tier
                    vocabulary the help-channel triage already uses.

THE MASTER SWITCH IS OFF. With `OUTREACH_ENABLED` false, everything above runs, the
record is written, the hold elapses, and the message EXPIRES instead of transmitting.
That is deliberate and it is the recommended way to start: a week of expired outreach
is a week of evidence about what Otto would have said, at zero risk to anybody, and
the switch is one env var when the evidence is boring.

HOW TRANSMISSION WORKS (owner decision, 2026-08-12, supersedes the 2026-08-06 revision).

Otto sends as OTTO, through a bot token, from `otto/slack.py`. No session, no model,
no cost.

This is the third shape and it resolves an argument the previous two had with each
other, so the history is worth keeping. The original design POSTed to chat.postMessage
with a token scoped to chat:write, and its argument was right: the narrowest action in
the system should not be executed by the broadest possible tool. It also never sent a
single message, because three defects stacked -- no token in the environment, the
master switch off, and an addressing bug underneath both (`target` holds an email
because the roster does, and chat.postMessage needs a channel or user id).

So 2026-08-06 replaced it with a spawned session on the Slack MCP connector, which
worked, and accepted two costs to get there: a session is a broader tool than a token,
and the connector is OAuth'd as THE OWNER, so every message arrived from them. That
second cost was written up at the time as a benefit -- replies land in the owner's
DMs, the thread reads like a conversation. In use it is the defect. A colleague cannot
tell an Otto nudge from the owner typing, and "sent by Claude" is a label people stop
seeing.

What changed is that the token now exists and is verified: an app with a `chat:write`
bot token and, deliberately, no read scope at all. That retires the honest caveat the
last revision ended on ("what the token had that this does not is enforcement by
somebody other than us; what this has that the token did not is that it works").
It now works AND is enforced by Slack. Specifically:

  * Otto's messages say Otto. Nobody is left guessing whether the owner wrote it.
  * A colleague DM is a GROUP conversation: Otto, the owner, and that person. The
    recipient can see who Otto works for, and the owner sees the REPLY rather than a report of what
    Otto said, and can correct it in the thread the person is reading. See
    config.OUTREACH_GROUP_DM.
  * The addressing bug stays fixed. slack.py resolves an email to a user id through
    users.lookupByEmail before opening anything, so the roster's email is still the
    field the whole module was written around.
  * A send costs no model tokens. The session it replaces measured $0.27 to call one
    tool with text it was forbidden to alter.
  * The token is checked out per send through the credential CLI (config
    CREDENTIAL_RUN_ARGV) and never enters the daemon's memory or any spawned
    session's environment.

The one thing this MOVED rather than kept: `slack._refuse_if_unattended` stops a
spawned session from POSTing as Otto directly, so every unattended send has to come
through the daemon, where the gates in this file run in Python before a record is
stored: the roster gate, members-only, forbidden subjects, rate limits, tier 0, and
the hold.

DIRECTED SENDS (owner decision, 2026-08-25). The hold and the forbidden-subject list
exist for messages Otto decides to send on its own. They are the wrong gate for a
message the owner asked for: "DM a colleague and me about the login problem" is the
owner's call, already made, and holding it ten minutes or refusing it because it says
"investigation" defeats the instruction. What happened without a door for that case:
twice in one day a board task reached for the Slack MCP connector instead, which is
OAuth'd as THE OWNER, and a colleague got two DMs "from the owner" that the owner
never typed. `directed()` is that door. It sends now, as Otto, into a group DM that
always includes the owner, keeps the roster gate (Otto still
cannot address a person it holds no dossier for), skips the hold and the subject
filter, and writes the same ledger record as everything else so the audit is in one
place. The guard denies the connector's DM tools in unattended sessions so the wrong
door is closed, not merely discouraged.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, launcher, people, slack
from .models import Outreach, iso, utcnow
from .runners import detached
from .store import Store

# The single MCP tool the sender is allowed to call. Everything else, built-in or
# otherwise, is removed from the session.
SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"

_FORBIDDEN = [re.compile(p, re.I) for p in config.OUTREACH_FORBIDDEN]


class Refused(RuntimeError):
    """An outreach that will not be recorded at all, with the reason why.

    Distinct from a killed or expired one on purpose. Those are messages that existed
    and were stopped; this is a message that was never legitimate, and the caller is
    told rather than silently handed a no-op.
    """


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------

def forbidden_hit(text: str) -> str | None:
    """The first forbidden subject this text touches, or None."""
    for r in _FORBIDDEN:
        m = r.search(text or "")
        if m:
            return m.group(0)
    return None


def _domain(login: str) -> str:
    """The domain part of a login, lowercased, or '' when there is none."""
    return login.rsplit("@", 1)[-1].strip().lower() if "@" in login else ""


def is_contractor(login: str) -> bool:
    """Whether a login sits on one of the configured contractor domains."""
    return _domain(login) in {d.strip().lower() for d in config.CONTRACTOR_DOMAINS if d}


def resolve(who: str) -> dict:
    """The roster row for an intended recipient, or raise.

    The gate, not a convenience. `who` has to resolve to somebody Otto already holds
    a dossier for, which rules out addresses that were guessed, hallucinated, or
    mistyped: they simply do not resolve, so there is nothing to send to.
    """
    p = people.get(who)
    if p is None:
        raise Refused(f"'{who}' is not in the people roster, so Otto cannot address them")
    if p.get("external"):
        raise Refused(
            f"{p.get('display_name')} is external. Every outside thread routes to the owner")
    login = str(p.get("login") or p.get("email") or "")
    org = {d.strip().lower() for d in config.ORG_DOMAINS if d}
    if not org:
        # No domains means no members, and the closed position is the only safe one:
        # an empty allowlist must never read as "allow everything".
        raise Refused(
            "OTTO_ORG_DOMAINS is not set, so Otto cannot tell who is a member of the "
            "org and refuses to address anyone")
    contractor = is_contractor(login)
    if contractor and not config.OUTREACH_ALLOW_CONTRACTORS:
        raise Refused(
            f"{p.get('display_name')} is a contractor ({login}). Set "
            "OTTO_OUTREACH_CONTRACTORS=1 to include them")
    if not contractor and _domain(login) not in org:
        raise Refused(
            f"{p.get('display_name')} has no login on an org domain ({login or 'none'}); "
            f"members are on {', '.join(sorted(org))}")
    return p


def sent_today(store: Store, now: datetime | None = None) -> list[Outreach]:
    """Outreach that actually reached somebody in the last 24h."""
    now = now or utcnow()
    cutoff = now - timedelta(hours=24)
    out = []
    for o in store.outreach():
        if o.state != "sent":
            continue
        try:
            when = datetime.fromisoformat((o.sent_at or o.at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.astimezone(timezone.utc) >= cutoff:
            out.append(o)
    return out


def _check_rate(store: Store, target: str, now: datetime | None = None) -> None:
    recent = sent_today(store, now)
    if len(recent) >= config.OUTREACH_MAX_PER_DAY:
        raise Refused(
            f"already sent {len(recent)} message(s) in 24h, cap is "
            f"{config.OUTREACH_MAX_PER_DAY}")
    same = [o for o in recent if o.target == target]
    if len(same) >= config.OUTREACH_MAX_PER_PERSON_DAY:
        raise Refused(
            f"already sent {len(same)} message(s) to this person in 24h, cap is "
            f"{config.OUTREACH_MAX_PER_PERSON_DAY}")


# ---------------------------------------------------------------------------
# composing
# ---------------------------------------------------------------------------

def compose(store: Store, *, to: str, body: str, why: str, source: str = "otto",
            tier: int = 0, channel: str = "slack-dm", run_id: str | None = None,
            task_id: str | None = None, hold_minutes: int | None = None,
            now: datetime | None = None) -> Outreach:
    """Record an outreach in `held` state. Raises `Refused` if a gate says no.

    Nothing is transmitted here. `deliver` does that, later, if the hold elapses.
    """
    now = now or utcnow()
    body = (body or "").strip()
    why = (why or "").strip()
    if not body:
        raise Refused("empty message")
    if not why:
        # The field the owner vetoes on. A message that cannot say why it is being sent
        # is one nobody can judge in ten seconds, which defeats the hold.
        raise Refused("every outreach needs a `why`: it is what the owner decides on")

    hit = forbidden_hit(f"{body} {why}")
    if hit:
        raise Refused(
            f"touches a subject Otto does not raise on its own initiative ('{hit}'). "
            "Write this one yourself")

    if channel == "slack-channel":
        if to not in config.OUTREACH_CHANNELS:
            raise Refused(
                f"channel '{to}' is not in OTTO_OUTREACH_CHANNELS. Otto sends DMs "
                "unattended, not channel posts")
        target, display, kind = to, to, "member"
    elif channel == "slack-dm":
        p = resolve(to)
        # Slack addressing is by user id or by email through the connector. The roster
        # holds the login, which IS the work email here, so that is what is stored
        # and what the sender resolves. Storing a raw U0... would make the ledger
        # unreadable for the one person who audits it.
        target = str(p.get("login") or p.get("email"))
        display = str(p.get("display_name") or target)
        kind = "contractor" if is_contractor(target) else "member"
    else:
        raise Refused(f"unknown channel '{channel}'")

    _check_rate(store, target, now)

    hold = config.OUTREACH_HOLD_MINUTES if hold_minutes is None else hold_minutes
    item = Outreach(
        channel=channel,  # type: ignore[arg-type]
        target=target, to=display, recipient_kind=kind,  # type: ignore[arg-type]
        body=body, why=why, source=source, tier=tier,
        hold_minutes=hold,
        send_after=iso(now + timedelta(minutes=hold)),
        at=iso(now), run_id=run_id, task_id=task_id,
    )
    store.put_outreach(item)
    store.log(f"outreach held for {display}: {body[:60]}", level="info",
              source="outreach")
    return item


# ---------------------------------------------------------------------------
# directed: the owner asked for this message, send it now as Otto
# ---------------------------------------------------------------------------

def directed(store: Store, *, to: list[str], body: str, why: str = "",
             source: str = "directed", run_id: str | None = None,
             task_id: str | None = None, now: datetime | None = None) -> Outreach:
    """Send a group DM (Otto, the owner, and `to`) right now, as Otto, and record it.

    For messages THE OWNER ASKED FOR, from a board card or a DM to Otto. No hold and
    no forbidden-subject filter: those gate Otto's own initiative, and this is theirs.
    What stays is the roster gate (`resolve`), so the recipient has to be a member of
    the org Otto already knows, and a per-run brake so a session that misreads an instruction
    cannot loop on one colleague. Raises `Refused` if a gate says no; a transport
    failure is recorded as `failed` on the returned record, not raised.
    """
    now = now or utcnow()
    body = (body or "").strip()
    if not body:
        raise Refused("empty message")
    owner = config.SLACK_OWNER_EMAIL.lower()
    if not owner:
        raise Refused("OTTO_SLACK_OWNER is not set, so Otto cannot open a group DM that "
                      "includes the owner")
    owner_key = owner.split("@")[0]
    others: list[str] = []
    for who in to:
        w = (who or "").strip()
        if not w:
            continue
        if w.lower() == owner or w.split("@")[0].lower() == owner_key:
            continue  # the owner is in every one of these conversations already
        if w not in others:
            others.append(w)
    if not others:
        raise Refused("nobody but the owner named; `otto tell` is the door for that")

    rows = [resolve(w) for w in others]
    emails: list[str] = []
    logins: list[str] = []
    names: list[str] = []
    for p in rows:
        login = str(p.get("login") or p.get("email") or "")
        email = str(p.get("email") or (login if "@" in login else ""))
        if not email:
            raise Refused(
                f"no work email recorded for {p.get('display_name') or login}, so "
                "Otto cannot open a conversation. Add one to the dossier first.")
        logins.append(login)
        emails.append(email)
        names.append(str(p.get("display_name") or login))
    target = ",".join(logins)
    display = " + ".join(names)
    kind = "contractor" if any(is_contractor(l) for l in logins) else "member"

    if run_id:
        same = [o for o in sent_today(store, now)
                if o.run_id == run_id and o.target == target]
        if len(same) >= config.DIRECTED_MAX_PER_RUN:
            raise Refused(
                f"this run already sent {len(same)} message(s) to {display}; cap is "
                f"{config.DIRECTED_MAX_PER_RUN} per run. Say what else you wanted to "
                "send in your final paragraph instead of sending it")

    item = Outreach(
        channel="slack-dm", target=target, to=display,
        recipient_kind=kind,  # type: ignore[arg-type]
        body=body, why=(why or "").strip() or "the owner asked for this message",
        source=source, tier=0, hold_minutes=0, send_after=iso(now), at=iso(now),
        run_id=run_id, task_id=task_id,
    )
    # The owner first, for the same reason as _send_as_otto: a stable member order keeps
    # Slack returning the same group conversation instead of opening a new one.
    members = [config.SLACK_OWNER_EMAIL] + emails
    ok, err = True, None
    try:
        slack.send(body, emails=members, timeout=config.OUTREACH_SEND_TIMEOUT)
    except slack.SlackError as e:
        ok, err = False, str(e)
    item.decided_at = iso(utcnow())
    item.decided_by = "owner"
    if ok:
        item.state = "sent"
        item.sent_at = iso(utcnow())
    else:
        item.state = "failed"
        item.error = err
    store.put_outreach(item)
    store.log(f"directed DM {item.state} to {display}: {body[:50]}"
              + (f" -- {err}" if err else ""),
              level="info" if ok else "warn", source="outreach")
    return item


# ---------------------------------------------------------------------------
# the hold expiring
# ---------------------------------------------------------------------------

def due(store: Store, now: datetime | None = None) -> list[Outreach]:
    """Held outreach whose window has closed."""
    now = now or utcnow()
    out = []
    for o in store.outreach():
        if o.state != "held":
            continue
        try:
            when = datetime.fromisoformat(o.send_after.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.astimezone(timezone.utc) <= now:
            out.append(o)
    return out


def kill(store: Store, oid: str, by: str = "owner") -> Outreach | None:
    o = store.get_outreach(oid)
    if o is None or o.state != "held":
        return None
    o.state = "killed"
    o.decided_at = iso(utcnow())
    o.decided_by = by  # type: ignore[assignment]
    store.put_outreach(o)
    store.log(f"outreach KILLED by {by}: {o.body[:60]}", level="info", source="outreach")
    return o


# The most a hold may be stretched to, measured from when the message was composed.
# A message held longer than a day is a draft nobody is deciding on, and the ledger
# should say "expired" or "killed" rather than carry a stale hold forever.
EXTEND_MAX_HOURS = 24


def extend(store: Store, oid: str, minutes: int = 10) -> Outreach | None:
    """The owner buying time on a held message without deciding yet.

    The hold is a deadline Otto set, and the dashboard shows it as a countdown.
    Before this the only moves were kill and send, so a message the owner wanted to think
    about for another ten minutes had to be killed and recomposed, which loses the
    record and the reason. Extending keeps both; `body` and `why` are untouched.
    """
    o = store.get_outreach(oid)
    if o is None or o.state != "held":
        return None
    if minutes <= 0:
        raise ValueError("extend by a positive number of minutes")
    now = utcnow()
    current = datetime.fromisoformat(o.send_after.replace("Z", "+00:00")).astimezone(timezone.utc)
    composed = datetime.fromisoformat(o.at.replace("Z", "+00:00")).astimezone(timezone.utc)
    new_after = max(now, current) + timedelta(minutes=minutes)
    cap = composed + timedelta(hours=EXTEND_MAX_HOURS)
    if new_after > cap:
        raise ValueError(
            f"a hold may not run past {EXTEND_MAX_HOURS}h from when the message was "
            f"composed ({o.at}); kill it or send it instead")
    o.send_after = iso(new_after)
    o.hold_minutes = int(round((new_after - composed).total_seconds() / 60))
    store.put_outreach(o)
    store.log(f"outreach hold extended {minutes}m by the owner, now sends at "
              f"{o.send_after}: {o.body[:60]}", level="info", source="outreach")
    return o


def send_now(store: Store, oid: str) -> Outreach | None:
    """The owner choosing not to wait out the hold."""
    o = store.get_outreach(oid)
    if o is None or o.state != "held":
        return None
    return _transmit(store, o, by="owner")


def settle(store: Store, now: datetime | None = None) -> list[str]:
    """Resolve every held outreach whose window has closed. The tick hook."""
    notes: list[str] = []
    for o in due(store, now):
        if o.tier > 0:
            # A producer said the owner must decide. The hold elapsing is not a decision,
            # so it stays held rather than becoming a send by timeout.
            continue
        notes.append(_settle_one(store, o))
    return notes


def _settle_one(store: Store, o: Outreach) -> str:
    if not config.OUTREACH_ENABLED:
        o.state = "expired"
        o.decided_at = iso(utcnow())
        o.decided_by = "otto"
        o.error = "outreach disabled (OTTO_OUTREACH=0), never transmitted"
        store.put_outreach(o)
        store.log(f"outreach EXPIRED unsent (disabled): {o.body[:60]}",
                  level="info", source="outreach")
        return f"outreach expired unsent, sending is off: {o.to} / {o.body[:40]}"
    # Re-checked at the moment of sending, not only at compose time. A day-old held
    # message whose recipient has since been offboarded must not go out because the
    # gate passed yesterday.
    try:
        if o.channel == "slack-dm":
            resolve(o.target)
        _check_rate(store, o.target)
    except Refused as e:
        o.state = "killed"
        o.decided_at = iso(utcnow())
        o.decided_by = "otto"
        o.error = f"refused at send time: {e}"
        store.put_outreach(o)
        return f"outreach dropped at send time ({e}): {o.to}"
    _transmit(store, o, by="timer")
    return (f"outreach {'sent' if o.state == 'sent' else 'FAILED'} to {o.to}: "
            f"{o.body[:40]}")


def _send_as_otto(o: Outreach) -> tuple[bool, str | None]:
    """Transmit as Otto, via the bot token. No session, no model, no cost.

    What changed and why (owner decision, 2026-08-12). This used to be a spawned Claude
    session calling the Slack MCP connector, which is OAuth'd as the owner: the message
    arrived FROM THE OWNER and a colleague could not tell an Otto nudge from the owner
    typing. It also
    cost about $0.27 a send to have a model call one tool with text it was forbidden
    to alter.

    A DM to a colleague is opened as a GROUP conversation containing Otto, the owner,
    and that person. The recipient can see who Otto works for, and the owner sees their reply
    rather than a report of what Otto said. A channel target is posted to directly:
    a channel already has an audience, so there is nobody to add.

    Addressing still fails closed, for the same reason as before: Otto messages a
    person Otto already holds a dossier for, by an id somebody recorded, and a model
    is never in a position to supply one.
    """
    if o.channel == "slack-dm":
        who = resolve(o.target)
        email = (who or {}).get("email") or (o.target if "@" in o.target else None)
        if not email:
            return False, (
                f"no work email recorded for {o.target}, so Otto cannot open a "
                f"conversation. Add one to the dossier, or run "
                f"`otto outreach resolve {o.target}`.")
        # The owner first: they are the constant in every one of these conversations, and a
        # stable member order keeps Slack returning the SAME group DM per person
        # instead of opening a new one each time.
        if config.OUTREACH_GROUP_DM and not config.SLACK_OWNER_EMAIL:
            return False, ("OTTO_SLACK_OWNER is not set, so Otto cannot open the group DM "
                           "that includes the owner; nothing was sent")
        people = [config.SLACK_OWNER_EMAIL, email] if config.OUTREACH_GROUP_DM else [email]
        try:
            _, _ = slack.send(o.body, emails=people,
                              timeout=config.OUTREACH_SEND_TIMEOUT)
        except slack.SlackError as e:
            return False, str(e)
        return True, None

    try:
        _, _ = slack.send(o.body, channel=o.target,
                          timeout=config.OUTREACH_SEND_TIMEOUT)
    except slack.SlackError as e:
        return False, str(e)
    return True, None


def _transmit(store: Store, o: Outreach, by: str) -> Outreach:
    ok, err = _send_as_otto(o)
    o.decided_at = iso(utcnow())
    o.decided_by = by  # type: ignore[assignment]
    if ok:
        o.state = "sent"
        o.sent_at = iso(utcnow())
        o.error = None
    else:
        # NOT retried. A send that failed for a real reason will fail again, and a
        # retry loop on a channel that reaches colleagues is the last place to put one.
        o.state = "failed"
        o.error = err
    store.put_outreach(o)
    store.log(f"outreach {o.state} ({by}) to {o.to}: {o.body[:50]}"
              + (f" -- {err}" if err else ""),
              level="info" if ok else "warn", source="outreach")
    return o


SEND_PROMPT = """Send this Slack direct message. That is the entire task.

channel_id:  {target}
MESSAGE:
{body}

Call {tool} once with channel_id set to EXACTLY the literal string
above, and the message text EXACTLY as written. The channel_id has already been
resolved for you: do not convert it, look it up, "correct" it, or substitute anything
for it. Do not rewrite the message, summarize it, add a greeting, add a sign-off, or
explain yourself to the recipient. The owner approved those words and that recipient, and
no others.

Do not send anything to anyone else. Do not send a second message. If the send
fails, say so plainly and stop; do not try another channel or another recipient.

Then reply with one short line saying whether it went.
"""


def _send_launcher(oid: str, prompt_file: Path) -> Path:
    """A session that can do exactly one thing.

    THE FLAGS HERE ARE THE WHOLE SAFETY ARGUMENT, AND TWO OBVIOUS ONES ARE WRONG.
    Verified empirically on 2026-08-06 rather than reasoned about, because the first
    two attempts both looked correct and both left the session fully capable:

      --dangerously-skip-permissions   MUST NOT be set. It overrides the allow-list
                                       entirely. Measured: a session with it plus an
                                       allow-list still had all 236 tools usable,
                                       including mail, files, identity, RMM and
                                       EDR. Every other spawner in Otto sets this;
                                       this one must not, and that is the point.
      --tools ""                       MUST NOT be set. It looks like the tightest
                                       possible bound (it does remove the 37 built-in
                                       tools) and it breaks permission matching for
                                       MCP tools: with it, the ALLOWED tool was denied
                                       too, so the sender could not send at all.

    What actually works, confirmed in both directions: no skip-permissions, no --tools,
    and `--allowed-tools` naming the one MCP tool. The allowed tool runs; anything else
    lands in permission_denials and returns "you haven't granted it yet". The built-in
    tools are still listed in the session, but they are not allow-listed, so they are
    denied the same way.

    If you add skip-permissions here to fix some future prompt, you have silently
    removed the bound and handed a headless session every credential on the box.
    """
    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--allowed-tools", SEND_TOOL]
    if config.OUTREACH_MODEL:
        args += ["--model", config.OUTREACH_MODEL]
    if config.OUTREACH_BUDGET_USD:
        args += ["--max-budget-usd", str(config.OUTREACH_BUDGET_USD)]
    return launcher.write_claude(
        config.LOG_DIR / f"outreach-{oid[:8]}",
        launcher.ClaudeSpec(cwd=str(config.OTTO_HOME), prompt_file=prompt_file, args=tuple(args)))


def _tool_was_used(stream: str) -> bool:
    """True only if the session actually invoked the send tool.

    The whole point. A session that says "message sent!" without calling anything is
    the failure mode that matters here, because it would mark an outreach `sent` in
    the ledger while the colleague received nothing, and the ledger is the thing the
    owner audits. Prose is not evidence; a tool_use event is.
    """
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "assistant":
            continue
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") == SEND_TOOL:
                return True
    return False


def _result_error(stream: str) -> str | None:
    """The session's own verdict, or None if it finished cleanly."""
    last = None
    for line in stream.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "result":
                last = obj
    if last is None:
        return "the send session produced no result"
    if last.get("is_error"):
        return (f"send session errored: "
                f"{last.get('subtype') or last.get('result') or 'unknown'}")
    return None


LOOKUP_TOOL = "mcp__claude_ai_Slack__slack_search_users"

_SLACK_ID = re.compile(r"\b([UW][A-Z0-9]{6,})\b")


def slack_id_for(target: str) -> str | None:
    """The Slack member id recorded for this address, or None.

    Read from the dossier, never guessed. `target` is an email because the roster is,
    and `resolve_slack_id` is what puts an id next to it.
    """
    p = people.get(target)
    if not p:
        return None
    got = str((p.get("meta") or {}).get("slack_id") or "").strip()
    return got or None


def resolve_slack_id(target: str) -> tuple[str | None, str | None]:
    """Look an email up in Slack and return (member_id, error). Cannot send anything.

    A SEPARATE session from the sender, and the split is the safety property: this one
    is allow-listed to the search tool and cannot message anybody, the sender is
    allow-listed to the send tool and cannot look anything up. Neither can do the
    other's job, so a confused lookup cannot become a misdirected message.

    The id is extracted from the tool RESULT by regex in Python, not read out of the
    model's prose, for the same reason `_tool_was_used` exists: what the session says
    it found is not evidence of what Slack returned.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-z0-9]+", "-", target.lower())[:32]
    prompt_file = config.LOG_DIR / f"slackid-{stem}.prompt.txt"
    prompt_file.write_text(
        f"Call {LOOKUP_TOOL} to find the Slack user whose email is exactly "
        f"{target}.\nReport the member id (U...) and the email on the profile you "
        f"found. Do not message anyone. If there is no exact email match, say so.\n",
        encoding="utf-8")

    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--allowed-tools", LOOKUP_TOOL]
    if config.OUTREACH_MODEL:
        args += ["--model", config.OUTREACH_MODEL]
    args += ["--max-budget-usd", str(config.OUTREACH_BUDGET_USD)]
    script = launcher.write_claude(
        config.LOG_DIR / f"slackid-{stem}",
        launcher.ClaudeSpec(cwd=str(config.OTTO_HOME), prompt_file=prompt_file, args=tuple(args)))

    try:
        proc = subprocess.run(
            launcher.command(script),
            cwd=str(config.OTTO_HOME), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=config.OUTREACH_SEND_TIMEOUT,
            creationflags=detached._FLAGS_HEADLESS,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"slack lookup failed: {e}"

    stream = (proc.stdout or "") + "\n" + (proc.stderr or "")
    (config.LOG_DIR / f"slackid-{stem}.log").write_text(stream, encoding="utf-8")

    # Only tool RESULTS are trusted, and only ones that also carry the email asked
    # for. A search can return several people; an id sitting next to the wrong
    # address is exactly the mistake this whole function exists to prevent.
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "user":
            continue
        for block in (obj.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = str(block.get("content"))
            if target.lower() not in text.lower():
                continue
            hit = _SLACK_ID.search(text)
            if hit:
                return hit.group(1), None
    return None, f"no Slack member id found for {target}"


def _send_via_connector(o: Outreach) -> tuple[bool, str | None]:
    """Transmit through the Slack MCP connector. Synchronous by necessity.

    `_transmit` has to know whether the message went before it writes the ledger, so
    this blocks. It is called at most OUTREACH_MAX_PER_DAY times a day and is bounded
    by OUTREACH_SEND_TIMEOUT, which is the trade accepted rather than building an
    async state machine for six messages.
    """
    # FAIL CLOSED ON ADDRESSING. Slack does not accept an email as a message target;
    # verified 2026-08-06, the connector returns channel_not_found exactly as the bot
    # token would. The original code comment claimed the connector took an email and
    # it does not, so this was wrong on both transports.
    #
    # Without a recorded id the model is the only thing that could supply one, and a
    # model guessing a Slack id is a message to the wrong colleague. There is no safe
    # default here, so there is no default: refuse, and say how to fix it.
    if o.channel == "slack-dm":
        sid = slack_id_for(o.target)
        if not sid:
            return False, (
                f"no Slack member id recorded for {o.target}. Slack will not accept an "
                f"email as a target, and Otto will not let a model guess one. "
                f"Run `otto outreach resolve {o.target}` first.")
    else:
        sid = o.target  # a channel id, already addressable

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    oid = o.id or uuid.uuid4().hex
    prompt_file = config.LOG_DIR / f"outreach-{oid[:8]}.prompt.txt"
    prompt_file.write_text(
        SEND_PROMPT.format(target=sid, body=o.body, tool=SEND_TOOL),
        encoding="utf-8")

    cmd = launcher.command(_send_launcher(oid, prompt_file))
    try:
        proc = subprocess.run(
            cmd, cwd=str(config.OTTO_HOME), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=config.OUTREACH_SEND_TIMEOUT,
            creationflags=detached._FLAGS_HEADLESS,
        )
    except subprocess.TimeoutExpired:
        # Unknown outcome, not a known failure: the session may have sent before it
        # hung. Say exactly that rather than picking a verdict, and never retry.
        return False, (f"send session timed out after {config.OUTREACH_SEND_TIMEOUT}s; "
                       "whether Slack received it is UNKNOWN, check the DM before resending")
    except OSError as e:
        return False, f"could not start the send session: {e}"

    stream = (proc.stdout or "") + "\n" + (proc.stderr or "")
    (config.LOG_DIR / f"outreach-{oid[:8]}.log").write_text(stream, encoding="utf-8")

    err = _result_error(stream)
    if not _tool_was_used(stream):
        return False, (err or "the send session never called the Slack tool, "
                              "so nothing was transmitted")
    if err:
        return False, err
    return True, None


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def held(store: Store) -> list[Outreach]:
    return [o for o in store.outreach() if o.state == "held"]


def summary(store: Store) -> dict:
    items = store.outreach()
    counts: dict[str, int] = {}
    for o in items:
        counts[o.state] = counts.get(o.state, 0) + 1
    return {
        "enabled": config.OUTREACH_ENABLED,
        "hold_minutes": config.OUTREACH_HOLD_MINUTES,
        "held": counts.get("held", 0),
        "sent_24h": len(sent_today(store)),
        "max_per_day": config.OUTREACH_MAX_PER_DAY,
        "counts": counts,
        # No credential to check any more: the connector is authenticated inside the
        # spawned session, not by an env var here. So the master switch IS the whole
        # answer, and a `claude` that has gone missing surfaces as a failed send with
        # its reason rather than as a pre-flight guess made from this process.
        "can_send": bool(config.OUTREACH_ENABLED),
    }


def render(store: Store, limit: int = 12) -> str:
    s = summary(store)
    lines = ["outreach", ""]
    state = ("ON" if s["enabled"] else "OFF (composed and held, never transmitted)")
    lines.append(f"  sending: {state}")
    if not s["enabled"]:
        # The switch does NOT gate `send_now`. The owner clicking send is a decision and
        # goes out regardless, so a banner implying nothing can leave would be a lie
        # about the one button he actually presses.
        lines.append("  (the timer will not send; your own Send button still will)")
    lines.append(f"  hold: {s['hold_minutes']} min   "
                 f"sent in 24h: {s['sent_24h']}/{s['max_per_day']}")
    lines.append("")
    items = store.outreach()[:limit]
    if not items:
        lines.append("  nothing. Otto has not composed a message to anybody.")
        return "\n".join(lines) + "\n"
    for o in items:
        mark = {"held": "HELD", "sent": "sent", "killed": "kill",
                "expired": "exp ", "failed": "FAIL"}.get(o.state, o.state[:4])
        lines.append(f"  {o.id[:6]}  {mark}  {o.to[:24]:<24} {o.body[:52]}")
        lines.append(f"          why: {o.why[:78]}")
        if o.state == "held":
            lines.append(f"          sends at {o.send_after[11:16]}Z  ->  "
                         f"otto outreach kill {o.id[:6]}")
        if o.error:
            lines.append(f"          {o.error[:78]}")
    return "\n".join(lines) + "\n"
