# Otto, in a window

A Tauri shell around the dashboard the daemon already serves. It is a **window, a
tray icon and a hotkey**, and deliberately nothing else: every behavior stays in
Python, where it is tested.

This is cheap because Otto's UI is already a web app talking to an HTTP API on
loopback. Tauri points a window at `http://127.0.0.1:8787` and adds the parts a
browser tab cannot do.

Windows only, for now. The scheme registration and autostart below are HKCU
registry writes.

## The rule this exists to obey

**The daemon must outlive the window.**

Otto's premise is that the floors hold when nobody is looking: schedules fire, feeds
ingest, tasks dispatch. If closing a window stopped any of that, Otto would quietly
stop being Otto, and the symptom would be silence, which is indistinguishable from a
quiet day.

So the shell may **start** the daemon and must **never** stop it:

| Action | Window | Daemon |
| --- | --- | --- |
| Launch app | opens | started if down (`otto ensure`, idempotent) |
| Close window (X) | hides to tray | untouched |
| Quit from tray | exits | **untouched, on purpose** |
| `otto stop` | unaffected | stops, a separate and deliberate act |

Verify both directions rather than assuming them: killing the app should leave the
daemon on the same pid, and launching the app with the daemon stopped should bring
it back on a new one.

## `otto://` links land here, not in a browser

A due toast's **Reply** button opens `otto://reply/<card id>`. The shell registers the
`otto` scheme in HKCU on every start (`register_all`, because this exe runs from
`target/release` and no installer did it), so Windows hands the link to Otto's own
window rather than to whatever owns `http://`. The first version used an `http://`
link and opened the default browser instead, which is the bug this exists to avoid.

- Otto running in the tray: single-instance forwards the URL to the running app, the
  window shows, and the dashboard's `#reply=<id>` handler opens that card's sheet.
- Otto not running: the link starts it, and the URL in its own argv is handled the
  same way after the window exists.
- `otto://open` just shows the window.

The Python side (`otto.notify.reply_url`) checks for the registered key before
choosing `otto://` over the http form, so a machine without the shell still gets a
working link in a browser. `OTTO_REPLY_SCHEME=otto|http` forces either.

## The tray is a status light

Not a launcher. It polls `/api/state` every 20s and reads the same signals `otto
doctor` and the dashboard read, so it cannot disagree with what you see when you open
the window.

- **green** floors holding
- **amber** a schedule has gone stale, or an API integration is down. Hover for what.
- **grey** the daemon is not running. Not red: that is a state a human may have chosen.

The icon is only rewritten when the state *changes*; the tooltip refreshes every poll
because the detail behind a warning moves.

## Starting with Windows

On by default, and toggleable from the tray, because an autostart entry somebody has
to find in the registry to remove is a thing done to a machine rather than a setting.
The entry is `HKCU\...\CurrentVersion\Run`, and it passes `--hidden`.

**A login start arrives in the tray, not as a window.** Nobody logs in wanting a
dashboard. Hidden starts also skip the wait-for-daemon step, since there is nothing
to look at: the tray simply goes grey then green as the daemon comes up, rather than
holding the icon back for fifteen seconds during the busiest moment of a session.

Checking whether a hidden start really is hidden is harder than it looks. An
otto-desktop process owns several top-level windows, one of them a permanently
visible WebView2 helper with an empty title, and `Get-Process MainWindowHandle`
points at the helper whether Otto's own window is shown or not. Measured that way
both starts look identical. Enumerate the process's top-level windows instead and
look for a **visible one titled "Otto"**.

## Using it

- **Left click tray** show/hide
- **Ctrl+Alt+O** the same, from anywhere
- **Tray menu** Open, Check now (reload), Start with Windows, Quit

## Building

Needs Rust (MSVC toolchain), and WebView2, which ships with Windows 11.

```
cd desktop/src-tauri
cargo build --release            # target/release/otto-desktop.exe
cargo tauri build                # .msi installer
```

`target/` is gitignored: gigabytes of compiled dependencies, reproducible from
`Cargo.lock`.

`OTTO_REPO` tells the shell where to run `python -m otto ensure` from. It defaults to
the current working directory, so set it when launching from a shortcut or the
autostart entry.

## What this is not

It does not host Otto and does not replace the daemon. Run the daemon headless
without ever opening this, and everything still works. That is the design, not a
limitation: a UI that the floors depended on would be a worse Otto.

The binary is unsigned, so Windows SmartScreen will warn once on first launch.
