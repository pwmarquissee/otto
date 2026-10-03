"""Duplicate cards, and merging them.

THE PROBLEM. Every filer on the board (findings, feeds, meeting notes, the API)
deduped on a fingerprint of the normalised title. That catches a title repeated
verbatim and nothing else, and the filers that most often see the same event are
language models that phrase it differently every time. Heartbeat filed fourteen
cards about one expired AWS SSO session in a fortnight: "AWS SSO session expired",
"Refresh AWS SSO session (work)", "AWS SSO re-authentication needed",
"Restart Claude Code after AWS SSO refresh". Same session, same fix, fourteen
things for the owner to click through and close. DEBT.md records four separate entries
about this.

THREE TIERS OF "SAME", strictest first. Two open cards in the same domain are
duplicates when:

  1. their fingerprints are equal (the old rule, kept: a title repeated verbatim
     with only a timestamp changed);
  2. they match the same TOPIC. A topic is a hand-written rule in
     `config.DEDUPE_TOPICS` for a recurring finding whose phrasings share almost
     no words ("re-auth" vs "expired" vs "refresh"). Topics are code, not data a
     feed can write, because a wrong topic rule silently folds real work into
     the wrong card;
  3. their titles are SIMILAR: after stopwords, light stemming and a synonym
     table, they share at least `DEDUPE_MIN_SHARED` content tokens and the
     Jaccard overlap is at least `DEDUPE_SIMILARITY`. Tier 3 is skipped when BOTH
     cards were typed by the owner: "follow up with the vendor" and "follow up
     with the vendor about the GPU" are two commitments, and the owner knows which.

WHAT MERGING MEANS. One card stays (the KEEPER), the rest are closed with
`duplicate_of` pointing at it. The keeper is the card with the most state: a
running or queued card first (it has a run attached and closing it would orphan
that), then an approved plan, a plan, an assessment, a card the owner typed, then the
oldest. The keeper absorbs what the duplicates knew: seen_count adds up, the
earliest due date wins, the highest priority wins, tags union, and an assessment
or plan the keeper lacked is carried over. Each duplicate's title and id is
appended to the keeper's detail so the trail is readable; the duplicate's own
detail stays on the duplicate, which is closed rather than deleted. Undo is
`otto task mv <id> backlog`.

A duplicate is NOT a completion. The board hides them from every column (the
count travels as `duplicates`, same as `faded`), `otto task ls` hides them
unless asked, and nothing that counts finished work should count them.

WHERE IT RUNS. At filing time, in every path that creates a card, so a repeat
bumps the existing card instead of landing. And as a sweep on the tick, over the
whole open board, so cards that were already there get folded too and a card
edited into a duplicate does not stay one. The sweep is gated on a digest of the
open cards, so an idle tick costs one list comprehension.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from . import config
from .models import Task, iso, utcnow
from .store import Store

OPEN_STATUSES = frozenset({"backlog", "queued", "running", "needs-you", "blocked"})

# Statuses that mean a run is attached. Never closed as a duplicate: the run would
# keep going with no card to report into.
_PROTECTED = frozenset({"running", "queued"})

_STATUS_RANK = {"running": 0, "queued": 1, "needs-you": 2, "blocked": 3, "backlog": 4}

_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


# ---------------------------------------------------------------------------
# tier 1: the fingerprint
# ---------------------------------------------------------------------------

_TIMEY = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:t\d{2}:\d{2}(?::\d{2})?z?)?\b"   # ISO date / datetime
    r"|\b\d{1,2}:\d{2}(?:z|\s*utc)?\b"                          # 04:07Z
    r"|\b\d{1,2}/\d{1,2}\b"                                     # 8/10, 9/30
    r"|\(?~?\b\d+\s*(?:min|mins|minutes|h|hr|hrs|hours|d|days)\b\s*(?:left|ago)?\)?"
)


def fingerprint(title: str, domain: str) -> str:
    """Normalised key for "is this the same finding as last time".

    Punctuation becomes a SPACE, not nothing. Deleting it would fold "Co-op" to
    "coop" while "Co op" stays "co op", so two phrasings of the same finding
    would get different keys and both land on the board. Space-then-collapse makes
    hyphens, apostrophes, and stray commas all normalise the same way.

    Timestamps, dates and durations are not identity. "Refresh SSO before
    2026-09-05T04:07Z" and "...before 2026-09-09T09:40Z" are the same finding
    about the same session, and heartbeat filed five of them in one week.
    """
    norm = (title or "").lower()
    norm = _TIMEY.sub(" ", norm)
    norm = re.sub(r"[^a-z0-9]+", " ", norm)
    return f"{domain}:{' '.join(norm.split())}"


# ---------------------------------------------------------------------------
# tier 2: topics
# ---------------------------------------------------------------------------

def topic(title: str) -> str | None:
    """The first topic rule in config.DEDUPE_TOPICS the title satisfies, or None."""
    text = (title or "").lower()
    for rule in config.DEDUPE_TOPICS:
        if all(re.search(p, text) for p in rule["all"]) and \
                not any(re.search(p, text) for p in rule.get("none", ())):
            return rule["name"]
    return None


# ---------------------------------------------------------------------------
# tier 3: similar titles
# ---------------------------------------------------------------------------

# Function words and the filler a filer adds around a subject. Content words stay,
# including verbs like "confirm" and "remove": "confirm X" and "remove X" are two
# different asks about one subject, and the overlap threshold decides them.
_STOP = frozenset("""
a an the and or but if then than to for of in on at by as is are was were be been
being with without from into onto over under after before until while when where
which who whom whose that this these those it its he she they them their there here
has have had do does did not no nor so too very can could should would may might
must will shall also still yet now just only again about via per between among
needs need needed up out off
""".split())

# Different words for the same thing, folded BEFORE stemming. Kept short and
# specific: a wide table makes unrelated cards look alike.
_SYNONYMS = {
    "expired": "expire", "expires": "expire", "expiry": "expire", "expiring": "expire",
    "expiration": "expire",
    "reauth": "reauth", "reauthenticate": "reauth", "reauthenticated": "reauth",
    "reauthentication": "reauth", "relogin": "reauth", "reauthorize": "reauth",
    "renew": "refresh", "renewal": "refresh", "renewed": "refresh", "refreshed": "refresh",
    "dupe": "duplicate", "dupes": "duplicate", "duplicated": "duplicate",
    "machine": "device", "workstation": "device", "host": "device", "pc": "device",
    "box": "device", "boxes": "device",
    "cred": "credential", "creds": "credential", "credentials": "credential",
    "secret": "credential", "secrets": "credential", "token": "credential",
    "tokens": "credential",
    "fp": "falsepositive",
}


def _stem(word: str) -> str:
    """Crude suffix stripping. Inside a set overlap, over-stemming costs nothing and
    under-stemming costs a match, so this errs towards stripping."""
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 6 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 5 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 5 and word.endswith("es"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(title: str) -> frozenset[str]:
    """Content tokens of a title, for the similarity tier."""
    text = (title or "").lower()
    text = _TIMEY.sub(" ", text)
    # "re-auth", "re auth", "reauth" are one word, and the punctuation split below
    # would make the first two of them two tokens, one of which ("re") is noise.
    text = re.sub(r"\bre[\s-]?(auth\w*|login|authenticat\w*|authoriz\w*)", r"re\1", text)
    text = re.sub(r"\bfalse[\s-]?positives?\b", "falsepositive", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    out = set()
    for w in text.split():
        if w in _STOP or len(w) < 2:
            continue
        w = _SYNONYMS.get(w, w)
        w = _stem(w)
        w = _SYNONYMS.get(w, w)
        out.add(w)
    return frozenset(out)


def similarity(a: str, b: str) -> tuple[float, int]:
    """(Jaccard overlap, shared token count) of two titles."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0, 0
    shared = ta & tb
    return len(shared) / len(ta | tb), len(shared)


# ---------------------------------------------------------------------------
# putting the tiers together
# ---------------------------------------------------------------------------

def why_same(a: Task, b: Task) -> str | None:
    """A one-line reason a and b are the same card, or None if they are not."""
    if a.id == b.id or a.domain != b.domain:
        return None
    fa = a.fingerprint or fingerprint(a.title, a.domain)
    fb = b.fingerprint or fingerprint(b.title, b.domain)
    if fa == fb:
        return "same title"
    ta, tb = topic(a.title), topic(b.title)
    if ta is not None and ta == tb:
        return f"topic {ta}"
    if ta != tb:
        # A topic is decisive both ways. A card its rule EXCLUDED ("AWS SSO session
        # renewal alerting" is work about the session, not the expired session) must
        # not be pulled back in by sharing most of its words with cards the rule
        # matched. Without this the first live dry run made exactly that card the
        # keeper for twelve expiry cards.
        return None
    if a.source == "manual" and b.source == "manual":
        return None
    ta_, tb_ = tokens(a.title), tokens(b.title)
    # A number that survived the timestamp strip is an identifier: a device id, a
    # CPU model, an HTTP status. Two titles whose identifiers have nothing in common
    # are about two things, however alike the words around them. "Archive RMM
    # device 36" and "... device 37" are 60% alike and are two devices.
    ida = {w for w in ta_ if any(ch.isdigit() for ch in w)}
    idb = {w for w in tb_ if any(ch.isdigit() for ch in w)}
    if ida and idb and ida.isdisjoint(idb):
        return None
    score, shared = similarity(a.title, b.title)
    if shared >= config.DEDUPE_MIN_SHARED and score >= config.DEDUPE_SIMILARITY:
        return f"titles {score:.0%} alike"
    return None


def is_open(t: Task) -> bool:
    return t.status in OPEN_STATUSES and not t.duplicate_of


# A card tagged this never takes part in dedupe: not as a keeper, not as a
# duplicate, not as the card a filing bumps. `otto task add --no-dedupe` sets it.
# A filing-time skip alone was not enough: the card landed, the digest changed,
# and the next tick's sweep folded it anyway.
DISTINCT_TAG = "distinct"


def participates(t: Task) -> bool:
    return is_open(t) and DISTINCT_TAG not in (t.tags or [])


def _rank(t: Task) -> tuple:
    """Lower sorts first. The card with the most state is the one that stays."""
    return (
        _STATUS_RANK.get(t.status, 9),
        0 if t.plan_approved else 1,
        0 if t.plan else 1,
        0 if t.assessed else 1,
        0 if t.source == "manual" else 1,
        t.created or "",
    )


def find_match(task: Task, tasks: Iterable[Task]) -> tuple[Task, str] | None:
    """The open card `task` duplicates, with the reason, or None.

    Used at filing time: the caller bumps the match instead of storing `task`.
    When several cards match, the one that would be kept by a sweep is returned,
    so filing and sweeping agree about where a repeat lands.
    """
    hits = []
    for other in tasks:
        if not participates(other):
            continue
        why = why_same(task, other)
        if why:
            hits.append((other, why))
    if not hits:
        return None
    hits.sort(key=lambda h: _rank(h[0]))
    return hits[0]


def absorb(keeper: Task, dupe: Task, when: str | None = None, stored: bool = True) -> None:
    """Fold what `dupe` knew into `keeper`, in place. Does not touch `dupe`.

    Shared by the filing paths (`stored=False`: dupe is a card that was never
    written, so its id means nothing) and the sweep (dupe is a stored card about
    to be closed), so a repeat is treated the same way whichever door it came
    through.
    """
    stamp = (when or iso(utcnow()))[:16].replace("T", " ")
    keeper.seen_count += max(1, dupe.seen_count)
    if dupe.due and (not keeper.due or dupe.due < keeper.due):
        keeper.due = dupe.due
    if _PRIORITY_RANK.get(dupe.priority, 0) > _PRIORITY_RANK.get(keeper.priority, 0):
        keeper.priority = dupe.priority
    # A repeat is evidence, so let it escalate once. Same rule findings used.
    if keeper.seen_count >= 3 and keeper.priority == "normal":
        keeper.priority = "high"
    for tag in dupe.tags:
        if tag not in keeper.tags:
            keeper.tags.append(tag)
    if dupe.origin_run_id:
        keeper.origin_run_id = dupe.origin_run_id
    if not keeper.notion_page_id and dupe.notion_page_id:
        keeper.notion_page_id = dupe.notion_page_id
    if not keeper.task_ref and dupe.task_ref:
        keeper.task_ref = dupe.task_ref
    # State the keeper lacked and the duplicate had. Never the other way: the
    # keeper was chosen because it has more, and an assessment is not averaged.
    if not keeper.assessed and dupe.assessed:
        keeper.owner, keeper.tier = dupe.owner, dupe.tier
        keeper.readiness, keeper.assessed = dupe.readiness, dupe.assessed
        keeper.assessed_note = dupe.assessed_note
    elif not keeper.tier and dupe.tier:
        keeper.tier = dupe.tier
    if not keeper.plan and dupe.plan:
        keeper.plan, keeper.plan_approved = dupe.plan, dupe.plan_approved
        keeper.run_mode = dupe.run_mode
    if not keeper.detail and dupe.detail:
        keeper.detail = dupe.detail
    # The trail. One line per absorbed card, dated, naming the card so its own
    # detail is a lookup away. The duplicate's detail is NOT copied: fourteen
    # copies of the same heartbeat paragraph is how detail stops being read.
    who = dupe.origin or dupe.source
    line = (f"--- merged {stamp} UTC: '{dupe.title[:120]}'"
            + (f" ({dupe.id[:8]}, {who}, {(dupe.created or '')[:10]})" if stored
               else f" (refiled by {who})")
            + " ---")
    keeper.detail = "\n\n".join(x for x in ((keeper.detail or "").rstrip(), line) if x)
    keeper.touch()


def close_as_duplicate(dupe: Task, keeper: Task, when: str | None = None) -> None:
    """Mark `dupe` closed into `keeper`. Nothing is deleted."""
    stamp = (when or iso(utcnow()))[:16].replace("T", " ")
    dupe.status = "done"
    dupe.duplicate_of = keeper.id
    if "duplicate" not in dupe.tags:
        dupe.tags.append("duplicate")
    line = f"--- duplicate of {keeper.id[:8]} '{keeper.title[:120]}', merged {stamp} UTC ---"
    dupe.detail = "\n\n".join(x for x in ((dupe.detail or "").rstrip(), line) if x)
    dupe.touch()


def clusters(tasks: list[Task]) -> list[list[tuple[Task, str]]]:
    """Groups of open cards that are the same card. Each member carries the reason
    it joined. Union-find over `why_same`, which is not transitive: a 60% match to
    A and a 60% match to B can land C with both. Accepted; the threshold is high
    enough that chains are short, and the dry run shows every reason."""
    open_tasks = [t for t in tasks if participates(t)]
    parent = {t.id: t.id for t in open_tasks}
    reason: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(open_tasks):
        for b in open_tasks[i + 1:]:
            why = why_same(a, b)
            if why:
                ra, rb = find(a.id), find(b.id)
                if ra != rb:
                    parent[rb] = ra
                reason.setdefault(a.id, why)
                reason.setdefault(b.id, why)

    groups: dict[str, list[Task]] = {}
    for t in open_tasks:
        groups.setdefault(find(t.id), []).append(t)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_rank)
        out.append([(t, reason.get(t.id, "")) for t in members])
    out.sort(key=lambda g: -len(g))
    return out


def plan(tasks: list[Task]) -> list[dict]:
    """What a sweep would do, without doing it. One row per card that would close."""
    rows = []
    for group in clusters(tasks):
        keeper = group[0][0]
        for t, why in group[1:]:
            if t.status in _PROTECTED:
                continue  # it has a run; it stays, and the keeper does too
            rows.append({
                "keeper_id": keeper.id, "keeper_title": keeper.title,
                "keeper_status": keeper.status,
                "dupe_id": t.id, "dupe_title": t.title, "dupe_status": t.status,
                "dupe_origin": t.origin or t.source, "dupe_created": t.created,
                "why": why,
            })
    return rows


def sweep(store: Store, dry_run: bool = False) -> list[dict]:
    """Fold every duplicate on the open board into its keeper. One lock, one write.

    Returns the rows `plan()` would have, so the caller can print or log them.
    """
    with store.lock:
        items = store.tasks()
        rows = plan(items)
        if dry_run or not rows:
            return rows
        by_id = {t.id: t for t in items}
        now = iso(utcnow())
        for row in rows:
            keeper, dupe = by_id[row["keeper_id"]], by_id[row["dupe_id"]]
            absorb(keeper, dupe, now)
            close_as_duplicate(dupe, keeper, now)
        store.save_tasks(items)
    return rows


# The tick gate. A sweep is O(n^2) over open cards, cheap at a few hundred but
# not free at the tick rate, and the board only changes when something files or
# the owner moves a card. Hash what matters and skip when nothing did.
_last_digest: str | None = None


def digest(tasks: Iterable[Task]) -> str:
    h = hashlib.sha256()
    for t in tasks:
        if is_open(t):
            h.update(f"{t.id}:{t.updated}\n".encode())
    return h.hexdigest()


def tick(store: Store) -> list[str]:
    """Sweep when the open board changed since the last look. Notes for the log."""
    global _last_digest
    current = digest(store.tasks())
    if current == _last_digest:
        return []
    rows = sweep(store)
    # Digest AFTER the sweep, so the sweep's own writes do not trigger another.
    _last_digest = digest(store.tasks())
    notes = [f"merged '{r['dupe_title'][:44]}' into {r['keeper_id'][:6]} "
             f"'{r['keeper_title'][:44]}' ({r['why']})" for r in rows]
    if len(rows) > 1:
        notes.append(f"dedupe folded {len(rows)} card(s) into "
                     f"{len({r['keeper_id'] for r in rows})} keeper(s)")
    return notes
