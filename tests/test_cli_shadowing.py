"""Guard against two module-level functions in cli.py sharing a name.

`otto status` crashed in production because `_print_sessions` was defined
twice: the one-line summary for status and the full table for `otto sessions`.
Python keeps the last definition, so the status call site got the wrong
signature. A duplicate def is always a bug in a flat CLI module.
"""
import ast
import collections
import pathlib

import otto.cli


def test_cli_has_no_duplicate_module_level_defs():
    src = pathlib.Path(otto.cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = collections.Counter(
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    dupes = sorted(n for n, c in names.items() if c > 1)
    assert not dupes, f"duplicate module-level defs in otto/cli.py: {dupes}"
