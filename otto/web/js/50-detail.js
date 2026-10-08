/* otto/web/js/50-detail.js: the notice detail sheet, the card reply sheet and the inspector frame.
 * Relies on: 00-shell.js, 10-helpers.js. */

/* ============================== notice detail ============================== */

/* A notice body is a plain-text list: `- ` for an item, leading whitespace for a
 * detail line under it. Producers already write it that way (nudges.py emits one
 * line per stale thread), so this only has to recover the structure that <p>
 * throws away, never invent any. Anything unrecognised stays a paragraph. */
function noticeLines(body) {
  return String(body || "").split("\n").filter((l) => l.trim());
}

function noticeRows(body) {
  const rows = [];
  for (const raw of noticeLines(body)) {
    const indented = /^\s/.test(raw);
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    if (bullet && !indented) {
      /* Split on the first colon so "Jonathan Criner, open since 2026-07-06 (30d)"
       * reads as the subject and the rest as what happened. Only when the lead-in
       * is short enough to actually be one: a colon inside prose is not a label. */
      const text = bullet[1];
      const at = text.indexOf(": ");
      if (at > 0 && at <= 90) {
        rows.push({ kind: "item", lead: text.slice(0, at), text: text.slice(at + 2) });
      } else {
        rows.push({ kind: "item", lead: null, text });
      }
    } else if (indented) {
      rows.push({ kind: "sub", text: line.replace(/^[-*•]\s+/, "") });
    } else {
      rows.push({ kind: "p", text: line });
    }
  }
  return rows;
}

/* Notices whose body is a summary of live state render that state instead of the
 * frozen string. threads-quiet is the case that forced it: the notice listed 8 of
 * 23 and said "15 more in the same band", which cannot be acted on, and by the time
 * it is read the counts have moved anyway. */
const LIVE_NOTICE = { "threads-quiet": renderThreadsLive };

async function renderThreadsLive(host) {
  host.appendChild(el("p", "nf-loading", "loading threads…"));
  let data;
  try {
    data = await api("/api/threads");
  } catch (e) {
    host.replaceChildren(el("p", "nf-p", "Could not load threads: " + e.message));
    return;
  }
  host.replaceChildren();

  /* Person focus carries into the thread list: the slug is on every row. */
  const allRows = data.rows || [];
  const rows = personSel ? allRows.filter((r) => r.slug === personSel.slug || personMatch(r.who)) : allRows;
  const c = data.counts || {}, b = data.bands || {};
  host.appendChild(el("p", "sec-note",
    `${c.quiet || 0} quiet (${b.stale_days}-${b.max_days} days), `
    + `${c.ancient || 0} over ${b.max_days} days, ${c.fresh || 0} fresh. `
    + "Write a note on any of them and Otto picks what to do with it."));
  if (personSel) {
    const fn = el("span", "focus-note");
    fn.appendChild(ico("ph ph-user-focus"));
    fn.appendChild(el("span", null, `${rows.length} of ${allRows.length} threads are ${personSel.name}'s`));
    host.appendChild(fn);
  }

  /* Quiet first, then ancient, then fresh: the notice is about what went quiet, and
   * the ancient ones are the keep-or-drop pile rather than the reply pile. */
  const ORDER = ["quiet", "ancient", "fresh"];
  const LABEL = {
    quiet: `Quiet · ${b.stale_days}-${b.max_days} days`,
    ancient: `Over ${b.max_days} days · keep or drop`,
    fresh: "Recent · nothing needed",
  };
  for (const band of ORDER) {
    const group = rows.filter((r) => r.band === band);
    if (!group.length) continue;
    const h = el("h3", "nf-band", LABEL[band]);
    h.appendChild(el("span", "nf-band-n", String(group.length)));
    host.appendChild(h);
    for (const r of group) host.appendChild(threadRow(r));
  }
}

function threadRow(r) {
  const item = el("div", "nf-item nf-thread");
  const head = el("div", "nf-thread-head");
  head.appendChild(el("span", "nf-lead", r.who));
  head.appendChild(el("span", "mono-dim", r.days + "d · " + r.when));
  head.appendChild(el("span", "spacer"));
  item.appendChild(head);
  item.appendChild(el("div", "nf-text", r.text));

  for (const n of r.notes || []) {
    const prev = el("div", "nf-note-prev");
    prev.appendChild(el("span", "mono-dim", n.at.slice(0, 10)));
    prev.appendChild(el("span", null, n.note));
    item.appendChild(prev);
  }
  /* What Otto concluded last time, against the thread it concluded it about. A
   * verdict filed anywhere else is one nobody reads next to the thing it judged. */
  for (const run of r.runs || []) {
    const v = el("div", "nf-verdict" + (run.status === "running" ? " live" : ""));
    v.appendChild(pill(run.status, run.status === "ok" ? "done"
      : run.status === "running" ? "running" : run.status));
    v.appendChild(el("span", null, run.result || (run.status === "running"
      ? "Otto is deciding…" : "no report")));
    item.appendChild(v);
  }

  const box = el("div", "nf-note-box");
  const ta = el("textarea", "input nf-note-in");
  ta.rows = 1;
  ta.placeholder = "note this thread… (what is true now, or what to do)";
  const send = el("button", "btn btn-secondary btn-xs", "Send to Otto");
  send.type = "button";
  send.disabled = true;
  ta.addEventListener("input", () => {
    send.disabled = !ta.value.trim();
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  });
  /* Ctrl/Cmd+Enter sends, because the textarea owns plain Enter. Same rule as the
   * action sheet. */
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && ta.value.trim()) submit();
  });
  async function submit() {
    send.disabled = true; ta.disabled = true;
    try {
      await act(`otto thread-note ${r.id.slice(0, 8)} "${ta.value.trim().slice(0, 40).replace(/"/g, "")}…"`, "/api/threads/note", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ thread_id: r.id, note: ta.value.trim() }),
      });
      box.replaceChildren(el("span", "nf-sent", "sent · Otto is deciding"));
      /* Repaint from the server so the new note and the running verdict appear
       * where they belong, rather than being faked client-side. */
      setTimeout(() => { const h = $("notice-full"); if (h) renderThreadsLive(h); }, 1200);
    } catch (e) {
      toast(e.message, true);
      send.disabled = false; ta.disabled = false;
    }
  }
  send.addEventListener("click", submit);
  box.appendChild(ta);
  box.appendChild(send);
  item.appendChild(box);
  return item;
}

function openNotice(n) {
  const tags = $("notice-tags");
  tags.replaceChildren();
  tags.appendChild(pill(n.level, n.level === "info" ? "idle" : n.level));
  tags.appendChild(el("span", "notice-src", n.source));
  if (n.seen_count > 1) {
    tags.appendChild(el("span", "chip-sm chip-seen", `seen ${n.seen_count}x`));
  }
  if (n.domain) tags.appendChild(domTag(n.domain));
  /* First sighting, not the latest. A thing outstanding since 19:14 keeps saying so. */
  tags.appendChild(ageEl(n.at, "mono-dim"));

  $("notice-title").textContent = n.title;

  const body = $("notice-full");
  body.replaceChildren();

  const live = LIVE_NOTICE[n.source];
  if (live) {
    $("notice-overlay").hidden = false;
    live(body);
    noticeFoot(n);
    return;
  }

  for (const r of noticeRows(n.body)) {
    if (r.kind === "item") {
      const item = el("div", "nf-item");
      if (r.lead) item.appendChild(el("div", "nf-lead", r.lead));
      item.appendChild(el("div", "nf-text", r.text));
      body.appendChild(item);
    } else if (r.kind === "sub") {
      body.appendChild(el("div", "nf-sub", r.text));
    } else {
      body.appendChild(el("p", "nf-p", r.text));
    }
  }

  noticeFoot(n);
  $("notice-overlay").hidden = false;
}

/* The same three actions the card offers. Being in the overlay means reading the
 * detail and acting on it do not need two separate clicks in two places. */
function noticeFoot(n) {
  const foot = $("notice-foot");
  foot.replaceChildren();
  if (n.command) {
    const c = el("button", "cmd-btn", n.command);
    c.type = "button";
    c.addEventListener("click", () => copy(n.command));
    foot.appendChild(c);
  }
  if (n.task_id) {
    const card = findCard(n.task_id);
    const rp = el("button", "btn btn-primary btn-xs", card ? "Reply" : "Reply (card gone)");
    rp.type = "button";
    rp.disabled = !card;
    rp.title = card ? "Update the card, or tell Otto what changed" : "That card is no longer on the board";
    rp.addEventListener("click", () => openReply(card));
    foot.appendChild(rp);
  }
  foot.appendChild(el("span", "spacer"));
  if (!n.read_at) {
    const r = el("button", "btn btn-secondary btn-xs", "Got it");
    r.type = "button";
    r.addEventListener("click", async () => {
      r.disabled = true;
      try {
        await act(`POST /api/notices/${n.id.slice(0, 6)}…/read`, "/api/notices/" + n.id + "/read", { method: "POST" });
        closeNotice(); await poll();
      } catch (err) { toast(err.message, true); r.disabled = false; }
    });
    foot.appendChild(r);
  }
  const x = el("button", "btn btn-secondary btn-xs", "Dismiss");
  x.type = "button";
  x.addEventListener("click", async () => {
    try {
      await act(`DELETE /api/notices/${n.id.slice(0, 6)}…`, "/api/notices/" + n.id, { method: "DELETE" });
      closeNotice(); await poll();
    } catch (err) { toast(err.message, true); }
  });
  foot.appendChild(x);
}

function closeNotice() { $("notice-overlay").hidden = true; }

/* ============================== card reply ============================== */
/* Asked 2026-09-03: a card nearing or past its due date should push a toast the owner
 * can ANSWER. This is where the answer is typed. Two lanes on purpose: the quick buttons
 * are the answers that need no interpretation and PATCH the card directly, with no
 * model and no wait; the text box is for everything else and goes to a small
 * /otto-card session that decides what follows and sends a receipt notice. */

function findCard(id) {
  if (!id) return null;
  return allCards().find((k) => k.id === id || k.id.startsWith(id)) || null;
}

function isoPlusDays(base, n) {
  const d = base ? new Date(String(base).slice(0, 10) + "T00:00:00") : new Date();
  if (isNaN(d)) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  /* Push from today when the card is already late: "+1 day" on a card two weeks
   * overdue should mean tomorrow, not thirteen days ago. */
  const from = d < today ? today : d;
  from.setDate(from.getDate() + n);
  return from.toISOString().slice(0, 10);
}

function openReply(card) {
  if (!card) return;
  replyCard = card;
  const tags = $("reply-tags");
  tags.replaceChildren();
  const d = dueLabel(card.due);
  if (d) tags.appendChild(pill(d.text, d.key));
  if (card.status) tags.appendChild(pill(card.status, card.status));
  if (card.priority && card.priority !== "normal") tags.appendChild(pill(card.priority, card.priority));
  tags.appendChild(el("span", "chip-mono", "task " + card.id.slice(0, 6)));
  if (card.domain) tags.appendChild(domTag(card.domain));
  $("reply-title").textContent = card.title;
  $("reply-why").textContent = card.assessed_note || card.detail || `on the board as ${card.status}`;

  const q = $("reply-quick");
  q.replaceChildren();
  const quick = (label, icon, cmd, body, toastMsg) => {
    const b = el("button", "btn btn-secondary btn-sm");
    b.type = "button";
    b.appendChild(ico(icon));
    b.appendChild(el("span", null, label));
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await act(cmd, `/api/tasks/${card.id}`, {
          method: "PATCH", headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        toast(toastMsg);
        closeReply();
        await poll();
      } catch (e) { toast(e.message, true); b.disabled = false; }
    });
    q.appendChild(b);
  };
  const id6 = card.id.slice(0, 6);
  if (card.status !== "done") {
    quick("Done", "ph ph-check", `otto task mv ${id6} done`, { status: "done" }, "marked done");
  }
  const d1 = isoPlusDays(card.due, 1), d7 = isoPlusDays(card.due, 7);
  if (d1) quick("Push a day", "ph ph-arrow-right", `otto task set ${id6} --due ${d1}`, { due: d1 }, `due ${d1}`);
  if (d7) quick("Push a week", "ph ph-arrow-fat-right", `otto task set ${id6} --due ${d7}`, { due: d7 }, `due ${d7}`);
  if (card.due) {
    /* An empty string, not null: TaskPatch drops None, so null would be "no change". */
    quick("Drop the date", "ph ph-calendar-x", `otto task set ${id6} --due ""`, { due: "" }, "due date cleared");
  }
  if (card.status !== "blocked") {
    quick("Blocked", "ph ph-hand", `otto task mv ${id6} blocked`, { status: "blocked" }, "marked blocked");
  }
  if (card.status === "backlog" || card.status === "needs-you") {
    /* Not done, not deleted: out of the columns. `otto task ls --faded` lists them
       and `otto task mv <id> backlog` brings one back. */
    quick("Fade", "ph ph-eye-slash", `otto task mv ${id6} faded`, { status: "faded" }, "faded");
  }

  $("reply-text").value = "";
  $("reply-overlay").hidden = false;
  $("reply-text").focus();
}

function closeReply() {
  replyCard = null;
  $("reply-overlay").hidden = true;
  if (location.hash.startsWith("#reply=")) history.replaceState(null, "", "#" + mode);
}

async function replySend(btn) {
  if (!replyCard) return;
  const text = $("reply-text").value.trim();
  if (!text) { toast("say something first, or use a quick button", true); return; }
  const card = replyCard;
  btn.disabled = true;
  try {
    const r = await act(`otto task reply ${card.id.slice(0, 6)} "…"`, `/api/tasks/${card.id}/reply`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (r && r.run) toast(`Otto is on it · run ${r.run.id.slice(0, 6)} · receipt arrives as a notice`);
    else toast(`saved on the card, but no session started: ${(r && r.error) || "unknown"}`, true);
    closeReply();
    await poll();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

/* The deep link, once there is a board to look the card up in. Called after every
 * successful poll; a no-op when nothing is pending. */
function openPendingReply() {
  if (!pendingReply || !state) return;
  const card = findCard(pendingReply);
  pendingReply = null;
  if (card) openReply(card);
  else toast("that card is no longer on the board", true);
}

/* ============================== inspector ============================== */

function select(type, id) {
  sel = { type, id };
  if (type === "run" && !activityCache[id]) loadActivity(id);
  if (type === "run" && !planCache[id]) loadPlan(id);
  if (type === "session" && !ledgerDetail[id]) loadLedgerSession(id);
  render();
}
function clearSelection() { sel = null; render(); }

async function loadActivity(id) {
  try {
    const a = await api(`/api/runs/${id}/activity`);
    activityCache[id] = a.events || a.timeline || [];
  } catch {
    activityCache[id] = [];
  }
  /* Only the inspector shows a timeline, so repaint only the inspector. Calling
   * render() here was the second of the two renders per poll tick. */
  if (sel && sel.id === id) {
    activitySig = id + ":" + activityCache[id].length;
    renderInspector();
  }
}

