"""Reading text arguments: inline, from a file, or from stdin, and the error that stops a bad one."""

from __future__ import annotations

import sys
from pathlib import Path



class DetailError(Exception):
    """--detail and --detail-file disagree, or the file will not read."""


def _resolve_detail(args) -> str | None:
    """Detail text, from --detail or --detail-file (- for stdin).

    Evidence-carrying detail belongs in a file. A detail string quoting a
    command line goes on *our* command line, where the EDR reads it as the
    thing it describes: on 2026-08-17 three `otto task` calls filing the
    certutil FP were themselves blocked by the EDR for containing the word
    certutil. A file path is inert, so the write lands the first time.
    """
    path = getattr(args, "detail_file", None)
    if path is None:
        return args.detail
    if args.detail is not None:
        raise DetailError("pass --detail or --detail-file, not both")
    # utf-8-sig, not utf-8: PowerShell 5.1 writes a BOM by default, and str.strip()
    # does not treat one as whitespace, so it would ride along into the detail text.
    if path == "-":
        return sys.stdin.read().lstrip("﻿").strip() or None
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    except OSError as e:
        raise DetailError(f"cannot read {path}: {e}") from e


def _read_text_arg(path: str | None, inline: str | None) -> str | None:
    if path is None:
        return inline
    if inline is not None:
        raise DetailError("pass the text or --file, not both")
    if path == "-":
        return sys.stdin.read().lstrip("\ufeff").strip() or None
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    except OSError as e:
        raise DetailError(f"cannot read {path}: {e}") from e
