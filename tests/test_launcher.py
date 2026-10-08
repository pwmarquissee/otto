"""otto/launcher.py: one per-run script, rendered for the platform.

The PowerShell and bash renderings are both tested on every platform by flipping
launcher.WINDOWS, because CI runs on both and a launcher abstraction written
blind against eight call sites is how a port turns into a rewrite.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

from otto import launcher
from otto.runners import herdrpane


def _sysfile(tmp_path) -> str:
    return str(tmp_path / "it's.txt")


def _spec(tmp_path, **kw):
    prompt = tmp_path / "p.prompt.txt"
    prompt.write_text("hi", encoding="utf-8")
    base = dict(cwd=str(tmp_path), prompt_file=prompt,
                args=("-p", "--output-format", "json", "--model", "sonnet",
                      "--disallowed-tools", "Bash Edit", "--append-system-prompt-file",
                      _sysfile(tmp_path)))
    base.update(kw)
    return launcher.ClaudeSpec(**base)


def test_powershell_rendering_quotes_every_value_and_pipes_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", True)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.ps1")
    spec = _spec(tmp_path, env={"OTTO_UNATTENDED": "1", "OTTO_RUN_NAME": "Tim's card"})
    path = launcher.write_claude(tmp_path / "abc-daily", spec)
    assert path.name == "abc-daily.launch.ps1"
    body = path.read_text(encoding="utf-8")
    lines = body.splitlines()
    assert lines[0] == "$ErrorActionPreference = 'Continue'"
    assert lines[1] == f"Set-Location -LiteralPath '{tmp_path}'"
    assert "$env:OTTO_UNATTENDED = '1'" in lines
    # PowerShell's own escape for a quote inside a single-quoted string.
    assert "$env:OTTO_RUN_NAME = 'Tim''s card'" in lines
    assert f"$prompt = Get-Content -LiteralPath '{spec.prompt_file}' -Raw" in lines
    assert ("$claudeArgs = @('-p', '--output-format', 'json', '--model', 'sonnet', "
            "'--disallowed-tools', 'Bash Edit', '--append-system-prompt-file', "
            "'" + _sysfile(tmp_path).replace("'", "''") + "')") in lines
    assert lines[-2] == "$prompt | & claude @claudeArgs"
    assert lines[-1] == "exit $LASTEXITCODE"
    assert "Tee-Object" not in body


def test_powershell_windowed_run_tees_to_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", True)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.ps1")
    path = launcher.write_claude(tmp_path / "w", _spec(tmp_path, tee_log=tmp_path / "w.log"))
    assert f"$prompt | & claude @claudeArgs 2>&1 | Tee-Object -FilePath '{tmp_path / 'w.log'}'" \
        in path.read_text(encoding="utf-8").splitlines()


def test_bash_rendering_is_the_same_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", False)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.sh")
    spec = _spec(tmp_path, env={"OTTO_UNATTENDED": "1", "OTTO_RUN_NAME": "Tim's card"})
    path = launcher.write_claude(tmp_path / "abc-daily", spec)
    assert path.name == "abc-daily.launch.sh"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert f"cd -- {shlex.quote(str(tmp_path))} || exit 1" in lines
    assert "export OTTO_UNATTENDED=1" in lines
    assert "export OTTO_RUN_NAME='Tim'\"'\"'s card'" in lines
    assert lines[-2] == ("claude -p --output-format json --model sonnet --disallowed-tools "
                         f"'Bash Edit' --append-system-prompt-file {shlex.quote(_sysfile(tmp_path))} "
                         f"< {shlex.quote(str(spec.prompt_file))}")
    assert lines[-1] == 'exit "$?"'
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o700 == 0o700


def test_bash_windowed_run_tees_and_keeps_claudes_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", False)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.sh")
    path = launcher.write_claude(tmp_path / "w", _spec(tmp_path, tee_log=tmp_path / "w.log"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[-2].endswith(f" 2>&1 | tee -a {shlex.quote(str(tmp_path / 'w.log'))}")
    assert lines[-1] == 'exit "${PIPESTATUS[0]}"'


def test_an_env_name_that_is_not_a_name_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not an environment variable name"):
        launcher.write_claude(tmp_path / "x", _spec(tmp_path, env={"A B": "1"}))
    assert not list(tmp_path.glob("x.launch.*"))


@pytest.mark.parametrize("windows", [True, False])
def test_shell_wrapper_reports_the_exit_code_in_the_runs_own_shell(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(launcher, "WINDOWS", windows)
    monkeypatch.setattr(launcher, "SUFFIX", ".launch.ps1" if windows else ".launch.sh")
    path = launcher.write_shell(tmp_path / "s", str(tmp_path), "echo hi")
    body = path.read_text(encoding="utf-8")
    assert "echo hi" in body
    if windows:
        assert path.suffix == ".ps1"
        assert 'Write-Output "OTTO_EXIT=$code"' in body
        assert "if ($null -eq $code) { $code = 0 }" in body
    else:
        assert path.suffix == ".sh"
        assert 'echo "OTTO_EXIT=$code"' in body


def test_command_shapes(tmp_path, monkeypatch):
    p = tmp_path / "r.launch.ps1"
    monkeypatch.setattr(launcher, "WINDOWS", True)
    assert launcher.command(p) == [launcher.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)]
    assert launcher.command(p, keep_open=True)[4] == "-NoExit"
    pane = launcher.pane_command(p)
    assert pane[:5] == [launcher.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    # Through -Command behind a UTF-8 console setting, the launcher path quoted.
    assert pane[5].startswith("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & '")
    assert str(p) in pane[5] and pane[5].endswith("; exit $LASTEXITCODE")

    monkeypatch.setattr(launcher, "WINDOWS", False)
    q = tmp_path / "r.launch.sh"
    assert launcher.command(q) == ["bash", str(q)]
    assert launcher.command(q, keep_open=True) == ["bash", str(q)], "no console to keep on POSIX"
    assert launcher.pane_command(q) == ["bash", str(q)]


def test_is_launcher_knows_both_suffixes():
    assert launcher.is_launcher("C:/l/abc.launch.ps1")
    assert launcher.is_launcher("/tmp/abc.launch.sh")
    assert not launcher.is_launcher("/tmp/abc.prompt.txt")


def test_pane_tee_script_has_a_bash_form(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", False)
    log = tmp_path / "run.log"
    target = str(tmp_path / "it's.launch.sh")
    script = herdrpane._tee_script(["bash", target], log,
                                   {"OTTO_RUN_ID": "abc", "OTTO_UNATTENDED": "1"})
    assert script.name == "run.log.pane.sh"
    body = script.read_text(encoding="utf-8")
    assert "export PYTHONUTF8=1 PYTHONIOENCODING=utf-8" in body
    assert "export OTTO_RUN_ID=abc" in body and "export OTTO_UNATTENDED=1" in body
    assert f"{{ bash {shlex.quote(target)}; }} 2>&1 | tee -- {shlex.quote(str(log))}" in body
    assert 'echo "otto: run exited ${PIPESTATUS[0]}"' in body
    assert herdrpane._pane_invocation(script) == f"bash {shlex.quote(str(script))}"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the bash launcher for real")
def test_bash_launcher_runs_for_real(tmp_path, monkeypatch):
    """A fake `claude` on PATH proves the script shape end to end: cwd, env,
    arguments, the prompt on stdin, and the exit code carried out."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "printf 'cwd=%s\\n' \"$PWD\"\n"
        "printf 'run=%s\\n' \"$OTTO_RUN_NAME\"\n"
        "printf 'args=%s\\n' \"$*\"\n"
        "printf 'stdin=%s\\n' \"$(cat)\"\n"
        "exit 7\n", encoding="utf-8")
    (fake / "claude").chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    spec = _spec(tmp_path, env={"OTTO_RUN_NAME": "it's a run"})
    path = launcher.write_claude(tmp_path / "real", spec)
    r = subprocess.run(launcher.command(path), capture_output=True, text=True)
    assert r.returncode == 7
    assert f"cwd={tmp_path}" in r.stdout
    assert "run=it's a run" in r.stdout
    assert "args=-p --output-format json --model sonnet --disallowed-tools Bash Edit" in r.stdout
    assert "stdin=hi" in r.stdout
