# Security

Otto is a personal operations daemon for one person on one workstation. This file
says what it defends against, what it enforces in code, and what it deliberately
does not. If you deploy it anywhere else (a shared host, a non-loopback bind, a box
other people log into), read "What is not enforced" first: most of the model rests
on "the only local user is the owner."

## Threat model

**The asset.** The daemon (`otto/daemon.py`, FastAPI on `127.0.0.1:8787`) can spawn
`claude --dangerously-skip-permissions` sessions with an arbitrary prompt in an
arbitrary directory, type into live terminals through herdr, run schedules (which
are PowerShell commands), and send Slack messages as the Otto bot. Anything that
can drive the daemon can do all of that as the owner.

**In scope:**

1. **Hostile web pages.** The owner's browser can reach loopback. Any page in any
   tab can send requests to `http://127.0.0.1:8787` and open
   `ws://127.0.0.1:8787/...`, and DNS rebinding can make a hostile hostname resolve
   to `127.0.0.1` mid-session. This is the attacker the daemon's browser guard is
   built for.
2. **Hostile model output in unattended runs.** Scheduled and dispatched sessions
   read email, Slack, tickets, and web content, and can be steered by what they
   read. They file cards, write titles, and post to the daemon's API. Text that
   came from a model must not become code when Otto later puts it into a shell
   line, a generated script, or a CLI argument.
3. **Hostile strings in state.** Card titles, session titles, hook payloads, herdr
   pane and agent names, run names. Each of these can reach a shell or a CLI, and
   each is treated as untrusted at the place it does.

**Out of scope:**

* **Other local processes running as the owner.** The daemon has no
  authentication by design. A local process can already read the owner's files,
  credentials, and browser profile; an API token stored on the same disk would not
  change that. See "The daemon trusts local processes" below.
* **Other users on the same machine, and other machines.** Loopback binding keeps
  remote hosts out. A multi-user host is not a supported deployment.
* **A model that is actively trying to escape.** The guard hook is a seatbelt, not
  a sandbox (below). Unattended sessions run with `--dangerously-skip-permissions`
  and the owner's ambient credentials. Run them only on work you would let a
  capable, occasionally wrong assistant do unsupervised.

## What is enforced

### Browser requests: Origin and Host allowlist (`otto/originguard.py`)

An ASGI middleware, first on the stack, checks every HTTP request and every
WebSocket handshake:

* **Host.** The `Host` header's hostname must be `127.0.0.1`, `localhost`, `::1`,
  the configured `OTTO_HOST` (when it is not a wildcard), the host of `OTTO_URL`,
  or a name in `OTTO_ALLOWED_HOSTS`. This is the DNS rebinding defense: a rebound
  page still sends its own hostname. The port is not compared, so a port forward
  to a different local port still works.
* **Origin.** A request that carries an `Origin` header came from a browser. Its
  origin must be one of the daemon's own (`http://127.0.0.1:<port>`,
  `http://localhost:<port>`, `http://[::1]:<port>`, the origin of `OTTO_URL`) or
  listed in `OTTO_ALLOWED_ORIGINS`. `Origin: null` (sandboxed frames, `file://`)
  is refused.
* **No Origin** means a non-browser client: the session hook, the `otto` CLI,
  `curl`, the desktop shell's health poll. These pass.

Refusals are HTTP 403, or for a WebSocket a close before accept (403 on the wire),
before any route or handler runs. This closes:

* a page opening the terminal socket (`/ws/term/<target>?mode=control`) and typing
  into a live pane (WebSockets are not covered by CORS);
* cross-origin "simple" POSTs that need no preflight, notably the bodyless routes
  (`/api/schedules/{name}/run`, `/api/outreach/{id}/send`,
  `/api/tasks/{id}/dispatch`, `/api/logistics/proposals/{id}/approve`, ...);
* JSON routes on older FastAPI releases that parsed a body with no `Content-Type`
  as JSON (current FastAPI's strict content-type check refuses those on its own;
  the guard does not depend on it);
* DNS rebinding reads and writes of everything.

The desktop shell loads `http://127.0.0.1:8787` directly
(`desktop/src-tauri/src/main.rs`, `BASE`), so its origin is already allowed. If you
reach the dashboard through a tunnel on another port, add that origin:
`OTTO_ALLOWED_ORIGINS=http://localhost:9000`. There is no wildcard setting.

### Ids and strings that reach a shell or a CLI (`otto/safeargs.py`)

Most external values reach subprocesses as argv elements, which is safe by
construction. The exceptions, and what now guards them:

| Sink | Value | Guard |
| --- | --- | --- |
| `herdr pane run` (typed into a pane's PowerShell) | resume session id in `herdr.claude_command` | session-id shape, refused otherwise |
| `wt.exe ... powershell -Command "claude --resume <id>"` (`sessions.resume_in_terminal`) | session id, title, cwd | session-id shape; `;` escaped as `\;` because wt splits its own command line on it |
| generated `.ps1` (`runners/detached.py`, `runners/herdrpane.py`) | run name (a card title), cwd, model, paths | `safeargs.ps_quote`, which doubles the typographic single quotes (U+2018, U+2019, U+201A, U+201B) as well as `'`; PowerShell closes a string on any of them. Env var names must be identifiers. |
| herdr CLI targets | pane id, agent name | `^w[0-9A-Za-z]+:p[0-9A-Za-z]+$` or `^[a-z][a-z0-9_-]{0,31}$`, checked at the API (422) and on the terminal socket before herdr is asked |
| herdr / git worktree | branch, base, path | no leading `-`, no whitespace, `..`, or `@{` |

Session ids are checked at the hook endpoint (`sessions.record`, 400 on a bad
shape) and again at each sink, because rows can reach state by paths that skip the
endpoint (older state files, `herdr.sync`).

### Paths from outside

* **Transcript paths in hook payloads** are read only when they are a `.jsonl`
  under `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`). The decision is
  made on the string; a UNC path is refused before anything touches it, because
  opening one is an SMB connection that hands the owner's NTLM credentials to the
  server it names.
* **Agent definition names** (`agent` on a spawn or card) must be a plain name, so
  `../x` cannot read an arbitrary `.md` into a session's system prompt.
* **Run log, prompt, and launcher file names** are built from a sanitized run name
  inside `OTTO_HOME/logs`.
* **Post URLs** in the writing pipeline must be `http(s)://`; the dashboard renders
  them as links in the daemon's origin.
* **Static files** are served by Starlette's `StaticFiles`, which refuses path
  traversal.

### Secrets

* The Slack bot token is injected by your credential CLI (`OTTO_CREDENTIAL_RUN`)
  into a child process's environment and the message body travels over stdin, so
  neither appears on a command line. The token never enters a spawned session's
  environment; sessions ask the daemon to send.
* Unattended sessions (`OTTO_UNATTENDED=1`) refuse to send Slack directly and
  must go through the daemon's outreach path, where the roster and rate limits
  apply.
* `OTTO_HERDR_CLAUDE_ARGS` and herdr `--env` values are visible in the process
  list. Do not put secrets in them.

## What is not enforced

### The daemon trusts local processes

Any process on the machine that can reach loopback can do everything the
dashboard can: spawn a run with any prompt and `skip_permissions`, create a
schedule whose command is arbitrary PowerShell, post to `/api/slack/*` as Otto,
write cards that later get dispatched, and post hook events. That includes
unattended Claude sessions themselves, which is why the guard hook exempts
localhost from its HTTP gate: writing to the board is their job. This is a design
choice for a single-user workstation, not an oversight. If you need a boundary
between local processes, Otto is the wrong tool without adding authentication.

Two routes added with the setup flow sit in the same trust class and are worth
naming. `POST /api/setup/settings` writes `<OTTO_HOME>/otto.env`; it accepts only
keys matching `^OTTO_[A-Z0-9_]+$` that `.env.example` documents, so a local process
can change Otto's own configuration but cannot plant `PATH` or anything another
program reads, and a value the file holds is overridden by the same name in the
environment. `POST /api/daemon/restart` exits the daemon and lets the keepalive entry
point start it again with a clean environment; a local process could already do that
with `otto stop`. Neither route is reachable from a web page, because both are
behind the Origin and Host allowlist above.

### The guard hook is a seatbelt, not a sandbox

`scripts/otto_guard.py` is a Claude Code PreToolUse hook that pattern-matches tool
inputs in unattended sessions and asks the owner (a desktop yes/no dialog) before
credential checkouts, raw HTTP writes, `gh api` mutations, identity-provider
mutations, and edits to test and CI configuration. It exists to catch a model
making a mistake in good faith. It does not stop a model that is trying to get
around it, and it is not meant to. Known ways around it, none of which will be
"fixed" by adding patterns:

* **Quoting and indirection.** Quoted strings are stripped before matching so
  prose does not trip it; a quoted flag (`curl "-X" "DELETE"`), a method in a
  variable (`$m='DELETE'; curl -X $m`), or a command name built at runtime
  (`& ("cu"+"rl")`) is not seen.
* **The localhost exemption** is a substring test: a remote mutation with
  `localhost` anywhere in the unquoted command (a query string, a comment) passes.
* **Another interpreter.** `python -c`, `node -e`, a script written to disk and
  then run, PowerShell `-EncodedCommand`, or any language's HTTP library performs
  the same mutation without a matched client name.
* **Splitting across calls.** Write the payload in one tool call, execute it in
  the next; each call alone looks benign.
* **Ambient credentials.** `gh` keyring auth, cached cloud sessions, and browser
  cookies are usable by any process the session starts.
* **MCP tools** outside the configured IdP pattern are not gated at all.
* **Only unattended sessions are covered.** The hook is a no-op without
  `OTTO_UNATTENDED=1`. Windowed runs, the owner's own sessions, and claude
  sessions inside herdr panes (including cards handed to a pane by the logistics
  strip, which start with `--dangerously-skip-permissions` by default via
  `OTTO_HERDR_CLAUDE_ARGS`) run without it. Set `OTTO_HERDR_CLAUDE_ARGS=""` to get
  Claude Code's own permission prompts back in panes.
* **Fail-open on a malformed hook payload.** The payload comes from Claude Code,
  not the model, so a parse failure allows rather than blocks the turn.

### Logs and state are plaintext

* Run logs (`OTTO_HOME/logs/*.log`, including the herdr pane tee) contain whatever
  the run printed. If a run echoed a secret, the log holds it.
* Prompt files, system-prompt addenda, and launcher scripts for every run are
  written next to the logs. They contain card text and any context Otto added.
* State (`OTTO_HOME/state/*.json`) holds board cards, session titles, Slack and
  meeting excerpts, and people notes. It is protected by the file system
  permissions of the owner's profile and nothing else.

### Non-loopback binds

`OTTO_HOST=0.0.0.0` (or any routable address) exposes an unauthenticated API that
can run code as you to everyone who can reach the port. The Host and Origin checks
do not help against a non-browser client on the network. Use an SSH or SSM port
forward instead (see `docs/otto-on-ec2.md`) and add the forwarded origin to
`OTTO_ALLOWED_ORIGINS`.

### Desktop shell

* The window loads the daemon's URL as remote content. Tauri v2 capabilities apply
  only to local content unless a capability lists remote URLs, and
  `capabilities/default.json` lists none, so the dashboard page has no Tauri IPC.
  `withGlobalTauri` is off. The capability set (global shortcut, autostart,
  deep link) is used from Rust, not from the page.
* `otto://` links accept two routes: `otto://open` shows the window, and
  `otto://reply/<id>` sets the dashboard's hash only when the id is 1 to 32 hex
  characters. A link cannot navigate the window anywhere else.
* On every start the shell registers `HKCU\Software\Classes\otto` to point at its
  own executable. Any process running as the owner can rewrite that key, which is
  the same local-process trust as above.
* The dashboard is served without a Content-Security-Policy header. It builds DOM
  with `textContent` rather than `innerHTML`; a CSP would be defense in depth, not
  a current fix.

## Dependencies

`requirements.txt` sets lower bounds only (`fastapi>=0.110`, `uvicorn>=0.27`,
`pydantic>=2.6`, `psutil>=5.9`, `requests>=2.31`) so Otto coexists with whatever is
already installed. Nothing is pinned and there is no lock file, so a fresh install
takes the newest releases. The tests also need `httpx` (Starlette's TestClient),
which is not listed. The desktop shell's Rust dependencies are pinned by
`desktop/src-tauri/Cargo.lock`. CI references actions by tag, not by commit SHA.

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

Please report security issues privately through GitHub's private vulnerability
reporting: the "Report a vulnerability" button on this repository's Security tab.
Do not open a public issue for something exploitable. Include what you ran, what
happened, and the version or commit. You should get an acknowledgment within a
week. Fixes land with a test in `tests/test_security.py` (or next to the code they
touch) that fails without the fix.
