/* otto/web/js/10-helpers.js: DOM and formatting helpers (`$`, `el`, ages, clocks, copy, echo), fetch wrappers and the status bar.
 * Relies on: 00-shell.js for the constants and state. */

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
/* The level a run was started at (models.Run.permissions). plan is read-only and
 * writes a proposal; yolo is the full operator; scoped is a caller's own allow or
 * deny list. Drawn wherever a run is named, so the level is never a surprise. */
const PERM_TITLE = {
  plan: "--permission-mode plan: read-only investigation that writes a plan",
  yolo: "--dangerously-skip-permissions: the full operator",
  scoped: "an allow or deny list of its own, not plan or yolo",
};
function permPill(level) {
  if (!level) return null;
  const s = el("span", "pill perm perm-" + level, level);
  s.title = PERM_TITLE[level] || level;
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

