"""--detail-file: keeping detection evidence off the command line.

The bug these guard: on 2026-08-17 Otto's daily sweep found a Falcon false
positive, wrote a task about it, and the task text quoted the certutil decode
flag. Falcon matched OUR command line and blocked the filing. Three `otto task`
calls died in 107 seconds, two of them to CommandLineKnownMalware.

The fix cannot be a check inside Otto: the process is killed before Python
starts. So what is testable is the primitive that lets a caller avoid argv
entirely, and every way it can be got wrong.
"""

from __future__ import annotations

import argparse
import io

import pytest

from otto.cli import DetailError, _resolve_detail

BOM = "﻿"


def args(**kw):
    ns = argparse.Namespace(detail=None, detail_file=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# ---- the plain path still works ---------------------------------------------

def test_inline_detail_is_returned_unchanged():
    assert _resolve_detail(args(detail="ordinary text")) == "ordinary text"


def test_no_detail_at_all_is_none():
    assert _resolve_detail(args()) is None


def test_absent_detail_file_attribute_falls_back_to_detail():
    """`otto propose --json-body` builds a namespace without detail_file."""
    ns = argparse.Namespace(detail="inline")
    assert _resolve_detail(ns) == "inline"


# ---- the file path -----------------------------------------------------------

def test_detail_is_read_from_a_file(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("evidence from a file\n", encoding="utf-8")
    assert _resolve_detail(args(detail_file=str(f))) == "evidence from a file"


def test_the_blocked_text_round_trips_intact(tmp_path):
    """The literal string that Falcon blocked, carried through a file.

    Assembled from parts so this test file does not itself put the flag on a
    command line when something greps or cats it. Same reason the fix exists.
    """
    flag = "-url" + "cache"
    body = (
        "ObfCertutilCmd fired on DESKTOP-IFHORGM.\n"
        "\n"
        f"Scope to a local decode with no URL / {flag} / -f, so the\n"
        "download-cradle abuse case still detects.\n"
    )
    f = tmp_path / "d.txt"
    f.write_text(body, encoding="utf-8")

    out = _resolve_detail(args(detail_file=str(f)))
    assert flag in out
    assert out.count("\n\n") == 1, "paragraph structure must survive"


def test_multiline_detail_keeps_its_shape(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("para one\n\npara two\n\npara three\n", encoding="utf-8")
    assert _resolve_detail(args(detail_file=str(f))).split("\n\n") == [
        "para one", "para two", "para three",
    ]


def test_an_empty_file_is_none_not_empty_string(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("   \n\n  ", encoding="utf-8")
    assert _resolve_detail(args(detail_file=str(f))) is None


# ---- stdin -------------------------------------------------------------------

def test_dash_reads_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped evidence\n"))
    assert _resolve_detail(args(detail_file="-")) == "piped evidence"


def test_empty_stdin_is_none(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n  \n"))
    assert _resolve_detail(args(detail_file="-")) is None


# ---- the BOM, which is not theoretical on Windows ----------------------------

def test_a_bom_written_by_powershell_is_stripped(tmp_path):
    """Windows PowerShell 5.1 writes a BOM by default, and str.strip() does not
    treat one as whitespace, so it would otherwise ride into the detail text."""
    f = tmp_path / "d.txt"
    f.write_bytes(b"\xef\xbb\xbfdetail written by Out-File\n")

    out = _resolve_detail(args(detail_file=str(f)))
    assert out == "detail written by Out-File"
    assert not out.startswith(BOM)


def test_a_bom_on_stdin_is_stripped(monkeypatch):
    """A PowerShell pipe emits one too."""
    monkeypatch.setattr("sys.stdin", io.StringIO(BOM + "piped\n"))
    out = _resolve_detail(args(detail_file="-"))
    assert out == "piped"
    assert not out.startswith(BOM)


def test_a_bom_does_not_break_json_carried_as_detail(tmp_path):
    import json
    f = tmp_path / "d.json"
    f.write_bytes(b'\xef\xbb\xbf{"id": "x"}\n')
    assert json.loads(_resolve_detail(args(detail_file=str(f)))) == {"id": "x"}


# ---- refusing the ambiguous cases --------------------------------------------

def test_both_flags_together_are_refused(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("from file", encoding="utf-8")
    with pytest.raises(DetailError) as e:
        _resolve_detail(args(detail="inline", detail_file=str(f)))
    assert "not both" in str(e.value)


def test_both_flags_are_refused_before_the_file_is_read(tmp_path):
    """Order matters: the conflict is the error, not the missing file."""
    with pytest.raises(DetailError) as e:
        _resolve_detail(args(detail="inline", detail_file=str(tmp_path / "nope.txt")))
    assert "not both" in str(e.value)


def test_a_missing_file_is_a_clean_error_not_a_traceback(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(DetailError) as e:
        _resolve_detail(args(detail_file=str(missing)))
    assert "cannot read" in str(e.value)
    assert str(missing) in str(e.value)


def test_a_directory_passed_as_the_file_is_a_clean_error(tmp_path):
    with pytest.raises(DetailError):
        _resolve_detail(args(detail_file=str(tmp_path)))
