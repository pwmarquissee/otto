"""telemetry.py: the OTLP receiver's flattening, its allowlist, and the settings
env block.

The allowlist test is the one that matters. Claude Code attaches user.email and
account ids to every event by default; the daemon appends what it receives to a
file that outlives any setting. If a field is not in telemetry.KEEP it must not
reach disk, and this is where that is asserted.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

from otto import telemetry


def _kv(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": v}}


def _payload(records, resource=None):
    res = [_kv(k, v) for k, v in (resource or {}).items()]
    return {"resourceLogs": [{"resource": {"attributes": res},
                              "scopeLogs": [{"logRecords": records}]}]}


def _api_request(**over):
    attrs = {
        "event.name": "api_request", "event.timestamp": "2026-08-27T10:00:00Z",
        "model": "claude-opus-5", "cost_usd": 0.4821, "duration_ms": 8100,
        "input_tokens": 12, "output_tokens": 1240, "cache_read_tokens": 184320,
        "cache_creation_tokens": 8100, "request_id": "req_abc", "query_source": "main",
        "user.email": "someone@example.com", "user.account_id": "user_01ABC",
        "session.id": "sess-1", "app.entrypoint": "cli",
    }
    attrs.update(over)
    nanos = int(datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc).timestamp() * 1e9)
    return {"timeUnixNano": str(nanos), "body": {"stringValue": "claude_code.api_request"},
            "attributes": [_kv(k, v) for k, v in attrs.items()]}


def test_flatten_keeps_accounting_and_drops_identity():
    rows = telemetry.flatten(_payload([_api_request()]))
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "api_request"
    assert r["sid"] == "sess-1"
    assert r["entry"] == "cli"
    assert r["model"] == "claude-opus-5"
    assert r["cost_usd"] == 0.4821
    assert r["cr"] == 184320 and r["cw"] == 8100 and r["out"] == 1240 and r["inp"] == 12
    assert r["request_id"] == "req_abc"
    dumped = json.dumps(r)
    assert "example.com" not in dumped
    assert "user_01ABC" not in dumped
    assert "email" not in dumped


def test_flatten_ignores_events_outside_the_list():
    rec = {"body": {"stringValue": "claude_code.permission_mode_changed"},
           "attributes": [_kv("from_mode", "plan"), _kv("to_mode", "auto"), _kv("session.id", "s")]}
    assert telemetry.flatten(_payload([rec])) == []


def test_flatten_takes_resource_attributes_and_timestamp_fallback():
    rec = _api_request()
    rec["attributes"] = [a for a in rec["attributes"] if a["key"] not in ("session.id", "event.timestamp")]
    rows = telemetry.flatten(_payload([rec], resource={"session.id": "from-resource"}))
    assert rows[0]["sid"] == "from-resource"
    assert rows[0]["at"].startswith("2026-08-27T")


def test_tool_result_keeps_name_and_sizes_but_never_parameters():
    rec = {"body": {"stringValue": "claude_code.tool_result"},
           "attributes": [_kv("event.name", "tool_result"), _kv("tool_name", "Bash"),
                          _kv("success", "true"), _kv("duration_ms", 40),
                          _kv("tool_result_size_bytes", 2048),
                          _kv("tool_parameters", '{"bash_command":"rm -rf secret"}'),
                          _kv("session.id", "s")]}
    rows = telemetry.flatten(_payload([rec]))
    assert rows[0]["tool"] == "Bash" and rows[0]["result_bytes"] == 2048
    assert "rm -rf" not in json.dumps(rows[0])


def test_decode_body_handles_gzip_and_rejects_protobuf():
    payload = _payload([_api_request()])
    raw = json.dumps(payload).encode()
    assert telemetry.decode_body(gzip.compress(raw), "gzip") == payload
    assert telemetry.decode_body(raw, None) == payload
    assert telemetry.decode_body(b"\x0a\x03\x08\x01\x12", None) is None


def test_by_session_sums_cost_and_dedupes_request_ids():
    rows = telemetry.flatten(_payload([
        _api_request(request_id="req_1", cost_usd=0.5),
        _api_request(request_id="req_1", cost_usd=0.5),   # exporter retry
        _api_request(request_id="req_2", cost_usd=0.25, model="claude-sonnet-5"),
    ]))
    by = telemetry.by_session(rows)
    s = by["sess-1"]
    assert s["requests"] == 2
    assert s["cost_usd"] == 0.75
    assert s["models"] == {"claude-opus-5": 1, "claude-sonnet-5": 1}
    assert s["entry"] == "cli"


def test_install_is_idempotent_and_preserves_other_env(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "claude-fable-5", "env": {"FOO": "bar"}}), encoding="utf-8")
    changed, _ = telemetry.install(p)
    assert changed
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["model"] == "claude-fable-5"
    assert data["env"]["FOO"] == "bar"
    assert data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert data["env"]["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert data["env"]["OTEL_METRICS_INCLUDE_ACCOUNT_UUID"] == "false"
    assert telemetry.installed_in(p)
    changed, _ = telemetry.install(p)
    assert not changed
    changed, _ = telemetry.uninstall(p)
    assert changed
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["env"] == {"FOO": "bar"}
    assert not telemetry.installed_in(p)


def test_install_refuses_to_rewrite_broken_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    changed, msg = telemetry.install(p)
    assert not changed and "refusing" in msg
    assert p.read_text(encoding="utf-8") == "{not json"
