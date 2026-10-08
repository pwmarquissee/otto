/* otto/web/js/75-terminal.js: the Terminal and Grid views over herdr panes, driven by term.js (`OttoTerm`).
 * Relies on: 00-shell.js, 10-helpers.js, term.js (loaded before this directory). */

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

