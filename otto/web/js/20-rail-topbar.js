/* otto/web/js/20-rail-topbar.js: the rail (mode buttons, badges, scope button) and the topbar (domain switch, connection banner).
 * Relies on: 00-shell.js, 10-helpers.js. */

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

