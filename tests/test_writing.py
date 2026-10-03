"""writing.py: post ideas mined from the week, drafts in the owner's voice, and the gate.

The load-bearing assertions are about the gate and about provenance. `scan()` has
to catch every class of thing the org rule says must not leave the company, and
it must never be the thing that clears a post: `posted` is set only by the owner's
own call. On the material side, Otto's own sessions must not be mistaken for a
week the owner lived, because a transcript of `-p` runs looks exactly like a typed one.

The word lists scan() consults (codenames, vendors, family, employer) are config
with empty defaults, so every scan test sets them to synthetic values first.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from otto import config, writing
from otto.models import Decision, Post, Run, iso, utcnow


# ---- helpers -----------------------------------------------------------------

def _result_log(tmp_path, payload: dict, *, is_error: bool = False) -> str:
    """A headless run's log: the one JSON result object Claude Code writes."""
    p = tmp_path / "run.log"
    p.write_text(json.dumps({"type": "result", "is_error": is_error,
                             "result": json.dumps(payload)}), encoding="utf-8")
    return str(p)


def _run(tmp_path, kind: str, payload: dict, *, post_id: str | None = None,
         status: str = "ok", cost: float | None = 0.12) -> Run:
    notes = f"mode=writing kind={kind}" + (f" post={post_id}" if post_id else "")
    return Run(id="a" * 32, name=f"writing-{kind}", runner="detached", status=status,
               log=_result_log(tmp_path, payload), notes=notes, cost_usd=cost)


def _idea(hook="The alert that cost three dollars to re-learn a fact already on disk",
          **kw) -> dict:
    base = {"hook": hook,
            "angle": "Nine scheduled runs died on upstream capacity and got actioned "
                     "as broken automation. The lesson is about labels, not outages.",
            "stance": "A failed run and a busy upstream are different facts, and a dashboard that merges them is lying.",
            "pushback": "A retry is a retry; nobody has time to classify why the API blinked.",
            "question": "What does the red on your dashboard actually mean?",
            "themes": ["ai", "enterprise"],
            "evidence": ["529 Overloaded killed nine scheduled runs"],
            "reveals": ["Otto, the orchestrator name"],
            "energy": "low"}
    base.update(kw)
    return base


# ---- the gate ----------------------------------------------------------------

@pytest.fixture
def wordlists(monkeypatch):
    monkeypatch.setattr(config, "WRITING_CODENAMES", ["Nimbus", "M5", "playtest", "Otto"])
    monkeypatch.setattr(config, "WRITING_VENDORS", ["Acme", "Grip"])
    monkeypatch.setattr(config, "WRITING_PERSONAL", ["Sam", "twins"])
    monkeypatch.setattr(config, "WRITING_EMPLOYER", ["Example Studios"])


def test_scan_catches_every_class(wordlists):
    text = ("We shipped Nimbus's M5 playtest build to WS-ART07 last week. "
            "A teammate signed off. Ping someone@example.com or U0EXAMPLE01, "
            "account 123456789012 at 10.0.4.7, see https://notion.so/x. "
            "It cost $3.20 and cut 40% off 370 tickets. Acme caught it. "
            "Sam and the twins are due in December at Example Studios.")
    kinds = {}
    for f in writing.scan(text):
        kinds.setdefault(f["kind"], []).append(f["match"])
    assert "Nimbus" in kinds["codename"]
    assert "M5" in kinds["codename"]
    assert "playtest" in kinds["codename"]
    assert "WS-ART07" in kinds["host"]
    assert "someone@example.com" in kinds["email"]
    assert "U0EXAMPLE01" in kinds["slack-id"]
    assert "123456789012" in kinds["aws-account"]
    assert "10.0.4.7" in kinds["ip"]
    assert kinds["url"] == ["https://notion.so/x"]
    assert "$3.20" in kinds["figure"]
    assert "40%" in kinds["figure"]
    assert "370" in kinds["figure"]
    assert "Acme" in kinds["vendor"]
    assert "Sam" in kinds["personal"]
    assert "twins" in kinds["personal"]
    assert "Example Studios" in kinds["employer"]


def test_scan_years_are_not_figures_and_common_words_are_not_vendors(wordlists):
    flags = writing.scan("In 2026 I learned to get a grip on acme-shaped build noise.")
    assert not [f for f in flags if f["kind"] == "figure"]
    # Vendors are matched by exact case: prose that happens to use the words does
    # not map the stack.
    assert not [f for f in flags if f["kind"] == "vendor"]


def test_scan_dedupes_repeats_and_is_empty_on_nothing(wordlists):
    flags = writing.scan("Otto did this. Then Otto did that. otto again.")
    assert len([f for f in flags if f["kind"] == "codename"]) == 1
    assert writing.scan("") == []
    assert writing.scan("A quiet week with nothing to hide.") == []


def test_scan_with_no_wordlists_still_catches_the_structural_classes():
    """Unconfigured lists are empty, not a crash, and the pattern-shaped classes
    (hosts, emails, ids, figures) do not depend on them."""
    for name in ("WRITING_CODENAMES", "WRITING_VENDORS", "WRITING_PERSONAL", "WRITING_EMPLOYER"):
        assert isinstance(getattr(config, name), list)
    kinds = {f["kind"] for f in writing.scan("Ping someone@example.com about WS-ART07, $12 so far.")}
    assert {"email", "host", "figure"} <= kinds


def test_scan_flags_colleagues_from_the_roster(monkeypatch, wordlists):
    from otto import people
    monkeypatch.setattr(config, "OWNER_NAME", "Alex Rivera")
    monkeypatch.setattr(config, "MEETINGS_OWNER", "Alex Rivera")
    monkeypatch.setattr(people, "load", lambda: [
        {"slug": "jwhitfield", "display_name": "Jane Whitfield", "lastName": "Whitfield"},
        {"slug": "arivera", "display_name": "Alex Rivera", "lastName": "Rivera"},
    ])
    flags = writing.scan("Jane Whitfield asked, and Alex Rivera answered. Then Whitfield left.")
    persons = [f["match"] for f in flags if f["kind"] == "person"]
    assert "Jane Whitfield" in persons and "Whitfield" in persons
    assert not any("Rivera" in m for m in persons), "the author is not a leak"


# ---- material ----------------------------------------------------------------

def test_material_excludes_ottos_own_sessions_and_short_ones(store):
    today = date(2026, 9, 25)
    day = (today - timedelta(days=1)).isoformat()
    store.upsert_run(Run(id="b" * 32, name="meeting-notes", runner="detached",
                         status="ok", session_id="otto-sess-1"))
    store.put_day(day, {"rollup": {
        "wall_minutes": 300, "by_project": {"D--otto": 200},
        "sessions": [
            {"session": "otto-sess-1", "minutes": 90, "title": "Otto ingest",
             "intent": "Read the notes", "project": "C--Users-alex"},
            {"session": "s2", "minutes": 60, "title": "Refresh", "project": "C--Users-alex",
             "intent": "You are Otto's refresher, fetching the work domain."},
            {"session": "s3", "minutes": 1, "title": "Blip", "intent": "hi", "project": "D--otto"},
            {"session": "s4", "minutes": 120, "title": "Reverse-engineer the kernel dump",
             "intent": "why does this machine hard reset", "project": "D--work-example"},
        ],
    }})
    text = writing.material(store, today=today)
    assert "Reverse-engineer the kernel dump" in text
    assert "Otto ingest" not in text
    assert "Refresh" not in text
    assert "Blip" not in text
    assert "why does this machine hard reset" in text


def test_material_carries_decisions_notices_and_the_existing_list(store):
    today = date(2026, 9, 25)
    store.add_decision(Decision(id="d-1", title="Bill myself for false positives",
                                decision="Every FP triage gets a cost line",
                                why="A number changes minds where a policy does not",
                                decided=(today - timedelta(days=2)).isoformat()))
    from otto import notify
    notify.post(store, "Known failure is now stale", body="heartbeat healed", source="known")
    store.upsert_post(Post(id="p-old", hook="An old idea", angle="a", status="idea"))
    store.upsert_post(Post(id="p-drop", hook="A dropped idea", angle="a", status="dropped"))
    text = writing.material(store, today=today)
    assert "Bill myself for false positives" in text
    assert "A number changes minds" in text
    assert "Known failure is now stale" in text
    assert "[idea] An old idea" in text
    assert "[dropped, he did not want it] A dropped idea" in text


def test_material_is_bounded(store):
    today = date(2026, 9, 25)
    store.put_day((today - timedelta(days=1)).isoformat(), {"rollup": {
        "wall_minutes": 10, "sessions": [
            {"session": f"s{i}", "minutes": 30, "title": "T" * 90, "intent": "I" * 160,
             "project": "D--x"} for i in range(400)],
    }})
    assert len(writing.material(store, today=today)) <= writing.MATERIAL_MAX_CHARS + 60


# ---- harvest: ideas ----------------------------------------------------------

def test_harvest_ideas_files_posts_with_provenance_and_one_notice(store, tmp_path):
    run = _run(tmp_path, "ideas", {"ideas": [_idea(), _idea(hook="Second thing", themes=["bogus", "security"])],
                                   "summary": "a week of cleanup"})
    notes = writing.harvest(store, run)
    posts = store.posts()
    assert len(posts) == 2
    first = next(p for p in posts if p.hook.startswith("The alert"))
    assert first.status == "idea"
    assert first.origin_run_id == run.id
    assert first.evidence == ["529 Overloaded killed nine scheduled runs"]
    assert first.energy == "low"
    assert first.stance.startswith("A failed run")
    assert first.pushback.startswith("A retry")
    assert first.question.endswith("mean?")
    second = next(p for p in posts if p.hook == "Second thing")
    assert second.themes == ["security"], "unknown themes are dropped, not stored"
    assert any("2 filed" in n for n in notes)
    notices = [n for n in store.notices() if n.source == "writing"]
    assert len(notices) == 1
    assert notices[0].title.startswith("2 post ideas")
    assert notices[0].command == "otto writing"


def test_harvest_ideas_dedupes_against_dropped_and_caps(store, tmp_path, monkeypatch):
    store.upsert_post(Post(id=writing.make_id("Second thing"), hook="Second thing!", angle="a",
                           status="dropped", fingerprint=writing.fingerprint("Second thing")))
    monkeypatch.setattr(config, "WRITING_IDEAS_MAX", 2)
    ideas = [_idea(hook="Second thing"), _idea(hook="A"), _idea(hook="B"), _idea(hook="C")]
    notes = writing.harvest(store, _run(tmp_path, "ideas", {"ideas": ideas}))
    hooks = sorted(p.hook for p in store.posts() if p.status == "idea")
    assert hooks == ["A", "B"]
    assert any("1 already on the list" in n for n in notes)


def test_harvest_ideas_on_a_failed_run_files_nothing(store, tmp_path):
    run = _run(tmp_path, "ideas", {"ideas": [_idea()]}, status="failed")
    run.log = _result_log(tmp_path, {"ideas": [_idea()]}, is_error=True)
    notes = writing.harvest(store, run)
    assert store.posts() == []
    assert notes and "session failed" in notes[0]


def test_harvest_ignores_other_runs(store, tmp_path):
    run = _run(tmp_path, "ideas", {"ideas": [_idea()]})
    run.notes = "mode=ingest kind=meetings"
    assert writing.harvest(store, run) == []
    assert store.posts() == []


# ---- draft lifecycle ---------------------------------------------------------

def _seed_idea(store) -> Post:
    p = writing._post_from(_idea(), "r" * 32)
    store.upsert_post(p)
    return p


def test_start_draft_refuses_the_missing_the_busy_and_the_posted(store, monkeypatch):
    monkeypatch.setattr(writing, "_spawn", lambda prompt, kind, post_id=None: Run(
        id="c" * 32, name="writing-draft", runner="detached", status="running",
        notes=f"mode=writing kind={kind} post={post_id}"))
    with pytest.raises(ValueError, match="no post"):
        writing.start_draft(store, "p-nope")
    p = _seed_idea(store)
    run, post = writing.start_draft(store, p.id, note="lead with the failure")
    assert post.status == "drafting" and post.draft_run_id == run.id
    assert post.notes[-1]["text"] == "lead with the failure"
    with pytest.raises(ValueError, match="already being drafted"):
        writing.start_draft(store, p.id)
    post.status = "posted"
    store.upsert_post(post)
    with pytest.raises(ValueError, match="rewrite history"):
        writing.start_draft(store, p.id)


def test_harvest_draft_lands_the_text_scans_it_and_keeps_history(store, tmp_path):
    p = _seed_idea(store)
    p.status, p.draft_run_id = "drafting", "a" * 32
    store.upsert_post(p)
    text = ("Nine runs died one afternoon and my dashboard called every one of them broken.\r\n\r\n"
            "They were not. Upstream was busy. I spent $3.20 re-learning that.\r\n\r\n"
            "What is the label on your failures telling you?")
    run = _run(tmp_path, "draft", {"draft": text, "alt_hooks": ["Another opener"],
                                   "reveals": ["Otto -> the tool I built"]}, post_id=p.id)
    notes = writing.harvest(store, run)
    fresh = store.get_post(p.id)
    assert fresh.status == "drafted"
    assert "\r" not in fresh.draft
    assert fresh.alt_hooks == ["Another opener"]
    assert fresh.cost_usd == pytest.approx(0.12)
    assert [f["match"] for f in fresh.flags if f["kind"] == "figure"] == ["$3.20"]
    assert fresh.versions == []
    assert any("1 flag" in n for n in notes)
    notice = next(n for n in store.notices() if n.source == "writing")
    assert notice.title.startswith("Draft ready")
    assert notice.command == f"otto writing show {p.id}"

    # A redraft keeps the earlier text.
    fresh.status, fresh.draft_run_id = "drafting", "d" * 32
    store.upsert_post(fresh)
    run2 = _run(tmp_path, "draft", {"draft": "Shorter.", "alt_hooks": []}, post_id=p.id)
    run2.id = "d" * 32
    writing.harvest(store, run2)
    again = store.get_post(p.id)
    assert again.draft == "Shorter."
    assert len(again.versions) == 1 and again.versions[0]["draft"].startswith("Nine runs")
    assert again.cost_usd == pytest.approx(0.24)


def test_harvest_draft_failure_reverts_status_and_records_why(store, tmp_path):
    p = _seed_idea(store)
    p.status, p.draft_run_id = "drafting", "a" * 32
    store.upsert_post(p)
    run = _run(tmp_path, "draft", {"draft": ""}, post_id=p.id)
    notes = writing.harvest(store, run)
    fresh = store.get_post(p.id)
    assert fresh.status == "idea"
    assert fresh.error == "the draft came back empty"
    assert "failed" in notes[0]
    assert not [n for n in store.notices() if n.source == "writing"], "no toast for a miss"


def test_posted_is_only_ever_set_by_the_owner(store, tmp_path):
    p = _seed_idea(store)
    with pytest.raises(ValueError, match="no draft"):
        writing.set_status(store, p.id, "posted")
    p.status, p.draft_run_id = "drafting", "a" * 32
    store.upsert_post(p)
    writing.harvest(store, _run(tmp_path, "draft", {"draft": "A clean post."}, post_id=p.id))
    assert store.get_post(p.id).status == "drafted", "harvest never posts"
    done = writing.set_status(store, p.id, "posted", url="https://linkedin.com/x")
    assert done.status == "posted" and done.posted_at and done.url == "https://linkedin.com/x"
    with pytest.raises(ValueError, match="status must be"):
        writing.set_status(store, p.id, "drafting")


def test_hand_edited_draft_is_rescanned_marked_theirs_and_handed_back(store, tmp_path, wordlists):
    p = _seed_idea(store)
    p.status, p.draft_run_id = "drafting", "a" * 32
    store.upsert_post(p)
    writing.harvest(store, _run(tmp_path, "draft", {"draft": "Mentions Acme."}, post_id=p.id))
    assert [f["kind"] for f in store.get_post(p.id).flags] == ["vendor"]
    assert store.get_post(p.id).edited is False
    edited = writing.set_status(store, p.id, draft="Mentions our EDR. I cut the rest.")
    assert edited.flags == []
    assert edited.edited is True
    assert len(edited.versions) == 1

    # The next draft run is told the text is the owner's, and keeps it in front of
    # the model.
    prompt = writing.build_draft_prompt(store, edited)
    assert "EDITED BY" in prompt
    assert "I cut the rest." in prompt
    assert "Do not restore anything" in prompt

    # A model draft landing supersedes the edit and clears the mark.
    edited.status, edited.draft_run_id = "drafting", "e" * 32
    store.upsert_post(edited)
    run = _run(tmp_path, "draft", {"draft": "Mentions our EDR. I cut the rest. And more."}, post_id=p.id)
    run.id = "e" * 32
    writing.harvest(store, run)
    again = store.get_post(p.id)
    assert again.edited is False
    assert again.versions[-1]["draft"] == "Mentions our EDR. I cut the rest."
    assert len(again.versions) == 2


def test_draft_prompt_carries_the_argument(store):
    p = _seed_idea(store)
    text = writing.build_draft_prompt(store, p)
    assert "stance:   A failed run" in text
    assert "pushback: A retry" in text
    assert "question: What does the red" in text
    assert "Hold the stance" in text
    assert "working title" in text, "the draft writes its own first line"


def test_ideas_prompt_asks_for_an_argument(store):
    text = writing.build_ideas_prompt(store)
    assert "STANCE" in text and '"pushback"' in text and '"question"' in text
    assert "ORDERED BY HOW LIKELY EACH IS TO START AN ARGUMENT" in text
    assert "NOT a confessional opener" in text
    assert 'does not start\n  with "I "' in text


def test_samples_outrank_rules_in_both_prompts(store, monkeypatch, tmp_path):
    sample = tmp_path / "samples.md"
    sample.write_text("## 1\n\nTo be clear. Time travel = RAID; archive is the backup.\n", encoding="utf-8")
    monkeypatch.setattr(config, "WRITING_SAMPLES_PATH", sample)
    p = _seed_idea(store)
    d = writing.build_draft_prompt(store, p)
    assert "Time travel = RAID" in d
    assert d.index("HOW HE ACTUALLY WRITES") < d.index("HIS VOICE. Rules he wrote")
    assert "the samples win" in d
    assert "The interesting part wasn't X" in d, "the tells are banned by name"
    i = writing.build_ideas_prompt(store)
    assert "Time travel = RAID" in i

    # And with no samples the prompt says so instead of pretending.
    monkeypatch.setattr(config, "WRITING_SAMPLES_PATH", tmp_path / "missing.md")
    assert "no samples recorded yet" in writing.build_draft_prompt(store, p)
    assert writing.status(store)["samples_set"] is False


# ---- prompts and the rest ----------------------------------------------------

def test_draft_prompt_carries_voice_notes_previous_and_exemplars(store):
    writing.ensure_voice()
    p = _seed_idea(store)
    p.draft = "The earlier attempt."
    p.notes = [{"at": iso(utcnow()), "text": "tighter"}]
    store.upsert_post(p)
    store.upsert_post(Post(id="p-pub", hook="Published one", angle="a", status="posted",
                           draft="This one went out.", posted_at=iso(utcnow())))
    text = writing.build_draft_prompt(store, p)
    assert "How I write" in text
    assert "tighter" in text
    assert "The earlier attempt." in text
    assert "This one went out." in text
    assert "Otto, the orchestrator name" in text, "what the idea must hide is passed on"


def test_ideas_prompt_is_bounded_and_names_the_cap(store, monkeypatch):
    monkeypatch.setattr(config, "WRITING_IDEAS_MAX", 3)
    text = writing.build_ideas_prompt(store)
    assert "at most 3 ideas" in text
    assert "MATERIAL: what" in text and "actually did" in text


def test_voice_seed_is_written_once_and_never_overwritten():
    p = writing.ensure_voice()
    assert p.read_text(encoding="utf-8") == writing.VOICE_SEED
    p.write_text("# mine\n", encoding="utf-8")
    writing.ensure_voice()
    assert p.read_text(encoding="utf-8") == "# mine\n"


def test_render_lists_by_state_and_shows_flags(store, tmp_path):
    p = _seed_idea(store)
    p.status, p.draft_run_id = "drafting", "a" * 32
    store.upsert_post(p)
    writing.harvest(store, _run(tmp_path, "draft", {"draft": "Cost $9."}, post_id=p.id))
    store.upsert_post(Post(id="p-i2", hook="An idea", angle="the angle", status="idea", energy="low",
                           stance="the claim", pushback="the objection"))
    out = writing.render(store)
    assert "DRAFTS (1)" in out and "IDEAS (1)" in out
    assert "1 flag" in out and "(quick)" in out
    one = writing.render_one(store.get_post(p.id))
    assert "BEFORE IT GOES OUT" in one and "$9" in one
    assert "stance    A failed run" in one and "pushback  A retry" in one
    assert "vs: " in out, "the list shows the objection under an idea"


def test_summary_is_counts_only(store):
    store.upsert_post(Post(id="p-1", hook="h", angle="a", status="idea"))
    s = writing.summary(store)
    assert s == {"counts": {"idea": 1, "drafting": 0, "drafted": 0, "posted": 0, "dropped": 0},
                 "running": 0}
