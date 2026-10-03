"""Prove a Slack user token can do what the slack-dm producer will need.

Run this BEFORE anyone writes the producer. It answers the four questions that
decide whether the incremental-sweep design is possible at all, and it answers
them against the real workspace rather than the documentation:

  1. Is this a USER token (acting as the owner) or a bot token? A bot token can only
     see conversations the bot is a member of, so it cannot read the owner's 1:1 DMs
     with other humans. This is the mistake worth catching here rather than after
     the producer is built.
  2. Which scopes were actually granted? Slack silently issues a token with
     whatever was approved, so the granted set is the only set that matters.
  3. Can it enumerate the owner's DMs and group DMs?
  4. Can it read message history with an `oldest` cursor? That cursor IS the
     watermark, so without it there is no incremental sweep.

Never prints the token, and never prints message text. Counts and channel ids
only: the point is to prove capability, not to dump DMs into a terminal.

    <secrets-cli> run --with SLACK_USER_TOKEN=<user-token-id> -- python producers\\slack_dm_verify.py

Windows note: most secrets CLIs need a real executable after `--`, so the
`python <script>` form above is required. `-- producers\\slack_dm_verify.py` will not work.

Exit code 0 means every check passed and the producer is buildable as designed.
Stdlib only, so it runs anywhere without a venv.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://slack.com/api/"

# What the producer needs, and why. Checked against the granted set rather than
# assumed from the manifest, because the install is what decides.
NEEDED = {
    "im:read": "list the owner's 1:1 DM channels",
    "im:history": "read messages in those DMs",
    "mpim:read": "list group DMs",
    "mpim:history": "read messages in group DMs",
    "users:read": "turn user ids into names",
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


def call(token: str, method: str, **params) -> tuple[dict, dict]:
    """One Slack API call. Returns (body, response headers)."""
    url = API + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
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
    token = os.environ.get("SLACK_USER_TOKEN", "").strip()
    if not token:
        print("SLACK_USER_TOKEN is not set in this process's environment.\n")
        print("Inject it with your secrets CLI so the value never touches a file or "
              "your shell:")
        print("  <secrets-cli> run --with SLACK_USER_TOKEN=<user-token-id> -- "
              "python producers\\slack_dm_verify.py")
        return 2

    # Deliberately reports only the PREFIX, which is the token's type and not a
    # secret. xoxp = user token, xoxb = bot token. Never the value.
    prefix = token.split("-")[0] if "-" in token else "(no prefix)"

    print("\n-- token identity ------------------------------------------------------")
    body, headers = call(token, "auth.test")
    if not check("auth.test succeeds", bool(body.get("ok")), body.get("error", "")):
        print("\n  A token that cannot authenticate makes every later check moot.")
        return 1
    print(f"        workspace : {body.get('team')}")
    print(f"        acting as : {body.get('user')}  ({body.get('user_id')})")

    check("token is a USER token (xoxp), not a bot token", prefix == "xoxp",
          f"prefix is {prefix!r} - a bot token cannot read the owner's DMs")
    # A user token authenticates as a person; a bot authenticates as a bot_id.
    check("auth.test reports a human identity, not a bot",
          "bot_id" not in body, "response carries bot_id, so this is a bot install")

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
        print("        (Slack did not return X-OAuth-Scopes; relying on the calls below)")
    for scope, why in sorted(NEEDED.items()):
        # Absent header is not proof of absence, so this only fails when Slack
        # told us the set and this scope was not in it.
        check(f"scope {scope} granted ({why})", not granted or scope in granted)
    extra = granted - set(NEEDED) - {"identify"}
    if extra:
        print(f"  note  granted scopes beyond what the producer needs: "
              f"{', '.join(sorted(extra))}")
        print("        least privilege matters here - this token can read private DMs.")

    print("\n-- can it see the owner's DMs? -----------------------------------------")
    # ONE CALL PER TYPE, not types="im,mpim". Measured: the combined
    # query returned 200 group DMs before a single 1:1, so a bounded page never
    # reached the `im` conversations at all and this check reported "0 DMs visible"
    # against a token that could see 20. Slack does not interleave the types, and
    # nothing documents an order. The producer must split the query the same way.
    convos: dict[str, list[dict]] = {}
    for kind in ("im", "mpim"):
        body, _ = call(token, "conversations.list", types=kind, limit=200,
                       exclude_archived="true")
        if not check(f"conversations.list(types={kind}) succeeds", bool(body.get("ok")),
                     body.get("error", "")):
            return 1
        convos[kind] = body.get("channels") or []
        more = bool((body.get("response_metadata") or {}).get("next_cursor"))
        print(f"        {kind:5}: {len(convos[kind]):3} visible"
              + ("  (more pages exist - the producer must follow next_cursor)" if more else ""))

    check("at least one 1:1 DM is visible", bool(convos["im"]),
          f"{len(convos['im'])} found")
    # DMs with deactivated accounts are still listed forever. Worth surfacing so
    # the producer skips them rather than paging dead conversations every run.
    dead = [c for c in convos["im"] if c.get("is_user_deleted")]
    if dead:
        print(f"  note  {len(dead)} of {len(convos['im'])} DMs are with deactivated "
              f"users; the producer should skip these.")

    print("\n-- can it read history from a cursor? ---------------------------------")
    every = convos["im"] + convos["mpim"]
    if not every:
        check("history readable", False, "no conversation to test against")
        return 1

    # Find a conversation that HAS messages. The first version of this test used
    # whatever conversation came back first, hit an empty one, and reported a pass
    # having proved nothing: `oldest` trivially returns nothing when there is
    # nothing. An inconclusive check that reads as green is worse than a red one.
    target = newest = None
    for c in every[:60]:
        body, _ = call(token, "conversations.history", channel=c["id"], limit=1)
        if not body.get("ok"):
            continue
        msgs = body.get("messages") or []
        if msgs and msgs[0].get("ts"):
            target, newest = c, msgs[0]["ts"]
            break
    if not check("conversations.history returns messages from some conversation",
                 target is not None,
                 "no messages in the first 60 conversations - cannot prove the cursor"):
        return 1
    print(f"        testing against {target['id']} (is_im={bool(target.get('is_im'))}), "
          f"newest ts {newest}")

    # The whole incremental design rests on `oldest` working, so prove it two ways:
    # the filter must exclude everything at or before the newest message, AND the
    # same call without the filter must return more. One assertion alone would pass
    # against a channel that simply returns nothing.
    body, _ = call(token, "conversations.history", channel=target["id"],
                   oldest=newest, limit=5)
    after = len(body.get("messages") or []) if body.get("ok") else -1
    body, _ = call(token, "conversations.history", channel=target["id"], limit=5)
    unfiltered = len(body.get("messages") or []) if body.get("ok") else -1
    check("the `oldest` cursor excludes already-seen messages (this is the watermark)",
          0 <= after <= 1, f"expected 0 or 1 newer than newest, got {after}")
    check("the same call without `oldest` returns more, so the filter is real",
          unfiltered > after, f"unfiltered={unfiltered}, filtered={after}")

    print("\n-- name resolution ----------------------------------------------------")
    body, _ = call(token, "users.list", limit=1)
    check("users.list succeeds (needed to turn ids into names)",
          bool(body.get("ok")), body.get("error", ""))

    print()
    if FAIL:
        print(f"{len(FAIL)} check(s) FAILED:")
        for f in FAIL:
            print(f"  - {f}")
        print("\nFix the app's scopes or token type and re-run. Do not build the "
              "producer against a token that cannot do these four things.")
        return 1
    print("All checks passed. The token can enumerate DMs and read history from a "
          "cursor,\nwhich is everything the incremental slack-dm producer needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
