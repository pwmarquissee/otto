"""Claude Code telemetry, received directly by the daemon.

Otto's rule has always been that it never prices tokens itself (models.py,
transcript.py): a headless run reports `total_cost_usd` and that number is
authoritative. Interactive sessions report nothing, so every terminal the owner
opened by hand was invisible to the History tab. When this was written those terminals
were 84% of the spend on this machine.

Claude Code closes the gap on its own if asked. Its built-in OpenTelemetry
exporter emits one `api_request` event per API call carrying the cost Claude Code
computed, plus the tokens, model, duration, and which subsystem made the call. It
normally wants a collector stack; here the daemon IS the collector. OTLP over
HTTP/JSON lands on POST /v1/logs, is flattened to one small record per event, and
is appended to telemetry.jsonl. No second process, no second port: the exporter
posts to the daemon that already runs, on the URL it already has.

WHAT IS KEPT AND WHAT IS DROPPED. Kept: the accounting attributes (session id,
model, tokens, cost, duration, query source, agent, tool name and result size).
Dropped on the floor: anything that names a person or carries content. user.email,
user.account_id, prompt text, response text, tool parameters, raw bodies. Claude
Code redacts most of those by default, but redaction there is a setting, and this
file outlives any setting, so the drop is enforced here as well. `KEEP` is the
whole allowlist; an attribute not in it does not reach disk.

Enabling it is an `env` block in ~/.claude/settings.json (see ENV and install()).
Every Claude Code process on the machine inherits it, including the ones Otto
spawns, so Otto's own runs report through the same channel and join to their
Run by session id.

When the daemon is down the exporter fails quietly and those turns are simply
not reported. That is the one hole, and ledger.py fills it: transcripts on disk
are the backfill, telemetry is the authority where it exists, and every session
says which one it is priced from.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

FILE = "telemetry.jsonl"

# Events worth keeping. Everything else Claude Code emits (permission mode changes,
# auth, plugin installs, MCP connection chatter) is noise for a cost ledger.
EVENTS = frozenset({
    "api_request", "api_error", "api_refusal", "tool_result", "user_prompt",
    "compaction",
})

# The allowlist. Per event: attribute name -> stored key. A missing entry means the
# attribute is discarded, which is the point.
_COMMON = {
    "session.id": "sid",
    "app.entrypoint": "entry",
    "app.version": "version",
    "prompt.id": "prompt_id",
}
KEEP: dict[str, dict[str, str]] = {
    "api_request": {
        "model": "model", "cost_usd": "cost_usd", "duration_ms": "duration_ms",
        "input_tokens": "inp", "output_tokens": "out",
        "cache_read_tokens": "cr", "cache_creation_tokens": "cw",
        "request_id": "request_id", "query_source": "source", "agent.name": "agent",
        "speed": "speed", "effort": "effort",
    },
    "api_error": {
        "model": "model", "status_code": "status", "attempt": "attempt",
        "duration_ms": "duration_ms", "request_id": "request_id", "query_source": "source",
    },
    "api_refusal": {
        "model": "model", "request_id": "request_id", "query_source": "source",
        "server_fallback_hop": "fallback",
    },
    "tool_result": {
        "tool_name": "tool", "success": "success", "duration_ms": "duration_ms",
        "tool_result_size_bytes": "result_bytes", "tool_input_size_bytes": "input_bytes",
        "error_type": "error_type",
    },
    "user_prompt": {"prompt_length": "prompt_length"},
    "compaction": {"pre_tokens": "pre_tokens", "post_tokens": "post_tokens", "trigger": "trigger"},
}


def env(base_url: str | None = None) -> dict[str, str]:
    """The settings.json `env` block that points Claude Code at the daemon.

    Logs only. The metrics stream duplicates what the api_request events already
    say, and every extra exporter is another thing to fail when the daemon is
    down. Account identifiers are switched off at the source, not just dropped
    here, so they never cross the wire at all.
    """
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": base_url or config.BASE_URL,
        "OTEL_LOGS_EXPORT_INTERVAL": "5000",
        "OTEL_METRICS_INCLUDE_ENTRYPOINT": "true",
        "OTEL_METRICS_INCLUDE_VERSION": "true",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
    }


# ---- settings.json ---------------------------------------------------------

def settings_path() -> Path:
    return config.CLAUDE_DIR / "settings.json"


def installed_in(path: Path | None = None) -> bool:
    p = path or settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    have = data.get("env") if isinstance(data, dict) else None
    if not isinstance(have, dict):
        return False
    want = env()
    return all(str(have.get(k)) == v for k, v in want.items())


def install(path: Path | None = None) -> tuple[bool, str]:
    """Merge the env block into settings.json. Idempotent; touches only our keys.

    Other `env` entries are left alone. Claude Code reads settings.json at
    process start, so sessions already open keep their old environment until
    they are restarted; new ones pick this up immediately.
    """
    p = path or settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError as e:
        return False, f"{p} is not valid JSON, refusing to rewrite it: {e}"
    if not isinstance(data, dict):
        return False, f"{p} is not a JSON object"
    have = data.get("env")
    if not isinstance(have, dict):
        have = {}
    want = env()
    if all(str(have.get(k)) == v for k, v in want.items()):
        return False, "telemetry env already present"
    have.update(want)
    data["env"] = have
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True, f"telemetry env written to {p} (new sessions export to {want['OTEL_EXPORTER_OTLP_ENDPOINT']})"


def uninstall(path: Path | None = None) -> tuple[bool, str]:
    p = path or settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"could not read {p}: {e}"
    have = data.get("env") if isinstance(data, dict) else None
    if not isinstance(have, dict):
        return False, "no env block"
    removed = [k for k in env() if k in have]
    for k in removed:
        del have[k]
    if not have:
        data.pop("env", None)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return bool(removed), f"removed {len(removed)} telemetry keys"


# ---- OTLP flattening ---------------------------------------------------------

def _attrs(items: Any) -> dict[str, Any]:
    """OTLP KeyValue list -> flat dict of plain values."""
    out: dict[str, Any] = {}
    for a in items or []:
        if not isinstance(a, dict):
            continue
        k = a.get("key")
        v = a.get("value")
        if not k or not isinstance(v, dict):
            continue
        if "stringValue" in v:
            out[k] = v["stringValue"]
        elif "intValue" in v:
            try:
                out[k] = int(v["intValue"])
            except (TypeError, ValueError):
                out[k] = v["intValue"]
        elif "doubleValue" in v:
            out[k] = v["doubleValue"]
        elif "boolValue" in v:
            out[k] = v["boolValue"]
    return out


def _name(body: Any, attrs: dict[str, Any]) -> str | None:
    raw = None
    if isinstance(body, dict):
        raw = body.get("stringValue")
    if not raw:
        raw = attrs.get("event.name")
    if not isinstance(raw, str) or not raw:
        return None
    return raw[len("claude_code."):] if raw.startswith("claude_code.") else raw


def _when(rec: dict[str, Any], attrs: dict[str, Any]) -> str | None:
    ts = attrs.get("event.timestamp")
    if isinstance(ts, str) and ts:
        return ts
    for key in ("timeUnixNano", "observedTimeUnixNano"):
        raw = rec.get(key)
        if raw in (None, "", 0, "0"):
            continue
        try:
            return datetime.fromtimestamp(int(raw) / 1e9, timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _num(v: Any) -> Any:
    """cost_usd and the token counts sometimes arrive as strings."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if "." in v or "e" in v.lower() else int(v)
        except ValueError:
            return v
    return v


def flatten(payload: Any) -> list[dict[str, Any]]:
    """An OTLP/JSON logs payload -> the records worth keeping, allowlisted."""
    out: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return out
    for rl in payload.get("resourceLogs") or []:
        if not isinstance(rl, dict):
            continue
        res = _attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs") or []:
            if not isinstance(sl, dict):
                continue
            for rec in sl.get("logRecords") or []:
                if not isinstance(rec, dict):
                    continue
                attrs = _attrs(rec.get("attributes"))
                name = _name(rec.get("body"), attrs)
                if name not in EVENTS:
                    continue
                merged = {**res, **attrs}
                row: dict[str, Any] = {"at": _when(rec, attrs), "name": name}
                for src, dst in _COMMON.items():
                    if src in merged:
                        row[dst] = merged[src]
                for src, dst in KEEP[name].items():
                    if src in merged:
                        row[dst] = _num(merged[src])
                out.append(row)
    return out


def decode_body(raw: bytes, content_encoding: str | None) -> Any:
    if (content_encoding or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # Almost always protobuf: OTEL_EXPORTER_OTLP_PROTOCOL is not http/json.
        return None


# ---- reading it back ---------------------------------------------------------

def path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.STATE_DIR) / FILE


def read(state_dir: Path | None = None, names: frozenset[str] | None = None) -> list[dict[str, Any]]:
    p = path(state_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if names is None or r.get("name") in names:
                    out.append(r)
    except OSError:
        return []
    return out


def by_session(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """api_request events rolled up per session: the reported cost and its cover.

    `requests` is how many API calls Claude Code told us about, which the ledger
    compares against the transcript's turn count before trusting `cost_usd` as the
    whole story. A daemon restart mid-session leaves a partial report, and a
    partial total presented as the total is the one mistake this must not make.
    """
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for r in records:
        if r.get("name") != "api_request":
            continue
        sid = r.get("sid")
        if not sid:
            continue
        rid = r.get("request_id")
        if rid:
            if rid in seen:
                continue
            seen.add(rid)
        s = out.setdefault(sid, {
            "requests": 0, "cost_usd": 0.0, "inp": 0, "out": 0, "cr": 0, "cw": 0,
            "first": None, "last": None, "models": {}, "entry": None,
        })
        s["requests"] += 1
        c = r.get("cost_usd")
        if isinstance(c, (int, float)):
            s["cost_usd"] += float(c)
        for k in ("inp", "out", "cr", "cw"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                s[k] += int(v)
        m = r.get("model")
        if m:
            s["models"][m] = s["models"].get(m, 0) + 1
        at = r.get("at")
        if at:
            s["first"] = min(s["first"] or at, at)
            s["last"] = max(s["last"] or at, at)
        if r.get("entry") and not s["entry"]:
            s["entry"] = r["entry"]
    for s in out.values():
        s["cost_usd"] = round(s["cost_usd"], 4)
    return out


def summary(state_dir: Path | None = None) -> dict[str, Any]:
    recs = read(state_dir, frozenset({"api_request"}))
    sessions = by_session(recs)
    return {
        "records": len(recs),
        "sessions": len(sessions),
        "cost_usd": round(sum(s["cost_usd"] for s in sessions.values()), 2),
        "since": min((r["at"] for r in recs if r.get("at")), default=None),
        "installed": installed_in(),
    }
