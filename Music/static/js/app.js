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
    if (!response.ok) throw new Error(data?.message || `Request failed (${response.status})`);
    return data;
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
    $("#yue2-options").hidden = minimax;
    $("#minimax-options").hidden = !minimax;
    $("#engine-label").textContent = minimax ? "MiniMax Music 3 · INT8" : "YuE2 · INT8";
    $("#model-note").hidden = !minimax || !minimaxMissing.length;
    $("#model-note").textContent = minimaxMissing.length ? `${minimaxMissing.length} model file${minimaxMissing.length === 1 ? "" : "s"} missing. Generate offers to find or download them.` : "";
  };
  const savedModel = localStorage.getItem("music.model");
  if (["yue2", "minimax_music3"].includes(savedModel)) modelSelect.value = savedModel;
  modelSelect.addEventListener("change", () => {
    try { localStorage.setItem("music.model", modelSelect.value); } catch { /* storage blocked */ }
    syncModel();
  });
  syncModel();
  Api.request("/api/models").then((models) => {
    minimaxMissing = models.minimax_music3?.missing || [];
    syncModel();
  }).catch(() => {});
  const strengthInput = $("#reference_strength");

  durationInput.addEventListener("input", () => {
    durationReadout.textContent = `${durationInput.value} sec`;
  });

  strengthInput.addEventListener("input", () => {
    $("#reference-strength-readout").textContent = Number(strengthInput.value).toFixed(2);
  });

  const referenceToggle = $("#use_reference");
  const youtubeToggle = $("#use_youtube");
  const manualSampling = $("#custom_music_sampling");
  const outputFormat = $("#output_format");
  const outputQuality = $("#output_quality");
  const syncReferenceFields = () => {
    const enabled = referenceToggle.checked;
    $("[data-local-reference]").hidden = !enabled || youtubeToggle.checked;
    $("[data-youtube-reference]").hidden = !enabled || !youtubeToggle.checked;
    youtubeToggle.disabled = !enabled;
    $("#reference_mode").disabled = !enabled;
  };
  referenceToggle.addEventListener("change", syncReferenceFields);
  youtubeToggle.addEventListener("change", syncReferenceFields);
  syncReferenceFields();
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

function filteredJobs() {
  const query = state.jobSearch.trim().toLowerCase();
  return state.jobs.filter((job) => {
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
  // Keep native media elements intact across SSE updates. In particular, do
  // not reset their src while another job's progress or queue state changes.
  if (current.nodeName === "AUDIO" && current.getAttribute("src") === next.getAttribute("src")) return current;
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
  const list = $("#jobs-list");
  const pager = $("#jobs-pager");
  const jobs = filteredJobs();
  const counts = { all: state.jobs.length, running: state.jobs.filter((job) => ACTIVE_JOB_STATES.has(job.state)).length,
    complete: state.jobs.filter((job) => job.state === "complete").length, failed: state.jobs.filter((job) => job.state === "failed").length };
  Object.entries(counts).forEach(([key, count]) => { $(`#count-${key}`).textContent = count; });
  if (!jobs.length) {
    list.replaceChildren(Object.assign(document.createElement("div"), {
      className: "jobs-empty",
      textContent: state.jobs.length ? "No tracks match this view." : "No generations yet. Describe a track and hit Generate.",
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
          <span class="job-chip">${job.model === "minimax_music3" ? "MiniMax Music 3" : "YuE2"}</span>
          <span class="job-chip">${job.duration}s requested</span>
          <span class="job-chip">seed ${job.seed}</span>
          <span class="job-elapsed" data-start="${job.started_at || job.created_at}" data-end="${isTerminal ? job.completed_at || "" : ""}"></span>
        </div>
        ${ACTIVE_STATES.has(job.state) ? `<div class="job-progress"><div class="job-progress-bar" style="width:${progress}%"></div></div>` : ""}
        ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
        ${job.state === "complete" && job.output_url ? `<audio controls src="${job.output_url}"></audio>` : ""}
        <div class="job-actions">
          <button type="button" class="secondary-button job-icon-button" data-job-info="${job.id}" title="Generation details" aria-label="Generation details">${window.BunnyStudioIcons?.icon("info") || "ⓘ"}</button>
          ${canCancel ? `<button type="button" class="secondary-button job-icon-button danger" data-cancel="${job.id}" title="Cancel" aria-label="Cancel">${window.BunnyStudioIcons?.icon("cancel") || "■"}</button>` : `<button type="button" class="secondary-button job-icon-button" data-retry="${job.id}" title="Retry: load the exact settings, reference and seed into the form" aria-label="Retry: load the exact settings, reference and seed into the form">${window.BunnyStudioIcons?.icon("retry") || "↻"}</button>`}
          ${job.state === "complete" && job.download_url ? `<a class="secondary-button job-icon-button" href="${job.download_url}" download title="Download" aria-label="Download">${window.BunnyStudioIcons?.icon("download") || "↓"}</a>` : ""}
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

// Like Video's Retry: the job's exact settings, reference and seed go back
// into the form for review instead of being queued straight away.
function retryJob(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job?.request?.style) {
    toast("The saved generation settings are unavailable.", "error");
    return;
  }
  const request = { ...job.request, seed: job.seed };
  const form = $("#generate-form");
  form.querySelectorAll("[data-request]").forEach((field) => {
    if (!(field.name in request)) return;
    const value = request[field.name];
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  try { localStorage.setItem("music.model", $("#model").value); } catch { /* storage blocked */ }
  // Re-run the form's own syncing (model panels, reference/sampling sections, readouts).
  ["#model", "#use_reference", "#use_youtube", "#custom_music_sampling", "#output_format"]
    .forEach((id) => $(id).dispatchEvent(new Event("change")));
  if (request.output_quality) $("#output_quality").value = request.output_quality;
  ["#duration", "#reference_strength"].forEach((id) => $(id).dispatchEvent(new Event("input")));
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
      <pre class="studio-prompt-text">${escapeHtml(value)}</pre>
    </section>`;
  body.innerHTML = `${promptSection("Style", job.style, "style")}${promptSection("Lyrics", job.lyrics || "Instrumental (no lyrics provided).", "lyrics")}`;
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
  window.BunnyStudioDialog?.open("#job-info");
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
    renderJobs();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* -------------------------------------------------------------- init -- */

bindForm();
refreshJobsOnce();
bindEvents();
setInterval(updateElapsedTimes, 1000);
