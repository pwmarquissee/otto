"""Single-writer JSON state store.

Design constraint: state is plain JSON on disk (chosen deliberately), but the
daemon, the CLI, spawned agents, and cron would all want to touch it. Rather
than bolt locking onto every caller, Otto enforces ONE writer:

  * The daemon process writes. `config.mark_daemon()` unlocks writes.
  * Everyone else reads, or calls the daemon's HTTP API.

Reads take no lock because writes are atomic: content goes to a temp file in
the same directory, then os.replace() swaps it in. On Windows os.replace is
atomic within a volume, so a reader sees either the whole old file or the whole
new one, never a torn write.

It does NOT follow that a concurrent read always succeeds. Windows can refuse
the open outright with a sharing violation while the swap is in flight, which
surfaces as PermissionError. That is transient and means "try again", never
"there is no data" - see `_read`, which lost the entire task board by confusing
the two.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .models import (
    Decision,
    Event,
    Integration,
    Proposal,
    RegistryEntry,
    Run,
    Schedule,
    Notice,
    Outreach,
    Post,
    Session,
    Snapshot,
    Task,
    iso,
    utcnow,
)

RUNS = "runs.json"
REGISTRY = "registry.json"
SCHEDULES = "schedules.json"
INTEGRATIONS = "integrations.json"
TASKS = "tasks.json"
SNAPSHOTS = "snapshots.json"
CHAT = "chat.json"
DAYS = "days.json"
NOTICES = "notices.json"
MEETINGS = "meetings.json"
FEEDS = "feeds.json"
DECISIONS = "decisions.json"
POSTS = "writing.json"
OUTREACH = "outreach.json"
SESSIONS = "sessions.json"
PROPOSALS = "logistics.json"
KNOWN = "known.json"
SETUP = "setup.json"
EVENTS = "events.jsonl"
# Claude Code's own per-request telemetry, appended by the OTLP receiver in the
# daemon. Read by ledger.py; the file layout is telemetry.py's.
TELEMETRY = "telemetry.jsonl"


class WriteDenied(RuntimeError):
    """Raised when a non-daemon process attempts a state write."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # Windows refuses to replace a file another handle has open, and readers
        # (an API request, a CLI, the desktop app) open these files without the
        # writer's lock on purpose. A read lasts microseconds, so a short retry
        # turns "Access is denied" from a lost write into a 25ms delay.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.dir = state_dir or config.STATE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        # One writer process is not enough on its own: the daemon's tick loop and
        # its request handlers (FastAPI runs sync endpoints in a threadpool) can
        # interleave read-modify-write cycles. Compound mutators hold this lock
        # across BOTH the read and the write. Reentrant so nesting is safe.
        self.lock = threading.RLock()
        # Event appends that could not land (see append_event). A count and the
        # last error, so `otto doctor` can say the log has holes rather than the
        # holes being silent.
        self.log_failures = 0
        self.last_log_error: str | None = None
        # Sessions get their OWN lock, and the exception needs justifying because
        # everything else shares one. A session write is on the critical path of
        # every Claude Code turn on this machine: the hook blocks the turn until the
        # POST returns. sessions.json participates in no compound mutation with any
        # other file, so sharing `self.lock` would buy nothing and would let an
        # unrelated multi-write tick (feed ingest, a run sweep) add its hold time to
        # every turn the owner takes.
        self.session_lock = threading.RLock()
        # Same argument for telemetry: an append per export batch from every live
        # session, no compound mutation, so it must not queue behind a tick.
        self.telemetry_lock = threading.Lock()
        # Change counter for everything a dashboard can see. Every state write
        # (`_write`) and every event append bumps it; /api/state caches its
        # payload per version and /ws/events tells the browser when it moved, so
        # the dashboard fetches on change instead of re-pulling megabytes on a
        # timer. Telemetry appends do NOT bump it: nothing in the state payload
        # reads telemetry, and a bump per export batch from every live session
        # would invalidate the cache every few seconds for no visible change.
        # Process-local: it restarts at 0 with the daemon, and the socket's hello
        # carries the current value so a reconnecting client resyncs.
        self.version = 0
        self.version_at = 0.0
        self._version_lock = threading.Lock()

    def _bump(self) -> None:
        with self._version_lock:
            self.version += 1
            self.version_at = time.time()

    # ---- raw json -----------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default: Any) -> Any:
        """Read a state file. Never invents emptiness.

        This function previously lost the entire task board. It caught OSError
        alongside JSONDecodeError, treated both as "corrupt", renamed the file to
        .corrupt and returned `default`. On Windows a reader can hit a transient
        PermissionError while `os.replace` swaps a file in, and PermissionError IS
        an OSError. So a healthy file got quarantined and the board reported empty.
        Then a read-modify-write persisted that emptiness. The recovered file parsed
        perfectly on the first attempt.

        Two rules now:
          1. A transient read error is retried, then RAISED. It is never allowed to
             masquerade as "there is no data". Returning `default` for "I could not
             read it" is the lie that caused the loss.
          2. Only a genuine JSONDecodeError counts as corruption, and quarantining
             one is loud: it is real data loss and must never be silent.
        """
        p = self._path(name)
        if not p.exists():
            return default

        last: OSError | None = None
        for attempt in range(4):
            try:
                text = p.read_text(encoding="utf-8")
                break
            except OSError as e:
                # Sharing violations clear in milliseconds; back off briefly.
                last = e
                time.sleep(0.05 * (attempt + 1))
        else:
            raise OSError(
                f"could not read {name} after 4 attempts: {last}. Refusing to treat "
                "an unreadable file as empty state."
            ) from last

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if not config.is_daemon():
                raise
            quarantine = p.with_suffix(p.suffix + ".corrupt")
            p.replace(quarantine)
            # Bypass self.log: it writes through this same class, and a failure here
            # must surface even if the event log itself is unhappy.
            try:
                self.append_event(Event(
                    level="crit", source="store",
                    message=(f"{name} FAILED TO PARSE and was quarantined to "
                             f"{quarantine.name}: {e}. State for it is now empty. "
                             "This is data loss, not a clean start."),
                ))
            except Exception:  # noqa: BLE001
                pass
            print(f"[otto] CRITICAL: {name} unparseable, quarantined to "
                  f"{quarantine.name}. {e}", file=sys.stderr)
            return default

    def _write(self, name: str, data: Any) -> None:
        if not config.is_daemon():
            raise WriteDenied(
                f"refusing to write {name}: only the Otto daemon writes state. "
                "Use the HTTP API (otto CLI) instead."
            )
        _atomic_write(self._path(name), json.dumps(data, indent=2) + "\n")
        self._bump()

    # ---- runs ---------------------------------------------------------------

    def runs(self) -> list[Run]:
        return [Run.model_validate(r) for r in self._read(RUNS, [])]

    def save_runs(self, runs: list[Run]) -> None:
        # Newest first, bounded. Active runs are never trimmed away.
        active = [r for r in runs if r.active]
        done = [r for r in runs if not r.active]
        done.sort(key=lambda r: r.started, reverse=True)
        keep = active + done[: config.RUN_HISTORY_LIMIT]
        keep.sort(key=lambda r: r.started, reverse=True)
        self._write(RUNS, [r.model_dump() for r in keep])

    def get_run(self, run_id: str) -> Run | None:
        for r in self.runs():
            if r.id == run_id or r.id.startswith(run_id):
                return r
        return None

    def upsert_run(self, run: Run) -> Run:
        with self.lock:
            runs = self.runs()
            for i, existing in enumerate(runs):
                if existing.id == run.id:
                    runs[i] = run
                    break
            else:
                runs.append(run)
            self.save_runs(runs)
        return run

    # ---- registry -----------------------------------------------------------

    def registry(self) -> list[RegistryEntry]:
        return [RegistryEntry.model_validate(e) for e in self._read(REGISTRY, [])]

    def save_registry(self, entries: list[RegistryEntry]) -> None:
        self._write(REGISTRY, [e.model_dump() for e in entries])

    # ---- schedules ----------------------------------------------------------

    def schedules(self) -> list[Schedule]:
        return [Schedule.model_validate(s) for s in self._read(SCHEDULES, [])]

    def save_schedules(self, schedules: list[Schedule]) -> None:
        self._write(SCHEDULES, [s.model_dump() for s in schedules])

    def get_schedule(self, name: str) -> Schedule | None:
        return next((s for s in self.schedules() if s.name == name), None)

    def upsert_schedule(self, sched: Schedule) -> Schedule:
        with self.lock:
            items = self.schedules()
            for i, existing in enumerate(items):
                if existing.name == sched.name:
                    items[i] = sched
                    break
            else:
                items.append(sched)
            self.save_schedules(items)
        return sched

    def delete_schedule(self, name: str) -> bool:
        with self.lock:
            items = self.schedules()
            remaining = [s for s in items if s.name != name]
            if len(remaining) == len(items):
                return False
            self.save_schedules(remaining)
            return True

    # ---- snapshots ----------------------------------------------------------

    def snapshots(self) -> dict[str, Snapshot]:
        """Keyed `domain/kind`. Legacy bare-`kind` keys are read as work.

        The old format stored `{"mail": {...}}` with no domain. Those predate the
        personal Gmail, so work is the correct reading, and migrating on read means
        no separate migration step can be forgotten.
        """
        raw = self._read(SNAPSHOTS, {})
        out: dict[str, Snapshot] = {}
        for stored_key, body in (raw or {}).items():
            if not isinstance(body, dict):
                continue
            body = dict(body)
            if "/" in stored_key:
                domain, _, kind = stored_key.partition("/")
                body.setdefault("domain", domain)
                body.setdefault("kind", kind)
            else:
                body.setdefault("kind", stored_key)
                body.setdefault("domain", config.WORK)
            try:
                snap = Snapshot.model_validate(body)
            except ValueError:
                continue
            out[snap.key] = snap
        return out

    def put_snapshot(self, snap: Snapshot) -> Snapshot:
        with self.lock:
            current = {k: v.model_dump() for k, v in self.snapshots().items()}
            current[snap.key] = snap.model_dump()
            self._write(SNAPSHOTS, current)
        return snap

    # ---- notices ------------------------------------------------------------

    def notices(self) -> list[Notice]:
        out = []
        for n in self._read(NOTICES, []):
            try:
                out.append(Notice.model_validate(n))
            except ValueError:
                continue
        return out

    def save_notices(self, notices: list[Notice]) -> None:
        notices.sort(key=lambda n: n.at, reverse=True)
        self._write(NOTICES, [n.model_dump() for n in notices[:200]])

    def put_notice(self, notice: Notice) -> Notice:
        with self.lock:
            all_n = self.notices()
            for i, existing in enumerate(all_n):
                if existing.id == notice.id:
                    all_n[i] = notice
                    break
            else:
                all_n.append(notice)
            self.save_notices(all_n)
        return notice

    def get_notice(self, notice_id: str) -> Notice | None:
        for n in self.notices():
            if n.id == notice_id or n.id.startswith(notice_id):
                return n
        return None

    def delete_notice(self, notice_id: str) -> bool:
        with self.lock:
            all_n = self.notices()
            keep = [n for n in all_n
                    if not (n.id == notice_id or n.id.startswith(notice_id))]
            if len(keep) == len(all_n):
                return False
            self.save_notices(keep)
            return True

    # ---- days ---------------------------------------------------------------
    # One record per calendar day, keyed by ISO date, holding the transcript rollup
    # (what the day contained) and the owner's own check-in (what only he can report).
    # Work and personal are NOT split here: he works from home and home life is an
    # input to the work, so a record that separated them would be the same blindness
    # this feature exists to remove.

    def days(self) -> dict[str, Any]:
        return self._read(DAYS, {}) or {}

    def get_day(self, day: str) -> dict[str, Any]:
        return self.days().get(day, {})

    def put_day(self, day: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge into a day record. Never replaces: a rollup refresh must not wipe a
        check-in the owner already wrote, and vice versa."""
        with self.lock:
            all_days = self.days()
            rec = dict(all_days.get(day) or {})
            rec.update({k: v for k, v in patch.items() if v is not None})
            rec["date"] = day
            rec["updated"] = iso(utcnow())
            all_days[day] = rec
            self._write(DAYS, all_days)
            return rec

    def delete_snapshot(self, domain: str, kind: str) -> bool:
        with self.lock:
            current = {k: v.model_dump() for k, v in self.snapshots().items()}
            if current.pop(f"{domain}/{kind}", None) is None:
                return False
            self._write(SNAPSHOTS, current)
            return True

    # ---- meeting notes ------------------------------------------------------
    # The ingester's memory of which Notion pages it has already read.
    #
    # Deliberately NOT a Snapshot. A snapshot is a picture of current state that
    # the next fetch replaces; this is an append-only record of what has been
    # consumed, and losing it means re-filing every action item from every meeting
    # of the last week. Different lifetime, so a different file.

    def meetings(self) -> dict[str, Any]:
        """Shape: {"watermark": iso|None, "pages": [{id,title,at,filed}], ...}."""
        raw = self._read(MEETINGS, {}) or {}
        raw.setdefault("watermark", None)
        raw.setdefault("pages", [])
        raw.setdefault("last_ingest", None)
        raw.setdefault("last_summary", None)
        raw.setdefault("filed_total", 0)
        return raw

    def seen_pages(self) -> set[str]:
        return {str(p.get("id")) for p in self.meetings().get("pages", []) if p.get("id")}

    def put_meetings(self, patch: dict[str, Any], pages: list[dict[str, Any]] | None = None
                     ) -> dict[str, Any]:
        """Merge into the ledger, appending newly-consumed pages.

        Merge rather than replace: a manual `otto meetings ingest` running beside a
        scheduled one must not roll the watermark backwards or forget pages the
        other just consumed. The watermark only ever moves forward.
        """
        with self.lock:
            rec = self.meetings()
            for k, v in patch.items():
                if k in ("pages", "watermark"):
                    continue
                if v is not None:
                    rec[k] = v
            new_wm = patch.get("watermark")
            if new_wm and (not rec.get("watermark") or str(new_wm) > str(rec["watermark"])):
                rec["watermark"] = new_wm
            if pages:
                known = {str(p.get("id")) for p in rec["pages"] if p.get("id")}
                rec["pages"] = (rec["pages"]
                                + [p for p in pages if str(p.get("id")) not in known]
                                )[-config.MEETINGS_SEEN_LIMIT:]
            self._write(MEETINGS, rec)
            return rec

    # ---- feed ingest ledger -------------------------------------------------
    # What the daemon has already consumed from each feed directory.
    #
    # Deliberately NOT a Snapshot, for the same reason `meetings` is not: a
    # snapshot is a picture the next fetch replaces, while this is the ingester's
    # memory. Without it the tick loop would re-ingest an unchanged file every 15
    # seconds, which for an `advisory` source means re-proposing the same cards
    # forever. Different lifetime, so a different file.
    #
    # Per source: {digest, produced_at, ingested_at, ingest_count, filed_total,
    #              state: "ok"|"rejected", error}. `digest` is over the raw bytes,
    # so an unchanged file is skipped and a REJECTED file is complained about
    # exactly once instead of every tick.

    def feeds(self) -> dict[str, Any]:
        return self._read(FEEDS, {}) or {}

    def feed_state(self, name: str) -> dict[str, Any]:
        return (self.feeds().get(name) or {}) if name else {}

    def put_feed_state(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge one source's ingest record. Merge, not replace: a rejected drop
        must not erase the digest of the good one before it, or the next tick
        would re-ingest the last good file as if it were new."""
        with self.lock:
            all_feeds = self.feeds()
            rec = dict(all_feeds.get(name) or {})
            rec.update(patch)
            rec["source"] = name
            all_feeds[name] = rec
            self._write(FEEDS, all_feeds)
            return rec

    # ---- decisions (append-only) --------------------------------------------
    # What was decided and why. Append-only on purpose, so the methods here are
    # deliberately NOT the upsert/delete pair every other entity gets: there is
    # `add_decision` and `supersede_decision`, and no way to edit or remove one.
    # A decision you can quietly rewrite is a decision whose history is a lie,
    # and the reason for keeping it is precisely to be able to read back what you
    # thought at the time.

    def decisions(self) -> list[Decision]:
        out: list[Decision] = []
        for d in self._read(DECISIONS, []):
            try:
                out.append(Decision.model_validate(d))
            except ValueError:
                continue  # an older row must not break the log
        out.sort(key=lambda d: (d.decided, d.at), reverse=True)
        return out

    def get_decision(self, decision_id: str) -> Decision | None:
        for d in self.decisions():
            if d.id == decision_id or d.id.startswith(decision_id):
                return d
        return None

    def add_decision(self, decision: Decision) -> Decision:
        """Append. Never trimmed, unlike runs/notices/events.

        Those are logs of activity where the old entries stop mattering. This is
        the record of why the system is the way it is, and the entries get MORE
        valuable with age, so there is no history limit to age one out.
        """
        with self.lock:
            items = self._read(DECISIONS, [])
            if any(str(d.get("id")) == decision.id for d in items):
                raise ValueError(f"decision {decision.id} already exists")
            items.append(decision.model_dump())
            self._write(DECISIONS, items)
        return decision

    def supersede_decision(self, old_id: str, new: Decision) -> tuple[Decision, Decision]:
        """Record `new` and mark `old_id` superseded by it, in one write.

        One lock, one write, because a half-applied supersede leaves either two
        live decisions that contradict each other or a pointer to a decision that
        does not exist. Both are worse than the change failing.
        """
        with self.lock:
            items = self._read(DECISIONS, [])
            idx = next((i for i, d in enumerate(items)
                        if str(d.get("id")) == old_id
                        or str(d.get("id", "")).startswith(old_id)), None)
            if idx is None:
                raise ValueError(f"no decision {old_id}")
            old = Decision.model_validate(items[idx])
            if old.superseded_by:
                raise ValueError(
                    f"decision {old.id} is already superseded by {old.superseded_by}; "
                    "supersede that one instead"
                )
            new.supersedes = old.id
            old.superseded_by = new.id
            items[idx] = old.model_dump()
            items.append(new.model_dump())
            self._write(DECISIONS, items)
        return old, new

    # ---- writing ------------------------------------------------------------
    # Post ideas and drafts. Never trimmed: a dropped idea is what stops the same
    # idea being mined again, and a posted one is the voice sample for the next.

    def posts(self) -> list[Post]:
        out: list[Post] = []
        for d in self._read(POSTS, []):
            try:
                out.append(Post.model_validate(d))
            except ValueError:
                continue
        out.sort(key=lambda p: p.updated, reverse=True)
        return out

    def get_post(self, post_id: str) -> Post | None:
        if not post_id:
            return None
        for p in self.posts():
            if p.id == post_id or p.id.startswith(post_id):
                return p
        return None

    def upsert_post(self, post: Post) -> Post:
        with self.lock:
            items = self._read(POSTS, [])
            for i, d in enumerate(items):
                if str(d.get("id")) == post.id:
                    items[i] = post.model_dump()
                    break
            else:
                items.append(post.model_dump())
            self._write(POSTS, items)
        return post

    # ---- outreach -----------------------------------------------------------
    # The ledger of messages Otto sent, or was stopped from sending, to somebody
    # other than the owner. Trimmed far more generously than notices (500 vs 200): this is
    # the audit trail for the one capability that acts on other people, and "what did
    # Otto say to my team last month" must still be answerable.

    def outreach(self) -> list[Outreach]:
        out: list[Outreach] = []
        for o in self._read(OUTREACH, []):
            try:
                out.append(Outreach.model_validate(o))
            except ValueError:
                continue
        out.sort(key=lambda o: o.at, reverse=True)
        return out

    def get_outreach(self, oid: str) -> Outreach | None:
        for o in self.outreach():
            if o.id == oid or o.id.startswith(oid):
                return o
        return None

    def put_outreach(self, item: Outreach) -> Outreach:
        with self.lock:
            items = self.outreach()
            for i, existing in enumerate(items):
                if existing.id == item.id:
                    items[i] = item
                    break
            else:
                items.append(item)
            items.sort(key=lambda o: o.at, reverse=True)
            self._write(OUTREACH, [o.model_dump() for o in items[:500]])
        return item

    # ---- logistics proposals ---------------------------------------------------
    # Small and rewritten whole on every refresh: a handful of (card, pane) pairs
    # plus the ones the owner dismissed, trimmed so a dismissed pair from a session that
    # ended weeks ago does not live forever.

    def proposals(self) -> list[Proposal]:
        out: list[Proposal] = []
        for p in self._read(PROPOSALS, []):
            try:
                out.append(Proposal.model_validate(p))
            except ValueError:
                continue
        return out

    def save_proposals(self, items: list[Proposal]) -> None:
        self._write(PROPOSALS, [p.model_dump() for p in items[:200]])

    def get_proposal(self, pid: str) -> Proposal | None:
        for p in self.proposals():
            if p.id == pid or p.id.startswith(pid):
                return p
        return None

    def set_revisit_by(self, decision_id: str, when: str | None) -> Decision:
        """Set (or clear) ONLY the revisit_by date on a live decision.

        The one mutable field, and the exception needs justifying because
        everything else here is append-only on principle.

        `revisit_by` is not part of the decision. It is a review appointment ABOUT
        the decision: what was decided, why, and what would change it all stay
        untouched, and only when-to-recheck moves. Forcing a supersede to schedule
        a review would mean restating the entire entry to add a date, which is
        tedious enough that nobody would do it, and an unwatched revisit condition
        is exactly the failure `decisions.gaps()` reports. A rule that makes fixing
        the reported problem expensive is a rule that produces unfixed problems.

        Superseded decisions are refused: scheduling a rethink of thinking that has
        already been replaced is meaningless, and the live successor is the thing
        that deserves the date.
        """
        with self.lock:
            items = self._read(DECISIONS, [])
            idx = next((i for i, d in enumerate(items)
                        if str(d.get("id")) == decision_id
                        or str(d.get("id", "")).startswith(decision_id)), None)
            if idx is None:
                raise ValueError(f"no decision {decision_id}")
            d = Decision.model_validate(items[idx])
            if d.superseded_by:
                raise ValueError(
                    f"{d.id} was superseded by {d.superseded_by}; set the date on that "
                    "one instead"
                )
            if when and not d.revisit:
                raise ValueError(
                    f"{d.id} has no revisit condition, so a date would say to think "
                    "again without saying about what. Supersede it to add one."
                )
            d.revisit_by = str(when)[:10] if when else None
            items[idx] = d.model_dump()
            self._write(DECISIONS, items)
            return d

    # ---- sessions -----------------------------------------------------------
    # Live Claude Code sessions, keyed by session_id, written by the hook endpoint.
    # Held under `session_lock`, not `self.lock` (see __init__ for why).

    def sessions(self) -> list[Session]:
        out: list[Session] = []
        for s in self._read(SESSIONS, []):
            try:
                out.append(Session.model_validate(s))
            except ValueError:
                continue  # one bad row must not blind the whole view
        out.sort(key=lambda s: s.last_event, reverse=True)
        return out

    def get_session(self, session_id: str) -> Session | None:
        for s in self.sessions():
            if s.session_id == session_id or s.session_id.startswith(session_id):
                return s
        return None

    def save_sessions(self, items: list[Session]) -> None:
        items.sort(key=lambda s: s.last_event, reverse=True)
        self._write(SESSIONS, [s.model_dump() for s in items[:config.SESSION_LIMIT]])

    def put_session(self, session: Session) -> Session:
        with self.session_lock:
            items = self.sessions()
            for i, existing in enumerate(items):
                if existing.session_id == session.session_id:
                    items[i] = session
                    break
            else:
                items.append(session)
            self.save_sessions(items)
        return session

    # ---- known failures -----------------------------------------------------
    # Plain dicts, not a model: the shape is four strings and a nullable timestamp,
    # and known.py owns the validation at the one place a row is created. Kept out
    # of schedules.json and integrations.json on purpose; see known.py for why an
    # annotation on an Integration row would not survive the next probe.

    def known(self) -> list[dict[str, Any]]:
        return [i for i in self._read(KNOWN, []) if isinstance(i, dict)
                and i.get("kind") and i.get("name")]

    def save_known(self, items: list[dict[str, Any]]) -> None:
        self._write(KNOWN, items)

    def get_known(self, kind: str, name: str) -> dict[str, Any] | None:
        for i in self.known():
            if i["kind"] == kind and i["name"] == name:
                return i
        return None

    def put_known(self, item: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            items = [i for i in self.known()
                     if not (i["kind"] == item["kind"] and i["name"] == item["name"])]
            items.append(item)
            self.save_known(items)
        return item

    def delete_known(self, kind: str, name: str) -> bool:
        with self.lock:
            items = self.known()
            kept = [i for i in items if not (i["kind"] == kind and i["name"] == name)]
            if len(kept) == len(items):
                return False
            self.save_known(kept)
            return True

    # ---- integrations -------------------------------------------------------


    # ---- setup: first-run progress, see otto/setup.py ----------------------

    def setup(self) -> dict[str, Any]:
        data = self._read(SETUP, {})
        return data if isinstance(data, dict) else {}

    def put_setup(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge, never replace: completion, skips, and the restart flag are written
        by different requests and none of them should erase the others."""
        data = self.setup()
        for k, v in patch.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        self._write(SETUP, data)
        return data
    def integrations(self) -> list[Integration]:
        return [Integration.model_validate(i) for i in self._read(INTEGRATIONS, [])]

    def save_integrations(self, items: list[Integration]) -> None:
        self._write(INTEGRATIONS, [i.model_dump() for i in items])

    # ---- tasks --------------------------------------------------------------

    def tasks(self) -> list[Task]:
        out: list[Task] = []
        for t in self._read(TASKS, []):
            try:
                out.append(Task.model_validate(t))
            except ValueError:
                continue  # a row from an older schema must not break the board
        return out

    def save_tasks(self, items: list[Task]) -> None:
        self._write(TASKS, [t.model_dump() for t in items])

    def get_task(self, task_id: str) -> Task | None:
        for t in self.tasks():
            if t.id == task_id or t.id.startswith(task_id):
                return t
        return None

    def upsert_task(self, task: Task) -> Task:
        with self.lock:
            items = self.tasks()
            for i, existing in enumerate(items):
                if existing.id == task.id:
                    items[i] = task
                    break
            else:
                items.append(task)
            self.save_tasks(items)
        return task

    def delete_task(self, task_id: str) -> bool:
        with self.lock:
            items = self.tasks()
            remaining = [t for t in items if t.id != task_id]
            if len(remaining) == len(items):
                return False
            self.save_tasks(remaining)
            return True

    # ---- chat ---------------------------------------------------------------

    def chat(self) -> dict[str, Any]:
        """Thread shape: {"session_id": str|None, "messages": [...]}."""
        raw = self._read(CHAT, {}) or {}
        raw.setdefault("session_id", None)
        raw.setdefault("messages", [])
        return raw

    def save_chat(self, thread: dict[str, Any]) -> None:
        # Bound the thread on disk. Claude Code keeps the real conversation via
        # --resume; this file is only what the UI renders.
        msgs = thread.get("messages") or []
        thread["messages"] = msgs[-config.CHAT_HISTORY_LIMIT :]
        self._write(CHAT, thread)

    # ---- events (append-only) ----------------------------------------------

    def append_event(self, event: Event) -> None:
        """Append one event line. Never raises for an I/O failure.

        Every caller logs AFTER the write it describes has landed (persist first,
        tell second, the ordering rule notify.py also follows). On Windows the
        append can fail transiently with a sharing violation while a reader has
        the events file, and when that exception reached FastAPI it turned a
        landed board edit into a 500 with no did-it-land signal: the caller
        retried and wrote twice (DEBT: `otto triage set` intermittent 500,
        root-caused 2026-09-03). So the open is retried a few times, and a line
        that still cannot land is counted and dropped. An event is a note about
        a write, never the write; a hole in the log is the lesser failure.
        WriteDenied still raises: that is a programming error, not I/O.
        """
        if not config.is_daemon():
            raise WriteDenied("only the Otto daemon appends events")
        p = self._path(EVENTS)
        line = json.dumps(event.model_dump()) + "\n"
        last: OSError | None = None
        for attempt in range(4):
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with self.lock, p.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
                self._bump()
                return
            except OSError as e:
                last = e
                time.sleep(0.02 * (attempt + 1))
        self.log_failures += 1
        self.last_log_error = f"{type(last).__name__}: {last}"

    def log(self, message: str, level: str = "info", source: str = "otto", **data: Any) -> None:
        self.append_event(Event(level=level, source=source, message=message, data=data))

    def append_telemetry(self, records: list[dict[str, Any]]) -> int:
        """Append flattened telemetry records. Daemon-only, one open per batch.

        Not under `self.lock`: a telemetry POST arrives every few seconds from every
        live Claude Code session and shares no read-modify-write with anything.
        Appends of whole lines from one process interleave safely; the file lock
        below only serialises the writers inside this process.
        """
        if not config.is_daemon():
            raise WriteDenied("only the Otto daemon appends telemetry")
        if not records:
            return 0
        p = self._path(TELEMETRY)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        with self.telemetry_lock, p.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return len(records)

    def events(self, limit: int = 100) -> list[Event]:
        p = self._path(EVENTS)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[Event] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                out.append(Event.model_validate_json(line))
            except ValueError:
                continue
        out.reverse()
        return out

    def trim_events(self) -> None:
        """Keep events.jsonl bounded. Daemon-only, called on tick."""
        if not config.is_daemon():
            raise WriteDenied("only the Otto daemon trims events")
        p = self._path(EVENTS)
        if not p.exists():
            return
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= config.EVENT_HISTORY_LIMIT:
            return
        keep = lines[-config.EVENT_HISTORY_LIMIT :]
        _atomic_write(p, "\n".join(keep) + "\n")

    # ---- meta ---------------------------------------------------------------

    def mark_attempt(self, name: str, status: str, run_id: str | None = None) -> Schedule | None:
        """Record that a run happened and FAILED, without touching last_run.

        `stamp()` means "this succeeded", and it resets the staleness clock. Calling
        it on a failed run makes a schedule that fails every single time read as
        perfectly fresh forever, which is the exact blindness Otto exists to remove.
        """
        with self.lock:
            sched = self.get_schedule(name)
            if sched is None:
                return None
            sched.last_status = status
            if run_id:
                sched.last_run_id = run_id
            return self.upsert_schedule(sched)

    def stamp(
        self,
        name: str,
        status: str = "ok",
        run_id: str | None = None,
        at: str | None = None,
    ) -> Schedule | None:
        """Record a successful run of a schedule. Replaces heartbeat.py stamp.

        `at` backdates the stamp, which migration needs so imported history keeps
        its real age instead of all looking like it happened just now.
        """
        with self.lock:
            sched = self.get_schedule(name)
            if sched is None:
                return None
            sched.last_run = at or iso(utcnow())
            sched.last_status = status
            if run_id:
                sched.last_run_id = run_id
            return self.upsert_schedule(sched)
