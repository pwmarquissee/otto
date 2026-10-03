# otto_guard.py

**Source** `scripts/otto_guard.py`
**Registered** `~/.claude/settings.json` PreToolUse, matcher
`Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|mcp__.*<your-idp>.*`, hook
timeout 150s (the timeout exists because the hook waits on a dialog; if a gate is
ever added for another MCP family, the matcher must widen with it or the new gate
is dead code. The file tools were added with the protected-config gate). The
`<your-idp>` segment must agree with `OTTO_GUARD_IDP_TOOL_PATTERN`: the matcher
decides which calls reach the hook at all, the pattern decides which of those gate.

## What it may do

Read stdin, raise one topmost yes/no MessageBox on the interactive desktop for a
gated call, print an allow/deny decision, and append one JSONL audit line per
asked decision to `~/.claude/otto/logs/guard-decisions.jsonl`. It calls no
network and imports only stdlib. Its authority is the PreToolUse verdict, which
Claude Code honors even under `--dangerously-skip-permissions`.

## What it gates

Active only when `OTTO_UNATTENDED=1`, which `runners/detached.py` sets in every
headless launcher (along with `OTTO_RUN_NAME`, so the dialog can say which run is
asking). A gated call raises the dialog: **Yes allows that one call; No, an
unanswered dialog (120s, `OTTO_GUARD_ASK_TIMEOUT`), or any failure to ask
denies** with a reason telling the session to stage the action for the owner.
`OTTO_GUARD_ASK=0` skips the dialog and restores flat deny, for hosts with no
interactive desktop (a headless server). Gated, destructive actions only:

* Credential checkout from a shell, as an INVOCATION: any command named in
  `OTTO_GUARD_CRED_COMMANDS` in command position (path-prefixed forms included),
  a read of any file fragment in `OTTO_GUARD_CRED_FILES`, or `get-secret-value`.
  Quoted strings, bash heredocs, and PowerShell here-strings are stripped before
  scanning, so prose, tags, and staged instructions that merely mention these
  never match. Both lists default empty, which means the checkout gate is OFF
  until you name your tools. Name them.
* Raw API writes from a shell: `gh api` with a non-GET method or body flags, and
  curl/wget/Invoke-RestMethod/Invoke-WebRequest/irm/iwr with a mutating method
  or a request body, scanned after the same stripping. Localhost is exempt (the
  board API is the session's job).
* Identity-provider MCP mutations: `create_ / update_ / delete_ / activate_ /
  deactivate_ / add_ / remove_ / confirm_` verbs on any tool whose full name
  matches `OTTO_GUARD_IDP_TOOL_PATTERN` (case-insensitive regex; a plain server
  name works). Reads pass. Default empty, which means the IdP gate is off.
* Protected config: Edit/Write/MultiEdit/NotebookEdit on the files that decide
  whether checks pass (pytest.ini, pyproject.toml, setup.cfg, tox.ini,
  conftest.py, ruff/mypy/flake8/pylint config, eslint/prettier/biome/tsconfig/
  jest/vitest/playwright config, golangci/clippy/rustfmt, pre-commit), CI
  workflows, every CLAUDE.md, `.claude/settings*.json`, `.claude/hooks/*`, and
  `scripts/otto_guard.py` / `scripts/otto_hook.py` themselves. Shell write shapes
  (redirect, `sed -i`, `tee`, `rm`/`mv`/`cp`, `Set-Content`/`Out-File`/
  `Remove-Item`, `git checkout|restore`, `python -c`) naming such a path gate
  too. A `Write` to a path that does not exist yet passes (scaffolding), and a
  protected filename inside a longer quoted string (prose in a findings file)
  does not count; only a bare token or a quoted string that IS a path does.

**Facts before the dialog.** A gated SHELL call must carry three comment lines
above the command: `# otto-gate: target=...`, `# otto-gate: rollback=...`,
`# otto-gate: authority=...`. Without all three (min 8 chars each, no
placeholders; `rollback=none` alone is refused, it has to say why) the call is
denied with a reason spelling out the format, and NO dialog is raised, so the
owner's attention is never spent on a call the session could not justify. With
them, the dialog leads with TARGET / ROLLBACK / AUTHORITY and shows the command
underneath, and the audit line records the facts. The header is stripped before
gate scanning and is an ordinary comment at execution. MCP calls and file tool
calls have no comment channel and go straight to the dialog, which shows the
target from the input (user id; file path with the exact old/new text).

NOT gated, on purpose: Slack and Gmail sends ("send freely" was chosen
explicitly over prompt-per-DM and draft-only; outreach.py's
roster/forbidden-subject interlocks still bound the daemon send path), local
file writes, `otto` CLI calls, gh porcelain (`gh pr create` is reviewable and
reversible), EDR detection updates and RMM actions (board cards the owner queues
close stale detections unattended, intended). The owner's interactive sessions
and windowed spawns are untouched entirely.

## Why (three lessons, in the order they were learned)

**Prose is not a control.** A scheduled morning run DM'd a colleague about a
stale SSH key, unattended. The outreach interlock in `otto/outreach.py` was
never in the path: the session held the Slack MCP send tool and
skip-permissions, so the only thing between it and a colleague's DMs was prose.
"It could circumvent them if it wanted." First version: deny outreach and
credential checkout in unattended sessions.

**Ambient credentials exist.** A board card whose body said "Needs the owner:
rotate both credentials" was queued with a "DM the colleague" context note and
dispatched unattended. The session revoked the colleague's GitHub token (`gh api
-X DELETE`) and archived their API key (raw `curl -X POST` to an admin API),
when the intent was a DM telling them to rotate. The guard allowed all of it:
its threat model said unattended sessions hold no tokens and the checkout CLI is
the only way to get one. False: `gh` is keyring-authenticated, a credential
helper caches an admin key on disk, cloud SSO can fetch secrets. So the gate
moved onto the mutation, not only the checkout. Same day, the owner chose
ask-over-deny: "if the gate is me saying yes or no, we should ALWAYS
interactively ask me." The click is the explicit ask; no click is a No.

**Match invocations, not words.** The first unattended morning under the wide
net raised three dialogs and all three timed out: a LOCAL findings file whose
prose named the checkout CLI, an `otto task add` with its name in `--tags`, and
the daily summary post to the owner's own ops channel. Two pattern bugs
(word-matching instead of invocation-matching) and one policy miss. "We may have
overcorrected... only needs to prompt for permission if it's going to be
requesting to perform destructive actions", and, asked directly, "send freely"
for Slack/Gmail. Hence the invocation anchoring, the prose-stripping, and the
removal of every outreach gate from this hook.

`dispatch.py`'s prompt template states the destructive-actions rule in prose;
this hook is the part that does not depend on being believed.

## Failure posture

Exit 0 always. Malformed stdin allows (it comes from the harness, not the
model). A failure to raise or read the DIALOG denies: at that point the call is
already known to be gated, so fail closed. Missing python.exe or a deleted
script silently allows via the `|| true` in the settings entry, so `heartbeat`
treats the file's absence as a finding (`probe_otto_guard`).

## Known limits, accepted

* Decisions are per-tool-call, not per-intent: a Yes authorizes one call, and a
  session doing a multi-step rotation will raise several dialogs. That is the
  desired shape, a human on every irreversible step.
* An anti-footgun, not an anti-adversary: a Python one-liner that POSTs with
  `urllib` from an already-held token is not pattern-matched, a PowerShell
  call-operator invocation through a quoted path (`& "C:\...\secrets-cli.exe"`)
  is stripped with the quotes, and the localhost exemption is a substring check.
  The deeper fix is not caching an ADMIN key on disk where unattended sessions
  run (a scoped identity for Otto, `docs/agent-identity.md`). Until then this
  closes the doors a session reaches for first, and the deny reason teaches it
  to stage instead of route around.
* Concurrent unattended runs can stack dialogs. Each names its run
  (`OTTO_RUN_NAME`) so they are answerable independently.
* The dialog carries up to 900 chars of the call. A payload longer than that is
  truncated, so an approval of a very long command is an approval of its head.
* Heredoc stripping requires a terminated heredoc; an unterminated one leaves
  its body scannable, which can only cause a spurious dialog, never a miss.
* The checkout and IdP gates are only as good as their knobs. An empty
  `OTTO_GUARD_CRED_COMMANDS` or `OTTO_GUARD_IDP_TOOL_PATTERN` is a gate that
  does not exist, and nothing here warns about it. Set both in `.env` before the
  first unattended run.
