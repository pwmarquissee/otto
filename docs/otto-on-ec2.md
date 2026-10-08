# Otto on EC2

A plan for moving the daemon and its MCP connections from the owner's Windows
workstation to a long-lived Linux EC2 instance, and giving Otto an AWS principal it
assumes instead of the owner's SSO session. Nothing here is built. It is the AWS
half of the cloud step in [agent-identity.md](agent-identity.md), plus the hosting
change that makes it simple. Account ids and names are placeholders.

## Why one change, not two

The identity document asks for an IAM role trusting a machine identity for Otto. A
workstation has none, so there it means OIDC federation with key material to hold
and refresh. An EC2 instance profile is a principal AWS already vouches for.
Nothing to hold or rotate, and CloudTrail records
`assumed-role/otto-agent-base/i-0abc...` on every call. Build the box and the box
is the identity. Do not build OIDC unless Otto later needs to run off EC2.

## What moves

### As is

- `otto/daemon.py` and the routers under `otto/api/`. FastAPI plus a `tick()` loop, no platform-specific calls.
- State. `OTTO_HOME/state/*.json` and `logs/`, plain JSON under a single-writer
  rule. Copy it across.
- The board, journal, advisor, store, and findings.
- Most MCP servers. Node and `uvx` servers have the same invocation on Linux.
  Their authentication is handled below.

### Needs porting

- Every spawn path used to be PowerShell. The eight sites now describe the run and
  `otto/launcher.py` writes a `.launch.ps1` (Windows) or `.launch.sh` (POSIX) and
  runs it with the platform's shell; the herdr pane tee script has a bash form
  too. `OTTO_POWERSHELL=pwsh` picks PowerShell 7 on Windows. What is still
  unmeasured is a real `claude` session started this way on Linux: the bash
  rendering is tested, the Claude Code run is not.
- MCP servers registered as Windows wrapper scripts. A wrapper with no `env` block
  is the right pattern. Each needs a shell sibling.
- Toasts. `notify.py` runs `scripts/Show-OttoToast.ps1`. Set `OTTO_NO_TOAST=1`.
  Slack DM then becomes the only channel by which Otto reaches the owner.
- Project roots. `OTTO_WORK_ROOTS` and `OTTO_PERSONAL_ROOTS` (`config.DOMAIN_ROOTS`)
  must point at directories on the box with the repos cloned there, or every
  repo-facing feature degrades silently. How much of a work drive belongs on a
  cloud box is a data-placement decision.
- `manifest.py` reads the Claude Desktop registry at `%APPDATA%\Claude`. Claude
  Desktop does not exist on Linux, so that check reports `unverified` on the box.
- Schedules whose command is a Windows script outside the repo. Port, re-path, or
  leave each on the workstation.

### Gets simpler

`Install-OttoDaemon.ps1` installs two scheduled tasks, `OttoDaemon` at logon and
`OttoKeepalive` every ten minutes running `otto ensure`. A systemd unit with
`Restart=always` and `WantedBy=multi-user.target` replaces both and survives
reboot without a logon.

This is also the point of the move. The Slack DM path (`inbox.py`, the `slack-dm`
feed with `max_age_hours=3`) stops when the owner's laptop sleeps.

## The blocker

Rule 5 in the identity document: unattended sessions cannot check out credentials.
An Otto-spawned session has `OTTO_UNATTENDED=1` and the guard gates the checkout.
A headless instance is always unattended. Three ways out:

1. Pre-provision everything into the instance environment. This recreates the
   inline antipattern rule 2 forbids, on a machine with longer uptime and a
   network position. Reject.
2. Change the checkout backend. Otto's material lives in AWS Secrets Manager under
   one prefix (`otto/*`), and the instance role authorizes the fetch. Still a
   store, still fetched at use time, and every fetch is a CloudTrail event naming
   `assumed-role/otto-agent-base/...`.
3. Split the daemon. Unattended work runs on EC2 with pre-scoped read-only access.
   Anything needing a checkout stays a schedule on the workstation.

Option 2 is the recommendation. It lets an unattended Otto acquire material it
cannot today, which reverses rule 5, so it is the owner's decision (gate 1 below)
and the MCP half of this plan waits on it. The `otto/*` prefix scope on the
instance role replaces the guard with a resource policy rather than removing it.

## The AWS identity

### Shape

- `otto-agent-base` in the IT account (`123456789012`), attached to the instance
  profile. It holds `sts:AssumeRole` on the Otto role ARNs, read on the `otto/*`
  secrets prefix, and SSM plus CloudWatch agent basics.
- `otto-agent-ro` in each account Otto reads, trusting `otto-agent-base` by ARN
  with an `sts:ExternalId` condition. Start with one account.
- `OttoBoundary`, a permissions boundary on every Otto role, denying `iam:*`
  writes, `kms:*`, `organizations:*`, and anything in your high-trust accounts.
  The boundary survives someone later attaching a wider policy by mistake.

### Not

- Not an Identity Center user. Costs a seat and is human-shaped.
- Not a member of any IdP group that federates into AWS. Those are the human path.
- Not an IAM user with a long-lived key. The instance profile makes one unnecessary.
- Not OIDC. Deferred until Otto needs to run off EC2.

### The guardrail that matters most

An org-level deny on Otto reaching your high-trust account (code signing, KMS keys,
release certificates) and the management account. Everything else Otto might touch
is recoverable. A code-signing key is not. One SCP or one boundary statement,
landed before the first Otto role is assumable anywhere.

### Acceptance test

```
aws sts get-caller-identity
```

From the box this returns
`arn:aws:sts::123456789012:assumed-role/otto-agent-base/i-0...` and not
`AWSReservedSSO_...`. Run it again after a cross-account assume.

An expired SSO session on the workstation kills every AWS-backed loader at once.
Instance role credentials are refreshed by the metadata service, so that failure
class goes away.

## The box

- If you already run a hardened Linux instance (Ubuntu LTS, dedicated VPC with no
  peering, IMDSv2 required, default-closed security group, nothing sensitive
  resident), copy it.
- Access is SSM Session Manager only. No port 22, no SSH CA to rotate, every
  session recorded in CloudTrail.
- A long-lived box holding a cross-account principal needs the EDR Linux sensor
  before production, `unattended-upgrades` on, and a place in your Linux patching.
- Sizing. The daemon is negligible. The load is several concurrent Claude Code
  sessions, each a Node process. Start at 4 vCPU and 8 GB on gp3 with room for repo
  clones and logs, and right-size after two weeks of CloudWatch. Price it against
  the real instance family and region before quoting a number.
- Dashboard. Keep the bind on `127.0.0.1:8787` and reach it through SSM port
  forwarding. No ingress, no ALB, no certificate, no auth layer to get wrong.

```
aws ssm start-session --target i-0... --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8787"],"localPortNumber":["8787"]}'
```

If the local port differs, add the forwarded origin to `OTTO_ALLOWED_ORIGINS` (see
[../SECURITY.md](../SECURITY.md)).

## Phasing

0. Decisions. The three gates below. Gate 1 decides whether MCP servers can run on
   the box at all.
1. Identity, provable before any migration. Write `OttoBoundary`, the high-trust
   and management-account deny, `otto-agent-base`, and one `otto-agent-ro` in a
   non-critical account. Launch a throwaway t3.micro with the instance profile and
   run the acceptance test plus one cross-account assume.
2. The box. VPC, security group, IMDSv2, instance profile, SSM only, EDR sensor,
   `unattended-upgrades`, systemd unit. Install Python, Node, `uvx`, `pwsh`, and
   Claude Code. No Otto state yet.
3. MCP connections, per gate 1. Provision `otto-agent` API clients in the EDR and
   RMM as the identity document specifies, land both in the `otto/*` prefix, and
   wire wrapper scripts that fetch at launch with no `env` block. A Google
   connector needs a one-time interactive OAuth consent, which a headless box
   cannot do. Run the flow on the owner's desktop and place the result, or
   port-forward the callback through SSM. `otto identity` on the box moving off
   zero is the acceptance test.
4. Daemon port. The launcher is extracted; measure a real run on the box. Set the
   project roots, decide what gets cloned, set `OTTO_NO_TOAST=1`, and port or
   strand each Windows-script schedule. Run with the workstation daemon stopped,
   on copied state, and diff the journal.
5. Cutover. Copy state, start the systemd unit, uninstall the two Windows tasks.
   Keep the workstation able to run Otto for a rollback window.

## Decision gates

1. May an unattended Otto fetch its own material at runtime? Reverses rule 5 in
   exchange for a Secrets Manager path gated by the instance role and scoped to one
   prefix. No answer means no MCP servers on the box.
2. Does personal-domain work move to a company box? `OTTO_PERSONAL_ROOTS` maps
   directories to `personal`. On EC2 that work runs on company compute, under the
   company EDR, in the company's CloudTrail. Options are work-only on the box, or
   accept the merge.
3. Does the Windows box stay an Otto? Reasons to keep one: the Claude Desktop
   registry check, toasts, local repo checkouts, endpoint-facing PowerShell work,
   attended checkout. Two daemons cannot share state under the single-writer rule.
   Recommendation is one daemon on EC2 plus a small workstation agent for
   desktop-only checks, agreed before phase 4.

## Not covered

- Cost with confirmed pricing.
- Whether the claude.ai connectors (Slack, Drive, Gmail, Calendar) behave the same
  on the box. They authenticate as the signed-in claude.ai account, not the
  machine, so they should follow the move and stay `human`-attributed as the
  identity document accepts. Not verified.
- Backup and recovery. Otto's state is the board, the journal, and the audit
  record. Whether that needs more than a state copy is not answered here.
