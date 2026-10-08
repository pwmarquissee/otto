/* otto/web/js/65-overlays-render.js: the overlays (palette, scope picker, person sheet, cover) and `render()`, the one function that paints a frame from `state`.
 * Relies on: every view file above: render() dispatches to them by `mode`. */

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
