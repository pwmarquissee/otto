"""otto_guard.py's gate decisions, against the incidents that shaped it.

Three generations of policy, each with its regression cases below:
  * the first unattended-outreach gate (a DM to a colleague) was later REPEALED,
    see below.
  * run d0e68a revoked a departed contractor's GitHub PAT and archived their API
    key. The exact command shapes that run used must be gated forever; if a
    refactor lets any of them pass ungated, the guard is decorative and this
    suite is the alarm.
  * the first unattended morning under the wide net raised three dialogs, all
    false positives (a local findings file whose PROSE named the secrets CLI, an
    `otto task add` with the CLI's name in --tags, the daily summary to the
    owner's own ops feed). The policy was narrowed to destructive actions only,
    with "send freely" for Slack/Gmail. Those three shapes must stay ungated,
    verbatim.

The operator-specific knobs (which command is a credential checkout, which cached
file is a credential, which MCP server is the identity provider) come from the
environment and are set explicitly by the `knobs` fixture, so these tests assert
the mechanism against known values rather than against whatever the host has.

Nothing here ever raises the real dialog: gating tests call _gated() (pure),
and the _resolve() tests monkeypatch _ask_owner and point the audit log at
tmp_path. If a test run pops a MessageBox on anybody's screen, that is a bug in
this file.

The guard is not a Python package (it runs as a bare script under -S), so it is
loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parent.parent / "scripts" / "otto_guard.py"
_spec = importlib.util.spec_from_file_location("otto_guard", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ORG = "example-org"
OPS_CHANNEL = "C0EXAMPLE01"      # a channel id: posts here are the owner's own feed
PERSON = "U0EXAMPLE01"           # a user id: a DM, never through the connector


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    """The operator's own tools, named for the test run only."""
    monkeypatch.setenv("OTTO_GUARD_CRED_COMMANDS", "secrets-cli,anthropic-env")
    monkeypatch.setenv("OTTO_GUARD_CRED_FILES", ".anthropic-env.cache")
    monkeypatch.setenv("OTTO_GUARD_IDP_TOOL_PATTERN", "idp-mcp-server")


def gated(tool, command=None, **tool_input):
    if command is not None:
        tool_input["command"] = command
    return guard._gated(tool, tool_input)


# ---- the d0e68a commands, verbatim shapes: gated forever -----------------------

def test_gh_api_delete_credential_authorization_gated():
    assert gated("Bash", f'gh api -X DELETE "orgs/{ORG}/credential-authorizations/123456789" -i')


def test_curl_post_anthropic_admin_api_gated():
    assert gated("Bash", (
        'curl -s -X POST "https://api.anthropic.com/v1/organizations/api_keys/apikey_x" '
        "-H \"x-api-key: $KEY\" -d '{\"status\":\"archived\"}'"
    ))


def test_sourcing_anthropic_env_gated():
    assert gated("Bash", "source .claude/anthropic-env.sh >/dev/null 2>&1; anthropic-env >/dev/null")


def test_reading_anthropic_env_cache_gated():
    assert gated("PowerShell", "Get-Content $HOME/.claude/.anthropic-env.cache")


def test_secretsmanager_checkout_gated():
    assert gated("Bash", "aws secretsmanager get-secret-value --secret-id ops/anthropic/admin-api-key")


def test_checkout_knobs_default_to_off(monkeypatch):
    """Unset, the checkout gate does not exist. The docstring says so and an
    operator who has not configured it must not get phantom dialogs; they must
    read the KNOBS section instead."""
    monkeypatch.setenv("OTTO_GUARD_CRED_COMMANDS", "")
    monkeypatch.setenv("OTTO_GUARD_CRED_FILES", "")
    assert not gated("Bash", "secrets-cli list")
    assert not gated("PowerShell", "Get-Content $HOME/.claude/.anthropic-env.cache")
    # The fixed gates are unaffected by the knobs.
    assert gated("Bash", "aws secretsmanager get-secret-value --secret-id x")


# ---- the three false positives, verbatim shapes: ungated forever ---------------

def test_local_findings_file_mentioning_the_cli_in_prose_ungated():
    # The 08:11 timeout: a heredoc writing a LOCAL markdown file whose body said
    # "Move both into the secrets CLI". Prose is not an invocation.
    assert not gated("Bash", (
        "mkdir -p /tmp/dailyfindings && cat > /tmp/dailyfindings/contractor.md <<'MDEOF'\n"
        "Two cleartext credential files on the Desktop.\n"
        "  1. Rotate both, since a leaked secrets-cli token implies repo scope on our own code.\n"
        "  2. Move both into secrets-cli so they are checked out just in time instead of living\n"
        "     on disk. Suggested: secrets-cli run -- gh auth login\n"
        "MDEOF"
    ))


def test_otto_task_add_with_the_cli_name_as_a_tag_ungated():
    # The 08:14 timeout: the CLI's name as a --tags value, not a command.
    assert not gated("Bash", (
        'export PYTHONPATH="D:/otto"; python -m otto task add '
        '"Departed contractor: two plaintext secrets on desktop" --status needs-you '
        "--priority high --origin nightly-sweep --tags secrets,secrets-cli,endpoint "
        '--detail "needs rotation and secrets-cli migration"'
    ))


def test_slack_channel_posts_ungated():
    # The 08:28 timeout, and the owner's call: sends flow freely. A channel post
    # to their own ops feed never raises a dialog and is never denied.
    assert not gated("mcp__claude_ai_Slack__slack_send_message",
                     channel_id=OPS_CHANNEL, message="daily run summary")
    assert not guard._misrouted_dm("mcp__claude_ai_Slack__slack_send_message",
                                   {"channel_id": OPS_CHANNEL, "message": "x"})
    assert not gated("mcp__claude_ai_Slack__slack_send_message_draft",
                     channel_id="U1", message="hi")


def test_slack_connector_dm_denied_flat_as_misrouted(monkeypatch, tmp_path):
    # Two board tasks once DM'd a colleague through the connector and both arrived
    # as the owner. A connector send aimed at a PERSON is denied with no dialog and
    # the reason names `otto dm`, the door that sends the same text as Otto.
    def boom(tool, tool_input):
        raise AssertionError("dialog raised for a misrouted DM; it must deny flat")
    monkeypatch.setattr(guard, "_ask_owner", boom)
    monkeypatch.setattr(guard, "_DECISIONS_LOG", str(tmp_path / "decisions.jsonl"))
    for tool, target in (
        ("mcp__claude_ai_Slack__slack_send_message", PERSON),            # user id
        ("mcp__claude_ai_Slack__slack_send_message", "D0EXAMPLE123"),   # DM channel
        ("mcp__claude_ai_Slack__slack_send_message", "G0EXAMPLE123"),   # mpim
        ("mcp__claude_ai_Slack__slack_schedule_message", PERSON),
        ("mcp__slack_other__slack_send_message", "W0EXAMPLE123"),       # renamed server
    ):
        d = guard._resolve(tool, {"channel_id": target, "message": "hey Alex"})
        out = d["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny", (tool, target)
        assert "otto dm" in out["permissionDecisionReason"]
        # The reason says whose name the message would have carried.
        assert "THE OWNER" in out["permissionDecisionReason"]
    # It is not part of the destructive set: _gated stays false, so nothing here
    # would ever reach the dialog path.
    assert not gated("mcp__claude_ai_Slack__slack_send_message",
                     channel_id=PERSON, message="hi")
    # Audited, like every other decision.
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and all("misrouted-dm" in ln for ln in lines)


def test_slack_connector_dm_with_no_target_untouched():
    # No channel_id at all is not a DM we can identify; leave it to the connector.
    assert not guard._misrouted_dm("mcp__claude_ai_Slack__slack_send_message",
                                   {"message": "hi"})


def test_gmail_sends_ungated():
    assert not gated("mcp__claude_ai_Gmail__send_message", to="x@example.com")
    assert not gated("mcp__claude_ai_Gmail__reply", message_id="m1")
    assert not gated("mcp__claude_ai_Gmail__forward", message_id="m1")
    assert not gated("mcp__google-multi__google_api_call",
                     path="/gmail/v1/users/me/messages/send", method="POST")


def test_staging_a_destructive_command_in_text_ungated():
    # dispatch.py TELLS denied sessions to state the exact command for the owner.
    # Writing `gh api -X DELETE ...` into a file or message must not prompt.
    assert not gated("Bash", (
        "cat > /tmp/staged.md <<'EOF'\n"
        f"Please run: gh api -X DELETE orgs/{ORG}/credential-authorizations/123\n"
        "Then: curl -X POST https://api.anthropic.com/v1/... -d '{\"status\":\"archived\"}'\n"
        "EOF"
    ))
    assert not gated("Bash", 'echo "run gh api -X DELETE orgs/x/y and secrets-cli run -- foo" >> notes.md')


# ---- credential checkout: invocations only -------------------------------------

def test_checkout_invocations_gated():
    assert gated("Bash", "secrets-cli list")
    assert gated("Bash", "cd /tmp && secrets-cli run -- slack")
    assert gated("PowerShell", r"D:\tools\secrets-cli.exe run -- slack")


def test_checkout_word_mentions_ungated():
    assert not gated("Bash", "echo my-secrets-cli-notes")
    assert not gated("Bash", "git log --grep secrets-cli --oneline")


# ---- raw HTTP writes ------------------------------------------------------------

def test_gh_api_get_ungated():
    assert not gated("Bash", f'gh api "orgs/{ORG}/credential-authorizations?per_page=100" --jq ".[]"')
    assert not gated("Bash", f"gh api --method GET orgs/{ORG}")


def test_gh_api_field_flags_default_post_gated():
    assert gated("Bash", f"gh api orgs/{ORG}/x -f name=y")
    assert gated("Bash", "gh api repos/o/r/dispatches --input payload.json")


def test_gh_non_api_subcommands_ungated():
    assert not gated("Bash", "gh pr create --title x --body y")
    assert not gated("Bash", "gh auth status")


def test_curl_get_ungated():
    assert not gated("Bash", 'curl -s "https://api.anthropic.com/v1/organizations/api_keys?limit=100" -H "x-api-key: $K"')


def test_curl_data_flag_gated_but_getified_ungated():
    assert gated("Bash", "curl https://example.com/api -d 'a=b'")
    assert not gated("Bash", "curl -G https://example.com/api -d 'a=b'")


def test_curl_quoted_method_still_gated():
    # Quote-stripping replaces "POST" with a placeholder token, which is still
    # a non-GET word to the method regex. The gate must survive quoting.
    assert gated("Bash", 'curl -X "POST" https://example.com/api')


def test_powershell_invoke_restmethod_post_gated_get_ungated():
    assert gated("PowerShell", "Invoke-RestMethod -Method Post -Uri https://idp.example.com/x -Body $b")
    assert gated("PowerShell", "irm https://api.example.com/x -Body $b")
    assert not gated("PowerShell", "Invoke-RestMethod -Uri https://api.example.com/x")


def test_localhost_writes_ungated():
    assert not gated("Bash", "curl -X PATCH http://localhost:8765/api/tasks/abc -d '{\"status\":\"done\"}'")
    assert not gated("PowerShell", "Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/tasks -Body $b")


def test_plain_shell_work_ungated():
    assert not gated("Bash", "git commit -m 'fix: thing'")
    assert not gated("PowerShell", "Get-ChildItem D:/otto")


# ---- identity provider --------------------------------------------------------------

def test_idp_mutations_gated_reads_ungated():
    assert gated("mcp__idp-mcp-server__deactivate_user", user_id="u1")
    assert gated("mcp__idp-mcp-server__update_policy_rule", rule_id="r1")
    assert gated("mcp__idp-mcp-server__remove_user_from_group", user_id="u1", group_id="g1")
    assert not gated("mcp__idp-mcp-server__get_user", user_id="u1")
    assert not gated("mcp__idp-mcp-server__list_policies")
    assert not gated("mcp__idp-mcp-server__get_logs", since="x")


def test_idp_pattern_unset_gates_nothing(monkeypatch):
    monkeypatch.setenv("OTTO_GUARD_IDP_TOOL_PATTERN", "")
    assert not gated("mcp__idp-mcp-server__deactivate_user", user_id="u1")


# ---- things that must stay open (intended unattended behavior) --------------------

def test_edr_and_rmm_untouched():
    assert not gated("mcp__edr-mcp__edr_update_detections", ids=["d1"])
    assert not gated("mcp__rmm__rmm_tickets_create", subject="x")
    assert not gated("mcp__rmm__rmm_devices_get", device_id=5)


# ---- the ask flow -------------------------------------------------------------------

FACTS = (
    f"# otto-gate: target=credential authorization 123456789 on org {ORG} (a departed contractor's PAT)\n"
    "# otto-gate: rollback=none, a revoked PAT cannot be restored; the contractor would mint a new one\n"
    "# otto-gate: authority=card 04bc9d, 'revoke the leaked PAT once the contractor confirms'\n"
)
BARE_CALL = ("Bash", {"command": f"gh api -X DELETE orgs/{ORG}/x"})
GATED_CALL = ("Bash", {"command": FACTS + f"gh api -X DELETE orgs/{ORG}/x"})


def _resolve_with_answer(monkeypatch, tmp_path, answer):
    monkeypatch.setattr(guard, "_ask_owner", lambda tool, tool_input: answer)
    monkeypatch.setattr(guard, "_DECISIONS_LOG", str(tmp_path / "decisions.jsonl"))
    return guard._resolve(*GATED_CALL)


def test_ungated_call_resolves_to_none_without_asking(monkeypatch, tmp_path):
    def boom(tool, tool_input):
        raise AssertionError("dialog raised for an ungated call")
    monkeypatch.setattr(guard, "_ask_owner", boom)
    assert guard._resolve("Bash", {"command": "git status"}) is None


def test_yes_allows(monkeypatch, tmp_path):
    d = _resolve_with_answer(monkeypatch, tmp_path, "yes")
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_no_denies_with_declined_reason(monkeypatch, tmp_path):
    d = _resolve_with_answer(monkeypatch, tmp_path, "no")
    out = d["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "clicked No" in out["permissionDecisionReason"]


def test_timeout_error_and_off_all_deny(monkeypatch, tmp_path):
    for answer in ("timeout", "error", "off"):
        d = _resolve_with_answer(monkeypatch, tmp_path, answer)
        assert d["hookSpecificOutput"]["permissionDecision"] == "deny", answer


def test_decisions_are_audited(monkeypatch, tmp_path):
    _resolve_with_answer(monkeypatch, tmp_path, "yes")
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"answer": "yes"' in lines[0]


def test_ask_disabled_by_env_returns_off(monkeypatch):
    monkeypatch.setenv("OTTO_GUARD_ASK", "0")
    assert guard._ask_owner(*GATED_CALL) == "off"


def test_dialog_text_names_run_and_command(monkeypatch):
    monkeypatch.setenv("OTTO_RUN_NAME", "rotate-exposed-key")
    text = guard._dialog_text(*GATED_CALL)
    assert "rotate-exposed-key" in text
    assert "gh api -X DELETE" in text


# ---- facts before the dialog ------------------------------------------------------
# A yes/no on a command string asks the owner to infer the blast radius themselves.
# The session has to state target, rollback and authority first; without them the
# call is denied WITHOUT spending the owner's attention on a dialog.

def test_gated_shell_call_without_facts_is_denied_without_asking(monkeypatch, tmp_path):
    def boom(tool, tool_input):
        raise AssertionError("dialog raised for a call that carried no facts")
    monkeypatch.setattr(guard, "_ask_owner", boom)
    monkeypatch.setattr(guard, "_DECISIONS_LOG", str(tmp_path / "d.jsonl"))
    d = guard._resolve(*BARE_CALL)
    out = d["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "target, rollback, authority" in out["permissionDecisionReason"]
    assert "# otto-gate: target=" in out["permissionDecisionReason"]
    assert '"answer": "facts-missing"' in (tmp_path / "d.jsonl").read_text(encoding="utf-8")


def test_placeholder_facts_count_as_missing():
    cmd = (
        "# otto-gate: target=n/a\n"
        "# otto-gate: rollback=none\n"
        "# otto-gate: authority=see above\n"
        f"gh api -X DELETE orgs/{ORG}/x"
    )
    assert guard._facts_missing(guard._facts(cmd)) == ["target", "rollback", "authority"]


def test_rollback_none_needs_a_reason():
    assert "rollback" in guard._facts_missing({
        "target": "the thing by name", "rollback": "none", "authority": "card 04bc9d says so"})
    assert guard._facts_missing({
        "target": "the thing by name", "rollback": "none, revocation is permanent",
        "authority": "card 04bc9d says so"}) == []


def test_facts_header_is_parsed_one_per_line_and_may_hold_quotes_and_semicolons():
    f = guard._facts(GATED_CALL[1]["command"])
    assert f["target"].startswith("credential authorization 123456789")
    assert "the contractor would mint a new one" in f["rollback"]
    assert f["authority"] == "card 04bc9d, 'revoke the leaked PAT once the contractor confirms'"


def test_facts_header_does_not_itself_trip_a_gate():
    # A header that quotes a destructive command, above a harmless one.
    assert not gated("Bash", (
        "# otto-gate: target=none, this is git status\n"
        "# otto-gate: rollback=curl -X POST would be the thing to undo, but nothing runs\n"
        "# otto-gate: authority=gh api -X DELETE is only mentioned here\n"
        "git status"
    ))


def test_with_facts_the_dialog_leads_with_them_and_is_asked(monkeypatch, tmp_path):
    seen = {}
    def fake_ask(tool, tool_input):
        seen["text"] = guard._dialog_text(tool, tool_input)
        return "yes"
    monkeypatch.setattr(guard, "_ask_owner", fake_ask)
    monkeypatch.setattr(guard, "_DECISIONS_LOG", str(tmp_path / "d.jsonl"))
    d = guard._resolve(*GATED_CALL)
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"
    text = seen["text"]
    assert text.index("TARGET:") < text.index("ROLLBACK:") < text.index("AUTHORITY:") < text.index("Tool: Bash")
    assert "123456789" in text
    # The header lines are not repeated inside the command block.
    assert text.count("otto-gate") == 0
    audit = (tmp_path / "d.jsonl").read_text(encoding="utf-8")
    assert '"facts"' in audit and "04bc9d" in audit


def test_idp_mutation_needs_no_facts_header(monkeypatch, tmp_path):
    # MCP inputs carry no comment channel; the JSON itself names the target.
    monkeypatch.setattr(guard, "_ask_owner", lambda tool, tool_input: "yes")
    monkeypatch.setattr(guard, "_DECISIONS_LOG", str(tmp_path / "d.jsonl"))
    d = guard._resolve("mcp__idp-mcp-server__deactivate_user", {"userId": "00u123"})
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---- protected config -------------------------------------------------------------
# The files that decide whether checks pass, and the harness that enforces this
# gate. Agents edit them to get to green; the Code Floor says they do not get
# weakened to make a change pass. Ask the owner, do not block.

def test_edit_to_test_and_lint_config_gated():
    for p in (
        r"D:\otto\pytest.ini", "D:/work/example/pyproject.toml", "/repo/.ruff.toml",
        "D:/work/example/web/eslint.config.mjs", "D:/work/example/web/tsconfig.json",
        "D:/x/.github/workflows/ci.yml", "/repo/conftest.py", "/repo/tests/conftest.py",
        "/repo/.golangci.yml", "/repo/clippy.toml",
    ):
        assert gated("Edit", file_path=p, old_string="a", new_string="b"), p


def test_edit_to_harness_files_gated():
    for p in (
        r"C:\Users\alex\.claude\settings.json", r"C:\Users\alex\.claude\settings.local.json",
        r"D:\otto\claude\CLAUDE.md", r"C:\Users\alex\.claude\CLAUDE.md",
        r"D:\otto\scripts\otto_guard.py", r"D:\otto\scripts\otto_hook.py",
        r"C:\Users\alex\.claude\hooks\on-stop.ps1",
    ):
        assert gated("Edit", file_path=p, old_string="a", new_string="b"), p
        assert gated("MultiEdit", file_path=p, edits=[{"old_string": "a", "new_string": "b"}]), p


def test_edit_to_ordinary_files_ungated():
    for p in (
        r"D:\otto\otto\board.py", r"D:\otto\tests\test_board.py", r"D:\otto\DEBT.md",
        r"D:\otto\claude\commands\triage.md", "/repo/README.md", "/repo/package.json",
        "/repo/src/config.py", "/repo/settings.json", "/repo/docs/pytest.ini.md",
    ):
        assert not gated("Edit", file_path=p, old_string="a", new_string="b"), p
        assert not gated("Write", file_path=p, content="x"), p


def test_write_to_existing_protected_config_gated_but_new_file_passes(tmp_path):
    existing = tmp_path / "pyproject.toml"
    existing.write_text("[tool.ruff]\n", encoding="utf-8")
    assert gated("Write", file_path=str(existing), content="[tool.ruff]\nline-length = 200\n")
    # Scaffolding a new project is not weakening anything.
    assert not gated("Write", file_path=str(tmp_path / "new" / "pyproject.toml"), content="[project]\n")


def test_shell_writes_to_protected_config_gated():
    for cmd in (
        "sed -i 's/addopts = -x/addopts = /' pytest.ini",
        "sed -i.bak -e 's/strict/lax/' D:/otto/pytest.ini",
        "echo '[tool.ruff]' > pyproject.toml",
        "cat >> .github/workflows/ci.yml <<'EOF'\n  continue-on-error: true\nEOF",
        "rm pytest.ini",
        "git checkout -- conftest.py",
        "Set-Content -Path pytest.ini -Value 'x'",
        'Set-Content -Path "D:\\otto\\pytest.ini" -Value $x',
        "Remove-Item C:/Users/alex/.claude/settings.json",
        "python -c \"open('pytest.ini','w').write('')\"",
        "mv pytest.ini pytest.ini.bak",
    ):
        assert gated("Bash", cmd) or gated("PowerShell", cmd), cmd


def test_shell_reads_and_prose_about_protected_config_ungated():
    for cmd in (
        "cat pytest.ini",
        "grep -n addopts pytest.ini pyproject.toml",
        "git diff -- pytest.ini",
        "python -m pytest -q 2>/dev/null",
        "Get-Content C:/Users/alex/.claude/settings.json | ConvertFrom-Json",
        "ls .github/workflows/",
        # A findings file whose PROSE names protected config, written by redirect.
        "cat > /tmp/findings/otto.md <<'EOF'\n"
        "Recommend loosening pytest.ini addopts and editing .github/workflows/ci.yml.\n"
        "EOF",
        'python -m otto task add "Fix pytest.ini timezone test" --detail "edit conftest.py to freeze the clock"',
        "echo done > /tmp/out.txt",
    ):
        assert not gated("Bash", cmd), cmd
        assert not gated("PowerShell", cmd), cmd


def test_protected_config_dialog_shows_path_and_diff(monkeypatch):
    monkeypatch.setenv("OTTO_RUN_NAME", "fix-timezone-test")
    text = guard._dialog_text("Edit", {
        "file_path": r"D:\otto\pytest.ini",
        "old_string": "addopts = -x --strict-markers",
        "new_string": "addopts =",
    })
    assert "PROTECTED CONFIG" in text and "pytest.ini" in text
    assert "--- old" in text and "--strict-markers" in text
    assert "+++ new" in text
