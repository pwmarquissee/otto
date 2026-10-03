# Agent identity

How Otto gets a principal of its own instead of using the owner's. `otto identity`
is the live version of this document. If the two disagree, the command is right.

## The gap

Otto runs as its owner. It uses the owner's MCP registrations, SSO sessions,
claude.ai connectors, and Claude seat. On a fresh install, an action Otto takes in
the identity provider appears as whatever shared service app the MCP server was
registered with, in the cloud provider as the owner's SSO role session, and in
Slack, Drive, and Notion as the owner. No log tells the agent and the human apart.
`otto identity` counts this. On a fresh install the count is 0 integrations
attributable to Otto.

Otto already has the controls that do not depend on the target system:

| Control | How | Where |
| --- | --- | --- |
| Enumerated work, with origin | the board; every filed item carries the agent and run that proposed it | `board.py`, `findings.py` |
| Propose, do not act | `auto=False` on anything filed by an agent; tier gating in `/orchestrate` | `dispatch.py` |
| Durable audit | stored events and notices with timestamps | `store.py` |
| Accounting without a model | the journal rollup and `advisor.py` make no model call | `advisor.py` |
| Liveness | a schedule or feed with `max_age_hours` alarms when it goes quiet | `advisor.py`, `feeds.py` |
| Policy in code | `WEEKEND_PAUSES_WORK` is a constant the scheduler checks, not a markdown line | `config.py`, `runners/scheduled.py` |

Those are claims Otto makes about itself. Attribution is the one claim a target
system makes about Otto, so it outranks them.

## Three states

- `human`. Authenticates as the person. Agent action and human action are the same
  record. This is the gap.
- `shared-service`. A non-human principal that the human's sessions and other
  automation also use. The log names the app, not who drove it. Better blast
  radius and rotation, no attribution.
- `agent`. A principal only this agent holds. The target system's log names it.

Most "we gave the automation a service account" setups stop at `shared-service`.
Otto's table does not count that as attribution.

## Rules

1. A non-human principal is not shaped like a human. No mailbox, no IdP user seat,
   no MFA enrollment, no group memberships that grant human app access. An
   identity that can receive a password reset email can be phished.
2. The credential lives in a store and is checked out at use time, not inline in
   `~/.claude.json`, where every process that reads the home directory can read
   it and rotation is by hand. Your secrets CLI is the checkout path, and
   `OTTO_GUARD_CRED_COMMANDS` names it so the guard can see it.
3. Scope per integration, not per agent. Reading EDR detections does not need EDR
   write.
4. The principal must be nameable in the target system's log before it counts. If
   the system has no per-principal audit field, a dedicated credential buys
   blast-radius containment only. Record it that way.
5. Unattended sessions cannot check out credentials. `scripts/otto_guard.py` gates
   the checkout when `OTTO_UNATTENDED=1`, so a scheduled agent cannot acquire a
   secret that was not provisioned for it. The steps below are run by a person.

## Provisioning order

### 1. EDR and RMM

Highest value per hour, no blocker. On a typical install both are `shared-service`
with the secret inline in `~/.claude.json`.

- EDR console: create an API client `otto-agent` with read-only scopes
  (detections, hosts, incidents).
- RMM: create a second API application for Otto, monitoring scopes only.
- Put both secrets in the credential store, remove the `env` blocks from
  `~/.claude.json`, and register each MCP server as a wrapper script with no `env`
  block that checks the key out at launch.
- Add a row for each to `config.PRINCIPALS` with `credential=store` and
  `principal=agent`, and lower `OTTO_INHERITED_BASELINE` in the same change.

Audit proof: the EDR's API audit log records the client id per call, and the RMM's
activity log records the API application.

### 2. Cloud

An IAM role in each account Otto needs, trusting a machine identity for Otto, with
a permissions boundary. Not an SSO directory user, which costs a seat and puts a
non-person in every access review. CloudTrail then records
`assumed-role/otto-agent/<session>`.

Start read-only in one account. The acceptance test is `sts get-caller-identity`
returning the Otto role, not the SSO role. [otto-on-ec2.md](otto-on-ec2.md) makes
this concrete.

### 3. Identity provider

Many IdP SKUs have no API access management (no custom authorization servers,
custom scopes, or client-credentials tokens). The working pattern is an API service
app using `private_key_jwt` against the org authorization server. Otto's key never
leaves the store, and no shared secret exists.

Granting the app scopes needs an admin who holds those scopes, and a service app
cannot self-escalate. This step is a person in the IdP console with super admin,
once per scope.

Scopes: users read, groups read, logs read. Offboarding scripts that write stay
attended under the owner's admin session, so a deactivation carries a human's name.

Audit proof: the system log `actor` becomes the Otto app's client id.

### 4. Notion

The MCP connector authenticates as the owner, so page history names the owner and
the connector sees everything the owner can. Use an internal integration token
shared only to the pages Otto needs.

### 5. Leave alone

- claude.ai connectors (Slack, Drive, Gmail, Calendar, and the rest). A connector
  authenticates as the signed-in claude.ai user. Separating this needs a second
  claude.ai identity or self-hosted MCP servers holding Otto's own tokens. Accepted
  as a product limitation.
- Claude inference. Otto's agents spend the owner's seat. A separate seat or
  API-key workspace is a spend decision.
- Admin APIs with no per-key audit surface. A dedicated key proves nothing if the
  provider cannot say which key did what.

## Verifying it

```
otto identity          # the scoreboard
otto identity -v       # audit field and blocker per row
otto identity --json   # for a risk-acceptance doc
```

The table is `config.PRINCIPALS` and the checker is `otto/identity.py`. The
declaration is data and the mechanism is code, same as `CAPABILITIES` and
`manifest.py`. `unverified` never reports `ok`, and a shared service account never
reports as attribution.

The standing gap does not alarm. It is known, accepted, and on the board.
Regression alarms: a secret appearing inline where the table says `store`, the
`human` count rising above `OTTO_INHERITED_BASELINE`, or a registered MCP server
holding an inline secret that no row declares.

## Limits

- The count covers integrations declared in `config.PRINCIPALS`. An integration
  with no row is invisible to it, apart from the undeclared-inline check over
  registered MCP servers.
- The checker reads the registries on this machine. It cannot see what a target
  system's log recorded. Check that once per integration by hand.
