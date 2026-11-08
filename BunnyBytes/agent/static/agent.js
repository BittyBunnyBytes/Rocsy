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
                  running: "Running", training: "Training", finalizing: "Finalizing", cancelled: "Cancelled", canceled: "Cancelled",
                  paused: "Paused" };
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
  let draftProject = "", projectTab = "chats";   // the project whose page is open: new chats start in it
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
    const shown = list.filter((s) => !doomed.has(s.id) && !s.project);    // a project's chats live on its page
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
    $("#a-menu-dot").hidden = !list.some((s) => s.busy && s.id !== current);
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
    for (const id of ids) { doomed.set(id, sessionOf(id)?.title || cache.get(id)?.snap.title || (openProject?.chats || []).find((c) => c.id === id)?.title || "session"); selected.delete(id); }
    if (doomed.has(current)) {
      const home = cache.get(current)?.snap.project || sessionOf(current)?.project;
      if (home && projectOf(home)) showProject(home); else open(null);
    }
    renderList(sessionsList, true); repaintProject();
    const n = doomed.size;
    toast(n === 1 ? `Deleted “${[...doomed.values()][0]}”` : `Deleted ${n} ${openProject ? "chats" : "sessions"}`, undoDelete);
    clearTimeout(doomTimer);
    doomTimer = setTimeout(commitDeletes, 6000);
  }
  function undoDelete() {
    clearTimeout(doomTimer);
    const back = [...doomed.keys()]; doomed.clear();
    renderList(sessionsList, true); repaintProject(); hideToast();
    if (back.length === 1 && !openProject) open(back[0]);
  }
  async function commitDeletes(keepalive = false) {
    clearTimeout(doomTimer);
    if (!doomed.size) return;
    const ids = [...doomed.keys()];
    let gone = [];
    hideToast();
    try {
      const r = await api("/sessions/delete", { method: "POST", body: JSON.stringify({ ids }), keepalive });
      gone = r.deleted;
      for (const id of r.deleted) { doomed.delete(id); cache.delete(id); titles.delete(id); }
      sessionsList = sessionsList.filter((s) => !r.deleted.includes(s.id));
      for (const id of r.busy) doomed.delete(id);
      if (r.busy.length) msg(`${r.busy.length} session${r.busy.length > 1 ? "s were" : " was"} running again, so ${r.busy.length > 1 ? "they weren't" : "it wasn't"} deleted`);
    } catch (err) { ids.forEach((id) => doomed.delete(id)); msg(err.message); }
    renderList(sessionsList, true);
    if (openProject) {
      openProject = { ...openProject, chats: (openProject.chats || []).filter((c) => !gone.includes(c.id)) };
      repaintProject();
      api(`/projects/${openProject.id}`).then(paintProject).catch(() => {});
    }
    refreshProjects().catch(() => {});
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
    sessionsList.filter((s) => !s.busy && !s.project && !doomed.has(s.id)).forEach((s) => selected.add(s.id));
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
      if (e.kind === "user") {           // your messages: a bubble, with its actions in a row under it
        const bubble = el("div", "a-ev-bubble"); bubble.append(text); li.append(bubble);
      } else li.append(text);
    }
    if (e.text && (e.kind === "assistant" || e.kind === "say")) {
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
    if (e.text && e.kind === "user") {   // after any attachments, so nothing covers it, however long the message
      const actions = el("div", "a-ev-actions");
      actions.append(copyButton(() => e.text, "your message", "a-copy-user"));
      li.append(actions);
    }
    if (e.kind === "ask") askCard(li, e);
    if (e.kind === "offer") offerRow(li, e);
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
  // Optional follow-ups under a reply: nothing waits on them. A click sends that text; any message
  // of yours sets them aside (still visible, no longer clickable).
  function offerRow(li, e) {
    li.querySelector(".a-ev-text")?.classList.add("a-offer-text");
    const row = el("div", "a-offer-choices");
    for (const c of e.choices || []) {
      const b = el("button", "a-offer-choice", c); b.type = "button"; b.dataset.choice = c;
      b.addEventListener("click", () => { closeOffer(li, c); deliver({ text: c, files: [] }); });
      row.append(b);
    }
    li.append(row);
  }
  function closeOffer(li, text) {
    if (li.classList.contains("is-closed")) return;
    li.classList.add("is-closed");
    li.querySelectorAll("button.a-offer-choice").forEach((b) => { b.disabled = true; if (b.dataset.choice === text) b.classList.add("is-picked"); });
  }
  const closeOpenOffers = (text) => document.querySelectorAll('#a-log .a-ev[data-kind="offer"]:not(.is-closed)').forEach((n) => closeOffer(n, text));
  function answerAsk(li, text) {
    if (li.classList.contains("is-answered")) return;
    li.classList.add("is-answered");
    $(".a-ask-state", li).textContent = "Answered";
    li.querySelectorAll("button").forEach((b) => { b.disabled = true; if (b.dataset.choice === text) b.classList.add("is-picked"); });
  }
  const answerOpenAsks = (text) => document.querySelectorAll('#a-log .a-ev[data-kind="ask"]:not(.is-answered)').forEach((n) => answerAsk(n, text));

  // Countdowns under the open question: "Keep the model loaded after a question" (until it unloads)
  // and "Answer its own questions" (until the Master Agent picks the answer itself).
  let parkTick = null;
  function renderParked(snap) {
    clearInterval(parkTick); parkTick = null;
    document.querySelectorAll("#a-log .a-ask-timer").forEach((n) => n.remove());
    const card = [...document.querySelectorAll('#a-log .a-ev[data-kind="ask"]:not(.is-answered)')].pop();
    if (!card) return;
    const timers = [
      snap?.parked?.until && { until: snap.parked.until, text: (t) => `Model stays loaded for ${t}`, end: "Unloading the model…" },
      snap?.auto_answer?.at && { until: snap.auto_answer.at, text: (t) => `No answer? The Master Agent picks the best one in ${t}`, end: "The Master Agent is picking the answer…" },
    ].filter(Boolean);
    const ticks = timers.map((x) => {
      const line = el("div", "a-ask-timer");
      const text = el("span", "a-ask-timer-text"), bar = el("span", "a-ask-timer-bar"), fill = el("span");
      bar.append(fill); line.append(text, bar); card.append(line);
      const total = Math.max(1, x.until - Date.now() / 1000);
      return () => {
        const left = Math.max(0, Math.round(x.until - Date.now() / 1000));
        text.textContent = left ? x.text(`${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`) : x.end;
        fill.style.width = `${Math.min(100, (left / total) * 100)}%`;
        return left;
      };
    });
    if (!ticks.length) return;
    const tick = () => { if (!ticks.map((f) => f()).some(Boolean)) { clearInterval(parkTick); parkTick = null; } };
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
      if (e.kind === "terminal" && String(e.tool_id || "").startsWith("h3-agentic:")) { agenticLogEvent(e, log); continue; }
      if (e.kind === "terminal") {
        const id = String(e.tool_id || e.i);
        let panel = terminals.get(id);
        const agentic = parseAgentic(e.command || "");
        if (e.command && panel?.isConnected && !!agentic !== panel.classList.contains("is-agentic") && !$(".a-terminal-output", panel).dataset.filled) {
          const fresh = agenticPanel(id, agentic); panel.replaceWith(fresh); panel = fresh; terminals.set(id, panel);   // command arrived late
        }
        if ((!panel || !panel.isConnected) && agentic) { panel = agenticPanel(id, agentic); terminals.set(id, panel); log.append(panel); }
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
        if (e.command || !$(".a-terminal-command", panel).textContent) $(".a-terminal-command", panel).textContent = "$ " + (e.command || "");
        if (!panel.classList.contains("is-agentic")) $(".a-term-peek", panel).textContent = (e.command || "").replace(/\s+/g, " ").trim().slice(0, 160);
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
          if (!panel.dataset.touched && !panel.classList.contains("is-agentic") && terminalOutput.textContent.length > 280) $(".a-term", panel).open = false;
          if (panel.classList.contains("is-agentic")) agenticResult(panel, output);
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
        answerOpenAsks(e.text); closeOpenOffers(e.text);
        const mine = [...log.querySelectorAll(".a-ev.is-pending")].find((n) => n.dataset.text === e.text);
        if (mine) { mine.replaceWith(eventNode(e)); continue; }
      }
      const node = eventNode(e);
      const liveThink = e.kind === "think" && log.querySelector('.a-ev[data-live="reasoning"]');
      if (liveThink) adoptLiveThink(liveThink, node);
      else log.append(node);
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
          // Each frame rebuilds the reply, so its code blocks are new elements that would open at the top.
          // Carry every block's scroll over (by position in the reply): one you were at the bottom of keeps
          // following the newest lines; one you scrolled up in stays exactly where you left it. Full screen too.
          const SCROLLERS = ".a-md-code, .a-md-table";
          const was = [...node.querySelectorAll(SCROLLERS)].map((s) => ({
            top: s.scrollTop, left: s.scrollLeft, tail: s.scrollHeight - s.scrollTop - s.clientHeight < 24,
            full: Boolean(s.closest(".a-md-codebox.is-full")) }));
          node.replaceChildren(md);
          [...node.querySelectorAll(SCROLLERS)].forEach((s, i) => {
            const w = was[i]; if (!w) return;
            if (w.full) { const box = s.closest(".a-md-codebox"); if (box) setFullCode(box, true); }
            s.scrollTop = w.tail ? s.scrollHeight : w.top;
            s.scrollLeft = w.left;
          });
        }
      }
      if (node !== log.lastElementChild && !(key === "reasoning" && node.nextElementSibling?.dataset.live === "assistant")) {
        const inner = kind === "think" && $(".a-think-body", node), top = inner ? inner.scrollTop : 0;
        log.append(node);                                      // moving a node resets its scroll: put it back
        if (inner && inner._held) { inner.scrollTop = top; inner._lastTop = top; }
      }
      if (kind === "think") { const body = $(".a-think-body", node); if (!body._held) body.scrollTop = body.scrollHeight; }   // once attached
    }
  }


  /* ------------------------------------------- agentic calls (own box) -- */
  // `$R agentic "request" --ref … --duration …` (or rocsy.py SESSION agentic …) gets its own card instead of a
  // plain terminal: the model, the options as chips, the request in full (H3 colours for prompts and ::VIDEO
  // plans), the result, and the raw command folded away.
  const AGENTIC_CALL = /(?:\$R|\$\{R\}|rocsy\.py\s+[0-9a-f]{32})\s+agentic(?=\s)/;
  const AGENTIC_MODELS = { minimax_h3: "MiniMax H3", wan_2_2: "Wan 2.2" };
  // Shell words from the start of src up to the end of that command (&& ; | newline or a redirect).
  function shellWords(src) {
    const words = []; let cur = null, i = 0;
    const push = () => { if (cur !== null) words.push(cur); cur = null; };
    while (i < src.length) {
      const c = src[i];
      if (c === "\n" || c === ";" || c === "|" || c === "&" || c === ">" || c === "<") break;
      if (/\s/.test(c)) { push(); i++; continue; }
      if (c === "'") { const j = src.indexOf("'", i + 1), end = j < 0 ? src.length : j; cur = (cur ?? "") + src.slice(i + 1, end); i = end + 1; continue; }
      if (c === '"') {
        let j = i + 1, out = "";
        while (j < src.length && src[j] !== '"') { if (src[j] === "\\" && '"\\$`\n'.includes(src[j + 1])) { out += src[j + 1]; j += 2; } else out += src[j++]; }
        cur = (cur ?? "") + out; i = j + 1; continue;
      }
      if (c === "\\") { cur = (cur ?? "") + (src[i + 1] ?? ""); i += 2; continue; }
      cur = (cur ?? "") + c; i++;
    }
    push();
    return words;
  }
  function parseAgentic(command) {
    const m = AGENTIC_CALL.exec(command);
    if (!m) return null;
    const words = shellWords(command.slice(m.index + m[0].length)), opts = {};
    let prompt = null;
    for (let k = 0; k < words.length; k++) {
      const w = words[k];
      if (w === "-h" || w === "--help") return null;
      if (w.startsWith("--")) { const [key, val] = w.slice(2).split(/=(.*)/s); opts[key] = val ?? words[++k] ?? ""; }
      else if (prompt === null) prompt = w;
    }
    if (!prompt) return null;
    // "$REQ" with REQ="$(cat FILE)" earlier in the command, or "$(cat FILE)" itself: say where it came from.
    let file = (prompt.match(/^\$\(cat\s+["']?([^"')]+)["']?\)$/) || [])[1];
    const v = prompt.match(/^\$\{?(\w+)\}?$/);
    if (v) file = (command.match(new RegExp(`\\b${v[1]}=["']?\\$\\(cat\\s+["']?([^"')]+)["']?\\)`)) || [])[1] || `$${v[1]}`;
    return { prompt: file ? "" : prompt, file, opts };
  }
  function agenticChip(text, cls = "") { return el("span", `a-agentic-chip ${cls}`.trim(), text); }
  // A clickable chip/pill: mouse, Enter or Space.
  function clickable(node, fn, title) {
    node.classList.add("is-clickable"); node.tabIndex = 0; node.setAttribute("role", "button"); if (title) node.title = title;
    node.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); } });
  }
  // Long rows (references, clips): the first `keep` items and a "+N more" toggle once there are more than `max`.
  function collapseRow(row, selector, max, keep, onToggle) {
    row.querySelector(":scope > .a-agentic-more")?.remove();
    const items = [...row.querySelectorAll(`:scope > ${selector}`)];
    if (items.length <= max) { items.forEach((x) => x.classList.remove("is-folded")); return; }
    const open = row.dataset.expanded === "1";
    items.forEach((x, i) => x.classList.toggle("is-folded", !open && i >= keep && !x.classList.contains("is-pinned")));
    const hidden = items.filter((x) => x.classList.contains("is-folded")).length;
    const more = el("button", "a-agentic-more", open ? "Show fewer" : `+${hidden} more`); more.type = "button";
    more.addEventListener("click", (e) => { e.stopPropagation(); row.dataset.expanded = open ? "" : "1"; onToggle?.(!open); collapseRow(row, selector, max, keep, onToggle); });
    row.append(more);
  }
  // What a ref chip opens: an attachment in the gallery's Sent tab, a job's outputs in Made.
  function openRefMedia(ref) {
    const snap = cache.get(current)?.snap || {};
    const a = (snap.attachments || []).find((x) => x.ref === String(ref).toLowerCase());
    if (a) return openGallery("sent", a.id);
    openJobMedia(ref);
  }
  function openJobMedia(ref) {
    const job = (cache.get(current)?.snap?.jobs || []).find((j) => j.ref === String(ref || "").toLowerCase());
    if (!job) return msg(ref ? `${ref} isn't in this session` : "The job hasn't started yet");
    if ((job.outputs || []).some((o) => o.url)) return openGallery("made", `${job.ref}:0`);
    const its = (job.iterations || []).filter((it) => it.url);
    if (its.length) return clipViewer(its.map((it) => iterationItem(job, it)), 0);
    msg(`${job.ref} has nothing to show yet`);
  }
  const iterationItem = (job, it) => ({ url: it.url, thumb: it.thumb, open: it.open,
    caption: `${job.ref} · clip ${it.clip ?? "?"} · ${it.phase === "full" ? "final" : "preview"} · attempt ${it.attempt || 1}${it.selected ? " · kept" : it.phase === "full" ? " · replaced" : ""}` });
  // A small player for clip renders (previews, retries, finals): ← → step through, Esc closes.
  function clipViewer(items, index) {
    let dlg = $("#a-clipview");
    if (!dlg) {
      dlg = el("dialog", "a-clipview"); dlg.id = "a-clipview";
      const head = el("div", "a-clipview-head"), cap = el("span", "a-clipview-cap"), close = el("button", "a-clipview-close", "✕"); close.type = "button"; close.title = "Close (Esc)";
      const openIn = el("a", "a-clipview-open", "Open in Video"); openIn.target = "_blank"; openIn.rel = "noopener";
      head.append(cap, openIn, close);
      const video = el("video"); video.controls = true; video.playsInline = true;
      const nav = el("div", "a-clipview-nav"), prev = el("button", "", "‹ Prev"), count = el("span", "a-clipview-count"), next = el("button", "", "Next ›");
      prev.type = next.type = "button"; nav.append(prev, count, next);
      dlg.append(head, video, nav); document.body.append(dlg);
      close.addEventListener("click", () => dlg.close());
      dlg.addEventListener("close", () => video.pause());
      dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
      prev.addEventListener("click", () => dlg._show(dlg._i - 1)); next.addEventListener("click", () => dlg._show(dlg._i + 1));
      dlg.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft") dlg._show(dlg._i - 1); else if (e.key === "ArrowRight") dlg._show(dlg._i + 1); });
    }
    dlg._show = (i) => {
      const list = dlg._items; if (!list.length) return;
      dlg._i = (i + list.length) % list.length;
      const it = list[dlg._i], video = $("video", dlg);
      video.src = it.url; video.poster = it.thumb || ""; video.play().catch(() => {});
      $(".a-clipview-cap", dlg).textContent = it.caption;
      const a = $(".a-clipview-open", dlg); a.hidden = !it.open; if (it.open) a.href = it.open;
      $(".a-clipview-count", dlg).textContent = `${dlg._i + 1} / ${list.length}`;
      $(".a-clipview-nav", dlg).hidden = list.length < 2;
    };
    dlg._items = items;
    if (!dlg.open) dlg.showModal();
    dlg._show(index);
  }
  function agenticPanel(id, call) {
    const panel = el("li", "a-terminal is-agentic"); panel.dataset.toolId = id;
    const details = el("details", "a-think a-term a-agentic");
    const summary = el("summary");
    const badge = el("span", "a-agentic-badge"); badge.append(el("span", "a-agentic-spark", "✦"), "Agentic");
    const model = AGENTIC_MODELS[call.opts.model] || call.opts.model || "MiniMax H3";
    const peek = call.file ? `request from ${call.file.split("/").pop()}` : call.prompt.replace(/\s+/g, " ").trim().slice(0, 160);
    summary.append(el("span", "a-think-icon"), badge, el("span", "a-think-label a-term-label a-agentic-model", model),
      el("span", "a-think-peek a-term-peek", peek), el("span", "a-think-meta a-term-meta", "starting"));
    summary.addEventListener("click", () => { panel.dataset.touched = "1"; });
    const body = el("div", "a-term-body a-agentic-body");
    const chips = el("div", "a-agentic-chips");
    const job = agenticChip("new job", "is-job"); chips.append(job);
    clickable(job, () => panel.dataset.ref && openJobMedia(panel.dataset.ref), "Watch this job's videos"); job.classList.remove("is-clickable");
    for (const r of String(call.opts.ref || "").split(/[,\s]+/).filter(Boolean)) {
      const kind = /^att/i.test(r) ? "att" : /^(image|krea|img)/i.test(r) ? "image" : /^mus/i.test(r) ? "audio" : /^vid/i.test(r) ? "video" : "";
      const chip = agenticChip(r, `is-ref ${kind ? "is-" + kind : ""}`);
      clickable(chip, () => openRefMedia(r), kind === "att" ? `Open ${r}` : `Watch ${r}`);
      chips.append(chip);
    }
    const label = { duration: (v) => `${v.replace(/s$/, "")} s`, aspect: (v) => v, mode: (v) => `${v} mode`, resume: (v) => `resumes ${v}`, preset: (v) => `preset ${v}` };
    for (const [k, f] of Object.entries(label)) if (call.opts[k]) {
      const chip = agenticChip(f(call.opts[k]));
      if (k === "resume") { chip.classList.add("is-video"); clickable(chip, () => openRefMedia(call.opts[k]), `Watch ${call.opts[k]}`); }
      chips.append(chip);
    }
    collapseRow(chips, ".a-agentic-chip", 10, 8);
    body.append(chips);
    const section = (name, node) => { const box = el("div", "a-agentic-section"); box.append(el("div", "a-agentic-label", name), node); return box; };
    if (call.file) body.append(section("Request", el("div", "a-agentic-file", `Read from ${call.file}`)));
    else if (modelSyntax(call.prompt, false)) body.append(section("Request", codeBox(call.prompt, modelSyntax(call.prompt, false))));
    else {
      const text = el("div", "a-agentic-prompt", call.prompt);
      const wrap = el("div", "a-agentic-prompt-wrap"); wrap.append(text, copyButton(() => call.prompt, "request", "a-agentic-copy"));
      body.append(section("Request", wrap));
    }
    if (call.opts.guide) body.append(section("Guide", el("div", "a-agentic-guide", call.opts.guide)));
    if (!call.orphan) body.append(section("Result", el("pre", "a-terminal-output a-agentic-output", "Starting Video's Agentic mode…")));
    else body.append(el("pre", "a-terminal-output a-agentic-output"));       // unused: a run card has no call output
    const run = el("div", "a-agentic-run"); run.hidden = true;
    const runHead = el("div", "a-agentic-runhead");
    runHead.append(el("span", "a-agentic-label", "Run"), el("span", "a-agentic-stage"));
    const activity = el("details", "a-agentic-fold a-agentic-activity"); activity.append(el("summary", "", "Activity"), el("ol", "a-agentic-events"));
    const vlog = el("details", "a-agentic-fold a-agentic-log"); vlog.append(el("summary", "", "Video's log"), el("pre", "a-agentic-logtext"));
    const alert = el("div", "a-agentic-alert"); alert.hidden = true;
    run.append(runHead, el("div", "a-agentic-overview"), el("div", "a-agentic-clips"), alert, activity, vlog);
    body.append(run);
    const raw = el("details", "a-agentic-raw"); raw.append(el("summary", "", "Command"), el("pre", "a-terminal-command"));
    body.append(raw);
    details.append(summary, body); details.open = true;
    panel.append(details);
    panel._job = job;
    return panel;
  }
  function agenticResult(panel, output) {
    const ref = (output.match(/\bstarted (\w+\d+)\b/) || [])[1];
    const meta = $(".a-term-meta", panel);
    if (panel.dataset.status === "failed" || /^error:/m.test(output) && !ref) { panel.dataset.status = "failed"; meta.textContent = "didn't start"; return; }
    if (ref) {
      panel._job.textContent = ref; panel._job.classList.add("is-started", "is-clickable"); meta.textContent = `${ref} started`;
      panel.dataset.ref = ref;
      const job = (cache.get(current)?.snap?.jobs || []).find((j) => j.ref === ref && j.kind === "agentic");
      if (job) agenticJob(job);
    }
  }

  // ---- the run itself: Video's Agentic log (mirrored as h3-agentic:<job id> terminal events) + the job record.
  const agenticCards = new Map();      // Vid job id -> its card (the call card, or a run-only card if no call card is in view)
  const agenticLogs = new Map();       // Vid job id -> latest log event
  function agenticCardFor(jobId) {
    const known = agenticCards.get(jobId);
    if (known?.isConnected) return known;
    const job = (cache.get(current)?.snap?.jobs || []).find((j) => j.job_id === jobId);
    const card = job && $("#a-log").querySelector(`.a-terminal.is-agentic[data-ref="${job.ref}"]:not(.is-orphan)`);
    if (card) { card.dataset.jobId = jobId; agenticCards.set(jobId, card); }
    return card || null;
  }
  function agenticLogEvent(e, log) {
    const jobId = String(e.tool_id).slice("h3-agentic:".length);
    agenticLogs.set(jobId, e);
    let card = agenticCardFor(jobId);
    if (!card) {                       // no call card (it was trimmed, or the run was started another way): a run-only card
      const label = String(e.command || "").replace(/^Video Agentic · /, "");
      card = agenticPanel(`run-${jobId}`, { prompt: label, file: null, opts: {}, orphan: true });
      card.classList.add("is-orphan"); card.dataset.jobId = jobId; card.dataset.status = "complete";
      $(".a-term-meta", card).textContent = "running";
      log.append(card); agenticCards.set(jobId, card);
    }
    agenticRunLog(card, e);
    const job = (cache.get(current)?.snap?.jobs || []).find((j) => j.job_id === jobId);
    if (job) agenticRunState(card, job);
  }
  function agenticJob(job) {
    let card = agenticCardFor(job.job_id);
    const call = $("#a-log").querySelector(`.a-terminal.is-agentic[data-ref="${job.ref}"]:not(.is-orphan)`);
    if (call && card !== call) {       // the call card turned up: it takes over from the run-only card
      card?.remove(); card = call; card.dataset.jobId = job.job_id; agenticCards.set(job.job_id, card);
      if (agenticLogs.has(job.job_id)) agenticRunLog(card, agenticLogs.get(job.job_id));
    }
    if (card) agenticRunState(card, job);
  }
  // What the log says about the plan and each clip: attempts, failures, rerolls, reviews.
  function parseAgenticLog(text) {
    const run = { planned: 0, phases: { staging: new Map(), full: new Map() }, lastPhase: new Map(), events: [], finished: 0, ended: false, lastError: "" };
    const clip = (n, phase) => {
      n = Number(n); phase = phase || run.lastPhase.get(n) || "staging";
      const m = run.phases[phase];
      if (!m.has(n)) m.set(n, { n, phase, attempts: 0, failures: [], rerolls: 0, state: "waiting" });
      return m.get(n);
    };
    let last = null;
    for (const raw of text.split("\n")) {
      const m = raw.match(/^\[(\w+)\] (.*)$/);
      if (!m) continue;
      const [, kind, line] = m;
      let x;
      if ((x = line.match(/Validated (\d+) clip\(s\)|revised the plan: (\d+) clip\(s\)/))) { run.planned = Number(x[1] || x[2]); run.events.push({ kind: "ok", text: line }); continue; }
      if ((x = line.match(/^(Staging|Full) clip (\d+), attempt (\d+): queued/))) {
        const phase = x[1].toLowerCase(), c = clip(x[2], phase);
        run.lastPhase.set(Number(x[2]), phase);
        c.attempts = Math.max(c.attempts, Number(x[3])); c.state = "rendering"; last = c;
        if (Number(x[3]) > 1) run.events.push({ kind: "reroll", text: `${x[1]} clip ${x[2]}: attempt ${x[3]}` });
        continue;
      }
      if ((x = line.match(/^Clip (\d+) finished/))) { const c = clip(x[1]); c.state = "done"; run.events.push({ kind: "ok", text: `${c.phase === "full" ? "Clip" : "Preview of clip"} ${x[1]} finished` }); continue; }
      if ((x = line.match(/^(Staging|Full) clip (\d+) attempt (\d+) failed: (.*)$/))) {
        const c = clip(x[2], x[1].toLowerCase()); c.failures.push({ attempt: Number(x[3]), reason: x[4] }); c.state = "failed"; run.lastError = line;
        run.events.push({ kind: "error", text: line }); continue;
      }
      if ((x = line.match(/^(Staging|Full)? ?clip (\d+) was refused .*?wasn't retried: (.*)$/i))) {
        const c = clip(x[2], x[1] && x[1].toLowerCase()); c.state = "failed"; c.failures.push({ attempt: c.attempts, reason: x[3] }); run.lastError = line;
        run.events.push({ kind: "error", text: line }); continue;
      }
      if ((x = line.match(/^Staging retry \d+: clip (\d+)|Best of \d+: rendering a second version of staging clip (\d+)|^Refining staging from clip (\d+)/))) {
        clip(x[1] || x[2] || x[3], "staging").rerolls++; run.events.push({ kind: "reroll", text: line }); continue;
      }
      if (/Retrying the same clip unchanged/.test(line)) { if (last) last.rerolls++; run.events.push({ kind: "reroll", text: (last ? `Clip ${last.n}: ` : "") + line }); continue; }
      if ((x = line.match(/Agentic session finished (\d+) full clip/))) { run.finished = Number(x[1]); run.events.push({ kind: "ok", text: line }); continue; }
      if (/Agentic session ended; cancelling/.test(line)) { run.ended = true; run.events.push({ kind: "warn", text: line }); continue; }
      if (kind === "ERROR") { run.lastError = line; run.events.push({ kind: "error", text: line }); continue; }
      if (kind === "WARN" && !/^Whisper: clip \d+'s speech/.test(line)) run.events.push({ kind: "warn", text: line });
      else if (/Replaced clip\(s\)|Clip \d+ rewritten|passed footage review|kept render|Staging previews are ready|Clip plan ready/.test(line)) run.events.push({ kind: "ok", text: line });
    }
    return run;
  }
  function agenticRunLog(card, e) {
    const run = $(".a-agentic-run", card); run.hidden = false;
    const text = String(e.text || "");
    card._log = parseAgenticLog(text); card._logError = String(e.error || "");
    const pre = $(".a-agentic-logtext", card), atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.textContent = text; if (atEnd) pre.scrollTop = pre.scrollHeight;
    const list = $(".a-agentic-events", card); list.replaceChildren();
    const shown = card._log.events.slice(-40);
    for (const x of shown) { const li = el("li", `is-${x.kind}`); li.append(el("span", "a-agentic-evicon", { ok: "✓", warn: "!", error: "✕", reroll: "↻" }[x.kind] || "•"), el("span", "", x.text)); list.append(li); }
    const problems = card._log.events.filter((x) => x.kind !== "ok").length;
    $(".a-agentic-activity > summary", card).textContent = `Activity · ${card._log.events.length}` + (problems ? ` (${problems} retr${problems === 1 ? "y" : "ies"}/warning${problems === 1 ? "" : "s"})` : "");
    agenticPaint(card);
  }
  function agenticRunState(card, job) {
    $(".a-agentic-run", card).hidden = false;
    card._jobState = job;
    agenticPaint(card);
  }
  const AGENTIC_RUNNING = new Set(["queued", "queuing", "preparing", "rendering", "running", "waiting_for_gpu", "planning", "finalizing", "encoding", "cancelling"]);
  function agenticPaint(card) {
    const job = card._jobState || {}, run = card._log || parseAgenticLog("");
    const state = String(job.state || "running");
    const outs = (job.outputs || []).filter((o) => o.kind === "video").length;
    const replaced = (job.iterations || []).filter((it) => it.phase === "full" && !it.selected).length;
    const kind = ["complete", "completed"].includes(state) ? "done" : ["failed", "error", "interrupted"].includes(state) ? "failed"
      : ["cancelled", "canceled"].includes(state) ? "cancelled" : state === "paused" ? "paused" : state === "awaiting_approval" ? "approval" : "running";
    card.dataset.run = kind;
    // At a glance (Vid's prompt ledger): how many clips are being written, written, rendered and verified.
    const ov = job.overview, box = $(".a-agentic-overview", card);
    box.replaceChildren(); box.hidden = !(ov && ov.total);
    if (ov && ov.total) {
      const KINDS = [["v", "verified", "verified"], ["d", "done", "rendered"], ["w", "written", "written"], ["i", "writing", "writing"], ["-", "todo", "to come"]];
      const counts = el("div", "a-agentic-counts");
      for (const [letter, key, name] of KINDS) {
        const n = ov.counts?.[key] || 0;
        if (!n && key !== "verified") continue;
        const c = el("span", `a-agentic-count is-${key}`); c.append(el("span", "a-agentic-dot"), el("b", "", String(n)), ` ${name}`);
        c.title = { verified: "Final render kept: locked (prompts/verified)", done: "Rendered, waiting for its final review",
          written: "Prompt validated and reviewed, waiting to render (prompts/written)", writing: "Draft prompt being checked or repaired (prompts/in-progress)",
          todo: "In the outline, not written yet" }[key];
        counts.append(c);
      }
      counts.append(el("span", "a-agentic-total", `${ov.total} clip${ov.total === 1 ? "" : "s"}` + (ov.directions ? ` · ${ov.directions} standing direction${ov.directions === 1 ? "" : "s"}` : "")));
      const grid = el("div", "a-agentic-grid");
      [...String(ov.strip || "")].forEach((letter, i) => {
        const k = KINDS.find((x) => x[0] === letter) || KINDS[4];
        const sq = el("span", `a-agentic-sq is-${k[1]}`); sq.title = `Clip ${i + 1}: ${k[2]}`; grid.append(sq);
      });
      const bar = el("div", "a-agentic-bar");
      for (const [, key] of KINDS) { const n = ov.counts?.[key] || 0; if (n) { const seg = el("span", `a-agentic-seg is-${key}`); seg.style.flexGrow = String(n); bar.append(seg); } }
      box.append(counts, bar, grid);
    }
    // the clip rows: previews (staging) and final renders, one pill per clip
    const strip = $(".a-agentic-clips", card); strip.replaceChildren();
    const ids = (m) => [...m.keys()];
    const count = Math.max(run.planned, ...ids(run.phases.staging), ...ids(run.phases.full), 0);
    const its = (job.iterations || []).filter((it) => it.url);
    const hasFull = run.phases.full.size > 0 || its.some((it) => it.phase === "full");
    const rows = [];
    if (run.phases.staging.size || its.some((it) => it.phase !== "full")) rows.push(["staging", "Previews"]);
    if (hasFull || (!rows.length && count)) rows.push(["full", "Final"]);
    for (const [phase, name] of rows) {
      const row = el("div", "a-agentic-cliprow"); row.dataset.phase = phase;
      if (card._expanded?.[phase]) row.dataset.expanded = "1";
      row.append(el("span", "a-agentic-cliplabel", name));
      const m = run.phases[phase];
      for (let n = 1; n <= count; n++) {
        const c = m.get(n) || { n, phase, attempts: 0, failures: [], rerolls: 0, state: "waiting" };
        let st = c.state;
        if (st === "rendering" && kind !== "running" && kind !== "paused" && kind !== "approval") st = kind === "done" ? "done" : kind === "cancelled" ? "cancelled" : "failed";   // ended mid-render
        if (st === "waiting" && kind === "done" && phase === "full") st = "done";
        const vids = its.filter((it) => Number(it.clip) === n && (it.phase === "full") === (phase === "full"));
        if (st === "waiting" && vids.length) st = "done";
        // Renders it took: attempts (failed ones included) or the versions there are to watch (best-of, staging retries).
        const versions = vids.length, renders = Math.max(c.attempts, versions, 1);
        const pill = el("span", `a-agentic-clip is-${st}`, String(n));
        if (renders > 1) pill.append(el("span", "a-agentic-tries", `×${renders}`));
        if (st === "rendering" || st === "failed") pill.classList.add("is-pinned");      // never folded away
        const info = [`Clip ${n} · ${phase === "full" ? "final render" : "preview"} · ${st}`, c.attempts ? `attempt ${c.attempts}` : "",
          c.rerolls ? `${c.rerolls} reroll(s)/retr${c.rerolls === 1 ? "y" : "ies"}` : "", ...c.failures.map((f) => `attempt ${f.attempt} failed: ${f.reason}`),
          versions ? `${versions} version${versions === 1 ? "" : "s"} to watch: click` : ""].filter(Boolean).join("\n");
        pill.title = info;
        if (versions) {
          const ordered = [...vids].sort((x, y) => (y.selected - x.selected) || ((x.attempt || 1) - (y.attempt || 1)));
          clickable(pill, () => clipViewer(ordered.map((it) => iterationItem(job, it)), 0), info);
        }
        row.append(pill);
      }
      strip.append(row);
      collapseRow(row, ".a-agentic-clip", 16, 12, (open) => { card._expanded = { ...(card._expanded || {}), [phase]: open }; });
    }
    if (outs && kind === "done" || replaced) {
      const sum = el("div", "a-agentic-cliprow is-summary");
      if (outs && kind === "done") {
        const ready = el("span", "a-agentic-outcount", job.full_output ? "▶ finished video" : `▶ ${outs} clip${outs === 1 ? "" : "s"} ready`);
        clickable(ready, () => openJobMedia(job.ref), "Watch them");
        sum.append(ready);
      }
      if (replaced) sum.append(el("span", "a-agentic-outcount is-muted", `${replaced} reroll${replaced === 1 ? "" : "s"} replaced`));
      strip.append(sum);
    }
    strip.hidden = !strip.childElementCount;
    const full = hasFull;
    // stage line + header status
    const done = [...run.phases.full.values()].filter((c) => c.state === "done").length;
    const ref = card.dataset.ref || job.ref || "";
    const meta = $(".a-term-meta", card);
    meta.textContent = ref + (ref ? " · " : "") + ({
      done: outs ? (job.full_output ? "finished" : `${outs} clip${outs === 1 ? "" : "s"}`) : "finished",
      failed: "failed", cancelled: "cancelled", paused: "paused", approval: "needs your approval",
      running: ov?.outline ? `${ov.counts.verified}/${ov.total} verified` : count && full ? `rendering ${done}/${count}` : count ? "previews" : "planning",
    })[kind];
    $(".a-agentic-stage", card).textContent = kind === "approval" ? "Staging previews are waiting for your approval in Vid"
      : kind === "running" || kind === "paused" ? String(job.stage || "") : "";
    // failure / stop callout
    const alert = $(".a-agentic-alert", card);
    const failedClip = [...run.phases.full.values(), ...run.phases.staging.values()].reverse().find((c) => c.state === "failed");
    const why = String(job.error || card._logError || (failedClip?.failures.at(-1)?.reason) || run.lastError || "");
    if (kind === "failed" || kind === "cancelled") {
      alert.hidden = false; alert.className = `a-agentic-alert is-${kind}`;
      alert.replaceChildren(el("strong", "", kind === "cancelled" ? "Cancelled" : failedClip ? `Stopped at clip ${failedClip.n}` : "The run failed"),
        el("span", "", why ? ` — ${why}` : kind === "cancelled" ? " — the unfinished renders were cancelled" : ""));
    } else if (kind === "running" && failedClip) {
      alert.hidden = false; alert.className = "a-agentic-alert is-retrying";
      alert.replaceChildren(el("strong", "", `Clip ${failedClip.n} failed`), el("span", "", ` — ${failedClip.failures.at(-1)?.reason || "retrying"}. Video retries it.`));
    } else alert.hidden = true;
  }

  /* ------------------------------------------------ thinking + markdown -- */
  const wordCount = (t) => (t.match(/\S+/g) || []).length;
  const THINK_SNAP = 48;      // px from the bottom of a live thought that counts as "back at the bottom"
  function thinkNode(text, live = false) {
    const li = el("li", "a-ev");
    li.dataset.kind = "think";
    const details = el("details", "a-think");
    const summary = el("summary");
    summary.append(el("span", "a-think-icon"), el("span", "a-think-label", live ? "Thinking" : "Thought"),
      el("span", "a-think-peek"), el("span", "a-think-meta"));
    const body = el("div", "a-think-body");
    // Live: follows the newest text. Scrolling UP holds it where you are; scrolling back DOWN to near its
    // bottom snaps it to the tail and it follows again. Finished: "Show all" lifts the cap.
    body._lastTop = 0;
    body.addEventListener("scroll", () => {
      const gap = body.scrollHeight - body.scrollTop - body.clientHeight;
      if (body.scrollTop < body._lastTop - 1) body._held = true;                    // you scrolled up
      else if (gap <= THINK_SNAP && body.scrollTop >= body._lastTop) {              // you came back down
        body._held = false;
        if (gap > 1 && li.classList.contains("is-live")) body.scrollTop = body.scrollHeight;
      }
      body._lastTop = body.scrollTop;
      body.classList.toggle("is-tail", !body._held);
    }, { passive: true });
    // A wheel up holds it even when nothing moves yet (already at its bottom, text still arriving).
    body.addEventListener("wheel", (e) => { if (e.deltaY < 0 && body.scrollHeight > body.clientHeight + 4) body._held = true; }, { passive: true });
    const more = el("button", "a-think-more", "Show all"); more.type = "button"; more.hidden = true;
    more.addEventListener("click", () => {
      const full = body.classList.toggle("is-full"); body.style.height = ""; more.textContent = full ? "Show less" : "Show all";
      if (full) { body._held = true; body.classList.remove("is-tail"); }       // expanded: stop pulling it to the tail
      else if (li.classList.contains("is-live")) { body._held = false; body.scrollTop = body.scrollHeight; }
    });
    details.addEventListener("toggle", () => { if (details.open && !li.classList.contains("is-live")) more.hidden = !body.classList.contains("is-full") && body.scrollHeight <= body.clientHeight + 4; });
    details.append(summary, body, more);
    li.append(details);
    // Short thoughts stay open; long ones fold to a one-line peek. Live thinking is always open.
    details.open = live || text.length <= 280;
    if (text) updateThink(li, text, live);
    return li;
  }
  // The finished thought replaces the live one in place. If you were reading it (scrolled up, expanded,
  // or resized it), it stays open where you were instead of folding to a one-line peek.
  function adoptLiveThink(liveLi, li) {
    const was = $(".a-think-body", liveLi), body = $(".a-think-body", li), details = $("details", li);
    const reading = was._held || was.classList.contains("is-full") || !!was.style.height;
    if (reading) {
      details.open = true;
      if (was.classList.contains("is-full")) { body.classList.add("is-full"); $(".a-think-more", li).textContent = "Show less"; }
      if (was.style.height) body.style.height = was.style.height;
    }
    const top = was.scrollTop;
    liveLi.replaceWith(li);
    if (reading) requestAnimationFrame(() => { body.scrollTop = top; const more = $(".a-think-more", li); more.hidden = !body.classList.contains("is-full") && body.scrollHeight <= body.clientHeight + 4; });
  }
  function updateThink(li, text, live) {
    const body = $(".a-think-body", li);
    body.textContent = text;
    $(".a-think-peek", li).textContent = text.replace(/\s+/g, " ").trim().slice(0, 160);
    const n = wordCount(text);
    $(".a-think-meta", li).textContent = `${n.toLocaleString()} word${n === 1 ? "" : "s"}`;
    if (live && !body._held) { body.classList.add("is-tail"); body.scrollTop = body.scrollHeight; }
    const more = $(".a-think-more", li);
    // Live too: once it overflows its box, "Show all" opens the whole thought (it streams on below).
    if (more) requestAnimationFrame(() => { more.hidden = !body.classList.contains("is-full") && body.scrollHeight <= body.clientHeight + 4; });
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
      const label = b.querySelector(".a-copy-all-label");
      b.innerHTML = ok ? COPIED_ICON : COPY_ICON; if (label) b.append(label); b.classList.toggle("is-copied", ok);
      b.title = ok ? "Copied" : "Couldn't copy"; if (!ok) msg("Couldn't copy to the clipboard");
      clearTimeout(b._reset); b._reset = setTimeout(() => { b.innerHTML = COPY_ICON; if (label) b.append(label); b.classList.remove("is-copied"); b.title = `Copy ${what}`; }, 1400);
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
  const H3_TELLS = /^[ \t]*(?:\*\*)?::VIDEO\b|\{\{CONTINUATION_FRAME\}\}|\b(integrated_multimodal_description|overall_soundscape|non_diegetic_music|subject_definitions|retention_analysis|detailed_description)\s*:|<(Subject|Picture|Video|Audio) \d+>|\[Shot \d+\]|<d>\s*\[/im;
  const H3_TASK_TYPES = ["keyframe completion", "reference generation", "video editing", "video continuation", "audio reuse", "audio reference"];
  const H3_RETENTION_MARKERS = ["fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference", "fully_copy", "partially_copy"];
  const H3_SYNTAX = new RegExp([
    // Extended-video plans (Video's Agentic ::VIDEO / ::END blocks) and their continuation placeholders
    String.raw`(?<planHead>^(?:[ \t]+|\*\*)?::VIDEO\b[^\n]*)`,
    String.raw`(?<planEnd>^(?:[ \t]+|\*\*)?::END\b[^\n]*)`,
    String.raw`(?<planBad>^[ \t]*(?:\*\*)?::(?!VIDEO\b|END\b)[^\n]*)`,
    String.raw`(?<placeholder>\{\{CONTINUATION_(?:FRAME|VIDEO)\}\})`,
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
  // A ::VIDEO header: its fields, NEW/EXTEND, clip ids, and refs=/exclude= aliases in their reference colours.
  // Problems Video's plan validator (backend/agentic.py) rejects are underlined, with the reason on hover.
  const PLAN_ALIAS = /^(image|video|audio)\d+$/i;
  const PLAN_FIELDS = new Set(["id", "action", "mode", "duration", "from", "frame", "refs", "exclude", "first", "last", "chars", "scene", "lora"]);
  const PLAN_SLUG = /^[a-z][a-z0-9_-]{0,31}$/;
  function planBad(span, value, why) { const b = span("plan-bad", value); b.title = why; return b; }
  function planProblem(key, value, fields, ctx) {
    const action = String(fields.action || "").toUpperCase(), id = Number(fields.id);
    switch (key) {
      case "id": return value !== String(ctx.next) ? `Clip ids run 1, 2, 3 … in plan order: this one should be id=${ctx.next}` : "";
      case "action": return /^(new|extend)$/i.test(value) ? "" : "action is NEW or EXTEND";
      case "mode": return /^(text|reference|keyframe)$/i.test(value) ? "" : "mode is text, reference or keyframe";
      case "duration": {
        const n = Number(value.replace(/s$/i, ""));
        return !Number.isFinite(n) || !/^\d+(\.\d+)?s?$/i.test(value) ? "duration is a number of seconds, e.g. duration=10"
          : n > 15 || n < 2 ? "One clip is 5–15 seconds (Wan 2–15)" : "";
      }
      case "from": {
        if (action !== "EXTEND") return "from= is only for EXTEND clips";
        if (/^session:[1-9]\d*$/.test(value)) return "";
        if (!/^\d+$/.test(value)) return "from= is an earlier clip id, 0, or session:<number>";
        return Number.isFinite(id) && Number(value) >= id ? "from= must name an EARLIER clip" : "";
      }
      case "frame": return value === "previous" ? (action === "NEW" ? "NEW clips can't use frame=previous (that's EXTEND)" : "") : "frame is previous (EXTEND) or -";
      case "first": return value === "First" ? (action === "EXTEND" ? "EXTEND clips can't use first=" : "") : "first is First or -";
      case "last": return value === "Last" ? "" : "last is Last or -";
      case "chars": { const n = value.toLowerCase().split(","); return n.length > 3 || new Set(n).size !== n.length || !n.every((x) => PLAN_SLUG.test(x)) ? "chars= takes up to 3 lowercase slugs (a-z0-9_-)" : ""; }
      case "scene": return PLAN_SLUG.test(value.toLowerCase()) ? "" : "scene= is one lowercase slug (a-z0-9_-, 32 max)";
      case "lora": return /^\d+$/.test(value) && Number(value) >= 1 ? "" : "lora= is a gallery number or -";
      case "refs": return value.split(",").includes("inherit") && action !== "EXTEND" ? "refs=inherit only works on EXTEND clips" : "";
      default: return "";
    }
  }
  function planValue(key, value, span, why) {
    if (value === "-") return [span("plan-none", value)];
    if (why) return [planBad(span, value, why)];
    if (key === "action") return [span(/^extend$/i.test(value) ? "plan-extend" : "plan-new", value)];
    if (key === "id" || key === "from") return [span("plan-id", value)];
    if (key === "refs" || key === "exclude") {
      return value.split(/(,)/).filter(Boolean).map((part) => part === "," ? span("plan-eq", part)
        : part === "inherit" ? span("plan-keyword", part)
        : PLAN_ALIAS.test(part) ? span("ref", part, h3Category(part.replace(/^image/i, "picture"))) : span("plan-value", part));
    }
    return [span("plan-value", value)];
  }
  function planHeader(line, span, ctx) {
    const head = span("plan-head", "");
    const at = line.indexOf("::VIDEO");
    if (at > 0) head.append(planBad(span, line.slice(0, at), "::VIDEO must start the line, with no indent or ** around it"));
    const tail = (line.match(/\*\*\s*$/) || [""])[0];
    const parts = line.slice(at + 7, line.length - tail.length).split(/(\s+)/).filter(Boolean);
    const fields = {}, seen = new Set();
    for (const part of parts) { const f = part.match(/^([a-z]+)=(.+)$/i); if (f && !(f[1].toLowerCase() in fields)) fields[f[1].toLowerCase()] = f[2]; }
    const action = String(fields.action || "").toUpperCase();
    const missing = ["id", "action", "duration", action === "EXTEND" ? "from" : "mode"].filter((k) => !fields[k]);
    head.append(missing.length ? planBad(span, "::VIDEO", `Missing ${missing.map((k) => k + "=").join(", ")}`) : span("plan", "::VIDEO"));
    for (const part of parts) {
      const field = part.match(/^([a-z]+)=(.+)$/i);
      if (!field) { head.append(/^\s+$/.test(part) ? part : planBad(span, part, "Not a key=value field: the whole header is one line of key=value fields with no spaces in values")); continue; }
      const key = field[1].toLowerCase();
      const why = !PLAN_FIELDS.has(key) ? `Unknown field ${key}= (the validator rejects the plan)`
        : seen.has(key) ? `${key}= appears twice in this header` : field[2] === "-" ? "" : planProblem(key, field[2], fields, ctx);
      seen.add(key);
      head.append(PLAN_FIELDS.has(key) ? span("plan-key", field[1]) : planBad(span, field[1], why), span("plan-eq", "="), ...planValue(key, field[2], span, PLAN_FIELDS.has(key) ? why : ""));
    }
    if (tail) head.append(planBad(span, tail, "::VIDEO must start the line, with no indent or ** around it"));
    ctx.next = (/^\d+$/.test(fields.id || "") ? Number(fields.id) : ctx.next) + 1;
    return head;
  }
  function highlightH3(code, text) {          // DOM-built spans, like the rest of renderMd
    const span = (cls, value, category) => { const s = el("span", `hl-${cls}`, value); if (category) s.dataset.category = category; return s; };
    let last = 0;
    const plan = { next: 1 };
    for (const match of text.matchAll(H3_SYNTAX)) {
      if (!match[0]) { H3_SYNTAX.lastIndex++; continue; }
      code.append(text.slice(last, match.index));
      last = match.index + match[0].length;
      const g = match.groups;
      if (g.planHead) code.append(planHeader(g.planHead, span, plan));
      else if (g.planEnd) {
        const tidy = /^::END\s*$/.test(g.planEnd);
        code.append(tidy ? span("plan", g.planEnd) : planBad(span, g.planEnd, "::END goes on a line of its own, with no indent or ** around it"));
      } else if (g.planBad) code.append(planBad(span, g.planBad, "Not plan syntax: a clip starts with a ::VIDEO id=… header line and closes with ::END"));
      else if (g.placeholder) {
        const p = span("placeholder", g.placeholder);
        if (g.placeholder.includes("VIDEO")) { p.classList.add("is-invalid"); p.title = "Not allowed: EXTEND attaches only the source clip's final frame. Use {{CONTINUATION_FRAME}}."; }
        else p.title = "Replaced at render time with the source clip's final frame (<Picture N>)";
        code.append(p);
      } else if (g.dialogue) {
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

  // Other models' prompt syntax, from shared/guides: music lyrics, Fish voice cues, Anima tags, Qwen Image refs.
  // Krea 2, Qwen3 and IndexTTS2 prompts are plain language with nothing to detect.
  const H3_FIELD = /^\s*(?:#{1,6}\s+)?(?:\*\*|__)?(integrated_multimodal_description|subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)(?:\*\*|__)?\s*:(?:\*\*|__)?[ \t]*(.*)$/;
  const LYRIC_SECTIONS = "Intro|Verse|Pre[- ]?Chorus|Chorus|Post[- ]?Chorus|Bridge|Hook|Refrain|Interlude|Break|Breakdown|Build(?:[- ]?up)?|Drop|Instrumental|Solo|Outro|Coda|Ending|Rap|Spoken(?: Word)?";
  // A section tag on its own line, optionally qualified ([Final Chorus], [Verse 2 - whispered]) and
  // followed by a performance cue in parentheses: [Bridge] (drums out, pads only).
  const LYRIC_TAG = new RegExp(String.raw`^\s*(?:\*\*)?\[(?:[A-Za-z]+[ -])?(?:${LYRIC_SECTIONS})\b[^\]\n]{0,40}\](?:\*\*)?(?:[ \t]*\([^)\n]*\))?\s*$`, "i");
  const VOICE_CUE = /\[[a-z][a-z ,'-]{1,30}\]/g;
  const MODEL_LABELS = { h3: "h3", minimax: "h3", "minimax-h3": "h3", hailuo: "h3", plan: "h3", "h3-plan": "h3", "video-plan": "h3",
    video: "h3", extended: "h3", wan: "h3", lyrics: "lyrics", lyric: "lyrics", song: "lyrics", music: "lyrics", yue: "lyrics", yue2: "lyrics",
    "minimax-music": "lyrics", minimax_music3: "lyrics", voice: "voice script", fish: "voice script", anima: "anima", qwen: "qwen image" };
  const MODEL_RULES = {
    lyrics: [[/^[ \t]*\[[^\]\n]+\](?=[ \t]*(?:\([^)\n]*\))?[ \t]*$)/, "task"], [/(?<=^[ \t]*\[[^\]\n]+\][ \t]*)\([^)\n]*\)/, "camera"]],
    "voice script": [[/\[[^\]\n]+\]/, "camera"]],
    anima: [[/\b(?:masterpiece|best quality|high quality|low quality|worst quality|score_\d|safe|sensitive|nsfw|explicit|highres|absurdres)\b/, "key"], [/@[^,\n]+/, "ref"], [/\([^()\n]+:\d+(?:\.\d+)?\)/, "time"]],
    "qwen image": [[/<image\d+>/, "ref"], [/“[^”\n]*”|"[^"\n]*"/, "speech"]],
  };
  function highlightModel(code, text, kind) {
    const rules = MODEL_RULES[kind];
    const re = new RegExp(rules.map(([r]) => `(${r.source})`).join("|"), "gm");
    let last = 0;
    for (const match of text.matchAll(re)) {
      code.append(text.slice(last, match.index));
      last = match.index + match[0].length;
      code.append(el("span", `hl-${rules[match.slice(1).findIndex(Boolean)][1]}`, match[0]));
    }
    code.append(text.slice(last));
  }
  const isLyrics = (lines) => lines.filter((l) => LYRIC_TAG.test(l)).length;
  // Which model a code block's text is written for. `loose` = the fence named a format an agent may have
  // picked by accident (```yaml, ```markdown): only an unmistakable prompt overrides it.
  function modelSyntax(source, loose) {
    const lines = source.split("\n");
    const tags = isLyrics(lines);
    if (loose) return lines.some((l) => (H3_FIELD.test(l) && !/^\s*summary/.test(l)) || PLAN_LINE.test(l)) ? "h3" : tags >= 2 ? "lyrics" : null;
    if (H3_TELLS.test(source)) return "h3";
    if (tags && !lines.some((l) => /^\s*[\w.-]+\s*=/.test(l))) return "lyrics";
    if (detectLang(source)) return null;
    if (/<image\d+>/.test(source)) return "qwen image";
    if ((source.match(/,/g) || []).length >= 3 && /\b(?:masterpiece|best quality|score_\d|\d+(?:girl|boy)s?)\b/.test(source)) return "anima";
    if ((source.match(VOICE_CUE) || []).length >= 2) return "voice script";
    return null;
  }
  // A prompt the agent wrote straight into its message, without a code block. Only whole-prompt shapes
  // count (two or more H3 fields, two or more lyric sections), so prose that mentions <Picture 1> stays prose.
  function bareH3(lines, start) {
    let i = start;
    const out = [];
    if (/^\s*For the target video\b/.test(lines[i])) {
      out.push(lines[i].trim(), "");
      i++;
      while (i < lines.length && !lines[i].trim()) i++;
    }
    const first = (lines[i] || "").match(H3_FIELD);
    if (!first) return null;
    // Usually it opens on the first section; a prompt pasted without its subject_definitions: label
    // opens later, and then needs three sections so prose with a "summary:" line stays prose.
    const opener = ["integrated_multimodal_description", "subject_definitions"].includes(first[1]);
    if (!opener && first[1] === "non_diegetic_music") return null;
    const seen = new Set();
    let end = i, empty = false;
    for (; i < lines.length; i++) {
      const line = lines[i], f = line.match(H3_FIELD);
      if (/^\s*(```|~~~)/.test(line)) break;
      if (f) {
        seen.add(f[1]); out.push(`${f[1]}: ${f[2]}`.trimEnd()); end = i; empty = !f[2].trim();
        if (f[1] === "non_diegetic_music" && !empty) break;
      } else if (line.trim()) { out.push(line); end = i; empty = false; }
      else {
        let k = i + 1;
        while (k < lines.length && !lines[k].trim()) k++;
        if (k >= lines.length) break;
        // Paragraphs of a section (detailed_description often has several) belong to the prompt
        // while another section label is still to come before a fence.
        const later = () => { for (let n = k; n < lines.length && !/^\s*(```|~~~)/.test(lines[n]); n++) if (H3_FIELD.test(lines[n])) return true; return false; };
        if (!empty && !H3_FIELD.test(lines[k]) && !/^\s*(<(?:Subject|Picture|Video|Audio) \d+>|\[Shot \d+\])/.test(lines[k]) && !later()) break;
        out.push("");
      }
    }
    if (seen.size < (opener ? 2 : 3)) return null;
    while (out.at(-1) === "") out.pop();
    return { source: out.join("\n"), named: "h3", end };
  }
  // A ::VIDEO plan written straight into a message: from its first header through the last ::END
  // (or to the end of the message while it's still streaming). Markdown bold or an indent around a
  // ::VIDEO / ::END line is dropped, so the copied plan pastes straight into Video.
  const PLAN_LINE = /^[ \t]*(?:\*\*)?::(VIDEO|END)\b/;
  const tidyPlanLine = (line) => PLAN_LINE.test(line) ? line.trim().replace(/^\*\*|\*\*$/g, "").trim() : line;
  function barePlan(lines, start) {
    if ((lines[start].match(PLAN_LINE) || [])[1] !== "VIDEO") return null;
    let end = -1, inBlock = false, i = start;
    for (; i < lines.length; i++) {
      const kind = (lines[i].match(PLAN_LINE) || [])[1];
      if (/^\s*(```|~~~)/.test(lines[i])) break;
      if (kind === "VIDEO") inBlock = true;
      else if (kind === "END") { inBlock = false; end = i; }
      else if (!inBlock && lines[i].trim()) break;      // prose after the plan
    }
    if (inBlock) { end = i - 1; while (end > start && !lines[end].trim()) end--; }   // unclosed: still being written
    return end < 0 ? null : { source: lines.slice(start, end + 1).map(tidyPlanLine).join("\n"), named: "h3", end };
  }
  function bareLyrics(lines, start) {
    if (!LYRIC_TAG.test(lines[start])) return null;
    const out = [];
    let end = start;
    for (let i = start; i < lines.length; i++) {
      if (lines[i].trim()) {
        if (/^\s*(```|~~~)/.test(lines[i])) break;
        out.push(LYRIC_TAG.test(lines[i]) ? lines[i].replace(/\*\*/g, "").trim() : lines[i]); end = i;
        continue;
      }
      let k = i + 1;
      while (k < lines.length && !lines[k].trim()) k++;
      if (k >= lines.length || !LYRIC_TAG.test(lines[k])) break;
      out.push("");
    }
    return isLyrics(out) >= 2 ? { source: out.join("\n"), named: "lyrics", end } : null;
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
  const LOOSE_LANGS = new Set(["md", "markdown", "yaml", "yml", "xml", "html"]);
  function codeBlock(source, named) {
    const pre = el("pre", "a-md-code"), code = el("code");
    const want = (named || "").toLowerCase();
    // A model prompt, named on the fence or detected: its own colours, wrapped.
    const kind = MODEL_LABELS[want] || (PLAIN_LANGS.has(want) ? modelSyntax(source, false) : LOOSE_LANGS.has(want) ? modelSyntax(source, true) : null);
    if (kind) {
      if (kind === "h3") highlightH3(code, source); else highlightModel(code, source, kind);
      pre.classList.add("is-plain", "is-model"); pre.dataset.model = kind; pre.append(code);
      return { pre, label: kind };
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

  function codeBox(source, named) {
    const { pre, label } = codeBlock(source, named);
    const box = el("div", "a-md-codebox"), head = el("div", "a-md-codehead");
    const tools = el("span", "a-md-codetools");
    tools.append(copyButton(() => source, named ? `${named} code` : "code", "a-copy-code"), fullButton(box));
    head.append(el("span", "a-md-codelang", label), tools);
    box.append(head, pre);
    if (pre.classList.contains("is-model")) box._prompt = { source, kind: pre.dataset.model };   // for "Copy all prompts"
    return box;
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
        root.append(codeBox(buf.join("\n"), fence[2]));
        continue;
      }
      const bare = barePlan(lines, i) || bareH3(lines, i) || bareLyrics(lines, i);
      if (bare) { close(); root.append(codeBox(bare.source, bare.named)); i = bare.end; continue; }
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
    // Two or more detected prompts (H3 prompts, ::VIDEO plan parts, lyrics…): one button copies them all,
    // in order, a blank line apart (valid between ::VIDEO blocks too, so split plan parts paste as one plan).
    const prompts = [...root.children].map((c) => c._prompt).filter(Boolean);
    if (prompts.length >= 2) {
      const kinds = [...new Set(prompts.map((x) => x.kind))];
      const what = `all ${prompts.length} ${kinds.length === 1 ? kinds[0] + " " : ""}prompts`;
      const bar = el("div", "a-md-copyall");
      const b = copyButton(() => prompts.map((x) => x.source.trim()).join("\n\n"), what, "a-copy-all");
      b.append(el("span", "a-copy-all-label", `Copy ${what}`));
      bar.append(b); root.append(bar);
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
    if (job.kind === "agentic") {   // Pause frees the GPU now; Resume reloads the same Vid session (2026-11-04)
      const pause = el("button", "a-pause", "Pause"); pause.type = "button"; links.append(pause);
      pause.addEventListener("click", async () => {
        const resume = pause.dataset.action === "resume";
        pause.disabled = true; pause.textContent = resume ? "Resuming…" : "Pausing…";
        try { await api(`/sessions/${current}/jobs/${job.ref}/${resume ? "resume" : "pause"}`, { method: "POST" }); }
        catch (e) { msg(e.message); }
        finally { pause.disabled = false; }
      });
    }
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
    const prompts = el("div", "a-job-prompts"); prompts.hidden = true;
    card.append(media(job), final, review, prompts, body);
    return card;
  }

  // Vid's prompt approval (its Hermes setting "Approve the clip prompts before rendering"): nothing renders until you
  // approve, reject with a reason, or flag clips with what to change. Rebuilt only when the prompts change.
  function renderPromptApproval(box, job) {
    const waiting = job.kind === "agentic" && job.state === "awaiting_approval" && job.awaiting === "prompts" && (job.prompts || []).length;
    box.hidden = !waiting;
    if (!waiting) { box.replaceChildren(); box.dataset.key = ""; return; }
    const offset = Number(job.prompt_offset || 0);
    const key = job.prompts.map((x) => `${x.id}:${x.prompt.length}:${x.prompt.slice(-30)}`).join("|");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    const head = el("div", "a-prompts-head");
    head.append(el("strong", "", `Approve ${job.prompts.length} clip prompt${job.prompts.length === 1 ? "" : "s"} before rendering`),
      el("span", "a-prompts-hint", "Approve, reject with a reason, or flag the clips to change"));
    const list = el("div", "a-prompts-list");
    for (const clip of job.prompts) {
      const n = Number(clip.id) + offset;
      const item = el("details", "a-prompts-clip"); item.dataset.clip = clip.id;
      const summary = el("summary");
      const flag = el("label", "a-prompts-flag"); const check = el("input"); check.type = "checkbox"; flag.append(check, " Flag");
      flag.addEventListener("click", (e) => e.stopPropagation());
      const kind = ({ text: "text", ref2v: "reference", fl2v: "keyframe" })[clip.mode] || clip.mode || "";
      summary.append(el("span", "a-think-icon"), el("b", "", `Clip ${n}`),
        el("span", "a-prompts-meta", `${clip.duration ?? "?"} s · ${clip.action || "NEW"}${clip.action === "EXTEND" && clip.source != null ? ` from ${Number(clip.source) ? Number(clip.source) + offset : "the previous batch"}` : ""} · ${kind}`), flag);
      const note = el("textarea", "a-prompts-note"); note.rows = 2; note.maxLength = 2000; note.hidden = true; note.placeholder = `What should change in clip ${n}?`;
      check.addEventListener("change", () => { note.hidden = !check.checked; item.classList.toggle("is-flagged", check.checked); if (check.checked) { item.open = true; note.focus(); } sync(); });
      note.addEventListener("input", sync);
      item.append(summary, note, codeBox(clip.prompt, "h3"));
      list.append(item);
    }
    const reason = el("textarea", "a-prompts-reason"); reason.rows = 2; reason.maxLength = 4000;
    reason.placeholder = "Reason (needed to reject; optional notes when approving or flagging)";
    reason.addEventListener("input", sync);
    const actions = el("div", "a-prompts-actions");
    const approve = el("button", "a-prompts-btn is-approve", "✓ Approve and render");
    const change = el("button", "a-prompts-btn is-flag", "Change flagged clips");
    const reject = el("button", "a-prompts-btn is-reject", "✕ Reject");
    for (const b of [approve, change, reject]) b.type = "button";
    actions.append(approve, change, reject);
    const flagged = () => Object.fromEntries([...list.children].filter((x) => $("input", x).checked).map((x) => [x.dataset.clip, $(".a-prompts-note", x).value.trim()]));
    function sync() {
      const f = flagged(), count = Object.keys(f).length;
      change.disabled = !count || Object.values(f).some((v) => !v);
      change.textContent = count ? `Change ${count} flagged clip${count === 1 ? "" : "s"}` : "Change flagged clips";
      change.title = !count ? "Tick Flag on a clip first" : change.disabled ? "Say what to change in each flagged clip" : "Only these prompts are rewritten, then you're asked again";
      reject.disabled = !reason.value.trim();
      reject.title = reject.disabled ? "Write the reason first" : "Hermes revises the whole plan from your reason, then asks again";
    }
    async function send(action) {
      [approve, change, reject].forEach((b) => { b.disabled = true; });
      try {
        await api(`/sessions/${current}/jobs/${job.ref}/prompt-decision`, { method: "POST",
          body: JSON.stringify({ action, notes: reason.value.trim(), clip_notes: action === "flag" ? flagged() : {} }) });
        box.hidden = true; box.dataset.key = "";
      } catch (e) { msg(e.message); approve.disabled = false; sync(); }
    }
    approve.addEventListener("click", () => send("approve"));
    change.addEventListener("click", () => send("flag"));
    reject.addEventListener("click", () => send("reject"));
    box.replaceChildren(head, list, reason, actions);
    sync();
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
    const pause = $(".a-pause", card);
    if (pause && !pause.disabled) {
      pause.hidden = done;
      pause.dataset.action = job.state === "paused" ? "resume" : "pause";
      pause.textContent = job.state === "paused" ? "Resume" : "Pause";
      pause.title = job.state === "paused" ? "Reload this same Vid session and continue; anything the pause interrupted is generated again"
        : "Pause now and free the GPU; Resume continues this same session";
    }
    const review = $(".a-job-approval", card);
    const waiting = job.kind === "agentic" && job.state === "awaiting_approval" && job.awaiting !== "prompts";
    review.hidden = !waiting;
    const promptBox = $(".a-job-prompts", card);
    if (promptBox) renderPromptApproval(promptBox, job);
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
    for (const job of jobs) if (job.kind === "agentic") agenticJob(job);
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
    renderCrumb(s);
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
    renderHead(entry.snap); renderJobs(entry.snap.jobs || []); renderBoards(entry.snap.boards || []); renderLive(entry.snap.live); renderMeters(entry.snap); renderParked(entry.snap);
  }

  /* ------------------------------------------------ open / stream -- */
  function open(id, push = true) {
    drawer(false);
    if (id === current && stream) return;
    closeStream();
    current = id || null;
    leaveProjectViews();
    $("#a-empty").hidden = !!current; $("#a-session").hidden = !current;
    updateJump();
    $("#a-log").replaceChildren(); cards.clear(); terminals.clear(); boards.clear(); hideBusy();
    $("#a-meters").hidden = true;
    if (push) history.replaceState(null, "", current ? `?s=${current}` : location.pathname);
    document.querySelectorAll(".a-sess").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === current)));
    listKey = "";
    renderRecentProjects();
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
      if (gapBelow() < 48 && host.scrollTop >= lastTop) { if (!follow) { follow = true; toBottom(); } }   // back at the bottom: snap and follow again
      else if (host.scrollTop < lastTop - 1) follow = false;    // scrolled up: stop following
      lastTop = host.scrollTop;
      updateJump();
    }, { passive: true });
    // A wheel/touch upward stops following even when nothing moves yet (already at the bottom).
    // Scrolling inside a box that can still scroll up (a live thought, a terminal, a code block) is reading
    // that box, not leaving the bottom of the chat.
    host.addEventListener("wheel", (e) => {
      if (e.deltaY >= 0) return;
      for (let n = e.target; n && n !== host; n = n.parentElement) {
        if (n.scrollHeight > n.clientHeight + 4 && n.scrollTop > 0 && /auto|scroll/.test(getComputedStyle(n).overflowY)) return;
      }
      follow = false;
    }, { passive: true });
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
  async function upload(f, update = renderFiles) {
    try {
      const type = f.file.type || "application/octet-stream";
      let res = await sendPart("/uploads", f.file.slice(0, PART, type),
        { "X-Filename": encodeURIComponent(f.name).replace(/%20/g, " "), "X-Upload-Total": String(f.size) });
      f.sent = res.received; update();
      while (!res.complete) {
        if (f.cancelled) { api(`/uploads/${res.id}`, { method: "DELETE" }).catch(() => {}); return; }
        for (let tries = 0; ; tries++) {
          try { res = await sendPart(`/uploads/${res.id}/append`, f.file.slice(f.sent, f.sent + PART, type), { "X-Upload-Offset": String(f.sent) }); break; }
          catch (err) { if (tries >= 2) throw err; await new Promise((r) => setTimeout(r, 1500 * (tries + 1))); }
        }
        f.sent = res.received; update();
      }
      if (f.cancelled) { api(`/uploads/${res.id}`, { method: "DELETE" }).catch(() => {}); return; }
      f.id = res.id; f.url = res.url;
    } catch (err) { f.error = err.message; }
    update();
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
    e.preventDefault(); dragDepth = 0; $("#a-drop").hidden = true;
    (draftProject && projectTab === "sources" ? addSources : addFiles)([...e.dataTransfer.files]);
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
      answerOpenAsks(payload.text); closeOpenOffers(payload.text);
      $("#a-log").append(bubble); followBottom();
    }
    const attachments = payload.files.map((f) => f.id);
    try {
      if (current) await api(`/sessions/${current}/message`, { method: "POST", body: JSON.stringify({ text: payload.text, attachments, on_busy: payload.on_busy || "ask", tz: TZ }) });
      else { const s = await api("/sessions", { method: "POST", body: JSON.stringify({ prompt: payload.text, attachments, tz: TZ, ...(draftProject ? { project: draftProject } : {}) }) }); remember(s, true); open(s.id); }
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

  /* ------------------------------------------------ projects -- */
  // A project groups chats and gives them shared instructions and sources. Its page has the
  // same composer as everywhere: sending from it starts a new chat inside the project.
  const FOLDER = '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/>';
  const PROJECT_ICONS = {
    folder: FOLDER,
    pen: '<path d="M4 20l1.5-6L15 4.5l4.5 4.5L10 18.5 4 20Z"/><circle cx="12.5" cy="11.5" r="1.4"/>',
    film: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M8 5v14M16 5v14M3.5 12h17"/>',
    image: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M20 16l-5-5-8 8"/>',
    music: '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    code: '<path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5"/>',
    book: '<path d="M5 19.5V5.5a2 2 0 0 1 2-2h12v14H7a2 2 0 0 0-2 2Zm0 0a2 2 0 0 0 2 2h12v-4"/>',
    star: '<path d="M12 4l2.5 5.2 5.7.8-4.1 4 1 5.7L12 17l-5.1 2.7 1-5.7-4.1-4 5.7-.8Z"/>',
  };
  const PROJECT_COLORS = ["blue", "purple", "green", "amber", "red", "slate"];
  let projectsList = [], openProject = null, projectKey = "";
  let pSelecting = false;               // the project's Chats tab is in select mode
  const pSelected = new Set();
  // A chat is busy while it runs; the session list knows best, the project snapshot may be older.
  const chatBusy = (c) => sessionOf(c.id)?.busy ?? ["starting", "thinking", "rendering", "waiting_for_gpu"].includes(c.state);
  function repaintProject() { if (openProject) { projectKey = ""; paintProject(openProject); } }
  const srcUploads = [];              // sources still uploading, or that failed
  const projectOf = (id) => projectsList.find((x) => x.id === id);
  const iconSvg = (name, size = 18) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PROJECT_ICONS[name] || FOLDER}</svg>`;
  function paintTile(node, project) { node.className = "a-tile"; node.dataset.color = project.color; node.innerHTML = iconSvg(project.icon); return node; }
  const tile = (project) => paintTile(el("span"), project);
  const longDate = (t) => new Date(t * 1000).toLocaleDateString(undefined, { month: "long", day: "numeric" });
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  function storeProject(project) {
    const { chats, ...row } = project;
    const at = projectsList.findIndex((x) => x.id === row.id);
    if (at >= 0) projectsList[at] = row; else projectsList.unshift(row);
    renderRecentProjects();
  }
  const refreshProjects = () => api("/projects").then((r) => { projectsList = r.projects; renderRecentProjects(); if (!$("#a-projects").hidden) renderProjectList(); });

  function renderRecentProjects() {
    const active = draftProject || (current && (cache.get(current)?.snap.project || sessionOf(current)?.project)) || "";
    $("#a-project-recent").replaceChildren(...projectsList.slice(0, 5).map((x) => {
      const b = el("button", "a-project-row"); b.type = "button"; b.dataset.project = x.id;
      b.setAttribute("aria-current", String(x.id === active));
      b.append(tile(x), el("span", "a-project-row-name", x.name));
      return b;
    }));
  }
  $("#a-project-recent").addEventListener("click", (e) => { const b = e.target.closest("[data-project]"); if (b) showProject(b.dataset.project); });

  function leaveProjectViews() {
    $("#a-projects").hidden = true; $("#a-project").hidden = true;
    leaveAgentViews();
    const had = draftProject;
    draftProject = ""; openProject = null; projectKey = "";
    $("#a-compose").hidden = false;
    if (had) hint();
  }
  function showProjects(push = true) {
    open(null, false);
    $("#a-empty").hidden = true; $("#a-projects").hidden = false; $("#a-compose").hidden = true;
    if (push) history.replaceState(null, "", "?projects=1");
    document.title = "Projects · Rocsy";
    renderProjectList();
    refreshProjects().catch((e) => msg(e.message));
  }
  function renderProjectList() {
    const q = $("#a-project-search").value.trim().toLowerCase();
    const rows = projectsList.filter((x) => x.name.toLowerCase().includes(q));
    $("#a-project-list").replaceChildren(...(rows.length ? rows.map((x) => {
      const b = el("button", "a-pv-row"); b.type = "button"; b.dataset.project = x.id;
      const text = el("span", "a-pv-row-text");
      text.append(el("span", "a-pv-row-title", x.name), el("span", "a-pv-row-sub", `${longDate(x.updated)} · ${plural(x.chat_count, "chat")} · ${plural(x.sources.length, "source")}`));
      b.append(tile(x), text);
      return b;
    }) : [el("p", "a-pv-empty", projectsList.length ? "No project has that name." : "No projects yet. A project keeps related chats together, with instructions and files they all share.")]));
  }
  $("#a-project-search").addEventListener("input", renderProjectList);
  $("#a-project-list").addEventListener("click", (e) => { const b = e.target.closest("[data-project]"); if (b) showProject(b.dataset.project); });
  $("#a-projects-link").addEventListener("click", () => showProjects());
  $("#a-project-back").addEventListener("click", () => showProjects());

  async function showProject(id, push = true) {
    open(null, false);
    $("#a-empty").hidden = true; $("#a-project").hidden = false;
    if (draftProject !== id) { pSelecting = false; pSelected.clear(); }
    draftProject = id; projectTab = "chats"; srcUploads.length = 0;
    if (push) history.replaceState(null, "", `?p=${id}`);
    const known = projectOf(id);
    if (known) paintProject(known);
    hint(); renderRecentProjects();
    if (narrow.matches) input.blur();
    try { paintProject(await api(`/projects/${id}`)); }
    catch (e) {
      if (e.status === 404) { projectsList = projectsList.filter((x) => x.id !== id); showProjects(); }
      msg(e.message);
    }
  }
  function paintProject(project) {
    if (draftProject !== project.id) return;
    const key = JSON.stringify(project) + projectTab + [...doomed.keys()].join() + pSelecting + [...pSelected].join();
    if (key === projectKey) return;
    projectKey = key;
    openProject = project;
    storeProject(project);
    paintTile($("#a-project-tile"), project);
    $("#a-project-name").textContent = project.name;
    document.title = `${project.name} · Rocsy`;
    $("#a-project-instr-text").textContent = project.instructions.trim() || "Add instructions";
    document.querySelectorAll("[data-ptab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.ptab === projectTab)));
    $("#a-project-chats").hidden = projectTab !== "chats";
    $("#a-project-sources").hidden = projectTab !== "sources";
    $("#a-project-access").hidden = projectTab !== "access";
    const chats = (project.chats || []).filter((c) => !doomed.has(c.id));
    for (const id of [...pSelected]) if (!chats.some((c) => c.id === id && !chatBusy(c))) pSelected.delete(id);
    $("#a-pchat-tools").hidden = projectTab !== "chats" || (!chats.length && !pSelecting);
    $("#a-pchat-select").setAttribute("aria-pressed", String(pSelecting)); $("#a-pchat-select").textContent = pSelecting ? "Done" : "Select";
    $("#a-pchat-selbar").hidden = !pSelecting;
    $("#a-pchat-count").textContent = `${pSelected.size} selected`;
    $("#a-pchat-delete").disabled = !pSelected.size;
    $("#a-pchat-delete").textContent = pSelected.size ? `Delete ${pSelected.size}` : "Delete";
    $("#a-project-chats").replaceChildren(...(chats.length ? chats.map((c) => {
      const row = el("div", "a-sess-row a-pchat-row"); row.dataset.id = c.id;
      const b = el("button", "a-pv-row a-pchat"); b.type = "button"; b.dataset.id = c.id; b.dataset.state = c.state;
      const busy = chatBusy(c);
      if (pSelecting) {
        const box = el("span", "a-check"); box.setAttribute("aria-hidden", "true");
        b.setAttribute("role", "checkbox"); b.setAttribute("aria-checked", String(pSelected.has(c.id)));
        if (busy) { b.disabled = true; b.title = "Running: stop it before deleting"; }
        row.classList.add("is-selecting"); row.append(box);
      }
      const text = el("span", "a-pv-row-text");
      text.append(el("span", "a-pv-row-title", c.title), el("span", "a-pv-row-sub", c.preview || stateText(c.state)));
      b.append(text, el("span", "a-dot"));
      row.append(b);
      if (!pSelecting && !busy) {
        const acts = el("span", "a-sess-acts");
        acts.append(iconBtn("a-sess-del a-pchat-del", `Delete “${c.title}”`, ICON.bin));
        row.append(acts);
      }
      return row;
    }) : project.chats ? [el("p", "a-pv-empty", "No chats yet. Write below to start the first one.")] : []));
    hint();
    renderSources();
  }
  function sourceType(a) {
    const ext = (a.name.split(".").pop() || "").toLowerCase();
    const named = { md: "Markdown", markdown: "Markdown", txt: "Text", pdf: "PDF", json: "JSON", csv: "CSV", html: "HTML", docx: "Word", py: "Python", js: "JavaScript" };
    if (named[ext]) return named[ext];
    return a.kind === "file" ? (a.name.includes(".") ? ext.toUpperCase() : "File") : a.kind[0].toUpperCase() + a.kind.slice(1);
  }
  // "Used for": a source with no tools is read for every request; one scoped to tools is read before
  // the agent first uses one of them in a chat.
  let sourceToolList = null, sourceToolsOpen = null;
  function sourceTools(a) {
    const box = el("details", "a-src-tools");
    box.open = sourceToolsOpen === a.id;      // stays open across the repaint a change causes
    box.addEventListener("toggle", () => { if (box.open) sourceToolsOpen = a.id; else if (sourceToolsOpen === a.id) sourceToolsOpen = null; });
    const chosen = new Set(a.tools || []);
    const labels = Object.fromEntries((sourceToolList || []).map((t) => [t.key, t.label]));
    const summary = el("summary", null, chosen.size ? [...chosen].map((k) => labels[k] || k).join(", ") : "All requests");
    summary.title = "Which tools this source is for";
    summary.setAttribute("aria-label", `${a.name} is used for: ${summary.textContent}`);
    const menu = el("div", "a-src-tools-menu");
    menu.append(el("p", "a-access-note", "Read for every request, or only before using these tools:"));
    if (!sourceToolList) {
      menu.append(el("p", "a-access-note", "Loading…"));
      api("/projects/options").then((o) => { sourceToolList = o.source_tools || []; renderSources(); }).catch((e) => msg(e.message));
    }
    for (const t of sourceToolList || []) {
      const row = el("label", "a-src-tools-row");
      const check = el("input"); check.type = "checkbox"; check.checked = chosen.has(t.key);
      check.addEventListener("change", async () => {
        check.checked ? chosen.add(t.key) : chosen.delete(t.key);
        try {
          const project = await api(`/projects/${openProject.id}/sources/${a.id}`, { method: "PATCH", body: JSON.stringify({ tools: [...chosen] }) });
          projectKey = null; paintProject(project);
        } catch (e) { msg(e.message); }
      });
      row.append(check, el("span", null, t.label));
      menu.append(row);
    }
    box.append(summary, menu);
    return box;
  }
  function renderSources() {
    const glyph = (kind) => el("span", "a-att-icon", kind === "video" ? "▶" : kind === "audio" ? "♪" : kind === "image" ? "▣" : "📄");
    const pending = srcUploads.map((f) => {
      const row = el("div", "a-source"); if (f.error) row.dataset.state = "error";
      const text = el("span", "a-pv-row-text");
      text.append(el("span", "a-pv-row-title", f.name), el("span", "a-pv-row-sub", f.error || `Uploading ${Math.floor(100 * f.sent / f.size)}% of ${kb(f.size)}`));
      row.append(glyph(f.kind), text);
      const x = el("button", "a-icon-btn"); x.type = "button"; x.innerHTML = ICON.bin; x.setAttribute("aria-label", `Stop adding ${f.name}`);
      x.addEventListener("click", () => { f.cancelled = true; srcUploads.splice(srcUploads.indexOf(f), 1); renderSources(); });
      row.append(x);
      return row;
    });
    const saved = (openProject?.sources || []).map((a) => {
      const row = el("div", "a-source");
      const text = el("span", "a-pv-row-text");
      text.append(el("span", "a-pv-row-title", a.name), el("span", "a-pv-row-sub", `${sourceType(a)} · ${kb(a.size)}`));
      const get = el("a", "a-icon-btn"); get.href = a.url; get.download = a.name; get.setAttribute("aria-label", `Download ${a.name}`); get.title = "Download";
      get.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 11l5 5 5-5M5 20h14"/></svg>';
      const del = el("button", "a-icon-btn"); del.type = "button"; del.innerHTML = ICON.bin; del.title = "Remove"; del.setAttribute("aria-label", `Remove ${a.name}`);
      del.addEventListener("click", async () => {
        if (!confirm(`Remove “${a.name}” from this project?`)) return;
        try { paintProject(await api(`/projects/${openProject.id}/sources/${a.id}`, { method: "DELETE" })); } catch (e) { msg(e.message); }
      });
      row.append(glyph(a.kind), text, sourceTools(a), get, del);
      return row;
    });
    $("#a-source-list").replaceChildren(...pending, ...saved);
    if (!pending.length && !saved.length) $("#a-source-list").append(el("p", "a-pv-empty", "No sources yet."));
  }
  function addSources(list) {
    const pid = draftProject; if (!pid) return;
    for (const file of list) {
      const f = { key: Math.random().toString(36).slice(2), file, name: file.name || "source", size: file.size, sent: 0, kind: kindOf(file.type || "") };
      srcUploads.push(f);
      upload(f, renderSources).then(async () => {
        if (f.cancelled || f.error) return;
        try {
          const project = await api(`/projects/${pid}/sources`, { method: "POST", body: JSON.stringify({ attachments: [f.id] }) });
          srcUploads.splice(srcUploads.indexOf(f), 1);
          paintProject(project); renderSources();
        } catch (e) { f.error = e.message; renderSources(); }
      });
    }
    renderSources();
  }
  document.querySelectorAll("[data-ptab]").forEach((b) => b.addEventListener("click", () => {
    projectTab = b.dataset.ptab; projectKey = "";
    if (openProject) paintProject(openProject);
    if (projectTab === "access") renderAccess();
  }));
  $("#a-project-chats").addEventListener("click", (e) => {
    const row = e.target.closest(".a-pchat-row"); if (!row) return;
    const id = row.dataset.id, chat = (openProject?.chats || []).find((c) => c.id === id);
    if (e.target.closest(".a-pchat-del")) return removeSessions([id]);
    if (pSelecting) {
      if (!chat || chatBusy(chat)) return;
      pSelected.has(id) ? pSelected.delete(id) : pSelected.add(id);
      return repaintProject();
    }
    open(id);
  });
  $("#a-project-chats").addEventListener("keydown", (e) => {
    const b = e.target.closest(".a-pchat"); if (!b || pSelecting) return;
    const chat = (openProject?.chats || []).find((c) => c.id === b.dataset.id);
    if ((e.key === "Delete" || e.key === "Backspace") && chat && !chatBusy(chat)) { e.preventDefault(); removeSessions([chat.id]); }
  });
  function setProjectSelecting(on) { pSelecting = on; pSelected.clear(); repaintProject(); }
  $("#a-pchat-select").addEventListener("click", () => setProjectSelecting(!pSelecting));
  $("#a-pchat-all").addEventListener("click", () => {
    (openProject?.chats || []).filter((c) => !chatBusy(c) && !doomed.has(c.id)).forEach((c) => pSelected.add(c.id));
    repaintProject();
  });
  $("#a-pchat-delete").addEventListener("click", () => { const ids = [...pSelected]; setProjectSelecting(false); removeSessions(ids); });
  $("#a-source-files").addEventListener("click", () => $("#a-source-input").click());
  $("#a-source-input").addEventListener("change", (e) => { addSources([...e.target.files]); e.target.value = ""; });
  $("#a-source-text").addEventListener("click", () => { $("#a-text-form").reset(); $("#a-text-dialog").showModal(); });
  $("#a-td-cancel").addEventListener("click", () => $("#a-text-dialog").close());
  $("#a-text-form").addEventListener("submit", () => {
    const body = $("#a-td-text").value; if (!body.trim()) return;
    const name = $("#a-td-name").value.trim();
    addSources([new File([body], /\.\w{1,8}$/.test(name) ? name : `${name}.md`, { type: "text/markdown" })]);
  });
  // A project's chats change while its page is open: keep the list current.
  setInterval(() => {
    if (draftProject && !document.hidden) api(`/projects/${draftProject}`).then(paintProject).catch(() => {});
  }, 5000);

  /* create and edit */
  let editing = null, pickIcon = "folder", pickColor = "blue";
  function renderPicks() {
    const pick = (host, items, chosen, make, onPick) => host.replaceChildren(...items.map((item) => {
      const b = el("button", "a-pick-opt"); b.type = "button"; b.setAttribute("role", "radio"); b.setAttribute("aria-checked", String(item === chosen)); b.setAttribute("aria-label", item);
      make(b, item);
      b.addEventListener("click", () => { onPick(item); renderPicks(); });
      return b;
    }));
    pick($("#a-pd-icons"), Object.keys(PROJECT_ICONS), pickIcon, (b, x) => { b.innerHTML = iconSvg(x); }, (x) => { pickIcon = x; });
    pick($("#a-pd-colors"), PROJECT_COLORS, pickColor, (b, x) => { const dot = el("span", "a-tile a-pick-color"); dot.dataset.color = x; b.append(dot); }, (x) => { pickColor = x; });
  }
  function editProject(project) {
    editing = project;
    $("#a-pd-title").textContent = project ? "Edit project" : "New project";
    $("#a-pd-name").value = project?.name || "";
    pickIcon = project?.icon || "folder"; pickColor = project?.color || "blue";
    $("#a-pd-error").hidden = true;
    renderPicks();
    $("#a-project-dialog").showModal(); $("#a-pd-name").focus();
  }
  $("#a-project-new").addEventListener("click", () => editProject(null));
  $("#a-project-new-side").addEventListener("click", () => { drawer(false); editProject(null); });
  $("#a-project-edit").addEventListener("click", () => editProject(openProject));
  $("#a-pd-cancel").addEventListener("click", () => $("#a-project-dialog").close());
  $("#a-project-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = JSON.stringify({ name: $("#a-pd-name").value, icon: pickIcon, color: pickColor });
    const save = $("#a-pd-save"); save.disabled = true;
    try {
      const project = await api(editing ? `/projects/${editing.id}` : "/projects", { method: editing ? "PATCH" : "POST", body });
      storeProject(project);
      $("#a-project-dialog").close();
      if (editing) paintProject(project); else showProject(project.id);
    } catch (err) { $("#a-pd-error").textContent = err.message; $("#a-pd-error").hidden = false; }
    finally { save.disabled = false; }
  });
  $("#a-project-delete").addEventListener("click", async () => {
    const project = openProject; if (!project) return;
    const n = project.chats?.length || 0, s = project.sources.length;
    if (!confirm(`Delete “${project.name}”?\n\n${s ? `Its ${plural(s, "source")} will be deleted. ` : ""}${n ? `Its ${plural(n, "chat")} will be kept and go back to your sessions.` : ""}`)) return;
    try {
      await api(`/projects/${project.id}`, { method: "DELETE" });
      projectsList = projectsList.filter((x) => x.id !== project.id);
      cache.forEach((entry) => { if (entry.snap?.project === project.id) entry.snap.project = ""; });
      api("/sessions").then((r) => renderList(r.sessions)).catch(() => {});
      showProjects();
    } catch (err) { msg(err.message); }
  });

  /* instructions */
  $("#a-project-instr").addEventListener("click", () => {
    $("#a-in-text").value = openProject?.instructions || ""; $("#a-in-error").hidden = true;
    $("#a-instr-dialog").showModal(); $("#a-in-text").focus();
  });
  $("#a-in-back").addEventListener("click", () => $("#a-instr-dialog").close());
  $("#a-in-save").addEventListener("click", async () => {
    try {
      paintProject(await api(`/projects/${openProject.id}`, { method: "PATCH", body: JSON.stringify({ instructions: $("#a-in-text").value }) }));
      $("#a-instr-dialog").close();
    } catch (err) { $("#a-in-error").textContent = err.message; $("#a-in-error").hidden = false; }
  });

  /* model and access */
  let accessOptions = null;
  async function saveProject(change) {
    try { paintProject(await api(`/projects/${openProject.id}`, { method: "PATCH", body: JSON.stringify(change) })); }
    catch (err) { msg(err.message); renderAccess(); }
  }
  function accessRow(label, on, enabled, note, onChange) {
    const row = el("label", "a-access-row");
    const box = el("input"); box.type = "checkbox"; box.checked = on && enabled; box.disabled = !enabled;
    box.addEventListener("change", () => onChange(box.checked));
    const words = el("span", "a-pv-row-text");
    words.append(el("span", "a-pv-row-title", label));
    if (note) words.append(el("span", "a-access-note", note));
    row.append(words, box);
    return row;
  }
  async function renderAccess() {
    const pid = openProject?.id; if (!pid) return;
    try {
      accessOptions = accessOptions || await Promise.all([api("/projects/options"), api("/options")]);
    } catch (err) { msg(err.message); return; }
    if (openProject?.id !== pid || projectTab !== "access") return;
    const [opts, models] = accessOptions, project = openProject;
    const access = project.access || {}, chosen = project.model || {};
    const select = $("#a-pa-model");
    const add = (host, value, label) => { const o = el("option", null, label); o.value = value; host.append(o); return o; };
    select.replaceChildren(); add(select, "", "Default (Settings → Agentic)");
    const known = new Set([""]);
    for (const pr of models.hermes?.providers || []) {
      const group = el("optgroup"); group.label = pr.name;
      for (const m of pr.models) { const v = JSON.stringify([pr.slug, m]); known.add(v); add(group, v, m); }
      select.append(group);
    }
    for (const cli of models.clis || []) {
      const group = el("optgroup"); group.label = `${cli.name} (coding CLI)`;
      for (const m of cli.models) { const v = JSON.stringify([`cli:${cli.id}`, m.id]); known.add(v); add(group, v, m.label); }
      select.append(group);
    }
    const value = chosen.model ? JSON.stringify([chosen.provider || "", chosen.model]) : "";
    if (!known.has(value)) add(select, value, chosen.model);
    select.value = value;
    const think = $("#a-pa-think");
    think.replaceChildren(...(models.reasoning || []).map((r) => { const o = el("option", null, r === "default" ? "Default (Settings → Agentic)" : r === "xhigh" ? "Extra high" : r[0].toUpperCase() + r.slice(1)); o.value = r; return o; }));
    think.value = chosen.reasoning || "default";
    const offInSettings = "Off in Settings → Agentic";
    const change = (patch) => saveProject({ access: { ...(openProject.access || {}), ...patch } });
    $("#a-pa-services").replaceChildren(...opts.services.map((x) => accessRow(x.label, access.services?.[x.key] !== false, x.on, x.on ? "" : offInSettings, (on) => {
      const services = { ...(openProject.access?.services || {}) };
      if (on) delete services[x.key]; else services[x.key] = false;
      change({ services });
    })));
    $("#a-pa-agents").replaceChildren(
      accessRow("Subagents", access.subagents !== false, opts.subagents_on && access.terminal !== false, !opts.subagents_on ? offInSettings : access.terminal === false ? "Off while the terminal is off, because a subagent has its own" : "Side tasks the Master Agent hands to other agents", (on) => change({ subagents: on })),
      accessRow("Studio Agentic modes", access.agentic !== false, opts.agentic_on, opts.agentic_on ? "A studio's own planning agent, like Video Agentic" : offInSettings, (on) => change({ agentic: on })));
    $("#a-pa-tools").replaceChildren(
      accessRow("Terminal", access.terminal !== false, opts.terminal_on, opts.terminal_on ? "Shell commands and scripts. Rocsy's services above still work when this is off." : offInSettings, async (on) => { await change({ terminal: on }); renderAccess(); }));
  }
  const saveModel = () => {
    const [provider, model] = $("#a-pa-model").value ? JSON.parse($("#a-pa-model").value) : ["", ""];
    const reasoning = $("#a-pa-think").value;
    saveProject({ model: { model, provider, reasoning: reasoning === "default" ? "" : reasoning } });
  };
  $("#a-pa-model").addEventListener("change", saveModel);
  $("#a-pa-think").addEventListener("change", saveModel);

  /* the open chat's place in a project */
  function renderCrumb(s) {
    const home = s.project && projectOf(s.project);
    const crumb = $("#a-crumb");
    crumb.hidden = !home;
    if (home) { crumb.textContent = home.name; crumb.dataset.project = home.id; }
    const wanted = home?.id || "";
    if ($("#a-move").dataset.home !== wanted) { $("#a-move").dataset.home = wanted; renderRecentProjects(); }
  }
  $("#a-crumb").addEventListener("click", (e) => showProject(e.currentTarget.dataset.project));
  async function moveCurrent(project) {
    const id = current;
    $("#a-move-dialog").close();
    try {
      await api(`/sessions/${id}/project`, { method: "POST", body: JSON.stringify({ project }) });
      const entry = cache.get(id); if (entry?.snap) { entry.snap.project = project; renderCrumb(entry.snap); }
      const row = sessionOf(id); if (row) row.project = project;
      renderList(sessionsList, true);
      toast(project ? `Moved to ${projectOf(project)?.name}` : "Moved out of the project");
      refreshProjects().catch(() => {});
    } catch (err) { msg(err.message); }
  }
  $("#a-move").addEventListener("click", () => {
    const here = cache.get(current)?.snap.project || "";
    const rows = projectsList.filter((x) => x.id !== here).map((x) => {
      const b = el("button", "a-pv-row"); b.type = "button";
      b.append(tile(x), el("span", "a-pv-row-title", x.name));
      b.addEventListener("click", () => moveCurrent(x.id));
      return b;
    });
    if (here) { const b = el("button", "a-pv-row"); b.type = "button"; b.append(el("span", "a-pv-row-title", "Take out of this project")); b.addEventListener("click", () => moveCurrent("")); rows.unshift(b); }
    if (!rows.length) rows.push(el("p", "a-pv-empty", "No projects yet. Create one from the sidebar first."));
    $("#a-mv-list").replaceChildren(...rows);
    $("#a-move-dialog").showModal();
  });
  $("#a-mv-cancel").addEventListener("click", () => $("#a-move-dialog").close());

  /* ------------------------------------------------ agents -- */
  // Every studio's Agentic sessions (Video, Image, Music, Voice), whoever started them. A session's page shows
  // its log and talks to it: text steers a running run, or continues a finished session; Pause/Resume/Stop.
  const STUDIO_NAME = { video: "Video", image: "Image", music: "Music", voice: "Voice" };
  const AGENT_STATE = { reviewing_plan: "Reviewing the plan", reviewing_output: "Reviewing the output", awaiting_approval: "Waiting for you",
                        awaiting_feedback: "Waiting for you", planning: "Planning", retrying: "Retrying", queuing: "Queuing", reviewing: "Reviewing" };
  const agentStateText = (st) => AGENT_STATE[st] || stateText(st);
  // the shared .a-dot colours, by what the state means
  const dotState = (a) => a.state === "paused" ? "waiting_for_gpu" : /awaiting/.test(a.state) ? "starting"
    : a.busy ? "thinking" : /fail|error|interrupt/.test(a.state) ? "failed" : /complete|done/.test(a.state) ? "done" : "stopped";
  let agentsList = [], agentsOffline = [], agentFilter = "all", agentOpen = null, agentTimer = null, agentLogKey = "", agentSending = false;
  let agentSelecting = false, agentDoomTimer = null;
  const agentSelected = new Set(), agentDoomed = new Map();     // "studio:id" -> title
  const akey = (a) => `${a.studio}:${a.id}`;
  const agentBusy = (a) => a.busy || a.state === "paused";

  function leaveAgentViews() {
    $("#a-agents").hidden = true; $("#a-agent").hidden = true;
    agentSelecting = false; agentSelected.clear();
    agentOpen = null; agentLogKey = ""; clearTimeout(agentTimer);
  }
  function badge(studio) { const b = el("span", "a-studio-badge", STUDIO_NAME[studio] || studio); b.dataset.studio = studio; return b; }
  function paintAgentsBusy() {
    const n = agentsList.filter((a) => a.busy).length;
    $("#a-agents-busy").hidden = !n; $("#a-agents-busy").textContent = n ? String(n) : "";
    $("#a-agents-busy").title = n ? `${n} running` : "";
  }
  async function loadAgents() {
    const r = await api("/agents");
    agentsList = r.agents || []; agentsOffline = r.offline || [];
    paintAgentsBusy();
    if (!$("#a-agents").hidden) renderAgents();
  }
  function showAgents(push = true) {
    open(null, false);
    $("#a-empty").hidden = true; $("#a-agents").hidden = false; $("#a-compose").hidden = true;
    if (push) history.replaceState(null, "", "?agents=1");
    document.title = "Agents · Rocsy";
    renderAgents();
    const tick = () => { clearTimeout(agentTimer); if ($("#a-agents").hidden) return; loadAgents().catch((e) => msg(e.message)).finally(() => { if (!$("#a-agents").hidden) agentTimer = setTimeout(tick, document.hidden ? 30000 : 8000); }); };
    tick();
  }
  function renderAgents() {
    const q = $("#a-agents-search").value.trim().toLowerCase();
    document.querySelectorAll("[data-afilter]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.afilter === agentFilter)));
    const rows = agentsList.filter((a) => !agentDoomed.has(akey(a)) && (agentFilter === "all" || (agentFilter === "busy" ? a.busy || a.state === "paused" : a.studio === agentFilter))
      && (!q || a.title.toLowerCase().includes(q) || (a.chat?.title || "").toLowerCase().includes(q)));
    $("#a-agents-offline").hidden = !agentsOffline.length;
    $("#a-agents-offline").textContent = agentsOffline.length ? `Not reachable right now: ${agentsOffline.map((o) => STUDIO_NAME[o.studio]).join(", ")}.` : "";
    for (const k of [...agentSelected]) if (!rows.some((a) => akey(a) === k && !agentBusy(a))) agentSelected.delete(k);
    $("#a-agents-select").setAttribute("aria-pressed", String(agentSelecting)); $("#a-agents-select").textContent = agentSelecting ? "Done" : "Select";
    $("#a-agents-selbar").hidden = !agentSelecting;
    $("#a-agents-count").textContent = `${agentSelected.size} selected`;
    $("#a-agents-delete").disabled = !agentSelected.size;
    $("#a-agents-delete").textContent = agentSelected.size ? `Delete ${agentSelected.size}` : "Delete";
    $("#a-agents-list").replaceChildren(...(rows.length ? rows.map((a) => {
      const row = el("div", "a-sess-row a-pchat-row"); row.dataset.agent = akey(a);
      const b = el("button", "a-pv-row a-agent-row a-pchat"); b.type = "button"; b.dataset.agent = akey(a); b.dataset.state = dotState(a);
      if (agentSelecting) {
        const box = el("span", "a-check"); box.setAttribute("aria-hidden", "true");
        b.setAttribute("role", "checkbox"); b.setAttribute("aria-checked", String(agentSelected.has(akey(a))));
        if (agentBusy(a)) { b.disabled = true; b.title = "Running or paused: stop it before deleting"; }
        row.classList.add("is-selecting"); row.append(box);
      }
      const text = el("span", "a-pv-row-text");
      const sub = [agentStateText(a.state), ago(a.updated)];
      if (a.runs > 1) sub.push(plural(a.runs, "run"));
      if (a.chat) sub.push(`from “${a.chat.title}”`);
      text.append(el("span", "a-pv-row-title", a.title), el("span", "a-pv-row-sub", sub.join(" · ")));
      b.append(badge(a.studio), text, el("span", "a-dot"));
      row.append(b);
      if (!agentSelecting && !agentBusy(a)) {
        const acts = el("span", "a-sess-acts");
        acts.append(iconBtn("a-sess-del a-agent-del", `Delete “${a.title.slice(0, 60)}”`, ICON.bin));
        row.append(acts);
      }
      return row;
    }) : [el("p", "a-pv-empty", agentsList.length ? "No agents match." : "No Agentic sessions yet. Start one in a studio's Agentic mode, or ask the Master Agent for an Agentic video.")]));
  }
  $("#a-agents-link").addEventListener("click", () => { drawer(false); showAgents(); });
  $("#a-agents-refresh").addEventListener("click", () => loadAgents().catch((e) => msg(e.message)));
  $("#a-agents-search").addEventListener("input", renderAgents);
  document.querySelectorAll("[data-afilter]").forEach((b) => b.addEventListener("click", () => { agentFilter = b.dataset.afilter; renderAgents(); }));
  $("#a-agents-list").addEventListener("click", (e) => {
    const row = e.target.closest(".a-sess-row[data-agent]"); if (!row) return;
    const k = row.dataset.agent, a = agentsList.find((x) => akey(x) === k);
    if (e.target.closest(".a-agent-del")) return removeAgents([k]);
    if (agentSelecting) {
      if (!a || agentBusy(a)) return;
      agentSelected.has(k) ? agentSelected.delete(k) : agentSelected.add(k);
      return renderAgents();
    }
    const at = k.indexOf(":"); showAgent(k.slice(0, at), k.slice(at + 1));
  });
  $("#a-agents-list").addEventListener("keydown", (e) => {
    const b = e.target.closest(".a-agent-row"); if (!b || agentSelecting) return;
    const a = agentsList.find((x) => akey(x) === b.dataset.agent);
    if ((e.key === "Delete" || e.key === "Backspace") && a && !agentBusy(a)) { e.preventDefault(); removeAgents([b.dataset.agent]); }
  });
  function setAgentSelecting(on) { agentSelecting = on; agentSelected.clear(); renderAgents(); }
  $("#a-agents-select").addEventListener("click", () => setAgentSelecting(!agentSelecting));
  $("#a-agents-all").addEventListener("click", () => {
    $("#a-agents-list").querySelectorAll(".a-agent-row:not(:disabled)").forEach((b) => agentSelected.add(b.dataset.agent));
    renderAgents();
  });
  $("#a-agents-delete").addEventListener("click", () => { const ks = [...agentSelected]; setAgentSelecting(false); removeAgents(ks); });
  // Gone from the list at once, deleted after a few seconds unless undone (like chats).
  function removeAgents(keys) {
    keys = keys.filter((k) => { const a = agentsList.find((x) => akey(x) === k); return a && !agentBusy(a) && !agentDoomed.has(k); });
    if (!keys.length) return;
    for (const k of keys) { agentDoomed.set(k, agentsList.find((x) => akey(x) === k)?.title || "session"); agentSelected.delete(k); }
    renderAgents();
    const n = agentDoomed.size;
    toast(n === 1 ? `Deleted “${[...agentDoomed.values()][0].slice(0, 60)}”` : `Deleted ${n} agent sessions`, undoAgentDelete);
    clearTimeout(agentDoomTimer); agentDoomTimer = setTimeout(commitAgentDeletes, 6000);
  }
  function undoAgentDelete() { clearTimeout(agentDoomTimer); agentDoomed.clear(); hideToast(); renderAgents(); }
  async function commitAgentDeletes(keepalive = false) {
    clearTimeout(agentDoomTimer);
    if (!agentDoomed.size) return;
    const keys = [...agentDoomed.keys()];
    const items = keys.map((k) => { const at = k.indexOf(":"); return { studio: k.slice(0, at), id: k.slice(at + 1) }; });
    if (toastUndo === undoAgentDelete) hideToast();
    try {
      const r = await api("/agents/delete", { method: "POST", body: JSON.stringify({ items }), keepalive });
      const gone = new Set(r.deleted.map(akey));
      agentsList = agentsList.filter((a) => !gone.has(akey(a)));
      if (r.busy.length) msg(`${plural(r.busy.length, "session")} started running again, so ${r.busy.length > 1 ? "they weren't" : "it wasn't"} deleted`);
      else if (r.errors.length) msg(`${plural(r.errors.length, "session")} couldn't be deleted: ${r.errors[0].error}`);
    } catch (err) { msg(err.message); }
    agentDoomed.clear();
    renderAgents(); paintAgentsBusy();
    loadAgents().catch(() => {});
  }
  window.addEventListener("pagehide", () => { if (agentDoomed.size) commitAgentDeletes(true); });
  $("#a-agent-back").addEventListener("click", () => showAgents());

  async function showAgent(studio, id, push = true) {
    open(null, false);
    $("#a-empty").hidden = true; $("#a-agent").hidden = false; $("#a-compose").hidden = true;
    agentOpen = { studio, id }; agentLogKey = "";
    if (push) history.replaceState(null, "", `?agent=${studio}:${encodeURIComponent(id)}`);
    const known = agentsList.find((a) => a.studio === studio && a.id === id);
    paintAgentHead(known || { studio, id, title: "Loading…", state: "" });
    $("#a-agent-log").replaceChildren();
    $("#a-agent-input").value = "";
    pollAgent(true);
  }
  function paintAgentHead(a) {
    const b = $("#a-agent-studio"); b.textContent = STUDIO_NAME[a.studio] || a.studio; b.dataset.studio = a.studio;
    $("#a-agent-title").textContent = a.title; $("#a-agent-title").title = a.title;
    document.title = `${(a.title || "Agent").slice(0, 40)} · Agents · Rocsy`;
    $("#a-agent-state").dataset.state = dotState(a);
    $("#a-agent-state-text").textContent = a.state ? agentStateText(a.state) : "";
    $("#a-agent-pause").hidden = !a.can_pause; $("#a-agent-resume").hidden = !a.can_resume;
    $("#a-agent-stop").hidden = !(a.can_pause || a.can_resume);
    $("#a-agent-open").href = a.open || `/${STUDIO_NAME[a.studio] || ""}/`;
    $("#a-agent-open").textContent = `Open in ${STUDIO_NAME[a.studio] || "studio"}`;
    $("#a-agent-chat").hidden = !a.chat;
    if (a.chat) { $("#a-agent-chat").href = `?s=${a.chat.id}`; $("#a-agent-chat").title = a.chat.title; }
    $("#a-agent-stage").hidden = !a.stage; $("#a-agent-stage").textContent = a.stage || "";
    $("#a-agent-help").textContent = a.help || "";
    $("#a-agent-input").placeholder = a.paused ? "Note for when it resumes, or /resume…"
      : a.busy ? "Steer this run… (/help for commands)" : a.state ? "Continue this session with a new instruction…" : "";
  }
  async function pollAgent(first = false) {
    clearTimeout(agentTimer);
    const at = agentOpen; if (!at) return;
    let a = null;
    try { a = await api(`/agents/${at.studio}/${encodeURIComponent(at.id)}`); }
    catch (e) { if (agentOpen === at) { msg(e.message); if (e.status === 404) { showAgents(); return; } } }
    if (agentOpen !== at) return;
    if (a) {
      paintAgentHead(a); paintAgentLog(a.log || [], first);
      const row = agentsList.find((x) => x.studio === a.studio && x.id === a.id);
      if (row) { row.state = a.state; row.busy = a.busy; paintAgentsBusy(); }
    }
    agentTimer = setTimeout(pollAgent, document.hidden ? 30000 : a?.busy ? 2500 : 10000);
  }
  function paintAgentLog(log, first) {
    const key = log.length + ":" + (log.at(-1)?.text.length || 0) + ":" + log.reduce((n, e) => n + e.text.length, 0);
    if (key === agentLogKey) return;
    agentLogKey = key;
    const box = $("#a-agent-log");
    const host = $("#a-agent");
    const atBottom = first || host.scrollHeight - host.scrollTop - host.clientHeight < 80;
    const openThoughts = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.i));
    box.replaceChildren(...log.flatMap((e, i) => {
      const text = e.text.trim();
      if (e.kind === "thinking" || e.kind === "stream") {
        // Video's rows start with a label line ("Planning\n…"); the framework's are the text alone.
        const cut = e.text.indexOf("\n");
        const label = cut < 0 ? "Hermes" : e.text.slice(0, cut).trim();
        const body = (cut < 0 ? e.text : e.text.slice(cut + 1)).trim();
        if (!body) return [];
        if (e.kind === "stream") { const li = el("li", "a-alog a-alog-say"); li.dataset.kind = "assistant"; li.append(el("span", "a-alog-label", label), el("div", null, body)); return [li]; }
        const li = el("li", "a-alog"); li.dataset.kind = "thinking";
        const d = el("details"); d.dataset.i = String(i); d.open = openThoughts.has(String(i));
        d.append(el("summary", null, `Thinking · ${label}`), el("pre", null, body));
        li.append(d); return [li];
      }
      if (!text) return [];
      const li = el("li", "a-alog"); li.dataset.kind = e.kind;
      if (e.kind === "user") li.append(el("span", "a-ev-bubble", text));
      else if (e.kind === "run") li.textContent = text;
      else { if (e.t) { const t = el("time", "a-alog-t", new Date(e.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })); li.append(t); } li.append(el("span", null, text)); }
      return [li];
    }));
    if (!log.length) box.append(el("li", "a-pv-empty", "No log yet."));
    if (atBottom) host.scrollTop = host.scrollHeight;
  }
  async function agentAction(action) {
    const at = agentOpen; if (!at) return;
    const btn = $(`#a-agent-${action}`); btn.disabled = true;
    try { await api(`/agents/${at.studio}/${encodeURIComponent(at.id)}/${action}`, { method: "POST", body: "{}" }); toast(action === "pause" ? "Paused" : action === "resume" ? "Resuming" : "Stopping"); }
    catch (e) { msg(e.message); }
    finally { btn.disabled = false; pollAgent(); }
  }
  $("#a-agent-pause").addEventListener("click", () => agentAction("pause"));
  $("#a-agent-resume").addEventListener("click", () => agentAction("resume"));
  $("#a-agent-stop").addEventListener("click", () => { if (confirm("Stop this run? Unfinished renders are cancelled.")) agentAction("stop"); });
  $("#a-agent-chat").addEventListener("click", (e) => { e.preventDefault(); const id = new URLSearchParams($("#a-agent-chat").search).get("s"); if (id) open(id); });
  $("#a-agent-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const at = agentOpen, text = $("#a-agent-input").value.trim();
    if (!at || !text || agentSending) return;
    agentSending = true; $("#a-agent-send").disabled = true;
    try {
      const r = await api(`/agents/${at.studio}/${encodeURIComponent(at.id)}/message`, { method: "POST", body: JSON.stringify({ text }) });
      $("#a-agent-input").value = "";
      if (r.reply) toast(r.reply.split("\n")[0].slice(0, 140));
      if (r.session_id && r.session_id !== at.id) {     // a finished Image/Music/Voice session continues in a new one
        toast("Continuing in a new session");
        loadAgents().catch(() => {});
        return showAgent(at.studio, r.session_id);
      }
      agentLogKey = ""; pollAgent();
    } catch (err) { msg(err.message); }
    finally { agentSending = false; $("#a-agent-send").disabled = false; }
  });
  $("#a-agent-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey || (!e.shiftKey && !narrow.matches))) { e.preventDefault(); $("#a-agent-form").requestSubmit(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && agentOpen) pollAgent(); });

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
  const hint = () => { const home = draftProject && projectOf(draftProject); if (home) { input.placeholder = narrow.matches ? "New chat…" : `New chat in ${home.name}…`; return; } input.placeholder = narrow.matches ? "Ask the Master Agent… (/ for commands)" : `Ask the Master Agent… (/ for commands, ${/Mac|iP/.test(navigator.platform) ? "⌘" : "Ctrl"}+Enter to send)`; };
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
  projectsList = initial?.projects || [];
  if (!COMMANDS.length) api("/commands").then((r) => { COMMANDS = r.commands; }).catch(() => {});
  current = params.get("s") || null;
  renderList(initial?.sessions || []);
  if (params.get("prompt")) { input.value = params.get("prompt"); grow(); }   // no-JS fallback from home: prefill only
  current = null;
  open(params.get("s"), false);
  if (params.get("p")) showProject(params.get("p"), false);
  else if (params.has("projects")) showProjects(false);
  else if (params.get("agent")) { const [st, ...rest] = params.get("agent").split(":"); showAgent(st, rest.join(":"), false); }
  else if (params.has("agents")) showAgents(false);
  loadAgents().catch(() => {});      // the sidebar's running count
  if (!initial) api("/sessions").then((r) => renderList(r.sessions)).catch((e) => msg(e.message));
})();
