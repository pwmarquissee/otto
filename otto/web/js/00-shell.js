/* otto/web/js: the dashboard, as ordered classic scripts.
 *
 * THE LOAD ORDER CONTRACT. index.html loads these files in name order, after
 * term.js, as plain <script> tags: no modules, no bundler, no build step. Classic
 * scripts share one global scope, so a top-level `let`, `const` or `function` in
 * an earlier file is visible to every later one. Two rules keep that honest:
 *
 *   1. Nothing that RUNS at top level in a file may call a function declared in a
 *      later file. Function declarations hoist within a file, not across files,
 *      so only 90-wiring.js (the last file) calls into the others at load time;
 *      everything before it only declares, or registers listeners that fire later.
 *   2. No name is declared at top level in two files. tests/test_dashboard_js.py
 *      checks that, the way test_cli_shadowing.py does for the CLI.
 *
 * The daemon sends Cache-Control: no-cache for everything under /static, so a
 * change here is picked up on the next load with one ETag round trip per file;
 * no version query strings are needed.
 */

/* otto/web/js/00-shell.js: the module docstring, the constants (poll cadence, titles, column sets) and the shell state: every top-level `let` the views share, the pointer-hold guard, the terminal view's persisted target.
 * Relies on: nothing; this is the first file. */

/* Otto dashboard — the "Otto agentic UI redesign" design, wired to the live API.
 *
 * Four modes (Today, Board, History, Control plane) plus a persistent inspector, a
 * command palette, and a repository scope. The design's defaults are the ones baked
 * in here: sidebar shell, triage stream, rail timeline, comfortable board.
 *
 * Two things this deliberately does NOT do, both because the design's mock data
 * faked them and the real system cannot:
 *
 *   - It never invents stream events to keep a timeline moving. A finished run has
 *     the events it has. Padding a live feed would be the dashboard lying about
 *     what an agent did.
 *   - It never prices tokens. Cost is whatever Claude Code reported, or absent.
 *
 * BORROWED FROM AN EARLIER DASHBOARD (2026-08-26). A game studio's test-console
 * design had nothing to do with Otto's domain and a great deal to do with its
 * problems, so a set of its operating ideas live here now:
 *
 *   - The SHELL owns connection state. One banner says how old the numbers are;
 *     views never render their own "unreachable", and last-known values grey out
 *     rather than vanish.
 *   - Every mutation ECHOES the otto command it was worth into the status bar. It is
 *     how the UI teaches the CLI, and it costs one string per action.
 *   - A run carries TWO LANES: OTTO (spawned, ran, reported) and WORK (what the
 *     session concluded). "Otto lost it" and "the work failed" are different facts
 *     and were the same shade of red.
 *   - The run inspector draws the PLAN, not only the result: a phase rail with the
 *     queued phases hollow, and a budget bar under the live one.
 *   - Disclosures state what is inside them, in counts. "Details" is a dead end;
 *     "40 of 212 events" is a decision.
 *   - Selection is shell state. Focus a person once and Board, Threads, Outreach and
 *     People keep them in focus until you clear it.
 *   - Split panes (Ctrl+\), refused below 600px a side rather than handed a width
 *     no view was written for.
 */

/* ============================== constants ============================== */

const POLL_MS = 3000;
/* A pane narrower than this breaks the views, so the shell refuses to make one. The
 * split control disables itself rather than handing a view a width it was never
 * promised. */
const PANE_MIN = 600;
const INSPECTOR_W = 400;
const RAIL_W = 212;
const DAY_MS = 86400000;
/* 1200ms was too aggressive once render() rebuilt the whole centre pane: with a
 * live run selected, poll() and loadActivity() each rendered, so the real rate was
 * a full rebuild about every 600ms. Both halves of that are fixed below; this is
 * the interval, not the fix. */
const POLL_MS_LIVE = 2000;
const CHAT_POLL_MS = 2000;
/* With the change socket (/ws/events) open, the daemon says when a state file was
 * written, so the timer poll is only a safety net for a missed message. Measured
 * before this: 2.9 MB every 3s, 52 MB a minute, for a board that had not changed. */
const POLL_MS_PUSH = 15000;
/* A write usually lands as several files in quick succession; one fetch covers them. */
const CHANGED_DEBOUNCE_MS = 150;
const EVT_BACKOFF_MIN = 1000, EVT_BACKOFF_MAX = 30000;
/* A daemon that is up but has no /ws/events (older build) is retried this often. */
const EVT_RETRY_ABSENT_MS = 300000;
/* Cards (or rows) rendered per column before a "Show more" control takes over. The
 * live board carried ~330 cards: 3,400 DOM nodes and ~80ms of layout on every
 * rebuild, for pages nobody had scrolled to. */
const PAGE_CARDS = 40;
/* Age labels ("3m ago") refresh in place on this cadence instead of the whole view
 * being rebuilt because the clock moved. Countdowns tick every second, but only
 * while one is on screen. */
const AGE_REFRESH_MS = 30000;
const CLOCK_TICK_MS = 1000;

const CRIT = "var(--crit)", WARN = "var(--warn)", OK = "var(--ok)";
const CRIT_BG = "var(--crit-bg)", WARN_BG = "var(--warn-bg)", OK_BG = "var(--ok-bg)";
const DIM = "color-mix(in srgb, var(--color-text) 55%, transparent)";
const DIM_BG = "color-mix(in srgb, var(--color-text) 8%, transparent)";
const ACCENT = "var(--color-accent)";
const ACCENT_BG = "color-mix(in srgb, var(--color-accent) 16%, transparent)";

/* `today` replaces the old `stream`. The view is Otto talking to the owner: what it
 * has to say, then what the day contained, then what needs them. Ask Otto is gone: it was
 * a deliberately crippled Claude Code sitting next to the real thing, and it took 8
 * messages in its entire life. */
const MODES = [
  ["today",    "ph ph-sun",                       "Today"],
  ["board",    "ph ph-kanban",                    "Board"],
  ["dispatch", "ph ph-lightning",                 "Dispatch"],
  ["terminal", "ph ph-terminal-window",           "Terminal"],
  ["grid",     "ph ph-squares-four",              "Grid"],
  ["history",  "ph ph-clock-counter-clockwise",   "History"],
  ["writing",  "ph ph-pen-nib",                   "Writing"],
  ["plane",    "ph ph-sliders",                   "Control plane"],
];
/* Setup is a mode like the others but not a permanent rail entry: renderRail() puts
 * it at the top only while state.setup says the daemon is unconfigured. It stays
 * reachable from #setup, the palette and the Control plane's System tab afterwards. */
const SETUP_MODE = ["setup", "ph ph-list-checks", "Setup"];
const TITLES = {
  today: "Today", board: "Board", dispatch: "Dispatch", history: "History", writing: "Writing",
  plane: "Control plane",
  terminal: "Terminal", grid: "Grid", setup: "Setup",
};
/* The otto command a pane switch is worth, per view. Two places used to carry this
 * map inline and disagree the moment a view was added. */
const MODE_CMD = { today: "status", board: "board", dispatch: "dispatch", history: "runs", writing: "writing", plane: "schedules",
  terminal: "herdr attach", grid: "herdr", setup: "setup" };

/* Every command Otto actually has. The palette's own copy says "nothing invented",
 * so this list is checked against otto/cli.py rather than guessed at. */
const COMMANDS = [
  { group: "Look",   cmd: "otto status",           desc: "one-screen view of what needs you" },
  { group: "Look",   cmd: "otto next",             desc: "ranked, with the reason and the command" },
  { group: "Look",   cmd: "otto gaps",             desc: "what nothing is watching at all" },
  { group: "Look",   cmd: "otto board",            desc: "all outstanding work in columns" },
  { group: "Look",   cmd: "otto runs",             desc: "run history" },
  { group: "Look",   cmd: "otto schedules",        desc: "cadence and staleness" },
  { group: "Look",   cmd: "otto due",              desc: "what the cadence says should run now" },
  { group: "Look",   cmd: "otto registry",         desc: "agent, skill and command inventory" },
  { group: "Look",   cmd: "otto machine",          desc: "host facts, informational only" },
  { group: "Look",   cmd: "otto events",           desc: "the event log" },
  { group: "Look",   cmd: "otto agenda",           desc: "calendar and mail snapshots" },
  { group: "Look",   cmd: "otto meetings",         desc: "action items taken off your meeting notes" },
  { group: "Look",   cmd: "otto setup",            desc: "walk through first-run configuration", act: () => setMode("setup") },
  { group: "Act",    cmd: "otto meetings ingest",  desc: "read new Notion meeting notes now" },
  { group: "Act",    cmd: "otto refresh",          desc: "pull mail + calendar, both domains" },
  { group: "Act",    cmd: "otto refresh --domain personal", desc: "just the personal inbox" },
  { group: "Act",    cmd: "otto probe",            desc: "integration liveness" },
  { group: "Act",    cmd: "otto scan",             desc: "rescan definitions on disk" },
  { group: "Act",    cmd: "otto launch <name>",    desc: "run a schedule now" },
  { group: "Act",    cmd: "otto spawn <name> --prompt \"…\"", desc: "a real session, skip-permissions" },
  { group: "Act",    cmd: "otto kill <run-id>",    desc: "terminate a tracked run" },
  { group: "Act",    cmd: "otto stop",             desc: "stop the daemon, leave agents running" },
  { group: "Act",    cmd: "otto chat \"…\"",         desc: "ask Otto from the terminal" },
  { group: "Board",  cmd: "otto task add \"…\"",     desc: "file a task" },
  { group: "Board",  cmd: "otto task mv <id> <status>", desc: "move a card" },
  { group: "Board",  cmd: "otto task rm <id>",     desc: "delete a card and kill its run" },
  { group: "Board",  cmd: "otto propose \"…\"",      desc: "file a finding, deduped by fingerprint" },
  { group: "Board",  cmd: "otto ack <run-id>",     desc: "acknowledge a failed run, clear its card" },
  { group: "Known",  cmd: "otto known",            desc: "failures you have annotated as known" },
  { group: "Known",  cmd: "otto known add schedule <name> --reason \"…\"", desc: "demote its alert until it heals" },
  { group: "Known",  cmd: "otto known rm <kind> <name>", desc: "clear a stale annotation" },
  { group: "People", cmd: "otto people",           desc: "the dossiers" },
  { group: "People", cmd: "otto threads",          desc: "every dated thread, banded" },
  { group: "People", cmd: "otto outreach",         desc: "messages Otto wants to send colleagues" },
  { group: "People", cmd: "otto outreach --extend <id> --minutes 10", desc: "push a held send back" },
  { group: "Writing", cmd: "otto writing",         desc: "post ideas from your week, drafts, what went out" },
  { group: "Writing", cmd: "otto writing ideas",   desc: "mine the last week for post ideas now" },
  { group: "Writing", cmd: "otto writing draft <id>", desc: "draft a post in your voice" },
  { group: "Writing", cmd: "otto writing voice",   desc: "the voice file Otto reads before every draft" },
  { group: "Health", cmd: "otto doctor",           desc: "is Otto itself healthy" },
  { group: "Health", cmd: "otto heartbeat",        desc: "dead loops and expired credentials" },
  { group: "Health", cmd: "otto config",           desc: "junctions, deploy state, drift" },
  { group: "Health", cmd: "otto retire",           desc: "what Otto should stop doing (deletes nothing)" },
  { group: "Health", cmd: "otto serve",            desc: "start the daemon" },
];

/* Things the dashboard itself can do from the palette. These run rather than copy,
 * and they are the only rows that do: every row above is a real CLI command. */
const DASH_ACTIONS = [
  { group: "Dashboard", cmd: "What is this?",       desc: "the cover card: why Otto exists", act: () => openCover() },
  { group: "Dashboard", cmd: "Split the workspace", desc: "two views side by side (Ctrl+\\)", act: () => toggleSplit() },
  { group: "Dashboard", cmd: "Clear person focus",  desc: "stop filtering views to one person", act: () => setPerson(null) },
  { group: "Dashboard", cmd: "Copy the last command", desc: "what the status bar shows", act: () => copy(lastEcho) },
];

const BRAKES = [
  ["Master switch",   "autorun.enabled",         "One flag returns everything to report-only."],
  ["Concurrency",     "autorun.max_concurrent",  "Never two unattended sessions at once."],
  ["Circuit breaker", "autorun.max_failures",    "A broken schedule disarms itself rather than relaunching forever."],
  ["Minimum gap",     "autorun.min_gap_minutes", "A cadence bug cannot produce a tight relaunch loop."],
  ["Per-run budget",  "autorun.budget_usd",      "A hand-launched run is watched. An unattended one has nobody to notice it running away."],
  ["Task budget",     "dispatch.budget_usd",     "Dispatched board tasks are capped harder than schedules."],
];

/* ============================== state ============================== */

let state = null;        // last /api/state
/* Board selection: card ids held for a batch move. Lives here, not in the DOM,
 * because render() rebuilds the DOM every poll and a selection that died with it
 * would be useless. `lastPick` is the anchor for shift-click ranges. */
let selected = new Set();
let lastPick = null;
/* Created-date filter on the board. Presets are what a cleanup pass actually asks
 * ("what came in this week", "what is older than a month"); custom is two dates. */
let createdFilter = { preset: "any", from: "", to: "" };
let repoData = null;     // last /api/repos
let registryRows = null; // lazy, control plane only

let mode = "stream";
let domain = "all";      // all | work | personal
let scope = null;        // repo key, or null for all roots
let sel = null;          // { type: "run" | "task" | "sched", id }
let planeTab = "schedules";
let regQuery = "";
let peopleRows = null;   // lazy, control plane only
let peopleQuery = "";
/* Load failures need their own state. Leaving rows at null and only toasting means
 * the pane says "Loading…" forever: the toast is gone in three seconds and nothing
 * on screen ever explains why, or offers a way to try again. */
let peopleError = null;
let registryError = null;
let writingData = null;      // /api/writing, lazy, Writing view only
let writingError = null;
let writingLoading = false;
let writingLoadedAt = 0;
let writingSig = "";         // folded into viewSignature so a landed draft repaints
/* Setup view. /api/setup is fetched on entry and after every action; nothing here
 * comes from /api/state except the summary the rail badge reads. */
let setupData = null;        // last GET /api/setup
let setupError = null;       // fetch failure, shown inline with Retry
let setupLoading = false;
let setupSig = "";           // folded into the pane signature so a refetch repaints
let setupBusy = null;        // step id with a request in flight, so its buttons disable
let setupMsg = {};           // step id -> { ok: bool, text } shown under the action
let setupDraft = {};         // "step:field" -> typed text, survives the rebuild render() does
let setupRestart = null;     // null | "restarting" | "timeout"
/* First-open decision, made once when the first state lands. Persisted for the
 * session so a reload mid-setup does not yank the person off the view they chose. */
let setupAutoOpened = false;
try { setupAutoOpened = sessionStorage.getItem("otto.setupAuto") === "1"; } catch { /* blocked */ }
let coverPending = false;    // the cover waits for the first state: Setup may take its place
let writingBusy = null;      // post id with a request in flight, so buttons disable
let writingOpenNote = null;  // post id whose redraft box is open
let writingUrlFor = null;    // post id whose "mark posted" box is open
let writingNotes = {};       // post id -> redraft note text, survives re-render
let writingEditing = null;   // post id whose draft is open as a textarea
let writingEdits = {};       // post id -> edited draft text, survives re-render
let writingShowDropped = false;
let paletteOpen = false, paletteQuery = "", paletteIdx = 0;
let scopeOpen = false, scopeQuery = "";
let dragId = null, overCol = null;
/* The open board card context menu, or null. Holds the DOM node so dismissal does
 * not have to search for it, and lives on <body> rather than inside the column so a
 * column's own overflow cannot clip it. */
let cardMenu = null;
/* Board card detail: "clamp" (default, 3 lines) or "full". Clamped by default
 * because a card's `detail` holds its evidence, and evidence is long: the board was
 * unscannable with six 2000-character writeups in a column. In memory rather than
 * persisted, matching `domain` and `scope` above. */
let cardDetail = "clamp";
/* Done starts folded. The server already drops finished cards older than a week, but
 * a busy week still lands 40 of them next to 38 in Backlog, and Done is the one column
 * you never act on: it is a record, not a queue. Folded it keeps the count visible and
 * the cards one click away. In memory, like the toggles above. */
let doneOpen = false;
let activityCache = {};  // run id -> events, fetched on selection
/* run id -> {verdict, plan}, from /api/runs/{id}. Refreshed with the activity for a
 * live run, so the phase rail moves while the timeline does. */
let planCache = {};
let toastTimer = null, pollTimer = null;
/* Connection state, owned here and nowhere else. `lastGoodAt` is when /api/state
 * last answered; the banner and the greying key off it. */
let lastGoodAt = null;
let lastPollError = null;
/* Second pane: a mode name, or null when the workspace is not split. `focusedPane`
 * is which pane Ctrl+1/2 and the mode rail act on. */
let splitMode = null;
let focusedPane = 0;
/* Shared person selection. A slug and its display name, persisted so a focus set on
 * Monday is still set on Tuesday. Cleared with Escape or the chip. */
let personSel = null;
try { personSel = JSON.parse(localStorage.getItem("otto.person") || "null"); } catch { personSel = null; }
/* Folds in the inspector timeline and the event log. In memory, like doneOpen. */
let timelineOpen = false, eventsOpen = false;
let coverOpen = false;
let replyCard = null;     // the card the reply sheet is open on
/* A #reply=<id> deep link (from a due toast's Reply button) arrives before the
 * first poll has produced a board, so it waits here until the card can be found. */
let pendingReply = (location.hash.match(/^#reply=([0-9a-f]{6,32})$/) || [])[1] || null;
let checkinDraft = "";    // survives the rebuild render() does
let checkinEnergy = null;
let lastSig = null;      // last rendered view signature; equal means skip
let activitySig = "";    // folded into the signature so a timeline change repaints
let renderDeferred = false;
/* Per-pane and per-chrome signatures, so a change in one place rebuilds that place
 * only: the split's second pane, the inspector, the palette and the scope picker
 * each compare their own inputs rather than riding every render(). */
let paneSigs = [null, null];
let pendingPaneSigs = ["", ""];
let inspSig = null, palSig = null, scopeSig = null;
/* Conditional GET. The daemon answers 304 to a matching If-None-Match, and a 304
 * costs no parse and no render. Counted so the connection chip can say so. */
let stateEtag = null;
let count304 = 0, count200 = 0;
/* The change socket. `evtOpen` slows the fallback poll; `evtVersion` is the
 * daemon's write counter from hello/changed, kept for the chip's tooltip. */
let evtSock = null, evtOpen = false, evtVersion = null;
let evtBackoff = EVT_BACKOFF_MIN, evtTimer = null, changedTimer = null;
let pollInFlight = null, pollQueued = false;
/* Board paging: column key -> how many cards are rendered. Survives rebuilds so a
 * column someone expanded stays expanded until the page reloads. */
let colShown = {};
let histShown = PAGE_CARDS;
/* The state payload truncates long task details to 600 chars (`detail_truncated`);
 * the inspector and the action sheet fetch the whole thing by id, once. */
let detailCache = {};
const detailBusy = new Set();
let hasCountdown = false;
/* Whether a mouse button or touch is currently down anywhere. render() holds off
 * while it is, see there. The resume is a timeout, not a direct call: pointerup
 * is followed by mouseup and click in the same task, and a synchronous rebuild
 * on pointerup would destroy the node before its click could dispatch. */
let pointerHeld = false;
let pointerHeldTimer = null;
document.addEventListener("pointerdown", () => {
  pointerHeld = true;
  /* A watchdog, because the release is not guaranteed to arrive: an HTML5 drag
   * swallows the mouse-up it started with, a pointer that leaves the window can
   * come up anywhere, and a frozen "held" flag deferred every render forever.
   * That was the dashboard freezing after a drag (2026-10-02). No click takes
   * four seconds, so after four the hold is treated as over. */
  clearTimeout(pointerHeldTimer);
  pointerHeldTimer = setTimeout(pointerReleased, 4000);
}, true);
function pointerReleased() {
  clearTimeout(pointerHeldTimer);
  pointerHeld = false;
  if (renderDeferred) setTimeout(() => { if (renderDeferred && !pointerHeld) render(true); }, 0);
}
for (const ev of ["pointerup", "pointercancel", "dragend", "drop", "mouseup"]) {
  document.addEventListener(ev, pointerReleased, true);
}
window.addEventListener("blur", pointerReleased);
/* Terminal views (term.js). The focused herdr target and the control/observe mode
 * persist, so the pane you were watching is the pane you come back to. */
let termTarget = null;
let termMode = "control";
try {
  termTarget = localStorage.getItem("otto.term.target") || null;
  termMode = localStorage.getItem("otto.term.mode") === "observe" ? "observe" : "control";
} catch { /* storage blocked: defaults */ }
let termPanes = null;        // /api/term/panes rows, null until the first load
let termPanesSig = "";       // folded into viewSignature so a new pane repaints
let termPanesAt = 0;
let termPanesTimer = null;
let termApiMissing = false;  // the relay is not deployed yet: the rail is enough
const TERM_PANES_MS = 4000;

