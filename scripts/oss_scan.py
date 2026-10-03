"""Release guard: fail on anything in the tree that looks private.

    python scripts/oss_scan.py [ROOT] [--json]

Walks ROOT (default: the repository root, the parent of this script's directory),
skipping .git, vendor, target, gen, node_modules, __pycache__ and .pytest_cache,
and reports file:line for:

  * email addresses whose domain is not example.com, example.org, .invalid or
    anthropic.com
  * 12-digit numbers (cloud account ids) other than 123456789012 and runs of one
    repeated digit (000000000000 through 999999999999)
  * Slack ids: a U/C/D/T/G followed by 0 and 8 to 10 uppercase alphanumerics
  * Windows home paths (a drive letter, then \\Users\\<name>)
  * credential shapes: AWS access key ids, Slack tokens, Anthropic keys, GitHub
    tokens, PEM headers
  * any word from OSS_SCAN_WORDS (comma separated, case-insensitive), so a private
    word list never has to be committed

Exit 1 when there are findings, 0 when clean. Stdlib only.

The credential patterns are assembled from pieces so this file's own source does
not match itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = frozenset({
    ".git", "vendor", "target", "gen", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# Binary and generated formats are skipped by extension before being opened.
SKIP_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".bmp", ".webp", ".svgz",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyd", ".o", ".a", ".lib",
    ".mp3", ".mp4", ".wav", ".mov", ".db", ".sqlite", ".sqlite3", ".lock",
})

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "invalid", "anthropic.com")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
TWELVE_DIGIT_RE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
SLACK_ID_RE = re.compile(r"\b[UCDTG]0[A-Z0-9]{8,10}\b")
# `<` is excluded from the name so a documentation placeholder like C:\Users\<name>
# passes; a real account name never contains it.
WIN_HOME_RE = re.compile(r"\b[A-Za-z]:\\Users\\[^\\/\s\"'<>|]+")

# A credential is the prefix PLUS a body. The bare prefix appears legitimately in
# code that validates tokens ("expected xoxb-") and in docs; flagging it taught
# people to skip the report, which is the one thing a release guard must not do.
_CRED_PARTS = (
    "AKIA" + "[0-9A-Z]{16}",
    "xox" + "[bpa]-" + "[A-Za-z0-9-]{8,}",
    "sk-" + "ant-" + "[A-Za-z0-9_-]{8,}",
    "gh" + "p_" + "[A-Za-z0-9]{20,}",
    "-----" + "BEGIN" + " [A-Z ]*PRIVATE KEY",
)
CRED_RE = re.compile("|".join(_CRED_PARTS))


# Shapes that are obviously synthetic. Tests need ids and addresses that parse,
# and a scanner that cannot tell C0EXAMPLEOPS from a real channel id teaches people
# to ignore it. Real Slack ids are base-36 noise; a readable word inside one is the
# tell. Reserved TLDs (.test, .example, .invalid, .localhost) are never routable.
PLACEHOLDER_TLDS = ("test", "example", "invalid", "localhost")
PLACEHOLDER_ID_WORDS = ("EXAMPLE", "TEST", "FAKE", "NOTALLOWED", "PLACEHOLDER")
PLACEHOLDER_USERS = ("alex", "example", "user", "you", "name", "someone", "owner", "me")


def _email_allowed(domain: str) -> bool:
    d = domain.lower()
    if d.rsplit(".", 1)[-1] in PLACEHOLDER_TLDS:
        return True
    if d.replace(".", "").isdigit():
        return True  # `@phosphor-icons/web@2.1.1`: a package version, not a mailbox
    return any(d == a or d.endswith("." + a) for a in ALLOWED_EMAIL_DOMAINS)


def _twelve_allowed(digits: str) -> bool:
    # 123456789012 is the documented example; 000000000001-style counters are tests.
    return digits == "123456789012" or len(set(digits)) == 1 or digits.startswith("0000000000")


def _slack_id_allowed(sid: str) -> bool:
    # A readable word, or an all-zero body (U0000000000), is documentation.
    return any(w in sid for w in PLACEHOLDER_ID_WORDS) or set(sid[1:]) == {"0"}


def _home_allowed(path: str) -> bool:
    name = path.replace("/", "\\").split("\\Users\\", 1)[-1].split("\\", 1)[0]
    return name.lower() in PLACEHOLDER_USERS


def denylist_pattern(words_env: str | None) -> re.Pattern[str] | None:
    """Compile OSS_SCAN_WORDS into one case-insensitive alternation, or None."""
    if not words_env:
        return None
    words = [w.strip() for w in words_env.split(",") if w.strip()]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)


def scan_line(line: str, deny: re.Pattern[str] | None) -> list[tuple[str, str]]:
    """Return (kind, match) pairs for one line of text."""
    out: list[tuple[str, str]] = []
    for m in EMAIL_RE.finditer(line):
        if not _email_allowed(m.group(1)):
            out.append(("email", m.group(0)))
    for m in TWELVE_DIGIT_RE.finditer(line):
        if not _twelve_allowed(m.group(0)):
            out.append(("account-id", m.group(0)))
    for m in SLACK_ID_RE.finditer(line):
        if not _slack_id_allowed(m.group(0)):
            out.append(("slack-id", m.group(0)))
    for m in WIN_HOME_RE.finditer(line):
        if not _home_allowed(m.group(0)):
            out.append(("home-path", m.group(0)))
    for m in CRED_RE.finditer(line):
        out.append(("credential", m.group(0)))
    if deny is not None:
        for m in deny.finditer(line):
            out.append(("denylist", m.group(0)))
    return out


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() in SKIP_SUFFIXES:
                continue
            yield p


def scan_file(path: Path, deny: re.Pattern[str] | None, root: Path) -> list[dict]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data[:8192]:
        return []
    text = data.decode("utf-8", errors="replace")
    rel = path.relative_to(root).as_posix()
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, match in scan_line(line, deny):
            findings.append({"path": rel, "line": lineno, "kind": kind, "match": match})
    return findings


def scan_tree(root: Path, deny: re.Pattern[str] | None) -> list[dict]:
    findings: list[dict] = []
    for p in iter_files(root):
        findings.extend(scan_file(p, deny, root))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fail on anything that looks private")
    ap.add_argument("root", nargs="?", default=None,
                    help="directory to scan (default: the repository root)")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    deny = denylist_pattern(os.environ.get("OSS_SCAN_WORDS"))
    findings = scan_tree(root, deny)

    if args.json:
        print(json.dumps({"root": str(root), "count": len(findings), "findings": findings},
                         indent=2))
    else:
        for f in findings:
            print(f"{f['path']}:{f['line']}: {f['kind']}: {f['match']}")
        if findings:
            files = len({f["path"] for f in findings})
            print(f"\n{len(findings)} finding(s) in {files} file(s) under {root}")
        else:
            print(f"clean: {root}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
