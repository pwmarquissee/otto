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

/* ============================== helpers ============================== */

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
function ico(name) {
  const i = document.createElement("i");
  i.className = name;
  return i;
}
function age(ts) {
  if (!ts) return "never";
  const t = typeof ts === "number" ? ts : Date.parse(ts);
  if (Number.isNaN(t)) return String(ts);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 172800) return (s / 3600).toFixed(1) + "h ago";
  return (s / 86400).toFixed(1) + "d ago";
}
/* An age label the clock tick refreshes in place. `pre`/`post` are the fixed text
 * around the age ("last " ... " busy"), kept on the node so the refresh can rebuild
 * the whole string without knowing who made it. */
function ageEl(ts, cls, pre, post) {
  const n = el("span", cls, (pre || "") + age(ts) + (post || ""));
  if (ts) {
    n.dataset.ts = typeof ts === "number" ? new Date(ts).toISOString() : String(ts);
    if (pre) n.dataset.pre = pre;
    if (post) n.dataset.post = post;
  }
  return n;
}
/* A countdown label ("sends in 7m"). Ticks every second while any is on screen. */
function countdownEl(ts, cls, pre) {
  const n = el("span", cls, (pre || "") + countdown(ts));
  n.dataset.cd = ts;
  if (pre) n.dataset.pre = pre;
  hasCountdown = true;
  return n;
}
/* Time-only refresh: every label with a data-ts/data-cd/data-hold attribute gets
 * its text recomputed, and nothing else moves. This is what replaced `s.at` in the
 * view signature; before it, every poll rebuilt the whole view so "3m ago" could
 * become "4m ago". */
function refreshClocks(ages) {
  if (ages) {
    for (const n of document.querySelectorAll("[data-ts]")) {
      n.textContent = (n.dataset.pre || "") + age(n.dataset.ts) + (n.dataset.post || "");
    }
  }
  if (!hasCountdown) return;
  const cds = document.querySelectorAll("[data-cd]");
  const holds = document.querySelectorAll("[data-hold]");
  if (!cds.length && !holds.length) { hasCountdown = false; return; }
  for (const n of cds) n.textContent = (n.dataset.pre || "") + countdown(n.dataset.cd);
  for (const n of holds) {
    const left = Math.max(0, Date.parse(n.dataset.hold) - Date.now());
    const total = Math.max(1, Number(n.dataset.total) || 1);
    if (n.classList.contains("hold-fill")) n.style.width = Math.min(100, Math.round(100 * (1 - left / total))) + "%";
    else n.textContent = holdLeft(left);
  }
}
function kfmt(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function money(v) { return v == null ? "" : "$" + Number(v).toFixed(3); }

async function api(path, opts) {
  const r = await fetch(path, { cache: "no-store", ...opts });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

function toast(msg, bad) {
  $("toast-text").textContent = msg;
  $("toast").classList.toggle("bad", !!bad);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 2400);
}
function copy(text) {
  if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
  toast("copied  " + text);
}

/* ── the status bar ──
 * Every mutation says what it was worth as an otto command. `cmd` is the CLI form
 * when one exists and the HTTP call when one does not: the bar never invents a
 * command the CLI lacks, matching the palette's own rule. */
let lastEcho = "otto status";
function echo(cmd, note) {
  lastEcho = cmd;
  const b = $("echo-cmd");
  if (!b) return;
  b.textContent = cmd;
  b.classList.remove("fresh");
  void b.offsetWidth;            // restart the animation
  b.classList.add("fresh");
  $("echo-note").textContent = note || "the command your last click was worth";
}
/* api() plus the echo. Every write in this file goes through here so the bar cannot
 * fall out of step with what the dashboard actually did. */
async function act(cmd, path, opts) {
  const r = await api(path, opts);
  echo(cmd);
  return r;
}

/* A path elided from the LEFT, with the leaf kept bright. */
function pathEl(text, cls) {
  const s = String(text || "");
  const wrap = el("span", "path-l" + (cls ? " " + cls : ""));
  wrap.title = s;
  const inner = el("span");
  const cut = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  if (cut > 0 && cut < s.length - 1) {
    inner.appendChild(el("span", null, s.slice(0, cut + 1)));
    inner.appendChild(el("span", "leaf", s.slice(cut + 1)));
  } else {
    inner.textContent = s;
  }
  wrap.appendChild(inner);
  return wrap;
}

/* A disclosure that says what is inside it while shut. "Details" is a dead end;
 * "40 of 212 events" is a decision. */
function foldHead(label, counts, open, onToggle) {
  const b = el("button", "fold-head" + (open ? " open" : ""));
  b.type = "button";
  b.appendChild(ico("ph ph-caret-right"));
  b.appendChild(el("span", "lbl", label));
  if (counts) b.appendChild(el("span", "counts", counts));
  b.addEventListener("click", onToggle);
  return b;
}

/* ── two lanes ──
 * OTTO: did the harness spawn it, run it, and hear back. WORK: what the session
 * concluded. The daemon computes the verdict (otto/verdict.py); this only draws it. */
const LANE_TONE = {
  running: ACCENT, ok: OK, done: OK,
  orphaned: CRIT, api: WARN, budget: WARN, killed: CRIT,
  failed: CRIT, "not-evaluated": DIM, skipped: DIM, due: WARN,
};
const LANE_GLYPH = {
  running: "ph ph-circle-notch", ok: "ph ph-check-circle", done: "ph ph-check-circle",
  orphaned: "ph ph-ghost", api: "ph ph-cloud-slash", budget: "ph ph-coins", killed: "ph ph-prohibit",
  failed: "ph ph-x-circle", "not-evaluated": "ph ph-minus-circle", skipped: "ph ph-minus-circle",
  due: "ph ph-clock",
};
/* Which side is broken decides the fill. Harness trouble gets the hatch, an unknown
 * outcome the dot screen, a work failure a solid tint. Same rule as the console. */
function verdictTexture(v) {
  const h = v.harness.state;
  if (h === "orphaned" || h === "killed") return "tex-incon";
  if (h === "api" || h === "budget") return "tex-harness";
  return "";
}
function verdictTone(v) {
  const h = v.harness.state, w = v.work.state;
  if (h === "running") return ACCENT;
  if (h !== "ok") return LANE_TONE[h] || CRIT;
  return LANE_TONE[w] || DIM;
}
function lane(name, st) {
  const tone = LANE_TONE[st.state] || DIM;
  const row = el("span", "lane");
  row.style.setProperty("--l-tone", tone);
  row.appendChild(el("span", "lane-name", name));
  row.appendChild(ico(LANE_GLYPH[st.state] || "ph ph-dot"));
  const s = el("span", "lane-state", st.label || st.state);
  if (st.detail) s.title = st.detail;
  row.appendChild(s);
  return row;
}
function lanes(v, mini) {
  const box = el("span", "lanes" + (mini ? " mini" : ""));
  box.appendChild(lane("OTTO", v.harness));
  box.appendChild(lane("WORK", v.work));
  return box;
}
function verdictBlock(v) {
  const box = el("div", "verdict " + verdictTexture(v));
  box.style.setProperty("--v-tone", verdictTone(v));
  const left = el("div");
  left.style.minWidth = "0";
  const head = el("div", "verdict-head");
  const word = el("span", "verdict-word");
  const g = v.harness.state !== "ok" && v.harness.state !== "running"
    ? LANE_GLYPH[v.harness.state] : LANE_GLYPH[v.work.state];
  word.appendChild(ico(g || "ph ph-dot"));
  word.appendChild(el("span", null, v.word));
  head.appendChild(word);
  head.appendChild(el("span", "verdict-imp", v.imperative));
  left.appendChild(head);
  const sub = v.work.detail || v.harness.detail;
  if (sub) left.appendChild(el("div", "verdict-sub", sub));
  box.appendChild(left);
  box.appendChild(lanes(v));
  return box;
}

/* ── the plan ──
 * Four phases the daemon knows about every run (spawned, working, reporting,
 * settled). Queued ones are drawn hollow and dim: a rail built only from results
 * shows history and hides the future. */
function phaseRail(plan) {
  const rail = el("div", "phase-rail");
  for (const p of (plan.phases || [])) {
    const ph = el("div", "phase " + p.state);
    ph.appendChild(el("span", "dot"));
    ph.appendChild(el("span", null, p.name));
    if (p.note) ph.appendChild(el("span", "note", p.note));
    else if (p.state === "queued") ph.appendChild(el("span", "note", "queued"));
    rail.appendChild(ph);
  }
  return rail;
}
function budgetRow(plan, live) {
  const b = plan.budget || {};
  const bits = [];
  if (plan.elapsed_seconds != null) {
    let t = Math.round(plan.elapsed_seconds) + "s";
    if (plan.expected_seconds != null) t += " / ~" + Math.round(plan.expected_seconds) + "s typical";
    bits.push(t);
  }
  if (b.usd != null) bits.push((b.spent != null ? money(b.spent) : "$0") + " / $" + b.usd + " cap");
  else if (b.spent != null) bits.push(money(b.spent) + ", no cap");
  if (!bits.length) return null;
  const row = el("div", "budget-row");
  const track = el("div", "budget-track");
  const fill = el("span", "budget-fill");
  let pct = b.pct;
  if (pct == null && plan.expected_seconds && plan.elapsed_seconds != null) {
    pct = Math.min(100, Math.round(100 * plan.elapsed_seconds / plan.expected_seconds));
  }
  fill.style.width = (pct == null ? 0 : pct) + "%";
  if (pct != null && pct >= 100) fill.classList.add("over");
  else if (pct != null && pct >= 75) fill.classList.add("warm");
  if (!live && pct == null) fill.style.width = "100%";
  track.appendChild(fill);
  row.appendChild(track);
  row.appendChild(el("span", null, bits.join("  ·  ")));
  return row;
}

/* ── sparkline: bars, not a line ──
 * A day's spend is a discrete event. A smoothed line hides the one day you are
 * looking for. Three tones against a reference line: under, over, far over. */
function sparkBars(series, ref, refLabel) {
  const max = Math.max(ref * 1.6, ...series.map((s) => s.v), 0.001);
  const box = el("div", "spark");
  if (ref > 0) {
    const line = el("span", "spark-line");
    line.style.bottom = Math.round((ref / max) * 100) + "%";
    box.appendChild(line);
    const lbl = el("span", "spark-lbl", refLabel);
    lbl.style.bottom = Math.min(80, Math.round((ref / max) * 100) + 3) + "%";
    box.appendChild(lbl);
  }
  for (const s of series) {
    const bar = el("span", "spark-bar" + (
      s.v === 0 ? " empty" : s.v > ref * 1.5 ? " far" : s.v > ref ? " over" : ""));
    bar.style.height = s.v === 0 ? "2px" : Math.max(3, Math.round((s.v / max) * 100)) + "%";
    bar.title = s.label + ": " + (s.v ? "$" + s.v.toFixed(2) : "no reported cost");
    box.appendChild(bar);
  }
  return box;
}

/* Person focus. A slug plus the name to match against free text where a slug is
 * not carried (board cards, outreach recipients). */
function setPerson(slug, name) {
  personSel = slug ? { slug, name: name || slug } : null;
  try {
    if (personSel) localStorage.setItem("otto.person", JSON.stringify(personSel));
    else localStorage.removeItem("otto.person");
  } catch { /* private window, or storage blocked */ }
  echo(personSel ? "otto people " + personSel.slug : "otto people",
    personSel ? "focused on " + personSel.name + " in every view" : "person focus cleared");
  render(true);
}
function personMatch(text) {
  if (!personSel) return true;
  const hay = String(text || "").toLowerCase();
  const first = personSel.name.split(/\s+/)[0].toLowerCase();
  return hay.includes(personSel.slug.toLowerCase())
    || hay.includes(personSel.name.toLowerCase())
    || (first.length > 3 && hay.includes(first));
}

/* Status -> [foreground, background]. The design's own map, verbatim. */
function stColor(key) {
  const M = {
    ok: [OK, OK_BG], done: [OK, OK_BG], linked: [OK, OK_BG], sync: [OK, OK_BG],
    "in sync": [OK, OK_BG], idle: [OK, OK_BG], current: [OK, OK_BG],
    running: [ACCENT, ACCENT_BG], "in-progress": [ACCENT, ACCENT_BG],
    due: [WARN, WARN_BG], warn: [WARN, WARN_BG], queued: [WARN, WARN_BG],
    high: [WARN, WARN_BG], drift: [WARN, WARN_BG], DRIFT: [WARN, WARN_BG],
    snapshot: [WARN, WARN_BG], blocked: [WARN, WARN_BG], mcp: [WARN, WARN_BG],
    backlog: [DIM, DIM_BG],
    crit: [CRIT, CRIT_BG], failed: [CRIT, CRIT_BG], orphaned: [CRIT, CRIT_BG],
    urgent: [CRIT, CRIT_BG], stale: [CRIT, CRIT_BG], "needs-you": [CRIT, CRIT_BG],
    down: [CRIT, CRIT_BG], broken: [CRIT, CRIT_BG], killed: [CRIT, CRIT_BG],
  };
  return M[key] || [DIM, DIM_BG];
}
/* A due date as pressure. Returns null when there is no date, so a card with no
 * deadline shows no chip at all rather than an empty one. */
function dueLabel(due) {
  if (!due) return null;
  const d = new Date(String(due).slice(0, 10) + "T00:00:00");
  if (isNaN(d)) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((d - today) / 86400000);
  if (days < 0) return { text: `overdue ${Math.abs(days)}d`, key: "crit" };
  if (days === 0) return { text: "due today", key: "urgent" };
  if (days === 1) return { text: "due tomorrow", key: "warn" };
  if (days <= 7) return { text: `due in ${days}d`, key: "due" };
  return { text: `due ${String(due).slice(0, 10)}`, key: "backlog" };
}
/* Glyph + word, never color alone. A pill read from two meters away, or by anyone who
 * cannot tell the warn amber from the crit red, still has to resolve. One glyph per
 * status, so the same state wears the same mark in every view. */
const PILL_GLYPH = {
  ok: "ph ph-check", done: "ph ph-check", linked: "ph ph-link", sync: "ph ph-check",
  "in sync": "ph ph-check", idle: "ph ph-circle", current: "ph ph-check",
  running: "ph ph-circle-notch", "in-progress": "ph ph-circle-notch", busy: "ph ph-circle-notch",
  due: "ph ph-clock", warn: "ph ph-warning", queued: "ph ph-hourglass", waiting: "ph ph-hand",
  high: "ph ph-arrow-up", urgent: "ph ph-arrow-fat-up", drift: "ph ph-arrows-left-right",
  DRIFT: "ph ph-arrows-left-right", snapshot: "ph ph-camera", blocked: "ph ph-hand",
  mcp: "ph ph-question", backlog: "ph ph-tray", info: "ph ph-info",
  crit: "ph ph-warning-octagon", failed: "ph ph-x-circle", orphaned: "ph ph-ghost",
  stale: "ph ph-hourglass-high", "needs-you": "ph ph-hand-pointing", down: "ph ph-plugs",
  broken: "ph ph-link-break", killed: "ph ph-prohibit", off: "ph ph-power", skipped: "ph ph-minus",
  auto: "ph ph-lightning", "not Otto's": "ph ph-eye-slash", tier: "ph ph-hand",
};
function pill(text, key, iconName) {
  const [fg, bg] = stColor(key);
  const s = el("span", "pill");
  s.style.color = fg; s.style.background = bg;
  const glyph = iconName || PILL_GLYPH[key] || PILL_GLYPH[String(text).split(" ")[0]];
  if (glyph) s.appendChild(ico(glyph));
  s.appendChild(el("span", null, text));
  return s;
}
function domTag(d) {
  const t = el("span", "dom-tag" + (d === "personal" ? " life" : ""));
  t.textContent = d === "personal" ? "life" : "work";
  return t;
}
function cadenceText(s) {
  const c = s.cadence || {};
  if (c.kind === "daily") return `daily ${c.at}`;
  if (c.kind === "weekly") return `${(c.days || []).join(",")} ${c.at}`;
  if (c.kind === "every") return `every ${c.hours}h`;
  return "manual";
}
function schedState(s) {
  if (s.stale) return [s.stale_level === "crit" ? "stale" : "warn", "stale", s.stale];
  if (s.due) return ["due", "due", s.due_reason];
  if (!s.enabled) return ["idle", "off", s.disabled_reason || "disabled"];
  return ["idle", "idle", s.due_reason];
}

/* ── filtering ──
 * Domain and scope are independent questions and both must pass. Scope is by the
 * directory a record was actually spawned in; nothing is inferred from a title. */
function inDomain(r) { return domain === "all" || (r.domain || "work") === domain; }
function repoKeyOf(rec) {
  if (!rec || !repoData) return null;
  const cwd = (rec.cwd || "").toLowerCase().replace(/\\+$/, "");
  if (!cwd) return null;
  let best = null;
  for (const r of repoData.repos) {
    const p = r.path.toLowerCase();
    if (cwd === p || cwd.startsWith(p + "\\") || cwd.startsWith(p + "/")) {
      if (!best || r.path.length > best.path.length) best = r;
    }
  }
  return best ? best.key : null;
}
function inScope(rec) { return !scope || repoKeyOf(rec) === scope; }
function keep(rec) { return inDomain(rec) && inScope(rec); }

/* ============================== rail ============================== */

function renderRail() {
  const s = state || {};
  const nextRows = ((s.briefing && s.briefing.next) || []).filter(keep);
  const today = nextRows.filter((r) => r.band === "today");
  /* OUTSTANDING work, which is every column except Done. The badge read 105 against
   * 43 things actually left to do, because 62 finished cards counted toward it. A
   * nav badge is a workload signal, and one that only ever goes up as you get things
   * done is worse than no badge: it trains you to stop reading it. */
  const boardTotal = ((s.board || {}).columns || [])
    .filter((c) => c.key !== "done")
    .flatMap((c) => c.cards || []).filter(keep).length;
  const badInt = (s.integrations || []).filter((i) => !i.ok && i.mode !== "mcp-only").length;

  /* inDomain, not keep: a notice has no cwd, so inScope() would hide every one of
   * them the moment a repo scope was set. This has to match viewToday's filter
   * EXACTLY, because the badge and the panel disagreeing is how you get a "3" over a
   * page that says "Nothing from Otto". It previously filtered by neither. */
  const unreadNotices = ((s.notices) || []).filter((n) => !n.read_at).filter(inDomain).length;

  /* This badge means one of two different things, so it has to say which.
   * Unread notices win it, because a message Otto chose to send outranks a list it
   * recomputes every poll -- but only notices are `hot`. A ranked count renders
   * neutral like Board's, so a coloured badge always means "Otto is telling you
   * something" and a plain one means "there are N things on this page". Reading the
   * colour used to tell you nothing. */
  /* Drafts waiting to be read. Ideas are not counted: a menu is not a workload, and
   * a badge that grows every Friday is a badge that gets ignored. */
  const draftsWaiting = ((s.writing || {}).counts || {}).drafted || 0;
  const counts = {
    today: [unreadNotices || today.length, unreadNotices > 0],
    board: [boardTotal, false],
    history: [0, false],
    writing: [draftsWaiting, false],
    plane: [badInt, badInt > 0],
    terminal: [0, false], grid: [0, false],
  };
  /* What each number counts, in words, on the thing you hover. A bare integer next
   * to a nav label is not self-describing, and this one is ambiguous by design. */
  const badgeWhy = {
    today: unreadNotices
      ? `${unreadNotices} unread message${unreadNotices === 1 ? "" : "s"} from Otto`
      : (today.length ? `${today.length} ranked "today" under Needs you` : ""),
    board: boardTotal ? `${boardTotal} card${boardTotal === 1 ? "" : "s"} outstanding, `
      + "Done excluded" : "",
    history: "",
    writing: draftsWaiting ? `${draftsWaiting} draft${draftsWaiting === 1 ? "" : "s"} waiting for you to read` : "",
    plane: badInt ? `${badInt} integration(s) down` : "",
    terminal: "", grid: "",
  };

  /* Setup sits above everything else while the daemon is unconfigured, with the
   * step count as its badge. Once complete it leaves the rail entirely. */
  const su = s.setup;
  const setupOpen = !!(su && su.complete === false);
  if (setupOpen) badgeWhy.setup = `${su.done || 0} of ${su.total || 0} setup steps done`;
  const railModes = setupOpen ? [SETUP_MODE, ...MODES] : MODES;

  /* Built into a fragment and swapped in once: the nav is live DOM. */
  const nav = $("rail-nav");
  const navFrag = document.createDocumentFragment();
  for (const [key, iconName, label] of railModes) {
    const b = el("button", "mode-btn" + (mode === key ? " on" : ""));
    b.type = "button";
    b.title = badgeWhy[key] ? `${label} — ${badgeWhy[key]}` : label;
    b.appendChild(el("span", "mode-mark"));
    b.appendChild(ico(iconName));
    b.appendChild(el("span", "mode-lbl", label));
    /* A mode without a badge (Dispatch, Terminal, Grid) is a mode, not a crash:
     * a throw here takes the whole rail and every view with it. */
    const [n, hot] = counts[key] || [0, false];
    if (key === "setup") {
      const c = el("span", "mode-count su-count", `${su.done || 0}/${su.total || 0}`);
      c.title = badgeWhy.setup;
      b.appendChild(c);
    } else if (n) {
      const c = el("span", "mode-count" + (hot ? " hot" : ""), String(n));
      c.title = badgeWhy[key] || "";
      b.appendChild(c);
    }
    b.addEventListener("click", () => setMode(key));
    navFrag.appendChild(b);
  }
  nav.replaceChildren(navFrag);

  const seg = $("domain-seg");
  const segFrag = document.createDocumentFragment();
  for (const [key, label] of [["all", "All"], ["work", "Work"], ["personal", "Life"]]) {
    const b = el("button", domain === key ? "on" : null, label);
    b.type = "button";
    b.addEventListener("click", () => { domain = key; sel = null; render(); });
    segFrag.appendChild(b);
  }
  seg.replaceChildren(segFrag);

  $("scope-btn").classList.toggle("on", !!scope);
  $("scope-label").textContent = scope || "All roots";
  $("scope-btn").title = scope
    ? "Otto is scoped to " + scope
    : "Open Otto in a repository (Ctrl+O)";

  if (s.persona) $("persona").textContent = s.persona;
}

/* ============================== topbar ============================== */

function renderTopbar() {
  const s = state || {};
  $("mode-title").textContent = TITLES[mode];
  document.title = "Otto · " + TITLES[mode];

  const nextRows = ((s.briefing && s.briefing.next) || []).filter(keep);
  const today = nextRows.filter((r) => r.band === "today");
  const live = (s.live || []).filter(keep);
  const scheds = (s.schedules || []).filter(keep);
  const stale = scheds.filter((x) => x.stale);
  const worst = stale.slice().sort(
    (a, b) => (b.stale_level === "crit") - (a.stale_level === "crit"))[0];
  let darkBit = "nothing stale";
  if (worst) {
    const m = /[\d.]+d|[\d.]+h/.exec(worst.stale || "");
    darkBit = worst.name + " dark " + (m ? m[0] : worst.stale);
  }
  $("state-line").textContent = [
    today.length + " need you",
    live.length + " in flight",
    darkBit,
  ].join("  ·  ");

  $("scope-chip").hidden = !scope;
  if (scope) {
    const r = (repoData ? repoData.repos : []).find((x) => x.key === scope);
    $("scope-chip-path").replaceChildren(pathEl(r ? r.path : scope));
  }
  $("person-chip").hidden = !personSel;
  if (personSel) $("person-chip-name").textContent = personSel.name;

  const host = $("top-actions");
  host.replaceChildren();
  const acts = {
    today: ["Refresh mail + calendar", doRefresh],
    board: ["Add task", addTask],
    plane: ["Probe now", doProbe],
  }[mode];
  if (acts) {
    const b = el("button", "btn btn-secondary btn-sm", acts[0]);
    b.type = "button";
    b.addEventListener("click", () => acts[1](b));
    host.appendChild(b);
  }

  /* Split control. Disabled, with the reason in the tooltip, when two panes would
   * each land under PANE_MIN. */
  const sb = $("split-btn");
  const can = canSplit();
  sb.disabled = !can && !splitMode;
  sb.classList.toggle("on", !!splitMode);
  sb.title = splitMode ? "Close the split (Ctrl+\\)"
    : can ? "Split the workspace (Ctrl+\\)"
    : `Split needs ${RAIL_W + PANE_MIN * 2}px of width: a pane under ${PANE_MIN}px breaks the views`;

  renderConnection();
  $("echo-right").textContent = splitMode
    ? `2 panes · ${Math.floor(paneWidth() / 2)}px each`
    : (state ? `${(s.runs || []).length} runs tracked` : "");
}

/* ── the shell owns connection state ──
 * One place decides what "unreachable" looks like. The chip in the rail says up or
 * down; the banner says since when, how old the numbers on screen are, and the retry
 * cadence; the centre greys. Nothing else in this file renders its own offline. */
function renderConnection() {
  const chip = $("daemon-chip");
  const ok = lastPollError == null && !!state;
  chip.classList.toggle("down", !ok);
  /* `live` means the change socket is open and the daemon pushes; without it the
   * chip says "poll" so a quiet dashboard can be told apart from a deaf one. */
  chip.classList.toggle("live", ok && evtOpen);
  $("daemon-text").textContent = ok
    ? "daemon up · " + (evtOpen ? "push" : "poll") + " · " + new Date().toISOString().slice(11, 19) + "Z"
    : "daemon unreachable";
  chip.title = ok
    ? `port 8787 · single writer · ${((state || {}).runs || []).length} runs tracked · `
      + (evtOpen
        ? `change socket open (v${evtVersion == null ? "?" : evtVersion}), fallback poll every ${POLL_MS_PUSH / 1000}s`
        : `no change socket, polling every ${POLL_MS / 1000}s`)
      + ` · ${count200} fetched, ${count304} unchanged (304)`
    : "start it with: otto serve";

  const banner = $("conn-banner");
  const stale = !ok && lastGoodAt != null;
  banner.hidden = ok || !state;
  if (!ok && state) {
    const since = lastGoodAt ? new Date(lastGoodAt).toTimeString().slice(0, 8) : "?";
    const txt = $("conn-text");
    txt.replaceChildren();
    txt.appendChild(el("b", null, "Daemon unreachable."));
    txt.appendChild(el("span", null, " "));
    const m = el("span", "mono", lastPollError || "no answer");
    txt.appendChild(m);
    txt.appendChild(el("span", null,
      ` · retrying every ${POLL_MS / 1000}s · values below are last known, frozen `
      + (lastGoodAt ? age(lastGoodAt) + ` (${since})` : "at an unknown time") + "."));
  }
  for (const id of ["centre", "centre2"]) $(id).classList.toggle("stale", stale);
}

function paneWidth() {
  const insp = $("inspector").dataset.idle === "1" ? 0 : INSPECTOR_W;
  return Math.max(0, window.innerWidth - RAIL_W - insp);
}
function canSplit() { return paneWidth() >= PANE_MIN * 2; }

/* ============================== stream ============================== */

/* ============================== today ==============================
 *
 * Otto talking to the owner, in that order:
 *   1. what Otto chose to say          (notices, deliberate messages)
 *   2. what the day contained          (yesterday's rollup + today's check-in)
 *   3. anything running right now      (only when something is)
 *   4. what needs the owner, what is unwatched
 *
 * The old Stream led with a ranked list Otto recomputes every poll. That is a
 * report, not a conversation, and the owner said it was not interesting. Messages first
 * inverts it: the top of this view is only ever something Otto decided to raise.
 */

function viewToday() {
  const s = state;
  const wrap = el("div", "today-wrap");

  if (scope) {
    const rp = repoPanel(s);
    if (rp) wrap.appendChild(rp);
  }

  /* -- 1. from Otto -- */
  const notices = (s.notices || []).filter(inDomain);
  const unread = notices.filter((n) => !n.read_at);
  const top = el("div", "today-top");

  /* -- 0. outreach on the clock --
   * Above everything, including notices, and only when something is actually held.
   * This is the one panel in Otto with a DEADLINE: every other row here can be read an
   * hour later without changing what happens, and a held message read an hour later
   * has already gone. */
  const held = ((s.outreach || {}).items || []).filter((o) => o.state === "held");
  if (held.length) top.appendChild(outreachPanel(s, held));

  const msgs = el("section", "sec");

  /* -- the celebration --
   * A known failure that healed. The daemon posts exactly one of these per
   * annotation; it sits above the notice list in gold because it is the one kind of
   * news Otto has that REMOVES something from the board. Dismissing the notice is
   * not enough: the annotation is still lying until it is cleared. */
  for (const n of unread.filter((x) => x.source === "known-stale")) {
    msgs.appendChild(staleBanner(n));
  }

  const mh = secHead("From Otto", unread.length ? unread.length + " unread" : "");
  if (unread.length) {
    const clear = el("button", "cmd-btn", "mark all read");
    clear.type = "button";
    clear.addEventListener("click", async () => {
      try { await act("otto notices --read", "/api/notices/read-all", { method: "POST" }); await poll(); }
      catch (e) { toast(e.message, true); }
    });
    mh.appendChild(clear);
  }
  msgs.appendChild(mh);

  const SHOW = 12;
  for (const n of notices.slice(0, SHOW)) {
    const card = el("article", "notice" + (n.read_at ? " read" : "") + " lv-" + n.level);
    const head = el("div", "notice-head");
    head.appendChild(pill(n.level, n.level === "info" ? "idle" : n.level));
    head.appendChild(el("span", "notice-src", n.source));
    /* A collapsed repeat has to say so, or the dedupe reads as lost information.
     * "seen 7x" is also the more useful fact: it is how long this has been nagging. */
    if (n.seen_count > 1) {
      head.appendChild(el("span", "chip-sm chip-seen", `seen ${n.seen_count}x`));
    }
    head.appendChild(el("span", "spacer"));
    head.appendChild(ageEl(n.at, "mono-dim"));
    card.appendChild(head);
    card.appendChild(el("h3", null, n.title));
    if (n.body) {
      card.appendChild(el("p", "notice-body", n.body));
      /* Say there is more, and how much. A clamped body is indistinguishable from
       * a short one, so without this the card gives no reason to click.
       *
       * Counts ITEMS, not hidden lines. The clamp is three RENDERED lines, and a
       * long item wraps to two or three of them, so "7 more lines" would be wrong
       * by however the text happened to wrap. How many things the notice is about
       * is both stable and the thing worth knowing. */
      const count = noticeLines(n.body).length;
      const more = el("div", "notice-more");
      more.appendChild(ico("ph ph-arrows-out-simple"));
      more.appendChild(el("span", null,
        count > 1 ? count + " items · click to read" : "click to read"));
      card.appendChild(more);
    }

    /* The whole card opens the detail. The action buttons below already call
     * stopPropagation, so they keep working without opening it. */
    if (n.body) {
      card.classList.add("clickable");
      card.addEventListener("click", () => openNotice(n));
    }

    const acts = el("div", "notice-acts");
    if (n.command) {
      const c = el("button", "cmd-btn", n.command);
      c.type = "button";
      c.addEventListener("click", (e) => { e.stopPropagation(); copy(n.command); });
      acts.appendChild(c);
    }
    if (!n.read_at) {
      const r = el("button", "btn btn-secondary btn-xs", "Got it");
      r.type = "button";
      r.addEventListener("click", async (e) => {
        e.stopPropagation();
        r.disabled = true;
        try {
          await act(`POST /api/notices/${n.id.slice(0, 6)}…/read`, "/api/notices/" + n.id + "/read", { method: "POST" });
          await poll();
        } catch (err) { toast(err.message, true); r.disabled = false; }
      });
      acts.appendChild(r);
    }
    const x = el("button", "btn btn-secondary btn-xs", "Dismiss");
    x.type = "button";
    x.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await act(`DELETE /api/notices/${n.id.slice(0, 6)}…`, "/api/notices/" + n.id, { method: "DELETE" });
        await poll();
      } catch (err) { toast(err.message, true); }
    });
    acts.appendChild(x);
    card.appendChild(acts);
    msgs.appendChild(card);
  }
  /* What the list is NOT showing, stated. Twelve cards over a thirteenth is a list
   * that looks complete and is not. */
  if (notices.length > SHOW) {
    const more = el("div", "more-row");
    more.appendChild(el("span", null, `${notices.length - SHOW} older not shown · `));
    const b = el("button", "cmd-btn", "otto notices --limit " + notices.length);
    b.type = "button";
    b.addEventListener("click", () => copy("otto notices --limit " + notices.length));
    more.appendChild(b);
    msgs.appendChild(more);
  }
  if (!notices.length) {
    msgs.appendChild(el("p", "sec-empty",
      "Nothing from Otto. It speaks when a loop goes dark, a run needs you, or the "
      + "morning rollup lands. Silence here means it has nothing to raise."));
  }
  top.appendChild(msgs);

  /* -- 2. the day -- */
  const dayBox = el("section", "sec");
  const prev = (s.day_prev || {}).rollup || null;
  const ci = (s.day || {}).checkin || null;
  dayBox.appendChild(secHead("Your day",
    prev ? prev.session_count + " sessions yesterday" : ""));

  if (prev) {
    const card = el("div", "day-card");
    const wall = prev.wall_minutes || 0, att = prev.attention_minutes || 0;
    const hm = (m) => Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
    const stat = el("div", "day-stats");
    for (const [v, k, hint] of [
      [hm(wall), "engaged", "real elapsed, idle gaps removed"],
      [hm(att), "attention", "summed across sessions, so it can exceed the clock"],
      [String(prev.session_count || 0), "sessions", ""],
    ]) {
      const cell = el("div", "day-stat");
      if (hint) cell.title = hint;
      cell.appendChild(el("div", "v", v));
      cell.appendChild(el("div", "k", k));
      stat.appendChild(cell);
    }
    card.appendChild(stat);

    const byProj = Object.entries(prev.by_project || {}).filter((kv) => kv[1] >= 5);
    const max = Math.max(1, ...byProj.map((kv) => kv[1]));
    for (const [proj, mins] of byProj.slice(0, 6)) {
      const row = el("div", "day-proj");
      row.appendChild(el("span", "mins", mins + "m"));
      row.appendChild(el("span", "name", proj));
      const track = el("span", "track");
      const fill = el("span", "fill");
      fill.style.width = Math.round((mins / max) * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      card.appendChild(row);
    }
    for (const ls of (prev.long_sessions || []).slice(0, 3)) {
      const row = el("div", "day-long");
      row.appendChild(el("span", "mins", ls.minutes + "m"));
      const b = el("div");
      b.appendChild(el("div", "t", ls.title || "(untitled)"));
      /* Stated intent verbatim next to elapsed time. That pair is the point: a
       * summary massaged into agreement with the outcome would hide the drift. */
      if (ls.intent) b.appendChild(el("div", "i", ls.intent));
      row.appendChild(b);
      card.appendChild(row);
    }
    dayBox.appendChild(card);
  } else {
    dayBox.appendChild(el("p", "sec-empty",
      "No rollup for yesterday yet. It lands at 06:00, or run otto day --refresh."));
  }

  const ciBox = el("div", "checkin");
  if (ci) {
    const h = el("div", "checkin-head");
    h.appendChild(el("strong", null, "Checked in"));
    if (ci.energy) h.appendChild(el("span", "chip-sm", "energy " + ci.energy + "/5"));
    h.appendChild(el("span", "spacer"));
    h.appendChild(ageEl(ci.at, "mono-dim"));
    ciBox.appendChild(h);
    if (ci.note) ciBox.appendChild(el("p", "checkin-note", ci.note));
  } else {
    ciBox.appendChild(el("div", "checkin-head",
      "How did you sleep, and what actually matters today?"));
    const ta = el("textarea", "input");
    ta.rows = 2;
    ta.id = "checkin-input";
    ta.placeholder = "A line or two. Otto cannot see this half.";
    ta.value = checkinDraft;
    ta.addEventListener("input", () => { checkinDraft = ta.value; });
    ciBox.appendChild(ta);
    const row = el("div", "checkin-acts");
    row.appendChild(el("span", "lbl", "energy"));
    const seg = el("div", "energy-seg");
    for (let i = 1; i <= 5; i++) {
      const b = el("button", checkinEnergy === i ? "on" : null, String(i));
      b.type = "button";
      b.title = "energy " + i + "/5";
      b.addEventListener("click", () => { checkinEnergy = i; render(true); });
      seg.appendChild(b);
    }
    row.appendChild(seg);
    row.appendChild(el("span", "spacer"));
    const send = el("button", "btn btn-primary btn-sm", "Save");
    send.type = "button";
    send.addEventListener("click", async () => {
      const note = checkinDraft.trim();
      if (!note && !checkinEnergy) { toast("nothing to record", true); return; }
      send.disabled = true;
      try {
        await act("otto checkin" + (checkinEnergy ? " --energy " + checkinEnergy : "")
          + (note ? ' "' + note.slice(0, 40).replace(/"/g, "") + (note.length > 40 ? "…" : "") + '"' : ""),
          "/api/day/" + new Date().toISOString().slice(0, 10), {
          method: "PUT", headers: { "content-type": "application/json" },
          body: JSON.stringify({ note: note || null, energy: checkinEnergy }),
        });
        checkinDraft = ""; checkinEnergy = null;
        toast("checked in");
        await poll();
      } catch (e) { toast(e.message, true); } finally { send.disabled = false; }
    });
    row.appendChild(send);
    ciBox.appendChild(row);
  }
  dayBox.appendChild(ciBox);
  top.appendChild(dayBox);
  wrap.appendChild(top);

  /* -- 3. in flight, only when there is something -- */
  const live = (s.live || []).filter(keep);
  if (live.length) {
    const lb = el("section", "sec today-live");
    lb.appendChild(secHead("In flight", live.length + " running"));
    for (const r of live) lb.appendChild(liveCard(r));
    wrap.appendChild(lb);
  }

  /* -- 3b. sessions --
   * Deliberately its own section, directly under In flight, because the two are
   * near-opposites and the contrast is the information. In flight is what Otto
   * SPAWNED, watched from outside. This is what Claude Code reports from inside,
   * including every terminal Otto never started, which used to be nothing at all. */
  const sessBox = sessionsSection(s);
  if (sessBox) wrap.appendChild(sessBox);

  wrap.appendChild(el("hr", "hr-fade"));

  /* -- 4. needs you / blind spots -- */
  const lower = el("div", "today-lower");
  lower.appendChild(sectionNeedsYou(s));
  lower.appendChild(sectionGaps(s));
  wrap.appendChild(lower);
  return wrap;
}

/* Live Claude Code sessions, from their own hooks. Returns null when there is
 * nothing worth a section, so a machine with no sessions shows no empty box. */
function sessionsSection(s) {
  const data = s.sessions || {};
  const summary = data.summary || {};
  const rows = (data.sessions || []).filter(inDomain).filter((x) => x.state !== "offline");

  if (!summary.hooks_installed) {
    /* Only nag once something has reported. A machine that never opted in gets
     * this from the gaps panel, which is where "nothing is watching" belongs. */
    if (!summary.total) return null;
    const box = el("section", "sec");
    box.appendChild(secHead("Sessions", "hooks not installed"));
    box.appendChild(el("p", "sec-note",
      "Otto can only see sessions it spawned itself. Run: otto sessions install"));
    return box;
  }
  if (!rows.length) return null;

  const box = el("section", "sec");
  const counts = ["busy", "waiting", "idle"]
    .filter((k) => summary[k]).map((k) => summary[k] + " " + k).join(", ");
  box.appendChild(secHead("Sessions", counts));
  box.appendChild(el("p", "sec-note",
    "Reported by each session's own hooks, not inferred from a log. "
    + (summary.unspawned
      ? summary.unspawned + " of these Otto did not spawn and could not see before."
      : "")));

  /* waiting first: it is the only state that is asking the owner for something. */
  const order = { waiting: 0, busy: 1, idle: 2 };
  rows.sort((a, b) => (order[a.state] ?? 3) - (order[b.state] ?? 3)
    || String(a.state_since).localeCompare(String(b.state_since)));

  appendPaged(box, rows, "sessions", (x) => {
    const card = el("article", "live-card");
    const where = x.repo || (x.cwd ? x.cwd.split(/[\\/]/).filter(Boolean).pop() : "unknown");
    const top = el("div", "live-top");
    top.appendChild(pill(x.state, x.state === "busy" ? "running"
      : x.state === "waiting" ? "due" : "idle"));
    /* The title is what the session is about (its transcript's first prompt, or a
     * name the owner gave it). Click it to rename: the transcript's guess is a start,
     * not a verdict. */
    const name = el("strong", "live-name", x.title || where);
    name.title = "click to rename";
    name.style.cursor = "text";
    name.onclick = async () => {
      const t = prompt("Name this session", x.title || "");
      if (t === null || !t.trim()) return;
      try { await api(`/api/sessions/${x.session_id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: t.trim() }) }); await poll(); }
      catch (e) { toast(e.message, true); }
    };
    top.appendChild(name);
    top.appendChild(el("span", "mono-dim", x.session_id.slice(0, 8)));
    if (x.host) top.appendChild(el("span", "mono-dim", x.host));
    else if (!x.run_id) top.appendChild(pill("not Otto's", "backlog"));
    top.appendChild(el("span", "spacer"));
    /* Open: a live session's window comes forward; an ended one resumes in a new
     * Windows Terminal tab. The daemon does it, since it runs in the owner's desktop
     * session. */
    const open = el("button", "density-chip", "open");
    open.type = "button";
    open.onclick = async (ev) => {
      ev.stopPropagation();
      try { const r = await api(`/api/sessions/${x.session_id}/open`, { method: "POST" }); toast(r.message); }
      catch (e) { toast(e.message, true); }
    };
    top.appendChild(open);
    top.appendChild(domTag(x.domain || "work"));
    top.appendChild(ageEl(x.state_since, "mono-dim"));
    card.appendChild(top);

    const detail = x.note || x.last_message;
    if (detail) {
      const now = el("div", "live-now");
      now.appendChild(el("span", "lbl", x.state === "waiting" ? "asking" : "last"));
      now.appendChild(el("span", null, detail));
      card.appendChild(now);
    }
    if (x.cwd) {
      const m = el("div", "live-meta");
      if (x.title) m.appendChild(el("span", "mono-dim", where + " · "));
      m.appendChild(pathEl(x.cwd));
      card.appendChild(m);
    }
    return card;
  });
  return box;
}

function liveCard(r) {
  const card = el("article", "live-card");
  const top = el("div", "live-top");
  top.appendChild(el("span", "spin"));
  top.appendChild(el("strong", "live-name", r.name));
  top.appendChild(el("span", "mono-dim", r.id.slice(0, 6) + " · pid " + r.pid));
  if (r.pane_id) top.appendChild(el("span", "mono-dim", "pane " + r.pane_id));
  top.appendChild(el("span", "spacer"));
  /* Plan progress, in the header, so "40 minutes in" reads against what is typical
   * for this schedule rather than against nothing. */
  if (r.plan && r.plan.elapsed_seconds != null) {
    let t = Math.round(r.plan.elapsed_seconds / 60) + "m";
    if (r.plan.expected_seconds) t += " / ~" + Math.round(r.plan.expected_seconds / 60) + "m typical";
    const pt = el("span", "mono-dim", t);
    if (r.plan.expected_seconds && r.plan.elapsed_seconds > r.plan.expected_seconds * 1.5) pt.style.color = WARN;
    top.appendChild(pt);
  } else {
    top.appendChild(ageEl(r.started, "mono-dim"));
  }
  card.appendChild(top);
  const now = el("div", "live-now");
  now.appendChild(el("span", "lbl", "now"));
  now.appendChild(el("span", null, r.current || "working"));
  card.appendChild(now);
  const meta = el("div", "live-meta");
  const bits = [];
  if (r.tool_calls != null) bits.push(r.tool_calls + " tool calls");
  if (r.subtasks_started) bits.push((r.subtasks_done || 0) + "/" + r.subtasks_started + " sub-tasks");
  if (r.output_tokens) bits.push(kfmt(r.output_tokens) + " out");
  meta.appendChild(el("span", null, bits.join("  ·  ") || "no output yet"));
  meta.appendChild(el("span", "spacer"));
  if (r.last_activity) {
    const secs = Math.max(0, Math.round((Date.now() - Date.parse(r.last_activity)) / 1000));
    const w = el("span", null, "last wrote " + secs + "s ago");
    if (secs > 45) w.style.color = WARN;
    meta.appendChild(w);
  }
  card.appendChild(meta);
  if (r.plan && r.plan.phases) {
    const rail = phaseRail(r.plan);
    rail.style.margin = "var(--space-3) 0 0";
    card.appendChild(rail);
  }
  card.addEventListener("click", () => select("run", r.id));
  return card;
}

/* The gold banner: a KNOWN failure that started passing. The notice carries the
 * `otto known rm <kind> <name>` command; the button here runs the same thing. */
function staleBanner(n) {
  const box = el("div", "stale-banner");
  box.appendChild(ico("ph ph-confetti"));
  const body = el("div");
  body.style.minWidth = "0";
  body.appendChild(el("div", "kick", "KNOWN FAILURE IS NOW STALE"));
  body.appendChild(el("h3", null, n.title.replace(/^Known failure is now stale:\s*/i, "")));
  body.appendChild(el("p", null, n.body || "Somebody fixed the underlying thing. The annotation is lying about it now."));
  box.appendChild(body);
  const acts = el("div", "acts");
  const m = /^otto known rm (\S+) (\S+)/.exec(n.command || "");
  if (m) {
    const clear = el("button", "btn btn-primary btn-sm");
    clear.type = "button";
    clear.appendChild(ico("ph ph-eraser"));
    clear.appendChild(el("span", null, "Clear annotation"));
    clear.addEventListener("click", async () => {
      clear.disabled = true;
      try {
        await act(n.command, `/api/known/${encodeURIComponent(m[1])}/${encodeURIComponent(m[2])}`, { method: "DELETE" });
        await api("/api/notices/" + n.id + "/read", { method: "POST" });
        toast(`${m[2]} is no longer marked known`);
        await poll();
      } catch (e) { toast(e.message, true); clear.disabled = false; }
    });
    acts.appendChild(clear);
  }
  const got = el("button", "btn btn-secondary btn-xs", "Later");
  got.type = "button";
  got.addEventListener("click", async () => {
    try { await api("/api/notices/" + n.id + "/read", { method: "POST" }); await poll(); }
    catch (e) { toast(e.message, true); }
  });
  acts.appendChild(got);
  box.appendChild(acts);
  return box;
}

function sectionNeedsYou(s) {
  const nextRows = ((s.briefing && s.briefing.next) || []).filter(keep);
  const sec = el("section", "sec");
  sec.appendChild(secHead("Needs you", nextRows.length + " ranked"));
  sec.appendChild(el("p", "sec-note",
    "Plain scoring over stored state. No tokens, no latency, identical every run."));
  const ICO = {
    outage: "ph ph-warning-octagon", schedule: "ph ph-clock", run: "ph ph-ghost",
    task: "ph ph-check-square", gap: "ph ph-eye-slash", due: "ph ph-clock",
  };
  let lastBand = null;
  nextRows.forEach((r, i) => {
    if (r.band !== lastBand) {
      lastBand = r.band;
      const b = el("div", "band");
      b.appendChild(el("span", "lbl", r.band));
      /* The count, so the rail badge has somewhere on this page to land. The header
       * says "8 ranked" and the badge said "3", and nothing on the page was a 3, so
       * there was no way to reconcile them by looking. */
      b.appendChild(el("span", "band-n",
        String(nextRows.filter((x) => x.band === r.band).length)));
      b.appendChild(el("span", "sec-rule"));
      sec.appendChild(b);
    }
    const sev = r.kind === "outage" ? "crit" : (r.band === "today" ? "warn" : "idle");
    const card = el("article", "need"
      + (r.band === "today" ? " today" : "")
      + (r.kind === "outage" ? " crit" : ""));
    const gut = el("div", "need-gutter");
    gut.appendChild(el("span", "need-rank", String(i + 1).padStart(2, "0")));
    gut.appendChild(el("span", "need-thread"));
    card.appendChild(gut);
    const body = el("div");
    body.style.minWidth = "0";
    const tags = el("div", "need-tags");
    tags.appendChild(pill(r.kind, sev, ICO[r.kind] || "ph ph-dot"));
    tags.appendChild(domTag(r.domain));
    body.appendChild(tags);
    body.appendChild(el("h3", null, r.title));
    body.appendChild(el("p", "need-why", r.why));
    const acts = el("div", "need-acts");
    const sched = (s.schedules || []).find(
      (x) => r.title === "run " + x.name || r.title === x.name + " has stopped running");
    if (sched) {
      const rb = el("button", "btn btn-primary btn-sm");
      rb.type = "button";
      rb.appendChild(ico("ph ph-play"));
      rb.appendChild(el("span", null, "Run " + sched.name));
      rb.addEventListener("click", (e) => { e.stopPropagation(); launch(sched.name, rb); });
      acts.appendChild(rb);
    }
    if (r.command) {
      const cb = el("button", "cmd-btn", r.command);
      cb.type = "button";
      cb.addEventListener("click", (e) => { e.stopPropagation(); copy(r.command); });
      acts.appendChild(cb);
    }
    body.appendChild(acts);
    card.appendChild(body);
    card.addEventListener("click", () => openAction(actionable(r, "stream")));
    sec.appendChild(card);
  });
  if (!nextRows.length) {
    sec.appendChild(emptyState(scope
      ? "Nothing ranked in this directory."
      : "Nothing to do. Every schedule is fresh and nothing is waiting.",
      scope ? "otto next" : "otto gaps"));
  }
  return sec;
}

/* An empty state says what to do next. The command is copyable, so the blank is a
 * prompt rather than a description of absence. */
function emptyState(text, cmd) {
  const p = el("div", "sec-empty");
  p.appendChild(el("div", null, text));
  if (cmd) {
    const b = el("button", "cmd-btn", cmd);
    b.type = "button";
    b.addEventListener("click", () => copy(cmd));
    p.appendChild(b);
  }
  return p;
}

function sectionGaps(s) {
  const gaps = ((s.briefing && s.briefing.gaps) || []).filter(keep);
  const sec = el("section", "sec");
  sec.appendChild(secHead("Blind spots", String(gaps.length), true));
  sec.appendChild(el("p", "sec-note",
    "An alert is something that broke and Otto was watching. A gap is something nothing was ever watching."));
  for (const g of gaps) {
    const card = el("article", "gap");
    const tags = el("div", "gap-tags");
    tags.appendChild(el("span", "gap-kind", g.kind));
    tags.appendChild(domTag(g.domain));
    card.appendChild(tags);
    card.appendChild(el("h3", null, g.title));
    card.appendChild(el("p", "gap-why", g.why));
    if (g.command) {
      const b = el("button", "cmd-btn block", g.command);
      b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); copy(g.command); });
      card.appendChild(b);
    }
    card.style.cursor = "pointer";
    card.addEventListener("click", () => openAction(actionable(g, "stream")));
    sec.appendChild(card);
  }
  if (!gaps.length) {
    sec.appendChild(emptyState("No blind spots. Everything Otto knows about has a watcher.",
      "otto retire"));
  }
  return sec;
}

/* Messages Otto is about to send a colleague, with the time left on each.
 *
 * Two things this panel must do that an ordinary list does not. It has to show the
 * FULL text, because the owner is being asked to approve by saying nothing and approving
 * text you were shown three lines of is not approval. And it has to show `why`, which
 * is the field the decision actually turns on: the message reads fine on its own, the
 * question is always whether the reason behind it is sound. */
function outreachPanel(s, held) {
  const sum = (s.outreach || {}).summary || {};
  const sec = el("section", "sec outreach-sec");
  const h = secHead("Otto wants to send", held.length + " held", true);
  sec.appendChild(h);

  if (!sum.enabled) {
    sec.appendChild(el("p", "outreach-off",
      "Sending is OFF. These expire unsent, so this panel is a preview of what Otto "
      + "would have said. Set OTTO_OUTREACH=1 when the previews stop surprising you."));
  }

  for (const o of held) {
    const card = el("article", "outreach-card" + (personMatch(o.to + " " + o.target) ? "" : " dimmed"));
    const head = el("div", "notice-head");
    head.appendChild(pill(o.tier > 0 ? "tier " + o.tier : "auto",
      o.tier > 0 ? "warn" : "idle"));
    head.appendChild(el("span", "notice-src", o.to));
    head.appendChild(el("span", "spacer"));
    /* The countdown is the whole point, so it is the loudest thing on the card. A
     * tier-1 message never sends itself, so it must not show a deadline it does not
     * have: that would train the owner to rush a decision nothing was waiting on. */
    head.appendChild(o.tier > 0
      ? el("span", "mono-dim", "waits for you")
      : countdownEl(o.send_after, "outreach-clock", "sends "));
    card.appendChild(head);

    card.appendChild(el("p", "outreach-body", o.body));
    card.appendChild(el("p", "outreach-why", "why: " + o.why));

    /* -- the hold strip --
     * A deadline Otto set deserves a visible clock. Word, progress, time left, and
     * the two ways to move the deadline: push it, or cut it short. A tier-1 message
     * has no clock because nothing is waiting on it. */
    if (o.tier === 0) {
      const strip = el("div", "hold-strip");
      strip.appendChild(el("span", "hold-word", "HOLDING"));
      const track = el("div", "hold-track");
      const fill = el("span", "hold-fill");
      const total = Math.max(1, (o.hold_minutes || 10) * 60000);
      const left = Math.max(0, Date.parse(o.send_after) - Date.now());
      fill.style.width = Math.min(100, Math.round(100 * (1 - left / total))) + "%";
      /* Both the bar and the figure tick in place with the countdown above. */
      fill.dataset.hold = o.send_after; fill.dataset.total = String(total);
      track.appendChild(fill);
      strip.appendChild(track);
      const leftEl = el("span", "hold-left", holdLeft(left));
      leftEl.dataset.hold = o.send_after;
      strip.appendChild(leftEl);
      hasCountdown = true;
      card.appendChild(strip);
    }

    const acts = el("div", "hold-acts");
    const stop = el("button", "btn btn-secondary btn-xs", "Don't send");
    stop.type = "button";
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      try {
        await act(`otto outreach --kill ${o.id.slice(0, 6)}`, "/api/outreach/" + o.id + "/kill", { method: "POST" });
        toast("killed"); await poll();
      } catch (e) { toast(e.message, true); stop.disabled = false; }
    });
    acts.appendChild(stop);

    if (o.tier === 0) {
      const more = el("button", "btn btn-secondary btn-xs", "+10 min");
      more.type = "button";
      more.title = "Push the send back ten minutes (24h from compose is the ceiling)";
      more.addEventListener("click", async () => {
        more.disabled = true;
        try {
          await act(`otto outreach --extend ${o.id.slice(0, 6)} --minutes 10`,
            "/api/outreach/" + o.id + "/extend?minutes=10", { method: "POST" });
          toast("hold extended 10 min"); await poll();
        } catch (e) { toast(e.message, true); } finally { more.disabled = false; }
      });
      acts.appendChild(more);
    }

    const go = el("button", "btn btn-xs", "Send now");
    go.type = "button";
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        await act(`otto outreach --send ${o.id.slice(0, 6)}`, "/api/outreach/" + o.id + "/send", { method: "POST" });
        toast("sent"); await poll();
      } catch (e) { toast(e.message, true); go.disabled = false; }
    });
    acts.appendChild(go);
    acts.appendChild(el("span", "spacer"));
    acts.appendChild(el("span", "mono-dim", o.source));
    card.appendChild(acts);
    sec.appendChild(card);
  }
  return sec;
}

/* "7m 02s left" / "now". Seconds shown under a minute because that is when the
 * decision is being made. */
function holdLeft(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "sending now";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s left";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + String(s % 60).padStart(2, "0") + "s left";
  return Math.round(m / 60) + "h left";
}

/* "in 7m" / "now". Deliberately not age(): this counts DOWN, and rendering a future
 * timestamp through a function that says "ago" is how you get a panel that tells you
 * a message went out nine minutes before it did. */
function countdown(ts) {
  const ms = Date.parse(ts) - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) return "now";
  const mins = Math.round(ms / 60000);
  if (mins < 1) return "in " + Math.round(ms / 1000) + "s";
  if (mins < 60) return "in " + mins + "m";
  return "in " + Math.round(mins / 60) + "h";
}

function secHead(title, count, warn) {
  const h = el("div", "sec-head");
  h.appendChild(el("h2", "sec-title" + (warn ? " warn" : ""), title));
  if (count) h.appendChild(el("span", "sec-count", count));
  h.appendChild(el("span", "sec-rule"));
  return h;
}

/* ============================== board ============================== */

/* /api/state.board is {columns:[{key,label,count,cards}], total, stored, derived},
 * NOT a map of status -> cards. Column order and labels come from the server so the
 * dashboard cannot disagree with the CLI about what the columns are. */
function boardColumns() {
  const b = (state && state.board) || {};
  return b.columns || [];
}
function allCards() {
  return boardColumns().flatMap((c) => c.cards || []);
}

/* ── paging ──
 * The first page of a long list, then a "Show N more" control that appends the
 * next page in place (no rebuild, scroll stays put) and loads itself when it
 * scrolls into view. Selection and drag work on whatever is rendered; select-all
 * and batch moves go by id and cover the unrendered cards too, which is what
 * "all shown here" meant before paging and still means. */
function appendPaged(host, items, key, make, before) {
  const shown = Math.min(items.length, Math.max(PAGE_CARDS, colShown[key] || 0));
  colShown[key] = shown;
  const frag = document.createDocumentFragment();
  for (const it of items.slice(0, shown)) frag.appendChild(make(it));
  host.insertBefore(frag, before || null);
  if (shown < items.length) host.insertBefore(moreButton(host, items, key, make), before || null);
}
function moreButton(host, items, key, make) {
  const b = el("button", "col-more");
  b.type = "button";
  const label = () => {
    const rest = items.length - colShown[key];
    b.textContent = `Show ${Math.min(PAGE_CARDS, rest)} more · ${rest} not rendered`;
  };
  label();
  const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) grow(); }, { rootMargin: "240px" });
  const grow = () => {
    if (!b.isConnected) { io.disconnect(); return; }
    const from = colShown[key], to = Math.min(items.length, from + PAGE_CARDS);
    const frag = document.createDocumentFragment();
    for (const it of items.slice(from, to)) frag.appendChild(make(it));
    host.insertBefore(frag, b);
    colShown[key] = to;
    if (to >= items.length) { io.disconnect(); b.remove(); } else label();
  };
  b.addEventListener("click", grow);
  io.observe(b);
  return b;
}

/* ── created-date filter ── */
const CREATED_PRESETS = [
  ["any", "any time"], ["today", "today"], ["7d", "last 7 days"], ["30d", "last 30 days"],
  ["older30", "older than 30 days"], ["custom", "custom range…"],
];
function inCreated(k) {
  const f = createdFilter;
  if (!f || f.preset === "any") return true;
  if (!k.created) return false;
  const t = Date.parse(k.created);
  if (Number.isNaN(t)) return false;
  const now = Date.now(), day = 86400000;
  if (f.preset === "today") {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    return t >= d.getTime();
  }
  if (f.preset === "7d") return t >= now - 7 * day;
  if (f.preset === "30d") return t >= now - 30 * day;
  if (f.preset === "older30") return t < now - 30 * day;
  if (f.preset === "custom") {
    if (f.from && t < Date.parse(f.from)) return false;
    if (f.to && t >= Date.parse(f.to) + day) return false;   // inclusive end date
    return true;
  }
  return true;
}
function createdFilterEl() {
  const box = el("span", "created-filter");
  box.appendChild(ico("ph ph-calendar-blank"));
  box.appendChild(el("span", null, "created"));
  const sel = el("select");
  for (const [v, label] of CREATED_PRESETS) {
    const o = el("option", null, label); o.value = v;
    if (createdFilter.preset === v) o.selected = true;
    sel.appendChild(o);
  }
  if (createdFilter.preset !== "any") sel.classList.add("on");
  sel.addEventListener("change", () => {
    createdFilter = { ...createdFilter, preset: sel.value };
    render(true);
  });
  box.appendChild(sel);
  if (createdFilter.preset === "custom") {
    for (const key of ["from", "to"]) {
      const inp = el("input"); inp.type = "date"; inp.value = createdFilter[key] || "";
      inp.title = key === "from" ? "created on or after" : "created on or before";
      if (inp.value) inp.classList.add("on");
      inp.addEventListener("change", () => {
        createdFilter = { ...createdFilter, [key]: inp.value };
        render(true);
      });
      box.appendChild(el("span", null, key === "from" ? "from" : "to"));
      box.appendChild(inp);
    }
  }
  return box;
}
/* The board's own filter: domain and scope as everywhere, plus the created date. */
function keepCard(k) { return keep(k) && inCreated(k); }

/* Every visible, movable card, in column order, for select-all and ranges. */
function visibleCards(colKey) {
  return boardColumns()
    .filter((c) => !colKey || c.key === colKey)
    .flatMap((c) => (c.cards || []).filter(keepCard).filter((k) => k.movable !== false));
}
function toggleSelected(id, on) {
  if (on === undefined) on = !selected.has(id);
  if (on) selected.add(id); else selected.delete(id);
}
function clearCardSelection() { selected.clear(); lastPick = null; render(true); }

function viewBoard() {
  const s = state;
  const wrap = el("div", "board-wrap" + (cardDetail === "full" ? " detail-full" : "")
    + (selected.size ? " selecting" : ""));

  const bar = el("div", "board-bar");
  bar.appendChild(el("span", null, "Backlog means not ready. Queued means run it."));
  bar.appendChild(el("span", "spacer"));
  bar.appendChild(createdFilterEl());
  if (s.board && s.board.faded) {
    const f = el("span", "sel-hint", `${s.board.faded} faded · otto task ls --faded`);
    bar.appendChild(f);
  }
  if (s.board && s.board.duplicates) {
    /* Closed into another card by the dedupe sweep. Not a completion, so not in
       Done; said out loud here so the board never quietly shows a subset. */
    const d = el("span", "sel-hint", `${s.board.duplicates} merged · otto task ls --duplicates`);
    bar.appendChild(d);
  }

  const clamped = cardDetail === "clamp";
  const dens = el("button", "density-chip");
  dens.type = "button";
  dens.appendChild(ico(clamped ? "ph ph-arrows-out-line-vertical" : "ph ph-arrows-in-line-vertical"));
  dens.appendChild(el("span", null, clamped ? "Compact" : "Full detail"));
  dens.title = clamped
    ? "Cards show 3 lines of detail. Click for the full text on every card."
    : "Cards show their whole detail. Click to go back to 3 lines.";
  dens.addEventListener("click", () => {
    cardDetail = clamped ? "full" : "clamp";
    render(true);
  });
  bar.appendChild(dens);

  const dsp = (s.dispatch || {});
  const chip = el("button", "dispatch-chip" + (dsp.enabled ? "" : " off"));
  chip.type = "button";
  chip.appendChild(ico(dsp.enabled ? "ph ph-lightning" : "ph ph-lightning-slash"));
  chip.appendChild(el("span", null, dsp.enabled ? "Auto-dispatch on" : "Auto-dispatch OFF"));
  chip.title = "Click to toggle";
  chip.addEventListener("click", async () => {
    chip.disabled = true;
    try {
      // `enabled` is a QUERY param on this endpoint, not a JSON body. A body
      // here 422s, and only on click, which is the worst place to find out.
      await act(`otto autodispatch ${dsp.enabled ? "off" : "on"}`,
        `/api/dispatch?enabled=${!dsp.enabled}`, { method: "POST" });
      toast("auto-dispatch " + (dsp.enabled ? "off" : "on"));
      await poll();
    } catch (e) { toast(e.message, true); } finally { chip.disabled = false; }
  });
  bar.appendChild(chip);

  /* Person focus applies here by name match on the card text, because a card does
   * not carry a slug. Said out loud, with the count, so a thinned board reads as
   * filtered rather than as finished. */
  if (personSel) {
    const all = allCards().filter(keepCard);
    const hit = all.filter((k) => personMatch(k.title + " " + (k.detail || "") + " " + (k.tags || []).join(" "))).length;
    const fn = el("span", "focus-note");
    fn.appendChild(ico("ph ph-user-focus"));
    fn.appendChild(el("span", null, `${hit} of ${all.length} cards mention ${personSel.name}`));
    bar.appendChild(fn);
  }
  wrap.appendChild(bar);

  const cols = el("div", "board-cols");
  for (const column of boardColumns()) {
    const key = column.key;
    const cards = (column.cards || []).filter(keepCard);
    const folded = key === "done" && !doneOpen;
    const col = el("div", "board-col"
      + (folded ? " folded" : "")
      + (overCol === key ? " over" : ""));

    const head = el("div", "col-head" + (key === "done" ? " foldable" : ""));
    /* Select every visible card in the column. Visible means after the filters, so
     * "created: older than 30 days" then this checkbox then Done is the sweep. */
    const movableHere = cards.filter((k) => k.movable !== false);
    if (movableHere.length && !folded) {
      const cb = el("input", "col-check"); cb.type = "checkbox";
      const allOn = movableHere.every((k) => selected.has(k.id));
      cb.checked = allOn;
      cb.indeterminate = !allOn && movableHere.some((k) => selected.has(k.id));
      cb.title = allOn ? "Deselect all in this column" : `Select all ${movableHere.length} shown here`;
      cb.addEventListener("click", (e) => {
        e.stopPropagation();
        for (const k of movableHere) toggleSelected(k.id, !allOn);
        render(true);
      });
      head.appendChild(cb);
    }
    head.appendChild(el("h4", key === "needs-you" ? "crit" : null, column.label || key));
    head.appendChild(el("span", "col-count", String(cards.length)));
    if (key === "done") {
      head.appendChild(el("span", "spacer"));
      head.appendChild(ico(folded ? "ph ph-caret-down" : "ph ph-caret-up"));
      head.title = folded ? "Show what got finished" : "Fold Done away";
      head.addEventListener("click", () => { doneOpen = !doneOpen; render(true); });
    }
    col.appendChild(head);

    if (key === "queued") {
      const note = el("div", "queued-note" + (dsp.enabled ? "" : " off"),
        dsp.enabled
          ? `Otto runs these with skip-permissions · max ${dsp.max_concurrent} at once`
          : "Auto-dispatch is OFF · nothing here will run");
      col.appendChild(note);
    }

    /* What the column is NOT showing, always stated. A folded Done still accepts a
     * drop (the handlers below are on the column, not the cards), so folding costs
     * nothing but the pixels. */
    const older = column.hidden
      ? `${column.hidden} finished more than ${column.hidden_after_days}d ago`
      : null;
    if (folded) {
      const n = cards.length;
      col.appendChild(el("div", "col-folded",
        (n ? `${n} finished in the last ${column.hidden_after_days}d`
           : "nothing finished lately")
        + (older ? ` · ${older}` : "")));
    } else {
      appendPaged(col, cards, "board:" + key, taskCard);
      if (!cards.length && !older) col.appendChild(el("div", "col-empty", "—"));
      if (older) col.appendChild(el("div", "col-empty", older + ", off the board"));
    }

    col.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      if (overCol !== key) { overCol = key; col.classList.add("over"); }
    });
    col.addEventListener("dragleave", () => {
      if (overCol === key) { overCol = null; col.classList.remove("over"); }
    });
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("over");
      const id = dragId;
      dragId = null; overCol = null;
      if (!id) return;
      /* Dragging one of a selection drags the selection. */
      const ids = selected.has(id) ? [...selected] : [id];
      await moveCards(ids, key);
    });
    cols.appendChild(col);
  }
  wrap.appendChild(cols);
  if (selected.size) wrap.appendChild(selectionBar());
  return wrap;
}

/* The bar under the columns while cards are held. Every destination the board has,
 * except queued: a batch that could start twenty unattended sessions in one click is
 * the wrong shape, and the daemon refuses it too. Fade is here because it is the
 * verb this bar exists for. */
function selectionBar() {
  const bar = el("div", "sel-bar");
  bar.appendChild(el("span", "sel-n", `${selected.size} selected`));
  bar.appendChild(el("span", "sel-hint", "move to"));
  const keys = [
    ["backlog", "Backlog", "b"], ["needs-you", "Needs you", "n"], ["blocked", "Blocked", "x"],
    ["done", "Done", "d"], ["faded", "Fade", "f"],
  ];
  for (const [key, label, hot] of keys) {
    const b = el("button", "sel-btn"); b.type = "button";
    b.appendChild(el("span", null, label));
    const kb = el("kbd", null, hot); b.appendChild(kb);
    b.title = `${label} · press ${hot}`;
    b.addEventListener("click", () => moveCards([...selected], key));
    bar.appendChild(b);
  }
  bar.appendChild(el("span", "spacer"));
  const all = el("button", "sel-btn quiet"); all.type = "button";
  all.appendChild(el("span", null, "select all shown"));
  all.addEventListener("click", () => { for (const k of visibleCards()) selected.add(k.id); render(true); });
  bar.appendChild(all);
  const clr = el("button", "sel-btn quiet"); clr.type = "button";
  clr.appendChild(el("span", null, "clear"));
  clr.appendChild(el("kbd", null, "esc"));
  clr.addEventListener("click", clearCardSelection);
  bar.appendChild(clr);
  bar.appendChild(el("span", "sel-hint", "ctrl-click adds · shift-click ranges · drag one moves all"));
  return bar;
}

/* Move one card to one column. The single write both ways of moving a card go
 * through, so drag and right-click can never disagree about what a move IS. */
async function moveCard(id, key) { return moveCards([id], key); }

/* Apply a move to the local state so the board repaints NOW. The PATCH was never
 * the slow part (30ms); waiting on the 2 MB /api/state refresh after it was the
 * 2-5 seconds. The next poll reconciles; on error the poll is forced and truth wins. */
function applyLocalMove(ids, key) {
  const b = state && state.board;
  if (!b) return;
  const want = new Set(ids);
  const moved = [];
  for (const col of b.columns || []) {
    const keep = [], out = [];
    for (const k of col.cards || []) (want.has(k.id) ? out : keep).push(k);
    if (out.length) { col.cards = keep; col.count = keep.length; moved.push(...out); }
  }
  const target = (b.columns || []).find((c) => c.key === key);
  for (const k of moved) k.status = key;
  if (target) { target.cards = [...moved, ...(target.cards || [])]; target.count = target.cards.length; }
  else if (key === "faded") b.faded = (b.faded || 0) + moved.length;
  for (const t of state.tasks || []) if (want.has(t.id)) t.status = key;
}
function pollSoon(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms); }

async function moveCards(ids, key) {
  ids = [...new Set(ids)];
  if (!ids.length) return;
  if (key === "queued" && ids.length > 1) {
    toast("queued is one card at a time, through otto triage promote", true);
    return;
  }
  applyLocalMove(ids, key);
  for (const id of ids) selected.delete(id);
  render(true);
  try {
    if (ids.length === 1) {
      await act(`otto task mv ${ids[0].slice(0, 6)} ${key}`, `/api/tasks/${ids[0]}`, {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ status: key }),
      });
    } else {
      await act(`otto task mv ${ids.map((i) => i.slice(0, 6)).join(" ")} ${key}`, "/api/tasks/batch", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids, patch: { status: key } }),
      });
    }
    toast(ids.length === 1 ? `moved to ${key}` : `moved ${ids.length} to ${key}`);
    pollSoon(600);
  } catch (err) {
    toast(err.message, true);
    poll();
  }
}

function closeCardMenu() {
  if (!cardMenu) return;
  cardMenu.remove();
  cardMenu = null;
}

/* Right-click a card to move it. Dragging is fine on a short board and stops being
 * possible on a long one: once a column runs past the viewport, the target column's
 * drop zone and the card you are holding are not on screen at the same time, and no
 * amount of auto-scroll makes that a pleasant thing to aim at. This needs no pointer
 * travel and works identically at any board length.
 *
 * Rows come from boardColumns(), never a literal list, so this cannot drift out of
 * step with board.COLUMNS the way a second copy of the names would. */
function openCardMenu(k, x, y) {
  closeCardMenu();
  const menu = el("div", "cardmenu");
  menu.appendChild(el("div", "cardmenu-head", k.title));

  if (k.movable !== false) {
    for (const column of boardColumns()) {
      const key = column.key;
      const here = key === k.status;
      const row = el("button", "cardmenu-row" + (here ? " on" : ""));
      row.type = "button";
      row.disabled = here;
      row.appendChild(ico(here ? "ph ph-dot-outline" : "ph ph-arrow-right"));
      row.appendChild(el("span", "lbl", column.label || key));
      if (here) row.appendChild(el("span", "cardmenu-note", "here now"));
      row.addEventListener("click", () => { closeCardMenu(); moveCard(k.id, key); });
      menu.appendChild(row);
    }
    /* The conversational verb, after the moves. Same sheet the due toast opens. */
    const talk = el("button", "cardmenu-row");
    talk.type = "button";
    talk.appendChild(ico("ph ph-chat-teardrop-text"));
    talk.appendChild(el("span", "lbl", "Tell Otto…"));
    talk.appendChild(el("span", "cardmenu-note", "update it, or give context"));
    talk.addEventListener("click", () => { closeCardMenu(); openReply(k); });
    menu.appendChild(talk);
  } else if (k.kind === "run" && k.run_id) {
    /* A failed run has no stored row to move, so the board's only verb for it is
     * acknowledge. Same call the inspector makes. */
    const row = el("button", "cardmenu-row");
    row.type = "button";
    row.appendChild(ico("ph ph-check"));
    row.appendChild(el("span", "lbl", "Acknowledge"));
    row.addEventListener("click", async () => {
      closeCardMenu();
      try {
        await act(`otto ack ${k.run_id.slice(0, 6)}`, `/api/runs/${k.run_id}/ack`, { method: "POST" });
        toast("acknowledged · card cleared");
        await poll();
      } catch (e) { toast(e.message, true); }
    });
    menu.appendChild(row);
  } else {
    /* Say why there is nothing to offer rather than showing an empty box. A derived
     * card clears itself when the condition clears; that IS the move. */
    menu.appendChild(el("div", "cardmenu-dead",
      "Computed card · fix the condition and it clears itself"));
  }

  document.body.appendChild(menu);
  cardMenu = menu;

  /* Position after measuring, so a card near the right or bottom edge flips instead
   * of opening off screen. */
  const r = menu.getBoundingClientRect();
  const pad = 8;
  const left = x + r.width + pad > window.innerWidth ? Math.max(pad, x - r.width) : x;
  const top = y + r.height + pad > window.innerHeight
    ? Math.max(pad, window.innerHeight - r.height - pad) : y;
  menu.style.left = left + "px";
  menu.style.top = top + "px";
}

/* Toggle one card, or with shift, the range from the last pick within the same
 * column. Range is by DOM order so it matches what the eye sees after filters. */
function pickCard(k, cardEl, range) {
  if (range && lastPick) {
    const col = cardEl.closest(".board-col");
    const nodes = col ? [...col.querySelectorAll(".tcard[data-id]")] : [];
    const ids = nodes.map((n) => n.dataset.id);
    const a = ids.indexOf(lastPick), b = ids.indexOf(k.id);
    if (a >= 0 && b >= 0) {
      for (const id of ids.slice(Math.min(a, b), Math.max(a, b) + 1)) selected.add(id);
      lastPick = k.id;
      render(true);
      return;
    }
  }
  toggleSelected(k.id);
  lastPick = selected.has(k.id) ? k.id : null;
  render(true);
}

function taskCard(k) {
  const movable = k.movable !== false;
  const focused = personMatch(k.title + " " + (k.detail || "") + " " + (k.tags || []).join(" "));
  const isSel = selected.has(k.id);
  const card = el("article", "tcard"
    + (movable ? "" : " derived")
    + (focused ? "" : " dimmed")
    + (isSel ? " selected" : "")
    + (dragId === k.id ? " dragging" : ""));
  card.dataset.id = k.id;

  if (movable) {
    const cb = el("input", "tcard-check"); cb.type = "checkbox"; cb.checked = isSel;
    cb.title = "Select for a batch move (ctrl-click the card works too)";
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      pickCard(k, card, e.shiftKey);
    });
    card.appendChild(cb);
  }

  const tags = el("div", "tcard-tags");
  if (k.priority && k.priority !== "normal") {
    const [fg, bg] = stColor(k.priority);
    const p = el("span", "chip-sm", k.priority);
    p.style.color = fg; p.style.background = bg;
    tags.appendChild(p);
  }
  if (k.task_ref) tags.appendChild(el("span", "chip-ref", k.task_ref));
  if (k.origin) tags.appendChild(el("span", "chip-mono", k.origin));
  if (k.seen_count > 1) tags.appendChild(el("span", "chip-sm chip-seen", `seen ${k.seen_count}x`));
  /* A due date the card carried but never showed. Rendered as pressure, not as a
   * date: "OVERDUE 3d" is a different message from "2026-08-01", and the second one
   * needs you to do the arithmetic before it means anything. */
  const d = dueLabel(k.due);
  if (d) {
    const [fg, bg] = stColor(d.key);
    const c = el("span", "chip-sm", d.text);
    c.style.color = fg; c.style.background = bg;
    tags.appendChild(c);
  }
  tags.appendChild(el("span", "spacer"));
  if (k.domain === "personal") {
    const l = el("span", "chip-sm", "life");
    l.style.border = "1px solid color-mix(in srgb, var(--color-accent) 45%, transparent)";
    l.style.color = ACCENT;
    tags.appendChild(l);
  }
  card.appendChild(tags);

  card.appendChild(el("div", "tcard-title", k.title));
  if (k.detail) card.appendChild(el("p", "tcard-detail", k.detail));

  const foot = el("div", "tcard-foot");
  foot.appendChild(ico(k.kind === "task" ? "ph ph-check-square" : "ph ph-broadcast"));
  foot.appendChild(el("span", null, k.kind || "task"));
  if (k.age) foot.appendChild(el("span", null, "· " + k.age));
  if (createdFilter.preset !== "any" && k.created) {
    foot.appendChild(el("span", "tcard-created", "created " + String(k.created).slice(0, 10)));
  }
  /* No "N more" marker here on purpose. It was written, then measured against the
   * real board: 90% of cards would carry one at any threshold that meant "was this
   * cut", and 37% at a threshold high enough to be rare, so it read as decoration
   * rather than signal on the one view whose whole problem was decoration.
   * `-webkit-line-clamp` already renders an ellipsis at the cut, which says the same
   * thing and costs no chrome. */
  foot.appendChild(el("span", "spacer"));
  if (k.run_id) {
    const lg = el("span", null, "log");
    lg.style.color = ACCENT;
    foot.appendChild(lg);
  }
  card.appendChild(foot);

  if (movable) {
    card.draggable = true;
    card.addEventListener("dragstart", (e) => {
      dragId = k.id; card.classList.add("dragging");
      /* Firefox will not start a drag with an empty dataTransfer. */
      if (e.dataTransfer) { e.dataTransfer.setData("text/plain", k.id); e.dataTransfer.effectAllowed = "move"; }
    });
    card.addEventListener("dragend", () => {
      dragId = null; overCol = null; card.classList.remove("dragging");
      /* A poll landed during the drag and was held back. Let it through now. */
      if (renderDeferred) render(true);
    });
  }
  /* The card opens the action modal, matching the stream. The full detail view is
   * one click further in, from the modal's own "Details" button. Ctrl/Cmd-click or
   * shift-click selects instead, like a file manager. */
  card.addEventListener("click", (e) => {
    if (movable && (e.ctrlKey || e.metaKey || e.shiftKey)) {
      e.preventDefault();
      pickCard(k, card, e.shiftKey);
      return;
    }
    openAction(actionable(k, "card"));
  });
  card.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openCardMenu(k, e.clientX, e.clientY);
  });
  return card;
}

/* ============================== history ============================== */

/* The ledger: every Claude Code session on this machine, priced. Otto's own runs
 * carry Claude Code's cost from the result JSON; everything else is priced from its
 * transcript, or from Claude Code's telemetry once that is on. Fetched on demand
 * and never in /api/state: the daemon re-reads transcripts to build it (memoised a
 * minute), and the numbers do not move faster than that. */
let ledger = null, ledgerAt = 0, ledgerBusy = false;
let ledgerDays = 14, histWho = "all";
try { histWho = localStorage.getItem("otto.hist.who") || "all"; } catch { histWho = "all"; }
try { ledgerDays = Number(localStorage.getItem("otto.hist.days")) || 14; } catch { ledgerDays = 14; }
const ledgerDetail = {};
const LEDGER_MS = 60000;
const WHO_LABEL = { yours: "You", otto: "Otto", other: "Other" };

function ensureLedger(force) {
  if (ledgerBusy) return;
  if (!force && ledger && !ledger.error && ledger.days === ledgerDays && Date.now() - ledgerAt < LEDGER_MS) return;
  ledgerBusy = true;
  api(`/api/ledger?days=${ledgerDays}`)
    .then((r) => { ledger = r; })
    .catch((e) => { ledger = { error: e.message, days: ledgerDays }; })
    .finally(() => { ledgerAt = Date.now(); ledgerBusy = false; render(); });
}
async function loadLedgerSession(sid) {
  try { ledgerDetail[sid] = await api(`/api/ledger/sessions/${sid}`); }
  catch (e) { ledgerDetail[sid] = { error: e.message }; }
  if (sel && sel.type === "session" && sel.id === sid) renderInspector();
}
function setHistWho(w) {
  histWho = w;
  try { localStorage.setItem("otto.hist.who", w); } catch { /* fine */ }
  echo("otto ledger" + (w === "all" ? "" : " --who " + w), "filtered the ledger");
  render(true);
}
function setLedgerDays(n) {
  ledgerDays = n;
  try { localStorage.setItem("otto.hist.days", String(n)); } catch { /* fine */ }
  echo(`otto ledger --days ${n}`, "changed the window");
  ensureLedger(true);
  render(true);
}

/* Dollars for a ledger, where a session can be $1,400 and a run $0.004. money() is
 * three decimals always, which is right for a run row and wrong for a total. */
function usd(v) {
  if (v == null) return "";
  const n = Number(v);
  if (n >= 1000) return "$" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (n >= 100) return "$" + n.toFixed(0);
  if (n >= 10) return "$" + n.toFixed(1);
  return "$" + n.toFixed(2);
}
function spanFmt(h) {
  if (h == null) return "";
  if (h < 1) return Math.round(h * 60) + " min";
  if (h < 48) return h.toFixed(1) + " h";
  return (h / 24).toFixed(1) + " days";
}
/* Local calendar day. toISOString() is UTC, which put every evening run on the next
 * day's row and made the weekday and the date beside it disagree. */
function localDay(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function sessionTitle(s) { return s.title || s.run_name || s.prompt || "(untitled)"; }
function whoDot(w) { const i = el("span", "who-dot " + w); i.title = WHO_LABEL[w] || w; return i; }
function segCtl(options, current, onPick, label) {
  const box = el("div", "hseg");
  box.setAttribute("role", "group");
  if (label) box.setAttribute("aria-label", label);
  for (const [key, text] of options) {
    const b = el("button", "hseg-b" + (key === current ? " on" : ""), text);
    b.type = "button";
    b.setAttribute("aria-pressed", key === current ? "true" : "false");
    b.addEventListener("click", () => onPick(key));
    box.appendChild(b);
  }
  return box;
}

/* Two-series stacked bars: your sessions and Otto's runs, one column per day. Bars,
 * not a line, for the same reason sparkBars gives: a day's spend is a discrete event. */
function stackBars(series) {
  const max = Math.max(...series.map((s) => s.yours + s.otto), 0.001);
  const box = el("div", "spark stack");
  for (const s of series) {
    const col = el("span", "spark-col");
    const total = s.yours + s.otto;
    if (total === 0) {
      col.appendChild(el("span", "seg empty"));
    } else {
      const o = el("span", "seg otto"); o.style.height = Math.max(0, Math.round((s.otto / max) * 100)) + "%";
      const y = el("span", "seg yours"); y.style.height = Math.max(s.yours > 0 ? 2 : 0, Math.round((s.yours / max) * 100)) + "%";
      col.appendChild(o); col.appendChild(y);
    }
    col.title = `${s.label}: ${usd(s.yours)} yours · ${usd(s.otto)} Otto`;
    box.appendChild(col);
  }
  return box;
}

function viewHistory() {
  const s = state;
  ensureLedger(false);
  const L = ledger && !ledger.error ? ledger : null;
  const wrap = el("div", "hist");

  /* -- controls -- */
  const bar = el("div", "hist-bar");
  bar.appendChild(segCtl([["all", "All"], ["yours", "Your sessions"], ["otto", "Otto's runs"]], histWho, setHistWho, "Whose spend"));
  bar.appendChild(segCtl([[7, "7d"], [14, "14d"], [30, "30d"]], ledgerDays, setLedgerDays, "Window"));
  bar.appendChild(el("span", "spacer"));
  if (L) {
    const t = L.totals;
    const sum = el("span", "hist-tot");
    sum.appendChild(el("b", "tnum", usd(t.usd)));
    sum.appendChild(el("span", null, ` in ${L.days}d  ·  `));
    sum.appendChild(whoDot("yours")); sum.appendChild(el("span", "tnum", usd(t.by_who.yours)));
    sum.appendChild(el("span", null, "  "));
    sum.appendChild(whoDot("otto")); sum.appendChild(el("span", "tnum", usd(t.by_who.otto)));
    bar.appendChild(sum);
  }
  wrap.appendChild(bar);

  const intro = el("p", "hist-intro");
  if (L) {
    const tel = L.telemetry, pr = L.pricing;
    if (tel.installed) {
      intro.textContent = `Otto's runs carry Claude Code's own cost. Other sessions are priced from Claude Code's telemetry where it exists (${tel.reported_sessions} so far) and from their transcripts at list rates otherwise (reviewed ${pr.reviewed}).`;
    } else {
      intro.textContent = `Otto's runs carry Claude Code's own cost. Every other session is an estimate from its transcript at list rates, reviewed ${pr.reviewed}, checked against Claude Code's figure at 0.999. `;
      const cmd = el("code", null, "otto telemetry install");
      intro.appendChild(el("span", null, "Turn on telemetry to price them from Claude Code directly: "));
      intro.appendChild(cmd);
    }
    if (pr.stale) {
      const w = el("span", "chip-sm stale-chip", `rates ${pr.age_days}d old`);
      w.title = "The rate card has not been reviewed within its review window. Estimates may be off.";
      intro.appendChild(el("span", null, " ")); intro.appendChild(w);
    }
  } else if (ledger && ledger.error) {
    intro.textContent = "The ledger could not be read: " + ledger.error;
  } else {
    intro.textContent = "Reading transcripts…";
  }
  wrap.appendChild(intro);

  /* -- spend per day, stacked -- */
  if (L && L.daily.length) {
    const byDate = new Map(L.daily.map((d) => [d.date, d]));
    const series = [];
    const t0 = new Date(); t0.setHours(0, 0, 0, 0);
    for (let i = L.days - 1; i >= 0; i--) {
      const key = localDay(new Date(t0.getTime() - i * DAY_MS));
      const d = byDate.get(key);
      const yours = histWho === "otto" ? 0 : (d ? d.yours : 0);
      const otto = histWho === "yours" ? 0 : (d ? d.otto + d.other : 0);
      series.push({ label: key, yours, otto });
    }
    const sw = el("div", "spark-wrap");
    const sh = el("div", "spark-head");
    sh.appendChild(el("span", null, `spend per day · last ${L.days} days`));
    sh.appendChild(el("span", "spacer"));
    const lg = el("span", "spark-legend");
    if (histWho !== "otto") { lg.appendChild(whoDot("yours")); lg.appendChild(el("span", null, "you")); }
    if (histWho !== "yours") { lg.appendChild(whoDot("otto")); lg.appendChild(el("span", null, "Otto")); }
    sh.appendChild(lg);
    sw.appendChild(sh);
    sw.appendChild(stackBars(series));
    const ax = el("div", "spark-axis");
    ax.appendChild(el("span", null, series[0].label.slice(5)));
    ax.appendChild(el("span", null, "today"));
    sw.appendChild(ax);
    wrap.appendChild(sw);
  }

  /* -- where the dollars go -- */
  if (L) {
    const rows = histWho === "all" ? L.sessions : L.sessions.filter((x) => x.who === histWho);
    const kinds = [["cr", "cache reads"], ["cw1", "cache writes 1h"], ["cw5", "cache writes 5m"], ["out", "output"], ["inp", "input"]];
    const k = {}; let kt = 0;
    for (const x of rows) for (const [key] of kinds) { k[key] = (k[key] || 0) + (x.cost_kind[key] || 0); kt += x.cost_kind[key] || 0; }
    if (kt > 0) {
      const comp = el("div", "comp");
      const strip = el("div", "comp-bar");
      const keys = el("div", "comp-keys");
      kinds.forEach(([key, label], i) => {
        const v = k[key] || 0; if (v <= 0) return;
        const seg = el("span", "comp-seg k" + i);
        seg.style.width = (100 * v / kt).toFixed(2) + "%";
        seg.title = `${label}: ${usd(v)} (${(100 * v / kt).toFixed(0)}%)`;
        strip.appendChild(seg);
        const kk = el("span", "comp-key");
        kk.appendChild(el("i", "k" + i));
        kk.appendChild(el("span", null, label + " "));
        kk.appendChild(el("span", "tnum dim", usd(v)));
        keys.appendChild(kk);
      });
      comp.appendChild(strip); comp.appendChild(keys);
      const rb = L.totals;
      const note = el("p", "comp-note");
      const crPct = Math.round(100 * (k.cr || 0) / kt);
      note.textContent = `${crPct}% is re-reading context already in cache: the running cost of long sessions. `;
      if (histWho !== "otto" && rb.rebuilds_yours) {
        note.appendChild(el("span", null,
          `${rb.rebuilds_yours} times a session resumed after an idle gap past the cache TTL and rewrote its context, ${usd(rb.rebuilds_yours_usd)} in all. A fresh session in the morning is cheaper than yesterday's terminal.`));
      }
      comp.appendChild(note);
      wrap.appendChild(comp);
    }

    /* -- ranked sessions -- */
    const ranked = rows.filter(keep).slice(0, 12);
    if (ranked.length) {
      const sec = el("section", "hist-day");
      const head = el("div", "hist-day-head");
      head.appendChild(el("h2", null, "Most expensive sessions"));
      head.appendChild(el("span", "mono-dim", `${rows.length} in ${L.days}d`));
      head.appendChild(el("span", "sec-rule"));
      sec.appendChild(head);
      const max = ranked[0].usd || 1;
      for (const x of ranked) {
        const row = el("div", "rank-row" + (sel && sel.type === "session" && sel.id === x.sid ? " on" : ""));
        row.appendChild(whoDot(x.who));
        const body = el("div", "rank-body");
        const top = el("div", "top");
        top.appendChild(el("strong", null, sessionTitle(x)));
        if (x.basis !== "reported") { const c = el("span", "chip-sm est-chip", "est."); c.title = "Estimated from the transcript at list rates"; top.appendChild(c); }
        body.appendChild(top);
        const bits = [];
        if (x.cwd) bits.push(pathEl(x.cwd));
        bits.push(el("span", null, `${x.turns.toLocaleString()} turns`));
        if (x.hours != null) bits.push(el("span", null, spanFmt(x.hours)));
        if (x.max_ctx) bits.push(el("span", null, kfmt(x.max_ctx) + " ctx"));
        if (x.rebuilds.count) { const r = el("span", "rebuild-bit", `${x.rebuilds.count} rebuilds ${usd(x.rebuilds.usd)}`); bits.push(r); }
        const sub = el("div", "detail");
        bits.forEach((b, i) => { if (i) sub.appendChild(el("span", null, "  ·  ")); sub.appendChild(b); });
        body.appendChild(sub);
        row.appendChild(body);
        const track = el("div", "rank-track");
        const fill = el("div", "rank-fill " + x.who);
        fill.style.width = Math.max(1, 100 * x.usd / max).toFixed(2) + "%";
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(el("span", "cost tnum", usd(x.usd)));
        row.addEventListener("click", () => select("session", x.sid));
        sec.appendChild(row);
      }
      wrap.appendChild(sec);
    }
  }

  if (histWho === "yours") return wrap;

  /* -- Otto's runs, by day. Unchanged in spirit: one row per run, Claude Code's own
   * cost. Two fixes: days are LOCAL days now, and a day that has unpriced runs says so
   * instead of presenting a confident total that covers a tenth of the work. -- */
  const SICO = {
    ok: ["ph ph-check-circle", OK], running: ["ph ph-circle-notch", ACCENT],
    failed: ["ph ph-x-circle", CRIT], orphaned: ["ph ph-ghost", CRIT],
    killed: ["ph ph-prohibit", CRIT], skipped: ["ph ph-minus-circle", DIM],
  };
  const days = new Map();
  for (const r of (s.runs || []).filter(keep)) {
    if (!r.started) continue;
    const d = new Date(r.started);
    const key = localDay(d);
    if (!days.has(key)) days.set(key, { date: key, at: d, entries: [], spend: 0, unpriced: 0 });
    const day = days.get(key);
    day.entries.push(r);
    if (r.cost_usd != null) day.spend += r.cost_usd; else day.unpriced += 1;
  }
  if (!days.size) {
    wrap.appendChild(emptyState(scope
      ? "No run has been spawned in this directory yet. Launch a schedule or spawn one here."
      : "No runs recorded yet. A schedule launch or a spawned session puts the first row here.",
      scope ? "otto spawn <name> --prompt \"…\"" : "otto launch <name>"));
    return wrap;
  }
  const runsHead = el("div", "hist-day-head runs-head");
  runsHead.appendChild(el("h2", null, "Otto's runs"));
  runsHead.appendChild(el("span", "mono-dim", "newest 80"));
  runsHead.appendChild(el("span", "sec-rule"));
  wrap.appendChild(runsHead);

  const today = localDay(new Date());
  /* Paged like the board: the first PAGE_CARDS rows across the day sections, then
   * a control that rebuilds with the next page. The day grouping makes an in-place
   * append awkward and the list is capped at 80 anyway. */
  let shownRows = 0, hiddenRows = 0;
  for (const day of days.values()) {
    if (shownRows >= histShown) { hiddenRows += day.entries.length; continue; }
    const sec = el("section", "hist-day");
    const head = el("div", "hist-day-head");
    const label = day.date === today ? "Today" : day.at.toLocaleDateString(undefined, { weekday: "long" });
    head.appendChild(el("h2", null, label));
    head.appendChild(el("span", "mono-dim", day.date));
    head.appendChild(el("span", "sec-rule"));
    if (day.entries.some((r) => r.cost_usd != null)) {
      const sp = el("span", "hist-spend", "$" + day.spend.toFixed(3));
      if (day.unpriced) {
        sp.textContent += ` · ${day.unpriced} unpriced`;
        sp.title = `${day.unpriced} of ${day.entries.length} runs reported no cost (running, killed, or a shell command)`;
      }
      head.appendChild(sp);
    }
    sec.appendChild(head);

    for (const r of day.entries) {
      if (shownRows >= histShown) { hiddenRows++; continue; }
      shownRows++;
      const row = el("div", "hist-row");
      row.appendChild(el("span", "at", new Date(r.started).toTimeString().slice(0, 5)));
      const [iName, iFg] = SICO[r.status] || ["ph ph-dot", "var(--color-text)"];
      const i = ico(iName);
      i.style.color = iFg;
      row.appendChild(i);

      const body = el("div");
      body.style.minWidth = "0";
      const top = el("div", "top");
      top.appendChild(el("strong", null, r.name));
      if (r.verdict) top.appendChild(lanes(r.verdict, true));
      else {
        const st = el("span", "mono-dim", r.status);
        st.style.color = stColor(r.status)[0];
        top.appendChild(st);
      }
      body.appendChild(top);
      const detail = el("p", "detail");
      const bits = [];
      if (r.output_tokens) bits.push(kfmt(r.output_tokens) + " out");
      if (r.notes) bits.push(r.notes);
      detail.textContent = bits.join("  ·  ");
      if (r.cwd) {
        if (bits.length) detail.appendChild(el("span", null, "  ·  "));
        const p = pathEl(r.cwd);
        p.style.maxWidth = "320px";
        detail.appendChild(p);
      }
      body.appendChild(detail);
      row.appendChild(body);

      row.appendChild(el("span", "cost tnum",
        r.cost_usd != null ? money(r.cost_usd) : (r.status === "running" ? "—" : "")));
      row.addEventListener("click", () => select("run", r.id));
      sec.appendChild(row);
    }
    wrap.appendChild(sec);
  }
  if (hiddenRows) {
    const more = el("button", "col-more", `Show ${Math.min(PAGE_CARDS, hiddenRows)} more · ${hiddenRows} not rendered`);
    more.type = "button";
    more.addEventListener("click", () => { histShown += PAGE_CARDS; render(true); });
    wrap.appendChild(more);
  }
  return wrap;
}

/* One session's story, in the inspector: what it cost, why, and what filled it. */
function inspectSession(s) {
  const sid = sel.id;
  const d = ledgerDetail[sid];
  const row = ledger && ledger.sessions ? ledger.sessions.find((x) => x.sid === sid) : null;
  const wrap = el("div");
  const head = el("div", "insp-head");
  const title = el("div", "insp-title");
  /* The live row's title outranks the transcript's guess, and clicking it renames:
   * the same PATCH the Today panel uses. */
  const liveRow = sessionRow(sid);
  const h2 = el("h2", null, (liveRow && liveRow.title) || sessionTitle(d && !d.error ? d : (row || {})));
  if (liveRow) {
    h2.title = "click to rename";
    h2.style.cursor = "text";
    h2.onclick = () => renameSession(sid, liveRow.title);
  }
  title.appendChild(h2);
  title.appendChild(el("span", "spacer"));
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  title.appendChild(x);
  head.appendChild(title);

  if (!d || d.error) {
    /* No transcript yet (a pane herdr just opened) or still reading it: the live
     * half still has everything the rail knows, so it is shown either way. */
    head.appendChild(el("p", "insp-sub", !d ? "Reading the transcript…" : d.error));
    wrap.appendChild(head);
    const lv = sessionLiveBlock(sid);
    if (lv) wrap.appendChild(lv);
    return wrap;
  }

  const sub = el("p", "insp-sub");
  sub.appendChild(whoDot(d.who));
  const bits = [WHO_LABEL[d.who] || d.who, `${d.turns.toLocaleString()} turns`];
  if (d.subagent_turns) bits.push(`${d.subagent_turns} in subagents`);
  if (d.hours != null) bits.push(spanFmt(d.hours));
  if (d.first) bits.push(new Date(d.first).toLocaleDateString(undefined, { month: "short", day: "numeric" }));
  sub.appendChild(el("span", null, " " + bits.join("  ·  ") + (d.cwd ? "  ·  " : "")));
  if (d.cwd) sub.appendChild(pathEl(d.cwd));
  head.appendChild(sub);

  const acts = el("div", "insp-acts");
  if (d.run_id) {
    const b = el("button", "btn btn-secondary btn-xs");
    b.type = "button";
    b.appendChild(ico("ph ph-arrow-square-out"));
    b.appendChild(el("span", null, "Open run"));
    b.addEventListener("click", () => select("run", d.run_id));
    acts.appendChild(b);
  }
  const cp = el("button", "btn btn-secondary btn-xs");
  cp.type = "button";
  cp.appendChild(ico("ph ph-terminal"));
  cp.appendChild(el("span", null, "otto ledger --session " + sid.slice(0, 8)));
  cp.addEventListener("click", () => echo("otto ledger --session " + sid.slice(0, 8), "the same story in the terminal"));
  acts.appendChild(cp);
  head.appendChild(acts);
  wrap.appendChild(head);
  const lv = sessionLiveBlock(sid);
  if (lv) wrap.appendChild(lv);

  /* cost */
  const basisText = { reported: "reported by Claude Code", estimate: "estimated from the transcript at list rates", partial: "estimated; telemetry covers part of it" }[d.basis];
  const cost = el("div", "insp-pad");
  cost.appendChild(el("h3", "sys-h3", "Cost"));
  const big = el("div", "led-big");
  big.appendChild(el("span", "tnum v", usd(d.usd)));
  big.appendChild(el("span", "dim", basisText));
  cost.appendChild(big);
  const kv = el("div", "led-kv");
  const kinds = [["cr", "cache reads"], ["cw1", "cache writes (1h)"], ["cw5", "cache writes (5m)"], ["out", "output"], ["inp", "input"]];
  const kt = Object.values(d.cost_kind).reduce((a, b) => a + b, 0) || 1;
  kinds.forEach(([key, label], i) => {
    const v = d.cost_kind[key] || 0; if (v <= 0) return;
    const r = el("div", "led-kv-row");
    r.appendChild(el("span", "k", label));
    const tr = el("span", "led-track"); const f = el("span", "led-fill k" + i); f.style.width = (100 * v / kt).toFixed(1) + "%"; tr.appendChild(f);
    r.appendChild(tr);
    r.appendChild(el("span", "v tnum", usd(v)));
    kv.appendChild(r);
  });
  cost.appendChild(kv);
  const perTurn = el("p", "led-note", `${usd(d.per_turn)} per turn · ` +
    Object.entries(d.models).sort((a, b) => b[1] - a[1]).map(([m, n]) => `${m.replace(/^claude-/, "")} ×${n}`).join(", "));
  cost.appendChild(perTurn);
  wrap.appendChild(cost);

  /* context */
  const ctx = el("div", "insp-pad");
  ctx.appendChild(el("h3", "sys-h3", "Context"));
  ctx.appendChild(el("p", "led-note",
    `First turn re-sent ${kfmt(d.first_ctx || 0)} tokens; the peak re-sent ${kfmt(d.max_ctx)}` +
    (d.peak_at ? ` (${new Date(d.peak_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })})` : "") +
    `. Every turn pays for the whole context again, so cost is context size times the turns that follow.`));
  if (d.rebuilds.length) {
    ctx.appendChild(el("p", "led-note warnish",
      `${d.rebuilds.length} of ${d.gaps} idle gaps outlasted the cache and rewrote the context from scratch: ${usd(d.rebuild_usd)} for resuming instead of starting fresh.`));
    const list = el("div", "led-list");
    for (const r of d.rebuilds.slice(0, 6)) {
      const li = el("div", "led-li");
      li.appendChild(el("span", "mono-dim", new Date(r.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })));
      li.appendChild(el("span", null, `idle ${r.idle_minutes >= 120 ? (r.idle_minutes / 60).toFixed(1) + " h" : r.idle_minutes + " min"}`));
      li.appendChild(el("span", "dim", `wrote ${kfmt(r.tokens)}`));
      li.appendChild(el("span", "tnum", usd(r.usd)));
      list.appendChild(li);
    }
    ctx.appendChild(list);
  }
  wrap.appendChild(ctx);

  /* timeline */
  if (d.timeline && d.timeline.length > 1) {
    const tl = el("div", "insp-pad");
    tl.appendChild(el("h3", "sys-h3", "Timeline"));
    const max = Math.max(...d.timeline.map((c) => c.usd), 0.001);
    const box = el("div", "spark tl");
    for (const c of d.timeline) {
      const b = el("span", "spark-bar");
      b.style.height = Math.max(3, Math.round(100 * c.usd / max)) + "%";
      b.style.background = "var(--color-accent)";
      b.title = `${c.at ? new Date(c.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""} · ${c.turns} turns · ${usd(c.usd)} · ${kfmt(c.peak_ctx)} ctx`;
      box.appendChild(b);
    }
    tl.appendChild(box);
    const ax = el("div", "spark-axis");
    ax.appendChild(el("span", null, d.first ? new Date(d.first).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""));
    ax.appendChild(el("span", null, d.last ? new Date(d.last).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : ""));
    tl.appendChild(ax);
    wrap.appendChild(tl);
  }

  /* tools */
  if (d.tools && d.tools.length) {
    const tools = el("div", "insp-pad");
    tools.appendChild(el("h3", "sys-h3", "What filled the context"));
    tools.appendChild(el("p", "led-note", "Injected is what a tool's output added. Amplified multiplies that by how many later turns re-sent it: the number that shows up as cache reads."));
    const tbl = el("table", "table led-table");
    const th = el("thead"); const thr = el("tr");
    for (const h of ["tool", "calls", "injected", "amplified", ""]) thr.appendChild(el("th", null, h));
    th.appendChild(thr); tbl.appendChild(th);
    const tb = el("tbody");
    for (const t of d.tools.slice(0, 8)) {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", t.tool));
      tr.appendChild(el("td", "tnum", String(t.calls)));
      tr.appendChild(el("td", "tnum", kfmt(t.injected)));
      tr.appendChild(el("td", "tnum", kfmt(t.amplified)));
      const sh = el("td"); const trk = el("span", "led-track"); const f = el("span", "led-fill k0"); f.style.width = t.share + "%"; trk.appendChild(f); sh.appendChild(trk);
      tr.appendChild(sh);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    tools.appendChild(tbl);
    if (d.details && d.details.length) {
      const dl = el("div", "led-list");
      for (const x of d.details.slice(0, 6)) {
        const li = el("div", "led-li");
        li.appendChild(el("span", "tnum", kfmt(x.amplified)));
        li.appendChild(el("span", "dim tnum", x.calls + "×"));
        const w = el("span", "mono ellip", x.what); w.title = x.what;
        li.appendChild(w);
        dl.appendChild(li);
      }
      tools.appendChild(dl);
    }
    wrap.appendChild(tools);
  }
  return wrap;
}

/* ============================== ask ============================== */

/* ============================== control plane ============================== */

function viewPlane() {
  const s = state;
  const wrap = el("div", "plane");

  const tabs = el("div", "plane-tabs");
  for (const [key, label] of [["schedules", "Schedules"], ["registry", "Registry"],
                              ["people", "People"], ["system", "System"]]) {
    const b = el("button", planeTab === key ? "on" : null, label);
    b.type = "button";
    b.addEventListener("click", () => {
      planeTab = key;
      if (key === "registry" && !registryRows) loadRegistry();
      if (key === "people" && !peopleRows) loadPeople();
      render();
    });
    tabs.appendChild(b);
  }
  wrap.appendChild(tabs);

  if (planeTab === "schedules") {
    const grid = el("div", "sched-grid");
    for (const sc of (s.schedules || []).filter(keep)) {
      const [cls, label, detail] = schedState(sc);
      const [fg, bg] = stColor(cls);
      /* A known failure is not a crit card. The reason is right there on the chip. */
      const card = el("article", "sched-card" + (cls === "stale" && !sc.known_reason ? " crit" : ""));
      const top = el("div", "sched-top");
      const p = el("span", "chip-sm", label);
      p.style.color = fg; p.style.background = bg;
      top.appendChild(p);
      top.appendChild(el("strong", null, sc.name));
      if (sc.known_reason) top.appendChild(knownChip(sc.known_reason));
      top.appendChild(el("span", "spacer"));
      if (sc.autostart) {
        const a = el("span", "armed-flag");
        a.appendChild(ico("ph ph-lightning"));
        a.appendChild(el("span", null, "armed"));
        top.appendChild(a);
      }
      card.appendChild(top);
      card.appendChild(el("p", "sched-detail", detail || ""));
      const kv = el("div", "sched-kv");
      kv.appendChild(el("span", null, cadenceText(sc)));
      kv.appendChild(ageEl(sc.last_run, null, "last "));
      card.appendChild(kv);

      const acts = el("div", "sched-acts");
      const rb = el("button", "btn btn-primary btn-xs");
      rb.type = "button";
      rb.appendChild(ico("ph ph-play"));
      rb.appendChild(el("span", null, "Run"));
      rb.addEventListener("click", (e) => { e.stopPropagation(); launch(sc.name, rb); });
      acts.appendChild(rb);
      const tb = el("button", "btn btn-secondary btn-xs", sc.enabled ? "Disable" : "Enable");
      tb.type = "button";
      tb.addEventListener("click", async (e) => {
        e.stopPropagation();
        tb.disabled = true;
        try {
          await toggleSchedule(sc);
          toast(`${sc.name} ${sc.enabled ? "disabled" : "enabled"}`);
          await poll();
        } catch (err) { toast(err.message, true); } finally { tb.disabled = false; }
      });
      acts.appendChild(tb);
      card.appendChild(acts);

      card.addEventListener("click", () => select("sched", sc.name));
      grid.appendChild(card);
    }
    wrap.appendChild(grid);
    if (!(s.schedules || []).filter(keep).length) {
      wrap.appendChild(emptyState("No schedules in this view. Add one, and give it a staleness limit so silence becomes an alarm.",
        "otto schedule add <name> --command \"…\" --max-age-hours 26"));
    }

  } else if (planeTab === "registry") {
    if (registryError) {
      wrap.appendChild(loadFailed(registryError, loadRegistry));
      return wrap;
    }
    const bar = el("div", "reg-bar");
    const q = el("input", "input");
    q.type = "search";
    q.placeholder = `Filter ${(registryRows || []).length} definitions by name, repo, or description…`;
    q.value = regQuery;
    q.addEventListener("input", () => {
      regQuery = q.value;
      const host = $("reg-body");
      if (host) fillRegistry(host);
    });
    bar.appendChild(q);
    for (const [kind, n] of Object.entries(s.registry_summary || {})) {
      const c = el("span", "reg-chip" + (kind === "missing" ? " missing" : ""));
      c.appendChild(el("b", null, String(n)));
      c.appendChild(el("span", null, kind));
      bar.appendChild(c);
    }
    wrap.appendChild(bar);

    const table = el("table", "table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["Name", "Kind", "Domain", "Scope", "Description"]) {
      hr.appendChild(el("th", null, h));
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    tbody.id = "reg-body";
    table.appendChild(tbody);
    wrap.appendChild(table);
    fillRegistry(tbody);

  } else if (planeTab === "people") {
    if (peopleError) {
      wrap.appendChild(loadFailed(peopleError, loadPeople));
      return wrap;
    }
    if (peopleRows === null) {
      wrap.appendChild(el("p", "dim", "Loading dossiers…"));
      return wrap;
    }
    const rows = peopleRows;
    const bar = el("div", "reg-bar");
    const q = el("input", "input");
    q.type = "search";
    q.placeholder = `Filter ${rows.length} people by name, title, team, or location…`;
    q.value = peopleQuery;
    q.addEventListener("input", () => {
      peopleQuery = q.value;
      const host = $("people-body");
      if (host) fillPeople(host);
    });
    bar.appendChild(q);
    const noted = rows.filter(r => r.has_notes).length;
    for (const [n, label] of [[rows.length, "dossiers"], [noted, "with notes"],
                              [rows.length - noted, "bare"]]) {
      const c = el("span", "reg-chip" + (label === "bare" && n ? " missing" : ""));
      c.appendChild(el("b", null, String(n)));
      c.appendChild(el("span", null, label));
      bar.appendChild(c);
    }
    wrap.appendChild(bar);

    const table = el("table", "table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["", "Name", "Title", "Team", "Location", "Started"]) {
      hr.appendChild(el("th", null, h));
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    tbody.id = "people-body";
    table.appendChild(tbody);
    wrap.appendChild(table);
    fillPeople(tbody);

  } else {
    const grid = el("div", "sys-grid");
    grid.appendChild(planeBrakes(s));

    const ints = el("div");
    ints.appendChild(el("h3", "sys-h3", "Integrations"));
    for (const i of (s.integrations || [])) {
      let label, cls;
      if (i.ok && i.mode === "mcp-only") { label = "mcp?"; cls = "mcp"; }
      else if (i.ok) { label = "ok"; cls = "ok"; }
      else if (i.known_reason) { label = "known"; cls = "warn"; }
      else { label = "down"; cls = "crit"; }
      const [fg, bg] = stColor(cls);
      const row = el("div", "kv-row int");
      const p = el("span", "state-pill", label);
      p.style.color = fg; p.style.background = bg;
      row.appendChild(p);
      const b = el("div");
      const nm = el("div");
      nm.style.display = "flex"; nm.style.gap = "8px"; nm.style.alignItems = "center"; nm.style.flexWrap = "wrap";
      nm.appendChild(el("strong", null, i.name));
      if (i.known_reason) nm.appendChild(knownChip(i.known_reason));
      /* Mark or clear a known failure on the row itself, since an integration has no
       * inspector of its own. */
      const kb = el("button", "cmd-btn", i.known_reason
        ? `otto known rm integration ${i.name}` : `otto known add integration ${i.name}`);
      kb.type = "button";
      kb.title = i.known_reason ? "Clear the known-failure annotation" : "Annotate this as a known failure, with a reason";
      kb.addEventListener("click", () => i.known_reason
        ? clearKnown("integration", i.name) : markKnown("integration", i.name));
      if (!i.ok || i.known_reason) nm.appendChild(kb);
      b.appendChild(nm);
      b.appendChild(el("p", "sub", i.detail || ""));
      row.appendChild(b);
      ints.appendChild(row);
    }
    grid.appendChild(ints);

    const cfg = el("div");
    cfg.appendChild(el("h3", "sys-h3", "Config & host"));
    const cfgRows = [];
    for (const j of ((s.config || {}).junctions || [])) {
      cfgRows.push([j.ok ? "linked" : "broken", j.name + "/",
        `${j.files != null ? j.files + " files · " : ""}${j.target}`]);
    }
    for (const d of ((s.config || {}).deploy || [])) {
      const key = d.state === "DRIFT" ? "drift" : (d.state === "snapshot" ? "snapshot" : "sync");
      cfgRows.push([key, d.name, d.state === "DRIFT"
        ? `newer: ${d.newer} — otto config deploy | adopt`
        : (d.newer || "copied, in sync")]);
    }
    for (const m of (s.machine || [])) {
      cfgRows.push(["host", `${m.label} · ${m.value}`, m.detail || ""]);
    }
    for (const [label, what, where] of cfgRows) {
      const [fg, bg] = label === "host" ? [DIM, DIM_BG] : stColor(label);
      const row = el("div", "kv-row cfg");
      const p = el("span", "state-pill", label);
      p.style.color = fg; p.style.background = bg;
      row.appendChild(p);
      const b = el("div");
      const st = el("strong", null, what);
      st.style.fontSize = "13px";
      b.appendChild(st);
      /* Junction targets are paths; keep the leaf visible. */
      const sub = el("p", "sub");
      if (/[\\/]/.test(where) && where.length > 48) sub.appendChild(pathEl(where));
      else sub.textContent = where;
      b.appendChild(sub);
      row.appendChild(b);
      cfg.appendChild(row);
    }
    /* The first-run checklist, after its rail entry has gone. */
    const again = el("div", "su-again");
    const ab = el("button", "btn btn-secondary btn-xs", "Run setup again");
    ab.type = "button";
    ab.title = "Open the first-run checklist";
    ab.addEventListener("click", () => setMode("setup"));
    again.appendChild(ab);
    again.appendChild(el("span", "sub", "otto setup"));
    cfg.appendChild(again);
    grid.appendChild(cfg);

    /* Events, folded shut by default and saying what is inside. The daemon hands
     * over 60; the fold states that, and the counts by level, so opening it is a
     * decision rather than a dead end. */
    const ev = el("div", "sys-wide");
    const evs = s.events || [];
    const byLv = {};
    for (const e of evs) byLv[e.level] = (byLv[e.level] || 0) + 1;
    const counts = Object.entries(byLv).sort((a, b) => b[1] - a[1]).map(([k, n]) => n + " " + k).join(" · ");
    ev.appendChild(foldHead("Events", `${evs.length} most recent · ${counts || "none"}`, eventsOpen,
      () => { eventsOpen = !eventsOpen; render(true); }));
    if (eventsOpen) {
      for (const e of evs.slice(0, 60)) {
        const row = el("div", "ev-row");
        row.appendChild(el("span", "at", (e.at || "").replace("T", " ").replace("Z", "")));
        const lv = el("span", null, e.level);
        lv.style.color = e.level === "crit" ? CRIT
          : (e.level === "warn" ? WARN : "color-mix(in srgb, var(--color-text) 38%, transparent)");
        row.appendChild(lv);
        row.appendChild(el("span", "src", e.source || ""));
        row.appendChild(el("span", "msg-txt", e.message || ""));
        ev.appendChild(row);
      }
      const more = el("div", "more-row");
      more.appendChild(el("span", null, "older events: "));
      const b = el("button", "cmd-btn", "otto events --limit 200");
      b.type = "button";
      b.addEventListener("click", () => copy("otto events --limit 200"));
      more.appendChild(b);
      ev.appendChild(more);
    }
    grid.appendChild(ev);
    wrap.appendChild(grid);
  }
  return wrap;
}

function knownChip(reason) {
  const c = el("span", "known-chip");
  c.appendChild(ico("ph ph-bookmark-simple"));
  c.appendChild(el("span", null, "known"));
  c.title = "Known failure: " + reason;
  return c;
}

/* Mark and clear known failures. The reason is required, because the reason is the
 * whole record: a bare "known" is a snooze button, and a snooze button on an alarm
 * that then heals is exactly the annotation that ends up lying. */
async function markKnown(kind, name) {
  const reason = prompt(`Why is ${kind} ${name} a known failure? (kept on the record, shown on the chip)`);
  if (!reason || !reason.trim()) return;
  try {
    await act(`otto known add ${kind} ${name} --reason "${reason.trim().slice(0, 60)}"`,
      `/api/known/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {
      method: "PUT", headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason: reason.trim() }),
    });
    toast(`${name} marked known`);
    await poll();
  } catch (e) { toast(e.message, true); }
}
async function clearKnown(kind, name) {
  try {
    await act(`otto known rm ${kind} ${name}`,
      `/api/known/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`${name} is no longer marked known`);
    await poll();
  } catch (e) { toast(e.message, true); }
}
async function toggleSchedule(sc) {
  return act(`otto toggle ${sc.name}${sc.enabled ? " --off" : ""}`,
    `/api/schedules/${encodeURIComponent(sc.name)}/toggle?enabled=${!sc.enabled}`, { method: "POST" });
}

/* People are not domain-scoped: a colleague is not "work" or "personal", so the
 * work/life filter and the repo scope both deliberately do not apply here. */
function fillPeople(tbody) {
  tbody.replaceChildren();
  const q = peopleQuery.toLowerCase();
  const rows = (peopleRows || [])
    .filter((p) => !q || [p.display_name, p.title, p.department, p.org,
                          p.city, p.countryCode, p.login, p.email]
      .some((x) => (x || "").toLowerCase().includes(q)))
    /* Colleagues first, then external contacts, then unset department last. The
     * department sink uses an explicit flag rather than a sentinel string, because
     * "~~".localeCompare("Creative") is NEGATIVE -- collation orders punctuation
     * before letters, so a sentinel floated everyone without a department to the TOP. */
    .sort((a, b) => (a.external ? 1 : 0) - (b.external ? 1 : 0) ||
                    (a.department ? 0 : 1) - (b.department ? 0 : 1) ||
                    (a.department || "").localeCompare(b.department || "") ||
                    (a.display_name || a.slug).localeCompare(b.display_name || b.slug));
  for (const p of rows) {
    const focused = personSel && personSel.slug === p.slug;
    const tr = el("tr", "row-click" + (focused ? " row-focus" : ""));
    const mark = el("td", "people-mark");
    if (p.has_notes) {
      const i = el("i", "ph ph-note");
      i.title = "has notes";
      mark.appendChild(i);
    }
    /* Focus from the row, without opening the dossier. The selection is shell
     * state: Board, Threads and Outreach all pick it up. */
    const fb = el("button", "insp-x");
    fb.type = "button";
    fb.title = focused ? "Stop focusing on " + (p.display_name || p.slug) : "Focus on this person everywhere";
    fb.setAttribute("aria-label", fb.title);
    fb.appendChild(ico(focused ? "ph ph-user-focus" : "ph ph-crosshair-simple"));
    fb.style.fontSize = "13px";
    if (focused) fb.style.color = WARN;
    fb.addEventListener("click", (e) => {
      e.stopPropagation();
      setPerson(focused ? null : p.slug, p.display_name || p.slug);
    });
    mark.appendChild(fb);
    tr.appendChild(mark);
    const nameCell = el("td", null, p.display_name || p.slug);
    if (p.external) {
      const b = el("span", "ext-badge", "ext");
      b.title = "external contact, Slack-sourced (no directory record)";
      nameCell.appendChild(b);
    }
    tr.appendChild(nameCell);
    tr.appendChild(el("td", "dim", p.title || "—"));
    tr.appendChild(el("td", null, p.external ? (p.org || "external") : (p.department || "—")));
    tr.appendChild(el("td", "dim",
      [p.city, p.countryCode].filter(Boolean).join(", ") || p.timezone || "—"));
    tr.appendChild(el("td", "dim sm", p.startDate || p.first_seen || "—"));
    tr.addEventListener("click", () => openPerson(p.slug));
    tbody.appendChild(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "dim");
    td.colSpan = 6;
    td.appendChild(emptyState(peopleQuery
      ? `Nobody matches "${peopleQuery}". Clear the filter, or sync the roster from the directory.`
      : "No dossiers yet. Sync the roster from a directory dump to seed one per person.", "otto people --sync <directory-dump>"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

function fillRegistry(tbody) {
  tbody.replaceChildren();
  const q = regQuery.toLowerCase();
  const rows = (registryRows || [])
    .filter((e) => inDomain(e))
    .filter((e) => !scope || e.repo === scope)
    .filter((e) => !q || [e.name, e.repo, e.description]
      .some((x) => (x || "").toLowerCase().includes(q)));
  const CAP = 400;
  for (const e of rows.slice(0, CAP)) {
    const tr = el("tr", e.missing ? "missing" : null);
    tr.appendChild(el("td", null, e.name));
    tr.appendChild(el("td", "mono", e.kind));
    const d = el("td", "dom", e.domain === "personal" ? "life" : "work");
    d.style.color = e.domain === "personal" ? ACCENT : DIM;
    tr.appendChild(d);
    tr.appendChild(el("td", "mono", e.repo || "global"));
    tr.appendChild(el("td", "desc", e.description || ""));
    tbody.appendChild(tr);
  }
  /* A silent cap reads as "that is all of them". Say what was dropped. */
  if (rows.length > CAP) {
    const tr = el("tr");
    const td = el("td", "desc more-row", `${rows.length - CAP} more not shown · narrow the filter, or: otto registry`);
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "desc");
    td.colSpan = 5;
    td.appendChild(emptyState(regQuery
      ? `Nothing matches "${regQuery}". Definitions added on disk since the last scan will not be here yet.`
      : "No definitions in this view. Rescan the roots to pick up what is on disk.", "otto scan"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

async function loadRegistry() {
  registryError = null;
  try {
    registryRows = await api("/api/registry");
  } catch (e) {
    registryError = e.message;      // same stuck-on-Loading flaw as people had
  }
  if ((mode === "plane" || splitMode === "plane") && planeTab === "registry") render(true);
}

async function loadPeople() {
  peopleError = null;
  try {
    peopleRows = await api("/api/people");
  } catch (e) {
    /* A 404 here has one overwhelmingly likely cause and it is worth naming, because
     * "not found" sends you looking for a missing dossier rather than a stale process. */
    peopleError = /404|not found/i.test(e.message)
      ? "This daemon was started before the dossier API existed. Restart it: "
        + "otto stop, then otto serve."
      : e.message;
  }
  if ((mode === "plane" || splitMode === "plane") && planeTab === "people") render(true);
}

/* Error + retry, shared by the lazy Control Plane panes. */
function loadFailed(msg, retry) {
  const box = el("div", "callout");
  box.appendChild(el("strong", "sm", "Could not load"));
  box.appendChild(el("p", "sm", msg));
  const b = el("button", "btn btn-sm", "Try again");
  b.type = "button";
  b.addEventListener("click", retry);
  box.appendChild(b);
  return box;
}

/* Dossier detail. Rendered as DOM nodes from the raw markdown rather than through
 * innerHTML: the bodies are hand-written by the owner, but a people directory is exactly
 * the file set you do not want to discover an injection sink in later. */
async function openPerson(slug) {
  let d;
  try { d = await api(`/api/people/${encodeURIComponent(slug)}`); }
  catch (e) { toast(e.message, true); return; }

  const shown = d.display_name || d.slug;
  $("person-title").textContent = shown;

  /* Pronouns sit beside the name, not buried in a facts grid. The point of the field
   * is that you read it before you write about someone -- Fran's dossier said "her"
   * for weeks because nothing put it in front of the reader. An unset value renders
   * as they/them and is styled as an assumption, so it reads as "not recorded yet"
   * rather than as a fact. */
  const tags = $("person-tags");
  tags.replaceChildren();
  tags.appendChild(el("span", "chip-sm chip-pro" + (d.pronouns_set ? "" : " assumed"),
                      d.pronouns || "they/them"));
  if (d.external) {
    const b = el("span", "chip-sm ext-chip", "external");
    b.title = "Slack-sourced; no directory record. sync() never touches this file.";
    tags.appendChild(b);
  }
  if (d.full_name && d.full_name !== shown) {
    tags.appendChild(el("span", "chip-sm", d.full_name));
  }
  /* Monikers sit with the name for the same reason pronouns do: they are what you
   * need before you write to someone, not a fact to go digging for. Fran asked for
   * the field after having to tell Otto himself that Fran is Francisco. Anything
   * already shown as the name or the full name is skipped, so the chips are the
   * names you would not otherwise know. */
  const shownNames = new Set([shown, d.full_name || ""].map((x) => x.toLowerCase()));
  for (const alias of d.monikers || []) {
    if (shownNames.has(alias.toLowerCase())) continue;
    const c = el("span", "chip-sm chip-alias", alias);
    c.title = "Also answers to this";
    tags.appendChild(c);
  }
  for (const v of [d.title, d.org, d.department, d.userType,
                   [d.city, d.countryCode].filter(Boolean).join(", ")]) {
    if (v) tags.appendChild(el("span", "chip-sm", v));
  }

  const body = $("person-body");
  body.replaceChildren();

  const facts = el("dl", "person-facts");
  const factRows = d.external
    ? [["email", "Email"], ["org", "Organization"], ["slack_id", "Slack ID"],
       ["timezone", "Timezone"], ["first_seen", "First seen"]]
    : [["login", "Login"], ["manager", "Manager"], ["startDate", "Started"],
       ["timezone", "Timezone"], ["employeeNumber", "Employee #"], ["discordid", "Discord"]];
  for (const [k, label] of factRows) {
    if (!d[k]) continue;
    facts.appendChild(el("dt", null, label));
    facts.appendChild(el("dd", null, String(d[k])));
  }
  if (facts.childElementCount) body.appendChild(facts);

  const meta = d.meta || {};
  if (meta.last_contact || meta.cadence || meta.circle) {
    const rel = el("div", "person-rel");
    for (const [k, label] of [["last_contact", "Last real conversation"],
                              ["cadence", "Reach out"], ["circle", "Circle"]]) {
      if (!meta[k]) continue;
      const item = el("div", "person-rel-i");
      item.appendChild(el("span", "person-rel-l", label));
      item.appendChild(el("strong", null, meta[k]));
      rel.appendChild(item);
    }
    body.appendChild(rel);
  }

  /* Strip the frontmatter and the otto:meta block: both are already rendered above,
   * as chips and as the relationship strip. Comments are guidance for whoever edits
   * the file and must never reach the screen -- they were half the visible content
   * before this rewrite. */
  const md = String(d.markdown || "")
    .replace(/^---[\s\S]*?\n---\n?/, "")
    .replace(/<!--\s*otto:meta[\s\S]*?-->/g, "")
    .replace(/<!--[\s\S]*?-->/g, "");

  /* Group by heading so a section with nothing under it can be dropped entirely
   * rather than rendered as a lonely label. */
  const groups = [];
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const h = line.match(/^#{1,4}\s+(.*)$/);
    if (h) { groups.push({ heading: h[1].trim(), lines: [] }); continue; }
    if (!groups.length) groups.push({ heading: null, lines: [] });
    groups[groups.length - 1].lines.push(line);
  }

  let wrote = 0;
  for (const g of groups) {
    const prose = g.lines.filter(l => !/^_Seeded from/.test(l));
    if (!prose.length) continue;
    if (g.heading) body.appendChild(el("h3", "person-h", g.heading));
    for (const l of prose) {
      body.appendChild(el("p", "person-p", l.replace(/\*\*/g, "").replace(/^_|_$/g, "")));
    }
    wrote++;
  }

  /* Empty sections are hidden, so an untouched dossier would otherwise show nothing
   * at all. Say so, and say what the sections are, so the blank state is a prompt. */
  if (!wrote) {
    const empty = el("div", "person-empty");
    empty.appendChild(el("p", "person-p", "Nothing written down yet."));
    empty.appendChild(el("p", "person-hint",
      "Sections ready in the file: " + sectionNames().join(" · ")));
    body.appendChild(empty);
  }

  const schema = await loadPeopleSchema();
  if (schema) body.appendChild(personEditor(d, schema));

  const prov = String(d.markdown || "").match(/^_Seeded from.*$/m);
  $("person-path").textContent = (prov ? "seeded from memory · " : "") + `${d.slug}.md`;
  const fb = $("person-focus");
  const focused = personSel && personSel.slug === d.slug;
  fb.replaceChildren(ico("ph ph-user-focus"), el("span", null, focused ? "Stop focusing" : "Focus everywhere"));
  fb.onclick = () => { setPerson(focused ? null : d.slug, shown); closePerson(); };
  $("person-overlay").hidden = false;
}

/* Section list and editable-field contract both come from the daemon rather than being
 * duplicated here. The hardcoded copy this replaces had drifted: it still listed seven
 * sections after "What we work on" was added to people.py, so the empty-state message
 * was quietly wrong about what the file contains. */
let peopleSchema = null;
const SECTION_FALLBACK = ["Who they are", "What we work on", "Working style",
                          "Relationship", "Threads", "Devices", "Known issues", "Baselines"];
function sectionNames() {
  return (peopleSchema && peopleSchema.sections) || SECTION_FALLBACK;
}

async function loadPeopleSchema() {
  if (peopleSchema) return peopleSchema;
  try { peopleSchema = await api("/api/people/meta/schema"); }
  catch (e) { toast(e.message, true); }
  return peopleSchema;
}

async function patchPerson(slug, body) {
  const cmd = body.note
    ? `otto people note ${slug} --section "${body.section}" "…"`
    : `PATCH /api/people/${slug} ${JSON.stringify(body.meta || body)}`;
  const d = await act(cmd, `/api/people/${encodeURIComponent(slug)}`, {
    method: "PATCH", headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  /* The roster caches display_name, pronouns and has_notes, so without this the list
   * behind the modal shows the old value until the next poll. */
  if (peopleRows) {
    const i = peopleRows.findIndex((p) => p.slug === slug);
    if (i >= 0) peopleRows[i] = { ...peopleRows[i], ...d, markdown: undefined };
  }
  return d;
}

/* Meta fields commit on change. A note commits on its own button, so a half-typed
 * thought is never written to a colleague's file. */
function personEditor(d, schema) {
  const wrap = el("div", "person-edit");
  wrap.appendChild(el("h3", "person-h", "Edit"));

  const grid = el("div", "person-edit-grid");
  for (const f of schema.fields) {
    const lab = el("label", "person-edit-f");
    lab.appendChild(el("span", "person-rel-l", f.key.replace(/_/g, " ")));
    const inp = el("input", "input sm");
    inp.type = f.key === "last_contact" ? "date" : "text";
    inp.value = (d.meta || {})[f.key] || "";
    inp.title = f.hint;
    if (f.key === "pronouns") {
      inp.placeholder = "they/them if blank";
      inp.setAttribute("list", "pronoun-options");
      inp.autocomplete = "off";
    }
    let last = inp.value;
    inp.addEventListener("change", async () => {
      if (inp.value === last) return;
      const val = inp.value;
      try {
        await patchPerson(d.slug, { meta: { [f.key]: val } });
        last = val;
        toast(`${f.key.replace(/_/g, " ")} ${val ? "set" : "cleared"}`);
        openPerson(d.slug);
        if (mode === "plane" && planeTab === "people") render();
      } catch (e) { toast(e.message, true); inp.value = last; }
    });
    lab.appendChild(inp);
    grid.appendChild(lab);
  }
  wrap.appendChild(grid);

  const dl = el("datalist");
  dl.id = "pronoun-options";
  for (const p of schema.pronoun_suggestions || []) dl.appendChild(el("option", null, p));
  wrap.appendChild(dl);

  const sel = el("select", "input sm");
  for (const s of sectionNames()) {
    const o = el("option", null, s);
    o.value = s;
    sel.appendChild(o);
  }
  /* Set the select's value directly rather than marking an option `selected`. Both work
   * in a browser, but only this is unambiguous -- and the DOM harness cannot reflect
   * option.selected into select.value, so the other form made the test pass a section
   * of "" while looking correct. */
  const DEFAULT_SECTION = "Relationship";        // the section this feature exists for
  if (sectionNames().includes(DEFAULT_SECTION)) sel.value = DEFAULT_SECTION;
  const ta = el("textarea", "input");
  ta.rows = 3;
  const dated = (schema.dated_sections || []).join(", ");
  ta.placeholder = dated
    ? `Add a note. ${dated} get today's date automatically.`
    : "Add a note.";
  const btn = el("button", "btn btn-primary btn-sm", "Add note");
  btn.type = "button";
  btn.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { toast("nothing to add", true); return; }
    btn.disabled = true;
    try {
      await patchPerson(d.slug, { note: text, section: sel.value });
      ta.value = "";
      toast(`note added to ${sel.value}`);
      openPerson(d.slug);
      if (mode === "plane" && planeTab === "people") render();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });
  const row = el("div", "person-note-row");
  row.appendChild(sel);
  row.appendChild(btn);
  const box = el("div", "person-note-box");
  box.appendChild(ta);
  box.appendChild(row);
  wrap.appendChild(box);
  return wrap;
}

function closePerson() { $("person-overlay").hidden = true; }

/* ============================== notice detail ============================== */

/* A notice body is a plain-text list: `- ` for an item, leading whitespace for a
 * detail line under it. Producers already write it that way (nudges.py emits one
 * line per stale thread), so this only has to recover the structure that <p>
 * throws away, never invent any. Anything unrecognised stays a paragraph. */
function noticeLines(body) {
  return String(body || "").split("\n").filter((l) => l.trim());
}

function noticeRows(body) {
  const rows = [];
  for (const raw of noticeLines(body)) {
    const indented = /^\s/.test(raw);
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    if (bullet && !indented) {
      /* Split on the first colon so "Jonathan Criner, open since 2026-07-06 (30d)"
       * reads as the subject and the rest as what happened. Only when the lead-in
       * is short enough to actually be one: a colon inside prose is not a label. */
      const text = bullet[1];
      const at = text.indexOf(": ");
      if (at > 0 && at <= 90) {
        rows.push({ kind: "item", lead: text.slice(0, at), text: text.slice(at + 2) });
      } else {
        rows.push({ kind: "item", lead: null, text });
      }
    } else if (indented) {
      rows.push({ kind: "sub", text: line.replace(/^[-*•]\s+/, "") });
    } else {
      rows.push({ kind: "p", text: line });
    }
  }
  return rows;
}

/* Notices whose body is a summary of live state render that state instead of the
 * frozen string. threads-quiet is the case that forced it: the notice listed 8 of
 * 23 and said "15 more in the same band", which cannot be acted on, and by the time
 * it is read the counts have moved anyway. */
const LIVE_NOTICE = { "threads-quiet": renderThreadsLive };

async function renderThreadsLive(host) {
  host.appendChild(el("p", "nf-loading", "loading threads…"));
  let data;
  try {
    data = await api("/api/threads");
  } catch (e) {
    host.replaceChildren(el("p", "nf-p", "Could not load threads: " + e.message));
    return;
  }
  host.replaceChildren();

  /* Person focus carries into the thread list: the slug is on every row. */
  const allRows = data.rows || [];
  const rows = personSel ? allRows.filter((r) => r.slug === personSel.slug || personMatch(r.who)) : allRows;
  const c = data.counts || {}, b = data.bands || {};
  host.appendChild(el("p", "sec-note",
    `${c.quiet || 0} quiet (${b.stale_days}-${b.max_days} days), `
    + `${c.ancient || 0} over ${b.max_days} days, ${c.fresh || 0} fresh. `
    + "Write a note on any of them and Otto picks what to do with it."));
  if (personSel) {
    const fn = el("span", "focus-note");
    fn.appendChild(ico("ph ph-user-focus"));
    fn.appendChild(el("span", null, `${rows.length} of ${allRows.length} threads are ${personSel.name}'s`));
    host.appendChild(fn);
  }

  /* Quiet first, then ancient, then fresh: the notice is about what went quiet, and
   * the ancient ones are the keep-or-drop pile rather than the reply pile. */
  const ORDER = ["quiet", "ancient", "fresh"];
  const LABEL = {
    quiet: `Quiet · ${b.stale_days}-${b.max_days} days`,
    ancient: `Over ${b.max_days} days · keep or drop`,
    fresh: "Recent · nothing needed",
  };
  for (const band of ORDER) {
    const group = rows.filter((r) => r.band === band);
    if (!group.length) continue;
    const h = el("h3", "nf-band", LABEL[band]);
    h.appendChild(el("span", "nf-band-n", String(group.length)));
    host.appendChild(h);
    for (const r of group) host.appendChild(threadRow(r));
  }
}

function threadRow(r) {
  const item = el("div", "nf-item nf-thread");
  const head = el("div", "nf-thread-head");
  head.appendChild(el("span", "nf-lead", r.who));
  head.appendChild(el("span", "mono-dim", r.days + "d · " + r.when));
  head.appendChild(el("span", "spacer"));
  item.appendChild(head);
  item.appendChild(el("div", "nf-text", r.text));

  for (const n of r.notes || []) {
    const prev = el("div", "nf-note-prev");
    prev.appendChild(el("span", "mono-dim", n.at.slice(0, 10)));
    prev.appendChild(el("span", null, n.note));
    item.appendChild(prev);
  }
  /* What Otto concluded last time, against the thread it concluded it about. A
   * verdict filed anywhere else is one nobody reads next to the thing it judged. */
  for (const run of r.runs || []) {
    const v = el("div", "nf-verdict" + (run.status === "running" ? " live" : ""));
    v.appendChild(pill(run.status, run.status === "ok" ? "done"
      : run.status === "running" ? "running" : run.status));
    v.appendChild(el("span", null, run.result || (run.status === "running"
      ? "Otto is deciding…" : "no report")));
    item.appendChild(v);
  }

  const box = el("div", "nf-note-box");
  const ta = el("textarea", "input nf-note-in");
  ta.rows = 1;
  ta.placeholder = "note this thread… (what is true now, or what to do)";
  const send = el("button", "btn btn-secondary btn-xs", "Send to Otto");
  send.type = "button";
  send.disabled = true;
  ta.addEventListener("input", () => {
    send.disabled = !ta.value.trim();
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  });
  /* Ctrl/Cmd+Enter sends, because the textarea owns plain Enter. Same rule as the
   * action sheet. */
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && ta.value.trim()) submit();
  });
  async function submit() {
    send.disabled = true; ta.disabled = true;
    try {
      await act(`otto thread-note ${r.id.slice(0, 8)} "${ta.value.trim().slice(0, 40).replace(/"/g, "")}…"`, "/api/threads/note", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ thread_id: r.id, note: ta.value.trim() }),
      });
      box.replaceChildren(el("span", "nf-sent", "sent · Otto is deciding"));
      /* Repaint from the server so the new note and the running verdict appear
       * where they belong, rather than being faked client-side. */
      setTimeout(() => { const h = $("notice-full"); if (h) renderThreadsLive(h); }, 1200);
    } catch (e) {
      toast(e.message, true);
      send.disabled = false; ta.disabled = false;
    }
  }
  send.addEventListener("click", submit);
  box.appendChild(ta);
  box.appendChild(send);
  item.appendChild(box);
  return item;
}

function openNotice(n) {
  const tags = $("notice-tags");
  tags.replaceChildren();
  tags.appendChild(pill(n.level, n.level === "info" ? "idle" : n.level));
  tags.appendChild(el("span", "notice-src", n.source));
  if (n.seen_count > 1) {
    tags.appendChild(el("span", "chip-sm chip-seen", `seen ${n.seen_count}x`));
  }
  if (n.domain) tags.appendChild(domTag(n.domain));
  /* First sighting, not the latest. A thing outstanding since 19:14 keeps saying so. */
  tags.appendChild(ageEl(n.at, "mono-dim"));

  $("notice-title").textContent = n.title;

  const body = $("notice-full");
  body.replaceChildren();

  const live = LIVE_NOTICE[n.source];
  if (live) {
    $("notice-overlay").hidden = false;
    live(body);
    noticeFoot(n);
    return;
  }

  for (const r of noticeRows(n.body)) {
    if (r.kind === "item") {
      const item = el("div", "nf-item");
      if (r.lead) item.appendChild(el("div", "nf-lead", r.lead));
      item.appendChild(el("div", "nf-text", r.text));
      body.appendChild(item);
    } else if (r.kind === "sub") {
      body.appendChild(el("div", "nf-sub", r.text));
    } else {
      body.appendChild(el("p", "nf-p", r.text));
    }
  }

  noticeFoot(n);
  $("notice-overlay").hidden = false;
}

/* The same three actions the card offers. Being in the overlay means reading the
 * detail and acting on it do not need two separate clicks in two places. */
function noticeFoot(n) {
  const foot = $("notice-foot");
  foot.replaceChildren();
  if (n.command) {
    const c = el("button", "cmd-btn", n.command);
    c.type = "button";
    c.addEventListener("click", () => copy(n.command));
    foot.appendChild(c);
  }
  if (n.task_id) {
    const card = findCard(n.task_id);
    const rp = el("button", "btn btn-primary btn-xs", card ? "Reply" : "Reply (card gone)");
    rp.type = "button";
    rp.disabled = !card;
    rp.title = card ? "Update the card, or tell Otto what changed" : "That card is no longer on the board";
    rp.addEventListener("click", () => openReply(card));
    foot.appendChild(rp);
  }
  foot.appendChild(el("span", "spacer"));
  if (!n.read_at) {
    const r = el("button", "btn btn-secondary btn-xs", "Got it");
    r.type = "button";
    r.addEventListener("click", async () => {
      r.disabled = true;
      try {
        await act(`POST /api/notices/${n.id.slice(0, 6)}…/read`, "/api/notices/" + n.id + "/read", { method: "POST" });
        closeNotice(); await poll();
      } catch (err) { toast(err.message, true); r.disabled = false; }
    });
    foot.appendChild(r);
  }
  const x = el("button", "btn btn-secondary btn-xs", "Dismiss");
  x.type = "button";
  x.addEventListener("click", async () => {
    try {
      await act(`DELETE /api/notices/${n.id.slice(0, 6)}…`, "/api/notices/" + n.id, { method: "DELETE" });
      closeNotice(); await poll();
    } catch (err) { toast(err.message, true); }
  });
  foot.appendChild(x);
}

function closeNotice() { $("notice-overlay").hidden = true; }

/* ============================== card reply ============================== */
/* Asked 2026-09-03: a card nearing or past its due date should push a toast the owner
 * can ANSWER. This is where the answer is typed. Two lanes on purpose: the quick buttons
 * are the answers that need no interpretation and PATCH the card directly, with no
 * model and no wait; the text box is for everything else and goes to a small
 * /otto-card session that decides what follows and sends a receipt notice. */

function findCard(id) {
  if (!id) return null;
  return allCards().find((k) => k.id === id || k.id.startsWith(id)) || null;
}

function isoPlusDays(base, n) {
  const d = base ? new Date(String(base).slice(0, 10) + "T00:00:00") : new Date();
  if (isNaN(d)) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  /* Push from today when the card is already late: "+1 day" on a card two weeks
   * overdue should mean tomorrow, not thirteen days ago. */
  const from = d < today ? today : d;
  from.setDate(from.getDate() + n);
  return from.toISOString().slice(0, 10);
}

function openReply(card) {
  if (!card) return;
  replyCard = card;
  const tags = $("reply-tags");
  tags.replaceChildren();
  const d = dueLabel(card.due);
  if (d) tags.appendChild(pill(d.text, d.key));
  if (card.status) tags.appendChild(pill(card.status, card.status));
  if (card.priority && card.priority !== "normal") tags.appendChild(pill(card.priority, card.priority));
  tags.appendChild(el("span", "chip-mono", "task " + card.id.slice(0, 6)));
  if (card.domain) tags.appendChild(domTag(card.domain));
  $("reply-title").textContent = card.title;
  $("reply-why").textContent = card.assessed_note || card.detail || `on the board as ${card.status}`;

  const q = $("reply-quick");
  q.replaceChildren();
  const quick = (label, icon, cmd, body, toastMsg) => {
    const b = el("button", "btn btn-secondary btn-sm");
    b.type = "button";
    b.appendChild(ico(icon));
    b.appendChild(el("span", null, label));
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await act(cmd, `/api/tasks/${card.id}`, {
          method: "PATCH", headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        toast(toastMsg);
        closeReply();
        await poll();
      } catch (e) { toast(e.message, true); b.disabled = false; }
    });
    q.appendChild(b);
  };
  const id6 = card.id.slice(0, 6);
  if (card.status !== "done") {
    quick("Done", "ph ph-check", `otto task mv ${id6} done`, { status: "done" }, "marked done");
  }
  const d1 = isoPlusDays(card.due, 1), d7 = isoPlusDays(card.due, 7);
  if (d1) quick("Push a day", "ph ph-arrow-right", `otto task set ${id6} --due ${d1}`, { due: d1 }, `due ${d1}`);
  if (d7) quick("Push a week", "ph ph-arrow-fat-right", `otto task set ${id6} --due ${d7}`, { due: d7 }, `due ${d7}`);
  if (card.due) {
    /* An empty string, not null: TaskPatch drops None, so null would be "no change". */
    quick("Drop the date", "ph ph-calendar-x", `otto task set ${id6} --due ""`, { due: "" }, "due date cleared");
  }
  if (card.status !== "blocked") {
    quick("Blocked", "ph ph-hand", `otto task mv ${id6} blocked`, { status: "blocked" }, "marked blocked");
  }
  if (card.status === "backlog" || card.status === "needs-you") {
    /* Not done, not deleted: out of the columns. `otto task ls --faded` lists them
       and `otto task mv <id> backlog` brings one back. */
    quick("Fade", "ph ph-eye-slash", `otto task mv ${id6} faded`, { status: "faded" }, "faded");
  }

  $("reply-text").value = "";
  $("reply-overlay").hidden = false;
  $("reply-text").focus();
}

function closeReply() {
  replyCard = null;
  $("reply-overlay").hidden = true;
  if (location.hash.startsWith("#reply=")) history.replaceState(null, "", "#" + mode);
}

async function replySend(btn) {
  if (!replyCard) return;
  const text = $("reply-text").value.trim();
  if (!text) { toast("say something first, or use a quick button", true); return; }
  const card = replyCard;
  btn.disabled = true;
  try {
    const r = await act(`otto task reply ${card.id.slice(0, 6)} "…"`, `/api/tasks/${card.id}/reply`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (r && r.run) toast(`Otto is on it · run ${r.run.id.slice(0, 6)} · receipt arrives as a notice`);
    else toast(`saved on the card, but no session started: ${(r && r.error) || "unknown"}`, true);
    closeReply();
    await poll();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

/* The deep link, once there is a board to look the card up in. Called after every
 * successful poll; a no-op when nothing is pending. */
function openPendingReply() {
  if (!pendingReply || !state) return;
  const card = findCard(pendingReply);
  pendingReply = null;
  if (card) openReply(card);
  else toast("that card is no longer on the board", true);
}

/* ============================== inspector ============================== */

function select(type, id) {
  sel = { type, id };
  if (type === "run" && !activityCache[id]) loadActivity(id);
  if (type === "run" && !planCache[id]) loadPlan(id);
  if (type === "session" && !ledgerDetail[id]) loadLedgerSession(id);
  render();
}
function clearSelection() { sel = null; render(); }

async function loadActivity(id) {
  try {
    const a = await api(`/api/runs/${id}/activity`);
    activityCache[id] = a.events || a.timeline || [];
  } catch {
    activityCache[id] = [];
  }
  /* Only the inspector shows a timeline, so repaint only the inspector. Calling
   * render() here was the second of the two renders per poll tick. */
  if (sel && sel.id === id) {
    activitySig = id + ":" + activityCache[id].length;
    renderInspector();
  }
}

/* ============================== writing ============================== */

async function loadWriting() {
  writingLoading = true;
  writingError = null;
  try {
    writingData = await api("/api/writing");
    writingLoadedAt = Date.now();
    writingSig = JSON.stringify([
      (writingData.posts || []).map((p) => [p.id, p.status, p.updated]),
      (writingData.running || []).map((r) => r.id),
    ]);
  } catch (e) {
    writingError = /404|not found/i.test(e.message)
      ? "This daemon was started before the writing API existed. Restart it: otto stop, then otto serve."
      : e.message;
  }
  writingLoading = false;
  if (mode === "writing" || splitMode === "writing") render(true);
}

/* One request against a post, with the button state and the reload handled once. */
async function writingAct(postId, cmd, path, opts) {
  writingBusy = postId;
  render(true);
  try {
    await act(cmd, path, opts);
    await loadWriting();
  } catch (e) {
    toast(e.message, true);
  } finally {
    writingBusy = null;
    render(true);
  }
}

function writingPatch(postId, cmd, body) {
  return writingAct(postId, cmd, "/api/writing/" + postId, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
}

function themeChips(p) {
  const row = el("span", "wr-themes");
  for (const t of p.themes || []) row.appendChild(el("span", "chip-sm wr-theme", t));
  if (p.energy === "low") {
    const q = el("span", "chip-sm wr-quick", "quick");
    q.title = "Writable in about fifteen minutes: the story is simple and already in your head.";
    row.appendChild(q);
  }
  return row;
}

function writingIdeaCard(p) {
  const card = el("article", "notice wr-card wr-idea");
  const head = el("div", "notice-head");
  head.appendChild(themeChips(p));
  head.appendChild(el("span", "spacer"));
  head.appendChild(ageEl(p.created, "mono-dim", p.id + " · "));
  card.appendChild(head);
  card.appendChild(el("h3", null, p.hook));
  if (p.stance || p.pushback || p.question) {
    const arg = el("div", "wr-arg");
    if (p.stance) {
      const r = el("div", "wr-arg-row");
      r.appendChild(el("span", "wr-lbl", "stance"));
      r.appendChild(el("span", null, p.stance));
      arg.appendChild(r);
    }
    if (p.pushback) {
      const r = el("div", "wr-arg-row vs");
      r.appendChild(el("span", "wr-lbl", "pushback"));
      r.appendChild(el("span", null, p.pushback));
      arg.appendChild(r);
    }
    if (p.question) {
      const r = el("div", "wr-arg-row");
      r.appendChild(el("span", "wr-lbl", "leaves them"));
      r.appendChild(el("span", null, p.question));
      arg.appendChild(r);
    }
    card.appendChild(arg);
  }
  card.appendChild(el("p", "wr-angle", p.angle));
  if ((p.evidence || []).length) {
    const ev = el("div", "wr-evidence");
    ev.appendChild(el("span", "wr-lbl", "rests on"));
    for (const e of p.evidence) ev.appendChild(el("span", "wr-ev", "“" + e + "”"));
    card.appendChild(ev);
  }
  if ((p.reveals || []).length) {
    const rv = el("div", "wr-evidence");
    rv.appendChild(el("span", "wr-lbl", "would have to hide"));
    for (const r of p.reveals) rv.appendChild(el("span", "wr-ev warn", r));
    card.appendChild(rv);
  }
  if (p.error) card.appendChild(el("p", "wr-error", "Last draft attempt: " + p.error));

  const acts = el("div", "notice-acts");
  const draft = el("button", "btn btn-primary btn-xs");
  draft.type = "button";
  draft.appendChild(ico("ph ph-pen-nib"));
  draft.appendChild(el("span", null, "Draft it"));
  draft.disabled = writingBusy === p.id;
  draft.addEventListener("click", () => writingAct(p.id, "otto writing draft " + p.id,
    "/api/writing/" + p.id + "/draft", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }));
  acts.appendChild(draft);
  const drop = el("button", "btn btn-secondary btn-xs", "Drop");
  drop.type = "button";
  drop.title = "Not this one. Kept, so it is not proposed again.";
  drop.disabled = writingBusy === p.id;
  drop.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status dropped", { status: "dropped" }));
  acts.appendChild(drop);
  card.appendChild(acts);
  return card;
}

function writingDraftCard(p) {
  const drafting = p.status === "drafting";
  const flags = p.flags || [];
  const card = el("article", "notice wr-card wr-draft" + (flags.length ? " lv-warn" : ""));
  const head = el("div", "notice-head");
  head.appendChild(themeChips(p));
  if (drafting) head.appendChild(pill("drafting", "running"));
  else if (flags.length) {
    const c = el("span", "chip-sm chip-seen", flags.length + " to look at");
    c.title = "The scan found things to decide on before this goes out. Listed below.";
    head.appendChild(c);
  } else {
    const c = el("span", "chip-sm wr-clean", "nothing flagged");
    c.title = "The scan found nothing. Not the same as safe: read it once as a stranger.";
    head.appendChild(c);
  }
  head.appendChild(el("span", "spacer"));
  const words = p.draft ? p.draft.split(/\s+/).filter(Boolean).length : 0;
  head.appendChild(ageEl(p.updated, "mono-dim",
    p.id + (words ? " · " + words + " words" : "") + (p.cost_usd ? " · " + usd(p.cost_usd) : "") + " · "));
  card.appendChild(head);
  card.appendChild(el("h3", null, p.hook));

  if (drafting) {
    card.appendChild(el("p", "sec-empty", "Otto is writing. This card updates itself when the draft lands."));
    return card;
  }

  const editing = writingEditing === p.id;
  if (editing) {
    const ta = el("textarea", "input wr-edit");
    ta.value = writingEdits[p.id] != null ? writingEdits[p.id] : (p.draft || "");
    ta.rows = Math.max(8, (ta.value.match(/\n/g) || []).length + 3);
    ta.addEventListener("input", () => { writingEdits[p.id] = ta.value; });
    card.appendChild(ta);
  } else {
    const body = el("div", "wr-body", p.draft || "");
    if (p.edited) {
      const tag = el("div", "wr-edited");
      tag.appendChild(ico("ph ph-pencil-simple"));
      tag.appendChild(el("span", null, "your edit, not yet sent back"));
      card.appendChild(tag);
    }
    card.appendChild(body);
  }

  if ((p.alt_hooks || []).length) {
    const alts = el("div", "wr-evidence");
    alts.appendChild(el("span", "wr-lbl", "other first lines"));
    for (const h of p.alt_hooks) alts.appendChild(el("span", "wr-ev", h));
    card.appendChild(alts);
  }

  if (flags.length) {
    const fl = el("div", "wr-flags");
    fl.appendChild(el("strong", "sm", "Before it goes out"));
    for (const f of flags) {
      const row = el("div", "wr-flag");
      row.appendChild(el("span", "wr-flag-kind", f.kind));
      row.appendChild(el("code", null, f.match));
      row.appendChild(el("span", "wr-flag-why", f.why));
      fl.appendChild(row);
    }
    card.appendChild(fl);
  }
  if ((p.notes || []).length) {
    const ns = el("div", "wr-evidence");
    ns.appendChild(el("span", "wr-lbl", "you asked for"));
    for (const n of p.notes) ns.appendChild(el("span", "wr-ev", n.text));
    card.appendChild(ns);
  }
  if (p.error) card.appendChild(el("p", "wr-error", "Last redraft attempt: " + p.error));

  const busy = writingBusy === p.id;
  const acts = el("div", "notice-acts");

  const cp = el("button", "btn btn-primary btn-xs");
  cp.type = "button";
  cp.appendChild(ico("ph ph-copy"));
  cp.appendChild(el("span", null, "Copy"));
  cp.addEventListener("click", () => { copy(p.draft || ""); echo("otto writing show " + p.id, "draft copied"); });
  acts.appendChild(cp);

  if (editing) {
    const edited = () => (writingEdits[p.id] != null ? writingEdits[p.id] : (p.draft || "")).trim();
    const save = el("button", "btn btn-primary btn-xs", "Save my edit");
    save.type = "button";
    save.disabled = busy;
    save.title = "Stored as your text and re-scanned. The next draft run keeps your changes.";
    save.addEventListener("click", () => {
      const text = edited();
      writingEditing = null;
      if (text === (p.draft || "").trim()) { render(true); return; }
      writingPatch(p.id, "otto writing edit " + p.id, { draft: text }).then(() => { delete writingEdits[p.id]; });
    });
    acts.appendChild(save);
    const both = el("button", "btn btn-secondary btn-xs", "Save and redraft…");
    both.type = "button";
    both.disabled = busy;
    both.title = "Store your edit, then open the note box so Otto takes another pass on YOUR version.";
    both.addEventListener("click", async () => {
      const text = edited();
      writingEditing = null;
      writingOpenNote = p.id;
      if (text !== (p.draft || "").trim()) {
        await writingPatch(p.id, "otto writing edit " + p.id, { draft: text });
        delete writingEdits[p.id];
      } else render(true);
    });
    acts.appendChild(both);
    const cancel = el("button", "btn btn-secondary btn-xs", "Discard");
    cancel.type = "button";
    cancel.addEventListener("click", () => { writingEditing = null; delete writingEdits[p.id]; render(true); });
    acts.appendChild(cancel);
    card.appendChild(acts);
    return card;
  }

  const ed = el("button", "btn btn-secondary btn-xs");
  ed.type = "button";
  ed.appendChild(ico("ph ph-pencil-simple"));
  ed.appendChild(el("span", null, "Edit"));
  ed.disabled = busy;
  ed.addEventListener("click", () => { writingEditing = p.id; writingOpenNote = null; writingUrlFor = null; render(true); });
  acts.appendChild(ed);

  const redo = el("button", "btn btn-secondary btn-xs", writingOpenNote === p.id ? "Cancel" : "Redraft…");
  redo.type = "button";
  redo.disabled = busy;
  redo.addEventListener("click", () => { writingOpenNote = writingOpenNote === p.id ? null : p.id; writingUrlFor = null; render(true); });
  acts.appendChild(redo);

  const posted = el("button", "btn btn-secondary btn-xs", writingUrlFor === p.id ? "Cancel" : "Posted…");
  posted.type = "button";
  posted.disabled = busy;
  posted.title = "You published it. Record the link so the next drafts can match it.";
  posted.addEventListener("click", () => { writingUrlFor = writingUrlFor === p.id ? null : p.id; writingOpenNote = null; render(true); });
  acts.appendChild(posted);

  const drop = el("button", "btn btn-secondary btn-xs", "Drop");
  drop.type = "button";
  drop.disabled = busy;
  drop.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status dropped", { status: "dropped" }));
  acts.appendChild(drop);
  card.appendChild(acts);

  if (writingOpenNote === p.id) {
    const box = el("div", "wr-box");
    const ta = el("textarea", "input");
    ta.rows = 3;
    ta.placeholder = p.edited
      ? "Another pass on YOUR version. \"keep my cuts, tighten the middle\" · \"finish the thought in the last paragraph\" · or leave empty and it just continues where you left it"
      : "What to change, in your words. \"tighter\" · \"less preachy\" · \"lead with the failure\" · \"lose the second paragraph\"";
    ta.value = writingNotes[p.id] || "";
    ta.addEventListener("input", () => { writingNotes[p.id] = ta.value; });
    box.appendChild(ta);
    const go = el("button", "btn btn-primary btn-xs", "Redraft with this note");
    go.type = "button";
    go.disabled = busy;
    go.addEventListener("click", () => {
      const note = (writingNotes[p.id] || "").trim();
      writingOpenNote = null;
      writingAct(p.id, "otto writing draft " + p.id + (note ? " --note \"…\"" : ""),
        "/api/writing/" + p.id + "/draft", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ note: note || null }) })
        .then(() => { delete writingNotes[p.id]; });
    });
    box.appendChild(go);
    card.appendChild(box);
  }
  if (writingUrlFor === p.id) {
    const box = el("div", "wr-box");
    const inp = el("input", "input");
    inp.placeholder = "the post's URL (optional)";
    inp.spellcheck = false;
    box.appendChild(inp);
    const go = el("button", "btn btn-primary btn-xs", "Mark posted");
    go.type = "button";
    go.disabled = busy;
    go.addEventListener("click", () => {
      const url = inp.value.trim();
      writingUrlFor = null;
      writingPatch(p.id, "otto writing set " + p.id + " --status posted" + (url ? " --url …" : ""), { status: "posted", url: url || null });
    });
    box.appendChild(go);
    card.appendChild(box);
  }
  return card;
}

function writingPostedRow(p) {
  const row = el("div", "wr-posted");
  row.appendChild(el("span", "mono-dim", (p.posted_at || p.updated || "").slice(0, 10)));
  row.appendChild(el("span", "wr-posted-hook", p.hook));
  if (p.url) {
    const a = el("a", "cmd-btn", "open");
    a.href = p.url; a.target = "_blank"; a.rel = "noopener";
    row.appendChild(a);
  }
  const show = el("button", "cmd-btn", "otto writing show " + p.id);
  show.type = "button";
  show.addEventListener("click", () => copy("otto writing show " + p.id));
  row.appendChild(show);
  return row;
}

function viewWriting() {
  const wrap = el("div", "today-wrap wr-wrap");
  const W = writingData;

  if (!W && !writingError) {
    if (!writingLoading) loadWriting();
    wrap.appendChild(el("p", "sec-empty", "Reading…"));
    return wrap;
  }
  if (writingError) {
    wrap.appendChild(loadFailed(writingError, loadWriting));
    return wrap;
  }

  const posts = W.posts || [];
  const running = W.running || [];
  const ideasRunning = running.some((r) => r.kind === "ideas");

  /* -- the lede: what this is, and the one button -- */
  const top = el("div", "wr-top");
  const lede = el("div", "wr-lede");
  lede.appendChild(el("h2", null, "Things worth saying, from the week you actually had"));
  const sched = W.schedule || {};
  const cad = sched.cadence || {};
  const when = cad.kind === "weekly" ? `${(cad.days || []).join(",")} ${cad.at}` : (cad.kind || "unscheduled");
  lede.appendChild(el("p", null,
    "Otto mines your transcripts, decisions and notices for post ideas (" + when
    + (sched.autostart && sched.enabled ? ", on its own" : ", by hand") + "), drafts the one you pick in your "
    + "voice, and flags what a stranger should not learn from it. Nothing is ever marked ready by Otto; "
    + "you post it, you say so."));
  const vp = el("p", "wr-voice");
  vp.appendChild(el("span", "mono-dim", W.voice_set ? "voice: " : "voice (still the seed, edit it): "));
  vp.appendChild(pathEl(W.voice_path));
  lede.appendChild(vp);
  top.appendChild(lede);

  const ctl = el("div", "wr-ctl");
  const mine = el("button", "btn btn-primary btn-sm");
  mine.type = "button";
  mine.appendChild(ico("ph ph-sparkle"));
  mine.appendChild(el("span", null, ideasRunning ? "Mining…" : "Ideas from this week"));
  mine.disabled = ideasRunning || writingBusy === "ideas";
  mine.addEventListener("click", () => writingAct("ideas", "otto writing ideas", "/api/writing/ideas", { method: "POST" }));
  ctl.appendChild(mine);
  if (W.last_ideas) {
    ctl.appendChild(el("span", "mono-dim", "last mined " + age(W.last_ideas.at)
      + (W.last_ideas.cost_usd ? " · " + usd(W.last_ideas.cost_usd) : "")));
  }
  top.appendChild(ctl);
  wrap.appendChild(top);

  /* -- drafts first: they are the thing waiting on you -- */
  const drafts = posts.filter((p) => p.status === "drafted" || p.status === "drafting");
  const ideas = posts.filter((p) => p.status === "idea");
  const done = posts.filter((p) => p.status === "posted");
  const dropped = posts.filter((p) => p.status === "dropped");

  const dsec = el("section", "sec wr-sec");
  dsec.appendChild(secHead("Drafts", drafts.length ? String(drafts.length) : ""));
  if (drafts.length) for (const p of drafts) dsec.appendChild(writingDraftCard(p));
  else dsec.appendChild(el("p", "sec-empty", ideas.length
    ? "Nothing drafted. Pick an idea below and Otto writes the first pass."
    : "Nothing drafted yet."));
  wrap.appendChild(dsec);

  const isec = el("section", "sec wr-sec");
  isec.appendChild(secHead("Ideas", ideas.length ? String(ideas.length) : ""));
  if (ideas.length) for (const p of ideas) isec.appendChild(writingIdeaCard(p));
  else {
    const e = el("p", "sec-empty");
    e.textContent = ideasRunning
      ? "Otto is reading the week. Ideas land here in a minute or two."
      : "No ideas on the list. The weekly run fills this on its own; the button above does it now.";
    isec.appendChild(e);
  }
  wrap.appendChild(isec);

  if (done.length) {
    const psec = el("section", "sec wr-sec");
    psec.appendChild(secHead("Posted", String(done.length)));
    for (const p of done) psec.appendChild(writingPostedRow(p));
    wrap.appendChild(psec);
  }
  if (dropped.length) {
    const more = el("div", "more-row");
    const b = el("button", "cmd-btn", (writingShowDropped ? "hide " : "show ") + dropped.length + " dropped");
    b.type = "button";
    b.addEventListener("click", () => { writingShowDropped = !writingShowDropped; render(true); });
    more.appendChild(b);
    wrap.appendChild(more);
    if (writingShowDropped) {
      const xsec = el("section", "sec wr-sec");
      for (const p of dropped) {
        const row = el("div", "wr-posted dim");
        row.appendChild(el("span", "mono-dim", (p.updated || "").slice(0, 10)));
        row.appendChild(el("span", "wr-posted-hook", p.hook));
        const back = el("button", "cmd-btn", "back to ideas");
        back.type = "button";
        back.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status idea", { status: "idea" }));
        row.appendChild(back);
        xsec.appendChild(row);
      }
      wrap.appendChild(xsec);
    }
  }
  return wrap;
}

/* What the inspector reads for the current selection, so it rebuilds only when
 * that changed. The selected record itself is serialized (a few KB at most); the
 * caches it draws on are cheap identities. */
function inspectorSignature() {
  const s = state || {};
  if (!sel) return "none";
  let item = null;
  if (sel.type === "task") {
    item = [allCards().find((k) => k.id === sel.id), (s.tasks || []).find((t) => t.id === sel.id),
      detailCache[sel.id], s.logistics, s.dispatch, s.sessions];
  } else if (sel.type === "run") {
    item = [(s.live || []).find((x) => x.id === sel.id), (s.runs || []).find((x) => x.id === sel.id),
      activitySig, planCache[sel.id], timelineOpen, eventsOpen, s.events, (s.repos || null)];
  } else if (sel.type === "sched") {
    item = [(s.schedules || []).find((x) => x.name === sel.id), s.known, s.autorun,
      (s.runs || []).filter((r) => r.name === sel.id).map((r) => r.id + r.status)];
  } else if (sel.type === "session") {
    item = [sessionRow(sel.id), s.logistics, ledgerDetail[sel.id], dispatchPickOpen, s.tasks ? s.tasks.length : 0];
  }
  return JSON.stringify([sel, item, personSel && personSel.slug, scope, domain, repoData && repoData.repos && repoData.repos.length]);
}

function renderInspector(force) {
  const host = $("inspector");
  const sig = inspectorSignature();
  if (!force && sig === inspSig) return;
  inspSig = sig;
  host.replaceChildren();
  const s = state || {};

  let body = null;
  if (sel && sel.type === "run") body = inspectRun(s);
  else if (sel && sel.type === "task") body = inspectTask(s);
  else if (sel && sel.type === "sched") body = inspectSched(s);
  else if (sel && sel.type === "session") body = inspectSession(s);

  /* Selection-only. With nothing selected the pane is EMPTY and CSS collapses the
   * whole column, giving Today the full width. The brakes used to live here and were
   * permanent furniture: useful, but settings, and they cost real estate on every
   * screen. They are in Control plane > System now. */
  host.dataset.idle = body ? "0" : "1";
  if (body) host.appendChild(body);
}

/* The brakes, the armed list, and the daemon warning. Settings-shaped content: read
 * once, checked occasionally, never needed while working. Lives in Control plane. */
function planeBrakes(s) {
  const wrap = el("div", "brakes-pane");

  wrap.appendChild(el("h3", "sys-h3", "The brakes"));
  wrap.appendChild(el("p", "insp-lede",
    "Unattended is the dangerous mode, so every guard is consulted before an autorun. "
    + "Nothing here is hidden behind a settings page."));

  const auto = s.autorun || {}, dsp = s.dispatch || {};
  const lookup = { autorun: auto, dispatch: dsp };
  for (const [name, path, why] of BRAKES) {
    const [obj, field] = path.split(".");
    let v = lookup[obj] ? lookup[obj][field] : undefined;
    let fg = "var(--color-text)";
    if (typeof v === "boolean") { fg = v ? OK : CRIT; v = v ? "on" : "OFF"; }
    else if (field.endsWith("usd")) { fg = WARN; v = v == null ? "unset" : "$" + v; }
    else if (field === "max_failures") v = v + " fails";
    else if (field === "min_gap_minutes") v = v + " min";
    if (v == null) v = "—";
    const row = el("div", "brake");
    const left = el("div");
    left.appendChild(el("strong", null, name));
    left.appendChild(el("p", null, why));
    row.appendChild(left);
    const val = el("span", "brake-val", String(v));
    val.style.color = fg;
    row.appendChild(val);
    wrap.appendChild(row);
  }

  /* Weekend policy sits with the brakes because it is the same kind of thing: a
   * standing rule that decides whether something is allowed to run. */
  const wk = el("div", "brake");
  const wl = el("div");
  wl.appendChild(el("strong", null, "Weekends"));
  wl.appendChild(el("p", null,
    "Work schedules never fire Sat/Sun and work notices do not toast. Personal runs "
    + "normally. Staleness excludes weekend hours, so a paused loop is not an alarm. "
    + "crit still reaches you."));
  wk.appendChild(wl);
  const wv = el("span", "brake-val", "life only");
  wv.style.color = ACCENT;
  wk.appendChild(wv);
  wrap.appendChild(wk);

  const armed = auto.armed || [];
  const h = el("h3", "sys-h3", "Armed");
  h.style.marginTop = "var(--space-8)";
  wrap.appendChild(h);
  wrap.appendChild(el("p", "insp-lede",
    "An enabled schedule is never auto-run. Arming is explicit, and this is the whole list."));
  const chips = el("div", "armed-chips");
  for (const a of armed) {
    const c = el("span", "armed-chip");
    c.appendChild(ico("ph ph-lightning"));
    c.appendChild(el("span", null, a.name || a));
    if (a.cadence) c.appendChild(el("span", "cad", cadenceText(a)));
    chips.appendChild(c);
  }
  if (!armed.length) chips.appendChild(el("p", "sec-empty", "Nothing is armed."));
  wrap.appendChild(chips);

  const stop = el("div", "callout");
  stop.style.marginTop = "var(--space-8)";
  stop.appendChild(el("strong", null, "Stopping the daemon"));
  const pp = el("p");
  pp.appendChild(el("span", null, "Use "));
  pp.appendChild(el("code", null, "otto stop"));
  pp.appendChild(el("span", null,
    ". Every spawned agent is a child of the daemon, and a tree kill takes them with "
    + "it, silently, as orphaned."));
  stop.appendChild(pp);
  wrap.appendChild(stop);
  return wrap;
}

/* Repo context when scoped. Moved out of the inspector so the inspector can be
 * purely "what you clicked". */
function repoPanel(s) {
  const r = (repoData ? repoData.repos : []).find((x) => x.key === scope);
  if (!r) return null;
  const box = el("section", "sec repo-panel");
  const hd = el("div", "repo-hd");
  hd.appendChild(ico("ph ph-folder-open"));
  hd.appendChild(el("h2", null, r.name));
  hd.appendChild(domTag(r.domain));
  box.appendChild(hd);
  box.appendChild(el("p", "repo-path", r.path));
  const boardTotal = allCards().filter(keep).length;
  const live = (s.live || []).filter(keep).length;
  const stats = el("div", "repo-stats");
  for (const [k, v, fg] of [
    ["definitions", r.defs, "var(--color-text)"],
    ["open work", boardTotal, boardTotal ? WARN : "var(--color-text)"],
    ["in flight", live, live ? ACCENT : "var(--color-text)"],
    ["last run", r.last, "var(--color-text)"],
  ]) {
    const cell = el("div", "repo-stat");
    const vv = k === "last run" ? el("div", "v") : el("div", "v", String(v));
    if (k === "last run") vv.appendChild(ageEl(v, null));
    vv.style.color = fg;
    cell.appendChild(vv);
    cell.appendChild(el("div", "k", k));
    stats.appendChild(cell);
  }
  box.appendChild(stats);
  return box;
}

function inspectRun(s) {
  const live = (s.live || []).find((x) => x.id === sel.id);
  const rec = live || (s.runs || []).find((x) => x.id === sel.id);
  if (!rec) return null;

  const wrap = el("div");
  const head = el("div", "insp-head");
  const title = el("div", "insp-title");
  if (live) title.appendChild(el("span", "spin"));
  title.appendChild(el("h2", null, rec.name));
  title.appendChild(el("span", "spacer"));
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  title.appendChild(x);
  head.appendChild(title);

  const events = activityCache[rec.id];
  const bits = [];
  if (rec.model) bits.push(rec.model);
  if (events) bits.push(events.filter((e) => e.kind === "tool").length + " tool calls");
  if (rec.subtasks_started) bits.push(`${rec.subtasks_done || 0}/${rec.subtasks_started} sub-tasks`);
  if (rec.output_tokens) bits.push(kfmt(rec.output_tokens) + " out");
  if (rec.cost_usd != null) bits.push(money(rec.cost_usd));
  if (rec.pid) bits.push("pid " + rec.pid);
  if (rec.cwd) {
    const sub = el("p", "insp-sub");
    sub.textContent = bits.join("  ·  ") + (bits.length ? "  ·  " : "");
    sub.appendChild(pathEl(rec.cwd));
    head.appendChild(sub);
  } else {
    head.appendChild(el("p", "insp-sub", bits.join("  ·  ")));
  }

  const acts = el("div", "insp-acts");
  const logB = el("button", "btn btn-secondary btn-xs");
  logB.type = "button";
  logB.appendChild(ico("ph ph-eye"));
  logB.appendChild(el("span", null, "Raw log"));
  logB.title = "otto logs " + rec.id.slice(0, 6);
  logB.addEventListener("click", () => {
    echo("otto logs " + rec.id.slice(0, 6), "opened the captured log");
    window.open(`/api/runs/${rec.id}/log`, "_blank");
  });
  acts.appendChild(logB);
  if (live) {
    const stopB = el("button", "btn btn-danger btn-xs");
    stopB.type = "button";
    stopB.appendChild(ico("ph ph-stop"));
    stopB.appendChild(el("span", null, "Stop"));
    stopB.addEventListener("click", async () => {
      stopB.disabled = true;
      try {
        await act("otto kill " + rec.id.slice(0, 6), `/api/runs/${rec.id}/kill`, { method: "POST" });
        toast("stopped " + rec.name);
        await poll();
      } catch (e) { toast(e.message, true); } finally { stopB.disabled = false; }
    });
    acts.appendChild(stopB);
  }
  if (!live && (rec.status === "failed" || rec.status === "orphaned") && !rec.reviewed_at) {
    const ab = el("button", "btn btn-secondary btn-xs");
    ab.type = "button";
    ab.appendChild(ico("ph ph-check"));
    ab.appendChild(el("span", null, "Acknowledge"));
    ab.addEventListener("click", async () => {
      ab.disabled = true;
      try {
        await act("otto ack " + rec.id.slice(0, 6), `/api/runs/${rec.id}/ack`, { method: "POST" });
        toast("acknowledged");
        await poll();
      } catch (e) { toast(e.message, true); ab.disabled = false; }
    });
    acts.appendChild(ab);
  }
  head.appendChild(acts);
  wrap.appendChild(head);

  /* -- the verdict, in two lanes --
   * Live runs carry it on the /api/live body; finished ones on the run row. The
   * /api/runs/{id} fetch adds the plan. */
  const cached = planCache[rec.id] || {};
  const v = rec.verdict || cached.verdict;
  if (v) wrap.appendChild(verdictBlock(v));

  /* -- the plan --
   * Four phases, with the queued ones hollow. A 40-minute run reads against what
   * is typical for its name, not against nothing. */
  const plan = rec.plan || cached.plan;
  if (plan && plan.phases) {
    wrap.appendChild(phaseRail(plan));
    const br = budgetRow(plan, !!live);
    if (br) wrap.appendChild(br);
  } else if (!planCache[rec.id]) {
    const p = el("p", "insp-lede", "reading the plan…");
    p.style.margin = "var(--space-3) var(--space-6) 0";
    wrap.appendChild(p);
  }

  const tl = el("div", "tl");
  if (events == null) {
    tl.appendChild(el("p", "sec-empty", "reading the run's activity…"));
  } else if (!events.length) {
    /* An orphaned run genuinely has no timeline. Say that rather than showing an
     * empty rail as though the session did nothing. */
    tl.appendChild(el("p", "sec-empty", rec.status === "orphaned"
      ? "No result object was ever written. Otto does not wait on detached children, so an orphaned run has no exit code and no timeline: the outcome is unknown, not failed."
      : "This run wrote no structured activity. The raw log may still have output."));
  } else {
    /* The timeline states what it is showing. Forty of forty needs no fold; forty
     * of two hundred says so, and opens to the rest. */
    const SHOW = 40;
    const tools = events.filter((e) => e.kind === "tool").length;
    const subs = events.filter((e) => e.kind === "subtask").length;
    const counts = [`${events.length} events`, tools ? `${tools} tool calls` : null, subs ? `${subs} sub-tasks` : null]
      .filter(Boolean).join(" · ");
    if (events.length > SHOW) {
      tl.appendChild(foldHead("Timeline",
        (timelineOpen ? "all " : `last ${SHOW} of `) + counts, timelineOpen,
        () => { timelineOpen = !timelineOpen; renderInspector(); }));
    } else {
      tl.appendChild(foldHead("Timeline", counts, true, () => {}));
    }
    const shown = timelineOpen ? events : events.slice(-SHOW);
    for (const e of shown) tl.appendChild(timelineEvent(e));
  }
  wrap.appendChild(tl);
  return wrap;
}

function timelineEvent(e) {
  let badge = "", badgeFg = "color-mix(in srgb, var(--color-text) 40%, transparent)";
  let dotBg = "transparent", dotBd = "color-mix(in srgb, var(--color-text) 25%, transparent)";
  let dotSize = "7px", dotGlow = "none";
  let labelFg = "var(--color-text)";

  if (e.kind === "tool") {
    if (e.ok === true) { badge = "ok"; badgeFg = OK; dotBg = OK; dotBd = OK; }
    else if (e.ok === false) { badge = "err"; badgeFg = CRIT; dotBg = CRIT; dotBd = CRIT; }
    else {
      badge = "…"; badgeFg = ACCENT; dotBg = ACCENT; dotBd = ACCENT;
      dotSize = "9px"; dotGlow = "0 0 10px -1px var(--color-accent)";
    }
  } else if (e.kind === "result") {
    badge = e.ok ? "done" : "fail"; badgeFg = e.ok ? OK : CRIT;
    dotBg = e.ok ? OK : CRIT; dotBd = dotBg;
  } else if (e.kind === "subtask") {
    badge = "sub-task"; badgeFg = ACCENT; dotBd = ACCENT;
    labelFg = "color-mix(in srgb, var(--color-text) 70%, transparent)";
  } else if (e.kind === "init") {
    badge = "start"; dotBd = ACCENT;
  } else {
    labelFg = "color-mix(in srgb, var(--color-text) 68%, transparent)";
    dotSize = "5px";
  }

  const row = el("div", "tl-ev");
  const gut = el("div", "tl-gutter");
  const dot = el("span", "tl-dot");
  dot.style.width = dotSize; dot.style.height = dotSize;
  dot.style.background = dotBg; dot.style.borderColor = dotBd;
  dot.style.boxShadow = dotGlow;
  gut.appendChild(dot);
  gut.appendChild(el("span", "tl-line"));
  row.appendChild(gut);

  const body = el("div", "tl-body");
  const top = el("div", "tl-top");
  const lb = el("strong", "tl-label", e.label || e.kind || "");
  lb.style.color = labelFg;
  top.appendChild(lb);
  if (badge) {
    const b = el("span", "tl-badge", badge);
    b.style.color = badgeFg;
    top.appendChild(b);
  }
  body.appendChild(top);
  if (e.detail) body.appendChild(el("p", "tl-detail", e.detail));
  if (e.result) {
    const r = el("p", "tl-result", e.result);
    r.style.color = e.ok === false
      ? CRIT : "color-mix(in srgb, var(--color-text) 38%, transparent)";
    body.appendChild(r);
  }
  row.appendChild(body);
  return row;
}

function inspectTask(s) {
  const card = allCards().find((k) => k.id === sel.id);
  if (!card) return null;
  const movable = card.movable !== false;
  const [fg, bg] = stColor(card.status);

  const wrap = el("div", "insp-pad");
  const hd = el("div", "insp-hd");
  const left = el("div");
  left.style.flex = "1"; left.style.minWidth = "0";
  const tags = el("div", "tcard-tags");
  const p = el("span", "chip-sm", card.status);
  p.style.color = fg; p.style.background = bg;
  tags.appendChild(p);
  tags.appendChild(el("span", "chip-ref",
    movable ? "stored task" : "derived · " + card.kind));
  left.appendChild(tags);
  left.appendChild(el("h2", null, card.title));
  hd.appendChild(left);
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  hd.appendChild(x);
  wrap.appendChild(hd);

  if (!movable) {
    const c = el("div", "callout warn tight");
    c.appendChild(el("strong", "sm", "Derived from live state"));
    /* A failed run is the one derived card whose condition never clears, so the
       generic "fix it and it goes away" copy was actively misleading: there was
       nothing to fix and no way to dismiss it. */
    c.appendChild(el("span", null, card.kind === "run"
      ? "No stored row, so there is nothing to drag or edit. A past failure never clears on its own — use Acknowledge below to file it away, or open the run first."
      : "No stored row, so there is nothing to drag or edit. Fix the condition and this clears itself on the next poll."));
    wrap.appendChild(c);
  }
  if (card.last_error) {
    const c = el("div", "callout tight");
    c.appendChild(el("strong", "sm", "Last attempt did not succeed"));
    c.appendChild(el("span", "mono", card.last_error));
    wrap.appendChild(c);
  }
  /* The payload truncates long details; the whole text is one GET away and the
   * inspector is the place that wants it. */
  const detailText = fullDetailOf(card);
  if (detailText) wrap.appendChild(el("p", "insp-detail", detailText));
  if (needsFullDetail(card)) {
    wrap.appendChild(el("p", "mono-dim", "loading the full detail…"));
    loadFullDetail(card.id);
  }
  if (card.result) {
    wrap.appendChild(el("h3", "insp-h2", "What the run reported"));
    wrap.appendChild(el("p", "insp-pre", card.result));
  }

  const kv = [
    ["Filed by", card.origin ? card.origin + " (agent)" : (card.source || card.kind)],
    ["Times seen", card.seen_count > 1 ? card.seen_count + "×  — recurrence escalated it" : null],
    ["Reference", card.task_ref],
    ["Agent", movable ? (card.agent || "none (plain session)") : null],
    ["Directory", card.cwd],
    ["Attempts", movable ? `${card.attempts || 0} of ${(s.dispatch || {}).max_attempts || 1}` : null],
    ["Auto-dispatch", movable ? (card.auto ? "yes" : "no — parked") : null],
    ["Age", card.age],
    ["Command", card.command],
  ];
  for (const [k, v] of kv) {
    if (!v) continue;
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(el("span", "v", String(v)));
    wrap.appendChild(row);
  }

  const lgx = taskDispatchBlock(card);
  if (lgx) wrap.appendChild(lgx);

  const acts = el("div", "insp-acts");
  acts.style.marginTop = "var(--space-6)";
  if (movable) {
    const rb = el("button", "btn btn-primary btn-sm");
    rb.type = "button";
    rb.appendChild(ico("ph ph-play"));
    rb.appendChild(el("span", null, card.attempts ? "Run again" : "Run now"));
    rb.addEventListener("click", async () => {
      rb.disabled = true;
      try {
        await act(`otto task run ${card.id.slice(0, 6)}`, `/api/tasks/${card.id}/dispatch`, { method: "POST" });
        toast("dispatched " + card.id.slice(0, 6));
        await poll();
      } catch (e) { toast(e.message, true); } finally { rb.disabled = false; }
    });
    acts.appendChild(rb);
  }
  if (card.command) {
    const cb = el("button", "btn btn-secondary btn-sm", "Copy command");
    cb.type = "button";
    cb.addEventListener("click", () => copy(card.command));
    acts.appendChild(cb);
  }
  if (card.run_id) {
    const lb = el("button", "btn btn-secondary btn-sm", "Open run");
    lb.type = "button";
    lb.addEventListener("click", () => select("run", card.run_id));
    acts.appendChild(lb);
  }
  /* Acknowledge. The board's only verb for a past failure, and the reason it has to
     exist: a derived card cannot be dragged to Done because there is no stored row
     to write, and the "fix the condition and it clears itself" rule does not apply
     to a run that already failed. That condition is history and never clears, so
     without this the card sits in Needs-you forever. */
  if (!movable && card.kind === "run" && card.run_id) {
    const ab = el("button", "btn btn-primary btn-sm");
    ab.type = "button";
    ab.appendChild(ico("ph ph-check"));
    ab.appendChild(el("span", null, "Acknowledge"));
    ab.addEventListener("click", async () => {
      ab.disabled = true;
      try {
        await act(`otto ack ${card.run_id.slice(0, 6)}`, `/api/runs/${card.run_id}/ack`, { method: "POST" });
        toast("acknowledged · card cleared");
        clearSelection();
        await poll();
      } catch (e) { toast(e.message, true); ab.disabled = false; }
    });
    acts.appendChild(ab);
  }
  wrap.appendChild(acts);
  return wrap;
}

function inspectSched(s) {
  const sc = (s.schedules || []).find((x) => x.name === sel.id);
  if (!sc) return null;
  const [cls, label, detail] = schedState(sc);
  const [fg, bg] = stColor(cls);

  const wrap = el("div", "insp-pad");
  const hd = el("div", "insp-hd");
  const left = el("div");
  left.style.flex = "1"; left.style.minWidth = "0";
  const tags = el("div", "tcard-tags");
  const p = el("span", "chip-sm", label);
  p.style.color = fg; p.style.background = bg;
  tags.appendChild(p);
  tags.appendChild(el("span", "chip-ref", cadenceText(sc)));
  if (sc.known_reason) tags.appendChild(knownChip(sc.known_reason));
  left.appendChild(tags);
  left.appendChild(el("h2", null, sc.name));
  hd.appendChild(left);
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  hd.appendChild(x);
  wrap.appendChild(hd);

  wrap.appendChild(el("p", "insp-detail", detail || ""));

  if (sc.known_reason) {
    const c = el("div", "callout warn tight");
    c.appendChild(el("strong", "sm", "Known failure"));
    c.appendChild(el("span", null, sc.known_reason
      + ". Its alert is demoted to info while this stands. When it succeeds on its own, Otto raises one notice telling you to clear it."));
    wrap.appendChild(c);
  }

  const kv = [
    ["Command", sc.command],
    ["Runner", sc.runner],
    ["Armed", sc.autostart ? "yes — runs unattended" : "no"],
    ["Stale after", sc.max_age_hours ? sc.max_age_hours + "h" : "no alarm set"],
    ["Last run", ageEl(sc.last_run, "v mono")],
    ["Last status", sc.last_status],
    ["Failures", sc.consecutive_failures ? String(sc.consecutive_failures) : null],
    ["Domain", sc.domain],
  ];
  for (const [k, v] of kv) {
    if (!v) continue;
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(v instanceof Node ? v : el("span", "v mono", String(v)));
    wrap.appendChild(row);
  }

  wrap.appendChild(el("p", "insp-note",
    "Due and stale are different questions. Cadence says when it should run; the staleness limit says when silence becomes an alarm."));

  const acts = el("div", "insp-acts");
  const rb = el("button", "btn btn-primary btn-sm");
  rb.type = "button";
  rb.appendChild(ico("ph ph-play"));
  rb.appendChild(el("span", null, "Run now"));
  rb.addEventListener("click", () => launch(sc.name, rb));
  acts.appendChild(rb);
  const tb = el("button", "btn btn-secondary btn-sm", sc.enabled ? "Disable" : "Enable");
  tb.type = "button";
  tb.addEventListener("click", async () => {
    tb.disabled = true;
    try {
      await toggleSchedule(sc);
      await poll();
    } catch (e) { toast(e.message, true); } finally { tb.disabled = false; }
  });
  acts.appendChild(tb);
  /* The known-failure verb lives here because this is where the owner reads the alert
   * about to be annotated. Only offered when there is something to annotate. */
  if (sc.known_reason) {
    const kb = el("button", "btn btn-secondary btn-sm", "Clear known");
    kb.type = "button";
    kb.addEventListener("click", () => clearKnown("schedule", sc.name));
    acts.appendChild(kb);
  } else if (cls === "stale" || cls === "warn" || sc.disabled_reason || sc.consecutive_failures) {
    const kb = el("button", "btn btn-secondary btn-sm", "Mark known");
    kb.type = "button";
    kb.title = "Annotate this as a known failure, with a reason. Demotes its alert until it heals.";
    kb.addEventListener("click", () => markKnown("schedule", sc.name));
    acts.appendChild(kb);
  }
  wrap.appendChild(acts);
  return wrap;
}

/* ============================== actions ============================== */

async function launch(name, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await act(`otto launch ${name}`, `/api/schedules/${encodeURIComponent(name)}/run`, { method: "POST" });
    toast(`launched ${name}` + (r && r.id ? ` · run ${r.id.slice(0, 6)}` : ""));
    await poll();
  } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}
async function doRefresh(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "refreshing…"; }
  try {
    const res = await act("otto refresh", "/api/refresh", { method: "POST" });
    const started = (res.runs || []).map((r) => r.domain).join(", ");
    const skipped = res.skipped || [];
    toast(`refreshing ${started || "nothing"}`
      + (skipped.length ? ` · skipped ${skipped.map((x) => x.domain).join(", ")}` : ""));
    await poll();
  } catch (e) { toast(e.message, true); } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Refresh mail + calendar"; }
  }
}
async function doProbe(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "probing…"; }
  try {
    await act("otto probe", "/api/integrations/probe", { method: "POST" });
    toast("probe complete");
    await poll();
  } catch (e) { toast(e.message, true); } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Probe now"; }
  }
}
async function addTask() {
  const title = prompt("Task title");
  if (!title || !title.trim()) return;
  try {
    await act(`otto task add "${title.trim().replace(/"/g, "")}"`, "/api/tasks", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        title: title.trim(),
        domain: domain === "all" ? "work" : domain,
      }),
    });
    toast("task added");
    await poll();
  } catch (e) { toast(e.message, true); }
}

/* ============================== action modal ==============================
 *
 * Click a stream item, add what Otto does not already know, send it to be worked.
 *
 * Two routes out, and the difference matters:
 *
 *   Send to Otto  files/updates a board task as `queued`. Auto-dispatch picks it
 *                 up under every existing brake (max concurrent, per-task budget,
 *                 one attempt) and the card is visible on the board the whole time.
 *   Run now       spawns the session immediately, bypassing all of that.
 *
 * Queue is the default because a dashboard button that silently starts an
 * unattended skip-permissions session is the thing the brakes exist for.
 *
 * A row whose id is `task:<uuid>` ALREADY has a stored task. Actioning it updates
 * that task rather than filing a second one, which is why advisor.next_up puts its
 * dedupe key on the row.
 */

let actionItem = null;   // the normalised item being actioned
let agentList = null;    // registry agents, loaded on first open

/* Stream rows and board cards describe the same work in different shapes, so both
 * are normalised here rather than special-cased at every use. The field that
 * matters is `storedId`: present means a stored task to UPDATE, absent means a
 * derived condition to FILE. Getting that wrong duplicates cards. */
function actionable(src, from) {
  if (from === "card") {
    const derived = src.movable === false;
    return {
      from: "card",
      storedId: derived ? null : src.id,
      title: src.title,
      why: src.detail || `on the board as ${src.status}`,
      command: src.command || null,
      domain: src.domain, kind: src.kind || "task",
      cwd: src.cwd || null, agent: src.agent || null,
      status: src.status, derived,
      attempts: src.attempts || 0,
      lastError: src.last_error || null,
      taskRef: src.task_ref || null,
      band: src.priority === "urgent" || src.priority === "high" ? "today" : null,
    };
  }
  const id = (src && src.id) || "";
  const storedId = id.startsWith("task:") ? id.slice(5) : null;
  /* A stream row that IS a task carries only a summary, so pull the real card for
   * its status, attempts and error rather than re-deriving them from the row. */
  const card = storedId ? allCards().find((k) => k.id === storedId) : null;
  return {
    from: "stream",
    storedId,
    title: src.title,
    why: src.why,
    command: src.command || null,
    domain: src.domain, kind: src.kind,
    cwd: (card && card.cwd) || null, agent: (card && card.agent) || null,
    status: card ? card.status : null,
    derived: false,
    attempts: (card && card.attempts) || 0,
    lastError: (card && card.last_error) || null,
    taskRef: (card && card.task_ref) || null,
    band: src.band,
  };
}

function storedTaskId(item) { return (item && item.storedId) || null; }

/* ── full task detail ──
 * /api/state carries at most 600 chars of a task's detail (`detail_truncated`),
 * which is plenty for a card and not for a prompt or the inspector. The board card
 * and the stored task may each carry the flag, depending on which the daemon
 * truncated, so both are checked. */
function needsFullDetail(k) {
  if (!k || !k.id || detailCache[k.id] != null) return false;
  if (k.detail_truncated) return true;
  const t = ((state || {}).tasks || []).find((x) => x.id === k.id);
  return !!(t && t.detail_truncated);
}
function fullDetailOf(k) {
  return (k && detailCache[k.id] != null ? detailCache[k.id] : (k && k.detail)) || "";
}
async function loadFullDetail(id) {
  if (detailBusy.has(id)) return detailCache[id];
  detailBusy.add(id);
  try {
    const t = await api(`/api/tasks/${id}`);
    detailCache[id] = (t && (t.detail || (t.task && t.task.detail))) || "";
  } catch { /* keep the truncated text; the next open tries again */ }
  finally { detailBusy.delete(id); }
  if (sel && sel.type === "task" && sel.id === id) renderInspector();
  return detailCache[id];
}

function buildPrompt(item, ctx, cwd) {
  const lines = [
    `You are acting on an item Otto surfaced. Do the work; do not just describe it.`,
    ``,
    `ITEM: ${item.title}`,
    `WHY OTTO RAISED IT: ${item.why || "(no reason recorded)"}`,
    `DOMAIN: ${item.domain || "work"}`,
  ];
  if (item.command) lines.push(`OTTO'S SUGGESTED COMMAND: ${item.command}`);
  lines.push(`WORKING DIRECTORY: ${cwd}`);
  if (ctx && ctx.trim()) {
    lines.push(``, `CONTEXT FROM THE OWNER (this is the authoritative extra detail):`, ctx.trim());
  }
  lines.push(
    ``,
    `Finish the whole item or say precisely what blocked you. If the item turns out`,
    `to be already done or not a real problem, say so plainly rather than inventing`,
    `work. Report what you changed.`,
  );
  return lines.join("\n");
}

function defaultCwd(item) {
  if (item && item.cwd) return item.cwd;
  if (scope) {
    const r = (repoData ? repoData.repos : []).find((x) => x.key === scope);
    if (r) return r.path;
  }
  return ((state && state.config) || {}).home || "";
}

async function openAction(item) {
  actionItem = item;
  if (!agentList) {
    try {
      const rows = registryRows || await api("/api/registry");
      registryRows = rows;
      agentList = rows.filter((e) => e.kind === "agent" && !e.missing)
        .map((e) => e.name).sort();
    } catch { agentList = []; }
  }
  /* The "why" becomes the prompt's WHY line. A truncated one would hand the
   * session 600 chars of a 3,000-char brief, so the whole thing is fetched first. */
  if (item.storedId) {
    const k = allCards().find((c) => c.id === item.storedId);
    if (k && needsFullDetail(k)) {
      const d = await loadFullDetail(item.storedId);
      if (actionItem !== item) return;
      if (d) item.why = d;
    }
  }

  const tags = $("action-tags");
  tags.replaceChildren();
  const sev = item.kind === "outage" ? "crit" : (item.band === "today" ? "warn" : "idle");
  tags.appendChild(pill(item.kind || "item", sev));
  tags.appendChild(domTag(item.domain));
  const stored = storedTaskId(item);
  if (stored) tags.appendChild(el("span", "chip-mono", "task " + stored.slice(0, 6)));
  if (item.taskRef) tags.appendChild(el("span", "chip-ref", item.taskRef));
  if (item.status) tags.appendChild(pill(item.status, item.status));
  if (item.derived) tags.appendChild(el("span", "chip-mono", "derived"));

  $("action-title").textContent = item.title;
  $("action-why").textContent = item.why || "Otto recorded no reason for this row.";
  $("action-ctx").value = "";
  /* A failed card's error is the most useful thing to read before writing context,
   * so it goes above the box rather than being buried in the inspector. */
  $("action-error").hidden = !item.lastError;
  if (item.lastError) $("action-error-text").textContent = item.lastError;
  /* Details only means something for a card that exists. */
  $("action-details").hidden = !stored;

  const sel = $("action-agent");
  sel.replaceChildren();
  const none = el("option", null, "none (plain session)");
  none.value = "";
  sel.appendChild(none);
  for (const a of (agentList || [])) {
    const o = el("option", null, a);
    o.value = a;
    sel.appendChild(o);
  }
  /* Suggest an agent from the item's own domain words, never silently: it is
   * pre-selected in a visible dropdown the user can override. */
  /* A card that already names an agent wins over the keyword guess: someone chose
   * it deliberately. */
  const guess = item.agent || suggestAgent(item, agentList || []);
  if (guess) sel.value = guess;

  $("action-cwd").value = defaultCwd(item);

  refreshActionPreview();
  $("action-overlay").hidden = false;
  $("action-ctx").focus();
}

function suggestAgent(item, agents) {
  const hay = ((item.title || "") + " " + (item.why || "")).toLowerCase();
  const rules = [
    [/\bidp\b|mfa|sso|contractor|google workspace|offboard/, "identity-admin"],
    [/\bedr\b|detection|phish|iam|exposed|credential|token|rotate/, "security-auditor"],
    [/\brmm\b|endpoint|bitlocker|gpu|driver|bsod|workstation|laptop/, "endpoint-support"],
    [/aws|ec2|vpc|lambda|cloudtrail|s3|kms/, "infra-ops"],
    [/cost|spend|budget|licen[cs]e|invoice/, "cost-analyst"],
  ];
  for (const [re, name] of rules) {
    if (re.test(hay) && agents.includes(name)) return name;
  }
  return null;
}

function refreshActionPreview() {
  if (!actionItem) return;
  const ctx = $("action-ctx").value;
  const cwd = $("action-cwd").value.trim();
  $("action-prompt").textContent = buildPrompt(actionItem, ctx, cwd || "(unset)");

  const item = actionItem;
  const stored = storedTaskId(item);
  const dsp = (state && state.dispatch) || {};
  $("action-plan-label").textContent = "Prompt the session will receive";
  const note = $("action-note");

  /* What Send will actually do, stated per starting status. A card already `done`
   * gets re-opened; a spent attempt gets reset. Neither should be a surprise. */
  const bits = [];
  if (!stored) {
    bits.push("Send files a new queued task.");
  } else {
    const id6 = stored.slice(0, 6);
    if (item.status === "done") bits.push(`Send RE-OPENS ${id6} and queues it.`);
    else if (item.status === "queued") bits.push(`${id6} is already queued; Send adds your context.`);
    else bits.push(`Send moves ${id6} to queued.`);
    if (item.attempts >= (dsp.max_attempts || 1)) {
      bits.push(`It already used its ${item.attempts} attempt(s), so Send resets the counter `
        + "and clears the old error, otherwise auto-dispatch would skip it forever.");
    }
  }
  bits.push(dsp.enabled
    ? `Auto-dispatch runs it, max ${dsp.max_concurrent} at once`
      + (dsp.budget_usd != null ? `, $${dsp.budget_usd} cap.` : ".")
    : "Auto-dispatch is OFF, so it will sit on the board until you turn it on.");
  bits.push("Run now spawns immediately, skipping every brake.");
  note.className = "action-note" + (dsp.enabled ? "" : " warn");
  note.textContent = bits.join(" ");
}

function closeAction() {
  actionItem = null;
  $("action-overlay").hidden = true;
}

async function actionQueue(btn) {
  if (!actionItem) return;
  const item = actionItem;
  const ctx = $("action-ctx").value.trim();
  const cwd = $("action-cwd").value.trim();
  const agent = $("action-agent").value || null;
  const stored = storedTaskId(item);

  btn.disabled = true;
  try {
    if (stored) {
      /* Append rather than overwrite. The existing detail is why the card exists,
       * and replacing it would destroy the record to add to it. */
      const current = allCards().find((k) => k.id === stored);
      const detail = [(current && current.detail) || item.why, ctx && `--- context added ${new Date().toISOString().slice(0, 16).replace("T", " ")} ---\n${ctx}`]
        .filter(Boolean).join("\n\n");
      const body = { status: "queued", auto: true };
      if (detail) body.detail = detail;
      if (agent) body.agent = agent;
      if (cwd) body.cwd = cwd;
      /* Without this a card that already failed re-queues and then never
       * dispatches, because eligible() skips attempts >= max_attempts. The note
       * above says this is happening. */
      const maxAttempts = ((state && state.dispatch) || {}).max_attempts || 1;
      if ((item.attempts || 0) >= maxAttempts) { body.attempts = 0; body.last_error = ""; }
      await act(`otto task mv ${stored.slice(0, 6)} queued`, `/api/tasks/${stored}`, {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      toast(`queued task ${stored.slice(0, 6)}`);
    } else {
      const detail = [item.why, ctx && `--- context from the owner ---\n${ctx}`]
        .filter(Boolean).join("\n\n");
      const t = await act(`otto task add "${item.title.slice(0, 40).replace(/"/g, "")}" --status queued`, "/api/tasks", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: item.title,
          status: "queued",
          domain: item.domain || "work",
          priority: item.band === "today" ? "high" : "normal",
          detail,
          agent, cwd: cwd || null, auto: true,
          /* Provenance: this came from a stream row, not from the owner typing a task. */
          origin: "stream:" + (item.id || item.kind || "item"),
          tags: ["from:stream"],
        }),
      });
      toast(`queued ${(t.id || "").slice(0, 6)} · ${item.title.slice(0, 34)}`);
    }
    closeAction();
    await poll();
    setMode("board");
  } catch (e) {
    toast(e.message, true);
  } finally { btn.disabled = false; }
}

async function actionRunNow(btn) {
  if (!actionItem) return;
  const item = actionItem;
  const ctx = $("action-ctx").value;
  const cwd = $("action-cwd").value.trim();
  const agent = $("action-agent").value || null;
  if (!cwd) { toast("a working directory is required to spawn", true); return; }

  btn.disabled = true;
  try {
    const name = (item.title || "stream-item")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40)
      || "stream-item";
    const run = await act(`otto spawn ${name}${agent ? " --agent " + agent : ""} --prompt "…"`, "/api/runs/spawn", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        name, prompt: buildPrompt(item, ctx, cwd), cwd, agent,
        mode: "headless", domain: item.domain || null,
        task_id: storedTaskId(item),
      }),
    });
    toast(`spawned ${name} · run ${(run.id || "").slice(0, 6)}`);
    closeAction();
    await poll();
    select("run", run.id);
    setMode("today");
  } catch (e) {
    toast(e.message, true);
  } finally { btn.disabled = false; }
}

/* ============================== overlays ============================== */

function paletteHits() {
  const q = paletteQuery.toLowerCase().trim();
  const repoHits = q
    ? (repoData ? repoData.repos : []).filter(
        (r) => (r.key + " " + r.path).toLowerCase().includes(q))
    : [];
  const match = (c) => !q || (c.cmd + " " + c.desc + " " + c.group).toLowerCase().includes(q);
  return repoHits.map((r) => ({
    group: "Open in", cmd: "open  " + r.key, desc: r.path, repo: r.key,
  })).concat(liveHits().filter(match)).concat(COMMANDS.filter(match)).concat(DASH_ACTIONS.filter(match));
}

function renderPalette() {
  $("palette-overlay").hidden = !paletteOpen;
  if (!paletteOpen) { palSig = null; return; }
  const list = $("palette-list");
  const hits = paletteHits();
  /* Rebuilt only when the query, the cursor or the hits changed; render() calls
   * this on every state change and the list is live DOM. */
  const sig = JSON.stringify([paletteQuery, paletteIdx, hits.map((c) => c.cmd + "\u0001" + c.desc)]);
  if (sig === palSig) return;
  palSig = sig;
  list.replaceChildren();
  if (!hits.length) {
    list.appendChild(el("p", "sheet-empty",
      "No such command. Otto would rather say none exists than invent a plausible one."));
    return;
  }
  const frag = document.createDocumentFragment();
  let group = null;
  hits.forEach((c, i) => {
    if (c.group !== group) {
      group = c.group;
      frag.appendChild(el("div", "pal-group", group));
    }
    const b = el("button", "pal-row" + (i === paletteIdx ? " on" : ""));
    b.type = "button";
    b.appendChild(el("span", "cmd", c.cmd));
    b.appendChild(el("span", "desc", c.desc));
    b.addEventListener("mouseenter", () => {
      if (paletteIdx !== i) { paletteIdx = i; renderPalette(); }
    });
    b.addEventListener("click", () => pickPalette(c));
    frag.appendChild(b);
  });
  list.appendChild(frag);
}
function pickPalette(c) {
  paletteOpen = false;
  renderPalette();
  if (c.repo) setScope(c.repo);
  else if (c.act) c.act();
  else { copy(c.cmd); echo(c.cmd, "copied from the palette"); }
}

/* The cover card. Shown once per browser session on first open, and any time from
 * the palette. Explains the two ideas a cold reader needs (alert vs gap, Otto vs
 * work) before the numbers make sense. */
function openCover() {
  coverOpen = true;
  $("cover-overlay").hidden = false;
  try { sessionStorage.setItem("otto.cover", "1"); } catch { /* blocked */ }
}
function closeCover() {
  coverOpen = false;
  $("cover-overlay").hidden = true;
}

function renderScopePicker() {
  $("scope-overlay").hidden = !scopeOpen;
  if (!scopeOpen) { scopeSig = null; return; }
  const list = $("scope-list");
  const sig = JSON.stringify([scopeQuery, scope, repoData && (repoData.repos || []).map(
    (r) => [r.key, r.running, r.missing, r.tasks, r.defs, r.note])]);
  if (sig === scopeSig) return;
  scopeSig = sig;
  list.replaceChildren();

  const totalDefs = (repoData ? repoData.repos : []).reduce((n, r) => n + r.defs, 0);
  const all = el("button", "all-row" + (scope ? "" : " on"));
  all.type = "button";
  all.appendChild(ico("ph ph-squares-four"));
  all.appendChild(el("strong", null, "All roots"));
  all.appendChild(el("span", "sub",
    `every repo on this machine · ${totalDefs} definitions`));
  all.addEventListener("click", () => setScope(null));
  list.appendChild(all);

  const q = scopeQuery.toLowerCase().trim();
  let shown = 0;
  for (const root of (repoData ? repoData.roots : [])) {
    const repos = (repoData.repos || [])
      .filter((r) => r.root === root.path)
      .filter((r) => !q || (r.key + " " + r.path + " " + (r.note || "")).toLowerCase().includes(q));
    if (!repos.length) continue;
    shown += repos.length;

    const rh = el("div", "root-head");
    rh.appendChild(el("span", "path", root.path));
    rh.appendChild(domTag(root.domain));
    rh.appendChild(el("span", "sec-rule"));
    list.appendChild(rh);

    for (const r of repos) {
      const b = el("button", "repo-row" + (scope === r.key ? " on" : ""));
      b.type = "button";
      const left = el("div");
      left.style.minWidth = "0";
      const top = el("div", "top");
      top.appendChild(el("strong", null, r.name));
      let flag = null, fFg = WARN, fBg = WARN_BG;
      if (r.running) { flag = "live"; fFg = ACCENT; fBg = ACCENT_BG; }
      else if (r.missing) { flag = r.missing + " missing"; fFg = CRIT; fBg = CRIT_BG; }
      else if (r.tasks) { flag = r.tasks + " open"; }
      if (flag) {
        const f = el("span", "flag", flag);
        f.style.color = fFg; f.style.background = fBg;
        top.appendChild(f);
      }
      left.appendChild(top);
      left.appendChild(el("p", "note", r.note || ""));
      b.appendChild(left);
      const counts = [`${r.defs} defs`];
      if (r.tasks) counts.push(`${r.tasks} tasks`);
      if (r.running) counts.push(`${r.running} running`);
      b.appendChild(el("span", "counts", counts.join(" · ")));
      b.addEventListener("click", () => setScope(r.key));
      list.appendChild(b);
    }
  }
  if (!shown) {
    list.appendChild(el("p", "sheet-empty",
      "No repository matches that. Roots come from DOMAIN_ROOTS — add one there and rescan."));
  }
}

function setScope(key) {
  scope = key;
  scopeOpen = false; scopeQuery = "";
  /* Scope and domain are independent filters, and combining them silently hides
   * work. Clearing domain on scope makes the visible set match what the picker
   * just promised. */
  domain = "all";
  sel = null;
  const r = key ? (repoData ? repoData.repos : []).find((x) => x.key === key) : null;
  toast(key ? "scoped to " + (r ? r.path : key) : "scope cleared · all roots");
  render();
}

/* ============================== render ============================== */

function setMode(next) {
  const m = TITLES[next] ? next : "today";
  /* With the workspace split, the rail drives the FOCUSED pane. */
  if (splitMode && focusedPane === 1) { splitMode = m; render(true); return; }
  const changed = mode !== m;
  mode = m;
  location.hash = mode;
  if (mode === "plane" && planeTab === "registry" && !registryRows) loadRegistry();
  if (mode === "plane" && planeTab === "people" && !peopleRows) loadPeople();
  if (mode === "writing" && !writingData) loadWriting();
  /* Setup refetches on every entry: the file may have been edited from a terminal
   * since the view was last open, and the page should not show that stale. */
  if (mode === "setup" && changed) loadSetup();
  render();
  /* The view-change transition is keyed on a class, not on insertion: a poll
   * re-renders the same view and a fade per poll would be a flicker. */
  const centre = $("centre");
  if (changed && centre) {
    centre.classList.add("view-enter");
    centre.addEventListener("animationend", () => centre.classList.remove("view-enter"), { once: true });
    setTimeout(() => centre.classList.remove("view-enter"), 400);
  }
}

/* What a view reads from the state, serialized. The Board reads two keys and gets
 * the cheap slice (~1ms); every other view gets everything except `at` (the clock,
 * which is what used to force a rebuild every poll) and `tasks` (2 MB that only
 * the inspector reads, by id). Hand-listing fields per view was how the Dispatch
 * rail and the sessions list came to be missing from the old signature and only
 * repainted because `at` changed. */
const BOARD_KEYS = ["board", "dispatch"];
const DIGEST_SKIP = new Set(["at", "tasks"]);
/* Every `at` at any depth is a clock stamp the daemon rewrites on each read
 * (dispatch.at, logistics.at, alerts[].at), and `machine` is the CPU gauge. Neither
 * is a change in what a view shows, except the Control plane's host rows. */
const digestReplacer = (k, v) => (k === "at" ? undefined : v);
function stateDigest(m) {
  const s = state || {};
  if (!m) return "";
  if (m === "board") return JSON.stringify(BOARD_KEYS.map((k) => s[k]), digestReplacer);
  const o = {};
  for (const k of Object.keys(s)) {
    if (DIGEST_SKIP.has(k)) continue;
    if (k === "machine" && m !== "plane") continue;
    o[k] = s[k];
  }
  return JSON.stringify(o, digestReplacer);
}

/* Everything that decides what ONE pane looks like. `idx` is folded in because the
 * split strip marks the focused pane. */
function paneSignature(m, idx) {
  return JSON.stringify([
    m, idx, !!splitMode, focusedPane, domain, scope, planeTab, regQuery, peopleQuery,
    cardDetail, doneOpen, selected.size, [...selected].sort(), createdFilter, colShown, histShown,
    personSel && personSel.slug, sel && sel.type, sel && sel.id,
    writingSig, writingError, writingOpenNote, writingUrlFor, writingBusy, writingEditing, writingShowDropped,
    ledgerAt, ledgerDays, histWho, registryRows ? registryRows.length : -1, registryError,
    peopleRows ? peopleRows.length : -1, peopleError,
    termTarget, termMode, termPanesSig,
    setupSig, setupError, setupLoading, setupBusy, setupRestart, JSON.stringify(setupMsg),
  ]) + stateDigest(m);
}

/* Everything that decides what the shell looks like: both panes plus the rail and
 * topbar counts. Comparing it means a poll that changed nothing costs zero DOM
 * work. Time is deliberately NOT here: age labels refresh in place (refreshClocks),
 * and the held-outreach countdown ticks on its own, so the clock moving never
 * rebuilds a view. */
function viewSignature() {
  const s = state || {};
  const a = paneSignature(mode, 0);
  const b = splitMode ? paneSignature(splitMode, 1) : "";
  pendingPaneSigs = [a, b];
  const rail = JSON.stringify([
    mode, splitMode, focusedPane, domain, scope, personSel && personSel.slug, s.persona,
    ((s.notices) || []).map((n) => [n.id, n.read_at, n.domain]),
    ((s.briefing || {}).next || []).map((r) => [r.band, r.domain, r.cwd]),
    (s.board || {}).total, ((s.board || {}).columns || []).map((c) => [c.key, (c.cards || []).length]),
    (s.integrations || []).map((i) => [i.name, i.ok, i.mode]),
    (s.writing || {}).counts,
    (s.schedules || []).map((x) => [x.name, x.stale, x.stale_level, x.domain]),
    (s.live || []).length, (s.runs || []).length,
    s.setup,
  ]);
  return rail + "\u0001" + a + "\u0001" + b;
}

/* A render replaces the centre pane wholesale, which would steal focus and discard
 * whatever is half-typed. So while the caret is in a field there, defer. */
function typingInCentre() {
  const a = document.activeElement;
  if (!a) return false;
  const tag = (a.tagName || "").toLowerCase();
  if (tag !== "input" && tag !== "textarea" && tag !== "select") return false;
  const centre = $("centre");
  return !!(centre && centre.contains && centre.contains(a));
}

function render(force) {
  if (!state) return;
  /* A forced render still records the signature, or the very next poll sees a
   * stale one and rebuilds again for nothing. */
  const sig = viewSignature();
  if (!force) {
    if (sig === lastSig) return;
    if (typingInCentre()) { renderDeferred = true; return; }
    /* A rebuild mid-drag destroys the node being dragged and the browser cancels
     * the drag. Before time left the signature every poll rebuilt, so every drag
     * longer than one poll interval died silently. This is why drag-and-drop "was
     * broken". The guard stays: a real change can still land mid-drag. */
    if (dragId) { renderDeferred = true; return; }
    /* Same hazard without a drag: a rebuild between mouse-down and mouse-up swaps
     * the node under the pointer, and the click never fires. The rebuild waits
     * for the pointer to come back up. */
    if (pointerHeld) { renderDeferred = true; return; }
  }
  lastSig = sig;
  renderDeferred = false;
  /* The menu was opened from a card node this rebuild is about to destroy, so it
   * would be left pointing at nothing. */
  closeCardMenu();
  const centre = $("centre"), centre2 = $("centre2");
  renderRail();
  renderTopbar();

  /* A split that no longer fits (window shrank, inspector opened) closes itself
   * rather than squeezing two views under PANE_MIN. */
  if (splitMode && !canSplit()) { splitMode = null; focusedPane = 0; pendingPaneSigs = [paneSignature(mode, 0), ""]; }

  /* Each pane rebuilds only when ITS inputs changed. A live run ticking in the
   * second pane used to rebuild the board in the first, and the other way round. */
  const [sig0, sig1] = pendingPaneSigs;
  if (force || sig0 !== paneSigs[0]) {
    const keep = centre.scrollTop;
    renderPane(centre, mode, 0);
    if (keep) centre.scrollTop = keep;
    paneSigs[0] = sig0;
  }
  $("split").classList.toggle("two", !!splitMode);
  centre2.hidden = !splitMode;
  if (splitMode) {
    if (force || sig1 !== paneSigs[1]) {
      const keep = centre2.scrollTop;
      renderPane(centre2, splitMode, 1);
      if (keep) centre2.scrollTop = keep;
      paneSigs[1] = sig1;
    }
  } else if (centre2.firstChild) {
    centre2.replaceChildren();
    paneSigs[1] = null;
  }

  renderInspector(force);
  renderPalette();
  renderScopePicker();
  /* The action modal is NOT rebuilt by render(): a poll tick would wipe half-typed
   * context. It owns its own DOM until it closes. */
}

/* One pane: a strip naming the view (only when split, so a single pane looks as it
 * always did) and the view itself. Views read `state`, not `mode`, so the same
 * function renders either pane. */
/* ---- Dispatch: the logistics control room ----------------------------------------
 * Keitora's shape. A rail of the sessions living in herdr with their state read off
 * the screen, a strip of suggestions pairing a runnable card with an idle session,
 * lanes per session, and the inspector for whichever card is selected. Nothing moves
 * without Approve. The data is `state.logistics`, computed by otto/logistics.py. */
const HERDR_DOT = { idle: "idle", done: "idle", working: "running", blocked: "due", unknown: "backlog" };
/* herdr's reading of a pane as the hook vocabulary, the same map Session.effective_state uses. */
const HERDR_EFF = { working: "busy", blocked: "waiting", idle: "idle", done: "idle" };

/* ---- Dispatch parity: the session inspector, drag-to-dispatch, the palette's live
 * rows, the rail preview and the worktree button. Every button here is a real
 * endpoint and every palette row a real otto command; nothing is invented for the
 * screen. The embedded terminal is NOT here: that is term.js. ---- */

/* Session id whose "Dispatch a card" picker is open in the inspector. In memory,
 * like the other folds: a picker that survived a reload would be a surprise. */
let dispatchPickOpen = null;
/* agent -> {line, at, fetchedAt, busy}. The rail's last-line preview for working
 * panes. The daemon holds each read for 10s per target and this mirrors it, so a
 * render storm (every poll while a run is live) never becomes a request storm. */
const peekCache = {};
const PEEK_MS = 10000;

function sessionRow(sid) {
  if (!sid) return null;
  const data = (state && state.sessions) || {};
  return (data.sessions || []).find((x) => x.session_id === sid) || null;
}
function railRowFor(sid, paneId) {
  const rail = ((state && state.logistics) || {}).rail || [];
  return rail.find((r) => (sid && r.session_id === sid) || (paneId && r.pane_id === paneId)) || null;
}
/* Panes a card may be handed to: herdr says idle or done, and no card is already on
 * them. The same rule logistics.targets applies server-side; the server still gates. */
function idleTargets() {
  const rail = ((state && state.logistics) || {}).rail || [];
  return rail.filter((r) => (r.status === "idle" || r.status === "done") && !r.task_id);
}
function sessionLabel(x, r) {
  if (x && x.title) return x.title;
  if (r && r.agent) return r.agent;
  const cwd = (x && x.cwd) || (r && r.cwd) || "";
  return cwd.split(/[\\/]/).filter(Boolean).pop() || (x ? x.session_id.slice(0, 8) : "session");
}

async function dispatchCard(taskId, target, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await act(`otto dispatch to ${taskId.slice(0, 6)} ${target}`, "/api/logistics/dispatch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId, target }),
    });
    toast(res.message);
    dispatchPickOpen = null;
    pollSoon(400);
  } catch (e) { toast(e.message, true); if (btn) btn.disabled = false; }
}
async function approveProposal(id) {
  try {
    const r = await act(`otto dispatch approve ${id.slice(0, 6)}`, `/api/logistics/proposals/${id}/approve`, { method: "POST" });
    toast(r.message); pollSoon(400);
  } catch (e) { toast(e.message, true); }
}
/* Focus marks a `done` pane seen on herdr's side, which is why the badge clears
 * after a click: herdr owns "seen", Otto only reflects it. */
async function focusAgent(target) {
  try {
    await act(`otto herdr focus ${target}`, `/api/herdr/focus/${encodeURIComponent(target)}`, { method: "POST" });
    toast("focused " + target); pollSoon(600);
  } catch (e) { toast(e.message, true); }
}
async function openSessionWindow(sid) {
  try {
    const r = await act(`otto sessions open ${sid.slice(0, 8)}`, `/api/sessions/${sid}/open`, { method: "POST" });
    toast(r.message);
  } catch (e) { toast(e.message, true); }
}
async function renameSession(sid, current) {
  const t = prompt("Name this session", current || "");
  if (t === null || !t.trim()) return;
  try {
    await act(`otto sessions name ${sid.slice(0, 8)} "${t.trim()}"`, `/api/sessions/${sid}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: t.trim() }),
    });
    await poll();
  } catch (e) { toast(e.message, true); }
}
async function openRepoInHerdr() {
  const p = prompt("Directory to open (a repo path)", "D:\\otto");
  if (!p) return;
  toast("starting claude in " + p + "…");
  try {
    const r = await act(`otto herdr open ${p}`, "/api/herdr/open", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cwd: p }),
    });
    toast("up in " + (r.agent || {}).pane_id); pollSoon(500);
  } catch (e) { toast(e.message, true); }
}
/* A worktree pane: a branch checked out beside the repo with claude running in it.
 * The endpoint is the worktree backend's contract; a daemon started before it
 * landed answers 404, and that is said plainly rather than as a stack trace. */
async function newWorktree() {
  /* Default to the repo the view is scoped to, else the first configured root:
   * a path typed from memory is the one most often typed wrong. */
  const scoped = scope && repoData ? (repoData.repos || []).find((r) => r.key === scope) : null;
  const firstRoot = repoData && repoData.roots && repoData.roots[0] ? repoData.roots[0].path : "";
  const repo = prompt("Repo to branch from (a path)", scoped ? scoped.path : firstRoot);
  if (!repo) return;
  const branch = prompt("Branch name for the worktree", "");
  if (branch === null || !branch.trim()) return;
  const name = branch.trim();
  toast("creating worktree " + name + "…");
  try {
    const r = await act("POST /api/herdr/worktree/create", "/api/herdr/worktree/create", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd: repo, branch: name, label: name }),
    });
    toast("up in " + ((r.agent || {}).pane_id || r.pane_id || "a new pane")); pollSoon(500);
  } catch (e) {
    toast(/404|not found/i.test(e.message)
      ? "this daemon has no worktree endpoint yet: restart it once the worktree backend has landed"
      : e.message, true);
  }
}

/* Fill `node` with the pane's last screen line, now from the cache and again when
 * the fetch lands. Writes the DOM node directly rather than re-rendering: the
 * preview is the one thing on the rail that changes every few seconds. */
function peekLine(agent, node) {
  const c = peekCache[agent];
  if (c && c.line) node.textContent = c.line.slice(0, 90);
  if (c && (c.busy || Date.now() - c.fetchedAt < PEEK_MS)) return;
  peekCache[agent] = { ...(c || {}), busy: true, fetchedAt: Date.now() };
  api(`/api/herdr/peek/${encodeURIComponent(agent)}`)
    .then((r) => {
      peekCache[agent] = { line: r.line || "", at: r.at, fetchedAt: Date.now(), busy: false };
      if (r.line && node.isConnected) node.textContent = r.line.slice(0, 90);
    })
    .catch(() => { peekCache[agent] = { ...(peekCache[agent] || {}), busy: false, fetchedAt: Date.now() }; });
}

/* Drop a card on a session lane = dispatch. The board's own drag pattern: dragId is
 * set by taskCard's dragstart, and render() holds off while it is set. A lane that
 * is not idle still takes the drop so it can say why it refused; a dropEffect of
 * "none" would swallow the event and the card would just snap back, unexplained. */
function laneDrop(col, r) {
  const ok = (r.status === "idle" || r.status === "done") && !r.task_id;
  col.addEventListener("dragover", (e) => {
    if (!dragId) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    col.classList.add(ok ? "over" : "lg-refuse");
  });
  col.addEventListener("dragleave", () => col.classList.remove("over", "lg-refuse"));
  col.addEventListener("drop", async (e) => {
    e.preventDefault();
    col.classList.remove("over", "lg-refuse");
    const id = dragId;
    dragId = null; overCol = null;
    if (!id) return;
    if (!ok) {
      toast(`${r.agent} is ${r.task_id ? "already carrying a card" : r.status || "unknown"}, not idle`, true);
      return;
    }
    await dispatchCard(id, r.agent);
  });
}

/* The live half of the session inspector: state as the hooks and herdr each see it,
 * where it is, what it last said or asked, the card on it, and the verbs. The cost
 * half below it comes from the ledger, loaded lazily by select(). */
function sessionLiveBlock(sid) {
  const x = sessionRow(sid);
  const r = railRowFor(sid, null);
  if (!x && !r) return null;
  const L = (state && state.logistics) || {};
  const box = el("div", "insp-pad lg-sess");

  const hook = x ? x.state : null;
  const hs = r ? r.status : (x && x.herdr_status);
  const eff = hs ? (HERDR_EFF[hs] || hook || "idle") : hook;
  const tags = el("div", "tcard-tags");
  if (eff) tags.appendChild(pill(eff, eff === "busy" ? "running" : eff === "waiting" ? "due" : "idle"));
  if (hs) tags.appendChild(el("span", "chip-ref", "herdr " + hs));
  if (hs === "done") {
    const b = el("span", "chip-sm lg-done-badge", "finished, not seen");
    b.title = "herdr: the turn ended and nobody has looked. Focus marks it seen.";
    tags.appendChild(b);
  }
  if (hook && hs && HERDR_EFF[hs] !== hook) tags.appendChild(el("span", "chip-sm", "hooks say " + hook));
  if (x && x.domain) { tags.appendChild(el("span", "spacer")); tags.appendChild(domTag(x.domain)); }
  box.appendChild(tags);

  const cwd = (x && x.cwd) || (r && r.cwd) || null;
  const kv = [
    ["Pane", r ? `${r.agent} · ${r.pane_id}` : (x && x.herdr_pane ? `${x.herdr_agent || ""} · ${x.herdr_pane}` : "not in herdr")],
    ["Directory", cwd],
    ["Host", x && x.host],
    ["Since", x ? ageEl(x.state_since, null, "", " " + (eff || x.state)) : null],
    ["Turns", x && x.turns ? String(x.turns) : null],
    ["Mode", x && x.permission_mode],
    ["Session", sid.slice(0, 8)],
  ];
  for (const [k, v] of kv) {
    if (!v) continue;
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", k));
    const vv = el("span", "v" + (k === "Session" ? " mono" : ""));
    if (k === "Directory") vv.appendChild(pathEl(v));
    else if (v instanceof Node) vv.appendChild(v);
    else vv.textContent = String(v);
    row.appendChild(vv);
    box.appendChild(row);
  }

  const note = (x && x.note) || (r && r.note);
  const last = (x && x.last_message) || (r && r.last_message);
  if (eff === "waiting" && note) {
    const c = el("div", "callout warn tight");
    c.appendChild(el("strong", "sm", "Asking you"));
    c.appendChild(el("span", null, note));
    box.appendChild(c);
  } else if (last) {
    box.appendChild(el("h3", "insp-h2", eff === "waiting" ? "Blocked, last said" : "Last message"));
    box.appendChild(el("p", "insp-pre", last));
  }

  if (r && r.task_id) {
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", "Running card"));
    const v = el("span", "v");
    const link = el("a", null, `#${r.task_id.slice(0, 6)} ${r.task_title || ""}`);
    link.href = "#"; link.onclick = (e) => { e.preventDefault(); select("task", r.task_id); };
    v.appendChild(link);
    row.appendChild(v);
    box.appendChild(row);
  }

  const acts = el("div", "insp-acts");
  if (r) {
    const f = el("button", "btn btn-primary btn-xs"); f.type = "button";
    f.appendChild(ico("ph ph-crosshair")); f.appendChild(el("span", null, "Focus"));
    f.title = "otto herdr focus " + r.agent;
    f.onclick = () => focusAgent(r.agent);
    acts.appendChild(f);
  }
  if (x) {
    const o = el("button", "btn btn-secondary btn-xs"); o.type = "button";
    o.appendChild(ico("ph ph-arrow-square-out")); o.appendChild(el("span", null, "Open"));
    o.title = "otto sessions open " + sid.slice(0, 8);
    o.onclick = () => openSessionWindow(sid);
    acts.appendChild(o);
    const n = el("button", "btn btn-secondary btn-xs"); n.type = "button";
    n.appendChild(ico("ph ph-pencil-simple")); n.appendChild(el("span", null, "Rename"));
    n.onclick = () => renameSession(sid, x.title);
    acts.appendChild(n);
  }
  const canTake = r && (r.status === "idle" || r.status === "done") && !r.task_id;
  if (canTake) {
    const d = el("button", "btn btn-secondary btn-xs"); d.type = "button";
    d.appendChild(ico("ph ph-lightning"));
    d.appendChild(el("span", null, dispatchPickOpen === sid ? "Close picker" : "Dispatch a card"));
    d.disabled = !(L.open || []).length;
    d.title = (L.open || []).length ? "hand one of the cards that may run to this pane" : "no card passes the promotion gate right now";
    d.onclick = () => { dispatchPickOpen = dispatchPickOpen === sid ? null : sid; renderInspector(); };
    acts.appendChild(d);
  }
  box.appendChild(acts);

  if (canTake && dispatchPickOpen === sid) {
    const pick = el("div", "lg-picker");
    pick.appendChild(el("p", "sec-note", `Cards the gate would pass, to ${r.agent}:`));
    for (const k of L.open || []) {
      const b = el("button", "lg-pick-row"); b.type = "button";
      b.appendChild(el("span", "chip-ref", "#" + k.id.slice(0, 6)));
      b.appendChild(el("span", "ttl", k.title));
      b.title = `otto dispatch to ${k.id.slice(0, 6)} ${r.agent}`;
      b.onclick = () => dispatchCard(k.id, r.agent, b);
      pick.appendChild(b);
    }
    box.appendChild(pick);
  }
  return box;
}

/* What the task inspector adds for dispatch: where a running card is, and where an
 * open card could go. Board cards do not carry pane_id, so the stored task is read. */
function taskDispatchBlock(card) {
  const s = state || {};
  const L = s.logistics || {};
  const t = (s.tasks || []).find((q) => q.id === card.id) || card;
  const box = el("div", "lg-task");
  let any = false;
  if (t.pane_id || t.session_id) {
    const r = railRowFor(t.session_id, t.pane_id);
    const x = sessionRow(t.session_id);
    const sid = t.session_id || (r && r.session_id);
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", "Running in"));
    const v = el("span", "v lg-inline");
    v.appendChild(el("span", null, r ? `${r.agent} (${r.status})` : ((x && x.herdr_agent) || t.pane_id || "a session")));
    if (r) {
      const f = el("button", "density-chip", "Focus"); f.type = "button";
      f.onclick = () => focusAgent(r.agent);
      v.appendChild(f);
    } else if (sid) {
      const f = el("button", "density-chip", "Open"); f.type = "button";
      f.onclick = () => openSessionWindow(sid);
      v.appendChild(f);
    }
    if (sid) {
      const o = el("button", "density-chip", "Session"); o.type = "button";
      o.onclick = () => { select("session", sid); };
      v.appendChild(o);
    }
    row.appendChild(v);
    box.appendChild(row);
    any = true;
  }
  if ((L.open || []).some((k) => k.id === card.id)) {
    const idle = idleTargets();
    const row = el("div", "lg-pick");
    row.appendChild(el("span", "mono-dim", idle.length ? "Dispatch →" : "may run · no idle session to take it"));
    for (const r of idle) {
      const b = el("button", "density-chip", r.agent); b.type = "button";
      b.title = `otto dispatch to ${card.id.slice(0, 6)} ${r.agent}`;
      b.onclick = () => dispatchCard(card.id, r.agent, b);
      row.appendChild(b);
    }
    box.appendChild(row);
    any = true;
  }
  return any ? box : null;
}

/* The palette's live rows: sessions to jump to, dispatches to make. Each one is an
 * otto command (shown as the row's cmd) and runs it rather than copying it. */
function liveHits() {
  const s = state || {};
  const L = s.logistics || {};
  const rail = L.rail || [];
  const out = [];
  const live = ((s.sessions || {}).sessions || []).filter((x) => x.state !== "offline");
  for (const x of live) {
    const r = rail.find((q) => q.session_id === x.session_id) || null;
    const st = r ? r.status : x.state;
    out.push({
      group: "Sessions", cmd: "session  " + sessionLabel(x, r), desc: `${st} · ${x.cwd || ""}`.trim(),
      act: () => { select("session", x.session_id); setMode("dispatch"); },
    });
  }
  for (const r of rail) {
    if (r.session_id && live.some((x) => x.session_id === r.session_id)) continue;
    out.push({
      group: "Sessions", cmd: "session  " + r.agent, desc: `${r.status} · ${r.kind || "pane"} · ${r.cwd || ""}`,
      act: () => focusAgent(r.agent),
    });
  }
  for (const p of L.proposals || []) {
    out.push({
      group: "Dispatch", cmd: `otto dispatch approve ${p.id.slice(0, 6)}`,
      desc: `#${p.task_id.slice(0, 6)} ${p.task_title} → ${p.agent}`,
      act: () => approveProposal(p.id),
    });
  }
  const idle = idleTargets();
  for (const k of L.open || []) {
    for (const r of idle) {
      out.push({
        group: "Dispatch", cmd: `otto dispatch to ${k.id.slice(0, 6)} ${r.agent}`,
        desc: `#${k.id.slice(0, 6)} ${k.title} → ${r.agent}`,
        act: () => dispatchCard(k.id, r.agent),
      });
    }
  }
  if ((L.herdr || {}).installed) {
    out.push({ group: "Dispatch", cmd: "otto herdr open <path>", desc: "a new workspace with claude running in it", act: openRepoInHerdr });
  }
  return out;
}

function viewDispatch() {
  const L = state.logistics || {};
  const h = L.herdr || {};
  const wrap = el("div", "lg-wrap");

  const bar = el("div", "board-bar");
  if (!h.installed) {
    bar.appendChild(el("span", null, "herdr is not installed. Dispatch needs it for the panes: herdr.dev"));
  } else if (!h.running) {
    bar.appendChild(el("span", null, "herdr server is not running."));
    const up = el("button", "density-chip", "start it");
    up.type = "button";
    up.onclick = async () => { try { const r = await api("/api/herdr/ensure", { method: "POST" }); toast(r.message); pollSoon(1500); } catch (e) { toast(e.message, true); } };
    bar.appendChild(up);
  } else {
    const counts = {};
    for (const r of (L.rail || [])) counts[r.status] = (counts[r.status] || 0) + 1;
    bar.appendChild(el("span", null, ["idle", "working", "blocked", "done"]
      .filter((k) => counts[k]).map((k) => `${counts[k]} ${k}`).join(" · ") || "no sessions in herdr yet"));
  }
  bar.appendChild(el("span", "spacer"));
  const open = el("button", "density-chip");
  open.type = "button";
  open.appendChild(ico("ph ph-plus"));
  open.appendChild(el("span", null, "Open a repo in herdr"));
  open.title = "A new workspace with claude running in it";
  open.onclick = openRepoInHerdr;
  bar.appendChild(open);
  const wt = el("button", "density-chip");
  wt.type = "button";
  wt.appendChild(ico("ph ph-git-branch"));
  wt.appendChild(el("span", null, "New worktree"));
  wt.title = "A branch checked out beside the repo, with claude running in it";
  wt.onclick = newWorktree;
  bar.appendChild(wt);
  const rf = el("button", "density-chip");
  rf.type = "button";
  rf.appendChild(ico("ph ph-arrows-clockwise"));
  rf.appendChild(el("span", null, "Refresh suggestions"));
  rf.onclick = async () => { try { await api("/api/logistics/refresh", { method: "POST" }); pollSoon(300); } catch (e) { toast(e.message, true); } };
  bar.appendChild(rf);
  wrap.appendChild(bar);

  const body = el("div", "lg-body");

  /* -- rail -- */
  const rail = el("aside", "lg-rail");
  rail.appendChild(el("h4", "lg-h", "Sessions in herdr"));
  const rows = L.rail || [];
  if (!rows.length) {
    rail.appendChild(el("p", "sec-note", h.running
      ? "No agents in panes. Open a repo above, or `otto herdr adopt <session>` to move an ended conversation in."
      : "Start the server to see panes."));
  }
  for (const r of rows) {
    const row = el("div", "lg-lane-row lg-" + (r.status || "unknown")
      + (sel && sel.type === "session" && r.session_id && sel.id === r.session_id ? " on" : ""));
    const top = el("div", "live-top");
    top.appendChild(pill(r.status || "?", HERDR_DOT[r.status] || "backlog"));
    top.appendChild(el("strong", "live-name", r.agent));
    top.appendChild(el("span", "spacer"));
    if (r.status === "done") {
      /* herdr's "finished and nobody has looked". Focusing the pane clears it. */
      const dn = el("span", "chip-sm lg-done-badge", "done");
      dn.title = "finished, not yet seen · click to focus";
      top.appendChild(dn);
    }
    if (r.task_id) top.appendChild(el("span", "chip-ref", "#" + r.task_id.slice(0, 6)));
    row.appendChild(top);
    const where = (r.cwd || "").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "";
    row.appendChild(el("div", "lg-sub", (r.title || r.task_title || where).slice(0, 80)));
    if (r.status === "blocked" && r.note) row.appendChild(el("div", "lg-ask", "? " + r.note.slice(0, 90)));
    else if (r.status === "working") {
      /* The last screen line, polled through /api/herdr/peek while the pane works.
       * The hook's last message is the fallback until the first peek lands. */
      const pv = el("div", "lg-sub mono-dim lg-peek", (r.last_message || "working…").slice(0, 90));
      row.appendChild(pv);
      peekLine(r.agent, pv);
    }
    row.title = r.cwd || "";
    row.onclick = async () => {
      /* Split: the clicked session also lands in the terminal region under the
       * lanes. The POST below still focuses it in herdr's own window. */
      setTermTarget(r.agent || r.pane_id);
      /* And the inspector shows the session: state, cost, the card on it, the verbs. */
      if (r.session_id) select("session", r.session_id);
      try { await api(`/api/herdr/focus/${encodeURIComponent(r.agent)}`, { method: "POST" }); toast("focused " + r.agent); }
      catch (e) { toast(e.message, true); }
    };
    rail.appendChild(row);
  }
  body.appendChild(rail);

  /* -- stage -- */
  const stage = el("div", "lg-stage");

  const strip = el("section", "lg-strip");
  const sh = el("div", "lg-strip-head");
  sh.appendChild(ico("ph ph-lightning"));
  sh.appendChild(el("strong", null, "Dispatch suggestions"));
  const idle = rows.filter((r) => r.status === "idle" || r.status === "done").length;
  sh.appendChild(el("span", "mono-dim", `${idle} idle session${idle === 1 ? "" : "s"} · ${(L.open || []).length} card${(L.open || []).length === 1 ? "" : "s"} may run`));
  strip.appendChild(sh);
  const props = L.proposals || [];
  if (!props.length) {
    strip.appendChild(el("p", "sec-note", idle
      ? "Nothing to propose: no backlog card passes the promotion gate for these sessions. Assess cards with otto triage, or hand one over from the Open lane."
      : "No idle session to propose to."));
  }
  const cards = el("div", "lg-props");
  for (const p of props) {
    const c = el("article", "lg-prop");
    const t = el("div", "live-top");
    t.appendChild(el("span", "chip-ref", "#" + p.task_id.slice(0, 6)));
    t.appendChild(el("span", "mono-dim", "→"));
    t.appendChild(pill(p.agent, "idle"));
    t.appendChild(el("span", "spacer"));
    const conf = el("span", "lg-conf", "conf " + p.confidence.toFixed(2));
    conf.style.setProperty("--conf", String(p.confidence));
    conf.title = "How well this card fits this pane, 0 to 1; the reason is below";
    t.appendChild(conf);
    c.appendChild(t);
    const ttl = el("div", "tcard-title", p.task_title);
    ttl.style.cursor = "pointer";
    ttl.onclick = () => select("task", p.task_id);
    c.appendChild(ttl);
    c.appendChild(el("div", "lg-sub", p.rationale));
    const acts = el("div", "lg-acts");
    const ok = el("button", "lg-approve", "Approve");
    ok.type = "button";
    ok.onclick = async () => {
      ok.disabled = true;
      try { const r = await act(`otto dispatch approve ${p.id.slice(0, 6)}`, `/api/logistics/proposals/${p.id}/approve`, { method: "POST" }); toast(r.message); pollSoon(400); }
      catch (e) { toast(e.message, true); ok.disabled = false; }
    };
    const no = el("button", "density-chip", "Dismiss");
    no.type = "button";
    no.onclick = async () => {
      try { await act(`otto dispatch dismiss ${p.id.slice(0, 6)}`, `/api/logistics/proposals/${p.id}/dismiss`, { method: "POST" }); pollSoon(300); }
      catch (e) { toast(e.message, true); }
    };
    acts.appendChild(ok); acts.appendChild(no);
    c.appendChild(acts);
    cards.appendChild(c);
  }
  strip.appendChild(cards);
  stage.appendChild(strip);

  /* -- lanes: Open (may run), then one per session -- */
  const lanes = el("div", "board-cols lg-lanes");
  const openCol = el("div", "board-col");
  const oh = el("div", "col-head");
  oh.appendChild(el("h4", null, "Open"));
  oh.appendChild(el("span", "col-count", String((L.open || []).length)));
  openCol.appendChild(oh);
  openCol.appendChild(el("div", "queued-note", "Cards the promotion gate would pass. Hand one to an idle session, or wait for a suggestion."));
  const idleTargets = rows.filter((r) => (r.status === "idle" || r.status === "done") && !r.task_id);
  for (const k of (L.open || [])) {
    const card = taskCard(k);
    if (idleTargets.length) {
      const pick = el("div", "lg-pick");
      for (const r of idleTargets) {
        const b = el("button", "density-chip", "Dispatch → " + r.agent);
        b.type = "button";
        b.onclick = async (ev) => {
          ev.stopPropagation(); b.disabled = true;
          try { const res = await act(`otto dispatch to ${k.id.slice(0, 6)} ${r.agent}`, "/api/logistics/dispatch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: k.id, target: r.agent }) }); toast(res.message); pollSoon(400); }
          catch (e) { toast(e.message, true); b.disabled = false; }
        };
        pick.appendChild(b);
      }
      card.appendChild(pick);
    }
    openCol.appendChild(card);
  }
  if (!(L.open || []).length) openCol.appendChild(el("div", "col-empty", "—"));
  lanes.appendChild(openCol);

  const running = L.running || [];
  for (const r of rows) {
    const col = el("div", "board-col lg-lane lg-" + (r.status || "unknown"));
    const hd = el("div", "col-head");
    hd.appendChild(pill(r.status || "?", HERDR_DOT[r.status] || "backlog"));
    hd.appendChild(el("h4", null, r.agent));
    const mine = running.filter((t) => t.pane_id === r.pane_id);
    hd.appendChild(el("span", "col-count", String(mine.length)));
    col.appendChild(hd);
    for (const t of mine) col.appendChild(taskCard(t));
    laneDrop(col, r);
    if (!mine.length) col.appendChild(el("div", "col-empty", r.status === "working" ? "working on its own prompt" : "—"));
    lanes.appendChild(col);
  }
  stage.appendChild(lanes);
  /* Split: the focused pane under the lanes. The pane itself lives in term.js and
   * survives this view being rebuilt; this only says where it goes. */
  const curT = termRows().find((x) => x.target === termTarget);
  if (curT) {
    const tsec = el("section", "lg-term");
    tsec.id = "lg-term";
    const th = el("div", "lg-strip-head");
    th.appendChild(ico("ph ph-terminal-window"));
    th.appendChild(el("strong", null, curT.agent || curT.target));
    th.appendChild(el("span", "mono-dim", termMode));
    th.appendChild(el("span", "spacer"));
    const full = el("button", "density-chip", "Terminal view");
    full.type = "button";
    full.onclick = () => setMode("terminal");
    th.appendChild(full);
    const hide = el("button", "density-chip", "Hide");
    hide.type = "button";
    hide.onclick = () => setTermTarget(null);
    th.appendChild(hide);
    tsec.appendChild(th);
    const host = el("div", "tm-host lg-term-host");
    tsec.appendChild(host);
    termMount(curT.target, host, { mode: termMode });
    stage.appendChild(tsec);
  }
  body.appendChild(stage);
  wrap.appendChild(body);
  return wrap;
}

/* ---- Terminal and Grid: herdr panes, live, in the browser ------------------------
 * keitora's three shapes. Terminal is one pane at full fidelity behind a strip of
 * tabs; Grid is every pane tiled, observe-only unless Terminal already holds it;
 * Split is Dispatch with the focused pane under its lanes (above). The panes
 * themselves live in term.js and survive render(): a view only says where a pane
 * goes this frame, and term.js parks the ones nobody mounted. */

function termViewOpen() {
  const live = (m) => m === "terminal" || m === "grid" || m === "dispatch";
  return live(mode) || live(splitMode);
}
function termBase(p) {
  return (p || "").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "";
}

/* Every pane a strip can show, in a stable order: the rail's agents first (they
 * carry status and task), then the plain shells only /api/term/panes knows about.
 * `target` is what the relay URL takes: the agent name when there is one, else the
 * pane id. Both sources are shaped defensively because the relay's row format is
 * the backend's call and the rail's is logistics.py's. */
function termRows() {
  const rail = ((state || {}).logistics || {}).rail || [];
  const byPane = new Map(rail.map((r) => [r.pane_id, r]));
  const rows = [], seen = new Set();
  const push = (r) => {
    const target = r.target || r.agent || r.name || r.pane_id || r.id;
    if (!target || seen.has(target)) return;
    seen.add(target);
    /* /api/term/panes puts the agent KIND in `agent` ("claude") and the name in
     * `name`; the rail puts the name in `agent`. Name first, label for a plain
     * shell, and the rail's `agent` last so neither source shows "claude". */
    rows.push({
      target, pane_id: r.pane_id || r.id || "", agent: r.name || r.label || r.agent || "",
      status: r.status || r.agent_status || "unknown", cwd: r.cwd || "",
      title: r.title || r.task_title || r.terminal_title || "",
      task_id: r.task_id || null, kind: r.kind || "",
    });
  };
  for (const r of rail) push(r);
  for (const p of (termPanes || [])) {
    const r = byPane.get(p.pane_id || p.id);
    push(r ? { ...p, ...r } : p);
  }
  return rows;
}

/* Polls /api/term/panes only while a terminal view is open, and stops when none
 * is. A 404 means the relay half is not deployed: the rail still lists the agents,
 * so the strip works, and the endpoint is tried again a minute later. */
async function loadTermPanes() {
  clearTimeout(termPanesTimer);
  termPanesTimer = null;
  if (!termApiMissing) {
    try {
      const r = await api("/api/term/panes");
      termPanes = Array.isArray(r) ? r : (r.panes || r.rows || []);
    } catch (e) {
      if (/not found|404/i.test(e.message)) {
        termApiMissing = true;
        setTimeout(() => { termApiMissing = false; }, 60000);
      }
      termPanes = termPanes || [];
    }
  }
  termPanesAt = Date.now();
  const sig = JSON.stringify(termRows().map((r) => [r.target, r.status, r.cwd, r.title]));
  if (sig !== termPanesSig) { termPanesSig = sig; render(); }
  if (termViewOpen()) termPanesTimer = setTimeout(loadTermPanes, TERM_PANES_MS);
}
function termPanesTick() {
  if (termPanesTimer) return;
  const wait = TERM_PANES_MS - (Date.now() - termPanesAt);
  if (wait <= 0) loadTermPanes();
  else termPanesTimer = setTimeout(loadTermPanes, wait);
}

function setTermTarget(t, opts) {
  termTarget = t || null;
  try { localStorage.setItem("otto.term.target", termTarget || ""); } catch { /* storage blocked */ }
  if (opts && opts.view && mode !== opts.view) setMode(opts.view); else render(true);
  if (termTarget) {
    echo("herdr attach " + termTarget, termMode);
    if (window.OttoTerm) OttoTerm.focus(termTarget);
  }
}
/* Every terminal mount goes through here. If term.js did not load (a stale
 * index.html from the browser cache was how we found out), the view explains
 * itself instead of throwing inside render(), which the poll loop would report as
 * the daemon being unreachable. */
function termMount(target, host, opts) {
  if (window.OttoTerm) { OttoTerm.mount(target, host, opts); return; }
  const n = el("div", "sec-empty centred");
  n.appendChild(el("div", null, "The terminal module did not load."));
  n.appendChild(el("div", "mono-dim", "Reload the window (Ctrl+R). If it persists: otto doctor, then check /static/term.js serves."));
  host.appendChild(n);
}
function setTermMode(m) {
  termMode = m === "observe" ? "observe" : "control";
  try { localStorage.setItem("otto.term.mode", termMode); } catch { /* storage blocked */ }
  if (termTarget && window.OttoTerm) OttoTerm.setMode(termTarget, termMode);
  echo("herdr attach " + (termTarget || ""), termMode === "observe" ? "observe: read only" : "control: your keys go to the pane");
  render(true);
}

/* Ctrl+Alt shortcuts. term.js refuses exactly these keys so they bubble out of a
 * focused xterm; anything else typed in a pane stays in the pane. */
function termHotkey(key) {
  const k = (key || "").toLowerCase();
  if (k === "t") { setMode("terminal"); return true; }
  if (k === "g") { setMode("grid"); return true; }
  if (k === "b") { setMode("board"); return true; }
  if (/^[1-9]$/.test(k)) {
    const r = termRows()[Number(k) - 1];
    if (!r) { toast("no pane " + k, true); return true; }
    setTermTarget(r.target, termViewOpen() ? null : { view: "terminal" });
    return true;
  }
  return false;
}

function termTab(r, i, cur) {
  const b = el("button", "tm-tab" + (r.target === cur ? " on" : ""));
  b.type = "button";
  b.appendChild(el("span", "tm-dot tm-" + (r.status || "unknown")));
  b.appendChild(el("span", "tm-tab-name", r.agent || r.target));
  const where = termBase(r.cwd);
  if (where) b.appendChild(el("span", "tm-tab-cwd", where));
  if (i < 9) b.appendChild(el("span", "kbd", "^⌥" + (i + 1)));
  b.title = [r.target, r.status, r.title, r.cwd].filter(Boolean).join(" · ");
  b.onclick = () => setTermTarget(r.target);
  return b;
}

function viewTerminal() {
  const rows = termRows();
  const h = ((state || {}).logistics || {}).herdr || {};
  termPanesTick();
  const wrap = el("div", "tm-wrap");

  const strip = el("div", "tm-strip");
  const cur = rows.some((r) => r.target === termTarget) ? termTarget : ((rows[0] || {}).target || null);
  rows.forEach((r, i) => strip.appendChild(termTab(r, i, cur)));
  if (!rows.length) {
    strip.appendChild(el("span", "sec-note", !h.installed ? "herdr is not installed."
      : (h.running ? "No panes in herdr. Open a repo from Dispatch." : "herdr server is not running. Start it from Dispatch.")));
  }
  strip.appendChild(el("span", "spacer"));
  const seg = el("div", "tm-seg");
  for (const m of ["control", "observe"]) {
    const b = el("button", "tm-seg-btn" + (termMode === m ? " on" : ""), m);
    b.type = "button";
    b.title = m === "control" ? "Your keystrokes go to the pane (one controller at a time)" : "Watch only";
    b.onclick = () => setTermMode(m);
    seg.appendChild(b);
  }
  strip.appendChild(seg);
  wrap.appendChild(strip);

  const stage = el("div", "tm-stage");
  if (cur) {
    const host = el("div", "tm-host");
    stage.appendChild(host);
    termMount(cur, host, { mode: termMode });
  } else {
    stage.appendChild(el("div", "sec-empty centred", "Nothing to attach to yet."));
  }
  wrap.appendChild(stage);
  return wrap;
}

function viewGrid() {
  const rows = termRows();
  termPanesTick();
  const wrap = el("div", "tg-wrap");
  const bar = el("div", "board-bar");
  bar.appendChild(el("span", null, rows.length
    ? `${rows.length} pane${rows.length === 1 ? "" : "s"} · watching. Click a name to take it in Terminal.`
    : "No panes in herdr."));
  bar.appendChild(el("span", "spacer"));
  bar.appendChild(el("span", "kbd", "^⌥T terminal · ^⌥B board"));
  wrap.appendChild(bar);

  const grid = el("div", "tg-grid");
  rows.forEach((r, i) => {
    const cell = el("section", "tg-cell" + (r.target === termTarget ? " on" : ""));
    const head = el("div", "tg-head");
    head.appendChild(pill(r.status, HERDR_DOT[r.status] || "backlog"));
    const name = el("button", "tg-name", r.agent || r.target);
    name.type = "button";
    name.title = "Open in Terminal";
    name.onclick = () => setTermTarget(r.target, { view: "terminal" });
    head.appendChild(name);
    const where = termBase(r.cwd);
    if (where) head.appendChild(el("span", "tg-cwd", where));
    head.appendChild(el("span", "spacer"));
    if (i < 9) head.appendChild(el("span", "kbd", "^⌥" + (i + 1)));
    cell.appendChild(head);
    const host = el("div", "tm-host tg-host");
    cell.appendChild(host);
    /* keepMode: a pane Terminal already controls is not demoted by being tiled. */
    termMount(r.target, host, { mode: "observe", keepMode: true });
    grid.appendChild(cell);
  });
  wrap.appendChild(grid);
  return wrap;
}

function viewFor(m) {
  if (m === "board") return viewBoard();
  if (m === "dispatch") return viewDispatch();
  if (m === "history") return viewHistory();
  if (m === "writing") return viewWriting();
  if (m === "plane") return viewPlane();
  if (m === "terminal") return viewTerminal();
  if (m === "grid") return viewGrid();
  if (m === "setup") return viewSetup();
  return viewToday();
}
function renderPane(host, m, idx) {
  host.replaceChildren();
  if (splitMode) {
    const strip = el("div", "pane-strip" + (focusedPane === idx ? " focused" : ""));
    for (const [key, , label] of MODES) {
      const b = el("button", "mode-pick" + (m === key ? " on" : ""), label);
      b.type = "button";
      b.addEventListener("click", () => {
        if (idx === 0) mode = key; else splitMode = key;
        focusedPane = idx;
        if (key === "plane" && planeTab === "registry" && !registryRows) loadRegistry();
        if (key === "plane" && planeTab === "people" && !peopleRows) loadPeople();
        if (key === "writing" && !writingData) loadWriting();
        echo("otto " + MODE_CMD[key], "pane " + (idx + 1) + " → " + label);
        render(true);
      });
      strip.appendChild(b);
    }
    const hint = el("span", "hint" + (focusedPane === idx ? " on" : ""),
      (focusedPane === idx ? "focused · " : "") + "^" + (idx + 1));
    strip.appendChild(hint);
    const x = el("button", "insp-x");
    x.type = "button";
    x.title = "Close this pane";
    x.setAttribute("aria-label", "Close this pane");
    x.appendChild(ico("ph ph-x"));
    x.addEventListener("click", () => closeSplit(idx));
    strip.appendChild(x);
    host.addEventListener("mousedown", () => {
      if (focusedPane !== idx) { focusedPane = idx; render(true); }
    }, { once: true });
    host.appendChild(strip);
  }
  host.appendChild(viewFor(m));
}
function toggleSplit() {
  if (splitMode) { closeSplit(1); return; }
  if (!canSplit()) {
    toast(`split needs ${RAIL_W + PANE_MIN * 2}px of width`, true);
    return;
  }
  /* The second pane opens on the view most often wanted beside the current one: the
   * board next to Today, Today next to anything else. */
  splitMode = mode === "board" ? "today" : "board";
  focusedPane = 1;
  echo("otto status & otto board", "two panes side by side");
  render(true);
}
function closeSplit(idx) {
  /* Closing pane 1 keeps pane 2's view as the single view. */
  if (idx === 0 && splitMode) { mode = splitMode; location.hash = mode; }
  splitMode = null; focusedPane = 0;
  echo("otto " + MODE_CMD[mode], "single pane");
  render(true);
}

/* ============================== setup ==============================
 * First-run configuration. GET /api/setup is the whole truth for this view and every
 * action refetches it; /api/state carries only the summary the rail badge reads.
 * docs/control-room/setup.md is the contract. */

const SETUP_STATUS = {
  done: "done", todo: "to do", skipped: "skipped", restart: "needs restart",
};
/* Button steps and where their click goes. A step can also name its own endpoint
 * in data.endpoint, which wins. */
const SETUP_BUTTON_PATH = {
  hooks: "/api/setup/hooks/install", herdr: "/api/setup/herdr/up", finish: "/api/setup/complete",
};
const SETUP_RESTART_MS = 60000;
const SETUP_HEALTH_MS = 500;

function setupDigest(d) {
  if (!d) return "";
  return JSON.stringify([d.complete, d.completed_at, d.restart_needed, d.progress,
    (d.steps || []).map((st) => [st.id, st.status, st.summary, st.detail, st.command,
      st.action && st.action.kind,
      st.action && (st.action.fields || []).map((f) => [f.name, f.value]),
      st.action && (st.action.options || []).map((o) => [o.value, o.checked]),
      st.data])]);
}

/* A POST that answers with the GET body (skip, unskip, complete, reset) is adopted on
 * the spot so the card changes on the click; the refetch that follows confirms it. */
function adoptSetup(d) {
  if (!d || !Array.isArray(d.steps)) return false;
  setupData = d;
  setupSig = setupDigest(d);
  return true;
}

async function loadSetup() {
  setupLoading = true;
  setupError = null;
  try {
    adoptSetup(await api("/api/setup"));
  } catch (e) {
    setupError = /404|not found/i.test(e.message)
      ? "This daemon was started before the setup API existed. Restart it: otto restart."
      : e.message;
  }
  setupLoading = false;
  if (mode === "setup" || splitMode === "setup") render(true);
}

/* What a step's response is worth as one line under its action. */
function setupResultText(step, r) {
  if (!r || typeof r !== "object") return "Done";
  if (typeof r.message === "string" && r.message) return r.message;
  if (Array.isArray(r.written) || Array.isArray(r.removed)) {
    const parts = [];
    if ((r.written || []).length) parts.push("saved " + r.written.join(", "));
    if ((r.removed || []).length) parts.push("removed " + r.removed.join(", "));
    return parts.length ? parts.join(", ") : "Nothing changed";
  }
  if (Array.isArray(r.armed)) return r.armed.length ? "armed " + r.armed.join(", ") : "Nothing armed";
  if (step.id === "first_card" && r.title) return "Filed: " + r.title;
  return "Done";
}

/* One request from a step card: the busy state, the inline message, the refetch, and
 * a state poll so the rail badge moves with it. Errors land on the card, not only in
 * a toast that is gone in three seconds. */
async function setupAct(stepId, step, cmd, path, body) {
  setupBusy = stepId;
  delete setupMsg[stepId];
  render(true);
  try {
    const opts = { method: "POST" };
    if (body !== undefined) {
      opts.headers = { "content-type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await act(cmd, path, opts);
    if (!adoptSetup(r)) setupMsg[stepId] = { ok: r == null || r.ok !== false, text: setupResultText(step, r) };
    /* Typed values were accepted; the refetch shows what the server now has. */
    for (const k of Object.keys(setupDraft)) if (k.startsWith(stepId + ":")) delete setupDraft[k];
    await loadSetup();
    pollSoon(0);
  } catch (e) {
    setupMsg[stepId] = { ok: false, text: e.message || String(e) };
  } finally {
    setupBusy = null;
    render(true);
  }
}

/* A field's stored value as text for its control. Paths come one per line. */
function setupFieldText(f) {
  const v = f.value;
  if (v == null) return "";
  if (Array.isArray(v)) return v.join("\n");
  const s = String(v);
  if (f.kind === "paths" && !s.includes("\n") && s.includes(";")) return s.split(";").join("\n");
  return s;
}

/* The values a form submit sends. Empty means leave unchanged, unless the field had a
 * value before, in which case null asks the server to remove the key. Paths go as
 * newline-separated text; the server splits them. */
function setupFormValues(step) {
  const values = {};
  for (const f of (step.action && step.action.fields) || []) {
    const key = step.id + ":" + f.name;
    const before = setupFieldText(f);
    const raw = key in setupDraft ? setupDraft[key] : before;
    const v = f.kind === "paths"
      ? raw.replace(/\r/g, "").split("\n").map((x) => x.trim()).filter(Boolean).join("\n")
      : raw.trim();
    if (v !== "") values[f.name] = v;
    else if (before !== "") values[f.name] = null;
  }
  return values;
}

function setupField(step, f, idx) {
  const key = step.id + ":" + f.name;
  const id = `su-${step.id}-${f.name || idx}`;
  const wrap = el("div", "su-field");
  const lbl = el("label", "su-label", f.label || f.name);
  lbl.htmlFor = id;
  wrap.appendChild(lbl);
  const isPaths = f.kind === "paths";
  const ctl = document.createElement(isPaths ? "textarea" : "input");
  ctl.id = id;
  ctl.name = f.name;
  ctl.className = "input" + (isPaths ? " su-paths" : "");
  if (!isPaths) ctl.type = "text";
  else ctl.rows = 3;
  ctl.value = key in setupDraft ? setupDraft[key] : setupFieldText(f);
  if (f.placeholder) ctl.placeholder = f.placeholder;
  ctl.addEventListener("input", () => { setupDraft[key] = ctl.value; });
  wrap.appendChild(ctl);
  const hint = f.hint || (isPaths ? "One path per line." : "");
  if (hint) wrap.appendChild(el("p", "su-hint", hint));
  return wrap;
}

function setupSubmitBtn(step, label) {
  const b = el("button", "btn btn-primary btn-sm", label);
  b.type = "button";
  b.disabled = setupBusy === step.id;
  return b;
}

/* The action block of one card, by action.kind. */
function setupAction(step) {
  const a = step.action;
  const box = el("div", "su-action");
  if (!a || a.kind === "none") {
    /* The daemon step: URL, OTTO_HOME, keepalive hint, whatever the server put in data. */
    const kv = el("dl", "su-kv");
    for (const [k, v] of Object.entries(step.data || {})) {
      if (v == null || typeof v === "object") continue;
      kv.appendChild(el("dt", null, k.replace(/_/g, " ")));
      kv.appendChild(el("dd", null, String(v)));
    }
    if (kv.firstChild) box.appendChild(kv);
    return box;
  }
  const cmd = step.command || "otto setup";

  if (a.kind === "form") {
    const form = el("div", "su-form");
    (a.fields || []).forEach((f, i) => form.appendChild(setupField(step, f, i)));
    const save = setupSubmitBtn(step, "Save");
    save.addEventListener("click", () => {
      const values = setupFormValues(step);
      if (!Object.keys(values).length) { setupMsg[step.id] = { ok: true, text: "Nothing to save" }; render(true); return; }
      setupAct(step.id, step, cmd, "/api/setup/settings", { values });
    });
    form.appendChild(save);
    box.appendChild(form);
    return box;
  }

  if (a.kind === "first_card") {
    const form = el("div", "su-form");
    const fields = (a.fields && a.fields.length) ? a.fields : [
      { name: "title", label: "Title", kind: "text", placeholder: "Something worth doing" },
      { name: "detail", label: "Detail", kind: "text", placeholder: "Why it matters, in a line or two" },
    ];
    fields.forEach((f, i) => form.appendChild(setupField(step, f, i)));
    const save = setupSubmitBtn(step, a.label || "File it");
    save.addEventListener("click", () => {
      const title = (setupDraft[step.id + ":title"] || "").trim();
      const detail = (setupDraft[step.id + ":detail"] || "").trim();
      if (!title) { setupMsg[step.id] = { ok: false, text: "A title is needed" }; render(true); return; }
      setupAct(step.id, step, cmd, "/api/setup/first-card", { title, detail: detail || null });
    });
    form.appendChild(save);
    box.appendChild(form);
    return box;
  }

  if (a.kind === "choice") {
    const form = el("div", "su-form su-choice");
    const picked = new Set();
    (a.options || []).forEach((o, i) => {
      const key = step.id + ":" + o.value;
      const on = key in setupDraft ? !!setupDraft[key] : !!o.checked;
      if (on) picked.add(o.value);
      const row = el("label", "su-opt");
      const cb = document.createElement("input");
      cb.type = a.multi === false ? "radio" : "checkbox";
      cb.name = "su-" + step.id;
      cb.id = `su-${step.id}-opt-${i}`;
      cb.value = o.value;
      cb.checked = on;
      cb.addEventListener("change", () => {
        if (cb.type === "radio") for (const x of a.options) setupDraft[step.id + ":" + x.value] = x.value === o.value;
        else setupDraft[key] = cb.checked;
        if (cb.checked) picked.add(o.value); else picked.delete(o.value);
        if (cb.type === "radio") { picked.clear(); picked.add(o.value); }
      });
      row.htmlFor = cb.id;
      row.appendChild(cb);
      const t = el("span", "su-opt-text");
      t.appendChild(el("strong", null, o.label || o.value));
      if (o.hint) t.appendChild(el("span", "su-hint", o.hint));
      row.appendChild(t);
      form.appendChild(row);
    });
    const save = setupSubmitBtn(step, a.label || "Save");
    save.addEventListener("click", () => {
      const vals = (a.options || []).map((o) => o.value).filter((v) => picked.has(v));
      const data = step.data || {};
      /* Two shapes: a settings key (integrations) or a direct endpoint (schedules). */
      if (data.endpoint || (step.id === "schedules" && !data.setting)) {
        const key = data.key || "arm";
        setupAct(step.id, step, cmd, data.endpoint || "/api/setup/schedules", { [key]: vals });
      } else {
        const key = data.setting || "OTTO_INTEGRATIONS";
        setupAct(step.id, step, cmd, "/api/setup/settings", { values: { [key]: vals.length ? vals.join(",") : "none" } });
      }
    });
    form.appendChild(save);
    box.appendChild(form);
    return box;
  }

  if (a.kind === "button") {
    const path = (step.data && step.data.endpoint) || SETUP_BUTTON_PATH[step.id];
    const b = setupSubmitBtn(step, a.label || step.title);
    if (!path) { b.disabled = true; b.title = "This daemon gave no endpoint for the step"; }
    else b.addEventListener("click", () => setupAct(step.id, step, cmd, path));
    box.appendChild(b);
    return box;
  }

  box.appendChild(el("p", "su-hint", `Unknown action "${a.kind}"; use the command below.`));
  return box;
}

function setupCard(step) {
  const status = SETUP_STATUS[step.status] ? step.status : "todo";
  const card = el("article", "su-step su-" + status);
  card.dataset.step = step.id;

  const head = el("div", "su-step-head");
  const pill = el("span", "su-pill su-pill-" + status, SETUP_STATUS[status]);
  pill.setAttribute("aria-label", "status: " + SETUP_STATUS[status]);
  head.appendChild(pill);
  head.appendChild(el("h3", "su-step-title", step.title || step.id));
  if (step.required === true) head.appendChild(el("span", "su-req", "required"));
  else if (step.required === false) head.appendChild(el("span", "su-req", "optional"));
  card.appendChild(head);

  if (step.summary) card.appendChild(el("p", "su-summary", step.summary));
  if (step.detail) card.appendChild(el("p", "su-detail", step.detail));

  card.appendChild(setupAction(step));

  const m = setupMsg[step.id];
  if (m) {
    const line = el("p", "su-msg " + (m.ok ? "ok" : "bad"));
    line.setAttribute("role", "status");
    line.appendChild(ico(m.ok ? "ph ph-check-circle" : "ph ph-warning-circle"));
    line.appendChild(el("span", null, m.text));
    card.appendChild(line);
  }

  const foot = el("div", "su-foot");
  if (step.command) {
    const row = el("div", "su-cmd");
    row.appendChild(el("code", null, step.command));
    const cp = el("button", "icon-btn su-copy");
    cp.type = "button";
    cp.title = "Copy the command";
    cp.setAttribute("aria-label", "Copy " + step.command);
    cp.appendChild(ico("ph ph-copy"));
    cp.addEventListener("click", () => { copy(step.command); echo(step.command, "copied from Setup"); });
    row.appendChild(cp);
    foot.appendChild(row);
  }
  foot.appendChild(el("span", "spacer"));
  /* Skip never blocks and is offered to required steps too, just more quietly. A
   * done step has nothing to skip; the finish step is the end, not a step. */
  if (step.id !== "finish" && status !== "done") {
    const skipped = status === "skipped";
    const sk = el("button", "su-skip" + (step.required ? " quiet" : ""), skipped ? "Unskip" : "Skip");
    sk.type = "button";
    sk.disabled = setupBusy === step.id;
    sk.title = skipped ? "Put this step back" : (step.required ? "Skip a required step; Otto records it, nothing blocks" : "Skip this step");
    sk.addEventListener("click", () => setupAct(step.id, step,
      `otto setup --${skipped ? "unskip" : "skip"} ${step.id}`,
      skipped ? "/api/setup/unskip" : "/api/setup/skip", { step: step.id }));
    foot.appendChild(sk);
  }
  card.appendChild(foot);
  return card;
}

/* The restart banner. Lives while restart_needed is true or a restart is in flight. */
function setupBanner(d) {
  const bar = el("div", "su-banner" + (setupRestart === "timeout" ? " bad" : ""));
  bar.setAttribute("role", "status");
  if (setupRestart === "restarting") {
    bar.appendChild(el("span", "spin"));
    bar.appendChild(el("span", "su-banner-text", "Restarting…"));
    return bar;
  }
  if (setupRestart === "timeout") {
    bar.appendChild(ico("ph ph-warning-circle"));
    const t = el("span", "su-banner-text");
    t.appendChild(document.createTextNode("Otto did not come back. Run "));
    t.appendChild(el("code", null, "otto ensure"));
    t.appendChild(document.createTextNode(" in a terminal."));
    bar.appendChild(t);
    const cp = el("button", "cmd-btn", "otto ensure");
    cp.type = "button";
    cp.addEventListener("click", () => copy("otto ensure"));
    bar.appendChild(cp);
    const again = el("button", "btn btn-secondary btn-sm", "Try again");
    again.type = "button";
    again.addEventListener("click", () => { setupRestart = null; restartOtto(); });
    bar.appendChild(again);
    return bar;
  }
  bar.appendChild(ico("ph ph-arrows-clockwise"));
  bar.appendChild(el("span", "su-banner-text", "Settings saved. Restart Otto to apply them."));
  const rb = el("button", "btn btn-primary btn-sm", "Restart Otto");
  rb.type = "button";
  rb.addEventListener("click", restartOtto);
  bar.appendChild(rb);
  const m = setupMsg.__restart;
  if (m) bar.appendChild(el("span", "su-msg bad", m.text));
  return bar;
}

/* POST the restart, then watch /api/health until it answers from a new pid. The
 * global poll fails meanwhile and the offline strip may show; that is expected. */
async function restartOtto() {
  if (setupRestart === "restarting") return;
  delete setupMsg.__restart;
  let oldPid = null;
  try {
    const r = await act("otto restart", "/api/daemon/restart", { method: "POST" });
    oldPid = r && r.pid != null ? r.pid : null;
  } catch (e) {
    setupMsg.__restart = { ok: false, text: e.message || String(e) };
    render(true);
    return;
  }
  setupRestart = "restarting";
  render(true);
  const deadline = Date.now() + SETUP_RESTART_MS;
  let back = false;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, SETUP_HEALTH_MS));
    try {
      const h = await fetch("/api/health", { cache: "no-store" });
      if (h.ok) {
        const j = await h.json();
        if (j && j.pid != null && j.pid !== oldPid) { back = true; break; }
      }
    } catch { /* still down */ }
  }
  setupRestart = back ? null : "timeout";
  if (back) {
    stateEtag = null;
    await loadSetup();
    poll();
  } else {
    render(true);
  }
}

function setupDate(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return iso || "";
  try { return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
  catch { return new Date(t).toISOString().slice(0, 16).replace("T", " "); }
}

function viewSetup() {
  const wrap = el("div", "su-wrap");
  if (!setupData) {
    if (setupError) {
      const box = el("div", "su-error");
      box.appendChild(el("h2", "insp-h2", "Setup could not load"));
      box.appendChild(el("p", "insp-lede", setupError));
      const b = el("button", "btn btn-secondary btn-sm", "Retry");
      b.type = "button";
      b.disabled = setupLoading;
      b.addEventListener("click", () => loadSetup());
      box.appendChild(b);
      wrap.appendChild(box);
      return wrap;
    }
    if (!setupLoading) loadSetup();
    wrap.appendChild(skeleton("setup"));
    return wrap;
  }
  const d = setupData;
  const steps = d.steps || [];
  const prog = d.progress || {};
  const done = prog.done != null ? prog.done : steps.filter((s) => s.status === "done").length;
  const total = prog.total != null ? prog.total : steps.length;

  const head = el("header", "su-head");
  const titleRow = el("div", "su-title");
  titleRow.appendChild(el("h2", null, d.complete ? "Setup complete" : "Setup"));
  if (d.complete) {
    if (d.completed_at) titleRow.appendChild(el("span", "su-sub", "finished " + setupDate(d.completed_at)));
    const acts = el("div", "su-head-acts");
    const go = el("button", "btn btn-primary btn-sm", "Open Today");
    go.type = "button";
    go.addEventListener("click", () => setMode("today"));
    acts.appendChild(go);
    const again = el("button", "btn btn-secondary btn-sm", "Run again");
    again.type = "button";
    again.disabled = setupBusy === "__reset";
    again.addEventListener("click", () => setupAct("__reset", { id: "__reset" }, "otto setup --reset", "/api/setup/reset"));
    acts.appendChild(again);
    titleRow.appendChild(acts);
  } else {
    titleRow.appendChild(el("span", "su-sub", `${done} of ${total} done`));
  }
  head.appendChild(titleRow);
  const bar = el("div", "su-bar");
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", "setup progress");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(total));
  bar.setAttribute("aria-valuenow", String(done));
  const fill = el("span", "su-bar-fill");
  fill.style.width = (total ? Math.round(100 * done / total) : 0) + "%";
  bar.appendChild(fill);
  head.appendChild(bar);
  if (setupError) head.appendChild(el("p", "su-msg bad", "Refresh failed: " + setupError));
  wrap.appendChild(head);

  if (d.restart_needed || setupRestart) wrap.appendChild(setupBanner(d));

  const m = setupMsg.__reset;
  if (m) wrap.appendChild(el("p", "su-msg " + (m.ok ? "ok" : "bad"), m.text));

  const list = el("div", "su-steps");
  for (const step of steps) list.appendChild(setupCard(step));
  wrap.appendChild(list);
  return wrap;
}

/* ============================== poll ============================== */

/* GET /api/state with If-None-Match. A 304 means the daemon's state has not changed
 * since the last 200: nothing to parse, nothing to render. A daemon without ETags
 * never answers 304 and this is a plain GET. */
async function fetchState() {
  const headers = {};
  if (stateEtag) headers["If-None-Match"] = stateEtag;
  const r = await fetch("/api/state", { cache: "no-store", headers });
  if (r.status === 304) { count304++; return null; }
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  stateEtag = r.headers.get("ETag") || null;
  count200++;
  return r.json();
}

/* One poll at a time. A change event landing while a fetch is in flight queues one
 * more rather than racing it, and callers that `await poll()` get the in-flight one. */
function poll() {
  if (pollInFlight) { pollQueued = true; return pollInFlight; }
  pollInFlight = pollOnce().finally(() => {
    pollInFlight = null;
    if (pollQueued) { pollQueued = false; pollSoon(0); }
  });
  return pollInFlight;
}

async function pollOnce() {
  clearTimeout(pollTimer);
  try {
    const [s, r] = await Promise.all([
      fetchState(),
      repoData ? Promise.resolve(repoData) : api("/api/repos"),
    ]);
    if (s) state = s;
    repoData = r;
    lastGoodAt = Date.now();
    const wasDown = lastPollError != null;
    lastPollError = null;
    /* The Writing view reloads its own data: every poll while a run is in flight
     * (a draft landing is the thing you are waiting for), every 30s otherwise. */
    if ((mode === "writing" || splitMode === "writing") && writingData && !writingLoading) {
      const busy = (writingData.running || []).length > 0;
      if (busy || Date.now() - writingLoadedAt > 30000) loadWriting();
    }
    /* A live run's activity is the one thing that changes second to second. */
    if (sel && sel.type === "run" && ((state || {}).live || []).some((x) => x.id === sel.id)) {
      loadActivity(sel.id);
      loadPlan(sel.id);
    }
    /* A 304 changed nothing, so only the chip's clock moves. */
    if (wasDown) render(true); else if (s) render();
    if (!firstOpenDone) { firstOpenDone = true; resolveFirstOpen(); }
    /* The change socket is opened from here, after a poll has succeeded, so a
     * refused handshake can be read as "this daemon has no /ws/events" rather than
     * "the daemon is down". Back from unreachable means a restart: a pending retry
     * (possibly the long one) is dropped and the socket tried now. */
    if (wasDown) { clearTimeout(evtTimer); evtTimer = null; }
    if (!evtSock && evtTimer == null) connectEvents();
    renderConnection();
    openPendingReply();
  } catch (e) {
    lastPollError = e.message || String(e);
    if (!state) {
      /* Nothing was ever received, so there is nothing to grey. The shell's empty
       * state says what to do next rather than describing the absence. */
      $("centre").replaceChildren();
      const box = el("div", "insp-pad");
      box.appendChild(el("h2", "insp-h2", "Daemon unreachable"));
      box.appendChild(el("p", "insp-lede", e.message));
      const b = el("button", "cmd-btn", "otto serve");
      b.type = "button";
      b.addEventListener("click", () => copy("otto serve"));
      box.appendChild(b);
      $("centre").appendChild(box);
      /* No state means no setup summary either: the cover shows as it always did. */
      if (coverPending) { coverPending = false; openCover(); }
    }
    renderTopbar();
  }
  schedulePoll();
}

/* Once, when the first state lands: an unconfigured daemon opens on Setup instead of
 * the cover, and the choice is remembered for the session so a reload while half way
 * through does not drag the person back from wherever they went. Otherwise the cover
 * behaves exactly as before. */
let firstOpenDone = false;
function resolveFirstOpen() {
  const su = state && state.setup;
  if (!setupAutoOpened && su && su.complete === false && !pendingReply) {
    setupAutoOpened = true;
    try { sessionStorage.setItem("otto.setupAuto", "1"); } catch { /* blocked */ }
    coverPending = false;
    if (mode !== "setup") setMode("setup"); else loadSetup();
    return;
  }
  if (coverPending) { coverPending = false; openCover(); }
}

async function loadPlan(id) {
  try {
    const r = await api(`/api/runs/${id}`);
    planCache[id] = { verdict: r.verdict || null, plan: r.plan || null };
  } catch {
    planCache[id] = { verdict: null, plan: null };
  }
  if (sel && sel.id === id) renderInspector();
}
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = state && (state.live || []).length > 0;
  /* With the change socket open the daemon tells us when to fetch, so the timer is
   * a safety net. Without it, the old cadence: 2s while a run is live, else 3s. */
  const ms = evtOpen ? POLL_MS_PUSH : (busy ? POLL_MS_LIVE : POLL_MS);
  pollTimer = setTimeout(poll, ms);
}

/* ============================== change socket ==============================
 *
 * /ws/events: {"t":"hello","v":N} on connect, {"t":"changed","v":N} whenever a
 * state file was written, {"t":"ping"} every 25s. A `changed` becomes one poll,
 * debounced, so a write that lands as five files costs one fetch. The socket
 * closing (daemon restart, or a daemon that does not have the endpoint yet) puts
 * the poll back on its timer and reconnects with backoff, forever: a daemon that
 * gains the feature on its next restart is picked up without a reload. */
function connectEvents() {
  clearTimeout(evtTimer);
  evtTimer = null;
  if (evtSock || typeof WebSocket === "undefined") return;
  let ws;
  try {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events`);
  } catch {
    scheduleEventsReconnect();
    return;
  }
  evtSock = ws;
  ws.onopen = () => {
    if (evtSock !== ws) return;
    evtOpen = true;
    evtBackoff = EVT_BACKOFF_MIN;
    renderConnection();
    /* Whatever was written while the socket was down is caught by one fetch now;
     * schedulePoll() at the end of it slows the timer. */
    pollSoon(0);
  };
  ws.onmessage = (ev) => {
    if (evtSock !== ws) return;
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "hello") {
      evtVersion = m.v;
    } else if (m.t === "changed") {
      evtVersion = m.v;
      clearTimeout(changedTimer);
      changedTimer = setTimeout(() => { changedTimer = null; pollSoon(0); }, CHANGED_DEBOUNCE_MS);
    }
    /* ping: the browser answers the pong itself; the frame arriving is the proof. */
  };
  ws.onerror = () => { /* onclose follows and does the work */ };
  ws.onclose = () => {
    if (evtSock !== ws) return;
    evtSock = null;
    const was = evtOpen;
    evtOpen = false;
    if (was) { renderConnection(); schedulePoll(); }
    /* Refused outright while the daemon answers polls: this daemon has no
     * /ws/events yet. Each failed handshake is a browser console error, so the
     * retry waits long; a daemon that comes back from unreachable is tried at
     * once (pollOnce), which is how a restart with the feature gets picked up. */
    scheduleEventsReconnect(!was && lastPollError == null && state ? EVT_RETRY_ABSENT_MS : null);
  };
}
function scheduleEventsReconnect(ms) {
  clearTimeout(evtTimer);
  evtTimer = setTimeout(connectEvents, ms || evtBackoff);
  evtBackoff = Math.min(EVT_BACKOFF_MAX, evtBackoff * 2);
}

/* ============================== skeleton ==============================
 * What the centre shows between the window opening and the first state arriving
 * (~550ms on the live board, longer cold). Shaped like the view it stands in for
 * so the page does not flash from blank to full. */
function skeleton(m) {
  const box = el("div", "skel skel-" + m);
  box.setAttribute("aria-busy", "true");
  box.setAttribute("aria-label", "loading");
  box.appendChild(el("div", "skel-line"));
  if (m === "board") {
    const cols = el("div", "skel-cols");
    for (let c = 0; c < 5; c++) {
      const col = el("div", "skel-col");
      col.appendChild(el("div", "skel-line w2"));
      for (let i = 0; i < 4; i++) col.appendChild(el("div", "skel-card"));
      cols.appendChild(col);
    }
    box.appendChild(cols);
  } else {
    box.appendChild(el("div", "skel-line w2"));
    for (let i = 0; i < 5; i++) box.appendChild(el("div", "skel-card"));
  }
  return box;
}

/* For the perf harness and anyone curious in the console: how the page is being
 * fed. Not rendered anywhere. */
window.ottoPerf = () => ({
  etag: stateEtag, fetched: count200, unchanged304: count304,
  socketOpen: evtOpen, socketVersion: evtVersion,
  pollMs: evtOpen ? POLL_MS_PUSH : POLL_MS, colShown: { ...colShown },
});

/* ============================== wiring ============================== */

$("scope-btn").addEventListener("click", () => {
  scopeOpen = true; scopeQuery = ""; paletteOpen = false;
  renderScopePicker(); renderPalette();
  $("scope-q").value = ""; $("scope-q").focus();
});
$("scope-clear").addEventListener("click", () => setScope(null));
$("palette-btn").addEventListener("click", openPalette);
/* Set here rather than in index.html: the palette now searches sessions and
 * dispatches too, and the hint should say so wherever the markup came from. */
$("palette-q").placeholder = "Search tasks, sessions, commands";

function openPalette() {
  paletteOpen = true; paletteQuery = ""; paletteIdx = 0; scopeOpen = false;
  renderPalette(); renderScopePicker();
  $("palette-q").value = ""; $("palette-q").focus();
}

$("palette-q").addEventListener("input", (e) => {
  paletteQuery = e.target.value; paletteIdx = 0; renderPalette();
});
$("palette-q").addEventListener("keydown", (e) => {
  const hits = paletteHits();
  if (e.key === "ArrowDown") {
    e.preventDefault(); paletteIdx = Math.min(hits.length - 1, paletteIdx + 1); renderPalette();
  } else if (e.key === "ArrowUp") {
    e.preventDefault(); paletteIdx = Math.max(0, paletteIdx - 1); renderPalette();
  } else if (e.key === "Enter") {
    e.preventDefault();
    const c = hits[paletteIdx];
    if (c) pickPalette(c);
  }
});
$("scope-q").addEventListener("input", (e) => {
  scopeQuery = e.target.value; renderScopePicker();
});
for (const [ov, sh] of [["palette-overlay", "palette-sheet"], ["scope-overlay", "scope-sheet"]]) {
  $(ov).addEventListener("click", () => {
    paletteOpen = false; scopeOpen = false;
    renderPalette(); renderScopePicker();
  });
  $(sh).addEventListener("click", (e) => e.stopPropagation());
}

$("action-details").addEventListener("click", () => {
  const id = storedTaskId(actionItem);
  closeAction();
  if (id) { select("task", id); setMode("board"); }
});
$("action-x").addEventListener("click", closeAction);
$("action-cancel").addEventListener("click", closeAction);
$("action-overlay").addEventListener("click", closeAction);
$("action-sheet").addEventListener("click", (e) => e.stopPropagation());
$("action-ctx").addEventListener("input", refreshActionPreview);
$("action-cwd").addEventListener("input", refreshActionPreview);
$("person-x").addEventListener("click", closePerson);
$("person-overlay").addEventListener("click", closePerson);
$("person-sheet").addEventListener("click", (e) => e.stopPropagation());
$("notice-x").addEventListener("click", closeNotice);
$("reply-x").addEventListener("click", closeReply);
$("reply-cancel").addEventListener("click", closeReply);
$("reply-send").addEventListener("click", (e) => replySend(e.currentTarget));
$("reply-overlay").addEventListener("click", (e) => { if (e.target.id === "reply-overlay") closeReply(); });
/* Ctrl+Enter sends, the same chord the action sheet uses. */
$("reply-text").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); replySend($("reply-send")); }
});
$("notice-overlay").addEventListener("click", closeNotice);
$("notice-sheet").addEventListener("click", (e) => e.stopPropagation());
$("action-agent").addEventListener("change", refreshActionPreview);
$("action-copy").addEventListener("click", () => copy($("action-prompt").textContent));
$("action-queue").addEventListener("click", (e) => actionQueue(e.currentTarget));
$("action-run").addEventListener("click", (e) => actionRunNow(e.currentTarget));
/* Ctrl/Cmd+Enter sends, because the textarea owns plain Enter. */
$("action-ctx").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    actionQueue($("action-queue"));
  }
});

/* Dismissal. Anything that moves the page under the menu, or any click that is not
 * one of its own rows, closes it. `capture` on scroll because the board columns
 * scroll independently and those events do not bubble to window. */
document.addEventListener("mousedown", (e) => {
  if (cardMenu && !cardMenu.contains(e.target)) closeCardMenu();
});
document.addEventListener("scroll", closeCardMenu, true);
window.addEventListener("resize", closeCardMenu);
window.addEventListener("blur", closeCardMenu);
/* Right-clicking anywhere else, including another card, replaces the open menu
 * rather than stacking a second one. The card handler stops propagation before this
 * sees it, so this only fires for the "somewhere else" case. */
document.addEventListener("contextmenu", closeCardMenu);

document.addEventListener("keydown", (e) => {
  const k = (e.key || "").toLowerCase();
  const meta = e.metaKey || e.ctrlKey;
  /* Terminal shortcuts first: Ctrl+Alt so they survive a focused xterm, which
   * refuses exactly these keys and lets them bubble here. */
  if (e.ctrlKey && e.altKey && !e.shiftKey && termHotkey(e.key)) { e.preventDefault(); return; }
  if (meta && k === "k") { e.preventDefault(); openPalette(); }
  else if (meta && k === "o") {
    e.preventDefault();
    scopeOpen = true; scopeQuery = ""; paletteOpen = false;
    renderScopePicker(); renderPalette();
    $("scope-q").value = ""; $("scope-q").focus();
  } else if (meta && e.key === "\\") {
    e.preventDefault(); toggleSplit();
  } else if (meta && (e.key === "1" || e.key === "2") && splitMode) {
    e.preventDefault();
    focusedPane = e.key === "2" ? 1 : 0;
    render(true);
  } else if (!meta && !e.altKey && selected.size && mode === "board" && !typingInCentre()
             && !cardMenu && !actionItem && !paletteOpen && !scopeOpen
             && $("reply-overlay").hidden && $("notice-overlay").hidden) {
    /* Batch hotkeys, only while cards are held on the board and nothing is on top. */
    const to = { d: "done", b: "backlog", f: "faded", n: "needs-you", x: "blocked" }[k];
    if (to) { e.preventDefault(); moveCards([...selected], to); }
    else if (k === "a") { e.preventDefault(); for (const c of visibleCards()) selected.add(c.id); render(true); }
  } else if (e.key === "Escape") {
    /* Topmost first. The cover (z 89) is above the notice overlay (z 88), which is
     * above the others. The card menu sits above everything (z 92), so it goes
     * first. Person focus clears last of all, after the selection, because it is
     * the most deliberate thing on the page. */
    if (cardMenu) closeCardMenu();
    else if (!$("reply-overlay").hidden) closeReply();
    else if (coverOpen) closeCover();
    else if (!$("notice-overlay").hidden) closeNotice();
    else if (!$("person-overlay").hidden) closePerson();
    else if (actionItem) closeAction();
    else if (paletteOpen || scopeOpen) {
      paletteOpen = false; scopeOpen = false;
      renderPalette(); renderScopePicker();
    } else if (selected.size) clearCardSelection();
    else if (sel) clearSelection();
    else if (personSel) setPerson(null);
  }
});

/* ── console-borrowed chrome ── */
$("split-btn").addEventListener("click", toggleSplit);
$("person-clear").addEventListener("click", () => setPerson(null));
$("echo-cmd").addEventListener("click", () => copy(lastEcho));
$("conn-cmd").addEventListener("click", () => copy("otto serve"));
$("cover-x").addEventListener("click", closeCover);
$("cover-go").addEventListener("click", closeCover);
$("cover-overlay").addEventListener("click", closeCover);
$("cover-sheet").addEventListener("click", (e) => e.stopPropagation());
/* Window resize can make a split illegal; render() closes it. */
window.addEventListener("resize", () => { if (splitMode || state) render(true); });
/* First open of the session gets the cover. Not first open ever: this is the page
 * the owner opens cold every morning, and the explanation is cheap to dismiss. The
 * decision waits for the first state, because an unconfigured daemon gets the Setup
 * view in the cover's place (resolveFirstOpen). */
try { coverPending = !sessionStorage.getItem("otto.cover"); } catch { /* blocked */ }

/* A render skipped because the caret was in a field must still happen once the
 * field is left, or the view silently goes stale for as long as focus stays there. */
document.addEventListener("focusout", () => {
  setTimeout(() => {
    if (renderDeferred && !typingInCentre()) render(true);
  }, 0);
});

window.addEventListener("hashchange", () => {
  const h = location.hash.slice(1);
  const reply = (h.match(/^reply=([0-9a-f]{6,32})$/) || [])[1];
  if (reply) {
    /* A toast clicked while the dashboard is already open. State is here, so open
     * the sheet now rather than waiting for the next poll. */
    pendingReply = reply;
    openPendingReply();
    return;
  }
  if (h && h !== mode) setMode(h);
});

mode = TITLES[location.hash.slice(1)] ? location.hash.slice(1) : "today";
/* Below the three-pane breakpoint the inspector is a fixed overlay, so a pre-made
 * selection would cover the stream on first paint. */
if (!$("centre").firstChild) $("centre").appendChild(skeleton(mode));
/* Clocks: ages every 30s, countdowns every second while one is on screen. */
let clockTicks = 0;
setInterval(() => {
  clockTicks++;
  refreshClocks(clockTicks % Math.round(AGE_REFRESH_MS / CLOCK_TICK_MS) === 0);
}, CLOCK_TICK_MS);
/* poll() opens the change socket once the first state has landed. */
poll();
