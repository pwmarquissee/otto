"""Detached Claude Code sessions.

This closes the gap in orchestrate.md, which spawns app-developer with
Start-Process and states outright that the orchestrator cannot see its output.
Here every spawn is recorded with a PID, a captured log, and a liveness check,
so an agent that dies, hangs, or never reports is visible instead of lost.

Two modes:
  headless     `claude -p` with stdout/stderr piped to a log file. Default,
               because it is the mode that actually produces observability.
  windowed     a visible console (Windows) that tees to the same log. Use when
               a human wants to watch or interact with the session.

The per-run script itself, and the shell that runs it, are otto/launcher.py's.

PID reuse is real on Windows, so a run stores both pid and the process
create_time. Liveness requires both to match before Otto believes the process
it sees is the one it started.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import psutil

from .. import config, herdr, launcher, safeargs, transcript
from ..models import Run, iso, utcnow
from . import herdrpane

# Windows creation flags.
#
# DETACHED_PROCESS (0x8) was the obvious choice and is WRONG here: a child with no
# console silently discards its output, so every captured log came back empty.
# Verified empirically. Windows children already survive parent exit (there is no
# job object binding them to the daemon), so detaching bought nothing.
_NEW_GROUP = 0x00000200
_NO_WINDOW = 0x08000000   # headless: no console window, redirection still works
_NEW_CONSOLE = 0x00000010  # windowed: a real console the human can watch

_FLAGS_HEADLESS = (_NEW_GROUP | _NO_WINDOW) if os.name == "nt" else 0
_FLAGS_WINDOWED = (_NEW_GROUP | _NEW_CONSOLE) if os.name == "nt" else 0


def _log_path(run_id: str, name: str) -> Path:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return config.LOG_DIR / f"{run_id[:6]}-{safe}.log"


def _resolve_agent_md(agent: str | None) -> Path | None:
    if not agent:
        return None
    # The name is joined into a path under ~/.claude/agents; "../x" would read any
    # .md on disk into the session's system prompt. Unknown shape reads as "no
    # such agent", which spawn() already reports.
    if not safeargs.is_agent_def(agent):
        return None
    p = config.CLAUDE_DIR / "agents" / f"{agent}.md"
    return p if p.is_file() else None


def _write_launcher(
    run_id: str,
    name: str,
    cwd: str,
    prompt_file: Path,
    agent_md: Path | None,
    log: Path,
    mode: str,
    skip_permissions: bool,
    budget_usd: float | None = None,
    system_extra: str | None = None,
    model: str | None = None,
    local_mcp: bool = True,
) -> Path:
    """Describe this run's `claude` invocation and write its launcher script.

    Why a script instead of a direct argv: on Windows `claude` is an npm shim
    (claude.CMD), which CreateProcess cannot invoke reliably, and both the prompt
    and an --append-system-prompt agent definition are far too long and too full
    of quotes to survive a command line. The launcher reads both from disk, so
    nothing large or quoted ever crosses a process boundary as an argument.
    otto/launcher.py renders the script for the platform.
    """
    env: dict[str, str] = {}
    args: list[str] = ["-p"]
    if mode != "windowed":
        # Headless means nobody is watching, and scripts/otto_guard.py keys off
        # exactly that: with this set, the PreToolUse hook refuses outreach sends
        # and credential checkout no matter what permission flags the session holds
        # (the owner's call, 2026-08-07). Windowed sessions have a human present, so
        # they are deliberately not marked.
        env["OTTO_UNATTENDED"] = "1"
        # Named so the guard's approval dialog can say WHICH run is asking
        # (since 2026-08-18 gated actions raise a modal instead of a flat
        # deny, and an unlabeled yes/no about a credential is unanswerable).
        env["OTTO_RUN_NAME"] = name
        # The id, for provenance: `otto dm` records it against the message it sends
        # and outreach.directed keys its per-run brake on it.
        env["OTTO_RUN_ID"] = run_id
        # stream-json, not json. Plain `json` buffers everything and writes one
        # object at exit, so the log is EMPTY for the entire life of the run and
        # "watch a running agent" is impossible. stream-json emits an NDJSON event
        # per step as it happens, and its final line is still the same
        # {"type":"result"} object, so harvesting is unaffected.
        # --verbose is required for stream-json under --print.
        args += ["--output-format", "stream-json", "--verbose"]
        if budget_usd:
            args += ["--max-budget-usd", str(budget_usd)]
    if model:
        # Per-run model choice, because the floor cost of a session is not the same
        # for every job. A spawned session pays ~38k cache-creation tokens for its
        # system context before it does anything, so a small bounded task (pick one
        # of four verbs, run one command) costs the same as a hard one on a premium
        # model. Letting the caller pick makes the cheap jobs cheap.
        args += ["--model", model]
    if not local_mcp:
        # An EMPTY config plus --strict-mcp-config, which is what drops the local
        # servers. Deliberately not a subset mechanism: naming servers to keep would
        # mean copying their definitions (including credentials) out of ~/.claude.json
        # into a file under LOG_DIR, and no workload needs that yet. See
        # config.MCP_FREE_COMMANDS for the measurement and the safety check.
        #
        # --disallowed-tools is NOT a substitute. It blocks calling a tool but the
        # definitions are still loaded into context, which is the thing that costs
        # money here (outreach.py's SEND_TOOL comment learned this the same way).
        empty = config.LOG_DIR / f"{run_id[:6]}-{name}.mcp.json"
        empty.write_text('{"mcpServers":{}}', encoding="utf-8")
        args += ["--strict-mcp-config", "--mcp-config", str(empty)]
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    # ONE file, passed by PATH with --append-system-prompt-file, never by value.
    #
    # It used to be two --append-system-prompt arguments carrying the text itself.
    # Windows PowerShell 5.1 hands a native exe an argument with embedded double
    # quotes unescaped, so the receiving side sees the value end at the first inner
    # quote and the rest as new arguments. Found 2026-09-03 when the first card-reply
    # session died with `error: unknown option` naming a fragment of the card's own
    # detail: its system context carried a card whose detail had quotes in it. The
    # DM path had only ever passed prose without quotes, so it looked fine. Probed
    # the same day: a plain embedded quote is silently STRIPPED and a backslash
    # before a quote SPLITS the argument. Escaping was tried and PowerShell's own
    # rewriting made it unreliable; a path has no quotes in it.
    #
    # Agent definition first, then the run's extra context, so the extra reads as an
    # addendum to the role rather than the other way round.
    parts: list[str] = []
    if agent_md:
        parts.append(Path(agent_md).read_text(encoding="utf-8"))
    if system_extra:
        parts.append(system_extra)
    if parts:
        extra_file = config.LOG_DIR / f"{run_id[:6]}-{name}.sysextra.txt"
        extra_file.write_text("\n\n".join(parts), encoding="utf-8")
        args += ["--append-system-prompt-file", str(extra_file)]

    spec = launcher.ClaudeSpec(
        cwd=str(cwd), prompt_file=prompt_file, args=tuple(args), env=env,
        # Visible console: tee so the human watches live and Otto still gets a log.
        tee_log=log if mode == "windowed" else None,
    )
    return launcher.write_claude(config.LOG_DIR / f"{run_id[:6]}-{name}", spec)


def spawn(
    name: str,
    prompt: str,
    cwd: str,
    agent: str | None = None,
    mode: str = "headless",
    task_id: str | None = None,
    tier: str | None = None,
    skip_permissions: bool = True,
    domain: str | None = None,
    budget_usd: float | None = None,
    system_extra: str | None = None,
    model: str | None = None,
    local_mcp: bool = True,
) -> Run:
    """Launch a Claude Code session and return a tracked Run."""
    if shutil.which("claude") is None:
        raise ValueError("`claude` not found on PATH - cannot spawn a session")

    workdir = Path(cwd)
    if not workdir.is_dir():
        raise ValueError(f"cwd does not exist: {cwd}")

    run_id = uuid.uuid4().hex
    log = _log_path(run_id, name)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    prompt_file = config.LOG_DIR / f"{run_id[:6]}-{safe_name}.prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    agent_md = _resolve_agent_md(agent)
    if agent and agent_md is None:
        raise ValueError(f"no agent definition at ~/.claude/agents/{agent}.md")

    script = _write_launcher(
        run_id, safe_name, cwd, prompt_file, agent_md, log, mode, skip_permissions,
        budget_usd, system_extra, model, local_mcp,
    )

    notes = f"mode={mode}" + (f" agent={agent}" if agent else "")
    pid: int | None = None
    pane_id: str | None = None
    workspace_id: str | None = None
    run_log: str | None = str(log)

    # Inside a herdr pane when the harness is up, so the run can be watched; the
    # process, the log and the result object are the same either way. Anything
    # herdr refuses BEFORE the command is in a shell drops to the plain process
    # below, and the run says so. See otto/runners/herdrpane.py.
    wanted, pane_note = _pane_wanted()
    if wanted:
        try:
            if mode == "windowed":
                iargs = _interactive_args(run_id, safe_name, skip_permissions, model, local_mcp)
                launched = herdrpane.launch_claude_interactive(
                    cwd, iargs, prompt, label=name,
                    env={"OTTO_RUN_NAME": name, "OTTO_RUN_ID": run_id})
                cmd = ["claude", *iargs]
                # An interactive TUI is not a log; usage comes off the transcript,
                # exactly as it does for a console window.
                run_log = None
                if not launched.prompted:
                    notes += f" | {launched.note}"
            else:
                cmd = launcher.pane_command(script)
                launched = herdrpane.launch_in_pane(
                    cmd, cwd, log, _run_env(run_id, name, mode), label=name,
                    match=str(script))
            pid, pane_id, workspace_id = launched.pid, launched.pane_id, launched.workspace_id
            notes += " | pane"
            if launched.note:
                notes += f" | {launched.note}"
        except herdr.HerdrError as e:
            pane_note = f"herdr unavailable, ran detached ({e.code})"
            pid = pane_id = workspace_id = None
            run_log = str(log)
    if pane_note:
        notes += f" | {pane_note}"

    if pane_id is None:
        if mode == "windowed":
            cmd = launcher.command(script, keep_open=True)
            proc = subprocess.Popen(cmd, cwd=cwd, creationflags=_FLAGS_WINDOWED)
        else:
            cmd = launcher.command(script)
            fh = log.open("w", encoding="utf-8", errors="replace")
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=_FLAGS_HEADLESS,
                    shell=False,
                )
            finally:
                # The child holds its own duplicate of the handle; keeping the
                # parent's copy open would leak one per spawn.
                fh.close()
        pid = proc.pid

    created = None
    if pid is not None:
        try:
            created = psutil.Process(pid).create_time()
        except psutil.Error:
            created = None

    return Run(
        id=run_id,
        name=name,
        runner="detached",
        status="running",
        domain=(domain or config.domain_for_path(cwd)),  # type: ignore[arg-type]
        pid=pid,
        pid_created=created,
        cwd=cwd,
        cmd=[str(c) for c in cmd],
        log=run_log,
        task_id=task_id,
        tier=tier,
        model=model,
        agent=agent,
        notes=notes,
        pane_id=pane_id,
        workspace_id=workspace_id,
    )


def _pane_wanted() -> tuple[bool, str | None]:
    """Whether this spawn goes into a herdr pane, and if not, why (None when the
    feature is simply off: that is the old behavior and needs no note)."""
    if not config.HERDR_RUNS:
        return False, None
    if not herdr.available():
        return False, "herdr unavailable, ran detached (not installed)"
    if not herdr.server_running():
        return False, "herdr unavailable, ran detached (server down)"
    return True, None


def _run_env(run_id: str, name: str, mode: str) -> dict[str, str]:
    """The per-run variables the launcher sets, mirrored onto the pane shell so
    a human who types into that pane afterward is under the same marks."""
    env = {"OTTO_RUN_NAME": name, "OTTO_RUN_ID": run_id}
    if mode != "windowed":
        env["OTTO_UNATTENDED"] = "1"
    return env


def _interactive_args(run_id: str, safe_name: str, skip_permissions: bool,
                      model: str | None, local_mcp: bool) -> list[str]:
    """The windowed launcher's claude arguments, minus `-p`: an attended pane
    runs a real interactive session and gets its prompt through herdr. The side
    files are the ones _write_launcher already wrote for this run."""
    args: list[str] = []
    if model:
        args += ["--model", model]
    if not local_mcp:
        args += ["--strict-mcp-config", "--mcp-config",
                 str(config.LOG_DIR / f"{run_id[:6]}-{safe_name}.mcp.json")]
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    extra_file = config.LOG_DIR / f"{run_id[:6]}-{safe_name}.sysextra.txt"
    if extra_file.is_file():
        args += ["--append-system-prompt-file", str(extra_file)]
    return args


def _alive(run: Run) -> bool:
    """True only if the stored pid AND create_time still match a live process."""
    if run.pid is None:
        return False
    try:
        p = psutil.Process(run.pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return False
        if run.pid_created is not None and abs(p.create_time() - run.pid_created) > 1.0:
            return False  # PID reused by an unrelated process
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
        return False


def _parse_result_json(run: Run) -> dict | None:
    """Pull Claude Code's JSON result object out of a headless run's log.

    The log is normally exactly one JSON object, but a warning line on stderr can
    precede it, so fall back to scanning lines from the end.
    """
    if not run.log:
        return None
    p = Path(run.log)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None

    for candidate in (raw, *reversed(raw.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


_EXIT_MARKER = re.compile(r"^OTTO_EXIT=(-?\d+)\s*$", re.MULTILINE)


def _shell_exit_code(run: Run) -> int | None:
    """Read the exit code a shell launcher printed as its last act."""
    if not run.log:
        return None
    p = Path(run.log)
    if not p.is_file():
        return None
    try:
        tail = p.read_text(encoding="utf-8", errors="replace")[-2000:]
    except OSError:
        return None
    found = _EXIT_MARKER.findall(tail)
    return int(found[-1]) if found else None


def _apply_result(run: Run, obj: dict) -> None:
    """Copy authoritative usage and outcome off the JSON result."""
    usage = obj.get("usage") or {}
    tin = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    if tin:
        run.input_tokens = tin
    if usage.get("output_tokens"):
        run.output_tokens = int(usage["output_tokens"])
    if isinstance(obj.get("total_cost_usd"), (int, float)):
        run.cost_usd = float(obj["total_cost_usd"])
    if obj.get("session_id"):
        run.session_id = str(obj["session_id"])
    if isinstance(obj.get("num_turns"), int):
        run.num_turns = obj["num_turns"]
    result = obj.get("result")
    if isinstance(result, str) and result.strip():
        run.result_summary = result.strip()[:400]

    if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
        run.status = "failed"
        run.exit_code = 1
        err = obj.get("api_error_status") or obj.get("subtype") or "error"
        run.notes = (run.notes or "") + f" | {err}"
        # Classify, do not just record. The 529 was already landing in `notes` and
        # every consumer still read the run as a plain failure, so the information
        # was present and unusable. `terminal_reason` is Claude Code's own verdict
        # on why the session ended; an api_error means the command never got a fair
        # chance to run, which is a different fact from the command being wrong.
        if (obj.get("terminal_reason") == "api_error"
                or obj.get("api_error_status") is not None):
            run.error_kind = "api"
        elif obj.get("terminal_reason") == "budget_exhausted":
            # Same argument as `api` one branch up, and found the same way: a
            # thread-note run was killed mid-tool-call by a budget set below the
            # floor cost of a spawned session, and reported only as "failed" with
            # an empty summary. The command was correct and had already been
            # chosen; nothing about it was wrong. Without this the reader cannot
            # tell "the cap was too low" from "the task was broken", and the fix
            # for each is the opposite of the other.
            run.error_kind = "budget"
            if not run.result_summary:
                errs = obj.get("errors") or []
                detail = str(errs[0]) if errs else "budget exhausted"
                run.result_summary = (
                    f"stopped by the spend cap before it could finish: {detail}. "
                    "Raise the cap or use a cheaper model; the work itself was not "
                    "judged."
                )
    else:
        run.status = "ok"
        run.exit_code = 0
        # Cleared on success, so a run object reused across a retry cannot carry a
        # stale verdict from the attempt before it.
        run.error_kind = None


def poll(run: Run) -> Run:
    """Refresh a run's status and usage. Returns the (possibly) updated run."""
    if run.status != "running":
        return run

    windowed = "mode=windowed" in (run.notes or "")

    if _alive(run):
        # Interactive sessions have no JSON result until they exit, so live usage
        # for those comes from the transcript, bound strictly to this run.
        if windowed:
            u = transcript.usage_for_run(run.cwd, run.pid_created)
            if u["input_tokens"]:
                run.input_tokens = int(u["input_tokens"])
            if u["output_tokens"]:
                run.output_tokens = int(u["output_tokens"])
            if u["cost_usd"] is not None:
                run.cost_usd = float(u["cost_usd"])
        return run

    run.ended = iso(utcnow())

    obj = _parse_result_json(run)
    if obj is not None:
        _apply_result(run, obj)
        return run

    # A plain shell run produces no result object, so its launcher prints its exit
    # code instead. Without this every successful shell run would land on the
    # `orphaned` fallback below.
    code = _shell_exit_code(run)
    if code is not None:
        run.exit_code = code
        run.status = "ok" if code == 0 else "failed"
        if code != 0:
            run.notes = (run.notes or "") + f" | exit {code}"
        return run

    if windowed:
        u = transcript.usage_for_run(run.cwd, run.pid_created)
        if u["input_tokens"]:
            run.input_tokens = int(u["input_tokens"])
        if u["output_tokens"]:
            run.output_tokens = int(u["output_tokens"])
        if u["cost_usd"] is not None:
            run.cost_usd = float(u["cost_usd"])

    # No JSON result and the process is gone. Otto did not wait on this child so
    # it has no exit code, and it refuses to guess success. `orphaned` is the
    # honest answer and is exactly the signal that used to be invisible.
    run.status = "orphaned"
    run.notes = (run.notes or "") + " | process gone with no result object"
    return run


def kill(run: Run) -> Run:
    """Terminate a tracked run's process tree."""
    if not _alive(run):
        run.status = "orphaned" if run.status == "running" else run.status
        run.ended = run.ended or iso(utcnow())
        return run
    try:
        parent = psutil.Process(run.pid)  # type: ignore[arg-type]
        for child in parent.children(recursive=True):
            child.terminate()
        parent.terminate()
        _, alive = psutil.wait_procs([parent], timeout=5)
        for p in alive:
            p.kill()
    except psutil.Error as e:
        run.notes = (run.notes or "") + f" | kill failed: {e}"
        return run
    run.status = "killed"
    run.ended = iso(utcnow())
    return run


def tail_log(run: Run, lines: int = 80) -> str:
    if not run.log:
        return "(no log)"
    p = Path(run.log)
    if not p.is_file():
        return f"(log missing: {run.log})"
    try:
        content = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(log unreadable: {e})"
    return "\n".join(content[-lines:]) or "(log empty)"
