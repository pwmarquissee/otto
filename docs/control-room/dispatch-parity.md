# Dispatch parity with keitora (everything but the terminal)

The Dispatch view is modeled on keitora's control room. This page lists what Otto
has of it as of 2026-10-01, and where each piece lives. The embedded terminal is
the one part not covered here; that is `otto/web/term.js`.

**Session inspector.** Clicking a rail row (or a session in the palette) selects
the session. The inspector shows the state as the hooks and herdr each see it,
pane and agent, directory, host, how long in that state, the last message or the
question it is blocked on, the running card, and the ledger cost (loaded lazily
from `/api/ledger/sessions/{sid}`). Verbs: Focus (`POST /api/herdr/focus/{agent}`),
Open (`POST /api/sessions/{id}/open`), Rename (`PATCH /api/sessions/{id}`), and
Dispatch a card, a picker of the cards the promotion gate would pass. Code:
`sessionLiveBlock` in `otto/web/app.js`. The task inspector gains "Running in"
with Focus for a card on a pane, and "Dispatch ->" per idle target for an open card.

**Drag to dispatch.** Drag a card from the Open lane onto a session lane. The lane
highlights; a lane that is working, blocked, or already carrying a card refuses
with a toast. Same `dragId` pattern as the board. `POST /api/logistics/dispatch`.

**Palette.** Ctrl+K lists live sessions (title or agent, status), one row per
suggestion (`otto dispatch approve <id>`), one per open card x idle target
(`otto dispatch to <id> <agent>`), and `otto herdr open`. Every row is a real
command; `liveHits` in app.js builds them.

**Rail preview.** Working rows show the pane's last screen line from
`GET /api/herdr/peek/{target}`, cached 10s per target in the daemon and mirrored
in the browser, so polling never turns into a herdr read per render.

**Notifications.** `herdr.sync` posts the same waiting notice the hooks would when
a pane turns `blocked` and the hooks did not already say waiting, with the
question read off the screen, and retires it when the pane moves on. Blocked rows
glow (off under `prefers-reduced-motion`); `done` rows carry a badge that herdr
clears on focus.

**Worktrees.** "New worktree" in the bar posts `/api/herdr/worktree/create`
`{cwd, branch, label}`. A daemon without that endpoint answers 404 and the UI
says so instead of failing silently.
