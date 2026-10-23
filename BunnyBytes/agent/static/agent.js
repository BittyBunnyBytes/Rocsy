// Rocsy Agent page. The server embeds the session list (and the open session) in the
// page, then pushes changes over one EventSource per open session (/events): new
// timeline lines, state, jobs and the session list. Each job is an attachment card in the
// timeline (where it finished, else where it started) that updates in place, so a playing
// video never reloads; the Gallery collects every output. Sessions you've opened switch
// instantly from memory.
(() => {
  const API = "/Agent/api";
  const $ = (s, root = document) => root.querySelector(s);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const STATE = { starting: "Starting", waiting_for_gpu: "Waiting for GPU", thinking: "Thinking", rendering: "Rendering",
                  done: "Done", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted",
                  complete: "Complete", completed: "Complete", queued: "Queued", preparing: "Preparing",
                  running: "Running", training: "Training", finalizing: "Finalizing", cancelled: "Cancelled", canceled: "Cancelled" };
  const FINAL = new Set(["complete", "completed", "failed", "cancelled", "canceled", "error", "interrupted"]);
  const SERVICE = { Vid: "Video", Krea: "Image", Mus: "Music", Motion: "Motion" };   // ids stay Vid/Krea/Mus in data
  const PAGE = { Vid: "Video", Krea: "Image", Mus: "Music" };   // their pages: /Video, /Image, /Music
  const service = (id) => SERVICE[id] || id;
  const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();   // /schedule times
  const cache = new Map();          // id -> {snap, events[], last}
  const cards = new Map();          // job ref -> its card in the open session's timeline
  const terminals = new Map();      // terminal tool id -> its panel (long sessions have hundreds)
  const boards = new Map();         // video board id (scan1) -> its card: the Trainer's auto-cut view of a video
  let current = null, stream = null, retry = null, listKey = "";
  // Show available reasoning and tool activity unless this browser opted out.
  let hideThink = false;
  try { hideThink = localStorage.getItem("agent-think-stream") === "hide"; } catch (_) {}

  async function api(path, options = {}) {
    const r = await fetch(API + path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...options });
    if (r.status === 401) { location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`; throw new Error("Log in again"); }
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) {
      const d = body.detail;
      const err = new Error((d && typeof d === "object" ? d.message : d) || body.message || `Request failed (${r.status})`);
      err.status = r.status; err.detail = d; throw err;
    }
    return body;
  }
  const kb = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  const msg = (text) => { $("#a-msg").textContent = text || ""; };
  const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? "now" : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`; };
  const stateText = (st) => STATE[st] || (String(st || "").charAt(0).toUpperCase() + String(st || "").slice(1)).replaceAll("_", " ");

  /* ------------------------------------------------ sessions list -- */
  // Rows: click opens; the pencil (or F2, or double-click) renames in place; the bin (or
  // Delete) removes it with a few seconds to undo. "Select" deletes several at once.
  const ICON = {
    edit: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4Z M13.5 6.5l4 4" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
    bin: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5M10 11v5.5M14 11v5.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  let sessionsList = [], selecting = false;
  const selected = new Set();
  const doomed = new Map();          // id -> title, deleted on screen but not yet on the server
  const titles = new Map();          // id -> last title shown, so a new name can flash
  let renaming = null;               // id being renamed in the list (the list won't redraw under it)

  function iconBtn(cls, label, svg) {
    const b = el("button", `a-icon-btn ${cls}`); b.type = "button"; b.setAttribute("aria-label", label); b.title = label; b.innerHTML = svg; b.tabIndex = -1;
    return b;
  }
  function renderList(list = sessionsList, force = false) {
    sessionsList = list;
    const shown = list.filter((s) => !doomed.has(s.id));
    const key = JSON.stringify(shown.map((s) => [s.id, s.state, s.jobs, s.title, s.named, Math.floor(s.updated / 60)]))
      + current + selecting + [...selected].join();
    if ((key === listKey && !force) || renaming) return;
    listKey = key;
    for (const id of [...selected]) if (!shown.some((s) => s.id === id && !s.busy)) selected.delete(id);
    $("#a-sessions").replaceChildren(...(shown.length ? shown.map((s) => {
      const row = el("div", "a-sess-row"); row.dataset.id = s.id; row.dataset.state = s.state;
      const b = el("button", "a-sess"); b.type = "button"; b.dataset.state = s.state; b.dataset.id = s.id;
      b.setAttribute("aria-current", String(s.id === current));
      if (selecting) {
        const box = el("span", "a-check"); box.setAttribute("aria-hidden", "true");
        b.setAttribute("role", "checkbox"); b.setAttribute("aria-checked", String(selected.has(s.id)));
        if (s.busy) { b.disabled = true; b.title = "Running: stop it before deleting"; }
        row.classList.add("is-selecting"); row.prepend(box);
      }
      const name = el("span", "a-sess-title", s.title);
      if (s.named === "agent") { name.classList.add("by-agent"); name.title = "Named by the Master Agent"; }
      if (titles.has(s.id) && titles.get(s.id) !== s.title) name.classList.add("is-renamed");
      titles.set(s.id, s.title);
      const meta = el("span", "a-sess-meta");
      meta.append(el("span", "a-dot"), el("span", null, `${stateText(s.state)} · ${ago(s.updated)}${s.jobs ? ` · ${s.jobs} job${s.jobs > 1 ? "s" : ""}` : ""}`));
      b.append(name, meta);
      row.append(b);
      if (!selecting) {
        const acts = el("span", "a-sess-acts");
        acts.append(iconBtn("a-sess-rename", `Rename “${s.title}”`, ICON.edit));
        if (!s.busy) acts.append(iconBtn("a-sess-del", `Delete “${s.title}”`, ICON.bin));
        row.append(acts);
      }
      return row;
    }) : [el("p", "a-sess-empty", doomed.size ? "No other sessions." : "No sessions yet. Ask for something below.")]));
    renderSelbar();
    $("#a-menu-dot").hidden = !shown.some((s) => s.busy && s.id !== current);
  }
  const sessionOf = (id) => sessionsList.find((s) => s.id === id);

  $("#a-sessions").addEventListener("click", (e) => {
    const row = e.target.closest(".a-sess-row"); if (!row) return;
    const id = row.dataset.id;
    if (e.target.closest(".a-sess-rename")) return renameInList(id);
    if (e.target.closest(".a-sess-del")) return removeSessions([id]);
    if (e.target.closest(".a-rename-input")) return;
    if (selecting) {
      if (sessionOf(id)?.busy) return;
      selected.has(id) ? selected.delete(id) : selected.add(id);
      return renderList(sessionsList, true);
    }
    open(id);
  });
  $("#a-sessions").addEventListener("dblclick", (e) => {
    const row = e.target.closest(".a-sess-row");
    if (row && !selecting && e.target.closest(".a-sess-title")) renameInList(row.dataset.id);
  });
  $("#a-sessions").addEventListener("keydown", (e) => {
    const b = e.target.closest(".a-sess"); if (!b || selecting) return;
    if (e.key === "F2") { e.preventDefault(); renameInList(b.dataset.id); }
    else if ((e.key === "Delete" || e.key === "Backspace") && !sessionOf(b.dataset.id)?.busy) {
      e.preventDefault();
      const next = b.closest(".a-sess-row").nextElementSibling || b.closest(".a-sess-row").previousElementSibling;
      removeSessions([b.dataset.id]);
      $(`.a-sess[data-id="${next?.dataset.id}"]`)?.focus();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const row = b.closest(".a-sess-row");
      const to = e.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
      $(".a-sess", to || row)?.focus();
    }
  });
  // Touch: press and hold a session to start selecting.
  let press = null;
  $("#a-sessions").addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" || selecting || e.target.closest(".a-sess-acts, .a-rename-input")) return;
    const row = e.target.closest(".a-sess-row"); if (!row || sessionOf(row.dataset.id)?.busy) return;
    const x = e.clientX, y = e.clientY;
    press = { x, y, timer: setTimeout(() => {
      press = null; navigator.vibrate?.(12);
      setSelecting(true); selected.add(row.dataset.id); renderList(sessionsList, true);
      swallowClick = true;
    }, 500) };
  });
  const unpress = () => { if (press) { clearTimeout(press.timer); press = null; } };
  $("#a-sessions").addEventListener("pointermove", (e) => { if (press && Math.hypot(e.clientX - press.x, e.clientY - press.y) > 10) unpress(); });
  ["pointerup", "pointercancel", "pointerleave"].forEach((n) => $("#a-sessions").addEventListener(n, unpress));
  $("#a-sessions").addEventListener("contextmenu", (e) => { if (e.target.closest(".a-sess-row") && matchMedia("(hover: none)").matches) e.preventDefault(); });
  let swallowClick = false;
  $("#a-sessions").addEventListener("click", (e) => { if (swallowClick) { swallowClick = false; e.stopImmediatePropagation(); } }, true);

  // Hovering a session warms its cache, so the click paints at once.
  $("#a-sessions").addEventListener("pointerover", (e) => {
    const b = e.target.closest(".a-sess");
    if (b && !cache.has(b.dataset.id)) api(`/sessions/${b.dataset.id}`).then((s) => remember(s, true)).catch(() => {});
  });

  /* rename: in the list or the header, Enter saves, Esc cancels, an empty name lets the agent pick one */
  function editTitle(node, id, done) {
    const s = sessionOf(id) || cache.get(id)?.snap;
    const before = s?.title || node.textContent;
    const box = el("input", "a-rename-input"); box.value = before; box.maxLength = 80;
    box.setAttribute("aria-label", "Session name (empty: let the agent name it)");
    node.hidden = true; node.after(box); box.focus(); box.select();
    let finished = false;
    const finish = async (save) => {
      if (finished) return; finished = true;
      const title = box.value.trim();
      box.remove(); node.hidden = false;
      done();
      if (!save || title === before) return;
      node.textContent = title || before;
      try {
        const r = await api(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
        applyTitle(id, r.title, r.named);
        if (!title) toast("The Master Agent will name this session on its next turn");
      } catch (err) { node.textContent = before; msg(err.message); }
    };
    box.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    box.addEventListener("blur", () => finish(true));
    box.addEventListener("click", (e) => e.stopPropagation());
  }
  function applyTitle(id, title, named) {
    const s = sessionOf(id); if (s) { s.title = title; s.named = named; }
    const entry = cache.get(id); if (entry) { entry.snap.title = title; entry.snap.named = named; }
    if (id === current) renderHead(entry?.snap || { ...s });
    renderList(sessionsList, true);
  }
  function renameInList(id) {
    const node = $(`.a-sess-row[data-id="${id}"] .a-sess-title`); if (!node) return;
    renaming = id;
    editTitle(node, id, () => { renaming = null; $(`.a-sess[data-id="${id}"]`)?.focus({ preventScroll: true }); });
  }
  function renameHead() {
    if (!current || $("#a-session .a-rename-input")) return;
    const id = current;
    editTitle($("#a-title"), id, () => $("#a-input").focus({ preventScroll: true }));
  }
  $("#a-rename").addEventListener("click", renameHead);
  $("#a-title").addEventListener("dblclick", renameHead);

  /* delete: gone from the screen at once, from the server after a few seconds unless undone */
  let doomTimer = null;
  function removeSessions(ids) {
    ids = ids.filter((id) => !sessionOf(id)?.busy && !doomed.has(id));
    if (!ids.length) return;
    for (const id of ids) { doomed.set(id, sessionOf(id)?.title || cache.get(id)?.snap.title || "session"); selected.delete(id); }
    if (doomed.has(current)) open(null);
    renderList(sessionsList, true);
    const n = doomed.size;
    toast(n === 1 ? `Deleted “${[...doomed.values()][0]}”` : `Deleted ${n} sessions`, undoDelete);
    clearTimeout(doomTimer);
    doomTimer = setTimeout(commitDeletes, 6000);
  }
  function undoDelete() {
    clearTimeout(doomTimer);
    const back = [...doomed.keys()]; doomed.clear();
    renderList(sessionsList, true); hideToast();
    if (back.length === 1) open(back[0]);
  }
  async function commitDeletes(keepalive = false) {
    clearTimeout(doomTimer);
    if (!doomed.size) return;
    const ids = [...doomed.keys()];
    hideToast();
    try {
      const r = await api("/sessions/delete", { method: "POST", body: JSON.stringify({ ids }), keepalive });
      for (const id of r.deleted) { doomed.delete(id); cache.delete(id); titles.delete(id); }
      sessionsList = sessionsList.filter((s) => !r.deleted.includes(s.id));
      for (const id of r.busy) doomed.delete(id);
      if (r.busy.length) msg(`${r.busy.length} session${r.busy.length > 1 ? "s were" : " was"} running again, so ${r.busy.length > 1 ? "they weren't" : "it wasn't"} deleted`);
    } catch (err) { ids.forEach((id) => doomed.delete(id)); msg(err.message); }
    renderList(sessionsList, true);
  }
  window.addEventListener("pagehide", () => { if (doomed.size) commitDeletes(true); });

  /* select mode */
  function setSelecting(on) {
    selecting = on; selected.clear();
    $("#a-select").setAttribute("aria-pressed", String(on)); $("#a-select").textContent = on ? "Done" : "Select";
    $("#a-selbar").hidden = !on;
    renderList(sessionsList, true);
  }
  function renderSelbar() {
    const n = selected.size;
    $("#a-sel-count").textContent = `${n} selected`;
    $("#a-sel-delete").disabled = !n;
    $("#a-sel-delete").textContent = n ? `Delete ${n}` : "Delete";
  }
  $("#a-select").addEventListener("click", () => setSelecting(!selecting));
  $("#a-sel-finished").addEventListener("click", () => {
    sessionsList.filter((s) => !s.busy && !doomed.has(s.id)).forEach((s) => selected.add(s.id));
    renderList(sessionsList, true);
  });
  $("#a-sel-delete").addEventListener("click", () => { const ids = [...selected]; setSelecting(false); removeSessions(ids); });

  /* toast */
  let toastTimer = null, toastUndo = null;
  function toast(text, undo = null) {
    $("#a-toast-text").textContent = text;
    toastUndo = undo; $("#a-toast-undo").hidden = !undo;
    const box = $("#a-toast"); box.hidden = false; box.classList.remove("is-in"); void box.offsetWidth; box.classList.add("is-in");
    clearTimeout(toastTimer); toastTimer = setTimeout(hideToast, undo ? 6000 : 3500);
  }
  function hideToast() { clearTimeout(toastTimer); $("#a-toast").hidden = true; toastUndo = null; }
  $("#a-toast-undo").addEventListener("click", () => toastUndo?.());
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && toastUndo && !e.target.closest("input, textarea")) { e.preventDefault(); toastUndo(); }
    else if (e.key === "F2" && current && !e.target.closest("input, textarea, .a-sessions")) { e.preventDefault(); renameHead(); }
    else if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey && !e.target.closest("input, textarea, select, [contenteditable]")) {
      const open = [...document.querySelectorAll('#a-log .a-ev[data-kind="ask"]:not(.is-answered)')].pop();   // the latest open question
      const choice = open?.querySelectorAll(".a-ask-choice")[Number(e.key) - 1];
      if (choice) { e.preventDefault(); choice.click(); }
    }
  });

  /* ------------------------------------------------ timeline -- */
  function remember(snap, full = false) {
    const entry = cache.get(snap.id) || { events: [], last: -1, snap: null };
    for (const e of snap.events || []) {
      if (e.i <= entry.last) continue;
      if (e.kind === "terminal" && e.tool_id != null) {   // drop older copies of this output
        const id = String(e.tool_id);
        let first = true;
        entry.events = entry.events.filter((x) => {
          if (x.kind !== "terminal" || String(x.tool_id) !== id) return true;
          if (!first) return false;
          first = false; x.text = ""; x.error = ""; return true;
        });
      }
      if (e.kind === "sent") for (const x of entry.events) if (e.refs?.includes(x.i)) x.queued = false;
      entry.events.push(e); entry.last = e.i;
    }
    if (full && entry.events.length > 3000) entry.events.splice(0, entry.events.length - 3000);
    entry.snap = { ...snap, events: undefined };
    cache.set(snap.id, entry);
    return entry;
  }

  function attachmentThumb(a) {
    const previewable = a.kind === "image" || a.kind === "video";
    const box = el(previewable ? "button" : "a", "a-att");
    box.title = `${a.ref || ""} ${a.name}`.trim();
    if (previewable) {
      box.type = "button";
      box.addEventListener("click", (event) => { event.preventDefault(); openGallery("sent", a.id); });
    } else { box.href = a.url; box.target = "_blank"; box.rel = "noopener"; }
    if (a.kind === "image") { const img = el("img"); img.src = a.url; img.alt = a.name; img.loading = "lazy"; img.decoding = "async"; box.append(img); }
    else if (a.kind === "video") { const v = el("video"); v.src = a.url + "#t=0.1"; v.muted = true; v.preload = "metadata"; v.playsInline = true; box.append(v); }
    else box.append(el("span", "a-att-icon", a.kind === "audio" ? "♪" : "📄"));
    box.append(el("span", "a-att-name", `${a.ref ? a.ref + " · " : ""}${a.name}`));
    return box;
  }

  function eventNode(e) {
    if (e.kind === "think") return thinkNode(e.text || "");
    const li = el("li", "a-ev");
    li.dataset.kind = e.kind;
    if (e.attachments?.length && e.kind === "user") li.classList.add("has-attachments");
    if (e.text) {
      const text = e.kind === "assistant" || e.kind === "say" ? renderMd(e.text)
        : el("span", "a-ev-text", e.kind === "tool" ? `⚡ ${e.text}` : e.text);
      text.classList.add("a-ev-text");
      if (e.kind === "user") text.replaceChildren(...e.text.split(/(@\{[^}\n]*\}?(?:\s+(?:all|random\s+\d+))?|(?<!\w)@[\w.-]+(?:\s+(?:all|random\s+\d+))?|^```[^\n]*$)/gm).map((part) => {
        if (/^@\{|^@[\w.-]+/.test(part)) return el("span", "a-syntax-mention", part);
        if (part.startsWith("```")) return el("span", "a-syntax-fence", part);
        return document.createTextNode(part);
      }));
      if (e.attachments?.length && e.kind === "user") {
        const bubble = el("div", "a-ev-bubble"); bubble.append(text); li.append(bubble);
      } else li.append(text);
    }
    if (e.text && (e.kind === "user" || e.kind === "assistant" || e.kind === "say")) {
      li.classList.add("has-copy");
      li.append(copyButton(() => e.text, e.kind === "user" ? "your message" : "message", "a-copy-msg"));
    }
    if (e.command) li.classList.add("is-command");
    if (e.queued) { li.classList.add("is-queued"); li.dataset.i = e.i; }
    if (e.attachments?.length) {
      // Videos the agent downloaded or made play right in the chat; the rest are thumbnails.
      const videos = e.kind === "user" ? [] : e.attachments.filter((a) => a.kind === "video");
      const rest = e.attachments.filter((a) => !videos.includes(a));
      if (rest.length) { const row = el("div", "a-ev-atts"); row.append(...rest.map(attachmentThumb)); li.append(row); }
      for (const a of videos) li.append(inlineVideo(a));
    }
    if (e.kind === "ask") askCard(li, e);
    return li;
  }

  // The agent's question: a card with its choices (1-9 pick one); any later message of yours answers it.
  const ASK_ICON = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4.5"/><path d="M12 18h.01"/></svg>';
  function askCard(li, e) {
    const head = el("div", "a-ask-head");
    const badge = el("span", "a-ask-badge"); badge.innerHTML = ASK_ICON;
    head.append(badge, el("span", "a-ask-label", "The Master Agent asks"), el("span", "a-ask-state", "Waiting for you"));
    li.prepend(head);
    if (e.choices?.length) {
      const row = el("div", "a-ev-choices");
      e.choices.forEach((c, n) => {
        const b = el("button", "a-ask-choice"); b.type = "button"; b.dataset.choice = c;
        if (n < 9) { const key = el("kbd", "a-ask-key", String(n + 1)); key.title = `Press ${n + 1}`; b.append(key); }
        b.append(el("span", "a-ask-choice-text", c));
        b.addEventListener("click", () => { answerAsk(li, c); deliver({ text: c, files: [] }); });
        row.append(b);
      });
      li.append(row);
    }
    const own = el("button", "a-ask-own", e.choices?.length ? "Or type your own answer" : "Type your answer"); own.type = "button";
    own.addEventListener("click", () => $("#a-input").focus());
    li.append(own);
  }
  function answerAsk(li, text) {
    if (li.classList.contains("is-answered")) return;
    li.classList.add("is-answered");
    $(".a-ask-state", li).textContent = "Answered";
    li.querySelectorAll("button").forEach((b) => { b.disabled = true; if (b.dataset.choice === text) b.classList.add("is-picked"); });
  }
  const answerOpenAsks = (text) => document.querySelectorAll('#a-log .a-ev[data-kind="ask"]:not(.is-answered)').forEach((n) => answerAsk(n, text));

  // "Keep the model loaded after a question" with a timer: a countdown under the open question until it unloads.
  let parkTick = null;
  function renderParked(p) {
    clearInterval(parkTick); parkTick = null;
    document.querySelectorAll("#a-log .a-ask-timer").forEach((n) => n.remove());
    const until = p?.until;
    const card = until && [...document.querySelectorAll('#a-log .a-ev[data-kind="ask"]:not(.is-answered)')].pop();
    if (!card) return;
    const line = el("div", "a-ask-timer");
    const text = el("span", "a-ask-timer-text"), bar = el("span", "a-ask-timer-bar"), fill = el("span");
    bar.append(fill); line.append(text, bar); card.append(line);
    const total = Math.max(1, until - Date.now() / 1000);
    const tick = () => {
      const left = Math.max(0, Math.round(until - Date.now() / 1000));
      text.textContent = left ? `Model stays loaded for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Unloading the model…";
      fill.style.width = `${Math.min(100, (left / total) * 100)}%`;
      if (!left) { clearInterval(parkTick); parkTick = null; }
    };
    tick(); parkTick = setInterval(tick, 1000);
  }

  // Where a job's card sits: after the line where it finished (or failed), else where it started.
  const jobEnds = (e) => e.job && !e.text.startsWith("Started ") && (e.kind === "job" || e.kind === "warn");
  function placeCard(ref, after) {
    const entry = cache.get(current); if (!entry) return;
    const job = (entry.snap.jobs || []).find((j) => j.ref === ref);
    if (!job) return;
    let card = cards.get(ref);
    if (!card) { card = jobCard(job); cards.set(ref, card); updateCard(card, job); }
    after.after(card);
  }

  /* ------------------------------------------- follow the bottom -- */
  // Opening a session jumps to its bottom and keeps following new lines. Scrolling up stops
  // following; scrolling back to the bottom (or the "Scroll to the bottom" button) resumes it.
  let follow = true, lastTop = 0;
  const gapBelow = () => { const h = $("#a-session"); return h.scrollHeight - h.scrollTop - h.clientHeight; };
  function updateJump() { $("#a-jump").hidden = !current || $("#a-session").hidden || gapBelow() < 80; }
  function toBottom(smooth = false) {
    const h = $("#a-session");
    h.scrollTo({ top: h.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    if (!smooth) lastTop = h.scrollTop;
  }
  function followBottom(smooth = false) { follow = true; toBottom(smooth); updateJump(); }
  function stick() { if (follow) toBottom(); updateJump(); }

  function appendEvents(events) {
    if (!events.length) return;
    const log = $("#a-log");
    const scrollOutputs = new Set();   // scrolled once, after the batch: reading scrollHeight per event forced a full layout each time
    for (const e of events) {
      if (e.kind === "terminal") {
        const id = String(e.tool_id || e.i);
        let panel = terminals.get(id);
        if (!panel || !panel.isConnected) {
          panel = el("li", "a-terminal"); panel.dataset.toolId = id; terminals.set(id, panel);
          // Same fold as thinking: chevron, label, one-line peek of the command, status on the right.
          const details = el("details", "a-think a-term");
          const summary = el("summary");
          summary.append(el("span", "a-think-icon"), el("span", "a-think-label a-term-label", "Terminal"),
            el("span", "a-think-peek a-term-peek"), el("span", "a-think-meta a-term-meta", "running"));
          const body = el("div", "a-term-body");
          body.append(el("pre", "a-terminal-command"), el("pre", "a-terminal-output", "Running…"));
          details.append(summary, body); details.open = true;
          // Once the user folds/unfolds it, completion no longer auto-folds it.
          summary.addEventListener("click", () => { panel.dataset.touched = "1"; });
          panel.append(details); log.append(panel);
        }
        const previousPhase = panel.dataset.phase;
        const phase = String(e.phase || "");
        if (previousPhase === "awaiting_approval" && phase && phase !== "awaiting_approval") {
          log.append(panel);
          panel.classList.remove("terminal-arriving");
          void panel.offsetWidth;
          panel.classList.add("terminal-arriving");
        }
        if (phase) panel.dataset.phase = phase;
        $(".a-terminal-command", panel).textContent = "$ " + (e.command || "");
        $(".a-term-peek", panel).textContent = (e.command || "").replace(/\s+/g, " ").trim().slice(0, 160);
        const terminalOutput = $(".a-terminal-output", panel);
        const output = [e.text, e.error].filter(Boolean).join("\n");
        // A blank event is the panel's place in the timeline; its output arrives in a later one.
        if (output || !terminalOutput.dataset.filled) {
          terminalOutput.textContent = output || (e.status === "running" ? "Running…" : "(no output)");
          if (output) terminalOutput.dataset.filled = "1";
          scrollOutputs.add(terminalOutput);
        }
        if (e.status === "complete" || e.status === "failed") {
          panel.dataset.status = e.error || (e.exit_code != null && e.exit_code !== 0) ? "failed" : "complete";
          $(".a-term-meta", panel).textContent = panel.dataset.status === "failed"
            ? `exit ${e.exit_code ?? "error"}` : "complete";
          // Like thoughts: short output stays open, long output folds to the one-line peek.
          if (!panel.dataset.touched && terminalOutput.textContent.length > 280) $(".a-term", panel).open = false;
        }
        continue;
      }
      if (e.kind === "sent") {           // queued messages the agent has now read
        for (const i of e.refs || []) log.querySelector(`.a-ev.is-queued[data-i="${i}"]`)?.classList.remove("is-queued");
        continue;
      }
      if (e.kind === "benchmark") {
        const run = e.run || "benchmark";
        let panel = [...log.querySelectorAll(".a-benchmark")].find((n) => n.dataset.run === run);
        if (!panel) {
          panel = el("li", "a-benchmark"); panel.dataset.run = run;
          panel.append(el("div", "a-benchmark-title", "llama.cpp benchmark"), el("pre", "a-benchmark-output"));
          log.append(panel);
        }
        const output = $("pre", panel);
        output.textContent += `${e.text || ""}\n`;
        output.scrollTop = output.scrollHeight;
        continue;
      }
      if (e.kind === "user") {           // the server's copy of a line we already showed
        answerOpenAsks(e.text);
        const mine = [...log.querySelectorAll(".a-ev.is-pending")].find((n) => n.dataset.text === e.text);
        if (mine) { mine.replaceWith(eventNode(e)); continue; }
      }
      const node = eventNode(e);
      log.append(node);
      if (e.kind === "board" && e.board) placeBoard(e.board, node);
      if (e.job && (jobEnds(e) || !cards.has(e.job))) placeCard(e.job, node);
    }
    requestAnimationFrame(() => {
      for (const o of scrollOutputs) o.scrollTop = o.scrollHeight;
      stick();
    });
  }

  // Streaming text (snap.live, pushed ~10x a second): thinking in an open, shimmering panel that
  // follows its tail; the reply rendered as markdown with a blinking caret.
  function renderLive(live = {}) {
    const log = $("#a-log");
    for (const [key, kind] of [["reasoning", "think"], ["assistant", "assistant"]]) {
      let node = log.querySelector(`.a-ev[data-live="${key}"]`);
      const value = live[key] || "";
      if (!value) { node?.remove(); continue; }
      if (!node) {
        node = kind === "think" ? thinkNode("", true) : el("li", "a-ev");
        node.classList.add("is-live");
        node.dataset.kind = kind;
        node.dataset.live = key;
      }
      if (node._value !== value) {
        node._value = value;
        if (kind === "think") updateThink(node, value, true);
        else {
          const md = renderMd(value);
          let tail = md;
          while (tail.lastChild && tail.lastChild === tail.lastElementChild && !/^(PRE|TABLE|HR|BR|DIV)$/.test(tail.lastChild.tagName)) tail = tail.lastChild;
          tail.append(el("span", "a-caret"));
          node.replaceChildren(md);
        }
      }
      if (node !== log.lastElementChild && !(key === "reasoning" && node.nextElementSibling?.dataset.live === "assistant")) log.append(node);
      if (kind === "think") { const body = $(".a-think-body", node); if (!body._held) body.scrollTop = body.scrollHeight; }   // once attached
    }
  }

  /* ------------------------------------------------ thinking + markdown -- */
  const wordCount = (t) => (t.match(/\S+/g) || []).length;
  function thinkNode(text, live = false) {
    const li = el("li", "a-ev");
    li.dataset.kind = "think";
    const details = el("details", "a-think");
    const summary = el("summary");
    summary.append(el("span", "a-think-icon"), el("span", "a-think-label", live ? "Thinking" : "Thought"),
      el("span", "a-think-peek"), el("span", "a-think-meta"));
    const body = el("div", "a-think-body");
    // Live: follow the tail unless the user scrolled up to read; finished: "Show all" lifts the cap.
    body.addEventListener("scroll", () => { body._held = body.scrollHeight - body.scrollTop - body.clientHeight > 24; body.classList.toggle("is-tail", !body._held); }, { passive: true });
    const more = el("button", "a-think-more", "Show all"); more.type = "button"; more.hidden = true;
    more.addEventListener("click", () => { const full = body.classList.toggle("is-full"); body.style.height = ""; more.textContent = full ? "Show less" : "Show all"; });
    details.addEventListener("toggle", () => { if (details.open && !li.classList.contains("is-live")) more.hidden = !body.classList.contains("is-full") && body.scrollHeight <= body.clientHeight + 4; });
    details.append(summary, body, more);
    li.append(details);
    // Short thoughts stay open; long ones fold to a one-line peek. Live thinking is always open.
    details.open = live || text.length <= 280;
    if (text) updateThink(li, text, live);
    return li;
  }
  function updateThink(li, text, live) {
    const body = $(".a-think-body", li);
    body.textContent = text;
    $(".a-think-peek", li).textContent = text.replace(/\s+/g, " ").trim().slice(0, 160);
    const n = wordCount(text);
    $(".a-think-meta", li).textContent = `${n.toLocaleString()} word${n === 1 ? "" : "s"}`;
    if (live && !body._held) { body.classList.add("is-tail"); body.scrollTop = body.scrollHeight; }
    const more = $(".a-think-more", li);
    if (more && !live) requestAnimationFrame(() => { more.hidden = !body.classList.contains("is-full") && body.scrollHeight <= body.clientHeight + 4; });
  }

  // Small, DOM-built (never innerHTML) markdown: fences, headings, lists, quotes, tables, rules,
  // `code`, **bold**, *italic*, ~~strike~~, [links](https://…) and bare URLs.
  const INLINE_MD = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*|__[^_\n]+?__)|(~~[^~\n]+?~~)|((?<![\w*])\*[^*\s](?:[^*\n]*[^*\s])?\*(?!\w)|(?<!\w)_[^_\s](?:[^_\n]*[^_\s])?_(?!\w))|(\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  function inlineMd(node, text) {
    let at = 0;
    for (const m of text.matchAll(INLINE_MD)) {
      if (m.index > at) node.append(text.slice(at, m.index));
      const [all, code, bold, strike, em, link, url] = m;
      if (code) node.append(el("code", "", code.slice(1, -1)));
      else if (bold) { const b = el("strong"); inlineMd(b, bold.slice(2, -2)); node.append(b); }
      else if (strike) { const d = el("del"); inlineMd(d, strike.slice(2, -2)); node.append(d); }
      else if (em) { const i = el("em"); inlineMd(i, em.slice(1, -1)); node.append(i); }
      else {
        const [, label, href] = link ? link.match(/^\[([^\]]+)\]\((.+)\)$/) : [null, url, url];
        const a = el("a", "", label); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
        node.append(a);
      }
      at = m.index + all.length;
    }
    if (at < text.length) node.append(text.slice(at));
  }
  const tableCells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  // Copy buttons: every message you sent or the agent wrote, and every code block it wrote.
  const COPY_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>';
  const COPIED_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* plain http or no permission: old way */ }
    const area = el("textarea"); area.value = text; area.setAttribute("readonly", ""); area.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.append(area); area.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch { ok = false; }
    area.remove(); return ok;
  }
  function copyButton(getText, what, cls = "") {
    const b = el("button", `a-copy ${cls}`.trim()); b.type = "button"; b.innerHTML = COPY_ICON;
    b.title = `Copy ${what}`; b.setAttribute("aria-label", `Copy ${what}`);
    b.addEventListener("click", async (event) => {
      event.stopPropagation();
      const ok = await copyText(getText());
      b.innerHTML = ok ? COPIED_ICON : COPY_ICON; b.classList.toggle("is-copied", ok);
      b.title = ok ? "Copied" : "Couldn't copy"; if (!ok) msg("Couldn't copy to the clipboard");
      clearTimeout(b._reset); b._reset = setTimeout(() => { b.innerHTML = COPY_ICON; b.classList.remove("is-copied"); b.title = `Copy ${what}`; }, 1400);
    });
    return b;
  }

  // Code blocks open full screen (an overlay, so phones work too; Esc or the button closes it).
  const EXPAND_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>';
  const SHRINK_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8h5V3M21 8h-5V3M3 16h5v5M21 16h-5v5"/></svg>';
  let fullCode = null;
  function setFullCode(box, on) {
    if (fullCode && fullCode !== box) setFullCode(fullCode, false);
    box.classList.toggle("is-full", on);
    document.body.classList.toggle("a-code-full", on);
    const b = $(".a-full-code", box);
    b.innerHTML = on ? SHRINK_ICON : EXPAND_ICON;
    b.title = on ? "Exit full screen (Esc)" : "Full screen"; b.setAttribute("aria-label", b.title);
    fullCode = on ? box : null;
    if (on) b.focus();
  }
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && fullCode) { event.preventDefault(); setFullCode(fullCode, false); } });
  function fullButton(box) {
    const b = el("button", "a-copy a-full-code"); b.type = "button"; b.innerHTML = EXPAND_ICON;
    b.title = "Full screen"; b.setAttribute("aria-label", "Full screen");
    b.addEventListener("click", (event) => { event.stopPropagation(); setFullCode(box, !box.classList.contains("is-full")); });
    return b;
  }

  // Which language an unlabelled code block is in: a few strong tells per language (highlight.js's
  // own auto-detection called Python "css" and an H3 prompt "vbnet"). Nothing clear -> null = plain text.
  const LANG_TELLS = {
    python: [/^\s*def \w+\(.*\)\s*(->.*)?:\s*$/m, /^\s*(from [\w.]+ )?import [\w., ]+$/m, /^\s*class \w+(\(.*\))?:\s*$/m, /\bself\./, /\bprint\(/, /^\s*(elif|except|with) .*:\s*$/m, /__name__ == /],
    javascript: [/\b(const|let) \w+ =/, /=>/, /\bconsole\.\w+\(/, /\bfunction\s*\w*\(/, /\bawait \w+/, /\bdocument\.|\bwindow\./, /^\s*import .* from ["']/m, /\bmodule\.exports\b|\brequire\(["']/],
    typescript: [/:\s*(string|number|boolean|void)\b/, /^\s*(export )?(interface|type) \w+/m, /\bas (string|number|const)\b/],
    bash: [/^\s*(\$ )?(sudo |cd |ls\b|docker |git |npm |npx |pip |curl |wget |echo |export |grep |apt |pacman |systemctl |cat |mkdir |chmod |rm |cp |mv |source |python3? |node )/m, /^#!\/(usr\/)?bin\/(env )?(ba|z|fi)?sh/m, /\s&&\s|\s\|\|\s/, /\$\{?\w+\}?/],
    csharp: [/\busing (System|UnityEngine)[\w.]*;/, /\b(public|private|protected) (static )?(class|void|int|string|bool|float|override)\b/, /\bnamespace \w+/, /\bMonoBehaviour\b/, /\bvar \w+ = new\b/],
    cpp: [/^\s*#include\s*[<"]/m, /\bstd::/, /\bint main\s*\(/, /\b(cout|cin|printf)\b/],
    java: [/\bpublic static void main\b/, /\bSystem\.out\.print/, /\bimport java\./],
    rust: [/\bfn \w+\s*[<(]/, /\blet mut\b/, /\bprintln!\(/, /\bimpl\b.*\{/, /::new\(/],
    go: [/^package \w+/m, /\bfunc (\(\w+ \*?\w+\) )?\w+\(/, /:= /, /\bfmt\.\w+\(/],
    php: [/<\?php/, /\$\w+\s*=.*;/],
    lua: [/^\s*local \w+\s*=/m, /\bfunction [\w.:]+\(.*\)[\s\S]*\bend\b/, /\bthen\b[\s\S]*\bend\b/],
    sql: [/^\s*(SELECT|INSERT INTO|UPDATE|DELETE FROM|CREATE (TABLE|INDEX|VIEW)|ALTER TABLE|WITH \w+ AS)\b/im, /\b(FROM|WHERE|JOIN|GROUP BY|ORDER BY|VALUES)\b/],
    css: [/^\s*[.#]?[\w-]+(\s*[,>+~ ]\s*[.#]?[\w-:]+)*\s*\{\s*$/m, /^\s*[\w-]+\s*:\s*[^;]+;\s*$/m, /@media\b|:root\b|var\(--/],
    xml: [/^\s*<!DOCTYPE html>|^\s*<\?xml/im, /<\/(div|span|html|body|head|p|a|ul|li|script|style|svg)>/i, /<(div|span|html|body|head|meta|link|script)\b[^>]*>/i],
    diff: [/^@@ .* @@/m, /^(\+\+\+|---) \S/m, /^[+-](?![+-])/m],
    ini: [/^\s*\[[\w. -]+\]\s*$/m, /^\s*[\w.-]+\s*=\s*\S/m],
    yaml: [/^\s*[\w.-]+:\s*$/m, /^\s+- \S/m, /^\s{2,}[\w.-]+:\s+\S{1,60}$/m],
  };
  // MiniMax H3 prompts get Video's own prompt colours (its PROMPT_SYNTAX / highlightPrompt in
  // Video/static/js/app.js, without the LoRA triggers and aliases, which need its live LoRA list).
  const H3_TELLS = /\b(integrated_multimodal_description|overall_soundscape|non_diegetic_music|subject_definitions|retention_analysis|detailed_description)\s*:|<(Subject|Picture|Video|Audio) \d+>|\[Shot \d+\]|<d>\s*\[/i;
  const H3_TASK_TYPES = ["keyframe completion", "reference generation", "video editing", "video continuation", "audio reuse", "audio reference"];
  const H3_RETENTION_MARKERS = ["fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference", "fully_copy", "partially_copy"];
  const H3_SYNTAX = new RegExp([
    String.raw`(?<dialogue><d>)(?<lang>\s*\[[^\]\n]*\])?(?<speech>.*?)(?<dialogueEnd><\/d>|$)`,
    String.raw`(?<ref><\s*(?:[Pp]icture|[Ii]mage|[Vv]ideo|[Aa]udio|[Ss]ubject)\s+\d+\s*>)`,
    String.raw`(?<control><scenetrans>|<cutoff>|\[unclear\])`,
    String.raw`(?<task>\[(?:${H3_TASK_TYPES.join("|")})(?: \+ (?:${H3_TASK_TYPES.join("|")}))*\])`,
    String.raw`(?<marker>\b(?:${H3_RETENTION_MARKERS.join("|")})\b|(?<=>:[ \t]*)reference(?=[ \t]+-))`,
    String.raw`(?<bare>\b(?:Image|Picture|Audio|Video|Subject) \d+\b)`,
    String.raw`(?<camera>\bwith (?:small|large) amplitude\b|\bat (?:slow|fast) speed\b|\b[Tt]he camera (?:\w+ly )?(?:pushes in|pulls out|zooms (?:in|out)|pans (?:left|right)|trucks (?:left|right)|tilts (?:up|down)|pedestals (?:up|down)|arcs(?: around)?|tracks|holds a static shot|shakes (?:slightly|strongly)|rolls (?:counter)?clockwise)\b|\b(?:Push In|Pull Out|Zoom In|Zoom Out|Pan Left|Pan Right|Truck Left|Truck Right|Tilt Up|Tilt Down|Pedestal Up|Pedestal Down|Arc Shot|Tracking Shot|Static Shot|Shake Slightly|Shake Strongly|Roll Clockwise|Roll Counterclockwise)\b|\bPOV(?: shot)?\b)`,
    String.raw`(?<framing>(?<![\w-])(?:[Ee]xtreme [Cc]lose-[Uu]p|[Cc]lose-[Uu]p|[Cc]lose [Ss]hot|[Mm]edium-[Ww]ide [Ss]hot|[Mm]edium [Ss]hot)(?![\w-]))`,
    String.raw`(?<key>^(?:[a-z]+(?:_[a-z]+)+|summary):)`,
    String.raw`(?<speaker>^[A-Z][A-Z0-9 .'-]*[A-Z0-9]:)`,
    String.raw`(?<shot>\[Shot\s*\d+\])`,
    String.raw`(?<speakerId>\(S\d+(?:\s*,\s*S\d+)*\))`,
    String.raw`(?<time>\b\d{1,2}:\d{2}(?:\.\d{1,3})?\b|\b\d+(?:\.\d+)?(?=[- ]seconds?\b))`,
    String.raw`(?<quote>“[^”\n]*”?|"[^"\n]*"?)`,
  ].join("|"), "gm");
  const h3Category = (token) => /video/i.test(token) ? "video" : /audio/i.test(token) ? "audio" : /subject/i.test(token) ? "subject" : "image";
  function highlightH3(code, text) {          // DOM-built spans, like the rest of renderMd
    const span = (cls, value, category) => { const s = el("span", `hl-${cls}`, value); if (category) s.dataset.category = category; return s; };
    let last = 0;
    for (const match of text.matchAll(H3_SYNTAX)) {
      if (!match[0]) { H3_SYNTAX.lastIndex++; continue; }
      code.append(text.slice(last, match.index));
      last = match.index + match[0].length;
      const g = match.groups;
      if (g.dialogue) {
        code.append(span("tag", g.dialogue));
        if (g.lang) code.append(span("lang", g.lang));
        g.speech.split(/(<scenetrans>|<cutoff>|\[unclear\])/).forEach((part, i) => { if (part) code.append(span(i % 2 ? "tag" : "speech", part)); });
        if (g.dialogueEnd) code.append(span("tag", g.dialogueEnd));
      } else if (g.ref || g.bare) code.append(span("ref", g.ref || g.bare, h3Category(g.ref || g.bare)));
      else if (g.control) code.append(span("tag", g.control));
      else if (g.task) code.append(span("task", g.task));
      else if (g.marker) code.append(span("marker", g.marker));
      else if (g.camera) code.append(span("camera", g.camera));
      else if (g.framing) code.append(span("framing", g.framing));
      else if (g.key || g.speaker) code.append(span("key", g.key || g.speaker));
      else if (g.shot) code.append(span("shot", g.shot));
      else if (g.speakerId) code.append(span("speaker-id", g.speakerId));
      else if (g.time) code.append(span("time", g.time));
      else if (g.quote) code.append(span("speech", g.quote));
    }
    code.append(text.slice(last));
  }

  // Prompts and prose with a "key: value" look (H3 prompts, scripts, lyrics) stay plain text.
  const PLAIN_TELLS = /\b(integrated_multimodal_description|overall_soundscape|non_diegetic_music|subject_definitions|retention_analysis|detailed_description)\s*:|<(Subject|Picture|Video|Audio) \d+>|^\s*\[(Verse|Chorus|Bridge|Intro|Outro|Pre-Chorus|Shot \d+)\]/im;
  function detectLang(code) {
    if (!code.trim() || PLAIN_TELLS.test(code)) return null;
    const t = code.trim();
    if (/^[[{]/.test(t)) { try { JSON.parse(t); return "json"; } catch { /* not JSON */ } }
    let best = null, bestScore = 0;
    for (const [lang, tells] of Object.entries(LANG_TELLS)) {
      const score = tells.reduce((n, re) => n + (re.test(code) ? 1 : 0), 0);
      if (score > bestScore) { best = lang; bestScore = score; }
    }
    if (best === "javascript" && LANG_TELLS.typescript.some((re) => re.test(code))) best = "typescript";
    // One tell is enough for a short snippet; prose-like text needs two.
    const sentences = (code.match(/[a-z]{3,}[.!?](\s|$)/g) || []).length;
    return bestScore >= (sentences > 1 ? 2 : 1) ? best : null;
  }

  // A code block: a known language (named on the fence, or detected) gets highlight.js colours; plain
  // text (an H3 prompt, a script, lyrics, prose) wraps instead of scrolling sideways.
  const PLAIN_LANGS = new Set(["", "text", "txt", "plain", "plaintext", "prompt"]);
  const LANG_ALIASES = { gdscript: "python", gd: "python", sh: "bash", shell: "bash", zsh: "bash", fish: "bash", console: "bash",
                         html: "xml", svg: "xml", cs: "csharp", "c#": "csharp", toml: "ini", jsonc: "json" };
  function codeBlock(source, named) {
    const pre = el("pre", "a-md-code"), code = el("code");
    const want = (named || "").toLowerCase();
    // An H3 prompt (labelled h3/minimax, or plain/unlabelled with H3 syntax in it): Video's colours, wrapped.
    if (["h3", "minimax", "minimax-h3", "hailuo"].includes(want) || (PLAIN_LANGS.has(want) && H3_TELLS.test(source))) {
      highlightH3(code, source);
      pre.classList.add("is-plain", "is-h3"); pre.append(code);
      return { pre, label: "h3" };
    }
    let lang = PLAIN_LANGS.has(want) ? (want ? null : detectLang(source)) : (LANG_ALIASES[want] || want);
    const hl = window.hljs && lang && hljs.getLanguage(lang) ? lang : null;
    if (hl) {
      try { code.innerHTML = hljs.highlight(source, { language: hl, ignoreIllegals: true }).value; code.classList.add("hljs"); }  // hljs escapes the text
      catch { code.textContent = source; }
    } else code.textContent = source;
    if (!lang) pre.classList.add("is-plain");
    pre.append(code);
    return { pre, label: want && !PLAIN_LANGS.has(want) ? want : lang || "text" };
  }

  function renderMd(text) {
    const root = el("div", "a-md");
    const lines = String(text).replace(/\r/g, "").split("\n");
    let para = null, list = null, quote = null;
    const close = () => { para = list = quote = null; };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)\s*([\w+#.-]*)/);
      if (fence) {
        close();
        const buf = [];
        while (++i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i]);
        const { pre, label } = codeBlock(buf.join("\n"), fence[2]);
        const box = el("div", "a-md-codebox"), head = el("div", "a-md-codehead");
        const tools = el("span", "a-md-codetools");
        tools.append(copyButton(() => buf.join("\n"), fence[2] ? `${fence[2]} code` : "code", "a-copy-code"), fullButton(box));
        head.append(el("span", "a-md-codelang", label), tools);
        box.append(head, pre);
        root.append(box);
        continue;
      }
      if (!line.trim()) { close(); continue; }
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) { close(); const h = el(`h${Math.min(heading[1].length + 2, 6)}`); inlineMd(h, heading[2]); root.append(h); continue; }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { close(); root.append(el("hr")); continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || "")) {
        close();
        const table = el("table"), head = el("tr");
        for (const c of tableCells(line)) { const th = el("th"); inlineMd(th, c); head.append(th); }
        table.append(el("thead"), el("tbody")); table.tHead.append(head);
        for (i += 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) {
          const tr = el("tr");
          for (const c of tableCells(lines[i])) { const td = el("td"); inlineMd(td, c); tr.append(td); }
          table.tBodies[0].append(tr);
        }
        i--;
        const wrap = el("div", "a-md-table"); wrap.append(table); root.append(wrap);
        continue;
      }
      const item = line.match(/^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$/);
      if (item) {
        const tag = /\d/.test(item[2]) ? "OL" : "UL";
        if (!list || list.tagName !== tag) {
          para = quote = null;
          list = el(tag.toLowerCase());
          if (tag === "OL" && parseInt(item[2], 10) !== 1) list.start = parseInt(item[2], 10);
          root.append(list);
        }
        const li = el("li");
        const task = item[3].match(/^\[([ xX])\]\s+(.*)$/);
        if (task) { li.classList.add("is-task"); li.dataset.done = task[1] !== " " ? "1" : ""; inlineMd(li, task[2]); }
        else inlineMd(li, item[3]);
        list.append(li);
        continue;
      }
      const q = line.match(/^\s*>\s?(.*)$/);
      if (q) {
        if (!quote) { para = list = null; quote = el("blockquote"); root.append(quote); }
        else quote.append(el("br"));
        inlineMd(quote, q[1]);
        continue;
      }
      if (list && /^\s{2,}\S/.test(line)) { list.lastElementChild.append(el("br")); inlineMd(list.lastElementChild, line.trim()); continue; }
      if (para) para.append(el("br"));
      else { list = quote = null; para = el("p"); root.append(para); }
      inlineMd(para, line);
    }
    return root;
  }

  /* ------------------------------------------------ job cards (in place) -- */
  function outputNode(out, job, index) {
    if (out.kind === "video") {
      const v = el("video"); v.src = out.url; v.poster = out.thumb || ""; v.controls = true; v.preload = "none"; v.playsInline = true; return v;
    }
    if (out.kind === "image") {
      const b = el("button", "a-job-img"); b.type = "button"; b.setAttribute("aria-label", `View ${job.ref} image ${index + 1}`);
      const img = el("img"); img.src = out.thumb || out.url; img.alt = ""; img.decoding = "async"; img.loading = "lazy"; b.append(img);
      b.addEventListener("click", () => openGallery("made", `${job.ref}:${index}`));
      return b;
    }
    if (out.kind === "audio") { const au = el("audio"); au.src = out.url; au.controls = true; au.preload = "none"; return au; }
    return el("span", "a-ph", out.name || "Finished");
  }

  function media(job, startIndex = 0) {
    const current = job.full_output ? [] : (job.outputs || []);
    const attempts = job.iterations?.length ? job.iterations : current;
    const showingAttempts = attempts === job.iterations && attempts.length > 1;
    const outs = showingAttempts ? [attempts[Math.min(startIndex, attempts.length - 1)]] : attempts;
    const box = el("div", "a-job-media");
    box.hidden = !!job.full_output && !job.iterations?.length;
    box.dataset.count = Math.min(outs.length, 4);
    box.dataset.iterationIndex = showingAttempts ? String(Math.min(startIndex, attempts.length - 1)) : "0";
    if (showingAttempts) {
      const groups = [];
      const byClip = new Map();
      attempts.forEach((item, index) => {
        const key = String(item.clip ?? "?");
        if (!byClip.has(key)) { byClip.set(key, groups.length); groups.push({ key, items: [] }); }
        groups[byClip.get(key)].items.push({ item, index });
      });
      const initial = attempts[Math.min(startIndex, attempts.length - 1)];
      let clipIndex = Math.max(0, groups.findIndex((group) => group.items.some(({ item }) => item === initial)));
      let iterationIndex = Math.max(0, groups[clipIndex].items.findIndex(({ item }) => item === initial));
      const content = el("div", "a-job-iteration-content");
      const clipNav = el("div", "a-job-clip-nav");
      const clipPrevious = el("button", "a-job-clip-prev", "‹");
      const clipLabel = el("span", "a-job-clip-label", `Clip ${groups[clipIndex].key}`);
      const clipNext = el("button", "a-job-clip-next", "›");
      clipPrevious.type = clipNext.type = "button";
      clipNav.hidden = groups.length < 2;
      const iterationNav = el("div", "a-job-iteration-nav");
      const previous = el("button", "a-job-iteration-prev", "‹");
      const label = el("span", "a-job-iteration-label");
      const next = el("button", "a-job-iteration-next", "›");
      previous.type = next.type = "button";
      const show = () => {
        const group = groups[clipIndex];
        const selected = group.items[iterationIndex];
        box.dataset.iterationIndex = String(selected.index);
        box.dataset.iterationUrl = selected.item.url || "";
        content.replaceChildren(outputNode(selected.item, job, selected.index));
        clipLabel.textContent = `Clip ${group.key}`;
        // Final renders (retries too) come first per clip, then the staging previews.
        const kind = selected.item.phase === "full" ? "Final" : "Preview";
        const retried = group.items.filter(({ item }) => item.phase === selected.item.phase).length > 1;
        label.textContent = `${kind}${retried && selected.item.selected ? " (kept)" : ""} · ${iterationIndex + 1} of ${group.items.length}`;
        clipPrevious.disabled = clipIndex === 0; clipNext.disabled = clipIndex === groups.length - 1;
        previous.disabled = iterationIndex === 0; next.disabled = iterationIndex === group.items.length - 1;
      };
      clipPrevious.addEventListener("click", () => { clipIndex -= 1; iterationIndex = 0; show(); });
      clipNext.addEventListener("click", () => { clipIndex += 1; iterationIndex = 0; show(); });
      previous.addEventListener("click", () => { iterationIndex -= 1; show(); });
      next.addEventListener("click", () => { iterationIndex += 1; show(); });
      clipNav.append(clipPrevious, clipLabel, clipNext);
      iterationNav.append(previous, label, next);
      box.append(clipNav, iterationNav, content); show();
    } else if (outs.length) box.append(...outs.map((out, i) => outputNode(out, job, i)));
    else box.append(el("span", "a-ph"));
    box.append(el("span", "a-bar"));
    return box;
  }

  function finalMedia(job) {
    const box = el("div", "a-job-final-media");
    if (job.full_output && job.outputs?.length) box.append(...job.outputs.map((out, i) => outputNode(out, job, i)));
    return box;
  }

  function jobCard(job) {
    const card = el("li", "a-job"); card.dataset.ref = job.ref;
    if (job.kind === "agentic") card.classList.add("is-agentic");
    const body = el("div", "a-job-body");
    const top = el("div", "a-job-top"); top.append(el("b", null, job.ref), el("span", null, job.kind === "agentic" ? `${service(job.service)} · Agentic` : service(job.service)));
    if (job.model) top.append(el("span", "a-job-model", job.model));   // the model the service runs this job with
    top.append(el("span", "a-state"));
    const links = el("div", "a-job-links");
    const a = el("a", "a-open", `Open in ${service(job.service)}`); links.append(a);
    const c = el("button", "a-cancel", "Cancel"); c.type = "button"; links.append(c);
    c.addEventListener("click", async () => {
      if (!confirm(`Cancel ${job.ref}?`)) return;
      c.disabled = true; c.textContent = "Cancelling…";
      try { await api(`/sessions/${current}/jobs/${job.ref}/cancel`, { method: "POST" }); } catch (e) { msg(e.message); c.disabled = false; c.textContent = "Cancel"; }
    });
    const review = el("div", "a-job-approval");
    const note = el("textarea", "a-job-approval-note");
    note.rows = 2; note.maxLength = 4000; note.placeholder = "Optional note for H3 Agentic…";
    const actions = el("div", "a-job-approval-actions");
    const accept = el("button", "a-job-approval-btn accept", "✓");
    const exclude = el("button", "a-job-approval-btn exclude", "×");
    accept.type = exclude.type = "button";
    accept.title = "Accept staging previews"; exclude.title = "Exclude staging previews";
    actions.append(accept, exclude); review.append(note, actions);
    async function decide(action) {
      accept.disabled = exclude.disabled = true; note.disabled = true; review.dataset.submitted = "true";
      try {
        await api(`/sessions/${current}/jobs/${job.ref}/agentic-approval`, {
          method: "POST", body: JSON.stringify({ action, notes: note.value })
        });
      } catch (e) { review.dataset.submitted = "false"; accept.disabled = exclude.disabled = false; note.disabled = false; msg(e.message); }
    }
    accept.addEventListener("click", () => decide("accept"));
    exclude.addEventListener("click", () => decide("exclude"));
    body.append(top, el("span", "a-job-label"), el("span", "a-job-err"), links);
    const final = el("div", "a-job-final");
    final.append(el("div", "a-job-final-title", "Full video"), finalMedia(job));
    card.append(media(job), final, review, body);
    return card;
  }

  function updateCard(card, job) {
    const outputs = (job.outputs || []).map((o) => o.url).join("|");
    const iterations = (job.iterations || []).map((o) => o.url).join("|");
    const mediaKey = `${job.state}||${outputs}||${iterations}`;
    if ((card.dataset.mediaKey || "") !== mediaKey) {      // only now does the media change
      const oldMedia = $(".a-job-media", card);
      // Stay on the clip being watched even when new attempts shift the list around it.
      const watching = oldMedia?.dataset.iterationUrl;
      const currentIndex = watching ? Math.max(0, (job.iterations || []).findIndex((item) => item.url === watching)) : 0;
      const approvalIndex = job.state === "awaiting_approval"
        ? (job.iterations || []).findIndex((item) => item.selected)
        : -1;
      const index = approvalIndex >= 0 ? approvalIndex : currentIndex;
      card.dataset.mediaKey = mediaKey;
      oldMedia.replaceWith(media(job, index));
    }
    $(".a-job-final", card).hidden = !job.full_output || !job.outputs?.length;
    const finalUrls = job.full_output ? (job.outputs || []).map((out) => out.url).join("|") : "";
    if (card.dataset.finalMediaKey !== finalUrls) {
      card.dataset.finalMediaKey = finalUrls;
      const finalBox = $(".a-job-final-media", card);
      finalBox.replaceChildren(...(job.full_output ? (job.outputs || []).map((out, i) => outputNode(out, job, i)) : []));
    }
    const done = FINAL.has(job.state);
    card.dataset.state = job.state;
    $(".a-state", card).textContent = stateText(job.state) + (job.progress != null && !done ? ` ${job.progress}%` : "");
    $(".a-job-label", card).textContent = job.label;
    $(".a-job-err", card).textContent = job.error || "";
    const open = job.outputs?.at(-1)?.open || (job.service === "Motion" ? "" : `/${PAGE[job.service] || job.service}/`);   // Motion has no page of its own
    $(".a-open", card).hidden = !open;
    if (open) $(".a-open", card).href = open;
    $(".a-cancel", card).hidden = done;
    const review = $(".a-job-approval", card);
    const waiting = job.kind === "agentic" && job.state === "awaiting_approval";
    review.hidden = !waiting;
    const submitted = waiting && job.approval_submitted;
    $(".a-job-approval-btn.accept", review).disabled = !!submitted;
    $(".a-job-approval-btn.exclude", review).disabled = !!submitted;
    $(".a-job-approval-note", review).disabled = !!submitted;
    review.dataset.submitted = String(!!submitted);
    const ph = $(".a-ph", card);
    if (ph) ph.textContent = job.state === "complete" ? (job.outputs?.[0]?.name || "Finished") : job.stage || stateText(job.state);
    $(".a-bar", card).style.width = job.progress != null && !done ? `${job.progress}%` : "0";
  }


  /* ------------------------------------------- videos + video boards -- */
  // A video the agent downloaded or made: the Trainer's player, inline in the chat.
  function inlineVideo(a) {
    const box = el("div", "a-ev-video");
    const label = el("div", "a-ev-video-label");
    const open = el("button", "a-link-btn", "Gallery"); open.type = "button";
    open.addEventListener("click", () => openGallery("sent", a.id));
    label.append(el("span", null, `${a.ref ? a.ref + " · " : ""}${a.name}`), open);
    const host = el("div");
    box.append(label, host);
    if (window.BunnyPlayer) BunnyPlayer.create(host, { src: a.url, preload: "metadata" });
    else { const v = el("video"); v.src = a.url; v.controls = true; v.preload = "metadata"; host.append(v); }
    return box;
  }

  // `$R scan` boards: the video with the agent's search drawn on the scrub bar (windows it is
  // looking at, kept with a score, skipped; picks in gold), planned trims as coloured ranges,
  // the crop box over the picture, and a feed of the frames it looked at.
  const TRIM_COLORS = ["#79add3", "#e1a744", "#9b86c4", "#7fbd54", "#d98c6a", "#5fb6b0"];
  const AGENT_COLOR = "#79add3";
  const secs2 = (t) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;
  function placeBoard(id, after) {
    let b = boards.get(id);
    if (!b) { b = boardCard(id); boards.set(id, b); }
    after.after(b.node);
    const data = (cache.get(current)?.snap.boards || []).find((x) => x.id === id);
    if (data) updateBoard(b, data);
  }
  function renderBoards(list) {
    for (const d of list) {
      const b = boards.get(d.id);
      if (b) updateBoard(b, d);
      else { const anchor = $("#a-log").lastElementChild; if (anchor) placeBoard(d.id, anchor); }
    }
  }
  function boardCard(id) {
    const node = el("li", "a-board"); node.dataset.board = id;
    const head = el("div", "a-board-head");
    const title = el("span", "a-board-title"), stat = el("span", "a-board-stat");
    head.append(title, stat);
    const stage = el("div", "a-board-player");
    const summary = el("p", "a-board-summary"); summary.hidden = true;
    const feed = el("div", "a-board-feed"); feed.hidden = true;
    node.append(head, stage, summary, feed);
    return { node, title, stat, stage, summary, feed, player: null, crop: null, key: "", preview: null, data: null };
  }
  function playRange(b, start, end) {
    const v = b.player.video;
    b.preview = { start, end };
    v.currentTime = start;
    v.play().catch(() => {});
  }
  function placeCrop(b) {
    const c = b.data?.crop, v = b.player?.video;
    if (!c || !v || !v.videoWidth || !v.clientWidth) { if (b.crop) b.crop.hidden = true; return; }
    const W = v.clientWidth, H = v.clientHeight, k = Math.min(W / v.videoWidth, H / v.videoHeight);
    const dw = v.videoWidth * k, dh = v.videoHeight * k, ox = v.offsetLeft + (W - dw) / 2, oy = v.offsetTop + (H - dh) / 2;
    Object.assign(b.crop.style, { left: `${ox + c.x * dw}px`, top: `${oy + c.y * dh}px`, width: `${c.w * dw}px`, height: `${c.h * dh}px` });
    b.crop.dataset.size = `${Math.round(c.w * b.data.width)}×${Math.round(c.h * b.data.height)}`;
    b.crop.hidden = false;
  }
  function updateBoard(b, d) {
    const key = JSON.stringify([d.updated, d.state]);
    if (key === b.key) return;
    b.key = key; b.data = d;
    if (!b.player && window.BunnyPlayer) {
      b.player = BunnyPlayer.create(b.stage, { src: d.url, duration: d.duration, preload: "metadata" });
      b.crop = el("div", "a-board-crop"); b.crop.hidden = true;
      $(".bp-stage", b.stage).append(b.crop);
      const v = b.player.video;
      v.addEventListener("loadedmetadata", () => placeCrop(b));
      new ResizeObserver(() => placeCrop(b)).observe(v);
      v.addEventListener("timeupdate", () => { const r = b.preview; if (r && v.currentTime >= r.end - 0.03) v.currentTime = r.start; });
      b.stage.addEventListener("pointerdown", (e) => { if (e.target.closest(".bp-track")) b.preview = null; });
    }
    b.node.dataset.state = d.state; b.node.dataset.mode = d.mode;
    b.title.textContent = `${d.mode === "search" ? "🔎" : "✂"} ${d.ref} · ${d.goal || d.name}`;
    b.title.title = `${d.name} · ${d.duration.toFixed(1)}s · ${d.width}×${d.height}`;
    const kept = d.verdicts.filter((v) => v.keep).length;
    const parts = [];
    if (d.plan.length) parts.push(`${d.verdicts.length}/${d.plan.length} checked`, `${kept} kept`);
    if (d.picks.length) parts.push(`${d.picks.length} picked`);
    if (d.trims.length) parts.push(`${d.trims.length} trim${d.trims.length > 1 ? "s" : ""}`);
    if (d.crop) parts.push(`crop ${Math.round(d.crop.w * d.width)}×${Math.round(d.crop.h * d.height)}`);
    parts.push(d.state === "done" ? "done" : d.scanning ? `looking at ${secs2(d.scanning[0])}` : "working");
    b.stat.textContent = parts.join(" · ");
    b.summary.textContent = d.summary || ""; b.summary.hidden = !d.summary;
    if (!b.player) return;
    const scanning = d.scanning ? [{ start: d.scanning[0], end: d.scanning[1], color: AGENT_COLOR, label: "Rocsy" }] : [];
    b.player.setScout(d.plan.length || d.verdicts.length ? {
      shots: [], windows: d.plan, verdicts: d.verdicts.map((v) => ({ ...v, subject: "" })), scanning,
      picks: d.picks.map((p) => ({ start: p[0], end: p[1] })),
    } : null);
    b.player.setSegments(d.trims.map(([start, end], i) => ({
      start, end, color: TRIM_COLORS[i % TRIM_COLORS.length], label: i + 1,
      title: `Trim ${i + 1}: ${secs2(start)} – ${secs2(end)} (${(end - start).toFixed(1)}s)`, onClick: () => playRange(b, start, end),
    })));
    placeCrop(b);
    paintFeed(b, d, scanning);
  }
  function paintFeed(b, d, scanning) {
    const picked = (start) => d.picks.some((p) => Math.abs(p[0] - start) < 0.01);
    const rank = (c) => (c.scanning ? 2 : picked(c.start) ? 0 : c.keep ? 1 : 3);
    const cards = [...scanning.map((w) => ({ ...w, scanning: true })), ...d.verdicts];
    cards.sort((x, y) => rank(x) - rank(y) || (x.scanning || y.scanning ? 0 : (y.score - x.score) || (x.start - y.start)));
    b.feed.hidden = !cards.length;
    b.feed.replaceChildren();
    const heading = { 0: "Picked", 1: "Kept", 2: "Looking now", 3: "Skipped" };
    let section = null;
    for (const c of cards) {
      if (rank(c) !== section) { section = rank(c); b.feed.append(el("div", "fc-head", heading[section])); }
      const card = el("button", "fc" + (c.scanning ? " scanning" : c.keep ? " keep" : " skip"));
      card.type = "button";
      if (c.scanning) card.style.setProperty("--ac", c.color);
      const thumb = d.thumbs?.[c.start.toFixed(2)];
      if (thumb) { const img = el("img"); img.loading = "lazy"; img.alt = ""; img.src = `${API}/sessions/${current}/boards/${d.id}/frames/${thumb}`; card.append(img); }
      const pk = !c.scanning && picked(c.start);
      if (pk) card.classList.add("pick");
      card.append(el("span", "fc-chip", c.scanning ? "Rocsy · looking" : pk ? (c.keep ? `PICK ${c.score}` : "PICKED") : c.keep ? `KEEP ${c.score}` : "SKIP"),
                  el("span", "fc-time", `${secs2(c.start)}`));
      card.title = c.scanning ? `Looking at ${secs2(c.start)} – ${secs2(c.end)}` : `${c.keep ? `keep ${c.score}/10` : "skip"}${c.reason ? " — " + c.reason : ""}${pk ? "\n★ picked" : ""}`;
      card.addEventListener("click", () => {
        b.feed.querySelectorAll(".fc.playing").forEach((x) => x.classList.remove("playing"));
        card.classList.add("playing");
        playRange(b, c.start, c.end);
      });
      b.feed.append(card);
    }
  }

  function renderJobs(jobs) {
    for (const job of jobs) {
      const card = cards.get(job.ref);
      if (card) updateCard(card, job);
      else {                          // its start line was trimmed from the log: show it at the end
        const anchor = $("#a-log").lastElementChild;
        if (anchor) placeCard(job.ref, anchor);
      }
      const currentCard = cards.get(job.ref);
      if (currentCard && job.kind === "agentic" && job.state === "awaiting_approval" && currentCard.dataset.approvalState !== "awaiting_approval") {
        currentCard.dataset.approvalState = "awaiting_approval";
        $("#a-log").append(currentCard);
        currentCard.classList.remove("approval-arriving");
        void currentCard.offsetWidth;
        currentCard.classList.add("approval-arriving");
        requestAnimationFrame(() => currentCard.scrollIntoView({ behavior: "smooth", block: "end" }));
      } else if (currentCard && job.state !== "awaiting_approval") {
        currentCard.dataset.approvalState = job.state;
      }
    }
    const count = galleryItems("made").length;
    $("#a-gallery-open").hidden = !count && !(cache.get(current)?.snap.attachments || []).length;
    $("#a-gallery-count").textContent = count ? String(count) : "";
    if ($("#a-gallery").open) renderGallery();
  }

  /* ------------------------------------------------ head -- */
  function renderHead(s) {
    const head = $("#a-title");
    if (head.textContent !== s.title && head.textContent && head.textContent !== "Loading…" && head.dataset.id === s.id) {
      head.classList.remove("is-renamed"); void head.offsetWidth; head.classList.add("is-renamed");
    }
    head.textContent = s.title; head.dataset.id = s.id;
    head.classList.toggle("by-agent", s.named === "agent");
    $("#a-rename").hidden = false;
    $("#a-stage").textContent = s.stage || stateText(s.state);
    $("#a-stop").hidden = !s.busy; $("#a-stop").disabled = !!s.stop;
    $("#a-stop-inline").hidden = !s.busy; $("#a-stop-inline").disabled = !!s.stop;
    $("#a-delete").hidden = s.busy;
    $("#a-approval").hidden = !s.pending;
    if (s.pending) {
      const command = s.pending.kind === "command";
      $("#a-approval").dataset.kind = command ? "command" : "job";
      $("#a-approval-text").replaceChildren(
        el("strong", "a-approval-title", command ? "Hermes wants to run a flagged command" : "Approve this GPU job?"),
        el(command ? "code" : "span", "a-approval-what", s.pending.what));
      const choices = command ? (s.pending.choices || ["once", "deny"]) : [];
      document.querySelectorAll("[data-approve]").forEach((b) => { b.hidden = command; });
      document.querySelectorAll("[data-choice]").forEach((b) => { b.hidden = !command || !choices.includes(b.dataset.choice); });
    }
    renderReview(s);
    $("#a-send").textContent = s.state === "thinking" ? "Steer" : "Send";
    const goal = $("#a-goal"), g = s.goal;
    goal.hidden = !g;
    if (g) {
      goal.dataset.status = g.status;
      goal.textContent = `${{ active: "⊙", paused: "⏸", done: "✓" }[g.status] || "·"} Goal · ${g.status} · ${g.turns}/${g.max_turns} turns · ${g.text}`;
      goal.title = g.text + (g.evidence ? `\n\nEvidence: ${g.evidence}` : "");
    }
    document.title = `${s.busy ? "● " : ""}${s.title} · Agent · Rocsy`;
  }

  /* ------------------------------------------------ review mode (/review) -- */
  // Proposals wait here with their full prompt: edit, then approve, or veto. Items already on
  // screen are kept (not rebuilt) so an edit in progress survives live updates.
  const REVIEW_ICON = { service: "⚙", reamod: "🧩", plan: "🗺", video: "🎬", agentic: "🎬", image: "🖼", music: "♪", voice: "🗣", postedit: "✂", captions: "💬", montage: "🎞", edit: "✂", train: "🧪", motion: "✦" };
  // Ask first: a service's first use in a session asks for an OK; allowing clears the service.
  function clearItem(x) {
    const item = el("div", "a-review-item a-review-clear"); item.dataset.id = x.id; item.dataset.tool = x.tool; item.dataset.service = x.clear || "";
    // "Video (incl. H3 Agentic, post-edits…)" -> the name, and what it covers underneath
    const [name, ...rest] = String(x.service_label || x.clear || "this service").split(" (");
    const covers = rest.length ? rest.join(" (").replace(/\)$/, "") : "";
    const top = el("div", "a-perm-top");
    const text = el("div", "a-perm-text");
    const title = el("strong", "a-perm-title"); title.append("Use ", el("span", "a-perm-service", name), "?");
    text.append(el("span", "a-perm-kicker", "Permission needed · first use this session"), title);
    if (covers) text.append(el("span", "a-perm-covers", `Covers ${covers}`));
    top.append(el("span", "a-perm-tile", REVIEW_ICON[x.tool] || "🔓"), text, el("span", "a-review-ref", x.id));
    const what = el("div", "a-perm-what");
    what.append(el("span", "a-perm-what-label", "It wants to"), el("span", "a-perm-what-title", x.title));
    if (x.details) what.append(el("code", "a-perm-what-details", x.details));
    const note = el("input", "a-review-note"); note.placeholder = "Note for the agent (optional)"; note.maxLength = 2000; note.hidden = true;
    const addNote = el("button", "a-perm-note-toggle", "+ Add a note"); addNote.type = "button";
    addNote.addEventListener("click", () => { note.hidden = false; addNote.hidden = true; note.focus(); });
    const actions = el("div", "a-review-actions a-perm-actions");
    const ok = el("button", "a-btn primary", "Allow this session"); ok.type = "button"; ok.title = "Allow it for the rest of this session";
    const always = el("button", "a-btn", "Always allow"); always.type = "button"; always.title = "Never ask about this service again (Settings → Agentic can forget it)";
    const no = el("button", "a-btn ghost a-perm-deny", "Don't allow"); no.type = "button";
    ok.addEventListener("click", () => decide(item, "approve"));
    always.addEventListener("click", () => decide(item, "approve", true));
    no.addEventListener("click", () => decide(item, "veto"));
    actions.append(addNote, no, always, ok);
    item.append(top, what, note, actions);
    return item;
  }
  function reviewItem(x) {
    if (x.clear) return clearItem(x);
    const item = el("div", "a-review-item"); item.dataset.id = x.id; item.dataset.tool = x.tool;
    const title = el("div", "a-review-title");
    title.append(el("span", "a-review-icon", REVIEW_ICON[x.tool] || "•"), el("strong", "", x.title), el("span", "a-review-ref", x.id));
    item.append(title);
    if (x.details) item.append(el("div", "a-review-details", x.details));
    for (const f of x.fields) {
      const label = el("label", "a-review-field");
      const area = el("textarea"); area.value = x.args[f.key] || ""; area.dataset.key = f.key; area.spellcheck = true;
      area.rows = Math.min(12, Math.max(2, area.value.split("\n").length + Math.floor(area.value.length / 100)));
      label.append(el("span", "", f.label), area); item.append(label);
    }
    const actions = el("div", "a-review-actions");
    const note = el("input", "a-review-note"); note.placeholder = "Note for the agent (optional)"; note.maxLength = 2000;
    const ok = el("button", "a-btn primary", x.tool === "plan" ? "Approve plan" : "Approve"); ok.type = "button";
    const no = el("button", "a-btn danger", "Veto"); no.type = "button";
    ok.addEventListener("click", () => decide(item, "approve"));
    no.addEventListener("click", () => decide(item, "veto"));
    actions.append(note, ok, no); item.append(actions);
    return item;
  }
  function renderReview(s) {
    const box = $("#a-review");
    const waiting = (s.proposals || []).filter((x) => x.status === "pending");
    if (box.dataset.session !== s.id) { box.replaceChildren(); box.dataset.session = s.id; }
    box.hidden = !waiting.length;
    const ids = new Set(waiting.map((x) => x.id));
    box.querySelectorAll(".a-review-item").forEach((n) => { if (!ids.has(n.dataset.id)) n.remove(); });
    if (!waiting.length) { box.replaceChildren(); return; }
    let head = $(".a-review-head", box);
    if (!head) {
      head = el("div", "a-review-head");
      head.append(el("strong"), el("span", "a-review-sub", "Nothing has started. Edit a prompt before approving it, or veto what you didn't ask for."));
      const all = el("div", "a-review-all");
      const ok = el("button", "a-btn", "Approve all"); ok.type = "button";
      const no = el("button", "a-btn danger", "Veto all"); no.type = "button";
      ok.addEventListener("click", async () => { for (const n of [...box.querySelectorAll(".a-review-item")]) if (!(await decide(n, "approve"))) break; });
      no.addEventListener("click", () => decide(null, "veto"));
      all.append(ok, no); head.append(all);
      box.prepend(head);
    }
    const permission = waiting.every((x) => x.clear);   // only "Ask first" service checks
    box.classList.toggle("is-permission", permission);
    $("strong", head).textContent = permission
      ? (waiting.length > 1 ? `The Master Agent asks to use ${waiting.length} services` : "The Master Agent asks to use a service")
      : (waiting.length > 1 ? `The Master Agent wants your OK for ${waiting.length} things` : "The Master Agent wants your OK");
    $(".a-review-sub", head).textContent = permission
      ? "Nothing has started. Allow it and the agent carries on; say no and it works without it."
      : "Nothing has started. Edit a prompt before approving it, or veto what you didn't ask for.";
    $(".a-review-all", head).hidden = waiting.length < 2;
    for (const x of waiting) if (!box.querySelector(`.a-review-item[data-id="${x.id}"]`)) box.append(reviewItem(x));
  }
  async function decide(item, decision, always = false) {
    const box = $("#a-review");
    const fields = {};
    item?.querySelectorAll("textarea[data-key]").forEach((a) => { fields[a.dataset.key] = a.value; });
    const buttons = [...(item || box).querySelectorAll("button")];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await api(`/sessions/${current}/review`, { method: "POST", body: JSON.stringify({
        id: item ? item.dataset.id : "*", decision, always, fields, note: item ? $(".a-review-note", item).value : "" }) });
      if (item) item.remove(); else box.querySelectorAll(".a-review-item").forEach((n) => n.remove());
      msg("");
      return true;
    } catch (e) { msg(e.message); buttons.forEach((b) => { b.disabled = false; }); return false; }
  }

  /* ------------------------------------------------ speed + context meters -- */
  // From the Hermes rocsy plugin after every model call (snap.usage: exact tokens/s, prompt size,
  // window, compression point); while a reply streams, the live delta rate (snap.live.rate).
  const kTok = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : String(n);
  function renderMeters(s) {
    const box = $("#a-meters"), u = s.usage || {}, live = s.live || {};
    const streaming = s.state === "thinking" && (live.assistant || live.reasoning) && live.rate != null;
    const tps = streaming ? live.rate : u.tps;
    const win = u.context_length, used = (u.prompt_tokens || 0) + (u.output_tokens || 0);
    const squeezing = renderCompress(s.compress) || renderPrefill(s.compress ? null : s.prefill);
    box.hidden = tps == null && !win && !squeezing;
    if (box.hidden) return;
    const speed = $("#a-tps");
    speed.hidden = tps == null;
    speed.classList.toggle("is-live", !!streaming);
    $("b", speed).textContent = tps == null ? "–" : (tps >= 100 ? Math.round(tps) : tps.toFixed(1));
    speed.title = streaming ? "Live generation speed (streamed tokens over the last few seconds)"
      : `Last model call: ${u.tps ?? "–"} tk/s generating` + (u.prefill_tps ? `, ${Math.round(u.prefill_tps)} tk/s reading the prompt` : "")
        + (u.ttfb != null ? `, first token after ${u.ttfb}s` : "") + (u.model ? `\n${u.model}` : "");
    const ctx = $("#a-ctx");
    ctx.hidden = !win || squeezing;             // the compression / prompt-reading bar takes its place while it runs
    if (ctx.hidden) return;
    const pct = Math.min(100, used / win * 100), left = Math.max(0, win - used);
    $(".a-ctx-fill", ctx).style.width = `${pct}%`;
    const at = u.compress_at && u.compress_at < win ? u.compress_at : null;
    const mark = $(".a-ctx-mark", ctx);
    mark.hidden = !at;
    if (at) mark.style.left = `calc(${at / win * 100}% - 1px)`;
    ctx.dataset.level = at ? (used >= at ? "hot" : used >= at * 0.8 ? "warm" : "ok") : (pct >= 90 ? "hot" : pct >= 70 ? "warm" : "ok");
    $(".a-ctx-text", ctx).textContent = `${kTok(left)} of ${kTok(win)} context left`;
    ctx.setAttribute("aria-valuemax", String(win)); ctx.setAttribute("aria-valuenow", String(used));
    ctx.title = `Context: ${used.toLocaleString()} / ${win.toLocaleString()} tokens used (${Math.round(pct)}%)`
      + (u.cache_read_tokens ? `, ${u.cache_read_tokens.toLocaleString()} from the prompt cache` : "")
      + (at ? `\nAuto-compresses at ${at.toLocaleString()} tokens (the mark): ${used >= at ? "due on the next call" : `${(at - used).toLocaleString()} to go`}` : "");
  }

  // Auto-compression (snap.compress, from the Hermes rocsy plugin + llama.cpp /slots): how far the
  // summary is - reading the conversation, then writing the summary - with the time left.
  const secs = (n) => n >= 90 ? `~${Math.round(n / 60)} min` : `~${Math.max(1, Math.round(n))} s`;
  function renderCompress(c) {
    const box = $("#a-compress");
    box.hidden = !c;
    if (!c) return false;
    const stage = c.phase === "done" || c.phase === "skipped" ? "done" : c.phase === "preparing" || !c.stage ? "preparing" : c.stage;
    const pct = stage === "preparing" ? 0 : Math.round(c.pct || 0);
    box.dataset.stage = stage;
    $(".a-compress-fill", box).style.width = `${stage === "done" ? 100 : pct}%`;
    box.setAttribute("aria-valuenow", String(stage === "done" ? 100 : pct));
    const left = c.eta ? ` · ${secs(c.eta)} left` : "";
    $(".a-compress-text", box).textContent = {
      preparing: "Compressing · preparing the summary…",
      reading: `Compressing ${pct}% · reading ${kTok(c.read || 0)}/${kTok(c.total || 0)}${left}`,
      writing: `Compressing ${pct}% · summary ${kTok(c.wrote || 0)}/~${kTok(c.target || 0)}${left}`,
      estimating: `Compressing ~${pct}%${left}`,
      done: c.phase === "skipped" ? "Compression skipped (no smaller)" : `Compressed${c.after ? ` to ~${kTok(c.after)}` : ""} ✓`,
    }[stage];
    box.title = `Auto-compression: ${c.tokens ? `${c.tokens.toLocaleString()} tokens, ` : ""}${c.messages ? `${c.messages} messages` : ""}`
      + (c.source === "slots" ? "\nLive from llama.cpp: prompt tokens read, then summary tokens written (incl. thinking) vs Hermes' summary target"
        : c.source === "estimate" ? "\nEstimated from the model's last measured speeds (no live counters for this model)" : "")
      + (c.t0 ? `\nRunning for ${secs((Date.now() / 1000) - c.t0)}` : "");
    return true;
  }

  // Before the first token (snap.prefill, from llama.cpp /slots or Strata /v1/status via llama-swap):
  // the model loading, then reading the prompt - a fresh session, or every call after a compression.
  // Loading has no progress from llama-swap: the bar runs against this model's average load time
  // (its last 5 loads, f.estimate), ticking every second between snapshots.
  const clock = (n) => `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
  let prefillTick = null, prefillShown = null;
  function loadingText(f, waited) {
    const est = f.estimate;
    if (!est) return `Loading the model… ${clock(waited)} · first timed load of this model, no estimate yet`;
    const left = est.avg - waited;
    const usual = `usually ${clock(est.avg)} (average of the last ${est.n === 1 ? "load" : `${est.n} loads`})`;
    return left > 0 ? `Loading the model… ${clock(waited)} · about ${clock(Math.ceil(left))} left · ${usual}`
      : `Loading the model… ${clock(waited)} · taking longer than usual, ${usual}`;
  }
  function renderPrefill(f) {
    const box = $("#a-prefill");
    box.hidden = !f;
    prefillShown = f;
    if (f?.stage === "loading" && !prefillTick) prefillTick = setInterval(() => { if (prefillShown?.stage === "loading") renderPrefill(prefillShown); }, 1000);
    if (f?.stage !== "loading" && prefillTick) { clearInterval(prefillTick); prefillTick = null; }
    if (!f) return false;
    const waitedNow = f.t0 ? Math.max(0, (Date.now() / 1000) - f.t0) : 0;
    const pct = f.stage === "reading" ? Math.round(f.pct || 0)
      : f.estimate?.avg ? Math.min(95, Math.round(100 * waitedNow / f.estimate.avg)) : 0;
    box.dataset.stage = f.stage === "reading" || (f.stage === "loading" && f.estimate) ? "reading" : "preparing";
    $(".a-compress-fill", box).style.width = `${pct}%`;
    box.setAttribute("aria-valuenow", String(pct));
    const waited = f.t0 ? (Date.now() / 1000) - f.t0 : 0;
    $(".a-compress-text", box).textContent = f.stage === "loading" ? loadingText(f, waited)
      : `Reading the prompt ${pct}% · ${kTok(f.read || 0)}/${kTok(f.total || 0)}`
        + (f.eta ? ` · first token in ${secs(f.eta)}` : f.read >= f.total ? " · first token any moment" : "");
    box.title = f.stage === "loading" ? `${f.model || "The model"} is loading into memory; then it reads the prompt before it can write`
        + (f.estimate ? `\nEstimate = the average of its last ${f.estimate.n} load time${f.estimate.n === 1 ? "" : "s"} (up to 5 are kept)` : "\nThis load is timed so the next one gets an estimate")
      : `The model reads the whole prompt before its first token (nothing cached after a compression or a reload)`
        + (f.rate ? `\nReading at ${f.rate.toLocaleString()} tk/s` : "") + (waited ? `\nReading for ${secs(waited)}` : "");
    return true;
  }

  function paint(entry, fresh) {
    if (fresh) { $("#a-log").replaceChildren(); cards.clear(); terminals.clear(); boards.clear(); appendEvents(entry.events); }
    renderHead(entry.snap); renderJobs(entry.snap.jobs || []); renderBoards(entry.snap.boards || []); renderLive(entry.snap.live); renderMeters(entry.snap); renderParked(entry.snap.parked);
  }

  /* ------------------------------------------------ open / stream -- */
  function open(id, push = true) {
    drawer(false);
    if (id === current && stream) return;
    closeStream();
    current = id || null;
    $("#a-empty").hidden = !!current; $("#a-session").hidden = !current;
    updateJump();
    $("#a-log").replaceChildren(); cards.clear(); terminals.clear(); boards.clear(); hideBusy();
    $("#a-meters").hidden = true;
    if (push) history.replaceState(null, "", current ? `?s=${current}` : location.pathname);
    document.querySelectorAll(".a-sess").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === current)));
    listKey = "";
    if (!current) { document.title = "Agent · Rocsy"; $("#a-input").focus(); return; }
    const entry = cache.get(current);
    follow = true; lastTop = 0;      // a session always opens at its latest line
    if (entry) { paint(entry, true); followBottom(); }
    else { $("#a-title").textContent = "Loading…"; $("#a-stage").textContent = ""; }
    connect();
    $("#a-input").focus({ preventScroll: true });
  }

  {
    const host = $("#a-session");
    host.addEventListener("scroll", () => {
      if (gapBelow() < 24 && host.scrollTop >= lastTop) follow = true;   // scrolled down to the bottom: follow again
      else if (host.scrollTop < lastTop - 1) follow = false;    // scrolled up: stop following
      lastTop = host.scrollTop;
      updateJump();
    }, { passive: true });
    // A wheel/touch upward stops following even when nothing moves yet (already at the bottom).
    host.addEventListener("wheel", (e) => { if (e.deltaY < 0) follow = false; }, { passive: true });
    // Content growing (images loading, terminals filling, cards) or the window/composer resizing.
    const resize = new ResizeObserver(() => stick());
    resize.observe($("#a-log")); resize.observe(host);
    $("#a-jump").addEventListener("click", () => followBottom(true));
  }

  function connect() {
    closeStream();
    if (!current || document.hidden) return;
    const id = current;
    const since = cache.get(id)?.last ?? -1;
    const source = new EventSource(`${API}/sessions/${id}/events?since=${since}`);
    stream = source;
    source.onmessage = (event) => {
      if (id !== current) return;
      const snap = JSON.parse(event.data);
      const known = cache.get(id)?.last ?? -1;
      const fresh = !cache.has(id) || $("#a-title").textContent === "Loading…";
      const entry = remember(snap);
      if (fresh) paint(entry, true);
      else { appendEvents((snap.events || []).filter((e) => e.i > known)); paint(entry, false); }
    };
    source.addEventListener("sessions", (event) => renderList(JSON.parse(event.data)));
    source.addEventListener("gone", () => { cache.delete(id); open(null); });
    source.onerror = () => {         // reconnect ourselves, so `since` is current and nothing repeats
      closeStream();
      retry = setTimeout(connect, 1500);
    };
  }

  function closeStream() {
    clearTimeout(retry);
    if (stream) { stream.close(); stream = null; }
  }

  /* ------------------------------------------------ compose -- */
  const input = $("#a-input");
  const highlight = $("#a-input-highlight");
  function syntaxHighlight() {
    const pieces = input.value.split(/(@\{[^}\n]*\}?(?:\s+(?:all|random\s+\d+))?|(?<!\w)@[\w.-]+(?:\s+(?:all|random\s+\d+))?|^```[^\n]*$)/gm);
    highlight.replaceChildren(...pieces.map((part) => {
      if (/^@\{|^@[\w.-]+/.test(part)) return el("span", "a-syntax-mention", part);
      if (part.startsWith("```")) return el("span", "a-syntax-fence", part);
      return document.createTextNode(part);
    }));
    // gray in-text completion for slash commands, shown after the caret
    const g = ghost();
    if (g) highlight.append(el("span", "a-syntax-ghost", g.fill + g.hint));
    highlight.append(document.createTextNode("\u200b"));
    highlight.scrollTop = input.scrollTop;
  }
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(200, input.scrollHeight)}px`; syntaxHighlight(); };
  input.addEventListener("input", grow);
  input.addEventListener("scroll", () => { highlight.scrollTop = input.scrollTop; });
  input.addEventListener("keydown", (e) => {
    if (!mentions.hidden && mentionKey(e)) return;
    if ((e.key === "Tab" && !e.shiftKey) || (e.key === "ArrowRight" && input.selectionStart === input.value.length)) {
      if (acceptGhost(e.key === "Tab")) { e.preventDefault(); return; }
    }
    if (!palette.hidden && paletteKey(e)) return;
    // Enter is a new line; Ctrl/⌘+Enter (or the Send button) sends. A one-line /command still sends on Enter.
    if (e.key !== "Enter" || e.isComposing) return;
    const command = !e.shiftKey && /^\/\S/.test(input.value) && !input.value.includes("\n");
    if (e.ctrlKey || e.metaKey || command) { e.preventDefault(); $("#a-compose").requestSubmit(); }
  });

  /* ------------------------------------------------ slash command menu -- */
  let COMMANDS = [], pick = 0, shown = [];
  const palette = $("#a-cmds");
  let ghostOff = false, cycle = null;
  function hidePalette() { palette.hidden = true; input.removeAttribute("aria-activedescendant"); syntaxHighlight(); }
  // full-command matches for what's typed sort first, so "/goal s" offers /goal status before /goal <objective>
  const matchCommands = (typed) => {
    const t = typed.trimEnd();
    return COMMANDS.filter((c) => c.cmd.startsWith(t) || (typed.startsWith(c.cmd + " ") && c.args))
      .sort((a, b) => b.cmd.startsWith(t) - a.cmd.startsWith(t));
  };
  // An argument spec as slots: "<time> [daily|weekdays|YYYY-MM-DD] [for 2h] <request>" or "on|off|<what>".
  // A slot has literal `choices`, a `keyword` that takes one value ("[for 2h]"), or is `free` text.
  const specSlots = (args) => (args.match(/\[[^\]]*\]|<[^>]*>|\S+/g) || []).map((t) => {
    const optional = t.startsWith("["), body = optional ? t.slice(1, -1) : t, alts = body.split("|");
    if (optional && body.includes(" ")) return { t, optional, keyword: body.split(" ")[0], choices: [] };
    const choices = alts.length > 1 ? alts.filter((o) => /^[a-z][\w-]*$/.test(o)) : [];
    return { t, optional, choices, date: alts.includes("YYYY-MM-DD"), free: !choices.length || alts.some((o) => o.startsWith("<")) };
  });
  // gray text for the arguments after `c.cmd `: complete the word being typed, else show the slots still to fill
  function argGhost(c, v) {
    const slots = specSlots(c.args), start = c.cmd.length + 1, words = v.slice(start).split(" ");
    const partial = words.pop().toLowerCase(), base = v.slice(0, v.length - partial.length);
    let si = 0, value = false;
    for (const w of words.map((x) => x.toLowerCase())) {
      if (!w) continue;
      if (value) { value = false; si++; continue; }
      if (si === 1 && slots[0].t === "<time>" && /^[ap]\.?m\.?$/.test(w)) continue;   // "3 am" is one time
      while (si < slots.length) {
        const sl = slots[si];
        if (sl.keyword) { if (w === sl.keyword) { value = true; break; } si++; continue; }
        if (sl.choices.includes(w) || (sl.date && /^\d{4}-\d{2}-\d{2}$/.test(w))) { si++; break; }
        if (sl.free) { if (si === slots.length - 1) return null; si++; break; }   // last free slot: the rest is free text
        if (sl.optional) { si++; continue; }
        return null;
      }
      if (si >= slots.length && !value) return null;
    }
    const later = (k) => slots.slice(k).map((x) => x.t).join(" ");
    if (value) return partial ? null : { fill: "", hint: (slots[si].t.slice(1, -1).split(" ")[1] || "") + " " + later(si + 1) };
    if (si >= slots.length) return null;
    if (!partial) {
      const sl = slots[si];
      return { fill: "", hint: later(si), tab: sl.choices[0] || "", slot: sl.choices.length ? { base, choices: sl.choices } : null };
    }
    // literal words the partial word could be: choices and keywords up to the first required slot
    for (let k = si; k < slots.length; k++) {
      const sl = slots[k];
      if (sl.choices.includes(partial)) return { fill: "", hint: later(k + 1) && " " + later(k + 1), slot: { base, choices: sl.choices } };
      const o = sl.keyword && sl.keyword.startsWith(partial) ? sl.keyword : sl.choices.find((x) => x.startsWith(partial));
      if (o) {
        if (sl.keyword) return { fill: o.slice(partial.length) + " ", hint: sl.t.slice(1, -1).split(" ")[1] + " " + later(k + 1) };
        return { fill: o.slice(partial.length), hint: "", slot: { base, choices: sl.choices } };
      }
      if (!sl.optional) break;
    }
    return null;
  }
  // What the gray text after the caret offers: `fill` is what Tab/→ inserts, `hint` is only shown,
  // `tab` is what Tab inserts when there's no fill, and `slot` lets further Tabs step through its choices.
  function ghost() {
    const v = input.value;
    if (ghostOff || document.activeElement !== input || !v.startsWith("/") || v.includes("\n")) return null;
    if (input.selectionStart !== v.length || input.selectionEnd !== v.length) return null;
    const typed = v.toLowerCase();
    const list = palette.hidden ? matchCommands(typed) : shown;
    const c = list[palette.hidden ? 0 : pick]; if (!c) return null;
    if (c.cmd.startsWith(typed)) {
      if (c.cmd === typed) return c.args ? { fill: " ", hint: c.args } : null;
      return { fill: c.cmd.slice(typed.length) + (c.args ? " " : ""), hint: c.args || "" };
    }
    return typed.startsWith(c.cmd + " ") && c.args ? argGhost(c, v) : null;
  }
  // Tab again right after completing an argument steps to its next choice, like a shell
  function acceptGhost(tab) {
    if (tab && cycle && input.value === cycle.value) {
      cycle.i = (cycle.i + 1) % cycle.choices.length;
      input.value = cycle.base + cycle.choices[cycle.i]; cycle.value = input.value; grow(); return true;
    }
    const g = ghost(), add = g && (g.fill || (tab && g.tab));
    if (!add) {
      // Tab on a choice typed by hand starts stepping from it
      const word = g?.slot && input.value.slice(g.slot.base.length).toLowerCase();
      if (!tab || !word || !g.slot.choices.includes(word)) return false;
      cycle = { ...g.slot, i: g.slot.choices.indexOf(word), value: input.value };
      return acceptGhost(true);
    }
    input.value += add; cycle = null;
    if (g.slot) {
      const word = input.value.slice(g.slot.base.length).toLowerCase();
      if (g.slot.choices.includes(word)) cycle = { ...g.slot, i: g.slot.choices.indexOf(word), value: input.value };
    }
    pick = 0; grow(); showPalette();
    return true;
  }
  // Tab only fills the command in; Enter or a click also sends one that takes no argument.
  function choose(index, send) {
    const c = shown[index]; if (!c) return;
    input.value = c.cmd + (c.args ? " " : ""); grow(); input.focus();
    if (!c.args && send) { hidePalette(); $("#a-compose").requestSubmit(); } else if (c.args) showPalette(); else hidePalette();
  }
  function showPalette() {
    const v = input.value;
    if (!v.startsWith("/") || v.includes("\n")) return hidePalette();
    const typed = v.toLowerCase().trimStart();
    // match on what's typed so far; once a full command plus a space is typed, only its own line stays
    shown = matchCommands(typed);
    if (!shown.length || (shown.length === 1 && typed.startsWith(shown[0].cmd + " ") && !shown[0].args)) return hidePalette();
    pick = Math.min(pick, shown.length - 1);
    palette.replaceChildren(...shown.map((c, i) => {
      const li = el("li", "a-cmd"); li.id = `a-cmd-${i}`; li.setAttribute("role", "option"); li.setAttribute("aria-selected", String(i === pick));
      const code = el("code", null, c.cmd); if (c.args) code.append(" ", el("i", null, c.args));
      li.append(code, el("span", null, c.help));
      li.addEventListener("mousedown", (ev) => { ev.preventDefault(); choose(i, true); });
      return li;
    }));
    palette.hidden = false;
    input.setAttribute("aria-activedescendant", `a-cmd-${pick}`);
    syntaxHighlight();
    const active = palette.children[pick];
    if (active.offsetTop < palette.scrollTop) palette.scrollTop = active.offsetTop;
    else if (active.offsetTop + active.offsetHeight > palette.scrollTop + palette.clientHeight)
      palette.scrollTop = active.offsetTop + active.offsetHeight - palette.clientHeight;
  }
  function paletteKey(e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); pick = (pick + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length; showPalette(); return true;
    }
    if (e.key === "Escape") { e.preventDefault(); ghostOff = true; hidePalette(); return true; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
      const c = shown[pick], typed = input.value.trim().toLowerCase();
      // Enter on a command that's already fully typed (with its argument) sends it
      if (e.key === "Enter" && c && (typed === c.cmd || typed.startsWith(c.cmd + " ")) && (!c.args || typed.length > c.cmd.length)) return false;
      // ...as does Enter on an argument typed for a command, unless another line was picked with the arrows
      if (e.key === "Enter" && !pick && shown.some((d) => d.args && typed.startsWith(d.cmd + " "))) return false;
      // Tab is only reached here with nothing left to complete
      if (e.key === "Tab" && shown.some((d) => typed.startsWith(d.cmd + " ") && !d.cmd.startsWith(typed))) { e.preventDefault(); return true; }
      e.preventDefault(); choose(pick, e.key === "Enter"); return true;
    }
    return false;
  }
  input.addEventListener("input", () => { pick = 0; ghostOff = false; cycle = null; showPalette(); mentionPick = 0; showMentions(); });
  // the gray completion only sits at the end of the text, so redraw when the caret moves or focus changes
  for (const ev of ["keyup", "click", "focus", "blur"]) input.addEventListener(ev, () => syntaxHighlight());
  input.addEventListener("click", showMentions);
  input.addEventListener("blur", () => setTimeout(() => { hidePalette(); hideMentions(); }, 120));
  /* ------------------------------------------------ pinned @ files -- */
  const mentions = $("#a-mentions");
  let pinnedCache = [], pinnedGroups = [], pinnedLoaded = 0, pinnedRequest = null, mentionPick = 0, mentionShown = [], mentionRevision = 0, textPending = 0;
  const isTextFile = (f) => /\.(txt|md|markdown|json|csv|log|py|js|jsx|ts|tsx|css|html|htm|yaml|yml|xml|srt|vtt|sh|toml|ini)$/i.test(f.name)
    || /^text\//.test(f.mime) || /^(application\/(json|xml|javascript))$/.test(f.mime);
  const languageOf = (name) => ({py:"python",js:"javascript",jsx:"jsx",ts:"typescript",tsx:"tsx",md:"markdown",yml:"yaml",sh:"bash",htm:"html"})[name.split(".").pop().toLowerCase()]
    || name.split(".").pop().toLowerCase().replace(/[^a-z0-9]/g, "");
  async function pinnedList(force = false) {
    if (!force && Date.now() - pinnedLoaded < 30000) return pinnedCache;
    if (!pinnedRequest) pinnedRequest = api("/pinned").then((result) => {
      pinnedCache = result.files || []; pinnedGroups = result.groups || []; pinnedLoaded = Date.now(); return pinnedCache;
    }).finally(() => { pinnedRequest = null; });
    return pinnedRequest;
  }
  input.addEventListener("focus", () => { pinnedList().catch(() => {}); });
  function mentionQuery() {
    const end = input.selectionStart, line = input.value.lastIndexOf("\n", end - 1) + 1;
    const start = input.value.lastIndexOf("@", end - 1);
    if (start < line || (start && /[\w@]/.test(input.value[start - 1]))) return null;
    let query = input.value.slice(start + 1, end);
    if (query.startsWith("{")) query = query.slice(1);
    if (query.includes("}") || query.length > 80) return null;
    return { start, end, query: query.toLowerCase().trim() };
  }
  function hideMentions() {
    mentionRevision++;
    if (!mentions.hidden) input.removeAttribute("aria-activedescendant");
    mentions.hidden = true;
  }
  async function showMentions() {
    const query = mentionQuery(), revision = ++mentionRevision;
    if (!query) { hideMentions(); return; }
    try {
      const pinned = await pinnedList();
      if (revision !== mentionRevision || !mentionQuery()) return;
      mentionShown = [
        ...pinnedGroups.filter((g) => !query.query || g.name.toLowerCase().includes(query.query)).map((g) => ({...g, isGroup:true})),
        ...pinned.filter((f) => !query.query || `${f.alias} ${f.name}`.toLowerCase().includes(query.query))
      ].slice(0, 8);
      if (!mentionShown.length) { hideMentions(); return; }
      hidePalette(); mentionPick = Math.min(mentionPick, mentionShown.length - 1);
      mentions.replaceChildren(...mentionShown.map((f, index) => {
        const row = el("li", "a-mention"); row.id = `a-mention-${index}`;
        row.setAttribute("role", "option"); row.setAttribute("aria-selected", String(index === mentionPick));
        row.append(el("span", "a-mention-kind", f.isGroup ? "GRP" : isTextFile(f) ? "TXT" : f.kind === "image" ? "IMG" : f.kind === "audio" ? "♪" : f.kind === "video" ? "▶" : "FILE"));
        const main = el("span", "a-mention-main");
        main.append(el("span", "a-mention-alias", `@${f.isGroup ? f.name : f.alias || f.name}`), el("span", "a-mention-name", f.isGroup ? `${f.files.length} pinned files · all or random N` : f.name));
        row.append(main, el("span", "a-mention-size", f.isGroup ? "GROUP" : kb(f.size)));
        row.addEventListener("mousedown", (event) => { event.preventDefault(); selectMention(index); });
        return row;
      }));
      mentions.hidden = false; input.setAttribute("aria-activedescendant", `a-mention-${mentionPick}`);
      const active = mentions.children[mentionPick];
      if (active.offsetTop < mentions.scrollTop) mentions.scrollTop = active.offsetTop;
      else if (active.offsetTop + active.offsetHeight > mentions.scrollTop + mentions.clientHeight)
        mentions.scrollTop = active.offsetTop + active.offsetHeight - mentions.clientHeight;
    } catch (error) { hideMentions(); msg(`Pinned files unavailable: ${error.message}`); }
  }
  function mentionKey(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); mentionPick = (mentionPick + (event.key === "ArrowDown" ? 1 : -1) + mentionShown.length) % mentionShown.length;
      showMentions(); return true;
    }
    if (event.key === "Escape") { event.preventDefault(); hideMentions(); return true; }
    if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey && !event.isComposing)) {
      event.preventDefault(); selectMention(mentionPick); return true;
    }
    return false;
  }
  function replaceDraft(start, end, replacement) {
    input.setRangeText(replacement, start, end, "end"); grow(); input.focus(); hideMentions();
  }
  async function selectMention(index) {
    const f = mentionShown[index], query = mentionQuery();
    if (!f || !query) return;
    if (f.isGroup) { replaceDraft(query.start, query.end, `@{${f.name}} all `); return; }
    const label = f.alias || f.name;
    if (!isTextFile(f)) {
      if (!files.some((entry) => entry.id === f.id)) {
        if (files.length >= 20) { msg("Up to 20 attachments per message"); return; }
        files.push({ key: `pin-${f.id}`, id: f.id, pinned: true, name: f.name, kind: f.kind, size: f.size,
                     preview: /^(image|video)$/.test(f.kind) ? f.url : "", url: f.url });
        renderFiles();
      }
      replaceDraft(query.start, query.end, `@{${label}} `);
      return;
    }
    if (f.size > 100000) { msg(`${f.name} is too large to insert into the chat (20,000 character limit)`); hideMentions(); return; }
    const typed = input.value.slice(query.start, query.end);
    hideMentions(); textPending++; renderFiles(); msg(`Inserting ${f.name}…`);
    try {
      const response = await fetch(f.url, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error(`File unavailable (${response.status})`);
      const content = await response.text();
      if (input.value.slice(query.start, query.end) !== typed) throw new Error("Draft changed; choose the file again");
      const longest = Math.max(2, ...[...content.matchAll(/`+/g)].map((match) => match[0].length));
      const fence = "`".repeat(longest + 1);
      const before = input.value.slice(0, query.start), after = input.value.slice(query.end);
      const replacement = `${before && !before.endsWith("\n") ? "\n\n" : ""}@{${label}} · ${f.name}\n${fence}${languageOf(f.name)}\n${content}${content.endsWith("\n") ? "" : "\n"}${fence}${after && !after.startsWith("\n") ? "\n" : ""}`;
      if (input.value.length - typed.length + replacement.length > input.maxLength) throw new Error("Text exceeds the 20,000 character chat limit");
      replaceDraft(query.start, query.end, replacement); msg("");
    } catch (error) { msg(error.message); }
    finally { textPending--; renderFiles(); }
  }
  function randomMembers(items, count) {
    const pool = [...items];
    for (let i = 0; i < count; i++) {
      const random = new Uint32Array(1); crypto.getRandomValues(random);
      const j = i + random[0] % (pool.length - i);
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    return pool.slice(0, count);
  }
  async function expandGroups(draft, attached) {
    if (!/@(?:\{|[\w-])/.test(draft)) return {text:draft, attached};
    await pinnedList(true);
    const pattern = /@(?:\{([^{}]+)\}|\s*([\w-]+))\s+(all|random\s+\d+)\b/gi;
    const matches = [...draft.matchAll(pattern)];
    if (!matches.length) return {text:draft, attached};
    let output = '', cursor = 0;
    const staged = [...attached], byId = new Map(pinnedCache.map(f => [f.id.slice(7), f]));
    for (const match of matches) {
      const name = match[1] || match[2];
      const group = pinnedGroups.find(g => g.name.toLowerCase() === name.toLowerCase());
      if (!group) throw new Error(`Pinned group “${name}” was not found`);
      const members = group.files.map(id => byId.get(id)).filter(Boolean);
      if (!members.length) throw new Error(`Pinned group “${name}” has no available files`);
      const command = match[3].toLowerCase();
      const count = command === 'all' ? members.length : Number(command.match(/\d+/)[0]);
      if (!Number.isSafeInteger(count) || count < 1 || count > members.length) throw new Error(`Choose 1–${members.length} files from “${name}”`);
      const chosen = command === 'all' ? members : randomMembers(members, count);
      let insertion = match[0];
      for (const file of chosen) {
        if (isTextFile(file)) {
          if (file.size > 100000) throw new Error(`${file.name} is too large to insert into chat`);
          const response = await fetch(file.url, {credentials:'same-origin', cache:'no-store'});
          if (!response.ok) throw new Error(`${file.name} is unavailable`);
          const content = await response.text();
          const longest = Math.max(2, ...[...content.matchAll(/`+/g)].map(m => m[0].length));
          const fence = '`'.repeat(longest + 1);
          insertion += `\n\n${file.name}\n${fence}${languageOf(file.name)}\n${content}${content.endsWith('\n') ? '' : '\n'}${fence}`;
        } else if (!staged.some(item => item.id === file.id)) {
          if (staged.length >= 20) throw new Error('Up to 20 attachments per message');
          staged.push({key:`pin-${file.id}`, id:file.id, pinned:true, name:file.name, kind:file.kind, size:file.size,
            preview:/^(image|video)$/.test(file.kind) ? file.url : '', url:file.url});
        }
      }
      output += draft.slice(cursor, match.index) + insertion; cursor = match.index + match[0].length;
    }
    output += draft.slice(cursor);
    if (output.length > input.maxLength) throw new Error('Expanded group text exceeds the 20,000 character chat limit');
    return {text:output, attached:staged};
  }
  /* ------------------------------------------------ attachments -- */
  const PART = 8 * 1024 * 1024;       // under the gateway's 50 MB body limit
  let files = [];                     // {key, file, name, kind, size, sent, id, error, preview, xhr}
  const kindOf = (type) => ["image", "video", "audio"].includes(type.split("/")[0]) ? type.split("/")[0] : "file";
  function renderFiles() {
    const host = $("#a-files");
    host.hidden = !files.length;
    host.replaceChildren(...files.map((f) => {
      const li = el("li", "a-file"); li.dataset.state = f.error ? "error" : f.id ? "ready" : "uploading";
      if (f.preview && f.kind === "image") { const img = el("img"); img.src = f.preview; img.alt = ""; li.append(img); }
      else if (f.preview && f.kind === "video") { const video = el("video"); video.src = f.preview; video.muted = true; video.preload = "metadata"; video.playsInline = true; li.append(video); }
      else li.append(el("span", "a-att-icon", f.kind === "video" ? "▶" : f.kind === "audio" ? "♪" : "📄"));
      const text = el("span", "a-file-text");
      text.append(el("span", "a-file-name", f.name),
        el("span", "a-file-meta", f.error ? f.error : f.id ? kb(f.size) : `${Math.floor(100 * f.sent / f.size)}% of ${kb(f.size)}`));
      li.append(text);
      const x = el("button", "a-file-x", "✕"); x.type = "button"; x.setAttribute("aria-label", `Remove ${f.name}`);
      x.addEventListener("click", () => removeFile(f.key)); li.append(x);
      return li;
    }));
    $("#a-send").disabled = textPending > 0 || files.some((f) => !f.id && !f.error);
  }
  function removeFile(key) {
    const f = files.find((x) => x.key === key); if (!f) return;
    f.cancelled = true;
    if (f.preview?.startsWith("blob:")) URL.revokeObjectURL(f.preview);
    if (f.id && !f.pinned) api(`/uploads/${f.id}`, { method: "DELETE" }).catch(() => {});
    files = files.filter((x) => x.key !== key); renderFiles();
  }
  async function sendPart(url, blob, headers) {
    const r = await fetch(API + url, { method: "POST", credentials: "same-origin", body: blob,
      headers: { "Content-Type": blob.type || "application/octet-stream", "X-Agent-Upload": "1", ...headers } });
    if (r.status === 401) { location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`; throw new Error("Log in again"); }
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(body.detail || body.message || `Upload failed (${r.status})`);
    return body;
  }
  async function upload(f) {
    try {
      const type = f.file.type || "application/octet-stream";
      let res = await sendPart("/uploads", f.file.slice(0, PART, type),
        { "X-Filename": encodeURIComponent(f.name).replace(/%20/g, " "), "X-Upload-Total": String(f.size) });
      f.sent = res.received; renderFiles();
      while (!res.complete) {
        if (f.cancelled) { api(`/uploads/${res.id}`, { method: "DELETE" }).catch(() => {}); return; }
        for (let tries = 0; ; tries++) {
          try { res = await sendPart(`/uploads/${res.id}/append`, f.file.slice(f.sent, f.sent + PART, type), { "X-Upload-Offset": String(f.sent) }); break; }
          catch (err) { if (tries >= 2) throw err; await new Promise((r) => setTimeout(r, 1500 * (tries + 1))); }
        }
        f.sent = res.received; renderFiles();
      }
      if (f.cancelled) { api(`/uploads/${res.id}`, { method: "DELETE" }).catch(() => {}); return; }
      f.id = res.id; f.url = res.url;
    } catch (err) { f.error = err.message; }
    renderFiles();
  }
  function addFiles(list) {
    for (const file of list) {
      if (files.length >= 20) { msg("Up to 20 attachments per message"); break; }
      const f = { key: Math.random().toString(36).slice(2), file, name: file.name || "pasted.png", size: file.size, sent: 0,
                  kind: kindOf(file.type || ""), preview: /^(image|video)\//.test(file.type || "") ? URL.createObjectURL(file) : "" };
      files.push(f); upload(f);
    }
    renderFiles(); input.focus();
  }
  $("#a-attach").addEventListener("click", () => $("#a-file-input").click());
  $("#a-file-input").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
  input.addEventListener("paste", (e) => {
    const pasted = [...(e.clipboardData?.files || [])];
    if (pasted.length) { e.preventDefault(); addFiles(pasted); }
  });
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("#a-drop").hidden = false; });
  document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#a-drop").hidden = true; } });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; $("#a-drop").hidden = true; addFiles([...e.dataTransfer.files]);
  });

  /* ------------------------------------------------ send (and the "jobs are rendering" choice) -- */
  let held = null;                    // the message waiting on the busy choice
  function hideBusy() { $("#a-busy").hidden = true; held = null; }
  function showBusy(detail, payload) {
    held = payload;
    $("#a-busy-title").textContent = `The Master Agent's ${detail.jobs.length > 1 ? `${detail.jobs.length} jobs are` : "job is"} using the GPU`;
    $('[data-busy="cancel"]').textContent = `Stop ${detail.jobs.length > 1 ? "them" : "it"} and send now`;
    $('[data-busy="queue"]').textContent = `Send when ${detail.jobs.length > 1 ? "they finish" : "it finishes"}`;
    $("#a-busy-text").textContent = `${detail.message.replace(/^The Master Agent's \d+ jobs? (is|are) using the GPU\. /, "")} Stop ${detail.jobs.length > 1 ? "them" : "it"} so the agent can read your message now, or send it once ${detail.jobs.length > 1 ? "they finish" : "it finishes"}.`;
    $("#a-busy-jobs").replaceChildren(...detail.jobs.map((j) => el("li", null,
      `${j.ref} · ${j.agentic ? "Video Agentic" : service(j.service)} · ${stateText(j.state)}${j.progress != null ? ` ${j.progress}%` : ""} — ${j.label}`)));
    $("#a-busy").hidden = false;
    $('[data-busy="cancel"]').focus();
  }
  document.querySelectorAll("[data-busy]").forEach((b) => b.addEventListener("click", () => {
    const payload = held; hideBusy();
    if (!payload) return;
    if (b.dataset.busy === "keep") { restore(payload); return; }
    deliver({ ...payload, on_busy: b.dataset.busy });
  }));
  function restore(payload) {
    if (!input.value) { input.value = payload.text; grow(); }
    if (payload.files?.length && !files.length) { files = payload.files; renderFiles(); }
    input.focus();
  }
  async function deliver(payload) {
    const send = $("#a-send"); send.disabled = true; msg("");
    let bubble = null;
    if (current) {                      // show it now; the server's copy confirms it
      const atts = payload.files.map((f) => ({ name: f.name, kind: f.kind, url: f.preview || f.url }));
      bubble = eventNode({ kind: "user", text: payload.text, command: payload.text.startsWith("/"), attachments: atts });
      bubble.classList.add("is-pending"); bubble.dataset.text = payload.text || "(see the attachments)";
      answerOpenAsks(payload.text);
      $("#a-log").append(bubble); followBottom();
    }
    const attachments = payload.files.map((f) => f.id);
    try {
      if (current) await api(`/sessions/${current}/message`, { method: "POST", body: JSON.stringify({ text: payload.text, attachments, on_busy: payload.on_busy || "ask", tz: TZ }) });
      else { const s = await api("/sessions", { method: "POST", body: JSON.stringify({ prompt: payload.text, attachments, tz: TZ }) }); remember(s, true); open(s.id); }
      payload.files.forEach((f) => f.preview?.startsWith("blob:") && setTimeout(() => URL.revokeObjectURL(f.preview), 60000));
    } catch (err) {
      bubble?.remove();
      if (err.detail?.code === "jobs_rendering") showBusy(err.detail, payload);
      else { msg(err.message); restore(payload); }
    } finally { send.disabled = false; }
  }
  $("#a-compose").addEventListener("submit", async (e) => {
    e.preventDefault();
    let text = input.value.trim();
    const originalText = text;
    if (!text && !files.length) return;
    if (textPending) { msg("Wait for the pinned text to finish loading"); return; }
    if (files.some((f) => !f.id && !f.error)) { msg("Wait for the attachments to finish uploading"); return; }
    hidePalette(); hideMentions();
    const head = text.split(/\s+/)[0].toLowerCase();
    if (!files.length && head === "/think") { input.value = ""; grow(); toggle.click(); return; }
    if (!files.length && head === "/new") {
      const rest = text.slice(4).trim();
      input.value = ""; grow(); open(null);
      if (!rest) return;
      input.value = rest; return $("#a-compose").requestSubmit();
    }
    let attached = files.filter((f) => f.id);
    try { ({text, attached} = await expandGroups(text, attached)); }
    catch (error) { msg(error.message); return; }
    if (input.value.trim() !== originalText) { msg('Draft changed; send again to use the updated text'); return; }
    const payload = { text, files: attached };
    input.value = ""; grow(); files = []; renderFiles();
    await deliver(payload);
  });
  $( "#a-new" ).addEventListener( "click", () => open( null ) );
  // Example requests in the empty state: a random sample of what the studios can do, filled
  // below. Clicking one puts its text in the composer and sends it, exactly as if typed.
  $( ".a-sugs" )?.addEventListener( "click", ( e ) => {
    const b = e.target.closest( ".a-sug" ); if ( !b ) return;
    input.value = $( ".a-sug-tx", b ).textContent.trim(); grow();
    $( "#a-compose" ).requestSubmit();
  } );
  // Same 24px stroked icons as the Rocsy top bar (gateway/topbar.py APPS), plus Motion, Edit, Chain.
  const SUG_ICON = {
    vid: '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>',
    img: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="M20.5 16l-5-5L6 20"/>',
    mus: '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    mot: '<path d="M2.5 8h4M2 12h5M2.5 16h4"/><rect x="9" y="5" width="12.5" height="14" rx="2.5"/><path d="M13.5 9.5v5l4-2.5z"/>',
    edt: '<circle cx="6" cy="6.5" r="2.6"/><circle cx="6" cy="17.5" r="2.6"/><path d="M8.2 8.1L20 18M8.2 15.9L20 6"/>',
    trn: '<path d="M9.5 3h5M10.5 3v6.2L5.2 18.4A1.7 1.7 0 0 0 6.7 21h10.6a1.7 1.7 0 0 0 1.5-2.6L13.5 9.2V3"/><path d="M7.5 15h9"/>',
    llm: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9zM9 1.5V4M15 1.5V4M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5"/>',
    sys: '<path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.5l8.5 4.5 8.5-4.5"/><path d="M3.5 16.5l8.5 4.5 8.5-4.5"/>',
    mix: '<path d="M11 3l1.8 4.7 4.7 1.8-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z"/><path d="M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  };
  const SUG_TAG = { vid: "Video", img: "Image", mus: "Music", mot: "Motion", edt: "Post-Edit", trn: "Trainer", llm: "Llama", sys: "System", mix: "Multi" };
  const EXAMPLES = [
    // Vid: agentic clips
    { s: "vid", t: "Generate me a video about birds" },
    { s: "vid", t: "Make a 30-second cinematic clip of a storm rolling in over the ocean" },
    { s: "vid", t: "Create a vertical video of a rainy neon city at night" },
    // Image: Krea / MOSS-Image / Qwen Image, alone and feeding Vid
    { s: "img", t: "Make an image of fruit mascot and use that to create a video" },
    { s: "img", t: "Design a cyberpunk poster for a synthwave party" },
    { s: "img", t: "Paint a watercolor mountain landscape at sunrise" },
    { s: "img", t: "Draw an astronaut cat floating in a sea of stars" },
    { s: "img", t: "Make three logo ideas for a coffee shop and give me the files to share" },
    // Music: Mus, alone and in a music video
    { s: "mus", t: "Create a song about AI and make a music video" },
    { s: "mus", t: "Write a lo-fi beat to study to" },
    { s: "mus", t: "Compose an epic orchestral theme for a space adventure" },
    { s: "mus", t: "Research this week's popular music styles and write a song in one of them" },
    // Motion: code-based Remotion overlays and typography
    { s: "mot", t: "Animate song lyrics as kinetic typography" },
    { s: "mot", t: "Make a countdown intro with pulsing neon numbers" },
    { s: "mot", t: "Put an animated title card over the start of my last video" },
    // Post-Edit: captions, music removal, voice-overs, final cuts
    { s: "edt", t: "Add pop-style captions to my last video" },
    { s: "edt", t: "Remove the music from my last video but keep the voices" },
    { s: "edt", t: "Add a voice-over intro to my last clip and fade it out at the end" },
    { s: "edt", t: "Make a montage of my finished clips with a song underneath" },
    // Trainer: LoRA datasets and runs
    { s: "trn", t: "Train a LoRA that turns everything into stained glass" },
    // Llama: llama-swap tuning on this machine
    { s: "llm", t: "Optimize a llama model for my hardware" },
    { s: "llm", t: "Benchmark qwen-27b and tune its llama-swap settings for my GPU" },
    { s: "llm", t: "Research current llama.cpp tuning tips for the RX 7900 XTX, then tune qwen-9b with them" },
    // System: services, the GPU queue, notifications
    { s: "sys", t: "Show me which services are running and which start at boot" },
    { s: "sys", t: "What's in the GPU queue right now, and what is each studio doing?" },
    { s: "sys", t: "Render a storm clip and send a notification to my phone when it's done" },
    // Chains across studios
    { s: "mix", t: "Stitch three clips into one video with a song underneath" },
    { s: "mix", t: "Make a nature slideshow with an upbeat soundtrack and captions" },
    { s: "mix", t: "Make a poster, turn it into a 10-second video, then add music and captions" },
  ];
  const sugIcon = ( kind ) => {
    const box = el( "span", "a-sug-ic" );
    const svg = document.createElementNS( "http://www.w3.org/2000/svg", "svg" );
    for ( const [k, v] of Object.entries( { viewBox: "0 0 24 24", width: "18", height: "18", fill: "none", stroke: "currentColor",
      "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" } ) ) svg.setAttribute( k, v );
    svg.innerHTML = SUG_ICON[kind] || SUG_ICON.mix;  // constant markup from SUG_ICON above
    box.append( svg );
    return box;
  };
  if ( $( ".a-sugs" ) ) {
    // One example from each of six different kinds, so the list shows the range.
    const kinds = [ ...new Set( EXAMPLES.map( ( x ) => x.s ) ) ].sort( () => Math.random() - 0.5 ).slice( 0, 6 );
    const picks = kinds.map( ( k ) => { const pool = EXAMPLES.filter( ( x ) => x.s === k ); return pool[Math.floor( Math.random() * pool.length )]; } );
    $( ".a-sugs" ).replaceChildren( ...picks.map( ( x ) => {
      const b = el( "button", "a-sug" ); b.type = "button"; b.dataset.s = x.s;
      b.append( sugIcon( x.s ), el( "span", "a-sug-tx", x.t ), el( "span", "a-sug-tag", SUG_TAG[x.s] ) );
      return b;
    } ) );
  }
  // Stop: in the header and next to Send/Steer (both shown only while the agent works).
  async function stopSession() {
    const buttons = [$("#a-stop"), $("#a-stop-inline")];
    if (!current || buttons.every((b) => b.hidden || b.disabled)) return;
    buttons.forEach((b) => { b.disabled = true; }); $("#a-stage").textContent = "Stopping…";
    try { await api(`/sessions/${current}/stop`, { method: "POST" }); } catch (e) { msg(e.message); buttons.forEach((b) => { b.disabled = false; }); }
  }
  $("#a-stop").addEventListener("click", stopSession);
  $("#a-stop-inline").addEventListener("click", stopSession);
  $("#a-delete").addEventListener("click", () => {
    if (!sessionOf(current)) sessionsList.push({ id: current, title: cache.get(current)?.snap.title || "", busy: false });
    removeSessions([current]);
  });
  document.querySelectorAll("[data-choice]").forEach((b) => b.addEventListener("click", async () => {
    $("#a-approval").hidden = true;
    try { await api(`/sessions/${current}/approve`, { method: "POST", body: JSON.stringify({ choice: b.dataset.choice }) }); }
    catch (e) { msg(e.message); $("#a-approval").hidden = false; }
  }));
  document.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
    $("#a-approval").hidden = true;
    try { await api(`/sessions/${current}/approve`, { method: "POST", body: JSON.stringify({ approve: b.dataset.approve === "1" }) }); }
    catch (e) { msg(e.message); $("#a-approval").hidden = false; }
  }));

  /* ------------------------------------------------ gallery -- */
  let galleryTab = "made", viewing = -1;
  function galleryItems(tab) {
    const snap = cache.get(current)?.snap;
    if (!snap) return [];
    if (tab === "sent") return (snap.attachments || []).map((a) => ({ key: a.id, kind: a.kind, url: a.url, thumb: a.kind === "image" ? a.url : "",
      caption: `${a.ref} · ${a.name} · ${kb(a.size)}`, open: a.url, name: a.name }));
    return (snap.jobs || []).flatMap((j) => (j.outputs || []).filter((o) => o.url).map((o, i) => ({
      key: `${j.ref}:${i}`, kind: o.kind, url: o.url, thumb: o.thumb || (o.kind === "image" ? o.url : ""),
      caption: `${j.ref} · ${j.label}`, open: o.open || `/${PAGE[j.service] || j.service}/`, name: o.name || "" })));
  }
  function thumbNode(item) {
    if (item.kind === "image" || item.thumb) { const img = el("img"); img.src = item.thumb || item.url; img.alt = ""; img.loading = "lazy"; img.decoding = "async"; return img; }
    if (item.kind === "video") { const v = el("video"); v.src = item.url + "#t=0.1"; v.muted = true; v.preload = "metadata"; v.playsInline = true; return v; }
    return el("span", "a-att-icon", item.kind === "audio" ? "♪" : "📄");
  }
  function renderGallery() {
    const items = galleryItems(galleryTab);
    document.querySelectorAll("#a-gallery [data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === galleryTab)));
    const grid = $("#a-gallery-grid");
    const keys = items.map((i) => i.key).join("|");
    if (grid.dataset.keys !== keys) {
      grid.dataset.keys = keys;
      grid.replaceChildren(...(items.length ? items.map((item, index) => {
        const b = el("button", "a-gal-item"); b.type = "button"; b.dataset.kind = item.kind;
        b.setAttribute("aria-label", item.caption);
        b.append(thumbNode(item), el("span", "a-gal-cap", item.caption));
        b.addEventListener("click", () => view(index));
        return b;
      }) : [el("p", "a-gal-empty", galleryTab === "made" ? "Nothing finished yet. Outputs land here as the agent's jobs complete." : "You haven't attached anything in this session.")]));
    }
  }
  function view(index) {
    const items = galleryItems(galleryTab);
    if (!items.length) return;
    viewing = (index + items.length) % items.length;
    const item = items[viewing];
    const stage = $("#a-viewer-media");
    let node;
    if (item.kind === "video") { node = el("video"); node.src = item.url; node.controls = true; node.autoplay = true; node.playsInline = true; }
    else if (item.kind === "audio") { node = el("audio"); node.src = item.url; node.controls = true; node.autoplay = true; }
    else if (item.kind === "image") { node = el("img"); node.src = item.url; node.alt = item.caption; }
    else node = el("p", null, item.name || "File");
    stage.replaceChildren(node);
    $("#a-viewer-caption").textContent = `${viewing + 1} / ${items.length} · ${item.caption}`;
    $("#a-viewer-open").href = item.open; $("#a-viewer-open").hidden = galleryTab === "sent";
    $("#a-viewer-download").href = item.url;
    $("#a-viewer").hidden = false; $("#a-gallery-grid").hidden = true;
  }
  function closeViewer() { $("#a-viewer").hidden = true; $("#a-gallery-grid").hidden = false; $("#a-viewer-media").replaceChildren(); viewing = -1; }
  function openGallery(tab = "made", key = null) {
    galleryTab = tab; closeViewer(); renderGallery();
    const dlg = $("#a-gallery");
    if (!dlg.open) dlg.showModal();
    if (key) { const i = galleryItems(tab).findIndex((x) => x.key === key); if (i >= 0) view(i); }
  }
  $("#a-gallery-open").addEventListener("click", () => openGallery((galleryItems("made").length ? "made" : "sent")));
  $("#a-gallery-close").addEventListener("click", () => $("#a-gallery").close());
  $("#a-gallery").addEventListener("close", closeViewer);
  $("#a-gallery").addEventListener("click", (e) => { if (e.target === $("#a-gallery")) $("#a-gallery").close(); });
  document.querySelectorAll("#a-gallery [data-tab]").forEach((t) => t.addEventListener("click", () => { galleryTab = t.dataset.tab; closeViewer(); renderGallery(); }));
  document.querySelectorAll(".a-viewer-nav").forEach((b) => b.addEventListener("click", () => view(viewing + Number(b.dataset.step))));
  $("#a-viewer-back").addEventListener("click", closeViewer);
  $("#a-gallery").addEventListener("keydown", (e) => {
    if ($("#a-viewer").hidden) return;
    if (e.key === "ArrowRight") { e.preventDefault(); view(viewing + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); view(viewing - 1); }
    else if (e.key === "Escape") { e.preventDefault(); closeViewer(); }
  });

  // Thinking / tool lines: visible by default, one toggle.
  const toggle = el("button", "a-think-toggle"); toggle.type = "button";
  const label = () => { toggle.textContent = hideThink ? "Show thinking and tool calls" : "Hide thinking and tool calls"; $("#a-log").classList.toggle("hide-think", hideThink); };
  toggle.addEventListener("click", () => { hideThink = !hideThink; label(); try { localStorage.setItem("agent-think-stream", hideThink ? "hide" : "show"); } catch (_) {} });
  $("#a-log").before(toggle); label();

  // A hidden tab drops its stream; coming back reconnects from where it left off.
  document.addEventListener("visibilitychange", () => { if (document.hidden) closeStream(); else if (current) connect(); });
  // With a session open its stream carries the list; otherwise refresh it now and then.
  setInterval(() => {
    listKey = "";
    if (!stream && !document.hidden) api("/sessions").then((r) => renderList(r.sessions)).catch(() => {});
  }, 20000);

  /* ------------------------------------------------ phones: sessions drawer, swipes -- */
  const narrow = matchMedia("(max-width: 760px)");
  function drawer(on) {
    on = on && narrow.matches;
    document.body.classList.toggle("drawer-open", on);
    $("#a-scrim").hidden = !on;
    $("#a-menu").setAttribute("aria-expanded", String(on));
    if (on) ($(".a-sess[aria-current=true]") || $("#a-new")).focus({ preventScroll: true });
  }
  $("#a-menu").addEventListener("click", () => drawer(!document.body.classList.contains("drawer-open")));
  $("#a-scrim").addEventListener("click", () => drawer(false));
  $("#a-new").addEventListener("click", () => drawer(false));
  const hint = () => { input.placeholder = narrow.matches ? "Ask the Master Agent… (/ for commands)" : `Ask the Master Agent… (/ for commands, ${/Mac|iP/.test(navigator.platform) ? "⌘" : "Ctrl"}+Enter to send)`; };
  narrow.addEventListener("change", () => { drawer(false); hint(); }); hint();
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("drawer-open")) drawer(false); });
  // swipe the drawer left to close it; swipe the viewer left/right for the next/previous output
  function swipe(node, onSwipe) {
    let start = null;
    node.addEventListener("touchstart", (e) => { const p = e.touches[0]; start = e.touches.length === 1 ? { x: p.clientX, y: p.clientY, t: Date.now() } : null; }, { passive: true });
    node.addEventListener("touchend", (e) => {
      if (!start) return;
      const p = e.changedTouches[0], dx = p.clientX - start.x, dy = p.clientY - start.y;
      if (Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy) && Date.now() - start.t < 600) onSwipe(dx < 0 ? -1 : 1);
      start = null;
    }, { passive: true });
  }
  swipe($("#a-side"), (dir) => { if (dir < 0) drawer(false); });
  swipe($("#a-viewer-media"), (dir) => view(viewing - dir));

  /* ------------------------------------------------ start -- */
  let initial = null;
  try { initial = JSON.parse($("#a-initial")?.textContent || "null"); } catch (_) {}
  const params = new URLSearchParams(location.search);
  if (initial?.session) remember(initial.session, true);
  COMMANDS = initial?.commands || [];
  if (!COMMANDS.length) api("/commands").then((r) => { COMMANDS = r.commands; }).catch(() => {});
  current = params.get("s") || null;
  renderList(initial?.sessions || []);
  if (params.get("prompt")) { input.value = params.get("prompt"); grow(); }   // no-JS fallback from home: prefill only
  current = null;
  open(params.get("s"), false);
  if (!initial) api("/sessions").then((r) => renderList(r.sessions)).catch((e) => msg(e.message));
})();
