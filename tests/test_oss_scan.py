"""scripts/oss_scan.py against a temporary tree.

Fixture strings that the scanner must flag are assembled from pieces so this test
file does not itself trip the scanner when it walks the repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "oss_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("oss_scan", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


oss_scan = _load()


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _write(tmp_path, "README.md", "\n".join([
        "# Project",
        "Contact dev@example.com or ops@example.org or nobody@host.invalid.",
        "Account 123456789012 and 000000000000 are placeholders.",
        "Run from any directory.",
    ]) + "\n")
    _write(tmp_path, "src/app.py", "VALUE = 42\n")
    # Skipped directories and binary-looking files must not produce findings.
    _write(tmp_path, ".git/config", "someone" + "@" + "private-corp.test\n")
    _write(tmp_path, "vendor/lib.js", "AKIA" + "ABCDEFGHIJKLMNOP\n")
    _write(tmp_path, "target/out.txt", "U0" + "ABCDEFGH1\n")
    _write(tmp_path, "__pycache__/x.txt", "gh" + "p_abcdef\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"AKIA" + b"ABCDEFGHIJKLMNOP")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" + b"AKIA" + b"ABCDEFGHIJKLMNOP")
    return tmp_path


def test_clean_tree_exits_zero(tree: Path, capsys, monkeypatch):
    monkeypatch.delenv("OSS_SCAN_WORDS", raising=False)
    rc = oss_scan.main([str(tree)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("clean:")


def test_each_private_shape_is_flagged(tree: Path, monkeypatch):
    monkeypatch.delenv("OSS_SCAN_WORDS", raising=False)
    # Real-looking values on purpose: reserved TLDs (.test), placeholder user
    # names (someone, alex) and bare token prefixes are allowed by design, so the
    # shapes here have to be the ones a leak would actually have.
    dirty = "\n".join([
        "email: " + "someone" + "@" + "private-corp.com",
        "account: " + "4111" + "11111111",
        "slack: " + "U0" + "ABCDEFGH1",
        "path: " + "C:" + "\\Users" + "\\jdoe\\repo",
        "aws: " + "AKIA" + "ABCDEFGHIJKLMNOP",
        "slack token: " + "xox" + "b-1234567890ab",
        "anthropic: " + "sk-" + "ant-abcdefgh12",
        "github: " + "gh" + "p_abcdefghijklmnopqrstuv",
        "pem: " + "-----" + "BEGIN RSA PRIVATE KEY-----",
    ]) + "\n"
    _write(tree, "notes/dirty.txt", dirty)

    findings = oss_scan.scan_tree(tree, None)
    by_kind = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)

    assert all(f["path"] == "notes/dirty.txt" for f in findings), findings
    assert [f["line"] for f in by_kind["email"]] == [1]
    assert [f["line"] for f in by_kind["account-id"]] == [2]
    assert [f["line"] for f in by_kind["slack-id"]] == [3]
    assert [f["line"] for f in by_kind["home-path"]] == [4]
    assert sorted(f["line"] for f in by_kind["credential"]) == [5, 6, 7, 8, 9]
    assert "denylist" not in by_kind


def test_placeholders_are_allowed(tree: Path):
    _write(tree, "docs/ok.md", "\n".join([
        "Mail dev@sub.example.com and root@localhost.invalid.",
        "Account 999999999999 is a placeholder; 1234567890123 is 13 digits.",
        "Home paths look like C:" + "\\Users" + "\\<name>\\.claude in docs.",
        "Not a slack id: U0abc (too short), UA0123456789 (no zero after U).",
    ]) + "\n")
    assert oss_scan.scan_tree(tree, None) == []


def test_denylist_from_env_is_case_insensitive(tree: Path, monkeypatch, capsys):
    _write(tree, "docs/words.md", "Mentions Acme and ACME-Internal once each.\n")
    monkeypatch.setenv("OSS_SCAN_WORDS", "acme, widgetco ,")
    rc = oss_scan.main([str(tree)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "docs/words.md:1: denylist: Acme" in out
    assert "docs/words.md:1: denylist: ACME" in out
    assert "2 finding(s) in 1 file(s)" in out


def test_json_output(tree: Path, monkeypatch, capsys):
    monkeypatch.delenv("OSS_SCAN_WORDS", raising=False)
    _write(tree, "a.txt", "id " + "C0" + "ABCDEFGH12" + "\n")
    rc = oss_scan.main([str(tree), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["count"] == 1
    assert payload["findings"][0] == {
        "path": "a.txt", "line": 1, "kind": "slack-id", "match": "C0" + "ABCDEFGH12",
    }


def test_script_runs_as_a_process(tree: Path):
    import subprocess

    r = subprocess.run([sys.executable, str(_SCRIPT), str(tree)],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("clean:")
