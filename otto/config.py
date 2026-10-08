"""Otto configuration: one object, resolved from the environment, re-resolvable.

Everything tunable lives here. The persona name is a single string: change
PERSONA_NAME and the CLI, dashboard, Slack posts, and Notion mirror all follow.

HOW IT IS SHAPED. `resolve(environ, path)` computes every setting from a mapping
(os.environ by default) and the settings file (<OTTO_HOME>/otto.env) and returns a
`Settings` object. `reload()` resolves again and copies the result onto this
module's globals, so `config.X` everywhere keeps reading plain attributes and sees
the new values; `current()` is the live Settings object. The module calls reload()
once at import, so importing it behaves as it always did.

PRECEDENCE. A key in the real environment wins, the file comes second, the default
written below comes last. The file is applied into os.environ (settings.apply), so
children the daemon spawns inherit it; a later reload drops what the previous apply
put there before applying the file again, so a changed file value lands while a
value the shell or a service definition set is never overridden by a file it did
not write.

WHAT A RELOAD CANNOT CHANGE. A handful of values are bound when the daemon builds
itself (the Store's directory, the bind address and origin guard, the scope that
decides what is imported, the tick period). Their environment keys are listed in
RESTART_KEYS; setup.write_settings applies everything else live and asks for a
restart only for those.

ONE-TIME STATE. The daemon marker (_IS_DAEMON, mark_daemon) and the UTF-8 stream
switch are process facts, not settings, and live outside resolve().
"""


from __future__ import annotations

import os
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from .originguard import default_hosts, default_origins
from . import settings as _settings


def profile_text() -> str:
    """The owner's operating profile, or "" if none is recorded.

    Returns empty rather than raising: every consumer must work on a machine that has
    no profile, so this can never be the reason a check-in fails.
    """
    try:
        return PROFILE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def domain_for_path(path: Path | str) -> str:
    """Which domain a filesystem path belongs to. Defaults to work."""
    p = Path(path).resolve()
    for root, domain in DOMAIN_ROOTS.items():
        try:
            p.relative_to(root.resolve())
            return domain
        except (ValueError, OSError):
            continue
    return WORK


def is_weekend(when=None) -> bool:
    from datetime import datetime as _d
    when = when or _d.now().astimezone()
    return when.weekday() >= 5      # 5 = Sat, 6 = Sun


def in_quiet_hours(when=None) -> bool:
    """True inside the overnight window. Handles the wrap across midnight."""
    from datetime import datetime as _d
    if QUIET_FROM == QUIET_TO:
        return False
    hour = (when or _d.now().astimezone()).hour
    if QUIET_FROM < QUIET_TO:                 # e.g. 01-06, no wrap
        return QUIET_FROM <= hour < QUIET_TO
    return hour >= QUIET_FROM or hour < QUIET_TO


class ExpectedSnapshot(NamedTuple):
    domain: str
    kind: str

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.kind}"


def expected_snapshots() -> list[ExpectedSnapshot]:
    return [ExpectedSnapshot(d, k) for d, kinds in SNAPSHOT_SOURCES.items() for k in kinds]


def voice_text() -> str:
    """The owner's writing voice, or "" if none is recorded. Never raises."""
    try:
        return WRITING_VOICE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def samples_text() -> str:
    """The owner's own writing, or "" if none is recorded. Never raises."""
    try:
        return WRITING_SAMPLES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

# ---- single-writer guard ----------------------------------------------------
# State is plain JSON, so concurrent writers would corrupt it. Only the daemon
# process is allowed to write; every other caller goes through the HTTP API.
# The daemon flips this flag at startup.
#
# This was an honor system, and an honor system is not a guard. Any script
# could `import config; config.mark_daemon()` and become a second writer to live
# state. That happened, from throwaway test scripts, while the daemon was up:
# two writers doing read-modify-write on tasks.json dropped 11 tasks. So the
# claim is now checked against the pidfile rather than trusted.

_IS_DAEMON = False


class NotTheDaemon(RuntimeError):
    """Raised when a process claims the writer role while the daemon holds it."""


def mark_daemon(*, force: bool = False) -> None:
    """Claim the writer role. Refuses if a live daemon already holds it.

    `force` exists only for `otto serve --force`, which deliberately supersedes a
    wedged daemon. Never pass it from a script: if you want to change state while
    the daemon is up, use the HTTP API, which is the whole reason the API exists.
    """
    global _IS_DAEMON
    if not force:
        holder = _pidfile_holder()
        if holder is not None and holder != os.getpid():
            raise NotTheDaemon(
                f"the Otto daemon (pid {holder}) already holds the writer role. "
                "Refusing to become a second writer to live state. Use the HTTP "
                "API instead: otto <command>, or PATCH http://127.0.0.1:8787/api/..."
            )
    _IS_DAEMON = True


def _pidfile_holder() -> int | None:
    """The pid in the pidfile, if that process is alive. Local to avoid importing
    daemon.py (which imports this module)."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    try:
        import psutil
        p = psutil.Process(pid)
        return pid if p.is_running() and "python" in (p.name() or "").lower() else None
    except Exception:  # noqa: BLE001 - psutil.NoSuchProcess, AccessDenied, ImportError
        return None


def is_daemon() -> bool:
    return _IS_DAEMON


def ensure_dirs() -> None:
    # PRODUCERS_DIR is deliberately absent: it lives in the repo and is committed,
    # so creating it at runtime would only ever mask a broken checkout.
    for d in (OTTO_HOME, STATE_DIR, LOG_DIR, FEED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def utf8_output() -> None:
    """Make stdout and stderr survive the content Otto actually prints.

    The calendar carries emoji: Reclaim names its blocks "\U0001f6e1 \U0001f371 Lunch"
    and people put them in invite titles. In a PowerShell console Python already picks
    utf-8 and those render. Redirect or pipe the same command and stdout falls back to
    the locale codepage, cp1252 on this machine, and `otto agenda` dies with
    UnicodeEncodeError PART WAY THROUGH its output: three events printed, then a
    traceback. Anything reading Otto through a pipe hits it, which includes redirecting
    to a file, `| Select-String`, and any automation capturing CLI output. The
    conformance harnesses hit it too, which is why this lives here rather than in
    cli.py: one implementation, called by every entry point that prints.

    `errors="replace"` on purpose. A character Otto cannot encode should cost one
    glyph, never the rest of the screen.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass  # a stream that cannot be reconfigured is not worth failing over

# ---- feed trust levels ------------------------------------------------------
# A feed file is UNTRUSTED INPUT. It may have been written by an agent, and an
# agent-written file must not be able to arm anything unattended: a feed that can
# raise a crit notice or file a task with auto=True is a feed that can start
# agents. Trust is therefore declared per source, and it is a CEILING enforced by
# the ingester -- never something the feed file itself can claim.
#
#   data      may become a panel (a Snapshot) and nothing else. Cannot file board
#             cards. Cannot raise anything above `info`. This is the default and
#             it is what almost every producer should be.
#   advisory  may additionally PROPOSE board cards, which go through
#             findings.file_tasks: fingerprint dedup, capped per run, overflow
#             logged rather than dropped silently, and auto=False always. So a
#             proposal lands in the backlog for a human to pull, exactly like an
#             agent's finding, and can never dispatch itself.
#
# There is no third level, and in particular there is no level that permits
# `crit` or auto-dispatch. Those stay reserved for Otto's own detectors, which
# are code in this repo rather than a JSON file anyone can drop.
TRUST_DATA = "data"
TRUST_ADVISORY = "advisory"
FEED_TRUST_LEVELS = (TRUST_DATA, TRUST_ADVISORY)

# Hard ceiling on the notice level a feed can reach, per trust level. Clamped, not
# validated: a feed asking for `crit` gets `warn` and a logged note, because
# rejecting the whole file would let a bad level field hide real content.
FEED_MAX_NOTICE_LEVEL = {TRUST_DATA: "info", TRUST_ADVISORY: "warn"}


class FeedSource(NamedTuple):
    """A DECLARED feed. Same declare-then-diff shape as CAPABILITIES above.

    Declaration is what makes a feed ingestable at all. An undeclared directory
    under FEED_DIR is never read: it is reported as drift and ignored. That is the
    load-bearing half of the trap this design has to avoid -- if merely creating a
    directory were enough to get ingested, then anything that can write a file
    could put cards on the owner's board, and "any producer can contribute" would mean
    "any writer can act". Declaring a source is the human step.
    """

    name: str                       # directory under FEED_DIR, exactly
    domain: str                     # work | personal
    trust: str = TRUST_DATA         # ceiling on what this source may do
    max_age_hours: float | None = None   # declared freshness; None = never stale
    title: str = ""                 # what the panel calls it
    why: str = ""                   # what it is for, and who produces it

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.name}"

    @property
    def kind(self) -> str:
        """Snapshot kind. Namespaced so a feed can never collide with, or be
        mistaken for, a snapshot Otto fetches itself (agenda, mail)."""
        return f"feed:{self.name}"


def feed_source(name: str) -> FeedSource | None:
    return next((s for s in FEED_SOURCES if s.name == name), None)


class Capability(NamedTuple):
    name: str                    # the registry key or loader filename, exactly
    surface: str                 # one of SURFACES
    expect: str = "present"      # "present" | "absent"
    why: str = ""                # only for expect="absent": what made it a regression

    @property
    def key(self) -> str:
        return f"{self.surface}/{self.name}"


# ---------------------------------------------------------------------------
# Agent identity: WHO Otto authenticates as, per integration.
# ---------------------------------------------------------------------------
#
# CAPABILITIES above answers "is the credential there". This table answers the
# question nothing else in Otto asks: "whose credential is it, and what will the
# target system's own audit log say after Otto acts".
#
# For a single-operator deployment the honest answer is mostly "the owner's". Otto
# runs as the owner, with the owner's MCP registrations and the owner's tokens, so
# an action Otto takes in the IdP, the cloud account, Slack or Drive is
# indistinguishable from one the owner took by hand. Observability, gating and
# policy-in-code are solved (the board, stored events, `WEEKEND_PAUSES_WORK`).
# Identity and privilege scoping are NOT, and the difference matters more than any
# of them, because every other control is a claim Otto makes about itself while
# attribution is a claim the target system makes about Otto.
#
# So this table exists to make that gap COUNTED rather than caveated. It is the
# scoreboard for closing it and the compensating-control artifact a risk-acceptance
# doc can point at: a specific number of integrations, each named, each with the
# reason it is not yet scoped and the audit field that would prove it if it were.
#
# NAMES ONLY, same rule as CAPABILITIES. Principal *identifiers* are fine (a client
# id is not a secret and is what the audit log shows); values never are.
#
# Two states that read alike and are not:
#   human           authenticates as the owner. Agent action == human action in the log.
#   shared-service  a non-human principal, but one the owner's interactive sessions
#                   and other automation also use. Better than `human` and still not
#                   attribution: the log names the app, not who drove it.
# `agent` is the target: a principal only Otto holds.
#
# THE ALARM IS REGRESSION, NOT THE STANDING GAP. INHERITED_BASELINE below is the
# count of `human` rows as of the last review. `gaps()` fires when the real count
# EXCEEDS it, i.e. when a new integration gets wired to the owner's account. The
# standing gap is reported by `otto identity` and tracked on the board; nagging about
# it daily would teach the owner to ignore the one row that means something new just
# broke.

PRINCIPAL_AGENT = "agent"            # a principal only Otto holds
PRINCIPAL_SHARED = "shared-service"  # non-human, but shared with the owner/other automation
PRINCIPAL_HUMAN = "human"            # the owner's own account
PRINCIPAL_UNVERIFIED = "unverified"  # part of the surface, not yet assessed

PRINCIPAL_KINDS = (PRINCIPAL_AGENT, PRINCIPAL_SHARED,
                   PRINCIPAL_HUMAN, PRINCIPAL_UNVERIFIED)


class Principal(NamedTuple):
    name: str                    # integration key; matches a CAPABILITIES name where one exists
    surface: str                 # one of SURFACES, or SURFACE_EXTERNAL for non-MCP integrations
    principal: str               # one of PRINCIPAL_KINDS
    credential: str              # one of CRED_LOCATIONS
    audit: str                   # what the TARGET system's log shows after Otto acts
    blocker: str = ""            # what stands between this row and `agent`
    identifier: str = ""         # the non-secret principal id/name, if known

    @property
    def key(self) -> str:
        return f"{self.surface}/{self.name}"

    @property
    def attributable(self) -> bool:
        return self.principal == PRINCIPAL_AGENT


class Settings(types.SimpleNamespace):
    """One resolution of every setting. `current()` is the live one; the module
    globals mirror it so `config.X` keeps working everywhere."""


# Keys whose value is bound when the daemon is built, so a write to the settings
# file applies to them only at the next start. Everything else is read at call
# time and a reload() is enough. setup.write_settings reads this to decide what
# to report; keep it honest by grepping for module-level `= config.` captures
# (daemon.POLL_RUNS_EVERY, the FastAPI app and its OriginGuard, the Store).
RESTART_KEYS = frozenset({
    "OTTO_HOME",             # the Store, LOG_DIR, PID_FILE and the people dir are built from it
    "OTTO_HOST",             # bind address, and the origin guard's loopback defaults
    "OTTO_PORT",             # same, plus every hook on the box dials it
    "OTTO_URL",              # folded into ALLOWED_ORIGINS/HOSTS when the app is built
    "OTTO_ALLOWED_ORIGINS",  # OriginGuard middleware is added once, at app creation
    "OTTO_ALLOWED_HOSTS",    # same
    "OTTO_SCOPE",            # decides what the daemon and the CLI import
    "OTTO_TICK",             # daemon.POLL_RUNS_EVERY is bound at import
})

# The identity defaults resolve() falls back to when neither the environment nor
# otto.env says otherwise: the closed position, no owner and no roots. Kept out
# of the resolver so one assignment pins the fresh-install position in a test
# (an install that filled these in cannot un-fill them through the environment,
# because an empty OTTO_WORK_ROOTS means "use the default").
_DEFAULT_OWNER_NAME = "the owner"
_DEFAULT_ORG_NAME = "your organization"
_DEFAULT_WORK_ROOTS: tuple[Path, ...] = ()
_DEFAULT_PERSONAL_ROOTS: tuple[Path, ...] = ()

# Locals of resolve() that are not settings: its parameters, the working mapping,
# and the helpers that read it.
_NOT_SETTINGS = frozenset({"environ", "path", "env", "_csv_env", "_base_url_parts",
                           "_allowed_origins", "_allowed_hosts", "_csv", "_path_list",
                           "_milestones", "_capabilities"})


def resolve(environ: Mapping[str, str] | None = None, path: Path | None = None) -> Settings:
    """Compute every setting from `environ` (os.environ when None) and the settings
    file, without touching this module's globals.

    With no `environ`, the file at `path` (default: the one OTTO_HOME names) is
    first applied into os.environ for the keys the environment lacks, so children
    inherit it. With an explicit mapping nothing is applied anywhere: the mapping
    is the whole truth, which is what a test or a dry run wants.
    """
    if environ is None:
        env: Mapping[str, str] = os.environ
        SETTINGS_PATH = Path(path) if path is not None else _settings.default_path()
        SETTINGS_APPLIED: tuple[str, ...] = tuple(_settings.apply(SETTINGS_PATH))
    else:
        env = environ
        SETTINGS_PATH = Path(path) if path is not None else _settings.default_path(environ)
        SETTINGS_APPLIED = ()


    # ---- persona ----------------------------------------------------------------

    PERSONA_NAME = "Otto"
    PERSONA_BLURB = "holds the floors so you can raise the ceilings"

    # ---- identity ---------------------------------------------------------------
    # Who Otto works for, and where. Nothing in this tree names a person, a company, a
    # mail domain or a Slack id: all of it comes from the environment, and every default
    # is the closed position. A feature that needs a value nobody set says so rather
    # than guessing, because a guess here lands on a colleague, not on a log line.
    #
    #   OWNER_NAME       how prompts refer to the person Otto works for, third person
    #   OWNER_EMAIL      "" means unconfigured
    #   ORG_DOMAINS      mail domains whose people Otto may address unattended. Empty
    #                    means outreach refuses everyone, which is the right default for
    #                    a system that can message people on its own initiative.
    #   CONTRACTOR_DOMAINS  mail domains that count as contractors (a separate switch
    #                    gates whether Otto may address them at all)


    def _csv(name: str) -> tuple[str, ...]:
        """A comma-separated env var as a tuple, blanks dropped."""
        return tuple(x.strip() for x in env.get(name, "").split(",") if x.strip())


    def _path_list(name: str) -> tuple[Path, ...]:
        """An os.pathsep-separated env var as Paths, blanks dropped."""
        return tuple(Path(p.strip()) for p in env.get(name, "").split(os.pathsep)
                     if p.strip())


    OWNER_NAME = env.get("OTTO_OWNER_NAME", _DEFAULT_OWNER_NAME).strip() or _DEFAULT_OWNER_NAME
    OWNER_EMAIL = env.get("OTTO_OWNER_EMAIL", "").strip().lower()
    OWNER_SLACK_ID = env.get("OTTO_OWNER_SLACK_ID", "").strip()
    # Slack user ids of bots that are known and sanctioned in the workspace. A reply from
    # one of these is not a human resolution and must never be read as one.
    KNOWN_BOT_SLACK_IDS = _csv("OTTO_KNOWN_BOT_SLACK_IDS")
    ORG_NAME = env.get("OTTO_ORG_NAME", _DEFAULT_ORG_NAME).strip() or _DEFAULT_ORG_NAME
    ORG_DOMAINS = tuple(d.lower().lstrip("@") for d in _csv("OTTO_ORG_DOMAINS"))
    CONTRACTOR_DOMAINS = tuple(d.lower().lstrip("@") for d in _csv("OTTO_CONTRACTOR_DOMAINS"))

    # ---- scope ----------------------------------------------------------------
    # Which half of Otto this daemon is.
    #
    #   core       the orchestrator the README describes: board, sessions, dispatch,
    #              schedules, hooks, ledger, runs, feeds, decisions, journal, nudges,
    #              priorities, retire, chat, refresh, today. The default.
    #   assistant  core plus the owner's assistant modules (outreach, writing, people,
    #              prep, wellbeing, summon, inbox, meetings): their routes, their tick
    #              hooks, their schedules and their subcommands.
    #
    # A scope, not pip extras, because nothing in the assistant needs a package the
    # core lacks; what differs is what the daemon loads. otto/assistant/__init__.py is
    # the boundary and tests/test_scope.py holds it. An unknown value is clamped to
    # core rather than raised, so a typo in otto.env cannot keep the daemon down; the
    # clamp is recorded in SCOPE_WARNING for `otto doctor` to show.
    SCOPES = ("core", "assistant")
    _scope_raw = env.get("OTTO_SCOPE", "core").strip().lower() or "core"
    SCOPE = _scope_raw if _scope_raw in SCOPES else "core"
    SCOPE_WARNING: str | None = (
        None if _scope_raw in SCOPES
        else f"OTTO_SCOPE={_scope_raw!r} is not one of {', '.join(SCOPES)}; running core")
    ASSISTANT = SCOPE == "assistant"

    # ---- domains ----------------------------------------------------------------
    # Otto spans work and personal life. Everything it tracks carries a domain so the
    # two never blur together in a view, an alert, or a Slack post.

    WORK = "work"
    PERSONAL = "personal"
    DOMAINS = (WORK, PERSONAL)

    # ---- paths ------------------------------------------------------------------

    HOME = Path.home()
    CLAUDE_DIR = HOME / ".claude"
    # Where Claude Code writes session transcripts. A hook payload's transcript_path is
    # read only when it is under one of these (sessions.transcript_allowed): the path
    # arrives in a POST any local process can send, and reading whatever it names
    # would put the head of an arbitrary file into a session title, or, for a UNC
    # path, hand the owner's NTLM credentials to whatever SMB server it names.
    # CLAUDE_CONFIG_DIR is Claude Code's own override of ~/.claude.
    TRANSCRIPT_ROOTS = tuple(dict.fromkeys(
        [CLAUDE_DIR / "projects"]
        + ([Path(env["CLAUDE_CONFIG_DIR"]) / "projects"]
           if env.get("CLAUDE_CONFIG_DIR") else [])))

    # This repo, derived from where this file sits rather than hardcoded, so a clone
    # somewhere else still resolves. Needed because a spawned session has to be told an
    # absolute path to the CLI wrapper: `otto` is a shell function, not an executable.
    OTTO_REPO = Path(__file__).resolve().parent.parent

    # State lives under ~/.claude/otto, NOT in the repo. The repo is code only.
    OTTO_HOME = Path(env.get("OTTO_HOME", CLAUDE_DIR / "otto"))
    STATE_DIR = OTTO_HOME / "state"
    LOG_DIR = OTTO_HOME / "logs"
    PID_FILE = OTTO_HOME / "daemon.pid"

    # How the owner wants to be worked with: what depletion looks like on them, which of
    # their own conclusions not to take at face value, when to push and when not to.
    # Written by the owner, in their own words.
    #
    # UNDER OTTO_HOME AND NOT IN THE REPO, and that is not incidental. It is health and
    # family context about one person, the same class as the people dossiers that
    # .gitignore already keeps out of the tree. Loading it from a path means it can be
    # edited without a deploy, and absent without breaking anything.
    PROFILE_PATH = Path(env.get("OTTO_PROFILE", OTTO_HOME / "profile.md"))

    # What matters right now. Same shape as the profile and for the same reason: prose,
    # short, changes a few times a year, editable without a deploy.
    #
    # It lives here rather than in CLAUDE.md because CLAUDE.md once carried "the <month>
    # demo is the top priority" for two months after the demo had happened, steering
    # every session toward a milestone in the past. Nobody lied; a dated fact was written
    # somewhere with no expiry. See otto/priorities.py.
    PRIORITIES_PATH = Path(env.get("OTTO_PRIORITIES", OTTO_HOME / "priorities.md"))

    # After this long unreviewed, Otto says so. Moving the sentence out of CLAUDE.md does
    # not stop it going stale, it only moves where it rots; being NOTICED is the fix.
    #
    # 45 days: long enough that a steady quarter needs one touch, short enough that a
    # finished milestone cannot sit there being read into every session for two months.
    PRIORITIES_MAX_AGE_DAYS = int(env.get("OTTO_PRIORITIES_MAX_AGE", "45"))

    # ---- daemon -----------------------------------------------------------------

    HOST = env.get("OTTO_HOST", "127.0.0.1")
    PORT = int(env.get("OTTO_PORT", "8787"))
    BASE_URL = env.get("OTTO_URL", f"http://{HOST}:{PORT}")

    # Who may talk to the daemon from a browser. The daemon has no authentication, so
    # a web page in any tab could otherwise drive it (otto/originguard.py has the
    # attacks). A request carrying an Origin must match ALLOWED_ORIGINS; every
    # request's Host must name one of ALLOWED_HOSTS (the DNS rebinding defense).
    # Requests with no Origin (hook, CLI, curl) are not browsers and pass. Both are
    # comma-separated and ADD to the defaults, which are the loopback names on PORT;
    # list a forwarded port's origin here, e.g. http://localhost:9000 for an SSM
    # tunnel. There is deliberately no wildcard.
    def _csv_env(name: str) -> list[str]:
        return [v.strip() for v in env.get(name, "").split(",") if v.strip()]


    def _base_url_parts() -> tuple[str | None, str | None]:
        """(origin, hostname) of OTTO_URL, so a CLI pointed at a name the owner chose is
        not refused by the daemon it is pointed at."""
        try:
            u = urlsplit(BASE_URL)
        except ValueError:
            return None, None
        if not u.scheme or not u.netloc:
            return None, None
        return f"{u.scheme}://{u.netloc}", u.hostname


    def _allowed_origins() -> list[str]:
        origin, _host = _base_url_parts()
        extra = [origin] if origin else []
        return default_origins(PORT, HOST) + extra + _csv_env("OTTO_ALLOWED_ORIGINS")


    def _allowed_hosts() -> list[str]:
        _origin, host = _base_url_parts()
        return default_hosts(HOST) + ([host] if host else []) + _csv_env("OTTO_ALLOWED_HOSTS")


    ALLOWED_ORIGINS = _allowed_origins()
    ALLOWED_HOSTS = _allowed_hosts()

    TICK_SECONDS = int(env.get("OTTO_TICK", "15"))

    # ---- AWS --------------------------------------------------------------------
    # When every profile is AWS SSO with no default, a bare sts call always fails, so
    # Otto probes ONE named profile to answer "is my SSO session live". Point this at the
    # profile whose session the automation actually depends on.
    AWS_PROFILE = env.get("OTTO_AWS_PROFILE", "default").strip() or "default"

    # Which integration probes run. Set means exactly these; "none" means no probe at
    # all; UNSET MEANS NONE TOO. A probe talks to an outside service (the Anthropic
    # probe calls the API, the AWS probe shells to the CLI), and nothing may leave the
    # box until it is configured, so `serve` without `setup` probes nothing. `otto setup`
    # writes this, and an install that predates the setting gets every probe it already
    # had written into otto.env once at daemon start (setup.pin_integrations), so
    # nothing changes for it. A probe for a product nobody here uses manufactures a red
    # row that can never go green, which is why the choice is explicit.
    _integrations_raw = env.get("OTTO_INTEGRATIONS")
    INTEGRATIONS_SET = _integrations_raw is not None
    INTEGRATIONS: tuple[str, ...] = tuple(
        x.strip().lower() for x in (_integrations_raw or "").split(",")
        if x.strip() and x.strip().lower() != "none")

    # ---- discovery --------------------------------------------------------------

    # Roots scanned for per-project .claude config, each mapped to a domain. Missing
    # roots are skipped silently, so this is safe to over-specify. os.pathsep-separated
    # in the env (`;` on Windows, `:` elsewhere), because paths contain commas and
    # colons and a separator that appears inside the values is no separator.
    WORK_ROOTS: tuple[Path, ...] = _path_list("OTTO_WORK_ROOTS") or _DEFAULT_WORK_ROOTS
    PERSONAL_ROOTS: tuple[Path, ...] = _path_list("OTTO_PERSONAL_ROOTS") or _DEFAULT_PERSONAL_ROOTS

    DOMAIN_ROOTS: dict[Path, str] = {
        **{p: WORK for p in WORK_ROOTS},
        **{p: PERSONAL for p in PERSONAL_ROOTS},
    }

    REPO_ROOTS = list(DOMAIN_ROOTS)

    # Directories inside a .claude dir that hold definitions, mapped to entry kind.
    DEFINITION_DIRS = {
        "agents": "agent",
        "commands": "command",
        "skills": "skill",
    }

    # ---- retention --------------------------------------------------------------

    RUN_HISTORY_LIMIT = 500
    EVENT_HISTORY_LIMIT = 2000
    SESSION_LIMIT = 200

    # ---- dashboard state payload -------------------------------------------------
    # GET /api/state is what the dashboard polls, and on a board of several hundred
    # cards it runs to megabytes, most of it the task list and most of that `detail`.
    # The payload is computed once per store version (store.version, bumped by every
    # write) and served from cache while nothing changes; STATE_IDLE_CACHE_SECONDS
    # bounds how long the clock-driven fields (due flags, machine snapshot, live
    # sessions) may lag while the store is quiet. A write invalidates the cache at once.
    STATE_IDLE_CACHE_SECONDS = float(env.get("OTTO_STATE_IDLE_CACHE", "10"))
    # Task `detail` in the state payload is cut to this many characters and flagged
    # `detail_truncated: true`; the card clamps to three lines so the rest is never
    # drawn. GET /api/tasks/{id} returns the full task for the inspector.
    STATE_DETAIL_CHARS = int(env.get("OTTO_STATE_DETAIL_CHARS", "600"))
    # How many runs and events ride in the payload. The dashboard renders every run
    # it is handed (grouped by day) and slices events at 60, so these match what it
    # shows rather than trimming history out from under it.
    STATE_RUNS = int(env.get("OTTO_STATE_RUNS", "80"))
    STATE_EVENTS = int(env.get("OTTO_STATE_EVENTS", "60"))
    # WebSocket /ws/events (otto/web_events.py): how often the socket checks
    # store.version, and how often it pings an idle socket so proxies and the browser
    # keep it open.
    WS_EVENTS_POLL_SECONDS = float(env.get("OTTO_WS_EVENTS_POLL", "0.25"))
    WS_EVENTS_PING_SECONDS = float(env.get("OTTO_WS_EVENTS_PING", "25"))

    # ---- sessions ----------------------------------------------------------------
    # Live Claude Code session state, reported by hooks (see otto/sessions.py).
    #
    # Every threshold here is state-aware, because silence means different things in
    # different states. An idle session emits no hooks at all and that is normal; a
    # session that said `busy` and then went quiet for hours is a different claim.
    #
    # Otto has no PTY and cannot ask a session whether it is alive, so it never
    # converts silence into a verdict about what happened. It converts silence into
    # `offline` only after a long gap, and records that the conclusion was inferred.

    # Blocked on a permission prompt for this long: the owner is the blocker, say so.
    # 15 minutes rather than something snappier because the common case is that they ARE
    # at the keyboard and answer in seconds, and an alert that fires before they could
    # plausibly have missed it is one they learn to ignore.
    SESSION_WAITING_ALERT_MINUTES = int(env.get("OTTO_SESSION_WAITING_MIN", "15"))

    # A turn that has been running this long without a Stop. Informational, not an
    # alarm: a deep agent turn genuinely can run this long, and the point is only to
    # make "this may have died mid-turn" visible instead of leaving `busy` on screen
    # forever with no way to tell.
    SESSION_BUSY_ALERT_HOURS = int(env.get("OTTO_SESSION_BUSY_HOURS", "2"))

    # No hook of any kind for this long: presume the session is gone. Generous,
    # because an idle session legitimately emits nothing overnight and calling one
    # offline at 09:00 that the owner is about to type into would make the panel a liar.
    SESSION_OFFLINE_HOURS = int(env.get("OTTO_SESSION_OFFLINE_HOURS", "12"))

    # How long an offline session stays in the list before it is dropped.
    SESSION_PRUNE_DAYS = int(env.get("OTTO_SESSION_PRUNE_DAYS", "7"))

    # The moment a session enters `waiting`, raise a toast naming the session and
    # what it asked. This is the immediate nudge; SESSION_WAITING_ALERT_MINUTES above
    # is the escalation that stays on the alerts list if it goes unanswered. Other
    # session dashboards do the same (OS notification + rail glow on `waiting`), and it
    # is the single feature that most reduces "which tab was asking me something".
    SESSION_WAITING_TOAST = env.get("OTTO_SESSION_WAITING_TOAST", "1") != "0"

    # Session titles are one line in a rail. Long enough for a sentence, short enough
    # that four of them fit on a screen next to their state.
    SESSION_TITLE_MAX = 72

    # ---- herdr: the harness the sessions live in --------------------------------
    # herdr (herdr.dev) owns the terminals: a background server that keeps every
    # pane running, one pane per Claude session, with idle/working/blocked read off
    # the screen and a socket API Otto can drive. Otto does not own PTYs and never
    # will; this is the piece it leans on instead. See otto/herdr.py.
    HERDR_BIN = env.get("OTTO_HERDR_BIN", "").strip() or None
    # Sync herdr's agent view into Otto's session rows on the tick.
    HERDR_SYNC = env.get("OTTO_HERDR_SYNC", "1") != "0"
    # Arguments Otto starts `claude` with inside a pane. The default assumes every
    # session runs with skip-permissions and that the guard hook is what gates the
    # dangerous calls, not the permission prompt. Set it to "" to get prompts back.
    HERDR_CLAUDE_ARGS = env.get("OTTO_HERDR_CLAUDE_ARGS",
                                       "--dangerously-skip-permissions").split()
    # How long to wait for a freshly launched claude to show its prompt in a pane.
    HERDR_START_TIMEOUT = int(env.get("OTTO_HERDR_START_TIMEOUT", "90"))
    # How often the watcher thread polls herdr's snapshot for the session rail.
    HERDR_POLL_SECONDS = float(env.get("OTTO_HERDR_POLL", "2"))
    # Otto's own runs (schedules, board tasks, card replies) happen inside panes of
    # an `otto-runs` workspace when the server is up, so the owner can watch them live
    # with scrollback next to their own sessions. Off, or server down, means the plain
    # detached process as before. See otto/runners/herdrpane.py.
    HERDR_RUNS = env.get("OTTO_HERDR_RUNS", "1") != "0"
    # A finished run's pane stays this long for scrollback, then the tick closes it.
    HERDR_RUNS_KEEP_MINUTES = int(env.get("OTTO_HERDR_RUNS_KEEP_MINUTES", "60"))
    # Start the herdr server when the daemon comes up, so the harness is there before
    # the first tick looks for panes. Off only for a machine that runs herdr itself.
    HERDR_AUTOSTART = env.get("OTTO_HERDR_AUTOSTART", "1") != "0"
    # Name the herdr window "Otto · N idle · M working · K blocked", once per change.
    HERDR_WINDOW_TITLE = env.get("OTTO_HERDR_WINDOW_TITLE", "1") != "0"

    # ---- logistics dispatch -------------------------------------------------------
    # Suggestions shown in the strip at once, and the floor below which a pairing is
    # not worth proposing. Three is about what fits above a board without crowding it.
    LOGISTICS_MAX_PROPOSALS = int(env.get("OTTO_LOGISTICS_MAX", "3"))
    LOGISTICS_MIN_CONFIDENCE = float(env.get("OTTO_LOGISTICS_MIN_CONF", "0.35"))
    # After a hand-off, how long the pane watcher ignores an idle reading, so the
    # agent has time to start its turn before "idle" is taken to mean "finished".
    LOGISTICS_SETTLE_SECONDS = int(env.get("OTTO_LOGISTICS_SETTLE", "45"))

    # ---- models -----------------------------------------------------------------
    # Which model a spawned session runs on. Read this before changing any of the
    # per-module model settings further down: they all describe themselves as
    # overriding "the default", and this is now that default.
    #
    # It used to be implicit. Every spawn path that did not pass --model inherited
    # whatever `model` happened to be in ~/.claude/settings.json, and configsync.py
    # is explicit that Claude Code owns and rewrites that file itself (plugin
    # toggles, permission grants). So Otto's default model was one /config click away
    # from changing silently, with nothing in Otto asserting otherwise. Now Otto says
    # it out loud and passes it on every spawn.
    #
    # `opus[1m]` rather than `claude-opus-5` because that is the exact string
    # settings.json already held, so pinning it changed nothing that was running.
    # The alias form is accepted by `claude --model` (verified 2026-08-12).
    DEFAULT_MODEL = env.get("OTTO_DEFAULT_MODEL", "opus[1m]").strip()

    # The escape hatch, opt-in per task and never automatic. Fable is Anthropic's most
    # capable model and is priced ABOVE Opus ($10/$50 per MTok against $5/$25), so it
    # is worth it only where the work is genuinely long-horizon or hard enough that a
    # better answer beats the bill.
    #
    # Deliberately a tag and NOT the board's risk tier. `tier` on a card
    # (tier-0-autonomous / tier-1-approval / tier-2-assistive) measures how much
    # autonomy the work may have, not how hard it is: tier-0 is read-only triage, and
    # tier-2 is never dispatched by Otto at all. Hanging the priciest model off that
    # axis would aim it at the cheapest jobs or at cards that never run. Difficulty
    # needs its own signal, so it gets one.
    DEEP_TAG = env.get("OTTO_DEEP_TAG", "deep").strip()
    DEEP_MODEL = env.get("OTTO_DEEP_MODEL", "claude-fable-5").strip()

    # Its own ceiling, above TASK_MAX_BUDGET_USD, because the whole point of reaching
    # for this model is a job that runs long. Capping a deep run at the ordinary task
    # budget would kill it mid-work having already paid the premium for the thinking.
    DEEP_BUDGET_USD = float(env.get("OTTO_DEEP_BUDGET_USD", "15"))

    # Slash commands run with `--strict-mcp-config` against an empty config, which
    # drops every MCP server from the session.
    #
    # EMPTY, AND READ THIS BEFORE ADDING A NAME. The prize is real: measured
    # 2026-08-12, a session with the local MCP servers loaded pays a 33,577-token
    # cached prefix before it reads anything, against 5,302 with none. Four local
    # servers (one of them exposing 200+ tools) were 28,275 tokens of every session,
    # re-read on every turn.
    #
    # But strict mode removes the Claude.ai CONNECTORS too (Slack, Gmail, Calendar,
    # Notion, Drive, Miro), not just the servers named in ~/.claude.json. There is no
    # way to keep them: they do not come from a config file this can substitute.
    #
    # Tried on slack-sweep and reverted the same hour. The run cost $0.35 instead of
    # $1.03 and looked like a 66% win. It was a run that did nothing: five ToolSearch
    # calls for "slack" returned "No matching deferred tools found", zero Slack tools
    # were called, and it ended with "No Slack MCP tool is connected in this session,
    # so I can't run the sweep."
    #
    # The check that misled me was asking a strict-mode session "do you have a tool
    # whose name contains slack?" - it answered YES, because deferred tool NAMES are
    # visible even when the tool cannot be loaded. A capability probe has to CALL the
    # tool and assert on the result, never ask whether it exists.
    #
    # So this only ever fits a command that needs no MCP server and no connector at
    # all: pure Bash/file work. Verify by calling a tool in strict mode, not by asking.
    MCP_FREE_COMMANDS = frozenset(
        c.strip() for c in env.get("OTTO_MCP_FREE", "").split(",") if c.strip()
    )

    # Slash commands that sweep a time window into a feed, mapped to the feed they
    # write. Otto injects an incremental window into these runs and advances a
    # watermark from the coverage they report back: see feeds.coverage_hint().
    #
    # A command belongs here only if it BOTH writes the named feed and reports
    # `coverage_since` in its drop. A command that is listed but does not report
    # coverage still works: the watermark never advances, so it keeps reading the full
    # default window and simply gains nothing.
    #
    # EMPTY since 2026-08-12, and slack-sweep is why. It was the only entry, and it no
    # longer needs the hint: retrieval moved out of the session into
    # producers/slack_dm_producer.py, which keeps a watermark PER CONVERSATION rather
    # than one for the whole sweep. That is strictly better (a busy DM and a dormant one
    # resume independently) and it is computed rather than self-reported, so there is
    # nothing for the model to be honest about.
    #
    # The mechanism stays because it is the right shape for any future sweep that still
    # does its own retrieval, and because feeds.coverage_hint and next_watermark are
    # tested independently of who uses them.
    SWEEP_FEED_COMMANDS: dict[str, str] = {}

    # ---- dossier contact tracking ---------------------------------------------------
    # producers/slack_dm_producer.py writes per-conversation direction facts into its
    # spool, and the daemon turns them into dossier `last_contact` dates. The producer
    # cannot import this module (producers do not depend on the thing they feed), so the
    # spool path is defined in both places and this one must match its default.
    SLACK_SPOOL_DIR = Path(env.get("OTTO_SLACK_SPOOL", OTTO_HOME / "slack-spool"))

    # A DM exchange counts as contact only when both directions happened within this many
    # days of each other. One-sided traffic is not a conversation: the owner pinging into
    # silence should keep the relationship reading as quiet, and so should a reply that
    # finally lands weeks after the question.
    CONTACT_EXCHANGE_WINDOW_DAYS = 4

    # ---- thread notes -------------------------------------------------------------
    # The owner writes a note against a quiet thread and Otto decides the verb
    # (otto/triage.py).
    # Each note is one spawned session, so the bounds here are what stop a triage sweep
    # from turning into a bill or a burst of colleague-facing messages.

    # Concurrency, not a queue. Triage is a human sitting there reading; more than a
    # couple in flight means the notes are being fired off faster than the answers can
    # be read, which is the state where nobody notices a wrong one.
    THREAD_NOTE_MAX_CONCURRENT = int(env.get("OTTO_THREAD_NOTE_CONCURRENT", "3"))

    # A floor between spawns, so a stuck send button cannot fan out 23 sessions.
    THREAD_NOTE_THROTTLE_SECONDS = int(env.get("OTTO_THREAD_NOTE_THROTTLE", "5"))

    # The model that does the judging. Haiku by default, and the reason is the shape of
    # the job rather than thrift for its own sake: picking one of four verbs and running
    # one command is bounded and mechanical, and every colleague-facing outcome is held
    # for review anyway.
    #
    # What makes the choice matter is the floor cost. A spawned session pays ~38k
    # cache-creation tokens for its system context before it reads a word of the prompt,
    # so the per-note bill is almost entirely that overhead, and it is the same overhead
    # whichever model pays it. Measured on the first live run: $0.79 a note on the
    # inherited default, which is $18 to triage the 23 quiet threads.
    #
    # Raising this to a premium model is one env var, but raise THREAD_NOTE_BUDGET_USD
    # with it or every run dies at the cap.
    THREAD_NOTE_MODEL = env.get("OTTO_THREAD_NOTE_MODEL", "claude-haiku-4-5-20251001")

    # Per-note ceiling. Must sit ABOVE the floor cost of a session on THREAD_NOTE_MODEL,
    # which is the mistake this constant already made once: it was set to 0.60 against a
    # session whose single turn cost 0.79, so the run was killed mid-tool-call having
    # already chosen the right answer, and reported only as "failed".
    THREAD_NOTE_BUDGET_USD = float(env.get("OTTO_THREAD_NOTE_BUDGET", "0.35"))

    # ---- comms ------------------------------------------------------------------
    # The owner's own one-way ops feed: where the daily summary lands and the only
    # channel Otto may post top-level into by default (see POST_CHANNELS). Name and id
    # both, because the id is what the API wants and the name is what a human reads in
    # `otto schedules`. Empty means no feed channel, and every post is refused until one
    # is configured.

    SLACK_CHANNEL = env.get("OTTO_SLACK_CHANNEL", "").strip().lstrip("#")
    SLACK_CHANNEL_ID = env.get("OTTO_SLACK_CHANNEL_ID", "").strip()

    # ---- weekends ---------------------------------------------------------------
    # The owner does not work weekends, so Otto does not either. On Sat/Sun a `work`
    # schedule is never due and never toasts; `personal` runs normally.
    #
    # The non-obvious half: staleness must agree. /daily has a 26h limit, so pausing it
    # Friday evening would trip a "loop has gone dark" alarm every Saturday. Weekend
    # hours are therefore excluded from the age of a work schedule, which keeps the
    # alarm meaningful instead of teaching the owner to ignore it every weekend.
    WEEKEND_PAUSES_WORK = True


    # ---- quiet hours --------------------------------------------------------------
    # Local hours in which a toast is HELD rather than fired. Not dropped: the notice is
    # still recorded and still owed a toast, it just waits for morning. `crit` is exempt.
    #
    # Written after a night that produced six warn toasts between 00:15 and 06:37 local,
    # all about one errand due the following day. Dedupe is the fix for the repetition;
    # this is the answer to the separate question of whether the one remaining toast was
    # worth waking up for.
    QUIET_FROM = int(env.get("OTTO_QUIET_FROM", "22"))   # inclusive
    QUIET_TO = int(env.get("OTTO_QUIET_TO", "7"))        # exclusive

    # A hard off switch for OS notifications, for anything that is not the real daemon.
    #
    # Redirecting OTTO_HOME to a temp directory sandboxes the STORE and nothing else. A
    # test harness that calls notify.deliver_pending still shells out to the toast script
    # and raises a real notification on a real desktop, and then deletes its temp store,
    # so the alarm has no record behind it to explain it. That happened on 2026-08-07: a
    # conformance run proving "crit breaks through quiet hours" put "Otto: Fleet-wide
    # outage" on the owner's screen with nothing in Otto to match it.
    #
    # Monkeypatching `_toast` in each harness works and is one forgotten line away from
    # doing it again, so the guarantee lives here instead. Every conformance script sets
    # OTTO_NO_TOAST=1 next to OTTO_HOME.
    TOASTS_ENABLED = env.get("OTTO_NO_TOAST", "") not in ("1", "true", "True")


    # How long the same subject stays deduped. A daily reminder that is genuinely still
    # outstanding should be able to say so once a day; anything more often is the same
    # sentence twice.
    NOTICE_DEDUPE_HOURS = int(env.get("OTTO_NOTICE_DEDUPE_HOURS", "20"))


    # ---- snapshots --------------------------------------------------------------
    # Some surfaces (calendar, mail) are only reachable through MCP servers, which
    # live in a Claude session and not in this daemon. Rather than fake access, Otto
    # accepts pushed snapshots and always shows how old they are.

    SNAPSHOT_KINDS = ("agenda", "mail")
    SNAPSHOT_STALE_HOURS = 12

    # What Otto is EXPECTED to be able to see, per domain. This is the switch, not a
    # description: listing a (domain, kind) here makes Otto report a HIGH gap while it
    # is missing. So a surface goes in only once there is actually an MCP server behind
    # it, otherwise Otto nags about a personal inbox that was never wired up, and that
    # is precisely the attention-spending it exists to avoid.
    #
    # work    -> the claude.ai Gmail/Calendar connectors, bound to the claude.ai login.
    # personal-> a separately registered local MCP server with its own Google grant.
    SNAPSHOT_SOURCES: dict[str, tuple[str, ...]] = {
        "work": ("agenda", "mail"),
        # Enabled 2026-07-30, after gmail_search and calendar_list_events were confirmed
        # answering for the `personal` alias. Not before: declaring a surface Otto cannot
        # actually reach makes it report a HIGH gap forever.
        "personal": ("agenda", "mail"),
    }


    # Which MCP tools the refresher should reach for, per domain, and which tools to
    # deny outright. The work connectors are account-level claude.ai ones bound to the
    # claude.ai login, so they cannot also serve a second Google account; a personal
    # inbox needs its own locally registered server. Filling in `personal` below and
    # adding its kinds to SNAPSHOT_SOURCES is the whole wiring job.
    #
    # `deny` is defense in depth only. The control that actually matters is the OAuth
    # scope granted to the server: a readonly grant cannot send mail no matter what the
    # prompt says or what the package decides to do.
    REFRESH_SOURCES: dict[str, dict[str, Any]] = {
        "work": {
            "hint": ("Use the claude.ai Google Calendar connector for events and the "
                     "claude.ai Gmail connector for mail. This is the work account."),
            "deny": ["mcp__claude_ai_Gmail__create_draft",
                     "mcp__claude_ai_Gmail__update_draft",
                     "mcp__claude_ai_Google_Calendar__create_event",
                     "mcp__claude_ai_Google_Calendar__update_event",
                     "mcp__claude_ai_Google_Calendar__delete_event"],
        },
        "personal": {
            # mcp-google-multi, registered as `google-multi`, holding only the `personal`
            # account alias. Work deliberately stays on the claude.ai connectors: they
            # already work and need no OAuth client of their own.
            #
            # Its tools are hidden until a `*_discover` call reveals them, which is what
            # keeps ~871 tools from landing in context on every session.
            # The category exclusions are not optional. Without -category:updates this
            # inbox returns ten listing price alerts and four newsletters per run, and a
            # personal snapshot that is 90% marketing is worse than no snapshot: it costs
            # attention every four hours and teaches the owner to stop reading the panel.
            "hint": (
                "Use the google-multi MCP server, account alias `personal`. Its tools are "
                "hidden until discovered, so if you cannot see gmail_search or "
                "calendar_list_events, call gmail_discover and calendar_discover first.\n"
                "  events: calendar_list_events, timeMin/timeMax bounding today only.\n"
                "  mail:   gmail_search with query exactly:\n"
                "            newer_than:1d -category:promotions -category:social "
                "-category:updates -category:forums\n"
                "This is a low-traffic personal inbox and the correct answer is usually an "
                "EMPTY list. Do not pad it. One item per threadId, never several copies of "
                "the same thread. Automated password resets, order confirmations, delivery "
                "notices and 'someone sent you a message' prompts are not reply-worthy.\n"
                "Drop spam and phishing rather than listing them. Gmail's category filters "
                "do not catch it all: a real one arrived in CATEGORY_PERSONAL wearing a "
                "`Re:` on a thread that never existed, a display name unrelated to the "
                "sending address, and a send date weeks before the subject it replied to. "
                "Treat those mismatches as the signal. Do NOT report it as something "
                "needing a reply, and never surface the link.\n"
                "Pass the `personal` account alias on every call. If a call returns an "
                "auth error, do NOT retry or attempt to re-authenticate: set that "
                "summary to \"unavailable: personal account not authenticated\"."
            ),
            "deny": [
                # On this server these denies are the PRIMARY control, not defense in
                # depth. mcp-google-multi ships send/modify/delete tools, so it requests
                # write scopes for itself; its own safety story is "writes deny-by-default"
                # at the tool layer, not a readonly OAuth grant. So there is no scope-level
                # backstop here and this list is what stands between the refresher and a
                # sent email. Keep it in sync if the server's tool names change.
                "mcp__google-multi__gmail_send",
                "mcp__google-multi__gmail_delete",
                "mcp__google-multi__gmail_trash",
                "mcp__google-multi__gmail_modify",
                "mcp__google-multi__calendar_create_event",
                "mcp__google-multi__calendar_update_event",
                "mcp__google-multi__calendar_delete_event",
            ],
        },
    }

    # ---- meeting notes ----------------------------------------------------------
    # Notion AI writes a page per meeting. The decisions and the "<owner> will..." lines
    # in those pages are real work, and until now they lived only in Notion, which
    # means they were only ever actioned if the owner reread the page. The ingester
    # turns them into board cards.
    #
    # Same risk class as the refresher above, and allowed to autostart for the same
    # reason: it reads Notion, writes nothing anywhere except Otto's own state, and
    # every mutating tool is both denied and outside its allow-list. It does NOT get
    # to queue work -- see MEETINGS_AUTOQUEUE.

    # The deny list itself lives in `meetings.DENIED_TOOLS`, next to the code it guards
    # and where the conformance harness can assert on it, exactly as refresh.py does. It
    # is the whole safety argument for autostarting this, so it does not belong in a
    # tunables file where widening it would look like a settings change.

    # Who "mine" means when the notes say "<name> will chase the cert". Anything owned by
    # somebody else is only filed when the owner is the one who has to chase it. The
    # name is matched against the attendee names Notion writes, so it should be the
    # owner's name as colleagues write it, not a handle. Defaults to OWNER_NAME, which
    # as a generic placeholder matches nobody, so set it.
    MEETINGS_OWNER = env.get("OTTO_MEETINGS_OWNER", OWNER_NAME).strip() or OWNER_NAME
    MEETINGS_OWNER_EMAIL = env.get("OTTO_MEETINGS_OWNER_EMAIL", OWNER_EMAIL).strip().lower()

    # How far back to look when there is no watermark yet, i.e. the first ever run.
    # Deliberately short: a first run that swept three months of meetings would file
    # a hundred stale cards and the board would be abandoned on day one.
    MEETINGS_FIRST_RUN_DAYS = int(env.get("OTTO_MEETINGS_FIRST_RUN_DAYS", "7"))

    # Per-run ceiling on filed cards, same reasoning as FINDINGS_MAX_PER_RUN. A day
    # with six meetings must not bury everything already on the board. Anything
    # dropped is reported, never silently truncated.
    MEETINGS_MAX_PER_RUN = int(env.get("OTTO_MEETINGS_MAX", "8"))

    # A due date this close raises a notice instead of only landing on the board.
    MEETINGS_DUE_SOON_DAYS = int(env.get("OTTO_MEETINGS_DUE_SOON_DAYS", "3"))

    # Page ids remembered, so a page is parsed exactly once. Well above the meeting
    # rate of a small company, and bounded so the ledger cannot grow forever.
    MEETINGS_SEEN_LIMIT = int(env.get("OTTO_MEETINGS_SEEN_LIMIT", "400"))

    # OFF, and it should stay off until the cards have been read for a few weeks.
    # On, a tier-0 action item extracted by a model from a transcript written by a
    # model would dispatch a skip-permissions session with nobody in the loop. Two
    # inference steps between "somebody said a thing in a meeting" and "an agent ran".
    # /orchestrate is the gate, and it is a human-shaped one on purpose.
    MEETINGS_AUTOQUEUE = env.get("OTTO_MEETINGS_AUTOQUEUE", "0") in ("1", "true", "True")

    # Extraction is mechanical: read a page, pull out the commitments, emit JSON. The
    # first live run inherited the default model and cost $1.71 for five meetings, which
    # at a four-hourly cadence is roughly $300/month to reread the same meeting notes.
    # Almost all of it is input: the session loads ~250 tool definitions and CLAUDE.md
    # before it reads a single page. Set to empty to inherit the default again.
    MEETINGS_MODEL = env.get("OTTO_MEETINGS_MODEL", "claude-sonnet-5").strip()

    # Hard ceiling per run, passed as --max-budget-usd. A schedule that runs unattended
    # six times a day needs a number it cannot exceed, not a hope that it will not.
    MEETINGS_BUDGET_USD = float(env.get("OTTO_MEETINGS_BUDGET_USD", "1.5"))

    # ---- chat -------------------------------------------------------------------
    # Each chat turn is a real `claude -p` run, so it costs money and takes seconds.
    # CHAT_CWD must be a workspace Claude Code already trusts, or the first turn will
    # block on the trust dialog and look like a hang.

    CHAT_CWD = Path(env.get("OTTO_CHAT_CWD", str(HOME)))
    CHAT_HISTORY_LIMIT = 60

    # Empty falls back to DEFAULT_MODEL. A turn carries the whole state snapshot plus
    # CHAT_CWD's CLAUDE.md, so turns are not cheap; set this to a smaller model id to
    # trade some judgement for cost.
    CHAT_MODEL = env.get("OTTO_CHAT_MODEL", "").strip()

    # A short mechanical fetch-and-format job, and now measured: 242k input tokens
    # against 1,636 output, $1.36 a run, 9.4 runs a day. That was $384/month to reformat
    # calendar and mail on the Opus default, where essentially all of the bill is input
    # overhead that any model pays identically.
    #
    # Haiku because the ratio says so, not because it is cheap. Nothing here needs
    # judgement: the model reads two connectors and emits JSON in a fixed shape, and
    # `_extract()` below already tolerates a fence or a stray sentence around it, which
    # is the only failure a smaller model plausibly adds.
    #
    # The precedent is thread notes: $0.79 a run on the default, $0.036 on Haiku, same
    # decision quality, 22x. Empty falls back to DEFAULT_MODEL.
    REFRESH_MODEL = env.get("OTTO_REFRESH_MODEL", "claude-haiku-4-5").strip()

    # ---- task auto-dispatch -----------------------------------------------------
    # A task in `queued` is dispatched as a real Claude Code session with
    # --dangerously-skip-permissions. That is a deliberate choice, and these are the
    # limits that keep it from becoming a runaway.
    #
    #   AUTODISPATCH        master switch. Flip it off and `queued` becomes inert.
    #   MAX_CONCURRENT      never more than this many task runs in flight at once.
    #   MAX_ATTEMPTS        a failed task goes to needs-you, it does NOT retry. Set
    #                       above 1 only if you want automatic retries.
    #   MIN_SECONDS_BETWEEN throttle, so a bad state cannot spawn a burst.
    #
    # Only STORED tasks are ever dispatched. Derived cards (a due schedule sitting in
    # the queued column) are never auto-run: launching /daily unattended is a very
    # different risk from running one task somebody explicitly queued.

    TASK_AUTODISPATCH = env.get("OTTO_AUTODISPATCH", "1") not in ("0", "false", "False")

    # Whether the seeded read-only schedules (refresh, meeting notes, writing ideas)
    # are armed on a fresh install. OFF by default: on a brand-new daemon they fired
    # on the first tick, before the owner had looked at anything, and spawned real
    # Claude sessions against the owner's own account. OTTO_AUTODISPATCH covers board
    # tasks only, not schedules. Arm them one by one with `otto schedule arm <name>`
    # once the integrations behind them are configured.
    SCHEDULES_ARMED_BY_DEFAULT = env.get("OTTO_ARM_DEFAULT_SCHEDULES", "0") in ("1", "true", "True")
    TASK_MAX_CONCURRENT = int(env.get("OTTO_TASK_CONCURRENCY", "2"))
    TASK_MAX_ATTEMPTS = int(env.get("OTTO_TASK_ATTEMPTS", "1"))
    TASK_MIN_SECONDS_BETWEEN = int(env.get("OTTO_TASK_THROTTLE", "20"))
    TASK_DEFAULT_CWD = Path(env.get("OTTO_TASK_CWD", str(HOME)))

    # Hard dollar ceiling per dispatched task, passed to Claude Code as
    # --max-budget-usd. Bounded discrete work deserves a bound. Schedules launched by
    # hand are deliberately NOT capped: /daily chains several steps and being cut off
    # mid-chain would leave partial state, which is worse than the spend. 0 disables.
    TASK_MAX_BUDGET_USD = float(env.get("OTTO_TASK_BUDGET_USD", "5"))

    # ---- writing ----------------------------------------------------------------
    # Short public posts (LinkedIn) drawn from the work the owner already did. The gap
    # this closes is a career one, not an ops one: a senior role carries an expectation
    # of mentorship, internal or external, and a depleted week leaves no energy for the
    # in-person kind. Writing about the work is the form of external mentorship that
    # fits, IF the ideas come to the owner instead of the owner having to go looking for
    # them. So the ideas are mined from what they already produce (the day rollups, the
    # decision log, the notices Otto raised) and never from a form.
    #
    # Two model calls, both bounded, both on a mid model: the ideas run once a week
    # and a draft run per post the owner picks. Neither needs a tool or an MCP server,
    # so the session is launched with the empty MCP config and every tool denied, which
    # is also what makes it cheap (see MCP_FREE_COMMANDS for the measurement).
    #
    # The confidentiality gate is deterministic and lives in writing.scan(): every
    # draft is checked for codenames, colleagues, hostnames, figures and personal
    # details before the owner sees it, and Otto never marks a post ready. The rule is
    # that unreleased product detail does not leave the company unless a person clears
    # it, and a scan that flags is how that stays a person's call.

    # The owner's voice, in their words. Hand-edited, like profile.md. Seeded once by
    # `writing.ensure_voice()` if missing, never overwritten after.
    WRITING_VOICE_PATH = Path(env.get("OTTO_WRITING_VOICE", OTTO_HOME / "writing-voice.md"))

    # Sonnet, not Haiku: the draft goes out under the owner's own name, so this is the
    # one place where the writing quality is the product. Not Opus either; the material
    # is small and the form is short. Empty falls back to DEFAULT_MODEL.
    WRITING_MODEL = env.get("OTTO_WRITING_MODEL", "claude-sonnet-5").strip()

    # Ceiling per run, passed as --max-budget-usd. An ideas run reads ~10k chars of
    # material and writes ~2k; a draft is smaller. A dollar is generous.
    WRITING_BUDGET_USD = float(env.get("OTTO_WRITING_BUDGET_USD", "1.0"))

    # Ideas kept per run. Five is a menu; twelve is a backlog, and a backlog of things
    # to write is the opposite of what a tired person needs.
    WRITING_IDEAS_MAX = int(env.get("OTTO_WRITING_IDEAS_MAX", "5"))

    # How far back the material reaches. A week matches the cadence, so nothing is
    # mined twice; the fingerprint check in harvest is the backstop if it is.
    WRITING_LOOKBACK_DAYS = int(env.get("OTTO_WRITING_LOOKBACK_DAYS", "7"))

    # Internal names that must not appear in anything public. Codenames, systems you
    # built, milestones, unreleased things. Comma-separated in the env; the scan is
    # case-insensitive and word-bounded. Empty means the codename check is inert, so
    # fill it in before the first draft rather than after the first leak.
    WRITING_CODENAMES: list[str] = list(_csv("OTTO_WRITING_CODENAMES"))

    # Vendors in the security and IT stack. Naming one in public is normal thought
    # leadership, and it also maps the stack for anybody reading, so it is flagged as
    # a decision rather than refused.
    WRITING_VENDORS: list[str] = list(_csv("OTTO_WRITING_VENDORS"))

    # Family and other personal names. Writing about the human side of the job is the
    # point, and these are still the owner's to decide on each time, so they flag
    # rather than block.
    WRITING_PERSONAL: list[str] = list(_csv("OTTO_WRITING_PERSONAL"))

    # The company. Under the owner's own name on LinkedIn the employer is already
    # public, so this is an "are you sure" and not a rule. Add the long and short forms
    # if they differ.
    WRITING_EMPLOYER: list[str] = list(_csv("OTTO_WRITING_EMPLOYER")) or [ORG_NAME]


    # Verbatim samples of things the owner actually wrote. This file, not the rules
    # file, is what teaches the register: the first drafts followed every rule in the
    # voice file and still read as an AI writing confidently about someone it had not
    # met, because a model cannot match a voice it has never seen a sentence of. Curated
    # by hand from the owner's Slack and mail (never from anything Otto drafted), and
    # added to whenever they write something they are happy with.
    WRITING_SAMPLES_PATH = Path(env.get("OTTO_WRITING_SAMPLES",
                                               OTTO_HOME / "writing-samples.md"))


    # ---- findings ---------------------------------------------------------------
    # Agents file work they notice into the backlog. The cap matters more than it
    # looks: a daily sweep files findings, and an uncapped filer turns the board into a
    # firehose nobody reads. Repeats bump seen_count on the existing card instead of
    # adding another.
    FINDINGS_MAX_PER_RUN = int(env.get("OTTO_FINDINGS_MAX", "5"))
    # Across ALL runs, per UTC day. The per-run cap did not bound intake: triage runs
    # every 4h, heartbeat every 6h, and every run files up to five, so one week in
    # September produced 137 cards against ~30 closed. Over this, findings are still
    # harvested but land in the debt file, not on the board.
    FINDINGS_MAX_PER_DAY = int(env.get("OTTO_FINDINGS_MAX_PER_DAY", "12"))
    # Findings ABOUT OTTO (its bugs, its loops, its own tooling) never go on the
    # board. They go here, in the repo, where fixing them happens. 41 of one week's
    # cards were Otto filing bugs against itself onto the owner's to-do list.
    DEBT_FILE = OTTO_REPO / "DEBT.md"

    # ---- unattended schedule runs (real cron) -----------------------------------
    # A schedule with runner="launch" + autostart=True is run by the daemon on its
    # cadence, with no human present. For a slash command that means a Claude Code
    # session holding --dangerously-skip-permissions at 08:00 while the owner is asleep.
    # These are the brakes:
    #
    #   AUTORUN            master switch. Off = every schedule is report-only again.
    #   MAX_CONCURRENT     never more than this many autorun sessions at once.
    #   MAX_FAILURES       consecutive failures before the schedule disables ITSELF.
    #                      Without this a broken schedule relaunches every cadence
    #                      forever, unwatched, and the bill is the only signal.
    #   MIN_GAP_MINUTES    floor between two autoruns of the SAME schedule, so a
    #                      cadence bug cannot produce a tight loop.
    #   BUDGET_USD         per-run dollar ceiling. 0 disables the cap.
    SCHEDULE_AUTORUN = env.get("OTTO_AUTORUN", "1") not in ("0", "false", "False")
    SCHEDULE_MAX_CONCURRENT = int(env.get("OTTO_AUTORUN_CONCURRENCY", "1"))
    SCHEDULE_MAX_FAILURES = int(env.get("OTTO_AUTORUN_MAX_FAILURES", "3"))
    SCHEDULE_MIN_GAP_MINUTES = int(env.get("OTTO_AUTORUN_MIN_GAP", "30"))
    SCHEDULE_BUDGET_USD = float(env.get("OTTO_AUTORUN_BUDGET_USD", "25"))

    # ---- machine health (informational only) ------------------------------------
    # The owner's own workstation, tracked as context rather than as a work queue. Disk
    # and offline signals are explicitly NOT action items and never raise alerts.

    MACHINE_PANEL = True
    # Paths whose most recent file mtime answers "when did this last back up".
    # os.pathsep-separated, like the roots above. Empty means the panel skips the row.
    BACKUP_PATHS: list[Path] = list(_path_list("OTTO_BACKUP_PATHS"))


    # ---- feed directories (producer/consumer ingestion) --------------------------
    # Borrowed from an earlier personal command-center's feed/<source>/current.json
    # layout. The problem it solves: a new data source means editing refresh.py AND the snapshot API,
    # because snapshots are keyed domain/kind and the daemon is the only writer. With
    # a feed directory any producer (a scheduled script, an agent, a Lambda, a manual
    # drop) can contribute without Otto knowing about it in advance.
    #
    # THE RULE THAT MAKES THIS LEGAL: a feed is an INBOX, not state.
    #
    # Otto has a single-writer rule -- only the daemon writes state, Store raises
    # WriteDenied anywhere else, and the CLI and agents mutate via HTTP. A directory
    # that arbitrary producers write to would CONFLICT with that if feeds were state.
    # They are not. Producers write FEED_DIR, which no reader of Otto's state ever
    # consults; the daemon reads those files on tick and ingests them into state
    # itself. So state keeps exactly one writer and many producers become possible.
    # Nothing here is a Store path and nothing here is served as current state.
    #
    # FEED_DIR is deliberately under OTTO_HOME and not in the repo: it is runtime
    # data, and a producer dropping a file must never dirty a git worktree.
    FEED_DIR = Path(env.get("OTTO_FEED_DIR", OTTO_HOME / "feed"))
    # Where producer scripts live. In the REPO, unlike the feed itself: a drop is
    # runtime data, but a producer is code, and the rule above is that state lives
    # under ~/.claude/otto while the repo is code only. Version-controlling producers
    # is also the only way to audit what a source is allowed to have reported.
    #
    # Otto never executes anything in here. It is a home for the scripts a human or a
    # cron entry runs, kept as one place so the feed and its producers are read
    # together. Executing them would make a file executable purely by being placed in
    # a directory, which is the one thing this design must not do.
    PRODUCERS_DIR = Path(env.get(
        "OTTO_PRODUCERS_DIR", Path(__file__).resolve().parent.parent / "producers"))

    FEED_FILE = "current.json"

    # A producer is not trusted to be well-behaved, so the daemon never reads an
    # unbounded file. 256 KB is far above any real feed (items are capped at 10) and
    # far below anything that would stall a tick.
    FEED_MAX_BYTES = int(env.get("OTTO_FEED_MAX_BYTES", str(256 * 1024)))

    # Items kept per feed, matching the snapshot cap. A feed is a panel, not a log.
    FEED_MAX_ITEMS = int(env.get("OTTO_FEED_MAX_ITEMS", "10"))


    # Declare a source here in the same change that adds its producer, and NOT before.
    # Every comment above about not declaring an aspiration applies with full force: a
    # declared source with no producer behind it reports a gap forever and teaches the
    # owner to ignore feed gaps, which is the same failure as SNAPSHOT_SOURCES nagging about
    # an inbox that was never wired up.
    #
    # Adding one is a single line plus a producer that writes
    # FEED_DIR/<name>/current.json:
    #
    #   FeedSource("netcheck", WORK, TRUST_DATA, max_age_hours=26,
    #              title="fleet netcheck",
    #              why="Scheduled netcheck run drops its rollup here; Otto has no "
    #                  "way to reach the fleet itself."),
    FEED_SOURCES: tuple[FeedSource, ...] = (
        # Declared 2026-08-04 with /slack-sweep, its producer. WHY THIS EXISTS: the
        # four-hourly refresher fetches SNAPSHOT_KINDS, which is agenda and mail only,
        # so Slack DMs -- where a real share of the owner's work is actually assigned --
        # were tracked nowhere but their memory. Slack reached Otto only when the
        # support-channel triage happened to scan that one channel once a day. DMs, never.
        #
        # ADVISORY, deliberately. `data` would render a panel the owner has to read and
        # re-triage every hour, which is the attention cost this is meant to remove.
        # Advisory lets the sweep PROPOSE cards, and the ceiling is what makes that
        # safe: findings.file_tasks forces auto=False, so a misread DM lands in the
        # backlog for a human to pull and can never dispatch an agent. Fingerprint
        # dedup is the other half, and it is what lets the producer sweep a rolling
        # 24h window every hour instead of tracking a cursor: the same DM bumps
        # seen_count on one card rather than filing 24. A missed run then self-heals,
        # where a cursor would have skipped the gap silently.
        #
        # max_age_hours=3 against an hourly cadence: two consecutive misses alarm, one
        # transient failure does not. A sweep that quietly dies is the failure mode
        # that matters here, because its silence is indistinguishable from a quiet day.
        FeedSource("slack-dm", WORK, TRUST_ADVISORY, max_age_hours=3,
                   title="slack DMs",
                   why="/slack-sweep drops actionable DMs and group DMs here hourly. "
                       "The daemon has no MCP access, so a headless session with the "
                       "Slack connector is the only thing that can read them."),
    )


    # ---- outreach (Otto talking to people who are not the owner) ------------------
    # The only capability in Otto whose mistakes land on somebody who never opted in.
    # Everything in this block is an interlock, and the defaults are the closed position.
    #
    # THE SHAPE: Otto composes, records, and holds. Silence sends. One click kills. That
    # is autonomy in the normal case (the owner does nothing) with a recall path for the
    # case that matters, which is the property draft-only does not give you and
    # immediate-send cannot.

    # Master switch. OFF means outreach is composed, recorded, held and then EXPIRED
    # rather than transmitted, so the whole pipeline is exercised and observable without
    # a single message reaching a colleague. This is the switch to flip after watching a
    # week of held messages you would have been happy to send.
    OUTREACH_ENABLED = env.get("OTTO_OUTREACH", "0") in ("1", "true", "True")

    # How long the owner gets. Ten minutes is chosen against the failure it exists to catch:
    # long enough to see a toast, read one line and click kill; short enough that "I
    # reset your MFA" still lands while the person is waiting on it.
    OUTREACH_HOLD_MINUTES = int(env.get("OTTO_OUTREACH_HOLD", "10"))

    # Rate limits, per rolling 24h. Not a cost control. A system that can message
    # colleagues unattended needs a bound that holds even when the thing generating the
    # messages is wrong, and "6 a day, 2 to any one person" is a bound a human would not
    # notice and a runaway loop cannot get past.
    OUTREACH_MAX_PER_DAY = int(env.get("OTTO_OUTREACH_MAX_DAY", "6"))
    OUTREACH_MAX_PER_PERSON_DAY = int(env.get("OTTO_OUTREACH_MAX_PERSON", "2"))

    # Directed DMs (`otto dm`, outreach.directed): messages the owner asked a session to
    # send. They skip the hold and the daily caps above, because those gate Otto's
    # initiative and this is the owner's. The one brake is per RUN per recipient: a
    # session that misreads "DM so-and-so" as a loop cannot page that person more than
    # this many times before it has to stop and explain itself. Keyed on OTTO_RUN_ID, so
    # it costs the owner nothing across a day of asking.
    DIRECTED_MAX_PER_RUN = int(env.get("OTTO_DIRECTED_MAX_PER_RUN", "2"))

    # Who Otto may address unattended. Members of the organization only (ORG_DOMAINS):
    # every external thread (partners, publishers, vendors) routes to the owner. Enforced
    # in outreach.py against the PEOPLE ROSTER, not against a string pattern -- Otto can
    # only message someone it already holds a dossier for, which is a much harder gate
    # to pass by accident than an address ending in the right domain. Contractors
    # (CONTRACTOR_DOMAINS) are a separate switch, below, and off by default.
    OUTREACH_ALLOW_CONTRACTORS = env.get(
        "OTTO_OUTREACH_CONTRACTORS", "0") in ("1", "true", "True")

    # Channels Otto may post to unattended. A DM reaches one person who can ignore it; a
    # channel post reaches everyone and cannot be unsent from anybody's memory. Empty
    # means DMs only, which is the default.
    OUTREACH_CHANNELS: list[str] = [c for c in (
        env.get("OTTO_OUTREACH_CHANNELS") or "").split(",") if c.strip()]

    # Subjects Otto never raises with a colleague on its own initiative, whatever tier a
    # producer claims. Checked against the message text as whole words. This list is not
    # about capability, it is about standing: Otto has none on any of it, and a message
    # from the owner's account about somebody's comp or somebody's suspected compromise
    # is a message the owner needs to have written themselves.
    OUTREACH_FORBIDDEN = [
        r"\bsalar(?:y|ies)\b", r"\bcompensation\b", r"\braise\b", r"\bbonus\b",
        r"\bequity\b", r"\boffer letter\b", r"\bperformance review\b", r"\bPIP\b",
        r"\btermination\b", r"\bfired\b", r"\blaid off\b", r"\boffboarding\b",
        r"\bresignation\b", r"\bdisciplinary\b", r"\bharassment\b", r"\bgrievance\b",
        r"\binvestigation\b", r"\bcompromised?\b", r"\bbreach\b", r"\bphish",
        r"\bmalware\b", r"\bransom", r"\bincident\b",
    ]

    # ---- Otto's own Slack identity ----------------------------------------------
    # Otto sends as ITSELF. Transmission used to be a spawned session calling the Slack
    # MCP connector, which is OAuth'd as the owner, so every automated message arrived
    # FROM THE OWNER and a colleague could not tell an Otto nudge from the owner typing.
    # Now it is a bot token and the messages say Otto.
    #
    # The name of the credential in the just-in-time secret store, checked out per send,
    # never held by the daemon and never placed in a spawned session's environment. See
    # otto/slack.py for why that boundary is the thing standing in for the guard hook,
    # which cannot see an HTTP POST.
    SLACK_BOT_CRED = env.get("OTTO_SLACK_BOT_CRED", "otto-dm-sweep-bot-token")

    # Who Otto is reporting to, and who it adds to every colleague conversation. An
    # email rather than a Slack id because that is the key Otto's people records use,
    # and because a readable constant is one somebody notices is wrong. Empty means
    # unconfigured: Otto cannot open a DM to nobody, and says so instead of guessing.
    SLACK_OWNER_EMAIL = env.get("OTTO_SLACK_OWNER", OWNER_EMAIL).strip().lower()

    # Channels where Otto may post a THREAD REPLY, and nowhere else. Empty means none.
    #
    # Two limits, both enforced in Python rather than asked for in a prompt, because the
    # caller is a model and a model that has been told not to do something is not a
    # system that cannot:
    #
    #   * this allowlist. Otto cannot reply in a channel that is not named here, whatever
    #     a session decides.
    #   * a thread_ts is REQUIRED. Otto can only ever speak inside an existing thread. It
    #     cannot start one, so it can never post top-level into a channel where sixty
    #     people would read it as an announcement.
    #
    # What is NOT enforced here, stated plainly: whether the message actually carried the
    # :otto: reaction that summons it. Verifying that needs channels:history, which the
    # user token deliberately does not have, so the summon gate lives in the command
    # prompt. That makes it the weakest interlock in Otto, and it is the one to move into
    # Python first if the triage ever becomes a script.
    REPLY_CHANNELS: set[str] = set(_csv("OTTO_REPLY_CHANNELS"))

    # Channels where Otto may post TOP-LEVEL, starting a message nobody threaded first.
    # A strictly stronger permission than REPLY_CHANNELS, so it is a separate list and
    # not a flag on that one: a channel Otto may answer questions in is not automatically
    # a channel Otto may announce into.
    #
    # WHY IT EXISTS. The daily run's summary is a top-level post, and until now the only
    # thing that could make one was the Slack CONNECTOR, which is OAuth'd as the owner
    # and is gated in unattended sessions. So a scheduled run had no path to its own
    # summary: it posted nothing for eight days (silence in the one channel the owner
    # watches) and posted AS THE OWNER on the days they happened to be at the desk.
    # Neither is the intended outcome, and both were invisible because the run stamped
    # success either way.
    #
    # Default is SLACK_CHANNEL_ID and only that: the owner's own one-way ops feed, where
    # the worst case of a bad post is the owner reading something wrong about their own
    # automation. Anything with an audience gets added deliberately, by id, one at a
    # time. With no feed channel configured this is empty and every post is refused.
    POST_CHANNELS: set[str] = set(_csv("OTTO_POST_CHANNELS")) or (
        {SLACK_CHANNEL_ID} if SLACK_CHANNEL_ID else set())

    # The schedule stamped when a summary actually lands in SLACK_CHANNEL_ID, and the
    # thing that makes silence loud. See runners/scheduled.default_schedules: it is
    # stamped by the POST, never by the run, so a run that finished without posting
    # leaves it stale and the board says so. The old failure was the reverse -- the run
    # stamped itself unconditionally, so heartbeat, `otto next` and `otto schedules` all
    # read healthy through eight dark days.
    FEED_POST_SCHEDULE = "daily-summary"

    # The reaction that summons Otto into a thread. Nothing else does: Otto reads the
    # support channel to tell the owner what is open, and stays silent in the channel
    # until somebody reacts with this.
    #
    # The prior design had Otto answer any ticket it judged tier-0, which put it in a race
    # with the human it works for. Once, an automation answered a colleague 49 seconds
    # after they asked and 50 seconds before the owner offered to hop on a call; the call
    # fixed it, and the thread still reads as unanswered forever. Summon-only deletes
    # that whole class: no timing heuristic, no handoff detection, no closure inference.
    # Silence is the default and a reaction is the authorization.
    # NO CODE READS THIS YET, and that is worth saying out loud rather than discovering.
    # The summon check happens in the help-triage command (OTTO_SUMMON_PROMPT), prose read
    # by a model, so THE COMMAND FILE IS THE SOURCE OF TRUTH for the name. Changing this
    # constant alone changes nothing at all; change both, or neither.
    #
    # It exists here anyway because the name has to live somewhere findable, and because
    # this becomes the live value the moment the triage converts from a session to a
    # script (at which point the same conversion makes the gate enforceable in Python
    # instead of asked for in a prompt). Until then it is documentation.
    #
    # `otto-help` and not `otto`: the international emoji set already contains one named
    # `otto`, so the name is reserved everywhere and cannot be claimed by a custom emoji
    # in any workspace. The workspace's custom-emoji list shows nothing, which is exactly
    # what a built-in collision looks like from the admin screen: taken, but not yours.
    SLACK_SUMMON_EMOJI = env.get("OTTO_SUMMON_EMOJI", "otto-help")

    # The channel Otto watches for that reaction, and the only one it reads: the
    # internal support channel where colleagues ask for help. Empty means the summon
    # watcher has nothing to watch and stays idle.
    HELP_CHANNEL = env.get("OTTO_HELP_CHANNEL", "").strip()
    ASK_TECH_CHANNEL = HELP_CHANNEL   # older name, kept for readers of the code

    # The slash command a summon dispatches to answer in that channel. Empty means the
    # watcher stays idle: a summon with nothing to run is a reaction nobody acts on.
    SUMMON_PROMPT = env.get("OTTO_SUMMON_PROMPT", "").strip()

    # How Otto hands a secret to a short-lived child without the secret entering the
    # daemon's own memory: a credential CLI's command prefix, with `{env}` and `{cred}`
    # placeholders for the variable name and the credential's name, e.g.
    # `mycred run --with {env}={cred} --`. Empty means Slack sends are refused with a
    # message naming this variable, which is the safe default.
    CREDENTIAL_RUN_ARGV = tuple(env.get("OTTO_CREDENTIAL_RUN", "").split())

    # The writing scanner's hostname shape. The default catches the common
    # `ABC-DESKTOP01` pattern; an org whose hostnames carry its own token sets a regex
    # here so a machine name never leaves in a draft.
    WRITING_HOST_PATTERN = env.get("OTTO_WRITING_HOST_PATTERN", "").strip() or None

    # MCP tool names that WRITE to production systems, as `label=regex` pairs separated
    # by `;`, matched case-insensitively against the full tool name. The hardening
    # audit treats a command that can reach one as production-mutating. Empty means
    # only the built-in signals apply.
    PROD_WRITE_SIGNALS: tuple[tuple[str, str], ...] = tuple(
        (p.split("=", 1)[0].strip(), p.split("=", 1)[1].strip())
        for p in env.get("OTTO_PROD_WRITE_SIGNALS", "").split(";") if "=" in p
    )

    # Master switch for the watcher. OFF means the summon is only noticed by the
    # four-hourly sweep, which is where it started and is a safe place to fall back to.
    SUMMON_POLL = env.get("OTTO_SUMMON_POLL", "1") in ("1", "true", "True")

    # Messages examined per poll. A support channel does not move fast, and a reaction can land
    # on a message well below the newest one, so this reaches back rather than assuming
    # the summon is recent.
    SUMMON_SCAN = int(env.get("OTTO_SUMMON_SCAN", "40"))

    # Seconds between polls. The daemon ticks far faster than this; a summon is a human
    # asking for help, and answering within a minute is indistinguishable from instant
    # while costing one Slack call a minute instead of four.
    SUMMON_EVERY_SECONDS = int(env.get("OTTO_SUMMON_EVERY", "60"))

    # A summoned run answers one thread. It should never be able to spend like a loop.
    SUMMON_BUDGET_USD = float(env.get("OTTO_SUMMON_BUDGET", "1.0"))
    SUMMON_MODEL = env.get("OTTO_SUMMON_MODEL", "claude-sonnet-5").strip()

    # ---- the owner talking to Otto -----------------------------------------------
    # Otto could speak in Slack and could not listen, which made it a thing you configure
    # from a terminal. The moments worth capturing happen away from the desk: see
    # otto/inbox.py. Off means Otto still sends and simply never reads its own DM.
    DM_POLL = env.get("OTTO_DM_POLL", "1") in ("1", "true", "True")

    # Seconds between checks of Otto's DM. Faster than the summon poll because this is a
    # person waiting for an acknowledgement rather than a ticket that can wait a minute.
    DM_EVERY_SECONDS = int(env.get("OTTO_DM_EVERY", "30"))

    # One batch of messages is one session. Filing a card is not hard work, and a ceiling
    # stops a misread instruction from turning into an expensive loop.
    DM_BUDGET_USD = float(env.get("OTTO_DM_BUDGET", "0.75"))
    DM_MODEL = env.get("OTTO_DM_MODEL", "claude-sonnet-5").strip()

    # A reply typed into a card's reply box (from a due toast, the notice sheet, or the
    # card menu) is the same shape of work as a DM: a sentence from the owner, a small
    # session that interprets it, and a receipt. Same model and the same ceiling, on
    # purpose; it is not a place for Opus to think for a dollar about a due date.
    CARD_REPLY_BUDGET_USD = float(env.get("OTTO_CARD_REPLY_BUDGET", str(DM_BUDGET_USD)))
    CARD_REPLY_MODEL = env.get("OTTO_CARD_REPLY_MODEL", DM_MODEL).strip()

    # Otto talks to a colleague in a GROUP DM containing Otto, the owner, and that
    # person, rather than a private 1:1. The recipient can see who Otto works for, the
    # owner sees the reply rather than a report of the send, and can correct it in the
    # conversation the person is actually reading.
    #
    # Off means 1:1 DMs and a separate rollup instead. Kept switchable because the group
    # thread is permanent and accumulates one per person, which is a cost worth being
    # able to back out of without a code change.
    OUTREACH_GROUP_DM = env.get("OTTO_OUTREACH_GROUP_DM", "1") in ("1", "true", "True")

    # How the message used to leave the machine. Retained because outreach can still fall
    # back to a spawned session if the bot credential is unavailable, and because the
    # measurement is the reason not to: a send cost $0.27 on Haiku to call one tool with
    # text that was already written. Sending through slack.py costs no model tokens.
    OUTREACH_MODEL = env.get("OTTO_OUTREACH_MODEL", "claude-haiku-4-5-20251001")

    # Above the floor cost of THIS session shape, which is higher than it looks: the sender
    # is not allowed to use the other MCP tools but their definitions are still loaded into
    # its context, so a trivial run measured $0.27 on Haiku doing nothing but replying "OK".
    # Set with headroom over that. The thread-note budget was set below its floor and every
    # run died mid-tool-call having already made the right decision; same mistake, one file
    # over.
    OUTREACH_BUDGET_USD = float(env.get("OTTO_OUTREACH_BUDGET", "0.75"))

    # The send blocks the daemon tick, so this is the ceiling on that stall. Generous
    # enough for a cold session plus one MCP round trip; a timeout leaves the outcome
    # UNKNOWN rather than failed, because the message may already have gone.
    OUTREACH_SEND_TIMEOUT = int(env.get("OTTO_OUTREACH_SEND_TIMEOUT", "120"))

    # ---- what Otto raises with the owner, unprompted -----------------------------
    # Otto's voice used to have four reasons to speak: the morning check-in, a meeting
    # brief, a feed producer asking, and dated commitments out of meeting notes. All four
    # fire on something that is BROKEN OR DUE. Nothing noticed a thing going quietly
    # stale, and nothing knew a date was coming. These are the three deterministic
    # additions; the open-ended fourth lives in /observe.

    # An open thread on a dossier, dated and untouched this long, is worth a mention.
    # 7 days because the dossier dates are written by hand at the point of contact, so a
    # week without one means a week without contact on that thread.
    #
    # Careful with the wording anywhere this surfaces: a Threads bullet is NOT necessarily
    # something the owner owes. prep.py documents the same trap. "Open since" is honest,
    # "you owe" asserts a debt over roughly half of them.
    NUDGE_THREAD_STALE_DAYS = int(env.get("OTTO_NUDGE_THREAD_DAYS", "7"))

    # And the ceiling, which is the part that keeps this from becoming a nag. Measured on
    # the real dossiers 2026-08-05: 38 dated threads were past 7 days, and the oldest was
    # 156. A daily notice about 38 things is the "76 board cards" failure again, and the
    # old ones are not news anyway -- a five-month-old note is archaeology, and the honest
    # verb for it is `otto retire`, not a nudge. Between the floor and this ceiling is the
    # band where a reply still recovers the thread.
    NUDGE_THREAD_MAX_DAYS = int(env.get("OTTO_NUDGE_THREAD_MAX", "30"))

    # Threads go out WEEKLY, not daily. Overdue cards and milestones are about today and
    # earn a daily check; "what went quiet" is a review, and a review that arrives every
    # morning is one nobody reads twice.
    NUDGE_THREAD_EVERY_DAYS = int(env.get("OTTO_NUDGE_THREAD_EVERY", "7"))

    # Dates that change what matters, and how far out to start counting. Declared on
    # purpose rather than inferred: a milestone Otto guessed at would be a countdown to a
    # date nobody agreed to. In the env as `label|YYYY-MM-DD|domain` entries separated by
    # `;` (labels carry commas, so commas are not the separator); domain defaults to
    # work. Stored here as (label, ISO date, domain).


    def _milestones(raw: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for entry in raw.split(";"):
            parts = [p.strip() for p in entry.split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            domain = parts[2] if len(parts) > 2 and parts[2] in DOMAINS else WORK
            out.append((parts[0], parts[1], domain))
        return out


    MILESTONES: list[tuple[str, str, str]] = _milestones(env.get("OTTO_MILESTONES", ""))
    MILESTONE_HORIZON_DAYS = int(env.get("OTTO_MILESTONE_HORIZON", "30"))

    # ---- a card's due date, as a conversation ------------------------------------
    # The ask, in the owner's words: "if a card is nearing its due date or past it, push
    # me a toast I can answer: update the card, or give Otto more context to decide next
    # steps."
    #
    # One notice PER CARD, not one digest, because a digest cannot carry a Reply button
    # that knows which card you mean. The toast deep-links the dashboard to that card's
    # reply box; free text there goes to a small session (/otto-card) that updates the
    # card or files what follows, and the quick buttons (done, push a day, push a week)
    # skip the model entirely.
    #
    # Each card interrupts once per PHASE: approaching, due today, overdue. A card that
    # is overdue and stays overdue is asked about again every DUE_RENAG_DAYS, not daily;
    # the answer to "still late?" does not change overnight and a daily repeat is the
    # thing that teaches you to dismiss the toast unread.
    DUE_SOON_DAYS = int(env.get("OTTO_DUE_SOON_DAYS", "1"))
    DUE_RENAG_DAYS = int(env.get("OTTO_DUE_RENAG_DAYS", "7"))
    # The ceiling per day. Measured 2026-09-03: fourteen live cards were due within a
    # day of each other because a meeting-notes ingest dated them all to the same
    # Friday. Fourteen toasts in seventy-five seconds is the "76 board cards" failure
    # with a buzz. The most pressing N get their own toast; the rest stay in the daily
    # digest, which is informational and does not interrupt.
    DUE_TOASTS_PER_DAY = int(env.get("OTTO_DUE_TOASTS_PER_DAY", "3"))
    # Where the toast's Reply button lands. "auto" picks otto:// when the desktop shell
    # has registered that scheme (it lands in Otto's window) and the http dashboard URL
    # otherwise (it lands in a browser). "otto" or "http" forces one. Found the hard way:
    # the http link opened a browser tab, which is not where Otto lives.
    REPLY_SCHEME = env.get("OTTO_REPLY_SCHEME", "auto").strip().lower()

    # ---- calendar blocks ---------------------------------------------------------
    # Recurring entries that occupy time without being meetings: a team standup that
    # says in its own title to self-organize in chat, Team Work Time, Social Hour, and
    # the Reclaim-scheduled lunch and decompress habits. They are real calendar entries
    # and the day view still shows all of them, because a day with Work Time in it is a
    # different day. What they must not do is trigger an interruption. A toast with
    # eight dossiers in it fifteen minutes before a standup nobody attends is how the
    # owner learns to dismiss prep notices unread, which costs them the one that mattered.
    #
    # Matched case-insensitively against the event title. Each entry is a regex, so
    # `\b` where a word could appear inside another one. Kept as titles rather than as a
    # "recurring?" flag because the calendar does not mark these any differently from a
    # weekly all-hands, which recurs and is a real meeting with a real deck.
    CALENDAR_BLOCKS = [p for p in (env.get("OTTO_CALENDAR_BLOCKS") or "").split("|") if p] or [
        r"\bstand-?ups?\b",
        r"\bwork time\b",
        r"\bsocial hour\b",
        r"\bfocus time\b",
        # Reclaim.ai habit blocks: Lunch, Decompress, and friends. Matched on the word,
        # not on "(Reclaim)", because the agenda titles are produced by the refresher's
        # model and drift between fetches: the same lunch block was "🍱 Lunch (Reclaim)"
        # at 08:00 on 2026-08-05 and "Lunch (Reclaim hold)" by 17:00. A pattern anchored
        # to the closing paren matched the first and missed the second, which is a filter
        # that works until the moment it silently stops.
        r"\breclaim\b",
        r"\bno meetings?\b",
        r"\bOOO\b",
    ]
    # Deliberately NOT in that list: "hold", "lunch", "coffee", "1:1 placeholder". A hold
    # with a person's name on it is a meeting being arranged, and "Lunch with <name>" is
    # a real call that happens to be at noon. The Reclaim tag catches the automated lunch
    # without catching the human one.

    # ---- meeting prep ------------------------------------------------------------
    # How far ahead of a meeting the prep notice fires. 15 minutes is chosen against
    # what the notice is FOR: long enough to read a dossier and open a card, short
    # enough that the owner is not already in something else when it lands. Earlier would
    # mean prepping during the previous meeting, which is where the interruption stops
    # being help.
    PREP_LEAD_MINUTES = int(env.get("OTTO_PREP_LEAD", "15"))

    # Whether the pre-meeting notice raises an OS toast or just lands in the dashboard.
    # Default on: a brief nobody sees is the same as no brief, and this is the one
    # interruption whose whole value is being timed.
    PREP_NOTIFY = env.get("OTTO_PREP_NOTIFY", "1") in ("1", "true", "True")

    # How many people get a FULL dossier block before the brief collapses to a compact
    # roster. Found by running prep against the real calendar: "Team Standups" matched
    # eight colleagues, and eight full blocks is a hundred lines nobody reads standing
    # up before a fifteen-minute standup. Above this, the brief keeps only the part of a
    # group meeting that is actually actionable (who you owe something to) and drops the
    # working-style and background material, which is 1:1 preparation by nature.
    PREP_MAX_PEOPLE = int(env.get("OTTO_PREP_MAX_PEOPLE", "3"))

    # ---- retire thresholds (what Otto should STOP doing) --------------------------
    # Every other detector in this system adds: scout files cards, findings files
    # cards, feeds propose cards, advisor.gaps proposes fixes. Nothing subtracts, and
    # the accumulation is visible (76 board cards, a heartbeat.md that documents its
    # own LOOPS config as dead). See retire.py for the whole argument.
    #
    # These are thresholds for "worth a second look", never for automatic deletion.
    # Nothing in Otto deletes a card or a schedule on a timer: an aged-out card is a
    # question for the owner, and a detector that answered it itself would be deciding
    # what matters to them, which is not a thing a threshold can know.

    # A backlog card untouched this long is a candidate for closing. 30 days is a
    # month of not mattering, which is evidence, though not proof.
    RETIRE_CARD_STALE_DAYS = int(env.get("OTTO_RETIRE_CARD_DAYS", "30"))

    # A finding refiled this many times while still sitting in the backlog. The
    # strongest signal in here: Otto keeps rediscovering it and the owner keeps declining
    # to act, so either it matters and should be scheduled or it does not and the
    # detector should stop reporting it. Both answers are better than the loop.
    RETIRE_NAG_SEEN = int(env.get("OTTO_RETIRE_NAG_SEEN", "4"))

    # A registry entry whose definition file vanished this long ago. Kept visible
    # rather than dropped (registry.reconcile marks `missing` deliberately), but past
    # a fortnight it is a stale reference rather than a recent deletion to notice.
    RETIRE_MISSING_DAYS = int(env.get("OTTO_RETIRE_MISSING_DAYS", "14"))

    # A schedule that has existed this long and never once run. Long enough that a
    # weekly cadence has had two chances, so this means "never worked" or "never
    # wanted", not "not due yet".
    RETIRE_NEVER_RAN_DAYS = int(env.get("OTTO_RETIRE_NEVER_DAYS", "16"))


    # ---- board ------------------------------------------------------------------
    # How long a finished card stays on the board. Done is the only column that grows
    # without bound: every other one drains by being worked, and on 2026-08-05 Done
    # held 65 cards against 38 in Backlog, so most of the board was history.
    #
    # This hides, it does NOT delete. The Task stays in the store, `otto task ls` still
    # lists it, and the rollups that read completions (advisor, meetings, today) read
    # the store rather than the board, so none of them lose a week. Set to 0 to keep
    # every finished card on the board forever.
    BOARD_DONE_DAYS = int(env.get("OTTO_BOARD_DONE_DAYS", "7"))


    # ---- backlog triage ---------------------------------------------------------
    # How many cards may be queued or running before the triage pass stops promoting.
    #
    # The cap is on work IN FLIGHT rather than per pass, and that is the whole point.
    # "Promote three at a time" bounds nothing unless you know the cadence, and the
    # problem this fixes is precisely that intake ran hourly while assessment ran
    # once a day. A cap on what is already in flight is self-limiting whatever the
    # cadence: the backlog drains as work finishes and can never flood.
    #
    # Sits at or below the autodispatch concurrency (2 tasks) on purpose, so promoted
    # cards actually start rather than piling up in `queued`, which would just move
    # the clutter one column to the right.
    TRIAGE_MAX_IN_FLIGHT = int(env.get("OTTO_TRIAGE_MAX_IN_FLIGHT", "3"))

    # When a backlog card is old enough to be worth CHECKING whether it already
    # happened. Age triggers the look, never the close: a card leaves the board on
    # positive evidence that the work is done, never because it got old. Otto does
    # not get to decide something stopped mattering just because nobody touched it.
    TRIAGE_STALE_DAYS = int(env.get("OTTO_TRIAGE_STALE_DAYS", "10"))

    # When a card the owner owns, that a feed filed and nobody has touched since, leaves the
    # columns. See backlog.fade_candidates for the rules. Fourteen days because that
    # is two working weeks: past it, a Slack ask nobody chased has been answered some
    # other way or has stopped mattering, and either way it is not a task.
    FADE_DAYS = int(env.get("OTTO_FADE_DAYS", "14"))

    # ---- duplicate cards ---------------------------------------------------------
    # See dedupe.py. Two open cards in one domain are the same card when their
    # fingerprints match, when they hit the same topic rule below, or when their
    # titles share at least DEDUPE_MIN_SHARED content tokens with a Jaccard overlap
    # of DEDUPE_SIMILARITY or more. 0.55 was calibrated against a live board on
    # 2026-10-01: every pair scoring 0.55 to 0.60 was the same work ("Add
    # engine-upgrade distributable for machines without the VCS client" / "Package
    # engine-upgrade distributable for boxes without the VCS client"), and the first
    # pairs that are NOT the same work appeared at 0.44 ("Add the SIEM scope to the
    # EDR API client" / "EDR API client missing the asset-read scope"). Lower it and
    # those fold; raise it and the vendor-token cards split again.
    DEDUPE_SIMILARITY = float(env.get("OTTO_DEDUPE_SIMILARITY", "0.55"))
    DEDUPE_MIN_SHARED = int(env.get("OTTO_DEDUPE_MIN_SHARED", "3"))

    # Topic rules: recurring findings whose phrasings share almost no words. Each is
    # a name, regexes that must ALL match the lowercased title, and regexes none of
    # which may. Code, not a feed-writable file, because a wrong rule folds real
    # work into the wrong card and nothing downstream can tell.
    #
    # Only the rule every deployment shares ships here. Vendor-specific ones (a
    # particular MCP server's token, a particular endpoint that 404s) belong in your
    # fork, next to the finding they fold: nine cards from four runs, titles ranging
    # from "<vendor> MCP token rejected with 401" to "Fix <vendor> MCP auth header",
    # is the shape to look for.
    DEDUPE_TOPICS: list[dict] = [
        {
            # Heartbeat filed fourteen of these in one month, every one worded
            # differently. The condition is one expired SSO session and the fix is
            # one `aws sso login`. Cards ABOUT the session (its duration, its token
            # cache, alerting on it, a profile that is not configured) are work, not
            # the condition, and stay separate.
            "name": "aws-sso-session",
            "all": [r"\baws\b.*\bsso\b|\bsso\b.*\baws\b",
                    r"expir|re-?auth|refresh|renew|validity|log ?in|sign ?in"],
            "none": [r"alert", r"monitor", r"duration", r"cache", r"profile", r"cards?\b",
                     r"role", r"polic", r"assertion"],
        },
    ]


    # ---- capability manifest ----------------------------------------------------
    # The DECLARED SHAPE of Otto's reachable world. Borrowed from an earlier personal
    # project's `.env.keys`, which existed because an always-on second machine was
    # silently missing an entire credential set and nothing compared the two machines,
    # so nobody noticed for months.
    #
    # NAMES ONLY. Never values, never fingerprints, never lengths. This table is
    # committed and read by anything; a length is a hint and a fingerprint is a
    # confirmation oracle, so neither belongs here.
    #
    # Otto's drift axis is not two machines, it is two REGISTRIES plus two loaders:
    #   * Claude Code (`~/.claude.json`) and Claude Desktop
    #     (`%APPDATA%\Claude\claude_desktop_config.json`) are separate MCP registries.
    #     One Desktop entry once had to be pulled because it was still polling a
    #     retired OAuth client id while Code's entry was fine. Nothing compared them.
    #   * a bash env loader under ~/.claude and a PowerShell one under tools\ load the
    #     SAME credential for two shells. They disagreed on cache format once and the
    #     heartbeat reported the API as down for a healthy key.
    #
    # `expect` is the improvement over the presence-only original. A presence-only
    # manifest cannot catch a REGRESSION: a decommissioned EDR server, a deactivated
    # bot, a registry entry that was deliberately removed. Each of those coming back
    # is a real event and a presence check is blind to all three.
    #
    # Declaring something here makes Otto alarm while reality disagrees, so declare only
    # what is actually true today. A manifest that nags about an aspiration teaches the
    # owner to ignore the manifest, which is the same failure as SNAPSHOT_SOURCES above.

    SURFACE_CODE_MCP = "code-mcp"           # ~/.claude.json  mcpServers
    SURFACE_DESKTOP_MCP = "desktop-mcp"     # claude_desktop_config.json  mcpServers
    SURFACE_LOADER_SH = "loader-sh"         # a bash env loader under ~/.claude
    SURFACE_LOADER_PS1 = "loader-ps1"       # a PowerShell env loader under tools\
    SURFACE_ACCOUNT = "account"             # claude.ai account-bound; no local file to read

    SURFACES = (SURFACE_CODE_MCP, SURFACE_DESKTOP_MCP,
                SURFACE_LOADER_SH, SURFACE_LOADER_PS1, SURFACE_ACCOUNT)


    def _capabilities(raw: str) -> tuple[Capability, ...]:
        """Parse OTTO_CAPABILITIES.

        Comma-separated `surface/name` entries. A leading `!` declares an expected
        ABSENCE, and an optional `=reason` after the name records why it is one, which
        is what the alarm prints if it comes back:

            code-mcp/okta,desktop-mcp/ninjaone,!code-mcp/old-edr=decommissioned 2026-07

        Entries whose surface is not one of SURFACES are dropped rather than raised on,
        because a typo in an env var must not stop the daemon from starting; `otto
        manifest` shows what was actually declared.
        """
        out: list[Capability] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            expect = "present"
            if entry.startswith("!"):
                expect, entry = "absent", entry[1:]
            why = ""
            if "=" in entry:
                entry, why = (s.strip() for s in entry.split("=", 1))
            surface, _, name = entry.partition("/")
            if surface not in SURFACES or not name:
                continue
            out.append(Capability(name, surface, expect, why))
        return tuple(out)


    # Adding a capability? Declare it in the same change that wires it up. An
    # undeclared-but-present capability is drift, not a feature. The public tree ships
    # an EMPTY manifest, because every entry is a fact about one machine: which MCP
    # servers it registers, which loader scripts it carries, which servers were retired
    # and must not return. Declare yours in OTTO_CAPABILITIES (format above), or in your
    # fork directly as Capability(...) rows:
    #
    #   Capability("okta", SURFACE_CODE_MCP),
    #   Capability("ninjaone", SURFACE_DESKTOP_MCP),
    #   Capability("old-edr", SURFACE_CODE_MCP, expect="absent",
    #              why="subscription lapsed; a registered server can only produce a "
    #                  "false outage"),
    #   Capability("env-loader.sh", SURFACE_LOADER_SH),
    #   Capability("claude_ai_Gmail", SURFACE_ACCOUNT),
    #
    # A server registered on BOTH registries is declared twice, once per surface. That
    # is the whole point of a per-surface manifest: Code and Desktop can drift apart,
    # and the retired-client story above is what happens when nobody compares them.
    CAPABILITIES: tuple[Capability, ...] = _capabilities(env.get("OTTO_CAPABILITIES", ""))

    # Where the credential is read from at use time. `inline` is the antipattern the
    # build is meant to remove: a long-lived client secret sitting in ~/.claude.json,
    # readable by every process that can read the owner's home, rotatable only by hand,
    # and handed to every MCP client that starts.
    CRED_INLINE = "inline"          # in ~/.claude.json mcpServers[*].env
    CRED_STORE = "store"            # checked out from a credential store at use time
    CRED_SSO = "sso"                # short-lived session from an SSO login
    CRED_ACCOUNT = "account"        # claude.ai account-bound OAuth; no local artifact
    CRED_LOCAL_FILE = "local-file"  # a token/key file on this machine
    CRED_UNVERIFIED = "unverified"  # not inspected in this pass; do not assert

    CRED_LOCATIONS = (CRED_INLINE, CRED_STORE, CRED_SSO, CRED_ACCOUNT,
                      CRED_LOCAL_FILE, CRED_UNVERIFIED)

    # Secret-ish env key names. Used to detect an inline credential from the registry
    # by KEY NAME ONLY -- the value is never read, so this cannot leak and cannot act
    # as a confirmation oracle. `*_CLIENT_ID`, `*_REGION` and `*_BASE_URL` are
    # deliberately not secrets: they are configuration, and flagging them would bury
    # the two entries that actually carry a secret.
    # AUTHORIZATION is in the list because it is the http-server equivalent of the
    # rest, and without it a `headers.Authorization` bearer reads as no credential at all.
    SECRETISH_ENV_KEYS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "API_KEY",
                          "APIKEY", "CREDENTIAL", "PASSPHRASE", "AUTHORIZATION")


    # Integrations that are not MCP registrations and have no local registry entry.
    SURFACE_EXTERNAL = "external"

    # EMPTY in the public tree, on purpose. Every row is an assessment of one
    # deployment's integrations: which service app the IdP server authenticates as,
    # whether the EDR API client is shared with humans, where the cloud SSO session
    # comes from. None of that transfers, and a row copied from somebody else's
    # assessment is worse than no row, because it asserts an audit trail that was never
    # checked. Fill this in from your own review, one row per integration, and say
    # `unverified` where you could not check rather than guessing. For example:
    #
    #   Principal("okta", SURFACE_CODE_MCP, PRINCIPAL_SHARED, CRED_STORE,
    #             audit="System Log actor = the service app's client id, identical "
    #                   "whether Otto or a human drove the call",
    #             blocker="A second service app for Otto needs an admin to grant it "
    #                     "scopes; the existing app cannot self-escalate.",
    #             identifier="<service app client id>"),
    #   Principal("claude_ai_connectors", SURFACE_ACCOUNT, PRINCIPAL_HUMAN, CRED_ACCOUNT,
    #             audit="Slack shows the owner posted; Drive shows the owner opened it",
    #             blocker="A claude.ai connector authenticates as the signed-in user. "
    #                     "Separating this needs a second identity or self-hosted MCP "
    #                     "servers holding Otto's own tokens.",
    #             identifier="<owner email>"),
    #   Principal("aws", SURFACE_EXTERNAL, PRINCIPAL_HUMAN, CRED_SSO,
    #             audit="CloudTrail userIdentity = the owner's Identity Center role session",
    #             blocker="An IAM role Otto assumes via OIDC, with no IdP user at all, is "
    #                     "the cheap path."),
    #
    # Adding an integration? Add it here in the same change, and if it authenticates as
    # the owner, raise INHERITED_BASELINE deliberately rather than letting the alarm be
    # silenced by a number nobody chose.
    PRINCIPALS: tuple[Principal, ...] = ()

    # Count of `human` rows accepted as of the last review. The alarm is a count ABOVE
    # this (new inheritance), not the number itself. Below it means the table is stale
    # and should be lowered in the same change that fixed the row -- a baseline nobody
    # maintains silences the detector exactly when it starts working. Resolving a row
    # from `unverified` to `human` is a bookkeeping raise, not a new integration, and
    # is exactly the case to raise this deliberately. Zero until PRINCIPALS has rows.
    INHERITED_BASELINE = int(env.get("OTTO_INHERITED_BASELINE", "0"))

    return Settings(**{k: v for k, v in locals().items() if k not in _NOT_SETTINGS})


_CURRENT: Settings | None = None
_RELOADS = 0


def reload(environ: Mapping[str, str] | None = None, path: Path | None = None) -> Settings:
    """Resolve again and make the result this module's globals.

    A no-argument reload re-reads the same file the running daemon writes
    (`SETTINGS_PATH`), so a value saved from the dashboard lands in the process
    that saved it. Anything in RESTART_KEYS is re-resolved too, but the objects
    built from the old value are not rebuilt; that is what the restart is for.
    """
    global _CURRENT, _RELOADS
    if environ is None and path is None and _CURRENT is not None:
        path = SETTINGS_PATH  # the file Otto writes is the file it reads back
    s = resolve(environ, path)
    globals().update(vars(s))
    _CURRENT = s
    _RELOADS += 1
    return s


def current() -> Settings:
    """The live settings, as last resolved."""
    assert _CURRENT is not None
    return _CURRENT


reload()

# The helpers above read the resolved values as module globals, which reload()
# rebinds every time it runs. Spelled out here, once, so a reader and a linter can
# see that the names exist without following globals().update(); the values are
# the ones reload() just set, and every later reload rebinds them the same way.
SETTINGS_PATH = current().SETTINGS_PATH
OTTO_HOME = current().OTTO_HOME
STATE_DIR = current().STATE_DIR
LOG_DIR = current().LOG_DIR
PID_FILE = current().PID_FILE
FEED_DIR = current().FEED_DIR
PROFILE_PATH = current().PROFILE_PATH
WRITING_VOICE_PATH = current().WRITING_VOICE_PATH
WRITING_SAMPLES_PATH = current().WRITING_SAMPLES_PATH
WORK = current().WORK
DOMAIN_ROOTS = current().DOMAIN_ROOTS
QUIET_FROM = current().QUIET_FROM
QUIET_TO = current().QUIET_TO
SNAPSHOT_SOURCES = current().SNAPSHOT_SOURCES
FEED_SOURCES = current().FEED_SOURCES
