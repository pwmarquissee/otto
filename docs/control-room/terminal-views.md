# Terminal views

The dashboard shows any herdr pane as a live terminal. herdr still owns every pane;
the daemon relays bytes over `ws://host/ws/term/<target>` (`otto/web_term.py`) and
the browser renders them with xterm.js (vendored, see `otto/web/vendor/README.md`).

Three views, after keitora's shapes:

- **Terminal** (`^⌥T`): one pane at full fidelity under a strip of tabs listing
  every pane (status dot, name, cwd basename, `^⌥1..9` picks one). The
  control/observe toggle says whether your keys go to the pane. One controller per
  pane; observers are unlimited. When someone else holds control the overlay
  offers Reconnect, Take over, or Observe instead.
- **Grid** (`^⌥G`): every pane tiled, two across (three above 1400px), observe
  mode unless Terminal already controls one. Click a name to open it in Terminal.
- **Split**: Dispatch gains a terminal region under its lanes. Clicking a rail row
  still focuses the pane in herdr's own window, and now also shows it here.

Pane lifecycle (`otto/web/term.js`, `window.OttoTerm`): a pane is created once per
target and belongs to no view. `render()` rebuilds the centre every few seconds, so
a view only calls `OttoTerm.mount(target, host, opts)` to say where the pane goes
this frame; its container, xterm scrollback and socket are moved, not recreated. A
MutationObserver spots containers a rebuild dropped and parks them in a detached
host; a pane parked for more than 30 seconds closes its socket and reopens on the
next mount, which keeps Grid cheap while you are on the Board. A pane the relay
closed stays closed until you press Reconnect, so it cannot fail in a loop.

Observers have no stdin: herdr refuses input, resize and scroll on an observe
stream. herdr sends one full frame when a stream opens and only changed cells after
that, so an observer whose box changes size (a Grid tile, the Dispatch split, a
window resize, coming back to Terminal) reopens its stream at the new size and takes
the full frame that comes with it, after a 400 ms settle. Before this, the local
resize dropped the screen and nothing ever repainted it: the pane went black and
stayed black (2026-10-02, measured in the desktop shell). The focused target and
mode persist in localStorage. The pane
list is `/api/term/panes` merged with `state.logistics.rail`; without the endpoint
the strip still works from the rail alone.
