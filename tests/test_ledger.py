"""ledger.py: pricing sessions from transcripts without getting the number wrong.

The dedupe test is the one that already failed once: the first pass at this
analysis on 2026-08-27 summed every `assistant` line and came out 2.1x high,
because Claude Code writes one line per content block with the same usage. The
image test is the mistake tare makes (base64 length / 4 is ~100x an image's real
token cost). The rebuild test is the finding worth the whole module: 147 resumed
sessions cost about $900 in rewritten context.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import ledger

T0 = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _assistant(sid, req, when, model="claude-opus-5", inp=10, out=100, cr=0, cw5=0, cw1=0,
               blocks=None, cwd="D:\\work\\proj", entry="cli"):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": cw5 + cw1,
             "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1}}
    return {"type": "assistant", "sessionId": sid, "requestId": req, "timestamp": _iso(when),
            "cwd": cwd, "entrypoint": entry, "version": "2.1.248", "uuid": f"u-{req}-{len(blocks or [])}",
            "message": {"model": model, "usage": usage, "content": blocks or [{"type": "text", "text": "hi"}]}}


def _user(sid, when, content, cwd="D:\\work\\proj"):
    return {"type": "user", "sessionId": sid, "timestamp": _iso(when), "cwd": cwd, "entrypoint": "cli",
            "message": {"role": "user", "content": content}}


def _write(root: Path, project: str, sid: str, lines, nested_agent: str | None = None):
    if nested_agent:
        d = root / project / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"agent-{nested_agent}.jsonl"
    else:
        d = root / project
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return p


def test_duplicate_content_blocks_count_once(tmp_path):
    sid = "aaaa1111-0000-0000-0000-000000000001"
    when = T0
    # One API response, three content blocks, three lines, identical usage.
    lines = [
        {"type": "ai-title", "aiTitle": "Dedupe me"},
        _user(sid, when, "hello"),
        _assistant(sid, "req_1", when, out=1000, blocks=[{"type": "thinking", "thinking": "..."}]),
        _assistant(sid, "req_1", when, out=1000, blocks=[{"type": "text", "text": "ok"}]),
        _assistant(sid, "req_1", when, out=1000, blocks=[{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]),
    ]
    _write(tmp_path, "D--work-proj", sid, lines)
    s = ledger.parse_file(tmp_path / "D--work-proj" / f"{sid}.jsonl")
    assert s["turns"] == 1
    assert s["dupes"] == 2
    assert s["tok"]["out"] == 1000
    assert s["title"] == "Dedupe me"
    assert s["user_turns"] == 1


def test_pricing_matches_the_rate_card():
    tok = {"inp": 1_000_000, "out": 1_000_000, "cr": 1_000_000, "cw5": 1_000_000, "cw1": 1_000_000}
    c = ledger.price("claude-opus-5", tok)
    assert c == {"inp": 5.0, "out": 25.0, "cr": 0.5, "cw5": 6.25, "cw1": 10.0}
    assert ledger.price("claude-opus-4-8-20260101", tok)["inp"] == 5.0   # dated alias -> family
    assert ledger.price("claude-nonesuch-9", tok) is None


def test_unknown_model_is_reported_not_guessed(tmp_path):
    sid = "bbbb1111-0000-0000-0000-000000000002"
    _write(tmp_path, "D--work-proj", sid, [_assistant(sid, "r1", T0, model="claude-nonesuch-9", out=500)])
    s = ledger.parse_file(tmp_path / "D--work-proj" / f"{sid}.jsonl")
    assert s["turns"] == 1
    assert sum(s["cost"].values()) == 0
    assert s["unpriced_models"] == {"claude-nonesuch-9": 1}


def test_image_results_are_sized_as_images_not_base64():
    huge_b64 = "A" * 2_000_000
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": huge_b64}}]
    assert ledger.result_tokens(content) == ledger.IMAGE_TOKENS
    text = [{"type": "text", "text": "x" * 4000}]
    assert ledger.result_tokens(text) == 1000
    assert ledger.result_tokens("y" * 400) == 100


def test_tool_attribution_amplifies_by_later_turns(tmp_path):
    sid = "cccc1111-0000-0000-0000-000000000003"
    t = T0
    lines = [
        _assistant(sid, "r1", t, blocks=[{"type": "tool_use", "id": "t1", "name": "Read",
                                          "input": {"file_path": "D:\\big\\bundle.js"}}]),
        _user(sid, t + timedelta(seconds=1), [{"type": "tool_result", "tool_use_id": "t1", "content": "z" * 40_000}]),
        _assistant(sid, "r2", t + timedelta(seconds=5), blocks=[{"type": "tool_use", "id": "t2", "name": "Bash",
                                                                  "input": {"command": "cd D:\\x && python -m pytest"}}]),
        _user(sid, t + timedelta(seconds=6), [{"type": "tool_result", "tool_use_id": "t2", "content": "ok", "is_error": True}]),
        _assistant(sid, "r3", t + timedelta(seconds=10)),
        _assistant(sid, "r4", t + timedelta(seconds=15)),
    ]
    _write(tmp_path, "D--work-proj", sid, lines)
    s = ledger.parse_file(tmp_path / "D--work-proj" / f"{sid}.jsonl")
    read = s["tools"]["Read"]
    # 10,000 tokens injected after request 1; requests 2, 3, 4 re-send it.
    assert read["injected"] == 10_000
    assert read["amplified"] == 30_000
    assert s["tools"]["Bash"]["errors"] == 1
    assert "Read · D:\\big\\bundle.js" in s["details"]
    assert "Bash · python" in s["details"]        # the `cd X &&` prefix is not the command


def test_tool_label_strips_cd_and_env_prefixes():
    assert ledger.tool_label("Bash", {"command": "cd /d/otto && PYTHONPATH=/d/otto python -m otto"}) == ("Bash", "python")
    assert ledger.tool_label("Bash", {"command": 'cd "C:/Program Files/x" ; ls -la'}) == ("Bash", "ls")
    assert ledger.tool_label("mcp__ninjaone__ninjaone_devices_list", {}) == ("MCP ninjaone", "ninjaone_devices_list")
    assert ledger.tool_label("Agent", {"subagent_type": "Explore"}) == ("Agent", "Explore")
    assert ledger.tool_label("WebFetch", {"url": "https://github.com/kelviq/tare"}) == ("WebFetch", "github.com")


def test_rebuild_after_idle_gap_is_priced(tmp_path):
    sid = "dddd1111-0000-0000-0000-000000000004"
    t = T0
    lines = [
        _assistant(sid, "r1", t, cr=500_000, cw1=20_000),
        _assistant(sid, "r2", t + timedelta(minutes=5), cr=520_000, cw1=5_000),
        # 90 minutes idle: the 1-hour cache is gone, the next turn rewrites it all.
        _assistant(sid, "r3", t + timedelta(minutes=95), cr=0, cw1=525_000),
        # 40 minutes idle: inside the TTL, no rebuild.
        _assistant(sid, "r4", t + timedelta(minutes=135), cr=525_000, cw1=3_000),
    ]
    _write(tmp_path, "D--work-proj", sid, lines)
    s = ledger.parse_file(tmp_path / "D--work-proj" / f"{sid}.jsonl")
    assert s["gaps"] == 1
    assert len(s["rebuilds"]) == 1
    rb = s["rebuilds"][0]
    assert rb["idle_minutes"] == 90
    assert rb["tokens"] == 525_000
    assert abs(rb["usd"] - 525_000 / 1e6 * 5.0 * 2.0) < 1e-6
    assert s["max_ctx"] == 528_010     # r4: 10 in + 525,000 read + 3,000 written
    assert s["first_ctx"] == 520_010


def test_build_merges_subagents_and_joins_runs(tmp_path):
    sid = "eeee1111-0000-0000-0000-000000000005"
    t = T0
    _write(tmp_path, "D--work-proj", sid, [
        {"type": "ai-title", "aiTitle": "Main thread"},
        _assistant(sid, "r1", t, out=1000, entry="sdk-cli"),
    ])
    _write(tmp_path, "D--work-proj", sid, [
        _assistant(sid, "r9", t + timedelta(seconds=30), out=2000, model="claude-haiku-4-5-20251001"),
    ], nested_agent="abc123")

    class Run:  # the two fields build() reads
        id = "run-1"
        name = "slack-sweep"
        session_id = sid

    rep = ledger.build(days=30, root=tmp_path, state_dir=tmp_path / "state", runs=[Run()],
                       now=t + timedelta(hours=1))
    assert rep["totals"]["sessions"] == 1
    s = rep["sessions"][0]
    assert s["turns"] == 2 and s["subagent_turns"] == 1
    assert s["who"] == "otto"
    assert s["run_id"] == "run-1"
    assert s["title"] == "Main thread"
    assert s["basis"] == "estimate"
    # opus 1000 out = $0.025 + $0.00005 in; haiku 2000 out = $0.01 + $0.00001 in;
    # the API rounds to four decimals.
    assert abs(s["usd"] - (0.025 + 0.00005 + 0.01 + 0.00001)) < 5e-5
    assert set(s["models"]) == {"claude-opus-5", "claude-haiku-4-5-20251001"}
    assert rep["pricing"]["stale"] is False
    assert rep["telemetry"]["records"] == 0


def test_build_prefers_telemetry_when_it_covers_the_session(tmp_path):
    sid = "ffff1111-0000-0000-0000-000000000006"
    t = T0
    _write(tmp_path, "D--work-proj", sid, [
        _assistant(sid, "r1", t, out=1000),
        _assistant(sid, "r2", t + timedelta(seconds=10), out=1000),
    ])
    state = tmp_path / "state"
    state.mkdir()
    rows = [{"at": _iso(t), "name": "api_request", "sid": sid, "request_id": "r1", "cost_usd": 0.03, "model": "claude-opus-5"},
            {"at": _iso(t), "name": "api_request", "sid": sid, "request_id": "r2", "cost_usd": 0.03, "model": "claude-opus-5"}]
    (state / "telemetry.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    rep = ledger.build(days=30, root=tmp_path, state_dir=state, now=t + timedelta(hours=1))
    s = rep["sessions"][0]
    assert s["basis"] == "reported"
    assert s["usd"] == 0.06
    assert s["reported_requests"] == 2
    assert abs(s["estimate_usd"] - 0.0501) < 1e-6
    assert rep["telemetry"]["reported_sessions"] == 1

    # Half the requests reported (daemon was down for the rest): partial, estimate wins.
    (state / "telemetry.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    rep = ledger.build(days=30, root=tmp_path, state_dir=state, now=t + timedelta(hours=1))
    s = rep["sessions"][0]
    assert s["basis"] == "partial"
    assert s["usd"] == s["estimate_usd"]


def test_who_of():
    assert ledger.who_of("cli", "s", set()) == "yours"
    assert ledger.who_of("sdk-cli", "s", set()) == "otto"
    assert ledger.who_of("claude-vscode", "s", set()) == "other"
    assert ledger.who_of("claude-vscode", "s", {"s"}) == "otto"   # joined to a Run


def test_rates_status_goes_stale():
    reviewed = datetime.strptime(ledger.RATES_REVIEWED, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    fresh = ledger.rates_status(reviewed + timedelta(days=1))
    assert fresh["stale"] is False and fresh["age_days"] == 1
    old = ledger.rates_status(reviewed + timedelta(days=ledger.RATES_MAX_AGE_DAYS + 1))
    assert old["stale"] is True


def test_detail_returns_timeline_and_tools(tmp_path):
    sid = "9999aaaa-0000-0000-0000-000000000007"
    t = T0
    lines = [_assistant(sid, "r1", t, blocks=[{"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}]),
             _user(sid, t + timedelta(seconds=1), [{"type": "tool_result", "tool_use_id": "t1", "content": "m" * 400}])]
    lines += [_assistant(sid, f"r{i}", t + timedelta(minutes=i)) for i in range(2, 30)]
    _write(tmp_path, "D--work-proj", sid, lines)
    d = ledger.detail("9999aaaa", root=tmp_path, state_dir=tmp_path / "state")
    assert d["sid"] == sid
    assert d["turns"] == 29
    assert 20 <= len(d["timeline"]) <= 29
    assert d["tools"][0]["tool"] == "Grep"
    assert d["tools"][0]["amplified"] == 100 * 28
    assert ledger.detail("nope", root=tmp_path) is None
