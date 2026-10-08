"""Guards for the otto/cli package.

`otto status` once crashed in production because `_print_sessions` was defined
twice in the flat cli.py: the one-line summary for status and the full table for
`otto sessions`. Python keeps the last definition, so the status call site got
the wrong signature. A duplicate def is always a bug in a CLI module, and now
that the verbs live in one module each, so is a cmd_ or add_ name that two
modules both define: the parser assembly would silently take one of them.
"""
import argparse
import ast
import collections
import os
import pathlib
import subprocess
import sys

import pytest

import otto.cli
from otto import assistant

PKG = pathlib.Path(otto.cli.__file__).parent
MODULES = sorted(p for p in PKG.glob("*.py") if p.name != "__init__.py")


def _defs(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_the_package_has_modules():
    assert len(MODULES) >= 10, [p.name for p in MODULES]


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_duplicate_module_level_defs(path):
    names = collections.Counter(_defs(path))
    dupes = sorted(n for n, c in names.items() if c > 1)
    assert not dupes, f"duplicate module-level defs in otto/cli/{path.name}: {dupes}"


def test_no_command_or_parser_defined_in_two_modules():
    """A cmd_ or add_ name in two modules is the same bug across files."""
    where: dict[str, list[str]] = collections.defaultdict(list)
    for path in MODULES:
        for name in _defs(path):
            if name.startswith(("cmd_", "add_")):
                where[name].append(path.name)
    dupes = {n: mods for n, mods in where.items() if len(mods) > 1}
    assert not dupes, f"defined in more than one cli module: {dupes}"


def test_every_module_under_the_size_the_split_was_for():
    """The flat file was 4,650 lines. If a module grows past this, split it again."""
    big = {p.name: sum(1 for _ in p.open(encoding="utf-8")) for p in MODULES}
    over = {n: c for n, c in big.items() if c > 700}
    assert not over, over


def _walk(parser, inherited=None):
    """(parser, fn) pairs; a subparser without its own fn inherits its parent's,
    which is how argparse fills the namespace (`otto telemetry status`)."""
    fn = parser._defaults.get("fn", inherited)
    yield parser, fn
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _walk(child, fn)


def test_every_parser_fn_default_is_a_function_in_the_package():
    """Every parser that sets `fn` points at a real callable from otto.cli, and
    every leaf parser (no subparsers of its own) has one, by itself or inherited,
    else `otto <verb>` would raise AttributeError on args.fn."""
    seen = 0
    for parser, fn in _walk(otto.cli.build_parser()):
        own = parser._defaults.get("fn")
        if parser._subparsers is None and parser.prog != "otto":
            assert fn is not None, f"{parser.prog} has no fn"
        if own is not None:
            seen += 1
            assert callable(own), parser.prog
            mod = getattr(own, "__module__", "")
            assert mod.startswith("otto.cli"), f"{parser.prog}: fn from {mod}"
    assert seen > 60, seen


def test_order_lists_cover_every_parser_once():
    adders = otto.cli._parsers()
    assert set(adders) == set(otto.cli.ORDER) | set(otto.cli.ASSISTANT_ORDER)
    assert len(otto.cli.ORDER) == len(set(otto.cli.ORDER))
    assert not set(otto.cli.ORDER) & set(otto.cli.ASSISTANT_ORDER)
    assert set(otto.cli.ASSISTANT_ORDER) == set(assistant.COMMANDS)


def test_help_lists_verbs_in_the_recorded_order():
    """The flat file's order, kept on purpose so `otto --help` reads as it always
    has. Run in a subprocess under the core scope so the assistant verbs stay out."""
    env = {**os.environ, "OTTO_SCOPE": "core", "OTTO_NO_TOAST": "1"}
    out = subprocess.run([sys.executable, "-m", "otto", "--help"], capture_output=True,
                         text=True, env=env, cwd=str(PKG.parent.parent)).stdout
    listed = out[out.index("{") + 1:out.index("}")].split(",")
    assert listed == otto.cli.ORDER
