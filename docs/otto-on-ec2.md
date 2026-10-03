# Otto on EC2, with an identity of its own

Plan for two changes that are really one change: moving the daemon and its MCP
connections off the owner's Windows workstation onto a long-lived Linux EC2 instance,
and giving Otto an AWS principal it assumes rather than riding the owner's SSO session.

Nothing here is built. Companion to `docs/agent-identity.md`, which states the
identity problem and the per-integration provisioning order; this document is the AWS
half of that document's section 2, made concrete, plus the hosting change that makes
it possible. Account ids and profile names below are placeholders: substitute your own.

## The central insight, first

These read like two tasks. They are one, and the order matters.

`docs/agent-identity.md` section 2 recommends "an IAM role in each account Otto needs,
trusting a machine identity for Otto." On a workstation that means OIDC, because a
workstation has no machine identity AWS will vouch for. So you have to invent one,
hold key material for it, and refresh it.

Moving Otto to EC2 supplies the machine identity for free. An instance profile **is** a
principal AWS already vouches for: nothing to hold, nothing to refresh, nothing to
rotate, nothing to leak, and CloudTrail records `assumed-role/otto-agent-base/i-0abc...`
on every call. The hosting move is not a prerequisite that happens to come first. It is
the thing that makes the identity design simple instead of elaborate.

So: build the box, and the box is the identity. Do not build OIDC federation unless Otto
later needs to run somewhere other than EC2.

## What actually moves, and what the migration costs

Measured against the repo, not assumed.

### Moves as-is

- **`otto/daemon.py`.** Zero platform-specific calls. FastAPI app plus a `tick()`
  loop. Pure Python. This is the part everyone expects to be hard and it is not.
- **State.** `~/.claude/otto/state/*.json` and `logs/`, resolved from `Path.home()` via
  `config.OTTO_HOME`. Plain JSON under a single-writer rule. Copies across and works.
- **The board, journal, advisor, store, findings.** No platform coupling.
- **Most MCP servers, mechanically.** Node and `uvx` servers have Linux equivalents of
  the same invocation. Their authentication is a separate problem, handled below.

### Is the migration

- **Every spawn path is PowerShell.** Several modules write a `.launch.ps1` into
  `LOG_DIR` and shell out to `powershell -NoProfile -ExecutionPolicy Bypass -File`:
  `launch.py`, `runners/detached.py`, `chat.py`, `meetings.py`, `refresh.py`,
  `outreach.py`, and `triage.py` (`OTTO_CMD`). This, not the daemon, is the port.

  Two ways to do it, and the cheap one should be measured before the clean one is
  written. **Cheap:** install PowerShell 7 (`pwsh`) on the box. The launchers are mostly
  environment assignment followed by exec of `claude`, so many may run unmodified once
  `powershell` becomes `pwsh`. **Clean:** extract the launcher into one module with a
  platform-selected backend, since the generated script is the same shape at every
  call site. Recommendation: measure with `pwsh` to get a working box quickly, then do
  the extraction as a separate change, because a launcher abstraction written blind
  against eight call sites is how a migration turns into a rewrite.

- **Any MCP server registered as a `.cmd` wrapper.** A wrapper with no `env` block is
  exactly right and is the pattern the others should copy. It needs a `.sh` sibling
  doing the same thing.

- **Toast notifications die.** `notify.py` shells `scripts/Show-OttoToast.ps1`. There
  is no Windows desktop on an EC2 instance. `OTTO_NO_TOAST=1` already exists next to
  `OTTO_HOME`, so this is a config flag, not code. But it means the notification path
  becomes Slack-only, and Slack DM is then the *sole* channel by which Otto reaches the
  owner. That single point of contact should be deliberate rather than discovered.

- **`DOMAIN_ROOTS` is a set of Windows paths.** `config.py` maps local directories to
  `work` and `personal`. Domain derivation, the repo registry, and `otto gaps` all key
  off these existing on disk. On Linux they must be re-rooted and the repos cloned, or
  every repo-facing feature degrades silently. Re-rooting is a config change; cloning
  a work drive onto a cloud box is a data-placement decision, including how much of it
  and whether it needs to be there at all.

- **`manifest.py` reads the Claude Desktop registry** at `%APPDATA%\Claude`. Claude
  Desktop does not exist on Linux, so that capability check goes permanently
  `unverified` on the box. Note the consequence: the undeclared-inline finding that
  `otto identity` surfaced came from exactly that registry. Whatever box keeps
  watching the Desktop registry is the box that keeps finding those.

- **Schedules whose command is a Windows script** outside the repo. Any schedule
  pointing at a PowerShell file on the workstation is either ported, re-pathed, or
  left behind on the workstation.

### Gets simpler

Windows task scheduling goes away. `Install-OttoDaemon.ps1` installs two tasks:
`OttoDaemon` on a logon trigger and `OttoKeepalive` every ten minutes calling
`otto ensure`. Both exist to work around a workstation: a daemon that dies at 02:00 stays
dead until the next logon. A systemd unit with `Restart=always` and
`WantedBy=multi-user.target` replaces both, needs no logon, and survives reboot. The
script's own design note (one Otto scheduler, not one Windows task per schedule) carries
over unchanged and is the reason this substitution is clean.

This is also the actual point of the move. Otto's Slack DM path (`inbox.py`, the
`slack-dm` feed with `max_age_hours=3`) currently stops when the owner's laptop sleeps.
A personal assistant that is awake only when its owner is at the desk is the problem
being solved.

## The blocker worth naming before anything else

`docs/agent-identity.md` rule 5: **unattended sessions cannot check out credentials.** An
Otto-spawned session gets `OTTO_UNATTENDED=1` and the guard gates the checkout. The
document calls this "a control, not an inconvenience," and it is enforced in practice:
writing this plan from an unattended session tripped it twice.

A headless EC2 instance is *permanently* unattended. There is never a human at that
console. So the current design and the target hosting are in direct conflict, and there
are only three honest ways out:

1. **Pre-provision everything into the instance environment.** Recreates the inline
   antipattern that rule 2 exists to forbid, on a machine with a longer uptime and a
   network position. Reject.
2. **Change the checkout backend.** Otto's material lives in AWS Secrets Manager and the
   authorizing fact becomes the instance role rather than a human at a console. It is
   still in a store and still fetched at use time, so rules 2 and 3 hold. And it is
   *better* than the status quo in one specific way: every fetch is a CloudTrail event
   naming `assumed-role/otto-agent-base/...`, so that access path becomes attributable,
   which it is not today.
3. **Split the daemon.** Unattended work runs on EC2 with pre-scoped read-only access
   only; anything needing a real checkout stays a schedule on the workstation where the
   owner is present.

Option 2 is the recommendation. But it means an unattended Otto **can** acquire material
it could not before, which is the exact thing rule 5 currently forbids on purpose. That
is the owner's call to reverse, not a detail to implement quietly. It is decision gate 1
below and the whole MCP half of this task is blocked behind it.

The mitigation that makes option 2 defensible: Otto's instance role is scoped to a narrow
prefix (`otto/*`) and nothing else, so the guard is replaced by a resource policy
rather than removed. Otto cannot reach any other prefix in the store.

## Part 2: the AWS identity, concretely

### Shape

One base role, per-account read-only roles that trust it, and a permissions boundary on
all of them.

- **`otto-agent-base`** in the IT account (`123456789012`), attached to the EC2 instance
  profile. It holds almost nothing directly: `sts:AssumeRole` on the Otto role ARNs,
  scoped read on the `otto/*` secrets prefix, and SSM plus CloudWatch agent basics.
- **`otto-agent-ro`** in each account Otto reads, trusting `otto-agent-base` by ARN with
  an `sts:ExternalId` condition. Start with exactly one account, not all of them.
- **`OttoBoundary`** permissions boundary on every Otto role, denying `iam:*` writes,
  `kms:*`, `organizations:*`, and anything in your high-trust accounts. A boundary is
  the control that survives somebody later attaching a wider policy by mistake.

### Explicitly not

- **Not an Identity Center user.** Costs a seat, appears in every access review as a
  person who is not one, and is human-shaped in exactly the way rule 1 forbids.
- **Not a member of any IdP group that federates into AWS.** Those groups are the human
  federation path. Otto in one of them is Otto inheriting the owner's access with extra
  indirection.
- **Not an IAM user with a long-lived key.** The instance profile exists precisely so
  that none is needed.
- **Not OIDC.** Deferred, not rejected. It is the right answer the day Otto needs to run
  off-EC2, and nothing here forecloses it.

### The guardrail that matters most

An explicit org-level deny on Otto reaching your high-trust account (code signing,
KMS keys, release certificates) and the management account. Everything else Otto
might touch is recoverable. A code-signing key is
not. This is one SCP or one boundary statement and it should land before the first Otto
role is assumable anywhere.

### Acceptance test

From the box: `aws sts get-caller-identity` returns
`arn:aws:sts::123456789012:assumed-role/otto-agent-base/i-0...` and **not**
`AWSReservedSSO_...`. That single line is the whole deliverable of part 2. Then the same
check again after a cross-account assume.

### A reliability win, not just an audit win

An expired SSO session on the workstation kills every AWS-backed loader at once,
silently, and needs a re-auth plus a session restart. Instance role credentials do not
expire from the consumer's point of view; the metadata service refreshes them. Moving
Otto's AWS access off the owner's SSO session removes that entire failure class for
anything AWS-backed. Worth stating because it is the argument that makes this
maintenance rather than compliance work.

## Part 1: the box

### Reuse your scratch-box pattern, and deliberately diverge in one place

If you already run a hardened Linux scratch box (Ubuntu LTS, dedicated VPC with no
peering, IMDSv2 required, default-closed SG, nothing sensitive resident on the box, no
backups), that is the paved road. Copy all of it.

Diverge on access. A box built for someone with no AWS identity needs an SSH CA and a
doorknock. The owner has AWS identity. So Otto's box gets **SSM Session Manager only**:
no port 22 in the SG at all, no CA to rotate, no doorknock to sweep, no CGNAT failure
mode, and every session recorded in CloudTrail. Strictly simpler and strictly stronger.
Do not copy the doorknock; it solves a constraint that does not apply here.

### Divergence number two: long-lived means the security bar goes up

A scratch box is default-off and its blast radius is compute. Otto's box is the
opposite: always on, holding a principal that can read across accounts, and running
agent sessions that act in your identity provider, EDR, RMM and Slack.

So the EDR Linux sensor is **required before production**, not an open question. The
fleet-of-one argument that justifies deferring it on a scratch box does not survive
contact with a long-lived box that holds Otto's identity. Same for patching:
unattended-upgrades enabled, and the box in scope for whatever the Linux patching story
is rather than exempt by being invisible.

### Sizing, needs a real number

The daemon itself is a tick loop plus FastAPI and is negligible. The load is the Claude
Code sessions it spawns, several concurrent, each a Node process. 2 vCPU is likely
marginal; 4 vCPU and 8GB is the safer starting point, on gp3 with room for repo clones
and logs. Order of magnitude for a 4 vCPU instance running continuously is low hundreds
of dollars a month on demand and materially less on a savings plan, but **that figure is
a guess and needs pricing confirmed against the actual instance family and region before
it goes to anyone**. It is a new recurring line item rather than a rounding error.
Right-size after two weeks of CloudWatch rather than guessing twice.

### Dashboard

The dashboard binds `127.0.0.1:8787` and should keep doing exactly that. Reach it via
SSM port forwarding (`aws ssm start-session --document-name
AWS-StartPortForwardingSession`). Zero ingress, no ALB, no certificate, no auth layer to
get wrong. Anything else is a public front door on a box holding cross-account access.

## Phasing

**Phase 0. Decisions.** The three gates below. Nothing downstream is safe to build first,
because gate 1 determines whether the MCP servers can run on the box at all.

**Phase 1. Identity, provable before any migration.** Write `OttoBoundary`, the
high-trust and management-account deny, `otto-agent-base`, and one `otto-agent-ro` in
one non-critical account. Launch a throwaway t3.micro with the instance profile and run
the `get-caller-identity` acceptance test plus one cross-account assume. This validates
the entire trust design for a few cents and an hour, with nothing migrated and nothing
at risk.

**Phase 2. The box.** VPC, SG, IMDSv2, instance profile from phase 1, SSM only, EDR
sensor, unattended-upgrades, systemd unit. Python, Node, `uvx`, `pwsh`, and Claude Code
installed. No Otto state yet.

**Phase 3. MCP connections.** Per gate 1's answer. Provision `otto-agent` API clients in
the EDR (read-only scopes) and RMM (monitoring scopes) as `docs/agent-identity.md`
section 1 already specifies, land both in the `otto/*` prefix, and wire wrapper
scripts that fetch at launch with no `env` block in the registry. Port any `.cmd`
wrapper to `.sh`. A Google connector needs a one-time interactive OAuth consent, which
is the one thing a headless box genuinely cannot do: run the flow on the owner's desktop
and place the result, or port-forward the callback through SSM. Then `otto identity` on
the box should move off zero for the first time, which is the acceptance test for this
phase.

**Phase 4. Daemon port.** `pwsh` measurement first, launcher extraction second. Re-root
`DOMAIN_ROOTS`, decide what gets cloned, set `OTTO_NO_TOAST=1`, and either port or strand
each Windows-script schedule. Run with the workstation daemon stopped, on copied state,
and diff the journal.

**Phase 5. Cutover.** Copy state, start the systemd unit, uninstall the two Windows
tasks. Keep the workstation able to run Otto for a rollback window.

## Decision gates

These are the owner's, and each one changes what gets built.

1. **May an unattended Otto fetch its own material at runtime?** Reverses rule 5 of
   `docs/agent-identity.md` in exchange for a Secrets Manager path gated by the instance
   role and scoped to one prefix. Blocks phase 3 entirely. No answer means no MCP servers
   on the box.

2. **Does personal-domain work move to a company box?** Otto covers work *and* personal
   by explicit design; `DOMAIN_ROOTS` maps a personal projects directory to `personal`.
   On EC2 that work runs on company-owned compute, under the company EDR, with every
   action in the company's CloudTrail. That may be entirely fine and it may not be, but
   it should be a decision rather than a side effect of a hosting change. Options:
   work-only on the box with personal staying on the workstation, or accept the merge.
   This one is easy to miss until it is already true.

3. **Does the Windows box stay an Otto?** There are real reasons to keep one: the Claude
   Desktop registry check that found the inline key, desktop toasts, the local repo
   checkouts, endpoint-facing PowerShell work, and attended checkout. A two-Otto split
   has a cost, though: two daemons, and the single-writer rule on state means they
   cannot share it. Recommendation is one canonical daemon on EC2 plus a small
   workstation agent for the desktop-only checks, but that shape is worth agreeing
   before phase 4 rather than after.

## What this plan does not cover

- Sizing and cost with confirmed pricing. Flagged above as a real number that is
  currently a guess.
- The internals of any `.cmd` MCP wrapper, unread because reading it from an unattended
  session trips the guard.
- Whether the `claude.ai` connectors (Slack, Drive, Gmail, Calendar, and the rest)
  behave the same on the box. They authenticate against the signed-in claude.ai
  *account*, not the machine, so they should follow the move and should also stay
  `human`-attributed, exactly as `docs/agent-identity.md` section 5 accepts. That is an
  expectation from how connectors work, not something verified here.
- Backup and recovery for the box. Scratch-box doctrine is "no backups." Otto's state is
  not scratch: it is the board, the journal, and the audit record. Whether that needs
  more than a state copy is out of scope here and should not be assumed answered by
  inheriting scratch-box doctrine.
