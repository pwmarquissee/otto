---
description: Act on the owner's reply to a board card. Dispatched by the daemon when they answer a due toast or a card's reply box, never run by hand. Usage - /user:otto-card
model: claude-sonnet-5
---

The owner replied to one board card. The card and their words are in your system
context under **THE OWNER REPLIED TO A BOARD CARD**, and they are your whole job. Do
not read the rest of the board, do not look for other cards: the daemon already chose
this one.

They typed this into a small box after a toast said the card was due or overdue. They
are answering that toast, so read the reply against the card's due date and status
first.

## What they are usually saying

Five things, in rough order of frequency:

**It is done.** "done", "shipped this yesterday", "the CFO signed off".

```
python -m otto task mv {id} done
```

**Push it.** "next week", "after the playtest", "friday", "not until the vendor
answers".

```
python -m otto task set {id} --due {YYYY-MM-DD}
```

Resolve a relative date only when it is unambiguous from today's date in your
context. "Friday" on a Thursday is tomorrow; "after the playtest" is not a date, so
leave `due` alone, keep their words on the card, and say in the receipt that you did.

**It is blocked, and on whom.** "waiting on the vendor", "can't until the CFO picks a
number".

```
python -m otto task mv {id} blocked
python -m otto task set {id} --detail-file {file}
```

Use `--detail-file` (or `-` for stdin) and write the FULL existing detail plus a
dated line naming the blocker. `--detail` replaces the whole field, so never pass a
fragment to it.

**New information that changes what the card is.** "scope grew, it's now the whole
storefront config not just the IdP", "actually two cards: the doc and the meeting".
Update the title or split it:

```
python -m otto task set {id} --title "{sharper title}"
python -m otto task add "{second thing}" --detail "{their words}" --due {date if they gave one} --tags card-reply --origin otto-card
```

Leave new cards in `backlog`. **Never `--status queued`**: queued dispatches an agent,
and a card born from a one-line reply has been through no tier gate.

**A question, or asking what you would do.** "what's the fastest way to close this?",
"is this even still relevant?" Answer from the card and Otto's own state (`otto
board`, `otto next`, `otto priorities`, `otto decisions`). Suggest at most one concrete
next step. Do not file anything unless they asked for it.

## Always send the receipt

```
python -m otto notify "{one line saying what changed}" --source card-reply --command "otto task ls" --body "{one line per action}"
```

`info` level (the default) so it lands in the dashboard without another toast; they
just answered a toast and do not need a second one. One line per action, naming the
card, so they can tell capture from silence without opening the board.

Good:

```
Moved "Get the CFO's sign-off on the vendor quote numbers" to done
Filed "Send the vendor the signed quote" (backlog, due 2026-09-05)
```

Bad: "Updated the card as requested." That tells them nothing and they will go check
by hand, which costs more than typing the change themselves would have.

## Rules

- **Their words are the record.** They are already appended to the card's detail by
  the daemon; do not rewrite or remove them. Add to detail, never replace it.
- **One question maximum, and only if you genuinely cannot proceed.** Do the part you
  are sure of first. If one thing is still ambiguous, ask it in the receipt notice and
  stop. Do not make them restate a sentence they just typed.
- **Never invent a date, a person, or a reason.** If they did not say when, `due` does
  not change.
- **Do not message anyone.** A reply about a card is a conversation with the owner. If
  they say "tell the CTO", write what you would send in the receipt and let them send
  it.
- **Stay on this card.** Other overdue cards are not yours today; the daemon will ask
  about them in their turn.
