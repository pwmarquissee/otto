# Vendored browser libraries

Served by the daemon under `/static/vendor/`. Pinned copies, not CDN links, so the
dashboard works when the machine is offline and a CDN outage cannot change what
the control room runs.

| File | Package | Version | Source |
| --- | --- | --- | --- |
| `xterm.js` | `@xterm/xterm` | 6.0.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.js |
| `xterm.css` | `@xterm/xterm` | 6.0.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.css |
| `addon-fit.js` | `@xterm/addon-fit` | 0.11.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.js |
| `addon-webgl.js` | `@xterm/addon-webgl` | 0.19.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-webgl@0.19.0/lib/addon-webgl.js |

All three are UMD builds. Loaded as plain scripts they expose `window.Terminal`
(the Terminal class itself, copied onto the global per export) and
`window.FitAddon.FitAddon`.

To upgrade: download the new files with the same names, update this table, and
check `otto/web/term.js` still constructs `new Terminal(...)` and
`new FitAddon.FitAddon()` against the new globals.
