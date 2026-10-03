/* Otto dashboard: live herdr panes in the browser.
 *
 * One TermPane per herdr target (an agent name like `otto` or a pane id like
 * `w1:p1`). Each holds an xterm.js Terminal, its DOM container, and a WebSocket
 * to the daemon's relay. The views (Terminal, Grid, Dispatch) do not own any of
 * this: they ask for a pane to be mounted into a host element and forget about it.
 *
 * WHY THE PANES OUTLIVE THE VIEWS. app.js's render() rebuilds the centre pane from
 * scratch on every change, so anything living inside a view dies every few
 * seconds. A terminal that lost its scrollback and reconnected on every poll would
 * be unusable. So pane containers are created once, parked in a detached host
 * when no view is showing them, and MOVED into whichever view mounts them next.
 * The xterm buffer and the socket both survive the move.
 *
 * WHY A MUTATION OBSERVER. render() has no hook that says "this view is gone".
 * Rather than wire one into a 5500-line file other agents are editing, this file
 * watches the document for subtree changes and sweeps: any pane whose container
 * is no longer connected was dropped by a rebuild, so it is parked and its idle
 * clock starts. A pane parked for more than IDLE_CLOSE_MS closes its socket; the
 * next mount reopens it. That is what keeps Grid with many panes cheap when you
 * are looking at the Board instead.
 *
 * Protocol (fixed, implemented by the daemon):
 *   ws://host/ws/term/<target>?cols=N&rows=N&mode=control|observe&takeover=0|1
 *   server -> client: {t:"frame", b:<base64 ANSI bytes>} | {t:"closed", reason}
 *                     | {t:"error", m}
 *   client -> server: {t:"input", d} | {t:"resize", cols, rows}
 *                     | {t:"scroll", delta} | {t:"release"}
 */
(function () {
  "use strict";

  const IDLE_CLOSE_MS = 30000;
  const RESIZE_DEBOUNCE_MS = 120;
  /* An observer cannot ask herdr to resize, so a size change means a new stream
   * (see doFit). Longer than the fit debounce so a window drag or a view switch
   * costs one herdr process, not one per tick. */
  const OBSERVE_RECONNECT_MS = 400;
  /* How many terminal lines one wheel notch asks the server for, once xterm's own
   * scrollback is exhausted and the request has to go to herdr's. */
  const WHEEL_LINES = 3;

  const panes = new Map();
  /* Detached on purpose: a parked pane is not in the document at all, so it costs
   * no layout. xterm keeps its buffer regardless of where its element is. */
  const parking = document.createElement("div");
  parking.className = "tm-parking";

  /* ── theme ──
   * xterm needs literal colours, and the dashboard's tokens are plain hex in :root,
   * so they can be read once and handed over. Anything color-mix() based is skipped
   * because xterm cannot parse it. Fallbacks are the Nocturne values from style.css
   * so a token rename degrades to the same look rather than to black on black. */
  function token(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return /^#[0-9a-f]{3,8}$/i.test(v) ? v : fallback;
  }
  function rgba(hex, a) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  /* Windows Terminal's defaults (the Campbell scheme, Cascadia Mono 12pt), so a
   * pane here reads exactly like the same session in a Windows Terminal tab. The
   * dashboard's own palette stays outside the pane; inside it, the terminal is a
   * terminal. */
  const CAMPBELL = {
    background: "#0c0c0c", foreground: "#cccccc",
    cursor: "#ffffff", cursorAccent: "#0c0c0c",
    selectionBackground: "rgba(255,255,255,0.28)", selectionInactiveBackground: "rgba(255,255,255,0.16)",
    black: "#0c0c0c", red: "#c50f1f", green: "#13a10e", yellow: "#c19c00",
    blue: "#0037da", magenta: "#881798", cyan: "#3a96dd", white: "#cccccc",
    brightBlack: "#767676", brightRed: "#e74856", brightGreen: "#16c60c", brightYellow: "#f9f158",
    brightBlue: "#3b78ff", brightMagenta: "#b4009e", brightCyan: "#61d6d6", brightWhite: "#f2f2f2",
  };
  const FONT = '"Cascadia Mono", "Cascadia Code", Consolas, ui-monospace, monospace';
  const FONT_PX = 16;   // 12pt at 96 dpi, Windows Terminal's default size

  function theme() {
    return CAMPBELL;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function b64bytes(b) {
    const s = atob(b);
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  /* Shortcuts the app owns. xterm sees every key first because its textarea has
   * focus, so these are refused here and bubble up to app.js's keydown handler. */
  function appShortcut(e) {
    return e.ctrlKey && e.altKey && /^([0-9]|g|t|b)$/i.test(e.key || "");
  }

  /* ── pane ── */
  function create(target, opts) {
    const root = el("div", "tm-pane");
    root.dataset.target = target;
    const termEl = el("div", "tm-xterm");
    const badge = el("span", "tm-badge", "observing");
    badge.title = "Read-only. Switch to control to type.";
    const overlay = el("div", "tm-overlay");
    const flashEl = el("div", "tm-flash");
    flashEl.hidden = true;
    root.appendChild(termEl);
    root.appendChild(badge);
    root.appendChild(flashEl);
    root.appendChild(overlay);

    const term = new Terminal({
      theme: theme(),
      fontFamily: FONT,
      fontSize: FONT_PX,
      lineHeight: 1.2,
      cursorBlink: false,
      /* herdr owns the scrollback and renders the viewport; every frame we get is
       * the screen as herdr draws it. A second buffer here meant two scroll
       * positions that disagreed: wheel-up asked herdr for history while xterm
       * also scrolled its own copy, and wheel-down could not reach the bottom of
       * both. With no local buffer, scrolling is herdr's and always lands. */
      scrollback: 0,
      allowProposedApi: false,
      convertEol: false,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    /* Ctrl+V (and Ctrl+Shift+V): xterm must not turn the key into input, and the
     * browser must keep its default, which is a native paste into xterm's focused
     * textarea. That paste event is caught by the capture listener below and sent
     * as one bracketed paste. The native path needs no clipboard permission,
     * which the async clipboard API would in this webview. */
    term.attachCustomKeyEventHandler((e) => {
      if (appShortcut(e)) return false;
      if (e.ctrlKey && !e.altKey && (e.key === "v" || e.key === "V")) return false;
      return true;
    });

    const pane = {
      target, root, termEl, badge, overlay, flashEl, term, fit,
      ws: null,
      lastError: "",
      flashTimer: null,
      mode: (opts && opts.mode) || "control",
      takeover: false,
      opened: false,        // term.open() has run (needs a sized element)
      wantFocus: false,
      mounted: false,
      parkedAt: 0,
      idleTimer: null,
      resizeTimer: null,
      reconnectTimer: null,
      sent: { cols: 0, rows: 0 },
      /* "connecting" | "open" | "closed" | "error" | "idle" (parked, socket shut) */
      status: "idle",
      reason: "",
      gl: null,
      ro: null,
    };

    term.onData((d) => send(pane, { t: "input", d }));
    term.onBinary((d) => send(pane, { t: "input", d }));

    /* A paste is one block, not a stream of keystrokes. A real terminal wraps it
     * in bracketed-paste markers when the application asked for them, which is how
     * Claude Code knows to fold a long paste into "[Pasted text +N lines]". xterm
     * only knows the application asked if it sees the DECSET 2004 sequence, and
     * herdr consumes that before the frames reach us, so xterm pasted raw: every
     * newline was an Enter, each line above the last was submitted as its own
     * prompt, and "only the last bit" arrived. We take the paste event ourselves
     * (capture, so xterm's textarea never sees it) and send the whole text wrapped.
     * Right-click paste from the browser menu lands here too. */
    termEl.addEventListener("paste", (e) => {
      const text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
      e.preventDefault();
      e.stopPropagation();
      if (text) sendPaste(pane, text);
    }, true);
    /* Clicking anywhere in the pane focuses the terminal, so a right-click paste
     * has a target even when the pane was not the active element. */
    root.addEventListener("mousedown", () => { if (pane.opened) pane.term.focus(); });

    /* Every wheel tick goes to herdr, which scrolls the pane (its own scrollback
     * for a shell, the transcript view for Claude Code; verified live) and clamps
     * at the bottom, so "scroll to the end" always reaches the end.
     *
     * CAPTURE PHASE, AND STOP PROPAGATION. xterm.js listens for wheel on its own
     * screen element and, when the application is on the alternate screen, turns
     * each notch into Up/Down arrow keys. With a Claude pane that cycled the
     * prompt history instead of scrolling, and it happened alongside our scroll
     * command because a bubbling listener on the container ran second and could
     * not stop xterm's. Taking the event before it reaches xterm is the only way
     * to make the wheel mean scroll. Observers get nothing: herdr refuses scroll
     * on an observe stream, and swallowing the event is better than arrow keys. */
    termEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!pane.ws || pane.ws.readyState !== 1 || pane.mode !== "control") return;
      const lines = Math.max(1, Math.min(WHEEL_LINES, Math.round(Math.abs(e.deltaY) / 40) || 1));
      send(pane, { t: "scroll", delta: e.deltaY < 0 ? -lines : lines });
    }, { capture: true, passive: false });

    /* The observer is how a pane learns it has a size at all. It fires when the
     * container is attached and laid out, which is the earliest moment xterm can
     * be opened (it measures a glyph), and again on every later resize. */
    pane.ro = new ResizeObserver(() => scheduleFit(pane));
    pane.ro.observe(root);

    setOverlay(pane, "idle");
    panes.set(target, pane);
    return pane;
  }

  /* Send pasted text as one bracketed paste. Line endings are normalized to CR,
   * which is what a terminal emits for Enter and what Claude Code's input box
   * treats as a newline inside a paste. */
  function sendPaste(pane, text) {
    if (!text || pane.mode !== "control") return;
    const body = text.replace(/\r\n/g, "\r").replace(/\n/g, "\r");
    send(pane, { t: "input", d: "\x1b[200~" + body + "\x1b[201~" });
  }

  function send(pane, msg) {
    if (!pane.ws || pane.ws.readyState !== 1) return;
    /* An observer's stream has no stdin: herdr refuses input, resize and scroll
     * alike ("this stream is observe-only", seen on the first live test when the
     * Grid's smaller box tried to resize). An observer that changes size gets a
     * new stream instead: see doFit. */
    if (pane.mode !== "control" && msg.t !== "release") return;
    pane.ws.send(JSON.stringify(msg));
  }

  /* A non-fatal error from the relay: shown for a moment over the bottom of the
   * pane, and remembered so a close that follows can say why. */
  function flash(pane, text) {
    pane.lastError = text;
    pane.flashEl.textContent = text;
    pane.flashEl.hidden = false;
    clearTimeout(pane.flashTimer);
    pane.flashTimer = setTimeout(() => { pane.flashEl.hidden = true; }, 4000);
  }

  function scheduleFit(pane) {
    clearTimeout(pane.resizeTimer);
    pane.resizeTimer = setTimeout(() => doFit(pane), RESIZE_DEBOUNCE_MS);
  }

  function doFit(pane) {
    if (!pane.root.isConnected || pane.root.clientHeight === 0 || pane.root.clientWidth === 0) return;
    if (!pane.opened) {
      pane.term.open(pane.termEl);
      pane.opened = true;
      /* GPU rendering. The default DOM renderer repaints thousands of elements per
       * frame and is the usual reason an embedded terminal feels a step behind a
       * native one. The addon must load after open(); on a lost context it is
       * dropped and xterm falls back to the DOM renderer by itself. */
      if (window.WebglAddon) {
        try {
          const gl = new WebglAddon.WebglAddon();
          gl.onContextLoss(() => gl.dispose());
          pane.term.loadAddon(gl);
          pane.gl = gl;
        } catch (e) { pane.gl = null; }
      }
    }
    pane.fit.fit();
    if (pane.wantFocus) { pane.wantFocus = false; pane.term.focus(); }
    const { cols, rows } = pane.term;
    if (!pane.ws && pane.mounted && pane.status === "idle") {
      connect(pane);
      return;
    }
    if (cols !== pane.sent.cols || rows !== pane.sent.rows) {
      pane.sent = { cols, rows };
      if (pane.mode === "control") {
        send(pane, { t: "resize", cols, rows });
      } else if (pane.ws) {
        /* THE BLACK OBSERVER PANE (2026-10-02). herdr sends one full frame when a
         * stream opens and only the changed cells after that, positioned for the
         * size the stream was opened at. Fitting the local xterm to a new box
         * (Grid's tile, the Dispatch split, a window resize, or just coming back
         * to Terminal) resizes the buffer and, with no scrollback, drops the
         * screen; a controller gets it back because herdr resizes the PTY and the
         * program redraws, but an observer may not send resize, so nothing ever
         * repainted and the pane stayed black until the stream was reopened.
         * Measured in the desktop shell: 7% of the pane lit at open, 0.2% after
         * two view switches. So an observer whose size changed reopens its
         * stream at the new size and takes the full frame that comes with it. */
        clearTimeout(pane.reconnectTimer);
        pane.reconnectTimer = setTimeout(() => {
          pane.reconnectTimer = null;
          if (pane.mounted && pane.mode !== "control" && pane.ws) connect(pane);
        }, OBSERVE_RECONNECT_MS);
      }
    }
  }

  /* ── socket ── */
  function wsUrl(pane) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = new URLSearchParams({
      cols: String(pane.term.cols || 80),
      rows: String(pane.term.rows || 24),
      mode: pane.mode,
      takeover: pane.takeover ? "1" : "0",
    });
    return `${proto}://${location.host}/ws/term/${encodeURIComponent(pane.target)}?${q}`;
  }

  function connect(pane) {
    shut(pane);
    pane.lastError = "";
    setOverlay(pane, "connecting");
    let ws;
    try {
      ws = new WebSocket(wsUrl(pane));
    } catch (e) {
      setOverlay(pane, "error", e.message || "could not open socket");
      return;
    }
    pane.ws = ws;
    pane.sent = { cols: pane.term.cols, rows: pane.term.rows };
    ws.onopen = () => {
      if (pane.ws !== ws) return;
      pane.takeover = false;
      setOverlay(pane, "open");
      /* The size may have changed between the fit that chose the URL and now. */
      doFit(pane);
    };
    ws.onmessage = (ev) => {
      if (pane.ws !== ws) return;
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.t === "frame") {
        if (pane.status !== "open") setOverlay(pane, "open");
        pane.term.write(b64bytes(m.b));
      } else if (m.t === "closed") {
        setOverlay(pane, "closed", m.reason || "closed by server");
      } else if (m.t === "error") {
        flash(pane, m.m || "error");
      }
    };
    ws.onerror = () => {
      if (pane.ws !== ws) return;
      if (pane.status === "connecting") setOverlay(pane, "error", "relay unreachable");
    };
    ws.onclose = (ev) => {
      if (pane.ws !== ws) return;
      pane.ws = null;
      /* A close after the server already explained itself keeps that reason. A
       * refusal arrives as an error record then a close, so the last error is the
       * reason when the close frame carries none. Anything else is a dropped link. */
      if (pane.status === "open" || pane.status === "connecting") {
        const why = ev.reason || pane.lastError || ("connection lost (" + ev.code + ")");
        setOverlay(pane, pane.lastError && !ev.reason ? "error" : "closed", why);
      }
    };
  }

  /* Close without changing the overlay. Callers decide what the pane says next. */
  function shut(pane) {
    const ws = pane.ws;
    if (!ws) return;
    pane.ws = null;
    ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
    try { ws.close(); } catch { /* already closing */ }
  }

  /* ── overlays ── */
  function setOverlay(pane, status, reason) {
    pane.status = status;
    pane.reason = reason || "";
    pane.badge.hidden = pane.mode !== "observe";
    pane.root.dataset.status = status;
    const o = pane.overlay;
    o.replaceChildren();
    if (status === "open") { o.hidden = true; return; }
    o.hidden = false;
    if (status === "connecting") {
      o.appendChild(el("span", "tm-ov-text", "connecting to " + pane.target + "…"));
      return;
    }
    if (status === "idle") {
      o.appendChild(el("span", "tm-ov-text", "paused"));
      return;
    }
    const line = el("div", "tm-ov-text");
    line.appendChild(el("strong", null, status === "error" ? "error" : "closed"));
    line.appendChild(el("span", null, " " + pane.reason));
    o.appendChild(line);
    const acts = el("div", "tm-ov-acts");
    const re = el("button", "tm-ov-btn", "Reconnect");
    re.type = "button";
    re.onclick = () => connect(pane);
    acts.appendChild(re);
    if (pane.mode === "control") {
      /* One controller per pane: when someone else holds it the relay refuses,
       * and the honest options are to watch or to take it. */
      const take = el("button", "tm-ov-btn", "Take over");
      take.type = "button";
      take.onclick = () => { pane.takeover = true; connect(pane); };
      acts.appendChild(take);
      const watch = el("button", "tm-ov-btn", "Observe instead");
      watch.type = "button";
      watch.onclick = () => setMode(pane.target, "observe");
      acts.appendChild(watch);
    }
    o.appendChild(acts);
  }

  /* ── lifecycle ── */
  function park(pane) {
    if (!pane.mounted && pane.root.parentNode === parking) return;
    pane.mounted = false;
    pane.parkedAt = Date.now();
    parking.appendChild(pane.root);
    clearTimeout(pane.idleTimer);
    clearTimeout(pane.reconnectTimer);
    pane.idleTimer = setTimeout(() => {
      if (pane.mounted) return;
      shut(pane);
      setOverlay(pane, "idle");
    }, IDLE_CLOSE_MS);
  }

  function mount(target, host, opts) {
    if (!target || !host) return null;
    opts = opts || {};
    const pane = panes.get(target) || create(target, opts);
    clearTimeout(pane.idleTimer);
    pane.mounted = true;
    const wanted = opts.mode || pane.mode;
    /* keepMode: a view that would rather observe (Grid) does not yank control
     * away from a pane the Terminal view already holds. */
    const live = pane.ws && pane.ws.readyState <= 1;
    if (wanted !== pane.mode && !(opts.keepMode && live)) {
      pane.mode = wanted;
      if (live) { if (pane.mode === "observe") send(pane, { t: "release" }); shut(pane); }
      setOverlay(pane, "idle");
    }
    if (pane.root.parentNode !== host) host.appendChild(pane.root);
    /* An idle pane (never opened, or parked past the idle limit) reconnects once
     * it has a size; a closed one waits for the Reconnect button, because a pane
     * whose process exited would otherwise reconnect-fail in a loop every render. */
    pane.badge.hidden = pane.mode !== "observe";
    scheduleFit(pane);
    return pane;
  }

  function unmount(target) {
    const pane = panes.get(target);
    if (pane) park(pane);
  }

  function setMode(target, mode) {
    const pane = panes.get(target);
    if (!pane || (mode !== "control" && mode !== "observe")) return;
    if (pane.mode === mode) return;
    if (pane.mode === "control") send(pane, { t: "release" });
    pane.mode = mode;
    shut(pane);
    setOverlay(pane, "idle");
    if (pane.mounted) scheduleFit(pane);
  }

  /* A focus asked for before the pane has a size (the usual case: the view was
   * just built) is honored by the fit that opens it. */
  function focus(target) {
    const pane = panes.get(target);
    if (!pane) return;
    if (pane.opened && pane.root.isConnected) pane.term.focus();
    else pane.wantFocus = true;
  }

  function dispose(target) {
    const pane = panes.get(target);
    if (!pane) return;
    shut(pane);
    clearTimeout(pane.idleTimer);
    clearTimeout(pane.resizeTimer);
    clearTimeout(pane.reconnectTimer);
    clearTimeout(pane.flashTimer);
    pane.ro.disconnect();
    pane.term.dispose();
    pane.root.remove();
    panes.delete(target);
  }

  function list() {
    return [...panes.values()].map((p) => ({
      target: p.target, mode: p.mode, status: p.status, reason: p.reason, mounted: p.mounted,
    }));
  }

  /* The sweep: after any DOM change, a mounted pane whose container fell out of
   * the document was dropped by a view rebuild. Batched to one check per frame
   * because render() produces hundreds of mutations at once. */
  let sweepQueued = false;
  function sweep() {
    sweepQueued = false;
    for (const pane of panes.values()) {
      if (pane.mounted && !pane.root.isConnected) park(pane);
    }
  }
  new MutationObserver(() => {
    if (sweepQueued) return;
    sweepQueued = true;
    requestAnimationFrame(sweep);
  }).observe(document.body, { childList: true, subtree: true });

  window.OttoTerm = { mount, unmount, focus, setMode, dispose, list };
})();
