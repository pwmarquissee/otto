// Otto, in a window and in the tray.
//
// This is a SHELL, not a rewrite. Otto's dashboard is already a web app talking to
// the daemon's HTTP API on loopback, so this owns a window, a tray icon and a
// hotkey, and nothing else. All the behaviour stays in Python where it is tested.
//
// THE RULE THIS FILE EXISTS TO OBEY
//
// The daemon must outlive the window. Otto's whole premise is that the floors hold
// when nobody is looking: schedules fire, feeds ingest, tasks dispatch. If closing a
// window stopped any of that, Otto would quietly stop being Otto, and the symptom
// would be silence, which is indistinguishable from a quiet day.
//
// So: this may START the daemon, and must NEVER stop it. Closing the window hides it.
// Quit from the tray closes the UI and leaves the daemon running, on purpose.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{process::Command, sync::Mutex, thread, time::Duration};

use tauri::{
    image::Image,
    menu::{CheckMenuItem, Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

/// Passed by the autostart entry. Starting at login must NOT throw a dashboard at
/// somebody who just logged in to do something else: a tray app arrives in the tray.
/// The window is still built, just hidden, so the hotkey and tray click are instant.
const HIDDEN_FLAG: &str = "--hidden";

const BASE: &str = "http://127.0.0.1:8787";
const HEALTH_EVERY: Duration = Duration::from_secs(20);

/// What the tray is saying. Ordered worst-last so a poll can take the max.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Health {
    Ok,
    Warn,
    Down,
}

impl Health {
    fn icon_bytes(self) -> &'static [u8] {
        match self {
            Health::Ok => include_bytes!("../icons/tray-ok.png"),
            Health::Warn => include_bytes!("../icons/tray-warn.png"),
            Health::Down => include_bytes!("../icons/tray-down.png"),
        }
    }
}

struct TrayState(Mutex<Option<Health>>);

/// Ask the daemon how it is. Returns the state and a one-line tooltip.
///
/// Deliberately reuses the same signals `otto doctor` and the dashboard use, rather
/// than inventing a second definition of "healthy" that could disagree with the one
/// shown when the window is opened.
fn poll_health() -> (Health, String) {
    let body = match ureq::get(&format!("{BASE}/api/state"))
        .timeout(Duration::from_secs(8))
        .call()
    {
        Ok(r) => match r.into_string() {
            Ok(s) => s,
            Err(_) => return (Health::Down, "Otto: unreadable reply".into()),
        },
        // Not "broken": the daemon may simply not be running, which is a state a
        // human might have chosen. Grey, not red.
        Err(_) => return (Health::Down, "Otto: daemon not running".into()),
    };

    let json: serde_json::Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(_) => return (Health::Down, "Otto: unparseable state".into()),
    };

    let mut problems: Vec<String> = Vec::new();

    if let Some(schedules) = json.get("schedules").and_then(|v| v.as_array()) {
        for s in schedules {
            if let Some(stale) = s.get("stale").and_then(|v| v.as_str()) {
                let name = s.get("name").and_then(|v| v.as_str()).unwrap_or("schedule");
                problems.push(format!("{name} stale ({stale})"));
            }
        }
    }
    // API integrations only. A manual-mode integration being "not ok" is a statement
    // about paperwork, not about anything that is currently broken.
    if let Some(integrations) = json.get("integrations").and_then(|v| v.as_array()) {
        for i in integrations {
            let ok = i.get("ok").and_then(|v| v.as_bool()).unwrap_or(true);
            let mode = i.get("mode").and_then(|v| v.as_str()).unwrap_or("");
            if !ok && mode == "api" {
                let name = i.get("name").and_then(|v| v.as_str()).unwrap_or("integration");
                problems.push(format!("{name} down"));
            }
        }
    }

    if problems.is_empty() {
        (Health::Ok, "Otto: floors holding".into())
    } else {
        let shown = problems
            .iter()
            .take(4)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n");
        let more = problems.len().saturating_sub(4);
        let tip = if more > 0 {
            format!("Otto: {} problem(s)\n{shown}\n+{more} more", problems.len())
        } else {
            format!("Otto: {} problem(s)\n{shown}", problems.len())
        };
        (Health::Warn, tip)
    }
}

fn daemon_is_up() -> bool {
    ureq::get(&format!("{BASE}/api/health"))
        .timeout(Duration::from_secs(3))
        .call()
        .is_ok()
}

/// Start the daemon if it is not already running, and wait briefly for it.
///
/// `otto ensure` is idempotent and is the same command a human would run, so this
/// cannot end up with two daemons. There is no matching stop anywhere in this file,
/// and that asymmetry is the point.
fn ensure_daemon(wait: bool) {
    if daemon_is_up() {
        return;
    }
    let mut cmd = Command::new("python");
    cmd.args(["-m", "otto", "ensure"]).current_dir(repo_dir());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console flash
    }
    let _ = cmd.spawn();

    // Waiting exists only so a VISIBLE window does not load a connection error and
    // read as "Otto is broken" when it had merely not started. Starting hidden at
    // login there is nothing to look at, and blocking would hold the tray icon back
    // for fifteen seconds during the busiest moment of the session. Let the health
    // poller show grey turning green instead.
    if !wait {
        return;
    }
    for _ in 0..30 {
        if daemon_is_up() {
            return;
        }
        thread::sleep(Duration::from_millis(500));
    }
}

/// Where `python -m otto ensure` runs from. Defaults to the current directory; set
/// OTTO_REPO when launching from a shortcut or the autostart entry.
fn repo_dir() -> String {
    std::env::var("OTTO_REPO").unwrap_or_else(|_| ".".to_string())
}

fn show(window: &tauri::WebviewWindow) {
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

/// Act on an `otto://` link. The two routes:
///
///   otto://reply/<card id>   show the window on that card's reply sheet
///   otto://open              show the window
///
/// The dashboard already understands `#reply=<id>` (a due toast used to carry the
/// http form of that link, which Windows handed to the default browser rather than to
/// this window). Setting the hash here reuses that path exactly, so the sheet the toast
/// opens is the same one the card menu and the notice sheet open; nothing new to keep
/// in step. The id is passed through only if it is hex, because it ends up inside a
/// JavaScript string.
fn open_route(app: &tauri::AppHandle, url: &str) {
    let Some(window) = app.get_webview_window("main") else { return };
    let rest = match url.strip_prefix("otto://") {
        Some(r) => r.trim_end_matches('/'),
        None => return,
    };
    if let Some(id) = rest.strip_prefix("reply/") {
        let id = id.trim_end_matches('/');
        if !id.is_empty() && id.len() <= 32 && id.chars().all(|c| c.is_ascii_hexdigit()) {
            // Assigning the same hash twice fires no hashchange, so clear it first;
            // the second toast about a card after its sheet was closed must still open.
            let js = format!(
                "if (location.hash === '#reply={id}') location.hash = ''; location.hash = 'reply={id}';"
            );
            let _ = window.eval(&js);
        }
    }
    show(&window);
}

/// Every `otto://` URL in an argv. Windows launches (or, via single-instance,
/// forwards to) the app with the clicked URL as an argument.
fn otto_urls(args: &[String]) -> Vec<String> {
    args.iter()
        .filter(|a| a.starts_with("otto://"))
        .cloned()
        .collect()
}

fn main() {
    let hidden = std::env::args().any(|a| a == HIDDEN_FLAG);

    tauri::Builder::default()
        // FIRST, per the plugin's own docs: a second launch (Windows opening an
        // otto:// link while Otto is already in the tray) hands its argv to this
        // instance and exits, instead of a second tray icon and a second window.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            let urls = otto_urls(&argv);
            if urls.is_empty() {
                // A plain second launch (double-clicked the exe again): just show.
                if let Some(w) = app.get_webview_window("main") {
                    show(&w);
                }
            }
            for url in urls {
                open_route(app, &url);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent, // ignored on Windows
            Some(vec![HIDDEN_FLAG]),
        ))
        .manage(TrayState(Mutex::new(None)))
        .setup(move |app| {
            // Bring the daemon up BEFORE a visible window points at it. See
            // ensure_daemon for why launching hidden deliberately does not wait.
            ensure_daemon(!hidden);

            let window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::External(BASE.parse().expect("BASE is a valid url")),
            )
            .title("Otto")
            .inner_size(1280.0, 860.0)
            .min_inner_size(720.0, 520.0)
            // Not skip_taskbar: that is a persistent property, so setting it for a
            // hidden start would keep Otto off the taskbar when it is later opened.
            // A hidden window is already absent from the taskbar.
            .visible(!hidden)
            .build()?;

            // Belt and braces. `.visible(false)` above is sufficient on its own;
            // hide() on an already-hidden window is a no-op and costs nothing, and
            // it keeps a login start from throwing a dashboard at somebody if a
            // future Tauri changes when the webview first shows itself.
            //
            // HOW TO CHECK THIS, because the obvious way is wrong. Do NOT use
            // Get-Process MainWindowHandle: an otto-desktop process owns several
            // top-level windows, one of them a permanently-visible WebView2 helper
            // with an empty title, and MainWindowHandle points at the helper whether
            // Otto's own window is shown or not. Measured that way, hidden and
            // visible starts look identical, which nearly produced a fix for a bug
            // that did not exist. Enumerate the process's top-level windows and look
            // for a VISIBLE one titled "Otto":
            //
            //   --hidden : one visible window, title ""        (helper only)
            //   normal   : title "Otto" AND title ""           (real window + helper)
            if hidden {
                let _ = window.hide();
            }

            // Close hides. The daemon keeps running and so does the tray icon, which
            // is the whole point: Otto is not a page you visit, it is a thing that
            // watches. Quit is available from the tray, and even that leaves the
            // daemon alone.
            {
                let w = window.clone();
                window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }

            // Start with Windows, on by default, and TOGGLEABLE FROM THE TRAY. An
            // autostart entry somebody has to find in the registry to remove is a
            // thing done to a machine; a checkbox next to the icon is a setting.
            let autostart = app.autolaunch();
            if !autostart.is_enabled().unwrap_or(false) {
                let _ = autostart.enable();
            }
            let autostart_on = autostart.is_enabled().unwrap_or(false);

            let open_i = MenuItem::with_id(app, "open", "Open Otto", true, None::<&str>)?;
            let check_i = MenuItem::with_id(app, "check", "Check now", true, None::<&str>)?;
            let login_i = CheckMenuItem::with_id(
                app,
                "login",
                "Start with Windows",
                true,
                autostart_on,
                None::<&str>,
            )?;
            let quit_i = MenuItem::with_id(
                app,
                "quit",
                "Quit (daemon keeps running)",
                true,
                None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&open_i, &check_i, &login_i, &quit_i])?;

            let tray = TrayIconBuilder::with_id("otto")
                .icon(Image::from_bytes(Health::Down.icon_bytes())?)
                .tooltip("Otto: starting")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => {
                        if let Some(w) = app.get_webview_window("main") {
                            show(&w);
                        }
                    }
                    "check" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.eval("location.reload()");
                            show(&w);
                        }
                    }
                    "login" => {
                        // Read the real state back rather than tracking a local
                        // boolean: the registry is the truth, and something else
                        // (Task Manager's Startup tab, a policy) may have changed it.
                        let al = app.autolaunch();
                        let on = al.is_enabled().unwrap_or(false);
                        let _ = if on { al.disable() } else { al.enable() };
                    }
                    // Closes the UI only. `otto stop` is a deliberate, separate act.
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(w) = tray.app_handle().get_webview_window("main") {
                            // Toggle: a second click on the tray should put it away
                            // again, which is what every other tray app does.
                            if w.is_visible().unwrap_or(false) {
                                let _ = w.hide();
                            } else {
                                show(&w);
                            }
                        }
                    }
                })
                .build(app)?;

            // Health poller. Only touches the icon when the state CHANGES, so the
            // tray is not rewritten every twenty seconds for nothing; the tooltip is
            // refreshed each time because the detail behind a warning moves.
            {
                let handle = app.handle().clone();
                thread::spawn(move || loop {
                    let (health, tip) = poll_health();
                    let state = handle.state::<TrayState>();
                    let changed = {
                        let mut last = state.0.lock().unwrap();
                        let changed = *last != Some(health);
                        *last = Some(health);
                        changed
                    };
                    if changed {
                        if let Ok(img) = Image::from_bytes(health.icon_bytes()) {
                            let _ = tray.set_icon(Some(img));
                        }
                    }
                    let _ = tray.set_tooltip(Some(&tip));
                    thread::sleep(HEALTH_EVERY);
                });
            }

            // otto:// links. This exe runs from target/release rather than from an
            // installer, so nothing registered the scheme for us; register_all writes
            // the HKCU class for `otto` pointing at THIS exe, every start, which also
            // means a rebuild in a new location re-points it. The Python side checks
            // that key before choosing otto:// over http:// for the toast link, so a
            // machine without the shell still gets a working link in a browser.
            if let Err(e) = app.deep_link().register_all() {
                eprintln!("otto: could not register otto:// scheme: {e}");
            }
            {
                let handle = app.handle().clone();
                app.deep_link().on_open_url(move |event| {
                    for url in event.urls() {
                        open_route(&handle, url.as_str());
                    }
                });
            }
            // Launched BY a link (Otto was not running): the URL is in our own argv.
            // register_all above ran first, so this cold start still lands on the
            // card rather than on the default view.
            {
                let handle = app.handle().clone();
                let args: Vec<String> = std::env::args().collect();
                for url in otto_urls(&args) {
                    open_route(&handle, &url);
                }
            }

            // Ctrl+Alt+O from anywhere. Otto is meant to be reachable without
            // hunting for a window.
            {
                let handle = app.handle().clone();
                let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyO);
                let _ = app.global_shortcut().on_shortcut(shortcut, move |_app, _sc, _ev| {
                    if let Some(w) = handle.get_webview_window("main") {
                        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
                            let _ = w.hide();
                        } else {
                            show(&w);
                        }
                    }
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("otto desktop failed to start");
}
