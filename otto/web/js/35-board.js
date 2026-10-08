/* otto/web/js/35-board.js: the Board view: columns, cards, batch select, the created-date filter, moves.
 * Relies on: 00-shell.js, 10-helpers.js. */

/* ============================== board ============================== */

/* /api/state.board is {columns:[{key,label,count,cards}], total, stored, derived},
 * NOT a map of status -> cards. Column order and labels come from the server so the
 * dashboard cannot disagree with the CLI about what the columns are. */
function boardColumns() {
  const b = (state && state.board) || {};
  return b.columns || [];
}
function allCards() {
  return boardColumns().flatMap((c) => c.cards || []);
}

/* ── paging ──
 * The first page of a long list, then a "Show N more" control that appends the
 * next page in place (no rebuild, scroll stays put) and loads itself when it
 * scrolls into view. Selection and drag work on whatever is rendered; select-all
 * and batch moves go by id and cover the unrendered cards too, which is what
 * "all shown here" meant before paging and still means. */
function appendPaged(host, items, key, make, before) {
  const shown = Math.min(items.length, Math.max(PAGE_CARDS, colShown[key] || 0));
  colShown[key] = shown;
  const frag = document.createDocumentFragment();
  for (const it of items.slice(0, shown)) frag.appendChild(make(it));
  host.insertBefore(frag, before || null);
  if (shown < items.length) host.insertBefore(moreButton(host, items, key, make), before || null);
}
function moreButton(host, items, key, make) {
  const b = el("button", "col-more");
  b.type = "button";
  const label = () => {
    const rest = items.length - colShown[key];
    b.textContent = `Show ${Math.min(PAGE_CARDS, rest)} more · ${rest} not rendered`;
  };
  label();
  const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) grow(); }, { rootMargin: "240px" });
  const grow = () => {
    if (!b.isConnected) { io.disconnect(); return; }
    const from = colShown[key], to = Math.min(items.length, from + PAGE_CARDS);
    const frag = document.createDocumentFragment();
    for (const it of items.slice(from, to)) frag.appendChild(make(it));
    host.insertBefore(frag, b);
    colShown[key] = to;
    if (to >= items.length) { io.disconnect(); b.remove(); } else label();
  };
  b.addEventListener("click", grow);
  io.observe(b);
  return b;
}

/* ── created-date filter ── */
const CREATED_PRESETS = [
  ["any", "any time"], ["today", "today"], ["7d", "last 7 days"], ["30d", "last 30 days"],
  ["older30", "older than 30 days"], ["custom", "custom range…"],
];
function inCreated(k) {
  const f = createdFilter;
  if (!f || f.preset === "any") return true;
  if (!k.created) return false;
  const t = Date.parse(k.created);
  if (Number.isNaN(t)) return false;
  const now = Date.now(), day = 86400000;
  if (f.preset === "today") {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    return t >= d.getTime();
  }
  if (f.preset === "7d") return t >= now - 7 * day;
  if (f.preset === "30d") return t >= now - 30 * day;
  if (f.preset === "older30") return t < now - 30 * day;
  if (f.preset === "custom") {
    if (f.from && t < Date.parse(f.from)) return false;
    if (f.to && t >= Date.parse(f.to) + day) return false;   // inclusive end date
    return true;
  }
  return true;
}
function createdFilterEl() {
  const box = el("span", "created-filter");
  box.appendChild(ico("ph ph-calendar-blank"));
  box.appendChild(el("span", null, "created"));
  const sel = el("select");
  for (const [v, label] of CREATED_PRESETS) {
    const o = el("option", null, label); o.value = v;
    if (createdFilter.preset === v) o.selected = true;
    sel.appendChild(o);
  }
  if (createdFilter.preset !== "any") sel.classList.add("on");
  sel.addEventListener("change", () => {
    createdFilter = { ...createdFilter, preset: sel.value };
    render(true);
  });
  box.appendChild(sel);
  if (createdFilter.preset === "custom") {
    for (const key of ["from", "to"]) {
      const inp = el("input"); inp.type = "date"; inp.value = createdFilter[key] || "";
      inp.title = key === "from" ? "created on or after" : "created on or before";
      if (inp.value) inp.classList.add("on");
      inp.addEventListener("change", () => {
        createdFilter = { ...createdFilter, [key]: inp.value };
        render(true);
      });
      box.appendChild(el("span", null, key === "from" ? "from" : "to"));
      box.appendChild(inp);
    }
  }
  return box;
}
/* The board's own filter: domain and scope as everywhere, plus the created date. */
function keepCard(k) { return keep(k) && inCreated(k); }

/* Every visible, movable card, in column order, for select-all and ranges. */
function visibleCards(colKey) {
  return boardColumns()
    .filter((c) => !colKey || c.key === colKey)
    .flatMap((c) => (c.cards || []).filter(keepCard).filter((k) => k.movable !== false));
}
function toggleSelected(id, on) {
  if (on === undefined) on = !selected.has(id);
  if (on) selected.add(id); else selected.delete(id);
}
function clearCardSelection() { selected.clear(); lastPick = null; render(true); }

function viewBoard() {
  const s = state;
  const wrap = el("div", "board-wrap" + (cardDetail === "full" ? " detail-full" : "")
    + (selected.size ? " selecting" : ""));

  const bar = el("div", "board-bar");
  bar.appendChild(el("span", null, "Backlog means not ready. Queued means run it."));
  bar.appendChild(el("span", "spacer"));
  bar.appendChild(createdFilterEl());
  if (s.board && s.board.faded) {
    const f = el("span", "sel-hint", `${s.board.faded} faded · otto task ls --faded`);
    bar.appendChild(f);
  }
  if (s.board && s.board.duplicates) {
    /* Closed into another card by the dedupe sweep. Not a completion, so not in
       Done; said out loud here so the board never quietly shows a subset. */
    const d = el("span", "sel-hint", `${s.board.duplicates} merged · otto task ls --duplicates`);
    bar.appendChild(d);
  }

  const clamped = cardDetail === "clamp";
  const dens = el("button", "density-chip");
  dens.type = "button";
  dens.appendChild(ico(clamped ? "ph ph-arrows-out-line-vertical" : "ph ph-arrows-in-line-vertical"));
  dens.appendChild(el("span", null, clamped ? "Compact" : "Full detail"));
  dens.title = clamped
    ? "Cards show 3 lines of detail. Click for the full text on every card."
    : "Cards show their whole detail. Click to go back to 3 lines.";
  dens.addEventListener("click", () => {
    cardDetail = clamped ? "full" : "clamp";
    render(true);
  });
  bar.appendChild(dens);

  const dsp = (s.dispatch || {});
  const chip = el("button", "dispatch-chip" + (dsp.enabled ? "" : " off"));
  chip.type = "button";
  chip.appendChild(ico(dsp.enabled ? "ph ph-lightning" : "ph ph-lightning-slash"));
  chip.appendChild(el("span", null, dsp.enabled ? "Auto-dispatch on" : "Auto-dispatch OFF"));
  chip.title = "Click to toggle";
  chip.addEventListener("click", async () => {
    chip.disabled = true;
    try {
      // `enabled` is a QUERY param on this endpoint, not a JSON body. A body
      // here 422s, and only on click, which is the worst place to find out.
      await act(`otto autodispatch ${dsp.enabled ? "off" : "on"}`,
        `/api/dispatch?enabled=${!dsp.enabled}`, { method: "POST" });
      toast("auto-dispatch " + (dsp.enabled ? "off" : "on"));
      await poll();
    } catch (e) { toast(e.message, true); } finally { chip.disabled = false; }
  });
  bar.appendChild(chip);

  /* Person focus applies here by name match on the card text, because a card does
   * not carry a slug. Said out loud, with the count, so a thinned board reads as
   * filtered rather than as finished. */
  if (personSel) {
    const all = allCards().filter(keepCard);
    const hit = all.filter((k) => personMatch(k.title + " " + (k.detail || "") + " " + (k.tags || []).join(" "))).length;
    const fn = el("span", "focus-note");
    fn.appendChild(ico("ph ph-user-focus"));
    fn.appendChild(el("span", null, `${hit} of ${all.length} cards mention ${personSel.name}`));
    bar.appendChild(fn);
  }
  wrap.appendChild(bar);

  const cols = el("div", "board-cols");
  for (const column of boardColumns()) {
    const key = column.key;
    const cards = (column.cards || []).filter(keepCard);
    const folded = key === "done" && !doneOpen;
    const col = el("div", "board-col"
      + (folded ? " folded" : "")
      + (overCol === key ? " over" : ""));

    const head = el("div", "col-head" + (key === "done" ? " foldable" : ""));
    /* Select every visible card in the column. Visible means after the filters, so
     * "created: older than 30 days" then this checkbox then Done is the sweep. */
    const movableHere = cards.filter((k) => k.movable !== false);
    if (movableHere.length && !folded) {
      const cb = el("input", "col-check"); cb.type = "checkbox";
      const allOn = movableHere.every((k) => selected.has(k.id));
      cb.checked = allOn;
      cb.indeterminate = !allOn && movableHere.some((k) => selected.has(k.id));
      cb.title = allOn ? "Deselect all in this column" : `Select all ${movableHere.length} shown here`;
      cb.addEventListener("click", (e) => {
        e.stopPropagation();
        for (const k of movableHere) toggleSelected(k.id, !allOn);
        render(true);
      });
      head.appendChild(cb);
    }
    head.appendChild(el("h4", key === "needs-you" ? "crit" : null, column.label || key));
    head.appendChild(el("span", "col-count", String(cards.length)));
    if (key === "done") {
      head.appendChild(el("span", "spacer"));
      head.appendChild(ico(folded ? "ph ph-caret-down" : "ph ph-caret-up"));
      head.title = folded ? "Show what got finished" : "Fold Done away";
      head.addEventListener("click", () => { doneOpen = !doneOpen; render(true); });
    }
    col.appendChild(head);

    if (key === "queued") {
      const note = el("div", "queued-note" + (dsp.enabled ? "" : " off"),
        dsp.enabled
          ? `Otto runs these with skip-permissions · max ${dsp.max_concurrent} at once`
          : "Auto-dispatch is OFF · nothing here will run");
      col.appendChild(note);
    }

    /* What the column is NOT showing, always stated. A folded Done still accepts a
     * drop (the handlers below are on the column, not the cards), so folding costs
     * nothing but the pixels. */
    const older = column.hidden
      ? `${column.hidden} finished more than ${column.hidden_after_days}d ago`
      : null;
    if (folded) {
      const n = cards.length;
      col.appendChild(el("div", "col-folded",
        (n ? `${n} finished in the last ${column.hidden_after_days}d`
           : "nothing finished lately")
        + (older ? ` · ${older}` : "")));
    } else {
      appendPaged(col, cards, "board:" + key, taskCard);
      if (!cards.length && !older) col.appendChild(el("div", "col-empty", "—"));
      if (older) col.appendChild(el("div", "col-empty", older + ", off the board"));
    }

    col.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      if (overCol !== key) { overCol = key; col.classList.add("over"); }
    });
    col.addEventListener("dragleave", () => {
      if (overCol === key) { overCol = null; col.classList.remove("over"); }
    });
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("over");
      const id = dragId;
      dragId = null; overCol = null;
      if (!id) return;
      /* Dragging one of a selection drags the selection. */
      const ids = selected.has(id) ? [...selected] : [id];
      await moveCards(ids, key);
    });
    cols.appendChild(col);
  }
  wrap.appendChild(cols);
  if (selected.size) wrap.appendChild(selectionBar());
  return wrap;
}

/* The bar under the columns while cards are held. Every destination the board has,
 * except queued: a batch that could start twenty unattended sessions in one click is
 * the wrong shape, and the daemon refuses it too. Fade is here because it is the
 * verb this bar exists for. */
function selectionBar() {
  const bar = el("div", "sel-bar");
  bar.appendChild(el("span", "sel-n", `${selected.size} selected`));
  bar.appendChild(el("span", "sel-hint", "move to"));
  const keys = [
    ["backlog", "Backlog", "b"], ["needs-you", "Needs you", "n"], ["blocked", "Blocked", "x"],
    ["done", "Done", "d"], ["faded", "Fade", "f"],
  ];
  for (const [key, label, hot] of keys) {
    const b = el("button", "sel-btn"); b.type = "button";
    b.appendChild(el("span", null, label));
    const kb = el("kbd", null, hot); b.appendChild(kb);
    b.title = `${label} · press ${hot}`;
    b.addEventListener("click", () => moveCards([...selected], key));
    bar.appendChild(b);
  }
  bar.appendChild(el("span", "spacer"));
  const all = el("button", "sel-btn quiet"); all.type = "button";
  all.appendChild(el("span", null, "select all shown"));
  all.addEventListener("click", () => { for (const k of visibleCards()) selected.add(k.id); render(true); });
  bar.appendChild(all);
  const clr = el("button", "sel-btn quiet"); clr.type = "button";
  clr.appendChild(el("span", null, "clear"));
  clr.appendChild(el("kbd", null, "esc"));
  clr.addEventListener("click", clearCardSelection);
  bar.appendChild(clr);
  bar.appendChild(el("span", "sel-hint", "ctrl-click adds · shift-click ranges · drag one moves all"));
  return bar;
}

/* Move one card to one column. The single write both ways of moving a card go
 * through, so drag and right-click can never disagree about what a move IS. */
async function moveCard(id, key) { return moveCards([id], key); }

/* Apply a move to the local state so the board repaints NOW. The PATCH was never
 * the slow part (30ms); waiting on the 2 MB /api/state refresh after it was the
 * 2-5 seconds. The next poll reconciles; on error the poll is forced and truth wins. */
function applyLocalMove(ids, key) {
  const b = state && state.board;
  if (!b) return;
  const want = new Set(ids);
  const moved = [];
  for (const col of b.columns || []) {
    const keep = [], out = [];
    for (const k of col.cards || []) (want.has(k.id) ? out : keep).push(k);
    if (out.length) { col.cards = keep; col.count = keep.length; moved.push(...out); }
  }
  const target = (b.columns || []).find((c) => c.key === key);
  for (const k of moved) k.status = key;
  if (target) { target.cards = [...moved, ...(target.cards || [])]; target.count = target.cards.length; }
  else if (key === "faded") b.faded = (b.faded || 0) + moved.length;
  for (const t of state.tasks || []) if (want.has(t.id)) t.status = key;
}
function pollSoon(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms); }

async function moveCards(ids, key) {
  ids = [...new Set(ids)];
  if (!ids.length) return;
  if (key === "queued" && ids.length > 1) {
    toast("queued is one card at a time, through otto triage promote", true);
    return;
  }
  applyLocalMove(ids, key);
  for (const id of ids) selected.delete(id);
  render(true);
  try {
    if (ids.length === 1) {
      await act(`otto task mv ${ids[0].slice(0, 6)} ${key}`, `/api/tasks/${ids[0]}`, {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ status: key }),
      });
    } else {
      await act(`otto task mv ${ids.map((i) => i.slice(0, 6)).join(" ")} ${key}`, "/api/tasks/batch", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids, patch: { status: key } }),
      });
    }
    toast(ids.length === 1 ? `moved to ${key}` : `moved ${ids.length} to ${key}`);
    pollSoon(600);
  } catch (err) {
    toast(err.message, true);
    poll();
  }
}

function closeCardMenu() {
  if (!cardMenu) return;
  cardMenu.remove();
  cardMenu = null;
}

/* Right-click a card to move it. Dragging is fine on a short board and stops being
 * possible on a long one: once a column runs past the viewport, the target column's
 * drop zone and the card you are holding are not on screen at the same time, and no
 * amount of auto-scroll makes that a pleasant thing to aim at. This needs no pointer
 * travel and works identically at any board length.
 *
 * Rows come from boardColumns(), never a literal list, so this cannot drift out of
 * step with board.COLUMNS the way a second copy of the names would. */
function openCardMenu(k, x, y) {
  closeCardMenu();
  const menu = el("div", "cardmenu");
  menu.appendChild(el("div", "cardmenu-head", k.title));

  if (k.movable !== false) {
    for (const column of boardColumns()) {
      const key = column.key;
      const here = key === k.status;
      const row = el("button", "cardmenu-row" + (here ? " on" : ""));
      row.type = "button";
      row.disabled = here;
      row.appendChild(ico(here ? "ph ph-dot-outline" : "ph ph-arrow-right"));
      row.appendChild(el("span", "lbl", column.label || key));
      if (here) row.appendChild(el("span", "cardmenu-note", "here now"));
      row.addEventListener("click", () => { closeCardMenu(); moveCard(k.id, key); });
      menu.appendChild(row);
    }
    /* The conversational verb, after the moves. Same sheet the due toast opens. */
    const talk = el("button", "cardmenu-row");
    talk.type = "button";
    talk.appendChild(ico("ph ph-chat-teardrop-text"));
    talk.appendChild(el("span", "lbl", "Tell Otto…"));
    talk.appendChild(el("span", "cardmenu-note", "update it, or give context"));
    talk.addEventListener("click", () => { closeCardMenu(); openReply(k); });
    menu.appendChild(talk);
  } else if (k.kind === "run" && k.run_id) {
    /* A failed run has no stored row to move, so the board's only verb for it is
     * acknowledge. Same call the inspector makes. */
    const row = el("button", "cardmenu-row");
    row.type = "button";
    row.appendChild(ico("ph ph-check"));
    row.appendChild(el("span", "lbl", "Acknowledge"));
    row.addEventListener("click", async () => {
      closeCardMenu();
      try {
        await act(`otto ack ${k.run_id.slice(0, 6)}`, `/api/runs/${k.run_id}/ack`, { method: "POST" });
        toast("acknowledged · card cleared");
        await poll();
      } catch (e) { toast(e.message, true); }
    });
    menu.appendChild(row);
  } else {
    /* Say why there is nothing to offer rather than showing an empty box. A derived
     * card clears itself when the condition clears; that IS the move. */
    menu.appendChild(el("div", "cardmenu-dead",
      "Computed card · fix the condition and it clears itself"));
  }

  document.body.appendChild(menu);
  cardMenu = menu;

  /* Position after measuring, so a card near the right or bottom edge flips instead
   * of opening off screen. */
  const r = menu.getBoundingClientRect();
  const pad = 8;
  const left = x + r.width + pad > window.innerWidth ? Math.max(pad, x - r.width) : x;
  const top = y + r.height + pad > window.innerHeight
    ? Math.max(pad, window.innerHeight - r.height - pad) : y;
  menu.style.left = left + "px";
  menu.style.top = top + "px";
}

/* Toggle one card, or with shift, the range from the last pick within the same
 * column. Range is by DOM order so it matches what the eye sees after filters. */
function pickCard(k, cardEl, range) {
  if (range && lastPick) {
    const col = cardEl.closest(".board-col");
    const nodes = col ? [...col.querySelectorAll(".tcard[data-id]")] : [];
    const ids = nodes.map((n) => n.dataset.id);
    const a = ids.indexOf(lastPick), b = ids.indexOf(k.id);
    if (a >= 0 && b >= 0) {
      for (const id of ids.slice(Math.min(a, b), Math.max(a, b) + 1)) selected.add(id);
      lastPick = k.id;
      render(true);
      return;
    }
  }
  toggleSelected(k.id);
  lastPick = selected.has(k.id) ? k.id : null;
  render(true);
}

function taskCard(k) {
  const movable = k.movable !== false;
  const focused = personMatch(k.title + " " + (k.detail || "") + " " + (k.tags || []).join(" "));
  const isSel = selected.has(k.id);
  const card = el("article", "tcard"
    + (movable ? "" : " derived")
    + (focused ? "" : " dimmed")
    + (isSel ? " selected" : "")
    + (dragId === k.id ? " dragging" : ""));
  card.dataset.id = k.id;

  if (movable) {
    const cb = el("input", "tcard-check"); cb.type = "checkbox"; cb.checked = isSel;
    cb.title = "Select for a batch move (ctrl-click the card works too)";
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      pickCard(k, card, e.shiftKey);
    });
    card.appendChild(cb);
  }

  const tags = el("div", "tcard-tags");
  if (k.priority && k.priority !== "normal") {
    const [fg, bg] = stColor(k.priority);
    const p = el("span", "chip-sm", k.priority);
    p.style.color = fg; p.style.background = bg;
    tags.appendChild(p);
  }
  if (k.task_ref) tags.appendChild(el("span", "chip-ref", k.task_ref));
  if (k.origin) tags.appendChild(el("span", "chip-mono", k.origin));
  if (k.seen_count > 1) tags.appendChild(el("span", "chip-sm chip-seen", `seen ${k.seen_count}x`));
  /* A due date the card carried but never showed. Rendered as pressure, not as a
   * date: "OVERDUE 3d" is a different message from "2026-08-01", and the second one
   * needs you to do the arithmetic before it means anything. */
  const d = dueLabel(k.due);
  if (d) {
    const [fg, bg] = stColor(d.key);
    const c = el("span", "chip-sm", d.text);
    c.style.color = fg; c.style.background = bg;
    tags.appendChild(c);
  }
  tags.appendChild(el("span", "spacer"));
  if (k.domain === "personal") {
    const l = el("span", "chip-sm", "life");
    l.style.border = "1px solid color-mix(in srgb, var(--color-accent) 45%, transparent)";
    l.style.color = ACCENT;
    tags.appendChild(l);
  }
  card.appendChild(tags);

  card.appendChild(el("div", "tcard-title", k.title));
  if (k.detail) card.appendChild(el("p", "tcard-detail", k.detail));

  const foot = el("div", "tcard-foot");
  foot.appendChild(ico(k.kind === "task" ? "ph ph-check-square" : "ph ph-broadcast"));
  foot.appendChild(el("span", null, k.kind || "task"));
  if (k.age) foot.appendChild(el("span", null, "· " + k.age));
  if (createdFilter.preset !== "any" && k.created) {
    foot.appendChild(el("span", "tcard-created", "created " + String(k.created).slice(0, 10)));
  }
  /* No "N more" marker here on purpose. It was written, then measured against the
   * real board: 90% of cards would carry one at any threshold that meant "was this
   * cut", and 37% at a threshold high enough to be rare, so it read as decoration
   * rather than signal on the one view whose whole problem was decoration.
   * `-webkit-line-clamp` already renders an ellipsis at the cut, which says the same
   * thing and costs no chrome. */
  foot.appendChild(el("span", "spacer"));
  if (k.run_id) {
    const lg = el("span", null, "log");
    lg.style.color = ACCENT;
    foot.appendChild(lg);
  }
  card.appendChild(foot);

  if (movable) {
    card.draggable = true;
    card.addEventListener("dragstart", (e) => {
      dragId = k.id; card.classList.add("dragging");
      /* Firefox will not start a drag with an empty dataTransfer. */
      if (e.dataTransfer) { e.dataTransfer.setData("text/plain", k.id); e.dataTransfer.effectAllowed = "move"; }
    });
    card.addEventListener("dragend", () => {
      dragId = null; overCol = null; card.classList.remove("dragging");
      /* A poll landed during the drag and was held back. Let it through now. */
      if (renderDeferred) render(true);
    });
  }
  /* The card opens the action modal, matching the stream. The full detail view is
   * one click further in, from the modal's own "Details" button. Ctrl/Cmd-click or
   * shift-click selects instead, like a file manager. */
  card.addEventListener("click", (e) => {
    if (movable && (e.ctrlKey || e.metaKey || e.shiftKey)) {
      e.preventDefault();
      pickCard(k, card, e.shiftKey);
      return;
    }
    openAction(actionable(k, "card"));
  });
  card.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openCardMenu(k, e.clientX, e.clientY);
  });
  return card;
}

