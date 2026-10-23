(() => {
  const base = window.TRAINER_BASE || "/Trainer";
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const enc = encodeURIComponent;
  const escapeHtml = (t) => String(t).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const CHUNK = 8 * 1024 * 1024;  // stays under the gateway's 50 MB request limit
  let current = null, data = null, batchTrim = null, batchTrimTimer = null, batchReview = null, reviewPick = null;
  const drafts = new Map();       // "dataset/file" -> unsaved caption text
  const seen = new Map();         // dataset -> last listing, painted at once when switching back

  async function api(path, options={}) {
    const r = await fetch(base + path, {credentials:"same-origin", headers:{"Content-Type":"application/json", ...(options.headers||{})}, ...options});
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(body.detail || body.message || body.error || `Request failed (${r.status})`);
    return body;
  }
  function note(text, bad=false) { const m = $("#ds-message"); m.textContent = text || ""; m.style.color = bad ? "var(--red)" : "var(--amber)"; }
  const changed = () => document.dispatchEvent(new CustomEvent("datasets-changed"));

  // ---- tabs ----
  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    for (const t of ["train", "datasets", "rules", "loras"]) { const p = $(`#tab-${t}`); if (p) p.hidden = name !== t; }
    document.body.classList.toggle("on-datasets", name === "datasets" || name === "rules");
    if (name === "datasets") loadList();
    if (name === "rules") document.dispatchEvent(new CustomEvent("rules-tab-shown"));
    if (name === "train") document.dispatchEvent(new CustomEvent("train-tab-shown"));
    if (name === "loras") document.dispatchEvent(new CustomEvent("lora-gallery-tab-shown"));
    if (location.hash !== `#${name}`) history.replaceState(null, "", name === "train" ? location.pathname : `#${name}`);
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

  // ---- dataset list ----
  function countsText(d) {
    const c = d.counts || {}, parts = [];
    if (c.video) parts.push(`${c.video} clip${c.video > 1 ? "s" : ""}`);
    if (c.image) parts.push(`${c.image} image${c.image > 1 ? "s" : ""}`);
    if (c.audio) parts.push(`${c.audio} audio`);
    return (parts.join(" · ") || "empty") + ` · ${d.size_mb} MB`;
  }
  async function loadList() {
    try {
      const res = await api("/api/datasets");
      $("#ds-root").textContent = res.root_host;
      const sel = $("#ds-select"); sel.replaceChildren(new Option(res.datasets.length ? "Choose…" : "No datasets yet", ""));
      for (const d of res.datasets) {
        sel.append(new Option(`${d.name} (${d.items})`, d.name));
      }
      sel.value = res.datasets.some((d) => d.name === current) ? current : "";
    } catch (e) { note(e.message, true); }
  }
  $("#ds-delete").addEventListener("click", async () => {
    if (!current) return;
    const typed = prompt(`Permanently delete the dataset "${current}" and ALL its files (clips, captions, _originals)?\n\nThis can't be undone. Type the dataset name to confirm:`);
    if (typed === null) return;
    if (typed.trim() !== current) { note("Name didn't match; nothing was deleted.", true); return; }
    try {
      await api(`/api/datasets/${enc(current)}?confirm=${enc(current)}`, {method:"DELETE"});
      note(`Deleted dataset "${current}".`);
      try { if (localStorage.getItem("trainer-dataset") === current) localStorage.removeItem("trainer-dataset"); } catch (_) {}
      current = null; data = null; drafts.clear();
      $("#ds-view").hidden = true; $("#ds-empty").hidden = false; $("#ds-delete").hidden = true; $("#ds-summary").textContent = "";
      await loadList(); changed();
    } catch (e) { note(e.message, true); }
  });
  $("#ds-select").addEventListener("change", (e) => { if (e.target.value) openDataset(e.target.value); });

  $("#ds-new").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.currentTarget;            // gone after the first await, so keep it
    const input = form.elements.name, name = input.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(name)) {
      note("Dataset names can use letters, numbers, _ and - (no spaces), and must start with a letter or number.", true);
      input.focus(); return;
    }
    const btn = form.querySelector("button"); btn.disabled = true;
    try {
      await api("/api/datasets", {method:"POST", body:JSON.stringify({name})});
      form.reset();
      await loadList(); changed();
      await openDataset(name);
      note(`Created dataset "${name}". Add files, paste a YouTube link, or drop videos here.`);
    } catch (e) { note(e.message, true); input.focus(); }
    finally { btn.disabled = false; }
  });

  // ---- one dataset ----
  async function openDataset(name) {
    if (current !== name) closeTerm();
    current = name;
    try { localStorage.setItem("trainer-dataset", name); } catch (_) {}
    $("#ds-empty").hidden = true; $("#ds-view").hidden = false; $("#ds-delete").hidden = false;
    batchTrim = null; batchReview = null; reviewPick = null;
    if (batchTrimTimer) clearInterval(batchTrimTimer);
    if (seen.has(name)) { data = seen.get(name); renderItems(); }   // instant, then refreshed below
    await reload();
    resumeBatchTrim();
    resumeTagJob();
    resumeFind();
    refreshUrlQueue();
    loadList();
  }
  async function reload() {
    if (!current) return;
    const name = current;
    try {
      const fresh = await api(`/api/datasets/${enc(name)}`);
      seen.set(name, fresh);
      if (current === name) { data = fresh; renderItems(); }
    }
    catch (e) { note(e.message, true); }
  }
  $("#ds-reload").addEventListener("click", () => { loadList(); reload(); });
  document.querySelectorAll("input[name=ds-filter]").forEach((r) => r.addEventListener("change", renderItems));
  function setView(mode) {
    $("#ds-items").className = `ds-items ${mode}`;
    document.querySelectorAll(".ds-viewmode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === mode)));
    try { localStorage.setItem("trainer-ds-view", mode); } catch (_) {}
  }
  document.querySelectorAll(".ds-viewmode button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  try { setView(localStorage.getItem("trainer-ds-view") === "grid" ? "grid" : "rows"); } catch (_) { setView("rows"); }

  function infoText(it) {
    const i = it.info || {};
    if (it.kind === "video") return [i.width && `${i.width}×${i.height}`, i.fps && `${i.fps} fps`, i.frames && `${i.frames} frames`, i.duration && `${i.duration}s`, i.sample_rate ? "sound" : "no sound"].filter(Boolean).join(" · ");
    if (it.kind === "audio") return [i.duration && `${i.duration}s`, i.sample_rate && `${i.sample_rate / 1000} kHz`].filter(Boolean).join(" · ");
    return `${it.size_mb} MB`;
  }
  function renderItems() {
    if (!data) return;
    const c = data.counts || {};
    updateSummary();
    renderStaged();
    const filter = document.querySelector("input[name=ds-filter]:checked").value;
    const host = $("#ds-items"); host.replaceChildren();
    const items = data.items.filter((it) => filter === "all" || (filter === "nocap" ? !it.caption : (it.h3_issues || []).length));
    if (!items.length) host.append(el("p", "muted", data.items.length ? "Nothing matches this filter." : "This dataset is empty. Add files above."));
    for (const it of items) host.append(itemCard(it));
    if (term.job && !$("#ds-term").hidden) paintTerm();
    $("#ds-originals").textContent = data.originals?.length ? `${data.originals.length} original(s) kept in ${current}/_originals after conversion (not used for training).` : "";
  }
  // ---- captions: auto-save (debounced) shared by the card and the editor ----
  const saveTimers = new Map();
  function captionSaver(it, key, ta, state) {
    const show = (txt) => { state.textContent = txt; state.dataset.state = txt; };
    show(it.caption ? "" : "missing");
    const flush = async () => {
      clearTimeout(saveTimers.get(key)); saveTimers.delete(key);
      const value = ta.value;
      if (value.trim() === (it.caption || "").trim()) { drafts.delete(key); show(it.caption ? "saved" : "missing"); return; }
      show("saving…");
      try {
        const r = await api(`/api/datasets/${enc(current)}/caption/${enc(it.file)}`, {method:"PUT", body:JSON.stringify({caption:value})});
        it.caption = r.caption;
        if (ta.value === value) { drafts.delete(key); show(r.caption ? "saved" : "missing"); }
        data.missing_captions = data.items.filter((x) => !x.caption).length;
        updateSummary();
        document.querySelectorAll(`textarea[data-key="${CSS.escape(key)}"]`).forEach((other) => { if (other !== ta && document.activeElement !== other) other.value = r.caption || ""; });
      } catch (e) { show("not saved"); note(e.message, true); }
    };
    ta.dataset.key = key;
    ta.addEventListener("input", () => { drafts.set(key, ta.value); show("editing…"); clearTimeout(saveTimers.get(key)); saveTimers.set(key, setTimeout(flush, 700)); });
    ta.addEventListener("blur", () => { if (saveTimers.has(key)) flush(); });
    return flush;
  }
  function updateSummary() {
    $("#ds-summary").textContent = `${countsText(data)}${data.missing_captions ? ` · ${data.missing_captions} without caption` : ""}`;
  }
  const mediaUrl = (it) => `${base}/api/datasets/${enc(current)}/media/${enc(it.file)}?v=${it.mtime}`;

  function itemCard(it) {
    const key = `${current}/${it.file}`;
    const card = el("article", "ds-item" + (it.file === selectedFile ? " selected" : ""));
    card.dataset.file = it.file;
    card.addEventListener("pointerdown", () => selectCard(it.file));
    const media = el("div", "ds-media");
    if (it.kind === "video") {
      const img = el("img"); img.loading = "lazy"; img.decoding = "async"; img.alt = it.file;
      img.src = `${base}/api/datasets/${enc(current)}/thumb/${enc(it.file)}?v=${it.mtime}`;
      media.append(img, el("span", "play", "▶"));
      media.title = "Hover to play · click to open";
      // Play on hover: a muted looping <video> over the thumbnail, removed on leave.
      media.addEventListener("pointerenter", (e) => {
        if (e.pointerType === "touch" || media.querySelector("video")) return;
        const v = el("video"); v.src = mediaUrl(it); v.muted = true; v.loop = true; v.playsInline = true; v.autoplay = true; v.className = "hover-video";
        media.append(v); v.play().catch(() => {});
      });
      media.addEventListener("pointerleave", () => { const v = media.querySelector("video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); } });
    } else if (it.kind === "image") {
      const img = el("img"); img.loading = "lazy"; img.decoding = "async"; img.src = mediaUrl(it); img.alt = it.file; media.append(img);
    } else {
      const a = el("audio"); a.controls = true; a.preload = "none"; a.src = mediaUrl(it); media.classList.add("audio"); media.append(a);
    }
    if (it.kind !== "audio") media.addEventListener("click", () => openEditor(it));
    card.append(media);
    const bodyEl = el("div", "ds-item-body");
    const head = el("div", "ds-item-head");
    const nameEl = el("strong", null, it.file); nameEl.title = it.file; head.append(nameEl);
    const row = el("div", "ds-spec");
    row.append(el("span", "ds-info", infoText(it)));
    head.append(row);
    if (it.kind === "video") {
      const issues = it.h3_issues || [];
      if (!issues.length) row.append(el("span", "ok", "✓ H3 spec"));
      else {
        row.append(el("span", "bad", "✗ " + issues.join(", ")));
        const fix = el("button", "button secondary", "Convert to H3 clip");
        fix.title = "GPU re-encode: 24 fps, longest valid length from the start (max 124 frames), sides cropped to multiples of 32, long side ≤1024. The original is kept in _originals/.";
        fix.addEventListener("click", async () => {
          fix.disabled = true; fix.textContent = "Converting…";
          try { const r = await api(`/api/datasets/${enc(current)}/convert/${enc(it.file)}`, {method:"POST"}); note(`Converted ${r.file}: ${r.frames} frames, ${r.size}.`); await reload(); }
          catch (e) { note(e.message, true); fix.disabled = false; fix.textContent = "Convert to H3 clip"; }
        });
        row.append(fix);
      }
    }
    const state = el("span", "ds-cap-state");
    const tools = el("div", "ds-cap-row"); tools.append(state);
    if (it.kind === "video") {
      const trimBtn = el("button", "button secondary icon", "✂"); trimBtn.title = "Trim into one or more clips (replaces this video)";
      trimBtn.addEventListener("click", () => queueTrim(it.file, false));
      tools.append(trimBtn);
    }
    const del = el("button", "button secondary danger icon", "✕"); del.title = "Delete this file and its caption";
    del.addEventListener("click", async () => {
      if (!confirm(`Delete ${it.file} and its caption from ${current}? This can't be undone.`)) return;
      try { await api(`/api/datasets/${enc(current)}/items/${enc(it.file)}`, {method:"DELETE"}); drafts.delete(key); await reload(); loadList(); changed(); }
      catch (e) { note(e.message, true); }
    });
    tools.append(del);
    head.append(tools);
    bodyEl.append(head);
    // Captions are edited in the large editor; the card just shows whether one exists.
    state.textContent = it.caption ? "" : "missing"; state.dataset.state = it.caption ? "" : "missing";
    const more = el("button", "button secondary ds-more", "Edit caption");
    more.title = it.caption ? it.caption.trim() : "No caption yet";
    more.addEventListener("click", () => openEditor(it));
    tools.prepend(more);
    const preview = el("span", "ds-cap-preview", it.caption ? it.caption.trim() : "No caption yet");
    if (!it.caption) preview.classList.add("empty");
    preview.title = "Click to edit"; preview.addEventListener("click", () => openEditor(it));
    head.insertBefore(preview, tools);
    card.append(bodyEl);
    return card;
  }

  // ---- "Show more": enlarged media + caption editor ----
  function openEditor(it) {
    const list = visibleItems();
    let idx = Math.max(0, list.findIndex((x) => x.file === it.file));
    const shell = modal("ds-editor");
    const render = () => {
      const cur = list[idx], key = `${current}/${cur.file}`;
      const stage = el("div", "ed-stage");
      if (cur.kind === "video") { BunnyPlayer.create(stage, {src: mediaUrl(cur), fps: cur.info?.fps || 24, duration: cur.info?.duration, autoplay: true, loop: true, muted: true}); }
      else if (cur.kind === "image") { const i = el("img"); i.src = mediaUrl(cur); stage.append(i); }
      else { const a = el("audio"); a.src = mediaUrl(cur); a.controls = true; stage.append(a); }
      const side = el("div", "ed-side");
      const top = el("div", "ed-top");
      top.append(el("strong", null, cur.file), el("span", "ds-info", infoText(cur)));
      const ta = el("textarea", "ed-caption"); ta.placeholder = "Caption for this clip, starting with your trigger word. Saves automatically.";
      ta.value = drafts.has(key) ? drafts.get(key) : (cur.caption || "").trim();
      const state = el("span", "ds-cap-state");
      captionSaver(cur, key, ta, state);
      const nav = el("div", "ed-nav");
      const prev = el("button", "button secondary", "◀ Previous"), next = el("button", "button secondary", "Next ▶");
      prev.disabled = idx === 0; next.disabled = idx === list.length - 1;
      prev.addEventListener("click", () => { ta.blur(); idx--; render(); });
      next.addEventListener("click", () => { ta.blur(); idx++; render(); });
      prev.title = "← (Alt+← while typing)"; next.title = "→ (Alt+→ while typing)";
      const auto = el("button", "button secondary", "✨ Auto-caption this clip");
      auto.title = "Let the vision model write this caption (uses the Auto-tag settings)";
      auto.addEventListener("click", async () => {
        const job = await openAutotag([cur.file]);
        if (!job) return;
        state.textContent = "auto-captioning…"; state.dataset.state = "";
        const onDone = async (ev) => {
          document.removeEventListener("tagjob-finished", onDone);
          const fresh = data.items.find((x) => x.file === cur.file);
          if (fresh && list[idx]?.file === cur.file) { Object.assign(cur, fresh); ta.value = (fresh.caption || "").trim(); state.textContent = ev.detail.done ? "saved" : "failed"; state.dataset.state = ev.detail.done ? "saved" : "not saved"; }
        };
        document.addEventListener("tagjob-finished", onDone);
      });
      const close = el("button", "button primary", "Done");
      close.addEventListener("click", () => shell.close());
      nav.append(prev, el("span", "ed-count", `${idx + 1} / ${list.length}`), next, state, close);
      side.append(top, ta, nav);
      if (cur.kind !== "audio") side.insertBefore(auto, nav);
      shell.body.replaceChildren(stage, side);
    };
    // ←/→ step through clips; while typing in the caption use Alt+←/→ so the cursor still moves.
    const onKey = (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const typing = e.target.closest?.("textarea, input, select");
      if (typing && !e.altKey) return;
      const nidx = idx + (e.key === "ArrowLeft" ? -1 : 1);
      if (nidx < 0 || nidx >= list.length) return;
      e.preventDefault(); document.activeElement?.blur?.(); idx = nidx; render();
      if (!typing) document.activeElement?.blur?.();
    };
    window.addEventListener("keydown", onKey);
    shell.onclose = () => { window.removeEventListener("keydown", onKey); document.activeElement?.blur?.(); const f = list[idx]?.file; renderItems(); if (f) selectCard(f); };
    render();
  }
  function visibleItems() {
    const filter = document.querySelector("input[name=ds-filter]:checked").value;
    return data.items.filter((it) => filter === "all" || (filter === "nocap" ? !it.caption : (it.h3_issues || []).length));
  }
  function modal(cls) {
    const overlay = el("div", `ds-modal ${cls}`);
    const box = el("div", "ds-modal-box"); const body = el("div", "ds-modal-body");
    box.append(body); overlay.append(box); document.body.append(overlay);
    document.body.classList.add("modal-open");
    const api_ = { body, overlay, onclose: null, close() {
      overlay.querySelectorAll("video").forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
      overlay.remove(); document.body.classList.remove("modal-open"); window.removeEventListener("keydown", onKey);
      api_.onclose?.();
    } };
    const onKey = (e) => {
      const all = document.querySelectorAll(".ds-modal");
      if (e.key === "Escape" && all[all.length - 1] === overlay && !e.target.closest?.("textarea, input")) api_.close();
    };
    window.addEventListener("keydown", onKey);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) api_.close(); });
    return api_;
  }

  // ---- trimmer: H3 Machine's reference trimmer (renderTrimTimeline), extended to many trims ----
  const MAX_TRIM_SECONDS = 15;          // H3 Machine's reference limit
  const H3_MAX_TRIM_SECONDS = 124 / 24; // longest H3 training clip (124 frames)
  const MIN_TRIM_SECONDS = 1;           // same minimum as H3 Machine
  const H3_GRID = [22, 39, 56, 73, 90, 107, 124];
  const LANE_COLORS = ["#79add3", "#e4b460", "#91ca7c", "#b6a0e1", "#dd8585", "#6fd0c4", "#e08fc4", "#c9c46b"];
  // ---- one trimmer at a time: every way of opening it goes through this queue ----
  const trimQueue = [];
  let trimRunning = false;
  function queueTrim(file, staged = false) {
    const ds = current;
    if (!ds || trimQueue.some((e) => e.dataset === ds && e.file === file && e.staged === staged)) return;
    trimQueue.push({dataset: ds, file, staged});
    pumpTrims();
  }
  async function pumpTrims() {
    if (trimRunning) return;
    trimRunning = true;
    try {
      while (trimQueue.length) {
        const e = trimQueue.shift();
        if (e.dataset !== current) { note(`Skipped trimming ${e.file}: it is in "${e.dataset}", and you switched datasets.`, true); continue; }
        await reload();                       // fresh info: it may have been trimmed, deleted or converted meanwhile
        const list = e.staged ? (data?.staged || []) : (data?.items || []);
        const it = list.find((x) => x.file === e.file && (e.staged || x.kind === "video"));
        if (!it) { note(`${e.file} is no longer in "${current}"; skipped.`, true); continue; }
        await openTrimmer({file: it.file, staged: e.staged, info: it.info, caption: it.caption});
      }
    } finally { trimRunning = false; }
  }

  function openTrimmer({file, staged, info, caption}) {
    return new Promise((resolve) => {
      const duration = (info && info.duration) || 0;
      const fps = (info && info.fps) || 24;
      const shell = modal("ds-trimmer");
      let h3Ready = true;
      try { h3Ready = localStorage.getItem("trainer-trim-h3") !== "0"; } catch (_) {}
      const maxTrim = () => (h3Ready ? H3_MAX_TRIM_SECONDS : MAX_TRIM_SECONDS);
      let segs = [{start: 0, end: Math.min(duration, maxTrim())}], active = 0;
      const ac = {job: null, timer: null, lines: [], next: 0, seen: new Set()};   // ✨ Auto-cut state
      const src = `${base}/api/datasets/${enc(current)}/media/${enc(file)}?staged=${staged ? "true" : "false"}`;

      shell.body.innerHTML = `
        <div class="tr-left"><div class="tr-stage" id="tr-stage"></div><div class="tr-feed" id="tr-feed" hidden></div></div>
        <div class="tr-side">
          <div class="tr-head">
            <div class="ed-top"><strong></strong><span class="ds-info"></span></div>
            <div class="tr-actions">
              <button type="button" class="button secondary" id="tr-cancel"></button>
              <button type="button" class="button primary" id="trim-save"></button>
            </div>
          </div>
          <div class="tr-row">
            <button type="button" class="button secondary tr-autocut-btn" id="tr-autocut" title="Let Hermes find good training clips in this video">✨ Auto-cut</button>
            <button type="button" class="button secondary" id="tr-add">+ Add trim</button>
            <button type="button" class="button secondary" id="tr-remove">Remove this trim</button>
            <button type="button" class="button secondary" id="trim-reset">Reset to full clip</button>
            <label class="tr-check"><input type="checkbox" id="tr-loop" checked> Loop selection</label>
            <label class="tr-check" title="24 fps, length snapped down to an H3 frame count (22-124), sides cropped to multiples of 32, long side ≤1024, audio 32 kHz stereo"><input type="checkbox" id="tr-h3"> H3-ready clips</label>
          </div>
          <p class="trim-readout" id="trim-readout"></p>
          <div class="tr-zoom-row">
            <div class="trim-overview" id="trim-overview" title="Whole video: click to jump the zoomed view there"><div class="ov-window" id="ov-window"></div></div>
            <select id="tr-zoom" title="Zoom the trim timelines (centred on the selected trim)">
              <option value="0">Full</option><option value="60">60 s</option><option value="30">30 s</option><option value="15">15 s</option>
            </select>
          </div>
          <div class="tr-ac" id="tr-ac" hidden>
            <div class="tr-ac-form">
              <label class="wide">Looking for <span class="muted">(what the LoRA is about)</span><input id="ac-concept" maxlength="300" placeholder="e.g. a watercolor style / my own character / a red sports car"></label>
              <label>Clips<input id="ac-count" type="number" min="1" max="50" value="8"></label>
              <label>Length<select id="ac-len"></select></label>
              <label>Model<select id="ac-model"></select></label>
              <label class="wide tr-ac-scene" title="On: detect scene cuts and scout one or two windows per shot. Off: scan throughout the video timeline."><input id="ac-scenes" type="checkbox"> Scout by scene cuts <span class="muted">1–2 windows per detected shot</span></label>
              <div class="tr-ac-go"><button type="button" class="button primary" id="ac-start">Find clips</button><button type="button" class="button secondary" id="ac-close">Close</button></div>
            </div>
            <div class="hterm tr-ac-term" id="ac-term" hidden>
              <div class="hterm-head"><span class="hterm-brand">☤ Hermes Agent</span><span class="hterm-sub"> · clip-scout</span><span class="hterm-state" id="ac-state"></span><button type="button" class="hterm-close" id="ac-cancel" title="Cancel">■</button></div>
              <div class="hterm-body" id="ac-body"></div>
              <div class="hterm-status" id="ac-status"></div>
            </div>
          </div>
          <div class="trim-lanes" id="trim-lanes"></div>
          <p class="box-note tr-keys">Keys: ←/→ one frame (Shift: 1 s) · ↑/↓ switch trim · Space play/pause · [ / ] set start/end at the playhead</p>
          <p class="box-note" id="tr-note"></p>
        </div>`;
      const q = (sel) => shell.body.querySelector(sel);
      q(".ed-top strong").textContent = file;
      try { q("#ac-scenes").checked = localStorage.getItem("trainer-autocut-scenes") === "1"; } catch (_) {}
      q("#ac-scenes").addEventListener("change", () => { try { localStorage.setItem("trainer-autocut-scenes", q("#ac-scenes").checked ? "1" : "0"); } catch (_) {} });
      q(".ed-top .ds-info").textContent = info ? [info.width && `${info.width}×${info.height}`, info.fps && `${info.fps} fps`, `${duration.toFixed(2)}s`].filter(Boolean).join(" · ") : "";
      const queued = trimQueue.filter((e) => e.dataset === current).length;
      if (queued) {
        const qn = el("span", "tr-queue", ` · ${queued} more video${queued > 1 ? "s" : ""} queued `);
        const clr = el("button", "linkish", "clear queue"); clr.type = "button";
        clr.addEventListener("click", () => { trimQueue.length = 0; qn.textContent = " · queue cleared"; });
        qn.append(clr); q(".ed-top .ds-info").append(qn);
      }
      q("#tr-h3").checked = h3Ready;
      q("#tr-cancel").textContent = staged ? "Close" : "Cancel";
      if (staged) q("#tr-cancel").title = "Close and keep the download for later";
      const player = BunnyPlayer.create(q("#tr-stage"), {src, fps: (info && info.fps) || 24, duration,
        onLoopToggle: (on) => { q("#tr-loop").checked = on; }});
      player.setLoop(true);
      const mediaEl = player.video, lanesHost = q("#trim-lanes"), readout = q("#trim-readout");
      // Zoomed view window shared by all lanes (Full = H3 Machine's whole-clip timeline).
      let zoom = 0, view = {start: 0, span: duration};   // start on the full clip
      const zoomSel = q("#tr-zoom"); zoomSel.value = String(zoom);
      [...zoomSel.options].forEach((o) => { if (+o.value && +o.value >= duration) o.disabled = true; });
      function centerView(t) {
        view.span = zoom && zoom < duration ? zoom : duration;
        view.start = Math.max(0, Math.min(duration - view.span, t - view.span / 2));
      }
      const pct = (t) => (view.span ? ((t - view.start) / view.span) * 100 : 0);
      const opct = (t) => (duration ? (t / duration) * 100 : 0);
      const snapFrames = (len) => Math.max(0, ...H3_GRID.filter((f) => f <= Math.round(len * 24)));
      const color = (i) => LANE_COLORS[i % LANE_COLORS.length];

      // One timeline ("lane") per trim, each in its own colour. The selected lane carries
      // H3 Machine's handles: start/end edges and the dotted middle to move the whole window.
      function buildLanes() {
        lanesHost.replaceChildren();
        segs.forEach((sg, i) => {
          const lane = el("div", "trim-lane" + (i === active ? " active" : ""));
          lane.dataset.index = i; lane.style.setProperty("--c", color(i));
          const label = el("span", "lane-label", String(i + 1));
          if (sg.reason) { label.title = `${sg.score != null ? `score ${sg.score}/10 · ` : ""}${sg.subject ? sg.subject + " — " : ""}${sg.reason}`; label.classList.add("has-reason"); }
          const tl = el("div", "trim-timeline lane-timeline");
          const track = el("div", "trim-track");
          const range = el("div", "trim-range");
          if (i === active) {
            const mid = el("div", "trim-handle-middle"); mid.title = "Drag to move the whole selection"; range.append(mid);
          }
          track.append(range, el("div", "trim-playhead"));
          if (i === active) {
            const hs = el("div", "trim-handle"); hs.dataset.handle = "start";
            const he = el("div", "trim-handle"); he.dataset.handle = "end";
            track.append(hs, he);
          }
          tl.append(track);
          const time = el("span", "lane-time");
          lane.append(label, tl, time);
          lanesHost.append(lane);
        });
        paint();
      }
      function paint() {
        const clampP = (v) => Math.max(0, Math.min(100, v));
        lanesHost.querySelectorAll(".trim-lane").forEach((lane) => {
          const sg = segs[+lane.dataset.index];
          const range = lane.querySelector(".trim-range");
          const l = clampP(pct(sg.start)), r = clampP(pct(sg.end));
          range.style.left = `${l}%`;
          range.style.width = `${Math.max(0, r - l)}%`;
          range.classList.toggle("offview", r <= 0 || l >= 100);
          const hs = lane.querySelector('[data-handle="start"]'), he = lane.querySelector('[data-handle="end"]');
          if (hs) { hs.style.left = `${pct(sg.start)}%`; hs.hidden = pct(sg.start) < -0.5 || pct(sg.start) > 100.5; }
          if (he) { he.style.left = `${pct(sg.end)}%`; he.hidden = pct(sg.end) < -0.5 || pct(sg.end) > 100.5; }
          const len = sg.end - sg.start;
          lane.querySelector(".lane-time").textContent = h3Ready ? `${snapFrames(len) || "<22"}f` : `${len.toFixed(2)}s`;
        });
        const cur = segs[active];
        const maxT = maxTrim();
        const capped = cur.end - cur.start >= maxT - 1e-6;
        let text = `Trim ${active + 1}: ${cur.start.toFixed(2)}s – ${cur.end.toFixed(2)}s `
          + `(${(cur.end - cur.start).toFixed(2)}s of ${duration.toFixed(2)}s, max ${maxT.toFixed(1)}s)`
          + (capped ? " — at the max clip length" : "");
        if (h3Ready) { const f = snapFrames(cur.end - cur.start); text += f ? ` → ${f} frames (${(f / 24).toFixed(2)}s) when cut` : " → too short for H3 (min 22 frames)"; }
        readout.textContent = text;
        readout.style.color = color(active);
        if (ac.job && scoutDone(ac.job) && !q("#tr-feed").hidden) queueMicrotask(() => paintScout(ac.job));
        player.setSegments(segs.map((sg, i) => ({start: sg.start, end: sg.end, color: color(i), active: i === active, label: i + 1,
          title: `Trim ${i + 1}: ${sg.start.toFixed(2)}–${sg.end.toFixed(2)}s${sg.reason ? ` · ${sg.reason}` : ""}`, onClick: () => select(i)})));
        const ov = q("#trim-overview");
        ov.querySelectorAll(".ov-seg").forEach((n) => n.remove());
        segs.forEach((sg, i) => { const b = el("div", "ov-seg"); b.style.left = `${opct(sg.start)}%`; b.style.width = `${Math.max(0.4, opct(sg.end) - opct(sg.start))}%`; b.style.background = color(i); ov.append(b); });
        const win = q("#ov-window"); win.style.left = `${opct(view.start)}%`; win.style.width = `${opct(view.start + view.span) - opct(view.start)}%`;
        q(".tr-zoom-row").classList.toggle("is-full", view.span >= duration);
        q("#tr-remove").disabled = segs.length < 2;
        const n = segs.length;
        q("#trim-save").textContent = `Cut ${n} clip${n > 1 ? "s" : ""}`;
        q("#tr-note").textContent = `Each trim becomes its own clip in ${current}. ${staged ? "The download is deleted after cutting." : "The original video (and its caption) is replaced by the cut clips."}${caption ? " Its caption is copied to every clip." : ""}`;
        paintPlayhead();
      }
      const paintPlayhead = () => lanesHost.querySelectorAll(".trim-playhead").forEach((ph) => { const v = pct(mediaEl.currentTime || 0); ph.style.left = `${v}%`; ph.hidden = v < 0 || v > 100; });
      const recenter = () => { const sg = segs[active]; centerView((sg.start + sg.end) / 2); };
      let previewRange = null;   // a scout window being previewed from the feed (not a trim)
      const select = (i) => { previewRange = null; active = Math.max(0, Math.min(segs.length - 1, i)); mediaEl.currentTime = segs[active].start; recenter(); buildLanes(); };
      zoomSel.addEventListener("change", () => { zoom = +zoomSel.value; recenter(); paint(); });
      q("#trim-overview").addEventListener("pointerdown", (e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        const t = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * duration;
        centerView(t); mediaEl.currentTime = t; paint();
      });
      // Clamp every trim to the active length limit (switching H3-ready shrinks long ones).
      function clampAll() { for (const sg of segs) { if (sg.end - sg.start > maxTrim()) sg.end = sg.start + maxTrim(); } }
      recenter();
      buildLanes();

      // Same drag rules as H3 Machine's renderTrimTimeline, via delegation on the lanes.
      const timeAt = (tl, event) => {
        const rect = tl.getBoundingClientRect();
        return view.start + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * view.span;
      };
      const follow = (onMove) => {
        const move = (e) => { onMove(e); paint(); };
        const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      };
      lanesHost.addEventListener("pointerdown", () => { previewRange = null; }, true);
      lanesHost.addEventListener("pointerdown", (event) => {
        const lane = event.target.closest(".trim-lane"); if (!lane) return;
        const i = +lane.dataset.index, tl = lane.querySelector(".lane-timeline");
        if (i !== active) { select(i); return; }           // first click on another lane selects it
        const cur = segs[active];
        const handle = event.target.closest(".trim-handle");
        if (handle) {
          event.preventDefault();
          if (handle.dataset.handle === "start") follow((e) => {
            const lowerBound = Math.max(0, cur.end - maxTrim()), upperBound = cur.end - MIN_TRIM_SECONDS;
            cur.start = Math.max(lowerBound, Math.min(timeAt(tl, e), upperBound)); mediaEl.currentTime = cur.start;
          });
          else follow((e) => {
            const upperBound = Math.min(duration, cur.start + maxTrim()), lowerBound = cur.start + MIN_TRIM_SECONDS;
            cur.end = Math.min(upperBound, Math.max(timeAt(tl, e), lowerBound)); mediaEl.currentTime = cur.end;
          });
          return;
        }
        if (event.target.closest(".trim-handle-middle")) {
          // Dotted middle handle: drags the whole window without changing its length.
          event.preventDefault(); event.stopPropagation();
          const st = { pointerTime: timeAt(tl, event), start: cur.start, end: cur.end };
          follow((e) => {
            const width = st.end - st.start;
            const ns = Math.max(0, Math.min(st.start + timeAt(tl, e) - st.pointerTime, duration - width));
            cur.start = ns; cur.end = ns + width; mediaEl.currentTime = cur.start;
          });
          return;
        }
        if (event.target.closest(".lane-label, .lane-time")) return;
        // Clicking the selected range previews it from its start; clicking the
        // empty track outside it scrubs to wherever was clicked.
        mediaEl.currentTime = event.target.closest(".trim-range") ? cur.start : timeAt(tl, event);
      });
      mediaEl.addEventListener("timeupdate", () => {
        paintPlayhead();
        const cur = previewRange || segs[active];
        if (q("#tr-loop").checked && !mediaEl.paused && (mediaEl.currentTime >= cur.end || mediaEl.currentTime < cur.start - 0.25)) mediaEl.currentTime = cur.start;
      });
      mediaEl.addEventListener("seeked", paintPlayhead);
      mediaEl.addEventListener("loadedmetadata", () => { mediaEl.currentTime = segs[active].start; });

      // Keyboard: frame stepping, trim switching, set edges at the playhead.
      const onKey = (e) => {
        if (e.target.closest?.("input:not([type=checkbox]), textarea, select")) return;
        const cur = segs[active], frame = 1 / fps;
        let handled = true;
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          mediaEl.pause();
          const step = (e.shiftKey ? 1 : frame) * (e.key === "ArrowLeft" ? -1 : 1);
          mediaEl.currentTime = Math.max(0, Math.min(duration, (mediaEl.currentTime || 0) + step));
          const v = pct(mediaEl.currentTime); if (v < 0 || v > 100) { centerView(mediaEl.currentTime); paint(); }
        } else if (e.key === "ArrowUp") select(active - 1);
        else if (e.key === "ArrowDown") select(active + 1);
        else if (e.key === " ") { if (mediaEl.paused) mediaEl.play().catch(() => {}); else mediaEl.pause(); }
        else if (e.key === "[") { const t = mediaEl.currentTime; cur.start = Math.max(Math.max(0, cur.end - maxTrim()), Math.min(t, cur.end - MIN_TRIM_SECONDS)); paint(); }
        else if (e.key === "]") { const t = mediaEl.currentTime; cur.end = Math.min(Math.min(duration, cur.start + maxTrim()), Math.max(t, cur.start + MIN_TRIM_SECONDS)); paint(); }
        else handled = false;
        if (handled) { e.preventDefault(); e.stopPropagation(); }
      };
      window.addEventListener("keydown", onKey, true);

      q("#trim-reset").addEventListener("click", () => { segs[active] = {start: 0, end: Math.min(duration, maxTrim())}; mediaEl.currentTime = 0; recenter(); paint(); });
      // ---- ✨ Auto-cut: Hermes scouts the video for good training clips ----
      q("#tr-autocut").addEventListener("click", async () => {
        const panel = q("#tr-ac"); panel.hidden = !panel.hidden;
        if (panel.hidden || q("#ac-model").options.length) return;
        try {
          const m = await api("/api/tagging/models");
          for (const x of m.models) q("#ac-model").append(new Option(`${x.id}${x.slots > 1 ? ` · ${x.slots} slots` : ""}`, x.id));
          q("#ac-model").value = m.models.some((x) => x.id === "qwen-9b") ? "qwen-9b" : (m.default || "");
        } catch (e) { note(e.message, true); }
        let saved = {}; try { saved = JSON.parse(localStorage.getItem(`trainer-tag-${current}`) || "{}"); } catch (_) {}
        q("#ac-concept").value = saved.concept || "";
      });
      const fillLen = () => {
        const sel = q("#ac-len"), cur = sel.value; sel.replaceChildren();
        const vo = new Option("Variable 2–5 s (random)", "var"); vo.title = h3Ready ? "Each clip gets a random length: 56, 73, 90 or 107 frames" : "Each clip gets a random length of 2-5 s";
        sel.append(vo);
        if (h3Ready) for (const f of H3_GRID) sel.append(new Option(`${f} frames (${(f / 24).toFixed(2)}s)`, (f / 24).toFixed(4)));
        else for (const sec of [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]) sel.append(new Option(`${sec} s`, String(sec)));
        sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "var";
      };
      fillLen();
      q("#ac-close").addEventListener("click", () => { q("#tr-ac").hidden = true; player.setScout(null); q("#tr-feed").hidden = true; });
      // Live scout map on the player's scrub bar + a feed of the latest judged windows.
      const feedSeen = new Map();
      // Once scouting is done, "picked" = currently a trim (so double-click edits and manual
      // trim changes stay in sync); while it runs, it is the scout's own live picks.
      const scoutDone = (job) => !["waiting_for_gpu", "preparing", "running"].includes(job.state);
      const segIndexFor = (start, end) => segs.findIndex((sg) => sg.start < end - 0.05 && sg.end > start + 0.05);
      const isPicked = (job, start, end) => (scoutDone(job) ? segIndexFor(start, end) >= 0 : (job.segments || []).some((p) => p.start === start));
      let lastToggle = 0;
      function togglePick(job, v) {
        // Phones can fire both our double-tap and a native dblclick: only act once.
        if (Date.now() - lastToggle < 500) return;
        lastToggle = Date.now();
        const idx = segIndexFor(v.start, v.end);
        if (idx >= 0) {
          if (segs.length === 1) { note("At least one trim has to stay; add another before un-picking this one.", true); return; }
          segs.splice(idx, 1);
          select(Math.min(active, segs.length - 1));
          note(`Moved ${v.start.toFixed(1)}s back to Kept.`);
        } else {
          segs.push({start: v.start, end: Math.min(duration, v.end), score: v.score, subject: v.subject, reason: v.reason});
          segs.sort((a, b) => a.start - b.start);
          clampAll();
          select(segs.findIndex((sg) => sg.start === v.start));
          note(`Picked ${v.start.toFixed(1)}s: it is now trim ${active + 1}.`);
        }
        paintScout(job);
      }
      function paintScout(job) {
        const scanning = Object.entries(job.agents || {}).filter(([, st]) => st.window)
          .map(([a, st]) => ({start: st.window[0], end: st.window[1], color: agentColor(a, job), label: agentShort(a)}));
        if (job.verifying) scanning.push({start: job.verifying[0], end: job.verifying[1], color: "#FFD700", label: "verify"});
        const pickList = scoutDone(job) ? (job.windows || []).filter((v) => isPicked(job, v.start, v.end)) : (job.segments || []);
        player.setScout({shots: job.shots || [], windows: job.plan || [], verdicts: job.windows || [], scanning, picks: pickList});
        const feed = q("#tr-feed"); feed.hidden = false;
        // Order: picked clips, other kept clips (best first), windows being scouted, then skips.
        const rank = (c) => (c.scanning ? 2 : isPicked(job, c.v.start, c.v.end) ? 0 : c.v.keep ? 1 : 3);
        const cards = [...scanning.map((w) => ({w, scanning: true})), ...(job.windows || []).map((v) => ({v}))];
        cards.sort((a, b) => rank(a) - rank(b) || (a.scanning || b.scanning ? 0 : (b.v.score - a.v.score) || (a.v.start - b.v.start)));
        feed.replaceChildren();
        let section = null;
        const heading = {0: "Picked", 1: "Kept", 2: "Scouting now", 3: "Skipped"};
        for (const c of cards) {
          if (rank(c) !== section) { section = rank(c); feed.append(el("div", "fc-head", heading[section])); }
          const start = c.scanning ? c.w.start : c.v.start, end = c.scanning ? c.w.end : c.v.end;
          const card = el("button", "fc" + (c.scanning ? " scanning" : c.v.keep ? " keep" : " skip"));
          card.type = "button";
          if (c.scanning) card.style.setProperty("--ac", c.w.color);
          const img = el("img"); img.loading = "lazy"; img.alt = "";
          img.src = `${base}/api/datasets/${enc(current)}/frame/${enc(file)}?t=${((start + end) / 2).toFixed(2)}&staged=${staged ? "true" : "false"}&w=240`;
          const chip = el("span", "fc-chip", c.scanning ? `${c.w.label} · scouting` : c.v.keep ? `KEEP ${c.v.score.toFixed(0)}` : "SKIP");
          const frames = el("span", "fc-frames", c.scanning ? "" : (c.v.frames || []).map((f) => (f ? "■" : "□")).join(""));
          const t = el("span", "fc-time", `${start.toFixed(1)}s`);
          card.append(img, chip, frames, t);
          if (!c.scanning) {
            const pk = isPicked(job, start, end);
            card.title = `${c.v.keep ? `keep ${c.v.score}/10` : "skip"} — ${c.v.subject ? c.v.subject + ": " : ""}${c.v.reason}${pk ? "\n★ picked" : ""}`
              + (scoutDone(job) && (pk || c.v.keep) ? `\nDouble-click: ${pk ? "move back to Kept" : "pick it (adds a trim)"}` : "");
            if (pk) { card.classList.add("pick"); if (!c.v.keep) chip.textContent = "PICKED"; }
            if (scoutDone(job) && (pk || c.v.keep)) {
              card.classList.add("toggleable");
              card.addEventListener("dblclick", (e) => { e.preventDefault(); togglePick(job, c.v); });
              // Double-tap on touch screens (dblclick is unreliable there).
              let lastTap = 0;
              card.addEventListener("pointerup", (e) => {
                if (e.pointerType === "mouse") return;
                const now = Date.now();
                if (now - lastTap < 350) { e.preventDefault(); togglePick(job, c.v); lastTap = 0; } else lastTap = now;
              });
            }
          }
          card.addEventListener("click", () => {
            // Play this clip in the preview: a picked one selects its trim; any other window
            // loops on its own until a trim is selected again.
            const idx = segs.findIndex((sg) => Math.abs(sg.start - start) < 0.01);
            if (idx >= 0) select(idx);
            else { previewRange = {start, end}; mediaEl.currentTime = start; }
            feed.querySelectorAll(".fc.playing").forEach((x) => x.classList.remove("playing"));
            card.classList.add("playing");
            mediaEl.play().catch(() => {});
          });
          feed.append(card);
        }
      }
      q("#ac-cancel").addEventListener("click", () => { if (ac.job) api(`/api/tag-jobs/${ac.job.id}/cancel`, {method:"POST"}).catch(() => {}); });
      q("#ac-start").addEventListener("click", async () => {
        const btn = q("#ac-start"); btn.disabled = true;
        const lenVal = q("#ac-len").value;
        const body = {model:q("#ac-model").value, concept:q("#ac-concept").value.trim(), count:Math.max(1, Math.min(50, +q("#ac-count").value || 8)),
                      staged:!!staged, h3:h3Ready, scene_mode:q("#ac-scenes").checked,
                      ...(lenVal === "var" ? {variable:true, min_seconds:2, max_seconds:5} : {seconds:+lenVal})};
        try {
          const job = await api(`/api/datasets/${enc(current)}/autocut/${enc(file)}`, {method:"POST", body:JSON.stringify(body)});
          Object.assign(ac, {job, lines: [], next: 0, seen: new Set()});
          q("#ac-term").hidden = false;
          clearInterval(ac.timer);
          const poll = async () => {
            try {
              const r = await api(`/api/tag-jobs/${job.id}/log?after=${ac.next}`);
              ac.job = r.job; ac.next = r.next;
              for (const l of r.lines) { const k = `${l.t}|${l.text}`; if (!ac.seen.has(k)) { ac.seen.add(k); ac.lines.push(l); } }
              const bodyEl = q("#ac-body"); bodyEl.replaceChildren(...ac.lines.map((l) => termLine(l, r.job))); bodyEl.scrollTop = bodyEl.scrollHeight;
              const st = q("#ac-state"); st.textContent = `● ${r.job.state === "complete" ? "done" : r.job.state.replaceAll("_", " ")}`; st.dataset.state = r.job.state;
              paintScout(r.job);
              q("#trim-save").disabled = scouting();
              q("#trim-save").title = scouting() ? "Wait for Hermes to finish scouting (or cancel it)" : "";
              q("#ac-status").textContent = `⚕ ${r.job.model}  │  ${r.job.processed}/${r.job.total || "?"} windows scouted  │  ${r.job.swarm} scout(s)`;
              if (!["waiting_for_gpu", "preparing", "running"].includes(r.job.state)) {
                clearInterval(ac.timer); btn.disabled = false;
                if (r.job.state === "complete" && r.job.segments?.length) {
                  segs = r.job.segments.map((x) => ({start:x.start, end:Math.min(duration, x.end), score:x.score, subject:x.subject, reason:x.reason}));
                  zoom = 0; zoomSel.value = "0";   // show every pick at once
                  clampAll(); select(0);
                  note(`Hermes picked ${segs.length} clip(s). Hover a lane number for the reason; adjust, then Cut.`);
                } else if (r.job.state === "complete") note("Hermes found no good clips; try a different 'Looking for' or a shorter length.", true);
              }
            } catch (e) { clearInterval(ac.timer); btn.disabled = false; }
          };
          poll(); ac.timer = setInterval(poll, 1200);
        } catch (e) { note(e.message, true); btn.disabled = false; }
      });

      q("#tr-add").addEventListener("click", () => {
        const len = Math.min(maxTrim(), Math.max(MIN_TRIM_SECONDS, segs[active].end - segs[active].start));
        const after = Math.max(...segs.map((sg) => sg.end));
        const start = after + len <= duration ? after : Math.min(mediaEl.currentTime || 0, Math.max(0, duration - len));
        segs.push({start, end: Math.min(duration, start + len)});
        select(segs.length - 1);
      });
      q("#tr-remove").addEventListener("click", () => { if (segs.length < 2) return; segs.splice(active, 1); select(Math.min(active, segs.length - 1)); });
      q("#tr-loop").addEventListener("change", (e) => player.setLoop(e.target.checked));
      q("#tr-h3").addEventListener("change", (e) => { h3Ready = e.target.checked; fillLen(); try { localStorage.setItem("trainer-trim-h3", h3Ready ? "1" : "0"); } catch (_) {} clampAll(); paint(); });
      let done = false;
      q("#tr-cancel").addEventListener("click", () => shell.close());
      const scouting = () => ac.job && ["waiting_for_gpu", "preparing", "running"].includes(ac.job.state);
      q("#trim-save").addEventListener("click", async () => {
        const btn = q("#trim-save");
        if (scouting()) { note("Hermes is still scouting this video. Wait for it (or press ■ to cancel it) before cutting.", true); return; }
        if (h3Ready) { const bad = segs.findIndex((sg) => snapFrames(sg.end - sg.start) < 22); if (bad >= 0) { note(`Trim ${bad + 1} is too short for H3.`, true); return; } }
        btn.disabled = true; btn.textContent = "Cutting…";
        try {
          const r = await api(`/api/datasets/${enc(current)}/trim/${enc(file)}?staged=${staged}`, {method:"POST", body:JSON.stringify({segments:segs.map((sg) => ({start:+sg.start.toFixed(3), end:+sg.end.toFixed(3)})), h3_ready:h3Ready})});
          done = true;
          note(`Cut ${r.clips.length} clip(s): ${r.clips.map((c) => c.file).join(", ")}. ${staged ? "Download removed." : "Original removed."}`);
          shell.close();
        } catch (e) { note(e.message, true); btn.disabled = false; paint(); }
      });
      shell.onclose = async () => {
        clearInterval(ac.timer);
        // Don't leave an auto-cut running (or waiting in the GPU queue) for a trimmer nobody is looking at.
        if (scouting()) { api(`/api/tag-jobs/${ac.job.id}/cancel`, {method: "POST"}).catch(() => {}); note("Auto-cut cancelled because the trimmer was closed."); }
        player.destroy(); window.removeEventListener("keydown", onKey, true); await reload(); loadList(); changed(); resolve(done); };
    });
  }

  // ---- batch auto-trim for waiting videos ----
  const BATCH_ACTIVE = ["queued", "running", "review", "cancelling"];
  // Videos the batch finished with (cut, or skipped by you) leave "Waiting to be trimmed" right away.
  function dropHandledStaged(job) {
    const gone = new Set([...Object.keys(job.saved || {}), ...(job.skipped || []).filter((s) => s.removed).map((s) => s.file)]);
    if (!gone.size || !data?.staged) return;
    const before = data.staged.length;
    data.staged = data.staged.filter((st) => !gone.has(st.file));
    if (data.staged.length === before) return;
    $("#ds-staged").querySelectorAll(".staged-item").forEach((row) => { if (gone.has(row.dataset.file)) row.remove(); });
    const title = $("#ds-staged .staged-head strong"); if (title) title.textContent = `Waiting to be trimmed (${data.staged.length})`;
    loadList(); changed();
  }
  function paintBatchTrim() {
    const host = $("#ds-batch-status");
    if (!host) return;
    const job = batchTrim;
    host.hidden = !job;
    if (!job) return;
    dropHandledStaged(job);
    paintBatchReview();   // first, so the file list knows which video is open
    const active = BATCH_ACTIVE.includes(job.state);
    const reviews = job.reviews || [], cutting = job.cutting || [];
    const reviewFiles = new Set(reviews.map((r) => r.file));
    host.dataset.state = job.state;
    const stateText = {queued:"Queued", running:"In progress", review:"Needs your review", cancelling:"Cancelling", complete:"Finished", cancelled:"Cancelled"};
    host.querySelector(".ds-batch-state").textContent = active && reviews.length && job.state === "running"
      ? `In progress · ${reviews.length} to review` : (stateText[job.state] || job.state);
    host.querySelector(".ds-batch-mode").textContent = job.scene_mode ? "Scene cuts" : "Timeline scan";
    const saved = job.saved || {};
    const skipped = new Map((job.skipped || []).map((item) => [item.file, item.reason]));
    host.querySelector("[data-batch-metric=videos]").textContent = `${job.done}/${job.total}`;
    host.querySelector("[data-batch-metric=clips]").textContent = String(job.clips);
    host.querySelector("[data-batch-metric=review]").textContent = String(reviews.length);
    host.querySelector("[data-batch-metric=left]").textContent = String((job.skipped || []).filter((s) => !s.removed).length);
    // Scouted-but-unreviewed videos count as 70 % done, ones being cut as 86 %.
    const scoutFraction = job.current && active ? 0.6 * ({queue:0.07, scouting:0.2 + 0.8 * Math.min(1, (job.scout_progress?.processed || 0) / Math.max(1, job.scout_progress?.total || 1))}[job.phase] || 0) : 0;
    const progress = host.querySelector(".ds-batch-overall-progress");
    progress.max = Math.max(1, job.total);
    progress.value = Math.min(job.total, job.done + reviews.length * 0.7 + cutting.length * 0.86 + scoutFraction);
    host.querySelector(".ds-batch-percent").textContent = `${Math.round(progress.value / Math.max(1, job.total) * 100)}%`;
    const busy = [job.current && job.phase === "queue", job.current && job.phase === "scouting", reviews.length > 0, cutting.length > 0];
    host.querySelectorAll(".ds-batch-step").forEach((step, index) => {
      step.classList.toggle("done", job.state === "complete" || (index < 2 && job.scouting_done));
      step.classList.toggle("current", active && busy[index]);
    });
    const currentPanel = host.querySelector(".ds-batch-current");
    currentPanel.hidden = !active;
    if (active) {
      host.querySelector(".ds-batch-current-file").textContent = job.current
        ? `Scouting ${Math.min(job.scouted + 1, job.total)} of ${job.total} · ${job.current}`
        : job.scouting_done ? `All ${job.total} videos scouted` : "Starting";
      host.querySelector(".ds-batch-current-stage").textContent = job.stage || "Working";
      const scout = host.querySelector(".ds-batch-scout-progress");
      const scouting = job.current && job.phase === "scouting" && job.scout_progress?.total > 0;
      scout.hidden = !scouting;
      if (scouting) {
        scout.querySelector("progress").max = job.scout_progress.total;
        scout.querySelector("progress").value = job.scout_progress.processed;
        scout.querySelector("span").textContent = `${job.scout_progress.processed}/${job.scout_progress.total} windows checked`;
      }
      const reviewCall = host.querySelector(".ds-batch-review-jump");
      reviewCall.hidden = !reviews.length || job.cancel_requested;
      reviewCall.textContent = `Review ${reviews.length} ready ↓`;
    }
    const list = host.querySelector(".ds-batch-files");
    const files = job.files || [];
    if (list.dataset.jobId !== job.id || list.children.length !== files.length) {
      list.dataset.jobId = job.id;
      list.replaceChildren(...files.map((file, index) => {
        const row = el("div", "ds-batch-file"); row.dataset.file = file;
        row.append(el("span", "ds-batch-file-number", String(index + 1)), el("span", "ds-batch-file-name", file),
          el("span", "ds-batch-file-state"));
        row.addEventListener("click", () => {
          if (!(batchTrim?.reviews || []).some((r) => r.file === file)) return;
          reviewPick = file; paintBatchReview();
          $("#ds-batch-review")?.scrollIntoView({behavior:"smooth", block:"start"});
        });
        return row;
      }));
    }
    list.querySelectorAll(".ds-batch-file").forEach((row) => {
      const file = row.dataset.file;
      let status = "Queued", kind = "queued";
      row.title = "";
      if (Object.hasOwn(saved, file)) { status = `${saved[file]} clip${saved[file] === 1 ? "" : "s"} saved`; kind = "saved"; }
      else if (skipped.has(file)) {
        const removed = (job.skipped || []).some((s) => s.file === file && s.removed);
        status = removed ? "Skipped · removed" : "Left waiting"; kind = "skipped"; row.title = skipped.get(file);
      }
      else if (cutting.includes(file)) { status = "Cutting"; kind = "cutting"; }
      else if (reviewFiles.has(file)) {
        kind = "review"; status = file === batchReview?.file ? "Reviewing" : "Ready to review"; row.title = "Click to review this video now";
      }
      else if (file === job.current && active) {
        kind = job.state === "cancelling" ? "cancelling" : job.phase || "running";
        status = {queue:"GPU queue", scouting:"Scouting", cancelling:"Stopping"}[kind] || "Working";
      } else if (job.state === "cancelled" || job.cancel_requested) { status = "Stopped"; kind = "cancelled"; }
      row.dataset.status = kind;
      row.querySelector(".ds-batch-file-state").textContent = status;
    });
    const skip = host.querySelector(".ds-batch-skips");
    const left = (job.skipped || []).filter((s) => !s.removed);
    skip.textContent = left.length ? `${left.length} left in Waiting to be trimmed: ${left.map((s) => `${s.file} (${s.reason})`).join("; ")}` : "";
    const cancel = host.querySelector("#ds-batch-cancel");
    cancel.hidden = !active;
    cancel.disabled = job.state === "cancelling";
    const start = $("#ds-batch-start"); if (start) start.disabled = active;
    const trimAll = $("#ds-trim-all"); if (trimAll) trimAll.disabled = active;
    $("#ds-staged").querySelectorAll(".staged-item").forEach((row) => {
      const file = row.dataset.file;
      const inBatch = active && (job.current === file || cutting.includes(file) || reviewFiles.has(file));
      row.querySelectorAll("button").forEach((button) => {
        if (button.dataset.originalTitle === undefined) button.dataset.originalTitle = button.title;
        button.disabled = inBatch;
        button.title = inBatch ? "Auto-trim is handling this video (review it above, or cancel the batch)" : button.dataset.originalTitle;
      });
    });
  }
  function paintBatchReview() {
    const host = $("#ds-batch-review");
    if (!host) return;
    const job = batchTrim;
    const reviews = job && BATCH_ACTIVE.includes(job.state) && !job.cancel_requested ? (job.reviews || []) : [];
    // Show the video you picked if it is still waiting, else the one you were on, else the oldest scouted.
    const review = reviews.find((r) => r.file === reviewPick) || reviews.find((r) => r.file === batchReview?.file) || reviews[0];
    reviewPick = null;
    if (!review) {
      if (!host.hidden) { host.querySelector("video")?.pause(); host.replaceChildren(); }
      host.hidden = true; batchReview = null; return;
    }
    const key = `${job.id}:${review.file}`;
    host.hidden = false;
    const position = reviews.indexOf(review);
    if (batchReview?.key === key && host.hasChildNodes()) {
      host.querySelector(".ds-batch-review-pos").textContent = `${position + 1} of ${reviews.length} ready`;
      host.querySelectorAll(".ds-batch-review-nav button").forEach((b) => { b.disabled = reviews.length < 2; });
      return;
    }
    const savedPicks = batchReview?.key === key ? batchReview.picks : new Set(review.choices.flatMap((choice, index) => choice.picked ? [index] : []));
    batchReview = {key, file:review.file, picks:new Set(savedPicks), previewEnd:0, focus:null, keys:null};
    host.replaceChildren();
    const head = el("div", "ds-batch-review-head");
    head.append(el("strong", null, `Review ${review.file}`));
    const nav = el("div", "ds-batch-review-nav");
    const prev = el("button", "button secondary", "‹ Prev"), next = el("button", "button secondary", "Next ›");
    prev.type = next.type = "button";
    const go = (step) => {
      const list = batchTrim?.reviews || []; if (list.length < 2) return;
      const at = list.findIndex((r) => r.file === batchReview?.file);
      reviewPick = list[(at + step + list.length) % list.length].file; paintBatchReview();
    };
    prev.addEventListener("click", () => go(-1)); next.addEventListener("click", () => go(1));
    nav.append(prev, el("span", "ds-batch-review-pos muted", `${position + 1} of ${reviews.length} ready`), next);
    head.append(nav); host.append(head);
    host.append(el("p", "muted", "Hermes keeps scouting the other videos while you review. Click a clip to preview it, double-click to move it between Kept and Picked, then cut only the Picked clips — the next scouted video opens right after."),
      el("p", "muted ds-batch-keys", "Keys: ←/→/↑/↓ move between clips · Space or Enter Kept ⇄ Picked · Shift+←/→ previous/next video · C cut & next"));
    const video = el("video", "ds-batch-video"); video.controls = true; video.preload = "metadata";
    video.src = `${base}/api/datasets/${enc(current)}/media/${enc(review.file)}?staged=true`;
    video.addEventListener("timeupdate", () => {
      if (batchReview?.key === key && batchReview.previewEnd && video.currentTime >= batchReview.previewEnd) {
        video.pause(); batchReview.previewEnd = 0;
      }
    });
    host.append(video);
    const count = el("div", "ds-batch-pick-count"); host.append(count);
    const grid = el("div", "ds-batch-choices"); host.append(grid);
    const controls = el("div", "ds-batch-review-actions");
    const approve = el("button", "button primary", "Cut picked clips & next"); approve.type = "button";
    const skip = el("button", "button secondary", "Skip & remove this video"); skip.type = "button";
    skip.title = "Removes the video from Waiting to be trimmed without cutting anything";
    controls.append(approve, skip); host.append(controls);
    nav.querySelectorAll("button").forEach((b) => { b.disabled = reviews.length < 2; });
    const update = () => {
      count.textContent = `${batchReview.picks.size} Picked · ${review.choices.length - batchReview.picks.size} Kept`;
      approve.disabled = !batchReview.picks.size;
      grid.querySelectorAll(".ds-batch-choice").forEach((card) => {
        const picked = batchReview.picks.has(Number(card.dataset.index));
        card.classList.toggle("picked", picked);
        card.classList.toggle("focused", batchReview.focus === Number(card.dataset.index));
        card.querySelector(".ds-batch-choice-state").textContent = picked ? "Picked" : "Kept";
        card.setAttribute("aria-pressed", String(picked));
      });
    };
    const playChoice = (choice) => {
      const play = () => { video.currentTime = choice.start; batchReview.previewEnd = choice.end; video.play().catch(() => {}); };
      if (video.readyState >= 1) play(); else video.addEventListener("loadedmetadata", play, {once:true});
    };
    const togglePick = (index) => {
      if (batchReview.picks.has(index)) batchReview.picks.delete(index);
      else if (batchReview.picks.size < 50) batchReview.picks.add(index);
      else { note("A review can contain at most 50 clips.", true); return; }
      update();
    };
    // Keyboard: arrows move between cards (and preview them), Space/Enter = Kept ⇄ Picked,
    // Shift+←/→ = previous/next video, C = cut picked & next.
    batchReview.keys = (e) => {
      const cards = [...grid.querySelectorAll(".ds-batch-choice")];
      if (!cards.length) return false;
      if (e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) { go(e.key === "ArrowLeft" ? -1 : 1); return true; }
      const cols = cards.filter((c) => c.offsetTop === cards[0].offsetTop).length || 1;
      const moves = {ArrowRight:1, ArrowLeft:-1, ArrowDown:cols, ArrowUp:-cols};
      if (e.key in moves) {
        const at = batchReview.focus ?? -1;
        batchReview.focus = at < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, at + moves[e.key]));
        update(); cards[batchReview.focus].scrollIntoView({block:"nearest"});
        playChoice(review.choices[batchReview.focus]);
        return true;
      }
      if ((e.key === " " || e.key === "Enter") && batchReview.focus != null) { togglePick(batchReview.focus); return true; }
      if ((e.key === "c" || e.key === "C") && !approve.disabled) { approve.click(); return true; }
      return false;
    };
    review.choices.forEach((choice, index) => {
      const card = el("div", "ds-batch-choice"); card.dataset.index = String(index);
      const preview = el("button", "ds-batch-preview"); preview.type = "button";
      preview.title = `Preview ${choice.start.toFixed(1)}–${choice.end.toFixed(1)} seconds`;
      const thumb = el("img"); thumb.loading = "lazy"; thumb.alt = "";
      thumb.src = `${base}/api/datasets/${enc(current)}/frame/${enc(review.file)}?t=${((choice.start + choice.end) / 2).toFixed(2)}&staged=true&w=240`;
      preview.append(thumb, el("span", null, `${choice.start.toFixed(1)}–${choice.end.toFixed(1)}s · score ${Number(choice.score).toFixed(0)}/10`));
      preview.tabIndex = -1;
      const state = el("span", "ds-batch-choice-state");
      card.title = "Click to preview · double-click to move between Kept and Picked";
      // One click previews; a second within 350 ms (mouse or touch) toggles Kept ⇄ Picked.
      let lastTap = 0;
      card.addEventListener("pointerup", (e) => {
        if (e.button > 0) return;
        const now = performance.now();
        batchReview.focus = index;
        if (now - lastTap < 350) { lastTap = 0; togglePick(index); return; }
        lastTap = now; update(); playChoice(choice);
      });
      card.addEventListener("dblclick", (e) => e.preventDefault());
      card.append(preview, state, el("span", "ds-batch-reason", choice.reason || choice.subject || "Hermes kept this clip"));
      grid.append(card);
    });
    const submit = async (skipVideo) => {
      approve.disabled = true; skip.disabled = true;
      try {
        const body = skipVideo ? {file:review.file, skip:true} : {file:review.file, picks:[...batchReview.picks].sort((a,b) => a-b)};
        batchTrim = (await api(`/api/datasets/${enc(current)}/autotrim-all/review`, {method:"POST", body:JSON.stringify(body)})).job;
        paintBatchTrim();
        pollBatchTrim();
      } catch (e) { note(e.message, true); approve.disabled = !batchReview?.picks.size; skip.disabled = false; pollBatchTrim(); }
    };
    approve.addEventListener("click", () => submit(false));
    skip.addEventListener("click", () => submit(true));
    update();
  }
  async function pollBatchTrim() {
    if (!current) return;
    const dataset = current;
    try {
      const response = await api(`/api/datasets/${enc(dataset)}/autotrim-all`);
      if (current !== dataset) return;
      const wasActive = batchTrim && BATCH_ACTIVE.includes(batchTrim.state);
      // Keep a cancellation result on this page, then omit it when the dataset is opened again.
      const incoming = response.job;
      batchTrim = !batchTrim && (incoming?.state === "cancelled" || incoming?.cancel_requested) ? null : incoming;
      if (batchTrim && !$("#ds-batch-status")) renderStaged();
      paintBatchTrim();
      const active = batchTrim && BATCH_ACTIVE.includes(batchTrim.state);
      if (active && !batchTrimTimer) batchTrimTimer = setInterval(pollBatchTrim, 2000);
      if (!active && batchTrimTimer) { clearInterval(batchTrimTimer); batchTrimTimer = null; }
      if (wasActive && !active) { await reload(); loadList(); changed(); }
    } catch (e) { note(e.message, true); }
  }
  function resumeBatchTrim() { pollBatchTrim(); }

  // ---- staged downloads (waiting to be trimmed) ----
  function renderStaged() {
    const host = $("#ds-staged"); host.replaceChildren();
    const list = data.staged || [];
    if (!list.length && !batchTrim) return;
    const head = el("div", "staged-head");
    head.append(el("strong", null, `Waiting to be trimmed (${list.length})`), el("span", "muted", "YouTube downloads and held uploads. Trim them into clips, add them as they are, or discard."));
    if (list.length) {
      const all = el("button", "button secondary", "✂ Trim all"); all.id = "ds-trim-all"; all.title = "Open the trimmer for each waiting video, one after another";
      all.addEventListener("click", () => { for (const st of list) queueTrim(st.file, true); });
      head.append(all);
      const auto = el("button", "button secondary", "✨ Auto-trim all"); auto.id = "ds-batch-start";
      auto.title = "Have Hermes find training clips in each waiting video, then review and approve the cuts";
      auto.addEventListener("click", async () => {
        const form = $("#ds-batch-form"); form.hidden = !form.hidden;
        if (form.hidden || form.dataset.loaded) return;
        try {
          const models = await api("/api/tagging/models");
          const select = form.querySelector("select");
          for (const model of models.models) select.append(new Option(model.id, model.id));
          select.value = models.models.some((x) => x.id === "qwen-9b") ? "qwen-9b" : (models.default || models.models[0]?.id || "");
          let saved = {}; try { saved = JSON.parse(localStorage.getItem(`trainer-tag-${current}`) || "{}"); } catch (_) {}
          form.elements.concept.value = saved.concept || "";
          form.dataset.loaded = "1";
        } catch (e) { note(e.message, true); }
      });
      head.append(auto);
    }
    host.append(head);
    const config = el("form", "ds-batch-form"); config.id = "ds-batch-form"; config.hidden = true;
    config.innerHTML = `<label>Vision model <select name="model" required></select></label><label>Looking for <input name="concept" maxlength="300" placeholder="Main subject of the video"></label><label>Clips per video <input name="count" type="number" min="1" max="50" value="8" required></label><label class="ds-batch-scene" title="On: detect scene cuts and scout one or two windows per shot. Off: scan throughout the video timeline."><input name="scene_mode" type="checkbox"> Scout by scene cuts <span class="muted">1–2 windows per shot</span></label><button class="button primary" type="submit">Start auto-trim</button><span class="muted">Timeline scan checks the whole video. Scene mode samples detected shots. Review picks before cutting.</span>`;
    try { config.elements.scene_mode.checked = localStorage.getItem("trainer-autocut-scenes") === "1"; } catch (_) {}
    config.elements.scene_mode.addEventListener("change", () => { try { localStorage.setItem("trainer-autocut-scenes", config.elements.scene_mode.checked ? "1" : "0"); } catch (_) {} });
    config.addEventListener("submit", async (event) => {
      event.preventDefault();
      const dataset = current, button = config.querySelector("button"); button.disabled = true;
      try {
        const job = await api(`/api/datasets/${enc(dataset)}/autotrim-all`, {method:"POST", body:JSON.stringify({model:config.elements.model.value, concept:config.elements.concept.value.trim(), count:Number(config.elements.count.value), scene_mode:config.elements.scene_mode.checked})});
        if (current !== dataset) return;
        batchTrim = job.job; config.hidden = true; paintBatchTrim();
        if (!batchTrimTimer) batchTrimTimer = setInterval(pollBatchTrim, 2000);
        note(`Auto-trim started for ${batchTrim.total} waiting video${batchTrim.total === 1 ? "" : "s"}.`);
      } catch (e) { note(e.message, true); } finally { button.disabled = false; }
    });
    host.append(config);
    const status = el("section", "ds-batch-status"); status.id = "ds-batch-status";
    status.setAttribute("aria-label", "Auto-trim batch progress");
    const top = el("div", "ds-batch-top");
    top.append(el("strong", null, "✨ Auto-trim batch"), el("span", "ds-batch-state"), el("span", "ds-batch-mode"));
    const cancel = el("button", "button secondary danger", "Cancel batch"); cancel.id = "ds-batch-cancel"; cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { batchTrim = (await api(`/api/datasets/${enc(current)}/autotrim-all/cancel`, {method:"POST"})).job; paintBatchTrim(); }
      catch (e) { note(e.message, true); cancel.disabled = false; }
    });
    top.append(cancel); status.append(top);
    const metrics = el("div", "ds-batch-metrics");
    for (const [key, label] of [["videos", "Videos handled"], ["clips", "Clips saved"], ["review", "Ready to review"], ["left", "Left waiting"]]) {
      const metric = el("div", "ds-batch-metric"); metric.append(el("strong", null, "0"), el("span", null, label));
      metric.querySelector("strong").dataset.batchMetric = key;
      metrics.append(metric);
    }
    status.append(metrics);
    const overall = el("div", "ds-batch-overall");
    const progress = el("progress", "ds-batch-overall-progress"); progress.setAttribute("aria-label", "Batch progress");
    overall.append(progress, el("span", "ds-batch-percent", "0%")); status.append(overall);
    const steps = el("div", "ds-batch-steps");
    for (const label of ["GPU queue", "Scout clips", "Review picks", "Cut & save"]) steps.append(el("span", "ds-batch-step", label));
    status.append(steps);
    const currentBox = el("div", "ds-batch-current");
    currentBox.append(el("strong", "ds-batch-current-file"), el("span", "ds-batch-current-stage"));
    const scoutProgress = el("div", "ds-batch-scout-progress");
    const scoutBar = el("progress"); scoutBar.setAttribute("aria-label", "Hermes scouting progress");
    scoutProgress.append(scoutBar, el("span")); currentBox.append(scoutProgress);
    const reviewJump = el("button", "button primary ds-batch-review-jump", "Review clips ↓"); reviewJump.type = "button";
    reviewJump.addEventListener("click", () => $("#ds-batch-review")?.scrollIntoView({behavior:"smooth", block:"start"}));
    currentBox.append(reviewJump); status.append(currentBox);
    status.append(el("div", "ds-batch-files"), el("div", "ds-batch-skips"));
    host.append(status);
    const review = el("div", "ds-batch-review"); review.id = "ds-batch-review"; review.hidden = true; host.append(review);
    paintBatchTrim();
    const grid = el("div", "staged-list");
    for (const st of list) {
      const row = el("div", "staged-item"); row.dataset.file = st.file;
      const img = el("img"); img.loading = "lazy"; img.alt = "";
      img.src = `${base}/api/datasets/${enc(current)}/frame/${enc(st.file)}?t=${Math.min(5, (st.info?.duration || 2) / 2).toFixed(2)}&staged=true&w=240`;
      const thumb = el("div", "staged-thumb"); thumb.append(img);
      if (st.info?.duration) thumb.append(el("span", "staged-dur", `${Math.floor(st.info.duration / 60)}:${String(Math.round(st.info.duration % 60)).padStart(2, "0")}`));
      const body = el("div", "staged-body");
      const nm = el("strong", null, st.file); nm.title = st.file;
      const i = st.info || {};
      body.append(nm, el("span", "ds-info", [i.width && `${i.width}×${i.height}`, i.fps && `${i.fps} fps`, st.size_mb != null && `${st.size_mb} MB`].filter(Boolean).join(" · ")));
      const btns = el("div", "staged-btns");
      const t = el("button", "button secondary", "✂ Trim"); t.addEventListener("click", () => queueTrim(st.file, true));
      const acc = el("button", "button secondary", "Add as-is"); acc.title = "Put it in the dataset untrimmed (convert it to the H3 spec afterwards if needed)";
      acc.addEventListener("click", async () => { try { await api(`/api/datasets/${enc(current)}/staged/${enc(st.file)}/accept`, {method:"POST"}); await reload(); loadList(); changed(); } catch (e) { note(e.message, true); } });
      const d = el("button", "button secondary danger", "Discard");
      d.addEventListener("click", async () => { if (!confirm(`Delete ${st.file}?`)) return; await api(`/api/datasets/${enc(current)}/staged/${enc(st.file)}`, {method:"DELETE"}).catch((e) => note(e.message, true)); reload(); });
      btns.append(t, acc, d); body.append(btns);
      row.append(thumb, body); grid.append(row);
    }
    host.append(grid);
    paintBatchTrim();
  }

  // ---- YouTube ----
  const isYoutube = (u) => { try { return /(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$/i.test(new URL(u).hostname); } catch (_) { return false; } };
  $("#ds-youtube").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.currentTarget, url = form.elements.url.value.trim();
    if (!current || !url) return;
    if (!isYoutube(url)) {
      // Any other site: Hermes fetches it with the clip-finder tools (age gates, JavaScript players).
      const btn = form.querySelector("button"); btn.disabled = true;
      try { if (await fetchWithHermes(url)) form.reset(); } finally { btn.disabled = false; }
      return;
    }
    const row = el("div", "ds-upload"), bar = el("div", "pbar"), fill = el("div", "pbar-fill"), text = el("span", "pbar-text", "YouTube · starting…");
    bar.append(fill, text); row.append(bar); $("#ds-uploads").append(row);
    const btn = form.querySelector("button"); btn.disabled = true;
    try {
      let t = await api(`/api/datasets/${enc(current)}/youtube`, {method:"POST", body:JSON.stringify({url, force: form.dataset.force === url})});
      form.reset(); delete form.dataset.force;
      if (t.state === "duplicate") {
        const pv = t.previous || {};
        text.textContent = `Already downloaded → ${pv.path || t.file} (${when(pv)}${pv.exists ? "" : ", since trimmed/removed"})`; bar.classList.add("done");
        const again = el("button", "button secondary", "Download again");
        again.addEventListener("click", () => { row.remove(); form.elements.url.value = url; form.dataset.force = url; form.requestSubmit(); });
        row.append(again);
        return;
      }
      while (!["done", "failed"].includes(t.state)) {
        await new Promise((r) => setTimeout(r, 1000));
        t = await api(`/api/downloads/${t.id}`);
        fill.style.width = `${Math.round(t.percent || 0)}%`;
        text.textContent = `${t.title || "YouTube"} · ${t.state === "merging" ? "merging…" : `${Math.round(t.percent || 0)}%`}`;
      }
      if (t.state === "failed") throw new Error(t.error || "Download failed");
      text.textContent = `${t.title || t.file} · downloaded`; bar.classList.add("done"); setTimeout(() => row.remove(), 4000);
      await reload();
      const st = (data.staged || []).find((x) => x.file === t.file);
      if (st) queueTrim(st.file, true);
    } catch (e) { text.textContent = `YouTube · ${e.message}`; bar.classList.add("failed"); }
    finally { btn.disabled = false; }
  });



  // ---- Hermes-style terminal pane (split view while auto-tagging) ----
  const term = {job: null, lines: [], next: 0, seen: new Set(), timer: null, pinned: true, follow: true, followed: null,
                followAgent: null, followSince: 0, followDwell: 15000};
  const tagActive = () => term.kind !== "find" && term.job && ["waiting_for_gpu", "preparing", "running"].includes(term.job.state) && !$("#ds-term").hidden;
  // Follow mode: keep the clip being tagged on screen until the user scrolls the page themselves.
  // Gentle: only scroll when the card leaves the comfortable middle of the screen, and glide there.
  // With several agents, watch one at a time and move on to another every 10-20 s.
  let glide = null;
  function smoothScrollTo(y) {
    if (glide) cancelAnimationFrame(glide.raf);
    const from = scrollY, to = Math.max(0, Math.min(y, document.documentElement.scrollHeight - innerHeight));
    const dist = Math.abs(to - from); if (dist < 4) return;
    const dur = Math.min(2000, Math.max(800, 500 + dist * 0.6));
    const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);   // easeInOutCubic
    const start = performance.now();
    glide = {raf: 0};
    const step = (now) => {
      if (!term.follow) { glide = null; return; }          // the user took over: stop immediately
      const t = Math.min(1, (now - start) / dur);
      window.scrollTo(0, from + (to - from) * ease(t));
      if (t < 1) glide.raf = requestAnimationFrame(step); else glide = null;
    };
    glide.raf = requestAnimationFrame(step);
  }
  function glideToCard(card, force = false) {
    const r = card.getBoundingClientRect(), vh = innerHeight;
    const comfortable = r.top > vh * 0.15 && r.bottom < vh * 0.85;
    if (comfortable && !force) return;
    smoothScrollTo(scrollY + r.top + r.height / 2 - vh * 0.42);
  }
  function pickFollowAgent(job) {
    const working = Object.entries(job.agents || {}).filter(([, st]) => st.current).map(([a]) => a);
    if (working.length <= 1) {
      if (working[0] && working[0] !== term.followAgent) { term.followAgent = working[0]; term.followSince = Date.now(); term.followDwell = 10000 + Math.random() * 10000; }
      return term.followAgent;
    }
    const now = Date.now();
    // An agent is briefly idle between clips: only give up on it after 3 s without work.
    if (term.followAgent && !working.includes(term.followAgent)) {
      term.followIdle = term.followIdle || now;
      if (now - term.followIdle < 3000) return term.followAgent;
    } else term.followIdle = 0;
    const stale = !term.followAgent || !working.includes(term.followAgent) || now - term.followSince > term.followDwell;
    if (stale) {
      const others = working.filter((a) => a !== term.followAgent);
      term.followAgent = others[Math.floor(Math.random() * others.length)] || working[0];
      term.followSince = now; term.followDwell = 10000 + Math.random() * 10000;
    }
    return term.followAgent;
  }
  function followCurrent(force = false) {
    const job = term.job;
    $("#hterm-follow").hidden = term.follow || !tagActive();
    const label = $("#hterm-following");
    if (!term.follow || !tagActive()) { label.hidden = true; return; }
    const agent = pickFollowAgent(job);
    const file = agent ? (job.agents[agent].current || term.followed) : job.current;
    const multi = Object.keys(job.agents || {}).length > 1;
    label.hidden = !(multi && agent);
    if (multi && agent) { label.textContent = `👁 following ${agentShort(agent)}`; label.style.setProperty("--ac", agentColor(agent)); }
    if (!file) return;
    if (!force && file === term.followed) return;
    const card = document.querySelector(`.ds-item[data-file="${CSS.escape(file)}"]`);
    if (!card) return;
    term.followed = file;
    glideToCard(card, force);
  }
  function pauseFollow(e) {
    if (!term.follow || !tagActive()) return;
    if (e.target?.closest?.(".hterm-body, .ds-modal")) return;   // scrolling inside the terminal is fine
    if (e.type === "keydown") {
      if (e.target.closest?.("input, textarea, select")) return;
      if (!["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) return;
    }
    term.follow = false; $("#hterm-follow").hidden = false; $("#hterm-following").hidden = true;
    if (glide) { cancelAnimationFrame(glide.raf); glide = null; }
  }
  window.addEventListener("wheel", pauseFollow, {passive: true});
  window.addEventListener("touchmove", pauseFollow, {passive: true});
  window.addEventListener("keydown", pauseFollow, true);
  // Dragging the page scrollbar: a pointerdown on the root's scrollbar area (outside the content box).
  window.addEventListener("pointerdown", (e) => { if (e.clientX >= document.documentElement.clientWidth) pauseFollow(e); }, true);
  $("#hterm-follow").addEventListener("click", () => { term.follow = true; term.followed = null; followCurrent(true); });
  // Clicking anywhere in the terminal (except its close button) also resumes follow mode.
  $("#ds-term").addEventListener("pointerdown", (e) => {
    if (e.target.closest("#hterm-close, #hterm-follow")) return;
    if (!term.follow) { term.follow = true; term.followed = null; followCurrent(true); }
  });
  const hhmmss = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
  const AGENT_COLORS = ["#4dabf7", "#e599f7", "#69db7c", "#ffa94d", "#ff8787", "#3bc9db", "#ffd43b", "#b197fc"];
  const agentColor = (a, job = term.job) => AGENT_COLORS[(job?.agents?.[a]?.color ?? 0) % AGENT_COLORS.length];
  const agentShort = (a) => a.replace(/^agent-/, "a");
  function termLine(l, job = term.job) {
    const row = el("div", `hl hl-${l.hermes ? "hermes" : l.kind}`);
    row.append(el("span", "hl-time", hhmmss(l.t)));
    const body = el("span", "hl-body");
    if (l.agent && job && Object.keys(job.agents || {}).length > 1) {
      const tag = el("span", "hl-agent", agentShort(l.agent)); tag.style.setProperty("--ac", agentColor(l.agent, job)); body.append(tag, document.createTextNode(" "));
    }
    if (l.hermes) { body.append(el("span", "hl-tag", "hermes"), document.createTextNode(" " + l.text)); }
    else if (l.kind === "think") { body.append(el("span", "hl-dim", "∴ "), el("span", "hl-think-text", l.text)); row.title = "Hermes' reasoning (click to expand)"; row.addEventListener("click", () => row.classList.toggle("open")); }
    else if (l.kind === "say") body.append(el("span", "hl-brand", "☤ "), el("span", "hl-say-text", l.text));
    else if (l.kind === "shot" && term.job) {
      const src = `${base}/api/finds/${term.job.id}/shots/${l.shot}`;
      body.append(el("span", "hl-dim", "📸 "), document.createTextNode(l.text || l.url));
      const img = el("img", "hl-shot-img"); img.src = src; img.alt = `Screenshot of ${l.url}`; img.title = `${l.url}\nClick to enlarge`;
      img.addEventListener("click", (e) => { e.stopPropagation(); showShot(src, l); });
      body.append(el("br"), img);
    }
    else if (l.kind === "user") body.append(el("span", "hl-prompt", "❯ "), document.createTextNode(l.text));
    else if (l.kind === "tool") {
      const [name, ...rest] = l.text.split("  ");
      body.append(el("span", "hl-bar", "┊ "), document.createTextNode(`${l.icon || "⚙"} `), el("span", "hl-tool", name), document.createTextNode("  " + rest.join("  ")));
      if (l.ms != null) body.append(el("span", "hl-dim", `  ${l.ms} ms`));
    }
    else if (l.kind === "assistant") body.append(el("span", "hl-brand", "☤ "), document.createTextNode(l.text));
    else if (l.kind === "ok") body.append(el("span", "hl-ok", "✓ "), document.createTextNode(l.text));
    else if (l.kind === "warn") body.append(el("span", "hl-warn", "⚠ "), document.createTextNode(l.text));
    else if (l.kind === "error") body.append(el("span", "hl-err", "✗ "), document.createTextNode(l.text));
    else body.append(el("span", "hl-dim", "· "), document.createTextNode(l.text));
    row.append(body);
    if (l.candidate != null) { row.classList.add("hl-link"); row.title = "Show this video"; row.addEventListener("click", () => flashCandidate(l.candidate)); }
    return row;
  }
  function paintTerm() {
    const job = term.job; if (!job) return;
    if (term.kind === "find") return paintFindTerm();
    const body = $("#hterm-body");
    const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 12;
    body.replaceChildren(...term.lines.map(termLine));
    const active = ["waiting_for_gpu", "preparing", "running"].includes(job.state);
    if (active) { const cur = el("div", "hl hl-cursor"); cur.append(el("span", "hl-time", ""), el("span", "hl-body", "")); cur.lastChild.append(el("span", "hl-spin", "⠋"), document.createTextNode(` ${job.stage}${job.current ? ` · ${job.current}` : ""}`)); body.append(cur); }
    if (atBottom || term.pinned) body.scrollTop = body.scrollHeight;
    term.pinned = false;
    $("#hterm-sub").textContent = ` · dataset-tagging · ${job.dataset}${job.source === "hermes" ? " · started by Hermes" : ""}`;
    const st = $("#hterm-state"); st.textContent = active ? "● running" : job.state === "complete" ? "● done" : `● ${job.state}`; st.dataset.state = job.state;
    const elapsed = Math.max(0, (Date.now() / 1000) - job.created);
    const agents = Object.entries(job.agents || {});
    const status = $("#hterm-status"); status.replaceChildren(document.createTextNode(`⚕ ${job.model}  │  ${job.done}/${job.total} captioned${job.failed ? `  │  ${job.failed} failed` : ""}  │  ${Math.floor(elapsed / 60)}:${String(Math.floor(elapsed % 60)).padStart(2, "0")} elapsed`));
    if (agents.length > 1) {
      status.append(document.createTextNode("  │  swarm "));
      for (const [a, st] of agents) { const t = el("span", "hl-agent", `${agentShort(a)} ${st.done}`); t.style.setProperty("--ac", agentColor(a)); status.append(t, document.createTextNode(" ")); }
    }
    document.querySelectorAll(".ds-item.tag-current").forEach((c) => { c.classList.remove("tag-current"); c.style.removeProperty("--ac"); c.querySelector(".agent-badge")?.remove(); });
    // Clips this run already captioned: a dimmed border in the colour of the agent that tagged
    // them (dim gold when a single agent ran).
    const tagged = job.tagged || {};
    const multiAgent = Object.keys(job.agents || {}).length > 1;
    document.querySelectorAll(".ds-item").forEach((c) => {
      const f = c.dataset.file, done = Object.prototype.hasOwnProperty.call(tagged, f);
      c.classList.toggle("tag-done", done);
      if (done && multiAgent && tagged[f]) c.style.setProperty("--dc", agentColor(tagged[f])); else c.style.removeProperty("--dc");
    });
    if (active) {
      const working = agents.filter(([, st]) => st.current).map(([a, st]) => [a, st.current]);
      if (!working.length && job.current) working.push([null, job.current]);
      for (const [a, file] of working) {
        const card = document.querySelector(`.ds-item[data-file="${CSS.escape(file)}"]`); if (!card) continue;
        card.classList.add("tag-current");
        if (a && agents.length > 1) { card.style.setProperty("--ac", agentColor(a)); const b = el("span", "agent-badge", agentShort(a)); card.append(b); }
      }
    }
    followCurrent();
  }
  let spinI = 0;
  setInterval(() => { const f = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[spinI++ % 10]; document.querySelectorAll(".hl-spin").forEach((s) => { s.textContent = f; }); }, 100);
  function openTerm(jobId, kind = "tag") {
    if (term.job?.id !== jobId) { Object.assign(term, {job: null, lines: [], next: 0, seen: new Set(), pinned: true}); }
    term.kind = kind;
    term.follow = true; term.followed = null; term.followAgent = null;   // opening the terminal always resumes follow mode
    $("#ds-term").hidden = false; $("#ds-split").classList.add("with-term");
    clearInterval(term.timer);
    const poll = async () => {
      try {
        const r = await api(kind === "find" ? `/api/finds/${jobId}/log?after=${term.next}` : `/api/tag-jobs/${jobId}/log?after=${term.next}`);
        term.job = r.job; term.next = r.next;
        let added = false;
        for (const l of [...r.lines, ...r.hermes]) {
          const key = `${l.t}|${l.hermes ? "h" : ""}|${l.text}`;
          if (term.seen.has(key)) continue;
          term.seen.add(key); term.lines.push(l); added = true;
        }
        if (added) term.lines.sort((a, b) => a.t - b.t);
        paintTerm();
        if (!["waiting_for_gpu", "preparing", "running"].includes(r.job.state)) { clearInterval(term.timer); term.timer = null; paintTerm(); }
      } catch (e) { clearInterval(term.timer); }
    };
    poll(); term.timer = setInterval(poll, 1500);
  }
  function closeTerm() {
    clearInterval(term.timer); term.timer = null;
    $("#ds-term").hidden = true; $("#ds-split").classList.remove("with-term");
    document.querySelectorAll(".ds-item.tag-current, .ds-item.tag-done").forEach((c) => { c.classList.remove("tag-current", "tag-done"); c.style.removeProperty("--dc"); });
  }
  $("#hterm-close").addEventListener("click", closeTerm);
  // ---- auto-tagging (llama vision model) ----
  let tagPoll = null;
  async function openAutotag(files) {
    if (!current) return;
    const [mres, sres] = await Promise.all([api("/api/tagging/models"), api("/api/tagging/settings")]).catch((e) => { note(e.message, true); return [null, null]; });
    if (!mres) return;
    const shell = modal("ds-tagdlg");
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(`trainer-tag-${current}`) || "{}"); } catch (_) {}
    const hasVideo = data.items.some((x) => x.kind === "video"), noun = hasVideo ? "clip" : "image";
    const kinds = new Set(data.items.map((x) => x.kind));
    const targets = sres.targets.filter((t) => t.media.some((m) => kinds.has(m)));
    const guideOf = (id) => sres.guides.find((g) => g.id === (sres.targets.find((t) => t.id === id)?.guide || "natural"));
    const guessTrigger = saved.trigger ?? ((data.items.find((x) => x.caption)?.caption || "").split(",")[0].trim().split(/\s+/).length === 1 ? (data.items.find((x) => x.caption)?.caption || "").split(",")[0].trim() : "");
    shell.body.innerHTML = `
      <div class="tag-form">
        <div class="tr-head"><div class="ed-top"><strong>Auto-tag ${files ? `${files.length} ${noun}` : `dataset “${current}”`}</strong><span class="ds-info">${hasVideo ? "A vision model from the Llama service looks at evenly spaced, timestamped frames of each clip and writes its caption." : "A vision model from the Llama service looks at each image and writes booru tags or a caption in the style the target model was trained on."} It waits its turn in the shared GPU queue and frees ComfyUI's VRAM first. You can also ask Hermes: “tag my ${escapeHtml(current)} dataset”.</span></div>
          <div class="tr-actions"><button type="button" class="button secondary" id="tg-cancel">Cancel</button><button type="button" class="button primary" id="tg-start">Start tagging</button></div></div>
        <div class="tag-grid">
          <label>Vision model<select id="tg-model"></select></label>
          <label>Target model <span class="muted">(the model you'll train; sets the caption style)</span><select id="tg-target" required><option value="" disabled>Choose the model you'll train…</option></select></label>
          <label>Caption format<select id="tg-format"><option value="auto">The target model's format</option><option value="tags">Booru tags</option><option value="natural">Sentences</option></select></label>
          <label>Trigger word <span class="muted">(starts every caption)</span><input id="tg-trigger" maxlength="64" placeholder="e.g. mytrigger"></label>
          <label class="wide">What the LoRA teaches <span class="muted">(optional; tells the model what to leave to the trigger word)</span><input id="tg-concept" maxlength="300" placeholder="e.g. a style: loose watercolor illustration / a person: my own character / a motion: slow-motion hair flip"></label>
          <label class="wide">Style sentence <span class="muted">(optional, added to the end of every caption)</span><input id="tg-style" maxlength="400" placeholder="e.g. Shot on 16mm film with warm grain."></label>
          <label class="tg-frames-wrap">Frames per clip <span class="muted">(more = sees motion better, slower)</span><select id="tg-frames"><option value="3">3 · fastest, start/middle/end</option><option value="6">6 · quick</option><option value="8">8 · balanced (default)</option><option value="12">12 · detailed motion</option><option value="16">16 · fast action</option><option value="24">24 · maximum</option></select></label>
          <label>Which ${noun}s<select id="tg-mode"><option value="missing">Only ${noun}s without a caption</option><option value="all">All ${noun}s (rewrite existing captions)</option></select></label>
          <label class="tr-check tg-unload"><input type="checkbox" id="tg-unload" checked> Unload the model when done (frees VRAM)</label>
          <div class="wide tg-swarm">
            <label class="tr-check"><input type="checkbox" id="tg-swarm"> Agent swarm <span class="muted">(several agents caption at once; each claims its own clips so they never work on the same one)</span></label>
            <label class="tg-swarm-n">Agents<input type="number" id="tg-agents" min="2" max="16" value="4"></label>
            <span class="box-note" id="tg-swarm-hint"></span>
          </div>
        </div>
        <details class="job-box tag-instr"><summary><span class="box-title">Tagging instructions (system prompt)</span><span class="box-summary">shared by every dataset</span></summary>
          <div class="box-body">
            <label>System prompt<textarea id="tg-system" rows="14"></textarea></label>
            <label>Booru tags prompt <span class="muted">(used instead of the system prompt when the format is tags)</span><textarea id="tg-tagsprompt" rows="12"></textarea></label>
            <label><span id="tg-guide-label">Model guidance</span> <span class="muted">(added after the prompt; one per target model)</span><textarea id="tg-guide" rows="6"></textarea></label>
            <label>Request template <span class="muted">({trigger} {concept} {style} {what} {examples} {existing} {file})</span><textarea id="tg-template" rows="5"></textarea></label>
            <label>Clip scout prompt <span class="muted">(✨ Auto-cut in the trimmer: how Hermes picks clips from long videos)</span><textarea id="tg-cutprompt" rows="10"></textarea></label>
            <div class="tr-row"><button type="button" class="button secondary" id="tg-save">Save instructions</button><button type="button" class="button secondary" id="tg-reset">Reset to defaults</button><span class="ds-cap-state" id="tg-saved"></span></div>
          </div>
        </details>
      </div>`;
    const q = (sel) => shell.body.querySelector(sel);
    for (const group of hasVideo ? ["Video", "Image"] : ["Image", "Video"]) {
      const list = targets.filter((t) => t.group === group);
      if (!list.length) continue;
      const og = document.createElement("optgroup"); og.label = group;
      for (const t of list) og.append(new Option(t.title, t.id));
      q("#tg-target").append(og);
    }
    const guideEdits = {};   // target id -> edited guidance not yet saved
    let shownGuide = null;
    const showGuide = () => {
      if (shownGuide) guideEdits[shownGuide] = q("#tg-guide").value;
      const picked = !!q("#tg-target").value;
      q("#tg-start").disabled = !picked;
      q("#tg-start").title = picked ? "" : "Choose the target model first";
      q("#tg-guide").disabled = !picked;
      if (!picked) {
        shownGuide = null;
        q("#tg-guide-label").textContent = "Model guidance · choose a target model";
        q("#tg-guide").value = "";
        return;
      }
      const g = guideOf(q("#tg-target").value); shownGuide = g.id;
      q("#tg-guide-label").textContent = `Model guidance · ${g.label}`;
      q("#tg-guide").value = guideEdits[g.id] ?? g.text;
    };
    // Never guessed from the dataset: only a model picked here before (for this dataset) comes back.
    q("#tg-target").value = saved.targetPicked && targets.some((t) => t.id === saved.target) ? saved.target : "";
    q("#tg-format").value = saved.format || "auto";
    q("#tg-target").addEventListener("input", showGuide);
    showGuide();
    q(".tg-frames-wrap").hidden = !hasVideo;
    for (const m of mres.models) q("#tg-model").append(new Option(`${m.name} (${m.id})${m.slots > 1 ? ` · ${m.slots} slots` : ""}`, m.id));
    const slotsOf = () => mres.models.find((m) => m.id === q("#tg-model").value)?.slots || 1;
    const swarmHint = () => {
      const on = q("#tg-swarm").checked, n = +q("#tg-agents").value || 2, slots = slotsOf();
      q(".tg-swarm-n").hidden = !on;
      q("#tg-swarm-hint").textContent = !on ? "" : slots >= n
        ? `${q("#tg-model").value} has ${slots} parallel slot(s): ${n} agents really run at the same time.`
        : `${q("#tg-model").value} has ${slots} parallel slot(s): only ${slots} agent(s) run at once, the rest wait their turn (no collisions, but less speed-up). Raise --parallel for this model in the llama config for more.`;
      q("#tg-swarm-hint").classList.toggle("warn", on && slots < n);
    };
    q("#tg-model").value = saved.model || mres.default || mres.models[0]?.id || "";
    q("#tg-trigger").value = guessTrigger || "";
    q("#tg-style").value = saved.style || "";
    q("#tg-concept").value = saved.concept || "";
    q("#tg-frames").value = String(saved.frames || 8); if (!q("#tg-frames").value) q("#tg-frames").value = "8";
    q("#tg-swarm").checked = !!saved.swarm; q("#tg-agents").value = saved.agents || 4;
    for (const id of ["#tg-swarm", "#tg-agents", "#tg-model"]) q(id).addEventListener("input", swarmHint);
    swarmHint();
    if (files) { q("#tg-mode").value = "all"; q("#tg-mode").disabled = true; }
    q("#tg-system").value = sres.system_prompt; q("#tg-template").value = sres.user_template; q("#tg-cutprompt").value = sres.cut_prompt || "";
    q("#tg-tagsprompt").value = sres.tags_prompt || "";
    q("#tg-save").addEventListener("click", async () => {
      if (shownGuide) guideEdits[shownGuide] = q("#tg-guide").value;
      try {
        const r = await api("/api/tagging/settings", {method:"PUT", body:JSON.stringify({system_prompt:q("#tg-system").value, user_template:q("#tg-template").value, default_model:q("#tg-model").value, cut_prompt:q("#tg-cutprompt").value, tags_prompt:q("#tg-tagsprompt").value, guide_overrides:guideEdits})});
        for (const g of sres.guides) g.text = r.guide_overrides?.[g.id] || g.default_text;
        q("#tg-saved").textContent = "saved";
      }
      catch (e) { q("#tg-saved").textContent = e.message; }
    });
    q("#tg-reset").addEventListener("click", () => { q("#tg-system").value = sres.defaults.system_prompt; q("#tg-template").value = sres.defaults.user_template; q("#tg-cutprompt").value = sres.defaults.cut_prompt; q("#tg-tagsprompt").value = sres.defaults.tags_prompt; for (const g of sres.guides) guideEdits[g.id] = g.default_text; if (shownGuide) q("#tg-guide").value = guideOf(q("#tg-target").value).default_text; q("#tg-saved").textContent = "defaults loaded (Save to keep)"; });
    q("#tg-cancel").addEventListener("click", () => shell.close());
    return new Promise((resolve) => {
      q("#tg-start").addEventListener("click", async () => {
        if (!q("#tg-target").value) { note("Choose the target model first: it sets how the captions are written.", true); q("#tg-target").focus(); return; }
        const body = {model:q("#tg-model").value, trigger:q("#tg-trigger").value.trim(), concept:q("#tg-concept").value.trim(), style:q("#tg-style").value.trim(), mode:q("#tg-mode").value, unload_after:q("#tg-unload").checked, frames:+q("#tg-frames").value || 8, target:q("#tg-target").value, caption_format:q("#tg-format").value,
          agents: q("#tg-swarm").checked ? Math.max(2, Math.min(16, +q("#tg-agents").value || 2)) : 1};
        if (files) body.files = files;
        try { localStorage.setItem(`trainer-tag-${current}`, JSON.stringify({model:body.model, trigger:body.trigger, concept:body.concept, style:body.style, frames:body.frames, target:body.target, targetPicked:true, format:body.caption_format, swarm:body.agents > 1, agents:+q("#tg-agents").value || 4})); } catch (_) {}
        try { const job = await api(`/api/datasets/${enc(current)}/autotag`, {method:"POST", body:JSON.stringify(body)}); resolve(job); shell.close(); watchTagJob(job.id); }
        catch (e) { note(e.message, true); }
      });
      shell.onclose = () => resolve(null);
    });
  }
  function renderTagJob(job) {
    const host = $("#ds-tagjob");
    if (!job) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false; host.replaceChildren();
    const active = ["waiting_for_gpu", "preparing", "running"].includes(job.state);
    const frac = job.total ? job.processed / job.total : 0;
    const bar = el("div", `pbar overall ${job.state === "complete" ? "complete" : job.state}`), fill = el("div", "pbar-fill"); fill.style.width = `${Math.round(frac * 100)}%`;
    bar.append(fill, el("span", "pbar-text", `✨ ${job.stage} · ${job.processed}/${job.total}${job.failed ? ` · ${job.failed} failed` : ""}${job.current && active ? ` · ${job.current}` : ""} · ${job.model}`));
    const row = el("div", "ds-tagjob-row"); row.append(bar);
    if (active) { const c = el("button", "button secondary", "Cancel"); c.addEventListener("click", () => api(`/api/tag-jobs/${job.id}/cancel`, {method:"POST"}).catch((e) => note(e.message, true))); row.append(c); }
    const tb = el("button", "button secondary", "Terminal"); tb.title = "Show the Hermes-style session log"; tb.addEventListener("click", () => ($("#ds-term").hidden || term.job?.id !== job.id ? openTerm(job.id) : closeTerm())); row.append(tb);
    if (!active) { const x = el("button", "button secondary icon", "✕"); x.title = "Hide"; x.addEventListener("click", () => { renderTagJob(null); closeTerm(); }); row.append(x); }
    host.append(row);
    if (job.errors?.length) host.append(el("p", "box-note bad", `Failed: ${job.errors.slice(-3).map((e) => `${e.file}: ${e.error}`).join(" · ")}`));
  }
  function watchTagJob(id) {
    clearInterval(tagPoll);
    if (!document.querySelector(".ds-editor")) openTerm(id);
    let lastDone = -1;
    const tick = async () => {
      try {
        const job = await api(`/api/tag-jobs/${id}`);
        if (job.dataset !== current) return;
        renderTagJob(job);
        if (job.done !== lastDone) { lastDone = job.done; if (!document.querySelector(".ds-modal")) reload(); }
        if (!["waiting_for_gpu", "preparing", "running"].includes(job.state)) {
          clearInterval(tagPoll); tagPoll = null; await reload(); loadList();
          note(`Tagging ${job.state}: ${job.done} captioned${job.failed ? `, ${job.failed} failed` : ""}.`, job.state !== "complete");
          document.dispatchEvent(new CustomEvent("tagjob-finished", {detail: job}));
        }
      } catch (e) { clearInterval(tagPoll); note(e.message, true); }
    };
    tick(); tagPoll = setInterval(tick, 2000);
  }
  async function resumeTagJob() {
    try { const r = await api(`/api/tag-jobs?dataset=${enc(current)}`); const live = r.jobs.reverse().find((j) => ["waiting_for_gpu", "preparing", "running"].includes(j.state)); if (live) watchTagJob(live.id); else renderTagJob(null); } catch (_) {}
  }
  $("#ds-autotag").addEventListener("click", () => openAutotag(null));

  // ---- 🔎 Find clips: Hermes searches video sites for source footage ----
  const FIND_ACTIVE = ["waiting_for_gpu", "preparing", "running"];
  const DL_ACTIVE = ["starting", "downloading", "merging"];
  const fmtDur = (s) => { if (!(s > 0)) return ""; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, "0"); return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`; };
  let findPoll = null, findDone = new Set();
  function paintFindTerm() {
    const job = term.job, body = $("#hterm-body");
    const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 12;
    body.replaceChildren(...term.lines.map((l) => termLine(l, null)));
    const active = FIND_ACTIVE.includes(job.state);
    if (active) { const cur = el("div", "hl hl-cursor"); cur.append(el("span", "hl-time", ""), el("span", "hl-body", "")); cur.lastChild.append(el("span", "hl-spin", "⠋"), document.createTextNode(` ${job.stage}`)); body.append(cur); }
    if (atBottom || term.pinned) body.scrollTop = body.scrollHeight;
    term.pinned = false;
    $("#hterm-sub").textContent = ` · clip-finder · ${job.dataset}${job.source === "hermes" ? " · started from a Hermes chat" : ""}${job.run_id ? ` · ${job.run_id}` : ""}`;
    const st = $("#hterm-state"); st.textContent = active ? "● running" : job.state === "complete" ? "● done" : `● ${job.state}`; st.dataset.state = job.state;
    const elapsed = Math.max(0, Date.now() / 1000 - job.created);
    $("#hterm-status").textContent = `⚕ ${job.model || "hermes default"}${job.reasoning && job.reasoning !== "default" ? ` · thinking ${job.reasoning}` : ""}  │  ${job.candidates.length}/${job.count} proposed  │  ${job.searches.length} search${job.searches.length === 1 ? "" : "es"}  │  ${job.steps}/${job.max_steps} helper calls  │  ${Math.floor(elapsed / 60)}:${String(Math.floor(elapsed % 60)).padStart(2, "0")} elapsed`;
    $("#hterm-follow").hidden = true; $("#hterm-following").hidden = true;
  }
  function showShot(src, l) {
    const shell = modal("ds-shotview");
    const head = el("div", "shot-head");
    const link = el("a", "find-title", l.text || l.url); link.href = l.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.title = "Open the page";
    const x = el("button", "button secondary", "Close"); x.addEventListener("click", () => shell.close());
    head.append(link, el("span", "ds-info", l.url), x);
    const img = el("img", "shot-full"); img.src = src; img.alt = `Screenshot of ${l.url}`;
    shell.body.append(head, img);
  }
  function flashCandidate(i) {
    const card = document.querySelector(`.find-card[data-i="${i}"]`); if (!card) return;
    card.scrollIntoView({block: "center", behavior: "smooth"});
    card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
  }
  function toggleFindTerm(id) {
    if (!$("#ds-term").hidden && term.job?.id === id) { closeTerm(); return; }
    openTerm(id, "find");
    requestAnimationFrame(() => $("#ds-term").scrollIntoView({block: "nearest", behavior: "smooth"}));
  }
  async function openFind() {
    if (!current) return;
    let info = {sites: {youtube: "YouTube", dailymotion: "Dailymotion", archive: "Internet Archive", bilibili: "Bilibili"}, recipes: {}};
    let inv = null;
    [info, inv] = await Promise.all([api("/api/finds").catch(() => info), api("/api/finds/models").catch(() => null)]);
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(`trainer-find-${current}`) || "{}"); } catch (_) {}
    const shell = modal("ds-tagdlg ds-finddlg");
    const learned = Object.keys(info.recipes || {});
    shell.body.innerHTML = `
      <div class="tag-form">
        <div class="tr-head"><div class="ed-top"><strong>Find clips for “${escapeHtml(current)}”</strong><span class="ds-info">Hermes searches video sites for source footage, inspects the promising videos and proposes the best ones. You decide what to download; downloads go to “Waiting to be trimmed”. With a model on this PC it waits its turn in the shared GPU queue; cloud models start right away. Click the Hermes status line to watch its agent log.</span></div>
          <div class="tr-actions"><button type="button" class="button secondary" id="fd-cancel">Cancel</button><button type="button" class="button primary" id="fd-start">Start search</button></div></div>
        <div class="tag-grid">
          <label class="wide">What to find<input id="fd-query" maxlength="400" placeholder="e.g. real-life footage of horses trotting in a field, close-ups, daylight"></label>
          <label class="wide">What the LoRA teaches <span class="muted">(optional; helps Hermes judge videos)</span><input id="fd-concept" maxlength="300" placeholder="e.g. a style: cartoon characters composited into real footage"></label>
          <label>Model <span class="muted">(Hermes' model picker)</span><select id="fd-model"></select></label>
          <label>Thinking<select id="fd-think">
            <option value="default">Hermes default</option><option value="none">Off</option><option value="minimal">Minimal</option><option value="low">Low</option>
            <option value="medium">Medium</option><option value="high">High</option><option value="xhigh">Extra high</option><option value="max">Max</option></select></label>
          <p class="box-note wide fd-model-hint" id="fd-model-hint"></p>
          <div class="wide fd-sites"><span class="fd-label">Sites</span>
            <div class="fd-site-list">${Object.entries(info.sites).map(([k, v]) => `<label class="tr-check"><input type="checkbox" value="${k}"> ${escapeHtml(v)}</label>`).join("")}
              ${learned.map((d) => `<label class="tr-check" title="Hermes worked out this site's search before: ${escapeHtml(info.recipes[d].template)}"><input type="checkbox" value="${escapeHtml(d)}"> ${escapeHtml(d)} <span class="muted">(learned)</span></label>`).join("")}</div>
            <label>Other sites <span class="muted">(any video site, comma separated; Hermes figures out how to search sites it doesn't know, and remembers them)</span><input id="fd-other" maxlength="400" placeholder="e.g. vimeo.com, pexels.com/videos, some-anime-site.net"></label>
          </div>
          <label>How many videos<input type="number" id="fd-count" min="1" max="30" value="8"></label>
          <label>Effort<select id="fd-steps"><option value="20">Quick (~20 steps)</option><option value="40" selected>Normal (~40 steps)</option><option value="80">Thorough (~80 steps)</option></select></label>
          <label>Shortest video <span class="muted">(minutes, 0 = any)</span><input type="number" id="fd-min" min="0" max="600" step="0.5" value="0"></label>
          <label>Longest video <span class="muted">(minutes, 0 = any)</span><input type="number" id="fd-max" min="0" max="600" step="0.5" value="0"></label>
          <label class="tr-check wide"><input type="checkbox" id="fd-auto"> Download every proposal automatically <span class="muted">(whole videos, to trim later; otherwise you pick)</span></label>
          <label class="tr-check wide"><input type="checkbox" id="fd-unload" checked> Unload Hermes' model when done (frees VRAM)</label>
        </div>
        <p class="box-note">The brief Hermes gets is editable in the Rules tab (Clip finder brief). You can also ask Hermes in chat: “find clips of … for my ${escapeHtml(current)} dataset”.</p>
      </div>`;
    const q = (sel) => shell.body.querySelector(sel);
    // Model list: "provider|model" values, "" = whatever Hermes is set to.
    const msel = q("#fd-model");
    msel.append(new Option(inv?.default?.model ? `Hermes default (${inv.default.model})` : "Hermes default", ""));
    for (const pr of inv?.providers || []) {
      const g = document.createElement("optgroup"); g.label = `${pr.name}${pr.local ? " · this PC's GPU" : " · cloud"}`;
      for (const m of pr.models) g.append(new Option(m, `${pr.slug}|${m}`));
      msel.append(g);
    }
    if (!inv) q("#fd-model-hint").textContent = "Couldn't reach Hermes' model list; the default model will be used.";
    const isLocal = (slug) => /^(custom|llama-cpp|lmstudio|ollama)(:|$)/.test(slug || "");
    const modelHint = () => {
      const [prov, model] = msel.value.split("|");
      const local = isLocal(prov || inv?.default?.provider || "custom");
      q("#fd-unload").disabled = !local;
      q("#fd-model-hint").textContent = !inv ? q("#fd-model-hint").textContent
        : local ? `${model || inv.default.model} runs on this PC's GPU: the search waits its turn in the GPU queue and frees ComfyUI's VRAM first.`
                : `${model} is a cloud model: the search starts right away, no GPU queue.`;
    };
    msel.addEventListener("input", modelHint);
    // v2 (2026-10-07): finder defaults to qwen-27b + medium thinking; older saved choices are dropped once.
    const fdef = inv?.finder_default || {model: "qwen-27b", provider: "", reasoning: "medium"};
    if ((saved.v || 1) < 2) { delete saved.model; delete saved.provider; delete saved.reasoning; }
    q("#fd-think").value = saved.reasoning || fdef.reasoning || "medium";
    const sitesSaved = saved.sites || ["youtube"];
    shell.body.querySelectorAll(".fd-site-list input").forEach((c) => { c.checked = sitesSaved.includes(c.value); });
    q("#fd-other").value = sitesSaved.filter((x) => !shell.body.querySelector(`.fd-site-list input[value="${CSS.escape(x)}"]`)).join(", ");
    q("#fd-query").value = saved.query || ""; q("#fd-concept").value = saved.concept || "";
    q("#fd-count").value = saved.count || 8; q("#fd-steps").value = String(saved.max_steps || 40);
    q("#fd-min").value = (saved.min_seconds || 0) / 60; q("#fd-max").value = (saved.max_seconds || 0) / 60;
    q("#fd-auto").checked = !!saved.auto_download;
    const wantModel = "model" in saved ? (saved.model ? `${saved.provider}|${saved.model}` : "") : `${fdef.provider}|${fdef.model}`;
    msel.value = [...msel.options].some((o) => o.value === wantModel) ? wantModel
      : ([...msel.options].find((o) => o.value.endsWith(`|${fdef.model}`) && isLocal(o.value.split("|")[0]))?.value ?? "");
    modelHint();
    q("#fd-cancel").addEventListener("click", () => shell.close());
    setTimeout(() => q("#fd-query").focus(), 0);
    q("#fd-start").addEventListener("click", async () => {
      const sites = [...shell.body.querySelectorAll(".fd-site-list input:checked")].map((c) => c.value)
        .concat(q("#fd-other").value.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean));
      const body = {query: q("#fd-query").value.trim(), concept: q("#fd-concept").value.trim(), sites, count: Math.max(1, Math.min(30, +q("#fd-count").value || 8)),
        max_steps: +q("#fd-steps").value || 40, min_seconds: Math.round((+q("#fd-min").value || 0) * 60), max_seconds: Math.round((+q("#fd-max").value || 0) * 60),
        auto_download: q("#fd-auto").checked, unload_after: q("#fd-unload").checked, reasoning: q("#fd-think").value,
        provider: msel.value.split("|")[0] || "", model: msel.value.split("|").slice(1).join("|") || "", v: 2};
      if (body.query.length < 2) { q("#fd-query").focus(); note("Tell Hermes what to look for.", true); return; }
      if (!sites.length) { note("Pick at least one site.", true); return; }
      try { localStorage.setItem(`trainer-find-${current}`, JSON.stringify(body)); } catch (_) {}
      try { const job = await api(`/api/datasets/${enc(current)}/find`, {method: "POST", body: JSON.stringify(body)}); shell.close(); watchFind(job.id); }
      catch (e) { note(e.message, true); }
    });
  }
  function findCard(job, c) {
    const card = el("div", "find-card"); card.dataset.i = c.i;
    const thumb = el("a", "find-thumb"); thumb.href = c.url; thumb.target = "_blank"; thumb.rel = "noopener noreferrer"; thumb.title = "Open on the site";
    if (c.thumb) { const img = el("img"); img.loading = "lazy"; img.alt = ""; img.src = `${base}/api/finds-thumb?u=${enc(c.thumb)}`; img.addEventListener("error", () => img.remove()); thumb.append(img); }
    if (c.duration) thumb.append(el("span", "staged-dur", fmtDur(c.duration)));
    const score = el("span", "find-score", `${c.score}/10`); score.dataset.level = c.score >= 8 ? "hi" : c.score >= 6 ? "mid" : "lo"; thumb.append(score);
    const body = el("div", "find-body");
    const title = el("a", "find-title", c.title); title.href = c.url; title.target = "_blank"; title.rel = "noopener noreferrer"; title.title = c.url;
    body.append(title, el("span", "ds-info", [c.site, c.height && `${c.height}p`, c.uploader].filter(Boolean).join(" · ")));
    if (c.reason) { const r = el("p", "find-reason", c.reason); r.title = c.reason; body.append(r); }
    const hasSection = c.start != null && c.end;
    if (hasSection) { const g = el("span", "find-section", `Hermes' pick: ${fmtDur(c.start) || "0:00"}–${fmtDur(c.end)}`); g.title = "Where Hermes thinks the best part is. The whole video is downloaded; use this when trimming."; body.append(g); }
    const acts = el("div", "find-actions"), d = c.download;
    const get = async (section, force = false) => {
      try { await api(`/api/finds/${job.id}/candidates/${c.i}/download`, {method: "POST", body: JSON.stringify({section, force})}); watchFind(job.id, true); }
      catch (e) { note(e.message, true); }
    };
    const post = async (what, body) => {
      try { renderFind(await api(`/api/finds/${job.id}/candidates/${c.i}/${what}`, {method: "POST", body: JSON.stringify(body || {})}), true); watchFind(job.id, true); }
      catch (e) { note(e.message, true); }
    };
    if (c.rejected) {
      card.classList.add("rejected");
      acts.append(el("span", "find-dl bad", `✕ Rejected${typeof c.rejected === "string" ? `: ${c.rejected}` : ""}`));
      const u = el("button", "button secondary", "Undo"); u.title = "Put it back (Hermes may propose it again)"; u.addEventListener("click", () => post("unreject")); acts.append(u);
      body.append(acts); card.append(thumb, body);
      return card;
    }
    if (!d || d.state === "failed") {
      const b = el("button", "button secondary", "⬇ Download");
      b.title = "Download the whole video into “Waiting to be trimmed” (trim or ✨ Auto-cut it there)"; b.addEventListener("click", () => get(false)); acts.append(b);
      if (d?.state === "failed") acts.append(el("span", "find-dl bad", d.error || "download failed"));
    } else if (DL_ACTIVE.includes(d.state)) {
      const bar = el("div", "pbar"), fill = el("div", "pbar-fill"); fill.style.width = `${Math.round(d.percent || 0)}%`;
      bar.append(fill, el("span", "pbar-text", d.state === "merging" ? "merging…" : d.percent ? `${Math.round(d.percent)}%` : "downloading…")); acts.append(bar);
    } else if (d.state === "done") {
      const ok = el("span", "find-dl ok", `✓ Saved to ${d.saved || d.file}`); ok.title = d.saved || d.file; acts.append(ok);
      if ((data?.staged || []).some((s) => s.file === d.file)) { const t = el("button", "button secondary", "✂ Trim"); t.addEventListener("click", () => queueTrim(d.file, true)); acts.append(t); }
    } else if (d.state === "duplicate") {
      const pv = d.previous || {};
      const dup = el("span", "find-dl dup", `Already downloaded → ${pv.path || d.file} (${when(pv)}${pv.exists ? "" : ", since trimmed/removed"})`); dup.title = dup.textContent; acts.append(dup);
      const again = el("button", "button secondary", "Download again"); again.addEventListener("click", () => get(false, true)); acts.append(again);
    }
    // Reject: Hermes looks for a replacement (now if it's running, in a continuation run if it finished).
    const rj = el("button", "button secondary find-reject", "✕ Reject");
    rj.title = job.fetch_url ? "Not this one" : "Not a good source: Hermes won't propose it again and looks for another";
    rj.addEventListener("click", () => {
      const form = el("form", "find-rejectform");
      const why = el("input"); why.placeholder = "Why? (optional, Hermes learns from it)"; why.maxLength = 300;
      const ok = el("button", "button secondary danger", "Reject"); ok.type = "submit";
      const no = el("button", "button secondary", "Cancel"); no.type = "button";
      form.append(why, ok, no); acts.replaceWith(form); why.focus();
      no.addEventListener("click", () => renderFind(lastFindJob, true));
      form.addEventListener("submit", (e) => { e.preventDefault(); post("reject", {reason: why.value.trim()}); });
    });
    acts.append(rj);
    body.append(acts);
    card.append(thumb, body);
    return card;
  }
  const when = (e) => (e && e.t > 1e9 ? new Date(e.t * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : e?.at || "earlier");
  let lastFindJob = null;
  function renderFind(job, force = false) {
    const host = $("#ds-findjob");
    lastFindJob = job;
    if (!job) { host.hidden = true; host.replaceChildren(); return; }
    if (!force && host.querySelector(".find-rejectform") && host.contains(document.activeElement)) return;   // don't wipe a reason being typed
    host.hidden = false; host.replaceChildren();
    const active = FIND_ACTIVE.includes(job.state);
    const row = el("div", "find-row");
    const status = el("button", "find-status"); status.type = "button"; status.dataset.state = job.state;
    status.title = "Open the Hermes agent log for this search";
    const kept = job.candidates.filter((c) => !c.rejected), nRej = job.candidates.length - kept.length;
    const line = active ? (job.status_line || job.stage) : job.state === "complete" ? `Done · ${kept.length} video(s) proposed${nRej ? ` · ${nRej} rejected (Hermes restarts a few seconds after a rejection)` : ""}` : `${job.stage}${job.error ? ` · ${job.error}` : ""}`;
    status.append(el("span", active ? "hl-spin" : "find-dot", active ? "⠋" : "●"), el("span", "find-brand", " ☤ Hermes "), el("span", "find-line", line));
    status.addEventListener("click", () => toggleFindTerm(job.id));
    const cnt = el("span", "find-count", `${kept.length}/${job.count}`);
    cnt.title = `${job.model || "Hermes default model"}${job.reasoning && job.reasoning !== "default" ? ` · thinking ${job.reasoning}` : ""}`;
    row.append(status, cnt);
    if (active) { const c = el("button", "button secondary", "Stop"); c.title = "Stop Hermes (proposals so far stay)"; c.addEventListener("click", () => api(`/api/finds/${job.id}/cancel`, {method: "POST"}).catch((e) => note(e.message, true))); row.append(c); }
    else {
      const again = el("button", "button secondary", "New search"); again.addEventListener("click", openFind); row.append(again);
      const x = el("button", "button secondary icon", "✕"); x.title = "Hide"; x.addEventListener("click", () => { try { localStorage.setItem(`trainer-find-hidden-${current}`, job.id); } catch (_) {} renderFind(null); if (term.kind === "find") closeTerm(); }); row.append(x);
    }
    host.append(row);
    if (job.summary && !active) { const s = el("p", "find-summary", job.summary); s.title = "Open the Hermes agent log"; s.addEventListener("click", () => toggleFindTerm(job.id)); host.append(s); }
    if (job.candidates.length) {
      const grid = el("div", "find-cands");
      for (const c of [...job.candidates].sort((a, b) => !!a.rejected - !!b.rejected || b.score - a.score || a.i - b.i)) grid.append(findCard(job, c));
      host.append(grid);
    } else if (active) host.append(el("p", "box-note", job.state === "waiting_for_gpu" ? "Waiting for the GPU queue (renders and training go first)…" : job.fetch_url ? `Hermes is getting the video at ${job.fetch_url}…` : "Proposals show up here as Hermes finds them."));
  }
  const fetchJobs = new Set();      // link fetches started on this page: open the trimmer when they land
  let urlQueuePoll = null, watchedFindId = null;
  function renderUrlQueue(queue) {
    const host = $("#ds-url-queue"), items = queue.items || [];
    host.hidden = !items.length;
    host.replaceChildren();
    if (!items.length) return;
    const head = el("div", "ds-url-queue-head");
    head.append(el("strong", null, "☤ Hermes URL queue"),
                el("span", null, `${queue.total} link${queue.total === 1 ? "" : "s"} in queue${queue.total > items.length ? " · includes other datasets" : ""}`));
    const list = el("div", "ds-url-queue-list");
    for (const item of items) {
      const active = item.state !== "queued";
      const row = el("div", "ds-url-queue-item"); row.dataset.state = item.state;
      const number = el("span", "ds-url-queue-number", String(item.position));
      number.title = `Position ${item.position} in the Hermes URL queue`;
      const info = el("div", "ds-url-queue-info");
      const link = el("span", "ds-url-queue-link", item.url); link.title = item.url;
      const stage = el("span", "ds-url-queue-stage", `${active ? "● Active" : "○ Waiting"} · ${item.stage || item.state}`);
      info.append(link, stage); row.append(number, info);
      if (active) {
        const view = el("button", "button secondary", "View"); view.type = "button";
        view.title = "Show this Hermes job";
        view.addEventListener("click", () => { watchFind(item.id, true); $("#ds-findjob").scrollIntoView({behavior:"smooth", block:"nearest"}); });
        row.append(view);
      }
      if (item.state === "downloading") { list.append(row); continue; }
      const cancel = el("button", "button secondary danger", active ? "Stop" : "Remove"); cancel.type = "button";
      cancel.title = active ? "Stop this Hermes fetch" : "Remove this link from the queue";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try { await api(`/api/finds/${item.id}/cancel`, {method: "POST"}); await refreshUrlQueue(); }
        catch (e) { note(e.message, true); cancel.disabled = false; }
      });
      row.append(cancel); list.append(row);
    }
    host.append(head, list);
  }
  async function refreshUrlQueue() {
    const dataset = current;
    if (!dataset) return;
    try {
      const queue = await api(`/api/datasets/${enc(dataset)}/fetch-queue`);
      if (dataset !== current) return;
      renderUrlQueue(queue);
      for (const item of queue.items) fetchJobs.add(item.id);
      const running = queue.items.find((item) => item.state !== "queued");
      if (running && watchedFindId !== running.id) watchFind(running.id, true);
      if (queue.items.length && !urlQueuePoll) urlQueuePoll = setInterval(refreshUrlQueue, 2000);
      if (!queue.items.length && urlQueuePoll) { clearInterval(urlQueuePoll); urlQueuePoll = null; }
    } catch (_) { /* Keep the current queue visible if a poll briefly fails. */ }
  }
  async function fetchWithHermes(url) {
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(`trainer-find-${current}`) || "{}"); } catch (_) {}
    if ((saved.v || 1) < 2) saved = {unload_after: saved.unload_after};   // pre-2026-10-07 choice → server default (qwen-27b, medium)
    const body = {query: "link", fetch_url: url, sites: [new URL(url).hostname], ...("model" in saved ? {model: saved.model, provider: saved.provider || ""} : {}),
                  reasoning: saved.reasoning || "medium", unload_after: saved.unload_after ?? true, max_steps: 20};
    try {
      const job = await api(`/api/datasets/${enc(current)}/find`, {method: "POST", body: JSON.stringify(body)});
      fetchJobs.add(job.id);
      await refreshUrlQueue();
      if (job.state !== "queued" && watchedFindId !== job.id) watchFind(job.id);
      note(job.queue_position > 1 ? `Added to Hermes URL queue at position ${job.queue_position}.` :
           `Hermes is fetching the link (${job.model || "default model"}). The trimmer opens when it is downloaded.`);
      return true;
    } catch (e) { note(e.message, true); return false; }
  }
  function watchFind(id, keepTerm = false) {
    watchedFindId = id;
    clearInterval(findPoll);
    try { localStorage.removeItem(`trainer-find-hidden-${current}`); } catch (_) {}
    const tick = async () => {
      try {
        const job = await api(`/api/finds/${id}`);
        if (job.dataset !== current) { clearInterval(findPoll); findPoll = null; return; }
        // A finished download shows up in "Waiting to be trimmed": refresh the dataset once.
        const fresh = job.candidates.filter((c) => c.download?.state === "done" && !findDone.has(c.download.id));
        if (fresh.length) {
          fresh.forEach((c) => findDone.add(c.download.id));
          if (!document.querySelector(".ds-modal")) await reload();
          loadHistory();
          if (fetchJobs.has(job.id)) for (const c of fresh) if (c.download.file) queueTrim(c.download.file, true);
        }
        if (fetchJobs.has(job.id) && ["failed", "cancelled"].includes(job.state)) { fetchJobs.delete(job.id); note(`Hermes couldn't fetch ${job.fetch_url}: ${job.error || job.state}. Click the Hermes line for its log.`, true); }
        if (fetchJobs.has(job.id) && job.state === "complete" && !job.candidates.length) { fetchJobs.delete(job.id); note(`Hermes found no downloadable video at ${job.fetch_url}${job.summary ? `: ${job.summary}` : "."}`, true); }
        renderFind(job);
        const busy = FIND_ACTIVE.includes(job.state) || job.candidates.some((c) => DL_ACTIVE.includes(c.download?.state));
        if (!busy) { clearInterval(findPoll); findPoll = null; }
      } catch (e) { clearInterval(findPoll); findPoll = null; }
    };
    tick(); findPoll = setInterval(tick, 2000);
  }
  async function resumeFind() {
    clearInterval(findPoll); findPoll = null; watchedFindId = null; renderFind(null);
    try {
      const r = await api(`/api/finds?dataset=${enc(current)}`);
      const last = [...r.jobs].reverse().find((j) => FIND_ACTIVE.includes(j.state) || j.candidates.some((c) => DL_ACTIVE.includes(c.download?.state))) || [...r.jobs].reverse().find((j) => j.state !== "queued");
      let hidden = ""; try { hidden = localStorage.getItem(`trainer-find-hidden-${current}`) || ""; } catch (_) {}
      if (last && (FIND_ACTIVE.includes(last.state) || last.id !== hidden)) watchFind(last.id);
    } catch (_) {}
  }
  $("#ds-find").addEventListener("click", openFind);
  // ---- download history (every video downloaded, and where it went) ----
  async function loadHistory() {
    const box = $("#ds-dlhist"); if (!box.open || !current) return;
    const all = $("#dlh-all").checked;
    try {
      const r = await api(`/api/download-history?limit=200${all ? "" : `&dataset=${enc(current)}`}`);
      const list = $("#dlh-list"); list.replaceChildren();
      $("#dlh-count").textContent = `${r.items.length}`;
      if (!r.items.length) list.append(el("p", "box-note", "Nothing downloaded yet."));
      for (const e of r.items) {
        const row = el("div", "dlh-row");
        const a = el("a", "find-title", e.title || e.url); a.href = e.url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = e.url;
        const where = el("span", e.exists ? "find-dl ok" : "find-dl", e.exists ? `→ ${e.path}` : `→ ${e.path} (trimmed/removed since)`); where.title = e.path;
        row.append(a, where, el("span", "ds-info", `${when(e)} · ${e.source || ""}`));
        list.append(row);
      }
    } catch (err) { note(err.message, true); }
  }
  $("#ds-dlhist").addEventListener("toggle", loadHistory);
  $("#dlh-all").addEventListener("change", loadHistory);
  document.addEventListener("datasets-changed", loadHistory);
  // Review-panel keys win while that panel is on screen (capture phase, before the clip-list keys).
  window.addEventListener("keydown", (e) => {
    const host = $("#ds-batch-review");
    if (!host || host.hidden || !batchReview?.keys || $("#tab-datasets").hidden || document.querySelector(".ds-modal")) return;
    if (e.target.closest?.("input, textarea, select, video") || e.ctrlKey || e.metaKey || e.altKey) return;
    const box = host.getBoundingClientRect();
    if (box.bottom < 80 || box.top > innerHeight - 80) return;
    if (batchReview.keys(e)) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  // ---- keyboard navigation in the clip list ----
  let selectedFile = null;
  function selectCard(file) {
    selectedFile = file;
    document.querySelectorAll(".ds-item.selected").forEach((c) => c.classList.remove("selected"));
    const card = [...document.querySelectorAll(".ds-item")].find((c) => c.dataset.file === file);
    if (card) { card.classList.add("selected"); card.scrollIntoView({block:"nearest"}); }
  }
  window.addEventListener("keydown", (e) => {
    if ($("#tab-datasets").hidden || document.querySelector(".ds-modal") || !data) return;
    if (e.target.closest?.("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
    const cards = [...document.querySelectorAll(".ds-item")];
    if (!cards.length) return;
    let i = cards.findIndex((c) => c.dataset.file === selectedFile);
    const cols = cards.filter((c) => c.offsetTop === cards[0].offsetTop).length || 1;
    const moves = {ArrowRight:1, ArrowLeft:-1, ArrowDown:cols, ArrowUp:-cols};
    if (e.key in moves) {
      e.preventDefault();
      i = i < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, i + moves[e.key]));
      selectCard(cards[i].dataset.file);
    } else if (e.key === "Enter" && i >= 0) {
      e.preventDefault();
      const it = data.items.find((x) => x.file === selectedFile); if (it) openEditor(it);
    } else if ((e.key === "t" || e.key === "T") && i >= 0) {
      const it = data.items.find((x) => x.file === selectedFile && x.kind === "video");
      if (it) { e.preventDefault(); queueTrim(it.file, false); }
    }
  });

  // ---- trigger word ----
  $("#ds-trigger").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const trigger = ev.currentTarget.elements.trigger.value.trim();
    if (drafts.size && !confirm("Some caption edits are still saving; continue anyway?")) return;
    try { const r = await api(`/api/datasets/${enc(current)}/trigger`, {method:"POST", body:JSON.stringify({trigger})}); drafts.clear(); note(`Added "${trigger}" to ${r.changed} caption(s).`); await reload(); loadList(); }
    catch (e) { note(e.message, true); }
  });

  // ---- uploads ----
  const isVideo = (name) => /\.(mp4|mov|webm|mkv)$/i.test(name);
  async function uploadFile(file, hold = false) {
    const row = el("div", "ds-upload");
    const bar = el("div", "pbar"), fill = el("div", "pbar-fill"), text = el("span", "pbar-text", `${file.name} · 0%`);
    bar.append(fill, text); row.append(bar); $("#ds-uploads").append(row);
    try {
      let offset = 0, last;
      do {
        const chunk = file.slice(offset, offset + CHUNK);
        const r = await fetch(`${base}/api/datasets/${enc(current)}/upload?filename=${enc(file.name)}&offset=${offset}&total=${file.size}&staged=${hold && isVideo(file.name)}`,
          {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/octet-stream"}, body:chunk});
        last = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(last.detail || `Upload failed (${r.status})`);
        offset += chunk.size;
        const f = file.size ? offset / file.size : 1;
        fill.style.width = `${Math.round(f * 100)}%`; text.textContent = `${file.name} · ${Math.round(f * 100)}%`;
      } while (offset < file.size);
      text.textContent = `${last.file || file.name} · ${last.staged ? "waiting to be trimmed" : "added"}`; bar.classList.add("done");
      setTimeout(() => row.remove(), 4000);
      return last.file || null;
    } catch (e) { text.textContent = `${file.name} · ${e.message}`; bar.classList.add("failed"); return null; }
  }
  async function uploadAll(files) {
    if (!current || !files.length) return;
    const hold = $("#ds-autotrim").checked;
    // Media first, captions after, so a .txt never lands before its clip is listed.
    const list = [...files].sort((a, b) => (a.name.endsWith(".txt") ? 1 : 0) - (b.name.endsWith(".txt") ? 1 : 0));
    let held = 0, added = 0;
    for (const f of list) {
      const name = await uploadFile(f, hold);
      if (!name) continue;
      if (hold && isVideo(f.name)) held++; else added++;
    }
    await reload(); loadList(); changed();
    if (held || added) note([held && `${held} video${held > 1 ? "s" : ""} waiting to be trimmed`, added && `${added} file${added > 1 ? "s" : ""} added`].filter(Boolean).join(" · ") + ".");
  }
  try { $("#ds-autotrim").checked = localStorage.getItem("trainer-autotrim") !== "0"; } catch (_) {}
  $("#ds-autotrim").addEventListener("change", (e) => { try { localStorage.setItem("trainer-autotrim", e.target.checked ? "1" : "0"); } catch (_) {} });
  $("#ds-files").addEventListener("change", (e) => { uploadAll(e.target.files); e.target.value = ""; });
  // Drag & drop anywhere on the Datasets tab, with a full-page overlay while dragging.
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  const dropOverlay = $("#drop-overlay");
  let dragDepth = 0;
  const dsTabOpen = () => !$("#tab-datasets").hidden;
  function showDrop(on) {
    dropOverlay.hidden = !on;
    if (!on) return;
    const hold = $("#ds-autotrim").checked;
    $("#drop-title").textContent = current ? `Drop to add to “${current}”` : "Pick or create a dataset first";
    $("#drop-sub").textContent = !current ? "Choose one in the Dataset menu (or type a name and press Create), then drop again."
      : hold ? "Videos go to “Waiting to be trimmed” · images, audio and .txt captions go straight in"
             : "Everything goes straight into the dataset (Hold videos for trimming is off)";
    dropOverlay.classList.toggle("no-target", !current);
  }
  window.addEventListener("dragenter", (e) => { if (!dsTabOpen() || !hasFiles(e)) return; e.preventDefault(); dragDepth++; showDrop(true); });
  window.addEventListener("dragover", (e) => { if (!dsTabOpen() || !hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = current ? "copy" : "none"; });
  window.addEventListener("dragleave", (e) => { if (!dsTabOpen() || !hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) showDrop(false); });
  window.addEventListener("drop", (e) => {
    if (!dsTabOpen() || !hasFiles(e)) return;
    e.preventDefault();               // never let the browser navigate to the dropped file
    dragDepth = 0; showDrop(false);
    if (current) uploadAll(e.dataTransfer.files);
    else note("Pick or create a dataset first, then drop your files again.", true);
  });

  // ---- start ----
  // The Rules tab moved to Rocsy Settings -> Agentic (2026-10-08).
  if (location.hash === "#rules") location.replace("/Settings#agentic");
  const tabFromHash = () => (location.hash === "#datasets" ? "datasets" : location.hash === "#loras" ? "loras" : "train");
  const start = tabFromHash();
  showTab(start);
  if (start === "datasets") {
    let saved = ""; try { saved = localStorage.getItem("trainer-dataset") || ""; } catch (_) {}
    if (saved) openDataset(saved).catch(() => {});
  }
  window.addEventListener("hashchange", () => showTab(tabFromHash()));
})();
