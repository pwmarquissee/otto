/* otto/web/js/55-writing.js: the Writing view: post ideas, drafts, the confidentiality scan, voice.
 * Relies on: 00-shell.js, 10-helpers.js. */

/* ============================== writing ============================== */

async function loadWriting() {
  writingLoading = true;
  writingError = null;
  try {
    writingData = await api("/api/writing");
    writingLoadedAt = Date.now();
    writingSig = JSON.stringify([
      (writingData.posts || []).map((p) => [p.id, p.status, p.updated]),
      (writingData.running || []).map((r) => r.id),
    ]);
  } catch (e) {
    writingError = /404|not found/i.test(e.message)
      ? "This daemon was started before the writing API existed. Restart it: otto stop, then otto serve."
      : e.message;
  }
  writingLoading = false;
  if (mode === "writing" || splitMode === "writing") render(true);
}

/* One request against a post, with the button state and the reload handled once. */
async function writingAct(postId, cmd, path, opts) {
  writingBusy = postId;
  render(true);
  try {
    await act(cmd, path, opts);
    await loadWriting();
  } catch (e) {
    toast(e.message, true);
  } finally {
    writingBusy = null;
    render(true);
  }
}

function writingPatch(postId, cmd, body) {
  return writingAct(postId, cmd, "/api/writing/" + postId, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
}

function themeChips(p) {
  const row = el("span", "wr-themes");
  for (const t of p.themes || []) row.appendChild(el("span", "chip-sm wr-theme", t));
  if (p.energy === "low") {
    const q = el("span", "chip-sm wr-quick", "quick");
    q.title = "Writable in about fifteen minutes: the story is simple and already in your head.";
    row.appendChild(q);
  }
  return row;
}

function writingIdeaCard(p) {
  const card = el("article", "notice wr-card wr-idea");
  const head = el("div", "notice-head");
  head.appendChild(themeChips(p));
  head.appendChild(el("span", "spacer"));
  head.appendChild(ageEl(p.created, "mono-dim", p.id + " · "));
  card.appendChild(head);
  card.appendChild(el("h3", null, p.hook));
  if (p.stance || p.pushback || p.question) {
    const arg = el("div", "wr-arg");
    if (p.stance) {
      const r = el("div", "wr-arg-row");
      r.appendChild(el("span", "wr-lbl", "stance"));
      r.appendChild(el("span", null, p.stance));
      arg.appendChild(r);
    }
    if (p.pushback) {
      const r = el("div", "wr-arg-row vs");
      r.appendChild(el("span", "wr-lbl", "pushback"));
      r.appendChild(el("span", null, p.pushback));
      arg.appendChild(r);
    }
    if (p.question) {
      const r = el("div", "wr-arg-row");
      r.appendChild(el("span", "wr-lbl", "leaves them"));
      r.appendChild(el("span", null, p.question));
      arg.appendChild(r);
    }
    card.appendChild(arg);
  }
  card.appendChild(el("p", "wr-angle", p.angle));
  if ((p.evidence || []).length) {
    const ev = el("div", "wr-evidence");
    ev.appendChild(el("span", "wr-lbl", "rests on"));
    for (const e of p.evidence) ev.appendChild(el("span", "wr-ev", "“" + e + "”"));
    card.appendChild(ev);
  }
  if ((p.reveals || []).length) {
    const rv = el("div", "wr-evidence");
    rv.appendChild(el("span", "wr-lbl", "would have to hide"));
    for (const r of p.reveals) rv.appendChild(el("span", "wr-ev warn", r));
    card.appendChild(rv);
  }
  if (p.error) card.appendChild(el("p", "wr-error", "Last draft attempt: " + p.error));

  const acts = el("div", "notice-acts");
  const draft = el("button", "btn btn-primary btn-xs");
  draft.type = "button";
  draft.appendChild(ico("ph ph-pen-nib"));
  draft.appendChild(el("span", null, "Draft it"));
  draft.disabled = writingBusy === p.id;
  draft.addEventListener("click", () => writingAct(p.id, "otto writing draft " + p.id,
    "/api/writing/" + p.id + "/draft", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }));
  acts.appendChild(draft);
  const drop = el("button", "btn btn-secondary btn-xs", "Drop");
  drop.type = "button";
  drop.title = "Not this one. Kept, so it is not proposed again.";
  drop.disabled = writingBusy === p.id;
  drop.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status dropped", { status: "dropped" }));
  acts.appendChild(drop);
  card.appendChild(acts);
  return card;
}

function writingDraftCard(p) {
  const drafting = p.status === "drafting";
  const flags = p.flags || [];
  const card = el("article", "notice wr-card wr-draft" + (flags.length ? " lv-warn" : ""));
  const head = el("div", "notice-head");
  head.appendChild(themeChips(p));
  if (drafting) head.appendChild(pill("drafting", "running"));
  else if (flags.length) {
    const c = el("span", "chip-sm chip-seen", flags.length + " to look at");
    c.title = "The scan found things to decide on before this goes out. Listed below.";
    head.appendChild(c);
  } else {
    const c = el("span", "chip-sm wr-clean", "nothing flagged");
    c.title = "The scan found nothing. Not the same as safe: read it once as a stranger.";
    head.appendChild(c);
  }
  head.appendChild(el("span", "spacer"));
  const words = p.draft ? p.draft.split(/\s+/).filter(Boolean).length : 0;
  head.appendChild(ageEl(p.updated, "mono-dim",
    p.id + (words ? " · " + words + " words" : "") + (p.cost_usd ? " · " + usd(p.cost_usd) : "") + " · "));
  card.appendChild(head);
  card.appendChild(el("h3", null, p.hook));

  if (drafting) {
    card.appendChild(el("p", "sec-empty", "Otto is writing. This card updates itself when the draft lands."));
    return card;
  }

  const editing = writingEditing === p.id;
  if (editing) {
    const ta = el("textarea", "input wr-edit");
    ta.value = writingEdits[p.id] != null ? writingEdits[p.id] : (p.draft || "");
    ta.rows = Math.max(8, (ta.value.match(/\n/g) || []).length + 3);
    ta.addEventListener("input", () => { writingEdits[p.id] = ta.value; });
    card.appendChild(ta);
  } else {
    const body = el("div", "wr-body", p.draft || "");
    if (p.edited) {
      const tag = el("div", "wr-edited");
      tag.appendChild(ico("ph ph-pencil-simple"));
      tag.appendChild(el("span", null, "your edit, not yet sent back"));
      card.appendChild(tag);
    }
    card.appendChild(body);
  }

  if ((p.alt_hooks || []).length) {
    const alts = el("div", "wr-evidence");
    alts.appendChild(el("span", "wr-lbl", "other first lines"));
    for (const h of p.alt_hooks) alts.appendChild(el("span", "wr-ev", h));
    card.appendChild(alts);
  }

  if (flags.length) {
    const fl = el("div", "wr-flags");
    fl.appendChild(el("strong", "sm", "Before it goes out"));
    for (const f of flags) {
      const row = el("div", "wr-flag");
      row.appendChild(el("span", "wr-flag-kind", f.kind));
      row.appendChild(el("code", null, f.match));
      row.appendChild(el("span", "wr-flag-why", f.why));
      fl.appendChild(row);
    }
    card.appendChild(fl);
  }
  if ((p.notes || []).length) {
    const ns = el("div", "wr-evidence");
    ns.appendChild(el("span", "wr-lbl", "you asked for"));
    for (const n of p.notes) ns.appendChild(el("span", "wr-ev", n.text));
    card.appendChild(ns);
  }
  if (p.error) card.appendChild(el("p", "wr-error", "Last redraft attempt: " + p.error));

  const busy = writingBusy === p.id;
  const acts = el("div", "notice-acts");

  const cp = el("button", "btn btn-primary btn-xs");
  cp.type = "button";
  cp.appendChild(ico("ph ph-copy"));
  cp.appendChild(el("span", null, "Copy"));
  cp.addEventListener("click", () => { copy(p.draft || ""); echo("otto writing show " + p.id, "draft copied"); });
  acts.appendChild(cp);

  if (editing) {
    const edited = () => (writingEdits[p.id] != null ? writingEdits[p.id] : (p.draft || "")).trim();
    const save = el("button", "btn btn-primary btn-xs", "Save my edit");
    save.type = "button";
    save.disabled = busy;
    save.title = "Stored as your text and re-scanned. The next draft run keeps your changes.";
    save.addEventListener("click", () => {
      const text = edited();
      writingEditing = null;
      if (text === (p.draft || "").trim()) { render(true); return; }
      writingPatch(p.id, "otto writing edit " + p.id, { draft: text }).then(() => { delete writingEdits[p.id]; });
    });
    acts.appendChild(save);
    const both = el("button", "btn btn-secondary btn-xs", "Save and redraft…");
    both.type = "button";
    both.disabled = busy;
    both.title = "Store your edit, then open the note box so Otto takes another pass on YOUR version.";
    both.addEventListener("click", async () => {
      const text = edited();
      writingEditing = null;
      writingOpenNote = p.id;
      if (text !== (p.draft || "").trim()) {
        await writingPatch(p.id, "otto writing edit " + p.id, { draft: text });
        delete writingEdits[p.id];
      } else render(true);
    });
    acts.appendChild(both);
    const cancel = el("button", "btn btn-secondary btn-xs", "Discard");
    cancel.type = "button";
    cancel.addEventListener("click", () => { writingEditing = null; delete writingEdits[p.id]; render(true); });
    acts.appendChild(cancel);
    card.appendChild(acts);
    return card;
  }

  const ed = el("button", "btn btn-secondary btn-xs");
  ed.type = "button";
  ed.appendChild(ico("ph ph-pencil-simple"));
  ed.appendChild(el("span", null, "Edit"));
  ed.disabled = busy;
  ed.addEventListener("click", () => { writingEditing = p.id; writingOpenNote = null; writingUrlFor = null; render(true); });
  acts.appendChild(ed);

  const redo = el("button", "btn btn-secondary btn-xs", writingOpenNote === p.id ? "Cancel" : "Redraft…");
  redo.type = "button";
  redo.disabled = busy;
  redo.addEventListener("click", () => { writingOpenNote = writingOpenNote === p.id ? null : p.id; writingUrlFor = null; render(true); });
  acts.appendChild(redo);

  const posted = el("button", "btn btn-secondary btn-xs", writingUrlFor === p.id ? "Cancel" : "Posted…");
  posted.type = "button";
  posted.disabled = busy;
  posted.title = "You published it. Record the link so the next drafts can match it.";
  posted.addEventListener("click", () => { writingUrlFor = writingUrlFor === p.id ? null : p.id; writingOpenNote = null; render(true); });
  acts.appendChild(posted);

  const drop = el("button", "btn btn-secondary btn-xs", "Drop");
  drop.type = "button";
  drop.disabled = busy;
  drop.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status dropped", { status: "dropped" }));
  acts.appendChild(drop);
  card.appendChild(acts);

  if (writingOpenNote === p.id) {
    const box = el("div", "wr-box");
    const ta = el("textarea", "input");
    ta.rows = 3;
    ta.placeholder = p.edited
      ? "Another pass on YOUR version. \"keep my cuts, tighten the middle\" · \"finish the thought in the last paragraph\" · or leave empty and it just continues where you left it"
      : "What to change, in your words. \"tighter\" · \"less preachy\" · \"lead with the failure\" · \"lose the second paragraph\"";
    ta.value = writingNotes[p.id] || "";
    ta.addEventListener("input", () => { writingNotes[p.id] = ta.value; });
    box.appendChild(ta);
    const go = el("button", "btn btn-primary btn-xs", "Redraft with this note");
    go.type = "button";
    go.disabled = busy;
    go.addEventListener("click", () => {
      const note = (writingNotes[p.id] || "").trim();
      writingOpenNote = null;
      writingAct(p.id, "otto writing draft " + p.id + (note ? " --note \"…\"" : ""),
        "/api/writing/" + p.id + "/draft", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ note: note || null }) })
        .then(() => { delete writingNotes[p.id]; });
    });
    box.appendChild(go);
    card.appendChild(box);
  }
  if (writingUrlFor === p.id) {
    const box = el("div", "wr-box");
    const inp = el("input", "input");
    inp.placeholder = "the post's URL (optional)";
    inp.spellcheck = false;
    box.appendChild(inp);
    const go = el("button", "btn btn-primary btn-xs", "Mark posted");
    go.type = "button";
    go.disabled = busy;
    go.addEventListener("click", () => {
      const url = inp.value.trim();
      writingUrlFor = null;
      writingPatch(p.id, "otto writing set " + p.id + " --status posted" + (url ? " --url …" : ""), { status: "posted", url: url || null });
    });
    box.appendChild(go);
    card.appendChild(box);
  }
  return card;
}

function writingPostedRow(p) {
  const row = el("div", "wr-posted");
  row.appendChild(el("span", "mono-dim", (p.posted_at || p.updated || "").slice(0, 10)));
  row.appendChild(el("span", "wr-posted-hook", p.hook));
  if (p.url) {
    const a = el("a", "cmd-btn", "open");
    a.href = p.url; a.target = "_blank"; a.rel = "noopener";
    row.appendChild(a);
  }
  const show = el("button", "cmd-btn", "otto writing show " + p.id);
  show.type = "button";
  show.addEventListener("click", () => copy("otto writing show " + p.id));
  row.appendChild(show);
  return row;
}

function viewWriting() {
  const wrap = el("div", "today-wrap wr-wrap");
  const W = writingData;

  if (!W && !writingError) {
    if (!writingLoading) loadWriting();
    wrap.appendChild(el("p", "sec-empty", "Reading…"));
    return wrap;
  }
  if (writingError) {
    wrap.appendChild(loadFailed(writingError, loadWriting));
    return wrap;
  }

  const posts = W.posts || [];
  const running = W.running || [];
  const ideasRunning = running.some((r) => r.kind === "ideas");

  /* -- the lede: what this is, and the one button -- */
  const top = el("div", "wr-top");
  const lede = el("div", "wr-lede");
  lede.appendChild(el("h2", null, "Things worth saying, from the week you actually had"));
  const sched = W.schedule || {};
  const cad = sched.cadence || {};
  const when = cad.kind === "weekly" ? `${(cad.days || []).join(",")} ${cad.at}` : (cad.kind || "unscheduled");
  lede.appendChild(el("p", null,
    "Otto mines your transcripts, decisions and notices for post ideas (" + when
    + (sched.autostart && sched.enabled ? ", on its own" : ", by hand") + "), drafts the one you pick in your "
    + "voice, and flags what a stranger should not learn from it. Nothing is ever marked ready by Otto; "
    + "you post it, you say so."));
  const vp = el("p", "wr-voice");
  vp.appendChild(el("span", "mono-dim", W.voice_set ? "voice: " : "voice (still the seed, edit it): "));
  vp.appendChild(pathEl(W.voice_path));
  lede.appendChild(vp);
  top.appendChild(lede);

  const ctl = el("div", "wr-ctl");
  const mine = el("button", "btn btn-primary btn-sm");
  mine.type = "button";
  mine.appendChild(ico("ph ph-sparkle"));
  mine.appendChild(el("span", null, ideasRunning ? "Mining…" : "Ideas from this week"));
  mine.disabled = ideasRunning || writingBusy === "ideas";
  mine.addEventListener("click", () => writingAct("ideas", "otto writing ideas", "/api/writing/ideas", { method: "POST" }));
  ctl.appendChild(mine);
  if (W.last_ideas) {
    ctl.appendChild(el("span", "mono-dim", "last mined " + age(W.last_ideas.at)
      + (W.last_ideas.cost_usd ? " · " + usd(W.last_ideas.cost_usd) : "")));
  }
  top.appendChild(ctl);
  wrap.appendChild(top);

  /* -- drafts first: they are the thing waiting on you -- */
  const drafts = posts.filter((p) => p.status === "drafted" || p.status === "drafting");
  const ideas = posts.filter((p) => p.status === "idea");
  const done = posts.filter((p) => p.status === "posted");
  const dropped = posts.filter((p) => p.status === "dropped");

  const dsec = el("section", "sec wr-sec");
  dsec.appendChild(secHead("Drafts", drafts.length ? String(drafts.length) : ""));
  if (drafts.length) for (const p of drafts) dsec.appendChild(writingDraftCard(p));
  else dsec.appendChild(el("p", "sec-empty", ideas.length
    ? "Nothing drafted. Pick an idea below and Otto writes the first pass."
    : "Nothing drafted yet."));
  wrap.appendChild(dsec);

  const isec = el("section", "sec wr-sec");
  isec.appendChild(secHead("Ideas", ideas.length ? String(ideas.length) : ""));
  if (ideas.length) for (const p of ideas) isec.appendChild(writingIdeaCard(p));
  else {
    const e = el("p", "sec-empty");
    e.textContent = ideasRunning
      ? "Otto is reading the week. Ideas land here in a minute or two."
      : "No ideas on the list. The weekly run fills this on its own; the button above does it now.";
    isec.appendChild(e);
  }
  wrap.appendChild(isec);

  if (done.length) {
    const psec = el("section", "sec wr-sec");
    psec.appendChild(secHead("Posted", String(done.length)));
    for (const p of done) psec.appendChild(writingPostedRow(p));
    wrap.appendChild(psec);
  }
  if (dropped.length) {
    const more = el("div", "more-row");
    const b = el("button", "cmd-btn", (writingShowDropped ? "hide " : "show ") + dropped.length + " dropped");
    b.type = "button";
    b.addEventListener("click", () => { writingShowDropped = !writingShowDropped; render(true); });
    more.appendChild(b);
    wrap.appendChild(more);
    if (writingShowDropped) {
      const xsec = el("section", "sec wr-sec");
      for (const p of dropped) {
        const row = el("div", "wr-posted dim");
        row.appendChild(el("span", "mono-dim", (p.updated || "").slice(0, 10)));
        row.appendChild(el("span", "wr-posted-hook", p.hook));
        const back = el("button", "cmd-btn", "back to ideas");
        back.type = "button";
        back.addEventListener("click", () => writingPatch(p.id, "otto writing set " + p.id + " --status idea", { status: "idea" }));
        row.appendChild(back);
        xsec.appendChild(row);
      }
      wrap.appendChild(xsec);
    }
  }
  return wrap;
}

/* What the inspector reads for the current selection, so it rebuilds only when
 * that changed. The selected record itself is serialized (a few KB at most); the
 * caches it draws on are cheap identities. */
function inspectorSignature() {
  const s = state || {};
  if (!sel) return "none";
  let item = null;
  if (sel.type === "task") {
    item = [allCards().find((k) => k.id === sel.id), (s.tasks || []).find((t) => t.id === sel.id),
      detailCache[sel.id], s.logistics, s.dispatch, s.sessions];
  } else if (sel.type === "run") {
    item = [(s.live || []).find((x) => x.id === sel.id), (s.runs || []).find((x) => x.id === sel.id),
      activitySig, planCache[sel.id], timelineOpen, eventsOpen, s.events, (s.repos || null)];
  } else if (sel.type === "sched") {
    item = [(s.schedules || []).find((x) => x.name === sel.id), s.known, s.autorun,
      (s.runs || []).filter((r) => r.name === sel.id).map((r) => r.id + r.status)];
  } else if (sel.type === "session") {
    item = [sessionRow(sel.id), s.logistics, ledgerDetail[sel.id], dispatchPickOpen, s.tasks ? s.tasks.length : 0];
  }
  return JSON.stringify([sel, item, personSel && personSel.slug, scope, domain, repoData && repoData.repos && repoData.repos.length]);
}

function renderInspector(force) {
  const host = $("inspector");
  const sig = inspectorSignature();
  if (!force && sig === inspSig) return;
  inspSig = sig;
  host.replaceChildren();
  const s = state || {};

  let body = null;
  if (sel && sel.type === "run") body = inspectRun(s);
  else if (sel && sel.type === "task") body = inspectTask(s);
  else if (sel && sel.type === "sched") body = inspectSched(s);
  else if (sel && sel.type === "session") body = inspectSession(s);

  /* Selection-only. With nothing selected the pane is EMPTY and CSS collapses the
   * whole column, giving Today the full width. The brakes used to live here and were
   * permanent furniture: useful, but settings, and they cost real estate on every
   * screen. They are in Control plane > System now. */
  host.dataset.idle = body ? "0" : "1";
  if (body) host.appendChild(body);
}

/* The brakes, the armed list, and the daemon warning. Settings-shaped content: read
 * once, checked occasionally, never needed while working. Lives in Control plane. */
function planeBrakes(s) {
  const wrap = el("div", "brakes-pane");

  wrap.appendChild(el("h3", "sys-h3", "The brakes"));
  wrap.appendChild(el("p", "insp-lede",
    "Unattended is the dangerous mode, so every guard is consulted before an autorun. "
    + "Nothing here is hidden behind a settings page."));

  const auto = s.autorun || {}, dsp = s.dispatch || {};
  const lookup = { autorun: auto, dispatch: dsp };
  for (const [name, path, why] of BRAKES) {
    const [obj, field] = path.split(".");
    let v = lookup[obj] ? lookup[obj][field] : undefined;
    let fg = "var(--color-text)";
    if (typeof v === "boolean") { fg = v ? OK : CRIT; v = v ? "on" : "OFF"; }
    else if (field.endsWith("usd")) { fg = WARN; v = v == null ? "unset" : "$" + v; }
    else if (field === "max_failures") v = v + " fails";
    else if (field === "min_gap_minutes") v = v + " min";
    if (v == null) v = "—";
    const row = el("div", "brake");
    const left = el("div");
    left.appendChild(el("strong", null, name));
    left.appendChild(el("p", null, why));
    row.appendChild(left);
    const val = el("span", "brake-val", String(v));
    val.style.color = fg;
    row.appendChild(val);
    wrap.appendChild(row);
  }

  /* Weekend policy sits with the brakes because it is the same kind of thing: a
   * standing rule that decides whether something is allowed to run. */
  const wk = el("div", "brake");
  const wl = el("div");
  wl.appendChild(el("strong", null, "Weekends"));
  wl.appendChild(el("p", null,
    "Work schedules never fire Sat/Sun and work notices do not toast. Personal runs "
    + "normally. Staleness excludes weekend hours, so a paused loop is not an alarm. "
    + "crit still reaches you."));
  wk.appendChild(wl);
  const wv = el("span", "brake-val", "life only");
  wv.style.color = ACCENT;
  wk.appendChild(wv);
  wrap.appendChild(wk);

  const armed = auto.armed || [];
  const h = el("h3", "sys-h3", "Armed");
  h.style.marginTop = "var(--space-8)";
  wrap.appendChild(h);
  wrap.appendChild(el("p", "insp-lede",
    "An enabled schedule is never auto-run. Arming is explicit, and this is the whole list."));
  const chips = el("div", "armed-chips");
  for (const a of armed) {
    const c = el("span", "armed-chip");
    c.appendChild(ico("ph ph-lightning"));
    c.appendChild(el("span", null, a.name || a));
    if (a.cadence) c.appendChild(el("span", "cad", cadenceText(a)));
    chips.appendChild(c);
  }
  if (!armed.length) chips.appendChild(el("p", "sec-empty", "Nothing is armed."));
  wrap.appendChild(chips);

  const stop = el("div", "callout");
  stop.style.marginTop = "var(--space-8)";
  stop.appendChild(el("strong", null, "Stopping the daemon"));
  const pp = el("p");
  pp.appendChild(el("span", null, "Use "));
  pp.appendChild(el("code", null, "otto stop"));
  pp.appendChild(el("span", null,
    ". Every spawned agent is a child of the daemon, and a tree kill takes them with "
    + "it, silently, as orphaned."));
  stop.appendChild(pp);
  wrap.appendChild(stop);
  return wrap;
}

/* Repo context when scoped. Moved out of the inspector so the inspector can be
 * purely "what you clicked". */
function repoPanel(s) {
  const r = (repoData ? repoData.repos : []).find((x) => x.key === scope);
  if (!r) return null;
  const box = el("section", "sec repo-panel");
  const hd = el("div", "repo-hd");
  hd.appendChild(ico("ph ph-folder-open"));
  hd.appendChild(el("h2", null, r.name));
  hd.appendChild(domTag(r.domain));
  box.appendChild(hd);
  box.appendChild(el("p", "repo-path", r.path));
  const boardTotal = allCards().filter(keep).length;
  const live = (s.live || []).filter(keep).length;
  const stats = el("div", "repo-stats");
  for (const [k, v, fg] of [
    ["definitions", r.defs, "var(--color-text)"],
    ["open work", boardTotal, boardTotal ? WARN : "var(--color-text)"],
    ["in flight", live, live ? ACCENT : "var(--color-text)"],
    ["last run", r.last, "var(--color-text)"],
  ]) {
    const cell = el("div", "repo-stat");
    const vv = k === "last run" ? el("div", "v") : el("div", "v", String(v));
    if (k === "last run") vv.appendChild(ageEl(v, null));
    vv.style.color = fg;
    cell.appendChild(vv);
    cell.appendChild(el("div", "k", k));
    stats.appendChild(cell);
  }
  box.appendChild(stats);
  return box;
}

function inspectRun(s) {
  const live = (s.live || []).find((x) => x.id === sel.id);
  const rec = live || (s.runs || []).find((x) => x.id === sel.id);
  if (!rec) return null;

  const wrap = el("div");
  const head = el("div", "insp-head");
  const title = el("div", "insp-title");
  if (live) title.appendChild(el("span", "spin"));
  title.appendChild(el("h2", null, rec.name));
  const pp = permPill(rec.permissions);
  if (pp) title.appendChild(pp);
  title.appendChild(el("span", "spacer"));
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  title.appendChild(x);
  head.appendChild(title);

  const events = activityCache[rec.id];
  const bits = [];
  if (rec.model) bits.push(rec.model);
  if (events) bits.push(events.filter((e) => e.kind === "tool").length + " tool calls");
  if (rec.subtasks_started) bits.push(`${rec.subtasks_done || 0}/${rec.subtasks_started} sub-tasks`);
  if (rec.output_tokens) bits.push(kfmt(rec.output_tokens) + " out");
  if (rec.cost_usd != null) bits.push(money(rec.cost_usd));
  if (rec.pid) bits.push("pid " + rec.pid);
  if (rec.cwd) {
    const sub = el("p", "insp-sub");
    sub.textContent = bits.join("  ·  ") + (bits.length ? "  ·  " : "");
    sub.appendChild(pathEl(rec.cwd));
    head.appendChild(sub);
  } else {
    head.appendChild(el("p", "insp-sub", bits.join("  ·  ")));
  }

  const acts = el("div", "insp-acts");
  const logB = el("button", "btn btn-secondary btn-xs");
  logB.type = "button";
  logB.appendChild(ico("ph ph-eye"));
  logB.appendChild(el("span", null, "Raw log"));
  logB.title = "otto logs " + rec.id.slice(0, 6);
  logB.addEventListener("click", () => {
    echo("otto logs " + rec.id.slice(0, 6), "opened the captured log");
    window.open(`/api/runs/${rec.id}/log`, "_blank");
  });
  acts.appendChild(logB);
  if (live) {
    const stopB = el("button", "btn btn-danger btn-xs");
    stopB.type = "button";
    stopB.appendChild(ico("ph ph-stop"));
    stopB.appendChild(el("span", null, "Stop"));
    stopB.addEventListener("click", async () => {
      stopB.disabled = true;
      try {
        await act("otto kill " + rec.id.slice(0, 6), `/api/runs/${rec.id}/kill`, { method: "POST" });
        toast("stopped " + rec.name);
        await poll();
      } catch (e) { toast(e.message, true); } finally { stopB.disabled = false; }
    });
    acts.appendChild(stopB);
  }
  /* A finished plan run has read and proposed; the next step is the same card at
   * full permissions, through the gate, in one click (otto task yolo). */
  if (!live && rec.permissions === "plan" && rec.task_id && rec.status === "ok") {
    const yb = el("button", "btn btn-primary btn-xs");
    yb.type = "button";
    yb.appendChild(ico("ph ph-lightning"));
    yb.appendChild(el("span", null, "Run again in yolo"));
    yb.title = "otto task yolo " + rec.task_id.slice(0, 6);
    yb.addEventListener("click", async () => {
      yb.disabled = true;
      try {
        const r = await act("otto task yolo " + rec.task_id.slice(0, 6), `/api/tasks/${rec.task_id}/yolo`, { method: "POST" });
        toast(r && r.run ? "running in yolo" : "approved, queued");
        await poll();
      } catch (e) { toast(e.message, true); } finally { yb.disabled = false; }
    });
    acts.appendChild(yb);
  }
  if (!live && (rec.status === "failed" || rec.status === "orphaned") && !rec.reviewed_at) {
    const ab = el("button", "btn btn-secondary btn-xs");
    ab.type = "button";
    ab.appendChild(ico("ph ph-check"));
    ab.appendChild(el("span", null, "Acknowledge"));
    ab.addEventListener("click", async () => {
      ab.disabled = true;
      try {
        await act("otto ack " + rec.id.slice(0, 6), `/api/runs/${rec.id}/ack`, { method: "POST" });
        toast("acknowledged");
        await poll();
      } catch (e) { toast(e.message, true); ab.disabled = false; }
    });
    acts.appendChild(ab);
  }
  head.appendChild(acts);
  wrap.appendChild(head);

  /* -- the verdict, in two lanes --
   * Live runs carry it on the /api/live body; finished ones on the run row. The
   * /api/runs/{id} fetch adds the plan. */
  const cached = planCache[rec.id] || {};
  const v = rec.verdict || cached.verdict;
  if (v) wrap.appendChild(verdictBlock(v));

  /* -- the plan --
   * Four phases, with the queued ones hollow. A 40-minute run reads against what
   * is typical for its name, not against nothing. */
  const plan = rec.plan || cached.plan;
  if (plan && plan.phases) {
    wrap.appendChild(phaseRail(plan));
    const br = budgetRow(plan, !!live);
    if (br) wrap.appendChild(br);
  } else if (!planCache[rec.id]) {
    const p = el("p", "insp-lede", "reading the plan…");
    p.style.margin = "var(--space-3) var(--space-6) 0";
    wrap.appendChild(p);
  }

  const tl = el("div", "tl");
  if (events == null) {
    tl.appendChild(el("p", "sec-empty", "reading the run's activity…"));
  } else if (!events.length) {
    /* An orphaned run genuinely has no timeline. Say that rather than showing an
     * empty rail as though the session did nothing. */
    tl.appendChild(el("p", "sec-empty", rec.status === "orphaned"
      ? "No result object was ever written. Otto does not wait on detached children, so an orphaned run has no exit code and no timeline: the outcome is unknown, not failed."
      : "This run wrote no structured activity. The raw log may still have output."));
  } else {
    /* The timeline states what it is showing. Forty of forty needs no fold; forty
     * of two hundred says so, and opens to the rest. */
    const SHOW = 40;
    const tools = events.filter((e) => e.kind === "tool").length;
    const subs = events.filter((e) => e.kind === "subtask").length;
    const counts = [`${events.length} events`, tools ? `${tools} tool calls` : null, subs ? `${subs} sub-tasks` : null]
      .filter(Boolean).join(" · ");
    if (events.length > SHOW) {
      tl.appendChild(foldHead("Timeline",
        (timelineOpen ? "all " : `last ${SHOW} of `) + counts, timelineOpen,
        () => { timelineOpen = !timelineOpen; renderInspector(); }));
    } else {
      tl.appendChild(foldHead("Timeline", counts, true, () => {}));
    }
    const shown = timelineOpen ? events : events.slice(-SHOW);
    for (const e of shown) tl.appendChild(timelineEvent(e));
  }
  wrap.appendChild(tl);
  return wrap;
}

function timelineEvent(e) {
  let badge = "", badgeFg = "color-mix(in srgb, var(--color-text) 40%, transparent)";
  let dotBg = "transparent", dotBd = "color-mix(in srgb, var(--color-text) 25%, transparent)";
  let dotSize = "7px", dotGlow = "none";
  let labelFg = "var(--color-text)";

  if (e.kind === "tool") {
    if (e.ok === true) { badge = "ok"; badgeFg = OK; dotBg = OK; dotBd = OK; }
    else if (e.ok === false) { badge = "err"; badgeFg = CRIT; dotBg = CRIT; dotBd = CRIT; }
    else {
      badge = "…"; badgeFg = ACCENT; dotBg = ACCENT; dotBd = ACCENT;
      dotSize = "9px"; dotGlow = "0 0 10px -1px var(--color-accent)";
    }
  } else if (e.kind === "result") {
    badge = e.ok ? "done" : "fail"; badgeFg = e.ok ? OK : CRIT;
    dotBg = e.ok ? OK : CRIT; dotBd = dotBg;
  } else if (e.kind === "subtask") {
    badge = "sub-task"; badgeFg = ACCENT; dotBd = ACCENT;
    labelFg = "color-mix(in srgb, var(--color-text) 70%, transparent)";
  } else if (e.kind === "init") {
    badge = "start"; dotBd = ACCENT;
  } else {
    labelFg = "color-mix(in srgb, var(--color-text) 68%, transparent)";
    dotSize = "5px";
  }

  const row = el("div", "tl-ev");
  const gut = el("div", "tl-gutter");
  const dot = el("span", "tl-dot");
  dot.style.width = dotSize; dot.style.height = dotSize;
  dot.style.background = dotBg; dot.style.borderColor = dotBd;
  dot.style.boxShadow = dotGlow;
  gut.appendChild(dot);
  gut.appendChild(el("span", "tl-line"));
  row.appendChild(gut);

  const body = el("div", "tl-body");
  const top = el("div", "tl-top");
  const lb = el("strong", "tl-label", e.label || e.kind || "");
  lb.style.color = labelFg;
  top.appendChild(lb);
  if (badge) {
    const b = el("span", "tl-badge", badge);
    b.style.color = badgeFg;
    top.appendChild(b);
  }
  body.appendChild(top);
  if (e.detail) body.appendChild(el("p", "tl-detail", e.detail));
  if (e.result) {
    const r = el("p", "tl-result", e.result);
    r.style.color = e.ok === false
      ? CRIT : "color-mix(in srgb, var(--color-text) 38%, transparent)";
    body.appendChild(r);
  }
  row.appendChild(body);
  return row;
}

function inspectTask(s) {
  const card = allCards().find((k) => k.id === sel.id);
  if (!card) return null;
  const movable = card.movable !== false;
  const [fg, bg] = stColor(card.status);

  const wrap = el("div", "insp-pad");
  const hd = el("div", "insp-hd");
  const left = el("div");
  left.style.flex = "1"; left.style.minWidth = "0";
  const tags = el("div", "tcard-tags");
  const p = el("span", "chip-sm", card.status);
  p.style.color = fg; p.style.background = bg;
  tags.appendChild(p);
  tags.appendChild(el("span", "chip-ref",
    movable ? "stored task" : "derived · " + card.kind));
  left.appendChild(tags);
  left.appendChild(el("h2", null, card.title));
  hd.appendChild(left);
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  hd.appendChild(x);
  wrap.appendChild(hd);

  if (!movable) {
    const c = el("div", "callout warn tight");
    c.appendChild(el("strong", "sm", "Derived from live state"));
    /* A failed run is the one derived card whose condition never clears, so the
       generic "fix it and it goes away" copy was actively misleading: there was
       nothing to fix and no way to dismiss it. */
    c.appendChild(el("span", null, card.kind === "run"
      ? "No stored row, so there is nothing to drag or edit. A past failure never clears on its own — use Acknowledge below to file it away, or open the run first."
      : "No stored row, so there is nothing to drag or edit. Fix the condition and this clears itself on the next poll."));
    wrap.appendChild(c);
  }
  if (card.last_error) {
    const c = el("div", "callout tight");
    c.appendChild(el("strong", "sm", "Last attempt did not succeed"));
    c.appendChild(el("span", "mono", card.last_error));
    wrap.appendChild(c);
  }
  /* The payload truncates long details; the whole text is one GET away and the
   * inspector is the place that wants it. */
  const detailText = fullDetailOf(card);
  if (detailText) wrap.appendChild(el("p", "insp-detail", detailText));
  if (needsFullDetail(card)) {
    wrap.appendChild(el("p", "mono-dim", "loading the full detail…"));
    loadFullDetail(card.id);
  }
  if (card.result) {
    wrap.appendChild(el("h3", "insp-h2", "What the run reported"));
    wrap.appendChild(el("p", "insp-pre", card.result));
  }

  const kv = [
    ["Filed by", card.origin ? card.origin + " (agent)" : (card.source || card.kind)],
    ["Times seen", card.seen_count > 1 ? card.seen_count + "×  — recurrence escalated it" : null],
    ["Reference", card.task_ref],
    ["Agent", movable ? (card.agent || "none (plain session)") : null],
    ["Directory", card.cwd],
    ["Attempts", movable ? `${card.attempts || 0} of ${(s.dispatch || {}).max_attempts || 1}` : null],
    ["Auto-dispatch", movable ? (card.auto ? "yes" : "no — parked") : null],
    ["Age", card.age],
    ["Command", card.command],
  ];
  for (const [k, v] of kv) {
    if (!v) continue;
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(el("span", "v", String(v)));
    wrap.appendChild(row);
  }

  const lgx = taskDispatchBlock(card);
  if (lgx) wrap.appendChild(lgx);

  const acts = el("div", "insp-acts");
  acts.style.marginTop = "var(--space-6)";
  if (movable) {
    /* Two buttons, two levels, named so the level is chosen rather than inherited:
     * plan reads and proposes, yolo is the full operator (models.Run.permissions). */
    const runBtn = (label, level, primary) => {
      const rb = el("button", "btn " + (primary ? "btn-primary" : "btn-secondary") + " btn-sm");
      rb.type = "button";
      rb.appendChild(ico(primary ? "ph ph-lightning" : "ph ph-play"));
      rb.appendChild(el("span", null, label));
      rb.title = PERM_TITLE[level];
      rb.addEventListener("click", async () => {
        rb.disabled = true;
        try {
          await act(`otto task run ${card.id.slice(0, 6)} --${level}`, `/api/tasks/${card.id}/dispatch?permissions=${level}`, { method: "POST" });
          toast(`dispatched ${card.id.slice(0, 6)} (${level})`);
          await poll();
        } catch (e) { toast(e.message, true); } finally { rb.disabled = false; }
      });
      return rb;
    };
    acts.appendChild(runBtn("Run (plan)", "plan", false));
    acts.appendChild(runBtn("Run (yolo)", "yolo", true));
    if (card.plan && !card.plan_approved) {
      /* The proposal is on the card; one word approves it and runs it through the gate. */
      const yb = el("button", "btn btn-primary btn-sm");
      yb.type = "button";
      yb.appendChild(ico("ph ph-check"));
      yb.appendChild(el("span", null, "Approve plan, run in yolo"));
      yb.title = "otto task yolo " + card.id.slice(0, 6);
      yb.addEventListener("click", async () => {
        yb.disabled = true;
        try {
          const r = await act(`otto task yolo ${card.id.slice(0, 6)}`, `/api/tasks/${card.id}/yolo`, { method: "POST" });
          toast(r && r.run ? "approved and running in yolo" : "approved, queued");
          await poll();
        } catch (e) { toast(e.message, true); } finally { yb.disabled = false; }
      });
      acts.appendChild(yb);
    }
  }
  if (card.command) {
    const cb = el("button", "btn btn-secondary btn-sm", "Copy command");
    cb.type = "button";
    cb.addEventListener("click", () => copy(card.command));
    acts.appendChild(cb);
  }
  if (card.run_id) {
    const lb = el("button", "btn btn-secondary btn-sm", "Open run");
    lb.type = "button";
    lb.addEventListener("click", () => select("run", card.run_id));
    acts.appendChild(lb);
  }
  /* Acknowledge. The board's only verb for a past failure, and the reason it has to
     exist: a derived card cannot be dragged to Done because there is no stored row
     to write, and the "fix the condition and it clears itself" rule does not apply
     to a run that already failed. That condition is history and never clears, so
     without this the card sits in Needs-you forever. */
  if (!movable && card.kind === "run" && card.run_id) {
    const ab = el("button", "btn btn-primary btn-sm");
    ab.type = "button";
    ab.appendChild(ico("ph ph-check"));
    ab.appendChild(el("span", null, "Acknowledge"));
    ab.addEventListener("click", async () => {
      ab.disabled = true;
      try {
        await act(`otto ack ${card.run_id.slice(0, 6)}`, `/api/runs/${card.run_id}/ack`, { method: "POST" });
        toast("acknowledged · card cleared");
        clearSelection();
        await poll();
      } catch (e) { toast(e.message, true); ab.disabled = false; }
    });
    acts.appendChild(ab);
  }
  wrap.appendChild(acts);
  return wrap;
}

function inspectSched(s) {
  const sc = (s.schedules || []).find((x) => x.name === sel.id);
  if (!sc) return null;
  const [cls, label, detail] = schedState(sc);
  const [fg, bg] = stColor(cls);

  const wrap = el("div", "insp-pad");
  const hd = el("div", "insp-hd");
  const left = el("div");
  left.style.flex = "1"; left.style.minWidth = "0";
  const tags = el("div", "tcard-tags");
  const p = el("span", "chip-sm", label);
  p.style.color = fg; p.style.background = bg;
  tags.appendChild(p);
  tags.appendChild(el("span", "chip-ref", cadenceText(sc)));
  if (sc.known_reason) tags.appendChild(knownChip(sc.known_reason));
  left.appendChild(tags);
  left.appendChild(el("h2", null, sc.name));
  hd.appendChild(left);
  const x = el("button", "insp-x");
  x.type = "button";
  x.setAttribute("aria-label", "Close the inspector");
  x.appendChild(ico("ph ph-x"));
  x.addEventListener("click", clearSelection);
  hd.appendChild(x);
  wrap.appendChild(hd);

  wrap.appendChild(el("p", "insp-detail", detail || ""));

  if (sc.known_reason) {
    const c = el("div", "callout warn tight");
    c.appendChild(el("strong", "sm", "Known failure"));
    c.appendChild(el("span", null, sc.known_reason
      + ". Its alert is demoted to info while this stands. When it succeeds on its own, Otto raises one notice telling you to clear it."));
    wrap.appendChild(c);
  }

  const kv = [
    ["Command", sc.command],
    ["Runner", sc.runner],
    ["Armed", sc.autostart ? "yes — runs unattended" : "no"],
    ["Stale after", sc.max_age_hours ? sc.max_age_hours + "h" : "no alarm set"],
    ["Last run", ageEl(sc.last_run, "v mono")],
    ["Last status", sc.last_status],
    ["Failures", sc.consecutive_failures ? String(sc.consecutive_failures) : null],
    ["Domain", sc.domain],
  ];
  for (const [k, v] of kv) {
    if (!v) continue;
    const row = el("div", "insp-kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(v instanceof Node ? v : el("span", "v mono", String(v)));
    wrap.appendChild(row);
  }

  wrap.appendChild(el("p", "insp-note",
    "Due and stale are different questions. Cadence says when it should run; the staleness limit says when silence becomes an alarm."));

  const acts = el("div", "insp-acts");
  const rb = el("button", "btn btn-primary btn-sm");
  rb.type = "button";
  rb.appendChild(ico("ph ph-play"));
  rb.appendChild(el("span", null, "Run now"));
  rb.addEventListener("click", () => launch(sc.name, rb));
  acts.appendChild(rb);
  const tb = el("button", "btn btn-secondary btn-sm", sc.enabled ? "Disable" : "Enable");
  tb.type = "button";
  tb.addEventListener("click", async () => {
    tb.disabled = true;
    try {
      await toggleSchedule(sc);
      await poll();
    } catch (e) { toast(e.message, true); } finally { tb.disabled = false; }
  });
  acts.appendChild(tb);
  /* The known-failure verb lives here because this is where the owner reads the alert
   * about to be annotated. Only offered when there is something to annotate. */
  if (sc.known_reason) {
    const kb = el("button", "btn btn-secondary btn-sm", "Clear known");
    kb.type = "button";
    kb.addEventListener("click", () => clearKnown("schedule", sc.name));
    acts.appendChild(kb);
  } else if (cls === "stale" || cls === "warn" || sc.disabled_reason || sc.consecutive_failures) {
    const kb = el("button", "btn btn-secondary btn-sm", "Mark known");
    kb.type = "button";
    kb.title = "Annotate this as a known failure, with a reason. Demotes its alert until it heals.";
    kb.addEventListener("click", () => markKnown("schedule", sc.name));
    acts.appendChild(kb);
  }
  wrap.appendChild(acts);
  return wrap;
}

