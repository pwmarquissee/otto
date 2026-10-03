"""Duplicate cards fold into one, whichever door they came through.

Heartbeat filed fourteen cards about one expired AWS SSO session in September 2026,
every one worded differently, and the exact-title fingerprint caught none of them.
These tests hold the three tiers of "same card" (fingerprint, topic, similar title),
the rules about who stays and who closes, and the fact that a closed duplicate is
not a completion.
"""

from __future__ import annotations

import pytest

from otto import board, config, dedupe, findings
from otto.models import Task

# Task.owner for work the human does. "otto" is the other value.
HUMAN = "owner"


def task(title: str, **kw) -> Task:
    kw.setdefault("source", "agent")
    kw.setdefault("origin", "heartbeat")
    t = Task(id=kw.pop("id", None) or ("t" + str(abs(hash(title)))).ljust(32, "0")[:32],
             title=title, **kw)
    t.fingerprint = dedupe.fingerprint(t.title, t.domain)
    return t


# ---- tier 1: fingerprint ---------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("Refresh AWS SSO before 2026-09-05T04:07:16Z", "Refresh AWS SSO before 2026-09-09T09:40Z"),
    ("AWS SSO auth near expiry (60min left)", "AWS SSO auth near expiry (3h left)"),
    ("GPU box running since 8/10", "GPU box running since 9/30"),
])
def test_time_shaped_numbers_are_not_identity(a, b):
    assert dedupe.fingerprint(a, "work") == dedupe.fingerprint(b, "work")


def test_findings_fingerprint_is_the_same_function():
    """Callers and older tests know it as findings.fingerprint."""
    assert findings.fingerprint is dedupe.fingerprint


# ---- tier 2: topics -----------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "AWS SSO session expired",
    "Refresh AWS SSO session (corp)",
    "AWS SSO re-authentication needed",
    "Re-auth AWS SSO (corp session expired 2h ago)",
    "AWS SSO expired, broke MCP + Anthropic loaders",
    "Restart Claude Code after AWS SSO refresh",
    "AWS SSO re-auth: ~1hr validity left",
])
def test_every_phrasing_of_the_expired_session_is_one_topic(title):
    assert dedupe.topic(title) == "aws-sso-session"


@pytest.mark.parametrize("title", [
    "AWS SSO session renewal alerting",                       # work about the session
    "Raise the corp AWS SSO session duration above 1 hour",
    "AWS SSO cache has 11 tokens, 10 long expired, no cleanup",
    "Five open AWS SSO cards name a profile that is not configured",
    "AWSReadOnlyAccess SSO role cannot read S3 bucket security config",
    "Legacy IdP /api/v1/authn is enabled org-wide and mints AWS SSO assertions daily",
    "IdP sweep can skip silently when the admin SSO expires",  # not AWS
])
def test_cards_about_the_session_are_not_the_expired_session(title):
    assert dedupe.topic(title) != "aws-sso-session"


def test_a_topic_mismatch_vetoes_the_fuzzy_tier():
    """The first live dry run folded 'AWS SSO session renewal alerting' into the
    expiry cluster on word overlap, then made it the keeper. A card a topic rule
    excluded must not be pulled back in by sharing words with cards it matched."""
    excluded = task("AWS SSO session renewal alerting")
    matched = task("Refresh AWS SSO session (corp)")
    assert dedupe.similarity(excluded.title, matched.title)[0] >= config.DEDUPE_SIMILARITY
    assert dedupe.why_same(excluded, matched) is None


# ---- tier 3: similar titles ---------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("Grant Assets:read to the EDR API client",
     "EDR API client missing Assets:read scope"),
    ("Add engine-upgrade distributable for non-launcher machines",
     "Package engine-upgrade distributable for non-launcher boxes"),
    ("Assign an RMM device to the concept artist who hit the UE crash",
     "Validate old map + assign RMM device for concept artist's UE crash"),
    ("Review priorities.md, 17 days since last review",
     "Review priorities.md, last reviewed 2026-08-31"),
    # Shares an identifier (24/7), so the differing "$820" does not split them.
    ("GPU workstation gpu-ws-alpha running 24/7 for 28 days, ~$820/mo reclaim",
     "GPU workstation gpu-ws-alpha running 24/7 since 8/10"),
])
def test_reworded_titles_about_the_same_work_match(a, b):
    assert dedupe.why_same(task(a), task(b)) is not None


def test_an_operator_topic_rule_folds_phrasings_the_fuzzy_tier_would_not(monkeypatch):
    """Topics are config. A recurring finding whose phrasings share almost no words
    gets its own rule, and the rule is what makes them one card. Only one side here
    carries an identifier (401); that is not a disagreement when the topic agrees."""
    monkeypatch.setattr(config, "DEDUPE_TOPICS", [{
        "name": "vendor-a-mcp-token",
        "all": [r"\bvendor-a\b", r"\bmcp\b", r"token"],
        "none": [r"rotate", r"renew"],
    }])
    a = task("vendor-a MCP token rejected with 401")
    b = task("Fix the vendor-a MCP token")
    assert dedupe.topic(a.title) == dedupe.topic(b.title) == "vendor-a-mcp-token"
    assert dedupe.why_same(a, b) is not None
    # Work ABOUT the token is excluded by the rule's `none` list, and the exclusion
    # holds even where the titles overlap.
    assert dedupe.topic("Rotate the vendor-a MCP token") is None
    assert dedupe.why_same(a, task("Rotate the vendor-a MCP token quarterly")) is None


@pytest.mark.parametrize("a, b", [
    ("Add NGSIEM scope to the EDR API client",
     "EDR API client missing Assets:read scope"),
    ("Archive RMM device 36", "Archive RMM device 37"),
    ("Renew the Houdini license", "Renew the Nuke license"),
    ("Replace the artist's 14900K", "Replace the animator's 13900K"),
])
def test_different_work_with_shared_vocabulary_stays_separate(a, b):
    assert dedupe.why_same(task(a), task(b)) is None


def test_domains_never_merge():
    a = task("Renew the passport", domain="personal")
    b = task("Renew the passport", domain="work")
    assert dedupe.why_same(a, b) is None


def test_two_cards_the_owner_typed_merge_only_on_exact_title():
    """'Follow up with the vendor' and 'Follow up with the vendor about the GPU' are
    two commitments. The owner knows which. An agent rewording one of theirs is still
    a repeat."""
    a = task("Follow up with the vendor about the GPU order", source="manual", origin=None)
    b = task("Follow up with the vendor about the GPU order and the RMA", source="manual", origin=None)
    assert dedupe.similarity(a.title, b.title)[0] >= config.DEDUPE_SIMILARITY
    assert dedupe.why_same(a, b) is None
    c = task("Follow up with the vendor about the GPU order and the RMA", source="agent")
    assert dedupe.why_same(a, c) is not None
    same = task("Follow up with the vendor about the GPU order!", source="manual", origin=None)
    assert dedupe.why_same(a, same) == "same title"


# ---- who stays ----------------------------------------------------------------------

def test_the_keeper_is_the_card_with_the_most_state():
    older = task("AWS SSO session expired", id="a" * 32, created="2026-09-01T00:00:00Z")
    assessed = task("Refresh AWS SSO session (corp)", id="b" * 32,
                    created="2026-09-10T00:00:00Z", assessed="2026-09-11T00:00:00Z",
                    owner=HUMAN, tier="tier-0-autonomous")
    planned = task("AWS SSO re-authentication needed", id="c" * 32,
                   created="2026-09-20T00:00:00Z", plan="run aws sso login")
    rows = dedupe.plan([older, assessed, planned])
    assert {r["dupe_id"] for r in rows} == {older.id, assessed.id}
    assert all(r["keeper_id"] == planned.id for r in rows)


def test_oldest_wins_when_nothing_else_separates_them():
    a = task("AWS SSO session expired", id="a" * 32, created="2026-09-01T00:00:00Z")
    b = task("Refresh AWS SSO session (corp)", id="b" * 32, created="2026-09-10T00:00:00Z")
    rows = dedupe.plan([b, a])
    assert len(rows) == 1 and rows[0]["keeper_id"] == a.id and rows[0]["dupe_id"] == b.id


def test_a_card_with_a_run_attached_is_never_closed():
    running = task("AWS SSO session expired", id="a" * 32, status="running",
                   created="2026-09-20T00:00:00Z")
    queued = task("Refresh AWS SSO session (corp)", id="b" * 32, status="queued",
                  created="2026-09-21T00:00:00Z")
    stale = task("AWS SSO re-authentication needed", id="c" * 32,
                 created="2026-09-01T00:00:00Z")
    rows = dedupe.plan([stale, queued, running])
    assert [r["dupe_id"] for r in rows] == [stale.id]
    assert rows[0]["keeper_id"] == running.id


def test_a_distinct_card_never_takes_part():
    """`--no-dedupe` has to survive the sweep, or it means nothing."""
    a = task("AWS SSO session expired", id="a" * 32, created="2026-09-01T00:00:00Z")
    kept_apart = task("Refresh AWS SSO session (corp)", id="b" * 32,
                      tags=[dedupe.DISTINCT_TAG], created="2026-09-02T00:00:00Z")
    c = task("AWS SSO re-authentication needed", id="c" * 32, created="2026-09-03T00:00:00Z")
    rows = dedupe.plan([a, kept_apart, c])
    assert [(r["keeper_id"], r["dupe_id"]) for r in rows] == [(a.id, c.id)]
    hit = dedupe.find_match(task("Re-auth AWS SSO"), [kept_apart])
    assert hit is None


# ---- what merging carries -----------------------------------------------------------

def test_absorb_carries_the_duplicates_evidence_into_the_keeper():
    keeper = task("AWS SSO session expired", id="a" * 32, seen_count=1, priority="normal",
                  tags=["from:heartbeat"])
    dupe = task("Refresh AWS SSO session (corp)", id="b" * 32, seen_count=2,
                priority="high", due="2026-10-03", tags=["from:daily"],
                assessed="2026-09-11T00:00:00Z", owner=HUMAN, tier="tier-0-autonomous",
                assessed_note="one aws sso login", detail="the session died at 03:00")
    dedupe.absorb(keeper, dupe, when="2026-10-01T09:00:00Z")
    assert keeper.seen_count == 3
    assert keeper.priority == "high"
    assert keeper.due == "2026-10-03"
    assert set(keeper.tags) == {"from:heartbeat", "from:daily"}
    assert (keeper.owner, keeper.tier, keeper.assessed_note) == \
        (HUMAN, "tier-0-autonomous", "one aws sso login")
    # The keeper had no detail, so it takes the duplicate's, then the trail line.
    assert keeper.detail.startswith("the session died at 03:00")
    assert "merged 2026-10-01 09:00 UTC: 'Refresh AWS SSO session (corp)'" in keeper.detail
    assert "bbbbbbbb" in keeper.detail


def test_absorb_does_not_copy_detail_the_keeper_already_has():
    """Fourteen copies of the same heartbeat paragraph is how detail stops being read.
    The duplicate's detail stays on the duplicate; the keeper gets one line."""
    keeper = task("AWS SSO session expired", id="a" * 32, detail="keeper's own evidence")
    dupe = task("Refresh AWS SSO session (corp)", id="b" * 32, detail="a long paragraph")
    dedupe.absorb(keeper, dupe)
    assert "a long paragraph" not in keeper.detail
    assert keeper.detail.startswith("keeper's own evidence")


def test_a_repeat_escalates_once():
    keeper = task("AWS SSO session expired", id="a" * 32, seen_count=2, priority="normal")
    dedupe.absorb(keeper, task("Refresh AWS SSO session (corp)"), stored=False)
    assert (keeper.seen_count, keeper.priority) == (3, "high")


def test_closing_a_duplicate_keeps_it_and_points_at_the_keeper():
    keeper = task("AWS SSO session expired", id="a" * 32)
    dupe = task("Refresh AWS SSO session (corp)", id="b" * 32, detail="its own detail")
    dedupe.close_as_duplicate(dupe, keeper, when="2026-10-01T09:00:00Z")
    assert dupe.status == "done"
    assert dupe.duplicate_of == keeper.id
    assert "duplicate" in dupe.tags
    assert dupe.detail.startswith("its own detail")
    assert "duplicate of aaaaaaaa" in dupe.detail


# ---- the sweep, through the store -----------------------------------------------------

def test_sweep_folds_the_board_in_one_write_and_dry_run_writes_nothing(store):
    cards = [
        task("AWS SSO session expired", id="a" * 32, created="2026-09-01T00:00:00Z"),
        task("Refresh AWS SSO session (corp)", id="b" * 32, created="2026-09-02T00:00:00Z"),
        task("AWS SSO re-auth: ~1hr validity left", id="c" * 32, created="2026-09-03T00:00:00Z"),
        task("Renew the Houdini license", id="d" * 32),
    ]
    store.save_tasks(cards)

    rows = dedupe.sweep(store, dry_run=True)
    assert len(rows) == 2
    assert all(t.status == "backlog" for t in store.tasks())

    rows = dedupe.sweep(store)
    by_id = {t.id: t for t in store.tasks()}
    assert by_id["a" * 32].status == "backlog"
    assert by_id["a" * 32].seen_count == 3
    assert by_id["b" * 32].duplicate_of == "a" * 32
    assert by_id["c" * 32].duplicate_of == "a" * 32
    assert by_id["d" * 32].status == "backlog" and by_id["d" * 32].duplicate_of is None
    # Idempotent: a second sweep finds nothing, because closed duplicates are not open.
    assert dedupe.sweep(store) == []


def test_tick_sweeps_only_when_the_open_board_changed(store, monkeypatch):
    store.save_tasks([task("AWS SSO session expired", id="a" * 32)])
    monkeypatch.setattr(dedupe, "_last_digest", None)
    calls = []
    real = dedupe.sweep

    def counting(s, dry_run=False):
        calls.append(1)
        return real(s, dry_run)

    monkeypatch.setattr(dedupe, "sweep", counting)
    dedupe.tick(store)
    dedupe.tick(store)
    assert len(calls) == 1
    store.upsert_task(task("Refresh AWS SSO session (corp)", id="b" * 32))
    notes = dedupe.tick(store)
    assert len(calls) == 2
    assert notes and "merged" in notes[0]


# ---- a filed repeat lands on the existing card ----------------------------------------

def test_a_reworded_finding_bumps_the_existing_card(store, monkeypatch):
    monkeypatch.setattr(config, "FINDINGS_MAX_PER_DAY", 100)
    store.save_tasks([task("AWS SSO session expired", id="a" * 32)])
    notes = findings.file_tasks(
        store, [{"title": "Re-authenticate expired corp AWS SSO session"}], "daily")
    assert len(store.tasks()) == 1
    assert store.tasks()[0].seen_count == 2
    assert "refiled" in notes[0] and "topic aws-sso-session" in notes[0]


def test_find_match_returns_the_card_a_sweep_would_keep():
    older = task("AWS SSO session expired", id="a" * 32, created="2026-09-01T00:00:00Z")
    planned = task("Refresh AWS SSO session (corp)", id="b" * 32,
                   created="2026-09-10T00:00:00Z", plan="aws sso login")
    hit = dedupe.find_match(task("AWS SSO re-authentication needed"), [older, planned])
    assert hit is not None and hit[0].id == planned.id


# ---- a duplicate is not a completion ---------------------------------------------------

def test_the_board_hides_duplicates_and_says_so(store, monkeypatch):
    monkeypatch.setattr(board, "derived_cards", lambda s: [])
    keeper = task("AWS SSO session expired", id="a" * 32)
    dupe = task("Refresh AWS SSO session (corp)", id="b" * 32)
    dedupe.close_as_duplicate(dupe, keeper)
    done = task("Renew the Houdini license", id="d" * 32, status="done")
    store.save_tasks([keeper, dupe, done])
    b = board.build(store)
    ids = {c["id"] for col in b["columns"] for c in col["cards"]}
    assert keeper.id in ids and done.id in ids and dupe.id not in ids
    assert b["duplicates"] == 1
    done_col = next(c for c in b["columns"] if c["key"] == "done")
    assert done_col["count"] == 1 and done_col["hidden"] == 0
