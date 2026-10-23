const API_BASE = document.body.dataset.appPrefix || "";
// LoRA lists per model family. Krea 2 and Lustify share one list.
// LORA_SLOTS / LORA_DEFAULTS / LORA_INFO always hold the current model's set (useLoraSet).
const LORA_SETS = {
  slots: JSON.parse(document.body.dataset.loraSlots || "{}"),
  defaults: JSON.parse(document.body.dataset.loraDefaults || "{}"),
  info: JSON.parse(document.body.dataset.loraInfo || "{}"),
};
let LORA_SLOTS = LORA_SETS.slots.krea || [];
let LORA_DEFAULTS = LORA_SETS.defaults.krea || [];
const REFERENCE_SLOT_COUNT = Number(document.body.dataset.referenceSlotCount || 4);
const REFERENCE_DEFAULTS = JSON.parse(document.body.dataset.referenceDefaults || "[]");
const MODEL_CONFIG = JSON.parse(document.body.dataset.modelConfig || "{}");
const QWEN = "qwen_image_21";
const ANIMA = "anima";
const MOSS = "moss";
const MAX_REFERENCE_SLOTS = Math.max(REFERENCE_SLOT_COUNT, ...Object.values(MODEL_CONFIG).map((config) => config.reference_slots || 0));
const MODEL_STORAGE_KEY = "krea.model";

const $ = (selector, parent = document) => parent.querySelector(selector);

const state = {
  jobs: [],
  comfyOnline: false,
  model: "krea2",
  checkpoints: { krea2: [], qwen_image_21: [] },
  references: Array.from({ length: MAX_REFERENCE_SLOTS }, (_, i) => ({
    enabled: i === 0,
    strength: REFERENCE_DEFAULTS[i] ?? 1.0,
    image: null,
    width: null,
    height: null,
    uploading: false,
    localUrl: null, // object URL of the file being uploaded, shown until the server copy loads
    original: null, // the uncropped upload, once this slot has been cropped
    crop: null, // {x, y, w, h} of the saved crop, normalized to the original
  })),
};

const isQwen = () => state.model === QWEN;
const referenceSlotCount = () => MODEL_CONFIG[state.model]?.reference_slots ?? REFERENCE_SLOT_COUNT;

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
  preparing: "Preparing",
  waiting_for_gpu: "Waiting for GPU",
  queued: "Queued",
  enhancing: "Enhancing prompt",
  rendering: "Rendering",
  complete: "Complete",
  failed: "Failed",
  cancelling: "Cancelling",
  cancelled: "Cancelled",
};

const ACTIVE_STATES = new Set(["waiting_for_gpu", "preparing", "queued", "enhancing", "rendering", "cancelling"]);

/* --------------------------------------------------------------- loras -- */

let LORA_INFO = LORA_SETS.info.krea || [];
let loraSet = "krea";
const loraSetFor = (model) => model === QWEN ? "qwen" : model === MOSS ? "moss" : model === ANIMA ? "anima" : "krea";
const loraStateBySet = {}; // each family's switches/strengths, kept while the other is shown

// Swap the LoRA list to `model`'s family, keeping what was set on the list being left.
function useLoraSet(model) {
  const next = loraSetFor(model);
  if (next === loraSet && document.querySelector("#lora-list .lora-row")) return;
  if (document.querySelector("#lora-list .lora-row")) loraStateBySet[loraSet] = collectLoras();
  loraSet = next;
  LORA_SLOTS = LORA_SETS.slots[next] || [];
  LORA_DEFAULTS = LORA_SETS.defaults[next] || [];
  LORA_INFO = LORA_SETS.info[next] || [];
  renderLoras();
  (loraStateBySet[next] || []).forEach((slot, index) => {
    const row = $(`#lora-list [data-lora-index="${index}"]`);
    if (!row || !slot) return;
    row.querySelector(".lora-enabled").checked = slot.enabled;
    row.querySelector(".lora-strength").value = slot.strength;
  });
  syncLoraRows();
}
const loraLabel = (index) => LORA_INFO[index]?.label || LORA_SLOTS[index].replace(/\.safetensors$/, "");
const loraDefaultStrength = (index) => LORA_INFO[index]?.strength ?? 0.8;

// One LoRA row, as in H3 Machine: "!" (LoRAs with trigger words) shows them on
// hover/focus, "i" opens the source page (its tooltip has the recommended
// strength). An enabled LoRA with a trigger is tinted amber as a reminder to
// put the trigger in the prompt. Strength starts at the recommended value.
function loraRowHtml(index) {
  const info = LORA_INFO[index] || {};
  const triggers = info.triggers || [];
  const enabled = Boolean(LORA_DEFAULTS[index]);
  const tip = `Trigger words:\n${triggers.map((word) => `• ${word}`).join("\n")}`;
  const strengthTitle = `Recommended ${loraDefaultStrength(index)}${info.strength_note ? ` — ${info.strength_note}` : ""}`;
  return `
    <div class="lora-row${triggers.length ? " has-trigger" : ""}${enabled ? " is-enabled" : ""}" data-lora-index="${index}" ${info.deleted ? "hidden" : ""}>
      <label class="lora-label" title="${escapeHtml(LORA_SLOTS[index])}">
        <span class="switch switch-small"><input type="checkbox" class="lora-enabled" ${enabled ? "checked" : ""} ${info.deleted ? "disabled" : ""}><span></span></span>
        <span class="lora-name">${escapeHtml(loraLabel(index))}</span>
      </label>
      <span class="lora-badges">
        ${triggers.length ? `<span class="lora-badge lora-badge-trigger" tabindex="0" aria-label="${escapeHtml(tip)}" data-tip="${escapeHtml(tip)}">!</span>` : ""}
        ${info.url ? `<a class="lora-badge" href="${escapeHtml(info.url)}" target="_blank" rel="noopener noreferrer" title="Open source page — ${escapeHtml(strengthTitle)}">i</a>` : ""}
      </span>
      <input type="number" class="lora-strength" min="0" max="2" step="0.05" value="${loraDefaultStrength(index)}" title="${escapeHtml(strengthTitle)}" aria-label="${escapeHtml(loraLabel(index))} strength">
    </div>`;
}

// Rows under a small heading per group (in order of each group's first appearance).
function renderLoras() {
  if (!LORA_SLOTS.length) {
    $("#lora-list").innerHTML = '<p class="field-hint">No LoRAs installed for this model. Open Gallery to upload or link one.</p>';
    return;
  }
  const groups = [...new Set(LORA_SLOTS.map((_, index) => LORA_INFO[index]?.group || ""))];
  const ordered = LORA_SLOTS.map((_, index) => index)
    .sort((a, b) => groups.indexOf(LORA_INFO[a]?.group || "") - groups.indexOf(LORA_INFO[b]?.group || "") || a - b);
  let group = null;
  $("#lora-list").innerHTML = ordered.map((index) => {
    const name = LORA_INFO[index]?.group || "";
    const heading = name && name !== group ? `<div class="lora-group">${escapeHtml(name)}</div>` : "";
    group = name;
    return heading + loraRowHtml(index);
  }).join("");
}

// The enabled tint follows the switches, however they changed (list, gallery, preset, "Use these settings").
function syncLoraRows() {
  let enabled = 0;
  document.querySelectorAll("#lora-list .lora-row").forEach((row) => {
    const on = row.querySelector(".lora-enabled").checked;
    row.classList.toggle("is-enabled", on);
    enabled += on;
  });
  const badge = $("#lora-count");
  if (badge) badge.textContent = enabled ? `${enabled} on` : "";
}

function collectLoras() {
  // Indexed by data-lora-index, not DOM order - rows are displayed grouped.
  const slots = [];
  document.querySelectorAll("#lora-list .lora-row").forEach((row) => {
    slots[Number(row.dataset.loraIndex)] = {
      enabled: row.querySelector(".lora-enabled").checked,
      strength: Number(row.querySelector(".lora-strength").value),
    };
  });
  return slots;
}

/* -------------------------------------------------------------- icons -- */
// Inline SVG icons, the same set and stroke style as H3 Machine's.
const ICON_PATHS = {
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  cancel: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  // Put this generation's prompt and settings back into the form.
  reuse: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  save: '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
  pencil: '<path d="M21.2 6.8a2.8 2.8 0 0 0-4-4L3.8 16.2a2 2 0 0 0-.5.8L2 21.4a.5.5 0 0 0 .6.6l4.4-1.3a2 2 0 0 0 .8-.5z"/><path d="m15 5 4 4"/>',
  upload: '<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
};
const uiIcon = (name) => `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name]}</svg>`;

// Template placeholders: an element with data-icon gets the icon as (the start of) its content.
function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    node.insertAdjacentHTML("afterbegin", uiIcon(node.dataset.icon));
    node.removeAttribute("data-icon");
  });
}

/* ------------------------------------------------------------ dialogs -- */

function openDialog(id) { $(id).hidden = false; }
function closeDialog(node) {
  node.hidden = true;
  if (node.id === "lora-gallery") galleryVideoObserver.disconnect();
  if (node.id === "viewer") {
    cropCleanup?.();
    cropCleanup = null;
    $("#viewer-stage").innerHTML = "";
  }
}

function bindDialogs() {
  document.addEventListener("click", (event) => {
    const close = event.target.closest("[data-dialog-close]");
    if (close) closeDialog(close.closest(".dialog"));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#lora-gallery").hidden && gallery.detail !== null) {
      gallery.detail = null; gallery.editing = false; renderLoraGallery(); return;
    }
    if (!$("#lora-gallery").hidden && !$("#lora-gallery-config").hidden) {
      $("#lora-gallery-config").hidden = true; renderLoraGallery(); return;
    }
    const open = [...document.querySelectorAll(".dialog:not([hidden])")].pop();
    if (open) closeDialog(open);
  });
}

// Full-size image: a reference or a generated image, with optional extra buttons.
function openViewer(src, caption = "", actions = "", checkerboard = false) {
  $("#viewer-stage").innerHTML = `<img src="${src}" alt=""${checkerboard ? ' class="checkerboard"' : ""}>`;
  $("#viewer-bar").innerHTML = `<span class="viewer-caption">${caption}</span>${actions}`;
  openDialog("#viewer");
}

/* --------------------------------------------------------- references -- */
// H3 Machine-style upload rows: a thumbnail (or an Upload box when empty), the
// slot's name/token, its strength, an on/off switch and a remove button.
// Images can be dropped onto a row (or anywhere on the list: first empty slot).
// The uploaded filenames are kept in this browser, so references survive a
// reload; their thumbnails come back from ComfyUI's input folder.
const REFERENCE_STORAGE_KEY = "krea.references";

const referencePreviewUrl = (image) => `${API_BASE}/api/references/${encodeURIComponent(image)}/preview`;

function loadStoredReferences() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(REFERENCE_STORAGE_KEY)); } catch { /* storage blocked */ }
  if (!Array.isArray(stored)) return;
  stored.slice(0, MAX_REFERENCE_SLOTS).forEach((slot, index) => {
    if (!slot || typeof slot !== "object") return;
    Object.assign(state.references[index], {
      enabled: Boolean(slot.enabled), strength: Number(slot.strength ?? state.references[index].strength),
      image: typeof slot.image === "string" ? slot.image : null, width: slot.width || null, height: slot.height || null,
      original: typeof slot.original === "string" ? slot.original : null, crop: slot.crop && typeof slot.crop === "object" ? slot.crop : null,
    });
  });
}

function saveReferences() {
  const slots = state.references.map(({ enabled, strength, image, width, height, original, crop }) => ({ enabled, strength, image, width, height, original, crop }));
  try { localStorage.setItem(REFERENCE_STORAGE_KEY, JSON.stringify(slots)); } catch { /* storage blocked */ }
}

// Rows come from the shared BunnyReferences component (H3 Machine's
// References card): thumbnail, token chip, size line, then Krea's own extras
// (strength, Replace) and the on/off switch. Only attached slots are listed,
// in slot order; removing one moves the later ones up, like H3.
function referenceItem(ref, index) {
  const token = isQwen() ? `<image${index + 1}>` : `Reference ${index + 1}`;
  const meta = ref.uploading ? "Uploading…" : ref.width && ref.height ? `${ref.width}×${ref.height}` : "Uploaded";
  const extras = `<span class="reference-tools">
      ${isQwen() ? "" : `<label class="reference-strength-field" title="Krea 2 style-reference LoRA weight — the strongest enabled reference sets it for all references">Strength <input type="number" class="reference-strength" min="0" max="2" step="0.05" value="${ref.strength}" aria-label="Reference ${index + 1} strength"></label>`}
      <button type="button" class="button-link" data-ref-pick="${index}">Replace</button>
    </span>`;
  return {
    key: String(index), thumb: ref.localUrl || (ref.image ? referencePreviewUrl(ref.image) : ""), token,
    insertToken: isQwen() ? token : null, meta, metaTitle: ref.image || "", extras, busy: ref.uploading, off: !ref.enabled,
    after: `<label class="switch" title="Use this reference"><input type="checkbox" class="reference-enabled" ${ref.enabled ? "checked" : ""} aria-label="Use Reference ${index + 1}"><span></span></label>`,
    removeLabel: `Remove Reference ${index + 1}`,
  };
}

function renderReferences() {
  const slots = state.references.slice(0, referenceSlotCount());
  const items = slots.map((ref, index) => (ref.image || ref.uploading ? referenceItem(ref, index) : null)).filter(Boolean);
  window.BunnyReferences.render($("#reference-list"), items);
  $("#reference-badge").textContent = items.length ? String(items.length) : "";
  renderReferenceTotals();
  scheduleEstimate();
  renderHotbar();
  updateSizeNote();
}

async function uploadReference(index, file) {
  if (!file) return;
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    toast("Reference images must be PNG, JPEG, or WEBP.", "error");
    return;
  }
  const ref = state.references[index];
  if (ref.localUrl) URL.revokeObjectURL(ref.localUrl);
  Object.assign(ref, { uploading: true, localUrl: URL.createObjectURL(file) });
  renderReferences();
  try {
    const form = new FormData();
    form.append("file", file);
    const result = await Api.request("/api/upload", { method: "POST", body: form });
    Object.assign(ref, { image: result.image, enabled: true, original: null, crop: null }, await imageSize(file));
    // An attached reference is meant to be used.
    if (!$("#use-references").checked) { $("#use-references").checked = true; updateReferenceHint(); }
  } catch (error) {
    toast(error.message, "error");
    URL.revokeObjectURL(ref.localUrl);
    ref.localUrl = null;
  } finally {
    ref.uploading = false;
    saveReferences();
    renderReferences();
  }
}

function removeReference(index) {
  const slots = state.references.slice(0, referenceSlotCount());
  if (slots[index]?.localUrl) URL.revokeObjectURL(slots[index].localUrl);
  // Later references move up one slot (H3 numbering), strengths stay with their image.
  for (let i = index; i < slots.length; i += 1) {
    const next = slots[i + 1];
    Object.assign(slots[i], next && (next.image || next.uploading)
      ? { enabled: next.enabled, strength: next.strength, image: next.image, width: next.width, height: next.height,
        localUrl: next.localUrl, original: next.original, crop: next.crop, uploading: next.uploading }
      : { image: null, width: null, height: null, localUrl: null, original: null, crop: null, uploading: false, enabled: false });
    if (!next || !(next.image || next.uploading)) break;
  }
  saveReferences();
  renderReferences();
}

function pickReferenceFile(index) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/webp";
  input.addEventListener("change", () => uploadReference(index, input.files?.[0]));
  input.click();
}

function addReferenceFiles(files) {
  const count = referenceSlotCount();
  const empty = state.references.slice(0, count).map((ref, i) => (!ref.image && !ref.uploading ? i : -1)).filter((i) => i >= 0);
  if (files.length > empty.length) toast(`Only ${empty.length} empty slot(s) left - the first ${empty.length} image(s) were added.`, "error");
  files.slice(0, empty.length).forEach((file, i) => uploadReference(empty[i], file));
}

function pickReferenceFiles() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/webp";
  input.multiple = true;
  input.addEventListener("change", () => addReferenceFiles([...(input.files || [])]));
  input.click();
}

function bindReferences() {
  const list = $("#reference-list");
  const refs = window.BunnyReferences;
  refs.bindList(list, {
    onInsert: (token) => refs.insertToken($("#prompt"), token),
    onView: (key) => openReferenceViewer(Number(key)),
    onRemove: (key) => removeReference(Number(key)),
  });
  list.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-ref-pick]");
    if (pick) pickReferenceFile(Number(pick.dataset.refPick));
  });
  list.addEventListener("change", (event) => {
    const row = event.target.closest("[data-reference-id]");
    if (!row) return;
    const ref = state.references[Number(row.dataset.referenceId)];
    if (event.target.classList.contains("reference-enabled")) { ref.enabled = event.target.checked; row.classList.toggle("is-off", !ref.enabled); }
    if (event.target.classList.contains("reference-strength")) ref.strength = Number(event.target.value);
    saveReferences();
    renderReferenceTotals();
    scheduleEstimate();
  });
  // Drop onto a row replaces that reference; anywhere else in the card adds.
  refs.bindDropZone($("#reference-drop-zone"), {
    area: $("#reference-panel"),
    onFiles: (files, key) => {
      if (key !== null) {
        uploadReference(Number(key), files[0]);
        files = files.slice(1);
      }
      if (files.length) addReferenceFiles(files);
    },
  });
  $("#reference-drop-zone").addEventListener("reference-pick", pickReferenceFiles);
  $("#reference-add").addEventListener("click", pickReferenceFiles);
  // The Viewer's "Replace…" button lives outside the list.
  $("#viewer").addEventListener("click", (event) => {
    const pick = event.target.closest("[data-ref-pick]");
    if (pick) pickReferenceFile(Number(pick.dataset.refPick));
  });
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const needsSpace = start > 0 && !/\s$/.test(textarea.value.slice(0, start));
  textarea.setRangeText(`${needsSpace ? " " : ""}${text}`, start, end, "end");
  textarea.focus();
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

async function imageSize(file) {
  try {
    const bitmap = await createImageBitmap(file);
    const size = { width: bitmap.width, height: bitmap.height };
    bitmap.close();
    return size;
  } catch {
    return { width: null, height: null };
  }
}

function collectReferences() {
  return state.references.slice(0, referenceSlotCount()).map((ref) => {
    // An empty slot is never sent as on (it may be hidden - see renderReferences).
    const slot = { enabled: ref.enabled && Boolean(ref.image), strength: ref.strength, image: ref.image };
    if (ref.width && ref.height) Object.assign(slot, { width: ref.width, height: ref.height });
    return slot;
  });
}

/* --------------------------------------------------------------- crop -- */
// Same crop tool as H3 Machine: drag the box or its corners, Save crop. It is
// non-destructive - the slot remembers the original upload (ref.original) and
// the box (ref.crop, normalized), so cropping again starts from the original
// with the box where it was left. The crop is uploaded as a new reference file.

let cropCleanup = null;

function openReferenceViewer(index) {
  cropCleanup?.();
  cropCleanup = null;
  const ref = state.references[index];
  if (!ref?.image && !ref?.localUrl) return;
  const src = ref.localUrl || referencePreviewUrl(ref.image);
  $("#viewer-stage").innerHTML = `<div class="crop-frame" id="crop-frame"><img id="viewer-image" src="${src}" alt=""></div>`;
  $("#viewer-bar").innerHTML = `
    <span class="viewer-caption">Reference ${index + 1}${ref.width ? ` · ${ref.width}×${ref.height}` : ""}${ref.crop ? " · cropped" : ""}</span>
    <button type="button" class="secondary-button" id="viewer-crop-start" ${ref.uploading ? "disabled" : ""}>Crop…</button>
    <button type="button" class="primary-button" id="viewer-crop-save" hidden>Save crop</button>
    <button type="button" class="secondary-button" id="viewer-crop-cancel" hidden>Cancel</button>
    ${ref.original ? `<button type="button" class="secondary-button" id="viewer-crop-reset" title="Go back to the uncropped upload">Undo crop</button>` : ""}
    <button type="button" class="secondary-button" data-dialog-close data-ref-pick="${index}">Replace…</button>`;
  $("#viewer-crop-start").addEventListener("click", () => startCrop(index));
  $("#viewer-crop-reset")?.addEventListener("click", () => resetCrop(index));
  openDialog("#viewer");
}

function startCrop(index) {
  const ref = state.references[index];
  const frame = $("#crop-frame");
  const img = $("#viewer-image");
  $("#viewer-crop-start").hidden = true;
  $("#viewer-crop-save").hidden = false;
  $("#viewer-crop-cancel").hidden = false;
  const box = document.createElement("div");
  box.className = "crop-box";
  for (const corner of ["nw", "ne", "sw", "se"]) {
    const handle = document.createElement("div");
    handle.className = `crop-handle ${corner}`;
    handle.dataset.corner = corner;
    box.appendChild(handle);
  }
  frame.appendChild(box);
  const seedBox = () => {
    const rect = img.getBoundingClientRect();
    const crop = ref.crop;
    if (crop) {
      Object.assign(box.style, { left: `${crop.x * rect.width}px`, top: `${crop.y * rect.height}px`, width: `${crop.w * rect.width}px`, height: `${crop.h * rect.height}px` });
    } else {
      const margin = Math.min(rect.width, rect.height) * 0.1;
      Object.assign(box.style, { left: `${margin}px`, top: `${margin}px`, width: `${rect.width - margin * 2}px`, height: `${rect.height - margin * 2}px` });
    }
  };
  // Always crop from the original upload, never a crop of a crop.
  const source = referencePreviewUrl(ref.original || ref.image);
  if (!img.src.endsWith(source)) img.src = source;
  if (img.complete && img.naturalWidth > 0) seedBox();
  else img.addEventListener("load", seedBox, { once: true });
  bindCropDrag(frame, box);
  $("#viewer-crop-save").onclick = () => saveCrop(index, img, box);
  $("#viewer-crop-cancel").onclick = () => openReferenceViewer(index);
}

function bindCropDrag(frame, box) {
  let dragMode = null;
  let startPointer = null;
  let startRect = null;
  let previousUserSelect = "";
  const currentRect = () => ({ left: box.offsetLeft, top: box.offsetTop, width: box.offsetWidth, height: box.offsetHeight });
  const applyStyle = (rect) => Object.assign(box.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
  // Moving only clamps the position (the size stays); resizing clamps the size.
  const clampMove = (rect) => {
    const bounds = frame.getBoundingClientRect();
    rect.left = Math.max(0, Math.min(rect.left, bounds.width - rect.width));
    rect.top = Math.max(0, Math.min(rect.top, bounds.height - rect.height));
    return rect;
  };
  const clampResize = (rect) => {
    const bounds = frame.getBoundingClientRect();
    rect.left = Math.max(0, Math.min(rect.left, bounds.width - 20));
    rect.top = Math.max(0, Math.min(rect.top, bounds.height - 20));
    rect.width = Math.max(20, Math.min(rect.width, bounds.width - rect.left));
    rect.height = Math.max(20, Math.min(rect.height, bounds.height - rect.top));
    return rect;
  };
  function onPointerDown(event) {
    const handle = event.target.closest(".crop-handle");
    dragMode = handle ? handle.dataset.corner : (event.target === box ? "move" : null);
    if (!dragMode) return;
    startPointer = { x: event.clientX, y: event.clientY };
    startRect = currentRect();
    previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";
    event.preventDefault();
  }
  function onPointerMove(event) {
    if (!dragMode) return;
    event.preventDefault();
    const dx = event.clientX - startPointer.x;
    const dy = event.clientY - startPointer.y;
    const rect = { ...startRect };
    if (dragMode === "move") {
      rect.left = startRect.left + dx;
      rect.top = startRect.top + dy;
      applyStyle(clampMove(rect));
      return;
    }
    if (dragMode.includes("w")) { rect.left = startRect.left + dx; rect.width = startRect.width - dx; }
    if (dragMode.includes("e")) rect.width = startRect.width + dx;
    if (dragMode.includes("n")) { rect.top = startRect.top + dy; rect.height = startRect.height - dy; }
    if (dragMode.includes("s")) rect.height = startRect.height + dy;
    applyStyle(clampResize(rect));
  }
  function onPointerUp() {
    dragMode = null;
    document.body.style.userSelect = previousUserSelect;
  }
  box.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  cropCleanup = () => {
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    document.body.style.userSelect = previousUserSelect;
  };
}

async function saveCrop(index, img, box) {
  const ref = state.references[index];
  const scaleX = img.naturalWidth / img.clientWidth;
  const scaleY = img.naturalHeight / img.clientHeight;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(box.offsetWidth * scaleX));
  canvas.height = Math.max(1, Math.round(box.offsetHeight * scaleY));
  canvas.getContext("2d").drawImage(
    img, box.offsetLeft * scaleX, box.offsetTop * scaleY, box.offsetWidth * scaleX, box.offsetHeight * scaleY,
    0, 0, canvas.width, canvas.height,
  );
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) return toast("Couldn't crop the image.", "error");
  const crop = { x: box.offsetLeft / img.clientWidth, y: box.offsetTop / img.clientHeight, w: box.offsetWidth / img.clientWidth, h: box.offsetHeight / img.clientHeight };
  $("#viewer-crop-save").disabled = true;
  try {
    const form = new FormData();
    form.append("file", new File([blob], "crop.png", { type: "image/png" }));
    const result = await Api.request("/api/upload", { method: "POST", body: form });
    if (ref.localUrl) URL.revokeObjectURL(ref.localUrl);
    Object.assign(ref, { original: ref.original || ref.image, image: result.image, crop, width: canvas.width, height: canvas.height, localUrl: null });
    saveReferences();
    renderReferences();
    openReferenceViewer(index);
    toast("Crop saved.");
  } catch (error) {
    $("#viewer-crop-save").disabled = false;
    toast(error.message, "error");
  }
}

// Back to the uncropped upload (it was never deleted).
async function resetCrop(index) {
  const ref = state.references[index];
  if (!ref.original) return;
  let size = { width: null, height: null };
  try {
    const image = new Image();
    image.src = referencePreviewUrl(ref.original);
    await image.decode();
    size = { width: image.naturalWidth, height: image.naturalHeight };
  } catch { /* size stays unknown */ }
  Object.assign(ref, { image: ref.original, original: null, crop: null, localUrl: null }, size);
  saveReferences();
  renderReferences();
  openReferenceViewer(index);
  toast("Crop undone.");
}

/* ------------------------------------------------------------ estimate -- */
// "Estimated time to complete" above the prompt, like H3 Machine: the average
// of the last 5 finished generations with the same render-relevant settings
// (from the generation history; there is no separate timing table here).

// Seconds per image (a multi-image job's time is split over its images), times
// the requested number of images.
function estimateFor(model, request) {
  const key = speedKey(model, request);
  const times = state.jobs
    .filter((job) => job.state === "complete" && job.started_at && job.completed_at && speedKey(job.model, job.request || {}) === key)
    .sort((a, b) => b.completed_at.localeCompare(a.completed_at))
    .slice(0, 5)
    .map((job) => (new Date(job.completed_at) - new Date(job.started_at)) / 1000 / (job.batch_count || job.request?.count || 1));
  if (!times.length) return null;
  const perImage = times.reduce((sum, value) => sum + value, 0) / times.length;
  const count = request.count || 1;
  return { seconds: perImage * count, perImage, count, samples: times.length };
}

// Settings that change render time: model, requested size, 4x upscale, number
// of references used, prompt enhancement (+ thinking), and Qwen-only sampling.
function speedKey(model, request) {
  const refs = request.use_references ? (request.references || []).filter((ref) => ref.enabled && ref.image).length : 0;
  const qwen = model === QWEN
    ? [isQwenTurboCheckpoint(request.checkpoint) ? "turbo" : request.steps, Boolean(refs && request.match_reference_size)]
    : [];
  const turbo = [request.speed || "standard", model === MOSS ? request.moss_steps || 50 : model === QWEN ? "" : request.krea_steps || ""];
  const enhancer = [Boolean(request.prompt_enhancer), Boolean(request.prompt_enhancer && request.enhancer_thinking)];
  return JSON.stringify([model, request.width, request.height, Boolean(request.enhance), refs, ...enhancer, ...qwen, ...turbo]);
}

function renderEstimate() {
  const settings = collectSettings();
  const estimate = estimateFor(state.model, settings);
  $("#eta").classList.toggle("is-empty", !estimate);
  $("#eta-value").textContent = estimate ? `~${formatSeconds(estimate.seconds)}` : "—";
  const basis = !estimate ? "no finished runs with these settings yet"
    : estimate.samples === 1 ? "from 1 run" : `avg of last ${estimate.samples} runs`;
  $("#eta-basis").textContent = estimate && estimate.count > 1 ? `${formatSeconds(estimate.perImage)} per image · ${basis}` : basis;
}

let estimateTimer = 0;
function scheduleEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(renderEstimate, 200);
}

/* ------------------------------------------------- reference limits -- */

function renderReferenceTotals() {
  const count = referenceSlotCount();
  const attached = state.references.slice(0, count).filter((ref) => ref.image).length;
  const used = $("#use-references").checked
    ? state.references.slice(0, count).filter((ref) => ref.image && ref.enabled).length : 0;
  const lines = [
    `Total references: ${attached}/${count}`,
    `Used this run: ${used}${$("#use-references").checked ? "" : " (Use reference images is off)"}`,
    "Images only: PNG, JPEG or WEBP, up to 20 MB each",
  ];
  $("#reference-count").innerHTML = lines.map((line) => `<span class="reference-total">${escapeHtml(line)}</span>`).join("");
  $("#reference-add").disabled = attached >= count;
  $("#reference-add").title = attached >= count ? `All ${count} reference slots are filled - remove one first` : "Add one or more images (they fill the empty slots)";
}

/* -------------------------------------------------------------- model -- */

function renderCheckpointOptions() {
  const row = $("#checkpoint-row"), select = $("#checkpoint");
  row.hidden = state.model === ANIMA;
  if (row.hidden) return;
  const options = state.checkpoints[state.model] || [];
  const saved = (() => { try { return localStorage.getItem(`krea.checkpoint.${state.model}`); } catch { return null; } })();
  const previous = select.value;
  // GGUF files load through ComfyUI-GGUF (backend checkpoints.loader_node); the label says so
  const label = (name) => /\.gguf$/i.test(name) ? `${name.replace(/\.gguf$/i, "")} · GGUF` : name;
  select.innerHTML = options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(label(name))}</option>`).join("");
  const preferred = state.model === QWEN ? "qwen_image_2.1_int8_convrot.safetensors"
    : state.model === MOSS ? ($("#speed").value === "standard" ? "moss_image_sft_int8_convrot.safetensors" : "moss_image_sft_bf16.safetensors")
    : "krea2_turbo_int8_convrot.safetensors";
  select.value = [previous, saved, preferred, options[0]].find((name) => options.includes(name)) || "";
  select.disabled = !options.length;
  updateSpeedFields();
  $("#checkpoint-hint").textContent = options.length ? `${options.length} installed checkpoint${options.length === 1 ? "" : "s"}${state.model === "krea2" ? " · Lustify is listed here" : ""}` : "No compatible checkpoint installed yet. Generate offers to download this model's files.";
}

async function refreshCheckpoints() {
  const hint = $("#checkpoint-hint");
  hint.textContent = "Checking ComfyUI…";
  try {
    state.checkpoints = await Api.request("/api/checkpoints");
    renderCheckpointOptions();
  } catch (error) { hint.textContent = error.message; }
}

function applyModel(model) {
  state.model = MODEL_CONFIG[model] ? model : "krea2";
  try { localStorage.setItem(MODEL_STORAGE_KEY, state.model); } catch { /* storage unavailable */ }
  $("#model").value = state.model;
  document.querySelectorAll(".qwen-only").forEach((node) => { node.hidden = !isQwen(); });
  document.querySelectorAll(".anima-only").forEach((node) => { node.hidden = state.model !== ANIMA; });
  document.querySelectorAll(".moss-only").forEach((node) => { node.hidden = state.model !== MOSS; });
  $("#lora-panel").hidden = false;
  $("#reference-panel").hidden = state.model === ANIMA || state.model === MOSS;
  $("#enhance-row").hidden = state.model === MOSS;
  if (state.model === ANIMA) {
    const versions = MODEL_CONFIG[ANIMA].versions || {};
    const selected = $("#anima-version").value || "aesthetic";
    $("#anima-version").innerHTML = Object.entries(versions).map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("");
    $("#anima-version").value = versions[selected] ? selected : "aesthetic";
  }
  renderSpeedOptions();
  updateSpeedFields();
  useLoraSet(state.model); // Qwen has its own LoRA list
  $("#sampling-model-label").textContent = MODEL_CONFIG[state.model]?.label || "";
  $(".brand-mark").textContent = isQwen() ? "Qw" : state.model === ANIMA ? "An" : state.model === MOSS ? "Ms" : "Kr2";
  renderCheckpointOptions();
  const maxSide = MODEL_CONFIG[state.model]?.max_side ?? 1536;
  for (const id of ["#width", "#height"]) {
    const input = $(id);
    input.max = String(maxSide);
    if (Number(input.value) > maxSide) input.value = String(maxSide);
  }
  renderSizePresets();
  renderReferences();
  updateReferenceHint();
}

// Size presets per model. Krea 2 / Lustify: about 1 MP (their native size),
// multiples of 32, at most 1536 per side. Qwen-Image 2.1: up to native 2K.
const KREA_SIZE_PRESETS = [
  ["1024x1024", "1:1 · 1024×1024 (1 MP)"], ["1536x1536", "1:1 · 1536×1536 (2.4 MP, max)"],
  ["1152x896", "4:3 · 1152×896"], ["896x1152", "3:4 · 896×1152"],
  ["1280x1024", "5:4 · 1280×1024"], ["1024x1280", "4:5 · 1024×1280 (portrait / Instagram)"],
  ["1216x832", "3:2 · 1216×832"], ["832x1216", "2:3 · 832×1216"],
  ["1344x768", "16:9 · 1344×768"], ["768x1344", "9:16 · 768×1344 (phone)"],
  ["1536x640", "21:9 · 1536×640 (cinematic)"],
];
const SIZE_PRESETS = {
  krea2: KREA_SIZE_PRESETS,
  lustify: KREA_SIZE_PRESETS,
  [ANIMA]: KREA_SIZE_PRESETS,
  [MOSS]: KREA_SIZE_PRESETS,
  [QWEN]: [
    ["1024x1024", "1:1 · 1024×1024 (1 MP)"], ["1536x1536", "1:1 · 1536×1536"], ["2048x2048", "1:1 · 2048×2048 (native 2K)"],
    // Exact ratios that stay within QWEN_MAX_PIXELS (2752×1536 = 4.23 MP); the
    // earlier 2400×1792 / 2528×1696 were 4.29-4.30 MP and got rejected on Generate.
    ["2304x1728", "4:3 · 2304×1728"], ["1728x2304", "3:4 · 1728×2304"],
    ["2496x1664", "3:2 · 2496×1664"], ["1664x2496", "2:3 · 1664×2496"],
    ["2752x1536", "16:9 · 2752×1536"], ["1536x2752", "9:16 · 1536×2752"],
  ],
};

function renderSizePresets() {
  const select = $("#size-preset");
  const groups = { Square: [], Landscape: [], Portrait: [] };
  for (const [value, label] of SIZE_PRESETS[state.model] || []) {
    const [width, height] = value.split("x").map(Number);
    const group = width === height ? "Square" : width > height ? "Landscape" : "Portrait";
    groups[group].push(`<option value="${value}">${label}</option>`);
  }
  select.innerHTML = `<option value="">Custom</option>`
    + Object.entries(groups).filter(([, options]) => options.length)
      .map(([name, options]) => `<optgroup label="${name}">${options.join("")}</optgroup>`).join("");
  syncSizePreset();
}

// Shows the preset matching the current width/height, or Custom.
function syncSizePreset() {
  const value = `${$("#width").value}x${$("#height").value}`;
  const select = $("#size-preset");
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
  updateSizeNote();
}

// Qwen with a reference and "Output follows Reference 1" ignores width/height:
// say so under the size preset, so a chosen size doesn't silently do nothing.
function updateSizeNote() {
  const note = $("#size-note");
  if (!note) return;
  const followsReference = isQwen() && $("#use-references").checked && $("#match-reference-size").checked
    && state.references.slice(0, referenceSlotCount()).some((ref) => ref.image && ref.enabled);
  note.hidden = !followsReference;
}

// Speed (Krea 2 / Lustify): the turbo LoRA's strength row only while a turbo option is picked.
// Speed menu per model. Krea 2 / Lustify: Krea 2 Turbo step-distillation LoRAs;
// Qwen-Image 2.1: Viggle's 6-step turbo LoRA (benchmark 1024²: 8.7 s vs 22.9 s at 25 steps).
const KREA_SPEEDS = [
  ["standard", "Standard · 8 steps"],
  ["turbo4", "Turbo · 4 steps"],
  ["turbo2", "Preview · 2 steps"],
];
const SPEED_OPTIONS = {
  krea2: KREA_SPEEDS,
  lustify: KREA_SPEEDS,
  [ANIMA]: [["standard", "Version default"]],
  [MOSS]: [["standard", "Standard · 50 steps"], ["dmd8", "DMD · 8 steps"], ["dmad4", "DMAD · 4 steps"]],
  [QWEN]: [["standard", "Standard"], ["viggle", "Viggle · 6 steps"]],
};
const SPEED_STEPS = { standard: 8, turbo4: 4, turbo2: 2 };
const SPEED_DESCRIPTIONS = {
  standard: "Balanced quality with adjustable sampling steps.",
  turbo4: "Faster render using a 4-step turbo LoRA.",
  turbo2: "Fastest option for checking a composition.",
  viggle: "Fast 6-step render. Steps and guidance are fixed.",
  dmd8: "Lodestones 8-step adapter · BF16 model, dedicated sampler, no CFG or negative prompt.",
  dmad4: "Rapid-moss 4-step adapter · BF16 model, dedicated sampler, no CFG or negative prompt.",
};

function renderSpeedOptions() {
  const select = $("#speed");
  const current = select.value;
  const options = SPEED_OPTIONS[state.model] || KREA_SPEEDS;
  select.innerHTML = options.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
  select.value = options.some(([value]) => value === current) ? current : "standard";
}

// Qwen-Image-2.1-Turbo checkpoints sample their own fixed 8-step schedule at CFG 1
// (backend checkpoints.is_qwen_turbo).
const isQwenTurboCheckpoint = (name) => /turbo/i.test(String(name || "").split("/").pop());

const TURBO_HINTS = {
  turbo4: "Start at 1.0. Try 0.75 if the result looks too textured.",
  turbo2: "Keep at 1.0; adjust steps for a different result.",
};
function updateSpeedFields() {
  if ($("#speed").options.length === 0) renderSpeedOptions();
  const speed = $("#speed").value;
  $("#speed-row").hidden = state.model === ANIMA;
  const mossFast = state.model === MOSS && speed !== "standard";
  for (const id of ["#moss-steps-row", "#moss-cfg-row"]) $(id).hidden = mossFast;
  $("#moss-negative-row").hidden = state.model !== MOSS || mossFast;
  for (const id of ["#moss-steps", "#moss-cfg", "#moss-negative-prompt"]) $(id).disabled = mossFast;
  if (state.model === MOSS) {
    const checkpoint = $("#checkpoint");
    if (!mossFast && checkpoint.disabled && checkpoint.value === "moss_image_sft_bf16.safetensors") {
      const saved = (() => { try { return localStorage.getItem("krea.checkpoint.moss"); } catch { return null; } })();
      if (saved && [...checkpoint.options].some((option) => option.value === saved)) checkpoint.value = saved;
    }
    if (mossFast && [...checkpoint.options].some((option) => option.value === "moss_image_sft_bf16.safetensors")) checkpoint.value = "moss_image_sft_bf16.safetensors";
    checkpoint.disabled = mossFast || !checkpoint.options.length;
  }
  $("#speed-description").textContent = isQwen() && speed === "standard"
    ? "Adjust steps and guidance below for more control."
    : SPEED_DESCRIPTIONS[speed] || "";
  document.querySelectorAll(".krea-only").forEach((node) => { node.hidden = isQwen() || state.model === ANIMA || state.model === MOSS; });
  document.querySelectorAll(".turbo-only").forEach((node) => { node.hidden = isQwen() || state.model === ANIMA || state.model === MOSS || speed === "standard"; });
  $("#turbo-hint").textContent = TURBO_HINTS[speed] || "";
  // Krea Steps: empty follows the speed (8 / 4 / 2).
  $("#krea-steps").placeholder = `auto (${SPEED_STEPS[speed] || 8})`;
  // Qwen: Viggle and Turbo checkpoints use their own fixed schedules, so the Steps / CFG boxes don't apply.
  const qwenTurbo = isQwen() && isQwenTurboCheckpoint($("#checkpoint").value);
  $("#speed").disabled = qwenTurbo;
  if (qwenTurbo) $("#speed-description").textContent = "Turbo checkpoint: fixed 8-step schedule at CFG 1. Steps, guidance and Viggle don't apply.";
  const viggle = isQwen() && speed === "viggle";
  for (const id of ["#steps", "#cfg"]) $(id).disabled = viggle || qwenTurbo;
  $("#steps").title = qwenTurbo ? "Qwen-Image Turbo always samples 8 steps (its own schedule, CFG 1)"
    : viggle ? "Viggle turbo always samples 6 steps (its own schedule, no CFG)" : "";
}

function updateReferenceHint() {
  $("#reference-hint").hidden = !(isQwen() && $("#use-references").checked);
}

/* ----------------------------------------------------------- settings -- */
// One settings object for the form: what Generate sends (buildPayload), what a
// preset stores (presetSnapshot) and what "Use these settings" puts back (applySettings).

const QWEN_FIELDS = {
  negative_prompt: ["#negative-prompt", "value"], steps: ["#steps", "number"], cfg: ["#cfg", "number"],
  transparent: ["#transparent", "checked"], reference_resolution: ["#reference-resolution", "number"],
  match_reference_size: ["#match-reference-size", "checked"],
  kv_cache_device: ["#kv-cache-device", "value"], kv_cache_dtype: ["#kv-cache-dtype", "value"],
  text_encoder: ["#qwen-text-encoder", "value"],
};
const ENHANCER_FIELDS = {
  prompt_enhancer: ["#prompt-enhancer", "checked"], enhancer_picks_size: ["#enhancer-picks-size", "checked"],
  enhancer_thinking: ["#enhancer-thinking", "checked"],
};

function collectSettings() {
  const settings = {
    model: state.model,
    checkpoint: state.model === ANIMA ? null : $("#checkpoint").value,
    prompt: $("#prompt").value.trim(),
    width: Number($("#width").value),
    height: Number($("#height").value),
    enhance: $("#enhance").checked,
    use_references: $("#use-references").checked,
    references: collectReferences(),
    count: Number($("#batch-count").value) || 1,
  };
  for (const [key, [selector, kind]] of Object.entries(ENHANCER_FIELDS)) {
    const input = $(selector);
    settings[key] = kind === "checked" ? input.checked : input.value;
  }
  if (isQwen()) {
    for (const [key, [selector, kind]] of Object.entries(QWEN_FIELDS)) {
      const input = $(selector);
      settings[key] = kind === "checked" ? input.checked : kind === "number" ? Number(input.value) : input.value;
    }
  } else if (state.model === ANIMA) {
    settings.anima_version = $("#anima-version").value;
    settings.negative_prompt = $("#anima-negative-prompt").value;
    settings.anima_cfg = Number($("#anima-cfg").value);
    const steps = Number($("#anima-steps").value);
    if (steps) settings.anima_steps = steps;
    settings.use_references = false;
    settings.references = [];
  } else if (state.model === MOSS) {
    settings.moss_steps = Number($("#moss-steps").value) || 50;
    settings.moss_cfg = Number($("#moss-cfg").value) || 3;
    settings.negative_prompt = $("#speed").value === "standard" ? $("#moss-negative-prompt").value : "";
    settings.use_references = false;
    settings.references = [];
    settings.enhance = false;
  } else {
    settings.turbo_strength = Number($("#turbo-strength").value) || 1;
    const steps = Number($("#krea-steps").value);
    if (steps) settings.krea_steps = steps;
  }
  settings.speed = $("#speed").value;
  settings.loras = collectLoras();
  const seedValue = $("#seed").value.trim();
  if (seedValue !== "") settings.seed = Number(seedValue);
  return settings;
}

// Fills the form from a settings object; keys it doesn't have are left alone.
function applySettings(settings, { withModel = false } = {}) {
  const targetModel = settings.model === "lustify" ? "krea2" : settings.model;
  if (withModel && targetModel && targetModel !== state.model) applyModel(targetModel);
  const requestedCheckpoint = settings.checkpoint || (settings.model === "lustify" ? "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors" : null);
  if (requestedCheckpoint && [...$("#checkpoint").options].some((option) => option.value === requestedCheckpoint)) $("#checkpoint").value = requestedCheckpoint;
  if (settings.prompt !== undefined) $("#prompt").value = settings.prompt;
  if (settings.width) $("#width").value = settings.width;
  if (settings.height) $("#height").value = settings.height;
  if (settings.width || settings.height) syncSizePreset();
  if (settings.enhance !== undefined) $("#enhance").checked = Boolean(settings.enhance);
  if (settings.use_references !== undefined) $("#use-references").checked = Boolean(settings.use_references);
  if ("seed" in settings) $("#seed").value = settings.seed ?? "";
  if (settings.count) $("#batch-count").value = String(settings.count);
  if (settings.speed) { renderSpeedOptions(); $("#speed").value = settings.speed; }
  if ("krea_steps" in settings || settings.model) $("#krea-steps").value = settings.krea_steps || "";
  if (settings.turbo_strength) $("#turbo-strength").value = settings.turbo_strength;
  if (settings.anima_version) $("#anima-version").value = settings.anima_version;
  if (settings.anima_steps !== undefined || settings.model === ANIMA) $("#anima-steps").value = settings.anima_steps || "";
  if (settings.anima_cfg !== undefined) $("#anima-cfg").value = settings.anima_cfg;
  if (settings.moss_steps !== undefined) $("#moss-steps").value = settings.moss_steps;
  if (settings.moss_cfg !== undefined) $("#moss-cfg").value = settings.moss_cfg;
  if (settings.model === MOSS) $("#moss-negative-prompt").value = settings.negative_prompt || "";
  if (settings.model === ANIMA || settings.anima_version) $("#anima-negative-prompt").value = settings.negative_prompt || "";
  updateSpeedFields();
  for (const [key, [selector, kind]] of Object.entries({ ...QWEN_FIELDS, ...ENHANCER_FIELDS })) {
    if (settings[key] === undefined) continue;
    if (kind === "checked") $(selector).checked = Boolean(settings[key]);
    else $(selector).value = settings[key];
  }
  if (Array.isArray(settings.loras)) {
    document.querySelectorAll("#lora-list .lora-row").forEach((row) => {
      const slot = settings.loras[Number(row.dataset.loraIndex)];
      if (!slot) return;
      row.querySelector(".lora-enabled").checked = Boolean(slot.enabled);
      row.querySelector(".lora-strength").value = slot.strength;
    });
  }
  // A job without references keeps the current attachments (its Use reference
  // images switch is off). Otherwise slots past the job's own references are
  // cleared, or older attachments would ride along with the restored settings.
  if (Array.isArray(settings.references) && settings.references.length) {
    state.references.forEach((ref, index) => {
      const slot = settings.references[index] || {};
      if (ref.localUrl) URL.revokeObjectURL(ref.localUrl);
      Object.assign(ref, { enabled: Boolean(slot.enabled && slot.image), strength: slot.strength ?? REFERENCE_DEFAULTS[index] ?? 1.0,
        image: slot.image || null, width: slot.width || null, height: slot.height || null, localUrl: null, original: null, crop: null });
    });
    saveReferences();
  }
  syncLoraRows();
  renderReferences();
  updateReferenceHint();
}

function buildPayload() {
  const payload = collectSettings();
  if (!payload.prompt) throw new Error("Enter a prompt.");
  // None installed at all: the server builds with the model's default file and offers to fetch it.
  if (state.model !== ANIMA && !payload.checkpoint && (state.checkpoints[state.model] || []).length) throw new Error("Choose an installed model checkpoint.");
  if (payload.use_references && payload.references.some((ref) => ref.enabled && !ref.image)) {
    throw new Error("An enabled reference slot needs an uploaded image.");
  }
  const maxPixels = MODEL_CONFIG[QWEN]?.max_pixels;
  if (isQwen() && maxPixels && payload.width * payload.height > maxPixels) {
    throw new Error(`Qwen Image 2.1 supports up to ${(maxPixels / 1e6).toFixed(1)} MP (e.g. 2048×2048 or 2752×1536).`);
  }
  return payload;
}

/* ------------------------------------------------------------ presets -- */
// Named settings per model, saved on the server (shared by every browser), like
// H3 Machine's. Picking a preset never changes the model, prompt, seed or
// reference images. The selected preset per model is remembered in this browser.

const PRESET_EXCLUDED_FIELDS = ["model", "prompt", "seed", "references"];
const presetKey = (model) => `krea.preset.${model}`;

function readSelectedPreset(model = state.model) {
  let name = "";
  try { name = localStorage.getItem(presetKey(model)) || ""; } catch { /* storage blocked */ }
  return name && presetsFor(model)[name] ? name : "";
}

function writeSelectedPreset(name, model = state.model) {
  try { if (name) localStorage.setItem(presetKey(model), name); else localStorage.removeItem(presetKey(model)); } catch { /* storage blocked */ }
}

const presetsFor = (model) => (state.presets || {})[model] || {};

async function loadPresets() {
  try { state.presets = await Api.request("/api/presets"); } catch (error) {
    state.presets = {};
    toast(`Couldn't load presets: ${error.message}`, "error");
  }
}

function presetSnapshot() {
  const settings = collectSettings();
  PRESET_EXCLUDED_FIELDS.forEach((field) => delete settings[field]);
  return settings;
}

function renderPresetBar() {
  const names = Object.keys(presetsFor(state.model)).sort((a, b) => a.localeCompare(b));
  const select = $("#preset");
  select.innerHTML = `<option value="">No preset</option>`
    + names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  select.value = readSelectedPreset();
  for (const id of ["#preset-save", "#preset-delete", "#preset-rename"]) $(id).disabled = !select.value;
  updatePresetStatus();
}

// "modified" once the form drifts from the selected preset (only the preset's own keys count).
function updatePresetStatus() {
  const name = readSelectedPreset();
  const preset = name && presetsFor(state.model)[name];
  const now = preset ? presetSnapshot() : null;
  // LoRA slots added after a preset was saved don't count as a change while they're off.
  const same = (key) => (key === "loras" && Array.isArray(preset.loras) && Array.isArray(now.loras)
    ? JSON.stringify(preset.loras) === JSON.stringify(now.loras.slice(0, preset.loras.length))
      && now.loras.slice(preset.loras.length).every((slot) => !slot.enabled)
    : JSON.stringify(preset[key]) === JSON.stringify(now[key]));
  const modified = preset && Object.keys(preset).some((key) => !PRESET_EXCLUDED_FIELDS.includes(key) && !same(key));
  $("#preset-status").textContent = modified ? "modified - save to update" : "";
}

function applyPreset(name) {
  const preset = presetsFor(state.model)[name];
  if (preset) applySettings(Object.fromEntries(Object.entries(preset).filter(([key]) => !PRESET_EXCLUDED_FIELDS.includes(key))));
}

function selectPreset(name) {
  writeSelectedPreset(name);
  if (name) { applyPreset(name); toast(`Preset "${name}" applied.`); }
  renderPresetBar();
}

async function storePreset(name, settings) {
  state.presets = await Api.request("/api/presets", { method: "PUT", body: { model: state.model, name, settings } });
}

function openPresetDialog(renaming = "") {
  $("#preset-dialog-title").textContent = `Save ${MODEL_CONFIG[state.model]?.label || "these"} settings as`;
  $("#preset-new-name").value = "";
  renderPresetList(renaming);
  openDialog("#preset-dialog");
  if (renaming) $("#preset-list .preset-rename-input")?.focus();
  else $("#preset-new-name").focus();
}

function renderPresetList(renaming = "") {
  const names = Object.keys(presetsFor(state.model)).sort((a, b) => a.localeCompare(b));
  const selected = readSelectedPreset();
  $("#preset-list-hint").hidden = !names.length;
  $("#preset-list").innerHTML = names.map((name) => name === renaming
    ? `<li class="preset-item is-renaming" data-preset="${escapeHtml(name)}">
        <input type="text" class="preset-rename-input" maxlength="60" value="${escapeHtml(name)}" aria-label="New name for ${escapeHtml(name)}">
        <button type="button" class="secondary-button" data-preset-action="rename-save">Rename</button>
        <button type="button" class="secondary-button" data-preset-action="rename-cancel">Cancel</button>
      </li>`
    : `<li class="preset-item" data-preset="${escapeHtml(name)}">
        <span class="preset-item-name">${escapeHtml(name)}${name === selected ? ` <span class="field-hint">(selected)</span>` : ""}</span>
        <button type="button" class="secondary-button" data-preset-action="overwrite">Save here</button>
        <button type="button" class="icon-button" data-preset-action="rename" title="Rename" aria-label="Rename ${escapeHtml(name)}">${uiIcon("pencil")}</button>
      </li>`).join("");
}

async function savePresetAs(name) {
  try { await storePreset(name, presetSnapshot()); } catch (error) { toast(error.message, "error"); return; }
  writeSelectedPreset(name);
  closeDialog($("#preset-dialog"));
  renderPresetBar();
  toast(`Preset "${name}" saved.`);
}

async function renamePreset(name, newName) {
  newName = newName.trim().slice(0, 60);
  if (!newName || newName === name) { renderPresetList(); return; }
  try {
    state.presets = await Api.request("/api/presets/rename", { method: "POST", body: { model: state.model, name, new_name: newName } });
  } catch (error) { toast(error.message, "error"); return; }
  try { if (localStorage.getItem(presetKey(state.model)) === name) writeSelectedPreset(newName); } catch { /* storage blocked */ }
  renderPresetList();
  renderPresetBar();
  toast(`Renamed "${name}" to "${newName}".`);
}

function bindPresets() {
  $("#preset").addEventListener("change", (event) => selectPreset(event.target.value));
  $("#preset-new").addEventListener("click", () => openPresetDialog());
  $("#preset-rename").addEventListener("click", () => openPresetDialog(readSelectedPreset()));
  $("#preset-save").addEventListener("click", async () => {
    const name = readSelectedPreset();
    if (!name) return;
    try { await storePreset(name, presetSnapshot()); } catch (error) { return toast(error.message, "error"); }
    renderPresetBar();
    toast(`Preset "${name}" updated.`);
  });
  $("#preset-delete").addEventListener("click", async () => {
    const name = readSelectedPreset();
    if (!name || !confirm(`Delete the preset "${name}"?`)) return;
    try {
      state.presets = await Api.request(`/api/presets?${new URLSearchParams({ model: state.model, name })}`, { method: "DELETE" });
    } catch (error) { return toast(error.message, "error"); }
    writeSelectedPreset("");
    renderPresetBar();
    toast(`Preset "${name}" deleted.`);
  });
  $("#preset-new-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = $("#preset-new-name").value.trim().slice(0, 60);
    if (!name) return;
    if (presetsFor(state.model)[name] && !confirm(`A preset named "${name}" already exists. Replace it?`)) return;
    savePresetAs(name);
  });
  $("#preset-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-preset-action]");
    if (!button) return;
    const item = button.closest("[data-preset]");
    const name = item.dataset.preset;
    const action = button.dataset.presetAction;
    if (action === "overwrite") { if (confirm(`Save the current settings over "${name}"?`)) savePresetAs(name); }
    else if (action === "rename") { renderPresetList(name); $("#preset-list .preset-rename-input")?.focus(); }
    else if (action === "rename-save") renamePreset(name, $(".preset-rename-input", item).value);
    else if (action === "rename-cancel") renderPresetList();
  });
  $("#preset-list").addEventListener("keydown", (event) => {
    if (!event.target.classList.contains("preset-rename-input")) return;
    if (event.key === "Enter") { event.preventDefault(); renamePreset(event.target.closest("[data-preset]").dataset.preset, event.target.value); }
    if (event.key === "Escape") { event.stopPropagation(); renderPresetList(); }
  });
  // Any form change can make the selected preset "modified".
  $("#generate-form").addEventListener("input", updatePresetStatus);
  $("#generate-form").addEventListener("change", updatePresetStatus);
  $("#generate-form").addEventListener("input", scheduleEstimate);
  $("#generate-form").addEventListener("change", syncLoraRows);
  $("#generate-form").addEventListener("change", scheduleEstimate);
  $("#use-references").addEventListener("change", renderReferenceTotals);
}

/* -------------------------------------------------------------- form -- */

function bindForm() {
  const form = $("#generate-form");
  const seedInput = $("#seed");
  const randomSeedButton = $("#random-seed");

  randomSeedButton.addEventListener("click", () => {
    seedInput.value = String(Math.floor(Math.random() * 2 ** 32));
  });

  $("#model").addEventListener("change", (event) => {
    applyModel(event.target.value);
    const name = readSelectedPreset();
    if (name) applyPreset(name); // each model keeps its own selected preset
    renderPresetBar();
  });
  $("#checkpoint-refresh").addEventListener("click", refreshCheckpoints);
  $("#checkpoint").addEventListener("change", (event) => { updateSpeedFields(); try { localStorage.setItem(`krea.checkpoint.${state.model}`, event.target.value); } catch { /* storage unavailable */ } });
  $("#use-references").addEventListener("change", updateReferenceHint);
  $("#speed").addEventListener("change", updateSpeedFields);
  $("#use-references").addEventListener("change", updateSizeNote);
  $("#match-reference-size").addEventListener("change", updateSizeNote);

  $("#size-preset").addEventListener("change", (event) => {
    const [width, height] = event.target.value.split("x");
    if (!width) return;
    $("#width").value = width;
    $("#height").value = height;
    $("#width").dispatchEvent(new Event("input", { bubbles: true })); // estimate + preset status
  });
  for (const id of ["#width", "#height"]) {
    $(id).addEventListener("input", syncSizePreset);
  }

  // Official Qwen-Image 2.1 background-removal template: edit mode, the
  // photo as <image1>, a fixed instruction, and an RGBA PNG out.
  $("#preset-remove-bg").addEventListener("click", () => {
    $("#prompt").value = "Remove the background, and output a PNG image";
    $("#use-references").checked = true;
    $("#match-reference-size").checked = true;
    $("#prompt-enhancer").checked = false;
    $("#transparent").checked = false;
    state.references[0].enabled = true;
    saveReferences();
    renderReferences();
    updateReferenceHint();
    toast(state.references[0].image ? "Background removal ready." : "Upload the image as Reference 1.");
  });
  $("#preset-transparent").addEventListener("click", () => {
    $("#transparent").checked = true;
    $("#use-references").checked = false;
    updateReferenceHint();
    $("#prompt").focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let payload;
    try { payload = buildPayload(); } catch (error) { toast(error.message, "error"); return; }
    const button = $("#generate-button");
    button.disabled = true;
    try {
      await Api.request("/api/generate", { method: "POST", body: payload });
      toast("Generation queued.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
}

/* -------------------------------------------------------------- jobs -- */
// Cards match H3 Machine's: meta chips, a progress bar with a time-left
// estimate, icon buttons, All / Running / Complete tabs, prompt search and
// "Show more" paging. While an image renders, a placeholder of the output's
// shape holds its place, so the list doesn't jump when the image arrives.

const JOBS_PAGE = 12;
const jobView = { filter: "all", query: "", limit: JOBS_PAGE, renderedKey: "" };

const isActiveJob = (job) => ACTIVE_STATES.has(job.state);
const isTerminalJob = (job) => ["complete", "failed", "cancelled"].includes(job.state);

function formatSeconds(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${total % 60}s` : `${total}s`;
}

// Same basis as the estimate above the prompt (estimateFor / speedKey).
const jobEstimate = (job) => estimateFor(job.model, job.request || {});

function jobEta(job) {
  if (!isActiveJob(job) || job.state === "cancelling") return "";
  const estimate = jobEstimate(job);
  if (!estimate) return "";
  if (!job.started_at || job.state === "waiting_for_gpu") return `est. ${formatSeconds(estimate.seconds)}`;
  // (estimateFor already multiplies by the job's image count)
  const left = estimate.seconds - (Date.now() - new Date(job.started_at).getTime()) / 1000;
  return left > 0 ? `~${formatSeconds(left)} left` : "running over estimate";
}

function jobElapsed(job) {
  return elapsed(job.started_at || job.created_at, isTerminalJob(job) ? job.completed_at || undefined : undefined);
}

function jobProgress(job) {
  return Math.max(0, Math.min(99, Number(job.progress) || 0));
}

function jobMatches(job) {
  if (jobView.filter === "running" && !isActiveJob(job)) return false;
  if (jobView.filter === "complete" && job.state !== "complete") return false;
  const query = jobView.query.trim().toLowerCase();
  return !query || `${job.prompt}\n${job.enhanced_prompt || ""}`.toLowerCase().includes(query);
}

// A multi-image generation: finished images in a grid, placeholders for the rest
// while it renders. Each opens the viewer, which pages through the set.
function jobImageGrid(job) {
  const count = job.batch_count || 1;
  const byIndex = new Map((job.images || []).map((image) => [image.index, image]));
  const active = isActiveJob(job);
  const columns = count <= 2 ? 2 : count <= 4 ? 2 : count <= 9 ? 3 : 4;
  const cells = Array.from({ length: count }, (_, index) => {
    const image = byIndex.get(index);
    if (image) {
      return `<button type="button" class="job-grid-cell" data-job-action="view" data-job="${job.id}" data-index="${index}" title="Image ${index + 1} · seed ${image.seed}"><img src="${image.thumb_url || image.url}" alt="" loading="lazy" decoding="async"${job.model === QWEN ? ' class="checkerboard"' : ""}></button>`;
    }
    return active ? `<span class="job-grid-cell is-pending"></span>` : "";
  }).join("");
  const rows = Math.ceil(count / columns);
  return `<div class="job-grid" style="--columns:${columns}; --rows:${rows}">${cells}</div>`;
}

// Every card gets the same fixed-shape media frame (see .job-media), whatever
// the image's resolution, so cards line up at one height. Images are
// letterboxed inside it; the viewer still shows them at their real shape.
function jobMedia(job, { active, multi, qwenImage }) {
  let inner = "";
  if (active && !multi) inner = `<div class="job-placeholder"><span class="job-placeholder-label">${STATE_LABELS[job.state] || job.state}</span></div>`;
  else if (multi && (active || (job.images || []).length)) inner = jobImageGrid(job);
  else if (!multi && job.state === "complete" && job.output_url) {
    inner = `<button type="button" class="job-image-button" data-job-action="view" data-job="${job.id}" aria-label="View full size"><img class="job-image${qwenImage ? " checkerboard" : ""}" src="${job.thumb_url || job.output_url}" alt="Generated image" loading="lazy" decoding="async"></button>`;
  } else inner = `<span class="job-media-empty">${STATE_LABELS[job.state] || job.state}</span>`;
  return `<div class="job-media">${inner}</div>`;
}

function jobCard(job) {
  const active = isActiveJob(job);
  const qwenImage = job.model === QWEN;
  const multi = (job.batch_count || 1) > 1;
  const button = (action, icon, title, extra = "") =>
    `<button type="button" class="secondary-button job-icon-button${extra}" data-job-action="${action}" data-job="${job.id}" title="${title}" aria-label="${title}">${uiIcon(icon)}</button>`;
  return `
    <div class="job-card${active ? " is-active" : ""}" data-job-id="${job.id}">
      <div class="job-head">
        <span class="job-prompt" title="${escapeHtml(job.prompt)}">${escapeHtml(job.prompt)}</span>
        <span class="job-state state-${job.state}">${STATE_LABELS[job.state] || job.state}</span>
      </div>
      <div class="job-meta">
        <span class="job-model">${escapeHtml(job.model_label || "Krea 2")}</span>
        <span>${job.width}&times;${job.height}</span>
        ${job.enhance ? `<span>4&times; upscale</span>` : ""}
        ${job.request?.prompt_enhancer ? `<span>PE</span>` : ""}
        ${multi ? `<span class="job-count">${(job.images || []).length}/${job.batch_count} images</span>` : ""}
        ${job.model === QWEN && isQwenTurboCheckpoint(job.request?.checkpoint) ? `<span>Turbo 8 steps</span>`
          : job.request?.speed === "viggle" ? `<span>Viggle 6 steps</span>`
          : job.model === MOSS && job.request?.speed === "dmd8" ? `<span>DMD 8 steps</span>`
          : job.model === MOSS && job.request?.speed === "dmad4" ? `<span>DMAD 4 steps</span>`
          : job.request?.speed && job.request.speed !== "standard" && job.model !== QWEN ? `<span>Turbo ${job.request.krea_steps || SPEED_STEPS[job.request.speed]} steps</span>` : ""}
        <span class="job-elapsed">${jobElapsed(job)}</span>
        ${active ? `<span class="job-eta">${jobEta(job)}</span>` : ""}
      </div>
      ${active ? `<div class="job-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${jobProgress(job)}"><div class="job-progress-bar" style="width:${jobProgress(job)}%"></div></div>` : ""}
      ${job.enhanced_prompt ? `<details class="job-enhanced"><summary>Enhanced prompt</summary><p>${escapeHtml(job.enhanced_prompt)}</p></details>` : `<div class="job-enhanced is-empty" aria-hidden="true"></div>`}
      ${job.error ? `<div class="job-error" title="${escapeHtml(job.error)}">${escapeHtml(job.error)}</div>` : ""}
      ${jobMedia(job, { active, multi, qwenImage })}
      <div class="job-actions">
        ${button("info", "info", "Generation details")}
        ${active ? button("cancel", "cancel", job.state === "cancelling" ? "Cancelling…" : "Cancel", " danger") : ""}
        ${job.zip_url && !active ? `<a class="secondary-button job-icon-button" href="${job.zip_url}" download title="Download all ${(job.images || []).length} images (zip)" aria-label="Download all images">${uiIcon("download")}</a>`
          : !multi && job.state === "complete" && job.download_url ? `<a class="secondary-button job-icon-button" href="${job.download_url}" download title="Download" aria-label="Download">${uiIcon("download")}</a>` : ""}
        ${job.request?.prompt ? button("reuse", "reuse", "Use these settings (prompt, model and settings go back into the form)") : ""}
        ${isTerminalJob(job) && job.request?.prompt ? button("retry", "retry", "Retry (load the exact settings, references and seed into the form)") : ""}
        ${isTerminalJob(job) ? button("remove", "trash", "Remove", " danger") : ""}
      </div>
    </div>`;
}

// Everything a card's markup depends on except progress/time, which updateJobMetrics patches in place.
const jobRenderKey = (job) => [job.id, job.state, job.prompt, job.enhanced_prompt, job.error, job.output_url, job.thumb_url, job.width, job.height, (job.images || []).length].join("::");

function renderJobs() {
  const counts = {
    all: state.jobs.length,
    running: state.jobs.filter(isActiveJob).length,
    complete: state.jobs.filter((job) => job.state === "complete").length,
  };
  document.querySelectorAll("[data-job-count]").forEach((node) => { node.textContent = counts[node.dataset.jobCount] || ""; });
  const matching = state.jobs.filter(jobMatches);
  const shown = matching.slice(0, jobView.limit);
  const key = `${jobView.filter}|${jobView.query}|${jobView.limit}|${shown.map(jobRenderKey).join("||")}`;
  if (key !== jobView.renderedKey) {
    jobView.renderedKey = key;
    const list = $("#jobs-list");
    if (!shown.length) {
      list.innerHTML = `<div class="jobs-empty">${state.jobs.length ? "No generations match." : "No generations yet. Describe an image and hit Generate."}</div>`;
    } else {
      // Keyed update: only cards whose markup changed are rebuilt, so the other images
      // don't reload (no blinking) and the list's scroll position stays where it was.
      list.querySelector(":scope > .jobs-empty")?.remove();
      const existing = new Map([...list.querySelectorAll(":scope > [data-job-id]")].map((node) => [node.dataset.jobId, node]));
      const template = document.createElement("template");
      let previous = null;
      for (const job of shown) {
        const renderKey = jobRenderKey(job);
        let node = existing.get(job.id);
        existing.delete(job.id);
        if (!node || node.dataset.renderKey !== renderKey) {
          template.innerHTML = jobCard(job).trim();
          const fresh = template.content.firstElementChild;
          fresh.dataset.renderKey = renderKey;
          if (node) node.replaceWith(fresh);
          node = fresh;
        }
        const slot = previous ? previous.nextElementSibling : list.firstElementChild;
        if (slot !== node) list.insertBefore(node, slot);
        previous = node;
      }
      existing.forEach((node) => node.remove());
    }
    const pager = $("#jobs-pager");
    pager.hidden = matching.length <= shown.length;
    pager.innerHTML = `<span>Showing ${shown.length} of ${matching.length}</span><button type="button" class="secondary-button" id="jobs-more">Show ${Math.min(JOBS_PAGE, matching.length - shown.length)} more</button>`;
  }
  updateJobMetrics();
  scheduleEstimate(); // a newly finished job can change it
}

function updateJobMetrics() {
  document.querySelectorAll("#jobs-list [data-job-id]").forEach((card) => {
    const job = state.jobs.find((item) => item.id === card.dataset.jobId);
    if (!job) return;
    const elapsedNode = $(".job-elapsed", card);
    if (elapsedNode) elapsedNode.textContent = jobElapsed(job);
    const eta = $(".job-eta", card);
    if (eta) eta.textContent = jobEta(job);
    const bar = $(".job-progress-bar", card);
    if (bar) {
      bar.style.width = `${jobProgress(job)}%`;
      bar.parentElement.setAttribute("aria-valuenow", String(jobProgress(job)));
    }
    const label = $(".job-placeholder-label", card);
    if (label) label.textContent = `${STATE_LABELS[job.state] || job.state}${job.state === "rendering" && job.progress ? ` · ${jobProgress(job)}%` : ""}`;
    const pending = $(".job-grid-cell.is-pending", card);
    card.querySelectorAll(".job-grid-cell.is-next").forEach((cell) => cell.classList.remove("is-next"));
    if (pending && job.state === "rendering") pending.classList.add("is-next"); // the image rendering now
  });
}

// Full-size view of one image of a generation; ‹ › (or arrow keys) page through a multi-image set.
let viewerJob = null;
function openJobImage(job, index) {
  const images = job.images?.length ? job.images : [{ index: 0, seed: job.seed, url: job.output_url, download_url: job.download_url }];
  const position = Math.max(0, images.findIndex((image) => image.index === index));
  const image = images[position];
  viewerJob = images.length > 1 ? { job, position } : null;
  const nav = images.length > 1
    ? `<button type="button" class="secondary-button" data-viewer-step="-1" aria-label="Previous image">&#8249;</button><span class="viewer-count">${position + 1} / ${images.length}</span><button type="button" class="secondary-button" data-viewer-step="1" aria-label="Next image">&#8250;</button>`
    : "";
  const actions = `${nav}${image.download_url ? `<a class="secondary-button" href="${image.download_url}" download>${uiIcon("download")} Download</a>` : ""}`;
  openViewer(image.url, `${escapeHtml(job.model_label || "")} · ${job.width}×${job.height} · seed ${image.seed}`, actions, job.model === QWEN);
}

function stepViewer(delta) {
  if (!viewerJob || $("#viewer").hidden) return;
  const images = viewerJob.job.images;
  const next = images[(viewerJob.position + delta + images.length) % images.length];
  openJobImage(state.jobs.find((item) => item.id === viewerJob.job.id) || viewerJob.job, next.index);
}

let jobActionIndex = null;
async function jobAction(action, jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  try {
    if (action === "cancel") await Api.request(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    else if (action === "remove") await Api.request(`/api/jobs/${jobId}`, { method: "DELETE" });
    else if (action === "retry") {
      // Like H3 Machine's Retry: everything (including the seed this job actually used) goes back into the form for review.
      applySettings({ ...job.request, seed: job.seed ?? job.request.seed ?? null }, { withModel: true });
      renderPresetBar();
      $("#generate-form").scrollIntoView({ behavior: "smooth", block: "start" });
      toast("Loaded the exact settings, references and seed. Review, then Generate.");
    }
    else if (action === "reuse") {
      applySettings({ ...job.request, seed: job.request.seed ?? null }, { withModel: true });
      renderPresetBar();
      $("#prompt").focus();
      toast("Settings loaded into the form.");
    } else if (action === "info") openJobInfo(job);
    else if (action === "view") openJobImage(job, Number(jobActionIndex ?? 0));
  } catch (error) {
    toast(error.message, "error");
  }
}

function openJobInfo(job) {
  const request = job.request || {};
  const row = (label, value) => (value === undefined || value === null || value === "" ? "" : `<dt>${label}</dt><dd>${value}</dd>`);
  const jobSet = loraSetFor(job.model);
  const loras = (request.loras || []).map((slot, index) => slot.enabled ? `${escapeHtml(LORA_SETS.info[jobSet]?.[index]?.label || (LORA_SETS.slots[jobSet]?.[index] || `LoRA ${index + 1}`).replace(/\.safetensors$/, ""))} <span class="field-hint">${slot.strength}</span>` : "").filter(Boolean);
  const refs = request.use_references ? (request.references || []).map((ref, index) => ref.enabled && ref.image
    ? `<figure class="job-info-ref"><img src="${referencePreviewUrl(ref.image)}" alt="" loading="lazy"><figcaption>Reference ${index + 1}${job.model === QWEN ? "" : ` · ${ref.strength}`}</figcaption></figure>` : "").filter(Boolean) : [];
  const duration = job.started_at && job.completed_at ? formatSeconds((new Date(job.completed_at) - new Date(job.started_at)) / 1000) : "";
  $("#job-info-body").innerHTML = `
    <section class="job-info-section">
      <div class="job-info-heading"><h3>Prompt</h3><button type="button" class="secondary-button job-icon-button" data-copy-prompt title="Copy prompt" aria-label="Copy prompt">${uiIcon("copy")}</button></div>
      <pre class="job-info-prompt">${escapeHtml(job.prompt)}</pre>
      ${job.enhanced_prompt ? `<h3>Enhanced prompt</h3><pre class="job-info-prompt">${escapeHtml(job.enhanced_prompt)}</pre>` : ""}
      ${request.negative_prompt ? `<h3>Negative prompt</h3><pre class="job-info-prompt">${escapeHtml(request.negative_prompt)}</pre>` : ""}
    </section>
    <section class="job-info-section">
      <h3>Settings</h3>
      <dl>
        ${row("Model", escapeHtml(job.model_label || job.model))}
        ${row("Checkpoint", escapeHtml(request.checkpoint || ""))}
        ${row("Size", `${job.width}×${job.height}`)}
        ${row("Seed", escapeHtml(String(job.seed)) + (request.seed == null ? ' <span class="field-hint">(random)</span>' : ""))}
        ${job.model === QWEN ? row("Steps / CFG", `${request.steps ?? ""} / ${request.cfg ?? ""}`) : ""}
        ${job.model === MOSS ? row("Steps / CFG", request.speed === "dmd8" ? "8 / none" : request.speed === "dmad4" ? "4 / none" : `${request.moss_steps ?? 50} / ${request.moss_cfg ?? 3}`) : ""}
        ${(job.batch_count || 1) > 1 ? row("Images", `${(job.images || []).length} of ${job.batch_count} · seeds ${escapeHtml(String(job.seed))} + 0…${job.batch_count - 1}`) : ""}
        ${job.model === QWEN ? row("Text encoder", request.text_encoder === "abliterated" ? "Qwen3-VL 8B Abliterated (BF16)" : "Qwen3-VL 8B (int8)") : ""}
        ${row("Speed", job.model === QWEN && isQwenTurboCheckpoint(request.checkpoint) ? "Qwen-Image Turbo, 8 steps, CFG 1"
          : request.speed === "viggle" ? "Viggle turbo LoRA, 6 steps"
          : job.model === MOSS ? (request.speed === "dmd8" ? "DMD, 8 steps" : request.speed === "dmad4" ? "DMAD, 4 steps" : `Standard, ${request.moss_steps ?? 50} steps`)
          : job.model === QWEN ? `Standard, ${request.steps ?? 25} steps`
          : `${!request.speed || request.speed === "standard" ? "Standard" : `Turbo LoRA (strength ${request.turbo_strength ?? 1})`}, ${request.krea_steps || SPEED_STEPS[request.speed || "standard"]} steps`)}
        ${row("4× upscale", job.enhance ? "on" : "off")}
        ${job.model === QWEN ? row("Transparent", request.transparent ? "on" : "off") : ""}
        ${row("Prompt enhancer", request.prompt_enhancer ? `on${request.enhancer_thinking ? " (thinking)" : ""}${request.enhancer_picks_size ? " · chooses ratio" : ""}` : "off")}
        ${row("LoRAs", loras.length ? loras.join("<br>") : "none")}
        ${row("References", refs.length ? `${refs.length}` : "none")}
        ${row("Status", `${STATE_LABELS[job.state] || job.state}${duration ? ` · rendered in ${duration}` : ""}`)}
        ${row("Created", new Date(job.created_at).toLocaleString())}
      </dl>
      ${refs.length ? `<div class="job-info-refs">${refs.join("")}</div>` : ""}
      ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
    </section>`;
  $("[data-copy-prompt]", $("#job-info-body")).addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(job.prompt); toast("Prompt copied."); } catch { toast("Couldn't copy - select the text instead.", "error"); }
  });
  openDialog("#job-info");
}

function bindJobs() {
  $("#jobs-list").addEventListener("click", (event) => {
    const target = event.target.closest("[data-job-action]");
    if (!target) return;
    jobActionIndex = target.dataset.index ?? null;
    jobAction(target.dataset.jobAction, target.dataset.job);
  });
  $("#viewer").addEventListener("click", (event) => {
    const step = event.target.closest("[data-viewer-step]");
    if (step) stepViewer(Number(step.dataset.viewerStep));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") stepViewer(-1);
    if (event.key === "ArrowRight") stepViewer(1);
  });
  document.querySelectorAll("[data-job-filter]").forEach((tab) => tab.addEventListener("click", () => {
    jobView.filter = tab.dataset.jobFilter;
    jobView.limit = JOBS_PAGE;
    document.querySelectorAll("[data-job-filter]").forEach((other) => {
      other.classList.toggle("is-active", other === tab);
      other.setAttribute("aria-selected", String(other === tab));
    });
    renderJobs();
  }));
  $("#job-search").addEventListener("input", (event) => {
    jobView.query = event.target.value;
    jobView.limit = JOBS_PAGE;
    renderJobs();
  });
  $("#jobs-pager").addEventListener("click", (event) => {
    if (!event.target.closest("#jobs-more")) return;
    jobView.limit += JOBS_PAGE;
    renderJobs();
  });
}

/* ------------------------------------------------------------- hotbar -- */
// Under the prompt, like H3 Machine's hotbar, but only what is relevant right
// now: the trigger words of the enabled LoRAs (Krea 2 / Lustify) and the
// <imageN> tags of the attached, enabled references (Qwen). Hidden when empty.
// A chip inserts its text at the cursor; hovering shows what it is.

const HOTBAR_HINT = "Hover or tap a chip to see what it is; click to insert it at the cursor.";
let hotbarItems = [];

function hotbarGroups() {
  const triggers = LORA_SLOTS.flatMap((_, index) => {
    const row = $(`#lora-list [data-lora-index="${index}"]`);
    if (!row?.querySelector(".lora-enabled").checked) return [];
    // "word (note)" -> only the word gets typed, as in H3 Machine.
    return (LORA_INFO[index]?.triggers || []).map((trigger) => ({
      label: trigger.replace(/\s*\(.*\)$/, ""), text: trigger.replace(/\s*\(.*\)$/, ""), kind: "trigger",
      title: `${loraLabel(index)} - ${trigger}`,
    }));
  });
  const references = !isQwen() || !$("#use-references").checked ? [] : state.references.slice(0, referenceSlotCount())
    .map((ref, index) => (ref.image && ref.enabled ? {
      label: `<image${index + 1}>`, text: `<image${index + 1}>`, kind: "ref",
      title: `Reference ${index + 1}${ref.width ? ` (${ref.width}×${ref.height})` : ""}`,
    } : null)).filter(Boolean);
  return [{ name: "LoRA triggers", items: triggers }, { name: "References", items: references }].filter((group) => group.items.length);
}

function renderHotbar() {
  const groups = hotbarGroups();
  hotbarItems = groups.flatMap((group) => group.items);
  const bar = $("#prompt-snippets");
  const hint = $("#prompt-snippet-hint");
  bar.hidden = hint.hidden = !hotbarItems.length;
  let index = 0;
  bar.innerHTML = groups.map((group) => `
    <div class="prompt-snippet-block"><div class="prompt-snippet-row">
      <span class="prompt-snippet-group">${escapeHtml(group.name)}</span>
      ${group.items.map((item) => `<button type="button" class="prompt-snippet hl-${item.kind}" data-snippet="${index++}" title="${escapeHtml(item.title)}">${escapeHtml(item.label)}</button>`).join("")}
    </div></div>`).join("");
  hint.textContent = HOTBAR_HINT;
}

function bindHotbar() {
  const bar = $("#prompt-snippets");
  // Keep the prompt's cursor: don't let the chip take focus.
  bar.addEventListener("pointerdown", (event) => { if (event.target.closest("[data-snippet]")) event.preventDefault(); });
  bar.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-snippet]");
    const item = chip && hotbarItems[Number(chip.dataset.snippet)];
    if (item) insertAtCursor($("#prompt"), item.text);
  });
  const hint = $("#prompt-snippet-hint");
  bar.addEventListener("pointerover", (event) => {
    const chip = event.target.closest("[data-snippet]");
    const item = chip && hotbarItems[Number(chip.dataset.snippet)];
    if (!item) return;
    hint.innerHTML = `<span class="prompt-snippet-hint-label hl-${item.kind}">${escapeHtml(item.label)}</span> ${escapeHtml(item.title)}`
      + `<span class="prompt-snippet-hint-insert">inserts: <code>${escapeHtml(item.text)}</code></span>`;
  });
  bar.addEventListener("pointerleave", () => { hint.textContent = HOTBAR_HINT; });
  // LoRA switches, the references switch, and gallery toggles all end in a form change.
  $("#generate-form").addEventListener("change", renderHotbar);
}

/* ------------------------------------------------------- lora gallery -- */
// H3's gallery flow, backed by Krea's model-specific LoRA slots and catalog.
const gallery = { index: {}, catalog: null, preview: new Map(), reveal: new Set(), detail: null, editing: false, draft: null, inspection: null, aliases: [], linkTaskId: null, gridScroll: 0 };
const galleryVideoObserver = new IntersectionObserver((entries) => entries.forEach(({target, isIntersecting}) => {
  if (isIntersecting) target.play().catch(() => {});
  else target.pause();
}), {threshold: 0.35});
function observeGalleryVideos() {
  galleryVideoObserver.disconnect();
  window.BunnyGalleryMedia?.observe($("#lora-gallery"));
  document.querySelectorAll("#lora-gallery video[data-gallery-video]").forEach((video) => galleryVideoObserver.observe(video));
}
const NSFW_BLUR_LEVEL = 4;
const GALLERY_NSFW_KEY = "krea.lora-gallery-nsfw";
const galleryAsset = (path) => `${API_BASE}/${path.startsWith("api/") ? path : `static/${path}`}`;
const galleryImageAttrs = (path, eager = false) => {
  const source = escapeHtml(galleryAsset(path));
  return window.BunnyGalleryMedia?.imageAttrs(source, eager) || `src="${source}" loading="lazy" decoding="async"`;
};
const galleryIcon = (name) => window.BunnyGalleryMedia?.icon(name) || uiIcon(name);
const loraRow = (index) => $(`#lora-list [data-lora-index="${index}"]`);
const galleryModel = () => loraSet;
const galleryInfo = (index) => gallery.index[LORA_SLOTS[index]] || {};
const galleryMeta = (index) => LORA_INFO[index] || {};
const galleryLabel = (index) => galleryMeta(index).label || LORA_SLOTS[index].split("/").pop().replace(/\.safetensors$/i, "");

function galleryShowsNsfw() {
  try { return localStorage.getItem(GALLERY_NSFW_KEY) === "on"; } catch { return false; }
}

function galleryIndexes() {
  const query = $("#lora-gallery-search").value.trim().toLowerCase();
  const group = $("#lora-gallery-group").value;
  const enabledOnly = $("#lora-gallery-enabled-only").checked;
  const order = gallery.catalog?.order || LORA_SLOTS;
  return order.map((name) => LORA_SLOTS.indexOf(name)).filter((index) => {
    if (index < 0 || galleryMeta(index).deleted) return false;
    if (group && (gallery.catalog?.assignments[LORA_SLOTS[index]] || galleryMeta(index).group || "Other") !== group) return false;
    if (enabledOnly && !loraRow(index)?.querySelector(".lora-enabled")?.checked) return false;
    if (!query) return true;
    const info = galleryInfo(index), meta = galleryMeta(index);
    return [galleryLabel(index), LORA_SLOTS[index], meta.group, info.name, info.creator, info.description,
      ...(meta.triggers || []), ...(info.trained_words || [])].some((value) => String(value || "").toLowerCase().includes(query));
  });
}

function galleryMedia(index, detail) {
  const filename = LORA_SLOTS[index];
  const previews = galleryInfo(index).previews || [];
  if (!previews.length) return `<div class="lora-card-media"><span class="lora-card-empty">No previews yet</span></div>`;
  const position = Math.min(gallery.preview.get(filename) || 0, previews.length - 1);
  const preview = previews[position];
  const blurred = Number(preview.nsfw || 1) >= NSFW_BLUR_LEVEL && !galleryShowsNsfw() && !gallery.reveal.has(`${filename}:${position}`);
  return `<div class="lora-card-media${blurred ? " is-blurred" : ""}">
    ${preview.video && !blurred ? `<video src="${galleryAsset(preview.video)}" poster="${galleryAsset(preview.poster)}" muted loop playsinline controls preload="none" data-gallery-video></video>`
      : `<img ${galleryImageAttrs(preview.poster, detail)} alt="">`}
    ${blurred ? `<button type="button" class="lora-card-nsfw" data-gallery-reveal>NSFW preview · tap to show</button>` : ""}
    ${previews.length > 1 ? `<button type="button" class="lora-media-nav prev" data-gallery-step="-1" aria-label="Previous preview">&#8249;</button>
      <button type="button" class="lora-media-nav next" data-gallery-step="1" aria-label="Next preview">&#8250;</button>
      <span class="lora-media-count">${position + 1} / ${previews.length}${preview.video ? " · video" : ""}</span>` : ""}
    </div>${previews.length > 1 ? `<div class="lora-thumbs">${previews.map((item, n) =>
      `<button type="button" class="lora-thumb${n === position ? " is-active" : ""}${Number(item.nsfw || 1) >= NSFW_BLUR_LEVEL && !galleryShowsNsfw() && !gallery.reveal.has(`${filename}:${n}`) ? " is-blurred" : ""}" data-gallery-thumb="${n}" aria-label="Preview ${n + 1}"><img ${galleryImageAttrs(item.poster)} alt="">${item.video ? `<span class="lora-thumb-video">${galleryIcon("play")}</span>` : ""}</button>`).join("")}</div>` : ""}`;
}

function galleryCard(index, detail = false) {
  const filename = LORA_SLOTS[index], meta = galleryMeta(index), info = galleryInfo(index), row = loraRow(index);
  const enabled = row?.querySelector(".lora-enabled")?.checked || false;
  const strength = row?.querySelector(".lora-strength")?.value ?? meta.strength ?? 1;
  const group = gallery.catalog?.assignments[filename] || meta.group || "Other";
  const triggers = meta.triggers?.length ? meta.triggers : info.trained_words || [];
  const label = galleryLabel(index);
  const aliasGroups = gallery.aliases.filter((entry) => entry.lora === filename);
  const source = [info.name && info.name !== label ? info.name : "", info.creator ? `by ${info.creator}` : ""].filter(Boolean).join(" ");
  const nav = detail ? (() => {
    const list = galleryIndexes(), at = list.indexOf(index);
    return `<div class="lora-detail-nav"><button type="button" class="secondary-button" data-gallery-back>&#8592; Back to gallery</button>
      <span><button type="button" class="secondary-button" data-gallery-lora="${list[at - 1] ?? ""}" ${at < 1 ? "disabled" : ""} aria-label="Previous LoRA">&#8249;</button>
      <button type="button" class="secondary-button" data-gallery-lora="${list[at + 1] ?? ""}" ${at >= list.length - 1 ? "disabled" : ""} aria-label="Next LoRA">&#8250;</button></span></div>`;
  })() : "";
  return `${nav}<article class="lora-card${enabled ? " is-enabled" : ""}${detail ? " lora-card-detail" : ""}" data-gallery-index="${index}">
    <div class="lora-card-gallery">${galleryMedia(index, detail)}</div>
    <div class="lora-card-body"><div class="lora-card-head"><div class="lora-card-title">
      ${detail ? `<strong>${escapeHtml(label)}</strong>` : `<button type="button" class="lora-card-open" data-gallery-open>${escapeHtml(label)}</button>`}
      ${source ? `<span class="field-hint">${escapeHtml(source)}</span>` : ""}</div>
      <input type="number" class="lora-card-strength" min="0" max="2" step="0.05" value="${escapeHtml(strength)}" aria-label="Strength">
      <label class="switch" title="Use this LoRA"><input type="checkbox" class="lora-card-enabled" ${enabled ? "checked" : ""} aria-label="Use ${escapeHtml(label)}"><span></span></label></div>
      <div class="lora-card-chips"><span class="lora-chip">${escapeHtml(group)}</span>${info.base_model ? `<span class="lora-chip">${escapeHtml(info.base_model)}</span>` : ""}
      ${triggers.map((word) => `<button type="button" class="reference-token" data-gallery-trigger="${escapeHtml(word)}" title="Add to prompt">${escapeHtml(word)}</button>`).join("")}</div>
      ${aliasGroups.length ? `<div class="lora-card-aliases">${aliasGroups.flatMap((entry) => entry.aliases.map((alias) => `<button type="button" class="lora-chip" data-gallery-alias="${escapeHtml(alias)}" title="Add shortcut for ${escapeHtml(entry.target)}">${escapeHtml(alias)} &rarr; ${escapeHtml(entry.target)}</button>`)).join("")}</div>` : ""}
      <p class="lora-card-note">Recommended ${escapeHtml(String(meta.strength ?? 1))}${meta.strength_note ? ` · ${escapeHtml(meta.strength_note)}` : ""}</p>
      ${info.description ? `<p class="lora-card-desc${detail ? " is-open" : ""}">${escapeHtml(info.description)}</p>` : ""}
      <div class="lora-card-foot">${!detail && info.description ? `<button type="button" class="button-link" data-gallery-open>Show more</button>` : "<span></span>"}
      <button type="button" class="secondary-button" data-gallery-edit>Edit</button>
      ${info.url || meta.url ? `<a class="button-link" href="${escapeHtml(info.url || meta.url)}" target="_blank" rel="noopener noreferrer">Source page &#8599;</a>` : ""}</div>
      ${detail && gallery.editing ? `<div class="lora-gallery-editor">
        <input type="text" data-edit-name maxlength="100" value="${escapeHtml(label)}" aria-label="LoRA name">
        <select data-edit-group aria-label="Category">${(gallery.catalog?.categories || [group]).map((category) => `<option value="${escapeHtml(category)}" ${category === group ? "selected" : ""}>${escapeHtml(category)}</option>`).join("")}</select>
        <input type="number" data-edit-strength min="0" max="2" step="0.05" value="${escapeHtml(String(meta.strength ?? 1))}" aria-label="Recommended strength">
        <input type="text" data-edit-triggers value="${escapeHtml(triggers.join(", "))}" aria-label="Trigger words" placeholder="Trigger words, comma separated">
        <textarea data-edit-description aria-label="Description" placeholder="Description">${escapeHtml(info.description || "")}</textarea>
        <input type="url" data-edit-url value="${escapeHtml(info.url || meta.url || "")}" aria-label="Source URL" placeholder="https://…">
        ${triggers.length ? `<select data-alias-target aria-label="Trigger for alias">${triggers.map((trigger) => `<option value="${escapeHtml(trigger)}">${escapeHtml(trigger)}</option>`).join("")}</select>
          <input type="text" data-alias-words placeholder="Shortcuts, comma separated" aria-label="Trigger aliases">
          <button type="button" class="secondary-button" data-alias-save>Save aliases</button>
          ${aliasGroups.map((entry) => `<button type="button" class="button-link" data-alias-delete="${escapeHtml(entry.target)}">Remove aliases for ${escapeHtml(entry.target)}</button>`).join("")}` : ""}
        <button type="button" class="primary-button" data-gallery-save>Save details</button>
        ${meta.custom_id ? `<button type="button" class="secondary-button" data-gallery-scrape>Ask Hermes to import URL</button><button type="button" class="secondary-button danger" data-gallery-delete>Delete LoRA</button>` : ""}
        <span class="field-hint" data-edit-status></span>
      </div>` : ""}
    </div></article>`;
}

function renderLoraGallery() {
  if (gallery.detail !== null) {
    $("#lora-gallery-grid").hidden = true;
    $("#lora-gallery-detail").hidden = false;
    $("#lora-gallery-detail").innerHTML = galleryCard(gallery.detail, true);
    observeGalleryVideos();
    return;
  }
  $("#lora-gallery-detail").hidden = true;
  $("#lora-gallery-detail").innerHTML = "";
  $("#lora-gallery-grid").hidden = false;
  const indexes = galleryIndexes();
  let previous = "";
  $("#lora-gallery-grid").innerHTML = indexes.length ? indexes.map((index) => {
    const group = gallery.catalog?.assignments[LORA_SLOTS[index]] || galleryMeta(index).group || "Other";
    const heading = $("#lora-gallery-by-group").checked && group !== previous ? `<h3 class="lora-gallery-section">${escapeHtml(group)}</h3>` : "";
    previous = group;
    return heading + galleryCard(index);
  }).join("") : `<p class="field-hint">${LORA_SLOTS.length ? "No LoRAs match." : "No LoRAs installed for this model. Upload a file or link a model below."}</p>`;
  $("#lora-gallery-grid").scrollTop = gallery.gridScroll;
  observeGalleryVideos();
}

function rerenderGalleryMedia(card, index) {
  card.querySelector(".lora-card-gallery").innerHTML = galleryMedia(index, card.classList.contains("lora-card-detail"));
  observeGalleryVideos();
}

async function openLoraGallery() {
  try {
    const [index, catalog, aliases] = await Promise.all([
      Api.request("/api/lora-gallery/shared"), Api.request(`/api/lora-gallery/catalog/${galleryModel()}`),
      Api.request(`/api/lora-gallery/aliases/${galleryModel()}`),
    ]);
    gallery.index = index;
    gallery.catalog = catalog;
    gallery.aliases = aliases;
  } catch (error) { toast(error.message, "error"); return; }
  gallery.detail = null;
  gallery.editing = false;
  $("#lora-gallery-model").textContent = {krea: "Krea 2", qwen: "Qwen-Image 2.1", moss: "MOSS-Image", anima: "Anima"}[galleryModel()];
  $("#lora-gallery-config").hidden = true;
  $("#lora-gallery-nsfw").checked = galleryShowsNsfw();
  const groups = gallery.catalog.categories || [];
  $("#lora-gallery-group").innerHTML = `<option value="">All categories</option>` + groups.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  renderLoraGallery();
  openDialog("#lora-gallery");
  $("#lora-gallery-search").focus();
}

async function saveGalleryDetails(card) {
  const index = Number(card.dataset.galleryIndex), filename = LORA_SLOTS[index], meta = galleryMeta(index);
  const value = (selector) => $(selector, card).value.trim();
  const body = {filename, label: value("[data-edit-name]"), group: value("[data-edit-group]"),
    default_strength: Number(value("[data-edit-strength]")), triggers: value("[data-edit-triggers]").split(",").map((word) => word.trim()).filter(Boolean),
    description: value("[data-edit-description]"), url: value("[data-edit-url]"), strength_note: meta.strength_note || ""};
  try {
    if (meta.custom_id) await Api.request(`/api/lora-gallery/custom/${meta.custom_id}`, {method: "PATCH", body});
    await Api.request(`/api/lora-gallery/catalog/${galleryModel()}/item`, {method: "PATCH", body});
    location.reload();
  } catch (error) { $("[data-edit-status]", card).textContent = error.message; }
}

function renderGalleryConfig() {
  const draft = gallery.draft;
  $("#lora-config-categories").innerHTML = draft.categories.map((name, index) => `<div class="lora-config-category" data-category-index="${index}">
    <input type="text" class="lora-category-name" value="${escapeHtml(name)}" maxlength="60" aria-label="Category name">
    <button type="button" class="secondary-button" data-category-move="-1" ${index === 0 ? "disabled" : ""}>↑</button>
    <button type="button" class="secondary-button" data-category-move="1" ${index === draft.categories.length - 1 ? "disabled" : ""}>↓</button>
    <button type="button" class="secondary-button danger" data-category-remove ${draft.categories.length < 2 ? "disabled" : ""}>Remove</button></div>`).join("") +
    `<div class="lora-config-items">${draft.order.map((filename, index) => `<div class="lora-config-item" data-order-index="${index}">
      <span>${escapeHtml(LORA_INFO[LORA_SLOTS.indexOf(filename)]?.label || filename)}</span>
      <select data-item-category aria-label="Category">${draft.categories.map((name) => `<option value="${escapeHtml(name)}" ${draft.assignments[filename] === name ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}</select>
      <button type="button" class="secondary-button" data-item-move="-1" ${index === 0 ? "disabled" : ""}>↑</button>
      <button type="button" class="secondary-button" data-item-move="1" ${index === draft.order.length - 1 ? "disabled" : ""}>↓</button></div>`).join("")}</div>`;
}

function bindLoraGallery() {
  $("#lora-gallery-open").addEventListener("click", openLoraGallery);
  $("#lora-reset").addEventListener("click", () => {
    document.querySelectorAll("#lora-list .lora-row").forEach((row) => { row.querySelector(".lora-strength").value = loraDefaultStrength(Number(row.dataset.loraIndex)); });
    $("#generate-form").dispatchEvent(new Event("change"));
    toast("LoRA strengths set to their recommended values.");
  });
  const host = $("#lora-gallery");
  host.addEventListener("click", async (event) => {
    const card = event.target.closest("[data-gallery-index]");
    const index = card ? Number(card.dataset.galleryIndex) : gallery.detail;
    if (event.target.closest("[data-gallery-back]")) { gallery.detail = null; gallery.editing = false; renderLoraGallery(); return; }
    const next = event.target.closest("[data-gallery-lora]");
    if (next && next.dataset.galleryLora !== "") { gallery.detail = Number(next.dataset.galleryLora); gallery.editing = false; renderLoraGallery(); return; }
    if (!card) return;
    const filename = LORA_SLOTS[index];
    const step = event.target.closest("[data-gallery-step]");
    const thumb = event.target.closest("[data-gallery-thumb]");
    if (step || thumb) {
      const count = galleryInfo(index).previews?.length || 1;
      gallery.preview.set(filename, thumb ? Number(thumb.dataset.galleryThumb) : ((gallery.preview.get(filename) || 0) + Number(step.dataset.galleryStep) + count) % count);
      rerenderGalleryMedia(card, index); return;
    }
    if (event.target.closest("[data-gallery-reveal]")) {
      gallery.reveal.add(`${filename}:${gallery.preview.get(filename) || 0}`); rerenderGalleryMedia(card, index); return;
    }
    if (event.target.closest("[data-gallery-open]")) { gallery.gridScroll = $("#lora-gallery-grid").scrollTop; gallery.detail = index; gallery.editing = false; renderLoraGallery(); return; }
    if (event.target.closest("[data-gallery-edit]")) { gallery.gridScroll = $("#lora-gallery-grid").scrollTop; gallery.detail = index; gallery.editing = true; renderLoraGallery(); return; }
    if (event.target.closest("[data-gallery-save]")) { await saveGalleryDetails(card); return; }
    if (event.target.closest("[data-gallery-scrape]")) {
      const status = $("[data-edit-status]", card);
      try {
        const url = $("[data-edit-url]", card).value.trim();
        const {task} = await Api.request(`/api/lora-gallery/custom/${galleryMeta(index).custom_id}/scrape`, {method: "POST", body: {url}});
        for (;;) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const current = (await Api.request(`/api/lora-gallery/task/${task.id}`)).task;
          status.textContent = current.stage + (current.error ? `: ${current.error}` : "");
          if (current.state === "complete") { location.reload(); break; }
          if (current.state === "failed") break;
        }
      } catch (error) { status.textContent = error.message; }
      return;
    }
    if (event.target.closest("[data-alias-save]")) {
      try {
        gallery.aliases = await Api.request(`/api/lora-gallery/aliases/${galleryModel()}`, {method: "POST", body: {
          filename, target: $("[data-alias-target]", card).value,
          aliases: $("[data-alias-words]", card).value.split(",").map((word) => word.trim()).filter(Boolean),
        }});
        renderLoraGallery();
      } catch (error) { $("[data-edit-status]", card).textContent = error.message; }
      return;
    }
    const aliasDelete = event.target.closest("[data-alias-delete]");
    if (aliasDelete) {
      try {
        const query = new URLSearchParams({filename, target: aliasDelete.dataset.aliasDelete});
        gallery.aliases = await Api.request(`/api/lora-gallery/aliases/${galleryModel()}?${query}`, {method: "DELETE"});
        renderLoraGallery();
      } catch (error) { $("[data-edit-status]", card).textContent = error.message; }
      return;
    }
    const aliasChip = event.target.closest("[data-gallery-alias]");
    if (aliasChip) {
      loraRow(index).querySelector(".lora-enabled").checked = true;
      insertAtCursor($("#prompt"), aliasChip.dataset.galleryAlias);
      $("#generate-form").dispatchEvent(new Event("change"));
      toast(`Added ${aliasChip.dataset.galleryAlias} to the prompt.`);
      renderLoraGallery(); return;
    }
    if (event.target.closest("[data-gallery-delete]")) {
      if (!confirm(`Delete ${galleryLabel(index)} and its installed LoRA file?`)) return;
      try { await Api.request(`/api/lora-gallery/custom/${galleryMeta(index).custom_id}`, {method: "DELETE"}); location.reload(); }
      catch (error) { $("[data-edit-status]", card).textContent = error.message; }
      return;
    }
    const trigger = event.target.closest("[data-gallery-trigger]");
    if (trigger) { insertAtCursor($("#prompt"), trigger.dataset.galleryTrigger); toast(`Added "${trigger.dataset.galleryTrigger}" to the prompt.`); }
  });
  host.addEventListener("change", (event) => {
    const card = event.target.closest("[data-gallery-index]");
    if (!card) return;
    const row = loraRow(Number(card.dataset.galleryIndex));
    if (event.target.classList.contains("lora-card-enabled")) {
      row.querySelector(".lora-enabled").checked = event.target.checked;
      card.classList.toggle("is-enabled", event.target.checked);
    }
    if (event.target.classList.contains("lora-card-strength")) row.querySelector(".lora-strength").value = event.target.value;
    $("#generate-form").dispatchEvent(new Event("change"));
  });
  for (const selector of ["#lora-gallery-search", "#lora-gallery-group", "#lora-gallery-enabled-only", "#lora-gallery-by-group"])
    $(selector).addEventListener(selector.endsWith("search") ? "input" : "change", renderLoraGallery);
  $("#lora-gallery-nsfw").addEventListener("change", (event) => {
    try { localStorage.setItem(GALLERY_NSFW_KEY, event.target.checked ? "on" : "off"); } catch { /* storage blocked */ }
    renderLoraGallery();
  });
  $("#lora-gallery-upload-file").addEventListener("change", (event) => {
    if (event.target.files?.[0] && !$("#lora-gallery-upload-name").value) $("#lora-gallery-upload-name").value = event.target.files[0].name.replace(/\.safetensors$/i, "");
  });
  $("#lora-gallery-upload").addEventListener("click", async () => {
    const file = $("#lora-gallery-upload-file").files?.[0], name = $("#lora-gallery-upload-name").value.trim();
    if (!file || !/\.safetensors$/i.test(file.name) || !name) { toast("Choose a .safetensors file and name.", "error"); return; }
    if (file.size > 6 * 1024 ** 3) { toast("LoRA exceeds 6 GiB.", "error"); return; }
    const button = $("#lora-gallery-upload"), status = $("#lora-gallery-upload-status");
    button.disabled = true;
    try {
      const size = 8 * 1024 * 1024;
      if (file.size <= size) {
        const body = new FormData(); body.append("model", galleryModel()); body.append("name", name); body.append("file", file);
        status.textContent = "Uploading…";
        await Api.request("/api/lora-gallery/upload", {method: "POST", body});
      } else {
        const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
        const total = Math.ceil(file.size / size);
        let next = 0, finished = 0;
        const worker = async () => {
          while (next < total) {
            const index = next++;
            const body = new FormData();
            body.append("upload_id", uploadId); body.append("index", String(index)); body.append("total", String(total));
            body.append("chunk", file.slice(index * size, (index + 1) * size), `${file.name}.part${index}`);
            for (let attempt = 1; ; attempt++) {
              try { await Api.request("/api/lora-gallery/chunk", {method: "POST", body}); break; }
              catch (error) { if (attempt === 4) throw error; await new Promise((resolve) => setTimeout(resolve, attempt * 1500)); }
            }
            status.textContent = `Uploading ${++finished} of ${total} parts…`;
          }
        };
        await Promise.all(Array.from({length: Math.min(3, total)}, worker));
        status.textContent = "Installing LoRA…";
        await Api.request("/api/lora-gallery/complete", {method: "POST", body: {upload_id: uploadId, total, model: galleryModel(), name}});
      }
      location.reload();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#lora-gallery-link-toggle").addEventListener("click", () => {
    const panel = $("#lora-gallery-link-panel"); panel.hidden = !panel.hidden;
    $("#lora-gallery-link-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  });
  $("#lora-gallery-link-inspect").addEventListener("click", async () => {
    const status = $("#lora-gallery-link-status"); status.textContent = "Reading model listing…";
    try {
      gallery.inspection = await Api.request("/api/lora-gallery/link/inspect", {method: "POST", body: {
        model: galleryModel(), url: $("#lora-gallery-link-url").value.trim(), token: $("#lora-gallery-link-token").value.trim(),
      }});
      $("#lora-gallery-link-file").innerHTML = gallery.inspection.candidates.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.filename)} · ${escapeHtml(item.base_model || item.version || "")}</option>`).join("");
      $("#lora-gallery-link-file").hidden = false; $("#lora-gallery-link-add").hidden = false;
      status.textContent = `${gallery.inspection.candidates.length} compatible file(s) found`;
    } catch (error) { status.textContent = error.message; }
  });
  $("#lora-gallery-link-add").addEventListener("click", async () => {
    const status = $("#lora-gallery-link-status"), meter = $("#lora-gallery-link-meter");
    try {
      const {task} = await Api.request("/api/lora-gallery/link", {method: "POST", body: {
        inspection_id: gallery.inspection.id, candidate_id: $("#lora-gallery-link-file").value,
        auto: $("#lora-gallery-link-auto").checked,
      }});
      meter.hidden = false;
      gallery.linkTaskId = task.id;
      $("#lora-gallery-link-cancel").hidden = false;
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const current = (await Api.request(`/api/lora-gallery/link/${task.id}`)).task;
        status.textContent = current.stage + (current.error ? `: ${current.error}` : "");
        meter.value = current.total_bytes ? Math.round(current.downloaded_bytes / current.total_bytes * 100) : 0;
        if (current.state === "complete") { location.reload(); break; }
        if (current.state === "failed" || current.state === "cancelled") break;
      }
    } catch (error) { status.textContent = error.message; }
    finally { gallery.linkTaskId = null; $("#lora-gallery-link-cancel").hidden = true; }
  });
  $("#lora-gallery-link-cancel").addEventListener("click", async () => {
    if (!gallery.linkTaskId) return;
    try { await Api.request(`/api/lora-gallery/link/${gallery.linkTaskId}/cancel`, {method: "POST"}); }
    catch (error) { $("#lora-gallery-link-status").textContent = error.message; }
  });
  $("#lora-gallery-config-button").addEventListener("click", () => {
    gallery.draft = structuredClone(gallery.catalog);
    gallery.detail = null;
    $("#lora-gallery-grid").hidden = true; $("#lora-gallery-detail").hidden = true;
    $("#lora-gallery-config").hidden = false; renderGalleryConfig();
  });
  $("#lora-config-back").addEventListener("click", () => { $("#lora-gallery-config").hidden = true; renderLoraGallery(); });
  $("#lora-config-add").addEventListener("click", () => {
    const input = $("#lora-config-new"), name = input.value.trim();
    if (name && !gallery.draft.categories.some((item) => item.toLowerCase() === name.toLowerCase())) {
      gallery.draft.categories.push(name); input.value = ""; renderGalleryConfig();
    }
  });
  $("#lora-config-categories").addEventListener("change", (event) => {
    const category = event.target.closest("[data-category-index]");
    if (category && event.target.classList.contains("lora-category-name")) {
      const before = gallery.draft.categories[Number(category.dataset.categoryIndex)], after = event.target.value.trim();
      if (!after) { event.target.value = before; return; }
      gallery.draft.categories[Number(category.dataset.categoryIndex)] = after;
      for (const [filename, name] of Object.entries(gallery.draft.assignments)) if (name === before) gallery.draft.assignments[filename] = after;
      renderGalleryConfig();
    }
    const item = event.target.closest("[data-order-index]");
    if (item && event.target.matches("[data-item-category]")) gallery.draft.assignments[gallery.draft.order[Number(item.dataset.orderIndex)]] = event.target.value;
  });
  $("#lora-config-categories").addEventListener("click", (event) => {
    const category = event.target.closest("[data-category-index]"), item = event.target.closest("[data-order-index]");
    const move = event.target.closest("[data-category-move], [data-item-move]");
    if (move) {
      const values = category ? gallery.draft.categories : gallery.draft.order;
      const at = Number(category?.dataset.categoryIndex ?? item?.dataset.orderIndex), to = at + Number(move.dataset.categoryMove ?? move.dataset.itemMove);
      [values[at], values[to]] = [values[to], values[at]]; renderGalleryConfig(); return;
    }
    if (category && event.target.closest("[data-category-remove]")) {
      const name = gallery.draft.categories.splice(Number(category.dataset.categoryIndex), 1)[0];
      for (const [filename, group] of Object.entries(gallery.draft.assignments)) if (group === name) gallery.draft.assignments[filename] = gallery.draft.categories[0];
      renderGalleryConfig();
    }
  });
  $("#lora-config-hermes").addEventListener("click", async () => {
    const status = $("#lora-config-status"); status.textContent = "Hermes is reading gallery details…";
    try {
      const {task} = await Api.request(`/api/lora-gallery/catalog/${galleryModel()}/organize`, {method: "POST"});
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const current = (await Api.request(`/api/lora-gallery/task/${task.id}`)).task;
        status.textContent = current.stage + (current.error ? `: ${current.error}` : "");
        if (current.state === "complete") {
          gallery.draft = current.proposal;
          renderGalleryConfig();
          status.textContent = "Review Hermes’s categories and order, then Save changes.";
          break;
        }
        if (current.state === "failed") break;
      }
    } catch (error) { status.textContent = error.message; }
  });
  $("#lora-config-save").addEventListener("click", async () => {
    try { await Api.request(`/api/lora-gallery/catalog/${galleryModel()}`, {method: "PUT", body: gallery.draft}); location.reload(); }
    catch (error) { $("#lora-config-status").textContent = error.message; }
  });
}

/* --------------------------------------------------------------- sse -- */

function bindEvents() {
  const source = new EventSource(`${API_BASE}/api/events`);
  source.addEventListener("jobs", (event) => {
    state.jobs = JSON.parse(event.data);
    renderJobs();
  });
  source.addEventListener("checkpoints", (event) => {
    state.checkpoints = JSON.parse(event.data); // new downloads show up without a refresh
    renderCheckpointOptions();
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

hydrateIcons();
renderLoras();
loadStoredReferences();
let savedModel = "krea2";
try { savedModel = localStorage.getItem(MODEL_STORAGE_KEY) || "krea2"; } catch { /* storage unavailable */ }
applyModel(savedModel === "lustify" ? "krea2" : savedModel);
if (savedModel === "lustify") { try { localStorage.setItem("krea.checkpoint.krea2", "lustifyNSFWCheckpoint_v10Krea2_fp8.safetensors"); } catch { /* storage unavailable */ } }
refreshCheckpoints();
window.addEventListener("focus", refreshCheckpoints);
bindDialogs();
bindReferences();
bindForm();
bindPresets();
bindJobs();
bindLoraGallery();
bindHotbar();
renderHotbar();
loadPresets().then(() => {
  const name = readSelectedPreset();
  if (name) applyPreset(name);
  renderPresetBar();
});
refreshJobsOnce();
bindEvents();
setInterval(updateJobMetrics, 1000);
