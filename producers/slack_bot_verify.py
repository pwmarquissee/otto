"""Prove Otto's bot token can send as itself, and can reach the owner for the rollup.

Run this after installing the Otto app and before any code depends on it. It
answers the questions that decide whether the outbound redesign works:

  1. Is this a BOT token (xoxb, posting as Otto) rather than the user token? If
     the wrong one is registered, every message still goes out as the owner and the
     whole point is lost - silently, because sending would still succeed.
  2. What is the sender's display name? This is what the whole company reads on every
     message, so it is worth reading back rather than assuming the manifest won.
  3. Can it resolve a person from a work email? Otto's people records key on
     email, not Slack id.
  4. Can it open a DM channel to the owner? That channel is where the rollup goes, so
     without it there is no reporting path.

BY DEFAULT THIS SENDS NOTHING. Opening a DM channel does not notify anyone and
posts no message. Pass --send-test to post one clearly-marked test message to
the owner's own DM (never to a channel, never to anyone else), which is the only way
to prove chat:write end to end. Inject the token with your secrets CLI:

    <secrets-cli> run --with SLACK_BOT_TOKEN=<id> -- python producers\\slack_bot_verify.py
    <secrets-cli> run --with SLACK_BOT_TOKEN=<id> -- python producers\\slack_bot_verify.py --send-test

The owner's work email comes from OTTO_OWNER_EMAIL, which must be set. Never prints
the token. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://slack.com/api/"

# The human whose DMs Otto reports into. Not a secret; it is the point of the tool.
# No default: a verifier that quietly resolved somebody else's address would prove
# the wrong thing, so an unset value is a clear error in main().
ROLLUP_EMAIL = os.environ.get("OTTO_OWNER_EMAIL", "").strip().lower()

NEEDED = {
    "chat:write": "post messages and thread replies",
    "im:write": "open a DM channel before messaging someone",
    # The group-DM design (Otto + owner + recipient) rests entirely on this one.
    # Cannot be proved functionally without creating a real group DM that appears
    # in a third person's sidebar, so the granted-scope check is the only
    # non-intrusive evidence there is. Do not drop it.
    "mpim:write": "open a group DM with the owner and the recipient",
    "users:read": "resolve a Slack user id",
    "users:read.email": "map a work email to a Slack user",
}

# Requesting this would let Otto post in any public channel without an invite.
# The design says channel access is an invite, so its presence is a finding.
UNWANTED = {
    "chat:write.public": "lets Otto post in public channels it was never invited to",
    "chat:write.customize": "lets Otto override its own name and icon per message",
}

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    # Detail is FAILURE evidence, so it prints only on failure. Printing it on
    # a pass made "scope chat:write.public NOT granted" render followed by
    # "granted - narrow it", which reads as the exact opposite of the result.
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok else ""))
    if not ok:
        FAIL.append(name)
    return ok


def call(token: str, method: str, post: dict | None = None, **params):
    url = API + method
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if post is not None:
        data = json.dumps(post).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (json.loads(r.read().decode("utf-8")),
                    {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        return ({"ok": False, "error": f"http_{e.code}"},
                {k.lower(): v for k, v in (e.headers or {}).items()})
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"network: {e.reason}"}, {}


def main() -> int:
    send_test = "--send-test" in sys.argv
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print("SLACK_BOT_TOKEN is not set in this process's environment.\n")
        print("Inject it with your secrets CLI so the value never touches a file or "
              "shell history:")
        print("  <secrets-cli> run --with SLACK_BOT_TOKEN=<credential-id> -- "
              "python producers\\slack_bot_verify.py")
        return 2
    if not ROLLUP_EMAIL:
        print("OTTO_OWNER_EMAIL is not set. It is the work email of the person Otto "
              "reports to,")
        print("and this verifier resolves it to a Slack user. Set it in .env and re-run.")
        return 2

    # Prefix only. xoxb = bot, xoxp = user. The type is not the secret.
    prefix = token.split("-")[0] if "-" in token else "(no prefix)"

    print("\n-- sender identity -----------------------------------------------------")
    body, headers = call(token, "auth.test")
    if not check("auth.test succeeds", bool(body.get("ok")), body.get("error", "")):
        return 1
    print(f"        workspace  : {body.get('team')}")
    print(f"        posts as   : {body.get('user')}  (bot_id {body.get('bot_id')})")

    check("token is a BOT token (xoxb), not the user token", prefix == "xoxb",
          f"prefix is {prefix!r} - a user token would still post as the owner")
    # The decisive test: a bot install carries bot_id. Without it this is a user
    # token and every message would go out under the owner's name.
    check("auth.test reports a bot identity", bool(body.get("bot_id")),
          "no bot_id, so this token posts as a human")

    print("\n-- granted scopes ------------------------------------------------------")
    # Lowercased keys: Slack sends `x-oauth-scopes`, and a plain dict() of the
    # headers is case-sensitive. Reading "X-OAuth-Scopes" off it silently
    # returned nothing, which made every scope check below pass vacuously
    # (`not granted` short-circuits the assertion). A check that cannot fail
    # is worse than no check: it reads green.
    granted = {s.strip() for s in (headers.get("x-oauth-scopes") or "").split(",") if s.strip()}
    if granted:
        print(f"        granted: {', '.join(sorted(granted))}")
    else:
        print("        (Slack returned no X-OAuth-Scopes header; relying on calls below)")
    for scope, why in sorted(NEEDED.items()):
        check(f"scope {scope} granted ({why})", not granted or scope in granted)
    for scope, why in sorted(UNWANTED.items()):
        check(f"scope {scope} NOT granted ({why})", scope not in granted,
              "granted - narrow it unless this was deliberate")
    # The bot reads only where Otto itself is, and both of these are expected:
    #   channels:history  the summon reaction in the help channel
    #   im:history        Otto's own DMs, so the owner can instruct it from a phone
    # On a BOT each reaches only conversations Otto is in; the same names on the
    # owner's user token would reach every public channel and every DM they have.
    #
    # This list has been stale twice, each time telling the owner to narrow a scope
    # that had just been deliberately added. Add the scope here in the same commit that adds it
    # to the manifest: a verifier that cries wolf is one people stop reading, which
    # costs more than the check was ever worth.
    expected_reads = {"channels:history", "im:history", "reactions:read"}
    reads = sorted(s for s in granted
                   if (s.endswith(":history") or s in {"channels:read", "groups:read",
                                                       "reactions:read"})
                   and s not in expected_reads)
    if reads:
        print(f"  note  unexpected read scopes on the bot ({', '.join(reads)}). "
              f"Otto reads only where it is; narrow this.")
    if "channels:history" in granted:
        print("  note  channels:history present and expected, bounded by channel "
              "membership:")
        print("        `/kick @Otto` in any channel Otto should not be able to read.")
    if "im:history" in granted:
        print("  note  im:history present and expected: Otto's OWN DMs only. Acting "
              "on the owner's")
        print("        messages alone is enforced in otto/inbox.py, not by this scope.")

    print("\n-- can it reach the owner for the rollup? ------------------------------")
    body, _ = call(token, "users.lookupByEmail", email=ROLLUP_EMAIL)
    ok = check(f"users.lookupByEmail resolves {ROLLUP_EMAIL}", bool(body.get("ok")),
               body.get("error", ""))
    if not ok:
        print("        Without this, Otto cannot find who to report to.")
        return 1
    uid = (body.get("user") or {}).get("id")
    print(f"        resolved to {uid}")

    # conversations.open is NOT a send: it returns (or creates) the DM channel and
    # notifies nobody. Safe to run unconditionally, and it is what proves im:write.
    body, _ = call(token, "conversations.open", post={"users": uid})
    ok = check("conversations.open returns a DM channel (proves im:write)",
               bool(body.get("ok")), body.get("error", ""))
    dm = ((body.get("channel") or {}).get("id")) if ok else None
    if dm:
        print(f"        rollup DM channel: {dm}")

    print("\n-- can it actually post? ----------------------------------------------")
    if not send_test:
        print("  skip  no message sent. chat:write is unproven until you pass "
              "--send-test,")
        print("        which posts ONE marked test message to the owner's own DM and "
              "nowhere else.")
    elif not dm:
        check("chat.postMessage", False, "no DM channel to post to")
    else:
        body, _ = call(token, "chat.postMessage", post={
            "channel": dm,
            "text": ":wrench: Otto bot token verification. This is a one-off test "
                    "message from producers/slack_bot_verify.py --send-test. "
                    "If you are reading it, chat:write works and Otto can send as "
                    "itself. Nothing else was posted.",
        })
        if check("chat.postMessage succeeds (posted as Otto, to the owner's DM only)",
                 bool(body.get("ok")), body.get("error", "")):
            print(f"        message ts {body.get('ts')} in {body.get('channel')}")
            print("        Check Slack: the sender should read 'Otto', not your own name.")

    print()
    if FAIL:
        print(f"{len(FAIL)} check(s) FAILED:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    if not send_test:
        print("All checks passed except chat:write, which is untested by design. "
              "Re-run with\n--send-test once you are ready for one message to land "
              "in your DMs.")
    else:
        print("All checks passed, including a real send. Otto can post as itself and "
              "reach you\nfor the rollup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
