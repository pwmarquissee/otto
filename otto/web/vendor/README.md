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
| `phosphor/regular/style.css`, `phosphor/regular/Phosphor.woff2` | `@phosphor-icons/web` | 2.1.1 | https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/ |
| `phosphor/fill/style.css`, `phosphor/fill/Phosphor-Fill.woff2` | `@phosphor-icons/web` | 2.1.1 | https://unpkg.com/@phosphor-icons/web@2.1.1/src/fill/ |

The icon stylesheets are the package's own, with the `@font-face` source list cut
to woff2 (the woff, ttf and svg fallbacks are weight no Chromium build needs); the
license is `phosphor/LICENSE` (MIT). The xterm files are UMD builds. Loaded as plain scripts they expose `window.Terminal`
(the Terminal class itself, copied onto the global per export) and
`window.FitAddon.FitAddon`.

To upgrade: download the new files with the same names, update this table, and
check `otto/web/term.js` still constructs `new Terminal(...)` and
`new FitAddon.FitAddon()` against the new globals.
