# Terminal relay

The Otto dashboard can embed any herdr pane as a live terminal. herdr already owns every
pane's PTY and streams it over a child process's stdio (`herdr terminal session
observe|control <target>`); `otto/termrelay.py` wraps one such process per viewer, and
`otto/web_term.py` mounts it on the daemon as a websocket. The browser side is xterm.js
writing the frames straight in. The daemon never touches the PTY itself.

## Protocol (the contract with otto/web/)

- `WS /ws/term/{target}?cols=N&rows=M&mode=control|observe&takeover=0|1`. Target is an
  agent name (`otto`) or a pane id (`w1:p1`). `control` is the default; `takeover=1`
  replaces the current controller. Observers are read-only and unlimited.
- Server to client: `{"t":"frame","b":"<base64 ANSI>"}`, `{"t":"closed","reason":..}`,
  `{"t":"error","m":..}`. An unknown target or bad mode gets one error then close 1008.
- Client to server: `{"t":"input","d":"<text>"}` (`\r` is Enter), `{"t":"resize","cols":..,
  "rows":..}`, `{"t":"scroll","delta":<rows, negative is up>}`, `{"t":"release"}`.
- `GET /api/term/panes` lists every pane with its workspace label, cwd, agent kind, agent
  name (`target` is the name when it has one, else the pane id), status and title.

Underneath, herdr's records are `terminal.frame` (base64 `bytes`, `seq`, `full`) and
`terminal.closed`; stdin takes `terminal.input` (`text`, not `data`), `terminal.resize`,
`terminal.scroll` (`direction` up/down plus unsigned `lines`) and `terminal.release`.
Bursts of frames within 16 ms are joined before they hit the socket.

## Verify by hand

1. `python -m pytest tests/test_termrelay.py -q` (fake herdr, no real pane).
2. `herdr terminal session observe otto --cols 100 --rows 30` prints one frame and waits.
3. With the daemon up, `GET /api/term/panes` lists the panes; open
   `ws://127.0.0.1:8787/ws/term/<pane id>?cols=100&rows=30`, send
   `{"t":"input","d":"echo relay-ok\r"}` and the echo comes back in a frame. Use a scratch
   workspace (`herdr workspace create --cwd <otto checkout> --label relay-test --no-focus`), never
   the live `otto` pane, and close it after (`herdr workspace close <id>`).
