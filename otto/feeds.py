"""Producer/consumer feed directories: many producers, still one writer.

Stolen from Gaia's `command-center/feed/<source>/current.json` + `producers/`. The
shape is decoupled on purpose: a producer writes a file, the consumer reads it,
and neither knows the other exists.

WHY OTTO WANTS IT. Adding a data source today means editing `refresh.py` AND the
snapshot API, because snapshots are keyed `domain/kind` and the daemon is the only
writer. That is fine for Gmail and Calendar, which the refresher fetches itself,
but it makes every new source a code change in two places. A feed directory lets
ANY producer contribute -- a scheduled script, an agent, a Lambda, a manual drop --
without Otto having been taught about it in advance.

THE TRAP, and the whole design decision.

Otto has a single-writer rule: only the daemon writes state, `Store` raises
`WriteDenied` anywhere else, and the CLI and agents mutate through HTTP. A feed
directory that arbitrary producers write to CONFLICTS with that -- if feeds are
treated as state.

  So feeds are an INBOX, not state.

Producers write `FEED_DIR/<source>/current.json`. Nothing that reads Otto's state
ever reads that file. The daemon reads it on tick and ingests it into state
itself, through the same `Store` methods everything else uses. State keeps exactly
one writer; many producers become possible. That is the resolution, and every
other rule in this module follows from it.

A consequence worth stating: `ingest()` is daemon-only, and deliberately so. It is
not that a non-daemon caller is forbidden by politeness -- `Store._write` will
raise `WriteDenied` at it. The feed directory is the only part of this that is
writable by anyone, and it is not state.

SECOND TRAP: a feed file is UNTRUSTED INPUT.

It may have been written by an agent, so it must not be able to arm anything
unattended. A feed that can raise a `crit` notice or file a task with `auto=True`
is a feed that can start agents. Three enforcements, none of them optional:

  DECLARED ONLY   An undeclared directory under FEED_DIR is NEVER read. It is
                  reported as drift and ignored. If creating a directory were
                  enough, anything that can write a file could put cards on
                  the owner's board. Declaring a source is the human step.
  TRUST CEILING   `config.FeedSource.trust` caps what a source may do, and the
                  ceiling is enforced here, never claimed by the file. `data` may
                  become a panel and nothing more. `advisory` may additionally
                  PROPOSE board cards.
  REUSED FILER    Proposed cards go through `findings.file_tasks`, which already
                  has the discipline: fingerprint dedup so a producer running
                  hourly bumps a counter instead of duplicating a card, a per-run
                  cap, overflow LOGGED rather than silently truncated, and
                  `auto=False` always. Reused rather than reinvented, because a
                  second filer would be a second place for that discipline to rot.

No trust level permits `crit`, and none permits auto-dispatch. Those stay with
Otto's own detectors, which are code in this repo rather than JSON anyone can drop.

THIRD: staleness. Otto never presents a stale snapshot as current, and a feed gets
the same treatment. The snapshot Otto stores is stamped with the producer's
`produced_at`, NOT with ingest time, so the age the owner reads is the age of the DATA.
Each source declares `max_age_hours`, and a producer that goes quiet becomes a
GAP rather than silence -- silence is the failure this whole system exists to
remove.

Prior art: `manifest.py` + `config.CAPABILITIES` for the declare-then-diff shape,
which this reuses down to the `undeclared()` / `gaps()` / `render()` split.

KNOWN LIMIT, stated rather than papered over: the schema enforced below is a
strict shared ENVELOPE (`FeedFile`, `extra="forbid"`), plus the per-source trust
ceiling. A per-source FIELD schema -- "netcheck items must carry a hostname" --
has no shape to take until a real source is declared, so the extension point is
marked at `_validate_items` and left unwired instead of guessed at.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, findings, notify
from .models import Snapshot, iso, utcnow
from .store import Store

# A source name becomes a directory name and a snapshot key, so it is constrained
# rather than trusted. This also makes traversal unrepresentable: no dot segments,
# no separators, so `feed/../../state` cannot be spelled.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# How far ahead of now a `produced_at` may be before the file is rejected.
# `produced_at` is producer-controlled and it drives the age Otto renders, so a
# producer could otherwise claim tomorrow and look permanently fresh -- which is
# precisely the "stale data presented as current" failure. Small tolerance for
# real clock skew between a Lambda and this box.
FUTURE_TOLERANCE = timedelta(minutes=5)

# Bounds on the window an incremental sweep is told to read. See coverage_hint().
#
# MIN exists so a watermark can never make a run trivially cheap and blind: a
# message edited or delivered slightly out of order just after the last sweep
# would otherwise sit in an unread sliver. Always re-reading a few hours costs
# little and removes a whole class of "it was there but nobody read it".
#
# MAX is the honest ceiling. If the daemon was down for three days, the right
# behaviour is NOT to search three days of history in one run: that is the
# unbounded-pagination cost the window exists to avoid. Read MAX, and report
# coverage_since so Otto can see the gap and say so, rather than pretending.
SWEEP_WINDOW_MIN_HOURS = 6
SWEEP_WINDOW_MAX_HOURS = 24


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------

class FeedNotice(BaseModel):
    """A message a producer wants the owner to see. Level is a REQUEST, not a
    decision: `ingest` clamps it to the source's trust ceiling."""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str | None = None
    level: Literal["info", "warn", "crit"] = "info"
    command: str | None = None
    # What makes this the same message as last run. Optional, and worth setting for
    # any producer that writes its text with a model.
    #
    # The drop is deduped on a bytes digest, which is exactly the wrong instrument
    # here: `/slack-sweep` re-summarises the same DM every hour and never writes the
    # same sentence twice, so every run looked new and posted another notice. Seven
    # for one errand overnight. A producer that supplies a stable key (the Slack
    # message ts, the card id, the thread id) gets deduped properly no matter how the
    # prose comes out; one that does not falls back to `notify.fingerprint`.
    key: str | None = None


class FeedTask(BaseModel):
    """A proposed board card. Honoured only for `advisory` sources, and only ever
    through findings.file_tasks, which forces auto=False."""

    model_config = ConfigDict(extra="forbid")

    title: str
    detail: str | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class FeedFile(BaseModel):
    """The one shape a `current.json` may take.

    `extra="forbid"` is deliberate. A producer that misspells `itmes` should get a
    loud rejection, not a silently empty panel: an ignored field means a producer
    believing it sent something it did not, which is worse than a parse error.
    """

    model_config = ConfigDict(extra="forbid")

    # Required. Everything else about staleness depends on it, so a producer that
    # omits it does not get to be ingested with a default of "now".
    produced_at: str
    summary: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[FeedTask] = Field(default_factory=list)
    notice: FeedNotice | None = None
    # The OLDEST moment this run actually read back to, which is not the same claim
    # as produced_at (when it finished). Optional, and only meaningful for a
    # producer that sweeps a time window.
    #
    # This is what makes an incremental sweep safe. A producer that re-reads a
    # rolling window every run is self-healing but pays for the whole window every
    # time; one that resumes from a stored point is cheap but skips a gap in
    # silence if a run half-fails. Reporting coverage turns that into a checkable
    # claim: Otto advances the watermark only when a run demonstrates it covered
    # everything back to the point it was given, and otherwise holds the watermark
    # so the next run re-reads the gap. See coverage_hint().
    coverage_since: str | None = None
    # Free-form provenance, e.g. which host or Lambda wrote it. Rendered, never
    # trusted, never used for a decision.
    producer: str | None = None


class Drop(NamedTuple):
    """A parsed feed file plus the bytes-digest that identifies this exact drop."""

    payload: FeedFile
    digest: str
    produced_at: datetime


class Status(NamedTuple):
    source: config.FeedSource
    state: str          # ok | stale | no-drop | no-dir | rejected | undeclared
    age_hours: float | None = None
    detail: str = ""
    items: int = 0

    @property
    def ok(self) -> bool:
        return self.state == "ok"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def source_dir(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid feed source name {name!r}")
    return config.FEED_DIR / name


def drop_path(name: str) -> Path:
    return source_dir(name) / config.FEED_FILE


def scaffold() -> list[str]:
    """Create the feed root and a directory per declared source.

    A producer should not have to mkdir its way in, and an empty directory that
    exists is a visible contract: it says Otto is waiting for this file.
    """
    made: list[str] = []
    if not config.FEED_DIR.is_dir():
        config.FEED_DIR.mkdir(parents=True, exist_ok=True)
        made.append(str(config.FEED_DIR))
    for src in config.FEED_SOURCES:
        d = source_dir(src.name)
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            made.append(str(d))
    return made


# ---------------------------------------------------------------------------
# reading a drop
# ---------------------------------------------------------------------------

def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _validate_items(items: list[dict[str, Any]], src: config.FeedSource
                    ) -> list[dict[str, Any]]:
    """Shared item validation, capped.

    EXTENSION POINT for per-source field schemas. Nothing dispatches on
    `src` yet, and inventing a per-source shape with no declared source to
    shape it would be a guess. What is enforced today: items are objects,
    string values are bounded, and the list is capped -- so a runaway producer
    cannot turn the panel into a log or the store into a dumping ground.
    """
    out: list[dict[str, Any]] = []
    for item in items[: config.FEED_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        clean: dict[str, Any] = {}
        for k, v in list(item.items())[:12]:
            key = str(k)[:40]
            if isinstance(v, str):
                clean[key] = v[:300]
            elif isinstance(v, (int, float, bool)) or v is None:
                clean[key] = v
            else:
                # Nested structures are not rendered by any panel, so they would
                # be weight with no reader.
                clean[key] = str(v)[:300]
        out.append(clean)
    return out


def read_drop(name: str) -> tuple[Drop | None, str | None]:
    """Read and validate one source's `current.json`. Returns (drop, error).

    Never raises for a producer's mistake: a bad feed file is a reportable
    condition, not a daemon problem. Both being None means "no file yet".
    """
    p = drop_path(name)
    if not p.is_file():
        return None, None

    try:
        size = p.stat().st_size
    except OSError as e:
        return None, f"unstatable ({type(e).__name__})"
    if size > config.FEED_MAX_BYTES:
        return None, (f"{size} bytes exceeds the {config.FEED_MAX_BYTES}-byte cap; "
                      "refusing to read")
    if size == 0:
        return None, "empty file"

    try:
        raw = p.read_bytes()
    except OSError as e:
        # A producer mid-write. Transient, and treating it as corruption would
        # quarantine a healthy feed -- the same mistake Store._read documents.
        return None, f"unreadable ({type(e).__name__}), will retry next tick"

    digest = hashlib.sha256(raw).hexdigest()

    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"not valid JSON: {e}"
    if not isinstance(obj, dict):
        return None, f"top level must be an object, got {type(obj).__name__}"

    try:
        payload = FeedFile.model_validate(obj)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in (first.get("loc") or ())) or "?"
        return None, f"schema: {loc}: {first.get('msg') or 'invalid'}"

    produced = _parse_ts(payload.produced_at)
    if produced is None:
        return None, f"produced_at {payload.produced_at!r} is not a timestamp"
    if produced > utcnow() + FUTURE_TOLERANCE:
        # See FUTURE_TOLERANCE: a future stamp is how a quiet producer would
        # look permanently fresh.
        return None, f"produced_at {payload.produced_at!r} is in the future"

    return Drop(payload, digest, produced), None


# ---------------------------------------------------------------------------
# incremental sweep window
# ---------------------------------------------------------------------------

def _clamp_window(watermark: datetime | None, now: datetime) -> datetime:
    """The moment a sweep should read back to, bounded both ways."""
    floor = now - timedelta(hours=SWEEP_WINDOW_MAX_HOURS)
    ceiling = now - timedelta(hours=SWEEP_WINDOW_MIN_HOURS)
    if watermark is None:
        return floor
    return min(max(watermark, floor), ceiling)


def next_watermark(prior: dict[str, Any], drop: Drop) -> tuple[str | None, str | None]:
    """The watermark to store after this drop, and why if it is being held.

    Advance only on a demonstrated claim. The run was given a point to read back
    to; if it reports covering at least that far, there is no gap and the next run
    can start where this one finished. If it reports less (a truncated search, a
    page cap hit) or reports nothing at all, the watermark does NOT move, so the
    next run re-reads the same span. Holding is always safe and merely costs money;
    advancing wrongly loses a message silently, which is the failure that matters.
    """
    asked = _parse_ts(prior.get("coverage_asked") or "")
    covered = _parse_ts(drop.payload.coverage_since or "")
    if covered is None:
        return prior.get("coverage_watermark"), "no coverage_since reported"
    if covered > drop.produced_at:
        # Nonsense: read back to after it finished. Distrust it entirely.
        return prior.get("coverage_watermark"), "coverage_since is after produced_at"
    if asked is not None and covered > asked + FUTURE_TOLERANCE:
        return (prior.get("coverage_watermark"),
                f"covered only back to {iso(covered)}, was asked for {iso(asked)}")
    return iso(drop.produced_at), None


def coverage_hint(store: Store, name: str, now: datetime | None = None) -> str | None:
    """Instruction text telling a sweep producer how far back to read.

    Returned as prose for a system prompt rather than a parameter, because the
    producer is a model reading a slash command, and the command already documents
    a default window. This narrows that default when Otto can prove the previous
    run covered the ground; when it cannot, this returns None and the command's own
    24h default stands unchanged.

    Also records what was asked, so next_watermark() can check the answer against
    the question rather than trusting the reply on its own.
    """
    now = now or utcnow()
    state = store.feed_state(name)
    watermark = _parse_ts(state.get("coverage_watermark") or "")
    if watermark is None:
        return None
    since = _clamp_window(watermark, now)
    # Recording the ask is not optional bookkeeping: next_watermark() checks the
    # reported coverage against it, so an unrecorded ask means an unverifiable
    # answer. If the write is refused (this is daemon-only state, and the caller
    # is normally launch.py inside the daemon), fail CLOSED: return no hint, the
    # producer reads its full default window, and nothing is narrowed on trust.
    try:
        store.put_feed_state(name, {"coverage_asked": iso(since)})
    except Exception:  # noqa: BLE001 - a refused write must not break the launch
        return None
    hours = (now - since).total_seconds() / 3600
    return (
        f"INCREMENTAL WINDOW (from Otto, overrides the default window in the "
        f"command): the previous sweep covered everything up to {iso(watermark)}. "
        f"Read back to {iso(since)} only, about {hours:.0f}h, instead of the full "
        f"default window.\n"
        f"You MUST report how far back you actually read, as `coverage_since` in "
        f"the drop envelope, RFC3339 UTC. Report the oldest moment you genuinely "
        f"covered: if pagination capped out, or a search failed, report the oldest "
        f"point you are SURE of, not the point you were asked for. Otto advances "
        f"its watermark only when your coverage reaches the requested point, so an "
        f"honest short answer costs one repeated read and a dishonest one loses "
        f"messages."
    )


# ---------------------------------------------------------------------------
# ingest  (daemon only)
# ---------------------------------------------------------------------------

def ingest(store: Store) -> list[str]:
    """Consume every declared feed into state. Called from the daemon tick.

    Returns notes for the event log. Only DECLARED sources are read; see the
    module docstring for why that is the load-bearing rule and not a convenience.
    """
    notes: list[str] = []
    for src in config.FEED_SOURCES:
        try:
            notes.extend(_ingest_one(store, src))
        except Exception as e:  # noqa: BLE001 - one bad feed must not stall the rest
            notes.append(f"feed {src.name}: ingest failed: {type(e).__name__}: {e}")
    return notes


def _ingest_one(store: Store, src: config.FeedSource) -> list[str]:
    notes: list[str] = []
    prior = store.feed_state(src.name)
    drop, error = read_drop(src.name)

    if error is not None:
        # Complain ONCE per bad drop, not every tick. Without this a malformed
        # file would fill the event log at the tick rate until someone noticed,
        # and the noise would bury the very message that explains the problem.
        stamp = f"err:{hashlib.sha256(error.encode()).hexdigest()[:16]}"
        if prior.get("digest") != stamp:
            store.put_feed_state(src.name, {
                "digest": stamp, "state": "rejected", "error": error,
                "checked_at": iso(utcnow()),
            })
            notes.append(f"feed {src.name} REJECTED: {error}")
        return notes

    if drop is None:
        return notes  # no file yet; staleness is `gaps()`'s job, not a log line

    if prior.get("digest") == drop.digest:
        return notes  # unchanged; re-ingesting would re-propose the same cards

    # 1. The panel. fetched_at is the producer's produced_at, never now: the age
    #    Otto renders has to be the age of the data, or a daemon restart would
    #    make a week-old drop look freshly fetched.
    items = _validate_items(drop.payload.items, src)
    store.put_snapshot(Snapshot(
        kind=src.kind,
        domain=src.domain,  # type: ignore[arg-type]
        summary=(drop.payload.summary or "")[:200] or None,
        items=items,
        source=f"feed:{src.name}",
        fetched_at=iso(drop.produced_at),
    ))
    notes.append(f"feed {src.name}: {len(items)} item(s) "
                 f"produced {iso(drop.produced_at)}")

    # 2. Proposed cards. Advisory only, and always through findings.file_tasks so
    #    the dedup / cap / overflow-logged / auto=False discipline is the same one
    #    agent findings get.
    filed = 0
    if drop.payload.tasks:
        if src.trust == config.TRUST_ADVISORY:
            raws = [{"title": t.title, "detail": t.detail,
                     "priority": t.priority, "domain": src.domain}
                    for t in drop.payload.tasks]
            filed_notes = findings.file_tasks(store, raws, origin=f"feed:{src.name}")
            notes.extend(filed_notes)
            filed = len(filed_notes)
        else:
            # Loud, not silent. A producer proposing cards it is not trusted to
            # propose has a wrong idea of its own role, and that is worth saying.
            notes.append(f"feed {src.name}: IGNORED {len(drop.payload.tasks)} "
                         f"proposed task(s) - trust is '{src.trust}', which cannot "
                         f"file to the board")

    # 3. Notice, clamped to the trust ceiling. Never rejected for asking too high:
    #    dropping the whole notice would lose real content over a bad level field.
    if drop.payload.notice is not None:
        ceiling = config.FEED_MAX_NOTICE_LEVEL.get(src.trust, "info")
        rank = {"info": 0, "warn": 1, "crit": 2}
        asked = drop.payload.notice.level
        level = asked if rank.get(asked, 0) <= rank.get(ceiling, 0) else ceiling
        if level != asked:
            notes.append(f"feed {src.name}: notice level '{asked}' CLAMPED to "
                         f"'{level}' (trust '{src.trust}')")
        notify.post(
            store, f"{src.title or src.name}: {drop.payload.notice.title}"[:160],
            body=drop.payload.notice.body, level=level, domain=src.domain,
            source=f"feed:{src.name}", command=drop.payload.notice.command,
            key=(f"feed:{src.name}:{drop.payload.notice.key}"
                 if drop.payload.notice.key else None),
        )

    # 4. The incremental-sweep watermark, which moves only on a coverage claim this
    #    run actually backed up. `held` is logged rather than swallowed: a producer
    #    that never reports coverage, or keeps falling short, is one whose window is
    #    silently not shrinking, and the only symptom otherwise is a bill.
    watermark, held = next_watermark(prior, drop)
    if held and drop.payload.coverage_since:
        notes.append(f"feed {src.name}: watermark HELD - {held}")

    store.put_feed_state(src.name, {
        "digest": drop.digest,
        "state": "ok",
        "error": None,
        "produced_at": iso(drop.produced_at),
        "ingested_at": iso(utcnow()),
        "producer": drop.payload.producer,
        "items": len(items),
        "ingest_count": int(prior.get("ingest_count") or 0) + 1,
        "filed_total": int(prior.get("filed_total") or 0) + filed,
        "checked_at": iso(utcnow()),
        "coverage_since": drop.payload.coverage_since,
        "coverage_watermark": watermark,
    })
    return notes


# ---------------------------------------------------------------------------
# declared vs observed
# ---------------------------------------------------------------------------

def observe() -> set[str]:
    """Directory names present under FEED_DIR. Names only."""
    if not config.FEED_DIR.is_dir():
        return set()
    try:
        return {d.name for d in config.FEED_DIR.iterdir() if d.is_dir()}
    except OSError:
        return set()


def undeclared() -> list[str]:
    """Present but not declared.

    Same drift axis as `manifest.undeclared()`. These are NOT ingested -- a
    directory nobody declared is one nobody has said is trusted to contribute, and
    reading it would make "any producer can contribute" mean "any writer can act".
    """
    return sorted(observe() - {s.name for s in config.FEED_SOURCES})


def status(store: Store | None = None) -> list[Status]:
    """Per-source state, ages read from what was actually ingested."""
    store = store or Store()
    ledger = store.feeds()
    now = utcnow()
    out: list[Status] = []

    for src in config.FEED_SOURCES:
        rec = ledger.get(src.name) or {}
        if rec.get("state") == "rejected":
            out.append(Status(src, "rejected", None,
                              str(rec.get("error") or "malformed drop")))
            continue
        if not source_dir(src.name).is_dir():
            out.append(Status(src, "no-dir", None,
                              "feed directory does not exist; run `otto feeds init`"))
            continue

        produced = _parse_ts(str(rec.get("produced_at") or "")) if rec.get("produced_at") else None
        if produced is None:
            out.append(Status(src, "no-drop", None,
                              "declared, directory present, nothing ingested yet"))
            continue

        age_h = (now - produced).total_seconds() / 3600
        count = int(rec.get("items") or 0)
        if src.max_age_hours is not None and age_h > src.max_age_hours:
            out.append(Status(src, "stale", age_h,
                              f"declared max age {src.max_age_hours}h", count))
        else:
            out.append(Status(src, "ok", age_h, "", count))
    return out


def gaps(store: Store | None = None) -> list[dict]:
    """Feed drift as advisor-shaped rows, wired the same way manifest.gaps() is."""
    rows: list[dict] = []
    for st in status(store):
        src = st.source
        if st.state == "stale":
            rows.append({
                "id": f"gap:feed-stale:{src.key}",
                "kind": "feed-stale",
                "domain": src.domain,
                "title": f"feed '{src.name}' has gone quiet "
                         f"({st.age_hours:.1f}h old, max {src.max_age_hours}h)",
                "why": "A producer going quiet must be a gap, not silence. The panel "
                       "still renders its age, so nothing stale is shown as current, "
                       f"but nobody has dropped a {src.name} file in "
                       f"{st.age_hours:.1f}h. " + (src.why or ""),
                "command": "otto feeds",
                "score": 70,
            })
        elif st.state == "rejected":
            rows.append({
                "id": f"gap:feed-rejected:{src.key}",
                "kind": "feed-rejected",
                "domain": src.domain,
                "title": f"feed '{src.name}' is being rejected: {st.detail[:70]}",
                "why": "The producer is writing, so it believes it is reporting, but "
                       "the drop fails validation and nothing is ingested. That is "
                       "worse than a dead producer because it looks alive.",
                "command": "otto feeds",
                "score": 75,
            })
        elif st.state in ("no-drop", "no-dir"):
            rows.append({
                "id": f"gap:feed-silent:{src.key}",
                "kind": "feed-silent",
                "domain": src.domain,
                "title": f"feed '{src.name}' declared but has never produced",
                "why": f"{st.detail}. Declared sources are meant to have a producer "
                       "behind them; either wire it up or undeclare it. "
                       + (src.why or ""),
                "command": "otto feeds",
                "score": 45,
            })
        elif st.state == "ok" and src.max_age_hours is None:
            # The advisor's own unknown-unknown class: a source that can stop
            # forever with nothing ever saying so.
            rows.append({
                "id": f"gap:feed-unbounded:{src.key}",
                "kind": "feed-unbounded",
                "domain": src.domain,
                "title": f"feed '{src.name}' has no declared max age",
                "why": "It can stop producing forever and nothing will say so. Set "
                       "max_age_hours on its FeedSource so going quiet alarms.",
                "command": "otto feeds",
                "score": 30,
            })

    for name in undeclared():
        rows.append({
            "id": f"gap:feed-undeclared:{name}",
            "kind": "feed-undeclared",
            "domain": config.WORK,
            "title": f"undeclared feed directory: {name}",
            "why": "Something is dropping files here and Otto is NOT reading them: "
                   "an undeclared source has nobody accountable for what it can put "
                   "in front of the owner. Declare it in config.FEED_SOURCES with a trust "
                   "level, or delete the directory.",
            "command": "otto feeds",
            "score": 40,
        })
    return rows


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _age(hours: float | None) -> str:
    if hours is None:
        return "     -"
    if hours <= 0:
        # Inside FUTURE_TOLERANCE, so a hair ahead of this clock. Rendering a
        # negative age would look like a bug in Otto rather than clock skew.
        return "   now"
    if hours < 1:
        return f"{hours * 60:5.0f}m"
    if hours < 48:
        return f"{hours:5.1f}h"
    return f"{hours / 24:5.1f}d"


def render(store: Store | None = None) -> str:
    """Human-readable report for `otto feeds`."""
    rows = status(store)
    extra = undeclared()
    if not rows and not extra:
        return ("feed directories\n\n"
                f"  no sources declared. Feed root: {config.FEED_DIR}\n"
                "  Declare one in config.FEED_SOURCES, then drop "
                f"<source>/{config.FEED_FILE}.\n")

    width = max([len(s.source.name) for s in rows] + [10])
    glyph = {"ok": "  ok", "stale": "STALE", "no-drop": "  ---",
             "no-dir": "  ---", "rejected": " REJECT"}
    lines = ["feed directories  (inbox, not state -- the daemon ingests)", "",
             f"  root: {config.FEED_DIR}", ""]
    for st in rows:
        src = st.source
        lines.append(f"  {glyph.get(st.state, '   ??'):>7}  {src.name:<{width}}  "
                     f"{_age(st.age_hours)}  {src.trust:<8} "
                     f"{src.domain:<8} {st.items:>2} item(s)")
        if st.detail:
            lines.append(f"           {st.detail}")
    lines.append("")

    if extra:
        lines.append("undeclared, NOT ingested:")
        lines += [f"        {n}" for n in extra]
        lines.append("")

    bad = [s for s in rows if not s.ok]
    lines.append(f"{len(rows)} declared, {len(bad)} not delivering, "
                 f"{len(extra)} undeclared")
    return "\n".join(lines)
