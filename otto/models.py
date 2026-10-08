"""Otto data model.

Pydantic gives validation plus clean JSON round-tripping, which matters when the
store is hand-inspectable JSON that a human might edit while the daemon is down.
"""

from __future__ import annotations

import uuid

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import config

RunStatus = Literal["running", "ok", "failed", "orphaned", "killed", "due", "skipped"]
Runner = Literal["detached", "scheduled", "inline", "external"]
EntryKind = Literal["agent", "command", "skill", "project"]
Domain = Literal["work", "personal"]


def safe_text(value: str) -> str:
    """Text that will survive being serialised to JSON. Use on anything external.

    A lone surrogate (an unpaired \\ud800-\\udfff) can be written into a state file
    without complaint - `json.dumps` escapes it happily - and then makes the whole
    record unserialisable on the way OUT. Pydantic raises, FastAPI returns 500, and
    the endpoint fails ENTIRELY: not the bad field, not the bad record, the whole
    response.

    That is not hypothetical. On 2026-08-15 one calendar event title arrived with a
    split emoji ("\\ufffd\\udc8f Personal Commitment"), landed in one session's
    last_message, and took `/api/state` and `/api/sessions` down with it. Both the
    dashboard and the desktop app showed "daemon unreachable" while the daemon was
    perfectly healthy and answering /api/health in four milliseconds. One broken
    character in one field of one record, and the whole UI was dark.

    So this is applied where text ENTERS state, not where it leaves. Once it is
    stored the damage is done, and every reader has to survive it forever.
    """
    if not value:
        return value
    # Replace rather than drop: U+FFFD is visible, so a mangled title still shows
    # up as a mangled title instead of silently losing a character.
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run(BaseModel):
    """One execution of an agent, command, or schedule."""

    id: str
    name: str
    runner: Runner
    status: RunStatus = "running"
    domain: Domain = "work"

    # Process identity. pid alone is not enough: Windows reuses PIDs, so we
    # pin create_time and compare both before believing a process is "ours".
    pid: int | None = None
    pid_created: float | None = None

    cwd: str | None = None
    cmd: list[str] = Field(default_factory=list)
    log: str | None = None

    started: str = Field(default_factory=lambda: iso(utcnow()))
    ended: str | None = None
    exit_code: int | None = None

    # Usage as reported by Claude Code itself (`-p --output-format json`), which
    # is authoritative. Otto never prices tokens on its own, so these numbers
    # cannot drift out of date with the model lineup.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    num_turns: int | None = None
    result_summary: str | None = None

    # What produced the cost above. Recorded because `cost_usd` on its own cannot
    # answer the only question worth asking of it: was this the right model for
    # this job. Otto knew both values at spawn time and threw them away, so the
    # first fifteen days of history can be summed but not explained.
    #
    # `model` is what was passed to --model, so None means the run inherited the
    # session default rather than "no model". `agent` duplicates what `notes`
    # already carried as free text, because a spend view cannot group on prose.
    model: str | None = None
    agent: str | None = None

    task_id: str | None = None  # the external queue's own id, when driven by one
    tier: str | None = None
    notes: str | None = None
    # What the session may do, as the flags it was started with (runners/detached):
    #   plan     --permission-mode plan: read-only investigation that writes a plan
    #   yolo     --dangerously-skip-permissions: the full operator, unattended
    #   scoped   an allow or deny list instead of either (outreach, writing, refresh)
    # Defaults to yolo because every run before the field existed was one.
    permissions: Literal["plan", "yolo", "scoped"] = "yolo"

    # WHY a failed run failed, when that is knowable and load-bearing. Today the only
    # value is "api": Claude Code reported `terminal_reason: api_error`, meaning the
    # session died on upstream capacity rather than on anything about the command.
    #
    # The distinction earns a field because Otto treated the two identically and it
    # cost real money. On 2026-08-05 a run of 529 Overloaded errors killed several
    # scheduled runs. Otto recorded "529" in `notes`, then raised each one as
    # "<name> failed (exit 1)", which is indistinguishable from a broken schedule.
    # Three of those got actioned from the dashboard, each dispatching an agent, and
    # all three agents concluded the same thing: upstream capacity, nothing broken.
    # About $3.20 to re-derive a fact already sitting in the run record. Worse, the
    # autorun circuit breaker counted them, so a long enough outage would have
    # disarmed working schedules for being downstream of a bad afternoon.
    error_kind: str | None = None

    # Where the run can be WATCHED, when it ran inside a herdr pane: the pane and
    # the `otto-runs` workspace it sits in. None for a plain detached process.
    # Cleared by the pane sweep once the pane is closed, so a stale pointer never
    # sends the owner to a pane that is gone.
    pane_id: str | None = None
    workspace_id: str | None = None

    # When the owner acknowledged a failure. Derived board cards stop being emitted
    # for a reviewed run.
    #
    # This field exists because the derived-card rule had a hole in it. The rule is
    # "you do not drag a derived card to Done; you fix the condition and the card
    # disappears", and that is right for a stale schedule or a down integration,
    # because those conditions can actually clear. A FAILED RUN is history. It never
    # clears, so a `movable: false` card describing one is a permanent nag with no
    # way to say "seen it" short of `otto prune` deleting the run outright. After the
    # 2026-08-05 529 burst there were several of these stuck in Needs-you, which is
    # how the hole was found: the board offered no verb for the only thing a person
    # actually wants to do with a past failure.
    reviewed_at: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "running"

    @property
    def transient(self) -> bool:
        """Failed for a reason that says nothing about the command, and that retrying
        is the correct response to."""
        return self.status == "failed" and self.error_kind == "api"


SessionState = Literal["busy", "waiting", "idle", "offline"]


class Session(BaseModel):
    """A Claude Code session on this machine, as its own hooks report it.

    The complement to Run, and deliberately a separate entity. A Run is something
    Otto SPAWNED and watches from the outside: a pid it can poll and an NDJSON log
    it parses. A Session is something Claude Code reports from the inside, once per
    turn, whether or not Otto started it.

    That difference is the whole reason this model exists. Otto could previously see
    only its own children, and could only infer their state from a log file's mtime,
    so "thinking hard" and "blocked on a permission prompt for forty minutes" looked
    identical. `waiting` is the state Otto could not observe at all, and it is the
    one that costs wall-clock. Every terminal the owner opens by hand was invisible
    outright.

    Keyed by session_id, NOT by directory. Two sessions in one repo are two
    sessions, and session_id is also what joins a hook to a Run once one has been
    parsed out of the stream (`runners.detached` sets `Run.session_id`), so a
    session with no matching run is exactly the class Otto never used to see.

    STATE IS REPORTED, NEVER GUESSED. Every transition here came from a hook that
    actually fired. The one exception is `offline` inferred from long silence, which
    is recorded in `offline_inferred` rather than presented as observed, for the same
    reason a vanished run is `orphaned` and not `failed`: an unknown outcome must not
    be dressed up as a known one.
    """

    session_id: str
    state: SessionState = "idle"

    cwd: str | None = None
    domain: Domain = "work"
    repo: str | None = None          # repos.key_for(cwd), when it is under a root
    transcript: str | None = None
    permission_mode: str | None = None

    first_seen: str = Field(default_factory=lambda: iso(utcnow()))
    last_event: str = Field(default_factory=lambda: iso(utcnow()))
    # When it entered the CURRENT state, which is not the same as the last event and
    # is the only field that can answer "how long has this been blocked on me".
    # Bumping it on every hook would make a session that has been waiting since 09:00
    # read as freshly waiting each time anything touched it.
    state_since: str = Field(default_factory=lambda: iso(utcnow()))

    turns: int = 0                   # UserPromptSubmit count, so far
    run_id: str | None = None        # the Otto run this session belongs to, if any
    note: str | None = None          # the Notification message, when that is why
    last_message: str | None = None  # truncated tail of the last assistant turn

    # ---- the harness (2026-10-01) ----
    # Four live sessions and 196 offline ones, each shown as a folder name and
    # eight hex characters, is not a list anyone can keep track of. These fields
    # are what keitora's rail has that the hook payload alone did not give us.
    #
    # What the session is ABOUT. Taken from the transcript (Claude Code's own
    # summary line when it has one, else the first prompt) unless the owner set one.
    title: str | None = None
    title_source: Literal["summary", "prompt", "user"] | None = None
    # The Claude Code process itself, resolved from the hook's parent pid while
    # the hook is still running. `pid_started` is the process create time, so a
    # recycled pid is not mistaken for the session that used to own it. With
    # these, "is it alive" is a fact Otto can check rather than a 12-hour guess.
    pid: int | None = None
    pid_started: float | None = None
    # The app the session is running in ("Windows Terminal", "Cursor", "VS Code")
    # and that app's pid, so `otto sessions open` can bring its window forward.
    host: str | None = None
    host_pid: int | None = None
    # Why it went offline: "hook" (SessionEnd fired), "process" (the Claude process
    # was observed gone), or "silence" (nothing for SESSION_OFFLINE_HOURS).
    offline_reason: Literal["hook", "process", "silence"] | None = None

    # ---- herdr (otto/herdr.py) ----
    # When the session lives in a herdr pane: the pane id ("w1:p1"), the agent's
    # name there (or its kind), and herdr's own reading of its state. herdr reads
    # the screen, so it sees `blocked` on an AskUserQuestion that no hook reports;
    # where the two disagree, `effective_state` prefers herdr for that reason.
    herdr_pane: str | None = None
    herdr_agent: str | None = None
    herdr_status: Literal["idle", "working", "blocked", "done", "unknown"] | None = None
    herdr_seen: str | None = None    # last sync that saw this pane

    # Set when `offline` was concluded from silence rather than from a SessionEnd
    # hook. Otto has no PTY to ask, so this is an inference and says so.
    offline_inferred: bool = False

    @property
    def live(self) -> bool:
        return self.state != "offline"

    @property
    def needs_input(self) -> bool:
        return self.state == "waiting"

    @property
    def effective_state(self) -> SessionState:
        """The state to act on. herdr's screen reading outranks the hooks while the
        pane is in view: it catches `blocked` on a question no hook fires for,
        and `working` on a turn whose UserPromptSubmit the daemon missed."""
        if self.state == "offline" or not self.herdr_pane:
            return self.state
        return {"working": "busy", "blocked": "waiting", "idle": "idle",
                "done": "idle"}.get(self.herdr_status or "", self.state)  # type: ignore[return-value]


class RegistryEntry(BaseModel):
    """A discovered agent / command / skill definition, or a project root."""

    name: str
    kind: EntryKind
    scope: Literal["global", "repo"]
    domain: Domain = "work"
    path: str
    repo: str | None = None
    description: str | None = None
    checksum: str | None = None
    mtime: str | None = None
    first_seen: str = Field(default_factory=lambda: iso(utcnow()))
    last_seen: str = Field(default_factory=lambda: iso(utcnow()))
    missing: bool = False

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.repo or '-'}:{self.kind}:{self.name}"


class Cadence(BaseModel):
    """When a schedule is due.

    kind:
      daily    -> every day, on/after `at`
      weekly   -> on `days`, on/after `at`
      every    -> every `hours` hours
      manual   -> never auto-due
    min_interval_days additionally suppresses a run that fired too recently,
    which is how /scout's "Wednesdays but not within 5 days" rule is expressed.
    """

    kind: Literal["daily", "weekly", "every", "manual"] = "manual"
    at: str = "08:00"
    days: list[str] = Field(default_factory=list)
    hours: int | None = None
    min_interval_days: int | None = None


class Schedule(BaseModel):
    name: str
    command: str
    domain: Domain = "work"
    cadence: Cadence = Field(default_factory=Cadence)
    enabled: bool = True
    autostart: bool = False  # False = Otto reports "due", does not launch
    # An allow-list, so adding a schedule can never arm something by accident:
    #   report   surface it as due and wait for a human (default)
    #   refresh  the built-in read-only email/calendar fetcher
    #   ingest   the built-in read-only Notion meeting-notes parser
    #   launch   run the command unattended, on cadence. Real cron.
    # `launch` additionally requires autostart=True and the master switch.
    #   writing  the built-in post-ideas miner. Reads Otto's own state, no tools
    runner: Literal["report", "refresh", "ingest", "launch", "writing"] = "report"
    description: str | None = None
    # The level a `launch` run gets (models.Run.permissions). yolo by default: the
    # seeded loops gather data and act on the board, which plan mode cannot do.
    permissions: Literal["plan", "yolo"] = "yolo"

    created: str = Field(default_factory=lambda: iso(utcnow()))
    last_run: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None

    # Freshness alarm, in hours, independent of cadence. This is the heartbeat
    # concept: cadence says when to run, max_age_hours says when to scream.
    max_age_hours: int | None = None

    # Unattended runs need a circuit breaker. A schedule that fails every cadence
    # would otherwise relaunch forever, burning money on a loop nobody is watching.
    consecutive_failures: int = 0
    disabled_reason: str | None = None
    last_autorun: str | None = None  # throttle floor, separate from last_run


class Integration(BaseModel):
    name: str
    ok: bool = False
    detail: str = ""
    mode: Literal["api", "mcp-only", "unconfigured"] = "api"
    checked_at: str | None = None
    # A number worth watching move, when the integration has one. Falcon reports
    # enrolled sensors here so a rollout is visible as a trend instead of a string
    # somebody has to re-read.
    metric: float | None = None
    metric_label: str | None = None


# `faded` is a card the owner never touched for FADE_DAYS: not done, not deleted, just
# out of the columns. The board had 200 of their own harvested commitments in
# `backlog`, and a pile that size is read by nobody, so nothing in it was actually
# visible.
# Faded cards keep their history, list with `otto task ls --faded`, and come back
# with `otto task mv <id> backlog`.
TaskStatus = Literal["backlog", "queued", "running", "needs-you", "blocked", "done",
                     "faded"]
Priority = Literal["low", "normal", "high", "urgent"]

# Who can do the work: Otto itself, or the person it works for. Unlike tier these
# are new fields with no legacy values, so they are closed sets and a typo is
# rejected at the edit rather than stored. `owner` is a role token, not a name, so
# the stored value stays valid when OWNER_NAME changes.
Owner = Literal["otto", "owner"]
# Whether it can be started at all. `needs-info` means something knowable is
# missing; `needs-decision` means a person has to choose, and no amount of
# gathering will resolve it.
Readiness = Literal["ready", "needs-info", "needs-decision"]


class Task(BaseModel):
    """A unit of work on the board.

    Covers both locally created tasks and mirrored Notion orchestrator rows. The
    board also renders derived cards (a due schedule, an orphaned run) which are
    NOT Tasks: they are computed each request and have no stored identity, so
    they cannot drift out of sync with the thing they describe.
    """

    id: str
    title: str
    status: TaskStatus = "backlog"
    domain: Domain = "work"
    priority: Priority = "normal"

    detail: str | None = None
    agent: str | None = None  # subagent type to dispatch to
    tags: list[str] = Field(default_factory=list)

    # Provenance. `source` distinguishes a task the owner typed from one an agent filed.
    source: Literal["manual", "notion", "scout", "agent"] = "manual"
    origin: str | None = None        # which agent/schedule filed it
    origin_run_id: str | None = None # the run it came out of
    # Normalised title+domain. A daily loop that keeps finding the same thing must
    # bump a counter, not add a new card every morning.
    fingerprint: str | None = None
    seen_count: int = 1
    # Set when a sweep closed this card into another one (dedupe.py). A card with
    # this set is `done` but is NOT a completion: the board hides it from every
    # column and nothing that counts finished work should count it.
    duplicate_of: str | None = None
    notion_page_id: str | None = None
    task_ref: str | None = None  # the external queue's own id when mirrored from one
    # Risk tier. Deliberately a free string rather than a Literal: nine live cards
    # already carry values written before this was formalised, and `Store.tasks()`
    # DROPS a row that fails validation. Tightening the type here would silently
    # delete cards rather than reject an edit. The values are validated where it
    # matters instead, at the promotion gate in backlog.py.
    tier: str | None = None

    # ---- triage assessment ----
    # Who can actually do this, how risky it is, and whether it can be started.
    # Set by the /triage pass, read by the promotion gate and the ranking.
    #
    # `owner` is the field that was missing and it is the important one. Most of
    # this board is the owner's own commitments ("follow up with so-and-so", "send
    # the shipping label"), not agent work, so "promote it" was never the right verb
    # for the majority. Separating owner from tier lets the ranking answer "what do
    # I start on" without pretending Otto could have done it.
    owner: Owner | None = None
    readiness: Readiness | None = None
    assessed: str | None = None       # when it was last assessed
    # One line, written for the ranked list. `detail` is the evidence and runs to
    # thousands of characters; printing that as the reason a card matters is what
    # made `otto next` unreadable.
    assessed_note: str | None = None

    # ---- plan gate ----
    # The dispatched prompt used to be title + detail + a fixed template, so the
    # owner never saw what a run intended before it ran, and pulled the assumptions
    # apart afterwards instead. `plan` is what the run will do, written where they
    # can edit it (`otto task plan <id> --file`) and approve it (`--approve`). An approved
    # plan replaces the detail as the prompt body. A tier-1 card runs first in
    # `prepare` mode, which writes the proposal INTO `plan` and applies nothing;
    # approving that proposal is what lets it run for real.
    plan: str | None = None
    plan_approved: str | None = None       # when the owner approved it
    run_mode: Literal["run", "prepare"] | None = None
    # The level the next run gets, when the owner pinned one (`otto task set --permissions`,
    # `otto task yolo`). None derives it: a prepare run is `plan`, anything else `yolo`
    # (dispatch.permissions_for). Elevating is one word; nothing elevates on its own.
    permissions: Literal["plan", "yolo"] | None = None
    # ---- stale loop ----
    # Re-verification used to key off `created`, so a card checked yesterday was
    # "stale" again today and got another essay appended. Age now counts from the
    # last check, and a card can say when it is worth looking at again.
    last_checked: str | None = None
    next_look: str | None = None           # YYYY-MM-DD; skip stale checks before it

    run_id: str | None = None  # set once dispatched, links the card to its run
    created: str = Field(default_factory=lambda: iso(utcnow()))
    updated: str = Field(default_factory=lambda: iso(utcnow()))
    due: str | None = None

    # ---- auto-dispatch ----
    # `queued` means "Otto, run this". That makes the board a trigger, not just a
    # view, so the fields below exist to bound it.
    auto: bool = True          # False = park it in queued without ever dispatching
    cwd: str | None = None     # working directory for the spawned session
    attempts: int = 0          # incremented per dispatch; caps retries
    last_error: str | None = None
    result: str | None = None  # what the run reported back

    # ---- logistics dispatch (otto/logistics.py) ----
    # Set when the card was handed to a LIVE session in a herdr pane rather than
    # spawned as a headless run. There is no Run to settle; the pane watcher reads
    # the agent's state and screen instead.
    session_id: str | None = None
    pane_id: str | None = None
    pane_seq: int | None = None   # herdr state_change_seq at hand-off

    def touch(self) -> None:
        self.updated = iso(utcnow())


class Proposal(BaseModel):
    """One suggestion from the dispatcher: this card, to that idle session.

    Advisory: nothing moves until the owner approves. A dismissed pair stays
    dismissed for as long as that session is the same conversation, so the strip
    does not re-propose what they already said no to."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str
    pane_id: str
    agent: str                      # herdr agent name, else the pane id
    session_id: str | None = None   # the Claude session in that pane
    cwd: str | None = None
    confidence: float = 0.0
    rationale: str = ""
    status: Literal["proposed", "approved", "dismissed", "stale"] = "proposed"
    created: str = Field(default_factory=lambda: iso(utcnow()))
    decided_at: str | None = None


class BoardCard(BaseModel):
    """One card in a board column. Either a stored Task or a derived item."""

    id: str
    title: str
    status: TaskStatus
    domain: Domain = "work"
    priority: Priority = "normal"
    kind: Literal["task", "schedule", "run", "alert"] = "task"
    detail: str | None = None
    agent: str | None = None
    tags: list[str] = Field(default_factory=list)
    task_ref: str | None = None
    run_id: str | None = None
    age: str | None = None
    origin: str | None = None
    seen_count: int = 1
    # A due date the Task already carried but no surface ever rendered, so a card
    # due tomorrow looked identical to one due in March. Board and `next` both read
    # it now, which is the only reason setting one is worth anything.
    due: str | None = None
    # Derived cards cannot be dragged: there is no stored row to update.
    movable: bool = True
    command: str | None = None  # what to run to action it
    # Dispatch state, stored tasks only.
    auto: bool = True
    attempts: int = 0
    last_error: str | None = None
    # Enough of the stored row for the board to filter and sort without a second
    # fetch: when it was created, who filed it, and the triage judgement.
    created: str | None = None
    source: str | None = None
    owner: str | None = None
    tier: str | None = None
    readiness: str | None = None


class Snapshot(BaseModel):
    """A pushed view of something Otto cannot reach on its own.

    Calendar and mail live behind MCP servers that exist inside a Claude session,
    not in this daemon. Rather than pretend to have access, Otto stores what a
    session pushes and always renders how stale it is.

    Keyed by (kind, domain), never kind alone. The owner has a work inbox and a
    personal one, so "mail" is two different things; storing them under one key meant the
    second push silently destroyed the first. Every other entity here carries a
    domain and snapshots were the exception, which is exactly how work and personal
    would have ended up merged in one answer.
    """

    kind: str
    domain: Domain = "work"
    source: str = "claude-session"
    fetched_at: str = Field(default_factory=lambda: iso(utcnow()))
    summary: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.kind}"


class MachineStat(BaseModel):
    """Informational host facts. Never an alert: disk and offline signals are
    explicitly not action items."""

    label: str
    value: str
    detail: str | None = None


class Alert(BaseModel):
    level: Literal["info", "warn", "crit"]
    source: str
    message: str
    domain: Domain = "work"
    at: str = Field(default_factory=lambda: iso(utcnow()))


class Notice(BaseModel):
    """A message Otto deliberately sent to the owner.

    Distinct from Alert, which is DERIVED: recomputed from live state on every
    request, with no identity and no read state. That is right for "this schedule is
    stale" and useless for "Otto wrote this at 06:00 and you read it at 09:00".
    A notice persists, can be read once, and raises its toast exactly once.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    at: str = Field(default_factory=lambda: iso(utcnow()))
    level: Literal["info", "warn", "crit"] = "info"
    domain: Domain = "work"
    source: str = "otto"
    title: str
    body: str | None = None
    command: str | None = None      # the thing to run, when there is one
    # The board card this notice is ABOUT, when there is exactly one. Set by the due
    # nudge. It is what turns the toast from an announcement into a conversation: the
    # toast's Reply button deep-links the dashboard to this card's reply box, and the
    # notice sheet grows a Reply button for the same reason. A notice about eight
    # cards has no single card and leaves this unset.
    task_id: str | None = None
    read_at: str | None = None
    notify: bool = True             # may raise an OS toast
    # Set when the toast actually fired, so a restart does not re-toast a week of
    # notices at once. Absent + notify means "still owed a nudge".
    notified_at: str | None = None

    # What makes two notices "the same thing". Producers may set it explicitly; when
    # they do not, `notify.fingerprint` derives one.
    #
    # This exists because of a real night. `/slack-sweep` re-summarises the same Slack
    # DM every hour, and a model does not write the same sentence twice, so the feed's
    # bytes-digest dedupe saw a new drop each time. Seven warn notices about one
    # errand, six of them toasted between 00:15 and 06:37 local. Digest dedupe answers
    # "are these bytes new"; the question that matters is "is this the same thing you
    # already told me", and only a subject key answers that.
    key: str | None = None
    # How many times this same thing has come round. A repeat bumps this instead of
    # creating another notice, matching how `findings` treats a refiled task.
    seen_count: int = 1
    # When it was last re-reported. `at` deliberately stays at the FIRST sighting, so
    # a thing outstanding since 19:14 keeps saying 19:14 rather than looking new every
    # hour, which is the property that makes an age worth reading.
    last_seen: str | None = None


OutreachState = Literal["held", "sent", "killed", "expired", "failed"]


class Outreach(BaseModel):
    """A message Otto wants to send to another person, and its hold.

    THE ONE THING THIS MODEL IS FOR. Every other record in Otto describes something
    that happened to the owner or to a machine. This one describes something Otto is
    about to do to a COLLEAGUE, which is the only category where a mistake lands on
    someone who never opted in. So the record exists before the send, not after it:
    an outreach is written in `held` state, sits for `hold_minutes`, and is
    transmitted only when the timer expires without the owner killing it.

    Silence sends. That is the deliberate choice and the reason this is autonomy
    rather than a queue of drafts: in the normal case the owner does nothing at all.
    What they get is a window, and a record that outlives the Slack message.

    `why` IS REQUIRED. Not decoration. It is the whole basis on which the owner
    decides in ten seconds whether to kill something, and it is the field that makes
    the ledger auditable a month later when the Slack thread has scrolled away. An
    outreach that cannot say why it is being sent is one nobody can veto on evidence.

    NEVER MUTATED INTO A DIFFERENT MESSAGE. `body` is fixed at creation. Editing is
    kill-and-recreate, so what the owner approved by staying silent is exactly what
    went out, and the ledger cannot claim they tacitly approved text they never saw.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    at: str = Field(default_factory=lambda: iso(utcnow()))
    # Where it goes. `target` is the addressable id (a Slack user id or channel id);
    # `to` is who that is in words, because a ledger full of U0 ids is not one
    # anybody audits.
    channel: Literal["slack-dm", "slack-channel"] = "slack-dm"
    target: str
    to: str
    # Members-only enforcement (config.ORG_DOMAINS) is done in code before this
    # record exists, and the verdict is stored so the ledger shows what was checked
    # rather than implying it. `member` is someone on an org domain; `contractor` is
    # someone on a contractor domain, which a separate switch has to allow.
    recipient_kind: Literal["member", "contractor"] = "member"

    body: str
    why: str
    source: str = "otto"            # what composed it
    tier: int = 0                   # 0 = eligible to send itself, >0 = never sends

    state: OutreachState = "held"
    hold_minutes: int = 10
    send_after: str = Field(default_factory=lambda: iso(utcnow()))
    sent_at: str | None = None
    decided_at: str | None = None
    # `owner` is the person Otto works for acting by hand (kill, send-now); `timer`
    # is the hold expiring; `otto` is Otto itself (expiry with outreach disabled).
    decided_by: Literal["owner", "timer", "otto"] | None = None
    error: str | None = None
    # Provenance, so a bad message can be traced to the thing that produced it.
    run_id: str | None = None
    task_id: str | None = None

    @property
    def open(self) -> bool:
        return self.state == "held"


class Event(BaseModel):
    at: str = Field(default_factory=lambda: iso(utcnow()))
    level: Literal["debug", "info", "warn", "crit"] = "info"
    source: str = "otto"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


PostStatus = Literal["idea", "drafting", "drafted", "posted", "dropped"]


class Post(BaseModel):
    """A short public post: an idea Otto mined from the week, then a draft.

    Why this is its own record and not a Task. A card is work that closes; a
    post is a piece of writing that goes through states (idea, drafted, posted)
    and keeps its history on the way (every earlier draft, every note the owner
    gave back). Putting it on the board would also put "write a LinkedIn post" in the
    same column as "rotate the Falcon API key", and the whole point is that this
    is the low-energy, no-deadline kind of work. It lives beside the board, in
    its own view.

    Never trimmed, like Decision. Dropped ideas are kept so the same idea is not
    mined again next week; posted ones are kept as the voice exemplars for the
    next draft.
    """

    id: str                            # p-xxxxxx, from the hook
    status: PostStatus = "idea"
    hook: str                          # the first line; what the reader sees
    angle: str                         # what it is actually about, 2-3 sentences
    # The part that makes a post worth replying to. A stance is a claim a peer could
    # argue with; the pushback is their best argument, written down so the draft
    # can meet it instead of hedging; the question is what the reader is left to
    # answer about their own situation. The first run had none of these and every
    # idea was true, unarguable, and forgettable.
    stance: str | None = None
    pushback: str | None = None
    question: str | None = None
    themes: list[str] = Field(default_factory=list)   # gamedev, ai, security, enterprise
    evidence: list[str] = Field(default_factory=list) # the material lines it rests on
    energy: str | None = None          # "low" = writable in fifteen minutes
    # What the IDEA already reveals, per the miner. The draft's own flags come from
    # writing.scan() and live in `flags`, which is the deterministic check.
    reveals: list[str] = Field(default_factory=list)

    draft: str | None = None
    # True when the current draft text is the owner's own edit rather than a model's.
    # The next draft run is told so and told to keep his changes; a model draft
    # landing clears it.
    edited: bool = False
    alt_hooks: list[str] = Field(default_factory=list)
    flags: list[dict] = Field(default_factory=list)   # writing.scan() over `draft`
    versions: list[dict] = Field(default_factory=list) # {at, draft, run_id} before each redraft
    notes: list[dict] = Field(default_factory=list)    # {at, text}: what the owner asked for

    url: str | None = None
    posted_at: str | None = None
    error: str | None = None           # why the last run produced nothing

    fingerprint: str | None = None     # dedupe across ideas runs
    origin_run_id: str | None = None   # the ideas run that mined it
    draft_run_id: str | None = None    # the run currently drafting, or the last
    cost_usd: float = 0.0              # summed over every run for this post

    created: str = Field(default_factory=lambda: iso(utcnow()))
    updated: str = Field(default_factory=lambda: iso(utcnow()))

    def touch(self) -> None:
        self.updated = iso(utcnow())


class Decision(BaseModel):
    """A decision that was made, why, and what would change it.

    Otto records what HAPPENED in several places (runs, events, day rollups) and
    what is TO DO on the board, but nowhere recorded what was DECIDED. The
    reasoning existed only inside task detail fields, which meant it left the
    board the moment a card went `done`: "DESIGN CONSTRAINT (<owner>, <date>)"
    on one card, "ROOT PROBLEM" on another, all of it destined for deletion.

    Append-only, like `Event` and unlike `Task`. There is no edit and no delete:
    to change a decision you record a new one that supersedes it, so the fact
    that you changed your mind survives alongside what you now think. Same
    reasoning as registry.reconcile marking a vanished definition `missing`
    rather than dropping it.

    `revisit` is the field that earns the model's keep, and it is the one thing
    taken structurally from AIS-OS (github.com/nateherkai/AIS-OS, MIT): a
    decision records what would change it. Otto adds the half that makes it
    load-bearing rather than decorative -- `revisit_by` turns the condition into
    a date `decisions.gaps()` watches, so a decision made on assumptions nobody
    rechecked becomes a gap instead of a sentence in a file. An inert "what would
    change my mind" is a note; a watched one is a control.
    """

    id: str
    title: str
    # Required, both of them. A decision with no `why` is the thing this model
    # exists to stop being lost, so recording one without it is refused rather
    # than stored half-empty: an entry that says only WHAT was decided is
    # indistinguishable from the task detail fields it replaces.
    decision: str
    why: str

    alternatives: str | None = None
    revisit: str | None = None      # what would change this
    revisit_by: str | None = None   # ISO date; once past, gaps() raises it

    # Who made the call. A display name, not a role token, because decisions are
    # read by people and "the owner decided" is less useful than a name. Defaults to
    # the configured OWNER_NAME at record time, so a decision recorded before the
    # name was configured says so honestly rather than carrying a placeholder
    # someone else chose.
    owner: str = Field(default_factory=lambda: config.OWNER_NAME)
    domain: Domain = "work"
    tags: list[str] = Field(default_factory=list)

    # Day-level, because a decision is a fact about a day, not a second. `at` is
    # the recording timestamp and can differ: backfilling an old decision keeps
    # its real date instead of pretending it was decided today.
    decided: str = Field(default_factory=lambda: iso(utcnow())[:10])
    at: str = Field(default_factory=lambda: iso(utcnow()))

    task_id: str | None = None      # the board card it came out of, when there is one
    origin: str | None = None       # who recorded it, when not the owner at a keyboard

    supersedes: str | None = None
    superseded_by: str | None = None

    @property
    def live(self) -> bool:
        return self.superseded_by is None
