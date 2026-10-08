"""The one per-run launcher, with a platform backend.

Every session Otto starts, and every schedule that is a shell command, runs
through a small script written into LOG_DIR for that run, never through a direct
argv. The reasons are Windows ones and they still hold: `claude` there is an npm
shim (claude.CMD) that CreateProcess cannot invoke reliably, and the prompt and
the system-prompt files are far too long and too full of quotes to survive a
command line. The script reads everything from disk, so nothing large or quoted
crosses a process boundary as an argument. Windows PowerShell 5.1 also strips or
splits embedded double quotes handed to a native exe, which is why every text
value reaches `claude` as a file path (`--append-system-prompt-file`) and never
by value; see otto/runners/detached.py for the day that was found.

Eight call sites used to each write their own PowerShell by hand and each shell
out to `powershell -NoProfile -ExecutionPolicy Bypass -File`. They now describe
the run (`ClaudeSpec`) and this module renders it for the platform:

  Windows   `<stem>.launch.ps1`, run by Windows PowerShell, or by pwsh when
            OTTO_POWERSHELL names it (PowerShell 7 is the cheap way onto Linux
            and the measured-first path in docs/otto-on-ec2.md).
  POSIX     `<stem>.launch.sh`, run by bash. Same shape: cd, exports, claude
            with its argument list and the prompt on stdin, the exit code
            carried out.

What is deliberately NOT abstracted: a schedule whose command is a shell string
is written in the shell it will run in, so a PowerShell one-liner stays a
Windows schedule. The process creation flags (no window, new console) live with
the spawners, because they are about the parent, not the script.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import safeargs

WINDOWS = os.name == "nt"
SUFFIX = ".launch.ps1" if WINDOWS else ".launch.sh"

# The PowerShell that runs a launcher. Windows PowerShell 5.1 is what every Windows
# box has; `pwsh` (PowerShell 7) is a choice, not a default, because 5.1's quoting
# behavior is what the sites were measured against.
POWERSHELL = os.environ.get("OTTO_POWERSHELL", "").strip() or "powershell"
_PS_BASE = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"]


@dataclass(frozen=True)
class ClaudeSpec:
    """One `claude` invocation: where, with which arguments, under which marks.

    `args` is the complete argument list (`-p`, output format, model, budget,
    tool allow or deny lists, prompt-file flags). The prompt itself arrives on
    stdin from `prompt_file`, which is the only way a long quoted prompt gets in
    intact on Windows. `env` is written into the script as assignments so the
    pane or console that runs it carries the same marks (OTTO_UNATTENDED,
    OTTO_RUN_ID) as the process. `tee_log` makes a windowed run both visible and
    logged.
    """
    cwd: str
    prompt_file: Path
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    tee_log: Path | None = None


def _check_env(env: dict[str, str]) -> None:
    for k in env:
        # The name is written into the script unquoted, so it must be a name.
        if not safeargs.is_env_name(k):
            raise ValueError(f"not an environment variable name: {k[:64]!r}")


def _render_ps(spec: ClaudeSpec) -> str:
    q = safeargs.ps_quote
    lines = [
        "$ErrorActionPreference = 'Continue'",
        f"Set-Location -LiteralPath {q(spec.cwd)}",
    ]
    lines += [f"$env:{k} = {q(v)}" for k, v in spec.env.items()]
    lines += [
        f"$prompt = Get-Content -LiteralPath {q(spec.prompt_file)} -Raw",
        "$claudeArgs = @(" + ", ".join(q(a) for a in spec.args) + ")",
    ]
    invoke = "$prompt | & claude @claudeArgs"
    if spec.tee_log is not None:
        # Visible console: tee so the human watches live and Otto still gets a log.
        invoke += f" 2>&1 | Tee-Object -FilePath {q(spec.tee_log)}"
    lines += [invoke, "exit $LASTEXITCODE"]
    return "\n".join(lines) + "\n"


def _render_sh(spec: ClaudeSpec) -> str:
    q = shlex.quote
    lines = [
        "#!/usr/bin/env bash",
        "# written by otto for one run; safe to delete once the run is over",
        f"cd -- {q(spec.cwd)} || exit 1",
    ]
    lines += [f"export {k}={q(v)}" for k, v in spec.env.items()]
    invoke = "claude " + " ".join(q(a) for a in spec.args) + f" < {q(str(spec.prompt_file))}"
    if spec.tee_log is not None:
        lines += [invoke + f" 2>&1 | tee -a {q(str(spec.tee_log))}", 'exit "${PIPESTATUS[0]}"']
    else:
        lines += [invoke, 'exit "$?"']
    return "\n".join(lines) + "\n"


def write_claude(stem: Path, spec: ClaudeSpec) -> Path:
    """Write `<stem>.launch.ps1` (Windows) or `<stem>.launch.sh` (POSIX) and
    return its path. `stem` is a path under LOG_DIR without the suffix."""
    _check_env(spec.env)
    path = stem.with_name(stem.name + SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_ps(spec) if WINDOWS else _render_sh(spec), encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o700)
    return path


def write_shell(stem: Path, cwd: str, command: str) -> Path:
    """A schedule's shell command, wrapped so the poller has something to read.

    A shell command emits no {"type":"result"} object, and Otto never waits on a
    detached child, so without help the poller would find no result and mark every
    successful shell run orphaned. The wrapper prints `OTTO_EXIT=<code>` last.
    The command is written in the shell it runs in: PowerShell on Windows, bash
    elsewhere.
    """
    path = stem.with_name(stem.name + SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        text = "\n".join([
            "$ErrorActionPreference = 'Continue'",
            f"Set-Location -LiteralPath {safeargs.ps_quote(cwd)}",
            command,
            "$code = $LASTEXITCODE",
            "if ($null -eq $code) { $code = 0 }",
            'Write-Output "OTTO_EXIT=$code"',
            "exit $code",
        ]) + "\n"
    else:
        text = "\n".join([
            "#!/usr/bin/env bash",
            f"cd -- {shlex.quote(cwd)} || exit 1",
            command,
            "code=$?",
            'echo "OTTO_EXIT=$code"',
            "exit $code",
        ]) + "\n"
    path.write_text(text, encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o700)
    return path


def command(launcher: Path, keep_open: bool = False) -> list[str]:
    """The argv that runs a launcher file as a child process.

    `keep_open` is the windowed case: the console stays up after the run so a
    human can read it. That is a Windows console feature (`-NoExit`); on POSIX
    there is no console to keep, so the flag changes nothing there.
    """
    if WINDOWS:
        return _PS_BASE + (["-NoExit"] if keep_open else []) + ["-File", str(launcher)]
    return ["bash", str(launcher)]


def pane_command(launcher: Path) -> list[str]:
    """The argv a herdr pane runs. On Windows the launcher is invoked through
    `-Command` behind a UTF-8 console setting, so the child decodes claude's UTF-8
    before the pane's tee re-encodes it; a `-File` child inherits the pane's code
    page and mangles the first non-Latin character."""
    if WINDOWS:
        inner = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                 f"& {safeargs.ps_quote(launcher)}; exit $LASTEXITCODE")
        return _PS_BASE + ["-Command", inner]
    return ["bash", str(launcher)]


def is_launcher(path: str | os.PathLike[str]) -> bool:
    """True for a file this module wrote, on either platform; the token tests
    and pid matching look for."""
    name = Path(path).name
    return name.endswith(".launch.ps1") or name.endswith(".launch.sh")
