/* otto/web/js/60-actions.js: the actions a stream item offers and the action modal (queue, run now, preview).
 * Relies on: 00-shell.js, 10-helpers.js, 50-detail.js for the sheets it closes. */

/* ============================== actions ============================== */

async function launch(name, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await act(`otto launch ${name}`, `/api/schedules/${encodeURIComponent(name)}/run`, { method: "POST" });
    toast(`launched ${name}` + (r && r.id ? ` · run ${r.id.slice(0, 6)}` : ""));
    await poll();
  } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}
async function doRefresh(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "refreshing…"; }
  try {
    const res = await act("otto refresh", "/api/refresh", { method: "POST" });
    const started = (res.runs || []).map((r) => r.domain).join(", ");
    const skipped = res.skipped || [];
    toast(`refreshing ${started || "nothing"}`
      + (skipped.length ? ` · skipped ${skipped.map((x) => x.domain).join(", ")}` : ""));
    await poll();
  } catch (e) { toast(e.message, true); } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Refresh mail + calendar"; }
  }
}
async function doProbe(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "probing…"; }
  try {
    await act("otto probe", "/api/integrations/probe", { method: "POST" });
    toast("probe complete");
    await poll();
  } catch (e) { toast(e.message, true); } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Probe now"; }
  }
}
async function addTask() {
  const title = prompt("Task title");
  if (!title || !title.trim()) return;
  try {
    await act(`otto task add "${title.trim().replace(/"/g, "")}"`, "/api/tasks", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        title: title.trim(),
        domain: domain === "all" ? "work" : domain,
      }),
    });
    toast("task added");
    await poll();
  } catch (e) { toast(e.message, true); }
}

/* ============================== action modal ==============================
 *
 * Click a stream item, add what Otto does not already know, send it to be worked.
 *
 * Two routes out, and the difference matters:
 *
 *   Send to Otto  files/updates a board task as `queued`. Auto-dispatch picks it
 *                 up under every existing brake (max concurrent, per-task budget,
 *                 one attempt) and the card is visible on the board the whole time.
 *   Run now       spawns the session immediately, bypassing all of that.
 *
 * Queue is the default because a dashboard button that silently starts an
 * unattended skip-permissions session is the thing the brakes exist for.
 *
 * A row whose id is `task:<uuid>` ALREADY has a stored task. Actioning it updates
 * that task rather than filing a second one, which is why advisor.next_up puts its
 * dedupe key on the row.
 */

let actionItem = null;   // the normalised item being actioned
let agentList = null;    // registry agents, loaded on first open

/* Stream rows and board cards describe the same work in different shapes, so both
 * are normalised here rather than special-cased at every use. The field that
 * matters is `storedId`: present means a stored task to UPDATE, absent means a
 * derived condition to FILE. Getting that wrong duplicates cards. */
function actionable(src, from) {
  if (from === "card") {
    const derived = src.movable === false;
    return {
      from: "card",
      storedId: derived ? null : src.id,
      title: src.title,
      why: src.detail || `on the board as ${src.status}`,
      command: src.command || null,
      domain: src.domain, kind: src.kind || "task",
      cwd: src.cwd || null, agent: src.agent || null,
      status: src.status, derived,
      attempts: src.attempts || 0,
      lastError: src.last_error || null,
      taskRef: src.task_ref || null,
      band: src.priority === "urgent" || src.priority === "high" ? "today" : null,
    };
  }
  const id = (src && src.id) || "";
  const storedId = id.startsWith("task:") ? id.slice(5) : null;
  /* A stream row that IS a task carries only a summary, so pull the real card for
   * its status, attempts and error rather than re-deriving them from the row. */
  const card = storedId ? allCards().find((k) => k.id === storedId) : null;
  return {
    from: "stream",
    storedId,
    title: src.title,
    why: src.why,
    command: src.command || null,
    domain: src.domain, kind: src.kind,
    cwd: (card && card.cwd) || null, agent: (card && card.agent) || null,
    status: card ? card.status : null,
    derived: false,
    attempts: (card && card.attempts) || 0,
    lastError: (card && card.last_error) || null,
    taskRef: (card && card.task_ref) || null,
    band: src.band,
  };
}

function storedTaskId(item) { return (item && item.storedId) || null; }

/* ── full task detail ──
 * /api/state carries at most 600 chars of a task's detail (`detail_truncated`),
 * which is plenty for a card and not for a prompt or the inspector. The board card
 * and the stored task may each carry the flag, depending on which the daemon
 * truncated, so both are checked. */
function needsFullDetail(k) {
  if (!k || !k.id || detailCache[k.id] != null) return false;
  if (k.detail_truncated) return true;
  const t = ((state || {}).tasks || []).find((x) => x.id === k.id);
  return !!(t && t.detail_truncated);
}
function fullDetailOf(k) {
  return (k && detailCache[k.id] != null ? detailCache[k.id] : (k && k.detail)) || "";
}
async function loadFullDetail(id) {
  if (detailBusy.has(id)) return detailCache[id];
  detailBusy.add(id);
  try {
    const t = await api(`/api/tasks/${id}`);
    detailCache[id] = (t && (t.detail || (t.task && t.task.detail))) || "";
  } catch { /* keep the truncated text; the next open tries again */ }
  finally { detailBusy.delete(id); }
  if (sel && sel.type === "task" && sel.id === id) renderInspector();
  return detailCache[id];
}

function buildPrompt(item, ctx, cwd) {
  const lines = [
    `You are acting on an item Otto surfaced. Do the work; do not just describe it.`,
    ``,
    `ITEM: ${item.title}`,
    `WHY OTTO RAISED IT: ${item.why || "(no reason recorded)"}`,
    `DOMAIN: ${item.domain || "work"}`,
  ];
  if (item.command) lines.push(`OTTO'S SUGGESTED COMMAND: ${item.command}`);
  lines.push(`WORKING DIRECTORY: ${cwd}`);
  if (ctx && ctx.trim()) {
    lines.push(``, `CONTEXT FROM THE OWNER (this is the authoritative extra detail):`, ctx.trim());
  }
  lines.push(
    ``,
    `Finish the whole item or say precisely what blocked you. If the item turns out`,
    `to be already done or not a real problem, say so plainly rather than inventing`,
    `work. Report what you changed.`,
  );
  return lines.join("\n");
}

function defaultCwd(item) {
  if (item && item.cwd) return item.cwd;
  if (scope) {
    const r = (repoData ? repoData.repos : []).find((x) => x.key === scope);
    if (r) return r.path;
  }
  return ((state && state.config) || {}).home || "";
}

async function openAction(item) {
  actionItem = item;
  if (!agentList) {
    try {
      const rows = registryRows || await api("/api/registry");
      registryRows = rows;
      agentList = rows.filter((e) => e.kind === "agent" && !e.missing)
        .map((e) => e.name).sort();
    } catch { agentList = []; }
  }
  /* The "why" becomes the prompt's WHY line. A truncated one would hand the
   * session 600 chars of a 3,000-char brief, so the whole thing is fetched first. */
  if (item.storedId) {
    const k = allCards().find((c) => c.id === item.storedId);
    if (k && needsFullDetail(k)) {
      const d = await loadFullDetail(item.storedId);
      if (actionItem !== item) return;
      if (d) item.why = d;
    }
  }

  const tags = $("action-tags");
  tags.replaceChildren();
  const sev = item.kind === "outage" ? "crit" : (item.band === "today" ? "warn" : "idle");
  tags.appendChild(pill(item.kind || "item", sev));
  tags.appendChild(domTag(item.domain));
  const stored = storedTaskId(item);
  if (stored) tags.appendChild(el("span", "chip-mono", "task " + stored.slice(0, 6)));
  if (item.taskRef) tags.appendChild(el("span", "chip-ref", item.taskRef));
  if (item.status) tags.appendChild(pill(item.status, item.status));
  if (item.derived) tags.appendChild(el("span", "chip-mono", "derived"));

  $("action-title").textContent = item.title;
  $("action-why").textContent = item.why || "Otto recorded no reason for this row.";
  $("action-ctx").value = "";
  /* A failed card's error is the most useful thing to read before writing context,
   * so it goes above the box rather than being buried in the inspector. */
  $("action-error").hidden = !item.lastError;
  if (item.lastError) $("action-error-text").textContent = item.lastError;
  /* Details only means something for a card that exists. */
  $("action-details").hidden = !stored;

  const sel = $("action-agent");
  sel.replaceChildren();
  const none = el("option", null, "none (plain session)");
  none.value = "";
  sel.appendChild(none);
  for (const a of (agentList || [])) {
    const o = el("option", null, a);
    o.value = a;
    sel.appendChild(o);
  }
  /* Suggest an agent from the item's own domain words, never silently: it is
   * pre-selected in a visible dropdown the user can override. */
  /* A card that already names an agent wins over the keyword guess: someone chose
   * it deliberately. */
  const guess = item.agent || suggestAgent(item, agentList || []);
  if (guess) sel.value = guess;

  $("action-cwd").value = defaultCwd(item);

  refreshActionPreview();
  $("action-overlay").hidden = false;
  $("action-ctx").focus();
}

function suggestAgent(item, agents) {
  const hay = ((item.title || "") + " " + (item.why || "")).toLowerCase();
  const rules = [
    [/\bidp\b|mfa|sso|contractor|google workspace|offboard/, "identity-admin"],
    [/\bedr\b|detection|phish|iam|exposed|credential|token|rotate/, "security-auditor"],
    [/\brmm\b|endpoint|bitlocker|gpu|driver|bsod|workstation|laptop/, "endpoint-support"],
    [/aws|ec2|vpc|lambda|cloudtrail|s3|kms/, "infra-ops"],
    [/cost|spend|budget|licen[cs]e|invoice/, "cost-analyst"],
  ];
  for (const [re, name] of rules) {
    if (re.test(hay) && agents.includes(name)) return name;
  }
  return null;
}

function refreshActionPreview() {
  if (!actionItem) return;
  const ctx = $("action-ctx").value;
  const cwd = $("action-cwd").value.trim();
  $("action-prompt").textContent = buildPrompt(actionItem, ctx, cwd || "(unset)");

  const item = actionItem;
  const stored = storedTaskId(item);
  const dsp = (state && state.dispatch) || {};
  $("action-plan-label").textContent = "Prompt the session will receive";
  const note = $("action-note");

  /* What Send will actually do, stated per starting status. A card already `done`
   * gets re-opened; a spent attempt gets reset. Neither should be a surprise. */
  const bits = [];
  if (!stored) {
    bits.push("Send files a new queued task.");
  } else {
    const id6 = stored.slice(0, 6);
    if (item.status === "done") bits.push(`Send RE-OPENS ${id6} and queues it.`);
    else if (item.status === "queued") bits.push(`${id6} is already queued; Send adds your context.`);
    else bits.push(`Send moves ${id6} to queued.`);
    if (item.attempts >= (dsp.max_attempts || 1)) {
      bits.push(`It already used its ${item.attempts} attempt(s), so Send resets the counter `
        + "and clears the old error, otherwise auto-dispatch would skip it forever.");
    }
  }
  bits.push(dsp.enabled
    ? `Auto-dispatch runs it, max ${dsp.max_concurrent} at once`
      + (dsp.budget_usd != null ? `, $${dsp.budget_usd} cap.` : ".")
    : "Auto-dispatch is OFF, so it will sit on the board until you turn it on.");
  bits.push("Run now spawns immediately, skipping every brake.");
  note.className = "action-note" + (dsp.enabled ? "" : " warn");
  note.textContent = bits.join(" ");
}

function closeAction() {
  actionItem = null;
  $("action-overlay").hidden = true;
}

async function actionQueue(btn) {
  if (!actionItem) return;
  const item = actionItem;
  const ctx = $("action-ctx").value.trim();
  const cwd = $("action-cwd").value.trim();
  const agent = $("action-agent").value || null;
  const stored = storedTaskId(item);

  btn.disabled = true;
  try {
    if (stored) {
      /* Append rather than overwrite. The existing detail is why the card exists,
       * and replacing it would destroy the record to add to it. */
      const current = allCards().find((k) => k.id === stored);
      const detail = [(current && current.detail) || item.why, ctx && `--- context added ${new Date().toISOString().slice(0, 16).replace("T", " ")} ---\n${ctx}`]
        .filter(Boolean).join("\n\n");
      const body = { status: "queued", auto: true };
      if (detail) body.detail = detail;
      if (agent) body.agent = agent;
      if (cwd) body.cwd = cwd;
      /* Without this a card that already failed re-queues and then never
       * dispatches, because eligible() skips attempts >= max_attempts. The note
       * above says this is happening. */
      const maxAttempts = ((state && state.dispatch) || {}).max_attempts || 1;
      if ((item.attempts || 0) >= maxAttempts) { body.attempts = 0; body.last_error = ""; }
      await act(`otto task mv ${stored.slice(0, 6)} queued`, `/api/tasks/${stored}`, {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      toast(`queued task ${stored.slice(0, 6)}`);
    } else {
      const detail = [item.why, ctx && `--- context from the owner ---\n${ctx}`]
        .filter(Boolean).join("\n\n");
      const t = await act(`otto task add "${item.title.slice(0, 40).replace(/"/g, "")}" --status queued`, "/api/tasks", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: item.title,
          status: "queued",
          domain: item.domain || "work",
          priority: item.band === "today" ? "high" : "normal",
          detail,
          agent, cwd: cwd || null, auto: true,
          /* Provenance: this came from a stream row, not from the owner typing a task. */
          origin: "stream:" + (item.id || item.kind || "item"),
          tags: ["from:stream"],
        }),
      });
      toast(`queued ${(t.id || "").slice(0, 6)} · ${item.title.slice(0, 34)}`);
    }
    closeAction();
    await poll();
    setMode("board");
  } catch (e) {
    toast(e.message, true);
  } finally { btn.disabled = false; }
}

async function actionRunNow(btn) {
  if (!actionItem) return;
  const item = actionItem;
  const ctx = $("action-ctx").value;
  const cwd = $("action-cwd").value.trim();
  const agent = $("action-agent").value || null;
  if (!cwd) { toast("a working directory is required to spawn", true); return; }

  btn.disabled = true;
  try {
    const name = (item.title || "stream-item")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40)
      || "stream-item";
    const run = await act(`otto spawn ${name}${agent ? " --agent " + agent : ""} --prompt "…"`, "/api/runs/spawn", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        name, prompt: buildPrompt(item, ctx, cwd), cwd, agent,
        mode: "headless", domain: item.domain || null,
        task_id: storedTaskId(item),
      }),
    });
    toast(`spawned ${name} · run ${(run.id || "").slice(0, 6)}`);
    closeAction();
    await poll();
    select("run", run.id);
    setMode("today");
  } catch (e) {
    toast(e.message, true);
  } finally { btn.disabled = false; }
}

