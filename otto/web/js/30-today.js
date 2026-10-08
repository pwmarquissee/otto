/* otto/web/js/30-today.js: the stream and the Today view: notices, running work, overnight schedules, the triage stream.
 * Relies on: 00-shell.js, 10-helpers.js. */

/* ============================== stream ============================== */

/* ============================== today ==============================
 *
 * Otto talking to the owner, in that order:
 *   1. what Otto chose to say          (notices, deliberate messages)
 *   2. what the day contained          (yesterday's rollup + today's check-in)
 *   3. anything running right now      (only when something is)
 *   4. what needs the owner, what is unwatched
 *
 * The old Stream led with a ranked list Otto recomputes every poll. That is a
 * report, not a conversation, and the owner said it was not interesting. Messages first
 * inverts it: the top of this view is only ever something Otto decided to raise.
 */

function viewToday() {
  const s = state;
  const wrap = el("div", "today-wrap");

  if (scope) {
    const rp = repoPanel(s);
    if (rp) wrap.appendChild(rp);
  }

  /* -- 1. from Otto -- */
  const notices = (s.notices || []).filter(inDomain);
  const unread = notices.filter((n) => !n.read_at);
  const top = el("div", "today-top");

  /* -- 0. outreach on the clock --
   * Above everything, including notices, and only when something is actually held.
   * This is the one panel in Otto with a DEADLINE: every other row here can be read an
   * hour later without changing what happens, and a held message read an hour later
   * has already gone. */
  const held = ((s.outreach || {}).items || []).filter((o) => o.state === "held");
  if (held.length) top.appendChild(outreachPanel(s, held));

  const msgs = el("section", "sec");

  /* -- the celebration --
   * A known failure that healed. The daemon posts exactly one of these per
   * annotation; it sits above the notice list in gold because it is the one kind of
   * news Otto has that REMOVES something from the board. Dismissing the notice is
   * not enough: the annotation is still lying until it is cleared. */
  for (const n of unread.filter((x) => x.source === "known-stale")) {
    msgs.appendChild(staleBanner(n));
  }

  const mh = secHead("From Otto", unread.length ? unread.length + " unread" : "");
  if (unread.length) {
    const clear = el("button", "cmd-btn", "mark all read");
    clear.type = "button";
    clear.addEventListener("click", async () => {
      try { await act("otto notices --read", "/api/notices/read-all", { method: "POST" }); await poll(); }
      catch (e) { toast(e.message, true); }
    });
    mh.appendChild(clear);
  }
  msgs.appendChild(mh);

  const SHOW = 12;
  for (const n of notices.slice(0, SHOW)) {
    const card = el("article", "notice" + (n.read_at ? " read" : "") + " lv-" + n.level);
    const head = el("div", "notice-head");
    head.appendChild(pill(n.level, n.level === "info" ? "idle" : n.level));
    head.appendChild(el("span", "notice-src", n.source));
    /* A collapsed repeat has to say so, or the dedupe reads as lost information.
     * "seen 7x" is also the more useful fact: it is how long this has been nagging. */
    if (n.seen_count > 1) {
      head.appendChild(el("span", "chip-sm chip-seen", `seen ${n.seen_count}x`));
    }
    head.appendChild(el("span", "spacer"));
    head.appendChild(ageEl(n.at, "mono-dim"));
    card.appendChild(head);
    card.appendChild(el("h3", null, n.title));
    if (n.body) {
      card.appendChild(el("p", "notice-body", n.body));
      /* Say there is more, and how much. A clamped body is indistinguishable from
       * a short one, so without this the card gives no reason to click.
       *
       * Counts ITEMS, not hidden lines. The clamp is three RENDERED lines, and a
       * long item wraps to two or three of them, so "7 more lines" would be wrong
       * by however the text happened to wrap. How many things the notice is about
       * is both stable and the thing worth knowing. */
      const count = noticeLines(n.body).length;
      const more = el("div", "notice-more");
      more.appendChild(ico("ph ph-arrows-out-simple"));
      more.appendChild(el("span", null,
        count > 1 ? count + " items · click to read" : "click to read"));
      card.appendChild(more);
    }

    /* The whole card opens the detail. The action buttons below already call
     * stopPropagation, so they keep working without opening it. */
    if (n.body) {
      card.classList.add("clickable");
      card.addEventListener("click", () => openNotice(n));
    }

    const acts = el("div", "notice-acts");
    if (n.command) {
      const c = el("button", "cmd-btn", n.command);
      c.type = "button";
      c.addEventListener("click", (e) => { e.stopPropagation(); copy(n.command); });
      acts.appendChild(c);
    }
    if (!n.read_at) {
      const r = el("button", "btn btn-secondary btn-xs", "Got it");
      r.type = "button";
      r.addEventListener("click", async (e) => {
        e.stopPropagation();
        r.disabled = true;
        try {
          await act(`POST /api/notices/${n.id.slice(0, 6)}…/read`, "/api/notices/" + n.id + "/read", { method: "POST" });
          await poll();
        } catch (err) { toast(err.message, true); r.disabled = false; }
      });
      acts.appendChild(r);
    }
    const x = el("button", "btn btn-secondary btn-xs", "Dismiss");
    x.type = "button";
    x.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await act(`DELETE /api/notices/${n.id.slice(0, 6)}…`, "/api/notices/" + n.id, { method: "DELETE" });
        await poll();
      } catch (err) { toast(err.message, true); }
    });
    acts.appendChild(x);
    card.appendChild(acts);
    msgs.appendChild(card);
  }
  /* What the list is NOT showing, stated. Twelve cards over a thirteenth is a list
   * that looks complete and is not. */
  if (notices.length > SHOW) {
    const more = el("div", "more-row");
    more.appendChild(el("span", null, `${notices.length - SHOW} older not shown · `));
    const b = el("button", "cmd-btn", "otto notices --limit " + notices.length);
    b.type = "button";
    b.addEventListener("click", () => copy("otto notices --limit " + notices.length));
    more.appendChild(b);
    msgs.appendChild(more);
  }
  if (!notices.length) {
    msgs.appendChild(el("p", "sec-empty",
      "Nothing from Otto. It speaks when a loop goes dark, a run needs you, or the "
      + "morning rollup lands. Silence here means it has nothing to raise."));
  }
  top.appendChild(msgs);

  /* -- 2. the day -- */
  const dayBox = el("section", "sec");
  const prev = (s.day_prev || {}).rollup || null;
  const ci = (s.day || {}).checkin || null;
  dayBox.appendChild(secHead("Your day",
    prev ? prev.session_count + " sessions yesterday" : ""));

  if (prev) {
    const card = el("div", "day-card");
    const wall = prev.wall_minutes || 0, att = prev.attention_minutes || 0;
    const hm = (m) => Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
    const stat = el("div", "day-stats");
    for (const [v, k, hint] of [
      [hm(wall), "engaged", "real elapsed, idle gaps removed"],
      [hm(att), "attention", "summed across sessions, so it can exceed the clock"],
      [String(prev.session_count || 0), "sessions", ""],
    ]) {
      const cell = el("div", "day-stat");
      if (hint) cell.title = hint;
      cell.appendChild(el("div", "v", v));
      cell.appendChild(el("div", "k", k));
      stat.appendChild(cell);
    }
    card.appendChild(stat);

    const byProj = Object.entries(prev.by_project || {}).filter((kv) => kv[1] >= 5);
    const max = Math.max(1, ...byProj.map((kv) => kv[1]));
    for (const [proj, mins] of byProj.slice(0, 6)) {
      const row = el("div", "day-proj");
      row.appendChild(el("span", "mins", mins + "m"));
      row.appendChild(el("span", "name", proj));
      const track = el("span", "track");
      const fill = el("span", "fill");
      fill.style.width = Math.round((mins / max) * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      card.appendChild(row);
    }
    for (const ls of (prev.long_sessions || []).slice(0, 3)) {
      const row = el("div", "day-long");
      row.appendChild(el("span", "mins", ls.minutes + "m"));
      const b = el("div");
      b.appendChild(el("div", "t", ls.title || "(untitled)"));
      /* Stated intent verbatim next to elapsed time. That pair is the point: a
       * summary massaged into agreement with the outcome would hide the drift. */
      if (ls.intent) b.appendChild(el("div", "i", ls.intent));
      row.appendChild(b);
      card.appendChild(row);
    }
    dayBox.appendChild(card);
  } else {
    dayBox.appendChild(el("p", "sec-empty",
      "No rollup for yesterday yet. It lands at 06:00, or run otto day --refresh."));
  }

  const ciBox = el("div", "checkin");
  if (ci) {
    const h = el("div", "checkin-head");
    h.appendChild(el("strong", null, "Checked in"));
    if (ci.energy) h.appendChild(el("span", "chip-sm", "energy " + ci.energy + "/5"));
    h.appendChild(el("span", "spacer"));
    h.appendChild(ageEl(ci.at, "mono-dim"));
    ciBox.appendChild(h);
    if (ci.note) ciBox.appendChild(el("p", "checkin-note", ci.note));
  } else {
    ciBox.appendChild(el("div", "checkin-head",
      "How did you sleep, and what actually matters today?"));
    const ta = el("textarea", "input");
    ta.rows = 2;
    ta.id = "checkin-input";
    ta.placeholder = "A line or two. Otto cannot see this half.";
    ta.value = checkinDraft;
    ta.addEventListener("input", () => { checkinDraft = ta.value; });
    ciBox.appendChild(ta);
    const row = el("div", "checkin-acts");
    row.appendChild(el("span", "lbl", "energy"));
    const seg = el("div", "energy-seg");
    for (let i = 1; i <= 5; i++) {
      const b = el("button", checkinEnergy === i ? "on" : null, String(i));
      b.type = "button";
      b.title = "energy " + i + "/5";
      b.addEventListener("click", () => { checkinEnergy = i; render(true); });
      seg.appendChild(b);
    }
    row.appendChild(seg);
    row.appendChild(el("span", "spacer"));
    const send = el("button", "btn btn-primary btn-sm", "Save");
    send.type = "button";
    send.addEventListener("click", async () => {
      const note = checkinDraft.trim();
      if (!note && !checkinEnergy) { toast("nothing to record", true); return; }
      send.disabled = true;
      try {
        await act("otto checkin" + (checkinEnergy ? " --energy " + checkinEnergy : "")
          + (note ? ' "' + note.slice(0, 40).replace(/"/g, "") + (note.length > 40 ? "…" : "") + '"' : ""),
          "/api/day/" + new Date().toISOString().slice(0, 10), {
          method: "PUT", headers: { "content-type": "application/json" },
          body: JSON.stringify({ note: note || null, energy: checkinEnergy }),
        });
        checkinDraft = ""; checkinEnergy = null;
        toast("checked in");
        await poll();
      } catch (e) { toast(e.message, true); } finally { send.disabled = false; }
    });
    row.appendChild(send);
    ciBox.appendChild(row);
  }
  dayBox.appendChild(ciBox);
  top.appendChild(dayBox);
  wrap.appendChild(top);

  /* -- 3. in flight, only when there is something -- */
  const live = (s.live || []).filter(keep);
  if (live.length) {
    const lb = el("section", "sec today-live");
    lb.appendChild(secHead("In flight", live.length + " running"));
    for (const r of live) lb.appendChild(liveCard(r));
    wrap.appendChild(lb);
  }

  /* -- 3b. sessions --
   * Deliberately its own section, directly under In flight, because the two are
   * near-opposites and the contrast is the information. In flight is what Otto
   * SPAWNED, watched from outside. This is what Claude Code reports from inside,
   * including every terminal Otto never started, which used to be nothing at all. */
  const sessBox = sessionsSection(s);
  if (sessBox) wrap.appendChild(sessBox);

  wrap.appendChild(el("hr", "hr-fade"));

  /* -- 4. needs you / blind spots -- */
  const lower = el("div", "today-lower");
  lower.appendChild(sectionNeedsYou(s));
  lower.appendChild(sectionGaps(s));
  wrap.appendChild(lower);
  return wrap;
}

/* Live Claude Code sessions, from their own hooks. Returns null when there is
 * nothing worth a section, so a machine with no sessions shows no empty box. */
function sessionsSection(s) {
  const data = s.sessions || {};
  const summary = data.summary || {};
  const rows = (data.sessions || []).filter(inDomain).filter((x) => x.state !== "offline");

  if (!summary.hooks_installed) {
    /* Only nag once something has reported. A machine that never opted in gets
     * this from the gaps panel, which is where "nothing is watching" belongs. */
    if (!summary.total) return null;
    const box = el("section", "sec");
    box.appendChild(secHead("Sessions", "hooks not installed"));
    box.appendChild(el("p", "sec-note",
      "Otto can only see sessions it spawned itself. Run: otto sessions install"));
    return box;
  }
  if (!rows.length) return null;

  const box = el("section", "sec");
  const counts = ["busy", "waiting", "idle"]
    .filter((k) => summary[k]).map((k) => summary[k] + " " + k).join(", ");
  box.appendChild(secHead("Sessions", counts));
  box.appendChild(el("p", "sec-note",
    "Reported by each session's own hooks, not inferred from a log. "
    + (summary.unspawned
      ? summary.unspawned + " of these Otto did not spawn and could not see before."
      : "")));

  /* waiting first: it is the only state that is asking the owner for something. */
  const order = { waiting: 0, busy: 1, idle: 2 };
  rows.sort((a, b) => (order[a.state] ?? 3) - (order[b.state] ?? 3)
    || String(a.state_since).localeCompare(String(b.state_since)));

  appendPaged(box, rows, "sessions", (x) => {
    const card = el("article", "live-card");
    const where = x.repo || (x.cwd ? x.cwd.split(/[\\/]/).filter(Boolean).pop() : "unknown");
    const top = el("div", "live-top");
    top.appendChild(pill(x.state, x.state === "busy" ? "running"
      : x.state === "waiting" ? "due" : "idle"));
    /* The title is what the session is about (its transcript's first prompt, or a
     * name the owner gave it). Click it to rename: the transcript's guess is a start,
     * not a verdict. */
    const name = el("strong", "live-name", x.title || where);
    name.title = "click to rename";
    name.style.cursor = "text";
    name.onclick = async () => {
      const t = prompt("Name this session", x.title || "");
      if (t === null || !t.trim()) return;
      try { await api(`/api/sessions/${x.session_id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: t.trim() }) }); await poll(); }
      catch (e) { toast(e.message, true); }
    };
    top.appendChild(name);
    top.appendChild(el("span", "mono-dim", x.session_id.slice(0, 8)));
    if (x.host) top.appendChild(el("span", "mono-dim", x.host));
    else if (!x.run_id) top.appendChild(pill("not Otto's", "backlog"));
    top.appendChild(el("span", "spacer"));
    /* Open: a live session's window comes forward; an ended one resumes in a new
     * Windows Terminal tab. The daemon does it, since it runs in the owner's desktop
     * session. */
    const open = el("button", "density-chip", "open");
    open.type = "button";
    open.onclick = async (ev) => {
      ev.stopPropagation();
      try { const r = await api(`/api/sessions/${x.session_id}/open`, { method: "POST" }); toast(r.message); }
      catch (e) { toast(e.message, true); }
    };
    top.appendChild(open);
    top.appendChild(domTag(x.domain || "work"));
    top.appendChild(ageEl(x.state_since, "mono-dim"));
    card.appendChild(top);

    const detail = x.note || x.last_message;
    if (detail) {
      const now = el("div", "live-now");
      now.appendChild(el("span", "lbl", x.state === "waiting" ? "asking" : "last"));
      now.appendChild(el("span", null, detail));
      card.appendChild(now);
    }
    if (x.cwd) {
      const m = el("div", "live-meta");
      if (x.title) m.appendChild(el("span", "mono-dim", where + " · "));
      m.appendChild(pathEl(x.cwd));
      card.appendChild(m);
    }
    return card;
  });
  return box;
}

function liveCard(r) {
  const card = el("article", "live-card");
  const top = el("div", "live-top");
  top.appendChild(el("span", "spin"));
  top.appendChild(el("strong", "live-name", r.name));
  top.appendChild(el("span", "mono-dim", r.id.slice(0, 6) + " · pid " + r.pid));
  if (r.pane_id) top.appendChild(el("span", "mono-dim", "pane " + r.pane_id));
  top.appendChild(el("span", "spacer"));
  /* Plan progress, in the header, so "40 minutes in" reads against what is typical
   * for this schedule rather than against nothing. */
  if (r.plan && r.plan.elapsed_seconds != null) {
    let t = Math.round(r.plan.elapsed_seconds / 60) + "m";
    if (r.plan.expected_seconds) t += " / ~" + Math.round(r.plan.expected_seconds / 60) + "m typical";
    const pt = el("span", "mono-dim", t);
    if (r.plan.expected_seconds && r.plan.elapsed_seconds > r.plan.expected_seconds * 1.5) pt.style.color = WARN;
    top.appendChild(pt);
  } else {
    top.appendChild(ageEl(r.started, "mono-dim"));
  }
  card.appendChild(top);
  const now = el("div", "live-now");
  now.appendChild(el("span", "lbl", "now"));
  now.appendChild(el("span", null, r.current || "working"));
  card.appendChild(now);
  const meta = el("div", "live-meta");
  const bits = [];
  if (r.tool_calls != null) bits.push(r.tool_calls + " tool calls");
  if (r.subtasks_started) bits.push((r.subtasks_done || 0) + "/" + r.subtasks_started + " sub-tasks");
  if (r.output_tokens) bits.push(kfmt(r.output_tokens) + " out");
  meta.appendChild(el("span", null, bits.join("  ·  ") || "no output yet"));
  meta.appendChild(el("span", "spacer"));
  if (r.last_activity) {
    const secs = Math.max(0, Math.round((Date.now() - Date.parse(r.last_activity)) / 1000));
    const w = el("span", null, "last wrote " + secs + "s ago");
    if (secs > 45) w.style.color = WARN;
    meta.appendChild(w);
  }
  card.appendChild(meta);
  if (r.plan && r.plan.phases) {
    const rail = phaseRail(r.plan);
    rail.style.margin = "var(--space-3) 0 0";
    card.appendChild(rail);
  }
  card.addEventListener("click", () => select("run", r.id));
  return card;
}

/* The gold banner: a KNOWN failure that started passing. The notice carries the
 * `otto known rm <kind> <name>` command; the button here runs the same thing. */
function staleBanner(n) {
  const box = el("div", "stale-banner");
  box.appendChild(ico("ph ph-confetti"));
  const body = el("div");
  body.style.minWidth = "0";
  body.appendChild(el("div", "kick", "KNOWN FAILURE IS NOW STALE"));
  body.appendChild(el("h3", null, n.title.replace(/^Known failure is now stale:\s*/i, "")));
  body.appendChild(el("p", null, n.body || "Somebody fixed the underlying thing. The annotation is lying about it now."));
  box.appendChild(body);
  const acts = el("div", "acts");
  const m = /^otto known rm (\S+) (\S+)/.exec(n.command || "");
  if (m) {
    const clear = el("button", "btn btn-primary btn-sm");
    clear.type = "button";
    clear.appendChild(ico("ph ph-eraser"));
    clear.appendChild(el("span", null, "Clear annotation"));
    clear.addEventListener("click", async () => {
      clear.disabled = true;
      try {
        await act(n.command, `/api/known/${encodeURIComponent(m[1])}/${encodeURIComponent(m[2])}`, { method: "DELETE" });
        await api("/api/notices/" + n.id + "/read", { method: "POST" });
        toast(`${m[2]} is no longer marked known`);
        await poll();
      } catch (e) { toast(e.message, true); clear.disabled = false; }
    });
    acts.appendChild(clear);
  }
  const got = el("button", "btn btn-secondary btn-xs", "Later");
  got.type = "button";
  got.addEventListener("click", async () => {
    try { await api("/api/notices/" + n.id + "/read", { method: "POST" }); await poll(); }
    catch (e) { toast(e.message, true); }
  });
  acts.appendChild(got);
  box.appendChild(acts);
  return box;
}

function sectionNeedsYou(s) {
  const nextRows = ((s.briefing && s.briefing.next) || []).filter(keep);
  const sec = el("section", "sec");
  sec.appendChild(secHead("Needs you", nextRows.length + " ranked"));
  sec.appendChild(el("p", "sec-note",
    "Plain scoring over stored state. No tokens, no latency, identical every run."));
  const ICO = {
    outage: "ph ph-warning-octagon", schedule: "ph ph-clock", run: "ph ph-ghost",
    task: "ph ph-check-square", gap: "ph ph-eye-slash", due: "ph ph-clock",
  };
  let lastBand = null;
  nextRows.forEach((r, i) => {
    if (r.band !== lastBand) {
      lastBand = r.band;
      const b = el("div", "band");
      b.appendChild(el("span", "lbl", r.band));
      /* The count, so the rail badge has somewhere on this page to land. The header
       * says "8 ranked" and the badge said "3", and nothing on the page was a 3, so
       * there was no way to reconcile them by looking. */
      b.appendChild(el("span", "band-n",
        String(nextRows.filter((x) => x.band === r.band).length)));
      b.appendChild(el("span", "sec-rule"));
      sec.appendChild(b);
    }
    const sev = r.kind === "outage" ? "crit" : (r.band === "today" ? "warn" : "idle");
    const card = el("article", "need"
      + (r.band === "today" ? " today" : "")
      + (r.kind === "outage" ? " crit" : ""));
    const gut = el("div", "need-gutter");
    gut.appendChild(el("span", "need-rank", String(i + 1).padStart(2, "0")));
    gut.appendChild(el("span", "need-thread"));
    card.appendChild(gut);
    const body = el("div");
    body.style.minWidth = "0";
    const tags = el("div", "need-tags");
    tags.appendChild(pill(r.kind, sev, ICO[r.kind] || "ph ph-dot"));
    tags.appendChild(domTag(r.domain));
    body.appendChild(tags);
    body.appendChild(el("h3", null, r.title));
    body.appendChild(el("p", "need-why", r.why));
    const acts = el("div", "need-acts");
    const sched = (s.schedules || []).find(
      (x) => r.title === "run " + x.name || r.title === x.name + " has stopped running");
    if (sched) {
      const rb = el("button", "btn btn-primary btn-sm");
      rb.type = "button";
      rb.appendChild(ico("ph ph-play"));
      rb.appendChild(el("span", null, "Run " + sched.name));
      rb.addEventListener("click", (e) => { e.stopPropagation(); launch(sched.name, rb); });
      acts.appendChild(rb);
    }
    if (r.command) {
      const cb = el("button", "cmd-btn", r.command);
      cb.type = "button";
      cb.addEventListener("click", (e) => { e.stopPropagation(); copy(r.command); });
      acts.appendChild(cb);
    }
    body.appendChild(acts);
    card.appendChild(body);
    card.addEventListener("click", () => openAction(actionable(r, "stream")));
    sec.appendChild(card);
  });
  if (!nextRows.length) {
    sec.appendChild(emptyState(scope
      ? "Nothing ranked in this directory."
      : "Nothing to do. Every schedule is fresh and nothing is waiting.",
      scope ? "otto next" : "otto gaps"));
  }
  return sec;
}

/* An empty state says what to do next. The command is copyable, so the blank is a
 * prompt rather than a description of absence. */
function emptyState(text, cmd) {
  const p = el("div", "sec-empty");
  p.appendChild(el("div", null, text));
  if (cmd) {
    const b = el("button", "cmd-btn", cmd);
    b.type = "button";
    b.addEventListener("click", () => copy(cmd));
    p.appendChild(b);
  }
  return p;
}

function sectionGaps(s) {
  const gaps = ((s.briefing && s.briefing.gaps) || []).filter(keep);
  const sec = el("section", "sec");
  sec.appendChild(secHead("Blind spots", String(gaps.length), true));
  sec.appendChild(el("p", "sec-note",
    "An alert is something that broke and Otto was watching. A gap is something nothing was ever watching."));
  for (const g of gaps) {
    const card = el("article", "gap");
    const tags = el("div", "gap-tags");
    tags.appendChild(el("span", "gap-kind", g.kind));
    tags.appendChild(domTag(g.domain));
    card.appendChild(tags);
    card.appendChild(el("h3", null, g.title));
    card.appendChild(el("p", "gap-why", g.why));
    if (g.command) {
      const b = el("button", "cmd-btn block", g.command);
      b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); copy(g.command); });
      card.appendChild(b);
    }
    card.style.cursor = "pointer";
    card.addEventListener("click", () => openAction(actionable(g, "stream")));
    sec.appendChild(card);
  }
  if (!gaps.length) {
    sec.appendChild(emptyState("No blind spots. Everything Otto knows about has a watcher.",
      "otto retire"));
  }
  return sec;
}

/* Messages Otto is about to send a colleague, with the time left on each.
 *
 * Two things this panel must do that an ordinary list does not. It has to show the
 * FULL text, because the owner is being asked to approve by saying nothing and approving
 * text you were shown three lines of is not approval. And it has to show `why`, which
 * is the field the decision actually turns on: the message reads fine on its own, the
 * question is always whether the reason behind it is sound. */
function outreachPanel(s, held) {
  const sum = (s.outreach || {}).summary || {};
  const sec = el("section", "sec outreach-sec");
  const h = secHead("Otto wants to send", held.length + " held", true);
  sec.appendChild(h);

  if (!sum.enabled) {
    sec.appendChild(el("p", "outreach-off",
      "Sending is OFF. These expire unsent, so this panel is a preview of what Otto "
      + "would have said. Set OTTO_OUTREACH=1 when the previews stop surprising you."));
  }

  for (const o of held) {
    const card = el("article", "outreach-card" + (personMatch(o.to + " " + o.target) ? "" : " dimmed"));
    const head = el("div", "notice-head");
    head.appendChild(pill(o.tier > 0 ? "tier " + o.tier : "auto",
      o.tier > 0 ? "warn" : "idle"));
    head.appendChild(el("span", "notice-src", o.to));
    head.appendChild(el("span", "spacer"));
    /* The countdown is the whole point, so it is the loudest thing on the card. A
     * tier-1 message never sends itself, so it must not show a deadline it does not
     * have: that would train the owner to rush a decision nothing was waiting on. */
    head.appendChild(o.tier > 0
      ? el("span", "mono-dim", "waits for you")
      : countdownEl(o.send_after, "outreach-clock", "sends "));
    card.appendChild(head);

    card.appendChild(el("p", "outreach-body", o.body));
    card.appendChild(el("p", "outreach-why", "why: " + o.why));

    /* -- the hold strip --
     * A deadline Otto set deserves a visible clock. Word, progress, time left, and
     * the two ways to move the deadline: push it, or cut it short. A tier-1 message
     * has no clock because nothing is waiting on it. */
    if (o.tier === 0) {
      const strip = el("div", "hold-strip");
      strip.appendChild(el("span", "hold-word", "HOLDING"));
      const track = el("div", "hold-track");
      const fill = el("span", "hold-fill");
      const total = Math.max(1, (o.hold_minutes || 10) * 60000);
      const left = Math.max(0, Date.parse(o.send_after) - Date.now());
      fill.style.width = Math.min(100, Math.round(100 * (1 - left / total))) + "%";
      /* Both the bar and the figure tick in place with the countdown above. */
      fill.dataset.hold = o.send_after; fill.dataset.total = String(total);
      track.appendChild(fill);
      strip.appendChild(track);
      const leftEl = el("span", "hold-left", holdLeft(left));
      leftEl.dataset.hold = o.send_after;
      strip.appendChild(leftEl);
      hasCountdown = true;
      card.appendChild(strip);
    }

    const acts = el("div", "hold-acts");
    const stop = el("button", "btn btn-secondary btn-xs", "Don't send");
    stop.type = "button";
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      try {
        await act(`otto outreach --kill ${o.id.slice(0, 6)}`, "/api/outreach/" + o.id + "/kill", { method: "POST" });
        toast("killed"); await poll();
      } catch (e) { toast(e.message, true); stop.disabled = false; }
    });
    acts.appendChild(stop);

    if (o.tier === 0) {
      const more = el("button", "btn btn-secondary btn-xs", "+10 min");
      more.type = "button";
      more.title = "Push the send back ten minutes (24h from compose is the ceiling)";
      more.addEventListener("click", async () => {
        more.disabled = true;
        try {
          await act(`otto outreach --extend ${o.id.slice(0, 6)} --minutes 10`,
            "/api/outreach/" + o.id + "/extend?minutes=10", { method: "POST" });
          toast("hold extended 10 min"); await poll();
        } catch (e) { toast(e.message, true); } finally { more.disabled = false; }
      });
      acts.appendChild(more);
    }

    const go = el("button", "btn btn-xs", "Send now");
    go.type = "button";
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        await act(`otto outreach --send ${o.id.slice(0, 6)}`, "/api/outreach/" + o.id + "/send", { method: "POST" });
        toast("sent"); await poll();
      } catch (e) { toast(e.message, true); go.disabled = false; }
    });
    acts.appendChild(go);
    acts.appendChild(el("span", "spacer"));
    acts.appendChild(el("span", "mono-dim", o.source));
    card.appendChild(acts);
    sec.appendChild(card);
  }
  return sec;
}

/* "7m 02s left" / "now". Seconds shown under a minute because that is when the
 * decision is being made. */
function holdLeft(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "sending now";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s left";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + String(s % 60).padStart(2, "0") + "s left";
  return Math.round(m / 60) + "h left";
}

/* "in 7m" / "now". Deliberately not age(): this counts DOWN, and rendering a future
 * timestamp through a function that says "ago" is how you get a panel that tells you
 * a message went out nine minutes before it did. */
function countdown(ts) {
  const ms = Date.parse(ts) - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) return "now";
  const mins = Math.round(ms / 60000);
  if (mins < 1) return "in " + Math.round(ms / 1000) + "s";
  if (mins < 60) return "in " + mins + "m";
  return "in " + Math.round(mins / 60) + "h";
}

function secHead(title, count, warn) {
  const h = el("div", "sec-head");
  h.appendChild(el("h2", "sec-title" + (warn ? " warn" : ""), title));
  if (count) h.appendChild(el("span", "sec-count", count));
  h.appendChild(el("span", "sec-rule"));
  return h;
}

