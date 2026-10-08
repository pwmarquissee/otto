---
description: Assess unjudged backlog cards, route them, and promote what may safely run. Designed for /loop.
---

Work the *unassessed* end of the Otto board. `/orchestrate` decides what to promote
among cards that already carry a judgment; this pass is what produces that judgment.

## Why this exists

Feeds write onto the backlog hourly (`slack-dm-fetch` every 1h, `slack-sweep` and
`meeting-notes` every 4h). When assessment happened once a day, as one step of a
daily chain capped at three cards, intake exceeded assessment every single day, so the
backlog only ever grew, and at one point most of the cards on the board carried no
tier at all. A card with no tier cannot pass any gate, so it was not merely unsorted,
it was unreachable.

## The one rule that matters

**Never `otto task mv <id> queued`.** Use:

```
python -m otto triage promote <id>
```

`queued` spawns a real Claude Code session with `--dangerously-skip-permissions`.
`triage promote` is a gate: it refuses anything not assessed, not owned by `otto`, not
`ready`, carrying no detail, over the in-flight cap, `tier-2-assistive`, or `tier-1-approval`
without either `--prepare` or an approved plan, and it tells you which. `task mv` is a plain state write and checks none of that.

If a promote is refused, the refusal is the answer. Do not work around it.

## Assess each card

```
python -m otto triage list --json --limit 20
```

Read the card's `detail`. It usually carries the real context: the verbatim Slack
message, the meeting quote, the evidence from a sweep. Then record three judgments and
a note:

```
python -m otto triage set <id> --owner <otto|owner> --tier <tier> --readiness <r> --note "<one line>"
```

### owner: who can actually do this

This is the field that was missing, and it is the important one. Most of this board is
the owner's own commitments, not agent work.

- **`owner`**: anything that needs their voice, their relationships, their authority,
  or their hands. "Follow up with the producer", "send the CFO a shipping label",
  "order a contractor's build", "answer a colleague's thread", any approval, any spend,
  any conversation. (The literal value is the owner's slug, as `otto triage set --help`
  shows it.)
- **`otto`**: self-contained technical work an unattended agent can finish and verify:
  a log analysis, an audit, a config read, a scripted change with a clear success test.

When in doubt this is the owner's. An agent dispatched at the owner's work does not
just fail, it acts on their behalf without them.

### tier: how much damage a mistake does

- **`tier-0-autonomous`**: read-only or trivially reversible. Analysis, audits,
  reports, triage.
- **`tier-1-approval`**: the work may be done but changes must not be applied without
  the owner. Anything that writes to production, identity, money, or another person.
- **`tier-2-assistive`**: gather and propose only; the owner executes.

Ambiguous is `tier-1`. Guessing low is the expensive mistake.

### readiness: can it be started at all

- **`ready`**: everything needed is present.
- **`needs-info`**: something knowable is missing. A version, a quote, a log, an id.
- **`needs-decision`**: a person has to choose. No amount of gathering resolves it.

### the note

One line, for the ranked list. `otto next` shows this instead of the detail, which is
why the list was unreadable before. Say what it is and what it is waiting on, not what
the evidence was:

> Waiting on the CFO's quote before the SKU can be ordered.

Not a paste of the Slack thread. The detail already holds that. Use `--note-file` if the
note has to quote a command line, for the same reason `--detail-file` exists.

## Then route it

- **`otto` + `tier-0-autonomous` + `ready`**: `python -m otto triage promote <id>`.
  The gate will stop you if any part of that is wrong.
- **`otto` + `tier-1-approval` + `ready`**: `python -m otto triage promote <id> --prepare`.
  This runs the card in PREPARE mode: the session investigates, writes a proposal
  (facts found, assumptions, numbered steps with exact commands, what it will not
  do) into the card's `plan`, applies nothing, and the card lands in `needs-you`.
  The owner edits and approves the plan (`otto task plan <id> --approve`); only then
  does `triage promote <id>` (no flag) let it run, with the approved plan as its
  prompt. Never work around the gate by promoting tier-1 without `--prepare`; the
  refusal tells you which path applies. Prepare runs count against the in-flight cap.
- **`otto` + `needs-info`**: you can often just get it. Read the log, check the repo,
  query the API. Then `--detail-file` the finding onto the card, set `--readiness ready`,
  and promote it on the next pass.
- **owner + `needs-decision`, and something is waiting on it**: move it to
  `needs-you` so it stops sitting in a pile of fifty:
  `python -m otto task mv <id> needs-you`. Only when a decision genuinely unblocks
  something. Do not use it to mean "important".
- **everything else owned by the owner**: leave it in `backlog`. It is now assessed, so
  it ranks properly and reads as one line. That is the whole fix for it.

## Stale cards

```
python -m otto triage list --stale --json
```

Cards not checked for `TRIAGE_STALE_DAYS` (10). Age is why you **look**, never why
you close. Age counts from the LAST CHECK, not from creation, and a card with a
`next_look` date in the future is not listed at all.

**When you have looked and the card is still open, record that in one line and
stop:**

```
python -m otto triage checked <id> --note "EDR still shows 1 unmanaged sensor, blocked on the user's consent" --next-look 2026-09-23
```

That moves `last_checked`, appends ONE dated line to the detail, and keeps the card
out of this list until the date. Do not append a re-verification essay to the
detail: in one two-week stretch a single card collected eight near-identical daily
re-verifies, and the evidence trail on four others was wiped when a pass used
`--detail-file` (which replaced) believing it appended. If you must add evidence to
the detail, use `otto task set <id> --detail-file <path> --append`.

Set `--next-look` whenever the card is waiting on a person or a date: nothing you
can read will change before then, so re-reading it is spend with no information.

Close one only on positive evidence that the work actually happened: the PR is merged,
the machine shipped, the account exists, the ticket is resolved. Go and check. Then:

```
python -m otto task set <id> --detail-file <path>   # append what you verified
python -m otto task mv <id> done
```

The card must end up saying what the evidence was and where you found it. "Looks old" is
not evidence, and neither is "probably handled". If you cannot verify it, leave it
open and say so in the note. A card wrongly closed is worse than a card left sitting:
sitting is visible, closed is not.

## Fading

```
python -m otto triage fade --dry-run
python -m otto triage fade
```

Run this once per pass, after assessing. It moves the owner's own harvested
commitments (feed- or agent-filed, owned by them or unassessed, no due date, priority
below high, not a decision, not carrying a protected tag) that nobody has touched for
`FADE_DAYS` (14) to `faded`. Faded is not done and not deleted: the card keeps its
history, lists with `otto task ls --faded`, and `otto task mv <id> backlog` brings it
back. It just stops being counted. Put the faded titles in the report so the owner
sees what left.

Do not fade by hand for any other reason, and never fade a card owned by `otto`.

## Findings about Otto go to DEBT.md, not the board

If during the pass you notice a bug in Otto itself (a CLI verb that misbehaves, a
loop that misfires, a dedupe miss, this command's own wording), do NOT
`otto task add` it. Append it to `DEBT.md` in the Otto checkout (title, what you saw,
where), or set `"about": "otto"` in the findings block and Otto routes it there
itself. In one week this pass once filed 41 cards about Otto onto the owner's board.
Their board is their work; Otto's bugs are worked in the repo.

## Excuses this pass has made before

Each row is a way a run has talked itself past one of the rules above. The right column is the answer.

| Rationalization | Reality |
|---|---|
| "The gate refused, but I can see the card is fine, `task mv` will do it" | The refusal is the answer. `task mv` skips every check the gate exists for. |
| "It is obviously tier-0, no need to read the whole detail" | A wrong tier is worse than no tier because the gate trusts it. Read the detail. |
| "It is technical, so `otto` can own it" | Technical is not the test. If it needs their voice, relationships, authority, or spend, it is the owner's. |
| "The tier-1 card is simple, promoting without `--prepare` saves a round trip" | The round trip is the approval. Never. |
| "The cap is full, but I will promote one more so it is ready" | The cap protects the ability to react to the first failure before the next starts. Say the cap is full and stop. |
| "It has sat three weeks and nobody cares, close it" | Age is why you look, never why you close. Positive evidence or leave it open and say so. |
| "I will append a quick re-verify note to the detail" | One card collected eight. Use `triage checked` with `--next-look`. |
| "Fifty cards, I will skim them all this pass" | Batches of 10-15. Fifty skims are fifty wrong tiers. |
| "The Otto CLI misbehaved, I will file a card so it is tracked" | DEBT.md. Forty-one of those landed on the owner's board in one week. |
| "The detail is thin, I will fill in what the card probably means" | Never invent. Set `needs-info`, or go and get the missing fact and append it. |

## Rules

- **Assess in batches of 10-15.** Reading the detail properly is the job; skimming
  fifty produces fifty wrong tiers, and a wrong tier is worse than no tier because the
  gate will then trust it.
- **Never invent a card.** This pass judges what is there.
- **Do not re-assess a card that already carries a judgment** unless its detail
  changed. `triage list` only returns unassessed cards, so trust it.
- The in-flight cap is 3. If nothing is promotable because the cap is full, that is the
  system working: say so and move on.

## Report

```
python -m otto triage status
```

Then one short summary:

```
triage {date} {time}
- assessed: {N} ({M} yours, {K} otto)
- promoted: {N} ({ids}); preparing: {N} ({ids})
- proposals awaiting your approval: {N} ({ids})   <- otto task plan <id>
- moved to needs-you: {N} ({ids})
- checked, still open: {N}; closed on evidence: {N} ({ids})
- faded: {N} ({titles, one line each})
- backlog: {before} -> {after}, {N} still unassessed
```

If everything is assessed and nothing is promotable, say "backlog clear, nothing to
promote" and stop.
