"""Otto's own runs, inside herdr panes.

WHY. A detached run is a process with a log file: Otto can poll it, parse it,
and bill it, but the owner cannot look at it: `otto logs <id>` shows NDJSON.
herdr owns the terminals the owner already works in, so a run that happens in
a pane is one to watch live, scroll back through, and sit next to the owner's
own sessions, with no new window and nothing to find afterward.

WHAT STAYS THE SAME. Every contract the detached runner has is kept on purpose:
the run still has a log file with byte-exact NDJSON, a pid Otto polls with the
create-time check, a result object the ledger reads at exit, and the same
settle paths. The pane is a place to look, not a new runner. The Run records
pane_id and workspace_id so the dashboard and `otto live` can point at it.

HOW THE LOG IS KEPT EXACT. The obvious `2>&1 | Tee-Object -FilePath` was tried
first and rejected on this machine (2026-10-01): Windows PowerShell 5.1 writes
Tee-Object files as UTF-16LE with a BOM, which no reader of these logs expects,
and native stderr through `2>&1` arrives as ErrorRecords that the formatter
renders as five lines of CategoryInfo noise. So the tee is a short per-run
script: a UTF-8 StreamWriter with LF line endings, each pipeline object
stringified (an ErrorRecord's string is just its message), flushed per line so
activity.py can tail a live run. [Console]::OutputEncoding is set to UTF-8 in
the pane shell and in the child so non-ASCII in Claude's output survives both
hops; the probe log of `{"text": "cafe <check>"}` came back byte-identical.

HOW THE PID IS FOUND. `herdr pane process-info` reports the pane's shell pid and
its foreground processes, but while a pipeline runs it lists only the shell
(verified: python was alive and only powershell.exe was reported). So the run's
process is found as a psutil child of that shell whose command line carries a
token Otto chose (the launcher path, or `claude` for an interactive session).
That child is the same process the detached runner would have tracked, so
Run.pid, pid_created, _alive() and kill() mean exactly what they meant.

WHEN IT CANNOT. Anything that fails BEFORE the command is submitted to the
shell raises HerdrError, and detached.spawn falls back to the plain Popen path.
Nothing raises after submission: a run that is already typing into a pane must
not be launched a second time, so a pid that could not be resolved comes back
as None with the reason, and Otto's poll judges it off the log.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import psutil

from .. import config, herdr, safeargs
from ..models import Run

WORKSPACE_LABEL = "otto-runs"

# How long to wait for the pane shell to exist before typing into it, and for
# the run's process to show up as a child of that shell afterward.
_SHELL_WAIT = 8.0
_PID_WAIT = 10.0
_POLL = 0.25


def _q(value: Any) -> str:
    """Single-quote for PowerShell. Delegates to safeargs.ps_quote, which doubles
    the typographic single quotes too: PowerShell closes a string on U+2019, and
    run names are card titles."""
    return safeargs.ps_quote(value)


# ---- the workspace -------------------------------------------------------------

def workspace_id(snap: dict[str, Any] | None = None) -> str:
    """The id of the shared `otto-runs` workspace, created on first use.

    One workspace rather than one per run so the owner has a single place to look,
    and the tabs inside it are the runs. Found by label because ids are not
    stable across herdr server restarts.
    """
    for ws in herdr.workspaces(snap):
        if ws.get("label") == WORKSPACE_LABEL and ws.get("workspace_id"):
            return str(ws["workspace_id"])
    ws_id, _root = herdr.create_workspace(str(config.HOME), WORKSPACE_LABEL)
    return ws_id


def create_tab(ws_id: str, cwd: str, label: str, env: dict[str, str] | None = None,
               focus: bool = False) -> str:
    """A new tab (one pane) in the workspace. Returns the pane id.

    A tab per run, not a split: splits shrink every other run's view, and a tab
    keeps each run's scrollback whole. `--env` marks the pane shell itself, so a
    human typing into the pane later is under the same OTTO_* variables the run
    was.
    """
    args = ["tab", "create", "--workspace", ws_id, "--cwd", cwd, "--label", label,
            "--focus" if focus else "--no-focus", *herdr.PANE_ENV_ARGS]
    for k, v in (env or {}).items():
        args += ["--env", f"{k}={v}"]
    r = herdr._run(args)
    pane = (r.get("root_pane") or {}).get("pane_id")
    if not pane:
        raise herdr.HerdrError("no_pane", f"tab create returned no pane: {r}")
    return str(pane)


def process_info(pane_id: str) -> dict[str, Any]:
    r = herdr._run(["pane", "process-info", "--pane", pane_id], timeout=10)
    return dict(r.get("process_info") or r)


def _wait_shell(pane_id: str, timeout: float = _SHELL_WAIT) -> int:
    """The pane shell's pid once the shell exists, so a command typed into the
    pane lands in a shell rather than in a PTY nobody is reading yet."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            info = process_info(pane_id)
            pid = info.get("shell_pid")
            if pid:
                return int(pid)
        except herdr.HerdrError as e:
            last = e
        time.sleep(_POLL)
    raise herdr.HerdrError("no_shell", f"pane {pane_id} has no shell after {timeout:.0f}s"
                           + (f" ({last})" if last else ""))


def _children_of(shell_pid: int) -> list[tuple[int, str]]:
    """(pid, cmdline) for every live descendant of the pane shell."""
    out: list[tuple[int, str]] = []
    try:
        parent = psutil.Process(shell_pid)
        for c in parent.children(recursive=True):
            try:
                out.append((c.pid, " ".join(c.cmdline() or [])))
            except psutil.Error:
                continue
    except psutil.Error:
        return out
    return out


def resolve_pid(pane_id: str, shell_pid: int, match: str | None,
                timeout: float = _PID_WAIT) -> tuple[int | None, str | None]:
    """The pid of the run's process under the pane shell, or (None, why).

    Foreground processes from process-info are checked first (they carry a
    cmdline), then psutil descendants of the shell, because process-info does
    not list a pipeline's child. A command that finished inside the window
    leaves no child at all; that is reported, not raised, since the work has
    happened and the log says how it went.
    """
    needle = (match or "").lower()
    deadline = time.time() + timeout
    newest: tuple[int, str] | None = None
    while time.time() < deadline:
        cands: list[tuple[int, str]] = []
        try:
            info = process_info(pane_id)
            for p in info.get("foreground_processes") or []:
                pid = p.get("pid")
                if pid and int(pid) != shell_pid:
                    cands.append((int(pid), str(p.get("cmdline") or "")))
        except herdr.HerdrError:
            pass
        cands += _children_of(shell_pid)
        for pid, cmdline in cands:
            if not needle or needle in cmdline.lower():
                return pid, None
        if cands:
            newest = cands[-1]
        time.sleep(_POLL)
    if newest is not None:
        # Something is running under the shell but nothing matched the token.
        # Track it anyway: wrong-but-alive beats "no pid", which would read the
        # run as finished while it is still working.
        return newest[0], f"pid matched by position, not by token {match!r}"
    return None, "no process under the pane shell; the command may have already finished"


# ---- headless: a command teed to the log ---------------------------------------

def _tee_script(cmdline: list[str] | str, log_path: Path, env: dict[str, str] | None) -> Path:
    """Write the per-run pane script. See the module docstring for why this is
    not Tee-Object."""
    if isinstance(cmdline, str):
        invoke = cmdline
    else:
        invoke = "& " + " ".join(_q(c) for c in cmdline)
    lines = [
        "$ErrorActionPreference = 'Continue'",
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)",
        # The console encoding above covers PowerShell's own output. A Python child
        # picks its stdout encoding from the environment, not the console, and on a
        # daemon that was started without PYTHONUTF8 that is cp1252: the first
        # non-Latin character in a run's output (a check mark in a test summary)
        # raised UnicodeEncodeError and the run exited 1. Every child in a pane gets
        # UTF-8 regardless of who launched the daemon.
        "$env:PYTHONUTF8 = '1'",
        "$env:PYTHONIOENCODING = 'utf-8'",
    ]
    for k, v in (env or {}).items():
        # The name is written into the script unquoted, so it must be a name.
        if not safeargs.is_env_name(k):
            raise herdr.HerdrError("bad_env", f"not an environment variable name: {k[:64]!r}")
        lines.append(f"$env:{k} = {_q(v)}")
    lines += [
        f"$__w = [IO.StreamWriter]::new({_q(log_path)}, $false, [Text.UTF8Encoding]::new($false))",
        "$__w.NewLine = \"`n\"",
        "try {",
        f"  {invoke} 2>&1 | ForEach-Object {{ $__s = \"$_\"; $__w.WriteLine($__s); $__w.Flush(); $__s }}",
        "} finally { $__w.Dispose() }",
        "\"otto: run exited $LASTEXITCODE\"",
    ]
    script = log_path.with_name(log_path.name + ".pane.ps1")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script


class Launched(NamedTuple):
    pane_id: str
    pid: int | None
    workspace_id: str
    note: str | None = None


def launch_in_pane(cmdline: list[str] | str, cwd: str, log_path: str | Path,
                   env: dict[str, str] | None, label: str,
                   match: str | None = None) -> Launched:
    """Run a command in a fresh pane of the otto-runs workspace, output teed to
    log_path byte-exact. Returns (pane_id, pid, workspace_id, note).

    `match` is a token the run's process has in its command line; it is how the
    pid is told apart from anything else under the pane shell. Defaults to the
    first element of the command line.
    """
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    if match is None:
        match = cmdline if isinstance(cmdline, str) else (cmdline[0] if cmdline else None)
    ws = workspace_id()
    pane = create_tab(ws, cwd, label, env)
    shell_pid = _wait_shell(pane)
    script = _tee_script(cmdline, log, env)
    # From here on nothing raises: the command is in the shell.
    herdr.run_in_pane(pane, f"& {_q(script)}")
    pid, note = resolve_pid(pane, shell_pid, match)
    return Launched(pane, pid, ws, note)


# ---- attended: an interactive claude, prompted once it is up -------------------

class LaunchedClaude(NamedTuple):
    pane_id: str
    pid: int | None
    workspace_id: str
    prompted: bool
    note: str | None = None


def launch_claude_interactive(cwd: str, args: list[str], prompt_text: str, label: str,
                              env: dict[str, str] | None = None,
                              timeout: float | None = None) -> LaunchedClaude:
    """An interactive `claude` in its own pane, with the run's prompt submitted
    through `herdr agent prompt` once herdr has detected the agent.

    `claude` is typed rather than started with `agent start` for the Windows
    reason in otto/herdr.py. The pane is focused because an attended run exists
    for the owner to sit at. If claude never reaches idle within the timeout the pane
    is closed and HerdrError raised: nothing has been prompted yet, so nothing
    is lost, and the caller may fall back to the console path.
    """
    ws = workspace_id()
    pane = create_tab(ws, cwd, label, env, focus=True)
    shell_pid = _wait_shell(pane)
    herdr.run_in_pane(pane, "claude " + " ".join(_q(a) for a in args) if args else "claude")
    deadline = time.time() + (timeout or config.HERDR_START_TIMEOUT)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = herdr.agent_for_pane(pane)
        if last and last.get("agent") == "claude" and last.get("agent_status") in ("idle", "done"):
            break
        time.sleep(1.0)
    else:
        try:
            close_pane(pane)
        except herdr.HerdrError:
            pass
        raise herdr.HerdrError(
            "agent_not_ready",
            f"claude did not come up in {pane} within the timeout"
            + (f" (last status {last.get('agent_status')})" if last else ""))
    pid, note = resolve_pid(pane, shell_pid, "claude", timeout=3.0)
    prompted = True
    try:
        herdr.prompt(pane, prompt_text)
    except herdr.HerdrError as e:
        prompted = False
        note = f"prompt not delivered ({e.code}); it is in the run's prompt file"
    return LaunchedClaude(pane, pid, ws, prompted, note)


# ---- lifecycle ----------------------------------------------------------------

def close_pane(pane_id: str) -> None:
    herdr._run(["pane", "close", pane_id], timeout=10)


def _ended_minutes_ago(run: Run) -> float | None:
    if not run.ended:
        return None
    try:
        when = datetime.fromisoformat(run.ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds() / 60


_last_sweep = 0.0
SWEEP_EVERY_SECONDS = 300


def sweep(store, keep_minutes: int | None = None, force: bool = False) -> list[str]:
    """Close the panes of runs that ended more than keep_minutes ago. Tick hook.

    A finished run's pane stays for scrollback until then. Never closes a pane
    whose process is still alive, whatever the run record says: the record can
    be wrong, the process cannot. Runs whose pane herdr no longer lists just
    lose the pointer. Costs nothing on a tick with no candidates; one snapshot
    otherwise, at most every SWEEP_EVERY_SECONDS.
    """
    global _last_sweep
    if not force and time.time() - _last_sweep < SWEEP_EVERY_SECONDS:
        return []
    _last_sweep = time.time()
    keep = config.HERDR_RUNS_KEEP_MINUTES if keep_minutes is None else keep_minutes
    # Imported here, not at module top: detached imports this module to launch,
    # and the sweep only needs its liveness check.
    from . import detached

    notes: list[str] = []
    try:
        with store.lock:
            runs = store.runs()
            candidates = [r for r in runs if r.pane_id and r.status != "running"
                          and (_ended_minutes_ago(r) or -1) >= keep]
            if not candidates:
                return []
            if not herdr.available():
                return []
            snap = herdr.snapshot()
            if snap is None:
                return []
            live_panes = {p.get("pane_id") for p in herdr.panes(snap)}
            changed = False
            for r in candidates:
                if detached._alive(r):
                    continue
                if r.pane_id in live_panes:
                    try:
                        close_pane(r.pane_id)
                    except herdr.HerdrError as e:
                        notes.append(f"could not close pane {r.pane_id} of {r.name}: {e}")
                        continue
                    notes.append(f"closed pane {r.pane_id} of {r.name} ({r.status})")
                r.notes = ((r.notes or "") + f" | pane {r.pane_id} closed").strip(" |")
                r.pane_id = None
                changed = True
            if changed:
                store.save_runs(runs)
    except Exception as e:  # noqa: BLE001 - a tick hook must never take the tick down
        notes.append(f"pane sweep error: {e}")
    return notes
