/* otto/web/js/90-wiring.js: the skeleton painted before the first state, the perf hook, and the wiring: every DOM listener, the keyboard map, the first render and the first poll().
 * Relies on: everything above; this file runs last and is the only one that calls into the others at load time. */

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
