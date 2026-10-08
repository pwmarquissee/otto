/* otto/web/js/85-poll.js: poll(): the /api/state fetch with its ETag, and the /ws/events change socket that turns pushes into polls.
 * Relies on: 00-shell.js, 10-helpers.js, 65-overlays-render.js (every poll ends in render()). */

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

