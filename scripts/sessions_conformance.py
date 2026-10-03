"""Conformance harness for live session state.

    python scripts\\sessions_conformance.py

WHAT THIS FILE IS FOR. This feature writes into `~/.claude/settings.local.json`,
runs inside every Claude Code turn on the machine, and reports a state the owner acts
on. Each of those is a way to do real damage that no screenshot would reveal.

  1. THE MERGE MUST NEVER EAT SOMEBODY ELSE'S HOOKS. A machine may already run a
     Stop hook from another tool and keep permission grants in the same file
     family. Installing telemetry that silently drops either is a far worse
     outcome than having no telemetry. Asserted directly, including on a
     re-install and on a changed interpreter path.
  2. THE HOOK CANNOT FAIL A TURN. It runs as a real subprocess here, against a
     daemon that is not running, with junk on stdin, and must still exit 0 and
     print nothing. Anything else means a broken hook takes the owner's session with
     it.
  3. `state_since` IS NOT `last_event`. The one question this data exists to
     answer is "how long has this been blocked on me". Bumping the clock on every
     event would silently make that answer always "just now", and the panel would
     look fine while being useless.
  4. SILENCE IS NOT A VERDICT. A session that went quiet mid-turn stays `busy`
     and is REPORTED as ambiguous. Otto has no PTY and cannot know whether it
     died, so converting silence into an outcome would be inventing one, which is
     the same error `orphaned` exists to avoid on runs.
  5. THE WAITING ALERT MUST NOT CRY WOLF. It fires past the threshold and stays
     silent before it, and a long turn is info rather than a warning, because an
     alarm that fires on normal operation is one the owner learns to skip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="otto-sessions-test-"))
os.environ["OTTO_HOME"] = str(TMP)
os.environ["OTTO_NO_TOAST"] = "1"   # never raise a real desktop notification from a test
# Point the hook at a port nothing is listening on, so the subprocess test
# exercises the daemon-down path rather than writing into real state.
os.environ["OTTO_PORT"] = "8799"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otto import config  # noqa: E402

config.utf8_output()
config.mark_daemon()
config.ensure_dirs()

from otto import sessions  # noqa: E402
from otto.models import Session, iso  # noqa: E402
from otto.store import Store  # noqa: E402

store = Store()
PASS: list[str] = []
FAIL: list[str] = []
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name}  <- {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))


def head(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def ago(**kw) -> str:
    return iso(NOW - timedelta(**kw))


def payload(sid: str = "s-1", **kw) -> dict:
    base = {"session_id": sid, "cwd": str(REPO), "transcript_path": "T.jsonl",
            "permission_mode": "default"}
    base.update(kw)
    return base


# ============================================================================
head("1. the merge preserves what it finds")

FOREIGN = {
    "permissions": {"allow": ["Bash(ls)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "powershell some-other-tool.ps1"}]}],
        "PostToolUse": [{"matcher": "Agent",
                         "hooks": [{"type": "command", "command": "beep.ps1"}]}],
    },
}


def commands(doc: dict, event: str) -> list[str]:
    out = []
    for entry in (doc.get("hooks") or {}).get(event) or []:
        for h in entry.get("hooks") or []:
            out.append(h.get("command", ""))
    return out


merged = sessions.merged_settings(FOREIGN, python="/py", script="/s.py")

check("unrelated top-level keys survive",
      merged.get("permissions") == {"allow": ["Bash(ls)"]},
      f"got {merged.get('permissions')}")
check("a foreign hook on a shared event survives",
      "powershell some-other-tool.ps1" in commands(merged, "Stop"),
      f"Stop = {commands(merged, 'Stop')}")
check("a foreign hook on an untouched event survives",
      commands(merged, "PostToolUse") == ["beep.ps1"],
      f"got {commands(merged, 'PostToolUse')}")
check("otto's own entry was added alongside it",
      any(sessions.HOOK_MARKER in c for c in commands(merged, "Stop")),
      f"Stop = {commands(merged, 'Stop')}")
check("all six events are installed",
      all(any(sessions.HOOK_MARKER in c for c in commands(merged, e))
          for e, _m, _s in sessions.HOOK_MAP),
      "one or more events missing")

twice = sessions.merged_settings(merged, python="/py", script="/s.py")
check("merging twice changes nothing (idempotent)", twice == merged,
      "second merge differed")

moved = sessions.merged_settings(merged, python="/NEW/py", script="/s.py")
stop_ours = [c for c in commands(moved, "Stop") if sessions.HOOK_MARKER in c]
check("a changed interpreter path replaces, never duplicates",
      len(stop_ours) == 1 and "/NEW/py" in stop_ours[0],
      f"got {stop_ours}")
check("...and still keeps the foreign hook",
      "powershell some-other-tool.ps1" in commands(moved, "Stop"),
      f"Stop = {commands(moved, 'Stop')}")

# The matchers are the two subtleties that cost a wrong state.
notif = (merged["hooks"]["Notification"])[0]
check("Notification is filtered to permission_prompt only",
      notif.get("matcher") == "permission_prompt",
      f"matcher = {notif.get('matcher')!r}")
check("StopFailure maps to idle, so an errored turn does not stick at busy",
      any("otto_hook" in c or sessions.HOOK_MARKER in c
          for c in commands(merged, "StopFailure")) and
      any(" idle " in c for c in commands(merged, "StopFailure")),
      f"StopFailure = {commands(merged, 'StopFailure')}")
check("SubagentStop is NOT installed (it fires mid-turn)",
      "SubagentStop" not in {e for e, _m, _s in sessions.HOOK_MAP},
      "SubagentStop present in HOOK_MAP")


head("1b. the install target is the one Claude Code actually reads")

# THE FINDING THIS PINS. `~/.claude/settings.local.json` is the better-looking
# target (gitignored, machine-local, and what keitora uses at the PROJECT level)
# and it silently does not work: Claude Code reads `permissions` from the
# user-level local file but never loads `hooks` from it. Verified 2026-08-05
# against a control hook in settings.json that fired for the same session. A
# regression here produces config that looks right and never runs, so it is
# asserted rather than left to a comment.
check("the user-level target is settings.json",
      sessions.GLOBAL_SETTINGS.name == "settings.json",
      f"target is {sessions.GLOBAL_SETTINGS.name}, which does not load hooks")
check("...and it is the user-level one",
      sessions.GLOBAL_SETTINGS.parent == config.CLAUDE_DIR,
      str(sessions.GLOBAL_SETTINGS))
check("a per-project install is still possible",
      "path" in sessions.install.__code__.co_varnames,
      "install() must keep taking an explicit target")


head("1c. install refuses to clobber what it cannot parse")

broken = TMP / "broken-settings.json"
broken.write_text("{ this is not json", encoding="utf-8")
changed, msg = sessions.install(path=broken)
check("unparseable settings file is refused, not overwritten",
      changed is False and broken.read_text(encoding="utf-8") == "{ this is not json",
      msg)

fresh = TMP / "fresh" / "settings.local.json"
changed, _msg = sessions.install(path=fresh)
check("install writes a fresh file", changed and fresh.is_file())
check("installed_in() sees its own install", sessions.installed_in(fresh))
changed_again, _m = sessions.install(path=fresh)
check("re-install on unchanged content writes nothing", changed_again is False)

# Round-trip through uninstall, with a foreign hook present the whole time.
doc = json.loads(fresh.read_text(encoding="utf-8"))
doc.setdefault("hooks", {}).setdefault("Stop", []).append(
    {"hooks": [{"type": "command", "command": "keep-me.ps1"}]})
fresh.write_text(json.dumps(doc), encoding="utf-8")
sessions.uninstall(path=fresh)
after = json.loads(fresh.read_text(encoding="utf-8"))
check("uninstall removes every otto entry",
      not sessions.installed_in(fresh)
      and not any(sessions.HOOK_MARKER in c
                  for e in (after.get("hooks") or {})
                  for c in commands(after, e)),
      "otto hooks survived uninstall")
check("uninstall leaves the foreign hook alone",
      "keep-me.ps1" in commands(after, "Stop"),
      f"Stop = {commands(after, 'Stop')}")


# ============================================================================
head("2. the hook cannot fail a turn")

HOOK = REPO / "scripts" / "otto_hook.py"
env = dict(os.environ)
env["OTTO_PORT"] = "8799"          # nothing listening
env.pop("OTTO_HOOK_DEBUG", None)


def run_hook(state: str, stdin: bytes) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-S", str(HOOK), state],
                          input=stdin, capture_output=True, env=env, timeout=30)


cases = [
    ("valid payload, daemon down", "idle", json.dumps(payload()).encode()),
    ("empty stdin", "idle", b""),
    ("garbage stdin", "busy", b"not json at all {{{"),
    ("json but not an object", "busy", b"[1,2,3]"),
    ("payload with no session_id", "idle", b'{"cwd":"D:/x"}'),
    ("unknown state argument", "banana", json.dumps(payload()).encode()),
    ("huge payload", "idle",
     json.dumps(payload(last_assistant_message="x" * 200000)).encode()),
]
for label, state, data in cases:
    p = run_hook(state, data)
    check(f"exit 0: {label}", p.returncode == 0, f"exit {p.returncode}")
    check(f"silent: {label}", p.stdout == b"" and p.stderr == b"",
          f"stdout={p.stdout[:60]!r} stderr={p.stderr[:60]!r}")

head("2b. the idle nudge must never be reported as waiting")

# THE BUG THIS EXISTS FOR. Claude Code sends two Notifications. "needs your
# permission" means blocked on the owner; "is waiting for your input" is the idle nudge
# and means the session is FREE. An early allow-list here matched the word
# "waiting" and flipped idle sessions into "needs you", which is the noisiest
# possible way to be wrong. It passed every unit assertion and was caught only by
# driving the real hook end to end, so these assert on the real message strings.
sys.path.insert(0, str(REPO / "scripts"))
import otto_hook  # noqa: E402

NOTIFICATIONS = [
    ("Claude needs your permission to use Bash", True, "permission prompt"),
    ("Claude needs your permission to use Edit", True, "permission prompt"),
    ("Claude is waiting for your input", False, "idle nudge"),
    ("Claude has been idle for 60 seconds", False, "idle nudge"),
    ("", True, "no message at all: trust the matcher"),
    ("some phrasing nobody has seen yet", True, "unknown: trust the matcher"),
]
for message, expected, label in NOTIFICATIONS:
    got = otto_hook._is_permission_prompt({"message": message})
    check(f"{'reports' if expected else 'skips  '} {label}: {message[:44]!r}",
          got is expected, f"got {got}, wanted {expected}")

# The guard in the installed command must survive a moved interpreter.
cmd = sessions.hook_command("idle", python="C:/definitely/not/here.exe")
check("installed command guards a missing interpreter", '[ -f "' in cmd and "|| true" in cmd, cmd)
check("installed command silences all output", ">/dev/null 2>&1" in cmd, cmd)
check("installed command carries the marker", sessions.HOOK_MARKER in cmd, cmd)


head("2c. the hook stays cheap and self-contained")

# `otto skills audit --verify sessions` evaluates its directives against ONE file,
# the declared source (otto/sessions.py), so it cannot see the hook at all. The hook
# is half this surface and the half that runs inside every turn, so its properties
# are asserted here instead. See the CONFORMANCE.md note.
hook_src = HOOK.read_text(encoding="utf-8")
hook_code = "\n".join(
    line for line in hook_src.splitlines()
    if not line.lstrip().startswith("#")
)
# Real import statements only. Matching raw text catches the module docstring,
# which discusses the very imports it promises not to make.
imports = [line.strip() for line in hook_src.splitlines()
           if line.startswith(("import ", "from "))]

check("the hook imports nothing from the otto package",
      not any("otto" in line for line in imports),
      f"module-scope imports: {imports}")
check("the hook imports nothing from site-packages",
      not any(m in line for line in imports
              for m in ("requests", "pydantic", "httpx", "fastapi")),
      f"module-scope imports: {imports}")
check("the hook does not import urllib at module scope",
      not any("urllib" in line for line in imports),
      "urllib.request costs ~62ms of import; it must stay inside the non-http branch")
check("the hook never prints", "print(" not in hook_code,
      "Claude Code reads hook output; a print is a protocol violation")
check("the hook exits 0 on an unexpected error",
      "except Exception" in hook_code and "sys.exit(0)" in hook_code,
      "a hook that can raise can fail a turn")
check("the hook denies the idle nudge rather than allow-listing permissions",
      "_IDLE_NUDGE" in hook_code and 'if "permission" in message' in hook_code,
      "an allow list flipped idle sessions to waiting once already")

# THE REGRESSION THIS BLOCK EXISTS FOR. A single timeout looks obviously correct
# and costs 4s per turn on this machine, because a closed loopback port here drops
# the SYN rather than refusing it, so connect hangs for the whole timeout instead
# of failing in microseconds. Both halves of the fix are asserted.
check("connect and read timeouts are separate",
      "CONNECT_TIMEOUT" in hook_code and "TIMEOUT" in hook_code,
      "one timeout means a down daemon costs the read timeout on every turn")
check("the connect timeout is tight (<0.5s)",
      0 < otto_hook.CONNECT_TIMEOUT < 0.5,
      f"CONNECT_TIMEOUT={otto_hook.CONNECT_TIMEOUT}; loopback accepts in <1ms")
check("connect timeout is well under the read timeout",
      otto_hook.CONNECT_TIMEOUT < otto_hook.TIMEOUT,
      "a busy daemon still needs time to answer once connected")
check("a failed connect trips a breaker",
      "_trip_breaker" in hook_code and "_breaker_open" in hook_code,
      "without it, every turn during an outage pays the connect timeout")
check("a successful connect clears the breaker",
      "_clear_breaker" in hook_code,
      "a daemon that came back would stay unreported for the breaker window")

# Measured, because a regression here is invisible to every text assertion above,
# and this is the path that actually broke: the daemon is DOWN for these runs (the
# harness points OTTO_PORT at a dead port), which is the expensive case.
import time  # noqa: E402

try:
    os.remove(otto_hook.BREAKER)
except OSError:
    pass

first = time.perf_counter()
run_hook("idle", json.dumps(payload("perf-1")).encode())
first_ms = (time.perf_counter() - first) * 1000
check(f"first hook against a DEAD daemon stays under 700ms (measured {first_ms:.0f}ms)",
      first_ms < 700,
      f"{first_ms:.0f}ms per hook is {first_ms * 2:.0f}ms added to every turn")

later = []
for _ in range(4):
    t0 = time.perf_counter()
    run_hook("idle", json.dumps(payload("perf-1")).encode())
    later.append((time.perf_counter() - t0) * 1000)
median_ms = sorted(later)[len(later) // 2]
check(f"subsequent hooks skip via the breaker (measured {median_ms:.0f}ms)",
      median_ms < 250,
      f"{median_ms:.0f}ms: the breaker is not short-circuiting the connect")


# ============================================================================
head("3. state_since tracks the transition, not the last event")

sessions.record(store, "busy", payload("s-clock"))
first = store.get_session("s-clock")
assert first is not None
# Backdate the transition, then send ANOTHER busy event (a second turn).
first.state_since = ago(minutes=40)
first.last_event = ago(minutes=40)
store.put_session(first)

sessions.record(store, "busy", payload("s-clock"))
same = store.get_session("s-clock")
check("a repeated state does NOT reset state_since",
      same.state_since == ago(minutes=40),
      f"state_since moved to {same.state_since}")
check("...but last_event does move", same.last_event != ago(minutes=40))
check("a repeated busy counts another turn", same.turns == 2, f"turns={same.turns}")

sessions.record(store, "idle", payload("s-clock"))
moved_state = store.get_session("s-clock")
check("a real transition DOES reset state_since",
      moved_state.state_since != ago(minutes=40),
      f"state_since={moved_state.state_since}")
check("idle does not count as a turn", moved_state.turns == 2, f"turns={moved_state.turns}")

sessions.record(store, "waiting", payload("s-note", message="Claude needs permission to run ls"))
noted = store.get_session("s-note")
check("a waiting session records what it is asking for",
      noted.note and "permission" in noted.note, f"note={noted.note!r}")
sessions.record(store, "idle", payload("s-note"))
check("the ask is cleared once it is answered",
      store.get_session("s-note").note is None,
      f"note={store.get_session('s-note').note!r}")

sessions.record(store, "busy", payload("s-attr"))
attr = store.get_session("s-attr")
check("cwd is resolved to a domain", attr.domain in ("work", "personal"), attr.domain)
check("a session Otto never spawned has no run_id", attr.run_id is None, str(attr.run_id))

try:
    sessions.record(store, "busy", {"cwd": "D:/x"})
    check("a payload with no session_id is refused", False, "no error raised")
except ValueError:
    check("a payload with no session_id is refused", True)


# ============================================================================
head("3b. a broken character cannot take the dashboard down")

# 2026-08-15: one session's last_message held a split emoji ("\ufffd\udc8f Personal
# Commitment", read off a calendar). json.dumps escaped the lone surrogate happily,
# so it STORED without complaint, and then /api/state and /api/sessions both
# returned 500 forever after. Both the dashboard and the Tauri app showed "daemon
# unreachable" while the daemon was answering /api/health in four milliseconds.
#
# Note what did NOT contain the bug: the field, the record, the endpoint. One bad
# character in one string took out every consumer of the whole file. That is why
# this is asserted on the SERIALIZED output rather than on the stored string: a
# check that the text "looks clean" would have passed on the escaped form too.
BAD = "\udc8f Personal Commitment"

sessions.record(store, "busy", payload("s-surrogate", last_assistant_message=BAD))
sessions.record(store, "waiting", payload("s-surrogate2", message=f"pick {BAD}"))

for sid in ("s-surrogate", "s-surrogate2"):
    got = store.get_session(sid)
    field = got.last_message if sid == "s-surrogate" else got.note
    check(f"{sid}: the surrogate is gone from state",
          field is not None and not any(0xD800 <= ord(c) <= 0xDFFF for c in field),
          f"{field!r}")
    check(f"{sid}: the text still survives",
          field is not None and "Personal Commitment" in field, f"{field!r}")

# The decisive one, and `ensure_ascii=False` is the whole reason it works.
#
# The obvious version of this check is vacuous. With the default ensure_ascii=True,
# json.dumps turns a lone surrogate into the seven ASCII characters `\udc8f` and the
# encode ALWAYS succeeds, so the assertion passes just as happily on the broken data
# it was written to catch. That escaping is also exactly why the bad character got
# into sessions.json in the first place: the write path could not see it either.
#
# ensure_ascii=False keeps the surrogate as a real character, which is the state
# Pydantic hands to the UTF-8 encoder when FastAPI serialises a response. That is
# the moment that raised, so that is the moment worth reproducing.
try:
    json.dumps([s.model_dump() for s in store.sessions()],
               default=str, ensure_ascii=False).encode("utf-8")
    check("the whole session list serialises to real UTF-8 bytes", True)
except (UnicodeEncodeError, TypeError) as e:
    check("the whole session list serialises to real UTF-8 bytes", False, str(e))


# ============================================================================
head("4. silence is reported, never converted into a verdict")

for sid in list(store.sessions()):
    pass
stuck = Session(session_id="s-stuck", state="busy", cwd=str(REPO),
                first_seen=ago(hours=5), last_event=ago(hours=5),
                state_since=ago(hours=5))
store.put_session(stuck)
sessions.sweep(store, now=NOW)
after_sweep = store.get_session("s-stuck")
check("a session quiet for 5h in busy is NOT rewritten to offline",
      after_sweep.state == "busy", f"state={after_sweep.state}")
check("...and is not marked as inferred-anything",
      after_sweep.offline_inferred is False)

gone = Session(session_id="s-gone", state="idle", cwd=str(REPO),
               first_seen=ago(days=2), last_event=ago(hours=20),
               state_since=ago(hours=20))
store.put_session(gone)
sessions.sweep(store, now=NOW)
swept = store.get_session("s-gone")
check(f"past {config.SESSION_OFFLINE_HOURS}h of total silence it is presumed offline",
      swept.state == "offline", f"state={swept.state}")
check("...and says the conclusion was inferred, not observed",
      swept.offline_inferred is True)

# A hook arriving after the sweep must win: the session was alive all along.
sessions.record(store, "busy", payload("s-gone"))
revived = store.get_session("s-gone")
check("a real hook overrides an inference", revived.state == "busy"
      and revived.offline_inferred is False, f"state={revived.state}")

ancient = Session(session_id="s-ancient", state="offline", cwd=str(REPO),
                  first_seen=ago(days=40), last_event=ago(days=30),
                  state_since=ago(days=30), offline_inferred=True)
store.put_session(ancient)
sessions.sweep(store, now=NOW)
check(f"offline for over {config.SESSION_PRUNE_DAYS}d is pruned",
      store.get_session("s-ancient") is None)

clean = Session(session_id="s-clean", state="offline", cwd=str(REPO),
                first_seen=ago(hours=3), last_event=ago(hours=2),
                state_since=ago(hours=2))
store.put_session(clean)
sessions.sweep(store, now=NOW)
check("a cleanly-ended session is not flagged as inferred",
      store.get_session("s-clean").offline_inferred is False)


# ============================================================================
head("5. the alert fires late and only for the right reason")

# Ids must differ within their first 8 characters: `source` truncates to that, so
# ids sharing a prefix would make every assertion below match the wrong session
# (or nothing at all) and pass vacuously.
QUIET, LOUD, LONG, DEAD = "quiet-01", "loud-001", "busy-001", "off-0001"

store.put_session(Session(session_id=QUIET, state="waiting", cwd=str(REPO),
                          first_seen=ago(minutes=5), last_event=ago(minutes=5),
                          state_since=ago(minutes=5)))
msgs = [a.message for a in sessions.alerts(store, now=NOW) if QUIET in a.source]
check(f"waiting for 5m (under {config.SESSION_WAITING_ALERT_MINUTES}m) says nothing",
      not msgs, str(msgs))

store.put_session(Session(session_id=LOUD, state="waiting", cwd=str(REPO),
                          first_seen=ago(minutes=90), last_event=ago(minutes=90),
                          state_since=ago(minutes=90),
                          note="Claude needs permission to run Bash"))
hits = [a for a in sessions.alerts(store, now=NOW) if LOUD in a.source]
check("waiting for 90m warns", len(hits) == 1 and hits[0].level == "warn",
      str([(a.level, a.message) for a in hits]))
check("...and says what it is blocked on",
      bool(hits) and "permission" in hits[0].message, hits[0].message if hits else "")

store.put_session(Session(session_id=LONG, state="busy", cwd=str(REPO),
                          first_seen=ago(hours=4), last_event=ago(hours=4),
                          state_since=ago(hours=4)))
bh = [a for a in sessions.alerts(store, now=NOW) if LONG in a.source]
check("a 4h turn is info, not a warning", len(bh) == 1 and bh[0].level == "info",
      str([(a.level, a.message) for a in bh]))
check("...and admits Otto cannot tell which it is",
      bool(bh) and "cannot tell" in bh[0].message, bh[0].message if bh else "")

store.put_session(Session(session_id=DEAD, state="offline", cwd=str(REPO),
                          first_seen=ago(days=1), last_event=ago(hours=1),
                          state_since=ago(hours=1)))
check("an offline session never alerts",
      not [a for a in sessions.alerts(store, now=NOW) if DEAD in a.source])


# ============================================================================
head("6. the gap detector reports on itself")

missing = TMP / "nowhere" / "settings.local.json"
real_global = sessions.GLOBAL_SETTINGS
try:
    sessions.GLOBAL_SETTINGS = missing
    g = sessions.gaps(store)
    check("with no hooks installed, the gap is raised",
          any(x["id"] == "gap:session-hooks" for x in g), str([x["id"] for x in g]))
    check("...and it is the only one (no point reporting silence too)",
          len(g) == 1, str([x["id"] for x in g]))

    sessions.install(path=missing)
    sessions.GLOBAL_SETTINGS = missing
    empty = Store(TMP / "empty-state")
    g2 = sessions.gaps(empty)
    check("installed but never fired is its own gap",
          any(x["id"] == "gap:session-hooks-silent" for x in g2),
          str([x["id"] for x in g2]))
    check("installed and reporting raises nothing", not sessions.gaps(store),
          str([x["id"] for x in sessions.gaps(store)]))
finally:
    sessions.GLOBAL_SETTINGS = real_global


# ============================================================================
head("7. the summary counts what the panels claim")

summary = sessions.summary(store)
live_rows = [s for s in store.sessions() if s.live]
check("live count matches the rows", summary["live"] == len(live_rows),
      f"{summary['live']} vs {len(live_rows)}")
check("unspawned counts live sessions with no run",
      summary["unspawned"] == len([s for s in live_rows if not s.run_id]),
      str(summary))
check("states sum to live",
      summary["busy"] + summary["waiting"] + summary["idle"] == summary["live"],
      str(summary))


# ============================================================================
print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    {f}")
print("=" * 62)
import shutil  # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
