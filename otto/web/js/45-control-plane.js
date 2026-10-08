/* otto/web/js/45-control-plane.js: Ask (chat) and the Control plane: schedules, registry, integrations, people, machine.
 * Relies on: 00-shell.js, 10-helpers.js. */

/* ============================== ask ============================== */

/* ============================== control plane ============================== */

function viewPlane() {
  const s = state;
  const wrap = el("div", "plane");

  const tabs = el("div", "plane-tabs");
  for (const [key, label] of [["schedules", "Schedules"], ["registry", "Registry"],
                              ["people", "People"], ["system", "System"]]) {
    const b = el("button", planeTab === key ? "on" : null, label);
    b.type = "button";
    b.addEventListener("click", () => {
      planeTab = key;
      if (key === "registry" && !registryRows) loadRegistry();
      if (key === "people" && !peopleRows) loadPeople();
      render();
    });
    tabs.appendChild(b);
  }
  wrap.appendChild(tabs);

  if (planeTab === "schedules") {
    const grid = el("div", "sched-grid");
    for (const sc of (s.schedules || []).filter(keep)) {
      const [cls, label, detail] = schedState(sc);
      const [fg, bg] = stColor(cls);
      /* A known failure is not a crit card. The reason is right there on the chip. */
      const card = el("article", "sched-card" + (cls === "stale" && !sc.known_reason ? " crit" : ""));
      const top = el("div", "sched-top");
      const p = el("span", "chip-sm", label);
      p.style.color = fg; p.style.background = bg;
      top.appendChild(p);
      top.appendChild(el("strong", null, sc.name));
      if (sc.known_reason) top.appendChild(knownChip(sc.known_reason));
      top.appendChild(el("span", "spacer"));
      if (sc.autostart) {
        const a = el("span", "armed-flag");
        a.appendChild(ico("ph ph-lightning"));
        a.appendChild(el("span", null, "armed"));
        top.appendChild(a);
      }
      card.appendChild(top);
      card.appendChild(el("p", "sched-detail", detail || ""));
      const kv = el("div", "sched-kv");
      kv.appendChild(el("span", null, cadenceText(sc)));
      kv.appendChild(ageEl(sc.last_run, null, "last "));
      card.appendChild(kv);

      const acts = el("div", "sched-acts");
      const rb = el("button", "btn btn-primary btn-xs");
      rb.type = "button";
      rb.appendChild(ico("ph ph-play"));
      rb.appendChild(el("span", null, "Run"));
      rb.addEventListener("click", (e) => { e.stopPropagation(); launch(sc.name, rb); });
      acts.appendChild(rb);
      const tb = el("button", "btn btn-secondary btn-xs", sc.enabled ? "Disable" : "Enable");
      tb.type = "button";
      tb.addEventListener("click", async (e) => {
        e.stopPropagation();
        tb.disabled = true;
        try {
          await toggleSchedule(sc);
          toast(`${sc.name} ${sc.enabled ? "disabled" : "enabled"}`);
          await poll();
        } catch (err) { toast(err.message, true); } finally { tb.disabled = false; }
      });
      acts.appendChild(tb);
      card.appendChild(acts);

      card.addEventListener("click", () => select("sched", sc.name));
      grid.appendChild(card);
    }
    wrap.appendChild(grid);
    if (!(s.schedules || []).filter(keep).length) {
      wrap.appendChild(emptyState("No schedules in this view. Add one, and give it a staleness limit so silence becomes an alarm.",
        "otto schedule add <name> --command \"…\" --max-age-hours 26"));
    }

  } else if (planeTab === "registry") {
    if (registryError) {
      wrap.appendChild(loadFailed(registryError, loadRegistry));
      return wrap;
    }
    const bar = el("div", "reg-bar");
    const q = el("input", "input");
    q.type = "search";
    q.placeholder = `Filter ${(registryRows || []).length} definitions by name, repo, or description…`;
    q.value = regQuery;
    q.addEventListener("input", () => {
      regQuery = q.value;
      const host = $("reg-body");
      if (host) fillRegistry(host);
    });
    bar.appendChild(q);
    for (const [kind, n] of Object.entries(s.registry_summary || {})) {
      const c = el("span", "reg-chip" + (kind === "missing" ? " missing" : ""));
      c.appendChild(el("b", null, String(n)));
      c.appendChild(el("span", null, kind));
      bar.appendChild(c);
    }
    wrap.appendChild(bar);

    const table = el("table", "table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["Name", "Kind", "Domain", "Scope", "Description"]) {
      hr.appendChild(el("th", null, h));
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    tbody.id = "reg-body";
    table.appendChild(tbody);
    wrap.appendChild(table);
    fillRegistry(tbody);

  } else if (planeTab === "people") {
    if (peopleError) {
      wrap.appendChild(loadFailed(peopleError, loadPeople));
      return wrap;
    }
    if (peopleRows === null) {
      wrap.appendChild(el("p", "dim", "Loading dossiers…"));
      return wrap;
    }
    const rows = peopleRows;
    const bar = el("div", "reg-bar");
    const q = el("input", "input");
    q.type = "search";
    q.placeholder = `Filter ${rows.length} people by name, title, team, or location…`;
    q.value = peopleQuery;
    q.addEventListener("input", () => {
      peopleQuery = q.value;
      const host = $("people-body");
      if (host) fillPeople(host);
    });
    bar.appendChild(q);
    const noted = rows.filter(r => r.has_notes).length;
    for (const [n, label] of [[rows.length, "dossiers"], [noted, "with notes"],
                              [rows.length - noted, "bare"]]) {
      const c = el("span", "reg-chip" + (label === "bare" && n ? " missing" : ""));
      c.appendChild(el("b", null, String(n)));
      c.appendChild(el("span", null, label));
      bar.appendChild(c);
    }
    wrap.appendChild(bar);

    const table = el("table", "table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["", "Name", "Title", "Team", "Location", "Started"]) {
      hr.appendChild(el("th", null, h));
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    tbody.id = "people-body";
    table.appendChild(tbody);
    wrap.appendChild(table);
    fillPeople(tbody);

  } else {
    const grid = el("div", "sys-grid");
    grid.appendChild(planeBrakes(s));

    const ints = el("div");
    ints.appendChild(el("h3", "sys-h3", "Integrations"));
    for (const i of (s.integrations || [])) {
      let label, cls;
      if (i.ok && i.mode === "mcp-only") { label = "mcp?"; cls = "mcp"; }
      else if (i.ok) { label = "ok"; cls = "ok"; }
      else if (i.known_reason) { label = "known"; cls = "warn"; }
      else { label = "down"; cls = "crit"; }
      const [fg, bg] = stColor(cls);
      const row = el("div", "kv-row int");
      const p = el("span", "state-pill", label);
      p.style.color = fg; p.style.background = bg;
      row.appendChild(p);
      const b = el("div");
      const nm = el("div");
      nm.style.display = "flex"; nm.style.gap = "8px"; nm.style.alignItems = "center"; nm.style.flexWrap = "wrap";
      nm.appendChild(el("strong", null, i.name));
      if (i.known_reason) nm.appendChild(knownChip(i.known_reason));
      /* Mark or clear a known failure on the row itself, since an integration has no
       * inspector of its own. */
      const kb = el("button", "cmd-btn", i.known_reason
        ? `otto known rm integration ${i.name}` : `otto known add integration ${i.name}`);
      kb.type = "button";
      kb.title = i.known_reason ? "Clear the known-failure annotation" : "Annotate this as a known failure, with a reason";
      kb.addEventListener("click", () => i.known_reason
        ? clearKnown("integration", i.name) : markKnown("integration", i.name));
      if (!i.ok || i.known_reason) nm.appendChild(kb);
      b.appendChild(nm);
      b.appendChild(el("p", "sub", i.detail || ""));
      row.appendChild(b);
      ints.appendChild(row);
    }
    grid.appendChild(ints);

    const cfg = el("div");
    cfg.appendChild(el("h3", "sys-h3", "Config & host"));
    const cfgRows = [];
    for (const j of ((s.config || {}).junctions || [])) {
      cfgRows.push([j.ok ? "linked" : "broken", j.name + "/",
        `${j.files != null ? j.files + " files · " : ""}${j.target}`]);
    }
    for (const d of ((s.config || {}).deploy || [])) {
      const key = d.state === "DRIFT" ? "drift" : (d.state === "snapshot" ? "snapshot" : "sync");
      cfgRows.push([key, d.name, d.state === "DRIFT"
        ? `newer: ${d.newer} — otto config deploy | adopt`
        : (d.newer || "copied, in sync")]);
    }
    for (const m of (s.machine || [])) {
      cfgRows.push(["host", `${m.label} · ${m.value}`, m.detail || ""]);
    }
    for (const [label, what, where] of cfgRows) {
      const [fg, bg] = label === "host" ? [DIM, DIM_BG] : stColor(label);
      const row = el("div", "kv-row cfg");
      const p = el("span", "state-pill", label);
      p.style.color = fg; p.style.background = bg;
      row.appendChild(p);
      const b = el("div");
      const st = el("strong", null, what);
      st.style.fontSize = "13px";
      b.appendChild(st);
      /* Junction targets are paths; keep the leaf visible. */
      const sub = el("p", "sub");
      if (/[\\/]/.test(where) && where.length > 48) sub.appendChild(pathEl(where));
      else sub.textContent = where;
      b.appendChild(sub);
      row.appendChild(b);
      cfg.appendChild(row);
    }
    /* The first-run checklist, after its rail entry has gone. */
    const again = el("div", "su-again");
    const ab = el("button", "btn btn-secondary btn-xs", "Run setup again");
    ab.type = "button";
    ab.title = "Open the first-run checklist";
    ab.addEventListener("click", () => setMode("setup"));
    again.appendChild(ab);
    again.appendChild(el("span", "sub", "otto setup"));
    cfg.appendChild(again);
    grid.appendChild(cfg);

    /* Events, folded shut by default and saying what is inside. The daemon hands
     * over 60; the fold states that, and the counts by level, so opening it is a
     * decision rather than a dead end. */
    const ev = el("div", "sys-wide");
    const evs = s.events || [];
    const byLv = {};
    for (const e of evs) byLv[e.level] = (byLv[e.level] || 0) + 1;
    const counts = Object.entries(byLv).sort((a, b) => b[1] - a[1]).map(([k, n]) => n + " " + k).join(" · ");
    ev.appendChild(foldHead("Events", `${evs.length} most recent · ${counts || "none"}`, eventsOpen,
      () => { eventsOpen = !eventsOpen; render(true); }));
    if (eventsOpen) {
      for (const e of evs.slice(0, 60)) {
        const row = el("div", "ev-row");
        row.appendChild(el("span", "at", (e.at || "").replace("T", " ").replace("Z", "")));
        const lv = el("span", null, e.level);
        lv.style.color = e.level === "crit" ? CRIT
          : (e.level === "warn" ? WARN : "color-mix(in srgb, var(--color-text) 38%, transparent)");
        row.appendChild(lv);
        row.appendChild(el("span", "src", e.source || ""));
        row.appendChild(el("span", "msg-txt", e.message || ""));
        ev.appendChild(row);
      }
      const more = el("div", "more-row");
      more.appendChild(el("span", null, "older events: "));
      const b = el("button", "cmd-btn", "otto events --limit 200");
      b.type = "button";
      b.addEventListener("click", () => copy("otto events --limit 200"));
      more.appendChild(b);
      ev.appendChild(more);
    }
    grid.appendChild(ev);
    wrap.appendChild(grid);
  }
  return wrap;
}

function knownChip(reason) {
  const c = el("span", "known-chip");
  c.appendChild(ico("ph ph-bookmark-simple"));
  c.appendChild(el("span", null, "known"));
  c.title = "Known failure: " + reason;
  return c;
}

/* Mark and clear known failures. The reason is required, because the reason is the
 * whole record: a bare "known" is a snooze button, and a snooze button on an alarm
 * that then heals is exactly the annotation that ends up lying. */
async function markKnown(kind, name) {
  const reason = prompt(`Why is ${kind} ${name} a known failure? (kept on the record, shown on the chip)`);
  if (!reason || !reason.trim()) return;
  try {
    await act(`otto known add ${kind} ${name} --reason "${reason.trim().slice(0, 60)}"`,
      `/api/known/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {
      method: "PUT", headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason: reason.trim() }),
    });
    toast(`${name} marked known`);
    await poll();
  } catch (e) { toast(e.message, true); }
}
async function clearKnown(kind, name) {
  try {
    await act(`otto known rm ${kind} ${name}`,
      `/api/known/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`${name} is no longer marked known`);
    await poll();
  } catch (e) { toast(e.message, true); }
}
async function toggleSchedule(sc) {
  return act(`otto toggle ${sc.name}${sc.enabled ? " --off" : ""}`,
    `/api/schedules/${encodeURIComponent(sc.name)}/toggle?enabled=${!sc.enabled}`, { method: "POST" });
}

/* People are not domain-scoped: a colleague is not "work" or "personal", so the
 * work/life filter and the repo scope both deliberately do not apply here. */
function fillPeople(tbody) {
  tbody.replaceChildren();
  const q = peopleQuery.toLowerCase();
  const rows = (peopleRows || [])
    .filter((p) => !q || [p.display_name, p.title, p.department, p.org,
                          p.city, p.countryCode, p.login, p.email]
      .some((x) => (x || "").toLowerCase().includes(q)))
    /* Colleagues first, then external contacts, then unset department last. The
     * department sink uses an explicit flag rather than a sentinel string, because
     * "~~".localeCompare("Creative") is NEGATIVE -- collation orders punctuation
     * before letters, so a sentinel floated everyone without a department to the TOP. */
    .sort((a, b) => (a.external ? 1 : 0) - (b.external ? 1 : 0) ||
                    (a.department ? 0 : 1) - (b.department ? 0 : 1) ||
                    (a.department || "").localeCompare(b.department || "") ||
                    (a.display_name || a.slug).localeCompare(b.display_name || b.slug));
  for (const p of rows) {
    const focused = personSel && personSel.slug === p.slug;
    const tr = el("tr", "row-click" + (focused ? " row-focus" : ""));
    const mark = el("td", "people-mark");
    if (p.has_notes) {
      const i = el("i", "ph ph-note");
      i.title = "has notes";
      mark.appendChild(i);
    }
    /* Focus from the row, without opening the dossier. The selection is shell
     * state: Board, Threads and Outreach all pick it up. */
    const fb = el("button", "insp-x");
    fb.type = "button";
    fb.title = focused ? "Stop focusing on " + (p.display_name || p.slug) : "Focus on this person everywhere";
    fb.setAttribute("aria-label", fb.title);
    fb.appendChild(ico(focused ? "ph ph-user-focus" : "ph ph-crosshair-simple"));
    fb.style.fontSize = "13px";
    if (focused) fb.style.color = WARN;
    fb.addEventListener("click", (e) => {
      e.stopPropagation();
      setPerson(focused ? null : p.slug, p.display_name || p.slug);
    });
    mark.appendChild(fb);
    tr.appendChild(mark);
    const nameCell = el("td", null, p.display_name || p.slug);
    if (p.external) {
      const b = el("span", "ext-badge", "ext");
      b.title = "external contact, Slack-sourced (no directory record)";
      nameCell.appendChild(b);
    }
    tr.appendChild(nameCell);
    tr.appendChild(el("td", "dim", p.title || "—"));
    tr.appendChild(el("td", null, p.external ? (p.org || "external") : (p.department || "—")));
    tr.appendChild(el("td", "dim",
      [p.city, p.countryCode].filter(Boolean).join(", ") || p.timezone || "—"));
    tr.appendChild(el("td", "dim sm", p.startDate || p.first_seen || "—"));
    tr.addEventListener("click", () => openPerson(p.slug));
    tbody.appendChild(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "dim");
    td.colSpan = 6;
    td.appendChild(emptyState(peopleQuery
      ? `Nobody matches "${peopleQuery}". Clear the filter, or sync the roster from the directory.`
      : "No dossiers yet. Sync the roster from a directory dump to seed one per person.", "otto people --sync <directory-dump>"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

function fillRegistry(tbody) {
  tbody.replaceChildren();
  const q = regQuery.toLowerCase();
  const rows = (registryRows || [])
    .filter((e) => inDomain(e))
    .filter((e) => !scope || e.repo === scope)
    .filter((e) => !q || [e.name, e.repo, e.description]
      .some((x) => (x || "").toLowerCase().includes(q)));
  const CAP = 400;
  for (const e of rows.slice(0, CAP)) {
    const tr = el("tr", e.missing ? "missing" : null);
    tr.appendChild(el("td", null, e.name));
    tr.appendChild(el("td", "mono", e.kind));
    const d = el("td", "dom", e.domain === "personal" ? "life" : "work");
    d.style.color = e.domain === "personal" ? ACCENT : DIM;
    tr.appendChild(d);
    tr.appendChild(el("td", "mono", e.repo || "global"));
    tr.appendChild(el("td", "desc", e.description || ""));
    tbody.appendChild(tr);
  }
  /* A silent cap reads as "that is all of them". Say what was dropped. */
  if (rows.length > CAP) {
    const tr = el("tr");
    const td = el("td", "desc more-row", `${rows.length - CAP} more not shown · narrow the filter, or: otto registry`);
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "desc");
    td.colSpan = 5;
    td.appendChild(emptyState(regQuery
      ? `Nothing matches "${regQuery}". Definitions added on disk since the last scan will not be here yet.`
      : "No definitions in this view. Rescan the roots to pick up what is on disk.", "otto scan"));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

async function loadRegistry() {
  registryError = null;
  try {
    registryRows = await api("/api/registry");
  } catch (e) {
    registryError = e.message;      // same stuck-on-Loading flaw as people had
  }
  if ((mode === "plane" || splitMode === "plane") && planeTab === "registry") render(true);
}

async function loadPeople() {
  peopleError = null;
  try {
    peopleRows = await api("/api/people");
  } catch (e) {
    /* A 404 here has one overwhelmingly likely cause and it is worth naming, because
     * "not found" sends you looking for a missing dossier rather than a stale process. */
    peopleError = /404|not found/i.test(e.message)
      ? "This daemon was started before the dossier API existed. Restart it: "
        + "otto stop, then otto serve."
      : e.message;
  }
  if ((mode === "plane" || splitMode === "plane") && planeTab === "people") render(true);
}

/* Error + retry, shared by the lazy Control Plane panes. */
function loadFailed(msg, retry) {
  const box = el("div", "callout");
  box.appendChild(el("strong", "sm", "Could not load"));
  box.appendChild(el("p", "sm", msg));
  const b = el("button", "btn btn-sm", "Try again");
  b.type = "button";
  b.addEventListener("click", retry);
  box.appendChild(b);
  return box;
}

/* Dossier detail. Rendered as DOM nodes from the raw markdown rather than through
 * innerHTML: the bodies are hand-written by the owner, but a people directory is exactly
 * the file set you do not want to discover an injection sink in later. */
async function openPerson(slug) {
  let d;
  try { d = await api(`/api/people/${encodeURIComponent(slug)}`); }
  catch (e) { toast(e.message, true); return; }

  const shown = d.display_name || d.slug;
  $("person-title").textContent = shown;

  /* Pronouns sit beside the name, not buried in a facts grid. The point of the field
   * is that you read it before you write about someone -- Fran's dossier said "her"
   * for weeks because nothing put it in front of the reader. An unset value renders
   * as they/them and is styled as an assumption, so it reads as "not recorded yet"
   * rather than as a fact. */
  const tags = $("person-tags");
  tags.replaceChildren();
  tags.appendChild(el("span", "chip-sm chip-pro" + (d.pronouns_set ? "" : " assumed"),
                      d.pronouns || "they/them"));
  if (d.external) {
    const b = el("span", "chip-sm ext-chip", "external");
    b.title = "Slack-sourced; no directory record. sync() never touches this file.";
    tags.appendChild(b);
  }
  if (d.full_name && d.full_name !== shown) {
    tags.appendChild(el("span", "chip-sm", d.full_name));
  }
  /* Monikers sit with the name for the same reason pronouns do: they are what you
   * need before you write to someone, not a fact to go digging for. Fran asked for
   * the field after having to tell Otto himself that Fran is Francisco. Anything
   * already shown as the name or the full name is skipped, so the chips are the
   * names you would not otherwise know. */
  const shownNames = new Set([shown, d.full_name || ""].map((x) => x.toLowerCase()));
  for (const alias of d.monikers || []) {
    if (shownNames.has(alias.toLowerCase())) continue;
    const c = el("span", "chip-sm chip-alias", alias);
    c.title = "Also answers to this";
    tags.appendChild(c);
  }
  for (const v of [d.title, d.org, d.department, d.userType,
                   [d.city, d.countryCode].filter(Boolean).join(", ")]) {
    if (v) tags.appendChild(el("span", "chip-sm", v));
  }

  const body = $("person-body");
  body.replaceChildren();

  const facts = el("dl", "person-facts");
  const factRows = d.external
    ? [["email", "Email"], ["org", "Organization"], ["slack_id", "Slack ID"],
       ["timezone", "Timezone"], ["first_seen", "First seen"]]
    : [["login", "Login"], ["manager", "Manager"], ["startDate", "Started"],
       ["timezone", "Timezone"], ["employeeNumber", "Employee #"], ["discordid", "Discord"]];
  for (const [k, label] of factRows) {
    if (!d[k]) continue;
    facts.appendChild(el("dt", null, label));
    facts.appendChild(el("dd", null, String(d[k])));
  }
  if (facts.childElementCount) body.appendChild(facts);

  const meta = d.meta || {};
  if (meta.last_contact || meta.cadence || meta.circle) {
    const rel = el("div", "person-rel");
    for (const [k, label] of [["last_contact", "Last real conversation"],
                              ["cadence", "Reach out"], ["circle", "Circle"]]) {
      if (!meta[k]) continue;
      const item = el("div", "person-rel-i");
      item.appendChild(el("span", "person-rel-l", label));
      item.appendChild(el("strong", null, meta[k]));
      rel.appendChild(item);
    }
    body.appendChild(rel);
  }

  /* Strip the frontmatter and the otto:meta block: both are already rendered above,
   * as chips and as the relationship strip. Comments are guidance for whoever edits
   * the file and must never reach the screen -- they were half the visible content
   * before this rewrite. */
  const md = String(d.markdown || "")
    .replace(/^---[\s\S]*?\n---\n?/, "")
    .replace(/<!--\s*otto:meta[\s\S]*?-->/g, "")
    .replace(/<!--[\s\S]*?-->/g, "");

  /* Group by heading so a section with nothing under it can be dropped entirely
   * rather than rendered as a lonely label. */
  const groups = [];
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const h = line.match(/^#{1,4}\s+(.*)$/);
    if (h) { groups.push({ heading: h[1].trim(), lines: [] }); continue; }
    if (!groups.length) groups.push({ heading: null, lines: [] });
    groups[groups.length - 1].lines.push(line);
  }

  let wrote = 0;
  for (const g of groups) {
    const prose = g.lines.filter(l => !/^_Seeded from/.test(l));
    if (!prose.length) continue;
    if (g.heading) body.appendChild(el("h3", "person-h", g.heading));
    for (const l of prose) {
      body.appendChild(el("p", "person-p", l.replace(/\*\*/g, "").replace(/^_|_$/g, "")));
    }
    wrote++;
  }

  /* Empty sections are hidden, so an untouched dossier would otherwise show nothing
   * at all. Say so, and say what the sections are, so the blank state is a prompt. */
  if (!wrote) {
    const empty = el("div", "person-empty");
    empty.appendChild(el("p", "person-p", "Nothing written down yet."));
    empty.appendChild(el("p", "person-hint",
      "Sections ready in the file: " + sectionNames().join(" · ")));
    body.appendChild(empty);
  }

  const schema = await loadPeopleSchema();
  if (schema) body.appendChild(personEditor(d, schema));

  const prov = String(d.markdown || "").match(/^_Seeded from.*$/m);
  $("person-path").textContent = (prov ? "seeded from memory · " : "") + `${d.slug}.md`;
  const fb = $("person-focus");
  const focused = personSel && personSel.slug === d.slug;
  fb.replaceChildren(ico("ph ph-user-focus"), el("span", null, focused ? "Stop focusing" : "Focus everywhere"));
  fb.onclick = () => { setPerson(focused ? null : d.slug, shown); closePerson(); };
  $("person-overlay").hidden = false;
}

/* Section list and editable-field contract both come from the daemon rather than being
 * duplicated here. The hardcoded copy this replaces had drifted: it still listed seven
 * sections after "What we work on" was added to people.py, so the empty-state message
 * was quietly wrong about what the file contains. */
let peopleSchema = null;
const SECTION_FALLBACK = ["Who they are", "What we work on", "Working style",
                          "Relationship", "Threads", "Devices", "Known issues", "Baselines"];
function sectionNames() {
  return (peopleSchema && peopleSchema.sections) || SECTION_FALLBACK;
}

async function loadPeopleSchema() {
  if (peopleSchema) return peopleSchema;
  try { peopleSchema = await api("/api/people/meta/schema"); }
  catch (e) { toast(e.message, true); }
  return peopleSchema;
}

async function patchPerson(slug, body) {
  const cmd = body.note
    ? `otto people note ${slug} --section "${body.section}" "…"`
    : `PATCH /api/people/${slug} ${JSON.stringify(body.meta || body)}`;
  const d = await act(cmd, `/api/people/${encodeURIComponent(slug)}`, {
    method: "PATCH", headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  /* The roster caches display_name, pronouns and has_notes, so without this the list
   * behind the modal shows the old value until the next poll. */
  if (peopleRows) {
    const i = peopleRows.findIndex((p) => p.slug === slug);
    if (i >= 0) peopleRows[i] = { ...peopleRows[i], ...d, markdown: undefined };
  }
  return d;
}

/* Meta fields commit on change. A note commits on its own button, so a half-typed
 * thought is never written to a colleague's file. */
function personEditor(d, schema) {
  const wrap = el("div", "person-edit");
  wrap.appendChild(el("h3", "person-h", "Edit"));

  const grid = el("div", "person-edit-grid");
  for (const f of schema.fields) {
    const lab = el("label", "person-edit-f");
    lab.appendChild(el("span", "person-rel-l", f.key.replace(/_/g, " ")));
    const inp = el("input", "input sm");
    inp.type = f.key === "last_contact" ? "date" : "text";
    inp.value = (d.meta || {})[f.key] || "";
    inp.title = f.hint;
    if (f.key === "pronouns") {
      inp.placeholder = "they/them if blank";
      inp.setAttribute("list", "pronoun-options");
      inp.autocomplete = "off";
    }
    let last = inp.value;
    inp.addEventListener("change", async () => {
      if (inp.value === last) return;
      const val = inp.value;
      try {
        await patchPerson(d.slug, { meta: { [f.key]: val } });
        last = val;
        toast(`${f.key.replace(/_/g, " ")} ${val ? "set" : "cleared"}`);
        openPerson(d.slug);
        if (mode === "plane" && planeTab === "people") render();
      } catch (e) { toast(e.message, true); inp.value = last; }
    });
    lab.appendChild(inp);
    grid.appendChild(lab);
  }
  wrap.appendChild(grid);

  const dl = el("datalist");
  dl.id = "pronoun-options";
  for (const p of schema.pronoun_suggestions || []) dl.appendChild(el("option", null, p));
  wrap.appendChild(dl);

  const sel = el("select", "input sm");
  for (const s of sectionNames()) {
    const o = el("option", null, s);
    o.value = s;
    sel.appendChild(o);
  }
  /* Set the select's value directly rather than marking an option `selected`. Both work
   * in a browser, but only this is unambiguous -- and the DOM harness cannot reflect
   * option.selected into select.value, so the other form made the test pass a section
   * of "" while looking correct. */
  const DEFAULT_SECTION = "Relationship";        // the section this feature exists for
  if (sectionNames().includes(DEFAULT_SECTION)) sel.value = DEFAULT_SECTION;
  const ta = el("textarea", "input");
  ta.rows = 3;
  const dated = (schema.dated_sections || []).join(", ");
  ta.placeholder = dated
    ? `Add a note. ${dated} get today's date automatically.`
    : "Add a note.";
  const btn = el("button", "btn btn-primary btn-sm", "Add note");
  btn.type = "button";
  btn.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { toast("nothing to add", true); return; }
    btn.disabled = true;
    try {
      await patchPerson(d.slug, { note: text, section: sel.value });
      ta.value = "";
      toast(`note added to ${sel.value}`);
      openPerson(d.slug);
      if (mode === "plane" && planeTab === "people") render();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });
  const row = el("div", "person-note-row");
  row.appendChild(sel);
  row.appendChild(btn);
  const box = el("div", "person-note-box");
  box.appendChild(ta);
  box.appendChild(row);
  wrap.appendChild(box);
  return wrap;
}

function closePerson() { $("person-overlay").hidden = true; }

