# Agent identity

How an agent gets a principal of its own, instead of inheriting the human's.

`otto identity` is the live version of this document; if the two disagree, the
command is right and this file is stale.

## The problem, stated without flattery

Otto runs as its owner. It uses the owner's MCP registrations, the owner's SSO
sessions, the owner's claude.ai connectors and the owner's Claude seat. Every other
personal agent built the same way does too, for the same reason: the fastest way to
give an agent capability is to hand it the credentials already sitting on the
machine.

What Otto has solved is worth naming precisely, because the gap is easier to see once
the solved parts are off the table:

| Control | How | Where |
| --- | --- | --- |
| Enumerated work, with origin | the board; every filed item carries the agent and run that proposed it | `board.py`, `findings.py` |
| Propose, do not act | `auto=False` on anything filed by an agent; tier gating in `/orchestrate` | `dispatch.py` |
| Durable audit | stored events and notices with timestamps, not a chat log | `store.py` |
| Accounting that cannot hallucinate | the journal rollup and `advisor.py` contain no model call | `advisor.py` |
| Liveness | heartbeat watchdog; a dead loop is louder than a failed one | `heartbeat` |
| Policy in code, not prose | `WEEKEND_PAUSES_WORK` is a constant enforced by `is_due()`, not a markdown line an agent can rewrite | `config.py` |

Every one of those is a claim Otto makes about itself. Attribution is the only claim a
*target system* makes about Otto, which is why it outranks all of them, and why
"Otto is observable" is not an answer to "whose account was that".

Concretely, on a fresh install: an action Otto takes in the identity provider appears
in its system log as whatever shared service app the MCP server was registered with;
in the cloud provider as the owner's SSO role session; in Slack, Drive and Notion as
the owner personally. Nothing in any of those logs distinguishes the agent from the
human. `otto identity` counts it, and on a fresh install the count is **0
integrations attributable to Otto**.

## Three states, and the one that looks like a fix but is not

- `human`: authenticates as the person. Agent action and human action are the same
  record. This is the gap.
- `shared-service`: a non-human principal, but one the human's interactive sessions
  and other automation also use. The log names the app, not who drove it. Better than
  `human` for blast radius and rotation; **not** attribution.
- `agent`: a principal only this agent holds. The target system's own log names it.

Most "we gave the automation a service account" stories stop at `shared-service` and
describe themselves as having done the third thing. Otto's table refuses to, which is
the only reason the number is trustworthy.

## Rules

1. **A non-human principal is not shaped like a human.** No mailbox, no IdP user
   seat, no MFA enrollment, no group memberships that grant human app access. An
   identity that can receive a password reset email is an identity that can be phished
   into an account takeover with no human to notice.
2. **The credential lives in a store and is checked out at use time.** Not inline in
   `~/.claude.json`: a secret there is readable by every process that can read the
   home directory, is handed to every MCP client that starts, and is rotatable only by
   hand. Your secrets CLI is the checkout path, and `OTTO_GUARD_CRED_COMMANDS` names
   it so the guard can see it.
3. **Scope per integration, not per agent.** One principal with every scope is the
   inline problem with extra steps. Otto reading EDR detections does not need EDR
   write.
4. **The principal must be nameable in the target system's log before it counts.**
   If the system exposes no per-principal audit field, a dedicated credential buys
   blast-radius containment and nothing else. Say so rather than counting it.
5. **Unattended sessions cannot check out credentials.** Enforced by
   `scripts/otto_guard.py`: an Otto-spawned session hits `OTTO_UNATTENDED=1` and the
   guard gates the checkout, so a scheduled agent cannot silently acquire a secret
   that was not already provisioned for it. This is a control, not an inconvenience,
   and it is why the steps below are deliberately human-run.

## Provisioning, in the order worth doing it

### 1. EDR and RMM (no blocker, highest value per hour)

On a typical install both are `shared-service` with the secret **inline in
`~/.claude.json`**, which is the antipattern this whole document exists to remove,
and both are a console form away from fixed.

- EDR console: API clients, create `otto-agent`, scopes **read-only only**
  (detections read, hosts read, incidents read). Not the wide client's scopes.
- RMM: create a second API application for Otto, monitoring scopes only.
- Put both secrets in the credential store, remove the `env` blocks from
  `~/.claude.json`, and have a wrapper script check out at launch. The pattern to
  copy is an MCP server registered as a wrapper script with **no `env` block at
  all**, so the key is acquired at start rather than sitting in the registry.
- Flip `credential` to `store` and `principal` to `agent` in `config.PRINCIPALS`, and
  lower `INHERITED_BASELINE` in the same change.

Audit proof: the EDR's API audit log records the client id per call. The RMM's
activity log records the API application. Two distinct names where there was one.

### 2. Cloud: an IAM role, not an IdP user

Recommended: an IAM role in each account Otto needs, trusting a machine identity for
Otto, with a permissions boundary. **Not** an SSO directory user.

Why not the IdP route: an Otto user in the IdP means a licensed seat, a human-shaped
account in the directory, and something that shows up in every user report and access
review as a person who is not one. The role route costs no seat and CloudTrail records
`assumed-role/otto-agent/<session>`, which is exactly the attribution wanted.

Start read-only in one account. `sts get-caller-identity` returning the Otto role
rather than the SSO role is the acceptance test. `docs/otto-on-ec2.md` makes this
concrete.

### 3. Identity provider: a second service app, which needs a super admin and cannot be automated

Many IdP SKUs have **no API access management**: no custom authorization servers, no
custom scopes, no client-credentials tokens for machine clients. The working pattern
is an API service app using **private_key_jwt** against the org authorization server.
Otto's key never leaves the store, and no shared secret exists to leak.

The blocker is real and worth stating rather than working around: creating the app
may be possible with the app-management scope the existing service app already holds,
but **granting it scopes requires an admin who holds those scopes**, and a service app
cannot self-escalate. So this step is a human in the IdP console with super admin,
once, per scope. That is a feature.

Scopes to grant, and no more: users read, groups read, logs read. Otto reads the
IdP; offboarding scripts that write are run attended by the owner and should keep
using their admin session, because a deactivation should have a human's name on it.

Audit proof: the system log `actor` becomes the Otto app's client id, distinct from
the shared service app.

### 4. Notion: an internal integration, not the user OAuth connector

The MCP connector authenticates as the owner's Notion account, so page history says
the owner edited the page. A bot principal means a Notion internal integration token
shared only to the pages Otto needs, which also fixes the over-broad access: the
connector can see everything the owner can.

### 5. Leave these alone, on purpose

- **claude.ai connectors** (Slack, Drive, Gmail, Calendar, and the rest). A
  connector authenticates as the signed-in claude.ai user. There is no per-agent
  principal to ask for. Separating this needs a second claude.ai identity or
  replacing the connectors with self-hosted MCP servers holding Otto's own tokens,
  which is a large build. This is the biggest remaining block of inherited access and
  the honest answer is "product limitation, accepted, written down".
- **Claude inference.** Otto's agents spend the owner's seat. A separate seat or an
  API-key workspace is a spend decision, not a config change.
- **Admin APIs with no per-key audit surface.** A dedicated key is trivial and proves
  nothing if the provider cannot tell you which key did what. Blast radius only.

## Verifying it, and keeping it verified

```powershell
otto identity          # the scoreboard
otto identity -v       # audit field and blocker per row
otto identity --json   # for a risk-acceptance doc
```

The table lives in `config.PRINCIPALS`; the checker is `otto/identity.py`. The split is
deliberate and matches `CAPABILITIES`/`manifest.py`: the declaration is data, the
mechanism is code, and neither can be quietly edited to make the other look better.

What alarms and what does not is the important part:

- The **standing gap does not alarm.** It is known, accepted, and on the board.
  A detector that fires every morning about a thing the owner chose gets ignored, and
  then it is not a detector.
- **Regression alarms.** A secret appearing inline where the table says `store`. A new
  integration wired to the owner's account, i.e. the `human` count rising above
  `INHERITED_BASELINE`. A registered MCP server holding an inline secret that no row
  declares. Those all mean something changed that nobody decided, which is the only
  identity finding worth interrupting a morning for.

It found one on its first run: an MCP server registered in the Claude **Desktop**
registry with an inline API key, declared nowhere. Not triaged here; it went on the
board.

## Why this is the pattern and not a proposal

Anyone building their own personal agent will hit this in the same order: capability
first, identity never, because inheriting the human's tokens works immediately and the
cost only shows up in an incident, when nobody can tell which actions were the agent's.

Two things make this shareable rather than aspirational. Attribution is **counted**, so
"we should scope agent credentials" becomes a number that has to move. And the count
is produced by code reading reality, not by a paragraph an agent could rewrite, so it
cannot flatter itself: `unverified` never reports `ok`, and a shared service account
never reports as attribution.

That is also what makes a risk-acceptance signable. Not "the agent is well behaved",
but: here are the integrations, here is which principal each one uses, here is what
each target system's log will show, here is the specific blocker on the ones still
inherited, and here is the check that alarms the day a new one is added.
