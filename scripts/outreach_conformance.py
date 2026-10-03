"""Conformance harness for outreach: the only thing in Otto that acts on other people.

Same shape as the other harnesses: throwaway OTTO_HOME, no daemon, no network.

    python scripts\\outreach_conformance.py

WHY THIS FILE IS THE MOST IMPORTANT ONE IN scripts/.

Every other capability in Otto fails onto the owner. This one fails onto a colleague, and
the failure is not recoverable: a Slack DM that should not have gone out has gone out.
The whole design rests on a set of claims that are only true if nothing in the code
path can be talked out of them, and "the docstring says it refuses" is not that.

  1. THE MASTER SWITCH REALLY IS THE CLOSED POSITION. With OTTO_OUTREACH unset, a
     composed message expires unsent even after its hold elapses. This is the claim the
     rollout plan depends on: a week of watching held messages costs nobody anything.
  2. THE GATES REFUSE RATHER THAN DEGRADE. Unknown recipient, external recipient,
     contractor while contractors are off, a forbidden subject, a channel post with no
     allowlist, an empty `why`. Each must raise, not warn-and-send, and NOT be recorded
     as a held message that a later tick might pick up.
  3. SILENCE SENDS, BUT ONLY FOR TIER 0. The hold expiring is not a decision, so a
     message a producer marked tier 1 stays held forever rather than being sent by
     timeout.
  4. THE GATES RE-RUN AT SEND TIME. A message held while its recipient was valid must
     not go out after they became invalid. Yesterday's verdict is not authorization.
  5. RATE LIMITS BOUND A RUNAWAY. The point is not tidiness. If whatever composes these
     is wrong in a loop, the ceiling is what stands between that and the owner's team.

Nothing here can send, and that is enforced TWICE. The transport is monkeypatched, and
OTTO_NO_SEND=1 makes otto/slack.py refuse at the wire regardless.

The second one exists because the first one failed. Once, the stub was bound to
a function name, that function was renamed, and this harness -- which sets
OUTREACH_ENABLED=True deliberately, to exercise the send path -- posted its fixture text
into a real group DM with a real colleague. A safety that depends on a test remembering
to re-bind a stub is not a safety. Never remove the env var to 'simplify' this.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-outreach-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
os.environ["OTTO_NO_SEND"] = "1"    # and never reach a real colleague, whatever is stubbed
os.environ.pop("OTTO_OUTREACH", None)          # master switch OFF, the default
os.environ.pop("OTTO_SLACK_BOT_TOKEN", None)   # and no transport credential
# The fixture roster below lives on these domains. Set here, not inherited from the
# operator's .env, so the member/contractor/external verdicts are the harness's own.
os.environ["OTTO_ORG_DOMAINS"] = "example.com"
os.environ["OTTO_CONTRACTOR_DOMAINS"] = "ext.example.com"
os.environ.pop("OTTO_OUTREACH_CONTRACTORS", None)   # contractors OFF, the default

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otto import config  # noqa: E402
from otto.models import Outreach, iso, utcnow  # noqa: E402

config.utf8_output()
config.mark_daemon()
config.ensure_dirs()

from otto import outreach, people  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def refuses(name: str, fn, expect: str = "") -> None:
    """Assert a gate raises Refused, and that nothing was recorded."""
    before = len(store.outreach())
    try:
        fn()
    except outreach.Refused as e:
        ok = expect.lower() in str(e).lower() if expect else True
        check(name, ok, f"raised the wrong reason: {e}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"raised {type(e).__name__} instead of Refused: {e}")
    else:
        check(name, False, "did NOT refuse")
    check(f"...and recorded nothing", len(store.outreach()) == before,
          "a refused message was written to the ledger anyway")


# ---- a fake roster ----------------------------------------------------------
# Not the real one: this harness must not depend on who works at the organization
# today, and it must be able to assert on an external and a contractor that reliably
# exist. Every person here is invented.
ROSTER = {
    "arivera@example.com": {
        "slug": "arivera", "login": "arivera@example.com",
        "email": "arivera@example.com", "display_name": "Alex Rivera",
        "external": False,
    },
    "sokafor@ext.example.com": {
        "slug": "sokafor", "login": "sokafor@ext.example.com",
        "email": "sokafor@ext.example.com", "display_name": "Sam Okafor",
        "external": False,
    },
    "klee@vendor.example": {
        "slug": "klee", "login": "klee@vendor.example", "email": "klee@vendor.example",
        "display_name": "Kim Lee", "external": True,
    },
}
people.get = lambda q: ROSTER.get(q)  # type: ignore[assignment]

SENT: list[dict] = []
# Stubbed at the transport boundary, which is the whole point of having one: every
# assertion below is about the interlocks (hold, rate, kill, forbidden subjects,
# required `why`), and those must hold no matter how the bytes reach Slack. The
# transport moved from a bot-token POST to the MCP connector once and not one
# of these assertions had to change, which is the evidence that the seam is in the
# right place.
# Transmission later moved from a spawned session on the MCP connector back to the
# bot token in otto/slack.py, so the stub moved with it. Both names are kept
# patched: `_send_as_otto` is what `_transmit` calls now, and `_send_via_connector`
# is still exercised by the addressing section below.
REAL_SEND = outreach._send_via_connector  # kept: the addressing section tests the real one
outreach._send_via_connector = lambda o: (SENT.append({"to": o.target, "body": o.body}), (True, None))[1]  # type: ignore[assignment]
outreach._send_as_otto = lambda o: (SENT.append({"to": o.target, "body": o.body}), (True, None))[1]  # type: ignore[assignment]

# Generous caps for the sections that are about the state machine. The real defaults
# (6/day, 2/person) are tight enough to fire in the middle of this harness, which they
# did on the first run: correct behavior, wrong place to assert it. The rate-limit
# section below sets its own.
config.OUTREACH_MAX_PER_DAY = 100
config.OUTREACH_MAX_PER_PERSON_DAY = 100

print(f"\n  OUTREACH CONFORMANCE   home {TMP}\n")
print("-- the gates refuse, and refuse LOUDLY ---------------------------------")

refuses("an unknown recipient is refused",
        lambda: outreach.compose(store, to="nobody@example.com", body="hi", why="w"),
        "not in the people roster")
refuses("an external recipient is refused",
        lambda: outreach.compose(store, to="klee@vendor.example", body="hi", why="w"),
        "external")
refuses("a contractor is refused while contractors are off",
        lambda: outreach.compose(store, to="sokafor@ext.example.com",
                                 body="hi", why="w"),
        "contractor")
refuses("an empty why is refused",
        lambda: outreach.compose(store, to="arivera@example.com", body="hi", why=" "),
        "why")
refuses("an empty body is refused",
        lambda: outreach.compose(store, to="arivera@example.com", body="", why="w"),
        "empty")
refuses("a channel post with no allowlist is refused",
        lambda: outreach.compose(store, to="C0EXAMPLEOPS", body="hi", why="w",
                                 channel="slack-channel"),
        "not in OTTO_OUTREACH_CHANNELS")

print("\n-- forbidden subjects, whatever the producer claims ---------------------")
for text in ("your salary review is scheduled",
             "we think your laptop is compromised",
             "about the harassment complaint",
             "your offboarding starts Monday",
             "this is a security incident"):
    refuses(f"refused: {text[:38]}",
            lambda t=text: outreach.compose(store, to="arivera@example.com",
                                            body=t, why="w"))
refuses("the subject check reads `why` too, not just the body",
        lambda: outreach.compose(store, to="arivera@example.com",
                                 body="quick question",
                                 why="following up on the phishing report"))

print("\n-- the happy path holds, and does not send ------------------------------")
o = outreach.compose(store, to="arivera@example.com",
                     body="Your new workstation shipped, tracking in your DMs.",
                     why="Alex asked last week and the order confirmed today.",
                     source="test")
check("a clean message is recorded", store.get_outreach(o.id) is not None)
check("...in held state", o.state == "held")
check("...with the hold from config",
      o.hold_minutes == config.OUTREACH_HOLD_MINUTES)
check("...addressed by login, not by an opaque id", o.target == "arivera@example.com")
check("...and named in words for the ledger", o.to == "Alex Rivera")
check("nothing was transmitted", SENT == [], str(SENT))
check("it is not yet due", outreach.due(store) == [])

print("\n-- the master switch is the closed position -----------------------------")
o.send_after = iso(utcnow() - timedelta(minutes=1))
store.put_outreach(o)
check("now it is due", [x.id for x in outreach.due(store)] == [o.id])
notes = outreach.settle(store)
after = store.get_outreach(o.id)
check("with sending OFF, an elapsed hold EXPIRES", after.state == "expired", after.state)
check("...and says why in the record", "disabled" in (after.error or ""), after.error)
check("...and still nothing was transmitted", SENT == [], str(SENT))
check("...and settle reported it", any("expired unsent" in n for n in notes), str(notes))

print("\n-- with sending on: silence sends --------------------------------------")
config.OUTREACH_ENABLED = True
o2 = outreach.compose(store, to="arivera@example.com", body="Ready when you are.",
                      why="she asked for a heads up", hold_minutes=10)
check("still held while the window is open", o2.state == "held")
outreach.settle(store)
check("...and settle leaves it alone", store.get_outreach(o2.id).state == "held")
o2.send_after = iso(utcnow() - timedelta(seconds=1))
store.put_outreach(o2)
outreach.settle(store)
sent2 = store.get_outreach(o2.id)
check("an elapsed hold sends", sent2.state == "sent", sent2.state)
check("...by the timer, recorded as such", sent2.decided_by == "timer")
check("...and the transport actually got it", len(SENT) == 1, str(SENT))
check("...with the exact text that was held", SENT[0]["body"] == "Ready when you are.")

print("\n-- tier 1 is never sent by timeout -------------------------------------")
o3 = outreach.compose(store, to="arivera@example.com", body="Need your call on this.",
                      why="ambiguous, the owner decides", tier=1)
o3.send_after = iso(utcnow() - timedelta(minutes=5))
store.put_outreach(o3)
outreach.settle(store)
check("a tier-1 message stays held past its window",
      store.get_outreach(o3.id).state == "held")
check("...and was not transmitted", len(SENT) == 1, str(SENT))
check("the owner can still send it deliberately",
      outreach.send_now(store, o3.id).state == "sent")
check("...recorded as their decision, not the timer's",
      store.get_outreach(o3.id).decided_by == "owner")

print("\n-- kill --------------------------------------------------------------")
o4 = outreach.compose(store, to="arivera@example.com", body="never mind",
                      why="testing the kill path")
check("kill works on a held message", outreach.kill(store, o4.id).state == "killed")
check("...and is idempotent, not a second state change",
      outreach.kill(store, o4.id) is None)
o4 = store.get_outreach(o4.id)
o4.send_after = iso(utcnow() - timedelta(minutes=5))
store.put_outreach(o4)
outreach.settle(store)
check("a killed message is never resurrected by settle",
      store.get_outreach(o4.id).state == "killed")
check("send_now refuses a killed message", outreach.send_now(store, o4.id) is None)

print("\n-- the gates re-run at SEND time, not just at compose time --------------")
o5 = outreach.compose(store, to="arivera@example.com", body="still relevant?",
                      why="held while she was still here")
o5.send_after = iso(utcnow() - timedelta(minutes=1))
store.put_outreach(o5)
# They leave. Yesterday's verdict is not authorization for today's send.
ROSTER["arivera@example.com"]["external"] = True
outreach.settle(store)
gone = store.get_outreach(o5.id)
check("a recipient who became external is not messaged", gone.state == "killed", gone.state)
check("...and the record says it was refused at send time",
      "send time" in (gone.error or ""), gone.error)
ROSTER["arivera@example.com"]["external"] = False

print("\n-- rate limits bound a runaway ----------------------------------------")
# A recipient with NO send history, so the loop genuinely runs into the ceiling here
# rather than being refused on the first call by earlier sections. The first version of
# this check passed without the loop ever executing, which is a test that proves the
# harness rather than the cap.
ROSTER["lmarsh@example.com"] = {
    "slug": "lmarsh", "login": "lmarsh@example.com",
    "email": "lmarsh@example.com", "display_name": "Lee Marsh",
    "external": False,
}
config.OUTREACH_MAX_PER_PERSON_DAY = 2
config.OUTREACH_MAX_PER_DAY = 100
before_sent = len(SENT)
attempts = refused = 0
for i in range(6):
    attempts += 1
    try:
        m = outreach.compose(store, to="lmarsh@example.com", body=f"loop {i}",
                             why="a producer stuck in a loop")
    except outreach.Refused:
        refused += 1
        continue
    m.send_after = iso(utcnow() - timedelta(seconds=1))
    store.put_outreach(m)
    outreach.settle(store)
delivered = len(SENT) - before_sent
check("a runaway producer gets exactly the per-person cap through, then nothing",
      delivered == config.OUTREACH_MAX_PER_PERSON_DAY,
      f"{delivered} of {attempts} got through, cap is "
      f"{config.OUTREACH_MAX_PER_PERSON_DAY}")
check("...and the rest were refused, not silently dropped",
      refused == attempts - config.OUTREACH_MAX_PER_PERSON_DAY,
      f"{refused} refused out of {attempts}")

# The global cap is a separate ceiling: it has to hold even when every message goes to
# a different person, which is the shape a runaway "notify everyone" producer takes.
config.OUTREACH_MAX_PER_DAY = len(outreach.sent_today(store))
refuses("the daily cap holds across different recipients",
        lambda: outreach.compose(store, to="arivera@example.com", body="one more",
                                 why="global ceiling"),
        "in 24h")
config.OUTREACH_MAX_PER_DAY = 100

print("\n-- the ledger ---------------------------------------------------------")
check("every message ever composed is still in the ledger",
      len(store.outreach()) == 7, str(len(store.outreach())))
check("body is never rewritten after the fact",
      all(o.body for o in store.outreach()))
states = {o.state for o in store.outreach()}
check("the ledger holds the whole lifecycle, not just successes",
      {"sent", "killed", "expired"} <= states, str(states))
check("summary reports what can actually happen",
      outreach.summary(store)["can_send"] == config.OUTREACH_ENABLED,
      "can_send disagrees with the master switch")


# ---------------------------------------------------------------------------
print("\n-- the transport tells the truth about what it did ----------------------")

# THE PROPERTY THIS SECTION EXISTS FOR. Transmission once moved from a bot-token POST to
# a spawned session calling the Slack MCP connector. A token either posts or
# returns an error; a model can say "sent!" having called nothing at all. If that were
# believed, the ledger would record `sent` for a message the colleague never received,
# and the ledger is the only record the owner audits. So the sender requires a real tool_use
# event as evidence, and these assert it on realistic stream-json.

CLAIMED_BUT_NEVER_CALLED = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "tools": [outreach.SEND_TOOL]}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Message sent to Sam successfully."}]}}),
    json.dumps({"type": "result", "is_error": False, "result": "Sent."}),
])
REALLY_CALLED = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "tools": [outreach.SEND_TOOL]}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": outreach.SEND_TOOL,
         "input": {"channel": "sam@example.com", "text": "hi"}}]}}),
    json.dumps({"type": "result", "is_error": False, "result": "Sent."}),
])
WRONG_TOOL = "\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__claude_ai_Slack__slack_read_channel",
         "input": {}}]}}),
    json.dumps({"type": "result", "is_error": False, "result": "Done."}),
])

check("a session that only CLAIMS it sent is not believed",
      outreach._tool_was_used(CLAIMED_BUT_NEVER_CALLED) is False,
      "prose was accepted as proof of a send")
check("a session that really called the send tool is believed",
      outreach._tool_was_used(REALLY_CALLED) is True)
check("calling some OTHER slack tool does not count as a send",
      outreach._tool_was_used(WRONG_TOOL) is False)
check("a clean run reports no error", outreach._result_error(REALLY_CALLED) is None)
check("an errored run is reported",
      outreach._result_error(json.dumps({"type": "result", "is_error": True,
                                         "subtype": "error_max_budget_usd"})) is not None)
check("a run with no result at all is an error, not a success",
      outreach._result_error("") is not None)

# THE BOUND ON THE SENDER, and the two assertions that matter are NEGATIVE ones.
# Measured: --dangerously-skip-permissions overrides the allow-list and
# leaves all 236 tools usable (Gmail, Drive, the IdP, the RMM, the EDR), and --tools ""
# breaks MCP permission matching so even the ALLOWED tool gets denied. Both flags look
# like they tighten this and both are wrong, so both are asserted absent. Every other
# spawner in Otto sets skip-permissions, which is exactly why this one needs a test
# rather than a comment.
launcher = outreach._send_launcher("testid00", TMP / "p.txt").read_text(encoding="utf-8")
check("the sender is allow-listed to exactly the send tool",
      "--allowed-tools" in launcher and outreach.SEND_TOOL in launcher, launcher)
check("the sender does NOT skip permissions (that would void the allow-list)",
      "skip-permissions" not in launcher,
      "skip-permissions is set, so the allow-list does nothing and the session is unbounded")
check("the sender does NOT pass --tools (that would deny its own send tool)",
      "'--tools'" not in launcher.replace('"', "'"), launcher)
check("only ONE tool is allow-listed",
      launcher.count("--allowed-tools") == 1
      and launcher.count("mcp__claude_ai_Slack__") == 1, launcher)
check("the sender cannot outlive the tick it blocks",
      "--max-budget-usd" in launcher, launcher)

# ADDRESSING FAILS CLOSED. Slack rejects an email as a message target on BOTH
# transports (verified: the connector returns channel_not_found, same as
# chat.postMessage would). The only other source of an id would be the model, and a
# guessed Slack id is a message delivered to the wrong colleague. So: no recorded id,
# no send.
# REAL_SEND, not the stub: the whole point is that the real function refuses before it
# spawns anything, so stubbing it out would test nothing.
_real_slack_id_for = outreach.slack_id_for
outreach.slack_id_for = lambda t: None  # type: ignore[assignment]
unaddressed = Outreach(channel="slack-dm", target="nobody@example.com", to="Nobody",
                       body="should never leave", why="test", tier=0)
before = len(SENT)
ok, err = REAL_SEND(unaddressed)
check("a message with no recorded Slack id is REFUSED, not guessed at",
      ok is False and "member id" in (err or ""), f"ok={ok} err={err}")
check("...and the refusal says how to fix it",
      "outreach resolve" in (err or ""), err or "")
check("...and it refused BEFORE spawning anything", len(SENT) == before,
      "a session was started for an unaddressable message")
outreach.slack_id_for = _real_slack_id_for  # type: ignore[assignment]

# The lookup and the send are deliberately different sessions with different tools.
lookup = outreach.resolve_slack_id.__doc__ or ""
check("the lookup session cannot send",
      outreach.LOOKUP_TOOL != outreach.SEND_TOOL
      and "search" in outreach.LOOKUP_TOOL,
      f"{outreach.LOOKUP_TOOL} vs {outreach.SEND_TOOL}")
check("an id is taken from the tool result, never from the model's prose",
      "by regex in Python" in lookup and "tool RESULT" in lookup,
      "resolve_slack_id no longer documents where the id comes from")

print()
# ---------------------------------------------------------------------------
print("\n-- sending AS OTTO, not as the owner ----------------------------------")
# The identity is the point of the bot-token transport, and it is exactly the kind of
# thing that fails silently: a user token in the bot credential slot would send
# perfectly well, under the owner's name, forever.
from otto import slack  # noqa: E402
from otto.runners import detached as runners_detached  # noqa: E402

os.environ.pop("SLACK_BOT_TOKEN", None)
r = slack._child({"channel": "D1", "text": "hi"})
check("no credential in the child is a refusal, not a silent no-op",
      not r.get("ok") and "SLACK_BOT_TOKEN" in (r.get("error") or ""), str(r))
os.environ["SLACK_BOT_TOKEN"] = "xoxp-fake"
r = slack._child({"channel": "D1", "text": "hi"})
check("a USER token is rejected before any POST (it would send as the owner, and succeed)",
      not r.get("ok") and "bot token" in (r.get("error") or ""), str(r))
os.environ["SLACK_BOT_TOKEN"] = "xoxb-fake"
r = slack._child({"channel": "D1", "text": "   "})
check("an empty message is refused (a blank DM is still a DM)",
      not r.get("ok") and "empty" in (r.get("error") or ""), str(r))
r = slack._child({"text": "no target"})
check("a send with no channel and nobody to open one with is refused",
      not r.get("ok"), str(r))
os.environ.pop("SLACK_BOT_TOKEN", None)

# The control that moved. A bot-token POST is not a tool call, so no PreToolUse hook
# can see it; this gate is what stops a spawned session sending as Otto directly. If
# this assertion ever fails, an unattended agent can message the company and nothing
# is in the way. (The hook's Slack job is the reverse one: denying the connector's DM
# tools, which send as the owner. tests/test_guard.py covers that.)
# Both gates are asserted against `_refuse_if_unattended` directly rather than through
# `send()`. Calling send() would mean a broken gate reaches the real transport, which
# is the exact way this harness messaged a colleague once already. Test the gate, not
# the thing the gate protects.
def _gate_error(**env: str) -> str:
    saved = {k: os.environ.get(k) for k in ("OTTO_NO_SEND", "OTTO_UNATTENDED")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        slack._refuse_if_unattended()
        return ""
    except slack.SlackError as e:
        return str(e)
    finally:
        for k, v in saved.items():
            os.environ[k] = v if v is not None else ""
            if v is None:
                os.environ.pop(k, None)

err = _gate_error(OTTO_UNATTENDED="1")
check("an UNATTENDED process cannot send as Otto (replaces the otto_guard hook)",
      "unattended" in err, err or "the gate allowed it")
err = _gate_error(OTTO_NO_SEND="1")
check("OTTO_NO_SEND blocks at the wire, whatever a test has stubbed",
      "OTTO_NO_SEND" in err, err or "the gate allowed it")
check("both gates are still armed in this very process",
      bool(_gate_error()) is False and os.environ.get("OTTO_NO_SEND") == "1")

# Read the other side rather than restating the constant: the guard is only worth
# anything if the variable it watches is the one detached.py actually sets, and those
# two files are edited years apart by people who will not remember this coupling.
detached_src = Path(runners_detached.__file__).read_text(encoding="utf-8")
check("the guard watches the env var runners/detached.py actually sets",
      f"{slack.UNATTENDED_ENV} = '1'" in detached_src,
      f"detached.py does not set {slack.UNATTENDED_ENV}")
check("the bot credential is a credential-store id, not a token value in config",
      not config.SLACK_BOT_CRED.startswith("xox"), config.SLACK_BOT_CRED)
check("the owner is in every colleague conversation when group DM is on",
      config.OUTREACH_GROUP_DM is True)

src = Path(outreach.__file__).read_text(encoding="utf-8")
check("outreach transmits through slack.py, not a spawned session",
      "_send_as_otto" in src and "slack.send(" in src)
check("the roster gate still runs before any send",
      "def resolve(" in src and "Refused" in src)

print(f"  {len(PASS)} ok, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
