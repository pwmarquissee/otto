/* otto/web/js/70-dispatch.js: the Dispatch view (the logistics control room) and dispatch parity: the session inspector, drag-to-dispatch, live palette rows, worktrees.
 * Relies on: 00-shell.js, 10-helpers.js, 65-overlays-render.js. */

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

