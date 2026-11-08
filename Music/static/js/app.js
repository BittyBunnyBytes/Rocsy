const API_BASE = document.body.dataset.appPrefix || "";

const $ = (selector, parent = document) => parent.querySelector(selector);

const state = {
  jobs: [],
  comfyOnline: false,
  jobFilter: "all",
  jobSearch: "",
};

class Api {
  static async request(path, options = {}) {
    const init = { ...options, headers: { ...(options.headers || {}) } };
    if (init.body && !(init.body instanceof FormData)) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(`${API_BASE}${path}`, init);
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("json") ? await response.json() : null;
    if (!response.ok) throw Object.assign(new Error(data?.message || `Request failed (${response.status})`), { status: response.status });
    return data;
  }

  // For requests a phone may cut by leaving the browser: wait until the page is visible, retry network errors
  // and 5xx (tries that failed while hidden don't count), with one Idempotency-Key so a retry whose first try
  // got through returns what it made.
  static whenVisible() {
    if (document.visibilityState === "visible") return Promise.resolve();
    return new Promise((resolve) => {
      const check = () => { if (document.visibilityState === "visible") { document.removeEventListener("visibilitychange", check); resolve(); } };
      document.addEventListener("visibilitychange", check);
    });
  }

  static async durable(path, makeOptions, label) {
    const key = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
    let failures = 0, backgrounded = 0, told = false;
    for (;;) {
      await this.whenVisible();
      let hidden = false;
      const watch = () => { if (document.visibilityState !== "visible") hidden = true; };
      document.addEventListener("visibilitychange", watch);
      try {
        const options = makeOptions();
        return await this.request(path, { ...options, headers: { ...(options.headers || {}), "Idempotency-Key": key } });
      } catch (error) {
        if (error.status && error.status < 500 && error.status !== 408 && error.status !== 429) throw error;
        if ((hidden || document.visibilityState !== "visible") && backgrounded < 30) {
          backgrounded += 1;
          if (!told) toast(`${label}: paused while you were away; it carries on now you're back.`);
          told = true;
          continue;
        }
        failures += 1;
        if (failures >= 5) throw error;
        await new Promise((resolve) => setTimeout(resolve, Math.min(1500 * failures, 6000)));
      } finally {
        document.removeEventListener("visibilitychange", watch);
      }
    }
  }
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 5000);
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

function elapsed(startIso, endIso) {
  if (!startIso) return "";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remaining}s` : `${remaining}s`;
}

const STATE_LABELS = {
  waiting_for_gpu: "Waiting for GPU",
  preparing: "Preparing",
  queued: "Queued",
  rendering: "Rendering",
  complete: "Complete",
  failed: "Failed",
  cancelling: "Cancelling",
  cancelled: "Cancelled",
};

const ACTIVE_STATES = new Set(["waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"]);

/* ------------------------------------------------------------ LoRAs -- */
// The gallery lists every YuE2 / MiniMax Music 3 LoRA; picks (file -> strength) are kept per model.
const MAX_LORAS = 4;
const lora = { items: [], picked: { yue2: new Map(), minimax_music3: new Map() } };
lora.picked.yue2_pack = lora.picked.yue2;   // Studio sings YuE2 LoRAs: same gallery, same picks

function currentModel() { return $("#model").value; }

function renderLoras() {
  const model = currentModel() === "yue2_pack" ? "yue2" : currentModel(), picked = lora.picked[model];
  const list = lora.items.filter((item) => item.model === model);
  const gallery = $("#lora-gallery");
  $("#lora-sub").textContent = picked.size ? `${picked.size} in use` : `${list.length} for ${model === "yue2" ? "YuE2" : "MiniMax Music 3"}`;
  if (!list.length) {
    gallery.innerHTML = `<div class="lora-empty">No ${model === "yue2" ? "YuE2" : "MiniMax Music 3"} LoRAs yet. Train one in Trainer on a music dataset (songs with style captions and lyrics), or put one in models/loras/Music/${model === "yue2" ? "YuE2" : "MiniMaxMusic3"}/.</div>`;
    return;
  }
  gallery.replaceChildren(...list.map((item) => {
    const used = picked.has(item.file);
    const tile = document.createElement("article");
    tile.className = "lora-tile" + (used ? " is-used" : "");
    const meta = [item.made_by === "Trainer" ? "Trainer" : "", item.dataset ? `dataset ${item.dataset}` : "", item.steps ? `${item.steps} steps` : "", `${item.size_mb} MB`].filter(Boolean).join(" · ");
    tile.innerHTML = `
      <div class="lora-top"><strong title="${escapeHtml(item.file)}">${escapeHtml(item.name)}</strong>
        <button type="button" class="${used ? "primary-button" : "secondary-button"} lora-use">${used ? "In use" : "Use"}</button></div>
      <small class="lora-meta">${escapeHtml(meta)}</small>
      ${item.trigger ? `<button type="button" class="lora-trigger" title="Add the trigger word to the music direction">trigger: ${escapeHtml(item.trigger)}</button>` : ""}
      ${item.preview_url ? `<music-player compact src="${escapeHtml(item.preview_url)}" data-title="${escapeHtml(item.name)}"></music-player>` : `<small class="lora-meta">No preview song</small>`}
      ${used ? `<label class="lora-strength">Strength <output>${Number(picked.get(item.file)).toFixed(2)}</output><input type="range" min="0" max="1.5" step="0.05" value="${picked.get(item.file)}"></label>` : ""}`;
    const addTrigger = () => {
      const style = $("#style");
      if (item.trigger && !style.value.toLowerCase().includes(item.trigger.toLowerCase())) {
        style.value = style.value.trim() ? `${item.trigger}, ${style.value.trim()}` : item.trigger;
      }
    };
    tile.querySelector(".lora-use").addEventListener("click", () => {
      if (used) picked.delete(item.file);
      else if (picked.size >= MAX_LORAS) return toast(`At most ${MAX_LORAS} LoRAs at once.`, "error");
      else { picked.set(item.file, 1); addTrigger(); }
      renderLoras();
    });
    tile.querySelector(".lora-trigger")?.addEventListener("click", addTrigger);
    const range = tile.querySelector(".lora-strength input");
    range?.addEventListener("input", () => { picked.set(item.file, Number(range.value)); range.previousElementSibling.textContent = Number(range.value).toFixed(2); });
    return tile;
  }));
}

async function loadLoras() {
  try {
    lora.items = (await Api.request("/api/loras")).loras || [];
    const files = new Set(lora.items.map((item) => item.file));
    for (const picked of Object.values(lora.picked)) for (const file of [...picked.keys()]) if (!files.has(file)) picked.delete(file);
  } catch (error) {
    $("#lora-gallery").innerHTML = `<div class="lora-empty">Could not list LoRAs: ${escapeHtml(error.message)}</div>`;
    return;
  }
  renderLoras();
}

/* -------------------------------------------------------------- form -- */

function bindForm() {
  const form = $("#generate-form");
  const durationInput = $("#duration");
  const durationReadout = $("#duration-readout");
  const seedInput = $("#seed");
  const randomSeedButton = $("#random-seed");
  const modelSelect = $("#model");
  let minimaxMissing = [];
  const syncModel = () => {
    const minimax = modelSelect.value === "minimax_music3";
    const studio = modelSelect.value === "yue2_pack";
    $("#yue2-options").hidden = minimax || studio;
    $("#minimax-options").hidden = !minimax;
    $("#engine-label").textContent = studio ? "YuE2 Studio · INT8" : minimax ? "MiniMax Music 3 · INT8" : "YuE2 · INT8";
    syncPack();
    syncCover();
    $("#model-note").hidden = !minimax || !minimaxMissing.length;
    $("#model-note").textContent = minimaxMissing.length ? `${minimaxMissing.length} model file${minimaxMissing.length === 1 ? "" : "s"} missing. Generate offers to find or download them.` : "";
  };
  const savedModel = localStorage.getItem("music.model");
  if (["yue2", "minimax_music3", "yue2_pack"].includes(savedModel)) modelSelect.value = savedModel;
  modelSelect.addEventListener("change", () => {
    try { localStorage.setItem("music.model", modelSelect.value); } catch { /* storage blocked */ }
    syncModel();
    renderLoras();
  });
  $("#lora-refresh").addEventListener("click", loadLoras);
  loadLoras();
  syncModel();
  Api.request("/api/models").then((models) => {
    minimaxMissing = models.minimax_music3?.missing || [];
    syncModel();
  }).catch(() => {});

  durationInput.addEventListener("input", () => {
    durationReadout.textContent = `${durationInput.value} sec`;
  });

  const manualSampling = $("#custom_music_sampling");
  const outputFormat = $("#output_format");
  const outputQuality = $("#output_quality");
  manualSampling.addEventListener("change", () => {
    $("[data-manual-sampling]").hidden = !manualSampling.checked;
  });

  const syncAudioFormat = () => {
    const format = outputFormat.value;
    const choices = format === "mp3" ? ["V0", "128k", "320k"] : format === "opus" ? ["64k", "96k", "128k", "192k", "320k"] : [];
    $("[data-format-quality]").hidden = choices.length === 0;
    outputQuality.disabled = choices.length === 0;
    const selected = choices.includes(outputQuality.value) ? outputQuality.value : (format === "opus" ? "128k" : "V0");
    outputQuality.replaceChildren(...choices.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value === "V0" ? "V0 · variable bitrate" : `${value.replace("k", " kbps")}`;
      option.selected = value === selected;
      return option;
    }));
  };
  outputFormat.addEventListener("change", syncAudioFormat);
  syncAudioFormat();

  randomSeedButton.addEventListener("click", () => {
    seedInput.value = String(Math.floor(Math.random() * 2 ** 32));
  });

  window.MusicLyrics.bindEditor($("#lyrics"));
  document.querySelectorAll("[data-lyric-tag]").forEach((button) => button.classList.add(`ly-${window.MusicLyrics.tagKind(button.dataset.lyricTag)}`));
  document.querySelectorAll("[data-lyric-tag]").forEach((button) => button.addEventListener("click", () => {
    const lyrics = $("#lyrics");
    const tag = button.dataset.lyricTag;
    const start = lyrics.selectionStart;
    const before = lyrics.value.slice(0, start);
    const after = lyrics.value.slice(lyrics.selectionEnd);
    const insert = `${before && !before.endsWith("\n") ? "\n" : ""}${tag}\n`;
    lyrics.setRangeText(insert, start, lyrics.selectionEnd, "end");
    lyrics.focus();
  }));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (currentModel() === "yue2_pack") return runPack();
    if (currentModel() === "yue2" && cover) return runCover();
    const style = $("#style").value.trim();
    if (!style) {
      toast("Enter a style/genre description.", "error");
      return;
    }
    const payload = {};
    form.querySelectorAll("[data-request]").forEach((field) => {
      if (field.type === "checkbox") payload[field.name] = field.checked;
      else if (field.type === "number" || field.type === "range") {
        if (field.value.trim() !== "") payload[field.name] = field.name.includes("seed") ? field.value.trim() : Number(field.value);
      } else payload[field.name] = field.value;
    });
    payload.style = style;
    payload.loras = [...lora.picked[payload.model || currentModel()]].map(([file, strength]) => ({ file, strength }));
    if (payload.model === "yue2" && voice.id) Object.assign(payload, voiceFields());

    const button = $("#generate-button");
    button.disabled = true;
    try {
      await Api.request("/api/generate", { method: "POST", body: payload });
      toast("Track added to the GPU queue.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
}

/* -------------------------------------------------------------- jobs -- */

const JOBS_PAGE = 12;
let jobsShown = JOBS_PAGE;
const ACTIVE_JOB_STATES = new Set(["waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"]);

// The list is for music. The song read behind Whisper and Agentic reports in the boxes it writes to.
const isSongRead = (job) => job.task === "align" || (job.task === "transcribe" && (job.request?.whisper || job.request?.describe));
const listedJobs = () => state.jobs.filter((job) => !isSongRead(job));
const COVER_ICON = '<svg class="studio-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>';
const REMIX_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/><path d="M13 9l6-1"/></svg>';
const EDIT_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const TRASH_ICON = '<svg class="studio-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';

function filteredJobs() {
  const query = state.jobSearch.trim().toLowerCase();
  return listedJobs().filter((job) => {
    const matchesState = state.jobFilter === "all" ||
      (state.jobFilter === "running" ? ACTIVE_JOB_STATES.has(job.state) : job.state === state.jobFilter);
    return matchesState && (!query || `${job.style} ${job.lyrics} ${job.model}`.toLowerCase().includes(query));
  });
}

function morphJobNode(current, next) {
  if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) return next;
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return current;
  }
  if (current.nodeType !== Node.ELEMENT_NODE) return current;
  // Keep players intact across SSE updates: playback, lyrics scroll and the
  // open tab must survive another job's progress or queue state changing.
  if (current.nodeName === "MUSIC-PLAYER" && current.getAttribute("src") === next.getAttribute("src")) {
    for (const name of ["data-timing", "data-cover", "data-cover-state", "data-cover-stage"]) {
      const value = next.getAttribute(name) || "";
      if ((current.getAttribute(name) || "") !== value) current.setAttribute(name, value);
    }
    return current;
  }
  for (const attribute of [...current.attributes]) {
    if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
  for (const attribute of [...next.attributes]) {
    if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
  }
  const currentChildren = [...current.childNodes];
  const nextChildren = [...next.childNodes];
  const length = Math.max(currentChildren.length, nextChildren.length);
  for (let index = 0; index < length; index += 1) {
    const oldChild = current.childNodes[index];
    const newChild = nextChildren[index];
    if (!newChild) oldChild?.remove();
    else if (!oldChild) current.append(newChild);
    else {
      const patched = morphJobNode(oldChild, newChild);
      if (patched !== oldChild) oldChild.replaceWith(patched);
    }
  }
  return current;
}

function reconcileJobCards(list, markup) {
  const template = document.createElement("template");
  template.innerHTML = markup;
  const existing = new Map([...list.querySelectorAll(":scope > .job-card")].map((card) => [card.dataset.jobId, card]));
  const desired = [...template.content.children].map((card) => {
    const previous = existing.get(card.dataset.jobId);
    existing.delete(card.dataset.jobId);
    return previous ? morphJobNode(previous, card) : card;
  });
  for (const stale of existing.values()) stale.remove();
  list.querySelectorAll(":scope > :not(.job-card)").forEach((node) => node.remove());
  let anchor = null;
  for (let index = desired.length - 1; index >= 0; index -= 1) {
    const card = desired[index];
    if (card.parentElement !== list || card.nextElementSibling !== anchor) list.insertBefore(card, anchor);
    anchor = card;
  }
}

function renderJobs() {
  syncPack();
  const list = $("#jobs-list");
  const pager = $("#jobs-pager");
  const jobs = filteredJobs();
  const listed = listedJobs();
  const counts = { all: listed.length, running: listed.filter((job) => ACTIVE_JOB_STATES.has(job.state)).length,
    complete: listed.filter((job) => job.state === "complete").length, failed: listed.filter((job) => job.state === "failed").length };
  Object.entries(counts).forEach(([key, count]) => { $(`#count-${key}`).textContent = count; });
  if (!jobs.length) {
    list.replaceChildren(Object.assign(document.createElement("div"), {
      className: "jobs-empty",
      textContent: listed.length ? "No tracks match this view." : "No generations yet. Describe a track and hit Generate.",
    }));
    pager.hidden = true;
    return;
  }
  const shown = jobs.slice(0, jobsShown);
  pager.hidden = shown.length >= jobs.length;
  pager.innerHTML = pager.hidden ? "" : `<span>Showing ${shown.length} of ${jobs.length}</span><button type="button" class="secondary-button" id="jobs-more">Show ${Math.min(JOBS_PAGE, jobs.length - shown.length)} more</button>`;
  const markup = shown.map((job) => {
    const progress = ACTIVE_STATES.has(job.state) ? job.progress ?? 0 : job.state === "complete" ? 100 : 0;
    const isTerminal = job.state === "complete" || job.state === "failed" || job.state === "cancelled";
    const canCancel = ACTIVE_STATES.has(job.state);
    return `
      <div class="job-card" data-job-id="${job.id}">
        <div class="job-head">
          <span class="job-style" title="${escapeHtml(job.style)}">${escapeHtml(job.style)}</span>
          <span class="job-state state-${job.state}">${STATE_LABELS[job.state] || job.state}</span>
        </div>
        <div class="job-meta">
          <span class="job-chip">${job.model === "minimax_music3" ? "MiniMax Music 3" : job.task === "cover" ? "Cover" : job.task ? "YuE2 Studio" : "YuE2"}</span>
          ${job.request?.remix_of ? `<span class="job-chip" title="New words, same seed and score">Remix</span>` : ""}
          ${job.duration ? `<span class="job-chip">${job.duration}s requested</span>` : ""}
          <span class="job-chip">seed ${job.seed_text || job.seed}</span>
          <span class="job-elapsed" data-start="${job.started_at || job.created_at}" data-end="${isTerminal ? job.completed_at || "" : ""}"></span>
        </div>
        ${ACTIVE_STATES.has(job.state) ? `<div class="job-progress"><div class="job-progress-bar" style="width:${progress}%"></div></div>` : ""}
        ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
        ${job.cover_state === "failed" ? `<div class="job-error">Song cover: ${escapeHtml(job.cover_error || "failed")}</div>` : ""}
        ${job.state === "complete" && job.output_url ? `<music-player src="${escapeHtml(job.output_url)}" data-cover="${escapeHtml(job.cover_url || "")}" data-cover-state="${job.cover_state || ""}" data-cover-stage="${escapeHtml(job.cover_stage || "")}" data-lyrics="${escapeHtml(job.sung_lyrics || job.lyrics || "")}" data-duration="${Number(job.duration) || 0}" data-title="${escapeHtml(job.style)}" data-sync-id="${escapeHtml(job.id)}" data-timing="${job.lyric_timing || ""}"></music-player>` : ""}
        <div class="job-actions">
          <button type="button" class="secondary-button job-icon-button" data-job-info="${job.id}" title="Generation details" aria-label="Generation details">${window.BunnyStudioIcons?.icon("info") || "ⓘ"}</button>
          ${canCancel ? `<button type="button" class="secondary-button job-icon-button danger" data-cancel="${job.id}" title="Cancel" aria-label="Cancel">${window.BunnyStudioIcons?.icon("cancel") || "■"}</button>` : `<button type="button" class="secondary-button job-icon-button" data-retry="${job.id}" title="Retry: load the exact settings, reference and seed into the form" aria-label="Retry: load the exact settings, reference and seed into the form">${window.BunnyStudioIcons?.icon("retry") || "↻"}</button>`}
          ${job.state === "complete" && job.output_url ? (job.cover_state === "running"
            ? `<button type="button" class="secondary-button job-cover-working" disabled aria-live="polite"><span class="cover-spinner" aria-hidden="true"></span>${escapeHtml(job.cover_stage || "Making the cover")}…</button>`
            : `<button type="button" class="secondary-button job-icon-button" data-cover="${job.id}" title="${job.cover_url ? "Make a new song cover" : "Make a song cover"}" aria-label="${job.cover_url ? "Make a new song cover" : "Make a song cover"}">${COVER_ICON}</button>`) : ""}
          ${editable(job) ? `<button type="button" class="secondary-button job-icon-button" data-edit-song="${job.id}" title="Edit: retake, cut, extend or re-word part of the song" aria-label="Edit: retake, cut, extend or re-word part of the song">${EDIT_ICON}</button>` : ""}
          ${remixKind(job) ? `<button type="button" class="secondary-button job-icon-button" data-remix="${job.id}" title="Remix: new words, same seed and score" aria-label="Remix: new words, same seed and score">${REMIX_ICON}</button>` : ""}
          ${job.state === "complete" && job.download_url ? `<a class="secondary-button job-icon-button" href="${job.download_url}" download title="Download" aria-label="Download">${window.BunnyStudioIcons?.icon("download") || "↓"}</a>` : ""}
          ${canCancel ? "" : `<button type="button" class="secondary-button job-icon-button danger" data-delete="${job.id}" title="Delete" aria-label="Delete">${TRASH_ICON}</button>`}
        </div>
      </div>`;
  }).join("");
  reconcileJobCards(list, markup);
  updateElapsedTimes();
}

$("#jobs-list").addEventListener("click", async (event) => {
  const info = event.target.closest("[data-job-info]");
  if (info) return showJobInfo(info.dataset.jobInfo);
  const retry = event.target.closest("[data-retry]");
  if (retry) return retryJob(retry.dataset.retry);
  const editSong = event.target.closest("[data-edit-song]");
  if (editSong) return openEdit(editSong.dataset.editSong);
  const remix = event.target.closest("[data-remix]");
  if (remix) return openRemix(remix.dataset.remix);
  const remove = event.target.closest("[data-delete]");
  if (remove) return deleteJob(remove);
  const art = event.target.closest(".mp-art");
  if (art) return showCover(art.closest(".job-card")?.dataset.jobId);
  const cover = event.target.closest("button[data-cover]");
  if (cover) return makeCover(cover.dataset.cover, cover);
  const button = event.target.closest("[data-cancel]");
  if (!button) return;
  button.disabled = true;
  try {
    await Api.request(`/api/jobs/${button.dataset.cancel}/cancel`, { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
});

async function makeCover(id, button) {
  button.disabled = true;
  // Show it working right away; the server's job updates take over from here.
  const job = state.jobs.find((item) => item.id === id);
  const before = job && { cover_state: job.cover_state, cover_stage: job.cover_stage, cover_error: job.cover_error };
  if (job) {
    Object.assign(job, { cover_state: "running", cover_stage: "Starting", cover_error: null });
    renderJobs();
  }
  try {
    const updated = await Api.request(`/api/jobs/${id}/cover`, { method: "POST" });
    const current = state.jobs.find((item) => item.id === id);
    if (current && updated?.cover_state) Object.assign(current, { cover_state: updated.cover_state, cover_stage: updated.cover_stage });
    renderJobs();
    return true;
  } catch (error) {
    toast(error.message, "error");
    if (job) {
      Object.assign(job, before);
      renderJobs();
    }
    button.disabled = false;
    return false;
  }
}

function showCover(id) {
  const job = state.jobs.find((item) => item.id === id);
  if (!job?.cover_url) return;
  const dialog = $("#cover-view");
  $("#cover-view-image").src = job.cover_url;
  $("#cover-view-title").textContent = job.style;
  $("#cover-view-download").href = job.cover_url;
  $("#cover-view-download").download = `${(job.style || "song").replace(/[^\w -]+/g, "").trim().slice(0, 60) || "song"} cover.png`;
  $("#cover-view-new").dataset.coverJob = id;
  $("#cover-view-new").disabled = false;
  window.BunnyStudioDialog?.open(dialog);
}

$("#cover-view-new").addEventListener("click", async (event) => {
  if (await makeCover(event.currentTarget.dataset.coverJob, event.currentTarget)) window.BunnyStudioDialog?.close?.($("#cover-view"));
});

async function deleteJob(button) {
  const id = button.dataset.delete;
  const job = state.jobs.find((item) => item.id === id);
  const kept = job?.output_url ? " The audio file stays in the output folder." : "";
  if (!confirm(`Delete "${(job?.style || "this entry").slice(0, 80)}" from the list?${kept}`)) return;
  button.disabled = true;
  try {
    await Api.request(`/api/jobs/${id}`, { method: "DELETE" });
    state.jobs = state.jobs.filter((item) => item.id !== id);
    renderJobs();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
}

// Like Video's Retry: the job's exact settings, reference and seed go back
// into the form for review instead of being queued straight away.
function retryJob(jobId) {
  const studioJob = state.jobs.find((item) => item.id === jobId);
  if (studioJob?.task === "cover") return loadCoverRequest(studioJob.request, studioJob.seed_text || studioJob.seed);
  if (studioJob?.task) return loadPackRequest({ ...studioJob.request, seed: studioJob.seed_text || studioJob.seed });
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job?.request?.style) {
    toast("The saved generation settings are unavailable.", "error");
    return;
  }
  const request = { ...job.request, seed: job.seed_text || job.seed };
  const form = $("#generate-form");
  form.querySelectorAll("[data-request]").forEach((field) => {
    if (!(field.name in request)) return;
    const value = request[field.name];
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  try { localStorage.setItem("music.model", $("#model").value); } catch { /* storage blocked */ }
  chooseVoice(request.voice_id ? "custom" : "source", request.voice_id || "", request);
  // Re-run the form's own syncing (model panels, reference/sampling sections, readouts).
  ["#model", "#custom_music_sampling", "#output_format"]
    .forEach((id) => $(id).dispatchEvent(new Event("change")));
  // An older job's quality may not exist for this format: keep the default syncAudioFormat just picked.
  if ([...$("#output_quality").options].some((option) => option.value === request.output_quality)) $("#output_quality").value = request.output_quality;
  lora.picked[$("#model").value] = new Map((request.loras || []).map((item) => [item.file, item.strength]));
  renderLoras();
  ["#duration"].forEach((id) => $(id).dispatchEvent(new Event("input")));
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  toast("Loaded the exact settings, reference and seed. Review, then Generate.");
}

function showJobInfo(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  const body = $("#job-info-body");
  const promptSection = (title, value, key) => `
    <section class="studio-prompt-section">
      <div class="studio-prompt-heading"><h3>${title}</h3>
        <button type="button" class="secondary-button job-icon-button" data-copy-prompt="${key}" title="Copy ${title.toLowerCase()}" aria-label="Copy ${title.toLowerCase()}">${window.BunnyStudioIcons?.icon("copy") || "▢"}</button>
      </div>
      <pre class="studio-prompt-text">${key === "lyrics" && job.lyrics ? window.MusicLyrics.highlight(job.lyrics) : escapeHtml(value)}</pre>
    </section>`;
  let tab = "details";
  try { tab = localStorage.getItem(JOB_INFO_TAB_KEY) === "stats" ? "stats" : "details"; } catch { /* storage blocked */ }
  body.innerHTML = `
    <div class="job-info-tabs" role="tablist" aria-label="Generation details">
      <button type="button" role="tab" data-info-tab="details">Details</button>
      <button type="button" role="tab" data-info-tab="stats">Stats</button>
    </div>
    <div class="job-info-pane" data-info-pane="stats" role="tabpanel"><p class="field-hint">Loading…</p></div>
    <div class="job-info-pane" data-info-pane="details" role="tabpanel">
      ${songFactsHtml(job)}
      ${job.task ? packResultHtml(job) : `${promptSection("Style", job.style, "style")}${promptSection("Lyrics", job.lyrics || "Instrumental (no lyrics provided).", "lyrics")}`}
    </div>`;
  body.querySelector("[data-pack-use]")?.addEventListener("click", () => {
    usePackResult(job);
    window.BunnyStudioDialog?.close?.($("#job-info"));
  });
  body.querySelectorAll("[data-copy-prompt]").forEach((button) => {
    button.addEventListener("click", async () => {
      const text = button.dataset.copyPrompt === "style" ? job.style : job.lyrics;
      try {
        await navigator.clipboard.writeText(text || "");
        toast(`${button.dataset.copyPrompt === "style" ? "Style" : "Lyrics"} copied.`);
      } catch {
        toast("Could not copy. Select the prompt text instead.", "error");
      }
    });
  });
  const showTab = (name) => {
    body.querySelectorAll("[data-info-tab]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.infoTab === name);
      button.setAttribute("aria-selected", String(button.dataset.infoTab === name));
    });
    body.querySelectorAll("[data-info-pane]").forEach((pane) => { pane.hidden = pane.dataset.infoPane !== name; });
    try { localStorage.setItem(JOB_INFO_TAB_KEY, name); } catch { /* storage blocked */ }
  };
  body.querySelectorAll("[data-info-tab]").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.infoTab)));
  showTab(tab);
  window.BunnyStudioDialog?.open("#job-info");
  loadJobStats(body.querySelector('[data-info-pane="stats"]'), job.id);
}

// The info dialog's Stats tab (like Video's): where the generation's time went, from
// GET /api/jobs/{id}/stats (backend/job_stats.py) - a workflow map in run order, then
// every stage slowest first, samplers with their per-step times.
const JOB_INFO_TAB_KEY = "music-job-info-tab";
const STATS_CATEGORY_ORDER = ["wait", "load", "compose", "encode", "sample", "decode", "output", "other"];

function formatSeconds(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${total % 60}s` : `${total}s`;
}

function formatStatSeconds(seconds) {
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return formatSeconds(seconds);
  const total = Math.round(seconds);
  return `${Math.floor(total / 3600)}h ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}m`;
}

function formatStatShare(seconds, total) {
  const share = total > 0 ? (seconds / total) * 100 : 0;
  return share >= 1 || share === 0 ? `${Math.round(share)}%` : "<1%";
}

function jobStatsHtml(stats) {
  if (!stats || !stats.available) {
    return `<p class="field-hint">${escapeHtml(stats?.message || "No stage timings for this generation.")}</p>`;
  }
  const total = stats.total || 0;
  const stages = stats.stages || [];
  const tip = (stage) => `${stage.label} · ${formatStatSeconds(stage.seconds)} (${formatStatShare(stage.seconds, total)})`;
  const ranked = stages.map((stage, index) => ({ ...stage, index })).sort((a, b) => b.seconds - a.seconds);
  const slowest = ranked[0];
  const work = ranked.filter((stage) => stage.category !== "wait");
  const longest = slowest?.seconds || 1;
  const quick = ranked.filter((stage) => stage.seconds < Math.max(1, total * 0.01));
  const shown = ranked.filter((stage) => !quick.includes(stage));
  const row = (stage) => {
    const steps = stage.steps || [];
    const stepMax = Math.max(...steps, 0.001);
    const later = steps.slice(1);
    const average = later.length ? later.reduce((sum, value) => sum + value, 0) / later.length : 0;
    const stepWord = /sampler/i.test(stage.class_type || "") ? "step" : "tick";
    const head = `
      <span class="stats-row-label"><span class="stats-dot" data-cat="${escapeHtml(stage.category)}"></span>
        <span class="stats-row-name">${escapeHtml(stage.label)}${stage.running ? ` <em class="stats-running">running</em>` : ""}</span>
        ${stage.class_type && stage.class_type !== stage.label ? `<span class="stats-row-class">${escapeHtml(stage.class_type)}</span>` : ""}</span>
      <span class="stats-row-bar"><span data-cat="${escapeHtml(stage.category)}" style="width:${Math.max(0.5, (stage.seconds / longest) * 100).toFixed(2)}%"></span></span>
      <span class="stats-row-time">${formatStatSeconds(stage.seconds)}</span>
      <span class="stats-row-share">${formatStatShare(stage.seconds, total)}</span>`;
    if (!steps.length) {
      return `<li class="stats-row"${stage.detail ? ` title="${escapeHtml(stage.detail)}"` : ""}>${head}</li>`;
    }
    return `<li class="stats-row"><details><summary>${head}</summary>
      <div class="stats-steps">
        <p class="field-hint">${steps.length} ${stepWord}s · first ${formatStatSeconds(steps[0])} (includes getting the model onto the GPU)${average ? ` · then ${formatStatSeconds(average)} per ${stepWord} on average` : ""}</p>
        <div class="stats-step-bars" role="img" aria-label="Seconds per ${stepWord}">${steps.map((value, index) => `
          <span data-tip="${escapeHtml(`${stepWord[0].toUpperCase()}${stepWord.slice(1)} ${index + 1} · ${formatStatSeconds(value)}`)}" style="height:${Math.max(3, (value / stepMax) * 100).toFixed(1)}%"></span>`).join("")}
        </div>
      </div></details></li>`;
  };
  const categories = [...(stats.categories || [])].sort((a, b) => STATS_CATEGORY_ORDER.indexOf(a.key) - STATS_CATEGORY_ORDER.indexOf(b.key));
  return `
    <div class="stats-tiles">
      <div class="stats-tile"><span>${stats.running ? "So far" : "Total time"}</span><strong>${formatStatSeconds(total)}</strong></div>
      <div class="stats-tile"><span>Slowest stage</span><strong>${escapeHtml(slowest ? slowest.label : "-")}</strong>
        <small>${slowest ? `${formatStatSeconds(slowest.seconds)} · ${formatStatShare(slowest.seconds, total)} of the total` : ""}</small></div>
      <div class="stats-tile"><span>Slowest work</span><strong>${escapeHtml(work[0] ? work[0].label : "-")}</strong>
        <small>${work[0] ? `${formatStatSeconds(work[0].seconds)} · ${formatStatShare(work[0].seconds, total)}` : ""}</small></div>
      <div class="stats-tile"><span>Nodes</span><strong>${stats.nodes_run} ran</strong><small>${stats.nodes_cached} cached (skipped)</small></div>
    </div>
    <section class="job-info-section">
      <h3>Workflow map${stats.running ? " · live" : ""}</h3>
      <div class="stats-map" role="img" aria-label="Time per stage in run order">${stages.map((stage) => `
        <span data-cat="${escapeHtml(stage.category)}" data-tip="${escapeHtml(tip(stage))}" style="flex-grow:${stage.seconds}"></span>`).join("")}
      </div>
      <div class="stats-axis"><span>0s</span><span>${formatStatSeconds(total / 2)}</span><span>${formatStatSeconds(total)}</span></div>
      <ul class="stats-legend">${categories.map((category) => `
        <li><span class="stats-dot" data-cat="${escapeHtml(category.key)}"></span>${escapeHtml(category.label)}
          <b>${formatStatSeconds(category.seconds)}</b><small>${formatStatShare(category.seconds, total)}</small></li>`).join("")}
      </ul>
    </section>
    <section class="job-info-section">
      <h3>Stages, slowest first</h3>
      <ol class="stats-rows">${shown.map(row).join("")}</ol>
      ${quick.length ? `<details class="stats-quick"><summary>${quick.length} quick stage${quick.length === 1 ? "" : "s"} · ${formatStatSeconds(quick.reduce((sum, stage) => sum + stage.seconds, 0))} together</summary>
        <ol class="stats-rows">${quick.map(row).join("")}</ol></details>` : ""}
    </section>
    <div class="stats-tip" hidden></div>`;
}

// Fills the Stats pane, binds the hover/tap tooltip once, and refreshes a running job every 5 s while the dialog is open.
async function loadJobStats(pane, jobId) {
  const show = (event) => {
    const tipBox = pane.querySelector(".stats-tip");
    if (!tipBox) return;
    const target = event.target.closest("[data-tip]");
    if (!target) { tipBox.hidden = true; return; }
    tipBox.textContent = target.dataset.tip;
    tipBox.hidden = false;
    const box = pane.getBoundingClientRect();
    tipBox.style.left = `${Math.min(Math.max(0, event.clientX - box.left + 12), Math.max(0, box.width - tipBox.offsetWidth))}px`;
    tipBox.style.top = `${event.clientY - box.top + 16}px`;
  };
  pane.addEventListener("pointermove", show);
  pane.addEventListener("pointerdown", show);
  pane.addEventListener("pointerleave", () => { const tipBox = pane.querySelector(".stats-tip"); if (tipBox) tipBox.hidden = true; });
  const refresh = async () => {
    if ($("#job-info").hidden || !pane.isConnected) return;
    let stats;
    try {
      stats = await Api.request(`/api/jobs/${jobId}/stats`);
    } catch (error) {
      if (!pane.querySelector(".stats-tiles")) pane.innerHTML = `<p class="job-error">${escapeHtml(error.message || "Could not load the stats.")}</p>`;
      setTimeout(refresh, 5000);
      return;
    }
    if (!pane.isConnected) return;
    const open = new Set([...pane.querySelectorAll(".stats-row details[open] .stats-row-name")].map((node) => node.textContent));
    pane.innerHTML = jobStatsHtml(stats);
    pane.querySelectorAll(".stats-row details").forEach((node) => { if (open.has(node.querySelector(".stats-row-name")?.textContent)) node.open = true; });
    const job = state.jobs.find((item) => item.id === jobId);
    if (stats.running || (job && ACTIVE_JOB_STATES.has(job.state))) setTimeout(refresh, 5000);
  };
  await refresh();
}

document.querySelectorAll("[data-job-filter]").forEach((button) => button.addEventListener("click", () => {
  state.jobFilter = button.dataset.jobFilter;
  jobsShown = JOBS_PAGE;
  document.querySelectorAll("[data-job-filter]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  renderJobs();
}));
$("#jobs-search").addEventListener("input", (event) => {
  state.jobSearch = event.target.value;
  jobsShown = JOBS_PAGE;
  renderJobs();
});

$("#jobs-pager").addEventListener("click", (event) => {
  if (!event.target.closest("#jobs-more")) return;
  jobsShown += JOBS_PAGE;
  renderJobs();
});

function updateElapsedTimes() {
  document.querySelectorAll(".job-elapsed").forEach((node) => {
    const { start, end } = node.dataset;
    if (!start) return;
    node.textContent = elapsed(start, end || undefined);
  });
}

/* --------------------------------------------------------------- sse -- */

function bindEvents() {
  const source = new EventSource(`${API_BASE}/api/events`);
  source.addEventListener("jobs", (event) => {
    state.jobs = JSON.parse(event.data);
    watchWhisper();
    renderJobs();
  });
  source.addEventListener("status", (event) => {
    const data = JSON.parse(event.data);
    state.comfyOnline = !!data.comfyui?.online;
    const dot = $("#comfy-status .status-dot");
    const label = $("#comfy-status strong");
    dot.classList.toggle("online", state.comfyOnline);
    label.textContent = state.comfyOnline ? "Online" : "Offline";
  });
  source.onerror = () => {
    // EventSource retries automatically; nothing else to do here.
  };
}

async function refreshJobsOnce() {
  try {
    state.jobs = await Api.request("/api/jobs");
    watchWhisper();
    renderJobs();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* -------------------------------------------------------------- init -- */

function setView(view) {
  document.body.dataset.view = view;
  document.querySelectorAll(".view-tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  // Only hidden, so a track that is playing keeps playing in Settings.
  $("#studio-view").hidden = view !== "studio";
  $("#settings-view").hidden = view !== "settings";
  try { sessionStorage.setItem("music.view", view); } catch { /* storage blocked */ }
}

function bindSettings() {
  document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
  document.querySelectorAll('input[name="default-viz"]').forEach((input) => {
    input.checked = input.value === window.MusicViz.getDefault();
    input.addEventListener("change", () => window.MusicViz.setDefault(input.value));
  });
  const vizCoverColors = $("#viz-cover-colors-setting");
  vizCoverColors.checked = window.MusicViz.getCoverColors();
  vizCoverColors.addEventListener("change", () => window.MusicViz.setCoverColors(vizCoverColors.checked));
  let view = "studio";
  try { view = sessionStorage.getItem("music.view") === "settings" ? "settings" : "studio"; } catch { /* storage blocked */ }
  setView(view);
  const timing = $("#lyric-timing-setting");
  const whisperModel = $("#whisper-model-setting");
  const cover = $("#song-cover-setting");
  const coverAgent = $("#cover-agent-setting");
  const coverImageModel = $("#cover-image-model-setting");
  const coverReads = $("#cover-reads-setting");
  const coverPrompt = $("#cover-prompt-setting");
  const coverPreset = $("#cover-preset-setting");
  const coverSize = $("#cover-size-setting");
  const coverSizeCustom = $("#cover-size-custom");
  const coverWidth = $("#cover-width-setting");
  const coverHeight = $("#cover-height-setting");
  let coverSizes = {};     // image model -> [{id: "WxH", label}]
  const sizesFor = (model) => (coverSizes[model] || []).map(({ id }) => id);
  // Quick sizes for the chosen image model, grouped like the Image studio's; any other saved size is Custom.
  const showSizes = () => {
    const groups = { Square: [], Landscape: [], Portrait: [] };
    for (const { id, label } of coverSizes[coverImageModel.value] || []) {
      const [w, h] = id.split("x").map(Number);
      groups[w === h ? "Square" : w > h ? "Landscape" : "Portrait"].push(new Option(label, id));
    }
    const options = [new Option("From the preset", "")];
    for (const [name, items] of Object.entries(groups)) {
      if (!items.length) continue;
      const group = document.createElement("optgroup");
      group.label = name;
      group.append(...items);
      options.push(group);
    }
    options.push(new Option("Custom…", "custom"));
    coverSize.replaceChildren(...options);
    const chosen = saved?.cover_size || "";
    const quick = !chosen || sizesFor(coverImageModel.value).includes(chosen);
    coverSize.value = quick ? chosen : "custom";
    coverSizeCustom.hidden = quick;
    if (!quick) [coverWidth.value, coverHeight.value] = chosen.split("x");
    const max = coverImageModel.value === "qwen_image_21" ? 2752 : 1536;
    coverWidth.max = coverHeight.max = String(max);
  };
  // What the size controls say now: "" (from the preset), a quick size, or the custom W x H.
  const chosenSize = () => {
    if (coverSize.value !== "custom") return coverSize.value;
    const w = parseInt(coverWidth.value, 10), h = parseInt(coverHeight.value, 10);
    return w > 0 && h > 0 ? `${w}x${h}` : saved?.cover_size || "";
  };
  let saved = null;
  let coverPresets = {};   // image model -> preset names (the Image studio's)
  // The presets saved for the chosen image model; a saved one that's gone stays visible, marked.
  const showPresets = () => {
    const names = coverPresets[coverImageModel.value] || [];
    const options = [new Option("None: current settings", ""), ...names.map((name) => new Option(name, name))];
    const chosen = saved?.cover_preset || "";
    if (chosen && !names.includes(chosen)) options.push(new Option(`${chosen} (not saved for this model: current settings)`, chosen));
    coverPreset.replaceChildren(...options);
    coverPreset.value = chosen;
  };
  const show = () => {
    timing.checked = saved.lyric_timing;
    whisperModel.value = saved.whisper_model;
    cover.checked = saved.song_cover;
    // A saved agent the model list doesn't have (yet) stays selectable.
    if (saved.cover_agent && ![...coverAgent.options].some((option) => option.value === saved.cover_agent)) coverAgent.append(new Option(saved.cover_agent, saved.cover_agent));
    coverAgent.value = saved.cover_agent;
    coverReads.value = saved.cover_reads || "both";
    coverImageModel.value = saved.cover_image_model;
    coverPrompt.value = saved.cover_system_prompt;
    showPresets();
    showSizes();
    $("#song-cover-options").hidden = !saved.song_cover;
    window.MusicTiming.setEnabled(saved.lyric_timing);
  };
  Api.request("/api/settings").then((loaded) => {
    saved = loaded;
    whisperModel.replaceChildren(...loaded.whisper_models.map(({ id, label }) => new Option(label, id)));
    coverImageModel.replaceChildren(...loaded.cover_image_models.map(({ id, label }) => new Option(label, id)));
    coverSizes = loaded.cover_sizes || {};
    show();
  }).catch(() => {});
  const loadPresets = () => Api.request("/api/cover-presets").then((data) => {
    coverPresets = data || {};
    if (saved) showPresets();
  }).catch(() => {});
  loadPresets();
  // Presets saved in the Image studio meanwhile show up when you come back to this tab.
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loadPresets(); });
  Api.request("/api/agentic/models").then((data) => {
    coverAgent.replaceChildren(new Option(`Image Agentic's model`, ""), ...data.models.map((id) => new Option(id, id)));
    if (saved) show();
  }).catch(() => {});
  const save = async () => {
    if (!saved) return;
    try {
      const body = {
        lyric_timing: timing.checked, whisper_model: whisperModel.value, song_cover: cover.checked,
        cover_agent: coverAgent.value, cover_reads: coverReads.value, cover_image_model: coverImageModel.value, cover_system_prompt: coverPrompt.value,
        // A preset belongs to one image model: switching the model goes back to the current settings.
        cover_preset: coverImageModel.value === saved.cover_image_model ? coverPreset.value : "",
        // So does a size the new model has no quick option for (its limits differ).
        cover_size: coverImageModel.value === saved.cover_image_model || sizesFor(coverImageModel.value).includes(chosenSize())
          ? chosenSize() : "",
      };
      Object.assign(saved, await Api.request("/api/settings", { method: "PUT", body }));
    } catch (error) {
      toast(error.message, "error");
    }
    show();
  };
  [timing, whisperModel, cover, coverAgent, coverReads, coverImageModel, coverPrompt, coverPreset, coverWidth, coverHeight].forEach((input) => input.addEventListener("change", save));
  coverSize.addEventListener("change", () => {
    if (coverSize.value !== "custom") return save();
    // Custom starts from the size in use, to edit from there.
    const [w, h] = (saved?.cover_size || "1024x1024").split("x");
    coverWidth.value ||= w;
    coverHeight.value ||= h;
    coverSizeCustom.hidden = false;
    coverWidth.focus();
  });
  $("#cover-prompt-reset").addEventListener("click", () => {
    coverPrompt.value = saved?.default_cover_system_prompt || "";
    save();
  });
}

bindSettings();
/* ------------------------------------------------------- YuE2 Studio -- */
// The pytraveler/YuE2-ComfyUI tasks (backend/pack.py). Text tasks end in the job's (i) dialog: "Use in form".
const PACK_SECTIONS = {
  song: ["song", "seed"], plan: ["song", "seed"], write: ["write", "language", "seed"],
  transcribe: ["file", "score-mode", "transcribe", "language", "seed"], midi: ["file", "score-mode", "midi"],
  vocals: ["file"], edit: ["edit"],
};
const PACK_OP_FIELDS = {
  retake: ["bars", "sung"], cut: ["bars"], words: ["lines", "text", "sung"], extend: ["text", "sung"],
  move: ["bars", "to"], break: ["to", "length", "sung"], restyle: ["bars", "restyle", "sung"],
};
let packUpload = null;   // {name, label} of the uploaded file, per task kind
const packFileKind = (task) => (task === "midi" ? "midi" : "audio");

function syncPack() {
  const on = $("#model").value === "yue2_pack";
  const task = $("#pk_task").value;
  $("#pack-card").hidden = !on;
  document.body.classList.toggle("is-yue2-studio", on);   // Agentic + workflow files drive the standard graphs only
  $("#output-card").hidden = on;
  $("#style").closest(".card").hidden = on && !["song", "plan"].includes(task);
  document.querySelectorAll("[data-pk]").forEach((node) => { node.hidden = !(PACK_SECTIONS[task] || []).includes(node.dataset.pk); });
  $("#pk_file").accept = packFileKind(task) === "midi" ? ".mid,.midi,.kar,.rmi" : "audio/*,.flac,.wav,.mp3,.ogg,.opus,.m4a";
  document.querySelectorAll("[data-op]").forEach((node) => { node.hidden = !(PACK_OP_FIELDS[$("#pk_op").value] || []).includes(node.dataset.op); });
  const source = $("#pk_source"), chosen = source.value;
  const songs = state.jobs.filter(editable);
  source.replaceChildren(...songs.map((job) => {
    const option = document.createElement("option");
    option.value = job.id;
    option.textContent = `${job.request?.task === "cover" ? "Cover: " : ""}${job.style.replace(/^Cover: /, "")}`.slice(0, 80);
    return option;
  }));
  if (songs.some((job) => job.id === chosen)) source.value = chosen;
}

function packOp() {
  const op = $("#pk_op").value, fields = PACK_OP_FIELDS[op], edit = { op };
  const number = (id) => { const value = $(id).value.trim(); return value === "" ? null : Number(value); };
  if (fields.includes("bars")) {
    const from = number("#pk_from"), until = number("#pk_until");
    if (from === null || until === null || until <= from) throw new Error("Enter the bars: from, then a later bar to stop before.");
    edit.bars = [from, until];
  }
  if (fields.includes("lines")) {
    const from = number("#pk_line_from"), until = number("#pk_line_until");
    if (from === null || until === null || until <= from) throw new Error("Enter the lines: from, then a later line to stop before.");
    edit.lines = [from, until];
  }
  if (fields.includes("to")) {
    if (number("#pk_to") === null) throw new Error("Enter the bar line.");
    edit.to = number("#pk_to");
  }
  if (fields.includes("length")) edit.length = number("#pk_len") || 4;
  if (fields.includes("text")) {
    edit.text = $("#pk_op_text").value;
    if (op === "words" && !edit.text.trim()) throw new Error("Enter the new words.");
  }
  if (fields.includes("restyle")) {
    edit.style = $("#pk_op_style").value.trim();
    if (!edit.style) throw new Error("Enter the new style.");
    edit.melody = "same";
  }
  if (fields.includes("sung")) {
    edit.seed = Math.floor(Math.random() * 2 ** 32);
    edit.takes = number("#pk_op_takes") || 1;
    edit.take = 0;
  }
  return edit;
}

function packEdits() {
  let edits;
  try { edits = JSON.parse($("#pk_edits").value || "[]"); } catch { throw new Error("The edits list isn't valid JSON."); }
  if (!Array.isArray(edits)) throw new Error("The edits list must be a JSON list.");
  return edits;
}

async function runPack() {
  const task = $("#pk_task").value, button = $("#generate-button");
  const integer = (id) => Number($(id).value || 0);
  button.disabled = true;
  try {
    const file = $("#pk_file").files[0];
    if (["transcribe", "midi", "vocals"].includes(task) && file && packUpload?.label !== file.name) {
      const body = new FormData();
      body.append("file", file);
      packUpload = await Api.request("/api/pack/upload", { method: "POST", body });
      $("#pk_file_name").textContent = packUpload.name;
    }
    const seed = $("#pk_seed").value.trim();
    const payload = {
      task, allow_downloads: $("#pk_downloads").checked, style: $("#style").value, lyrics: $("#lyrics").value,
      score_abc: $("#pk_score").value, cot: $("#pk_cot").value, max_seconds: integer("#pk_seconds"), cfg_scale: Number($("#pk_cfg").value || 0),
      vae: $("#pk_vae").value, attention_backend: $("#pk_attention").value, ode_steps: integer("#pk_steps"), transpose: integer("#pk_transpose"),
      vocals_only: $("#pk_vocals_only").checked, low_vram: $("#pk_low_vram").checked, offload: $("#pk_offload").value,
      idea: $("#pk_idea").value, instructions: $("#pk_instructions").value, language: $("#pk_language").value.trim() || "auto", length: $("#pk_length").value,
      llm_model: $("#pk_llm").value.trim() || "auto", input_file: ["transcribe", "midi", "vocals"].includes(task) ? packUpload?.name || "" : "",
      score_mode: $("#pk_score_mode").value, recognize_lyrics: $("#pk_recognize").checked, second_hearing: $("#pk_second").checked, listen: $("#pk_listen").value,
      vocal_track: $("#pk_vocal_track").value.trim() || "auto", instrument_track: $("#pk_instrument_track").value.trim() || "auto",
      without_sections: $("#pk_without_sections").checked, source_job: task === "edit" ? $("#pk_source").value : "",
      edits: task === "edit" ? packEdits() : [],
      loras: [...lora.picked.yue2].map(([name, strength]) => ({ file: name, strength })),
    };
    if (seed) payload.seed = seed;
    await Api.request("/api/pack/run", { method: "POST", body: payload });
    toast("Added to the GPU queue.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

// The info dialog's first lines: whether this is a cover (of what) and which voice sings it.
function voiceDescription(request, cover) {
  const tune = [];
  if (request.voice_strength !== undefined && Number(request.voice_strength) !== 100) tune.push(`strength ${Math.round(request.voice_strength)}%`);
  if (Number(request.voice_semitones)) tune.push(`${request.voice_semitones > 0 ? "+" : ""}${request.voice_semitones} semitones`);
  const extra = tune.length ? ` · ${tune.join(", ")}` : "";
  const custom = (id) => {
    const known = voice.list.find((item) => item.id === id);
    return known ? `${known.name} (custom voice)` : voice.listed ? "A custom voice that has since been deleted" : "A custom voice";
  };
  if (!cover) return request.voice_id ? custom(request.voice_id) + extra : "YuE2's own voice";
  if (request.voice_source === "off") return "YuE2's own voice (no conversion)";
  if (request.voice_source === "voice audio input") {
    return (request.voice_id ? custom(request.voice_id) : `Uploaded recording: ${request.voice_file || "?"}`) + extra;
  }
  return "The original song's singer" + extra;
}

function songFactsHtml(job) {
  const request = job.request || {};
  const yue2 = job.model === "yue2" || job.model === "yue2_pack";
  const rows = [];
  if (job.task === "cover") {
    rows.push(["Type", request.remix_of ? "Cover (remix with new words)" : "Cover"]);
    rows.push(["Cover of", request.youtube_url || (request.input_file || "").replace(/^music_[a-f0-9]+_/, "") || "?"]);
    rows.push(["Voice", voiceDescription(request, true)]);
  } else if (job.task === "edit") {
    const source = state.jobs.find((item) => item.id === request.source_job);
    if (source?.task === "cover") {
      rows.push(["Type", "Edit of a cover"]);
      rows.push(["Voice", `${voiceDescription(source.request || {}, true)} (the cover's)`]);
    } else {
      rows.push(["Type", source ? "Edit of a song" : "Edit"]);
      rows.push(["Voice", "YuE2's own voice"]);
    }
  } else if (!job.task && yue2) {
    rows.push(["Type", request.remix_of ? "Song (remix with new words)" : "Song"]);
    rows.push(["Voice", voiceDescription(request, false)]);
  }
  if (!rows.length) return "";
  return `<dl class="song-facts">${rows.map(([key, value]) => `<dt>${key}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>`;
}

function packResultHtml(job) {
  const result = job.result || {};
  const section = (title, value) => (value ? `<section class="studio-prompt-section"><div class="studio-prompt-heading"><h3>${title}</h3></div><pre class="studio-prompt-text">${escapeHtml(value)}</pre></section>` : "");
  return `${section("Task", job.style)}${section("Style", result.style)}${section("Lyrics", result.lyrics || job.lyrics)}${section("Score", result.score)}
    ${job.task !== "cover" && (result.style || result.lyrics || result.score) ? `<button type="button" class="secondary-button" data-pack-use>Use in form</button>` : ""}`;
}

function usePackResult(job) {
  const result = job.result || {}, score = result.score || "";
  $("#model").value = "yue2_pack";
  $("#pk_task").value = "song";
  if (result.style) $("#style").value = result.style;
  if (result.lyrics) $("#lyrics").value = result.lyrics;
  if (score) $("#pk_score").value = score;
  // mode and cot are a pair: a cover sings with the mode its score was written in.
  if (job.request?.task === "transcribe" || job.request?.task === "midi") $("#pk_cot").value = job.request.score_mode;
  $("#model").dispatchEvent(new Event("change"));
  $("#lyrics").dispatchEvent(new Event("input"));
  toast("Loaded into the form. Review, then Generate.");
}

function loadPackRequest(request) {
  const set = { pk_task: request.task, style: request.style, lyrics: request.lyrics, pk_score: request.score_abc, pk_cot: request.cot, pk_seconds: request.max_seconds,
    pk_cfg: request.cfg_scale, pk_vae: request.vae, pk_attention: request.attention_backend, pk_steps: request.ode_steps, pk_transpose: request.transpose,
    pk_offload: request.offload, pk_idea: request.idea, pk_instructions: request.instructions, pk_language: request.language, pk_length: request.length, pk_llm: request.llm_model,
    pk_score_mode: request.score_mode, pk_listen: request.listen, pk_vocal_track: request.vocal_track, pk_instrument_track: request.instrument_track,
    pk_edits: JSON.stringify(request.edits || [], null, 1), pk_seed: request.seed ?? "" };
  $("#model").value = "yue2_pack";
  Object.entries(set).forEach(([id, value]) => { const node = document.getElementById(id); if (node && value !== undefined) node.value = value; });
  $("#pk_vocals_only").checked = !!request.vocals_only;
  $("#pk_low_vram").checked = !!request.low_vram;
  $("#pk_recognize").checked = !!request.recognize_lyrics;
  $("#pk_second").checked = !!request.second_hearing;
  $("#pk_without_sections").checked = request.without_sections !== false;
  packUpload = request.input_file ? { name: request.input_file, label: "" } : null;
  $("#pk_file_name").textContent = request.input_file || "";
  $("#model").dispatchEvent(new Event("change"));
  if (request.source_job) $("#pk_source").value = request.source_job;
  $("#pack-card").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("Loaded the exact settings and seed. Review, then Generate.");
}

/* ------------------------------------------------------------- covers -- */
// custom_nodes/RSP through backend/pack.py, task "cover". The dialog picks the song; the Reference Audio card holds its settings.
let cover = null;        // {input_file, youtube_url, youtube_start, youtube_length, label}
const voice = { kind: "source", id: "", list: [] };   // kind: source (the song's singer on covers) | off (YuE2's own) | custom
// Voice previews (see openPreview): the voice being previewed and the previews this browser made.
const PREVIEW_KEY = "music.voicePreviews";
const preview = { id: "", items: [], timer: 0 };
try { preview.items = (JSON.parse(localStorage.getItem(PREVIEW_KEY) || "[]") || []).slice(0, 6); } catch { preview.items = []; }
let songJob = null;      // {id, whisper, agentic, label}: the queued read of the song

function syncCover() {
  const on = $("#model").value === "yue2";
  $("#cover-bar").hidden = !on;
  $("#reference-card").hidden = !on || !cover;
  $("#cv_label").textContent = cover?.label || "";
  $("#cover-open").textContent = cover ? "Change song" : "Cover audio";
  const influence = Number($("#cv_audio").value);
  $("#cv_audio_out").textContent = influence;
  $("#cv_audio_note").textContent = influence >= 70 ? "Follows the song's melody and chords." : influence >= 35 ? "Follows the song's melody only." : "Keeps its lyrics and your style, no tune.";
  $("#cv_style_out").textContent = $("#cv_style").value;
  $("#cv_weird_out").textContent = $("#cv_weird").value;
  syncVoice();
}

function voiceName() {
  if (voice.kind === "custom") return voice.list.find((item) => item.id === voice.id)?.name || voice.savedName || "Custom voice";
  return voice.kind === "source" && cover ? "The song's singer" : "YuE2's own";
}

function syncVoice() {
  $("#voice-open").hidden = $("#model").value !== "yue2";
  $("#voice-open").textContent = `Voice: ${voiceName()}`;
  $("#vc_strength_out").textContent = $("#vc_strength").value;
  // A preview uses these settings too, so they show while one is open whatever voice is chosen.
  $("#voice-settings").hidden = !preview.id && (voice.kind === "off" || (voice.kind === "source" && !cover));
}

function voiceFields() {
  return { voice_id: voice.id, voice_strength: Number($("#vc_strength").value), voice_semitones: Number($("#vc_semitones").value || 0), conversion_steps: Number($("#vc_steps").value || 40) };
}

function chooseVoice(kind, id = "", request = null) {
  voice.kind = kind;
  voice.id = kind === "custom" ? id : "";
  if (request) {
    $("#vc_strength").value = request.voice_strength ?? 100;
    $("#vc_semitones").value = request.voice_semitones ?? 0;
    $("#vc_steps").value = request.conversion_steps ?? 40;
  }
  renderVoices();
  syncVoice();
  saveVoice();
}

// The chosen voice and its settings are kept in this browser: a phone reloads the page when you come back to it, and
// the cover being set up is restored then (cover draft), so its voice has to be too, not quietly the song's singer.
const VOICE_KEY = "music.voice";

function saveVoice() {
  try {
    localStorage.setItem(VOICE_KEY, JSON.stringify({ kind: voice.kind, id: voice.id, name: voice.kind === "custom" ? voiceName() : "", strength: $("#vc_strength").value,
      semitones: $("#vc_semitones").value, steps: $("#vc_steps").value }));
  } catch { /* storage blocked: this page only */ }
}

function restoreVoice() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(VOICE_KEY) || "null"); } catch { saved = null; }
  if (!saved || !["source", "off", "custom"].includes(saved.kind) || (saved.kind === "custom" && !saved.id)) return;
  voice.kind = saved.kind;
  voice.id = saved.kind === "custom" ? String(saved.id) : "";
  voice.savedName = saved.kind === "custom" ? String(saved.name || "") : "";
  if (saved.strength !== undefined) $("#vc_strength").value = saved.strength;
  if (saved.semitones !== undefined) $("#vc_semitones").value = saved.semitones;
  if (saved.steps !== undefined) $("#vc_steps").value = saved.steps;
  renderVoices();
  syncVoice();
}

function renderVoices() {
  const row = (kind, id, name, note, extra = "") => `<div class="voice-row${voice.kind === kind && voice.id === id ? " is-active" : ""}">
    <label><input type="radio" name="voice-pick" data-voice-kind="${kind}" data-voice-id="${id}"${voice.kind === kind && voice.id === id ? " checked" : ""}><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(note)}</small></span></label>${extra}</div>`;
  $("#voice-list").innerHTML = row("source", "", "The song's singer", "On covers. Other songs keep YuE2's own voice.")
    + row("off", "", "YuE2's own", "No voice conversion.")
    + voice.list.map((item) => row("custom", item.id, item.name, "Custom voice", `<audio controls preload="none" src="${API_BASE}/api/voices/${item.id}/audio"></audio><button type="button" class="secondary-button" data-voice-preview="${item.id}" aria-label="Preview ${escapeHtml(item.name)} on a song">Preview</button><button type="button" class="secondary-button" data-voice-rename="${item.id}" aria-label="Rename ${escapeHtml(item.name)}">Rename</button><button type="button" class="secondary-button" data-voice-delete="${item.id}" aria-label="Delete ${escapeHtml(item.name)}">Delete</button>`)).join("");
}

async function loadVoices() {
  let listed = true;
  try {
    voice.list = (await Api.request("/api/voices")).voices || [];
    voice.listed = true;
  } catch {
    listed = false;   // a dropped request (phone, network) must not throw away the voice you chose
  }
  if (listed && voice.kind === "custom" && !voice.list.some((item) => item.id === voice.id)) {
    voice.kind = "source";
    voice.id = "";
    saveVoice();
    toast("The voice you'd chosen isn't in your voices any more; using the song's singer.", "error");
  }
  renderVoices();
  syncVoice();
}

function coverSourceTab(kind) {
  document.querySelectorAll("[data-cover-source]").forEach((tab) => {
    const active = tab.dataset.coverSource === kind;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll("[data-cover-pane]").forEach((pane) => { pane.hidden = pane.dataset.coverPane !== kind; });
}

async function useCoverSong() {
  const kind = document.querySelector("[data-cover-source].is-active").dataset.coverSource;
  const button = $("#cd_use");
  button.disabled = true;
  try {
    let source;
    if (kind === "youtube") {
      const url = $("#cd_url").value.trim();
      if (!/^https?:\/\//.test(url)) throw new Error("Paste a YouTube link first.");
      source = { input_file: "", youtube_url: url, youtube_start: Number($("#cd_start").value || 0), youtube_length: Number($("#cd_length").value || 0), label: url };
    } else {
      const picked = $("#cd_file").files[0];
      if (!picked) throw new Error("Choose a song first.");
      let file = picked;
      try { file = new File([await picked.arrayBuffer()], picked.name, { type: picked.type }); } catch { /* read it as it uploads */ }
      button.textContent = "Uploading…";
      const stored = await Api.durable("/api/pack/upload", () => {
        const body = new FormData();
        body.append("file", file);
        return { method: "POST", body };
      }, "Uploading the song");
      source = { input_file: stored.name, youtube_url: "", youtube_start: 0, youtube_length: 0, label: stored.label };
    }
    cover = source;
    if ($("#cd_agentic").checked && $("#style").value.trim() && !directionWish()) $("#style").value = "";
    syncCover();
    saveCoverDraft();
    window.BunnyStudioDialog?.close?.($("#cover-dialog"));
    const writing = $("#cd_whisper").checked || $("#cd_agentic").checked;
    await readSong($("#cd_whisper").checked, $("#cd_agentic").checked);
    // Show what's being written; with nothing to write, the settings that just appeared.
    $(writing ? "#prompt-card" : "#reference-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Use this song";
  }
}

// The last Music direction Agentic wrote. If the box still holds exactly that text, it is Agentic's own
// output for the previous song, not a wish of the user's: sending it back as the "wish" for a new song made
// Agentic copy it (every cover came back as the same direction).
const AGENTIC_DIRECTION_KEY = "music.agenticDirection";
function rememberAgenticDirection(text) {
  try { localStorage.setItem(AGENTIC_DIRECTION_KEY, String(text || "").trim()); } catch { /* storage blocked */ }
}
function directionWish() {
  const text = $("#style").value.trim();
  let written = "";
  try { written = localStorage.getItem(AGENTIC_DIRECTION_KEY) || ""; } catch { /* storage blocked */ }
  const earlier = (state.jobs || []).some((job) => String(job.result?.direction || "").trim() === text);
  return text && (text === written.trim() || earlier) ? "" : text;
}

// One queued job serves both options: the score Agentic reads is the one Whisper's lyrics are laid under.
async function readSong(whisper, agentic) {
  if (!whisper && !agentic) return;
  const { label, ...where } = cover;
  const job = await Api.durable("/api/pack/run", () => ({ method: "POST", body: {
    task: "transcribe", ...where, score_mode: "full", whisper, describe: agentic, allow_downloads: true,
    direction: agentic, direction_wish: directionWish(), direction_label: label || "" } }), "Reading the song");
  songJob = { id: job.id, whisper, agentic, label };
  saveCoverDraft();
  $("#cv_write").disabled = true;
  showSongProgress(songJob, job);
}

// The box being written to: animated border, a status line above it, locked until the text is in.
function setWriting(id, text) {
  const box = document.getElementById(id), status = document.getElementById(`${id}-writing`);
  box.classList.toggle("is-writing", !!text);
  box.readOnly = !!text;
  box.setAttribute("aria-busy", String(!!text));
  status.hidden = !text;
  if (status.textContent !== (text || "")) status.textContent = text || "";
}

function showSongProgress(read, job) {
  const percent = job?.progress > 0 && job.progress < 100 ? ` ${job.progress}%` : "";
  const waiting = job?.state === "waiting_for_gpu" ? "waiting for the GPU" : job?.state === "preparing" ? "getting the GPU ready" : "";
  if (read.whisper) setWriting("lyrics", waiting ? `Whisper: ${waiting}` : `Whisper is transcribing the lyrics${percent}`);
  if (read.agentic) setWriting("style", waiting ? `Agentic: ${waiting}` : `Agentic is reading the song${percent}`);
}

// Types the finished text in, so it reads as written rather than appearing. Instant when motion is unwanted.
async function typeInto(id, text) {
  const box = document.getElementById(id);
  const put = (value) => { box.value = value; box.dispatchEvent(new Event("input")); box.scrollTop = box.scrollHeight; };
  if (document.hidden || matchMedia("(prefers-reduced-motion: reduce)").matches) return put(text);
  const steps = Math.min(60, Math.max(10, Math.ceil(text.length / 8)));
  for (let step = 1; step < steps; step += 1) {
    put(text.slice(0, Math.ceil(text.length * step / steps)));
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  put(text);
  box.scrollTop = 0;
}

function watchWhisper() {
  if (!songJob) return;
  const job = state.jobs.find((item) => item.id === songJob.id);
  const read = songJob;
  if (job) read.seen = true;
  if ((!job && !read.seen) || ACTIVE_JOB_STATES.has(job?.state)) {
    if (job) showSongProgress(read, job);
    return;
  }
  if (job?.state !== "complete") {
    songJob = null;
    saveCoverDraft();
    $("#cv_write").disabled = false;
    if (read.whisper) setWriting("lyrics", null);
    if (read.agentic) setWriting("style", null);
    if (job && job.state !== "cancelled") toast(job.error || "Couldn't read the song.", "error");
    return;
  }
  if (read.whisper && !read.lyricsDone) {
    read.lyricsDone = true;
    saveCoverDraft();
    writeLyrics(job.result?.lyrics);
  }
  if (read.agentic && !read.directionDone) {
    // The server writes the direction once the read is done (pack "direction"); older reads ask for it here.
    const written = job.result?.direction_state;
    if (!job.request?.direction) {
      read.directionDone = true;
      writeDirection(read);
    } else if (!written || written === "writing") {
      setWriting("style", "Agentic is writing the Music direction");
      $("#cv_write").disabled = true;
      return;   // songJob stays until it's in, across reloads too
    } else {
      read.directionDone = true;
      if (written === "done" && job.result.direction) {
        setWriting("style", "Agentic is writing the Music direction");
        rememberAgenticDirection(job.result.direction);
        typeInto("style", job.result.direction).then(() => {
          setWriting("style", null);
          toast("Music direction is in. Review it, then Generate.");
        });
      } else {
        setWriting("style", null);
        toast(job.result?.direction_error || "Agentic couldn't write the Music direction.", "error");
      }
    }
  }
  songJob = null;
  saveCoverDraft();
  $("#cv_write").disabled = false;
}

async function writeLyrics(lyrics) {
  if (lyrics) {
    setWriting("lyrics", "Whisper is writing the lyrics");
    await typeInto("lyrics", lyrics);
    toast("Lyrics are in. Recognised words aren't exact, so read them through.");
  } else {
    toast("Whisper didn't return any lyrics.", "error");
  }
  setWriting("lyrics", null);
}

async function writeDirection(read) {
  const button = $("#cv_write");
  button.disabled = true;
  setWriting("style", "Agentic is writing the Music direction");
  try {
    const { style } = await Api.request("/api/cover/direction", { method: "POST", body: { job_id: read.id, label: read.label, wish: directionWish() } });
    rememberAgenticDirection(style);
    await typeInto("style", style);
    toast("Music direction is in. Review it, then Generate.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setWriting("style", null);
    button.disabled = false;
  }
}

async function runCover() {
  if (songJob || document.querySelector("#style.is-writing, #lyrics.is-writing")) return toast("Still writing. Generate when it's done.", "error");
  const style = $("#style").value.trim();
  if (!style) return toast("Enter a style/genre description.", "error");
  const button = $("#generate-button");
  button.disabled = true;
  try {
    const payload = { ...coverFields(), style, lyrics: $("#lyrics").value };
    await Api.request("/api/pack/run", { method: "POST", body: payload });
    toast("Cover added to the GPU queue.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function coverFields() {
  const { label, ...where } = cover;
  const fields = {
    task: "cover", ...where, whisper: $("#cd_whisper").checked, allow_downloads: $("#cv_downloads").checked,
    audio_influence: Number($("#cv_audio").value), style_influence: Number($("#cv_style").value), weirdness: Number($("#cv_weird").value),
    vocal_gender: $("#cv_gender").value, voice_source: { source: "source singer", off: "off", custom: "voice audio input" }[voice.kind],
    ...voiceFields(), transpose: Number($("#cv_transpose").value || 0),
    loras: [...lora.picked.yue2].map(([file, strength]) => ({ file, strength })),
  };
  if ($("#seed").value.trim()) fields.seed = $("#seed").value.trim();
  return fields;
}

// What Agentic mode renders with besides the form: the same cover or voice Generate would use.
window.MusicStudio = {
  agenticSettings() {
    if (currentModel() !== "yue2") return {};
    if (cover) return { cover: coverFields() };
    return voice.id ? voiceFields() : {};
  },
};

function loadCoverRequest(request, seed) {
  cover = { input_file: request.input_file || "", youtube_url: request.youtube_url || "", youtube_start: request.youtube_start || 0,
    youtube_length: request.youtube_length || 0, label: request.youtube_url || request.input_file };
  chooseVoice({ "source singer": "source", off: "off", "voice audio input": "custom" }[request.voice_source] || "source", request.voice_id || "", request);
  const set = { model: "yue2", style: request.style, lyrics: request.lyrics, seed, cv_audio: request.audio_influence, cv_style: request.style_influence,
    cv_weird: request.weirdness, cv_gender: request.vocal_gender, cv_transpose: request.transpose };
  Object.entries(set).forEach(([id, value]) => { document.getElementById(id).value = value ?? ""; });
  $("#cv_downloads").checked = !!request.allow_downloads;
  $("#cd_whisper").checked = !!request.whisper;
  lora.picked.yue2 = new Map((request.loras || []).map((item) => [item.file, item.strength]));
  $("#model").dispatchEvent(new Event("change"));
  $("#lyrics").dispatchEvent(new Event("input"));
  $("#reference-card").scrollIntoView({ behavior: "smooth", block: "start" });
  saveCoverDraft();
  toast("Loaded the exact cover settings and seed. Review, then Generate.");
}

$("#cover-open").addEventListener("click", () => window.BunnyStudioDialog?.open?.($("#cover-dialog")));
document.querySelectorAll("[data-cover-source]").forEach((tab) => tab.addEventListener("click", () => coverSourceTab(tab.dataset.coverSource)));
$("#cd_use").addEventListener("click", useCoverSong);
$("#cv_write").addEventListener("click", () => readSong(false, true).catch((error) => toast(error.message, "error")));
$("#cv_remove").addEventListener("click", () => {
  // The read has no card to cancel it from any more, and its song is going away.
  if (songJob) Api.request(`/api/jobs/${songJob.id}/cancel`, { method: "POST" }).catch((error) => toast(error.message, "error"));
  songJob = null;
  cover = null;
  syncCover();
  saveCoverDraft();
});
["#cv_audio", "#cv_style", "#cv_weird", "#vc_strength"].forEach((id) => $(id).addEventListener("input", syncCover));
$("#voice-open").addEventListener("click", () => { loadVoices(); window.BunnyStudioDialog?.open?.($("#voice-dialog")); });
$("#voice-list").addEventListener("change", (event) => {
  const pick = event.target.closest("[data-voice-kind]");
  if (pick) chooseVoice(pick.dataset.voiceKind, pick.dataset.voiceId);
});
$("#voice-list").addEventListener("click", async (event) => {
  const previewButton = event.target.closest("[data-voice-preview]");
  if (previewButton) return openPreview(previewButton.dataset.voicePreview);
  const rename = event.target.closest("[data-voice-rename]");
  if (rename) {
    const item = voice.list.find((row) => row.id === rename.dataset.voiceRename);
    const name = prompt("New name for this voice:", item?.name || "");
    if (name === null || !name.trim() || name.trim() === item?.name) return;
    rename.disabled = true;
    try {
      const renamed = await Api.request(`/api/voices/${rename.dataset.voiceRename}`, { method: "PATCH", body: { name: name.trim() } });
      await loadVoices();
      if (voice.id === renamed.id) saveVoice();
      toast(`Renamed to ${renamed.name}.`);
    } catch (error) {
      toast(error.message, "error");
      rename.disabled = false;
    }
    return;
  }
  const button = event.target.closest("[data-voice-delete]");
  if (!button) return;
  const item = voice.list.find((row) => row.id === button.dataset.voiceDelete);
  if (!confirm(`Delete the voice "${item?.name || ""}"? Songs already made keep it.`)) return;
  try {
    await Api.request(`/api/voices/${button.dataset.voiceDelete}`, { method: "DELETE" });
    await loadVoices();
  } catch (error) {
    toast(error.message, "error");
  }
});
// A voice from a YouTube link: the server downloads it, you trim it here, then it's added. The page keeps the download's
// id so leaving and coming back picks it up again (downloading or waiting to be trimmed).
const VOICE_IMPORT_KEY = "music.voiceImport";
const trim = { id: null, duration: 0, stopAt: null };

function voiceImportPending(value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(VOICE_IMPORT_KEY) || "null");
    value ? localStorage.setItem(VOICE_IMPORT_KEY, JSON.stringify(value)) : localStorage.removeItem(VOICE_IMPORT_KEY);
  } catch { return null; }
}

function clockSeconds(text, label) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const parts = raw.split(":");
  if (parts.length > 3 || parts.some((part) => !/^\d+(\.\d+)?$/.test(part))) throw new Error(`${label}: write it like 1:05 or 65.`);
  return parts.reduce((total, part) => total * 60 + Number(part), 0);
}

function clockText(seconds) {
  const whole = Math.max(0, seconds);
  const minutes = Math.floor(whole / 60), rest = whole - minutes * 60;
  return `${minutes}:${rest < 10 ? "0" : ""}${rest.toFixed(1).replace(/\.0$/, "")}`;
}

function voiceImportStatus(text) {
  $("#vc_status").hidden = !text;
  $("#vc_status").textContent = text || "";
}

function trimSpan() {
  const start = clockSeconds($("#vc_from").value, "From") ?? 0;
  const end = clockSeconds($("#vc_to").value, "To") ?? trim.duration;
  return { start: Math.min(start, trim.duration), end: Math.min(end, trim.duration) };
}

function showTrimLength() {
  try {
    const { start, end } = trimSpan();
    const length = end - start;
    $("#vc_trim_length").textContent = length > 0
      ? `Keeping ${clockText(length)} of ${clockText(trim.duration)}.${length < 20 ? " 20-25 s or more of singing works best." : ""}`
      : "To has to be after From.";
  } catch (error) {
    $("#vc_trim_length").textContent = error.message;
  }
}

function openTrim(id, job) {
  trim.id = id;
  trim.duration = Number(job.duration) || 0;
  $("#vc_trim_title").textContent = job.title || "YouTube audio";
  $("#vc_trim_audio").src = `${API_BASE}/api/voices/youtube/${id}/audio`;
  $("#vc_from").value = "0:00";
  $("#vc_to").value = clockText(trim.duration);
  if (!$("#vc_trim_name").value) $("#vc_trim_name").value = job.title || "";
  $("#vc_trim").hidden = false;
  showTrimLength();
}

function closeTrim() {
  const audio = $("#vc_trim_audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  trim.id = null;
  $("#vc_trim").hidden = true;
  $("#vc_trim_name").value = "";
  voiceImportPending(null);
}

async function followVoiceImport(id) {
  const button = $("#vc_fetch");
  button.disabled = true;
  try {
    for (;;) {
      await Api.whenVisible();
      let job;
      try {
        job = await Api.request(`/api/voices/youtube/${id}`);
      } catch (error) {
        if (error.status === 404) throw error;
        await new Promise((resolve) => setTimeout(resolve, 3000));
        continue;
      }
      if (job.state === "ready") {
        voiceImportStatus("");
        openTrim(id, job);
        return;
      }
      if (job.state === "done") {
        voiceImportStatus("");
        voiceImportPending(null);
        return;
      }
      if (job.state === "failed") throw new Error(job.error || "The YouTube download failed.");
      voiceImportStatus(`${job.stage || "Working"}…`);
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  } catch (error) {
    voiceImportStatus("");
    voiceImportPending(null);
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

$("#vc_fetch").addEventListener("click", async () => {
  const url = $("#vc_url").value.trim(), button = $("#vc_fetch");
  if (!url) return toast("Paste a YouTube link first.", "error");
  if (trim.id) await Api.request(`/api/voices/youtube/${trim.id}`, { method: "DELETE" }).catch(() => {});
  closeTrim();
  button.disabled = true;
  voiceImportStatus("Starting the YouTube download…");
  try {
    const job = await Api.durable("/api/voices/youtube", () => ({ method: "POST", body: { url } }), "YouTube download");
    voiceImportPending({ id: job.id, at: Date.now() });
    $("#vc_url").value = "";
    await followVoiceImport(job.id);
  } catch (error) {
    voiceImportStatus("");
    toast(error.message, "error");
    button.disabled = false;
  }
});
$("#vc_url").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); $("#vc_fetch").click(); } });
["#vc_from", "#vc_to"].forEach((id) => $(id).addEventListener("input", showTrimLength));
document.querySelectorAll("[data-trim-mark]").forEach((button) => button.addEventListener("click", () => {
  $(button.dataset.trimMark === "from" ? "#vc_from" : "#vc_to").value = clockText($("#vc_trim_audio").currentTime);
  showTrimLength();
}));
$("#vc_trim_play").addEventListener("click", () => {
  try {
    const { start, end } = trimSpan();
    const audio = $("#vc_trim_audio");
    trim.stopAt = end;
    audio.currentTime = start;
    audio.play();
  } catch (error) {
    toast(error.message, "error");
  }
});
$("#vc_trim_audio").addEventListener("timeupdate", (event) => {
  if (trim.stopAt !== null && event.target.currentTime >= trim.stopAt) { event.target.pause(); trim.stopAt = null; }
});
$("#vc_trim_audio").addEventListener("pause", () => { trim.stopAt = null; });
$("#vc_trim_discard").addEventListener("click", async () => {
  if (trim.id) await Api.request(`/api/voices/youtube/${trim.id}`, { method: "DELETE" }).catch(() => {});
  closeTrim();
});
$("#vc_trim_add").addEventListener("click", async () => {
  const button = $("#vc_trim_add");
  let span;
  try {
    span = trimSpan();
  } catch (error) {
    return toast(error.message, "error");
  }
  if (span.end - span.start < 3) return toast("Keep at least 3 seconds.", "error");
  button.disabled = true;
  try {
    const id = trim.id;
    const item = await Api.durable(`/api/voices/youtube/${id}/add`, () => ({ method: "POST",
      body: { name: $("#vc_trim_name").value.trim(), start: span.start, end: span.end } }), "Adding the voice");
    closeTrim();
    await loadVoices();
    chooseVoice("custom", item.id);
    toast(`Added ${item.name}, and it's selected.`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

function resumeVoiceImport() {
  const pending = voiceImportPending();
  if (!pending?.id) return;
  if (Date.now() - (pending.at || 0) > 3 * 3600000) return voiceImportPending(null);
  followVoiceImport(pending.id);
}

$("#vc_add").addEventListener("click", async () => {
  const file = $("#vc_file").files[0], name = $("#vc_name").value.trim(), button = $("#vc_add");
  if (!name || !file) return toast("Give the voice a name and a recording.", "error");
  button.disabled = true;
  try {
    const body = new FormData();
    body.append("name", name);
    body.append("file", file);
    const item = await Api.request("/api/voices", { method: "POST", body });
    $("#vc_name").value = "";
    $("#vc_file").value = "";
    await loadVoices();
    chooseVoice("custom", item.id);
    toast(`Added ${item.name}, and it's selected.`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});
["#vc_strength", "#vc_semitones", "#vc_steps"].forEach((id) => $(id).addEventListener("change", saveVoice));
loadVoices();

// ---- Voice preview: a custom voice sung over a short stretch of one of your songs (or an uploaded clip), to hear it
// before using it. The server keeps previews out of the library and keeps the newest few; this browser remembers
// which ones it made (localStorage), so a running one is followed again after a reload.

function savePreviews() {
  try { localStorage.setItem(PREVIEW_KEY, JSON.stringify(preview.items.map(({ job, ...item }) => item))); } catch { /* private mode */ }
}

function previewSongs() {
  return listedJobs().filter((job) => job.state === "complete" && job.output_url);
}

function openPreview(voiceId) {
  preview.id = voiceId;
  const name = voice.list.find((item) => item.id === voiceId)?.name || "this voice";
  $("#vp_title").textContent = `Preview ${name}`;
  const songs = previewSongs();
  const chosen = $("#vp_song").value;
  $("#vp_song").innerHTML = songs.map((job) => `<option value="${job.id}">${escapeHtml((job.style || "Untitled").slice(0, 80))}</option>`).join("")
    + `<option value="upload">Upload a clip…</option>`;
  if ([...$("#vp_song").options].some((option) => option.value === chosen)) $("#vp_song").value = chosen;
  syncPreviewSource();
  $("#voice-preview").hidden = false;
  syncVoice();
  renderPreviews();
  $("#voice-preview").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closePreview() {
  preview.id = "";
  $("#voice-preview").hidden = true;
  syncVoice();
}

function syncPreviewSource() {
  $("#vp_file_row").hidden = $("#vp_song").value !== "upload";
}

function previewStatus(item) {
  const job = item.job;
  if (!job) return "Checking…";
  if (job.state === "rendering") return job.progress > 0 && job.progress < 100 ? `Converting… ${job.progress}%` : "Converting…";
  if (job.state === "failed") return job.error || "The conversion failed.";
  return STATE_LABELS[job.state] || job.state;
}

function renderPreviews() {
  const items = preview.items.filter((item) => !preview.id || item.voice_id === preview.id);
  $("#vp_results").innerHTML = items.map((item) => {
    const job = item.job, active = !job || ACTIVE_STATES.has(job.state), done = job?.state === "complete" && job.output_url;
    const voiceLabel = voice.list.find((row) => row.id === item.voice_id)?.name || item.voice_name || "Custom voice";
    const tune = [`${Math.round(item.strength)}%`, item.semitones ? `${item.semitones > 0 ? "+" : ""}${item.semitones} st` : "", `${item.steps} steps`].filter(Boolean).join(" · ");
    return `<div class="voice-preview-item${job?.state === "failed" ? " is-failed" : ""}">
      <header><strong>${escapeHtml(voiceLabel)} · ${escapeHtml(item.song)}</strong><small>${Math.round(item.start)}–${Math.round(item.start + item.seconds)} s · ${escapeHtml(tune)}</small></header>
      ${done ? `<audio controls preload="metadata" src="${job.output_url}"></audio>` : `<small>${escapeHtml(previewStatus(item))}</small>`}
      ${active && job ? `<div class="cover-actions"><button type="button" class="secondary-button" data-preview-cancel="${item.id}">Cancel</button></div>` : ""}
    </div>`;
  }).join("");
}

async function pollPreviews() {
  clearTimeout(preview.timer);
  let running = false, changed = false;
  for (const item of preview.items) {
    if (item.job && !ACTIVE_STATES.has(item.job.state)) continue;
    try {
      const job = await Api.request(`/api/jobs/${item.id}`);
      changed = changed || JSON.stringify(job) !== JSON.stringify(item.job);
      item.job = job;
    } catch (error) {
      if (error.status === 404) { item.gone = true; changed = true; continue; }   // pruned on the server
    }
    running = running || !item.job || ACTIVE_STATES.has(item.job.state);
  }
  if (preview.items.some((item) => item.gone)) {
    preview.items = preview.items.filter((item) => !item.gone);
    savePreviews();
  }
  if (changed) renderPreviews();
  if (running) preview.timer = setTimeout(pollPreviews, document.visibilityState === "visible" ? 2000 : 8000);
}

async function startPreview() {
  const button = $("#vp_go");
  const voiceId = preview.id;
  if (!voiceId) return;
  const start = Math.max(0, Number($("#vp_start").value || 0));
  const seconds = Math.min(60, Math.max(5, Number($("#vp_length").value || 20)));
  button.disabled = true;
  try {
    let source, song;
    if ($("#vp_song").value === "upload") {
      const picked = $("#vp_file").files[0];
      if (!picked) throw new Error("Choose a clip to upload first.");
      let file = picked;
      try { file = new File([await picked.arrayBuffer()], picked.name, { type: picked.type }); } catch { /* read it as it uploads */ }
      button.textContent = "Uploading…";
      const stored = await Api.durable("/api/pack/upload", () => {
        const body = new FormData();
        body.append("file", file);
        return { method: "POST", body };
      }, "Uploading the clip");
      source = { input_file: stored.name };
      song = stored.label;
    } else {
      const job = previewSongs().find((item) => item.id === $("#vp_song").value);
      if (!job) throw new Error("Choose a song to preview the voice on.");
      source = { source_job: job.id };
      song = (job.style || "Untitled").slice(0, 80);
    }
    button.textContent = "Starting…";
    const tune = voiceFields();
    const job = await Api.durable("/api/pack/run", () => ({ method: "POST", body: {
      task: "voice_preview", ...source, ...tune, voice_id: voiceId, preview_start: start, preview_seconds: seconds } }), "Voice preview");
    preview.items.unshift({ id: job.id, voice_id: voiceId, voice_name: voice.list.find((item) => item.id === voiceId)?.name || "",
      song, start, seconds, strength: tune.voice_strength, semitones: tune.voice_semitones, steps: tune.conversion_steps, job });
    preview.items = preview.items.slice(0, 6);
    savePreviews();
    renderPreviews();
    pollPreviews();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Convert";
  }
}

$("#vp_song").addEventListener("change", syncPreviewSource);
$("#vp_go").addEventListener("click", startPreview);
$("#vp_close").addEventListener("click", closePreview);
$("#vp_results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-preview-cancel]");
  if (!button) return;
  button.disabled = true;
  try {
    await Api.request(`/api/jobs/${button.dataset.previewCancel}/cancel`, { method: "POST" });
    pollPreviews();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
});
if (preview.items.length) pollPreviews();

$("#pk_task").addEventListener("change", syncPack);
$("#pk_op").addEventListener("change", syncPack);
$("#pk_file").addEventListener("change", () => { packUpload = null; $("#pk_file_name").textContent = ""; });
$("#pk_add_edit").addEventListener("click", () => {
  try {
    const edits = packEdits();
    edits.push(packOp());
    $("#pk_edits").value = JSON.stringify(edits, null, 1);
  } catch (error) {
    toast(error.message, "error");
  }
});

bindForm();
refreshJobsOnce();
bindEvents();
// Back from another app: phones may have dropped the live updates meanwhile, so catch up at once.
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshJobsOnce(); });
setInterval(updateElapsedTimes, 1000);


/* ------------------------------------------------------------ enhance -- */
// Like Video's Enhance: Hermes rewrites Music direction or Lyrics in place for the loaded model (nothing is
// rendered); Undo puts back what was there. POST /api/enhance -> the framework's write-prompt (a GPU-queue turn).
const enhanceBefore = {};

function enhanceBody(field, extra = {}) {
  const model = $("#model").value;
  const duration = model === "yue2_pack" ? Number($("#pk_seconds")?.value || 0) : Number($("#duration").value || 0);
  return { field, style: $("#style").value, lyrics: $("#lyrics").value, model, duration, ...extra };
}

// Enhance runs as a job on the server: leaving the browser can't lose it. A running one is kept in this browser
// (Music direction and Lyrics; the Remix box lives in a dialog a reload closes) and picked up after a reload.
const ENHANCE_PENDING_KEY = "music.enhancePending";

function enhancePending(boxId, value) {
  try {
    const all = JSON.parse(localStorage.getItem(ENHANCE_PENDING_KEY) || "{}");
    if (value === undefined) return all[boxId];
    if (value) all[boxId] = value; else delete all[boxId];
    localStorage.setItem(ENHANCE_PENDING_KEY, JSON.stringify(all));
  } catch { /* storage blocked: this page only */ }
  return undefined;
}

async function followEnhance(id, view) {
  let failures = 0;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await Api.whenVisible();
    try {
      const job = await Api.request(`/api/enhance/${id}`, { cache: "no-store" });
      failures = 0;
      if (job.live) view.update(job.live);
      if (job.state === "done") return job.text;
      if (job.state === "failed") throw Object.assign(new Error(job.error || "Hermes couldn't enhance it."), { final: true });
    } catch (error) {
      if (error.final || error.status === 404) throw error;
      // A dropped poll (the browser was left, the network switched) is retried; a server that stays down isn't.
      if (document.visibilityState === "visible" && ++failures >= 20) throw error;
    }
  }
}

async function runEnhance(boxId, body, button, undo, resume = null) {
  const box = document.getElementById(boxId);
  const before = resume ? resume.before : box.value;
  if (resume && !box.value) box.value = before;   // a reload emptied it: show what's being enhanced
  button.disabled = true;
  setWriting(boxId, body.field === "style" ? "Hermes is enhancing the Music direction…" : "Hermes is enhancing the lyrics…");
  const keep = boxId === "style" || boxId === "lyrics";
  const view = enhanceOutput(document.querySelector(`[data-enhance-panel="${boxId}"]`));
  try {
    let id = resume?.id;
    if (!id) {
      ({ id } = await Api.durable("/api/enhance/start", () => ({ method: "POST", body }), "Enhance"));
      if (keep) enhancePending(boxId, { id, before, field: body.field, at: Date.now() });
    }
    const text = await followEnhance(id, view);
    view.finish(true);
    if (keep) enhancePending(boxId, null);
    enhanceBefore[boxId] = before;
    setWriting(boxId, "");
    await typeInto(boxId, text);
    undo.hidden = false;
    return true;
  } catch (error) {
    view.finish(false, error.message);
    if (keep) enhancePending(boxId, null);
    setWriting(boxId, "");
    toast(error.message, "error");
    return false;
  } finally {
    button.disabled = false;
  }
}


/* ---------------------------------------------------- enhance output -- */
// While Enhance runs, the model's own output shows under its box: its thinking (folded once it starts writing)
// and the text as it's written. It stays after it finishes, until ✕ or the next Enhance.
const ENHANCE_PHASES = { waiting: "Waiting for the GPU", loading: "Loading the model", thinking: "Thinking", writing: "Writing", done: "Finished" };

function enhanceOutput(anchor) {
  let box = anchor.nextElementSibling?.classList.contains("enhance-output") ? anchor.nextElementSibling : null;
  if (!box) {
    box = document.createElement("div");
    box.className = "enhance-output";
    box.setAttribute("aria-live", "polite");
    box.innerHTML = `<div class="enhance-output-head"><span class="enhance-output-status"></span><span class="enhance-output-model"></span>
      <button type="button" class="enhance-output-close" aria-label="Hide the model output">&#10005;</button></div>
      <details class="enhance-output-thinking" hidden><summary>Thinking</summary><pre></pre></details>
      <pre class="enhance-output-text" hidden></pre>`;
    box.querySelector(".enhance-output-close").addEventListener("click", () => { box.hidden = true; });
    // A toggle the page didn't ask for is the user's: from then on it stays as they left it.
    box.querySelector("details").addEventListener("toggle", (event) => { if (event.target.open !== box.wantOpen) box.dataset.userToggled = "1"; });
    anchor.after(box);
  }
  const status = box.querySelector(".enhance-output-status"), modelTag = box.querySelector(".enhance-output-model");
  const thinking = box.querySelector("details"), thinkingText = thinking.querySelector("pre"), output = box.querySelector(".enhance-output-text");
  const fill = (pre, text) => {
    if (pre.textContent === text) return;
    const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.textContent = text;
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  };
  box.hidden = false;
  box.classList.remove("is-done", "is-failed");
  delete box.dataset.userToggled;
  thinking.hidden = true; box.wantOpen = thinking.open = true; output.hidden = true;
  thinkingText.textContent = ""; output.textContent = ""; modelTag.textContent = "";
  status.textContent = "Starting…";
  return {
    update({ phase = "", model = "", thinking: thought = "", text = "", note = "" }) {
      status.textContent = note || ENHANCE_PHASES[phase] || phase || "Working";
      if (model) modelTag.textContent = model;
      if (thought) {
        thinking.hidden = false;
        thinking.querySelector("summary").textContent = `Thinking · ${thought.length.toLocaleString()} chars`;
        fill(thinkingText, thought);
      }
      if (text) {
        output.hidden = false;
        fill(output, text);
        if (!box.dataset.userToggled && thinking.open) box.wantOpen = thinking.open = false;   // the answer is what matters now
      }
    },
    finish(ok, note) {
      box.classList.add(ok ? "is-done" : "is-failed");
      status.textContent = note || (ok ? "Finished" : "Failed");
    },
  };
}

// After a reload: an Enhance that was running carries on into its box.
function resumeEnhances() {
  for (const field of ["style", "lyrics"]) {
    const pending = enhancePending(field);
    if (!pending?.id) continue;
    if (Date.now() - (pending.at || 0) > 3 * 3600000) { enhancePending(field, null); continue; }
    const go = document.querySelector(`[data-enhance-go="${field}"]`);
    const toggle = document.querySelector(`[data-enhance="${field}"]`);
    toggle.disabled = true;
    toast(`Picking up your Enhance of the ${field === "style" ? "Music direction" : "lyrics"}.`);
    runEnhance(field, { field }, go, document.querySelector(`[data-enhance-undo="${field}"]`), pending)
      .finally(() => { toggle.disabled = false; });
  }
}

function undoEnhance(boxId, undo) {
  const box = document.getElementById(boxId);
  if (boxId in enhanceBefore) {
    box.value = enhanceBefore[boxId];
    box.dispatchEvent(new Event("input"));
    delete enhanceBefore[boxId];
  }
  undo.hidden = true;
}

// Enhance opens a line for what to change (optional); Enter or the line's own Enhance runs it, Esc or ✕ closes it.
function enhancePanel(field, open) {
  const panel = document.querySelector(`[data-enhance-panel="${field}"]`);
  const toggle = document.querySelector(`[data-enhance="${field}"]`);
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (open) panel.querySelector("input").focus();
}

const enhanceRequests = {
  style: (wish) => enhanceBody("style", { wish }),
  lyrics: (wish) => enhanceBody("lyrics", { wish }),
  rx_lyrics: (wish) => remixJob && { field: "lyrics", style: remixJob.style, lyrics: $("#rx_lyrics").value,
    original_lyrics: $("#rx_lyrics").dataset.original || "", wish, model: remixJob.model || "yue2", duration: Number(remixJob.duration) || 0 },
};

async function goEnhance(field) {
  const input = document.querySelector(`[data-enhance-wish="${field}"]`);
  const body = enhanceRequests[field](input.value.trim());
  if (!body) return;
  const go = document.querySelector(`[data-enhance-go="${field}"]`);
  const toggle = document.querySelector(`[data-enhance="${field}"]`);
  toggle.disabled = true;
  const done = await runEnhance(field, body, go, document.querySelector(`[data-enhance-undo="${field}"]`));
  toggle.disabled = false;
  if (done) { input.value = ""; enhancePanel(field, false); }   // a failure keeps what was typed, to try again
}

document.querySelectorAll("[data-enhance]").forEach((button) => button.addEventListener("click", () => {
  const field = button.dataset.enhance;
  enhancePanel(field, document.querySelector(`[data-enhance-panel="${field}"]`).hidden);
}));
document.querySelectorAll("[data-enhance-go]").forEach((button) => button.addEventListener("click", () => goEnhance(button.dataset.enhanceGo)));
document.querySelectorAll("[data-enhance-close]").forEach((button) => button.addEventListener("click", () => enhancePanel(button.dataset.enhanceClose, false)));
document.querySelectorAll("[data-enhance-wish]").forEach((input) => input.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); goEnhance(input.dataset.enhanceWish); }
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); enhancePanel(input.dataset.enhanceWish, false); }
}));
document.querySelectorAll("[data-enhance-undo]").forEach((button) => button.addEventListener("click", () => undoEnhance(button.dataset.enhanceUndo, button)));
// Typing over an enhanced text makes it yours: Undo would throw that away.
["style", "lyrics"].forEach((id) => document.getElementById(id).addEventListener("input", (event) => {
  if (!event.isTrusted) return;
  const undo = document.querySelector(`[data-enhance-undo="${id}"]`);
  if (undo) undo.hidden = true;
  delete enhanceBefore[id];
}));

/* -------------------------------------------------------------- remix -- */
// Remix: a finished song sung again with new words, keeping its seed and its score (the sheet music), so the
// melody stays. YuE2: the saved score is sung as is; songs from before scores were saved have the composer write it
// again from the original words with the same seed (score_lyrics). YuE2 Studio songs pass their score_abc;
// covers re-read the same recording with the same seed.
let remixJob = null;

function remixKind(job) {
  if (job.state !== "complete" || !job.output_url) return "";
  if (!job.task && (job.model || "yue2") === "yue2") return "yue2";
  return job.task === "song" || job.task === "cover" ? job.task : "";
}

const sungLines = (text) => String(text || "").split("\n").map((line) => line.trim()).filter((line) => line && !/^\[[^\]]*\]$/.test(line));

function syncRemixCount() {
  const now = sungLines($("#rx_lyrics").value).length, was = sungLines($("#rx_lyrics").dataset.original).length;
  const count = $("#rx_count");
  count.classList.toggle("is-off", now !== was);
  count.textContent = now === was ? `${now} sung lines, as in the song. Keep lines about as long as the ones they replace.`
    : `${now} sung lines; the song has ${was}. Keep the same number so the words fit the melody.`;
}

function openRemix(id) {
  const job = state.jobs.find((item) => item.id === id);
  const kind = job && remixKind(job);
  if (!kind) return toast("This song can't be remixed.", "error");
  remixJob = job;
  const words = job.sung_lyrics || job.lyrics || job.request?.lyrics || "";
  $("#rx_title").textContent = job.style;
  $("#rx_lyrics").value = words;
  $("#rx_lyrics").dataset.original = words;
  document.querySelector('[data-enhance-wish="rx_lyrics"]').value = "";
  enhancePanel("rx_lyrics", false);
  document.querySelector('[data-enhance-undo="rx_lyrics"]').hidden = true;
  delete enhanceBefore.rx_lyrics;
  $("#rx_note").textContent = kind === "cover"
    ? `Same seed (${job.seed_text}) and the same recording, so the cover follows the same score. Only the words change.`
    : job.result?.score ? `Same seed (${job.seed_text}) and the song's saved score: the melody stays, only the words change.`
      : `Same seed (${job.seed_text}): the score is written again from the original words, then sung with yours.`;
  syncRemixCount();
  window.BunnyStudioDialog?.open?.($("#remix-dialog"));
}

async function submitRemix() {
  const job = remixJob;
  if (!job) return;
  const lyrics = $("#rx_lyrics").value.trim();
  const original = String($("#rx_lyrics").dataset.original || "").trim();
  if (!lyrics) return toast("Write the new words first.", "error");
  if (lyrics === original) return toast("Change some words first; the same words give the same song.", "error");
  const kind = remixKind(job);
  const request = { ...(job.request || {}) };
  let path = "/api/generate", body;
  if (kind === "yue2") {
    const score = job.result?.score || "";
    body = { ...request, seed: null, lyrics, score_abc: score,
      score_lyrics: score ? "" : (request.score_lyrics || request.lyrics || original), remix_of: job.id };
  } else {
    path = "/api/pack/run";
    body = { ...request, seed: null, lyrics, remix_of: job.id };   // the server uses the song's exact seed
    if (kind === "song" && job.result?.score) body.score_abc = job.result.score;
  }
  const button = $("#rx_go");
  button.disabled = true;
  try {
    await Api.request(path, { method: "POST", body });
    window.BunnyStudioDialog?.close?.($("#remix-dialog"));
    toast("Remix queued: same seed and score, new words.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

$("#rx_lyrics").addEventListener("input", (event) => {
  syncRemixCount();
  if (event.isTrusted) { document.querySelector('[data-enhance-undo="rx_lyrics"]').hidden = true; delete enhanceBefore.rx_lyrics; }
});
$("#rx_reset").addEventListener("click", () => {
  $("#rx_lyrics").value = $("#rx_lyrics").dataset.original || "";
  syncRemixCount();
});
$("#rx_go").addEventListener("click", submitRemix);


/* ------------------------------------------------------- edit a track -- */
// YuE2 Studio songs and their edits can be edited; so can covers made since covers keep their YuE2 take (the edit
// works on that take, then the cover's voice sings it again), and the edits of those covers.
function editable(job) {
  if (job.state !== "complete" || !job.output_url) return false;
  const task = job.request?.task;
  if (task === "song") return true;
  if (task === "cover") return Boolean(job.result?.take);
  if (task === "edit") {
    const source = state.jobs.find((item) => item.id === job.request?.source_job);
    return !(source?.request?.task === "cover" || source?.result?.take) || Boolean(job.result?.take);
  }
  return false;
}

function openEdit(id) {
  $("#model").value = "yue2_pack";
  $("#model").dispatchEvent(new Event("change"));
  $("#pk_task").value = "edit";
  syncPack();
  $("#pk_source").value = id;
  $("#pk_edits").value = "[]";
  $("#pack-card").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("Pick an edit (Retake, New words, Extend…), press Add edit, then Generate.");
}


/* ------------------------------------------------------- cover draft -- */
// A cover being set up (the chosen song, the read of it still running, and what's in Music direction and Lyrics)
// is kept in this browser, so a phone that closes the page while you're away picks it up where it was: the read and
// the direction carry on on the server, and their text is filled in when you're back.
const COVER_DRAFT_KEY = "music.coverDraft";
const COVER_DRAFT_DAYS = 3;

function saveCoverDraft() {
  try {
    if (!cover && !songJob) return localStorage.removeItem(COVER_DRAFT_KEY);
    localStorage.setItem(COVER_DRAFT_KEY, JSON.stringify({ at: Date.now(), cover, songJob,
      style: $("#style").value, lyrics: $("#lyrics").value }));
  } catch { /* storage blocked: this page only */ }
}

function restoreCoverDraft() {
  let draft;
  try { draft = JSON.parse(localStorage.getItem(COVER_DRAFT_KEY) || "null"); } catch { draft = null; }
  if (!draft || Date.now() - (draft.at || 0) > COVER_DRAFT_DAYS * 86400000) return;
  if (draft.cover) {
    cover = draft.cover;
    if ($("#model").value !== "yue2") {
      $("#model").value = "yue2";
      $("#model").dispatchEvent(new Event("change"));
    }
  }
  if (draft.style && !$("#style").value) $("#style").value = draft.style;
  if (draft.lyrics && !$("#lyrics").value) { $("#lyrics").value = draft.lyrics; $("#lyrics").dispatchEvent(new Event("input")); }
  // seen: the read was queued before the reload, so a job missing from the list is gone, not "not listed yet".
  if (draft.songJob) songJob = { ...draft.songJob, seen: true };
  syncCover();
  if (songJob) {
    if (songJob.whisper && !songJob.lyricsDone) setWriting("lyrics", "Whisper: picking up where it was");
    if (songJob.agentic && !songJob.directionDone) setWriting("style", "Agentic: picking up where it was");
    $("#cv_write").disabled = true;
  }
  toast(songJob ? "Picked up your cover where you left it." : "Your cover song is still chosen.");
}

// What you type while a cover is chosen is kept with it (typing into a box being written can't happen: it's locked).
["style", "lyrics"].forEach((id) => document.getElementById(id).addEventListener("input", () => { if (cover || songJob) saveCoverDraft(); }));

// Last, once everything it uses is defined (the first job list arrives after this, asynchronously).
restoreVoice();
restoreCoverDraft();
resumeEnhances();
resumeVoiceImport();
