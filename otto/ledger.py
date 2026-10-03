"""The session ledger: every Claude Code session on this machine, priced.

Three sources, in order of authority, and every session says which one it is
priced from:

  reported   Claude Code's own cost figure. Headless runs put it in the JSON
             result (Run.cost_usd); every session exports it per request through
             telemetry.py once the env block is installed. Never adjusted.
  estimate   The transcript's token counts times a rate card. Used for sessions
             that predate telemetry or ran while the daemon was down. Checked
             against Claude Code's own figure on 318 runs on 2026-08-27: median
             ratio 0.999. The card carries a review date and reports itself stale
             like anything else in Otto that can quietly stop being true.

Two details that decide whether the numbers are right at all:

  * One API response is written to the transcript as one `assistant` line PER
    CONTENT BLOCK (thinking, text, tool_use), each repeating the same usage
    object. Deduping by requestId is not optional: without it every figure here
    is roughly doubled. The count collapsed is reported.
  * Subagent transcripts live in <session>/subagents/*.jsonl and belong to the
    session that spawned them. Claude Code bills them as part of the session;
    so does this.

What the ledger knows beyond a total, borrowed from kelviq/tare and checked
against this machine's data:

  rebuilds   An idle gap longer than the prompt cache TTL means the next turn
             rewrites the whole context at the cache-write rate. 147 of them in
             the owner's sessions cost about $900 of $6,300 when this was written.
             Resuming yesterday's terminal is not free, and this says what it cost.
  context    First and peak context size. A session at a ~1M-token context pays
             about $0.50 a turn on Opus 5 in cache reads before it writes a word.
  tools      What each tool put into the context (injected) times how many later
             turns re-sent it (amplified). Images are sized at what the API charges
             for an image, not at the length of their base64, which is the 100x
             mistake tare makes and the reason its "top files" are all PNGs.

Scanning a gigabyte of transcripts is not free either, so per-file summaries are
cached by (size, mtime) in STATE_DIR/ledger-cache.json and only changed files are
re-read. Only the daemon writes the cache; a CLI caller computes in memory.
"""

from __future__ import annotations

import collections
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, telemetry

# ---- rate card ----------------------------------------------------------------
# USD per million tokens (input, output), Anthropic list price. Cache read is 0.1x
# input, a 5-minute cache write 1.25x, a 1-hour write 2x. Claude Code uses 1-hour
# writes for interactive sessions, which is why cw1 is tracked separately: it is
# not a rounding detail, it was 23% of all spend on this machine.
RATES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "<synthetic>": (0.0, 0.0),
}
RATES_REVIEWED = "2026-08-27"
RATES_MAX_AGE_DAYS = 45
CACHE_READ = 0.10
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0

# An image costs (width * height) / 750 tokens and Claude Code downsizes to fit
# 1568px, so ~1,600 is the ceiling; screenshots land near it. Text is ~4 chars a
# token, which is comparative rather than exact and says so in the API.
IMAGE_TOKENS = 1_600
CHARS_PER_TOKEN = 4.0

# A rebuild: the turn after an idle gap past the 1-hour TTL writes a big cache.
REBUILD_GAP_MINUTES = 60
REBUILD_MIN_TOKENS = 100_000

CACHE_FILE = "ledger-cache.json"
CACHE_VERSION = 3
TOOL_DETAILS_KEEP = 40
SESSIONS_CAP = 300

KINDS = ("inp", "out", "cr", "cw5", "cw1")


def rates_status(today: datetime | None = None) -> dict[str, Any]:
    now = (today or datetime.now(timezone.utc)).date()
    reviewed = datetime.strptime(RATES_REVIEWED, "%Y-%m-%d").date()
    age = (now - reviewed).days
    return {
        "reviewed": RATES_REVIEWED,
        "age_days": age,
        "max_age_days": RATES_MAX_AGE_DAYS,
        "stale": age > RATES_MAX_AGE_DAYS,
        "rates": {m: {"in": i, "out": o} for m, (i, o) in RATES.items() if m != "<synthetic>"},
    }


def rate_for(model: str) -> tuple[float, float] | None:
    if model in RATES:
        return RATES[model]
    # A dated alias (claude-opus-4-8-20260101) prices as its family.
    for k, v in RATES.items():
        if k != "<synthetic>" and model.startswith(k):
            return v
    return None


def price(model: str, tok: dict[str, int]) -> dict[str, float] | None:
    rt = rate_for(model)
    if rt is None:
        return None
    i, o = rt
    return {
        "inp": tok["inp"] / 1e6 * i,
        "out": tok["out"] / 1e6 * o,
        "cr": tok["cr"] / 1e6 * i * CACHE_READ,
        "cw5": tok["cw5"] / 1e6 * i * CACHE_WRITE_5M,
        "cw1": tok["cw1"] / 1e6 * i * CACHE_WRITE_1H,
    }


def tokens_of(usage: dict[str, Any]) -> dict[str, int]:
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    if cc:
        cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
        cw1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    else:
        # Older transcripts carry only the total; Claude Code's interactive default
        # is the 1-hour TTL, but "unknown" is priced at the cheaper 5m rate so an
        # old file cannot be over-billed by a guess.
        cw5 = int(usage.get("cache_creation_input_tokens") or 0)
        cw1 = 0
    return {
        "inp": int(usage.get("input_tokens") or 0),
        "out": int(usage.get("output_tokens") or 0),
        "cr": int(usage.get("cache_read_input_tokens") or 0),
        "cw5": cw5,
        "cw1": cw1,
    }


# ---- tools ----------------------------------------------------------------------

def tool_label(name: str | None, tool_input: Any) -> tuple[str, str | None]:
    """A raw tool name -> a groupable label and the specific thing it touched."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if not name:
        return "unknown", None
    m = re.match(r"^mcp__(.+?)__(.+)$", name)
    if m:
        return f"MCP {m.group(1)}", m.group(2)
    if name in ("Task", "Agent"):
        return "Agent", str(ti.get("subagent_type") or "general-purpose")
    if name == "Skill":
        return "Skill", str(ti.get("skill") or ti.get("skill_name") or ti.get("command") or "?")
    if name in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
        p = ti.get("file_path") or ti.get("notebook_path")
        return name, str(p) if p else None
    if name in ("Bash", "PowerShell"):
        cmd = str(ti.get("command") or "").strip()
        # `cd X && real-command`: the interesting word is the one after the cd.
        cmd = re.sub(r"^cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)\s*", "", cmd)
        cmd = re.sub(r"^\$env:\S+\s*=\s*\S+\s*;\s*", "", cmd)
        cmd = re.sub(r"^(\w+=\S*\s+)+", "", cmd)          # VAR=x VAR2=y command
        first = cmd.split()[0] if cmd.split() else None
        return name, first
    if name == "WebFetch":
        url = str(ti.get("url") or "")
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        return name, host or None
    if name in ("Grep", "Glob"):
        return name, None
    return name, None


def result_tokens(content: Any, fallback: Any = None) -> int:
    """Size a tool_result the way the API bills it, not the way JSON spells it."""
    if content is None and fallback is None:
        return 0
    if isinstance(content, str):
        return int(len(content) / CHARS_PER_TOKEN)
    if isinstance(content, list):
        n = 0
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "image":
                n += IMAGE_TOKENS
            elif t == "text":
                n += int(len(str(b.get("text") or "")) / CHARS_PER_TOKEN)
            elif t == "tool_reference":
                n += 8
            else:
                n += int(len(json.dumps(b, default=str)) / CHARS_PER_TOKEN)
        if n or fallback is None:
            return n
    if fallback is not None:
        try:
            return int(len(json.dumps(fallback, default=str)) / CHARS_PER_TOKEN)
        except (TypeError, ValueError):
            return int(len(str(fallback)) / CHARS_PER_TOKEN)
    return int(len(json.dumps(content, default=str)) / CHARS_PER_TOKEN)


# ---- one transcript -------------------------------------------------------------

def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _local_day(d: datetime) -> str:
    return d.astimezone().date().isoformat()


# Lines worth parsing. file-history-snapshot lines can be megabytes and carry
# nothing the ledger wants, so the cheap check happens before json.loads.
_PREFILTER_RE = re.compile(r'"type"\s*:\s*"(?:assistant|user|ai-title|last-prompt)"')


def _blank() -> dict[str, Any]:
    return {
        "cwd": None, "entry": None, "version": None, "title": None, "prompt": None,
        "branch": None, "first": None, "last": None,
        "turns": 0, "user_turns": 0, "dupes": 0,
        "tok": {k: 0 for k in KINDS}, "cost": {k: 0.0 for k in KINDS},
        "models": {}, "cost_model": {}, "unpriced_models": {},
        "days": {}, "hours": {},
        "first_ctx": None, "max_ctx": 0, "peak_at": None,
        "rebuilds": [], "gaps": 0,
        "tools": {}, "details": {},
    }


def parse_file(path: Path, timeline: bool = False) -> dict[str, Any]:
    """Reduce one transcript to its ledger summary.

    With `timeline` the per-request sequence is kept too (for the detail view),
    which is never cached: it is the one thing that scales with the file.
    """
    s = _blank()
    seen: set[str] = set()
    tool_names: dict[str, tuple[str, str | None]] = {}
    events: list[dict[str, Any]] = []   # tool results, for amplification
    seq: list[tuple[datetime | None, str, dict[str, int], float]] = []  # per request
    prev_ts: datetime | None = None

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return s
    with fh:
        for line in fh:
            if not _PREFILTER_RE.search(line):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            kind = r.get("type")
            if kind == "ai-title":
                if r.get("aiTitle"):
                    s["title"] = str(r["aiTitle"])[:160]
                continue
            if kind == "last-prompt":
                if r.get("lastPrompt"):
                    s["prompt"] = str(r["lastPrompt"])[:160]
                continue

            ts = _parse_ts(r.get("timestamp"))
            if ts is not None:
                s["first"] = ts.isoformat() if not s["first"] or ts.isoformat() < s["first"] else s["first"]
                s["last"] = ts.isoformat() if not s["last"] or ts.isoformat() > s["last"] else s["last"]
            if r.get("cwd") and not s["cwd"]:
                s["cwd"] = str(r["cwd"])
            if r.get("entrypoint") and not s["entry"]:
                s["entry"] = str(r["entrypoint"])
            if r.get("version") and not s["version"]:
                s["version"] = str(r["version"])
            if r.get("gitBranch") and not s["branch"]:
                s["branch"] = str(r["gitBranch"])

            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            content = msg.get("content")
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

            if kind == "user":
                if isinstance(content, str) and not r.get("isSidechain"):
                    s["user_turns"] += 1
                for b in blocks:
                    if b.get("type") != "tool_result":
                        continue
                    label, detail = tool_names.get(b.get("tool_use_id"), ("unknown", None))
                    n = result_tokens(b.get("content"), r.get("toolUseResult"))
                    events.append({"label": label, "detail": detail, "tokens": n,
                                   "error": bool(b.get("is_error")), "req_index": len(seen)})
                continue

            if kind != "assistant":
                continue
            for b in blocks:
                if b.get("type") == "tool_use":
                    tool_names[b.get("id")] = tool_label(b.get("name"), b.get("input"))
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            key = r.get("requestId") or msg.get("id") or r.get("uuid")
            if key in seen:
                s["dupes"] += 1
                continue
            seen.add(key)

            model = str(msg.get("model") or "?")
            tok = tokens_of(usage)
            cost = price(model, tok)
            s["turns"] += 1
            s["models"][model] = s["models"].get(model, 0) + 1
            for k in KINDS:
                s["tok"][k] += tok[k]
            total = 0.0
            if cost is None:
                s["unpriced_models"][model] = s["unpriced_models"].get(model, 0) + 1
            else:
                for k in KINDS:
                    s["cost"][k] += cost[k]
                total = sum(cost.values())
                s["cost_model"][model] = s["cost_model"].get(model, 0.0) + total

            ctx = tok["inp"] + tok["cr"] + tok["cw5"] + tok["cw1"]
            if s["first_ctx"] is None:
                s["first_ctx"] = ctx
            if ctx > s["max_ctx"]:
                s["max_ctx"] = ctx
                s["peak_at"] = ts.isoformat() if ts else None

            if ts is not None:
                day = _local_day(ts)
                d = s["days"].setdefault(day, {"cost": 0.0, "turns": 0})
                d["cost"] += total
                d["turns"] += 1
                h = str(ts.astimezone().hour)
                s["hours"][h] = s["hours"].get(h, 0.0) + total
                if prev_ts is not None:
                    gap = (ts - prev_ts).total_seconds() / 60
                    if gap > REBUILD_GAP_MINUTES:
                        s["gaps"] += 1
                        wrote = tok["cw5"] + tok["cw1"]
                        if wrote >= REBUILD_MIN_TOKENS:
                            rc = (cost["cw5"] + cost["cw1"]) if cost else 0.0
                            s["rebuilds"].append({
                                "at": ts.isoformat(), "idle_minutes": int(gap),
                                "tokens": wrote, "usd": round(rc, 4),
                            })
                prev_ts = ts
            if timeline:
                seq.append((ts, model, tok, total))

    # Amplification: a result injected at request i is re-sent by every later one.
    total_req = len(seen)
    for e in events:
        amp = e["tokens"] * max(0, total_req - e["req_index"])
        t = s["tools"].setdefault(e["label"], {"calls": 0, "injected": 0, "amplified": 0, "errors": 0})
        t["calls"] += 1
        t["injected"] += e["tokens"]
        t["amplified"] += amp
        t["errors"] += 1 if e["error"] else 0
        if e["detail"]:
            dk = f"{e['label']} · {e['detail']}"
            d = s["details"].setdefault(dk, {"calls": 0, "injected": 0, "amplified": 0})
            d["calls"] += 1
            d["injected"] += e["tokens"]
            d["amplified"] += amp
    if len(s["details"]) > TOOL_DETAILS_KEEP:
        top = sorted(s["details"].items(), key=lambda kv: -kv[1]["amplified"])[:TOOL_DETAILS_KEEP]
        s["details"] = dict(top)
    for k in KINDS:
        s["cost"][k] = round(s["cost"][k], 6)
    for m in s["cost_model"]:
        s["cost_model"][m] = round(s["cost_model"][m], 6)
    for d in s["days"].values():
        d["cost"] = round(d["cost"], 6)
    if timeline:
        s["_timeline"] = _chunks(seq)
    return s


def _chunks(seq: list[tuple[datetime | None, str, dict[str, int], float]], n: int = 24) -> list[dict[str, Any]]:
    """The session's story in at most `n` equal-request slices."""
    if not seq:
        return []
    size = max(1, len(seq) // n)
    out = []
    for i in range(0, len(seq), size):
        part = seq[i:i + size]
        stamps = [p[0] for p in part if p[0]]
        out.append({
            "at": stamps[0].isoformat() if stamps else None,
            "turns": len(part),
            "usd": round(sum(p[3] for p in part), 4),
            "peak_ctx": max(p[2]["inp"] + p[2]["cr"] + p[2]["cw5"] + p[2]["cw1"] for p in part),
            "out": sum(p[2]["out"] for p in part),
        })
    return out


# ---- the scan -------------------------------------------------------------------

def projects_root() -> Path:
    return config.CLAUDE_DIR / "projects"


def _files(root: Path) -> list[tuple[Path, str, bool]]:
    """(path, session id, nested) for every transcript under root."""
    out: list[tuple[Path, str, bool]] = []
    if not root.is_dir():
        return out
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            out.append((f, f.stem, False))
        for sub in proj.iterdir():
            if not sub.is_dir():
                continue
            sa = sub / "subagents"
            if sa.is_dir():
                for f in sa.glob("*.jsonl"):
                    out.append((f, sub.name, True))
    return out


def _cache_path(state_dir: Path | None) -> Path:
    return (state_dir or config.STATE_DIR) / CACHE_FILE


def _load_cache(state_dir: Path | None) -> dict[str, Any]:
    p = _cache_path(state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": CACHE_VERSION, "files": {}}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "files": {}}
    return data


def _save_cache(state_dir: Path | None, cache: dict[str, Any]) -> None:
    if not config.is_daemon():
        return
    from .store import _atomic_write  # local import: store imports models, not us
    try:
        _atomic_write(_cache_path(state_dir), json.dumps(cache, separators=(",", ":")))
    except OSError:
        pass


def scan(root: Path | None = None, state_dir: Path | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Every transcript's summary, keyed by path. Cached by (size, mtime)."""
    root = root or projects_root()
    cache = _load_cache(state_dir)
    files = cache.setdefault("files", {})
    live: set[str] = set()
    changed = 0
    stats = {"files": 0, "parsed": 0, "bytes": 0}
    for path, sid, nested in _files(root):
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        live.add(key)
        stats["files"] += 1
        stats["bytes"] += st.st_size
        ent = files.get(key)
        if ent and ent.get("size") == st.st_size and ent.get("mtime") == int(st.st_mtime):
            continue
        summ = parse_file(path)
        files[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "sid": sid, "nested": nested,
                      "project": path.parent.name if not nested else path.parents[2].name,
                      "summary": summ}
        changed += 1
        stats["parsed"] += 1
    for key in [k for k in files if k not in live]:
        del files[key]
        changed += 1
    if changed:
        _save_cache(state_dir, cache)
    return files, stats


# ---- sessions -----------------------------------------------------------------------

def _merge(into: dict[str, Any], summ: dict[str, Any], nested: bool) -> None:
    into["turns"] += summ["turns"]
    into["dupes"] += summ["dupes"]
    if nested:
        into["subagent_turns"] += summ["turns"]
    else:
        into["user_turns"] += summ["user_turns"]
        for k in ("cwd", "entry", "version", "title", "prompt", "branch"):
            if summ.get(k) and not into.get(k):
                into[k] = summ[k]
        into["first_ctx"] = summ["first_ctx"] if into["first_ctx"] is None else into["first_ctx"]
        into["rebuilds"].extend(summ["rebuilds"])
        into["gaps"] += summ["gaps"]
    for k in KINDS:
        into["tok"][k] += summ["tok"][k]
        into["cost"][k] += summ["cost"][k]
    for m, n in summ["models"].items():
        into["models"][m] = into["models"].get(m, 0) + n
    for m, c in summ["cost_model"].items():
        into["cost_model"][m] = into["cost_model"].get(m, 0.0) + c
    for m, n in summ.get("unpriced_models", {}).items():
        into["unpriced_models"][m] = into["unpriced_models"].get(m, 0) + n
    for d, v in summ["days"].items():
        dd = into["days"].setdefault(d, {"cost": 0.0, "turns": 0})
        dd["cost"] += v["cost"]
        dd["turns"] += v["turns"]
    for h, v in summ["hours"].items():
        into["hours"][h] = into["hours"].get(h, 0.0) + v
    if summ["max_ctx"] > into["max_ctx"]:
        into["max_ctx"] = summ["max_ctx"]
        into["peak_at"] = summ["peak_at"]
    for k, t in summ["tools"].items():
        tt = into["tools"].setdefault(k, {"calls": 0, "injected": 0, "amplified": 0, "errors": 0})
        for f in tt:
            tt[f] += t.get(f, 0)
    for k, t in summ["details"].items():
        tt = into["details"].setdefault(k, {"calls": 0, "injected": 0, "amplified": 0})
        for f in tt:
            tt[f] += t.get(f, 0)
    for k in ("first", "last"):
        v = summ.get(k)
        if v and (into[k] is None or (v < into[k] if k == "first" else v > into[k])):
            into[k] = v


def who_of(entry: str | None, sid: str, run_sids: set[str]) -> str:
    """yours | otto | other. Otto's spawns are `sdk-cli` and join a Run by session id;
    `cli` is a terminal the owner opened. Anything else (an IDE, an SDK script) is `other`
    rather than guessed into one of the two."""
    if sid in run_sids or entry == "sdk-cli":
        return "otto"
    if entry == "cli":
        return "yours"
    return "other"


def _span_hours(first: str | None, last: str | None) -> float | None:
    a, b = _parse_ts(first), _parse_ts(last)
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 2)


def build(days: int = 30, root: Path | None = None, state_dir: Path | None = None,
          runs: list[Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The ledger over the last `days`: sessions ranked by cost, the daily series,
    the composition, and where each dollar figure came from."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    files, stats = scan(root, state_dir)

    run_by_sid: dict[str, Any] = {}
    for r in runs or []:
        sid = getattr(r, "session_id", None)
        if sid:
            run_by_sid[sid] = r
    run_sids = set(run_by_sid)

    sessions: dict[str, dict[str, Any]] = {}
    for key, ent in files.items():
        sid = ent["sid"]
        into = sessions.get(sid)
        if into is None:
            into = _blank()
            into.update({"sid": sid, "project": ent["project"], "subagent_turns": 0, "dupes": 0})
            sessions[sid] = into
        _merge(into, ent["summary"], ent["nested"])

    tel = telemetry.by_session(telemetry.read(state_dir, frozenset({"api_request"})))

    out: list[dict[str, Any]] = []
    for sid, s in sessions.items():
        if not s["last"] or s["last"] < cutoff or s["turns"] == 0:
            continue
        estimate = sum(s["cost"].values())
        rep = tel.get(sid)
        basis = "estimate"
        total = estimate
        if rep and s["turns"] and rep["requests"] >= 0.95 * s["turns"]:
            basis = "reported"
            total = rep["cost_usd"]
        elif rep:
            basis = "partial"
        rb_tok = sum(r["tokens"] for r in s["rebuilds"])
        rb_usd = sum(r["usd"] for r in s["rebuilds"])
        run = run_by_sid.get(sid)
        who = who_of(s["entry"], sid, run_sids)
        out.append({
            "sid": sid,
            "who": who,
            "domain": config.domain_for_path(s["cwd"]) if s["cwd"] else config.WORK,
            "project": s["project"],
            "cwd": s["cwd"],
            "branch": s["branch"],
            "entry": s["entry"],
            "version": s["version"],
            "title": s["title"] or (getattr(run, "name", None) if run else None),
            "prompt": s["prompt"],
            "run_id": getattr(run, "id", None) if run else None,
            "run_name": getattr(run, "name", None) if run else None,
            "first": s["first"],
            "last": s["last"],
            "hours": _span_hours(s["first"], s["last"]),
            "turns": s["turns"],
            "subagent_turns": s["subagent_turns"],
            "user_turns": s["user_turns"],
            "dupes": s["dupes"],
            "usd": round(total, 4),
            "estimate_usd": round(estimate, 4),
            "reported_usd": round(rep["cost_usd"], 4) if rep else None,
            "reported_requests": rep["requests"] if rep else 0,
            "basis": basis,
            "per_turn": round(total / s["turns"], 4) if s["turns"] else 0.0,
            "tok": s["tok"],
            "cost_kind": {k: round(v, 4) for k, v in s["cost"].items()},
            "models": s["models"],
            "cost_model": {m: round(c, 4) for m, c in s["cost_model"].items()},
            "unpriced_models": s["unpriced_models"],
            "first_ctx": s["first_ctx"],
            "max_ctx": s["max_ctx"],
            "peak_at": s["peak_at"],
            "gaps": s["gaps"],
            "rebuilds": {"count": len(s["rebuilds"]), "tokens": rb_tok, "usd": round(rb_usd, 4)},
            "days": {d: {"usd": round(v["cost"], 4), "turns": v["turns"]} for d, v in s["days"].items()},
        })
    out.sort(key=lambda s: -s["usd"])

    # Daily series, split by who, on local dates. Estimates throughout, so a day is
    # comparable to the day before it regardless of when telemetry was switched on.
    daily: dict[str, dict[str, Any]] = {}
    hours: dict[str, float] = {}
    day_floor = (now - timedelta(days=days)).astimezone().date().isoformat()
    for s in out:
        for d, v in s["days"].items():
            if d < day_floor:
                continue
            row = daily.setdefault(d, {"date": d, "yours": 0.0, "otto": 0.0, "other": 0.0,
                                       "yours_turns": 0, "otto_turns": 0, "other_turns": 0})
            row[s["who"]] += v["usd"]
            row[s["who"] + "_turns"] += v["turns"]
    # Hour-of-day, the owner's sessions only: when the spend actually happens.
    yours = {s["sid"] for s in out if s["who"] == "yours"}
    for ent in files.values():
        summ = ent["summary"]
        if ent["sid"] not in yours or not summ["last"] or summ["last"] < cutoff:
            continue
        for h, v in summ["hours"].items():
            hours[h] = hours.get(h, 0.0) + v

    by_kind = {k: round(sum(s["cost_kind"][k] for s in out), 2) for k in KINDS}
    by_model: dict[str, float] = collections.defaultdict(float)
    for s in out:
        for m, c in s["cost_model"].items():
            by_model[m] += c
    by_who = {w: round(sum(s["usd"] for s in out if s["who"] == w), 2) for w in ("yours", "otto", "other")}
    rebuild_usd = sum(s["rebuilds"]["usd"] for s in out if s["who"] == "yours")
    tel_summary = {
        "records": sum(v["requests"] for v in tel.values()),
        "sessions": len(tel),
        "installed": telemetry.installed_in(),
        "reported_sessions": sum(1 for s in out if s["basis"] == "reported"),
        "partial_sessions": sum(1 for s in out if s["basis"] == "partial"),
    }

    return {
        "days": days,
        "generated": now.isoformat(),
        "pricing": rates_status(now),
        "telemetry": tel_summary,
        "scan": stats,
        "totals": {
            "usd": round(sum(s["usd"] for s in out), 2),
            "by_who": by_who,
            "sessions": len(out),
            "sessions_by_who": {w: sum(1 for s in out if s["who"] == w) for w in ("yours", "otto", "other")},
            "turns": sum(s["turns"] for s in out),
            "dupes": sum(s["dupes"] for s in out),
            "by_kind": by_kind,
            "by_model": {m: round(c, 2) for m, c in sorted(by_model.items(), key=lambda kv: -kv[1])},
            "rebuilds_yours_usd": round(rebuild_usd, 2),
            "rebuilds_yours": sum(s["rebuilds"]["count"] for s in out if s["who"] == "yours"),
        },
        "daily": [daily[d] for d in sorted(daily)],
        "hours": {h: round(v, 2) for h, v in sorted(hours.items(), key=lambda kv: int(kv[0]))},
        "sessions": [{k: v for k, v in s.items() if k != "days"} for s in out[:SESSIONS_CAP]],
    }


def detail(sid: str, root: Path | None = None, state_dir: Path | None = None,
           runs: list[Any] | None = None) -> dict[str, Any] | None:
    """One session, parsed live: everything build() has plus the timeline and tools."""
    root = root or projects_root()
    hits = [(p, s, n) for p, s, n in _files(root) if s == sid or s.startswith(sid)]
    if not hits:
        return None
    sids = {s for _, s, _ in hits}
    if len(sids) > 1:
        return {"ambiguous": sorted(sids)}
    sid = next(iter(sids))
    into = _blank()
    into.update({"sid": sid, "subagent_turns": 0, "dupes": 0})
    timeline: list[dict[str, Any]] = []
    project = None
    for p, _, nested in hits:
        summ = parse_file(p, timeline=not nested)
        if not nested:
            timeline = summ.pop("_timeline", [])
            project = p.parent.name
        else:
            summ.pop("_timeline", None)
            project = project or p.parents[2].name
        _merge(into, summ, nested)
    run = next((r for r in (runs or []) if getattr(r, "session_id", None) == sid), None)
    rep = telemetry.by_session(telemetry.read(state_dir, frozenset({"api_request"}))).get(sid)
    estimate = sum(into["cost"].values())
    basis, total = "estimate", estimate
    if rep and into["turns"] and rep["requests"] >= 0.95 * into["turns"]:
        basis, total = "reported", rep["cost_usd"]
    elif rep:
        basis = "partial"
    tools = sorted(into["tools"].items(), key=lambda kv: -kv[1]["amplified"])
    details = sorted(into["details"].items(), key=lambda kv: -kv[1]["amplified"])[:20]
    amp_total = sum(t["amplified"] for _, t in tools) or 1
    return {
        "sid": sid,
        "who": who_of(into["entry"], sid, {getattr(run, "session_id", None)} if run else set()),
        "project": project,
        "cwd": into["cwd"], "branch": into["branch"], "entry": into["entry"], "version": into["version"],
        "title": into["title"] or (getattr(run, "name", None) if run else None),
        "prompt": into["prompt"],
        "run_id": getattr(run, "id", None) if run else None,
        "run_name": getattr(run, "name", None) if run else None,
        "first": into["first"], "last": into["last"], "hours": _span_hours(into["first"], into["last"]),
        "turns": into["turns"], "subagent_turns": into["subagent_turns"], "user_turns": into["user_turns"],
        "dupes": into["dupes"],
        "usd": round(total, 4), "estimate_usd": round(estimate, 4),
        "reported_usd": round(rep["cost_usd"], 4) if rep else None,
        "reported_requests": rep["requests"] if rep else 0,
        "basis": basis,
        "per_turn": round(total / into["turns"], 4) if into["turns"] else 0.0,
        "tok": into["tok"],
        "cost_kind": {k: round(v, 4) for k, v in into["cost"].items()},
        "models": into["models"],
        "cost_model": {m: round(c, 4) for m, c in into["cost_model"].items()},
        "first_ctx": into["first_ctx"], "max_ctx": into["max_ctx"], "peak_at": into["peak_at"],
        "gaps": into["gaps"],
        "rebuilds": into["rebuilds"],
        "rebuild_usd": round(sum(r["usd"] for r in into["rebuilds"]), 4),
        "timeline": timeline,
        "tools": [{"tool": k, **t, "share": round(100 * t["amplified"] / amp_total, 1)} for k, t in tools[:20]],
        "details": [{"what": k, **t} for k, t in details],
        "pricing": rates_status(),
    }
