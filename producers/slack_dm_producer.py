"""Fetch new Slack DMs since a watermark. No model, no judgment, no cost.

WHAT THIS REPLACES, AND WHY IT IS WORTH A SCRIPT

`/slack-sweep` was the most expensive thing Otto ran. Measured: about
$1.03 to $1.91 a run, 22 to 26 turns, roughly 680k input tokens, because a Claude
session paginated a rolling 24-hour search on every run and re-read the whole window
from scratch. Almost none of that was classification. It was retrieval, done by the
most expensive tool available, over and over, on the same messages.

Retrieval is not a judgment call. `conversations.history` with an `oldest` cursor is
exact, and a watermark is a number. So retrieval moves here, where it costs nothing,
and the session that follows only has to answer the one question that genuinely needs
a model: is this message a request, a commitment, or an open question aimed at the
owner.

WHY THIS CANNOT RUN INSIDE THE SWEEP SESSION

`scripts/otto_guard.py` gates credential checkout in any unattended session, so a
headless Claude run cannot obtain the token. That is the guard working, not an
obstacle to route around: a session that could check out this credential could read
every DM the owner has. The daemon runs this instead, and the session reads the file.

WHAT IT WRITES

A spool file (not a feed drop). The feed envelope wants classified output - items and
tasks - and this script deliberately makes no such claim. It reports what arrived.

    %USERPROFILE%\\.claude\\otto\\slack-spool\\new.json

Besides `messages`, the spool carries `contacts`: per-conversation direction facts
for 1:1 DMs (when the peer last wrote, when the owner last wrote). Facts only - whether an
exchange counts as "contact" is policy, and policy lives with the consumer
(otto/people.py, which turns these into dossier last_contact dates).

THE SPOOL IS A BUFFER, NOT A MAILBOX SLOT

Each run MERGES the previous spool's messages forward instead of overwriting them.
Found the hard way: this ran hourly while /slack-sweep read the spool every four
hours, and an overwrite dropped up to three of every four spools - fetched past the
watermark, so gone from the pipeline entirely, silently. The watermark comment below
says "losing messages silently is the one failure this design must not have"; the
overwrite was that failure, one file downstream.

Carried messages expire after RETAIN_HOURS (26h), COUNTED AND LOGGED, never silent.
The sweep may therefore see a message in more than one spool; re-classifying one it
already filed is dedup's job downstream and costs cents, while a dropped message
costs a task nobody ever sees. Rows carry `fetched_at` (epoch of the run that
fetched them) so expiry is measured from fetch, not from message time.

Run it with the token injected by your secrets CLI so it never touches a file, a
shell history, or argv:

    <secrets-cli> run --with SLACK_USER_TOKEN=<user-token-id> -- python producers\\slack_dm_producer.py

Stdlib only. Imports nothing from Otto: this is a producer, and producers do not get
to depend on the thing they feed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://slack.com/api/"

HOME = pathlib.Path(os.environ.get("USERPROFILE") or pathlib.Path.home())
SPOOL_DIR = pathlib.Path(os.environ.get("OTTO_SLACK_SPOOL",
                                        HOME / ".claude" / "otto" / "slack-spool"))
STATE_PATH = SPOOL_DIR / "watermarks.json"
OUT_PATH = SPOOL_DIR / "new.json"

# First run has no watermark. Do NOT fetch all history: a five-year DM is not news,
# and pulling it would spool tens of thousands of messages for a classifier to read.
BOOTSTRAP_HOURS = float(os.environ.get("OTTO_SLACK_BOOTSTRAP_HOURS", "24"))

# A few years at one company leaves a user with hundreds of DMs and group DMs. At
# Slack's rate limit a sweep of all of them is tens of minutes, and nearly all of it is
# spent confirming that years-old conversations are still quiet. So a conversation
# that comes back empty COLD_AFTER times in a
# row drops to being checked every COLD_EVERY runs.
#
# Keyed on CONSECUTIVE EMPTY FETCHES, not on how old the last message is. The first
# version used message age and was wrong in a way that quietly did nothing: a
# conversation with no messages in the window never gets a last-message time recorded
# at all, so it stayed "unknown", stayed due, and was polled every single run forever.
# The dormant conversations that the tiering exists to skip were exactly the ones it
# could never skip. An empty streak is self-correcting: it needs no knowledge of true
# last activity, and one real message resets it.
COLD_AFTER = int(os.environ.get("OTTO_SLACK_COLD_AFTER", "3"))
COLD_EVERY = int(os.environ.get("OTTO_SLACK_COLD_EVERY", "6"))

# Gap between calls, PER METHOD, because Slack's rate limits are per method and not
# per app. conversations.history is Tier 3 (about 50/min); conversations.list and
# users.list are Tier 2 (about 20/min), which is two and a half times slower.
#
# Got this wrong twice, in the same way both times. First at 0.12s for everything,
# roughly ten times the fastest allowance. Then at 1.25s for everything, which is
# right for history and still more than twice too fast for the list calls, so it kept
# 429ing on exactly the calls that run first.
#
# Neither failed loudly. Slack answers a 429 with a Retry-After of ten seconds, so
# going too fast is SLOWER than pacing correctly, and the only symptom is a log line
# that reads like weather. A retry handler is not a rate limit.
TIER2 = float(os.environ.get("OTTO_SLACK_PAUSE_LIST", "3.2"))    # ~19/min
TIER3 = float(os.environ.get("OTTO_SLACK_PAUSE_HISTORY", "1.3"))  # ~46/min
PAUSE_FOR = {
    "conversations.list": TIER2,
    "users.list": TIER2,
    "conversations.history": TIER3,
}

# Messages per conversation per run. A thread that exploded overnight should not be
# able to spool ten thousand lines into a classifier's context.
MAX_PER_CONVO = int(os.environ.get("OTTO_SLACK_MAX_PER_CONVO", "40"))

# How long an unswept message rides forward in the spool before it expires (see THE
# SPOOL IS A BUFFER above). A day and change: the slack-dm feed alarms stale at 9h,
# so by the time anything actually expires here, the broken sweep has been visibly
# broken for most of a day. Expiry is logged with a count, never silent.
RETAIN_HOURS = float(os.environ.get("OTTO_SLACK_SPOOL_RETAIN_HOURS", "26"))


def log(msg: str) -> None:
    """Progress to stderr. stdout stays machine-readable, and NEITHER carries DM text."""
    print(msg, file=sys.stderr, flush=True)


class Slack:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0
        self.throttled = 0
        # Per-method pace, ADAPTIVE. The published tiers are a starting guess and the
        # real allowance varies by workspace and app: at the documented Tier 3 rate a
        # full run still took 57 backoffs. Rather than hand-tune a number that is
        # wrong somewhere else, each 429 widens that method's gap by 15% and it
        # settles on whatever this workspace actually permits.
        #
        # Deliberately never narrows. Creeping back down would rediscover the limit
        # continuously, which is how a polite client becomes a rude one on a long run.
        self.pace = dict(PAUSE_FOR)

    def call(self, method: str, **params) -> dict:
        url = API + method + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.calls += 1
                    body = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Slack says exactly how long to wait. Guessing is how a polite
                    # client becomes an impolite one.
                    wait = int((e.headers or {}).get("Retry-After") or 5)
                    self.throttled += 1
                    old = self.pace.get(method, TIER2)
                    self.pace[method] = min(old * 1.15, 10.0)
                    log(f"    rate limited on {method}, sleeping {wait}s "
                        f"(pace {old:.2f}s -> {self.pace[method]:.2f}s)")
                    time.sleep(wait + 1)
                    continue
                return {"ok": False, "error": f"http_{e.code}"}
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 3:
                    return {"ok": False, "error": f"transport: {e}"}
                time.sleep(2 ** attempt)
                continue
            if body.get("error") == "ratelimited":
                time.sleep(5)
                continue
            time.sleep(self.pace.get(method, TIER2))
            return body
        return {"ok": False, "error": "gave up after retries"}

    def paged(self, method: str, key: str, **params) -> list[dict]:
        """Follow next_cursor to exhaustion. Every list endpoint here needs it."""
        out: list[dict] = []
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            body = self.call(method, **p)
            if not body.get("ok"):
                log(f"    {method} failed: {body.get('error')}")
                return out
            out.extend(body.get(key) or [])
            cursor = ((body.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return out


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": 0, "conversations": {}}


def save_state(state: dict) -> None:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def conversations(api: Slack) -> list[dict]:
    """Every DM and group DM, both types, deactivated peers dropped.

    ONE CALL PER TYPE. `types="im,mpim"` is a trap: measured, Slack
    returned 200 group DMs before a single 1:1, so a bounded page never reached the
    `im` conversations and the sweep looked at zero DMs while reporting success.
    Nothing documents an ordering, so do not rely on one.
    """
    found: list[dict] = []
    for kind in ("im", "mpim"):
        got = api.paged("conversations.list", "channels",
                        types=kind, limit=200, exclude_archived="true")
        log(f"  {kind}: {len(got)}")
        found.extend(got)
    # Measured once: 83 of the first 200 DMs were with deactivated accounts. They can
    # never carry anything new, and polling them is 40% of the request budget spent
    # on nothing.
    live = [c for c in found if not c.get("is_user_deleted")]
    if len(live) != len(found):
        log(f"  skipping {len(found) - len(live)} conversation(s) with deactivated users")
    return live


def due(convo: dict, state: dict, run: int) -> bool:
    """Whether to poll this conversation on this run. See COLD_AFTER."""
    rec = state["conversations"].get(convo["id"])
    if not rec:
        return True  # never seen: always look
    if int(rec.get("empty_streak") or 0) < COLD_AFTER:
        return True
    # Stable per-conversation offset, so the cold set is spread evenly across runs
    # instead of every dormant conversation landing on the same one and spiking it.
    # sum(bytes) rather than hash(): hash() is salted per process in Python 3, so the
    # rotation would reshuffle on every run and the spreading would not hold.
    offset = sum(convo["id"].encode()) % COLD_EVERY
    return (run + offset) % COLD_EVERY == 0


def harvest(api: Slack, convo: dict, state: dict, bootstrap: float) -> list[dict]:
    """New messages in one conversation, oldest first."""
    cid = convo["id"]
    rec = state["conversations"].setdefault(cid, {})
    oldest = rec.get("watermark") or f"{bootstrap:.6f}"

    body = api.call("conversations.history", channel=cid, oldest=oldest,
                    limit=MAX_PER_CONVO, inclusive="false")
    if not body.get("ok"):
        # Do NOT advance the watermark on a failure. A conversation that errored is
        # re-read next run; a watermark moved past an unread window loses messages
        # silently, which is the one failure this design must not have.
        rec["last_error"] = body.get("error")
        return []
    rec.pop("last_error", None)

    msgs = [m for m in (body.get("messages") or [])
            if m.get("type") == "message" and not m.get("subtype")]
    msgs.sort(key=lambda m: float(m.get("ts") or 0))
    if msgs:
        newest = msgs[-1]["ts"]
        rec["watermark"] = newest
        rec["last_message_at"] = float(newest)
        rec["empty_streak"] = 0
        track_directions(rec, convo.get("user") if not convo.get("is_mpim") else None,
                         msgs)
    else:
        # Nothing new. On the FIRST empty fetch also pin the watermark to now, or a
        # conversation that never speaks re-reads its whole bootstrap window every
        # run and can never go cold.
        rec.setdefault("watermark", f"{time.time():.6f}")
        rec["empty_streak"] = int(rec.get("empty_streak") or 0) + 1
    rec["checked_at"] = time.time()
    if body.get("has_more"):
        # Capped rather than paged: the watermark sits at the newest message we took,
        # so the remainder arrives next run. Recorded so a persistently busy
        # conversation is visible rather than quietly clipped every time.
        rec["truncated_at"] = rec.get("watermark")
    return msgs


def track_directions(rec: dict, peer: str | None, msgs: list[dict]) -> None:
    """Record when the peer last wrote and when the owner last wrote, per conversation.

    Only 1:1 DMs: `user` on an `im` conversation is the peer, so any other sender in
    it is the owner. Group DMs carry no single "them" and are skipped. Kept as raw
    timestamps in the state record - whether an exchange counts as contact is the
    consumer's policy, not this script's.
    """
    if not peer:
        return
    for m in msgs:
        key = "last_inbound_ts" if m.get("user") == peer else "last_outbound_ts"
        rec[key] = max(float(rec.get(key) or 0), float(m.get("ts") or 0))


def carry_forward(path: pathlib.Path, now: float) -> tuple[list[dict], int]:
    """The previous spool's messages still young enough to ride forward, plus how
    many expired.

    Rows written before `fetched_at` existed inherit the old spool's produced_at,
    so upgrading does not age an entire spool to zero or keep it forever.
    """
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], 0
    default_fetch = now
    try:
        default_fetch = datetime.strptime(
            prev.get("produced_at") or "", "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    kept: list[dict] = []
    expired = 0
    for r in prev.get("messages") or []:
        try:
            fetched = float(r.get("fetched_at") or default_fetch)
        except (TypeError, ValueError):
            fetched = default_fetch
        if now - fetched > RETAIN_HOURS * 3600:
            expired += 1
            continue
        r["fetched_at"] = fetched
        kept.append(r)
    return kept, expired


def merge_spool(carried: list[dict], fresh: list[dict],
                fetched_at: float) -> list[dict]:
    """Carried + fresh, fresh stamped with this run's fetch time and winning any
    (conversation, ts) collision, sorted oldest first as the spool always has been."""
    for r in fresh:
        r["fetched_at"] = fetched_at
    fresh_keys = {(r.get("conversation"), r.get("ts")) for r in fresh}
    out = [r for r in carried
           if (r.get("conversation"), r.get("ts")) not in fresh_keys]
    out += fresh
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report counts, write nothing, move no watermark")
    ap.add_argument("--limit-conversations", type=int, default=0,
                    help="stop after N conversations (for a quick check)")
    args = ap.parse_args()

    token = os.environ.get("SLACK_USER_TOKEN", "").strip()
    if not token:
        log("SLACK_USER_TOKEN is not set. Inject it with your secrets CLI, for example:")
        log("  <secrets-cli> run --with SLACK_USER_TOKEN=<user-token-id> -- "
            "python producers\\slack_dm_producer.py")
        return 2
    if not token.startswith("xoxp-"):
        log("this needs the USER token (xoxp-): a bot token cannot see the owner's DMs")
        return 2

    api = Slack(token)
    state = load_state()
    run = int(state.get("runs") or 0) + 1
    bootstrap = time.time() - BOOTSTRAP_HOURS * 3600
    started = time.time()

    # Snapshot the watermarks BEFORE harvesting. coverage_since is "the oldest moment
    # this run read back to", which is where it RESUMED FROM, and harvesting
    # overwrites exactly that. Computing it afterwards reports the oldest post-run
    # watermark, which on any incremental run is roughly now: the drop would claim it
    # covered nothing, every time, while having covered the full gap.
    resumed_from = {cid: rec.get("watermark")
                    for cid, rec in state["conversations"].items() if rec.get("watermark")}

    log(f"run {run}, listing conversations")
    convos = conversations(api)
    polled = skipped = 0
    harvested: list[dict] = []

    for convo in convos:
        if args.limit_conversations and polled >= args.limit_conversations:
            break
        if not due(convo, state, run):
            skipped += 1
            continue
        polled += 1
        for m in harvest(api, convo, state, bootstrap):
            harvested.append({
                "conversation": convo["id"],
                "is_group": bool(convo.get("is_mpim")),
                "ts": m.get("ts"),
                "user": m.get("user"),
                "text": m.get("text") or "",
                "thread_ts": m.get("thread_ts"),
            })

    # Names, resolved once for the whole batch rather than per message. Emails ride
    # along for the contact facts: the dossier matcher prefers them over display
    # names, which people change. Missing scope just means empty strings.
    names: dict[str, str] = {}
    emails: dict[str, str] = {}
    if harvested:
        for u in api.paged("users.list", "members", limit=200):
            prof = u.get("profile") or {}
            names[u.get("id")] = (prof.get("display_name")
                                  or prof.get("real_name") or u.get("name") or u.get("id"))
            if prof.get("email"):
                emails[u.get("id")] = prof["email"]
    for row in harvested:
        row["who"] = names.get(row.get("user") or "", row.get("user") or "unknown")

    # Contact facts for the 1:1 conversations that produced messages this run. Emitted
    # from state, so a run that only saw the owner's half of an exchange still reports the
    # peer's half from the previous run.
    im_peer = {c["id"]: c.get("user") for c in convos
               if not c.get("is_mpim") and c.get("user")}
    contacts: list[dict] = []
    for cid in sorted({r["conversation"] for r in harvested if not r["is_group"]}):
        peer = im_peer.get(cid)
        rec = state["conversations"].get(cid) or {}
        if not peer or not (rec.get("last_inbound_ts") or rec.get("last_outbound_ts")):
            continue
        contacts.append({
            "peer": peer,
            "who": names.get(peer, peer),
            "email": emails.get(peer, ""),
            "last_inbound_ts": rec.get("last_inbound_ts"),
            "last_outbound_ts": rec.get("last_outbound_ts"),
        })

    harvested.sort(key=lambda r: float(r.get("ts") or 0))
    elapsed = time.time() - started
    log(f"  polled {polled}, skipped {skipped} cold, {len(harvested)} new message(s), "
        f"{api.calls} API calls, {api.throttled} throttled, {elapsed:.0f}s")
    if api.throttled:
        log(f"  settled pace: " + ", ".join(f"{m.split('.')[-1]}={s:.2f}s"
                                            for m, s in sorted(api.pace.items())))

    if args.dry_run:
        log("  dry run: nothing written, no watermark moved")
        print(json.dumps({"ok": True, "dry_run": True, "new_messages": len(harvested),
                          "polled": polled, "skipped_cold": skipped,
                          "api_calls": api.calls}))
        return 0

    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    # Merge unswept messages from the previous spool forward (THE SPOOL IS A BUFFER
    # in the module docstring). Overwriting them was silent data loss: three of every
    # four hourly spools never lived to see the four-hourly sweep.
    carried, expired_n = carry_forward(OUT_PATH, started)
    if expired_n:
        log(f"  EXPIRED {expired_n} carried message(s): fetched over "
            f"{RETAIN_HOURS:.0f}h ago and never swept. That is data loss, and if "
            "the sweep has been down this line is the receipt.")
    if carried:
        log(f"  carrying {len(carried)} unswept message(s) from the previous spool")
    messages = merge_spool(carried, harvested, started)

    # The honest coverage claim, computed rather than asserted: the oldest point
    # this run actually read back to. On a first run that is the bootstrap window;
    # afterwards it is the oldest watermark it resumed from. Carried messages can be
    # older than that, and they are in the file, so the claim widens to match.
    coverage_epoch = min((float(resumed_from.get(c["id"]) or bootstrap) for c in convos),
                         default=bootstrap)
    if carried:
        coverage_epoch = min(coverage_epoch,
                             min(float(r.get("ts") or coverage_epoch) for r in carried))

    out = {
        "produced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": f"slack_dm_producer.py on {os.environ.get('COMPUTERNAME', '?')}",
        "coverage_since": datetime.fromtimestamp(
            coverage_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "polled": polled,
        "skipped_cold": skipped,
        "api_calls": api.calls,
        "messages": messages,
        "contacts": contacts,
    }
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    tmp.replace(OUT_PATH)

    state["runs"] = run
    save_state(state)

    log(f"  wrote {OUT_PATH}")
    print(json.dumps({"ok": True, "new_messages": len(harvested),
                      "carried": len(carried), "expired": expired_n,
                      "polled": polled, "skipped_cold": skipped,
                      "api_calls": api.calls, "spool": str(OUT_PATH)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
