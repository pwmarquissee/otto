# Security

Otto runs for one person on one workstation. This file lists what the daemon
defends against, what the code enforces, and what it trusts without checking. If
you run it on a shared host, bind it to a routable address, or let other people log
into the machine, read "What is not enforced" first. The model assumes the only
local user is the owner.

## Threat model

The daemon (`otto/daemon.py` with its routers under `otto/api/`, FastAPI on `127.0.0.1:8787`) can start
`claude --dangerously-skip-permissions` with any prompt in any directory, type into
live terminals through herdr, run schedules (PowerShell commands), and send Slack
messages as the Otto bot. Anything that can drive the daemon can do all of that as
the owner.

In scope:

1. Web pages in the owner's browser. Any tab can call `http://127.0.0.1:8787` or
   open `ws://127.0.0.1:8787/...`, and DNS rebinding can point a hostile hostname
   at `127.0.0.1` after the page loads.
2. Model output in unattended runs. Sessions read mail, Slack, tickets, and web
   pages, then file cards, write titles, and call the daemon's API. Text from a
   model must not become code when Otto puts it in a shell line, a generated
   script, or a CLI argument.
3. Strings in state. Card titles, session titles, hook payloads, herdr pane and
   agent names, run names. Each can reach a shell or a CLI and is checked there.

Out of scope:

- Other local processes running as the owner. The daemon has no authentication. A
  local process can already read the owner's files, credentials, and browser
  profile, so a token on the same disk would add nothing.
- Other users on the machine, and other machines. Loopback binding keeps remote
  hosts out. A multi-user host is not supported.
- A model trying to escape. The guard hook catches mistakes, not attackers.
  Unattended sessions run with `--dangerously-skip-permissions` and the owner's
  credentials. Give them only work you would let a capable, sometimes wrong
  assistant do unsupervised.

## What is enforced

### Origin and Host allowlist (`otto/originguard.py`)

An ASGI middleware, first on the stack, checks every HTTP request and WebSocket
handshake.

- Host. The `Host` hostname must be `127.0.0.1`, `localhost`, `::1`, the configured
  `OTTO_HOST` (unless it is a wildcard), the host of `OTTO_URL`, or a name in
  `OTTO_ALLOWED_HOSTS`. This is the DNS rebinding defense. The port is not
  compared, so a port forward to another local port works.
- Origin. A request with an `Origin` header came from a browser. The origin must be
  one of the daemon's own (`http://127.0.0.1:<port>`, `http://localhost:<port>`,
  `http://[::1]:<port>`, the origin of `OTTO_URL`) or listed in
  `OTTO_ALLOWED_ORIGINS`. `Origin: null` is refused.
- No Origin means a non-browser client (the session hook, the `otto` CLI, `curl`,
  the desktop shell's health poll). These pass.

Refusals are HTTP 403, or a WebSocket close before accept, before any route runs.
This keeps a page out of the terminal socket (`/ws/term/<target>?mode=control`),
the bodyless POST routes (`/api/schedules/{name}/run`, `/api/outreach/{id}/send`,
`/api/tasks/{id}/dispatch`, and the like), and everything else after a DNS rebind.

The desktop shell loads `http://127.0.0.1:8787` directly
(`desktop/src-tauri/src/main.rs`, `BASE`), so its origin is already allowed. For a
tunnel on another port, add it with `OTTO_ALLOWED_ORIGINS=http://localhost:9000`.
There is no wildcard.

### Strings that reach a shell or a CLI (`otto/safeargs.py`)

Most external values reach subprocesses as argv elements, which need no escaping.
The exceptions:

| Sink | Value | Guard |
| --- | --- | --- |
| `herdr pane run` (typed into a pane's PowerShell) | resume session id in `herdr.claude_command` | session-id shape, refused otherwise |
| `wt.exe ... powershell -Command "claude --resume <id>"` (`sessions.resume_in_terminal`) | session id, title, cwd | session-id shape; `;` escaped as `\;` because wt splits its own command line on it |
| generated `.ps1` (`runners/detached.py`, `runners/herdrpane.py`) | run name (a card title), cwd, model, paths | `safeargs.ps_quote`, which doubles `'` and the typographic single quotes (U+2018, U+2019, U+201A, U+201B), since PowerShell closes a string on any of them; env var names must be identifiers |
| herdr CLI targets | pane id, agent name | `^w[0-9A-Za-z]+:p[0-9A-Za-z]+$` or `^[a-z][a-z0-9_-]{0,31}$`, checked at the API (422) and on the terminal socket |
| herdr / git worktree | branch, base, path | no leading `-`, no whitespace, `..`, or `@{` |

Session ids are checked at the hook endpoint (`sessions.record`, 400 on a bad
shape) and again at each sink, because rows can reach state by paths that skip the
endpoint (older state files, `herdr.sync`).

### Paths from outside

- Transcript paths in hook payloads are read only when they are a `.jsonl` under
  `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`). The check is on the
  string. A UNC path is refused before anything opens it, since opening one is an
  SMB connection that sends the owner's NTLM credentials to the named server.
- Agent definition names (`agent` on a spawn or card) must be a plain name, so
  `../x` cannot pull an arbitrary `.md` into a session's system prompt.
- Run log, prompt, and launcher file names come from a sanitized run name inside
  `OTTO_HOME/logs`.
- Post URLs in the writing pipeline must start with `http://` or `https://`.
- Static files go through Starlette's `StaticFiles`, which refuses path traversal.

### Settings and restart routes

`POST /api/setup/settings` writes `<OTTO_HOME>/otto.env`. It accepts only keys
matching `^OTTO_[A-Z0-9_]+$` that `.env.example` documents, so a local caller can
change Otto's settings but cannot set `PATH` or anything another program reads. A
value in the environment overrides the same key in the file.
`POST /api/daemon/restart` exits the daemon and lets the keepalive helper start it
again with a clean environment. Both routes sit behind the allowlist above, so no
web page can reach them. Both are open to local processes, like every other route.

### Secrets

- Your credential CLI (`OTTO_CREDENTIAL_RUN`) injects the Slack bot token into a
  child process's environment, and the message body travels over stdin, so neither
  appears on a command line. The token never enters a spawned session's
  environment. Sessions ask the daemon to send.
- Unattended sessions (`OTTO_UNATTENDED=1`) refuse to send Slack directly and go
  through the daemon's outreach path, where the roster and rate limits apply.
- `OTTO_HERDR_CLAUDE_ARGS` and herdr `--env` values show in the process list. Do
  not put secrets in them.

### Permission levels (`otto/runners/detached.py`)

Every session Otto starts runs at one of three levels, recorded on the run
(`permissions`) and in its environment (`OTTO_RUN_PERMISSIONS`):

- **plan**: `--permission-mode plan --permission-prompts none`. Claude Code's own
  plan mode: reads files, runs read-only commands, and is refused any write
  (measured: `git log` ran, Write was refused). What a PREPARE run gets, so the
  rule the prompt states is also one the session cannot cross. No skip flag.
- **yolo**: `--dangerously-skip-permissions`. The full operator, unattended. What
  every unattended run was before the levels existed, and still what
  implementation, the card-reply and DM sessions, and the scheduled loops get,
  because they act on the board. Nothing elevates to it on its own: a card gets it
  from `otto task yolo`, `otto task run --yolo`, `otto task set --permissions yolo`,
  or the dashboard's Run (yolo); a schedule from its own field.
- **scoped**: neither flag; the caller passes an allow or deny list of its own
  (the outreach sender is allowed one tool, the writing miner none).

The level is not the guard. `scripts/otto_guard.py` keys off `OTTO_UNATTENDED`
and gates the same actions at every level; see below for what it does and does
not catch.

## What is not enforced

### The daemon trusts local processes

Any process that can reach loopback can do everything the dashboard can. It can
spawn a run with any prompt and `skip_permissions`, create a schedule whose command
is arbitrary PowerShell, post to `/api/slack/*` as Otto, write cards that later get
dispatched, post hook events, rewrite `otto.env`, and restart the daemon.
Unattended Claude sessions are such processes, which is why the guard hook exempts
localhost from its HTTP gate. If you need a boundary between local processes, add
authentication first.

### The guard hook catches mistakes, not attackers

`scripts/otto_guard.py` is a Claude Code `PreToolUse` hook. In unattended sessions
it pattern-matches tool inputs and asks the owner through a desktop yes/no dialog
before credential checkouts (`OTTO_GUARD_CRED_COMMANDS`, `OTTO_GUARD_CRED_FILES`,
Secrets Manager reads), raw HTTP writes, `gh api` mutations, identity-provider
mutations on tools matching `OTTO_GUARD_IDP_TOOL_PATTERN`, and edits to test, lint,
and CI configuration. Known ways around it, which adding patterns will not fix:

- Quoting and indirection. Quoted strings are stripped before matching so prose
  does not trip it. A quoted flag (`curl "-X" "DELETE"`), a method in a variable,
  or a command name built at runtime is not seen.
- The localhost exemption is a substring test. A remote mutation with `localhost`
  anywhere in the unquoted command passes.
- Another interpreter. `python -c`, `node -e`, a script written then run,
  PowerShell `-EncodedCommand`, or any HTTP library performs the same mutation
  without a matched client name.
- Splitting across calls. Write the payload in one tool call, run it in the next.
- Ambient credentials. `gh` keyring auth, cached cloud sessions, and browser
  cookies are usable by any process the session starts.
- MCP tools outside the IdP pattern are not gated.
- Only unattended sessions are covered. Without `OTTO_UNATTENDED=1` the hook does
  nothing. Windowed runs, the owner's own sessions, and claude sessions in herdr
  panes (including cards handed to a pane by dispatch, which start with
  `--dangerously-skip-permissions` through `OTTO_HERDR_CLAUDE_ARGS`) run without
  it. Set `OTTO_HERDR_CLAUDE_ARGS=""` to get Claude Code's own prompts back in panes.
- A malformed hook payload allows the turn. The payload comes from Claude Code, not
  the model.

### Logs and state are plaintext

- Run logs (`OTTO_HOME/logs/*.log`, including the herdr pane tee) hold whatever the
  run printed, secrets included.
- Prompt files, system-prompt addenda, and launcher scripts for every run sit next
  to the logs and hold card text and any context Otto added.
- State (`OTTO_HOME/state/*.json`) holds board cards, session titles, Slack and
  meeting excerpts, and people notes. Only the owner's file permissions protect it.

### Non-loopback binds

`OTTO_HOST=0.0.0.0` (or any routable address) exposes an unauthenticated API that
runs code as you to everyone who can reach the port. The Host and Origin checks do
nothing against a non-browser client on the network. Use an SSH or SSM port forward
instead (see [docs/otto-on-ec2.md](docs/otto-on-ec2.md)) and add the forwarded
origin to `OTTO_ALLOWED_ORIGINS`.

### Desktop shell

- The window loads the daemon's URL as remote content. `capabilities/default.json`
  lists no remote URLs and `withGlobalTauri` is off, so the page has no Tauri IPC.
  The global shortcut, autostart, and deep link permissions are used from Rust.
- `otto://open` shows the window. `otto://reply/<id>` sets the dashboard's hash
  only when the id is 1 to 32 hex characters. No other route exists.
- On every start the shell registers `HKCU\Software\Classes\otto` to point at its
  own executable. Any process running as the owner can rewrite that key.
- The dashboard has no Content-Security-Policy header. It builds DOM with
  `textContent`, not `innerHTML`.

## Dependencies

`pyproject.toml` sets lower bounds only (`fastapi>=0.110`, `uvicorn>=0.27`,
`pydantic>=2.6`, `psutil>=5.9`, `requests>=2.31`, `websockets>=12`) so Otto coexists
with whatever is installed beside it. `requirements.lock` pins the exact set
(generated with `uv pip compile --universal`, test extras included) and is what CI
installs, so a run is reproducible. Install from the lock to get the same set. The desktop shell's Rust
dependencies are pinned by `desktop/src-tauri/Cargo.lock`. CI references actions by
tag, not by commit SHA.

## Configuration that affects security

| Variable | Effect |
| --- | --- |
| `OTTO_HOST`, `OTTO_PORT` | bind address and port; keep the host on loopback |
| `OTTO_ALLOWED_ORIGINS` | extra browser origins allowed to call the daemon |
| `OTTO_ALLOWED_HOSTS` | extra `Host` names accepted (rebinding check) |
| `OTTO_HERDR_CLAUDE_ARGS` | flags for claude in herdr panes; default skips permissions |
| `OTTO_GUARD_CRED_COMMANDS`, `OTTO_GUARD_CRED_FILES`, `OTTO_GUARD_IDP_TOOL_PATTERN` | the guard's credential and IdP gates; empty disables them |
| `OTTO_GUARD_ASK`, `OTTO_GUARD_ASK_TIMEOUT` | dialog versus flat deny, and the wait |
| `OTTO_CREDENTIAL_RUN` | the credential CLI that injects the Slack token |
| `OTTO_INTEGRATIONS` | which outside services the daemon probes every five minutes; `none` makes no outbound probe call at all |

## Reporting a vulnerability

Report security issues privately through GitHub's private vulnerability reporting
(the "Report a vulnerability" button on the repository's Security tab). Do not open
a public issue for anything exploitable. Include what you ran, what happened, and
the version or commit. Expect an acknowledgment within a week. Fixes land with a
test in `tests/test_security.py`, or next to the code they touch, that fails
without the fix.
