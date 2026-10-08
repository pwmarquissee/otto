/* otto/web/js/80-setup.js: the first-run Setup view against GET /api/setup.
 * Relies on: 00-shell.js, 10-helpers.js. */

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

