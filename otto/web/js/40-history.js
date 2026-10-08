/* otto/web/js/40-history.js: the History view: runs, the ledger and spend tables, per-session detail.
 * Relies on: 00-shell.js, 10-helpers.js. */

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

