"""The dashboard's scripts: ordered classic files under otto/web/js, no build.

The load-order contract is the header of otto/web/js/00-shell.js: index.html loads
the files in name order after term.js, every top-level declaration is global, so
no name may be declared twice across files and nothing that runs at load time in
an earlier file may call into a later one. These checks hold the first two
mechanically (the JS analog of tests/test_cli_shadowing.py) and syntax-check each
file with node when node is on PATH. A browser is not part of the suite; the DOM
parity check lives in the split's own verification, not here.
"""

from __future__ import annotations

import collections
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "otto" / "web"
JS_DIR = WEB / "js"
INDEX = WEB / "index.html"

_SRC = re.compile(r'<script\s+src="/static/([^"]+)"')
_DECL = re.compile(r"^(?:async\s+function|function|class|const|let|var)\s+([A-Za-z_$][\w$]*)")


def _script_srcs() -> list[str]:
    return _SRC.findall(INDEX.read_text(encoding="utf-8"))


def _js_files() -> list[Path]:
    return sorted(JS_DIR.glob("*.js"))


def test_every_script_tag_resolves_to_a_file():
    srcs = _script_srcs()
    assert srcs, "index.html has no script tags"
    missing = [s for s in srcs if not (WEB / s).is_file()]
    assert not missing, f"index.html references scripts that do not exist: {missing}"


def test_js_directory_is_listed_completely_and_in_order():
    listed = [s.removeprefix("js/") for s in _script_srcs() if s.startswith("js/")]
    on_disk = [p.name for p in _js_files()]
    assert on_disk, "otto/web/js is empty"
    assert listed == on_disk, (
        "index.html must list every file in otto/web/js, in name order, nothing else:\n"
        f"  index.html: {listed}\n  on disk:    {on_disk}")
    # The split files come after term.js, which defines the OttoTerm global they use.
    srcs = _script_srcs()
    assert srcs.index("term.js") < srcs.index("js/" + listed[0])


def test_no_top_level_name_is_declared_in_two_files():
    """Classic scripts share one global scope, so a second declaration of the same
    name silently wins (let/const would throw at load). One home per name."""
    where: dict[str, list[str]] = collections.defaultdict(list)
    for p in _js_files():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = _DECL.match(line)
            if m:
                where[m.group(1)].append(p.name)
    dupes = {n: fs for n, fs in where.items() if len(fs) > 1}
    assert not dupes, f"top-level names declared in more than one file: {dupes}"
    # And the split did not lose the names the page is wired on.
    for must in ("render", "poll", "state", "mode", "$", "el"):
        assert must in where, f"{must} is not declared at top level in any file"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH; syntax check needs it")
def test_each_file_parses_with_node():
    for p in _js_files():
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"node --check {p.name}:\n{r.stderr}"


def test_first_file_states_the_load_order_contract():
    head = _js_files()[0].read_text(encoding="utf-8")[:2500]
    assert "LOAD ORDER CONTRACT" in head
    assert "index.html loads these files in name order" in head
    assert "Cache-Control: no-cache" in head
    # Every file says what it owns and what it relies on, so a reader can tell
    # whether moving a function would break the order.
    for p in _js_files():
        text = p.read_text(encoding="utf-8")[:4000]
        assert f"otto/web/js/{p.name}:" in text, f"{p.name} has no ownership header"
        assert "Relies on:" in text, f"{p.name} does not say what it relies on"
