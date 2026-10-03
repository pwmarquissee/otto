# Otto desktop shell

A Tauri window, tray icon, and hotkey around the dashboard the daemon already
serves at `http://127.0.0.1:8787`. All behavior stays in Python. The shell adds
only what a browser tab cannot do. Windows only.

## The daemon outlives the window

The shell may start the daemon and never stops it. Closing the window hides it to
the tray. Quit from the tray exits the shell and leaves the daemon running. Only
`otto stop` stops the daemon.

On launch the shell runs `python -m otto ensure`, which starts the daemon if it is
down and does nothing otherwise. `OTTO_REPO` sets the directory that command runs
from. It defaults to the current directory, so set it for shortcuts and the
autostart entry.

## Using it

- Left click the tray icon, or press Ctrl+Alt+O anywhere, to show or hide the window.
- Tray menu: Open Otto, Check now (reload), Start with Windows, Quit (daemon keeps running).

The tray icon polls `/api/state` every 20 seconds. Green means schedules and
integrations are healthy. Amber means a schedule is stale or an integration is down;
hover for which. Gray means the daemon is not running.

## Registry writes

The shell writes two HKCU keys on every start.

- `Software\Classes\otto` registers the `otto://` scheme, so a toast's Reply button
  (`otto://reply/<card id>`) opens that card in this window instead of a browser.
  `otto://open` shows the window. If the shell is not running, the link starts it.
  `otto.notify.reply_url` checks for this key and falls back to an `http://` link
  ending in `#reply=<id>` when it is absent. `OTTO_REPLY_SCHEME=otto` or
  `OTTO_REPLY_SCHEME=http` forces one form.
- `...\CurrentVersion\Run` starts the shell at login with `--hidden`, so it lands
  in the tray and skips the wait for the daemon. Start with Windows is turned on the
  first time the shell runs and then left alone, so the tray toggle sticks.

## Building

Needs Rust with the MSVC toolchain and WebView2, which ships with Windows 11.

```
cd desktop/src-tauri
cargo build --release            # target/release/otto-desktop.exe
cargo tauri build                # .msi installer
```

`target/` is gitignored. The binary is unsigned, so SmartScreen warns once on first
launch.

## Limits

Not built in CI. The shell does not host Otto; the daemon runs headless without it.
To confirm a `--hidden` start is hidden, enumerate the process's top-level windows
and look for a visible one titled "Otto". `MainWindowHandle` points at a WebView2
helper window whether Otto's window is shown or not.
