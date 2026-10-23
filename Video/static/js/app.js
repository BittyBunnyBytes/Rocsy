const API_BASE = document.body.dataset.appPrefix || "";

const state = {
  model: "minimax_h3",
  mode: "text",
  references: [],
  firstFrameId: null,
  lastFrameId: null,
  shFirstFrameId: null,
  shLastFrameId: null,
  continueFromJob: null, // { id, prompt } of the job being continued, or null
  savedResolution: null, // width/height/aspect/megapixels from before a "Match resolution" click, or null
  jobs: [],
  filter: "all",
  stitchMode: false,
  compareMode: false,
  stitchSelection: [], // ordered job ids, in click order - drives ffmpeg's concat order
  stitchMusicId: null, // reference id of the uploaded background-music track, or null
  options: {},
  serviceStatus: {},
  aliasMatches: [],
  aliasIndex: 0,
  viewer: null, // { ids: [referenceId...], index } while the reference viewer is open, else null
};

// Per-job scratch state for the SeedHunter candidate-picker (which candidate is
// selected, upscale/RIFE toggles) - keyed by job id, not part of `state` proper
// since it's transient UI-only and never sent anywhere until Finalize is clicked.
const finalizeChoices = new Map();

function finalizeChoice(jobId) {
  if (!finalizeChoices.has(jobId)) {
    finalizeChoices.set(jobId, { candidate: 1, enableUpscale: false, enableRife: false });
  }
  return finalizeChoices.get(jobId);
}

const MODELS = {
  // minDuration: the model's own minimum for a valid/trained-range generation
  // (H3's node schema documents "trained range is ~124-362 frames", i.e. ~5s+
  // at 24fps - shorter clips are accepted by the node but fall outside what
  // the model was actually trained on). Enforced as a hard slider floor so an
  // invalid-but-technically-accepted duration is never queued by accident.
  minimax_h3: { label: "MiniMax H3", advancedPanel: "#h3-advanced", modesAllowed: ["text", "fl2v", "ref2v"], minDuration: 5 },
  ltx_2_5: { label: "LTX 2.5", advancedPanel: "#ltx-advanced", modesAllowed: ["text", "fl2v", "ref2v"], minDuration: 1 },
  // Wan 2.2 A14B: trained on 5 s (81 frames at 16 fps); the server caps it at 10 s.
  wan_2_2: { label: "Wan 2.2", advancedPanel: "#wan-advanced", modesAllowed: ["text", "fl2v"], minDuration: 1, maxDuration: 10, defaultDuration: 5 },
  seedhunter: { label: "H3 SeedHunter", advancedPanel: "#seedhunter-advanced", modesAllowed: [], minDuration: 5 },
};
const modelLabels = { minimax_h3: "MiniMax H3", ltx_2_5: "LTX 2.5", wan_2_2: "Wan 2.2", seedhunter: "H3 SeedHunter" };
// The filename-keyed LoRA lists (stack up to N, in list order; rows carry
// data-ltx-lora in both): LTX 2.5 and Wan 2.2. MiniMax H3 uses positional slots.
const LIST_LORAS = {
  ltx_2_5: { list: "#ltx-lora-list", details: "#ltx-lora-details", key: "ltx_loras", max: "max_ltx_loras", maxLabel: "#ltx-lora-max" },
  wan_2_2: { list: "#wan-lora-list", details: "#wan-lora-details", key: "wan_loras", max: "max_wan_loras", maxLabel: "#wan-lora-max" },
};
const listLoras = (model = state.model) => LIST_LORAS[model] || null;
const ltxTileDetails = {
  low: "Low · 512px tiles, 64px overlap, 64/8 temporal",
  medium: "Medium · 768px tiles, 192px overlap, 128/32 temporal",
  large: "Large · 1024px tiles, 256px overlap, 128/48 temporal",
};
const aspectRatios = { "16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4 };
const defaultPrompts = {
  minimax_h3: "integrated_multimodal_description: A cinematic continuous shot with natural motion and stable details.\noverall_soundscape: Natural ambient sound.\nnon_diegetic_music: N/A",
  ltx_2_5: "A cinematic continuous shot with natural motion, stable details, intentional camera movement, and synchronized ambient sound.",
  wan_2_2: "A cinematic shot with natural motion and stable details: describe the subject, the scene, how it moves, the camera movement, and the lighting and style.",
  seedhunter: "integrated_multimodal_description: A cinematic continuous shot with natural motion and stable details.\noverall_soundscape: Natural ambient sound.\nnon_diegetic_music: N/A",
};
const promptDrafts = { minimax_h3: defaultPrompts.minimax_h3, ltx_2_5: defaultPrompts.ltx_2_5, wan_2_2: defaultPrompts.wan_2_2, seedhunter: defaultPrompts.seedhunter };
const completedJobs = new Set();
let activeAgenticSessionId = null;
let activeAgenticSessionKind = "new";
let activeAgenticRunJobId = null;
let agenticFocusDismissed = false; // "Show all" pressed while a resumed session is focused
let renderedAgenticFocus = null;
const agenticWatchers = new Set();
const AGENTIC_ACTIVE_STATES = new Set(["waiting_for_gpu", "running", "queuing", "rendering", "reviewing", "awaiting_approval"]);
const ACTIVE_STATES = new Set(["waiting_for_gpu", "preparing", "queued", "rendering", "cancelling"]);
const STATE_LABELS = {
  waiting_for_gpu: "waiting for GPU", preparing: "preparing", queued: "queued", rendering: "rendering",
  cancelling: "cancelling", cancelled: "cancelled", complete: "complete", failed: "failed",
  awaiting_selection: "approval required", finalizing: "finalizing",
};
function needsAttention(job) {
  return job.state === "awaiting_selection";
}
let alertsEnabled = localStorage.getItem("h3-completion-alerts") !== "off";
let audioContext = null;

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];

// A request that can stall (network switch) shouldn't hold a cached promise or an
// in-flight guard forever: these quick POSTs give up instead (see Api.request).
const timeoutSignal = (ms) => (typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(ms) : undefined);

class Api {
  static async request(path, options = {}) {
    const init = { ...options, headers: { ...(options.headers || {}) } };
    // GETs give up after 30 s: a request stalled by a network switch never
    // settled, and a stuck re-sync kept every later live update buffered
    // (resyncState). POSTs can legitimately take minutes (Enhance, Generate
    // waking ComfyUI), so they keep the browser's own limits.
    if (!init.signal && (!init.method || init.method === "GET") && typeof AbortSignal.timeout === "function") {
      init.signal = AbortSignal.timeout(30_000);
    }
    if (init.body && !(init.body instanceof FormData)) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      if (error?.name === "TimeoutError") throw Object.assign(new Error("The server didn't answer in time. Check your connection and try again."), { code: "timeout" });
      throw error;
    }
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("json") ? await response.json() : null;
    if (!response.ok) throw Object.assign(new Error(data?.message || `Request failed (${response.status})`), { code: data?.error, status: response.status });
    return data;
  }

  // Files over UPLOAD_DIRECT_MAX go up in UPLOAD_CHUNK pieces: Cloudflare's
  // tunnel rejects bodies over 100 MB and the gateway over 50 MB (a 123 MB
  // upload failed with a bare 413). The server reassembles them and checks
  // them exactly like a direct upload (POST /api/references/chunked).
  // Phone photos (4032x3024, 3-8 MB) are far bigger than any render uses
  // (H3 tops out near 1856x1024): shrink them in the browser before they cross
  // the home uplink. Long edge 2560, EXIF rotation applied, PNG keeps its alpha.
  static async shrinkImage(file) {
    const MAX_EDGE = 2560;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || typeof createImageBitmap !== "function") return file;
    let bitmap;
    try { bitmap = await createImageBitmap(file, { imageOrientation: "from-image" }); } catch { return file; }
    const scale = MAX_EDGE / Math.max(bitmap.width, bitmap.height);
    if (scale >= 1) { bitmap.close?.(); return file; }
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const context = canvas.getContext("2d");
    context.imageSmoothingQuality = "high";
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const type = file.type === "image/png" ? "image/png" : "image/jpeg";
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, 0.92));
    if (!blob || blob.size >= file.size) return file;
    const name = type === "image/jpeg" ? file.name.replace(/\.(webp|jpe?g)$/i, "") + ".jpg" : file.name;
    return new File([blob], name, { type });
  }

  static async upload(file, role) {
    file = await this.shrinkImage(file);
    // Parallel 8 MB chunks for anything over 8 MB: one request stream rarely
    // fills a phone link through the Cloudflare tunnel, three at once do.
    const UPLOAD_DIRECT_MAX = 8 * 1024 * 1024, UPLOAD_CHUNK = 8 * 1024 * 1024, PARALLEL = 3;
    if (file.size <= UPLOAD_DIRECT_MAX) {
      const body = new FormData();
      body.append("file", file);
      if (role) body.append("role", role);
      return this.request(`${API_BASE}/api/references`, { method: "POST", body });
    }
    const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
    const total = Math.ceil(file.size / UPLOAD_CHUNK);
    const mb = Math.round(file.size / 1048576);
    let done = 0, result = null, next = 0;
    const sendChunk = async (index) => {
      const body = new FormData();
      body.append("chunk", file.slice(index * UPLOAD_CHUNK, (index + 1) * UPLOAD_CHUNK), `${file.name}.part${index}`);
      body.append("upload_id", uploadId);
      body.append("index", String(index));
      body.append("total", String(total));
      body.append("filename", file.name);
      body.append("content_type", file.type || "application/octet-stream");
      if (role) body.append("role", role);
      // A dropped request used to fail the whole upload: network errors and 5xx
      // are retried (the server overwrites the part); 4xx are real answers.
      for (let attempt = 1; ; attempt++) {
        try {
          const reply = await this.request(`${API_BASE}/api/references/chunked`, { method: "POST", body });
          if (reply?.id) result = reply; // whichever part completes the set gets the reference back
          return;
        } catch (error) {
          if ((error.status && error.status < 500) || attempt >= 4) throw error;
          toast(`Upload part ${index + 1} failed (${error.message}); retrying…`);
          await new Promise((resolve) => setTimeout(resolve, 1500 * attempt));
        }
      }
    };
    toast(`Uploading ${file.name} (${mb} MB)…`);
    const worker = async () => {
      while (next < total) {
        const index = next++;
        await sendChunk(index);
        done += 1;
        if (done < total && done % Math.max(1, Math.round(total / 4)) === 0) toast(`Uploading ${file.name}: ${Math.round(done / total * 100)}% of ${mb} MB`);
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL, total) }, worker));
    if (!result?.id) throw new Error("The upload didn't finish on the server. Try again.");
    return result;
  }

  static patchReference(id, fields) {
    const body = new FormData();
    for (const [key, value] of Object.entries(fields)) {
      if (value !== undefined && value !== null) body.append(key, value);
    }
    return this.request(`${API_BASE}/api/references/${id}`, { method: "PATCH", body });
  }

  static replaceReferenceContent(id, blob, filename, crop) {
    const body = new FormData();
    body.append("file", blob, filename);
    if (crop) {
      body.append("crop_x", crop.x);
      body.append("crop_y", crop.y);
      body.append("crop_w", crop.w);
      body.append("crop_h", crop.h);
    }
    return this.request(`${API_BASE}/api/references/${id}/content`, { method: "PUT", body });
  }
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

// Inline SVG icons for the UI (instead of emoji, which look different on every
// platform). Same stroke style as JOB_ICONS but sized to the text (.ui-icon).
// The template uses <span data-icon="name"> placeholders; hydrateIcons fills them.
const ICON_PATHS = {
  star: '<path fill="currentColor" d="M12 2.5l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 17.3l-5.8 3.1 1.1-6.5-4.7-4.6 6.5-.9z"/>',
  sparkles: '<path d="M9.9 15.5a2 2 0 0 0-1.4-1.4l-6.1-1.6a.5.5 0 0 1 0-1l6.1-1.6a2 2 0 0 0 1.4-1.4l1.6-6.1a.5.5 0 0 1 1 0l1.6 6.1a2 2 0 0 0 1.4 1.4l6.1 1.6a.5.5 0 0 1 0 1l-6.1 1.6a2 2 0 0 0-1.4 1.4l-1.6 6.1a.5.5 0 0 1-1 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  save: '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
  pencil: '<path d="M21.2 6.8a2.8 2.8 0 0 0-4-4L3.8 16.2a2 2 0 0 0-.5.8L2 21.4a.5.5 0 0 0 .6.6l4.4-1.3a2 2 0 0 0 .8-.5z"/><path d="m15 5 4 4"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18"/><path d="M17 3v18"/><path d="M3 12h18"/><path d="M3 7.5h4"/><path d="M3 16.5h4"/><path d="M17 7.5h4"/><path d="M17 16.5h4"/>',
  settings: '<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  play: '<path fill="currentColor" d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z"/>',
  pause: '<rect fill="currentColor" x="6" y="4" width="4" height="16" rx="1"/><rect fill="currentColor" x="14" y="4" width="4" height="16" rx="1"/>',
  volume: '<path d="M11 4.7a.7.7 0 0 0-1.2-.5L6.4 7.6a1.4 1.4 0 0 1-1 .4H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.4a1.4 1.4 0 0 1 1 .4l3.4 3.4a.7.7 0 0 0 1.2-.5z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.4 18.4a9 9 0 0 0 0-12.8"/>',
  volumeOff: '<path d="M11 4.7a.7.7 0 0 0-1.2-.5L6.4 7.6a1.4 1.4 0 0 1-1 .4H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.4a1.4 1.4 0 0 1 1 .4l3.4 3.4a.7.7 0 0 0 1.2-.5z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>',
  timer: '<path d="M10 2h4"/><path d="M12 14l3-3"/><circle cx="12" cy="14" r="8"/>',
  alert: '<path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  images: '<path d="M18 22H4a2 2 0 0 1-2-2V6"/><path d="m22 13-1.3-1.3a2.4 2.4 0 0 0-3.4 0L11 18"/><circle cx="12" cy="8" r="2"/><rect x="6" y="2" width="16" height="16" rx="2"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  thumbsUp: '<path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 18.5 21H7V10l4.3-7.2A1.9 1.9 0 0 1 15 5.9Z"/>',
  thumbsDown: '<path d="M17 14V3"/><path d="M9 18.1 10 14H4.2a2 2 0 0 1-2-2.3l1.4-7A2 2 0 0 1 5.5 3H17v11l-4.3 7.2A1.9 1.9 0 0 1 9 18.1Z"/>',
  repeat: '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9"/><path d="M14.5 14.5 20 20"/><path d="M8.1 8.1 12 12"/>',
};
const uiIcon = (name) => `<svg class="ui-icon ui-icon-${name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name]}</svg>`;

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => { node.outerHTML = uiIcon(node.dataset.icon); });
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 5000);
}

/* ------------------------------------------------------------- alerts -- */

// Alerts = a completion sound while the page is open, plus notifications.
// Notifications go through the service worker (Android refuses the plain
// Notification constructor) and, where supported, Web Push - the server
// pushes on completion, so they arrive even when the tab is closed or the
// phone is locked. iOS only allows this for the app added to the Home Screen.
const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isInstalled = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
let swRegistration = null;
let pushSubscribed = false;

// BunnyBytes notification hub (gateway): one subscription per device for every
// studio, through the site-wide service worker at /notify-sw. Returns false when
// there's no gateway (standalone / offline copy) so the app's own push is used.
const BunnyHub = {
  async available() {
    if (this._available !== undefined) return this._available;
    try {
      const response = await fetch("/api/notifications", { credentials: "same-origin" });
      this._data = response.ok ? await response.json() : null;
    } catch { this._data = null; }
    return (this._available = Boolean(this._data?.key));
  },
  async registration() {
    this._registration ||= await navigator.serviceWorker.register("/notify-sw", { scope: "/" });
    return this._registration;
  },
  label() {
    const ua = navigator.userAgent;
    const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
      : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "Device";
    const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
    return `${os} · ${browser}`;
  },
  async post(path, body) {
    const response = await fetch(path, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  },
  async subscribe() {
    const registration = await this.registration();
    const padded = (this._data.key + "=".repeat((4 - (this._data.key.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    const serverKey = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    const subscription = await registration.pushManager.getSubscription()
      || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: serverKey });
    await this.post("/api/notifications/subscribe", { subscription: subscription.toJSON(), label: this.label() });
    return subscription;
  },
  async unsubscribe() {
    const subscription = await (await this.registration()).pushManager.getSubscription();
    if (subscription) {
      await this.post("/api/notifications/unsubscribe", { endpoint: subscription.endpoint }).catch(() => {});
      await subscription.unsubscribe();
    }
  },
  async test() {
    const subscription = await (await this.registration()).pushManager.getSubscription();
    if (subscription) await this.post("/api/notifications/test", { endpoint: subscription.endpoint }).catch(() => {});
  },
};

function notificationPermission() {
  return "Notification" in window ? Notification.permission : "unsupported";
}

function updateAlertsButton() {
  const button = $("#alerts-toggle");
  const permission = notificationPermission();
  button.classList.toggle("is-enabled", alertsEnabled);
  button.setAttribute("aria-pressed", alertsEnabled ? "true" : "false");
  button.title = !alertsEnabled ? "Enable completion alerts"
    : pushSubscribed ? "Alerts on · notifications arrive even with the app closed"
    : permission === "granted" ? "Alerts on · notifications while this page is open"
    : isIOS && !isInstalled ? "Completion sound on · for notifications, add this app to your Home Screen first"
    : "Completion sound on · tap to allow notifications";
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  try {
    swRegistration = await navigator.serviceWorker.register(`${API_BASE}/sw.js`, { scope: `${API_BASE}/` });
  } catch { return; }
  // A tapped notification focuses an open tab and asks it to show that job.
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data?.type === "show-job") showJob(event.data.jobId);
  });
  await syncPushSubscription();
}

function base64UrlToBytes(value) {
  const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
}

// Makes sure this device's push subscription exists and the server has it
// (re-subscribing if the server's key changed). Only once permission is granted.
async function syncPushSubscription() {
  pushSubscribed = false;
  if (!pushSupported || !swRegistration || !alertsEnabled || notificationPermission() !== "granted") return updateAlertsButton();
  if (await BunnyHub.available()) {
    try {
      // Drop this app's own subscription (if any): the hub's covers every studio.
      const own = await swRegistration.pushManager.getSubscription();
      if (own) await own.unsubscribe();
      await BunnyHub.subscribe();
      pushSubscribed = true;
    } catch { /* push stays off; in-page notifications still work */ }
    return updateAlertsButton();
  }
  try {
    const { public_key: publicKey } = await Api.request(`${API_BASE}/api/push/key`);
    const serverKey = base64UrlToBytes(publicKey);
    let subscription = await swRegistration.pushManager.getSubscription();
    const currentKey = subscription?.options?.applicationServerKey;
    if (subscription && currentKey && !new Uint8Array(currentKey).every((byte, index) => byte === serverKey[index])) {
      await subscription.unsubscribe();
      subscription = null;
    }
    subscription ||= await swRegistration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: serverKey });
    await Api.request(`${API_BASE}/api/push/subscribe`, { method: "POST", body: subscription.toJSON() });
    pushSubscribed = true;
  } catch { /* push stays off; in-page notifications still work */ }
  updateAlertsButton();
}

async function removePushSubscription() {
  pushSubscribed = false;
  if (await BunnyHub.available()) {
    try { await BunnyHub.unsubscribe(); } catch { /* already gone */ }
    return;
  }
  try {
    const subscription = await swRegistration?.pushManager.getSubscription();
    if (subscription) {
      await Api.request(`${API_BASE}/api/push/unsubscribe`, { method: "POST", body: { endpoint: subscription.endpoint } });
      await subscription.unsubscribe();
    }
  } catch { /* already gone */ }
}

function showJob(jobId) {
  if (isMobileLayout()) setMobileTab("jobs");
  let card = document.querySelector(`[data-job-id="${CSS.escape(jobId)}"]`);
  if (!card) {
    // Older than the current page (or filtered out): show everything up to it.
    const index = state.jobs.findIndex((job) => job.id === jobId);
    if (index < 0) return false;
    state.filter = "all";
    state.jobSearch = "";
    $("#jobs-search").value = "";
    $$('[data-filter]').forEach((item) => item.classList.toggle("is-active", item.dataset.filter === "all"));
    state.jobsShown = Math.ceil((index + 1) / JOBS_PAGE) * JOBS_PAGE;
    renderJobs();
    card = document.querySelector(`[data-job-id="${CSS.escape(jobId)}"]`);
    if (!card) return false;
  }
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("is-flash");
  setTimeout(() => card.classList.remove("is-flash"), 1600);
  return true;
}

function unlockAudio(force = false) {
  if (!alertsEnabled && !force) return;
  audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
  if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
}

// Completion sound (Settings -> Completion sound). The choice is kept on the
// server so every device plays the same ping; loaded at startup. Built-in
// sounds are synthesized, an uploaded one is decoded once and cached.
let pingSettings = { sound: "chime", volume: 0.6, custom: null };
let pingBuffer = null;
let pingBufferUrl = null;
// notes: [start s, Hz]; decay = each note's fade (s); level = peak at 60% volume.
const PING_TONES = {
  chime: { type: "sine", notes: [[0, 660], [0.14, 880]], decay: 0.22, level: 0.16 },
  ding: { type: "sine", notes: [[0, 1318.5], [0, 2637]], decay: 0.9, level: 0.12 },
  bell: { type: "sine", notes: [[0, 523.25], [0, 1046.5], [0, 1567.98], [0, 2637]], decay: 1.5, level: 0.07 },
  triple: { type: "square", notes: [[0, 880], [0.13, 880], [0.26, 880]], decay: 0.08, level: 0.05 },
  coin: { type: "square", notes: [[0, 987.77], [0.08, 1318.5]], decay: 0.3, level: 0.05 },
  marimba: { type: "sine", notes: [[0, 523.25], [0.11, 659.25], [0.22, 783.99]], decay: 0.35, level: 0.18 },
  pop: { type: "sine", notes: [[0, 380]], decay: 0.1, level: 0.3, slide: 1400 },
  rise: { type: "triangle", notes: [[0, 523.25], [0.09, 659.25], [0.18, 783.99], [0.27, 1046.5]], decay: 0.3, level: 0.14 },
};

async function loadPingSettings() {
  try { pingSettings = await Api.request(`${API_BASE}/api/settings/ping`); } catch { /* keep the default chime */ }
  renderPingSettings();
}

async function pingCustomBuffer() {
  const url = pingSettings.custom?.url;
  if (!url) return null;
  if (pingBufferUrl !== url) {
    const response = await fetch(url);
    if (!response.ok) return null;
    pingBuffer = await audioContext.decodeAudioData(await response.arrayBuffer());
    pingBufferUrl = url;
  }
  return pingBuffer;
}

async function playCompletionPing(force = false) {
  try {
    unlockAudio(force);
    if (!audioContext || audioContext.state !== "running") return;
    const { sound, volume } = pingSettings;
    if (sound === "none" || volume <= 0) return;
    if (sound === "custom") {
      const buffer = await pingCustomBuffer();
      if (buffer) {
        const source = audioContext.createBufferSource();
        const gain = audioContext.createGain();
        source.buffer = buffer;
        gain.gain.value = volume;
        source.connect(gain).connect(audioContext.destination);
        source.start();
        return;
      }
    }
    const tone = PING_TONES[sound] || PING_TONES.chime;
    const peak = Math.max(0.0002, tone.level * (volume / 0.6));
    const now = audioContext.currentTime;
    tone.notes.forEach(([delay, frequency]) => {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = tone.type;
      oscillator.frequency.setValueAtTime(frequency, now + delay);
      if (tone.slide) oscillator.frequency.exponentialRampToValueAtTime(tone.slide, now + delay + tone.decay);
      gain.gain.setValueAtTime(0.0001, now + delay);
      gain.gain.exponentialRampToValueAtTime(peak, now + delay + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + tone.decay);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(now + delay);
      oscillator.stop(now + delay + tone.decay + 0.02);
    });
  } catch { /* Audio alerts are best-effort and require browser interaction. */ }
}

function renderPingSettings() {
  const select = $("#ping-sound");
  if (!select) return;
  const custom = pingSettings.custom;
  const option = $('option[value="custom"]', select);
  option.disabled = !custom;
  option.textContent = custom ? `Your sound: ${custom.name}` : "Your sound (upload one below)";
  select.value = pingSettings.sound;
  $("#ping-volume").value = String(pingSettings.volume);
  $("#ping-volume-readout").textContent = `${Math.round(pingSettings.volume * 100)}%`;
  $("#ping-upload").textContent = custom ? "Replace sound…" : "Upload sound…";
  $("#ping-remove").hidden = !custom;
}

async function savePingSettings(changes) {
  Object.assign(pingSettings, changes);
  renderPingSettings();
  try { pingSettings = await Api.request(`${API_BASE}/api/settings/ping`, { method: "PUT", body: changes }); }
  catch (error) { toast(error.message, "error"); }
  renderPingSettings();
}

function bindPingSettings() {
  $("#ping-sound").addEventListener("change", (event) => { savePingSettings({ sound: event.target.value }); playCompletionPing(true); });
  const volume = $("#ping-volume");
  volume.addEventListener("input", () => { $("#ping-volume-readout").textContent = `${Math.round(Number(volume.value) * 100)}%`; });
  volume.addEventListener("change", () => { savePingSettings({ volume: Number(volume.value) }); playCompletionPing(true); });
  $("#ping-test").addEventListener("click", () => playCompletionPing(true));
  $("#ping-upload").addEventListener("click", () => $("#ping-file").click());
  $("#ping-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const button = $("#ping-upload");
    button.disabled = true;
    button.textContent = "Uploading…";
    try {
      pingSettings = await Api.request(`${API_BASE}/api/settings/ping/sound`, { method: "POST", body });
      toast("Saved as the completion sound.");
      playCompletionPing(true);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; renderPingSettings(); }
  });
  $("#ping-remove").addEventListener("click", async () => {
    try { pingSettings = await Api.request(`${API_BASE}/api/settings/ping/sound`, { method: "DELETE" }); }
    catch (error) { toast(error.message, "error"); }
    renderPingSettings();
  });
}

function pushCompletionAlert(job) {
  if (!alertsEnabled) return;
  playCompletionPing();
  navigator.vibrate?.([120, 60, 120]);
  // With a push subscription the server sends the notification itself (same
  // tag), so showing one here too would just double up.
  if (pushSubscribed || notificationPermission() !== "granted") return;
  if (job.agentic_session_id) return;  // Agentic clips only get the session's milestone pushes
  const title = "Your video is ready";
  const options = {
    body: `${job.model_label || "Video"} · ${job.mode_label || "Generation"}\n${String(job.prompt || "Completed successfully").slice(0, 140)}`,
    tag: `h3-job-${job.id}`,
    icon: `${API_BASE}/static/icons/icon-192.png`,
    badge: `${API_BASE}/static/icons/badge-96.png`,
    data: { url: `${API_BASE}/?job=${job.id}` },
  };
  try {
    if (swRegistration) {
      swRegistration.showNotification(title, options).catch(() => {});
      return;
    }
    const notification = new Notification(title, options);
    notification.onclick = () => { window.focus(); showJob(job.id); notification.close(); };
  } catch { /* Some browsers restrict notifications despite granted permission. */ }
}

function detectCompletedJobs(jobs) {
  jobs.forEach((job) => {
    if (job.state !== "complete" || completedJobs.has(job.id)) return;
    completedJobs.add(job.id);
    pushCompletionAlert(job);
    if (isMobileLayout() && document.body.dataset.mobileTab !== "jobs") state.unseenCompletions += 1;
  });
}

/* -------------------------------------------------------- mobile tabs -- */
// On narrow screens the form and the Generations list are separate tabs, and
// only the active one is rendered: while on Create, job cards (and their
// videos) aren't built at all - renderJobs() just marks them stale.
const mobileQuery = window.matchMedia("(max-width: 900px)");
const tabScroll = { create: 0, jobs: 0 };
state.unseenCompletions = 0;
state.jobsStale = false;

function isMobileLayout() {
  return mobileQuery.matches;
}

function jobsVisible() {
  if (["best", "enhance", "postedit", "settings"].includes(document.body.dataset.view)) return false;
  return !isMobileLayout() || document.body.dataset.mobileTab === "jobs";
}

// Top tabs: Studio (the normal page) or Best settings. The leaderboard is
// only fetched while its tab is open, and the job cards are dropped while
// it is, so no videos stay loaded off-screen.
function setView(view) {
  if ((document.body.dataset.view || "studio") === view) return;
  if (document.body.dataset.view === "enhance" && enhanceSaveTimer) saveEnhancement(); // leaving mid-edit
  if (document.body.dataset.view === "postedit") leavePostEdit();
  document.body.dataset.view = view;
  $$("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#best-view").hidden = view !== "best";
  $("#enhance-view").hidden = view !== "enhance";
  $("#postedit-view").hidden = view !== "postedit";
  $("#settings-view").hidden = view !== "settings";
  try { sessionStorage.setItem("h3-view", view); } catch { /* per-tab convenience only */ }
  if (view === "best" || view === "enhance" || view === "postedit" || view === "settings") {
    $("#jobs-list").innerHTML = "";
    state.jobsStale = true;
    if (view === "best") loadBestSettings();
    else if (view === "enhance") loadEnhancement();
    else if (view === "settings") loadSettingsView();
    else loadPostEdit();
  }
  if (view !== "best") {
    $("#best-list").innerHTML = "";
    if (jobsVisible() && state.jobsStale) renderJobs();
  }
  window.scrollTo(0, 0);
}

function bindViewTabs() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("#best-refresh").addEventListener("click", loadBestSettings);
  let saved = null;
  try { saved = sessionStorage.getItem("h3-view"); } catch { /* ignore */ }
  if (["best", "enhance", "postedit", "settings"].includes(saved)) setView(saved);
}

async function loadBestSettings() {
  const list = $("#best-list");
  list.innerHTML = `<li class="best-empty">Loading…</li>`;
  try {
    const { items } = await Api.request(`${API_BASE}/api/leaderboard`);
    if (document.body.dataset.view !== "best") return; // left the tab meanwhile
    list.innerHTML = items.length
      ? items.map(bestSettingsRow).join("")
      : `<li class="best-empty">No rated videos yet. Rate finished generations with the thumbs to fill this list.</li>`;
  } catch (error) {
    list.innerHTML = `<li class="best-empty">Couldn't load: ${escapeHtml(error.message)}</li>`;
  }
}

function bestSettingsRow(item, index) {
  const total = item.good + item.bad;
  const percent = total ? Math.round(item.good / total * 100) : 0;
  const time = item.seconds == null
    ? `<span class="best-time is-empty">no timing</span>`
    : `<span class="best-time" title="Average of ${item.timed} timed render(s)">${uiIcon("timer")} ${formatSeconds(item.seconds)}</span>`;
  const chips = item.settings.map(([label, value]) =>
    `<span class="best-chip"><span>${escapeHtml(label)}</span>${escapeHtml(value)}</span>`).join("");
  return `<li class="best-row${item.legendary ? " is-legendary" : ""}">
    <div class="best-rank">#${index + 1}</div>
    <div class="best-main">
      <div class="best-title">
        <strong>${escapeHtml(item.model_label)}</strong>
        <span>${item.width}×${item.height}</span>
        ${time}
      </div>
      <div class="best-chips">${chips}</div>
      ${item.reasons.length ? `<div class="best-why"><span>${uiIcon("star")} Best because</span>${item.reasons.map(([label, count]) =>
        `<span class="best-reason-chip">${escapeHtml(label)}${count > 1 ? ` <b>×${count}</b>` : ""}</span>`).join("")}</div>` : ""}
    </div>
    <div class="best-score${item.legendary ? " is-legendary" : ""}" title="${item.good} of ${total} rated good, ${item.best} best quality possible">
      <strong>${item.score.toFixed(2)}</strong>
      <span>${item.best ? `${uiIcon("star")} ${item.best} · ` : ""}${uiIcon("thumbsUp")} ${item.good - item.best} · ${uiIcon("thumbsDown")} ${item.bad}</span>
      <span>${percent}% good</span>
    </div>
  </li>`;
}

function setMobileTab(tab) {
  const current = document.body.dataset.mobileTab;
  if (current === tab) return;
  tabScroll[current] = window.scrollY;
  document.body.dataset.mobileTab = tab;
  $$("[data-mobile-tab]").forEach((button) => {
    const active = button.dataset.mobileTab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  try { sessionStorage.setItem("h3-mobile-tab", tab); } catch { /* per-tab convenience only */ }
  if (tab === "jobs") {
    state.unseenCompletions = 0;
    if (state.jobsStale) renderJobs();
  } else {
    // Leaving Generations: drop its cards so no videos stay loaded off-screen.
    $("#jobs-list").innerHTML = "";
    state.jobsStale = true;
  }
  updateMobileTabBadge();
  window.scrollTo(0, tabScroll[tab] || 0);
}

function updateMobileTabBadge() {
  const running = state.jobs.filter((job) => isActiveJob(job) || needsAttention(job)).length;
  const badge = $("#mobile-jobs-badge");
  setText(badge, running ? String(running) : "");
  badge.classList.toggle("has-new", state.unseenCompletions > 0 && document.body.dataset.mobileTab !== "jobs");
}

function bindMobileTabs() {
  $$("[data-mobile-tab]").forEach((button) => button.addEventListener("click", () => setMobileTab(button.dataset.mobileTab)));
  let saved = null;
  try { saved = sessionStorage.getItem("h3-mobile-tab"); } catch { /* ignore */ }
  if (saved === "jobs") setMobileTab("jobs");
  // Rotating/resizing across the breakpoint: desktop shows both, so render the list.
  mobileQuery.addEventListener("change", () => { if (jobsVisible() && state.jobsStale) renderJobs(); });
}

async function toggleAlerts() {
  const permission = notificationPermission();
  if (alertsEnabled && permission !== "default") {
    alertsEnabled = false;
    localStorage.setItem("h3-completion-alerts", "off");
    await removePushSubscription();
    toast("Completion alerts disabled.");
    return updateAlertsButton();
  }
  alertsEnabled = true;
  localStorage.setItem("h3-completion-alerts", "on");
  unlockAudio();
  if (isIOS && !isInstalled && permission === "unsupported") {
    toast("Sound alerts are on. For notifications on iPhone: tap Share → Add to Home Screen, open Video from there, then tap the bell.");
    return updateAlertsButton();
  }
  const granted = permission === "granted" || (permission === "default" && await Notification.requestPermission() === "granted");
  if (!granted) {
    toast(permission === "denied"
      ? "Sound alerts are on. Notifications are blocked for this site - allow them in the browser's site settings."
      : "Sound alerts are on. Notifications were not allowed.");
    return updateAlertsButton();
  }
  await syncPushSubscription();
  if (pushSubscribed && BunnyHub._available) {
    BunnyHub.test();
    toast("Notifications on for this device - every BunnyBytes studio can reach you; pick events in Settings → Notifications. A test is on its way.");
  } else if (pushSubscribed) {
    const subscription = await swRegistration.pushManager.getSubscription();
    Api.request(`${API_BASE}/api/push/test`, { method: "POST", body: { endpoint: subscription.endpoint } }).catch(() => {});
    toast("Notifications on - you'll get one when a video finishes, even with the app closed. A test notification is on its way.");
  } else {
    toast("Notifications on while this page is open.");
  }
  updateAlertsButton();
}

/* --------------------------------------------------------- references -- */

function currentReference(id) {
  return state.references.find((reference) => reference.id === id);
}

// The backend publishes a fresh full reference list over SSE right after
// every upload, often before that same upload's own fetch() promise here
// resolves - so an uploader that just does array.push(...uploaded) can add
// a reference the SSE handler already delivered, showing every new upload
// twice until the next full-list replace (SSE event, or a page reload)
// happens to overwrite it away. Merging by id makes it idempotent either way.
function mergeReferences(newItems) {
  const byId = new Map(state.references.map((reference) => [reference.id, reference]));
  for (const item of newItems) byId.set(item.id, item);
  state.references = [...byId.values()];
}

// References are one global pool (see backend/store.py), not scoped per
// generation - filtering by MIME type alone means every widget's uploads
// bleed into every other widget that wants "all images" (e.g. an LTX 2.5
// keyframe upload showing up in a later SeedHunter submit). `role` matches
// the upload widget itself (see UPLOAD_ROLE_CONFIG) and is what actually
// scopes a reference to where it was meant to be used.
function referencesByRole(...roles) {
  const set = new Set(roles);
  return state.references.filter((reference) => set.has(reference.role));
}

// MiniMax H3's own reference-conditioning nodes read <Picture N>/<Video N>/
// <Audio N> tokens straight out of the prompt text - these helpers surface
// that exact canonical token per reference. `alias` (e.g. "image3") is
// assigned once at upload and never reused, so the number stays stable even
// if another reference of the same kind is deleted later; the backend
// re-maps it to whatever contiguous position MiniMax H3 actually needs at
// generation time (see backend/generation.py's alias_map), so nothing here
// has to track positions itself.
const REFERENCE_CATEGORY_LABELS = { image: "Picture", video: "Video", audio: "Audio" };
const REFERENCE_LIMITS = { image: 9, video: 3, audio: 3, videoSeconds: 15, audioSeconds: 15 };
// LTX 2.5 Reference mode (LiconStudio MSR V2, see backend/generation.py's
// validate_ltx_msr_references): pic1-pic4 + a background slot, and a voice
// for each of the first two images, each fitting one 5s audio slot.
const LTX_MSR_LIMITS = { image: 5, audio: 2, audioSeconds: 5 };

// Thumbnail for a reference token or FL2V keyframe tag (none for audio):
// images show themselves, videos their extracted first frame.
// `reference` is the suggestion's own attached reference (promptTags); the
// FL2V @tags fall back to the current keyframes.
function referenceThumb(token, reference = null) {
  const keyframe = { "@image1": state.firstFrameId, "@First": state.firstFrameId, "@image2": state.lastFrameId, "@Last": state.lastFrameId }[token];
  if (!reference && keyframe !== undefined) reference = currentReference(keyframe);
  if (!reference) return null;
  const category = referenceCategory(reference);
  return category === "image" ? reference.small_url || reference.preview_url : category === "video" ? reference.small_url || reference.thumbnail_url || null : null;
}

function referenceCategory(reference) {
  return (reference.mime_type || reference.category || "").split("/")[0];
}

function referenceStableNumber(reference) {
  const match = /(\d+)$/.exec(reference.alias || "");
  return match ? match[1] : null;
}

function referenceToken(reference) {
  // An audio-only video is sent to H3 as audio, so its tag is <Audio N> (the server re-aliases it).
  const label = REFERENCE_CATEGORY_LABELS[reference.audio_only ? "audio" : referenceCategory(reference)];
  const number = referenceStableNumber(reference);
  return label && number ? `<${label} ${number}>` : null;
}

// The trimmed length actually sent to generation (see backend/generation.py's
// effective_duration()) - not the raw upload length. Card text and the
// combined-duration totals/validation all need this, not reference.duration,
// or a trimmed-down clip still reads (and counts against the 15s cap) as if
// it were full length.
function effectiveDuration(reference) {
  const duration = reference.duration;
  if (duration == null) return null;
  const start = Math.max(0, reference.trim_start || 0);
  const end = reference.trim_end != null ? Math.min(reference.trim_end, duration) : duration;
  return Math.max(0, end - start);
}

// Keep a <video>/<audio> preview inside the reference's trim window (the part
// generation actually gets): start at trim_start, loop back (or stop) at
// trim_end, and pull a scrub outside the window back in. Safe to call again on
// a reused element (the hover preview) - the window is just replaced.
function playTrimmed(media, reference, { loop = true } = {}) {
  const start = Math.max(0, reference.trim_start || 0);
  media.peTrim = { start, end: reference.trim_end ?? null, loop };
  const toStart = () => { if (media.peTrim && Math.abs(media.currentTime - media.peTrim.start) > 0.05) media.currentTime = media.peTrim.start; };
  if (!media.peTrimBound) {
    media.peTrimBound = true;
    media.loop = false; // the window loops itself
    const check = () => {
      const window = media.peTrim;
      if (!window) return;
      const end = window.end ?? media.duration;
      if (media.currentTime < window.start - 0.1) media.currentTime = window.start;
      else if (Number.isFinite(end) && media.currentTime >= end - 0.02) {
        media.currentTime = window.start;
        if (!window.loop) media.pause();
      }
    };
    const watch = () => {
      check();
      if (!media.paused && media.isConnected) requestAnimationFrame(watch); // frame-accurate while playing
    };
    media.addEventListener("play", () => requestAnimationFrame(watch));
    media.addEventListener("seeked", check);
    media.addEventListener("ended", () => {
      if (!media.peTrim) return;
      media.currentTime = media.peTrim.start;
      if (media.peTrim.loop) media.play().catch(() => {});
    });
    media.addEventListener("loadedmetadata", () => { if (media.peTrim) media.currentTime = media.peTrim.start; });
  }
  if (media.readyState >= 1) toStart();
}

// "0:02.0-0:06.5 of 0:12.0" for a trimmed video/audio reference, else "".
function trimLabel(reference) {
  const duration = reference.duration;
  const start = reference.trim_start || 0;
  const end = reference.trim_end;
  if (duration == null || (!start && (end == null || end >= duration - 0.05))) return "";
  const time = (seconds) => `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  return `trimmed ${time(start)}–${time(end ?? duration)} of ${time(duration)}`;
}

// Custom player for a trimmed reference (replaces the browser's controls in the
// fullscreen prompt's reference viewer): the scrub bar spans only the trimmed
// part, times are relative to it, and audio gets a waveform scrubber. Built on
// playTrimmed, which keeps playback inside the window.
const TRIM_PLAYER_SPEEDS = [0.25, 0.5, 1, 1.5, 2];

function mountTrimPlayer(host, media, reference, { loop = true, waveform = false } = {}) {
  playTrimmed(media, reference, { loop });
  const bar = document.createElement("div");
  bar.className = `trim-player${waveform ? " has-wave" : ""}`;
  bar.innerHTML = `
    <button type="button" class="tp-button tp-play" aria-label="Play" title="Play / pause (Space)">${uiIcon("play")}</button>
    <div class="tp-scrub" role="slider" tabindex="0" aria-label="Position" aria-valuemin="0">
      ${waveform ? `<canvas class="tp-wave"></canvas>` : `<div class="tp-track"></div>`}
      <div class="tp-fill"></div><div class="tp-head"></div>
      <span class="tp-hover" hidden></span>
    </div>
    <span class="tp-time">0:00.0 / 0:00.0</span>
    <button type="button" class="tp-button tp-speed" title="Playback speed">1x</button>
    <button type="button" class="tp-button tp-loop${loop ? " is-on" : ""}" title="Loop the trimmed part" aria-pressed="${loop}">${uiIcon("repeat")}</button>
    <button type="button" class="tp-button tp-mute" title="Mute / unmute (M)">${uiIcon(media.muted ? "volumeOff" : "volume")}</button>
    <input class="tp-volume" type="range" min="0" max="1" step="0.05" value="${media.muted ? 0 : media.volume}" aria-label="Volume">`;
  host.append(bar);
  const q = (selector) => bar.querySelector(selector);
  const window_ = () => {
    const start = media.peTrim?.start || 0;
    const end = media.peTrim?.end ?? (media.duration || reference.duration || start + 1);
    return { start, end, length: Math.max(0.01, end - start) };
  };
  const clock = (seconds) => `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  const render = () => {
    const { start, length } = window_();
    const local = Math.max(0, Math.min(length, media.currentTime - start));
    const percent = local / length * 100;
    q(".tp-fill").style.width = `${percent}%`;
    q(".tp-head").style.left = `${percent}%`;
    q(".tp-time").textContent = `${clock(local)} / ${clock(length)}`;
    q(".tp-scrub").setAttribute("aria-valuenow", local.toFixed(1));
    q(".tp-scrub").setAttribute("aria-valuemax", length.toFixed(1));
    const playing = !media.paused;
    q(".tp-play").innerHTML = uiIcon(playing ? "pause" : "play");
    q(".tp-play").setAttribute("aria-label", playing ? "Pause" : "Play");
  };
  const tick = () => { render(); if (!media.paused && bar.isConnected) requestAnimationFrame(tick); };
  ["play", "pause", "seeked", "loadedmetadata", "timeupdate"].forEach((type) => media.addEventListener(type, () => {
    render();
    if (type === "play") requestAnimationFrame(tick);
  }));
  const toggle = () => (media.paused ? media.play().catch(() => {}) : media.pause());
  q(".tp-play").addEventListener("click", toggle);
  if (media.tagName === "VIDEO") media.addEventListener("click", toggle);

  // Scrubbing (click or drag), in trimmed-window time.
  const scrub = q(".tp-scrub");
  const timeAt = (event) => {
    const rect = scrub.getBoundingClientRect();
    const { start, length } = window_();
    return start + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * length;
  };
  scrub.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    scrub.setPointerCapture(event.pointerId);
    const wasPlaying = !media.paused;
    media.pause();
    media.currentTime = timeAt(event);
    const move = (e) => { media.currentTime = timeAt(e); render(); };
    const up = () => {
      scrub.removeEventListener("pointermove", move);
      scrub.removeEventListener("pointerup", up);
      if (wasPlaying) media.play().catch(() => {});
    };
    scrub.addEventListener("pointermove", move);
    scrub.addEventListener("pointerup", up);
  });
  scrub.addEventListener("pointermove", (event) => {
    const hover = q(".tp-hover");
    const rect = scrub.getBoundingClientRect();
    hover.hidden = false;
    hover.style.left = `${Math.max(0, Math.min(rect.width, event.clientX - rect.left))}px`;
    hover.textContent = clock(timeAt(event) - window_().start);
  });
  scrub.addEventListener("pointerleave", () => { q(".tp-hover").hidden = true; });
  scrub.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 1 : 1 / 24; // a frame, or a second with Shift
    if (event.key === "ArrowRight") media.currentTime = Math.min(window_().end, media.currentTime + step);
    else if (event.key === "ArrowLeft") media.currentTime = Math.max(window_().start, media.currentTime - step);
    else return;
    event.preventDefault();
    media.pause();
  });

  q(".tp-speed").addEventListener("click", (event) => {
    const next = TRIM_PLAYER_SPEEDS[(TRIM_PLAYER_SPEEDS.indexOf(media.playbackRate) + 1) % TRIM_PLAYER_SPEEDS.length] || 1;
    media.playbackRate = next;
    event.currentTarget.textContent = `${next}x`;
  });
  q(".tp-loop").addEventListener("click", (event) => {
    const on = !media.peTrim.loop;
    media.peTrim.loop = on;
    event.currentTarget.classList.toggle("is-on", on);
    event.currentTarget.setAttribute("aria-pressed", String(on));
  });
  const volume = q(".tp-volume");
  const syncVolume = () => {
    q(".tp-mute").innerHTML = uiIcon(media.muted || media.volume === 0 ? "volumeOff" : "volume");
    volume.value = media.muted ? 0 : media.volume;
  };
  q(".tp-mute").addEventListener("click", () => {
    media.muted = !media.muted;
    if (!media.muted && media.volume === 0) media.volume = 1;
    syncVolume();
  });
  volume.addEventListener("input", () => { media.volume = Number(volume.value); media.muted = media.volume === 0; syncVolume(); });
  media.addEventListener("volumechange", syncVolume);

  // Space / M while the pointer is over this reference (not while typing in the prompt).
  // (host outlives this player - the stage is reused - so the handler drops itself once the player is gone)
  const onKey = (event) => {
    if (!bar.isConnected) { host.removeEventListener("keydown", onKey); return; }
    if (event.target.closest("textarea, input:not(.tp-volume)")) return;
    if (event.code === "Space") { event.preventDefault(); toggle(); }
    else if (event.key.toLowerCase() === "m") q(".tp-mute").click();
  };
  host.addEventListener("keydown", onKey);

  if (waveform) {
    const draw = async () => {
      const data = await peWaveform(reference);
      const canvas = q(".tp-wave");
      if (!data || !canvas.isConnected) return;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = canvas.clientWidth * ratio;
      canvas.height = canvas.clientHeight * ratio;
      const ctx = canvas.getContext("2d");
      const { start, end } = window_();
      const first = Math.floor(start / data.duration * data.peaks.length);
      const last = Math.max(first + 1, Math.ceil(Math.min(end, data.duration) / data.duration * data.peaks.length));
      const peaks = data.peaks.slice(first, last);
      const barWidth = canvas.width / peaks.length, mid = canvas.height / 2;
      ctx.fillStyle = "rgba(165, 140, 240, 0.7)";
      peaks.forEach((peak, index) => {
        const h = Math.max(ratio, peak * (canvas.height - 4 * ratio));
        ctx.fillRect(index * barWidth, mid - h / 2, Math.max(ratio, barWidth - ratio * 0.4), h);
      });
    };
    requestAnimationFrame(draw);
  }
  render();
  return bar;
}

function renderKeyframes() {
  renderFrameSlot("first", currentReference(state.firstFrameId));
  renderFrameSlot("last", currentReference(state.lastFrameId));
  renderFrameSlot("sh-first", currentReference(state.shFirstFrameId));
  renderFrameSlot("sh-last", currentReference(state.shLastFrameId));
}

function renderFrameSlot(role, reference) {
  const preview = $(`#${role}-frame-preview`);
  const removeButton = $(`#${role}-frame-remove`);
  preview.hidden = !reference;
  if (reference) preview.src = reference.small_url || reference.preview_url; // a 64 px square
  removeButton.hidden = !reference;
  if (role === "first") {
    $("#first-frame-match").hidden = !reference || state.model !== "ltx_2_5";
  }
  const isSeedhunter = role.startsWith("sh-");
  const pairIds = [
    isSeedhunter ? state.shFirstFrameId : state.firstFrameId,
    isSeedhunter ? state.shLastFrameId : state.lastFrameId,
  ].filter(Boolean);
  preview.onclick = () => {
    if (!pairIds.length) return;
    const index = pairIds.indexOf(reference ? reference.id : null);
    openViewer(pairIds, index === -1 ? 0 : index);
  };
}

// LTX 2.5 Reference = MSR V2: the backend turns each <Picture N>/<Audio N>
// into the positional "Image k"/"Audio k" the LoRA was trained on (upload
// order; the 5th image is the scene/background slot), and Audio k is always
// the voice of Image k. Returns id -> {slot, note}, or null outside MSR.
function msrSlots() {
  if (state.model !== "ltx_2_5" || state.mode !== "ref2v") return null;
  const all = referencesByRole("references");
  const images = all.filter((reference) => referenceCategory(reference) === "image");
  const audios = all.filter((reference) => referenceCategory(reference) === "audio");
  const slots = new Map();
  images.forEach((reference, index) => slots.set(reference.id, {
    slot: `Image ${index + 1}`, note: index === 4 ? "scene / background" : "", token: referenceToken(reference),
  }));
  audios.forEach((reference, index) => {
    const partner = images[index];
    slots.set(reference.id, {
      slot: `Audio ${index + 1}`, token: referenceToken(reference),
      note: partner ? `voice of ${referenceToken(partner)}` : "no matching image",
      partnerToken: partner ? referenceToken(partner) : null,
    });
  });
  return { slots, images: images.map((reference) => slots.get(reference.id)), audios: audios.map((reference) => slots.get(reference.id)) };
}

function renderReferenceRows(listId, references) {
  const list = $(listId);
  const ids = references.map((reference) => reference.id);
  const msr = listId === "#reference-list" ? msrSlots() : null;
  list.innerHTML = references.map((reference) => {
    const slot = msr?.slots.get(reference.id);
    const msrBadge = slot
      ? `<span class="msr-slot" title="MSR sees ${escapeHtml(referenceToken(reference) || "")} as &quot;${slot.slot}&quot;">&rarr; ${slot.slot}${slot.note ? ` &middot; ${escapeHtml(slot.note)}` : ""}</span>`
      : "";
    const token = referenceToken(reference) || `@${reference.alias}`;
    const eff = effectiveDuration(reference);
    const isTrimmed = eff != null && Math.abs(eff - reference.duration) > 0.05;
    const duration = eff != null
      ? `<span class="reference-file-name">${eff.toFixed(1)}s${isTrimmed ? ` (of ${reference.duration.toFixed(1)}s)` : ""}</span>`
      : "";
    const category = referenceCategory(reference);
    // Images render their own bytes; videos get their extracted first-frame
    // thumbnail (the raw video file can't be used as an <img> src). Audio,
    // or a video whose thumbnail extraction failed, falls back to an icon.
    const thumbSrc = category === "image" ? reference.small_url || reference.preview_url : reference.small_url || reference.thumbnail_url;
    const preview = thumbSrc
      ? `<img class="reference-thumb" data-view-reference="${reference.id}" src="${thumbSrc}" alt="">`
      : `<button type="button" class="reference-thumb reference-thumb-icon" data-view-reference="${reference.id}" aria-label="Preview">${uiIcon(category === "video" ? "play" : "music")}</button>`;
    return `
    <div class="reference-row" data-reference-id="${reference.id}">
      ${preview}
      <div class="reference-info">
        <button type="button" class="reference-token" data-category="${category}" data-insert-token="${escapeHtml(token)}" title="Insert ${escapeHtml(token)} into the prompt">${escapeHtml(token)}</button>
        ${msrBadge}
        ${duration}
        ${category === "video" && listId === "#reference-list" && state.model === "minimax_h3" ? `<label class="reference-audio-only" title="Use this video’s audio as a reference, without its frames"><input type="checkbox" data-audio-only-reference="${reference.id}" ${reference.audio_only ? "checked" : ""} ${!reference.has_audio || reference.muted ? "disabled" : ""}> Audio only</label>` : ""}
        ${category === "video" ? `<span class="reference-match">
          <button type="button" class="button ghost" data-match-duration="${reference.id}" title="Set Duration to this video's ${eff != null ? `${eff.toFixed(1)}s ` : ""}length">&#9201; Match length</button>
          <button type="button" class="button ghost" data-match-size="${reference.id}" title="Set Resolution to this video's size">&#10530; Match size</button>
        </span>` : ""}
      </div>
      <button type="button" class="icon-button" data-remove-reference="${reference.id}" aria-label="Remove ${escapeHtml(token)}">&#10005;</button>
    </div>`;
  }).join("");
  $$('[data-audio-only-reference]', list).forEach((input) => input.addEventListener("change", () => toggleReferenceAudioOnly(input.dataset.audioOnlyReference, input.checked)));
  $$('[data-remove-reference]', list).forEach((button) => button.addEventListener("click", () => removeReference(button.dataset.removeReference)));
  $$('[data-insert-token]', list).forEach((button) => button.addEventListener("click", () => insertTokenAtCursor(button.dataset.insertToken)));
  $$('[data-match-duration]', list).forEach((button) => button.addEventListener("click", () => matchDurationToVideo(button.dataset.matchDuration)));
  $$('[data-match-size]', list).forEach((button) => button.addEventListener("click", () => matchSizeToVideo(button.dataset.matchSize)));
  $$('[data-view-reference]', list).forEach((el) => el.addEventListener("click", () => openViewer(ids, ids.indexOf(el.dataset.viewReference))));
}

function renderReferenceGallery() {
  const all = referencesByRole("references");
  const images = all.filter((reference) => referenceCategory(reference) === "image");
  const videos = all.filter((reference) => referenceCategory(reference) === "video");
  const audios = all.filter((reference) => referenceCategory(reference) === "audio");
  const ltx = state.model === "ltx_2_5";
  $("#reference-hint").hidden = !ltx;
  renderPromptSnippets(); // its References group lists these tokens
  scheduleEstimate(); // reference counts are part of the estimate's settings
  if (ltx) {
    $("#reference-count").innerHTML = [
      `Images: ${images.length}/${LTX_MSR_LIMITS.image} (Image 5 = background/scene)`,
      `Voices: ${audios.length}/${LTX_MSR_LIMITS.audio} (≤${LTX_MSR_LIMITS.audioSeconds}s each)`,
      ...(videos.length ? [`Videos aren't used in LTX Reference mode - remove ${videos.length}`] : []),
    ].map((line) => `<span class="reference-total">${escapeHtml(line)}</span>`).join("");
    renderReferenceRows("#reference-list", all);
    return;
  }
  const voiceVideos = videos.filter((reference) => reference.audio_only);
  const videoSeconds = videos.filter((reference) => !reference.audio_only).reduce((sum, reference) => sum + (effectiveDuration(reference) || 0), 0);
  const audioSeconds = audios.reduce((sum, reference) => sum + (effectiveDuration(reference) || 0), 0) + voiceVideos.reduce((sum, reference) => sum + Math.min(5, effectiveDuration(reference) || 0), 0);
  const maxTotal = REFERENCE_LIMITS.image + REFERENCE_LIMITS.video + REFERENCE_LIMITS.audio;
  $("#reference-count").innerHTML = [
    `Total references: ${all.length}/${maxTotal}`,
    `Images: ${images.length}/${REFERENCE_LIMITS.image}`,
    `Video references: ${videoSeconds.toFixed(1)} / ${REFERENCE_LIMITS.videoSeconds.toFixed(1)} sec (${videos.length}/${REFERENCE_LIMITS.video}; ${voiceVideos.length} audio only)`,
    `Audio references: ${audioSeconds.toFixed(1)} / ${REFERENCE_LIMITS.audioSeconds.toFixed(1)} sec (${audios.length + voiceVideos.length}/${REFERENCE_LIMITS.audio})`,
  ].map((line) => `<span class="reference-total">${escapeHtml(line)}</span>`).join("");
  renderReferenceRows("#reference-list", all);
}

function renderSeedhunterGalleries() {
  const images = referencesByRole("sh-images");
  const videos = referencesByRole("sh-videos");
  const audios = referencesByRole("sh-audios");
  $("#sh-image-count").textContent = `${images.length}/6`;
  $("#sh-video-count").textContent = `${videos.length}/3`;
  $("#sh-audio-count").textContent = `${audios.length}/3`;
  renderReferenceRows("#sh-image-list", images);
  renderReferenceRows("#sh-video-list", videos);
  renderReferenceRows("#sh-audio-list", audios);
  const total = images.length + videos.length + audios.length + (state.shFirstFrameId ? 1 : 0) + (state.shLastFrameId ? 1 : 0);
  $("#sh-reference-summary").textContent = total ? `References (${total} attached)` : "References — optional";
  renderPromptSnippets(); // its References group lists these tokens
  scheduleEstimate(); // reference counts are part of the estimate's settings
}

async function toggleReferenceAudioOnly(id, enabled) {
  try {
    const before = state.references.find((reference) => reference.id === id);
    const oldToken = before && referenceToken(before);
    const updated = await Api.patchReference(id, { audio_only: enabled });
    mergeReferences([updated]);
    const newToken = referenceToken(updated);
    // Rename the clip's tag in the prompt too (<Video 2> -> <Audio 3>), whatever spacing/case it was typed with.
    const match = oldToken && /^<(\w+) (\d+)>$/.exec(oldToken);
    if (match && newToken && newToken !== oldToken) {
      const pattern = new RegExp(`<\\s*${match[1]}\\s+${match[2]}\\s*>`, "gi");
      const prompt = $("#prompt");
      if (pattern.test(prompt.value)) {
        prompt.value = prompt.value.replace(pattern, newToken);
        toast(`${oldToken} is now ${newToken} in the prompt.`);
      }
    }
    renderReferenceGallery();
  } catch (error) { toast(error.message, "error"); renderReferenceGallery(); }
}

function insertTokenAtCursor(token) {
  const textarea = $("#prompt");
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const needsSpace = start > 0 && !/\s$/.test(textarea.value.slice(0, start));
  textarea.setRangeText(`${needsSpace ? " " : ""}${token}`, start, end, "end");
  textarea.focus();
  closeAliasMenu();
}

/* ------------------------------------------------- prompt highlight -- */
// The prompt textarea's own text is transparent; this colored copy sits
// behind it. Highlight spans may only change color/background (never
// weight, size or padding) or the two layers drift out of alignment.
// Reference (full-reference / ref2va) vocabulary - fixed values from the
// Full-Reference Mode Rewrite Output Format Guide.
const H3_TASK_TYPES = ["keyframe completion", "reference generation", "video editing", "video continuation", "audio reuse", "audio reference"];
const H3_RETENTION_MARKERS = ["fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference", "fully_copy", "partially_copy"];
// Agentic finishing triggers, any casing - mirrors AGENTIC_FINISH_PATTERNS in app.py.
const anyCase = (word) => word.replace(/[a-z]/g, (c) => `[${c}${c.toUpperCase()}]`);
const FINISH_TRIGGER_SOURCE = String.raw`\b${anyCase("post")}[\s_-]?${anyCase("edit")}\w*|\b${anyCase("stitch")}(?:${anyCase("ed")}|${anyCase("es")}|${anyCase("ing")})?\b`;
const FINISH_TRIGGER_RE = new RegExp(FINISH_TRIGGER_SOURCE, "g");
const FINISH_SUGGESTIONS = [
  { token: "post edit", label: "Agentic: Post-Edit the final clips (voiceover, captions, text, music, trims)" },
  { token: "stitch", label: "Agentic: stitch the final clips into one video" },
];
// "post", "post-e", "stit"... -> the trigger words they start (4+ letters typed).
const FINISH_FRAGMENT = /(?:^|[^\w-])((?:po|st)[a-z _-]{2,7})$/i;
function finishSuggestions(fragment) {
  const typed = fragment.toLowerCase().replace(/[_-]/g, " ");
  return FINISH_SUGGESTIONS.filter((entry) => entry.token.startsWith(typed) && entry.token !== typed)
    .map((entry) => ({ ...entry, insert: `${entry.token} §`, kind: "finish", category: "finish" }));
}

const PROMPT_SYNTAX = new RegExp([
  // H3 dialogue: <d>[English] spoken words</d> (unclosed: to end of line)
  String.raw`(?<dialogue><d>)(?<lang>\s*\[[^\]\n]*\])?(?<speech>.*?)(?<dialogueEnd><\/d>|$)`,
  String.raw`(?<ref><\s*(?:[Pp]icture|[Ii]mage|[Vv]ideo|[Aa]udio|[Ss]ubject)\s+\d+\s*>)`,
  String.raw`(?<legacy>@(?:image1|image2|First|Last)\b)`,
  String.raw`(?<control><scenetrans>|<cutoff>|\[unclear\])`,
  String.raw`(?<finish>${FINISH_TRIGGER_SOURCE})`,
  // Reference-mode summary prefix: [video editing + audio reuse]
  String.raw`(?<task>\[(?:${H3_TASK_TYPES.join("|")})(?: \+ (?:${H3_TASK_TYPES.join("|")}))*\])`,
  // retention_analysis markers; the bare word "reference" only as in "<Audio 1>: reference - ..."
  String.raw`(?<marker>\b(?:${H3_RETENTION_MARKERS.join("|")})\b|(?<=>:[ \t]*)reference(?=[ \t]+-))`,
  String.raw`(?<bare>\b(?:Image|Picture|Audio|Video|Subject) \d+\b)`,
  // Official camera wording (orange): "The camera pans left", "Arc Shot", "POV", amplitude, speed.
  String.raw`(?<camera>\bwith (?:small|large) amplitude\b|\bat (?:slow|fast) speed\b|\b[Tt]he camera (?:\w+ly )?(?:pushes in|pulls out|zooms (?:in|out)|pans (?:left|right)|trucks (?:left|right)|tilts (?:up|down)|pedestals (?:up|down)|arcs(?: around)?|tracks|holds a static shot|shakes (?:slightly|strongly)|rolls (?:counter)?clockwise)\b|\b(?:Push In|Pull Out|Zoom In|Zoom Out|Pan Left|Pan Right|Truck Left|Truck Right|Tilt Up|Tilt Down|Pedestal Up|Pedestal Down|Arc Shot|Tracking Shot|Static Shot|Shake Slightly|Shake Strongly|Roll Clockwise|Roll Counterclockwise)\b|\bPOV(?: shot)?\b)`,
  // Official composition (green) - the shot sizes used in the guide's examples; whole hyphenated words only.
  String.raw`(?<framing>(?<![\w-])(?:[Ee]xtreme [Cc]lose-[Uu]p|[Cc]lose-[Uu]p|[Cc]lose [Ss]hot|[Mm]edium-[Ww]ide [Ss]hot|[Mm]edium [Ss]hot)(?![\w-]))`,
  // H3 section keys (integrated_multimodal_description:, summary:) at line start
  String.raw`(?<key>^(?:[a-z]+(?:_[a-z]+)+|summary):)`,
  // LTX-style speaker lines (ARTIST: ...) at line start
  String.raw`(?<speaker>^[A-Z][A-Z0-9 .'-]*[A-Z0-9]:)`,
  String.raw`(?<shot>\[Shot\s*\d+\])`,
  String.raw`(?<speakerId>\(S\d+(?:\s*,\s*S\d+)*\))`,
  String.raw`(?<time>\b\d{1,2}:\d{2}(?:\.\d{1,3})?\b|\b\d+(?:\.\d+)?(?=[- ]seconds?\b))`,
  String.raw`(?<quote>“[^”\n]*”?|"[^"\n]*"?)`,
].join("|"), "gm");

function tokenCategory(token) {
  return /video/i.test(token) ? "video" : /audio/i.test(token) ? "audio" : /subject/i.test(token) ? "subject" : "image";
}

// LoRA trigger words for the current model: those of enabled LoRAs light up
// (.hl-trigger), those of LoRAs that are off are dimmed (.hl-trigger-off) so a
// trigger left in the prompt for a LoRA that isn't loaded stands out.
// Triggers are cleaned like the hotbar's: "SHW_AKIRA (Akira)" -> SHW_AKIRA,
// "char_<firstname> (...)" matches char_ + any word, instructions are skipped.
function triggerPattern(trigger) {
  if (/start with|\bprompt with\b/i.test(trigger)) return null;
  const word = trigger.replace(/\s*\(.*\)\s*$/, "").replace(/[\s,.:;]+$/, "").trim();
  if (word.length < 3) return null;
  return word.split(/(<[^<>]+>)/).map((part, i) => (i % 2 ? "[\\p{L}\\p{N}]+" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))).join("");
}

// Aliases (openAliasDialog) join the same matcher: an alias lights up with
// its LoRA's on/off state, and generate() swaps in its target keyword.
const aliasFamily = (model = state.model) => (listLoras(model) ? model : "minimax_h3");
const escapeRegex = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
let aliasVersion = 0; // bumped when state.triggerAliases changes

let triggerMatcher = { signature: null, regex: null, enabled: [], loras: [], aliases: [] };
function loraTriggerMatcher() {
  // One pass over the list's checkboxes: this runs on every keystroke, and a
  // selector query per LoRA was most of the typing lag on long prompts.
  const loraList = listLoras();
  const enabled = new Map();
  $$("[data-lora-enabled]", $(loraList ? loraList.list : "#minimax-lora-list")).forEach((box) => {
    const row = box.closest(loraList ? "[data-ltx-lora]" : "[data-lora-index]");
    if (row) enabled.set(loraList ? row.dataset.ltxLora : row.dataset.loraIndex, box.checked);
  });
  const rows = !state.options ? [] : loraList
    ? (state.options[loraList.key] || []).map((lora) => [lora, enabled.get(lora.filename)])
    : (state.options.minimax_h3_lora_slots || []).map((lora, index) => [lora, enabled.get(String(index))]);
  const signature = `${state.model}:${aliasVersion}:${rows.map(([, on]) => (on ? 1 : 0)).join("")}`;
  if (signature === triggerMatcher.signature) return triggerMatcher;
  const keyOf = (lora, index) => (loraList ? lora.filename : index);
  // lower-cased pattern -> { pattern, on: enabled by any LoRA, loras: [{ key, label }], alias }
  const patterns = new Map();
  rows.forEach(([lora, on], index) => (lora.triggers || []).forEach((trigger) => {
    const pattern = triggerPattern(trigger);
    if (!pattern) return;
    const entry = patterns.get(pattern.toLowerCase()) || { pattern, on: false, loras: [], alias: null };
    entry.on ||= Boolean(on);
    const key = keyOf(lora, index);
    if (!entry.loras.some((item) => item.key === key)) entry.loras.push({ key, label: lora.label });
    patterns.set(pattern.toLowerCase(), entry);
  }));
  // An alias wins over a trigger spelled the same way.
  (state.triggerAliases?.[aliasFamily()] || []).forEach(({ alias, lora: filename, target }) => {
    const index = rows.findIndex(([lora]) => lora.filename === filename);
    if (index < 0) return;
    const [lora, on] = rows[index];
    const pattern = alias.split(/\s+/).map(escapeRegex).join("\\s+");
    patterns.set(pattern.toLowerCase(), { pattern, on: Boolean(on), loras: [{ key: keyOf(lora, index), label: lora.label }], alias: { alias, target } });
  });
  // Unofficial camera moves / shot compositions (no LoRA): shared by every model.
  Object.keys(UNOFFICIAL_FAMILIES).forEach((family) => (state.triggerAliases?.[family] || []).forEach(({ alias, target }) => {
    const pattern = alias.split(/\s+/).map(escapeRegex).join("\\s+");
    patterns.set(pattern.toLowerCase(), { pattern, on: true, loras: [], alias: { alias, target, camera: true, family } });
  }));
  // Longest first, so a phrase wins over a word inside it.
  const list = [...patterns.values()].sort((a, b) => b.pattern.length - a.pattern.length);
  triggerMatcher = {
    signature,
    // "-" counts as part of a word: "wide shot" must not match inside "medium-wide shot".
    regex: list.length ? new RegExp(`(?<![\\p{L}\\p{N}_-])(?:${list.map(({ pattern }) => `(${pattern})`).join("|")})(?![\\p{L}\\p{N}_-])`, "giu") : null,
    enabled: list.map((entry) => entry.on),
    loras: list.map((entry) => entry.loras),
    aliases: list.map((entry) => entry.alias),
  };
  return triggerMatcher;
}

// The prompt as sent on Generate: every alias (outside dialogue, quotes and
// other syntax - exactly where it's highlighted) replaced by its keyword.
function expandAliases(text) {
  const { regex, aliases } = loraTriggerMatcher();
  if (!regex || !aliases.some(Boolean)) return text;
  // A camera move's wording ("The camera pans…") is capitalized at the start
  // of a sentence or shot and lower-cased mid-sentence ("…, the camera pans…").
  const sentenceStart = (before) => /(^|[.!?]["”']?\s+|\n\s*|\]\s*|:\s*)$/.test(before);
  const expandPlain = (plain, base) => plain.replace(regex, (...args) => {
    const groups = args.slice(1, 1 + aliases.length);
    const alias = aliases[groups.findIndex((group) => group !== undefined)];
    if (!alias) return args[0];
    if (!alias.camera) return alias.target;
    const before = text.slice(0, base + args[1 + aliases.length]);
    // "a two shot" -> "a two-character medium shot", not "a a …" (camera clauses keep "the camera")
    const target = alias.family !== "camera" && /\b(a|an|the)\s+$/i.test(before) ? alias.target.replace(/^(a|an|the)\s+/i, "") : alias.target;
    const first = sentenceStart(before) ? target[0].toUpperCase() : target[0].toLowerCase();
    return first + target.slice(1);
  });
  let out = "";
  let last = 0;
  for (const match of text.matchAll(PROMPT_SYNTAX)) {
    if (!match[0]) { PROMPT_SYNTAX.lastIndex++; continue; }
    out += expandPlain(text.slice(last, match.index), last) + match[0];
    last = match.index + match[0].length;
  }
  // A camera move is a whole clause: "and a dolly zoom" -> "and the camera pushes in…",
  // so an article left in front of the expanded "the camera" is dropped.
  return (out + expandPlain(text.slice(last), last)).replace(/\b(?:an?|the)\s+(the camera\b)/gi, "$1");
}

// Plain text between the other highlights: escaped, with LoRA triggers wrapped.
// `offset` is where `text` starts in the prompt; the spans carry their range
// (data-from/-to, for the caret) and pattern (data-trigger, for the tip).
function highlightPlain(text, offset) {
  const { regex, enabled, aliases } = loraTriggerMatcher();
  if (!regex || !text) return escapeHtml(text);
  let html = "";
  let last = 0;
  for (const match of text.matchAll(regex)) {
    const index = match.slice(1).findIndex((group) => group !== undefined);
    const from = offset + match.index;
    const cls = aliases[index]?.camera ? "hl-camera-alias" : `hl-trigger${enabled[index] ? "" : "-off"}`;
    html += escapeHtml(text.slice(last, match.index))
      + `<span class="${cls}" data-trigger="${index}" data-from="${from}" data-to="${from + match[0].length}">${escapeHtml(match[0])}</span>`;
    last = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(last));
}

function highlightPrompt(text) {
  let html = "";
  let last = 0;
  for (const match of text.matchAll(PROMPT_SYNTAX)) {
    if (!match[0]) { PROMPT_SYNTAX.lastIndex++; continue; }
    html += highlightPlain(text.slice(last, match.index), last);
    last = match.index + match[0].length;
    const g = match.groups;
    const span = (cls, value, extra = "") => `<span class="hl-${cls}"${extra}>${escapeHtml(value)}</span>`;
    if (g.dialogue) {
      html += span("tag", g.dialogue) + (g.lang ? span("lang", g.lang) : "") + g.speech.split(/(<scenetrans>|<cutoff>|\[unclear\])/).map((part, i) => part && span(i % 2 ? "tag" : "speech", part)).join("") + (g.dialogueEnd ? span("tag", g.dialogueEnd) : "");
    } else if (g.ref) html += span("ref", g.ref, ` data-category="${tokenCategory(g.ref)}"`);
    else if (g.legacy) html += span("ref", g.legacy, ` data-category="image"`);
    else if (g.control) html += span("tag", g.control);
    else if (g.finish) html += span("finish", g.finish, ` title="Agentic runs this after the final clips render"`);
    else if (g.task) html += span("task", g.task);
    else if (g.marker) html += span("marker", g.marker);
    else if (g.bare) html += span("ref", g.bare, ` data-category="${tokenCategory(g.bare)}"`);
    else if (g.camera) html += span("camera", g.camera);
    else if (g.framing) html += span("framing", g.framing);
    else if (g.key) html += span("key", g.key);
    else if (g.speaker) html += span("key", g.speaker);
    else if (g.shot) html += span("shot", g.shot);
    else if (g.speakerId) html += span("speaker-id", g.speakerId);
    else if (g.time) html += span("time", g.time);
    else if (g.quote) html += span("speech", g.quote);
  }
  // A trailing newline needs a character after it to take up a line.
  return html + highlightPlain(text.slice(last), last) + "​";
}

/* ------------------------------------------------- LoRA trigger tip -- */
// Hovering a highlighted trigger (or putting the caret in one, for phones)
// shows which LoRA it belongs to, with a switch that turns that LoRA on/off.
// The highlight spans sit behind the transparent textarea and get no pointer
// events, so the pointer is hit-tested against their boxes instead.
const triggerTip = { span: null, hideTimer: null };

function loraRowFor(key) {
  return listLoras()
    ? $(`[data-ltx-lora="${CSS.escape(String(key))}"]`, $(listLoras().list))
    : $(`[data-lora-index="${key}"]`, $("#minimax-lora-list"));
}

function triggerSpanAt(x, y) {
  return $$(".hl-trigger, .hl-trigger-off, .hl-camera-alias", $("#prompt-highlight"))
    .find((span) => [...span.getClientRects()].some((rect) => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom));
}

function renderTriggerTip() {
  const tip = $("#trigger-tip");
  const span = triggerTip.span;
  const loras = span ? loraTriggerMatcher().loras[Number(span.dataset.trigger)] : null;
  const alias = span ? loraTriggerMatcher().aliases[Number(span.dataset.trigger)] : null;
  if (!span || (!loras?.length && !alias?.camera)) { tip.hidden = true; hideLoraHover(); return; }
  tip.innerHTML = alias?.camera
    ? `<div class="trigger-tip-word">Unofficial ${UNOFFICIAL_FAMILIES[alias.family]?.noun || "camera move"} <b class="trigger-tip-camera">${escapeHtml(span.textContent)}</b></div>`
      + `<div class="trigger-tip-expands">Becomes on Generate:<br><code>${escapeHtml(alias.target)}</code></div>`
    : (alias
    ? `<div class="trigger-tip-word">Alias <b>${escapeHtml(span.textContent)}</b> → <code>${escapeHtml(alias.target)}</code> on Generate</div>`
    : `<div class="trigger-tip-word">Trigger <b>${escapeHtml(span.textContent)}</b></div>`) + loras.map(({ key, label }) => {
    const box = loraRowFor(key) && $("[data-lora-enabled]", loraRowFor(key));
    const on = Boolean(box?.checked);
    const blocked = box?.disabled ? ' title="The LoRA limit is reached - turn another one off first"' : "";
    return `<label class="trigger-tip-row" data-tip-row="${escapeHtml(String(key))}"${blocked}>
      <span class="trigger-tip-lora">${escapeHtml(label)}</span>
      <span class="trigger-tip-state" data-on="${on}">${on ? "on" : "off"}</span>
      <input type="checkbox" data-tip-lora="${escapeHtml(String(key))}" ${on ? "checked" : ""} ${box?.disabled ? "disabled" : ""} aria-label="${escapeHtml(label)} on/off">
    </label>`;
  }).join("");
  tip.hidden = false;
  // Just under the word (above it when there's no room), kept on screen.
  const rect = span.getClientRects()[0] || span.getBoundingClientRect();
  const width = tip.offsetWidth;
  const height = tip.offsetHeight;
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
  const below = rect.bottom + 6;
  tip.style.left = `${left}px`;
  tip.style.top = `${below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 6) : below}px`;
}

function showTriggerTip(span) {
  clearTimeout(triggerTip.hideTimer);
  if (span === triggerTip.span && !$("#trigger-tip").hidden) return;
  triggerTip.span = span;
  renderTriggerTip();
}

function hideTriggerTipSoon() {
  clearTimeout(triggerTip.hideTimer);
  // A short grace period to get from the word to the tip below it.
  triggerTip.hideTimer = setTimeout(hideTriggerTipNow, 150);
}

function hideTriggerTipNow() {
  clearTimeout(triggerTip.hideTimer);
  triggerTip.span = null;
  $("#trigger-tip").hidden = true;
  hideLoraHover();
}

// After the prompt re-highlights, the tip follows the same trigger's new span.
function refreshTriggerTip() {
  if (!triggerTip.span || $("#trigger-tip").hidden) return;
  const { from } = triggerTip.span.dataset;
  triggerTip.span = $(`#prompt-highlight [data-from="${from}"]`);
  renderTriggerTip();
}

/* ------------------------------------------- LoRA hover preview (tip) -- */
// Hovering a LoRA row in the trigger tip plays that LoRA's gallery previews
// above the pointer: its videos one after another (looping back to the first),
// or its image when it has no video. NSFW previews stay blurred unless the
// gallery's "Show NSFW previews" is on.
const loraHover = { filename: null, items: [], index: 0 };

async function ensureGalleryIndex() {
  if (gallery.index) return gallery.index;
  let builtIn = {}, custom = {}, overrides = {};
  try { builtIn = await Api.request(`${API_BASE}/api/lora-gallery/shared`); } catch { /* optional preview data */ }
  try { custom = (await Api.request(`${API_BASE}/api/lora-gallery/custom`)).index || {}; } catch { /* optional managed entries */ }
  try {
    gallery.catalog = await Api.request(`${API_BASE}/api/lora-gallery/catalog/${galleryModel()}`);
    overrides = gallery.catalog.index || {};
  } catch { /* gallery remains usable without config */ }
  gallery.index = { ...builtIn, ...custom };
  for (const [filename, info] of Object.entries(overrides)) gallery.index[filename] = { ...(gallery.index[filename] || {}), ...info };
  return gallery.index;
}

function loraFilenameForKey(key) {
  return listLoras() ? key : (state.options?.minimax_h3_lora_slots || [])[Number(key)]?.filename;
}

function placeLoraHover(x, y) {
  const box = $("#lora-hover-preview");
  const left = Math.max(8, Math.min(x - box.offsetWidth / 2, window.innerWidth - box.offsetWidth - 8));
  const above = y - box.offsetHeight - 16;
  box.style.left = `${left}px`;
  box.style.top = `${above >= 8 ? above : Math.min(y + 20, window.innerHeight - box.offsetHeight - 8)}px`;
}

function playLoraHoverItem() {
  const box = $("#lora-hover-preview");
  const video = $("video", box);
  const img = $("img", box);
  const item = loraHover.items[loraHover.index];
  const count = loraHover.items.length;
  $(".lora-hover-count", box).textContent = count > 1 ? `${loraHover.index + 1} / ${count}` : "";
  if (item.video) {
    img.hidden = true;
    video.hidden = false;
    video.loop = count === 1; // several videos: "ended" moves on to the next
    video.poster = galleryAsset(item.poster);
    video.src = galleryAsset(item.video);
    video.play().catch(() => { /* autoplay blocked - the poster still shows */ });
  } else {
    video.pause();
    video.hidden = true;
    img.hidden = false;
    img.src = galleryAsset(item.poster);
  }
}

async function showLoraHover(filename, x, y) {
  const box = $("#lora-hover-preview");
  if (loraHover.filename === filename) { if (!box.hidden) placeLoraHover(x, y); return; }
  loraHover.filename = filename;
  const index = await ensureGalleryIndex();
  if (loraHover.filename !== filename) return; // moved on while loading
  const previews = index[filename]?.previews || [];
  const lora = [...(state.options?.minimax_h3_lora_slots || []), ...(state.options?.ltx_loras || []), ...(state.options?.wan_loras || [])].find((item) => item.filename === filename);
  const hidden = (item) => !galleryShowsNsfw() && item.nsfw >= NSFW_BLUR_LEVEL;
  const videos = previews.filter((item) => item.video && !hidden(item));
  const images = previews.filter((item) => !hidden(item));
  $(".lora-hover-title", box).textContent = lora?.label || filename;
  box.classList.remove("is-blurred");
  if (videos.length) loraHover.items = videos;
  else if (images.length) loraHover.items = images.slice(0, 1).map((item) => ({ poster: item.poster }));
  else if (previews.length) { // only NSFW previews and they're hidden: a blurred poster
    loraHover.items = [{ poster: previews[0].poster }];
    box.classList.add("is-blurred");
  } else loraHover.items = [];
  $(".lora-hover-note", box).textContent = !previews.length ? "No preview for this LoRA."
    : box.classList.contains("is-blurred") ? "NSFW preview - turn on \"Show NSFW previews\" in the LoRA gallery to see it." : "";
  $(".lora-hover-media", box).hidden = !loraHover.items.length;
  loraHover.index = 0;
  box.hidden = false;
  if (loraHover.items.length) playLoraHoverItem();
  placeLoraHover(x, y);
}

function hideLoraHover() {
  loraHover.filename = null;
  const box = $("#lora-hover-preview");
  box.hidden = true;
  $("video", box).pause();
  $("video", box).removeAttribute("src");
}

function bindLoraHover() {
  const tip = $("#trigger-tip");
  const video = $("#lora-hover-preview video");
  video.addEventListener("ended", () => {
    if (loraHover.items.length < 2) return;
    loraHover.index = (loraHover.index + 1) % loraHover.items.length;
    playLoraHoverItem();
  });
  tip.addEventListener("pointermove", (event) => {
    if (event.pointerType !== "mouse") return;
    const row = event.target.closest("[data-tip-row]");
    const filename = row && loraFilenameForKey(row.dataset.tipRow);
    if (filename) showLoraHover(filename, event.clientX, event.clientY);
    else if (loraHover.filename) hideLoraHover();
  });
  tip.addEventListener("pointerleave", hideLoraHover);
}

function bindTriggerTip() {
  const textarea = $("#prompt");
  const tip = $("#trigger-tip");
  let frame = 0;
  textarea.addEventListener("mousemove", (event) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const span = triggerSpanAt(event.clientX, event.clientY);
      if (span) showTriggerTip(span); else if (!tip.matches(":hover")) hideTriggerTipSoon();
    });
  });
  textarea.addEventListener("mouseleave", hideTriggerTipSoon);
  tip.addEventListener("mouseenter", () => clearTimeout(triggerTip.hideTimer));
  // Leaving the tip closes it (and the LoRA preview) at once, unless the
  // pointer went back onto the trigger word itself.
  tip.addEventListener("mouseleave", (event) => { if (!triggerSpanAt(event.clientX, event.clientY)) hideTriggerTipNow(); });
  // Touch / keyboard: the caret inside a trigger opens its tip.
  const fromCaret = (event) => {
    if (event.pointerType === "mouse") return;
    const caret = textarea.selectionStart;
    if (caret !== textarea.selectionEnd) return;
    const span = $$("[data-trigger]", $("#prompt-highlight")).find((node) => caret >= Number(node.dataset.from) && caret <= Number(node.dataset.to));
    if (span) showTriggerTip(span); else if (!triggerTip.span || event.type !== "keyup" || !tip.matches(":hover")) { triggerTip.span = null; tip.hidden = true; hideLoraHover(); }
  };
  textarea.addEventListener("pointerup", fromCaret);
  textarea.addEventListener("keyup", (event) => { if (!event.key.startsWith("Arrow") && event.key !== "Home" && event.key !== "End") return; fromCaret(event); });
  textarea.addEventListener("scroll", () => { triggerTip.span = null; tip.hidden = true; hideLoraHover(); });
  tip.addEventListener("pointerdown", (event) => event.preventDefault()); // keep the prompt's focus/caret
  tip.addEventListener("change", (event) => {
    const input = event.target.closest("[data-tip-lora]");
    if (!input) return;
    const row = loraRowFor(input.dataset.tipLora);
    const box = row && $("[data-lora-enabled]", row);
    if (!box || box.disabled) { renderTriggerTip(); return; }
    box.checked = input.checked;
    row.classList.toggle("is-enabled", box.checked);
    // The list's own change handlers update the count, limits, hotbar and highlight.
    box.dispatchEvent(new Event("change", { bubbles: true }));
    refreshTriggerTip();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !tip.hidden) { triggerTip.span = null; tip.hidden = true; hideLoraHover(); } });
}

// Android Chrome scrolls a focused text field back into view whenever the
// viewport resizes - and scrolling down collapses the URL bar, which is a
// resize - so after typing in the prompt, scrolling to a setting below kept
// snapping back up to it. A scroll gesture that starts outside the field
// being edited drops focus (closing the keyboard) so nothing pulls back.
// Gestures inside the prompt, its snippet bar or the alias menu keep focus,
// so snippets still insert at the caret.
function bindScrollReleasesFocus() {
  const editable = 'textarea, input:not([type="checkbox"]):not([type="radio"]):not([type="range"]), select, [contenteditable="true"]';
  document.addEventListener("touchmove", (event) => {
    const focused = document.activeElement;
    if (!focused || !focused.matches?.(editable)) return;
    if (focused.contains(event.target)) return;
    if (focused.id === "prompt" && event.target.closest?.(".prompt-editor, .prompt-snippets, .alias-menu")) return;
    focused.blur();
  }, { passive: true });
}

let refreshPromptHighlight = () => {}; // set by bindPromptHighlight

function bindPromptHighlight() {
  const textarea = $("#prompt");
  const layer = $("#prompt-highlight");
  const sync = () => {
    // clientWidth/Height exclude the textarea's scrollbar, so both layers wrap at the same width.
    // Read before the innerHTML write below, which would otherwise force a layout to answer them.
    const width = textarea.clientWidth, height = textarea.clientHeight;
    layer.innerHTML = highlightPrompt(textarea.value);
    refreshTriggerTip();
    layer.style.width = `${width}px`;
    layer.style.height = `${height}px`;
    layer.scrollTop = textarea.scrollTop;
  };
  // Programmatic edits (model switch, Retry, Enhance, token inserts) set
  // .value or call setRangeText, neither of which fires "input".
  const valueProperty = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
  Object.defineProperty(textarea, "value", {
    get() { return valueProperty.get.call(this); },
    set(value) { valueProperty.set.call(this, value); sync(); schedulePromptDraftSave(); },
  });
  const setRangeText = textarea.setRangeText;
  textarea.setRangeText = function (...args) { setRangeText.apply(this, args); sync(); schedulePromptDraftSave(); };
  textarea.addEventListener("input", () => { sync(); schedulePromptDraftSave(); });
  textarea.addEventListener("scroll", () => { layer.scrollTop = textarea.scrollTop; });
  new ResizeObserver(sync).observe(textarea);
  refreshPromptHighlight = sync;
  sync();
}

// Fullscreen prompt: the whole editor suite (text, highlighting, alias menu,
// snippet bar) fills the window; the text area takes all the spare height.
function setPromptFullscreen(on) {
  $("#prompt-suite").classList.toggle("is-fullscreen", on);
  document.body.classList.toggle("is-prompt-fullscreen", on);
  if (!on) { closePromptRefStage(); closePromptRefAudio(); }
  const button = $("#prompt-fullscreen");
  button.setAttribute("aria-pressed", String(on));
  button.innerHTML = on ? "&#10003; Done" : '<svg class="prompt-fs-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg> Fullscreen';
  button.title = on ? "Back to the normal layout (Esc)" : "Edit the prompt full screen (Esc to exit)";
  // A history entry per fullscreen, so the phone's Back button (or gesture)
  // leaves the editor instead of the page; Done/Esc drop that entry again.
  if (on && !history.state?.promptFullscreen) history.pushState({ promptFullscreen: true }, "");
  if (!on && history.state?.promptFullscreen) history.back();
  syncPromptViewport();
  renderPromptSnippets(); // row count differs in fullscreen
  // Phones: no keyboard popping up on open/close - tapping the text does that.
  if (!coarsePointer.matches) $("#prompt").focus();
}

function bindPromptFullscreen() {
  $("#prompt-fullscreen").addEventListener("click", () => setPromptFullscreen(!$("#prompt-suite").classList.contains("is-fullscreen")));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !$("#prompt-suite").classList.contains("is-fullscreen")) return;
    if (!$("#alias-menu").hidden) return; // Esc closes the @/< menu first
    setPromptFullscreen(false);
  });
  if (history.state?.promptFullscreen) history.replaceState(null, ""); // left over from before a reload
  window.addEventListener("popstate", () => {
    if ($("#prompt-suite").classList.contains("is-fullscreen") && !history.state?.promptFullscreen) setPromptFullscreen(false);
  });
  // Hides the phone keyboard so a reference preview gets the room back.
  $("#prompt-keyboard-hide").addEventListener("click", () => $("#prompt").blur());
  bindPromptViewport();
}

// Phones: the on-screen keyboard only shrinks the visual viewport (iOS never
// resizes the layout one), so the fixed fullscreen suite ran on under the
// keyboard and hid the hotbar. While fullscreen, the suite is pinned to the
// visual viewport, and body.is-keyboard-open marks the keyboard being up:
// the hotbar drops to 2 rows and the hint to one line (see snippetRowCount).
const coarsePointer = window.matchMedia("(pointer: coarse)");
let promptViewportFull = 0; // tallest visual viewport at this width = no keyboard
let promptViewportWidth = 0;
let promptKeyboardOpen = false;

function syncPromptViewport() {
  const suite = $("#prompt-suite");
  const view = window.visualViewport;
  const on = suite.classList.contains("is-fullscreen");
  // Pinch-zoomed: leave the layout alone rather than chase the zoomed view.
  if (!on || !view || Math.abs(view.scale - 1) > 0.01) {
    suite.style.top = "";
    suite.style.height = "";
    setPromptKeyboardOpen(false);
    return;
  }
  if (Math.round(view.width) !== promptViewportWidth) { promptViewportWidth = Math.round(view.width); promptViewportFull = 0; } // rotated
  promptViewportFull = Math.max(promptViewportFull, view.height, document.activeElement?.id === "prompt" ? 0 : window.innerHeight);
  suite.style.top = `${view.offsetTop}px`;
  suite.style.height = `${view.height}px`;
  setPromptKeyboardOpen(coarsePointer.matches && document.activeElement?.id === "prompt" && view.height < promptViewportFull - 120);
}

function setPromptKeyboardOpen(open) {
  if (open === promptKeyboardOpen) return;
  promptKeyboardOpen = open;
  document.body.classList.toggle("is-keyboard-open", open);
  renderPromptSnippets(); // fewer hotbar rows while typing
  $("#prompt").dispatchEvent(new Event("h3:caret")); // keep the caret in view after the resize
}

function bindPromptViewport() {
  const view = window.visualViewport;
  if (!view) return;
  let frame = 0;
  const sync = () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      if (!$("#prompt-suite").classList.contains("is-fullscreen")) return;
      syncPromptViewport();
      refitPromptRef();
    });
  };
  view.addEventListener("resize", sync);
  view.addEventListener("scroll", sync); // iOS pans the page to the caret
  $("#prompt").addEventListener("focus", sync);
  $("#prompt").addEventListener("blur", sync);
  $("#prompt").addEventListener("h3:caret", () => {
    const textarea = $("#prompt");
    if (document.activeElement !== textarea) return;
    // Nudge the textarea's own scroll so the caret line isn't under the hotbar.
    const caret = caretClientRect();
    const box = textarea.getBoundingClientRect();
    if (caret.bottom > box.bottom - 8) textarea.scrollTop += caret.bottom - box.bottom + 24;
  });
}

// Fullscreen prompt references: "Show references" opens a thumbnail column to
// the right of the prompt (above the hotbar). Tapping an image or video splits
// the prompt area in half with it; audio plays in a bar above the prompt.
// Tapping the active thumbnail again closes it.
const PROMPT_REFS_KEY = "h3-prompt-refs-open";
let promptRefsOpen = false;
try { promptRefsOpen = localStorage.getItem(PROMPT_REFS_KEY) === "1"; } catch { /* storage blocked */ }
let promptRefShown = null; // image/video in the split view
let promptRefAudio = null; // audio playing in the top bar

// Keyframes (Keyframe mode) first, then the attached references (promptTags).
function promptRefItems() {
  const keyframeIds = state.model === "seedhunter" ? [state.shFirstFrameId, state.shLastFrameId]
    : state.mode === "fl2v" ? [state.firstFrameId, state.lastFrameId] : [];
  const legacyTags = state.model === "minimax_h3"; // same rule as referenceSnippets
  const keyframes = keyframeIds.map((id, index) => ({
    reference: id ? currentReference(id) : null,
    token: legacyTags ? ["@First", "@Last"][index] : null,
    label: ["First frame", "Last frame"][index],
  })).filter((item) => item.reference);
  return [...keyframes, ...promptTags()];
}

function promptRefHead(item, category) {
  const token = item.token
    ? `<button type="button" class="reference-token" data-category="${category}" data-prompt-ref-insert="${escapeHtml(item.token)}" title="Insert ${escapeHtml(item.token)} into the prompt">${escapeHtml(item.token)}</button>`
    : "";
  const trimmed = category === "image" ? "" : trimLabel(item.reference);
  return `${token}<span class="prompt-ref-label">${escapeHtml(item.label || "")}</span>${trimmed ? `<span class="prompt-ref-trim" title="Only this part is played, and sent to generation">${escapeHtml(trimmed)}</span>` : ""}`;
}

function renderPromptRefs() {
  const open = promptRefsOpen && $("#prompt-suite").classList.contains("is-fullscreen");
  const items = promptRefItems();
  const ids = new Set(items.map((item) => item.reference.id));
  if (promptRefShown && !ids.has(promptRefShown)) closePromptRefStage();
  if (promptRefAudio && !ids.has(promptRefAudio)) closePromptRefAudio();
  const button = $("#prompt-refs-toggle");
  button.setAttribute("aria-pressed", String(open));
  button.title = open ? "Hide the references list" : "Show the attached references next to the prompt";
  button.innerHTML = `${uiIcon("images")} <span class="prompt-refs-text">${open ? "Hide" : "Show"} references</span>${items.length ? ` <span class="prompt-refs-count">${items.length}</span>` : ""}`;
  const list = $("#prompt-ref-list");
  list.hidden = !open;
  if (!open) return;
  list.innerHTML = items.length ? items.map(({ reference, token, label }) => {
    const category = referenceCategory(reference);
    const src = category === "image" ? reference.small_url || reference.preview_url : category === "video" ? reference.small_url || reference.thumbnail_url : null;
    const active = reference.id === promptRefShown || reference.id === promptRefAudio;
    const title = token && label ? `${token} - ${label}` : token || label || "";
    return `<button type="button" role="listitem" class="prompt-ref-item${active ? " is-active" : ""}" data-prompt-ref="${reference.id}" aria-pressed="${active}" title="${escapeHtml(title)}">
      <span class="prompt-ref-thumb">${src ? `<img src="${src}" alt="" loading="lazy">` : uiIcon(category === "audio" ? "music" : "play")}${
        category === "video" && src ? `<span class="prompt-ref-badge">${uiIcon("play")}</span>` : ""}</span>
      <span class="prompt-ref-name" data-category="${category}">${escapeHtml(token || label || "")}</span>
    </button>`;
  }).join("") : `<p class="prompt-ref-empty">No references attached. Add them in Reference mode, or keyframes in Keyframe mode.</p>`;
}

function showPromptRef(id) {
  const item = promptRefItems().find((entry) => entry.reference.id === id);
  if (!item) return;
  const { reference } = item;
  const category = referenceCategory(reference);
  if (category === "audio") {
    if (promptRefAudio === id) return closePromptRefAudio();
    promptRefAudio = id;
    const bar = $("#prompt-ref-audio");
    bar.hidden = false;
    bar.innerHTML = `${uiIcon("music")}${promptRefHead(item, category)}
      <audio src="${reference.preview_url}" autoplay preload="auto"></audio>
      <div class="prompt-ref-player"></div>
      <button type="button" class="icon-button prompt-ref-close" data-prompt-ref-close="audio" title="Stop and close" aria-label="Stop and close">&#10005;</button>`;
    mountTrimPlayer($(".prompt-ref-player", bar), $("audio", bar), reference, { loop: false, waveform: true });
  } else {
    if (promptRefShown === id) return closePromptRefStage();
    promptRefShown = id;
    const stage = $("#prompt-ref-stage");
    stage.hidden = false;
    $("#prompt-ref-split").hidden = false;
    // Muted autoplay loop; the controls can unmute it.
    const media = category === "video"
      ? `<video src="${reference.preview_url}" autoplay muted playsinline preload="auto"></video>`
      : `<img src="${reference.preview_url}" alt="">`;
    stage.innerHTML = `<div class="prompt-ref-head">${promptRefHead(item, category)}
        <button type="button" class="icon-button prompt-ref-close" data-prompt-ref-close="stage" title="Close" aria-label="Close">&#10005;</button>
      </div>
      <div class="prompt-ref-media">${media}</div>`;
    // Opens at the fit once the media knows its size (half and half until then).
    fitPromptRef();
    const element = $(".prompt-ref-media > :is(img, video)", stage);
    if (category === "video") {
      stage.tabIndex = -1; // lets Space/M reach the player after a click
      mountTrimPlayer(stage, element, reference);
    }
    element.addEventListener(category === "video" ? "loadedmetadata" : "load", () => { if (promptRefFitMode) fitPromptRef(); }, { once: true });
  }
  renderPromptRefs();
}

function closePromptRefStage() {
  promptRefShown = null;
  const stage = $("#prompt-ref-stage");
  stage.hidden = true;
  stage.innerHTML = "";
  $("#prompt-ref-split").hidden = true;
  $("#prompt-suite .prompt-editor").style.flex = ""; // back to full width
  $$("[data-prompt-ref]", $("#prompt-ref-list")).forEach((node) => node.classList.toggle("is-active", node.dataset.promptRef === promptRefAudio));
}

function closePromptRefAudio() {
  promptRefAudio = null;
  const bar = $("#prompt-ref-audio");
  bar.hidden = true;
  bar.innerHTML = ""; // removing the <audio> stops it
  $$("[data-prompt-ref]", $("#prompt-ref-list")).forEach((node) => node.classList.toggle("is-active", node.dataset.promptRef === promptRefShown));
}

// The divider between the prompt and the split-view reference: drag (or arrow
// keys) to resize. Each reference opens at its "fit" - the size where the
// image/video fills its pane with no bars - until the divider is moved.
// Drags snap to that fit and to half and half; double-click goes back to fit.
const PROMPT_REF_SPLIT_MIN = 0.2;
const PROMPT_REF_SPLIT_MAX = 0.8;
const PROMPT_REF_SPLIT_SNAP = 0.04; // a drag this close to a snap point lands on it
let promptRefSplit = 0.5; // the reference's share of prompt + reference
let promptRefFitMode = true; // follow the fit (window resizes, media loading)

function promptRefSplitAxis() {
  return getComputedStyle($(".prompt-workspace")).flexDirection === "column" ? "column" : "row";
}

function applyPromptRefSplit() {
  // Percentage bases that both shrink: sizes stay in proportion, padding included.
  $("#prompt-ref-stage").style.flex = `0 1 ${promptRefSplit * 100}%`;
  $("#prompt-suite .prompt-editor").style.flex = `0 1 ${(1 - promptRefSplit) * 100}%`;
  $("#prompt-ref-split").setAttribute("aria-valuenow", String(Math.round(promptRefSplit * 100)));
}

function setPromptRefSplit(share) {
  promptRefSplit = Math.min(PROMPT_REF_SPLIT_MAX, Math.max(PROMPT_REF_SPLIT_MIN, share));
  applyPromptRefSplit();
}

// Share at which the media fills its box exactly: side by side the box height
// is fixed, so width = height x aspect; stacked, the width is fixed. null until
// the image/video has loaded its size.
function promptRefFitShare() {
  const media = $("#prompt-ref-stage .prompt-ref-media > :is(img, video)");
  const width = media?.naturalWidth || media?.videoWidth;
  const height = media?.naturalHeight || media?.videoHeight;
  if (!width || !height) return null;
  const box = media.parentElement.getBoundingClientRect();
  const stage = $("#prompt-ref-stage").getBoundingClientRect();
  const editor = $("#prompt-suite .prompt-editor").getBoundingClientRect();
  const share = promptRefSplitAxis() === "column"
    ? (box.width * height / width + stage.height - box.height) / (stage.height + editor.height)
    : (box.height * width / height + stage.width - box.width) / (stage.width + editor.width);
  return Math.min(PROMPT_REF_SPLIT_MAX, Math.max(PROMPT_REF_SPLIT_MIN, share));
}

function fitPromptRef() {
  promptRefFitMode = true;
  const fit = promptRefFitShare();
  setPromptRefSplit(fit ?? 0.5);
}

function refitPromptRef() {
  if (promptRefShown) promptRefFitMode ? fitPromptRef() : applyPromptRefSplit();
}

function bindPromptRefSplit() {
  const handle = $("#prompt-ref-split");
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("is-dragging");
    const column = promptRefSplitAxis() === "column";
    // Measured once: the editor + stage span, the handle sits between them.
    const editor = $("#prompt-suite .prompt-editor").getBoundingClientRect();
    const stage = $("#prompt-ref-stage").getBoundingClientRect();
    // Row: editor | handle | stage. Column (phone): stage above handle above editor.
    const start = column ? stage.top : editor.left;
    const end = column ? editor.bottom : stage.right;
    const snaps = [promptRefFitShare(), 0.5].filter((value) => value != null);
    let snappedTo = null;
    const move = (moveEvent) => {
      const at = (column ? moveEvent.clientY : moveEvent.clientX) - start;
      const fraction = at / Math.max(1, end - start);
      const share = column ? fraction : 1 - fraction;
      // Nearest snap point in range (the fit wins a tie with half and half).
      snappedTo = snaps.filter((value) => Math.abs(share - value) < PROMPT_REF_SPLIT_SNAP)
        .sort((a, b) => Math.abs(share - a) - Math.abs(share - b))[0] ?? null;
      handle.classList.toggle("is-snapped", snappedTo != null);
      handle.classList.toggle("is-fit", snappedTo != null && snappedTo === snaps[0] && snaps.length > 1);
      setPromptRefSplit(snappedTo ?? share);
    };
    const stop = () => {
      handle.classList.remove("is-dragging", "is-snapped", "is-fit");
      promptRefFitMode = snappedTo != null && snappedTo === snaps[0] && snaps.length > 1;
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", stop);
      handle.removeEventListener("pointercancel", stop);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
  });
  handle.addEventListener("keydown", (event) => {
    const column = promptRefSplitAxis() === "column";
    const grow = column ? "ArrowDown" : "ArrowLeft"; // moving the handle toward the prompt grows the reference
    const shrink = column ? "ArrowUp" : "ArrowRight";
    if (event.key === "Enter") return fitPromptRef();
    if (event.key !== grow && event.key !== shrink) return;
    event.preventDefault();
    promptRefFitMode = false;
    setPromptRefSplit(promptRefSplit + (event.key === grow ? 0.05 : -0.05));
  });
  // Window resizes / phone rotation: keep following the fit unless moved by hand.
  window.addEventListener("resize", refitPromptRef);
  handle.addEventListener("dblclick", fitPromptRef);
}

function bindPromptRefs() {
  $("#prompt-refs-toggle").addEventListener("click", () => {
    promptRefsOpen = !promptRefsOpen;
    try { localStorage.setItem(PROMPT_REFS_KEY, promptRefsOpen ? "1" : "0"); } catch { /* storage blocked */ }
    if (!promptRefsOpen) { closePromptRefStage(); closePromptRefAudio(); }
    renderPromptRefs();
  });
  $("#prompt-ref-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-prompt-ref]");
    if (item) showPromptRef(item.dataset.promptRef);
  });
  for (const id of ["#prompt-ref-stage", "#prompt-ref-audio"]) {
    $(id).addEventListener("click", (event) => {
      const insert = event.target.closest("[data-prompt-ref-insert]");
      if (insert) return insertTokenAtCursor(insert.dataset.promptRefInsert);
      const close = event.target.closest("[data-prompt-ref-close]");
      if (close) (close.dataset.promptRefClose === "audio" ? closePromptRefAudio : closePromptRefStage)();
    });
  }
}

// Prompt drafts survive a page refresh: each model's draft (promptDrafts) is
// kept in this browser's localStorage and restored on load. Per-browser only;
// nothing is saved until the saved drafts have been restored, so a load can't
// overwrite them with the empty box.
const PROMPT_DRAFTS_KEY = "h3-prompt-drafts";
let promptDraftsRestored = false;
let promptDraftTimer = null;

function restorePromptDrafts() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PROMPT_DRAFTS_KEY)); } catch { /* storage blocked or corrupt */ }
  if (saved && typeof saved === "object") {
    Object.keys(promptDrafts).forEach((model) => { if (typeof saved[model] === "string") promptDrafts[model] = saved[model]; });
    if (typeof saved[state.model] === "string") $("#prompt").value = saved[state.model];
  }
  promptDraftsRestored = true;
}

function savePromptDrafts() {
  clearTimeout(promptDraftTimer);
  if (!promptDraftsRestored) return;
  promptDrafts[state.model] = $("#prompt").value;
  try { localStorage.setItem(PROMPT_DRAFTS_KEY, JSON.stringify(promptDrafts)); } catch { /* storage full or blocked */ }
}

function schedulePromptDraftSave() {
  if (!promptDraftsRestored) return;
  clearTimeout(promptDraftTimer);
  promptDraftTimer = setTimeout(savePromptDrafts, 300);
}

/* --------------------------------------------------- prompt snippets -- */
// The scrolling bar under the prompt. `text` marks the caret with "§";
// `line` snippets start on their own line. `kind` picks the chip color
// (matches the .hl-* highlight classes). Wording follows
// Official-H3-Prompting-Guidelines.md.
const nextNumber = (pattern) => Math.max(0, ...[...$("#prompt").value.matchAll(pattern)].map((m) => Number(m[1]))) + 1;
const snippet = (label, kind, title, text, line = false) => ({ label, kind, title, text: typeof text === "function" ? text : () => text, line });
const nextShot = () => nextNumber(/\[Shot\s*(\d+)\]/g);
const nextSpeaker = () => nextNumber(/\(S(\d+)/g);
const lastShot = () => Math.max(1, nextShot() - 1);
const videoSeconds = () => (Number($("#duration").value) || 0).toFixed(2);

const CAMERA_MOVES = [
  ["Push In", "pushes in"], ["Pull Out", "pulls out"], ["Zoom In", "zooms in"], ["Zoom Out", "zooms out"],
  ["Pan Left", "pans left"], ["Pan Right", "pans right"], ["Truck Left", "trucks left"], ["Truck Right", "trucks right"],
  ["Tilt Up", "tilts up"], ["Tilt Down", "tilts down"], ["Pedestal Up", "pedestals up"], ["Pedestal Down", "pedestals down"],
  ["Arc Shot", "arcs around"], ["Tracking Shot", "tracks"], ["Static Shot", "holds a static shot"],
  ["Shake Slightly", "shakes slightly"], ["Shake Strongly", "shakes strongly"],
  ["Roll CW", "rolls clockwise"], ["Roll CCW", "rolls counterclockwise"],
];
// Hovering a camera chip plays an example clip of the move above the pointer
// (static/camera-clips, from CameraBench - see its CREDITS.md). clipSlug turns
// a chip label into its file name: "Arc Shot" -> arc, "Roll CW" -> roll-cw.
const clipSlug = (label) => label.toLowerCase().replace(/ shot$/, "").replace(/\s+/g, "-");
const CAMERA_EXPLAIN = {
  "push-in": "The camera itself travels forward, toward the subject (a dolly in). The view gets closer and the background shifts.",
  "pull-out": "The camera travels backward, away from the subject (a dolly out), revealing more of the scene.",
  "zoom-in": "The camera stays put and the lens zooms: everything simply gets bigger, with no change in perspective.",
  "zoom-out": "The lens zooms out from a fixed spot, so more of the scene fits in the frame.",
  "pan-left": "The camera turns left on the spot, like turning your head.",
  "pan-right": "The camera turns right on the spot, like turning your head.",
  "truck-left": "The whole camera slides sideways to the left, keeping its direction (unlike a pan).",
  "truck-right": "The whole camera slides sideways to the right, keeping its direction (unlike a pan).",
  "tilt-up": "The camera stays in place and angles upward, like looking up.",
  "tilt-down": "The camera stays in place and angles downward, like looking down.",
  "pedestal-up": "The whole camera rises straight up while still looking forward (unlike a tilt).",
  "pedestal-down": "The whole camera lowers straight down while still looking forward.",
  "arc": "The camera circles around the subject, keeping it in view.",
  "tracking": "The camera moves along with a moving subject, keeping pace with it.",
  "static": "The camera does not move at all - only the scene moves.",
  "shake-slightly": "A handheld feel: small, unsteady jitters, without a deliberate move.",
  "shake-strongly": "Strong, erratic shaking, as in chaos or a rough handheld shot.",
  "roll-cw": "The camera rotates clockwise around its lens axis, tipping the horizon.",
  "roll-ccw": "The camera rotates counterclockwise around its lens axis, tipping the horizon.",
  "pov": "Point of view: we see through a character's eyes.",
  "small-amplitude": "The move covers only a small range - a subtle shift.",
  "large-amplitude": "The move covers a big range - a sweeping change of view.",
  "slow-speed": "The move happens slowly and gently.",
  "fast-speed": "The move happens quickly, like a whip pan.",
};
const cameraSnippet = (label, title, text) => ({ ...snippet(label, "camera", title, text), clip: clipSlug(label) });
const CAMERA_GROUP = { name: "Camera", items: [
  ...CAMERA_MOVES.map(([label, verb]) => cameraSnippet(label, `The camera ${verb}`, `The camera ${verb} §`)),
  cameraSnippet("POV", "The subject's point of view", "A POV shot from §"),
  cameraSnippet("small amplitude", "Small-range change", "with small amplitude §"),
  cameraSnippet("large amplitude", "Large-range change", "with large amplitude §"),
  cameraSnippet("slow speed", "Slow movement", "at slow speed §"),
  cameraSnippet("fast speed", "Fast movement", "at fast speed §"),
] };

// Unofficial camera moves: the "camera" alias family (built-ins seeded by the
// server, plus any made in the alias dialog). Their chips insert the short
// name; generate() expands it into official H3 camera wording. Hidden behind
// the Camera group's (+) so they aren't mistaken for official terms.
// Built-ins are keyed by their target wording: chip label, the alias typed,
// the example clip and an explanation.
const CAMERA_ALIAS_META = {
  "The camera pans left with large amplitude at fast speed": { label: "Whip Pan Left", insert: "whip pan left", clip: "whip-pan-left", explain: "A very fast, wide pan that blurs the image between two framings." },
  "The camera pans right with large amplitude at fast speed": { label: "Whip Pan Right", insert: "whip pan right", clip: "whip-pan-right", explain: "A very fast, wide pan that blurs the image between two framings." },
  "The camera zooms in with large amplitude at fast speed": { label: "Crash Zoom", insert: "crash zoom", clip: "crash-zoom", explain: "A sudden, fast zoom onto the subject for impact." },
  "The camera pushes in while it zooms out": { label: "Dolly Zoom", insert: "dolly zoom", clip: "dolly-zoom", explain: "Moving in while zooming out: the subject stays the same size while the background stretches (the vertigo effect)." },
  "The camera pedestals up with large amplitude while it tilts down": { label: "Crane Up", insert: "crane up", clip: "crane-up", explain: "The camera rises high while angling down to keep the subject in view, like a crane or jib." },
  "The camera pedestals down with large amplitude while it tilts up": { label: "Crane Down", insert: "crane down", clip: "crane-down", explain: "The camera descends from high up while angling up to keep the subject in view." },
  "The camera arcs around the subject in a clockwise direction": { label: "Orbit CW", insert: "orbit clockwise", clip: "orbit-cw", explain: "Circling the subject clockwise (an arc shot with a direction)." },
  "The camera arcs around the subject in a counterclockwise direction": { label: "Orbit CCW", insert: "orbit counterclockwise", clip: "orbit-ccw", explain: "Circling the subject counterclockwise (an arc shot with a direction)." },
  "The camera tracks the subject from the front, pulling out as they move toward it": { label: "Lead Tracking", insert: "lead tracking", clip: "lead-tracking", explain: "The camera moves backward in front of the subject as they walk toward it." },
  "The camera tracks the subject from behind, pushing in as they move forward": { label: "Tail Tracking", insert: "tail tracking", clip: "tail-tracking", explain: "The camera follows behind the subject as they move away." },
  "The camera tracks the subject from the side, trucking alongside them": { label: "Side Tracking", insert: "side tracking", clip: "tracking", explain: "The camera moves sideways alongside the subject, keeping pace." },
  "The camera trucks smoothly high above the scene at slow speed in an aerial view": { label: "Drone Shot", insert: "drone shot", clip: "drone-shot", explain: "A high, smooth aerial view gliding over the scene." },
  "The camera shakes slightly in a handheld style": { label: "Handheld", insert: "handheld camera", clip: "shake-slightly", explain: "The loose, slightly shaky feel of a camera held by hand." },
  "The camera pushes in with small amplitude at slow speed": { label: "Slow Push-In", insert: "slow push-in", clip: "slow-push-in", explain: "A slow, subtle move toward the subject to build tension." },
  "The camera pulls out with large amplitude, revealing": { label: "Pull-Back Reveal", insert: "pull-back reveal", clip: "pull-back-reveal", explain: "Pulling back to reveal more of the scene - follow it with what gets revealed." },
};
const UNOFFICIAL_CAMERA_KEY = "h3-unofficial-camera";
// Shot composition (next to Camera). The official guide has no shot-size list;
// these five appear in its own examples ("a medium-wide shot frames…", "a close
// shot…", "cuts to a close-up…", "an extreme close-up…"), so they are the
// official chips. Hover shows an example still (static/shot-examples, from
// CameraBench-Pro - see its CREDITS.md).
const SHOT_EXPLAIN = {
  "medium-wide": "From about the knees up, with some of the surroundings - the guide's own opening framing.",
  "medium": "From about the waist up: body language and face together.",
  "close-shot": "From about the chest up - closer than medium, looser than a close-up.",
  "close-up": "The face (or one object) fills most of the frame.",
  "extreme-close-up": "One small detail fills the frame - an eye, lips, a fingertip.",
  "two-shot": "Two characters framed together, usually side by side at medium size.",
  "over-the-shoulder": "Looking past one character's shoulder (in the foreground) at the other.",
  "full": "The whole body, head to toe, with a little space around it.",
  "cowboy": "From mid-thigh up - a classic western framing.",
  "wide": "The whole setting, the characters small inside it.",
  "establishing": "Very far away, to show where the scene takes place.",
  "insert": "A tight shot of a small object or detail that matters to the story.",
  "low-angle": "The camera looks up at the subject, making it feel powerful.",
  "high-angle": "The camera looks down at the subject, making it feel small.",
  "birds-eye": "Straight down from directly above.",
  "worms-eye": "Straight up from the ground.",
  "dutch": "The frame is tilted so the horizon slants, for unease or tension.",
};
// Capitalized at the start of a sentence or shot ("[Shot 1] A close-up of…"), else lower case.
const shotSnippet = (label, image, title, text) => ({
  ...snippet(label, "framing", title, () => (/(^|[.!?]["”']?\s+|\n\s*|\]\s*|:\s*)$/.test(textBeforeCaret()) ? text[0].toUpperCase() + text.slice(1) : text)),
  image,
});
const SHOT_GROUP = { name: "Composition", items: [
  shotSnippet("Medium-Wide Shot", "medium-wide", "Knees up, with surroundings", "a medium-wide shot frames §"),
  shotSnippet("Medium Shot", "medium", "Waist up", "a medium shot of §"),
  shotSnippet("Close Shot", "close-shot", "Chest up", "a close shot of §"),
  shotSnippet("Close-Up", "close-up", "The face fills the frame", "a close-up of §"),
  shotSnippet("Extreme Close-Up", "extreme-close-up", "One detail fills the frame", "an extreme close-up of §"),
] };
// Built-in unofficial compositions, keyed by their target wording.
const SHOT_ALIAS_META = {
  "a two-character medium shot": { label: "Two Shot", insert: "two shot", image: "two-shot" },
  "an over-the-shoulder medium shot": { label: "Over-the-Shoulder", insert: "over-the-shoulder shot", image: "over-the-shoulder" },
  "a head-to-toe full shot": { label: "Full Shot", insert: "full shot", image: "full" },
  "a mid-thigh-up medium-wide shot": { label: "Cowboy Shot", insert: "cowboy shot", image: "cowboy" },
  "a wide full-environment shot": { label: "Wide Shot", insert: "wide shot", image: "wide" },
  "an extreme wide establishing shot": { label: "Establishing Shot", insert: "establishing shot", image: "establishing" },
  "an extreme close-up insert shot": { label: "Insert Shot", insert: "insert shot", image: "insert" },
  "a low-angle upward-looking shot": { label: "Low Angle", insert: "low angle shot", image: "low-angle" },
  "a high-angle downward-looking shot": { label: "High Angle", insert: "high angle shot", image: "high-angle" },
  "a top-down bird's-eye view": { label: "Bird's-Eye", insert: "birds eye view", image: "birds-eye" },
  "a ground-level upward worm's-eye view": { label: "Worm's-Eye", insert: "worms eye view", image: "worms-eye" },
  "a tilted-horizon dutch-angle shot": { label: "Dutch Angle", insert: "dutch angle", image: "dutch" },
};
Object.values(SHOT_ALIAS_META).forEach((meta) => { meta.explain = SHOT_EXPLAIN[meta.image]; });

// Unofficial alias families (built-ins seeded by the server): their chips sit
// behind their group's (+) and use the gradient color.
const UNOFFICIAL_FAMILIES = {
  camera: { base: () => CAMERA_GROUP, meta: CAMERA_ALIAS_META, key: UNOFFICIAL_CAMERA_KEY, noun: "camera move" },
  shot: { base: () => SHOT_GROUP, meta: SHOT_ALIAS_META, key: "h3-unofficial-shot", noun: "shot composition" },
};
const isUnofficialFamily = (family) => Object.hasOwn(UNOFFICIAL_FAMILIES, family);
const unofficialOpen = (family) => { try { return localStorage.getItem(UNOFFICIAL_FAMILIES[family].key) === "open"; } catch { return false; } };
function setUnofficialOpen(family, open) {
  try { localStorage.setItem(UNOFFICIAL_FAMILIES[family].key, open ? "open" : "closed"); } catch { /* storage blocked */ }
  renderPromptSnippets();
}

function unofficialGroup(family) {
  const { base, meta, noun } = UNOFFICIAL_FAMILIES[family];
  const group = base();
  const open = unofficialOpen(family);
  const toggle = {
    ...snippet(open ? "−" : "+", "camera-toggle", open ? `Hide the unofficial ${noun}s`
      : `Show unofficial ${noun}s - they expand into fuller wording on Generate`, ""),
    action: () => setUnofficialOpen(family, !open),
  };
  if (!open) return { ...group, items: [toggle, ...group.items] };
  const order = Object.keys(meta);
  const rank = (item) => (order.includes(item.target) ? order.indexOf(item.target) : order.length);
  const extra = aliasGroups(family).sort((a, b) => rank(a) - rank(b)).map((item) => {
    const info = meta[item.target];
    const insert = info && item.aliases.some((alias) => alias.toLowerCase() === info.insert.toLowerCase()) ? info.insert : item.aliases[0];
    return {
      ...snippet(info?.label || insert, "camera-x", `Unofficial - "${insert}" becomes "${item.target}" on Generate`, `${insert} §`),
      clip: info?.clip || null, image: info?.image || null, explain: info?.explain || "", expands: item.target,
    };
  });
  return { ...group, items: [toggle, ...group.items, ...extra] };
}

const H3_SNIPPET_GROUPS = [
  { name: "Structure", items: [
    snippet("Template", "key", "The three core fields, a blank line between each (guide 2.2)", "integrated_multimodal_description: [Shot 1] §\n\noverall_soundscape: \n\nnon_diegetic_music: N/A", true),
    snippet("description:", "key", "Visuals, action, shots, speakers and dialogue along the timeline", "integrated_multimodal_description: §", true),
    snippet("soundscape:", "key", "Ambient, action and non-verbal sounds across the whole video", "overall_soundscape: §", true),
    snippet("music:", "key", "Background music only the audience hears", "non_diegetic_music: §", true),
    snippet("music: N/A", "key", "No background music", "non_diegetic_music: N/A§", true),
    snippet("soundscape: N/A", "key", "Only when the whole video is explicitly silent (guide 4.6)", "overall_soundscape: N/A§", true),
  ] },
  // Guide 4.1: state the overall style at the start of [Shot 1] ("[Shot 1] Live-action, cinematic, a medium-wide shot frames…").
  { name: "Style", items: ["Cinematic", "live-action", "2D-animated", "3D CG", "claymation", "watercolor", "vintage film"].map((style) =>
    snippet(style, "style", "One of the guide's common styles - put it first in [Shot 1]", () =>
      `${/(^|\]\s*)$/.test(textBeforeCaret()) ? style[0].toUpperCase() + style.slice(1)
        : /^[A-Z][a-z]/.test(style) ? style[0].toLowerCase() + style.slice(1) : style}, §`)) }, // "Live-action, cinematic, …"
  { name: "Keyframe line", items: [
    snippet("First frame", "ref", "I2VA instruction - first line of the prompt, then a blank line",
      "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n§"),
    snippet("First + last", "ref", "FL2VA instruction - end time from Duration, last shot from the prompt's [Shot N] markers", () =>
      `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot ${lastShot()}) aligns with the ${videoSeconds()}-second mark of the target video.\n\n§`),
    snippet("Last frame", "ref", "L2VA instruction - end time from Duration, last shot from the prompt's [Shot N] markers", () =>
      `How the reference pictures align with the target video — <Picture 1> (from [Shot ${lastShot()}]) aligns with the ${videoSeconds()}-second mark of the target video.\n\n§`),
  ] },
  { name: "Shots", items: [
    snippet("[Shot N]", "shot", "Next shot marker (numbered automatically)", () => `[Shot ${nextShot()}] §`),
    snippet("Cut", "shot", "A new shot with its cut time - no timestamp on Shot 1", () => `[Shot ${nextShot()}] At 00:05.000, the camera cuts to §`),
    snippet("Timestamp", "time", "Cut time - strictly increasing, within the video length", "At 00:05.000, §"),
    ...["the shot cuts to", "the shot transitions to", "the shot changes to", "the shot switches to"].map((phrase) =>
      snippet(phrase.replace("the shot ", ""), "shot", `Ordinary cut wording: "${phrase}" (guide 4.2)`, `${phrase} §`)),
    snippet("cross-dissolve", "shot", "Only when explicitly wanted", "the shot cross-dissolves to §"),
    snippet("fade", "shot", "Only when explicitly wanted", "the shot fades to §"),
    snippet("wipe", "shot", "Only when explicitly wanted", "the shot wipes to §"),
  ] },
  CAMERA_GROUP,
  { name: "Speech", items: [
    snippet("(S#)", "speaker-id", "ID for a new speaker (numbered automatically) - reuse it every time they vocalize", () => `(S${nextSpeaker()}) §`),
    snippet("(S1,S2)", "speaker-id", "Numbered speakers speaking or singing together", "(S1,S2) §"),
    snippet("says <d>", "speech", "Spoken line - words kept verbatim", "says: <d>[English] §</d>"),
    snippet("sings <d>", "speech", "Sung line - lyrics kept verbatim", "sings: <d>[English] §</d>"),
    snippet("together <d>", "speech", "Several speakers at once", "shout together, <d>[English] §</d>"),
    snippet("Voiceover", "speech", "Off-screen voiceover, with the required lips-closed line",
      "says in an off-screen voiceover: <d>[English] §</d> while their lips remain completely closed."),
    snippet("[English]", "lang", "Language tag - first thing inside <d>", "[English] §"),
    ...["Chinese", "Japanese", "Korean", "Spanish", "French"].map((language) =>
      snippet(`[${language}]`, "lang", `Language tag for a line spoken in ${language} - keep the words in that language`, `[${language}] §`)),
    snippet("<scenetrans>", "tag", "Where a line crosses a cut - use at both connecting points", "<scenetrans>§"),
    snippet("across the cut", "tag", "State that the audio continues across the cut", "the line continues seamlessly across the cut. §"),
    snippet("into next shot", "tag", "Continuity wording (guide 4.4)", "the line continues uninterrupted into the next shot. §"),
    snippet("carries over", "tag", "Continuity wording (guide 4.4)", "the line carries over from the previous shot. §"),
    snippet("remains audible", "tag", "Continuity wording (guide 4.4)", "the audio remains audible across the transition. §"),
    snippet("<cutoff>", "tag", "Speech truncated by the end of the video", "<cutoff>§"),
  ] },
  { name: "On-screen text", items: [
    snippet("sign \"…\"", "speech", "Visible text, verbatim, in English double quotes", "a sign reading \"§\""),
    snippet("subtitle \"…\"", "speech", "Visible subtitle, verbatim", "a subtitle reading \"§\""),
    snippet("banner \"…\"", "speech", "Visible banner text, verbatim", "a banner reading \"§\""),
    snippet("label \"…\"", "speech", "Visible label text, verbatim", "a label reading \"§\""),
    snippet("neon \"…\"", "speech", "Visible neon text, verbatim (the guide's own example)", "a neon sign reading \"§\" glows "),
  ] },
];
// Reference mode (ref2va) uses the six-section full-reference format; shots,
// camera and speech syntax are shared with the three-field format above.
// Wording follows the Full-Reference Mode Rewrite Output Format Guide.
const textBeforeCaret = () => { const textarea = $("#prompt"); return textarea.value.slice(0, textarea.selectionStart ?? textarea.value.length); };
const nextSubject = () => nextNumber(/<\s*Subject\s+(\d+)\s*>/gi);
// The first attached reference of a kind (<Picture 2> if that is the first
// picture), else number 1.
const firstRef = (kind) => promptTags().map((tag) => tag.token).find((token) => new RegExp(`^<${kind} \\d+>$`, "i").test(token)) || `<${kind} 1>`;
// A task chip starts "[type] "; right after a task tag (or inside an open
// one) it joins it instead: "[video editing] " + chip -> "[video editing + audio reuse] ".
const TASK_TAG_END = new RegExp(String.raw`\[(?:${H3_TASK_TYPES.join("|")})(?: \+ (?:${H3_TASK_TYPES.join("|")}))*(\] ?)$`);
const OPEN_TASK_TAG = /\[(?:[a-z]+(?: [a-z]+)*(?: \+ [a-z]+(?: [a-z]+)*)*(?: \+ )?)?$/;
const taskSnippet = (type, title) => ({
  ...snippet(type, "task", title, () => {
    const before = textBeforeCaret();
    if (TASK_TAG_END.test(before)) return ` + ${type}] §`;
    if (!OPEN_TASK_TAG.test(before)) return `[${type}] §`;
    return /(?:\[| \+ )$/.test(before) ? `${type}§` : ` + ${type}§`;
  }),
  glue: () => TASK_TAG_END.test(textBeforeCaret()) || OPEN_TASK_TAG.test(textBeforeCaret()),
  // chars before the caret the insert replaces (the closing "] ")
  eat: () => TASK_TAG_END.exec(textBeforeCaret())?.[1].length || 0,
});
const H3_REF_SNIPPET_GROUPS = [
  { name: "Structure", items: [
    snippet("Template", "key", "The six full-reference sections, in order",
      "subject_definitions:\n§\n\nsummary:\n\n\nretention_analysis:\n\n\ndetailed_description:\n\n\noverall_soundscape:\n\n\nnon_diegetic_music:\nN/A", true),
    snippet("subject_definitions:", "key", "One line per referenced item: what its label denotes, its role, the features to follow", "subject_definitions:\n§", true),
    snippet("summary:", "key", "One short paragraph starting with the task-type tag - no new labels here", "summary:\n§", true),
    snippet("retention_analysis:", "key", "One line per label: where it appears and how it is kept", "retention_analysis:\n§", true),
    snippet("detailed_description:", "key", "Style sentence, then shots in playback order (350-500 words)", "detailed_description:\n§", true),
    snippet("soundscape:", "key", "Ambience and physical sounds across the whole video", "overall_soundscape:\n§", true),
    snippet("music:", "key", "Background music only the audience hears", "non_diegetic_music:\n§", true),
    snippet("music: N/A", "key", "No background music", "non_diegetic_music:\nN/A§", true),
  ] },
  { name: "Definitions", items: [
    { ...snippet("<Subject N>", "ref", "Next subject label (numbered automatically) - reusable visible content: a person, animal, object, scene, costume, style or motion", () => `<Subject ${nextSubject()}> §`), category: "subject" },
    snippet("Subject is…", "ref", "Define a subject from a reference - cite the picture/video it comes from here, not on its own line", () =>
      `<Subject ${nextSubject()}> is the § in ${firstRef("Picture")}, with `, true),
    snippet("Picture = frame", "ref", "Only when the image itself is a first frame, keyframe, last frame or composition anchor", () =>
      `${firstRef("Picture")} is the first frame of [Shot 1], showing §`, true),
    snippet("Storyboard", "ref", "An image used as shot planning", () =>
      `${firstRef("Picture")} is a storyboard reference for [Shot 1] and [Shot 2], defining their viewpoint, subject placement, and shot order.§`, true),
    snippet("Edit source", "ref", "The video being edited", () => `${firstRef("Video")} is the source video for the target video edit.§`, true),
    snippet("Continue from", "ref", "The video being continued", () => `${firstRef("Video")} is the source video whose ending the target video continues from.§`, true),
    snippet("Video structure", "ref", "A video used only for camera movement, cuts or rhythm", () =>
      `${firstRef("Video")} is the reference for the target video's camera movement, cuts, and pacing.§`, true),
    snippet("Voice timbre", "ref", "Audio bound to a speaker - reuse that speaker's (Sx)", () =>
      `${firstRef("Audio")} is the voice-timbre reference for <Subject 1> (S1).§`, true),
    snippet("Video's audio", "ref", "A reference video's synchronized soundtrack, reused", () =>
      `${firstRef("Audio")} is the synchronized audio track of ${firstRef("Video")} and is reused in the target video.§`, true),
  ] },
  { name: "Task type", items: [
    taskSnippet("keyframe completion", "An image is a first frame, keyframe, last frame or other concrete frame anchor"),
    taskSnippet("reference generation", "References guide character, scene, style, motion, camera or storyboard - not a frame, not an edited/continued video"),
    taskSnippet("video editing", "An existing source video is directly modified"),
    taskSnippet("video continuation", "New content continues from an existing source video"),
    taskSnippet("audio reuse", "The same audio signal is reused in full or in part"),
    taskSnippet("audio reference", "Only style, timbre, words, texture, beat or continuity of the audio is referenced"),
    snippet("edited version", "ref", "Required opening of a video-editing summary", () => `The target video is an edited version of ${firstRef("Video")}. §`),
  ] },
  { name: "Retention", items: [
    snippet("Subject line", "ref", "retention_analysis line for a subject", "<Subject 1> (appears in [Shot 1]): fully_preserved - §", true),
    snippet("Picture line", "ref", "retention_analysis line for a frame anchor", () => `${firstRef("Picture")} ([Shot 1] first frame): fully_preserved - §`, true),
    snippet("Video line", "ref", "retention_analysis line for a whole-video role", () => `${firstRef("Video")} (source video): fully_preserved - §`, true),
    snippet("Audio line", "ref", "retention_analysis line for audio - no (Sx) here", () => `${firstRef("Audio")}: reference - §`, true),
    snippet("fully_preserved", "marker", "Visual: the defined role is fully kept", "fully_preserved - §"),
    snippet("partially_preserved", "marker", "Visual: still used, some defined traits changed or partly kept", "partially_preserved - §"),
    snippet("attribute_transfer", "marker", "Visual: traits moved onto a different identifiable subject", "attribute_transfer - §"),
    snippet("weak_reference", "marker", "Visual or audio: only broad style/category/atmosphere similarity", "weak_reference - §"),
    snippet("fully_copy", "marker", "Audio: the complete source audio is the complete final track", "fully_copy - §"),
    snippet("partially_copy", "marker", "Audio: part of the timeline or some layers copied, or sounds added/removed", "partially_copy - §"),
    snippet("reference", "marker", "Audio: not copied - only timbre, rhythm, style, words or texture referenced", "reference - §"),
  ] },
  { name: "Description", items: [
    snippet("Style opening", "key", "One or two style sentences before [Shot 1]", "The target video is in a § style with ", true),
    snippet("begins from", "ref", "Shot's first frame is a picture", () => `the shot begins from ${firstRef("Picture")} §`),
    snippet("keyframe", "ref", "Shot's keyframe is a picture", () => `the shot's keyframe corresponds to ${firstRef("Picture")} §`),
    snippet("ends on", "ref", "Shot ends on a picture", () => `the shot ends on ${firstRef("Picture")} §`),
    snippet("voice from", "ref", "Speaker's timbre referenced from an audio", () => `using the voice timbre referenced from ${firstRef("Audio")}, §`),
    snippet("off-screen", "speech", "A subject speaking off-screen keeps <Subject N> (Sx)", "says off-screen, <d>[English] §</d>"),
    snippet("[unclear]", "tag", "Unintelligible span in reused dialogue or lyrics", "[unclear]§"),
    snippet("audio cue", "ref", "A verbal cue inside reused BGM/soundtrack - no (Sx), the audio is the source (guide 5.4)", () =>
      `When ${firstRef("Audio")} reaches the phrase <d>[English] §</d>, `),
    snippet("copied ambience", "ref", "overall_soundscape line for copied ambience (guide 6)", () =>
      `The copied ambience layer from ${firstRef("Audio")} continues throughout the target video.§`),
    snippet("reused score", "ref", "non_diegetic_music line for a reused audience-only score (guide 6)", () =>
      `${firstRef("Audio")} is directly reused as the complete audience-only score.§`),
  ] },
  ...H3_SNIPPET_GROUPS.filter((group) => group.name !== "Structure" && group.name !== "Keyframe line"),
];
const LTX_SNIPPET_GROUPS = [
  { name: "Dialogue", items: [
    snippet("SPEAKER: “…”", "key", "A dialogue line", "SPEAKER: “§”", true),
    snippet("“quote”", "speech", "Quoted speech", "“§”"),
    snippet("sign \"…\"", "speech", "Visible on-screen text", "a sign reading \"§\""),
  ] },
  CAMERA_GROUP,
];

// Wan 2.2 renders no sound: camera wording and visible text only.
const WAN_SNIPPET_GROUPS = [
  { name: "Text", items: [snippet("sign \"…\"", "speech", "Visible on-screen text", "a sign reading \"§\"")] },
  CAMERA_GROUP,
];

// LTX 2.5 Reference (MSR V2), following the MSR V2 sample workflow's prompt:
// a description line per reference first ("Image 1: Young man, ...",
// "Image 3: Scene, ...", "Audio 1: Male voice, belongs only to Image 1."),
// then shot paragraphs. The <Picture N>/<Audio N> tokens typed here become
// those positional names at render time.
function msrSnippetGroups() {
  const msr = msrSlots();
  if (!msr) return null;
  const { images, audios } = msr;
  const blockText = () => [
    ...images.map((image, index) => `${image.token}: ${image.note ? "Scene, " : ""}${index === 0 ? "§" : ""}`),
    ...audios.filter((audio) => audio.partnerToken).map((audio) => `${audio.token}: Voice, belongs only to ${audio.partnerToken}.`),
  ].join("\n") + (images.length ? "\n\n" : "");
  const speakers = audios.filter((audio) => audio.partnerToken);
  return [
    { name: "MSR references", items: [
      ...(images.length ? [snippet("Reference block", "key", "One description line per attached reference, voices paired the way MSR pairs them (Audio k = voice of Image k)", blockText, true)] : []),
      ...images.map((image) => snippet(`${image.token}:`, "ref", `Describe ${image.token} (MSR ${image.slot}${image.note ? `, ${image.note}` : ""})`,
        `${image.token}: ${image.note ? "Scene, " : ""}§`, true)),
      ...speakers.map((audio) => snippet(`${audio.token} voice`, "ref", `${audio.token} is the voice of ${audio.partnerToken} (MSR ${audio.slot})`,
        `${audio.token}: § voice, belongs only to ${audio.partnerToken}.`, true)),
    ] },
    { name: "MSR speech", items: [
      ...speakers.map((audio) => snippet(`${audio.partnerToken} speaks`, "speech", `Lip-synced line in ${audio.token}'s voice`,
        `${audio.partnerToken} speaks with visible lip movement synchronized with ${audio.token}. ${audio.token} (${audio.partnerToken}): "§"`)),
      ...images.filter((image) => !image.note).map((image) => snippet(`${image.token}: “…”`, "speech", `A line spoken by ${image.token}`, `${image.token}: "§"`)),
      snippet("lips closed", "speech", "Keep a non-speaking character silent", "watches silently with lips closed§"),
    ] },
    { name: "MSR shots", items: [
      snippet("Keep identities", "key", "The sample's opening line for multi-shot sequences",
        () => `The camera axis remains fixed throughout the sequence. ${images[0]?.token || "<Picture 1>"} is established on the left side of the frame${images[1] && !images[1].note ? ` and ${images[1].token} on the right side` : ""}. Preserve their identities, clothing and screen positions across every hard cut.\n\n§`, true),
      snippet("New shot", "shot", "Each shot is its own paragraph", "\n\n§"),
      snippet("Hard cut", "shot", "Cut to a new shot", "Hard cut to §"),
      ...images.filter((image) => image.note).map((image) => snippet(`matching ${image.token}`, "ref", "Place the shot in the scene/background reference", `inside the setting matching ${image.token}§`)),
    ] },
  ];
}

// Tokens for whatever references/keyframes are attached right now.
function referenceSnippets() {
  const tags = state.model === "minimax_h3" && state.mode === "fl2v"
    ? FL2V_LEGACY_TAGS.map((tag) => ({ token: `@${tag}`, label: null }))
    : promptTags();
  return tags.map(({ token, label }) =>
    ({ ...snippet(token, "ref", label ? `${token} - ${label}` : token, `${token} §`), category: tokenCategory(token) }));
}

// Every trigger word of every enabled LoRA. Some are listed as
// "SHW_AKIRA (Akira)" - only the part before " (" gets typed.
function loraTriggerSnippets() {
  if (!state.options) return [];
  const loras = listLoras()
    ? collectListLoras().map((item) => (state.options[listLoras().key] || []).find((lora) => lora.filename === item.lora))
    : $$("[data-lora-index]", $("#minimax-lora-list"))
      .filter((row) => $("[data-lora-enabled]", row).checked)
      .map((row) => (state.options.minimax_h3_lora_slots || [])[Number(row.dataset.loraIndex)]);
  return loras.filter(Boolean).flatMap((lora) => (lora.triggers || []).map((trigger) => {
    const word = trigger.replace(/\s*\(.*\)$/, "");
    return snippet(word, "trigger", `${lora.label} - ${trigger}`, `${word}§`);
  }));
}

function renderPromptSnippets() {
  const groups = [
    ...(state.model === "wan_2_2" ? WAN_SNIPPET_GROUPS
      : state.model === "ltx_2_5" ? (msrSnippetGroups()?.concat(CAMERA_GROUP) || LTX_SNIPPET_GROUPS)
      : state.model === "minimax_h3" && state.mode === "ref2v" ? H3_REF_SNIPPET_GROUPS : H3_SNIPPET_GROUPS),
    { name: "References", items: referenceSnippets() },
    { name: "LoRA triggers", items: loraTriggerSnippets() },
  ].flatMap((group) => (group === CAMERA_GROUP ? [unofficialGroup("camera"), unofficialGroup("shot")] : [group])).filter((group) => group.items.length);
  state.promptSnippets = groups.flatMap((group) => group.items);
  renderPromptRefs(); // same triggers: references, mode, fullscreen
  refreshPromptHighlight(); // LoRA trigger colors follow the same changes
  let index = 0;
  const chip = (item) => `<button type="button" class="prompt-snippet hl-${item.kind}" data-snippet="${index++}"${
    item.category ? ` data-category="${item.category}"` : ""}${item.clip ? ` data-clip="${item.clip}"` : ""}${
    item.image ? ` data-image="${item.image}"` : ""}${item.clip || item.image ? "" : ` title="${escapeHtml(item.title)}"`}>${item.kind === "camera-x"
    ? `<span class="camera-x-text">${escapeHtml(item.label)}</span>` : escapeHtml(item.label)}</button>`; // gradient on the text only
  // Each group is split into rows of about equal width by label length (the
  // group name sits at the start of the top row): 2 rows normally, and in
  // fullscreen as many as the drag handle above the bar sets (default 4).
  $("#prompt-snippet-hint").textContent = SNIPPET_HINT_DEFAULT;
  const rows = snippetRowCount();
  const width = (text) => text.length + 4;
  $("#prompt-snippets").innerHTML = groups.map((group) => {
    const lines = Math.min(rows, group.items.length);
    const total = width(group.name) + group.items.reduce((sum, item) => sum + width(item.label), 0);
    const split = Array.from({ length: lines }, () => []);
    let used = width(group.name);
    let row = 0;
    group.items.forEach((item, i) => {
      // Move down once this row is full, keeping enough items for the rows below.
      const itemsLeft = group.items.length - i;
      if (row < lines - 1 && split[row].length && (used + width(item.label) / 2 > (total * (row + 1)) / lines || itemsLeft <= lines - 1 - row)) row++;
      split[row].push(item);
      used += width(item.label);
    });
    return `<div class="prompt-snippet-block">${split.map((items, i) => `
      <div class="prompt-snippet-row">${i === 0 ? `<span class="prompt-snippet-group">${escapeHtml(group.name)}</span>` : ""}${items.map(chip).join("")}</div>`).join("")}
    </div>`;
  }).join("");
}

// Fullscreen hotbar height, in whole rows (per browser).
const SNIPPET_ROWS_KEY = "h3-snippet-rows-fullscreen";
const SNIPPET_ROWS_MIN = 1;
const SNIPPET_ROWS_MAX = 8;
let fullscreenSnippetRows = 4;
try {
  const saved = Number(localStorage.getItem(SNIPPET_ROWS_KEY));
  if (saved >= SNIPPET_ROWS_MIN && saved <= SNIPPET_ROWS_MAX) fullscreenSnippetRows = saved;
} catch { /* storage blocked */ }

function snippetRowCount() {
  if (!$("#prompt-suite").classList.contains("is-fullscreen")) return 2;
  return promptKeyboardOpen ? Math.min(2, fullscreenSnippetRows) : fullscreenSnippetRows;
}

function setFullscreenSnippetRows(rows) {
  rows = Math.max(SNIPPET_ROWS_MIN, Math.min(SNIPPET_ROWS_MAX, rows));
  if (rows === fullscreenSnippetRows) return;
  fullscreenSnippetRows = rows;
  try { localStorage.setItem(SNIPPET_ROWS_KEY, String(rows)); } catch { /* storage blocked */ }
  renderPromptSnippets();
  updateSnippetResizeLabel();
}

function updateSnippetResizeLabel() {
  const handle = $("#prompt-snippets-resize");
  handle.setAttribute("aria-valuenow", String(fullscreenSnippetRows));
  handle.title = `Hotbar: ${fullscreenSnippetRows} row${fullscreenSnippetRows === 1 ? "" : "s"} - drag up/down (or arrow keys) to add or remove a row`;
}

// The grip above the hotbar (fullscreen only): dragging snaps to whole rows;
// up = more rows, down = fewer. Arrow keys step one row.
function bindSnippetResize() {
  const handle = $("#prompt-snippets-resize");
  updateSnippetResizeLabel();
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const row = $(".prompt-snippet-row", $("#prompt-snippets"));
    const rowHeight = (row ? row.getBoundingClientRect().height : 26) + 6; // row + gap
    const startY = event.clientY;
    const startRows = fullscreenSnippetRows;
    handle.classList.add("is-dragging");
    const move = (moveEvent) => setFullscreenSnippetRows(startRows + Math.round((startY - moveEvent.clientY) / rowHeight));
    const stop = () => {
      handle.classList.remove("is-dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", stop);
      handle.removeEventListener("pointercancel", stop);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
  });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    setFullscreenSnippetRows(fullscreenSnippetRows + (event.key === "ArrowUp" ? 1 : -1));
  });
}

/* ------------------------------------------ reference hover preview -- */
// Hovering a reference token - <Picture 1>, Video 2, @First - in the prompt, or
// a References chip in the hotbar, shows that attached asset above the pointer
// (like the LoRA preview). Videos play muted; audio shows its name and length.
// Tokens with nothing attached (or <Subject N>) show nothing.
const refHover = { id: null, hideTimer: null };

function refTokenKey(token) {
  return token.replace(/[<>]/g, "").trim().replace(/\s+/g, " ").toLowerCase().replace(/^image(?= \d)/, "picture");
}

function referenceForToken(token) {
  const key = refTokenKey(token);
  const legacy = { "@image1": "@first", "@image2": "@last" }[key] || key;
  return promptRefItems().find((item) => item.token && refTokenKey(item.token) === legacy) || null;
}

function refSpanAt(x, y) {
  return $$(".hl-ref", $("#prompt-highlight"))
    .find((span) => [...span.getClientRects()].some((rect) => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom));
}

function placeRefHover(x, y) {
  const box = $("#ref-hover-preview");
  const left = Math.max(8, Math.min(x - box.offsetWidth / 2, window.innerWidth - box.offsetWidth - 8));
  const above = y - box.offsetHeight - 16;
  box.style.left = `${left}px`;
  box.style.top = `${above >= 8 ? above : Math.min(y + 20, window.innerHeight - box.offsetHeight - 8)}px`;
}

function showRefHover(token, x, y) {
  const item = referenceForToken(token);
  if (!item) return hideRefHover();
  clearTimeout(refHover.hideTimer);
  const box = $("#ref-hover-preview");
  const { reference } = item;
  if (refHover.id !== reference.id) {
    refHover.id = reference.id;
    const category = referenceCategory(reference);
    const video = $("video", box);
    const img = $("img", box);
    const audio = $(".ref-hover-audio", box);
    img.hidden = category !== "image";
    video.hidden = category !== "video";
    audio.hidden = category !== "audio";
    if (category === "image") img.src = reference.preview_url;
    if (category === "video") {
      video.poster = reference.thumbnail_url || "";
      video.src = reference.preview_url;
      playTrimmed(video, reference);
      video.play().catch(() => { /* autoplay blocked - the poster still shows */ });
    } else { video.pause(); video.removeAttribute("src"); }
    if (category === "audio") audio.innerHTML = uiIcon("music");
    const token = $(".ref-hover-token", box);
    token.textContent = item.token;
    token.dataset.category = category;
    const seconds = effectiveDuration(reference);
    $(".ref-hover-meta", box).textContent = seconds != null ? `${seconds.toFixed(1)}s` : "";
    $(".ref-hover-label", box).textContent = item.label || "";
    box.hidden = false;
  }
  placeRefHover(x, y);
}

function hideRefHover(delay = 0) {
  clearTimeout(refHover.hideTimer);
  refHover.hideTimer = setTimeout(() => {
    refHover.id = null;
    const box = $("#ref-hover-preview");
    box.hidden = true;
    $("video", box).pause();
    $("video", box).removeAttribute("src");
  }, delay);
}

function bindRefHover() {
  const textarea = $("#prompt");
  let frame = 0;
  textarea.addEventListener("mousemove", (event) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const span = refSpanAt(event.clientX, event.clientY);
      if (span) showRefHover(span.textContent, event.clientX, event.clientY); else if (refHover.id) hideRefHover();
    });
  });
  textarea.addEventListener("mouseleave", () => hideRefHover());
  textarea.addEventListener("scroll", () => hideRefHover());
  textarea.addEventListener("input", () => hideRefHover());
  const bar = $("#prompt-snippets");
  const chipToken = (target) => {
    const chip = target.closest(".prompt-snippet.hl-ref");
    return chip ? state.promptSnippets[Number(chip.dataset.snippet)]?.label : null;
  };
  bar.addEventListener("pointermove", (event) => {
    if (event.pointerType !== "mouse") return;
    const token = chipToken(event.target);
    if (token) showRefHover(token, event.clientX, event.clientY); else if (refHover.id) hideRefHover();
  });
  bar.addEventListener("pointerleave", () => hideRefHover());
  bar.addEventListener("scroll", () => hideRefHover());
  // Touch: tapping a References chip inserts it as usual and shows it for a few seconds.
  bar.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse") return;
    const token = chipToken(event.target);
    if (!token) return;
    showRefHover(token, event.clientX, event.clientY);
    hideRefHover(3000);
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideRefHover(); });
}

/* ---------------------------------------------- camera move previews -- */
// Hover (or tap, on touch) a camera chip: a looping example clip plays just
// above the pointer, with a one-line explanation of the move.
const cameraPreview = { chip: null, hideTimer: null };

// All example clips (~2.3 MB) are fetched once into memory so a hover plays
// instantly instead of waiting on a request: on desktop shortly after load,
// on touch devices only once the hotbar is first used (to spare mobile data).
const cameraClipUrls = new Map(); // slug -> object URL
let cameraClipQueue = null;

function cameraClipSlugs() {
  return [...new Set([...Object.keys(CAMERA_EXPLAIN), ...Object.values(CAMERA_ALIAS_META).map((meta) => meta.clip)])];
}

const cameraClipSrc = (slug) => cameraClipUrls.get(slug) || `${API_BASE}/static/camera-clips/${slug}.mp4`;

function preloadCameraClips(first = null) {
  if (!cameraClipQueue) {
    Object.keys(SHOT_EXPLAIN).forEach((image) => { new Image().src = `${API_BASE}/static/shot-examples/${image}.jpg`; });
    cameraClipQueue = cameraClipSlugs();
    const worker = async () => {
      while (cameraClipQueue.length) {
        const slug = cameraClipQueue.shift();
        if (cameraClipUrls.has(slug)) continue;
        try {
          const response = await fetch(`${API_BASE}/static/camera-clips/${slug}.mp4`);
          if (response.ok) cameraClipUrls.set(slug, URL.createObjectURL(await response.blob()));
        } catch { /* offline - hovering falls back to the plain URL */ }
      }
    };
    for (let i = 0; i < 4; i++) worker();
  }
  // A clip hovered before its turn jumps the queue.
  if (first && cameraClipQueue.includes(first)) cameraClipQueue.splice(0, 0, ...cameraClipQueue.splice(cameraClipQueue.indexOf(first), 1));
}

function placeCameraPreview(x, y) {
  const box = $("#camera-preview");
  const width = box.offsetWidth;
  const height = box.offsetHeight;
  const left = Math.max(8, Math.min(x - width / 2, window.innerWidth - width - 8));
  const above = y - height - 16;
  box.style.left = `${left}px`;
  box.style.top = `${above >= 8 ? above : Math.min(y + 20, window.innerHeight - height - 8)}px`;
}

function showCameraPreview(chip, x, y) {
  clearTimeout(cameraPreview.hideTimer);
  const box = $("#camera-preview");
  if (cameraPreview.chip !== chip) {
    cameraPreview.chip = chip;
    const slug = chip.dataset.clip;
    const video = $("video", box);
    preloadCameraClips(slug);
    if (slug) {
      video.src = cameraClipSrc(slug);
      video.play().catch(() => { /* autoplay blocked - the first frame still shows */ });
    }
    const item = state.promptSnippets[Number(chip.dataset.snippet)];
    const image = chip.dataset.image;
    $("img", box).hidden = !image;
    video.hidden = Boolean(image);
    if (image) {
      // The stills aren't redistributed (CameraBench-Pro states no license): without them the
      // card shows just the explanation.
      video.pause();
      const still = $("img", box);
      still.onerror = () => { still.hidden = true; $(".camera-preview-credit", box).textContent = ""; };
      still.src = `${API_BASE}/static/shot-examples/${image}.jpg`;
    }
    $(".camera-preview-credit", box).textContent = image ? "Example: CameraBench-Pro" : "Example: CameraBench, CC BY 4.0";
    $(".camera-preview-title", box).textContent = chip.textContent + (item?.expands ? " (unofficial)" : "");
    $(".camera-preview-text", box).textContent = item?.explain || CAMERA_EXPLAIN[slug] || SHOT_EXPLAIN[image] || "";
    $(".camera-preview-expands", box).hidden = !item?.expands;
    $(".camera-preview-expands", box).textContent = item?.expands ? `Becomes: “${item.expands}”` : "";
  }
  box.hidden = false;
  placeCameraPreview(x, y);
}

function hideCameraPreview(delay = 0) {
  clearTimeout(cameraPreview.hideTimer);
  cameraPreview.hideTimer = setTimeout(() => {
    cameraPreview.chip = null;
    const box = $("#camera-preview");
    box.hidden = true;
    $("video", box).pause();
  }, delay);
}

function bindCameraPreview() {
  const bar = $("#prompt-snippets");
  bar.addEventListener("pointerenter", () => preloadCameraClips(), { once: true });
  bar.addEventListener("touchstart", () => preloadCameraClips(), { once: true, passive: true });
  if (window.matchMedia?.("(hover: hover)").matches) {
    const start = () => preloadCameraClips();
    setTimeout(() => (window.requestIdleCallback ? requestIdleCallback(start, { timeout: 4000 }) : start()), 2500);
  }
  bar.addEventListener("pointermove", (event) => {
    if (event.pointerType !== "mouse") return;
    const chip = event.target.closest("[data-clip], [data-image]");
    if (chip) showCameraPreview(chip, event.clientX, event.clientY);
    else if (cameraPreview.chip) hideCameraPreview();
  });
  bar.addEventListener("pointerleave", (event) => { if (event.pointerType === "mouse") hideCameraPreview(); });
  bar.addEventListener("scroll", () => hideCameraPreview());
  // Touch: the tap inserts the move as usual and shows its clip for a few seconds.
  bar.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse") return;
    const chip = event.target.closest("[data-clip], [data-image]");
    if (!chip) return;
    showCameraPreview(chip, event.clientX, event.clientY);
    hideCameraPreview(3500);
  });
}

const SNIPPET_HINT_DEFAULT = "Hover or tap a chip to see what it means and what it inserts.";

function showSnippetHint(button) {
  const hint = $("#prompt-snippet-hint");
  const item = button && state.promptSnippets[Number(button.dataset.snippet)];
  if (!item) return;
  const inserts = item.action ? "" : item.text().replace("§", "…").replace(/\n/g, " ⏎ ").trim();
  hint.innerHTML = `<span class="prompt-snippet-hint-label hl-${item.kind}"${item.category ? ` data-category="${item.category}"` : ""}>${escapeHtml(item.label)}</span>`
    + ` ${escapeHtml(item.title.startsWith(`${item.label} - `) ? item.title.slice(item.label.length + 3) : item.title)}`
    + (inserts ? `<span class="prompt-snippet-hint-insert">inserts: <code>${escapeHtml(inserts)}</code></span>` : "");
}

function insertSnippet(snippet) {
  const textarea = $("#prompt");
  let start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  let text = snippet.text();
  const glue = snippet.glue?.();
  if (start === end) start -= snippet.eat?.() || 0;
  const before = textarea.value.slice(0, start);
  if (snippet.line) text = (before && !before.endsWith("\n") ? "\n" : "") + text;
  else if (before && !/\s$/.test(before) && !glue) text = ` ${text}`;
  const caret = text.indexOf("§");
  text = text.replace("§", "");
  textarea.focus();
  textarea.setSelectionRange(start, end);
  // execCommand keeps the insert on the browser's undo stack (Ctrl+Z).
  if (!document.execCommand("insertText", false, text)) textarea.setRangeText(text, start, end, "end");
  const position = start + (caret < 0 ? text.length : caret);
  textarea.setSelectionRange(position, position);
  closeAliasMenu();
}

/* ------------------------------------------------------- ref viewer -- */
// A single modal reused for references and first/last keyframes: `ids` is
// whichever list was open when it was clicked (so prev/next only slides
// through that same context), always re-read from state.references by id so
// edits made in the viewer (crop/mute/trim/label) are never stale.

let cropCleanup = null;

function openViewer(ids, index = 0) {
  if (!ids.length) return;
  state.viewer = { ids, index: Math.max(0, Math.min(index, ids.length - 1)) };
  $("#ref-viewer").hidden = false;
  renderViewer();
}

function closeViewer() {
  cropCleanup?.();
  cropCleanup = null;
  state.viewer = null;
  $("#ref-viewer").hidden = true;
  $("#ref-viewer-stage").innerHTML = "";
  $("#ref-viewer-toolbar").innerHTML = "";
}

function viewerNavigate(delta) {
  if (!state.viewer) return;
  const { ids, index } = state.viewer;
  state.viewer.index = (index + delta + ids.length) % ids.length;
  renderViewer();
}

function viewerCurrentReference() {
  return state.viewer ? currentReference(state.viewer.ids[state.viewer.index]) : null;
}

function renderViewer() {
  cropCleanup?.();
  cropCleanup = null;
  const reference = viewerCurrentReference();
  if (!reference) return closeViewer();

  const multiple = state.viewer.ids.length > 1;
  $("#ref-viewer-prev").hidden = !multiple;
  $("#ref-viewer-next").hidden = !multiple;
  $("#ref-viewer-position").textContent = multiple ? `${state.viewer.index + 1} / ${state.viewer.ids.length}` : "";

  const stage = $("#ref-viewer-stage");
  const toolbar = $("#ref-viewer-toolbar");
  const category = referenceCategory(reference);
  const token = referenceToken(reference) || `@${reference.alias}`;
  const header = `
    <div class="ref-viewer-row">
      <button type="button" class="reference-token" data-category="${category}" id="ref-viewer-token">${escapeHtml(token)}</button>
    </div>`;

  if (category === "image") {
    stage.innerHTML = `<div class="crop-frame" id="crop-frame"><img id="viewer-image" src="${reference.preview_url}" alt=""></div>`;
    toolbar.innerHTML = header + `
      <div class="ref-viewer-row">
        <button type="button" class="button ghost" id="viewer-crop-start">Crop…</button>
        <button type="button" class="button" id="viewer-crop-save" hidden>Save crop</button>
        <button type="button" class="button ghost" id="viewer-crop-cancel" hidden>Cancel</button>
      </div>`;
    $("#viewer-crop-start").addEventListener("click", () => startCrop(reference));
  } else if (category === "video") {
    stage.innerHTML = `<video id="viewer-media" src="${reference.preview_url}" controls playsinline ${reference.muted ? "muted" : ""}></video>`;
    const muteDisabled = reference.has_audio === false;
    toolbar.innerHTML = header + `
      <div class="ref-viewer-row">
        <button type="button" class="button ghost" id="viewer-mute-toggle" ${muteDisabled ? "disabled" : ""} title="${muteDisabled ? "This clip has no audio track" : ""}">
          ${reference.muted ? "Unmute (include audio)" : "Mute (strip audio)"}
        </button>
      </div>
      <div id="viewer-trim"></div>`;
    if (!muteDisabled) $("#viewer-mute-toggle").addEventListener("click", () => toggleMute(reference));
    renderTrimTimeline(reference, $("#viewer-media"), stitchMusicTrimCeiling(reference));
  } else if (category === "audio") {
    stage.innerHTML = `<audio id="viewer-media" src="${reference.preview_url}" controls></audio>`;
    toolbar.innerHTML = header + `<div id="viewer-trim"></div>`;
    renderTrimTimeline(reference, $("#viewer-media"), stitchMusicTrimCeiling(reference));
  } else {
    stage.innerHTML = "";
    toolbar.innerHTML = header;
  }

  $("#ref-viewer-token").addEventListener("click", () => insertTokenAtCursor(token));
}

async function toggleMute(reference) {
  try {
    const updated = await Api.patchReference(reference.id, { muted: !reference.muted });
    mergeReferences([updated]);
    renderViewer();
    renderReferenceGallery();
    renderSeedhunterGalleries();
  } catch (error) { toast(error.message, "error"); }
}

/* image crop - a draggable/resizable box over the full-resolution preview,
   cropped client-side onto a canvas and PUT back to replace the reference's
   stored bytes in place (same id/alias/token, just different pixels). */
function startCrop(reference) {
  const frame = $("#crop-frame");
  const img = $("#viewer-image");
  $("#viewer-crop-start").hidden = true;
  $("#viewer-crop-save").hidden = false;
  $("#viewer-crop-cancel").hidden = false;

  const box = document.createElement("div");
  box.className = "crop-box";
  box.id = "crop-box";
  for (const corner of ["nw", "ne", "sw", "se"]) {
    const handle = document.createElement("div");
    handle.className = `crop-handle ${corner}`;
    handle.dataset.corner = corner;
    box.appendChild(handle);
  }
  frame.appendChild(box);

  const seedBox = () => {
    const rect = img.getBoundingClientRect();
    const crop = reference.crop;
    if (crop) {
      // Restore the last saved crop's position/size so re-cropping starts
      // from where the user left off, not a fresh centered guess.
      box.style.left = `${crop.x * rect.width}px`;
      box.style.top = `${crop.y * rect.height}px`;
      box.style.width = `${crop.w * rect.width}px`;
      box.style.height = `${crop.h * rect.height}px`;
    } else {
      const margin = Math.min(rect.width, rect.height) * 0.1;
      box.style.left = `${margin}px`;
      box.style.top = `${margin}px`;
      box.style.width = `${rect.width - margin * 2}px`;
      box.style.height = `${rect.height - margin * 2}px`;
    }
  };
  // Always crop from the pristine original (never a crop of a crop) -
  // reassigning .src resets `complete` to false until the new image loads,
  // except when it's already showing that exact URL (first-ever crop, where
  // original_url and preview_url are the same), in which case it's already
  // loaded and seedBox can run immediately.
  img.src = reference.original_url;
  if (img.complete && img.naturalWidth > 0) seedBox();
  else img.addEventListener("load", seedBox, { once: true });

  bindCropDrag(frame, box);

  $("#viewer-crop-save").onclick = () => saveCrop(reference, img, box);
  $("#viewer-crop-cancel").onclick = () => renderViewer();
}

function bindCropDrag(frame, box) {
  let dragMode = null;
  let startPointer = null;
  let startRect = null;
  let previousUserSelect = "";

  const currentRect = () => ({ left: box.offsetLeft, top: box.offsetTop, width: box.offsetWidth, height: box.offsetHeight });
  const applyStyle = (rect) => {
    box.style.left = `${rect.left}px`;
    box.style.top = `${rect.top}px`;
    box.style.width = `${rect.width}px`;
    box.style.height = `${rect.height}px`;
  };
  // Moving must only ever clamp position, keeping the box's size fixed -
  // clamping width/height here too (as if resizing) made the box shrink by
  // itself whenever it was dragged near the right or bottom edge.
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
    // Fast drags otherwise trigger the browser's own text/element selection
    // as the pointer crosses surrounding UI, which looks like the crop box
    // itself glitching mid-drag.
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

async function saveCrop(reference, img, box) {
  const scaleX = img.naturalWidth / img.clientWidth;
  const scaleY = img.naturalHeight / img.clientHeight;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(box.offsetWidth * scaleX));
  canvas.height = Math.max(1, Math.round(box.offsetHeight * scaleY));
  const ctx = canvas.getContext("2d");
  ctx.drawImage(
    img, box.offsetLeft * scaleX, box.offsetTop * scaleY, box.offsetWidth * scaleX, box.offsetHeight * scaleY,
    0, 0, canvas.width, canvas.height,
  );
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) return toast("Couldn't crop the image.", "error");
  // Normalized to the original's own displayed size, not pixels, so it's
  // still valid however large the crop dialog happens to be next time.
  const crop = {
    x: box.offsetLeft / img.clientWidth, y: box.offsetTop / img.clientHeight,
    w: box.offsetWidth / img.clientWidth, h: box.offsetHeight / img.clientHeight,
  };
  try {
    const updated = await Api.replaceReferenceContent(
      reference.id, blob, `${(reference.filename || "crop").replace(/\.[^.]+$/, "")}.png`, crop,
    );
    mergeReferences([updated]);
    renderViewer();
    renderReferenceGallery();
    renderSeedhunterGalleries();
    renderKeyframes();
    toast("Crop saved.");
  } catch (error) { toast(error.message, "error"); }
}

/* video/audio trim - dual-handle range over the clip's full duration; drag
   the track to scrub the player, drag a handle to move that edge. Saving
   PATCHes trim_start/trim_end, which build_workflow() turns into an actual
   Video Slice / Trim Audio Duration node so the model only ever sees the
   trimmed window (see backend/generation.py's effective_duration()). */
const MAX_TRIM_SECONDS = 15;
// Matches app.py's MIN_TRIM_SECONDS - below this, a low-frame-rate clip can
// drop under MiniMax H3's 5-decoded-frame minimum and the generation errors.
const MIN_TRIM_SECONDS = 1;

function renderTrimTimeline(reference, mediaEl, maxTrimSeconds = MAX_TRIM_SECONDS) {
  const container = $("#viewer-trim");
  const duration = reference.duration || 0;
  if (!duration) { container.innerHTML = ""; return; }
  let localStart = reference.trim_start || 0;
  let localEnd = Math.min(reference.trim_end != null ? reference.trim_end : duration, localStart + maxTrimSeconds);

  container.innerHTML = `
    <div class="trim-zoom-bar">
      <button type="button" class="button ghost trim-zoom-button" id="trim-zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
      <button type="button" class="button ghost trim-zoom-button" id="trim-zoom-in" title="Zoom in (Ctrl/⌘ + scroll on the timeline)" aria-label="Zoom in">+</button>
      <button type="button" class="button ghost trim-zoom-button" id="trim-zoom-fit" title="Show the whole clip">Fit</button>
      <button type="button" class="button ghost trim-zoom-button" id="trim-zoom-selection" title="Zoom to the selected part">Selection</button>
      <span class="trim-zoom-level" id="trim-zoom-level"></span>
    </div>
    <div class="trim-timeline" id="trim-timeline">
      <div class="trim-track" id="trim-track">
        <div class="trim-ticks" id="trim-ticks"></div>
        <div class="trim-range" id="trim-range">
          <div class="trim-handle-middle" id="trim-handle-middle" title="Drag to move the whole selection"></div>
        </div>
        <div class="trim-playhead" id="trim-playhead"></div>
        <div class="trim-handle" id="trim-handle-start" data-handle="start"></div>
        <div class="trim-handle" id="trim-handle-end" data-handle="end"></div>
      </div>
    </div>
    <div class="trim-overview" id="trim-overview" hidden title="The whole clip: drag the box to scroll">
      <div class="trim-overview-range" id="trim-overview-range"></div>
      <div class="trim-overview-window" id="trim-overview-window"></div>
    </div>
    <p class="trim-readout" id="trim-readout"></p>
    <div class="ref-viewer-row">
      <button type="button" class="button" id="trim-save">Save trim</button>
      <button type="button" class="button ghost" id="trim-reset">Reset to full clip</button>
    </div>`;

  const timeline = $("#trim-timeline");
  const track = $("#trim-track");
  const range = $("#trim-range");
  const playhead = $("#trim-playhead");
  const readout = $("#trim-readout");
  const overview = $("#trim-overview");
  // Visible window of the clip (seconds). Long clips open zoomed around the selection.
  const minSpan = Math.min(duration, 0.5);
  const view = { start: 0, end: duration };
  const setView = (start, span) => {
    span = Math.max(minSpan, Math.min(duration, span));
    start = Math.max(0, Math.min(duration - span, start));
    view.start = start;
    view.end = start + span;
  };
  const zoomTo = (span, anchor = (view.start + view.end) / 2, at = 0.5) => setView(anchor - span * at, span);
  const fitSelection = () => {
    const width = Math.max(localEnd - localStart, minSpan);
    zoomTo(Math.min(duration, width * 1.6), (localStart + localEnd) / 2);
  };
  if (Number.isFinite(maxTrimSeconds) && duration > maxTrimSeconds * 4) fitSelection();
  const pct = (t) => ((t - view.start) / (view.end - view.start)) * 100;

  function paintTicks() {
    const span = view.end - view.start;
    const step = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((value) => span / value <= 10) || 1200;
    const label = (t) => (t >= 60 ? `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}` : `${+t.toFixed(2)}s`);
    let html = "";
    for (let t = Math.ceil(view.start / step) * step; t <= view.end + 1e-9; t += step) {
      html += `<span class="trim-tick" style="left:${pct(t)}%"><span>${label(t)}</span></span>`;
    }
    $("#trim-ticks").innerHTML = html;
  }

  function paint() {
    range.style.left = `${pct(localStart)}%`;
    range.style.width = `${Math.max(0, pct(localEnd) - pct(localStart))}%`;
    $("#trim-handle-start").style.left = `${pct(localStart)}%`;
    $("#trim-handle-end").style.left = `${pct(localEnd)}%`;
    if (mediaEl) playhead.style.left = `${pct(mediaEl.currentTime)}%`;
    paintTicks();
    const zoom = duration / (view.end - view.start);
    $("#trim-zoom-level").textContent = zoom > 1.01 ? `${zoom.toFixed(zoom < 10 ? 1 : 0)}× · ${view.start.toFixed(1)}–${view.end.toFixed(1)}s` : "Whole clip";
    $("#trim-zoom-in").disabled = view.end - view.start <= minSpan + 1e-6;
    $("#trim-zoom-out").disabled = $("#trim-zoom-fit").disabled = zoom <= 1.001;
    overview.hidden = zoom <= 1.001;
    $("#trim-overview-range").style.left = `${(localStart / duration) * 100}%`;
    $("#trim-overview-range").style.width = `${((localEnd - localStart) / duration) * 100}%`;
    $("#trim-overview-window").style.left = `${(view.start / duration) * 100}%`;
    $("#trim-overview-window").style.width = `${((view.end - view.start) / duration) * 100}%`;
    const capped = Number.isFinite(maxTrimSeconds) && localEnd - localStart >= maxTrimSeconds - 1e-6;
    readout.textContent = `${localStart.toFixed(2)}s – ${localEnd.toFixed(2)}s `
      + `(${(localEnd - localStart).toFixed(2)}s of ${duration.toFixed(2)}s`
      + (Number.isFinite(maxTrimSeconds) ? `, max ${maxTrimSeconds.toFixed(1)}s)` : ")")
      + (capped ? " — at the max clip length" : "");
  }
  paint();

  // Pointer -> clip time inside the visible window; dragging past an edge scrolls the view along.
  const timeFromEvent = (event, autoPan = false) => {
    const rect = track.getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    if (autoPan && (fraction < 0.03 || fraction > 0.97)) {
      const span = view.end - view.start;
      setView(view.start + (fraction < 0.03 ? -1 : 1) * span * 0.02, span);
    }
    return view.start + Math.max(0, Math.min(1, fraction)) * (view.end - view.start);
  };

  function dragHandle(id, onMove) {
    $(id).addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const move = (moveEvent) => { onMove(timeFromEvent(moveEvent, true)); paint(); };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }

  dragHandle("#trim-handle-start", (t) => {
    const lowerBound = Math.max(0, localEnd - maxTrimSeconds);
    const upperBound = localEnd - MIN_TRIM_SECONDS;
    localStart = Math.max(lowerBound, Math.min(t, upperBound));
    if (mediaEl) mediaEl.currentTime = localStart;
  });
  dragHandle("#trim-handle-end", (t) => {
    const upperBound = Math.min(duration, localStart + maxTrimSeconds);
    const lowerBound = localStart + MIN_TRIM_SECONDS;
    localEnd = Math.min(upperBound, Math.max(t, lowerBound));
    if (mediaEl) mediaEl.currentTime = localEnd;
  });
  // Dotted middle handle: drags the whole window without changing its length.
  let middleDragStart = null;
  $("#trim-handle-middle").addEventListener("pointerdown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    middleDragStart = { pointerTime: timeFromEvent(event), start: localStart, end: localEnd };
    const move = (moveEvent) => {
      const pointer = timeFromEvent(moveEvent, true);
      const delta = pointer - middleDragStart.pointerTime;
      const width = middleDragStart.end - middleDragStart.start;
      let newStart = Math.max(0, Math.min(middleDragStart.start + delta, duration - width));
      localStart = newStart;
      localEnd = newStart + width;
      if (mediaEl) mediaEl.currentTime = localStart;
      paint();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });

  timeline.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".trim-handle, .trim-handle-middle")) return;
    if (!mediaEl) return;
    // Clicking the selected range previews it from its start; clicking the
    // empty track outside it scrubs to wherever was clicked, same as before.
    mediaEl.currentTime = event.target.closest("#trim-range") ? localStart : timeFromEvent(event);
  });
  mediaEl?.addEventListener("timeupdate", () => {
    const t = mediaEl.currentTime;
    if (!mediaEl.paused && (t < view.start || t > view.end)) setView(t - (view.end - view.start) * 0.1, view.end - view.start);
    paint();
  });

  // Zoom: buttons, Ctrl/⌘ + wheel around the pointer, plain wheel pans when zoomed, pinch on touch.
  const zoomAround = (factor, anchor, at) => { zoomTo((view.end - view.start) * factor, anchor, at); paint(); };
  $("#trim-zoom-in").addEventListener("click", () => zoomAround(0.5, mediaEl && mediaEl.currentTime >= view.start && mediaEl.currentTime <= view.end ? mediaEl.currentTime : (localStart + localEnd) / 2, 0.5));
  $("#trim-zoom-out").addEventListener("click", () => zoomAround(2, (view.start + view.end) / 2, 0.5));
  $("#trim-zoom-fit").addEventListener("click", () => { setView(0, duration); paint(); });
  $("#trim-zoom-selection").addEventListener("click", () => { fitSelection(); paint(); });
  timeline.addEventListener("wheel", (event) => {
    const rect = track.getBoundingClientRect();
    const at = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      zoomAround(Math.exp(event.deltaY * 0.002), view.start + at * (view.end - view.start), at);
    } else if (view.end - view.start < duration - 1e-6) {
      event.preventDefault();
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      setView(view.start + (delta / rect.width) * (view.end - view.start), view.end - view.start);
      paint();
    }
  }, { passive: false });
  const touches = new Map();
  let pinch = null;
  timeline.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "touch") return;
    touches.set(event.pointerId, event.clientX);
    if (touches.size === 2) {
      const [a, b] = [...touches.values()];
      const rect = track.getBoundingClientRect();
      pinch = { distance: Math.abs(a - b) || 1, span: view.end - view.start, at: ((a + b) / 2 - rect.left) / rect.width };
      pinch.anchor = view.start + pinch.at * pinch.span;
    }
  });
  timeline.addEventListener("pointermove", (event) => {
    if (!touches.has(event.pointerId)) return;
    touches.set(event.pointerId, event.clientX);
    if (pinch && touches.size === 2) {
      const [a, b] = [...touches.values()];
      zoomTo(pinch.span * pinch.distance / (Math.abs(a - b) || 1), pinch.anchor, pinch.at);
      paint();
    }
  });
  const endTouch = (event) => { touches.delete(event.pointerId); if (touches.size < 2) pinch = null; };
  timeline.addEventListener("pointerup", endTouch);
  timeline.addEventListener("pointercancel", endTouch);
  // Overview: click to centre the view there, drag the box to scroll.
  overview.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    const rect = overview.getBoundingClientRect();
    const span = view.end - view.start;
    const scrollTo = (clientX) => { setView(((clientX - rect.left) / rect.width) * duration - span / 2, span); paint(); };
    scrollTo(event.clientX);
    const move = (moveEvent) => scrollTo(moveEvent.clientX);
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });

  $("#trim-save").addEventListener("click", async () => {
    try {
      const updated = await Api.patchReference(reference.id, { trim_start: localStart.toFixed(3), trim_end: localEnd.toFixed(3) });
      mergeReferences([updated]);
      renderViewer();
      renderReferenceGallery();
      renderSeedhunterGalleries();
      toast("Trim saved.");
    } catch (error) { toast(error.message, "error"); }
  });
  $("#trim-reset").addEventListener("click", async () => {
    try {
      const updated = await Api.patchReference(reference.id, { trim_start: 0, trim_end: duration });
      mergeReferences([updated]);
      renderViewer();
      renderReferenceGallery();
      renderSeedhunterGalleries();
    } catch (error) { toast(error.message, "error"); }
  });
}

function bindDropZones(parent) {
  $$('[data-drop-role]', parent).forEach((zone) => {
    for (const eventName of ["dragenter", "dragover"]) zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.add("is-dragging"); });
    for (const eventName of ["dragleave", "drop"]) zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.remove("is-dragging"); });
    zone.addEventListener("drop", (event) => uploadFiles([...event.dataTransfer.files], zone.dataset.dropRole));
  });
}

const UPLOAD_ROLE_CONFIG = {
  first: { typePrefix: "image/", max: 1, error: "Choose a PNG, JPEG, WEBP, or BMP image." },
  last: { typePrefix: "image/", max: 1, error: "Choose a PNG, JPEG, WEBP, or BMP image." },
  // MiniMax H3's Ref2V node accepts images, videos, and audio as references
  // (see MiniMaxH3ReferenceToVideo) - one widget, three accepted kinds. Actual
  // per-kind caps (9 images / 3 videos / 3 audio, 15s combined video and
  // audio duration) are enforced server-side at upload time; `max` here is
  // just a sane per-drop batch cap, not the real limit.
  references: {
    typePrefix: ["image/", "video/", "audio/"], max: 15,
    error: "Choose a PNG/JPEG/WEBP/BMP image, an MP4/WEBM/MOV/MKV video, or an MP3/WAV/M4A/FLAC/OGG audio file.",
  },
  "sh-first": { typePrefix: "image/", max: 1, error: "Choose a PNG, JPEG, WEBP, or BMP image." },
  "sh-last": { typePrefix: "image/", max: 1, error: "Choose a PNG, JPEG, WEBP, or BMP image." },
  "sh-images": { typePrefix: "image/", max: 6, error: "Choose a PNG, JPEG, WEBP, or BMP image." },
  "sh-videos": { typePrefix: "video/", max: 3, error: "Choose an MP4, WEBM, MOV, or MKV video." },
  "sh-audios": { typePrefix: "audio/", max: 3, error: "Choose an MP3, WAV, M4A, FLAC, or OGG audio file." },
  "stitch-music": { typePrefix: "audio/", max: 1, error: "Choose an MP3, WAV, M4A, FLAC, or OGG audio file." },
};

// After an upload, if a video or audio clip's raw length pushes its whole
// category over the 15s combined cap, jump straight into the trim editor for
// it instead of leaving the user to notice the totals and hunt for the fix.
function openTrimEditorIfOverBudget(uploaded) {
  const ltx = state.model === "ltx_2_5";
  const overBudget = uploaded.find((reference) => {
    const category = referenceCategory(reference);
    if (ltx) return category === "audio" && (effectiveDuration(reference) || 0) > LTX_MSR_LIMITS.audioSeconds;
    if (category !== "video" && category !== "audio") return false;
    const limitSeconds = category === "video" ? REFERENCE_LIMITS.videoSeconds : REFERENCE_LIMITS.audioSeconds;
    const totalSeconds = referencesByRole("references")
      .filter((item) => referenceCategory(item) === category)
      .reduce((sum, item) => sum + (effectiveDuration(item) || 0), 0);
    return totalSeconds > limitSeconds;
  });
  if (!overBudget) return;
  const ids = referencesByRole("references").map((item) => item.id);
  openViewer(ids, ids.indexOf(overBudget.id));
  const category = referenceCategory(overBudget);
  if (ltx) return toast(`${overBudget.filename} is over LTX Reference mode's ${LTX_MSR_LIMITS.audioSeconds}s per-voice limit - trim it below.`, "error");
  const limitSeconds = category === "video" ? REFERENCE_LIMITS.videoSeconds : REFERENCE_LIMITS.audioSeconds;
  toast(`${overBudget.filename} pushes ${category} references over the ${limitSeconds.toFixed(1)}s combined limit - trim it below.`, "error");
}

async function uploadFiles(files, role) {
  const config = UPLOAD_ROLE_CONFIG[role] || UPLOAD_ROLE_CONFIG.references;
  const prefixes = Array.isArray(config.typePrefix) ? config.typePrefix : [config.typePrefix];
  const matching = files.filter((file) => prefixes.some((prefix) => file.type.startsWith(prefix)));
  if (!matching.length) return toast(config.error, "error");
  try {
    const uploaded = [];
    for (const file of matching.slice(0, config.max)) uploaded.push(await Api.upload(file, role));
    mergeReferences(uploaded);
    if (role === "first") {
      if (state.firstFrameId) restoreResolutionSnapshot(); // replacing a frame undoes any pending match
      state.firstFrameId = uploaded[0].id;
    }
    if (role === "last") state.lastFrameId = uploaded[0].id;
    if (role === "sh-first") state.shFirstFrameId = uploaded[0].id;
    if (role === "sh-last") state.shLastFrameId = uploaded[0].id;
    if (role === "stitch-music") state.stitchMusicId = uploaded[0].id;
    renderKeyframes();
    renderReferenceGallery();
    renderSeedhunterGalleries();
    renderStitchMusic();
    if (role === "references") openTrimEditorIfOverBudget(uploaded);
  } catch (error) { toast(error.message, "error"); }
}

async function removeReference(id) {
  try {
    await Api.request(`${API_BASE}/api/references/${id}`, { method: "DELETE" });
    state.references = state.references.filter((reference) => reference.id !== id);
    if (state.firstFrameId === id) state.firstFrameId = null;
    if (state.lastFrameId === id) state.lastFrameId = null;
    if (state.shFirstFrameId === id) state.shFirstFrameId = null;
    if (state.shLastFrameId === id) state.shLastFrameId = null;
    if (state.stitchMusicId === id) state.stitchMusicId = null;
    renderKeyframes();
    renderReferenceGallery();
    renderSeedhunterGalleries();
    renderStitchMusic();
  } catch (error) { toast(error.message, "error"); }
}

/* -------------------------------------------------------- model/mode -- */

function setMode(mode) {
  state.mode = mode;
  $("#mode").value = mode;
  // Model-aware: minimax_h3 is the only model whose fl2v/ref2v modes actually
  // use the shared #keyframe-panel/#reference-panel - seedhunter has its own
  // dedicated reference panel and must never show these, regardless of what
  // `mode` happens to hold left over from a previous model.
  const allowed = MODELS[state.model].modesAllowed;
  $("#keyframe-panel").hidden = !(allowed.includes("fl2v") && mode === "fl2v");
  $("#reference-panel").hidden = !(allowed.includes("ref2v") && mode === "ref2v");
  // Only when that mode's turbo LoRA actually applies - the 6/8 counts are
  // what Parasyte/LightX were tuned for; with them off there's no reason to
  // overwrite whatever Steps the user set.
  if (state.model === "minimax_h3") {
    const turbo = speedupStatus(mode)[mode === "ref2v" ? "lightx" : "parasyte"];
    if (!turbo.locked && turbo.box.checked) $("#steps").value = mode === "ref2v" ? 8 : 6;
  }
  // Ref2v now supports the same checkpoint picker as Text/FL2V - build_workflow()
  // already bypasses the turbo LoRA/AdaLN fix for any checkpoint other than
  // REF2V_TURBO_LORA_CHECKPOINT (see backend/generation.py), so switching
  // checkpoints here is safe in every mode; "Workflow default" still resolves
  // to the tuned ref2v checkpoint, keeping the fix active.
  $("#minimax-checkpoint-row").hidden = Boolean(listLoras());
  if (mode === "text") restoreResolutionSnapshot();
  renderKeyframes(); // refreshes #first-frame-match visibility, which also depends on state.model
  renderReferenceGallery(); // limits/hint differ between MiniMax H3 and LTX 2.5
  closeAliasMenu();
  renderSpeedups();
  renderPromptSnippets();
  if (state.options) renderCheckpointOptions();
  scheduleEstimate();
}

function setModel(model) {
  const previousModel = state.model;
  if (previousModel !== model) {
    promptDrafts[previousModel] = $("#prompt").value;
    $("#prompt").value = promptDrafts[model] || defaultPrompts[model];
  }
  state.model = model;
  $("#model").value = model;
  const config = MODELS[model];
  Object.values(MODELS).forEach((entry) => { $(entry.advancedPanel).hidden = entry !== config; });
  $("#minimax-model-details").hidden = Boolean(listLoras(model));
  Object.entries(LIST_LORAS).forEach(([key, list]) => { $(list.details).hidden = model !== key; });
  $("#sh-references-card").hidden = model !== "seedhunter";
  $("#sh-finalize-card").hidden = model !== "seedhunter";
  $("#sampling-model-label").textContent = config.label;
  updateLoraCount();
  $("#minimax-checkpoint-row").hidden = Boolean(listLoras(model));
  $("#h3-speedups").hidden = model !== "minimax_h3"; // SeedHunter/LTX workflows don't have these nodes
  $("#h3-multi-gpu-card").hidden = Boolean(listLoras(model));
  $("#h3-upscale-card").hidden = model !== "minimax_h3"; // SeedHunter upscales in its finalize pass

  const modeFieldRelevant = config.modesAllowed.length > 0;
  $("#mode-row").hidden = !modeFieldRelevant;
  $('#mode option[value="ref2v"]').disabled = !config.modesAllowed.includes("ref2v");
  $('#mode option[value="ref2v"]').hidden = !config.modesAllowed.includes("ref2v");
  $('#mode option[value="fl2v"]').disabled = !config.modesAllowed.includes("fl2v");
  $('#mode option[value="fl2v"]').hidden = !config.modesAllowed.includes("fl2v");

  $("#prompt").placeholder = model === "ltx_2_5" ? "Describe one continuous LTX 2.5 shot..."
    : model === "wan_2_2" ? "Describe one Wan 2.2 shot: subject, scene, motion, camera, lighting and style (no sound)..."
    : model === "seedhunter" ? "Describe your shot - use Sections/< in the ComfyUI Prompt IDE style, or just write naturally."
    : "Describe your video...";
  applyDurationBounds(config);
  // Always refresh keyframe/reference panel visibility for the new model, even
  // when the mode value itself isn't changing - it was previously only done
  // conditionally, which left stale panels visible after switching models.
  setMode(config.modesAllowed.includes(state.mode) ? state.mode : (config.modesAllowed[0] || state.mode));
  if (model === "seedhunter") renderSeedhunterGalleries();
  if (state.options) renderPresetBar();
  return updateResolution();
}

// The Duration readout. MiniMax H3 renders duration * 24 frames snapped up to
// its 17k+5 grid, kept within its trained 124-362 frames (generation.h3_frame_count),
// so show what it really renders: 15s -> 362 frames = 15.08s.
const H3_MAX_FRAMES = 362;
const H3_MIN_FRAMES = 124;
function h3FrameCount(seconds) {
  let frames = Math.max(H3_MIN_FRAMES, Math.round(seconds * 24));
  frames += (5 - (frames % 17) + 17) % 17;
  while (frames > H3_MAX_FRAMES) frames -= 17;
  return frames;
}

function durationReadout(seconds) {
  if (state.model !== "minimax_h3") return `${seconds}s`;
  const frames = h3FrameCount(Number(seconds));
  return `${seconds}s · ${frames} frames = ${(frames / 24).toFixed(2)}s`;
}

// The Duration bar: a styled fill + ruler drawn over the (invisible) native
// range input, which still does all the input handling. Positions use the
// same thumb-inset math as the browser: thumb center runs from w/2 to 100%-w/2.
function updateDurationMeter() {
  const input = $("#duration");
  const min = Number(input.min), max = Number(input.max), value = Number(input.value);
  const at = (seconds) => (max > min ? (seconds - min) / (max - min) : 1);
  $("#duration-meter").style.setProperty("--p", at(value));
  $("#duration-readout").textContent = durationReadout(value);
  input.setAttribute("aria-valuetext", `${value} seconds`);
  const ruler = $("#duration-ruler");
  if (ruler.dataset.range !== `${min}-${max}`) {
    ruler.dataset.range = `${min}-${max}`;
    const ticks = [];
    for (let seconds = min; seconds <= max; seconds++) {
      const major = seconds === min || seconds === max || seconds % 5 === 0;
      ticks.push(`<span class="duration-tick${major ? " is-major" : ""}" style="--p:${at(seconds)}">${major ? `<b>${seconds}s</b>` : ""}</span>`);
    }
    ruler.innerHTML = ticks.join("");
  }
  $$(".duration-tick", ruler).forEach((tick, index) => tick.classList.toggle("is-filled", min + index <= value));
}

function bindDurationMeter() {
  const input = $("#duration");
  // Retry/options/model switches set .value directly, which fires no "input".
  const valueProperty = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  Object.defineProperty(input, "value", {
    get() { return valueProperty.get.call(this); },
    set(value) { valueProperty.set.call(this, value); updateDurationMeter(); },
  });
  input.addEventListener("input", updateDurationMeter);
  // "Match length" leaves free steps; touching the slider goes back to whole seconds.
  ["pointerdown", "keydown"].forEach((type) => input.addEventListener(type, () => { input.step = "1"; }));
  updateDurationMeter();
}

function applyDurationBounds(config) {
  const input = $("#duration");
  const before = Number(input.value); // read first: a range input clamps itself when max drops
  input.min = config.minDuration;
  input.max = config.maxDuration || 15;
  updateDurationMeter(); // the ruler starts at the new minimum
  if (before < config.minDuration || before > Number(input.max)) {
    input.value = before > Number(input.max) ? config.defaultDuration || input.max : config.minDuration;
    $("#duration-readout").textContent = durationReadout(Number(input.value));
  }
  const hint = $("#duration-hint");
  hint.hidden = config.minDuration <= 1;
  if (!hint.hidden) hint.textContent = `${config.label} needs at least ${config.minDuration}s - shorter clips fall outside its trained range and tend to look broken.`;
}

/* -------------------------------------------------------------- status -- */

function renderStatus() {
  const comfy = state.serviceStatus.comfyui || {};
  const qwen = state.serviceStatus.qwen || {};
  setServiceNode($("#comfy-status"), comfy, comfy.online ? (comfy.rendering ? `${comfy.rendering} rendering` : "Ready") : (comfy.starting ? "Starting" : "Offline"));
  $("span:not(.status-dot)", $("#qwen-status")).textContent = "Llama";
  setServiceNode($("#qwen-status"), { online: !!state.llamaOnline }, state.llamaOnline ? "Ready" : "Offline");
}

function setServiceNode(node, service, label) {
  $(".status-dot", node).classList.toggle("is-online", Boolean(service.online));
  $(".status-dot", node).classList.toggle("is-busy", Boolean(service.starting || service.rendering));
  $("strong", node).textContent = label;
}

/* ---------------------------------------------------------------- jobs -- */

function elapsed(job) {
  const start = new Date(job.started_at || job.created_at).getTime();
  const end = job.completed_at ? new Date(job.completed_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatSeconds(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
}

// "Estimated time to complete" under the form: average render time of the
// last 10 finished runs with these same settings (fewer if that's all there is).
let etaTimer = null;
let etaRequest = 0;
function scheduleEstimate() {
  clearTimeout(etaTimer);
  etaTimer = setTimeout(refreshEstimate, 350);
}

async function refreshEstimate() {
  const ticket = ++etaRequest;
  let result;
  try {
    result = await Api.request(`${API_BASE}/api/estimate`, { method: "POST", body: collectSettings(), signal: timeoutSignal(20_000) });
  } catch {
    return;
  }
  if (ticket !== etaRequest) return; // a newer request is on its way
  renderScore(result.score);
  renderCompileStatus(result.torch_compile);
  const value = $("#eta-value");
  const basis = $("#eta-basis");
  $("#eta").classList.toggle("is-empty", !result.seconds);
  // Yellow = no run with exactly these settings; worked out from runs that
  // differ only in steps (estimates.step_fit), so even more of a guess.
  $("#eta").classList.toggle("is-fitted", Boolean(result.fit));
  value.title = basis.title = result.fit ? stepFitTitle(result.fit) : "";
  if (!result.seconds) {
    value.textContent = "—";
    basis.textContent = "no finished runs with these settings yet";
    return;
  }
  value.textContent = `~${formatSeconds(result.seconds)}`;
  if (result.fit) {
    const fit = result.fit;
    basis.textContent = `≈ ${formatSeconds(fit.overhead)} + ${fit.per_step.toFixed(1)}s/step · fit from ${fit.runs} runs${fit.extrapolated ? " (extrapolated)" : ""}`;
    return;
  }
  basis.textContent = result.samples === 1 ? "from 1 run" : `avg of last ${result.samples} runs`;
}

function stepFitTitle(fit) {
  return `No run with exactly these settings yet. Least-squares line through ${fit.runs} run(s) that differ only in steps `
    + `(${fit.steps.join(", ")}): time ≈ ${fit.overhead}s fixed + ${fit.per_step}s per step`
    + (fit.r2 != null ? `, R² ${fit.r2}` : "") + (fit.extrapolated ? ". This step count is outside the measured range, so it's extrapolated." : ".");
}

// torch.compile state for the current settings (app.py torch_compile_status).
// Compiled runs never feed the estimate - compile time varies - so this line
// says whether the next run starts warm or has to compile ("cool off").
function renderCompileStatus(status) {
  const node = $("#eta-compile");
  node.hidden = !status;
  if (!status) return;
  node.dataset.status = status.status;
  node.innerHTML = {
    cold: `${uiIcon("settings")} torch.compile: the first run compiles first. That extra time varies and isn't in the estimate.`,
    restarted: `${uiIcon("alert")} torch.compile cooled off: ComfyUI restarted since the last compiled run, so this run compiles again (extra time varies).`,
    warm: `${uiIcon("settings")} torch.compile is warm for these settings. Compile time isn't in the estimate.`,
    changed: `${uiIcon("alert")} torch.compile will cool off: changing ${escapeHtml(status.changes.join(", "))} from the last compiled run means it compiles again (extra time varies).`,
  }[status.status] || "";
}

// Thumbs score for the current settings: every rated video with the same
// look-affecting settings (estimates.quality_settings), no cap.
function renderScore(score) {
  const node = $("#eta-score");
  const total = score ? score.good + score.bad : 0;
  node.classList.toggle("is-empty", !total);
  if (!total) {
    node.classList.remove("is-legendary");
    node.textContent = "no rated videos with these settings yet";
    node.title = "";
    return;
  }
  const percent = Math.round(score.good / total * 100);
  // An extreme thumbs up counts as good too; `best` breaks those out.
  const best = score.best || 0;
  node.innerHTML = `${best ? `${uiIcon("star")} ${best} · ` : ""}${uiIcon("thumbsUp")} ${score.good - best} · ${uiIcon("thumbsDown")} ${score.bad} (${percent}% good)`;
  node.title = `${score.good} of ${total} rated video(s) with these settings were good` +
    (best ? `, ${best} of them best quality possible` : "");
  node.dataset.tone = percent >= 70 ? "good" : percent >= 40 ? "mixed" : "bad";
  node.classList.toggle("is-legendary", Boolean(score.legendary)); // lots of best-quality results
}

// A running card's time left: its estimate (fixed when it was queued) minus
// time spent rendering so far.
function jobEta(job) {
  const seconds = job.estimate?.seconds;
  if (!seconds || !isActiveJob(job)) return "";
  if (!job.started_at) return `est. ${formatSeconds(seconds)}`;
  const left = seconds - (Date.now() - new Date(job.started_at).getTime()) / 1000;
  return left > 0 ? `~${formatSeconds(left)} left` : "running over estimate";
}

function isActiveJob(job) {
  return ACTIVE_STATES.has(job.state);
}

function jobsNeedRender(previousJobs, nextJobs) {
  if (previousJobs.length !== nextJobs.length) return true;
  return previousJobs.some((previous, index) => {
    const next = nextJobs[index];
    return !next || jobRenderKey(previous) !== jobRenderKey(next);
  });
}

// Runs every second: writes only what changed (same-value writes still dirty
// style/layout) and skips hidden tabs; the next tick catches up.
const setText = (node, text) => { if (node && node.textContent !== text) node.textContent = text; };
function updateJobMetrics() {
  if (document.hidden) return;
  updateMobileTabBadge();
  const byId = new Map(state.jobs.map((job) => [job.id, job]));
  $$('[data-job-id]', $("#jobs-list")).forEach((card) => {
    const job = byId.get(card.dataset.jobId);
    if (job) updateCardMetrics(card, job);
  });
  for (const id of progressPace.keys()) if (!byId.has(id)) progressPace.delete(id);
}

// Active cards only, 4x a second: the smoothed bar, elapsed and ETA stay live
// without touching every finished card in the list.
function updateActiveJobMetrics() {
  if (document.hidden) return;
  const list = $("#jobs-list");
  state.jobs.forEach((job) => {
    if (!isActiveJob(job)) return;
    const card = $(`[data-job-id="${CSS.escape(job.id)}"]`, list);
    if (card) updateCardMetrics(card, job);
  });
}

function updateCardMetrics(card, job) {
  const stateNode = $(".job-state", card);
  const stateClass = `job-state state-${job.state}`;
  if (stateNode.className !== stateClass) stateNode.className = stateClass;
  setText(stateNode, jobStateText(job));
  setText($(".job-elapsed", card), elapsed(job));
  setText($(".job-eta", card), jobEta(job));
  const phase = $(".job-phase", card);
  if (phase) {
    const current = jobPhase(job);
    const entering = current > 1 && String(current) !== phase.dataset.phase;
    phase.dataset.phase = String(current);
    const bar = $(".job-main-bar", phase);
    if (entering) bar.style.transition = "none"; // restart from 0 instead of sliding back from full
    phase.classList.toggle("is-phase2", current === 2);
    phase.classList.toggle("is-phase3", current === 3);
    const width = `${jobProgressWidth(job)}%`;
    if (bar.style.width !== width) bar.style.width = width;
    if (entering) { void bar.offsetWidth; bar.style.transition = ""; }
    let label = $(".job-phase-label", phase);
    if (!label && job.phase2) { // the card was drawn before the server knew this job has a phase 2
      label = document.createElement("span");
      label.className = "job-phase-label";
      phase.prepend(label);
    }
    setText(label, jobPhaseLabel(job));
    const track = $(".job-progress", phase);
    const now = String(Math.round(jobProgressWidth(job)));
    if (track.getAttribute("aria-valuenow") !== now) track.setAttribute("aria-valuenow", now);
    const aria = jobPhaseLabel(job) || "Progress";
    if (track.getAttribute("aria-label") !== aria) track.setAttribute("aria-label", aria);
  }
  const retry = $("[data-retry]", card);
  if (retry && !retryAvailable(job)) retry.remove(); // 42h window closed while the page was open
}

/* -------------------------------------------------------------- stitch -- */

function isStitchable(job) {
  return Boolean(job && job.state === "complete" && job.output_url);
}

function pruneStitchSelection() {
  state.stitchSelection = state.stitchSelection.filter((id) => isStitchable(state.jobs.find((job) => job.id === id)));
}

function toggleStitchMode(force) {
  state.stitchMode = force !== undefined ? force : !state.stitchMode;
  if (state.stitchMode && state.compareMode) toggleCompareMode(false);
  if (!state.stitchMode) {
    state.stitchSelection = [];
    $("#stitch-settings").hidden = true;
    $("#stitch-settings-toggle").classList.remove("is-enabled");
    $("#stitch-settings-toggle").setAttribute("aria-pressed", "false");
  }
  renderStitchBar();
  applyStitchModeToCards();
}

// A family with variants (Fade/Wipe/Slide/Circle) isn't itself a real xfade
// transition name - the actual value to send is whatever the (possibly
// hidden, if the user never opened it) variant picker holds, defaulting to
// that family's first variant.
function stitchTransitionFamily() {
  return (state.options.stitch_transitions || []).find((item) => item.value === $("#stitch-transition").value);
}

function resolvedStitchTransition() {
  const family = stitchTransitionFamily();
  if (!family?.variants?.length) return family ? family.value : "cut";
  return $("#stitch-transition-variant").value || family.variants[0][0];
}

function updateStitchTransitionFields() {
  const family = stitchTransitionFamily();
  const variants = family?.variants || [];
  $("#stitch-transition-variant-row").hidden = !variants.length;
  if (variants.length) {
    $("#stitch-transition-variant-label").textContent = family.variant_label || "Variant";
    fillOptions("#stitch-transition-variant", variants.map(([value, label]) => ({ value, label })));
  }
  $("#stitch-crossfade-row").hidden = $("#stitch-transition").value === "cut";
}

function collectStitchSettings() {
  const customResolution = $("#stitch-resolution-mode").value === "custom";
  return {
    transition: resolvedStitchTransition(),
    crossfade_seconds: Number($("#stitch-crossfade-seconds").value) || 0.5,
    fps: Number($("#stitch-fps").value) || 24,
    match_fps: $("#stitch-fps-match").checked,
    width: customResolution ? Number($("#stitch-width").value) || null : null,
    height: customResolution ? Number($("#stitch-height").value) || null : null,
    music_reference_id: state.stitchMusicId,
    music_volume: Number($("#stitch-music-volume").value) / 100,
  };
}

function stitchSelectionTotalSeconds() {
  return state.stitchSelection.reduce((sum, id) => {
    const job = state.jobs.find((item) => item.id === id);
    return sum + (Number(job?.duration) || 0);
  }, 0);
}

// Background music is trimmed with the same editor every other audio
// reference uses, but its ceiling is the currently selected stitch's total
// length (it gets looped/truncated to fit that regardless) rather than
// MiniMax H3's fixed 15s reference cap. No clips selected yet -> no
// meaningful ceiling, so let the full uploaded file be trimmed freely.
function stitchMusicTrimCeiling(reference) {
  if (reference.role !== "stitch-music") return MAX_TRIM_SECONDS;
  const total = stitchSelectionTotalSeconds();
  return total > 0 ? total : Infinity;
}

function renderStitchMusic() {
  const reference = currentReference(state.stitchMusicId);
  renderReferenceRows("#stitch-music-list", reference ? [reference] : []);
  $("#stitch-music-count").textContent = reference ? "1 track attached" : "No background music attached";
  $("#stitch-music-upload").textContent = reference ? "Replace music…" : "+ Add music…";
  $("#stitch-music-volume-row").hidden = !reference;
}

function toggleStitchSelection(jobId) {
  if (!isStitchable(state.jobs.find((job) => job.id === jobId))) return;
  const index = state.stitchSelection.indexOf(jobId);
  if (index === -1) state.stitchSelection.push(jobId); else state.stitchSelection.splice(index, 1);
  renderStitchBar();
  applyStitchModeToCards();
}

function applyStitchModeToCards() {
  const host = $("#jobs-list");
  if (state.compareMode) { applyCompareModeToCards(); return; }
  host.classList.remove("compare-mode");
  host.classList.toggle("stitch-mode", state.stitchMode);
  $$('[data-job-id]', host).forEach((card) => {
    const job = state.jobs.find((item) => item.id === card.dataset.jobId);
    const stitchable = isStitchable(job);
    card.classList.toggle("stitch-selectable", state.stitchMode && stitchable);
    card.classList.toggle("stitch-ineligible", state.stitchMode && !stitchable);
    card.classList.toggle("is-selected", state.stitchMode && state.stitchSelection.includes(card.dataset.jobId));
  });
}

function renderStitchBar() {
  pruneStitchSelection();
  $("#stitch-bar").hidden = !state.stitchMode;
  $("#stitch-toggle").classList.toggle("is-active", state.stitchMode);
  $("#stitch-summary").textContent = `${state.stitchSelection.length} selected · ${stitchSelectionTotalSeconds().toFixed(1)}s total`;
  $("#stitch-complete").disabled = state.stitchSelection.length < 2;
}

/* ------------------------------------------------------------ compare -- */

// Compare: pick two finished generations (next to Stitch); the viewer opens
// as soon as the second is picked. It shows them as big as they fit: side by
// side or stacked, whichever gives the videos more area at this window size.
const compare = { selection: [], layout: "auto", sound: "off", playing: true, show: "0" };

function toggleCompareMode(force) {
  state.compareMode = force !== undefined ? force : !state.compareMode;
  compare.selection = [];
  if (state.compareMode && state.stitchMode) toggleStitchMode(false);
  $("#compare-bar").hidden = !state.compareMode;
  $("#compare-toggle").classList.toggle("is-active", Boolean(state.compareMode));
  renderCompareBar();
  applyStitchModeToCards();
}

function renderCompareBar() {
  const picked = compare.selection.length;
  $("#compare-summary").textContent = picked ? `1 picked - pick 1 more to compare` : "Pick 2 generations to compare";
}

function toggleCompareSelection(jobId) {
  if (!isStitchable(state.jobs.find((job) => job.id === jobId))) return;
  const index = compare.selection.indexOf(jobId);
  if (index !== -1) compare.selection.splice(index, 1);
  else compare.selection.push(jobId);
  renderCompareBar();
  applyCompareModeToCards();
  if (compare.selection.length === 2) {
    const ids = [...compare.selection];
    toggleCompareMode(false);
    openCompare(ids);
  }
}

function applyCompareModeToCards() {
  const host = $("#jobs-list");
  host.classList.remove("stitch-mode");
  host.classList.add("compare-mode");
  $$('[data-job-id]', host).forEach((card) => {
    const pickable = isStitchable(state.jobs.find((item) => item.id === card.dataset.jobId));
    card.classList.remove("stitch-selectable", "stitch-ineligible", "is-selected");
    card.classList.toggle("compare-selectable", pickable);
    card.classList.toggle("compare-ineligible", !pickable);
    card.classList.toggle("is-compare-picked", compare.selection.includes(card.dataset.jobId));
  });
}

function clearCompareClasses() {
  $("#jobs-list").classList.remove("compare-mode");
  $$('[data-job-id]', $("#jobs-list")).forEach((card) => card.classList.remove("compare-selectable", "compare-ineligible", "is-compare-picked"));
}

function compareCaption(job, letter) {
  const size = job.width && job.height ? `${job.width}×${job.height}` : "";
  const duration = job.duration ? `${Number(job.duration).toFixed(1)}s` : "";
  const bits = [job.model_label, job.mode_label, size, duration].filter(Boolean).join(" · ");
  return `<strong>${letter}</strong> ${escapeHtml(bits)}<span>${escapeHtml(String(job.prompt || "").slice(0, 140))}</span>`;
}

function openCompare(ids) {
  compare.ids = ids;
  compare.playing = true;
  const panes = $$(".compare-pane", $("#compare-stage"));
  ids.forEach((id, index) => {
    const job = state.jobs.find((item) => item.id === id);
    const video = $("video", panes[index]);
    video.src = job.output_url;
    video.currentTime = 0;
    video.addEventListener("loadedmetadata", layoutCompare, { once: true });
    $("figcaption", panes[index]).innerHTML = compareCaption(job, index ? "B" : "A");
  });
  setCompareSound(compare.sound);
  $("#compare-viewer").hidden = false;
  document.body.classList.add("is-comparing");
  layoutCompare();
  playCompare(true);
}

function closeCompare() {
  $("#compare-viewer").hidden = true;
  document.body.classList.remove("is-comparing");
  $$("#compare-stage video").forEach((video) => { video.pause(); video.removeAttribute("src"); video.load(); });
  clearCompareClasses();
}

function playCompare(play) {
  compare.playing = play;
  $$("#compare-stage video").forEach((video) => (play ? video.play().catch(() => {}) : video.pause()));
  $("#compare-play").innerHTML = play ? `${uiIcon("pause")} Pause` : `${uiIcon("play")} Play`;
}

function setCompareSound(sound) {
  compare.sound = sound;
  $$("#compare-stage video").forEach((video, index) => { video.muted = sound !== String(index); });
  $$("[data-compare-sound]").forEach((button) => button.classList.toggle("is-active", button.dataset.compareSound === sound));
}

// Picks row vs column by which one lets both videos (at their own aspect
// ratios, sharing one height or one width) come out larger.
function layoutCompare() {
  const stage = $("#compare-stage");
  const videos = $$("video", stage);
  const ratios = videos.map((video) => (video.videoWidth && video.videoHeight ? video.videoWidth / video.videoHeight : 16 / 9));
  const gap = 8;
  const width = stage.clientWidth;
  const height = stage.clientHeight - 2 * 44; // room for the captions
  // Side by side: one shared height h, widths h*r1 + h*r2 <= width - gap.
  const rowH = Math.min(height, (width - gap) / (ratios[0] + ratios[1]));
  const rowArea = rowH * rowH * (ratios[0] + ratios[1]);
  // Stacked: one shared width w, heights w/r1 + w/r2 <= height - gap.
  const colW = Math.min(width, (height - gap) / (1 / ratios[0] + 1 / ratios[1]));
  const colArea = colW * colW * (1 / ratios[0] + 1 / ratios[1]);
  const layout = compare.layout !== "auto" ? compare.layout : rowArea >= colArea ? "row" : "column";
  stage.dataset.layout = layout;
  stage.dataset.show = compare.show;
  $("#compare-show").hidden = layout !== "single";
  $$("[data-compare-layout]").forEach((button) => button.classList.toggle("is-active", button.dataset.compareLayout === compare.layout));
}

// Single layout: one video fills the screen; both keep playing so flipping
// between A and B is instant and stays in time. Sound follows the shown one
// unless it's off.
function showCompare(index) {
  compare.show = index;
  $("#compare-stage").dataset.show = index;
  $$("[data-compare-show]").forEach((button) => button.classList.toggle("is-active", button.dataset.compareShow === index));
  if (compare.sound !== "off") setCompareSound(index);
}

function bindCompare() {
  $("#compare-toggle").addEventListener("click", () => toggleCompareMode());
  $("#compare-cancel").addEventListener("click", () => { toggleCompareMode(false); clearCompareClasses(); });
  $("#compare-close").addEventListener("click", closeCompare);
  $("#compare-play").addEventListener("click", () => playCompare(!compare.playing));
  $("#compare-restart").addEventListener("click", () => {
    $$("#compare-stage video").forEach((video) => { video.currentTime = 0; });
    playCompare(true);
  });
  $("#compare-swap").addEventListener("click", () => openCompare([...compare.ids].reverse()));
  $$("[data-compare-sound]").forEach((button) => button.addEventListener("click", () => setCompareSound(button.dataset.compareSound)));
  $$("[data-compare-show]").forEach((button) => button.addEventListener("click", () => showCompare(button.dataset.compareShow)));
  $$("[data-compare-layout]").forEach((button) => button.addEventListener("click", () => { compare.layout = button.dataset.compareLayout; layoutCompare(); }));
  // Tapping the video: flips A/B in Single (the quickest way to compare), else play/pause.
  $$("#compare-stage video").forEach((video) => video.addEventListener("click", () => {
    if ($("#compare-stage").dataset.layout === "single") showCompare(compare.show === "0" ? "1" : "0");
    else playCompare(!compare.playing);
  }));
  window.addEventListener("resize", () => { if (!$("#compare-viewer").hidden) layoutCompare(); });
  document.addEventListener("keydown", (event) => {
    if ($("#compare-viewer").hidden) return;
    if (event.key === "Escape") closeCompare();
    else if ($("#compare-stage").dataset.layout === "single" && ["ArrowLeft", "ArrowRight", "a", "b", "Tab"].includes(event.key)) {
      event.preventDefault();
      showCompare(event.key === "a" ? "0" : event.key === "b" ? "1" : compare.show === "0" ? "1" : "0");
    }
    else if (event.key === " " && !event.target.closest("button, input, textarea, select")) { event.preventDefault(); playCompare(!compare.playing); }
  });
}

async function completeStitch() {
  if (state.stitchSelection.length < 2) return;
  const button = $("#stitch-complete");
  button.disabled = true;
  button.textContent = "Stitching…";
  try {
    const job = await Api.request(`${API_BASE}/api/stitch`, { method: "POST", body: { job_ids: state.stitchSelection, ...collectStitchSettings() } });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    toggleStitchMode(false);
    renderJobs();
    toast("Stitching selected generations together.");
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = "Complete Stitch";
  }
}

function candidatePicker(job) {
  const choice = finalizeChoice(job.id);
  const thumbs = job.candidate_urls.map((url, index) => {
    const n = index + 1;
    return `<label class="candidate-option ${choice.candidate === n ? "is-selected" : ""}">
      <input type="radio" name="candidate-${job.id}" value="${n}" ${choice.candidate === n ? "checked" : ""} data-candidate-pick="${job.id}">
      <video src="${url}" controls playsinline preload="metadata"></video>
      <span>Candidate ${n}</span>
    </label>`;
  }).join("");
  return `<div class="candidate-picker" data-job-id="${job.id}">
    <p class="hint">Pick a candidate, then finalize.</p>
    <div class="candidate-grid">${thumbs}</div>
    <div class="field-row">
      <label>3D latent upscale</label>
      <input type="checkbox" data-finalize-upscale="${job.id}" ${choice.enableUpscale ? "checked" : ""}>
    </div>
    <div class="field-row">
      <label>RIFE interpolation</label>
      <input type="checkbox" data-finalize-rife="${job.id}" ${choice.enableRife ? "checked" : ""}>
    </div>
    <button type="button" class="primary-button" data-finalize="${job.id}">Finalize</button>
  </div>`;
}

// Inline SVGs (stroke = currentColor) so the icons follow the button's
// text/hover colors; the text label lives in title/aria-label instead.
const jobIcon = (paths) => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const JOB_ICONS = {
  download: jobIcon('<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>'),
  // A picture frame with a down arrow: download the frame the player is on.
  lastFrame: jobIcon('<rect x="3" y="3" width="18" height="14" rx="2"/><path d="m3 14 5-5 4 4 3-3 6 6"/><path d="M12 17v5"/><path d="m9 19 3 3 3-3"/>'),
  continue: jobIcon('<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>'),
  retry: jobIcon('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>'),
  remove: jobIcon(ICON_PATHS.trash),
  cancel: jobIcon('<rect x="6" y="6" width="12" height="12" rx="1"/>'),
  info: jobIcon('<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>'),
  copy: jobIcon('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>'),
  thumbsUp: jobIcon(ICON_PATHS.thumbsUp),
  // Thumbs up with a sparkle in the empty top-left corner.
  thumbsUpExtreme: jobIcon('<path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 18.5 21H7V10l4.3-7.2A1.9 1.9 0 0 1 15 5.9Z"/><path d="M4 1.5v5"/><path d="M1.5 4h5"/>'),
  thumbsDown: jobIcon(ICON_PATHS.thumbsDown),
  postEdit: jobIcon(ICON_PATHS.scissors),
  edit: jobIcon(ICON_PATHS.pencil),
};

// Thumbs up/down on a finished video, pushed to the right end of the button
// row. The ratings feed the score next to the time estimate (/api/estimate).
function jobRatingButtons(job) {
  const button = (value, icon, label) => `<button type="button" class="secondary-button job-icon-button job-rate${job.rating === value ? " is-active" : ""}" data-rate="${value}" data-rate-job="${job.id}" aria-pressed="${job.rating === value}" title="${label}" aria-label="${label}">${icon}</button>`;
  return `<span class="job-rating">${button(2, JOB_ICONS.thumbsUpExtreme, "Extreme thumbs up: best quality possible (hold to skip the questions)")}${button(1, JOB_ICONS.thumbsUp, "Good result")}${button(-1, JOB_ICONS.thumbsDown, "Bad result")}</span>`;
}

// Phase 1 = the render; phase 2 = H3 latent upscale + second pass (job.phase2, see app.py job_phase2).
// H3 latent upscale jobs: 1 render, 2 latent upscale, 3 VAE decode.
function jobPhase(job) {
  if (!job.phase2 || job.phase2.status === "waiting") return 1;
  return job.phase2.status === "decoding" ? 3 : 2;
}

// Decoding reports no progress, so phase 3 fills by elapsed time against the
// server's estimate (h3_decode_seconds), holding at 95% until the job finishes.
function decodeProgress(job) {
  const { decode_started: started, decode_estimate: estimate } = job.phase2 || {};
  if (!started || !estimate) return 0;
  return Math.min(95, Math.max(0, (Date.now() / 1000 - started) / estimate * 100));
}

// One bar: the render's progress, then (H3 latent upscale jobs) it restarts
// from 0 in gold for phase 2 and again in green for phase 3.
function jobProgressWidth(job) {
  const phase = jobPhase(job);
  const value = phase === 3 ? decodeProgress(job) : smoothedProgress(job, phase, Number(phase === 2 ? job.phase2.progress : job.progress) || 0);
  return Math.round(Math.max(0, Math.min(phase === 1 ? 99 : 100, value)) * 10) / 10;
}

// Sampler steps arrive as jumps (8 steps = 12% each, often 20+ s apart), so
// between two server ticks the bar creeps forward at the pace of the last
// step - stopping short of where the next real tick will land.
const progressPace = new Map(); // job id -> { key, value, at, step, msPerUnit }
function smoothedProgress(job, phase, reported) {
  if (job.state !== "rendering") { progressPace.delete(job.id); return reported; }
  const now = Date.now();
  const key = `${phase}`;
  let pace = progressPace.get(job.id);
  if (!pace || pace.key !== key || reported < pace.value) {
    pace = { key, value: reported, at: now, step: 0, msPerUnit: 0 };
    progressPace.set(job.id, pace);
  } else if (reported > pace.value) {
    const step = reported - pace.value;
    // Paced only from a full step-to-step interval: the first jump after a phase
    // starts includes model loading, and one after a page load was mid-step.
    pace.msPerUnit = pace.step ? (now - pace.at) / step : 0;
    Object.assign(pace, { value: reported, at: now, step });
  }
  if (!pace.msPerUnit || !pace.step) return reported;
  return reported + Math.min(pace.step * 0.9, (now - pace.at) / pace.msPerUnit);
}

// The state chip, plus what a long step is doing (SeedHunter finalize, and
// Post-Edit's GPU music removal / loudness measuring, which sit at 0% or 60%).
const JOB_STAGE_SUFFIX = { finalize: "finalize", separate: "removing music on the GPU", measure: "leveling audio" };
function jobStateText(job) {
  const label = STATE_LABELS[job.state] || job.state;
  const suffix = job.stage === "finalize" ? (job.state !== "awaiting_selection" ? JOB_STAGE_SUFFIX.finalize : null)
    : isActiveJob(job) ? JOB_STAGE_SUFFIX[job.stage] : null;
  return suffix ? `${label} (${suffix})` : label;
}

function jobPhaseLabel(job) {
  if (!job.phase2) return "";
  const phase = jobPhase(job);
  if (phase === 1) return "Phase 1 of 3 · Render";
  if (phase === 3) return "Phase 3 of 3 · Decoding video";
  const { chunk, chunks } = job.phase2;
  return `Phase 2 of 3 · Latent upscale · ${chunks > 1 ? `chunk ${chunk} of ${chunks}` : "upscaling"}`;
}

function jobProgress(job) {
  return `<div class="job-phase${jobPhase(job) === 2 ? " is-phase2" : jobPhase(job) === 3 ? " is-phase3" : ""}" data-phase="${jobPhase(job)}">
    ${job.phase2 ? `<span class="job-phase-label">${escapeHtml(jobPhaseLabel(job))}</span>` : ""}
    <div class="job-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${jobProgressWidth(job)}" aria-label="${escapeHtml(jobPhaseLabel(job) || "Progress")}"><div class="job-progress-bar job-main-bar" style="width:${jobProgressWidth(job)}%"></div></div>
  </div>`;
}

function jobCard(job) {
  const active = isActiveJob(job);
  const cancelling = job.state === "cancelling";
  const finalizing = job.state === "finalizing";
  const awaitingSelection = job.state === "awaiting_selection" && job.candidate_urls;
  const terminal = ["complete", "failed", "cancelled"].includes(job.state) || awaitingSelection;
  return `<div class="job-card${job.agentic_session_id ? " agentic-job" : ""}${job.agentic_phase === "staging" ? " agentic-staging-job" : ""}" data-job-id="${job.id}">
    <div class="job-head">
      <span class="job-prompt" title="${escapeHtml(job.prompt)}">${escapeHtml(job.prompt)}${job.prompt_truncated ? "…" : ""}</span>
      <span class="job-state state-${job.state}">${escapeHtml(jobStateText(job))}</span>
    </div>
    <div class="job-meta">
      ${job.agentic_clip_index ? `<span class="agentic-clip-badge">Agentic ${{staging: "preview clip", postedit: "Post-Edit step", stitch: "Stitch step"}[job.agentic_phase] || "full clip"} ${job.agentic_planned_clip || job.agentic_clip_index}${job.agentic_attempt > 1 ? ` · retry ${job.agentic_attempt - 1}` : ""}</span>` : ""}
      <span>${escapeHtml(job.model_label || "MiniMax H3")}</span>
      <span>${escapeHtml(job.mode_label || "")}</span>
      <span>${job.width || "?"}&times;${job.height || "?"}</span>
      <span>${job.duration ? `${job.duration}s` : ""}</span>
      <span>${job.fps ? `${Number(job.fps).toFixed(job.fps % 1 ? 2 : 0)}fps` : ""}</span>
      <span class="job-elapsed">${elapsed(job)}</span>
      ${isActiveJob(job) ? `<span class="job-eta${job.estimate?.fit ? " is-fitted" : ""}" title="${escapeHtml(job.estimate?.fit ? stepFitTitle(job.estimate.fit)
        : job.estimate?.samples ? `Average of the last ${job.estimate.samples} run(s) with these settings` : "")}">${jobEta(job)}</span>` : ""}
    </div>
    ${active ? jobProgress(job) : finalizing ? `<div class="job-progress"><div class="job-progress-bar" style="width:0%"></div></div>` : ""}
    ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
    ${awaitingSelection ? `<div class="approval-banner">Approval required &mdash; pick a candidate below</div>` : ""}
    ${awaitingSelection ? candidatePicker(job) : ""}
    ${job.state === "complete" && job.output_url ? `<video class="job-video" src="${job.output_url}"${job.poster_url ? ` poster="${job.poster_url}" preload="none"` : ` preload="metadata"`} controls playsinline></video>` : ""}
    <div class="job-actions">
      <button type="button" class="secondary-button job-icon-button" data-info="${job.id}" title="Generation details" aria-label="Generation details">${JOB_ICONS.info}</button>
      ${(active || finalizing) ? `<button type="button" class="secondary-button job-icon-button danger" data-cancel="${job.id}" ${cancelling ? "disabled" : ""} title="${cancelling ? "Cancelling…" : "Cancel"}" aria-label="${cancelling ? "Cancelling" : "Cancel"}">${JOB_ICONS.cancel}</button>` : ""}
      ${job.state === "complete" && job.download_url ? `<a class="secondary-button job-icon-button" href="${job.download_url}" download title="Download" aria-label="Download">${JOB_ICONS.download}</a>` : ""}
      ${job.state === "complete" && job.last_frame_url ? `<button type="button" class="secondary-button job-icon-button" data-frame-download="${job.id}" title="Download the frame the player is on (PNG) - pause where you want it" aria-label="Download the current frame">${JOB_ICONS.lastFrame}</button>` : ""}
      ${job.state === "complete" && job.output_url ? `<button type="button" class="secondary-button job-icon-button" data-continue="${job.id}" title="Continue from this video…" aria-label="Continue from this video">${JOB_ICONS.continue}</button>` : ""}
      ${job.state === "complete" && job.output_url ? (job.model === "postedit"
        ? `<button type="button" class="secondary-button job-icon-button" data-reedit="${job.id}" title="Edit again: the original video with this edit's saved settings" aria-label="Edit again with saved settings">${JOB_ICONS.edit}</button>
           <button type="button" class="secondary-button job-icon-button" data-postedit="${job.id}" title="Edit this rendered result as a new source (edits stack)" aria-label="Edit this rendered result">${JOB_ICONS.postEdit}</button>`
        : `<button type="button" class="secondary-button job-icon-button" data-postedit="${job.id}" title="Edit video (trim, color, voice-over, music, text)" aria-label="Edit video">${JOB_ICONS.edit}</button>`) : ""}
      ${retryAvailable(job) ? `<button type="button" class="secondary-button job-icon-button" data-retry="${job.id}" data-retry-expires="${escapeHtml(job.retry_expires_at)}" title="${escapeHtml(retryTitle(job))}" aria-label="Retry">${JOB_ICONS.retry}</button>` : ""}
      ${terminal ? `<button type="button" class="secondary-button job-icon-button danger" data-remove="${job.id}" title="Remove" aria-label="Remove">${JOB_ICONS.remove}</button>` : ""}
      ${job.state === "complete" && job.output_url && !["stitch", "postedit"].includes(job.model) ? jobRatingButtons(job) : ""}
    </div>
    ${job.rating === 2 ? jobBestReasons(job) : ""}
  </div>`;
}

// Fields that actually affect a card's markup - kept in sync with jobCard().
// Deliberately excludes `progress`: the SSE "jobs" handler already skips
// calling renderJobs() at all for pure progress ticks (see jobsNeedRender),
// and updateJobMetrics() patches the progress bar/elapsed time in place.
const JOB_RENDER_FIELDS = ["id", "model_label", "mode_label", "prompt", "state", "stage", "output_url", "poster_url", "candidate_urls", "error", "retry_expires_at", "rating", "rating_reasons", "agentic_session_id", "agentic_clip_index", "agentic_phase", "agentic_planned_clip", "agentic_attempt", "agentic_approval_run_id"];

function jobRenderKey(job) {
  return JOB_RENDER_FIELDS.map((field) => job[field]).join("::");
}

// The card's frame button: the frame its video player is showing right now
// (full resolution, from the server - the player itself may be scaled down).
function downloadCurrentFrame(jobId, card) {
  const job = state.jobs.find((item) => item.id === jobId);
  const video = $(".job-video", card);
  if (!job?.last_frame_url) return;
  const time = video ? video.currentTime : 0;
  const link = document.createElement("a");
  link.href = `${job.last_frame_url}?t=${time.toFixed(4)}`;
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
  toast(`Downloading the frame at ${time.toFixed(2)}s.`);
}

function bindJobCard(node) {
  node.addEventListener("click", (event) => {
    if (!state.stitchMode && !state.compareMode) return;
    if (event.target.closest("button, a, input, label, video")) return;
    if (state.compareMode) toggleCompareSelection(node.dataset.jobId);
    else toggleStitchSelection(node.dataset.jobId);
  });
  const one = (selector, handler) => { const el = $(selector, node); if (el) el.addEventListener("click", handler); };
  one("[data-cancel]", (event) => cancelJob(event.currentTarget.dataset.cancel));
  one("[data-remove]", (event) => removeJob(event.currentTarget.dataset.remove));
  one("[data-finalize]", (event) => finalizeJob(event.currentTarget.dataset.finalize));
  one("[data-continue]", (event) => continueFromVideo(event.currentTarget.dataset.continue));
  one("[data-postedit]", (event) => openPostEdit(event.currentTarget.dataset.postedit));
  one("[data-reedit]", (event) => peReeditJob(event.currentTarget.dataset.reedit));
  one("[data-retry]", (event) => retryJob(event.currentTarget.dataset.retry, event.currentTarget));
  one("[data-info]", (event) => openJobInfo(event.currentTarget.dataset.info));
  one("[data-frame-download]", (event) => downloadCurrentFrame(event.currentTarget.dataset.frameDownload, node));
  $$("[data-rate]", node).forEach((button) => button.addEventListener("click", () => rateJob(button)));
  const extreme = $('[data-rate="2"]', node);
  if (extreme) bindLongPress(extreme, () => quickExtremeRating(extreme.dataset.rateJob));
  one("[data-best-reasons]", (event) => openBestReasons(event.currentTarget.dataset.bestReasons));
  $$('[data-candidate-pick]', node).forEach((input) => input.addEventListener("change", () => {
    finalizeChoice(input.dataset.candidatePick).candidate = Number(input.value);
    // Toggle the selected style directly instead of re-rendering: the choice
    // lives in `finalizeChoices`, not on the job object, so it isn't part of
    // jobRenderKey() and a re-render would leave this card looking unchanged.
    $$(".candidate-option", node).forEach((label) => label.classList.toggle("is-selected", label.contains(input)));
  }));
  $$('[data-finalize-upscale]', node).forEach((input) => input.addEventListener("change", () => {
    finalizeChoice(input.dataset.finalizeUpscale).enableUpscale = input.checked;
  }));
  $$('[data-finalize-rife]', node).forEach((input) => input.addEventListener("change", () => {
    finalizeChoice(input.dataset.finalizeRife).enableRife = input.checked;
  }));
}

// The Generations list is paged: JOBS_PAGE newest matching jobs, plus more
// per "Show more". Filter or search changes go back to one page.
const JOBS_PAGE = 12;
state.jobsShown = JOBS_PAGE;
state.jobSearch = "";

function jobMatchesFilter(job, filter = state.filter) {
  return filter === "all" || (filter === "running" ? (isActiveJob(job) || needsAttention(job)) : job.state === "complete");
}

// The job list carries long prompts cut short (prompt_truncated); search
// fetches the full ones once (GET /api/jobs/prompts, revalidated by ETag) so
// it still finds a word deep in a long Reference prompt.
let fullPrompts = null, fullPromptsLoading = null;
function loadFullPrompts() {
  fullPromptsLoading ??= Api.request(`${API_BASE}/api/jobs/prompts`)
    .then((prompts) => { fullPrompts = prompts; if (state.jobSearch) renderJobs(); })
    .catch(() => {})
    .finally(() => { fullPromptsLoading = null; });
  return fullPromptsLoading;
}

function matchingJobs() {
  const words = state.jobSearch.toLowerCase().split(/\s+/).filter(Boolean);
  return state.jobs.filter((job) => jobMatchesFilter(job)
    && words.every((word) => `${(job.prompt_truncated && fullPrompts?.[job.id]) || job.prompt || ""} ${job.model_label || ""} ${job.mode_label || ""} ${job.agentic_session_title || ""}`.toLowerCase().includes(word)));
}

function resetJobsPage() {
  state.jobsShown = JOBS_PAGE;
  $("#jobs-list").scrollTop = 0;
}

// Infinite scroll: load the next page once the end of the list is within
// ~1.5 screens - inside the panel's own scroll on desktop, the page's on phones.
let loadMoreQueued = false;
function maybeLoadMoreJobs() {
  if (loadMoreQueued) return;
  loadMoreQueued = true;
  requestAnimationFrame(() => {
    loadMoreQueued = false;
    const button = $("#jobs-show-more");
    if (!jobsVisible() || $("#jobs-more").hidden || button.hidden) return;
    const list = $("#jobs-list");
    const ownScroll = getComputedStyle(list).overflowY === "auto";
    const remaining = ownScroll
      ? list.scrollHeight - list.scrollTop - list.clientHeight
      : $("#jobs-more").getBoundingClientRect().top - window.innerHeight;
    if (remaining < (ownScroll ? list.clientHeight : window.innerHeight) * 1.5) {
      state.jobsShown += JOBS_PAGE;
      renderJobs();
    }
  });
}

function renderJobsPager(total, shown) {
  $$("[data-filter-count]").forEach((node) => {
    const count = state.jobs.filter((job) => jobMatchesFilter(job, node.dataset.filterCount)).length;
    node.textContent = count ? count : "";
  });
  $("#jobs-more").hidden = total <= JOBS_PAGE && shown >= total;
  $("#jobs-more-count").textContent = `Showing ${shown} of ${total}`;
  const button = $("#jobs-show-more");
  button.hidden = shown >= total;
  button.textContent = `Show ${Math.min(JOBS_PAGE, total - shown)} more`;
}

// The resumed Agentic session, while it has the jobs list to itself.
function focusedAgenticSessionId() {
  return activeAgenticSessionKind === "resumed" && !agenticFocusDismissed ? activeAgenticSessionId : null;
}

function renderJobs() {
  const host = $("#jobs-list");
  const focusId = focusedAgenticSessionId();
  // Focused: every clip of that session (no search, filter or paging), nothing else.
  const focusJobs = focusId ? state.jobs.filter((job) => job.agentic_session_id === focusId) : [];
  const matching = focusJobs.length ? focusJobs : matchingJobs();
  host.classList.toggle("is-agentic-focus", Boolean(focusJobs.length));
  const filtered = matching.slice(0, state.jobsShown);
  renderJobsPager(matching.length, filtered.length);
  updateMobileTabBadge();
  if (!jobsVisible()) {
    state.jobsStale = true; // built when the Generations tab opens
    return;
  }
  state.jobsStale = false;
  if (!filtered.length) {
    host.innerHTML = `<div class="jobs-empty">${state.jobs.length ? "No matching generations." : "No generations yet. Describe a shot and hit Generate."}</div>`;
    renderStitchBar();
    return;
  }
  // Reuse cards and groups so live updates do not reset video playback or
  // collapse a session the user has opened.
  $(".jobs-empty", host)?.remove();
  const existingNodes = new Map($$('[data-job-id]', host).map((node) => [node.dataset.jobId, node]));
  const existingGroups = new Map($$(':scope > [data-agentic-session-id]', host)
    .map((node) => [node.dataset.agenticSessionId, node]));
  const entries = [];
  const grouped = new Map();
  filtered.forEach((job) => {
    if (!job.agentic_session_id) {
      entries.push({ job });
      return;
    }
    let entry = grouped.get(job.agentic_session_id);
    if (!entry) {
      entry = { id: job.agentic_session_id, jobs: [], title: job.agentic_session_title || job.prompt };
      grouped.set(entry.id, entry);
      entries.push(entry);
    }
    entry.jobs.push(job);
  });
  function place(parent, node, index) {
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  }
  function cardFor(job) {
    const key = jobRenderKey(job);
    let node = existingNodes.get(job.id);
    existingNodes.delete(job.id);
    if (!node || node.dataset.jobRenderKey !== key) {
      const wrapper = document.createElement("div");
      wrapper.innerHTML = jobCard(job);
      const fresh = wrapper.firstElementChild;
      fresh.dataset.jobRenderKey = key;
      bindJobCard(fresh);
      if (node) node.replaceWith(fresh);
      node = fresh;
    }
    return node;
  }
  entries.forEach((entry, index) => {
    if (entry.job) {
      place(host, cardFor(entry.job), index);
      return;
    }
    let group = existingGroups.get(entry.id);
    existingGroups.delete(entry.id);
    if (!group) {
      // A plain div (not <details>) so it can dissolve into the jobs grid: its tile
      // and clips flow like any other card until the session is resumed (is-focused).
      group = document.createElement("div");
      group.className = "agentic-session-group";
      group.dataset.agenticSessionId = entry.id;
      group.innerHTML = `<div class="agentic-session-head"><span class="agentic-session-mark" aria-hidden="true"></span><span class="agentic-session-heading"><strong>Agentic session</strong><small class="agentic-session-title"></small></span><span class="agentic-session-count"></span><span class="agentic-collapse-chips"><button type="button" class="secondary-button agentic-collapse-chip" data-collapse="previews" aria-expanded="true"></button><button type="button" class="secondary-button agentic-collapse-chip" data-collapse="finals" aria-expanded="true"></button></span><span class="agentic-session-buttons"><button type="button" class="secondary-button agentic-show-all-button" title="Show every generation again (the session stays active)">Show all</button><button type="button" class="secondary-button job-icon-button agentic-info-button" title="What was sent to start this session" aria-label="What was sent to start this session">${JOB_ICONS.info}</button><button type="button" class="secondary-button job-icon-button agentic-folder-button" title="Session folder: assets, prompt log, rewrite clips" aria-label="Session folder: assets, prompt log, rewrite clips">${jobIcon('<path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>')}</button><button type="button" class="secondary-button agentic-stitch-button" title="Join this session's final clips into one video, in story order" hidden>Stitch</button><button type="button" class="secondary-button agentic-resume-button">Resume</button><button type="button" class="secondary-button job-icon-button danger agentic-delete-button" title="Delete this session and every video it made" aria-label="Delete this session and every video it made">${JOB_ICONS.remove}</button></span></div><div class="agentic-session-cards"><div class="agentic-stage-group"><button type="button" class="agentic-stage-head" aria-expanded="true"><strong>Staging previews</strong><span class="agentic-stage-count"></span><span class="card-chevron" aria-hidden="true"></span></button><div class="agentic-stage-cards"></div><div class="agentic-stage-approval" hidden><label>Notes for every clip<textarea class="agentic-stage-notes" rows="3" placeholder="Changes for the whole series (style, setting, characters). Notes for one clip go in the box under that clip."></textarea></label><div class="agentic-stage-actions"><button type="button" class="secondary-button agentic-stage-accept" data-stage-action="accept">Accept and run final</button><button type="button" class="secondary-button danger agentic-stage-exclude" data-stage-action="exclude">Exclude and retry staging</button></div></div></div><div class="agentic-final-group"><button type="button" class="agentic-stage-head agentic-final-head" aria-expanded="true"><strong>Final clips</strong><span class="agentic-stage-count agentic-final-count"></span><span class="card-chevron" aria-hidden="true"></span></button><div class="agentic-final-cards"></div></div></div>`;
      // Previews / final clips collapse together from the tile chips (grid) or the lane headers (resumed).
      $(".agentic-stage-head", group).addEventListener("click", () => toggleAgenticCollapse(group, "previews"));
      $(".agentic-final-head", group).addEventListener("click", () => toggleAgenticCollapse(group, "finals"));
      $$(".agentic-collapse-chip", group).forEach((chip) => chip.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleAgenticCollapse(group, chip.dataset.collapse);
      }));
      $(".agentic-show-all-button", group).addEventListener("click", () => {
        agenticFocusDismissed = true;
        renderedAgenticFocus = null;
        renderJobs();
      });
      $(".agentic-stage-approval", group).addEventListener("click", async (event) => {
        const button = event.target.closest("[data-stage-action]");
        if (!button) return;
        const runId = group.dataset.agenticApprovalRunId;
        if (!runId) return;
        const controls = $$("[data-stage-action]", group);
        controls.forEach((control) => { control.disabled = true; });
        try {
          const sessionId = group.dataset.agenticSessionId;
          const clipNotes = Object.fromEntries([...agenticClipNotes].filter(([key, value]) => key.startsWith(`${sessionId}:`) && value.trim())
            .map(([key, value]) => [key.split(":").pop(), value.trim()]));
          await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}/stage-decision`, {
            method: "POST", body: {action: button.dataset.stageAction, notes: $(".agentic-stage-notes", group).value, clip_notes: clipNotes, run_id: runId},
          });
          [...agenticClipNotes.keys()].filter((key) => key.startsWith(`${sessionId}:`)).forEach((key) => agenticClipNotes.delete(key));
          saveAgenticClipNotes();
          $(".agentic-stage-notes", group).value = "";
          $$(".agentic-clip-note", group).forEach((box) => box.remove());
          $(".agentic-stage-approval", group).hidden = true;
          toast(button.dataset.stageAction === "accept" ? "Staging accepted; Hermes is preparing final prompts." : "Staging excluded; Hermes will revise and retry previews.");
        } catch (error) { toast(error.message, "error"); }
        finally { controls.forEach((control) => { control.disabled = false; }); }
      });
      $(".agentic-delete-button", group).addEventListener("click", () => deleteAgenticSession(group.dataset.agenticSessionId));
      $(".agentic-stitch-button", group).addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        stitchAgenticSession(group.dataset.agenticSessionId, event.currentTarget);
      });
      $(".agentic-folder-button", group).addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openAgenticSessionFolder(group.dataset.agenticSessionId);
      });
      $(".agentic-info-button", group).addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openAgenticSessionInfo(group.dataset.agenticSessionId);
      });
      $(".agentic-resume-button", group).addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (activeAgenticSessionId === group.dataset.agenticSessionId) endAgenticSession();
        else resumeAgenticSession(group.dataset.agenticSessionId);
      });
    }
    group.style.setProperty("--session-hue", agenticSessionHue(entry.id));
    $(".agentic-session-title", group).textContent = entry.title;
    $(".agentic-session-count", group).textContent = `${entry.jobs.length} render${entry.jobs.length === 1 ? "" : "s"}`;
    const staging = entry.jobs.filter((job) => job.agentic_phase === "staging");
    const final = entry.jobs.filter((job) => job.agentic_phase !== "staging");
    // Every attempt of one clip (retries, best of 2, Whisper re-renders) shares one card with ‹ › arrows.
    const stagingClips = groupAgenticAttempts(staging);
    const finalClips = groupAgenticAttempts(final);
    const stageGroup = $(".agentic-stage-group", group);
    stageGroup.hidden = !staging.length;
    $(".agentic-final-group", group).hidden = !final.length;
    $(".agentic-stage-count", group).textContent = `${stagingClips.length} preview${stagingClips.length === 1 ? "" : "s"}`;
    $(".agentic-final-count", group).textContent = `${finalClips.length} clip${finalClips.length === 1 ? "" : "s"}`;
    // Previews auto-hide once every final clip has rendered (unless the user opened them again).
    const fullClips = final.filter((job) => (job.agentic_phase || "full") === "full");
    group.dataset.finalsDone = staging.length && fullClips.length && fullClips.every((job) => ["complete", "failed", "cancelled"].includes(job.state))
      && fullClips.some((job) => job.state === "complete") ? "1" : "";
    $('[data-collapse="previews"]', group).hidden = !staging.length;
    $('[data-collapse="finals"]', group).hidden = !final.length;
    group.dataset.previewCount = stagingClips.length;
    group.dataset.finalCount = finalClips.length;
    // Stitch needs two finished full clips; the server picks the kept attempt of each, in story order.
    const stitchable = finalClips.filter(([, attempts]) => attempts.some((job) => (job.agentic_phase || "full") === "full" && job.state === "complete"));
    $(".agentic-stitch-button", group).hidden = stitchable.length < 2;
    applyAgenticCollapse(group);
    const approvalRunId = entry.jobs.find((job) => job.agentic_approval_run_id)?.agentic_approval_run_id || "";
    group.dataset.agenticApprovalRunId = approvalRunId;
    $(".agentic-stage-approval", group).hidden = !approvalRunId || !staging.length;
    const stageCards = $(".agentic-stage-cards", group);
    const finalCards = $(".agentic-final-cards", group);
    staging.sort((a, b) => (a.agentic_clip_index || 0) - (b.agentic_clip_index || 0));
    final.sort((a, b) => (a.agentic_clip_index || 0) - (b.agentic_clip_index || 0));
    const notesOpen = Boolean(approvalRunId && staging.length);
    stagingClips.forEach(([key, attempts], cardIndex) => {
      const job = shownAgenticAttempt(key, attempts);
      const card = cardFor(job);
      place(stageCards, card, cardIndex);
      syncAgenticAttemptPager(card, key, attempts, job);
      syncAgenticClipNote(card, job, entry.id, notesOpen);
    });
    finalClips.forEach(([key, attempts], cardIndex) => {
      const job = shownAgenticAttempt(key, attempts);
      const card = cardFor(job);
      place(finalCards, card, cardIndex);
      syncAgenticAttemptPager(card, key, attempts, job);
    });
    group.classList.toggle("is-focused", entry.id === focusId);
    place(host, group, index);
  });
  syncAgenticSessionButtons();
  existingNodes.forEach((node) => node.remove());
  existingGroups.forEach((node) => node.remove());
  renderStitchBar();
  applyStitchModeToCards();
  maybeLoadMoreJobs(); // keeps loading until the list at least fills its view
}

// Per-clip staging notes, "sessionId:clip" -> text; kept across re-renders and reloads until submitted.
const AGENTIC_CLIP_NOTES_KEY = "h3-agentic-clip-notes";
const agenticClipNotes = new Map((() => { try { return Object.entries(JSON.parse(localStorage.getItem(AGENTIC_CLIP_NOTES_KEY) || "{}")); } catch { return []; } })());

function saveAgenticClipNotes() {
  try { localStorage.setItem(AGENTIC_CLIP_NOTES_KEY, JSON.stringify(Object.fromEntries(agenticClipNotes))); } catch { /* this page only */ }
}

function syncAgenticClipNote(card, job, sessionId, show) {
  let box = $(".agentic-clip-note", card);
  if (!show) { box?.remove(); return; }
  if (box) return;
  const clip = job.agentic_planned_clip || job.agentic_clip_index;
  const key = `${sessionId}:${clip}`;
  box = document.createElement("label");
  box.className = "agentic-clip-note";
  box.innerHTML = `<span>Notes for clip ${clip}</span><textarea rows="2" spellcheck="true" placeholder="What should change in this clip only?"></textarea>`;
  const textarea = $("textarea", box);
  textarea.value = agenticClipNotes.get(key) || "";
  textarea.addEventListener("input", () => {
    if (textarea.value.trim()) agenticClipNotes.set(key, textarea.value); else agenticClipNotes.delete(key);
    saveAgenticClipNotes();
  });
  ["click", "pointerdown", "keydown"].forEach((type) => box.addEventListener(type, (event) => event.stopPropagation()));
  card.append(box);
}

// Attempt pager: run + phase + planned clip -> the attempt the user picked with ‹ › (sessionStorage).
const AGENTIC_ATTEMPT_KEY = "h3-agentic-attempts";
const agenticAttemptChoice = new Map((() => { try { return Object.entries(JSON.parse(sessionStorage.getItem(AGENTIC_ATTEMPT_KEY) || "{}")); } catch { return []; } })());

function groupAgenticAttempts(jobs) {
  const groups = new Map();
  for (const job of jobs) {
    const key = `${job.agentic_run || ""}|${job.agentic_phase || "full"}|${job.agentic_planned_clip ?? job.agentic_clip_index}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(job);
  }
  for (const attempts of groups.values()) attempts.sort((a, b) => (a.agentic_clip_index || 0) - (b.agentic_clip_index || 0));
  // Cards in plan order (clip number), runs in the order they happened.
  return [...groups.entries()].sort(([, a], [, b]) => (a[0].agentic_clip_index || 0) - (b[0].agentic_clip_index || 0));
}

function shownAgenticAttempt(key, attempts) {
  const picked = attempts.find((job) => job.id === agenticAttemptChoice.get(key));
  if (picked) return picked;
  return attempts.findLast((job) => job.agentic_selected)
    || attempts.findLast((job) => job.state === "complete")
    || attempts[attempts.length - 1];
}

function syncAgenticAttemptPager(card, key, attempts, job) {
  let pager = $(".agentic-attempt-pager", card);
  if (attempts.length < 2) { pager?.remove(); return; }
  if (!pager) {
    pager = document.createElement("div");
    pager.className = "agentic-attempt-pager";
    pager.innerHTML = `<button type="button" class="secondary-button" data-attempt-step="-1" aria-label="Previous attempt">‹</button><span class="agentic-attempt-label"></span><button type="button" class="secondary-button" data-attempt-step="1" aria-label="Next attempt">›</button>`;
    pager.addEventListener("click", (event) => {
      const button = event.target.closest("[data-attempt-step]");
      event.stopPropagation();
      if (!button) return;
      const { attempts: list, key: pagerKey } = pager._attempts;
      const index = list.findIndex((item) => item.id === card.dataset.jobId);
      const next = list[(index + Number(button.dataset.attemptStep) + list.length) % list.length];
      agenticAttemptChoice.set(pagerKey, next.id);
      try { sessionStorage.setItem(AGENTIC_ATTEMPT_KEY, JSON.stringify(Object.fromEntries(agenticAttemptChoice))); } catch { /* this page only */ }
      renderJobs();
    });
    $(".job-head", card)?.after(pager);
  }
  pager._attempts = { attempts, key };
  const index = attempts.indexOf(job);
  $(".agentic-attempt-label", pager).textContent = `Attempt ${index + 1} of ${attempts.length}${job.agentic_selected ? " · kept" : ""}`;
  pager.title = job.agentic_selected ? "The attempt Agentic kept for this clip" : "An earlier or discarded attempt of this clip";
}

const AGENTIC_COLLAPSE_KEY = "h3-agentic-collapsed";
// sessionId -> {previews, finals}; kept in memory and mirrored to localStorage when it's available.
const agenticCollapsed = (() => { try { return JSON.parse(localStorage.getItem(AGENTIC_COLLAPSE_KEY) || "{}"); } catch { return {}; } })();

function applyAgenticCollapse(group) {
  const saved = agenticCollapsed[group.dataset.agenticSessionId] || {};
  const counts = { previews: Number(group.dataset.previewCount || 0), finals: Number(group.dataset.finalCount || 0) };
  const automatic = { previews: group.dataset.finalsDone === "1", finals: false };
  for (const kind of ["previews", "finals"]) {
    const collapsed = saved[kind] ?? automatic[kind];
    group.classList.toggle(`${kind}-collapsed`, collapsed);
    const chip = $(`[data-collapse="${kind}"]`, group);
    chip.textContent = `${collapsed ? "▸" : "▾"} ${kind === "previews" ? "Previews" : "Final"} ${counts[kind]}`;
    chip.title = `${collapsed ? "Show" : "Hide"} this session's ${kind === "previews" ? "staging previews" : "final clips"}`;
    chip.setAttribute("aria-expanded", String(!collapsed));
    $(kind === "previews" ? ".agentic-stage-head:not(.agentic-final-head)" : ".agentic-final-head", group).setAttribute("aria-expanded", String(!collapsed));
  }
}

function toggleAgenticCollapse(group, kind) {
  const id = group.dataset.agenticSessionId;
  // Store the explicit choice (true or false) so it overrides the automatic hide.
  const entry = { ...(agenticCollapsed[id] || {}), [kind]: !group.classList.contains(`${kind}-collapsed`) };
  agenticCollapsed[id] = entry;
  try { localStorage.setItem(AGENTIC_COLLAPSE_KEY, JSON.stringify(agenticCollapsed)); } catch { /* this page only */ }
  applyAgenticCollapse(group);
}

// Grabs the last decoded frame of a finished job's video as a PNG blob, by
// seeking a hidden <video> near its end and reading it back through a
// <canvas>. Fallback only: continueFromVideo uses /last-frame?raw=1. Seeks to duration - 0.1s rather than the exact end: some
// browsers never fire `seeked` for a seek to precisely `duration`.
function captureLastFrame(videoUrl) {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    video.addEventListener("loadedmetadata", () => {
      video.addEventListener("seeked", () => {
        const canvas = document.createElement("canvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Got a blank frame."))), "image/png");
      }, { once: true });
      video.currentTime = Math.max(0, video.duration - 0.1);
    }, { once: true });
    video.addEventListener("error", () => reject(new Error("Couldn't load the video.")), { once: true });
    video.src = videoUrl;
  });
}

async function continueFromVideo(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job || !job.output_url) return;
  // SeedHunter has its own richer continuation (server-side overlap-frame
  // crossfade stitching across the whole clip, not just a still keyframe) -
  // keep using that when SeedHunter is already the open model. Every other
  // model only ever takes a still first-frame keyframe, so for those we hand
  // the video's last frame to whatever model/mode is currently open instead
  // of forcing a switch to SeedHunter.
  if (state.model === "seedhunter") {
    state.continueFromJob = { id: job.id, prompt: job.prompt };
    renderContinuationBanner();
    $("#seedhunter-advanced").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("Continuing from this video - set your prompt for what happens next, then Generate.");
    return;
  }
  if (!MODELS[state.model].modesAllowed.includes("fl2v")) {
    return toast(`${MODELS[state.model].label} has no Keyframe mode to continue into.`, "error");
  }
  try {
    // The server's exact final frame of the master (cached PNG) - a phone used to
    // download and seek the whole full-res video to grab it in a canvas.
    let blob;
    try {
      const response = await fetch(`${job.last_frame_url}?raw=1`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      blob = await response.blob();
    } catch { blob = await captureLastFrame(`${job.output_url}?original=1`); } // full-res master, not the playback copy
    const reference = await Api.upload(new File([blob], "continue-last-frame.png", { type: "image/png" }), "first");
    mergeReferences([reference]);
    if (state.firstFrameId) restoreResolutionSnapshot(); // new keyframe undoes any pending resolution match, same as a manual replace
    state.firstFrameId = reference.id;
    setMode("fl2v");
    renderKeyframes();
    renderReferenceGallery();
    $("#keyframe-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    toast(`Continuing from this video's last frame with ${MODELS[state.model].label} - review the prompt, then Generate.`);
  } catch (error) {
    toast(`Couldn't capture the last frame: ${error.message}`, "error");
  }
}

/* --------------------------------------------------------------- retry -- */

// The backend keeps each job's reference files for 42h (see
// backend/retry_cache.py) and only reports retry_expires_at while that
// snapshot is still live; this re-checks the clock so the button also goes
// away on a page that's been left open past the deadline.
function retryAvailable(job) {
  return Boolean(job.retry_expires_at) && !ACTIVE_STATES.has(job.state) && job.state !== "finalizing"
    && new Date(job.retry_expires_at).getTime() > Date.now();
}

function retryTitle(job) {
  const hours = Math.max(0, (new Date(job.retry_expires_at).getTime() - Date.now()) / 3_600_000);
  const left = hours >= 1 ? `${Math.floor(hours)}h` : `${Math.max(1, Math.round(hours * 60))}m`;
  return `Load this generation's exact settings, references and seed into the form (available for ${left} more)`;
}

// ISO timestamps from the server are UTC; show them in local time.
function infoValue(value) {
  return /^\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:\d{2})$/.test(value) ? new Date(value).toLocaleString() : value;
}

// The (i) panel's Stats tab: where the render's time went, from details.stats
// (backend/job_details.py job_stats) - a workflow map in run order, then every
// stage slowest first, sampler-like nodes with their per-step times.
const JOB_INFO_TAB_KEY = "h3-job-info-tab";
const STATS_CATEGORY_ORDER = ["wait", "load", "encode", "sample", "upscale", "decode", "output", "other"];

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
    return `<p class="hint">${escapeHtml(stats?.message || "No stage timings for this generation.")}</p>`;
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
        <p class="hint">${steps.length} ${stepWord}s · first ${formatStatSeconds(steps[0])} (includes getting the model onto the GPU)${average ? ` · then ${formatStatSeconds(average)} per ${stepWord} on average` : ""}</p>
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

// Hover / tap tooltip for the map and step bars, bound once on the pane (its contents get replaced).
function bindJobStats(pane, jobId, stats) {
  if (!pane) return;
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
  if (stats?.running) pollJobStats(pane, jobId);
}

// A running job's Stats refresh every 5 s while the panel is open (skipped while the Details tab shows).
function pollJobStats(pane, jobId) {
  setTimeout(async () => {
    if ($("#job-info").hidden || !pane.isConnected) return;
    if (pane.hidden) { pollJobStats(pane, jobId); return; }
    let details;
    try {
      details = await Api.request(`${API_BASE}/api/jobs/${jobId}/details`);
    } catch {
      pollJobStats(pane, jobId); // keep the last numbers, try again
      return;
    }
    if (!pane.isConnected) return;
    const open = new Set([...pane.querySelectorAll(".stats-row details[open] .stats-row-name")].map((node) => node.textContent));
    pane.innerHTML = jobStatsHtml(details.stats);
    pane.querySelectorAll(".stats-row details").forEach((node) => { if (open.has(node.querySelector(".stats-row-name")?.textContent)) node.open = true; });
    if (details.stats?.running) pollJobStats(pane, jobId);
  }, 5000);
}

// The (i) panel: every setting a job ran with, from GET /api/jobs/{id}/details.
async function openJobInfo(jobId) {
  $("#job-info-title").textContent = "Generation details";
  const body = $("#job-info-body");
  body.innerHTML = `<p class="hint">Loading…</p>`;
  $("#job-info").hidden = false;
  $("#job-info-close").focus();
  let details;
  try {
    details = await Api.request(`${API_BASE}/api/jobs/${jobId}/details`);
  } catch (error) {
    body.innerHTML = `<p class="job-error">${escapeHtml(error.message || "Could not load details.")}</p>`;
    return;
  }
  if ($("#job-info").hidden) return; // closed while loading
  const inputThumbs = (details.inputs || []).map((input) => `
    <a class="session-asset" href="${escapeHtml(input.url)}" target="_blank" rel="noopener" title="${escapeHtml(`${input.kind}: ${input.filename}`)}">
      ${input.small_url ? `<img src="${escapeHtml(input.small_url)}" alt="" loading="lazy">` : `<span class="session-asset-icon">${escapeHtml(input.category)}</span>`}
      <span>${escapeHtml(input.token || input.kind)}</span></a>`).join("");
  const sections = details.sections.map((section) => `
    <section class="job-info-section">
      <h3>${escapeHtml(section.title)}</h3>
      <dl>${section.rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(infoValue(value))}</dd>`).join("")}</dl>
      ${section.title === "Inputs" && inputThumbs ? `<div class="session-assets job-info-inputs">${inputThumbs}</div>` : ""}
    </section>`).join("");
  const job = state.jobs.find((item) => item.id === jobId);
  const settingsText = job && job.model !== "stitch" && MODELS[job.model] ? jobSettingsText(details.sections) : "";
  let tab = "details";
  try { tab = localStorage.getItem(JOB_INFO_TAB_KEY) === "stats" ? "stats" : "details"; } catch { /* storage blocked */ }
  body.innerHTML = `
    <div class="job-info-tabs" role="tablist" aria-label="Generation details">
      <button type="button" role="tab" data-info-tab="details">Details</button>
      <button type="button" role="tab" data-info-tab="stats">Stats</button>
    </div>
    <div class="job-info-pane" data-info-pane="stats" role="tabpanel">${jobStatsHtml(details.stats)}</div>
    <div class="job-info-pane" data-info-pane="details" role="tabpanel">
    ${details.prompt ? `<section class="job-info-section">
      <div class="job-info-heading">
        <h3>Prompt</h3>
        <button type="button" class="secondary-button job-icon-button" id="job-info-copy" title="Copy prompt" aria-label="Copy prompt">${JOB_ICONS.copy}</button>
      </div>
      <pre class="job-info-prompt">${escapeHtml(details.prompt)}</pre>
    </section>` : ""}
    ${settingsText ? `<div class="job-info-heading">
      <h3 class="job-info-settings-title">Settings</h3>
      <button type="button" class="secondary-button job-icon-button" id="job-info-load-settings" title="Load these settings into the form (keeps your prompt and inputs)" aria-label="Load settings into the form">${JOB_ICONS.retry}</button>
    </div>` : ""}
    ${sections}
    <p class="hint">${escapeHtml(details.note)}</p>
    </div>`;
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
  bindJobStats(body.querySelector('[data-info-pane="stats"]'), jobId, details.stats);
  const copy = async (text, what) => {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} copied.`);
    } catch {
      toast(`Could not copy - select the ${what.toLowerCase()} text instead.`, "error");
    }
  };
  $("#job-info-copy")?.addEventListener("click", () => copy(details.prompt, "Prompt"));
  $("#job-info-load-settings")?.addEventListener("click", () => loadJobSettings(jobId, details));
}

// The Agentic session (i) panel: the prompt, guide and settings the session's
// first run was sent, from GET /api/agentic/sessions/{id}/start.
// The session folder: assets the session owns, each plan clip's current prompt + its history, and
// "Rewrite clips" (notes -> rewritten prompts -> re-render at the final preset, no new research or plan).
async function openAgenticSessionFolder(sessionId) {
  $("#job-info-title").textContent = "Agentic session folder";
  const body = $("#job-info-body");
  body.innerHTML = `<p class="hint">Loading…</p>`;
  $("#job-info").hidden = false;
  $("#job-info-close").focus();
  let folder;
  try {
    folder = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}/folder`, {cache: "no-store"});
  } catch (error) {
    body.innerHTML = `<p class="job-error">${escapeHtml(error.message || "Could not load the session folder.")}</p>`;
    return;
  }
  if ($("#job-info").hidden) return;
  const time = (seconds) => new Date(seconds * 1000).toLocaleString();
  const clipRows = folder.clips.map((clip) => `
    <section class="job-info-section session-clip">
      <div class="job-info-heading">
        <h3><label><input type="checkbox" class="session-clip-pick" value="${clip.id}" checked> Clip ${clip.id}</label>
          <small>${escapeHtml(`${clip.action} · ${clip.mode} · ${clip.duration}s${clip.refs.length ? ` · refs ${clip.refs.join(", ")}` : ""}${clip.render_id ? "" : " · not rendered"}`)}</small></h3>
        <button type="button" class="secondary-button job-icon-button" data-copy-clip="${clip.id}" title="Copy prompt" aria-label="Copy clip ${clip.id} prompt">${JOB_ICONS.copy}</button>
      </div>
      <pre class="job-info-prompt">${escapeHtml(clip.prompt)}</pre>
      ${clip.history.length > 1 ? `<details class="session-history"><summary>Prompt history (${clip.history.length})</summary>${clip.history.slice().reverse().map((entry) => `
        <div class="session-history-entry"><div class="hint">${escapeHtml(time(entry.at))} · ${escapeHtml(entry.why)}</div><pre class="job-info-prompt">${escapeHtml(entry.prompt)}</pre></div>`).join("")}</details>` : ""}
    </section>`).join("");
  const assets = folder.assets.map((ref) => {
    const thumb = ref.category === "image" ? ref.small_url || ref.preview_url : ref.small_url || ref.thumbnail_url;
    return `<a class="session-asset" href="${escapeHtml(ref.preview_url)}" target="_blank" rel="noopener" title="${escapeHtml(ref.filename)}">
      ${thumb ? `<img src="${escapeHtml(thumb)}" alt="" loading="lazy">` : `<span class="session-asset-icon">${escapeHtml(ref.category)}</span>`}
      <span>${escapeHtml(ref.alias)}${ref.duration ? ` · ${Number(ref.duration).toFixed(1)}s` : ""}</span></a>`;
  }).join("");
  body.innerHTML = `
    <section class="job-info-section">
      <p class="hint">Kept until the session is deleted. On disk: <code>${escapeHtml(folder.folder)}</code> (assets/ + prompts.md).</p>
    </section>
    ${folder.clips.length ? `<section class="job-info-section session-rewrite">
      <h3>Rewrite clips</h3>
      <p class="hint">Hermes rewrites the ticked clips' prompts to follow your notes (leave empty to re-render them as they are), then renders them at the final preset. No new research or plan; the session's assets are reused.</p>
      <textarea id="session-rewrite-notes" rows="3" placeholder="What should change in the ticked clips?"></textarea>
      <div class="session-rewrite-actions"><button type="button" class="secondary-button" id="session-rewrite-all">Tick all</button><button type="button" class="secondary-button" id="session-rewrite-none">Untick all</button>
      <button type="button" class="primary-button" id="session-rewrite-go" ${folder.busy ? "disabled title=\"This session is running\"" : ""}>Rewrite &amp; render ticked clips</button></div>
    </section>` : `<p class="hint">${escapeHtml(folder.plan_error || "No plan yet.")}</p>`}
    ${clipRows}
    <section class="job-info-section"><h3>Assets (${folder.assets.length})</h3><div class="session-assets">${assets || `<p class="hint">No assets yet.</p>`}</div></section>`;
  $$("[data-copy-clip]", body).forEach((button) => button.addEventListener("click", () => {
    const clip = folder.clips.find((item) => String(item.id) === button.dataset.copyClip);
    navigator.clipboard?.writeText(clip.prompt).then(() => toast(`Clip ${clip.id} prompt copied.`), () => toast("Couldn't copy.", "error"));
  }));
  $("#session-rewrite-all", body)?.addEventListener("click", () => $$(".session-clip-pick", body).forEach((box) => { box.checked = true; }));
  $("#session-rewrite-none", body)?.addEventListener("click", () => $$(".session-clip-pick", body).forEach((box) => { box.checked = false; }));
  $("#session-rewrite-go", body)?.addEventListener("click", async (event) => {
    const clips = $$(".session-clip-pick", body).filter((box) => box.checked).map((box) => Number(box.value));
    if (!clips.length) return toast("Tick at least one clip.", "error");
    const notes = $("#session-rewrite-notes", body).value.trim();
    if (!confirm(`Render ${clips.length} clip${clips.length === 1 ? "" : "s"} at the final preset${notes ? " after Hermes rewrites them" : " with their current prompts"}?`)) return;
    event.currentTarget.disabled = true;
    try {
      await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}/rewrite`, { method: "POST", body: { notes, clips } });
      $("#job-info").hidden = true;
      toast(`Rewriting ${clips.length} clip${clips.length === 1 ? "" : "s"}. Follow it in the Hermes Agent log.`);
      await adoptAgenticSession({ id: sessionId, kind: "resumed" });
    } catch (error) {
      toast(error.message, "error");
      event.currentTarget.disabled = false;
    }
  });
}

async function openAgenticSessionInfo(sessionId) {
  $("#job-info-title").textContent = "Agentic session start";
  const body = $("#job-info-body");
  body.innerHTML = `<p class="hint">Loading…</p>`;
  $("#job-info").hidden = false;
  $("#job-info-close").focus();
  let start;
  try {
    start = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}/start`, {cache: "no-store"});
  } catch (error) {
    body.innerHTML = `<p class="job-error">${escapeHtml(error.message || "Could not load the session start.")}</p>`;
    return;
  }
  if ($("#job-info").hidden) return; // closed while loading
  const textBlock = (title, text, id) => `<section class="job-info-section">
      <div class="job-info-heading">
        <h3>${escapeHtml(title)}</h3>
        <button type="button" class="secondary-button job-icon-button" id="${id}" title="Copy" aria-label="Copy ${escapeHtml(title.toLowerCase())}">${JOB_ICONS.copy}</button>
      </div>
      <pre class="job-info-prompt">${escapeHtml(text)}</pre>
    </section>`;
  // An empty prompt box sends the guide as the prompt (AgenticRequest), so show it once.
  const blocks = start.guide_as_prompt
    ? textBlock("Request (the guide, sent with an empty prompt box)", start.prompt, "agentic-info-copy-prompt")
    : textBlock("Prompt", start.prompt, "agentic-info-copy-prompt")
      + (start.guide.trim() ? textBlock("Agentic guide", start.guide, "agentic-info-copy-guide") : "");
  const sections = start.sections.map((section) => `
    <section class="job-info-section">
      <h3>${escapeHtml(section.title === "Agentic" ? section.title : `Form: ${section.title}`)}</h3>
      <dl>${section.rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(infoValue(value))}</dd>`).join("")}</dl>
    </section>`).join("");
  body.innerHTML = `${blocks}${sections}
    <p class="hint">"Form" sections are the Studio settings sent with the request; Hermes renders each clip with the staging/final preset over them.${
      start.exact ? "" : " This session was started before runs kept their own settings and has been resumed since, so the settings shown are the latest run's (the prompt and guide are the first run's)."}</p>`;
  const copy = async (text, what) => {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} copied.`);
    } catch {
      toast(`Could not copy - select the ${what.toLowerCase()} text instead.`, "error");
    }
  };
  $("#agentic-info-copy-prompt")?.addEventListener("click", () => copy(start.prompt, start.guide_as_prompt ? "Request" : "Prompt"));
  $("#agentic-info-copy-guide")?.addEventListener("click", () => copy(start.guide, "Guide"));
}

// Sections left out of "Copy settings": uploaded assets, and run bookkeeping (IDs, timestamps).
const JOB_INFO_UNCOPIED_SECTIONS = new Set(["Inputs", "Run"]);

function jobSettingsText(sections) {
  return sections.filter((section) => !JOB_INFO_UNCOPIED_SECTIONS.has(section.title) && section.rows.length)
    .map((section) => [`[${section.title}]`, ...section.rows.map(([label, value]) => `${label}: ${value}`)].join("\n"))
    .join("\n\n");
}

// Request fields "Load settings" never takes from a job: the prompt and uploaded inputs.
const JOB_SETTINGS_EXCLUDED_FIELDS = ["prompt", "reference_ids", "reference_video_ids", "reference_audio_ids",
  "first_frame_id", "last_frame_id", "continue_from_job_id", "overlap_frames"];

// Rebuilds request fields from the details rows (backend/job_details.py formats them).
// Used until the running server returns details.request. Rows the job didn't show
// (e.g. Parasyte on a Reference job) leave the current form value alone.
function settingsFromDetails(details, model) {
  const rows = new Map(details.sections.flatMap((section) => section.rows.map(([label, value]) => [`${section.title}/${label}`, String(value)])));
  const get = (key) => rows.get(key);
  const num = (text) => { const value = parseFloat(text); return Number.isFinite(value) ? value : undefined; };
  const onOff = (text) => text === undefined ? undefined : text.startsWith("On");
  const inParens = (text) => text?.match(/\(([^()]*)\)\s*$/)?.[1];
  const loraLines = (text) => (!text || text === "None") ? [] : text.split("\n").map((line) => {
    const at = line.lastIndexOf(" @ ");
    return { label: line.slice(0, at), strength: num(line.slice(at + 3)) };
  });
  const out = {};
  const set = (field, value) => { if (value !== undefined && value !== null && !Number.isNaN(value)) out[field] = value; };

  const size = get("Output/Resolution")?.match(/^(\d+)×(\d+)/);
  if (size) {
    set("width", Number(size[1]));
    set("height", Number(size[2]));
    set("megapixels", Number(size[1]) * Number(size[2]) / 1_000_000);
  }
  set("aspect", get("Output/Aspect"));
  set("duration", num(get("Output/Duration")));
  set("seed", num(get("Sampling/Seed")));
  const fps = num(get("Output/FPS"));

  if (model === "wan_2_2") {
    if (fps) set("wan_fps", Math.round(fps));
    set("steps", num(get("Sampling/Steps")));
    set("wan_high_steps", num(get("Sampling/High-noise steps")));
    set("sampler", get("Sampling/Sampler"));
    set("scheduler", get("Sampling/Scheduler"));
    set("wan_cfg", num(get("Sampling/CFG")));
    set("wan_shift", num(get("Sampling/Shift")));
    const turbo = get("Workflow/Turbo");
    if (turbo) set("wan_turbo", turbo.startsWith("Off") ? "off" : turbo.startsWith("Slow-Mo") ? "motionfix" : "lightx2v");
    const lines = get("Workflow/LoRAs");
    if (lines !== undefined) {
      out.wan_loras = loraLines(lines).map(({ label, strength }) => {
        const lora = (state.options.wan_loras || []).find((item) => item.label === label);
        return lora && { lora: lora.filename, strength: strength ?? lora.default_strength };
      }).filter(Boolean);
    }
    return out;
  }

  if (model === "ltx_2_5") {
    if (fps) set("ltx_fps", Math.round(fps));
    const workflowLabel = get("Workflow/Workflow")?.replace(/\s*\([^()]*\)\s*$/, "");
    set("ltx_workflow", state.options.ltx_workflows.find((item) => item.label === workflowLabel)?.value);
    const tiles = get("Workflow/VAE decode tiles")?.toLowerCase();
    set("ltx_tile_preset", state.options.ltx_tile_presets.find((item) => item.value === tiles)?.value);
    const lines = get("Workflow/LoRAs");
    if (lines !== undefined) {
      out.ltx_loras = loraLines(lines).map(({ label, strength }) => {
        const lora = state.options.ltx_loras.find((item) => item.label === label);
        return lora && { lora: lora.filename, strength: strength ?? lora.default_strength };
      }).filter(Boolean);
    }
    return out;
  }

  const steps = get("Sampling/Steps");
  set("steps", steps?.includes("PDD fixed") ? num(steps.match(/form's (\d+)/)?.[1]) : num(steps));
  set("sampler", get("Sampling/Sampler"));
  const scheduler = get("Sampling/Scheduler");
  if (scheduler && !scheduler.startsWith("PDD")) set("scheduler", scheduler);
  const checkpoint = get("Model/Checkpoint");
  if (checkpoint !== undefined) out.checkpoint = checkpoint.startsWith("Workflow default") ? null : (inParens(checkpoint) ?? null);
  const loras = get("Model/LoRAs");
  if (loras !== undefined) {
    const byLabel = new Map(loraLines(loras).map((item) => [item.label, item.strength]));
    out.loras = state.options.minimax_h3_lora_slots.map((slot) => byLabel.has(slot.label)
      ? { enabled: true, strength: byLabel.get(slot.label) ?? slot.default_strength } : { enabled: false });
  }
  set("reference_sizing", get("Speedup & Saves/Reference sizing") ?? get("SeedHunter/Reference sizing"));

  if (model === "seedhunter") {
    set("candidate_count", num(get("SeedHunter/Candidates")));
    set("enable_live_preview", onOff(get("SeedHunter/Live preview")));
    return out;
  }

  if (fps) {
    const rates = $$("#h3-fps option").map((option) => Number(option.value));
    set("h3_fps", rates.find((rate) => Math.abs(rate - fps) < 0.5));
  }
  set("enable_upscale", rows.has("Latent upscale/Factor"));
  if (out.enable_upscale) {
    set("upscale_factor", num(get("Latent upscale/Factor")));
    // Upscaled jobs list their final size; the form wants the base (H3 snaps sizes to 32).
    if (out.width && out.upscale_factor) {
      out.width = Math.round(out.width / out.upscale_factor / 32) * 32;
      out.height = Math.round(out.height / out.upscale_factor / 32) * 32;
      out.megapixels = out.width * out.height / 1_000_000;
    }
    const upscaleSteps = get("Latent upscale/Steps");
    if (!upscaleSteps.includes("PDD")) set("upscale_steps", num(upscaleSteps));
    set("upscale_denoise", num(get("Latent upscale/Denoise")));
    set("upscale_seed", num(get("Latent upscale/Seed")));
  }
  const parasyte = get("Speedup & Saves/Parasyte turbo LoRA");
  if (parasyte) {
    set("parasyte_enabled", onOff(parasyte));
    set("parasyte_strength", num(parasyte.split("@")[1]));
  }
  const sla = get("Speedup & Saves/SLA attention");
  if (sla) {
    set("sla_enabled", onOff(sla));
    set("sla_sparsity", num(sla.match(/sparsity ([\d.]+)/)?.[1]));
    if (sla.includes("dense first step")) set("sla_dense_step", sla.endsWith("On"));
  }
  const turbo = get("Speedup & Saves/Reference turbo LoRA");
  if (turbo) {
    set("ref2v_turbo_enabled", !turbo.startsWith("Off"));
    if (turbo.startsWith("PDD")) set("ref2v_turbo", "pdd");
    if (turbo.startsWith("LightX")) {
      set("ref2v_turbo", "lightx");
      set("ref2v_strength", num(turbo.split("@")[1]));
    }
  }
  const attention = get("Speedup & Saves/Attention");
  if (attention) set("attention", attention.startsWith("Flash") ? "flash" : attention.startsWith("Comfy") ? "kitchen" : "pytorch");
  else if (get("Speedup & Saves/Comfy-Kitchen attention")) set("attention", onOff(get("Speedup & Saves/Comfy-Kitchen attention")) ? "kitchen" : "pytorch");
  set("spectrum_enabled", onOff(get("Speedup & Saves/Spectrum forecasting")));
  set("block_cache_enabled", onOff(get("Speedup & Saves/H3 block cache")));
  set("ffn_chunking_enabled", onOff(get("Speedup & Saves/FFN chunking")));
  const lowVram = get("Speedup & Saves/Low-VRAM attention");
  if (lowVram) {
    set("low_vram_attention_enabled", lowVram.startsWith("on"));
    if (lowVram.startsWith("on")) set("low_vram_attention_chunks", num(lowVram.match(/(\d+) head/)?.[1]));
  }
  set("torch_compile_enabled", onOff(get("Speedup & Saves/torch.compile")));
  set("ref_video_downscale", onOff(get("Speedup & Saves/Ref video downscale")));
  return out;
}

// The (i) panel's load button: a job's settings into the form, switching model/mode
// to match. The prompt box and the attached references/keyframes stay as they are.
async function loadJobSettings(jobId, details) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job || !MODELS[job.model]) return;
  let settings;
  if (details.request) {
    settings = { ...details.request };
    JOB_SETTINGS_EXCLUDED_FIELDS.forEach((field) => delete settings[field]);
  } else {
    settings = settingsFromDetails(details, job.model);
  }
  const prompt = job.model === state.model ? $("#prompt").value : (promptDrafts[job.model] || defaultPrompts[job.model]);
  const isSeedhunter = job.model === "seedhunter";
  await applyRetryRequest({
    ...collectSettings(), ...settings, model: job.model, mode: job.mode, prompt,
    first_frame_id: isSeedhunter ? state.shFirstFrameId : state.firstFrameId,
    last_frame_id: isSeedhunter ? state.shLastFrameId : state.lastFrameId,
    continue_from_job_id: state.continueFromJob?.id ?? null,
  });
  // Same fields applyPreset() fills: applyRetryRequest skips them while upscale is off.
  if (job.model === "minimax_h3") {
    setField("#h3-upscale-factor", settings.upscale_factor);
    setField("#h3-upscale-steps", settings.upscale_steps);
    setField("#h3-upscale-denoise", settings.upscale_denoise);
    setField("#h3-upscale-chunking", settings.upscale_chunking ?? true);
    writeUpscaleAdvanced(settings.upscale_advanced);
  }
  if (!settings.aspect) updateMegapixelsFromResolution(); // details only list the aspect when it drove the size
  renderPresetBar();
  closeJobInfo();
  if (isMobileLayout()) setMobileTab("create");
  $("#generate-form").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("Settings loaded. Your prompt and inputs were kept.");
}

function closeJobInfo() {
  $("#job-info").hidden = true;
  $("#job-info-body").innerHTML = "";
}

async function retryJob(jobId, button) {
  if (button) button.disabled = true;
  try {
    let result;
    try {
      result = await Api.request(`${API_BASE}/api/jobs/${jobId}/retry`, { method: "POST", body: {} });
    } catch (error) {
      if (error.code !== "retry_replaces_references") throw error;
      if (!confirm(`${error.message}\n\nContinue?`)) return;
      result = await Api.request(`${API_BASE}/api/jobs/${jobId}/retry`, { method: "POST", body: { replace_references: true } });
    }
    state.references = await Api.request(`${API_BASE}/api/references`);
    await applyRetryRequest(result.request);
    $("#generate-form").scrollIntoView({ behavior: "smooth", block: "start" });
    const restored = result.restored ? ` ${result.restored} reference(s) restored from the saved copy.` : "";
    toast(`Loaded the exact settings, references and seed.${restored} Review, then Generate.`);
  } catch (error) {
    toast(error.message, "error");
    if (error.code === "retry_expired") button?.remove();
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

function setField(id, value) {
  if (value === undefined || value === null) return;
  const input = $(id);
  if (input.type === "checkbox") input.checked = Boolean(value);
  else input.value = value;
}

async function applyRetryRequest(request) {
  const isSeedhunter = request.model === "seedhunter";
  state.savedResolution = null;
  state.mode = request.mode;
  await setModel(request.model);
  if (!isSeedhunter) setMode(request.mode);
  $("#prompt").value = request.prompt;
  promptDrafts[request.model] = request.prompt;

  // After setModel()/setMode(): both reset resolution and step defaults.
  setField("#aspect", request.aspect);
  // The server stores the exact width*height/1e6 (e.g. 0.90112), not what was typed.
  if (request.megapixels != null) setField("#megapixels", Number(formatMegapixels(request.megapixels)));
  setField("#width", request.width);
  setField("#height", request.height);
  $("#resolution-detail").textContent = `Output guide ${request.width} × ${request.height} · ${formatMegapixels(Number(request.megapixels))} MP`;
  setField("#duration", request.duration);
  $("#duration-readout").textContent = durationReadout(request.duration);
  setField("#seed", request.seed);
  const isWan = request.model === "wan_2_2";
  const fieldPrefix = isSeedhunter ? "#sh-" : isWan ? "#wan-" : "#";
  setField(`${fieldPrefix}steps`, request.steps);
  setField(`${fieldPrefix}sampler`, request.sampler);
  setField(`${fieldPrefix}scheduler`, request.scheduler);
  if (isWan) {
    setField("#wan-turbo", request.wan_turbo ?? "lightx2v");
    setField("#wan-cfg", request.wan_cfg ?? 1);
    setField("#wan-shift", request.wan_shift ?? 8);
    $("#wan-high-steps").value = request.wan_high_steps ?? "";
    setField("#wan-fps", request.wan_fps ?? 16);
    setField("#wan-output-quality", request.output_quality || "standard");
    $("#wan-negative-prompt").value = request.negative_prompt || "";
  }
  setField("#parasyte", request.parasyte_strength);
  setField("#sla", request.sla_sparsity);
  setField("#ref-strength", request.ref2v_strength);
  setField("#parasyte-enabled", request.parasyte_enabled ?? true); // older jobs predate the toggles
  setField("#sla-enabled", request.sla_enabled ?? true);
  setField("#ref2v-turbo-enabled", request.ref2v_turbo_enabled ?? true);
  setField("#ref2v-turbo", request.ref2v_turbo ?? "lightx"); // jobs from before the choice ran LightX
  // Requests from before the selector only have the Kitchen toggle.
  setField("#attention-backend", request.attention ?? (request.kitchen_attention_enabled === false ? "pytorch" : "kitchen"));
  setField("#ref-video-downscale", request.ref_video_downscale ?? true);
  setField("#experimental-ref-speedups", request.experimental_ref_speedups ?? false);
  setField("#sla-dense-step", request.sla_dense_step ?? true);
  setField("#spectrum-enabled", request.spectrum_enabled ?? false);
  setField("#block-cache-enabled", request.block_cache_enabled ?? false);
  setField("#ffn-chunking-enabled", request.ffn_chunking_enabled ?? false);
  setField("#low-vram-attention-enabled", request.low_vram_attention_enabled ?? false);
  setField("#low-vram-attention-chunks", request.low_vram_attention_chunks ?? 4);
  setField("#multi-gpu-offload", request.multi_gpu_offload ?? "off");
  setField("#multi-gpu-split-gb", request.multi_gpu_split_gb ?? 8);
  $("#multi-gpu-split-row").hidden = $("#multi-gpu-offload").value !== "split";
  renderMultiGpuPresets();
  setField("#torch-compile-enabled", request.torch_compile_enabled ?? false);
  renderSpeedups();
  setField("#ref-size", request.reference_sizing);
  setField("#ltx-workflow", request.ltx_workflow);
  setField("#ltx-tiles", request.ltx_tile_preset);
  $("#ltx-tile-detail").textContent = ltxTileDetails[request.ltx_tile_preset] || "";
  setField("#ltx-fps", request.ltx_fps);
  setField("#h3-fps", request.h3_fps ?? 24);
  if (!isWan) { // Wan keeps its own copies of these (#wan-...), so H3's stay as they were
    setField("#cfg", request.cfg ?? 1); // older jobs predate these
    $("#negative-prompt").value = request.negative_prompt || "";
    $("#shift-video").value = request.sigma_shift_video ?? "";
    $("#shift-audio").value = request.sigma_shift_audio ?? "";
    setField("#output-quality", request.output_quality || "standard");
  }
  renderCfg();
  setField("#sh-candidate-count", request.candidate_count);
  setField("#sh-overlap-frames", request.overlap_frames);
  setField("#sh-preview-enable", request.enable_live_preview);
  setField("#sh-upscale-enable", request.enable_upscale);
  setField("#sh-upscale-model", request.upscale_model);
  setField("#sh-upscale-megapixels", request.upscale_megapixels);
  setField("#sh-upscale-steps", request.upscale_steps);
  setField("#sh-upscale-denoise", request.upscale_denoise);
  setField("#sh-upscale-seed", request.upscale_seed);
  setField("#sh-rife-enable", request.enable_rife);
  setField("#sh-rife-fps", request.rife_target_fps);
  $("#sh-upscale-enable").dispatchEvent(new Event("change"));
  $("#sh-rife-enable").dispatchEvent(new Event("change"));
  const h3Upscale = request.model === "minimax_h3" && request.enable_upscale;
  setField("#h3-upscale-enable", h3Upscale);
  if (h3Upscale) {
    setField("#h3-upscale-factor", request.upscale_factor ?? 1.5);
    setField("#h3-upscale-steps", request.upscale_steps);
    setField("#h3-upscale-denoise", request.upscale_denoise);
    setField("#h3-upscale-seed", request.upscale_seed);
    setField("#h3-upscale-chunking", request.upscale_chunking ?? true); // jobs from before the toggle
    writeUpscaleAdvanced(request.upscale_advanced); // jobs from before it get the defaults
  }
  $("#h3-upscale-params").hidden = !h3Upscale;
  updateUpscaleCheck();
  $("#minimax-checkpoint").value = request.checkpoint || "";
  updateCheckpointHint();

  $$("[data-lora-index]", $("#minimax-lora-list")).forEach((row) => {
    const slot = (request.loras || [])[Number(row.dataset.loraIndex)] || { enabled: false };
    $("[data-lora-enabled]", row).checked = Boolean(slot.enabled) && !row.hidden;
    if (slot.strength !== undefined) $("[data-lora-strength]", row).value = slot.strength;
    row.classList.toggle("is-enabled", Boolean(slot.enabled) && !row.hidden);
  });
  Object.values(LIST_LORAS).forEach((list) => {
    const host = $(list.list);
    const byName = new Map((request[list.key] || []).map((item) => [item.lora, item.strength]));
    $$("[data-ltx-lora]", host).forEach((row) => {
      const enabled = byName.has(row.dataset.ltxLora);
      $("[data-lora-enabled]", row).checked = enabled;
      if (enabled) $("[data-lora-strength]", row).value = byName.get(row.dataset.ltxLora);
      row.classList.toggle("is-enabled", enabled);
    });
    host.dispatchEvent(new Event("change")); // refreshes the "(N on)" count and the max-4 lockout
  });
  renderPromptSnippets(); // checkboxes set directly above fire no "change"
  updateLoraCount();

  if (isSeedhunter) {
    state.shFirstFrameId = request.first_frame_id || null;
    state.shLastFrameId = request.last_frame_id || null;
    const source = request.continue_from_job_id && state.jobs.find((job) => job.id === request.continue_from_job_id);
    state.continueFromJob = source ? { id: source.id, prompt: source.prompt } : null;
    renderContinuationBanner();
  } else if (request.mode === "fl2v") {
    state.firstFrameId = request.first_frame_id || null;
    state.lastFrameId = request.last_frame_id || null;
  }
  renderKeyframes();
  renderReferenceGallery();
  renderSeedhunterGalleries();
  renderSpeedups(); // after the checkpoint is restored - LightX's lock depends on it
  closeAliasMenu();
}

function clearContinuation() {
  state.continueFromJob = null;
  renderContinuationBanner();
}

function renderContinuationBanner() {
  const banner = $("#sh-continue-banner");
  const active = Boolean(state.continueFromJob);
  banner.hidden = !active;
  $("#sh-overlap-row").hidden = !active;
  if (active) $("#sh-continue-banner-text").textContent = `Continuing from: "${state.continueFromJob.prompt.slice(0, 80)}"`;
}

async function finalizeJob(id) {
  const choice = finalizeChoice(id);
  const button = $(`[data-finalize="${id}"]`);
  if (button) { button.disabled = true; button.textContent = "Finalizing…"; }
  try {
    const job = await Api.request(`${API_BASE}/api/jobs/${id}/finalize`, { method: "POST", body: {
      candidate: choice.candidate,
      enable_upscale: choice.enableUpscale,
      upscale_model: $("#sh-upscale-model").value,
      upscale_megapixels: Number($("#sh-upscale-megapixels").value),
      upscale_steps: Number($("#sh-upscale-steps").value),
      upscale_denoise: Number($("#sh-upscale-denoise").value),
      upscale_seed: Number($("#sh-upscale-seed").value),
      enable_rife: choice.enableRife,
      rife_target_fps: Number($("#sh-rife-fps").value),
    }});
    finalizeChoices.delete(id);
    state.jobs = state.jobs.map((item) => item.id === id ? job : item);
    renderJobs();
    toast("Finalizing selected candidate.");
  } catch (error) {
    toast(error.message, "error");
    if (button) { button.disabled = false; button.textContent = "Finalize"; }
  }
}

async function cancelJob(id) {
  try {
    const result = await Api.request(`${API_BASE}/api/jobs/${id}/cancel`, { method: "POST" });
    if (result.job) state.jobs = state.jobs.map((job) => job.id === id ? result.job : job);
    renderJobs();
    toast(result.job?.state === "cancelling" ? "Cancellation requested." : "Generation cancelled.");
  } catch (error) { toast(error.message, "error"); }
}

// Holding a button (~0.5s) runs onLongPress instead of its click. The click
// that follows the release is swallowed page-wide (onLongPress may re-render
// the card, so the click can land on a new button), and the phone's hold
// menu is blocked.
const LONG_PRESS_MS = 500;
function bindLongPress(button, onLongPress) {
  let timer = null;
  let start = null;
  const cancel = () => { clearTimeout(timer); timer = null; };
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    start = { x: event.clientX, y: event.clientY };
    cancel();
    timer = setTimeout(() => {
      timer = null;
      swallowNextClick();
      navigator.vibrate?.(30);
      onLongPress();
    }, LONG_PRESS_MS);
  });
  button.addEventListener("pointermove", (event) => {
    if (timer && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) cancel(); // scrolling
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((type) => button.addEventListener(type, cancel));
  button.addEventListener("contextmenu", (event) => event.preventDefault());
}

// Long press on the extreme thumbs up: save it without the questionnaire,
// keeping any reasons already picked.
function swallowNextClick() {
  const swallow = (event) => { event.stopPropagation(); event.preventDefault(); };
  document.addEventListener("click", swallow, { capture: true, once: true });
  // A release that produces no click (finger slid off) must not eat the next real tap.
  document.addEventListener("pointerdown", () => document.removeEventListener("click", swallow, true), { capture: true, once: true });
}

async function quickExtremeRating(id) {
  const job = state.jobs.find((item) => item.id === id);
  if (await saveRating(id, 2, job?.rating === 2 ? job.rating_reasons || [] : [])) toast("Extreme thumbs up saved");
}

async function rateJob(button) {
  const id = button.dataset.rateJob;
  const job = state.jobs.find((item) => item.id === id);
  const value = Number(button.dataset.rate);
  const rating = job?.rating === value ? 0 : value; // clicking the active thumb clears it
  if (rating === 2) { openBestReasons(id); return; } // saved from the questionnaire
  await saveRating(id, rating);
}

async function saveRating(id, rating, reasons = []) {
  try {
    const updated = await Api.request(`${API_BASE}/api/jobs/${id}/rating`, { method: "POST", body: { rating, reasons } });
    state.jobs = state.jobs.map((item) => item.id === id ? updated : item);
    renderJobs();
    scheduleEstimate(); // the score for the current settings may have changed
    return true;
  } catch (error) { toast(error.message, "error"); return false; }
}

// "Why was it best quality?" - tap-only questionnaire behind the extreme
// thumbs up. Answers (estimates.RATING_REASONS) are totalled per settings
// combo on the Best settings tab.
let ratingReasons = null; // every [[key, label], ...] from /api/rating-reasons, for card labels
let ratingReasonsLoading = null;

function loadRatingReasons() {
  ratingReasonsLoading ??= Api.request(`${API_BASE}/api/rating-reasons`)
    .then(({ reasons }) => { ratingReasons = reasons; if (jobsVisible()) renderJobs(); })
    .catch(() => { ratingReasonsLoading = null; });
  return ratingReasonsLoading;
}

function reasonLabel(key) {
  return ratingReasons?.find(([reasonKey]) => reasonKey === key)?.[1] || key;
}

function jobBestReasons(job) {
  const reasons = job.rating_reasons || [];
  const label = reasons.length
    ? reasons.map((key) => `<span class="best-reason-chip">${escapeHtml(reasonLabel(key))}</span>`).join("")
    : `<span class="best-reason-empty">Why was it best? Tap to tell</span>`;
  return `<button type="button" class="job-best-reasons" data-best-reasons="${job.id}" title="Edit why this was best quality">${uiIcon("star")} ${label}</button>`;
}

async function openBestReasons(id) {
  const job = state.jobs.find((item) => item.id === id);
  if (!job) return;
  let options;
  try {
    ({ reasons: options } = await Api.request(`${API_BASE}/api/rating-reasons?job_id=${encodeURIComponent(id)}`));
  } catch (error) { toast(error.message || "Couldn't load the questionnaire.", "error"); return; }
  const picked = new Set(job.rating === 2 ? job.rating_reasons || [] : []);
  $("#best-reasons-options").innerHTML = options.map(([key, label]) =>
    `<button type="button" class="best-reason-option${picked.has(key) ? " is-picked" : ""}" data-reason="${key}" aria-pressed="${picked.has(key)}">${escapeHtml(label)}</button>`).join("");
  const dialog = $("#best-reasons");
  dialog.dataset.jobId = id;
  const video = $("#best-reasons-video");
  video.src = job.output_url;
  setBestReasonsSound(false);
  dialog.hidden = false;
  video.play().catch(() => { /* autoplay blocked - the controls-free video just shows its first frame */ });
  $("#best-reasons-save").focus();
}

function closeBestReasons() {
  $("#best-reasons").hidden = true;
  const video = $("#best-reasons-video");
  video.pause();
  video.removeAttribute("src"); // stop streaming it in the background
  video.load();
}

// Starts muted so it can autoplay; tapping the video or the button toggles sound
// (handy when judging lip sync or audio).
function setBestReasonsSound(on) {
  $("#best-reasons-video").muted = !on;
  const button = $("#best-reasons-sound");
  button.setAttribute("aria-pressed", String(on));
  button.innerHTML = on ? `${uiIcon("volume")} Sound on` : `${uiIcon("volumeOff")} Tap for sound`;
}

function bindBestReasons() {
  $("#best-reasons-options").addEventListener("click", (event) => {
    const option = event.target.closest("[data-reason]");
    if (!option) return;
    const picked = option.classList.toggle("is-picked");
    option.setAttribute("aria-pressed", String(picked));
  });
  $("#best-reasons-save").addEventListener("click", () => {
    const reasons = $$("#best-reasons-options .is-picked").map((option) => option.dataset.reason);
    const id = $("#best-reasons").dataset.jobId;
    closeBestReasons();
    saveRating(id, 2, reasons);
  });
  $("#best-reasons-close").addEventListener("click", closeBestReasons);
  const toggleSound = () => setBestReasonsSound($("#best-reasons-video").muted);
  $("#best-reasons-video").addEventListener("click", toggleSound);
  $("#best-reasons-sound").addEventListener("click", toggleSound);
  $("#best-reasons-backdrop").addEventListener("click", closeBestReasons);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#best-reasons").hidden) closeBestReasons();
  });
  loadRatingReasons();
}

async function deleteAgenticSession(sessionId) {
  const jobs = state.jobs.filter((job) => job.agentic_session_id === sessionId);
  if (jobs.some((job) => ["waiting_for_gpu", "preparing", "queued", "rendering", "cancelling", "finalizing"].includes(job.state))) {
    return toast("Cancel this session's running renders before deleting it.", "error");
  }
  try {
    const result = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    state.jobs = state.jobs.filter((job) => job.agentic_session_id !== sessionId);
    if (activeAgenticSessionId === sessionId) {
      activeAgenticSessionId = null;
      activeAgenticSessionKind = "new";
      activeAgenticRunJobId = null;
      showAgenticResume();
    }
    renderJobs();
    toast(`Session deleted with ${result.renders_deleted} video${result.renders_deleted === 1 ? "" : "s"}.`);
  } catch (error) { toast(error.message, "error"); }
}

async function removeJob(id) {
  try {
    const result = await Api.request(`${API_BASE}/api/jobs/${id}`, { method: "DELETE" });
    state.jobs = state.jobs.filter((job) => job.id !== id);
    renderJobs();
    toast(result?.files_deleted ? "Deleted, along with its video file." : "Removed from history.");
  } catch (error) { toast(error.message, "error"); }
}

/* ------------------------------------------------------------ generate -- */

function collectSettings() {
  const isSeedhunter = state.model === "seedhunter";
  return {
    model: state.model,
    mode: state.mode,
    prompt: $("#prompt").value,
    reference_ids: isSeedhunter ? referencesByRole("sh-images").map((reference) => reference.id)
      : state.mode === "ref2v" ? referencesByRole("references").map((reference) => reference.id) : [],
    reference_video_ids: isSeedhunter ? referencesByRole("sh-videos").map((reference) => reference.id) : [],
    reference_audio_ids: isSeedhunter ? referencesByRole("sh-audios").map((reference) => reference.id) : [],
    first_frame_id: isSeedhunter ? state.shFirstFrameId : (state.mode === "fl2v" ? state.firstFrameId : null),
    last_frame_id: isSeedhunter ? state.shLastFrameId : (state.mode === "fl2v" ? state.lastFrameId : null),
    megapixels: Number($("#megapixels").value),
    width: Number($("#width").value),
    height: Number($("#height").value),
    aspect: $("#aspect").value,
    duration: Number($("#duration").value),
    steps: Number($(isSeedhunter ? "#sh-steps" : state.model === "wan_2_2" ? "#wan-steps" : "#steps").value),
    seed: Number($("#seed").value),
    sampler: $(isSeedhunter ? "#sh-sampler" : state.model === "wan_2_2" ? "#wan-sampler" : "#sampler").value,
    scheduler: $(isSeedhunter ? "#sh-scheduler" : state.model === "wan_2_2" ? "#wan-scheduler" : "#scheduler").value,
    parasyte_enabled: $("#parasyte-enabled").checked,
    parasyte_strength: Number($("#parasyte").value),
    sla_enabled: $("#sla-enabled").checked,
    sla_sparsity: Number($("#sla").value),
    ref2v_turbo_enabled: $("#ref2v-turbo-enabled").checked,
    ref2v_turbo: $("#ref2v-turbo").value,
    ref2v_strength: Number($("#ref-strength").value),
    attention: $("#attention-backend").value,
    ref_video_downscale: $("#ref-video-downscale").checked,
    experimental_ref_speedups: $("#experimental-ref-speedups").checked,
    sla_dense_step: $("#sla-dense-step").checked,
    spectrum_enabled: $("#spectrum-enabled").checked,
    block_cache_enabled: $("#block-cache-enabled").checked,
    ffn_chunking_enabled: $("#ffn-chunking-enabled").checked,
    low_vram_attention_enabled: $("#low-vram-attention-enabled").checked,
    low_vram_attention_chunks: Number($("#low-vram-attention-chunks").value),
    multi_gpu_offload: $("#multi-gpu-offload").value,
    multi_gpu_split_gb: Number($("#multi-gpu-split-gb").value),
    torch_compile_enabled: $("#torch-compile-enabled").checked,
    reference_sizing: $("#ref-size").value,
    ltx_tile_preset: $("#ltx-tiles").value,
    ltx_workflow: $("#ltx-workflow").value,
    ltx_fps: Number($("#ltx-fps").value),
    h3_fps: Number($("#h3-fps").value),
    cfg: Number($("#cfg").value) || 1,
    negative_prompt: $(state.model === "wan_2_2" ? "#wan-negative-prompt" : "#negative-prompt").value,
    sigma_shift_video: $("#shift-video").value === "" ? null : Number($("#shift-video").value),
    sigma_shift_audio: $("#shift-audio").value === "" ? null : Number($("#shift-audio").value),
    output_quality: $(state.model === "wan_2_2" ? "#wan-output-quality" : "#output-quality").value,
    candidate_count: Number($("#sh-candidate-count").value),
    continue_from_job_id: state.continueFromJob ? state.continueFromJob.id : null,
    overlap_frames: Number($("#sh-overlap-frames").value),
    enable_live_preview: $("#sh-preview-enable").checked,
    // MiniMax H3 has its own Latent upscale card; SeedHunter uses its finalize card's.
    enable_upscale: state.model === "minimax_h3" ? $("#h3-upscale-enable").checked : $("#sh-upscale-enable").checked,
    upscale_model: $("#sh-upscale-model").value,
    upscale_megapixels: Number($("#sh-upscale-megapixels").value),
    upscale_steps: Number($(state.model === "minimax_h3" ? "#h3-upscale-steps" : "#sh-upscale-steps").value),
    upscale_denoise: Number($(state.model === "minimax_h3" ? "#h3-upscale-denoise" : "#sh-upscale-denoise").value),
    upscale_seed: Number($(state.model === "minimax_h3" ? "#h3-upscale-seed" : "#sh-upscale-seed").value),
    upscale_factor: Number($("#h3-upscale-factor").value),
    upscale_chunking: $("#h3-upscale-chunking").checked,
    upscale_advanced: readUpscaleAdvanced(),
    enable_rife: $("#sh-rife-enable").checked,
    rife_target_fps: Number($("#sh-rife-fps").value),
    checkpoint: $("#minimax-checkpoint").value || null,
    loras: collectLoraSlots(),
    ltx_loras: collectListLoras("ltx_2_5"),
    wan_loras: collectListLoras("wan_2_2"),
    wan_turbo: $("#wan-turbo").value,
    wan_cfg: Number($("#wan-cfg").value) || 1,
    wan_shift: Number($("#wan-shift").value) || 8,
    wan_high_steps: $("#wan-high-steps").value === "" ? null : Number($("#wan-high-steps").value),
    wan_fps: Number($("#wan-fps").value),
  };
}

// Mirrors backend/generation.py's validate_reference_limits() so an
// over-limit ref2v submission is caught before a round trip, with the same
// per-limit explanation the server would otherwise return.
function referenceLimitError() {
  if (state.mode !== "ref2v") return null;
  if (state.model === "ltx_2_5") return ltxReferenceLimitError();
  if (state.model !== "minimax_h3") return null;
  const all = referencesByRole("references");
  const images = all.filter((reference) => referenceCategory(reference) === "image");
  const videos = all.filter((reference) => referenceCategory(reference) === "video");
  const audios = all.filter((reference) => referenceCategory(reference) === "audio");
  if (images.length > REFERENCE_LIMITS.image) return `MiniMax H3 allows at most ${REFERENCE_LIMITS.image} image references; ${images.length} are attached.`;
  if (videos.length > REFERENCE_LIMITS.video) return `MiniMax H3 allows at most ${REFERENCE_LIMITS.video} video references; ${videos.length} are attached.`;
  const voiceVideos = videos.filter((reference) => reference.audio_only);
  if (voiceVideos.some((reference) => reference.muted || !reference.has_audio)) return "Audio only requires an unmuted video with an audio track.";
  if (audios.length + voiceVideos.length > REFERENCE_LIMITS.audio) return `MiniMax H3 allows at most ${REFERENCE_LIMITS.audio} audio references; ${audios.length + voiceVideos.length} are attached.`;
  const videoSeconds = videos.filter((reference) => !reference.audio_only).reduce((sum, reference) => sum + (effectiveDuration(reference) || 0), 0);
  if (videoSeconds > REFERENCE_LIMITS.videoSeconds) return `Video references total ${videoSeconds.toFixed(1)}s (after trim), over the ${REFERENCE_LIMITS.videoSeconds.toFixed(1)}s combined limit.`;
  const audioSeconds = audios.reduce((sum, reference) => sum + (effectiveDuration(reference) || 0), 0) + voiceVideos.reduce((sum, reference) => sum + Math.min(5, effectiveDuration(reference) || 0), 0);
  if (audioSeconds > REFERENCE_LIMITS.audioSeconds) return `Audio references total ${audioSeconds.toFixed(1)}s (after trim), over the ${REFERENCE_LIMITS.audioSeconds.toFixed(1)}s combined limit.`;
  return null;
}

function ltxReferenceLimitError() {
  const all = referencesByRole("references");
  const images = all.filter((reference) => referenceCategory(reference) === "image");
  const audios = all.filter((reference) => referenceCategory(reference) === "audio");
  if (all.some((reference) => referenceCategory(reference) === "video")) return "LTX 2.5 Reference mode takes images and audio only - remove the video references.";
  if (!images.length) return "LTX 2.5 Reference mode needs at least one reference image.";
  if (images.length > LTX_MSR_LIMITS.image) return `LTX 2.5 Reference mode allows at most ${LTX_MSR_LIMITS.image} images (4 subjects + 1 background); ${images.length} are attached.`;
  if (audios.length > LTX_MSR_LIMITS.audio) return `LTX 2.5 Reference mode allows at most ${LTX_MSR_LIMITS.audio} voices (for Image 1 and Image 2); ${audios.length} are attached.`;
  if (audios.length > Math.min(images.length, 4)) return "Each voice belongs to the image in the same position (Audio 1 -> Image 1), so add an image for every audio clip.";
  const long = audios.findIndex((reference) => (effectiveDuration(reference) || 0) > LTX_MSR_LIMITS.audioSeconds + 1e-6);
  if (long >= 0) return `Audio ${long + 1} is ${effectiveDuration(audios[long]).toFixed(1)}s after trim; LTX 2.5 Reference mode uses at most ${LTX_MSR_LIMITS.audioSeconds}s per voice - trim it down.`;
  return null;
}

// One at a time: the button was only disabled after the resolution/upscale
// awaits, so a quick double tap (or Ctrl+Enter) could queue the job twice.
let generateInFlight = false;
async function generate() {
  if (generateInFlight) return;
  generateInFlight = true;
  try { await generateOnce(); } finally { generateInFlight = false; }
}

async function generateOnce() {
  const limitError = referenceLimitError();
  if (limitError) return toast(limitError, "error");
  // Wait for the latest megapixel/aspect calculation, then preview and queue
  // the same settings snapshot so an in-flight response cannot change size.
  await resolutionUpdatePromise;
  const body = collectSettings();
  if (body.prompt.includes("::VIDEO")) return toast("This is a multi-clip plan. Press Agentic to queue its clips.", "error");
  if (!await confirmH3Upscale(body)) return;
  const button = $("#generate-button");
  button.disabled = true;
  // Generate wakes an offline ComfyUI and waits for it (up to ~90 s server-side).
  button.textContent = state.serviceStatus?.comfyui?.online === false ? "Starting ComfyUI…" : "Queuing…";
  try {
    body.prompt = expandAliases(body.prompt); // LoRA trigger aliases -> their keywords
    const job = await Api.request(`${API_BASE}/api/generate`, { method: "POST", body, signal: timeoutSignal(180_000) });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    if (state.continueFromJob) clearContinuation();
    renderJobs();
    toast("Generation added to the ComfyUI queue.");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "Generate"; }
}

// The body both Enhance and the Enhancement tab's preview send.
function enhanceRequestBody() {
  const request = collectSettings();
  return {
    model: request.model, prompt: request.prompt, additional_prompt: $("#enhance-extra-prompt").value,
    mode: request.mode, reference_ids: request.reference_ids,
    first_frame_id: state.firstFrameId || request.first_frame_id,
    last_frame_id: state.lastFrameId || request.last_frame_id, duration: request.duration,
    available_reference_ids: referencesByRole("references").map((reference) => reference.id),
  };
}

let promptBeforeEnhance = null;

function flashRefinedPrompt() {
  const layer = $("#prompt-highlight");
  layer.classList.remove("prompt-refined-flash");
  void layer.offsetWidth;
  layer.classList.add("prompt-refined-flash");
  const editor = $("#prompt-suite");
  editor.classList.remove("prompt-refined-editor-flash");
  void editor.offsetWidth;
  editor.classList.add("prompt-refined-editor-flash");
}

function setPrompt(text) {
  const prompt = $("#prompt");
  prompt.value = text;
  prompt.dispatchEvent(new Event("input", { bubbles: true })); // highlight, snippets, drafts, session save
}

async function enhance() {
  const button = $("#enhance");
  const body = enhanceRequestBody();
  if (!body.prompt.trim()) return toast("Write a prompt first - Enhance refines what's there.", "error");
  button.disabled = true;
  button.textContent = "Waiting for GPU…";
  try {
    const { job_id } = await Api.request(`${API_BASE}/api/enhancement/enhance`, { method: "POST", body });
    enhanceLogJobId = job_id;
    enhanceLogRenderedCount = -1;
    let result;
    const started = Date.now();
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const job = await Api.request(`${API_BASE}/api/enhancement/jobs/${job_id}`, { cache: "no-store" });
      if (job.state === "failed") throw new Error(job.error || "Enhancement failed");
      if (job.state === "cancelled") throw new Error("Enhancement cancelled");
      if (job.state === "complete") { result = job.result; break; }
      const elapsed = Math.floor((Date.now() - started) / 1000);
      button.textContent = job.state === "running"
        ? `${job.phase || "Reading guides and loading model"}${job.progress?.content_chars ? ` · ${job.progress.content_chars.toLocaleString()} chars` : ""} · ${elapsed}s`
        : `Waiting for GPU… ${elapsed}s`;
    }
    promptBeforeEnhance = body.prompt;
    setPrompt(result.prompt);
    if (result.prompt.includes("::VIDEO")) toast("Hermes created a multi-clip plan. Press Agentic to queue its clips.");
    flashRefinedPrompt();
    $("#enhance-undo").hidden = false;
    toast(`Prompt refined by ${result.model} in ${result.seconds}s. Undo puts your version back.`);
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.innerHTML = `${uiIcon("sparkles")} Enhance`; }
}

// One hue per Agentic session: the feed group, its row in Agentic runs and the header chip share it.
function agenticSessionHue(sessionId) {
  const colors = [190, 268, 34, 155, 330, 90];
  let hash = 0;
  for (const char of sessionId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
}

async function stitchAgenticSession(sessionId, button) {
  button.disabled = true;
  button.textContent = "Stitching…";
  try {
    const job = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}/stitch`, { method: "POST" });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    renderJobs();
    toast("Stitching the session's final clips in order.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Stitch";
  }
}

function syncAgenticSessionButtons() {
  $$(".agentic-session-group").forEach((group) => {
    const active = group.dataset.agenticSessionId === activeAgenticSessionId;
    const button = $(".agentic-resume-button", group);
    if (!button) return;
    button.textContent = active ? "End Session" : "Resume";
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-label", active ? "End this Agentic session" : "Resume this Agentic session");
  });
}

function saveAgenticSelection() {
  try {
    if (activeAgenticSessionId) sessionStorage.setItem("h3-active-agentic-session", JSON.stringify({
      id: activeAgenticSessionId, kind: activeAgenticSessionKind, jobId: activeAgenticRunJobId,
    }));
    else sessionStorage.removeItem("h3-active-agentic-session");
  } catch { /* storage unavailable */ }
}

function showAgenticResume() {
  const active = Boolean(activeAgenticSessionId);
  const running = active && Boolean(activeAgenticRunJobId);
  const status = $("#agentic-session-status");
  status.textContent = active ? `${running ? "Running" : "Resuming"} ${activeAgenticSessionId.slice(-8)}` : "";
  status.hidden = !active;
  status.classList.toggle("is-active", active);
  if (active) status.style.setProperty("--session-hue", agenticSessionHue(activeAgenticSessionId));
  refreshAgenticQueue();
  saveAgenticSelection();
  syncAgenticSessionButtons();
  if (focusedAgenticSessionId() !== renderedAgenticFocus) {
    renderedAgenticFocus = focusedAgenticSessionId();
    renderJobs();
    $("#jobs-list").scrollTop = 0;
  }
}

const AGENTIC_DISMISSED_KEY = "h3-agentic-dismissed-runs";

function dismissedAgenticRuns() {
  try { return JSON.parse(localStorage.getItem(AGENTIC_DISMISSED_KEY) || "[]"); } catch { return []; }
}

function startNewAgenticSession() {
  // A run left behind with Start New keeps rendering; don't re-adopt it on the next page load.
  if (activeAgenticRunJobId) {
    try { localStorage.setItem(AGENTIC_DISMISSED_KEY, JSON.stringify([activeAgenticRunJobId, ...dismissedAgenticRuns()].slice(0, 20))); } catch { /* storage unavailable */ }
  }
  activeAgenticSessionId = null;
  activeAgenticSessionKind = "new";
  activeAgenticRunJobId = null;
  showAgenticResume();
  toast("New session ready. Any earlier Agentic run continues in the background.");
}

async function endAgenticSession() {
  const runningJob = activeAgenticRunJobId;
  if (runningJob) {
    try {
      await Api.request(`${API_BASE}/api/enhancement/jobs/${encodeURIComponent(runningJob)}/cancel`, {method: "POST"});
      toast("Ending the active Agentic run and its unfinished renders.");
    } catch (error) {
      toast(`Could not end the run: ${error.message}`, "error");
      return;
    }
  } else {
    toast("Session released. The next Agentic run will create a new session.");
  }
  activeAgenticSessionId = null;
  activeAgenticSessionKind = "new";
  activeAgenticRunJobId = null;
  showAgenticResume();
}

async function watchAgenticJob(jobId, sessionId) {
  if (agenticWatchers.has(jobId)) return;
  agenticWatchers.add(jobId);
  let known = 0;
  let errors = 0;
  let lastState = null;
  try {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      let job;
      try {
        job = await Api.request(`${API_BASE}/api/enhancement/jobs/${encodeURIComponent(jobId)}`, {cache: "no-store"});
        errors = 0;
      } catch (error) {
        if (++errors >= 8) throw error;
        continue;
      }
      if ((job.generation_ids || []).length > known) {
        known = job.generation_ids.length;
        try { applyJobs(await Api.request(`${API_BASE}/api/jobs`, {cache: "no-store"})); }
        catch { /* the next live update or poll will catch up */ }
      }
      if (activeAgenticRunJobId === jobId) {
        if (job.state !== lastState) {
          showAgenticResume();
          if (job.state === "awaiting_approval") {
            $("#agentic-session-status").textContent = `Approval needed ${sessionId.slice(-8)}`;
          }
        }
        $("#agentic-session-status").title = job.phase || job.state;
      }
      lastState = job.state;
      if (job.state === "failed") throw new Error(job.error || "Agentic session failed");
      if (job.state === "cancelled") { toast("Agentic session ended."); break; }
      if (job.state === "complete") { toast("Agentic session finished."); break; }
    }
  } catch (error) {
    toast(`Agentic session ${sessionId.slice(-8)}: ${error.message}`, "error");
  } finally {
    agenticWatchers.delete(jobId);
    if (activeAgenticRunJobId === jobId) {
      activeAgenticRunJobId = null;
      if (activeAgenticSessionKind === "new") activeAgenticSessionId = null;
      showAgenticResume();
    }
  }
}

async function resumeAgenticSession(sessionId) {
  try {
    const session = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(sessionId)}`, {cache: "no-store"});
    setView("studio");
    if (isMobileLayout()) setMobileTab("create");
    if (session.generation?.model) await applyRetryRequest(session.generation);
    setPrompt(session.guide_as_prompt ? "" : session.master_prompt || session.generation?.prompt || "");
    const guide = $("#enhance-extra-prompt");
    guide.value = session.guide || "";
    guide.dispatchEvent(new Event("input", {bubbles: true}));
    activeAgenticSessionId = session.id;
    activeAgenticSessionKind = "resumed";
    agenticFocusDismissed = false;
    const lastRun = session.runs?.at(-1);
    activeAgenticRunJobId = AGENTIC_ACTIVE_STATES.has(session.state) ? lastRun?.job_id || null : null;
    showAgenticResume();
    if (activeAgenticRunJobId) watchAgenticJob(activeAgenticRunJobId, session.id);
    $("#enhance-composer").open = true;
    $("#enhance-composer").scrollIntoView({behavior: "smooth", block: "start"});
    toast(activeAgenticRunJobId ? "Watching this running Agentic session." : "Agentic session restored. Edit the prompt or guide, then press Agentic to continue.");
  } catch (error) { toast(error.message, "error"); }
}

showAgenticResume();

async function agentic() {
  const button = $("#agentic");
  // A run is already going: queue this one as a new session; the GPU queue runs them in turn.
  const queuedBehind = Boolean(activeAgenticRunJobId);
  if (queuedBehind) { activeAgenticSessionId = null; activeAgenticSessionKind = "new"; activeAgenticRunJobId = null; }
  const generation = collectSettings();
  generation.first_frame_id ||= state.firstFrameId;
  generation.last_frame_id ||= state.lastFrameId;
  const guide = $("#enhance-extra-prompt").value;
  if (!generation.prompt.trim() && !guide.trim()) return toast("Write a prompt or an Agentic guide first.", "error");
  const guideAsPrompt = !generation.prompt.trim();
  if (guideAsPrompt) generation.prompt = guide.trim();
  button.disabled = true;
  button.textContent = "Queuing…";
  try {
    generation.prompt = expandAliases(generation.prompt);
    const wasResuming = !queuedBehind && Boolean(activeAgenticSessionId);
    const body = { generation, guide, guide_as_prompt: guideAsPrompt,
      available_reference_ids: referencesByRole("references").map((reference) => reference.id),
      resume_session_id: activeAgenticSessionId };
    const {job_id, session_id} = await Api.request(`${API_BASE}/api/enhancement/agentic`, {method: "POST", body});
    activeAgenticSessionId = session_id;
    activeAgenticSessionKind = wasResuming ? "resumed" : "new";
    activeAgenticRunJobId = job_id;
    showAgenticResume();
    enhanceLogJobId = job_id;
    enhanceLogRenderedCount = -1;
    watchAgenticJob(job_id, session_id);
    toast(wasResuming ? "Resumed Agentic session." : queuedBehind ? "Agentic run queued behind the current one." : "Agentic run queued.");
    refreshAgenticQueue();
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.innerHTML = `${uiIcon("sparkles")} Queue Agentic run`; }
}

// Queued Agentic runs (2026-10-18): every unfinished Agentic run, oldest first, in the Agentic card.
const AGENTIC_QUEUE_LABELS = {waiting_for_gpu: "Waiting for GPU", running: "Planning", queuing: "Queuing clips", rendering: "Rendering",
  reviewing: "Reviewing", awaiting_approval: "Needs approval"};
var agenticQueueBusy = false; // var: showAgenticResume() runs (and refreshes the queue) before this line on page load

async function refreshAgenticQueue() {
  if (agenticQueueBusy) return;
  agenticQueueBusy = true;
  try {
    const {jobs} = await Api.request(`${API_BASE}/api/enhancement/jobs`, {cache: "no-store"});
    const runs = (jobs || []).filter((job) => job.agentic && AGENTIC_ACTIVE_STATES.has(job.state))
      .sort((a, b) => a.created_at - b.created_at);
    const badge = $("#agentic-queue-badge");
    badge.hidden = runs.length < 2;
    badge.textContent = `${runs.length} queued`;
    const resumed = activeAgenticSessionId && !activeAgenticRunJobId ? activeAgenticSessionId : null;
    $("#agentic-queue").hidden = !runs.length && !resumed;
    $("#agentic-queue-count").textContent = runs.length === 1 ? "1 run" : `${runs.length} runs, run in order`;
    $("#agentic-queue-list").replaceChildren(...runs.map((job, index) => {
      const item = document.createElement("li");
      item.className = "agentic-queue-item";
      item.classList.toggle("is-current", job.id === activeAgenticRunJobId);
      item.dataset.state = job.state;
      item.style.setProperty("--session-hue", agenticSessionHue(job.session_id));
      const minutes = Math.max(0, Math.floor((Date.now() / 1000 - job.created_at) / 60));
      item.innerHTML = `<span class="agentic-queue-index">${index + 1}</span>`
        + `<span class="agentic-queue-main"><span class="agentic-queue-prompt"></span><small><span class="agentic-queue-state">${escapeHtml(AGENTIC_QUEUE_LABELS[job.state] || job.state.replaceAll("_", " "))}</span> · ${minutes ? `${minutes} min ago` : "just now"} · ${escapeHtml(job.session_id.slice(-8))}</small></span>`
        + `<span class="agentic-queue-buttons"><button type="button" class="secondary-button" data-queue-action="watch">${job.id === activeAgenticRunJobId ? "Watching" : "Watch"}</button><button type="button" class="secondary-button danger" data-queue-action="stop">Stop</button></span>`;
      $(".agentic-queue-prompt", item).textContent = job.prompt_preview || "(no prompt)";
      $('[data-queue-action="watch"]', item).disabled = job.id === activeAgenticRunJobId;
      item.dataset.jobId = job.id;
      item.dataset.sessionId = job.session_id;
      return item;
    }));
    if (resumed) {
      const item = document.createElement("li");
      item.className = "agentic-queue-item is-current";
      item.style.setProperty("--session-hue", agenticSessionHue(resumed));
      item.innerHTML = `<span class="agentic-queue-index">↻</span><span class="agentic-queue-main"><span class="agentic-queue-prompt">Continuing an earlier session</span><small>The next run adds to session ${escapeHtml(resumed.slice(-8))}</small></span><span class="agentic-queue-buttons"><button type="button" class="secondary-button danger" data-queue-action="release">End Session</button></span>`;
      $("#agentic-queue-list").append(item);
    }
  } catch { /* offline; the next tick tries again */ }
  finally { agenticQueueBusy = false; }
}

$("#agentic-queue-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-queue-action]");
  if (!button) return;
  const item = button.closest(".agentic-queue-item");
  if (button.dataset.queueAction === "release") {
    endAgenticSession();
  } else if (button.dataset.queueAction === "watch") {
    await adoptAgenticSession({id: item.dataset.sessionId, kind: "resumed"});
    agenticFocusDismissed = false;
    showAgenticResume();
  } else {
    try {
      await Api.request(`${API_BASE}/api/enhancement/jobs/${encodeURIComponent(item.dataset.jobId)}/cancel`, {method: "POST"});
      toast("Stopping that Agentic run.");
    } catch (error) { toast(error.message, "error"); }
  }
  refreshAgenticQueue();
});
setInterval(() => { if (!document.hidden && (document.body.dataset.view || "studio") === "studio") refreshAgenticQueue(); }, 4000);
refreshAgenticQueue();

async function restoreAgenticSelection() {
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem("h3-active-agentic-session") || "null"); } catch { saved = null; }
  if (saved?.id && await adoptAgenticSession(saved)) return;
  // New tab, relaunched PWA or a discarded phone tab has no sessionStorage: find the running run on the server.
  try {
    const {jobs} = await Api.request(`${API_BASE}/api/enhancement/jobs`, {cache: "no-store"});
    const dismissed = new Set(dismissedAgenticRuns());
    const running = (jobs || []).find((job) => job.agentic && job.session_id && AGENTIC_ACTIVE_STATES.has(job.state) && !dismissed.has(job.id));
    if (running) await adoptAgenticSession({id: running.session_id, kind: "new"});
  } catch { /* offline; the next load tries again */ }
}

async function adoptAgenticSession(saved) {
  try {
    const session = await Api.request(`${API_BASE}/api/agentic/sessions/${encodeURIComponent(saved.id)}`);
    activeAgenticSessionId = session.id;
    activeAgenticSessionKind = saved.kind === "new" ? "new" : "resumed";
    const lastRun = session.runs?.at(-1);
    activeAgenticRunJobId = AGENTIC_ACTIVE_STATES.has(session.state) ? lastRun?.job_id || null : null;
    if (activeAgenticRunJobId) { enhanceLogJobId = activeAgenticRunJobId; enhanceLogRenderedCount = -1; }
    if (!activeAgenticRunJobId && activeAgenticSessionKind === "new") activeAgenticSessionId = null;
    showAgenticResume();
    if (activeAgenticRunJobId) watchAgenticJob(activeAgenticRunJobId, session.id);
    return Boolean(activeAgenticSessionId);
  } catch { return false; /* session removed or unavailable */ }
}

// The Hermes guide gets the same highlight layer + suggestion menu, for the finishing triggers only.
const guideMenu = { matches: [], index: 0, replace: 0 };

function highlightGuide(text) {
  let html = "";
  let last = 0;
  for (const match of text.matchAll(FINISH_TRIGGER_RE)) {
    html += escapeHtml(text.slice(last, match.index)) + `<span class="hl-finish">${escapeHtml(match[0])}</span>`;
    last = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(last)) + "\u200b";
}

function closeGuideMenu() {
  $("#guide-menu").hidden = true;
  guideMenu.matches = [];
}

function renderGuideMenu() {
  const menu = $("#guide-menu");
  menu.innerHTML = guideMenu.matches.map((entry, index) => `<button type="button" class="alias-option hl-finish${index === guideMenu.index ? " is-active" : ""}" role="option" data-category="finish" data-guide-index="${index}"><span class="alias-option-token">${escapeHtml(entry.token)}</span><span class="alias-option-label">${escapeHtml(entry.label)}</span></button>`).join("");
  menu.hidden = false;
}

function insertGuideSuggestion(entry) {
  const textarea = $("#enhance-extra-prompt");
  const end = textarea.selectionStart;
  const start = end - guideMenu.replace;
  const text = entry.insert.replace("§", "");
  textarea.focus();
  textarea.setSelectionRange(start, end);
  if (!document.execCommand("insertText", false, text)) {
    textarea.setRangeText(text, start, end, "end");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }
  closeGuideMenu();
}

function bindGuideHighlight() {
  const textarea = $("#enhance-extra-prompt");
  const layer = $("#guide-highlight");
  const sync = () => {
    const width = textarea.clientWidth, height = textarea.clientHeight;
    layer.innerHTML = highlightGuide(textarea.value);
    layer.style.width = `${width}px`;
    layer.style.height = `${height}px`;
    layer.scrollTop = textarea.scrollTop;
  };
  const update = () => {
    if (textarea.selectionStart !== textarea.selectionEnd) return closeGuideMenu();
    const match = textarea.value.slice(0, textarea.selectionStart).match(FINISH_FRAGMENT);
    const matches = match ? finishSuggestions(match[1]) : [];
    if (!matches.length) return closeGuideMenu();
    const previous = guideMenu.matches[guideMenu.index]?.token;
    guideMenu.matches = matches;
    guideMenu.replace = match[1].length;
    guideMenu.index = Math.max(0, matches.findIndex((entry) => entry.token === previous));
    renderGuideMenu();
  };
  // Resume, restore and Enhance set .value, which fires no "input".
  const valueProperty = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
  Object.defineProperty(textarea, "value", {
    get() { return valueProperty.get.call(this); },
    set(value) { valueProperty.set.call(this, value); sync(); },
  });
  textarea.addEventListener("input", () => { sync(); update(); });
  textarea.addEventListener("click", update);
  textarea.addEventListener("scroll", () => { layer.scrollTop = textarea.scrollTop; });
  textarea.addEventListener("blur", () => setTimeout(closeGuideMenu, 120));
  textarea.addEventListener("keydown", (event) => {
    if (!guideMenu.matches.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      guideMenu.index = (guideMenu.index + (event.key === "ArrowDown" ? 1 : -1) + guideMenu.matches.length) % guideMenu.matches.length;
      renderGuideMenu();
    } else if (event.key === "Tab" || event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      insertGuideSuggestion(guideMenu.matches[guideMenu.index]);
    } else if (event.key === "Escape") {
      event.stopPropagation();
      closeGuideMenu();
    }
  });
  $("#guide-menu").addEventListener("mousedown", (event) => {
    const option = event.target.closest("[data-guide-index]");
    if (!option) return;
    event.preventDefault();
    insertGuideSuggestion(guideMenu.matches[Number(option.dataset.guideIndex)]);
  });
  new ResizeObserver(sync).observe(textarea);
  sync();
}
bindGuideHighlight();

function undoEnhance() {
  if (promptBeforeEnhance === null) return;
  setPrompt(promptBeforeEnhance);
  promptBeforeEnhance = null;
  $("#enhance-undo").hidden = true;
}

/* ----------------------------------------------------------- post-edit -- */
// The Post-Edit tab: re-render a finished video (backend/postedit.py) with
// timing, frame, look, voice-over (backend/tts.py), music, captions and text.
// Everything is positioned on the OUTPUT timeline: the trimmed source played
// at `speed`, plus `hold_end` seconds of frozen last frame. The preview runs a
// clock over that timeline (the video covers the trimmed part, a hold phase
// the rest) and plays the music/voice clips in sync. Color/speed/crop/flip are
// approximated with CSS; gamma, temperature, sharpen, denoise, grain, reverse,
// stabilize and interpolation only show up in the render.

const PE_DRAFT_KEY = "h3-postedit-draft";
const PE_LOOK_SLIDERS = [
  // [field, label, min, max, step, default, format]
  ["brightness", "Brightness", -1, 1, 0.01, 0, (v) => (v > 0 ? "+" : "") + v.toFixed(2)],
  ["contrast", "Contrast", 0, 3, 0.01, 1, (v) => v.toFixed(2)],
  ["saturation", "Saturation", 0, 3, 0.01, 1, (v) => v.toFixed(2)],
  ["gamma", "Gamma", 0.1, 3, 0.01, 1, (v) => v.toFixed(2)],
  ["hue", "Hue shift", -180, 180, 1, 0, (v) => `${v}°`],
  ["temperature", "Temperature", 1000, 12000, 100, 6500, (v) => `${v}K`],
  ["sharpen", "Sharpen / blur", -2, 3, 0.05, 0, (v) => v.toFixed(2)],
  ["denoise", "Denoise", 0, 10, 0.1, 0, (v) => v.toFixed(1)],
  ["grain", "Film grain", 0, 60, 1, 0, (v) => String(v)],
  ["vignette", "Vignette", 0, 1, 0.01, 0, (v) => `${Math.round(v * 100)}%`],
];
const PE_LOOKS = {
  none: {},
  cinematic: { contrast: 1.12, saturation: 0.9, temperature: 5800, vignette: 0.35, grain: 6, gamma: 0.95 },
  warm: { temperature: 4800, saturation: 1.1, brightness: 0.02 },
  cool: { temperature: 8200, saturation: 0.95 },
  vivid: { saturation: 1.45, contrast: 1.1, sharpen: 0.5 },
  faded: { contrast: 0.8, saturation: 0.75, brightness: 0.06, gamma: 1.1 },
  noir: { saturation: 0, contrast: 1.3, vignette: 0.5, grain: 10 },
  vhs: { saturation: 1.25, contrast: 0.9, sharpen: -0.8, grain: 22, temperature: 7200, rgb_split: 3 },
  teal: { contrast: 1.1, saturation: 1.05, teal_orange: 0.75, vignette: 0.2 },
  dreamy: { brightness: 0.03, contrast: 0.92, saturation: 0.95, glow: 0.55, temperature: 6000 },
  retro: { tone_curve: "vintage", saturation: 0.85, grain: 14, vignette: 0.3 },
  bleach: { saturation: 0.45, contrast: 1.35, gamma: 0.95, sharpen: 0.4 },
  glitch: { saturation: 1.3, contrast: 1.15, rgb_split: 8, grain: 12 },
  matte: { tone_curve: "lighter", contrast: 0.85, saturation: 0.9, gamma: 1.05 },
};
// Effects a look preset may set; picking a preset puts the ones it doesn't name back to their defaults.
// (Bars, pixelate and slow zoom are framing choices: presets leave them alone.)
const PE_LOOK_FX = { tone_curve: "none", teal_orange: 0, glow: 0, rgb_split: 0 };
const PE_EFFECTS = { ...PE_LOOK_FX, pixelate: 0, letterbox: "off", slow_zoom: 0 };
const PE_AUDIO_FX = {
  original_eq: "off", original_pitch: 0, original_echo: "off", voice_fx: "off", voice_deess: false,
  master_bass: 0, master_treble: 0, master_compress: "off", stereo_width: 1, music_fade_in: 0,
};
const PE_TAB_KEY = "h3-postedit-tab";
const PE_MUSIC_ROLES = new Set(["postedit-music", "stitch-music"]);

function peDefaults() {
  const look = Object.fromEntries(PE_LOOK_SLIDERS.map(([field, , , , , value]) => [field, value]));
  return {
    trim_start: 0, trim_end: null, speed: 1, reverse: false, hold_end: 0, fade_in: 0, fade_out: 0, fade_color: "black",
    aspect: "none", zoom: 1, pan_x: 0.5, pan_y: 0.5, flip_h: false, flip_v: false, rotate: 0,
    width: null, height: null, fps: null, interpolate: false, stabilize: false, ...look, ...PE_EFFECTS, ...PE_AUDIO_FX,
    original_volume: 1, mute_original: false, audio_denoise: false, remove_music: "off", normalize: false,
    next_job_id: "", next_reference_id: "", next_transition: "fade", next_transition_duration: 1, next_fade_out: 0, next_match_loudness: true,
    music_reference_id: null, music_volume: 0.3, music_start: 0, music_in: 0, music_out: null, music_loop: true, music_fade_out: 1.5,
    voices: [], duck: true, duck_strength: 0.6,
    captions: false, caption_source: "tts", caption_size: 22, caption_position: "bottom", caption_color: "#ffffff",
    caption_style: "classic", pop_words: 2, pop_highlight: "#ffe14d", pop_uppercase: true, pop_tilt: true,
    texts: [], quality: "high",
  };
}

const pe = {
  bound: false, sourceId: null, source: null, sourceKey: "", edit: peDefaults(),
  tts: { provider: "edge", edgeVoices: null, locale: "en-US", voice: { edge: "en-US-ChristopherNeural", tiktok: "en_male_narration" }, settings: null },
  captions: {}, words: {}, transcripts: {}, transcribing: {}, audio: {}, selected: null,
  clock: { playing: false, t: 0, holdFrom: null, raf: 0 },
  saveTimer: null,
};

const peNum = (value, fallback = 0) => (Number.isFinite(Number(value)) && value !== "" && value !== null ? Number(value) : fallback);
const peClamp = (value, low, high) => Math.min(high, Math.max(low, value));
function peTime(seconds) {
  const s = Math.max(0, seconds || 0);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

/* ---- derived timeline values ---- */
function peTrim() {
  const duration = pe.source?.duration || 0;
  const start = peClamp(peNum(pe.edit.trim_start), 0, Math.max(0, duration - 0.1));
  const end = peClamp(pe.edit.trim_end == null ? duration : peNum(pe.edit.trim_end, duration), start + 0.1, duration || start + 0.1);
  return { start, end };
}
function peSegment() { const { start, end } = peTrim(); return (end - start) / pe.edit.speed; }
function peLength() { return peSegment() + peNum(pe.edit.hold_end); }
function peReference(id) { return state.references.find((reference) => reference.id === id); }

function peCropRect() {
  const edit = pe.edit, source = pe.source;
  if (!source || edit.aspect === "none") return null;
  let w = 1, h = 1;
  if (edit.aspect !== "free") {
    const [a, b] = edit.aspect.split(":").map(Number);
    const target = a / b, native = source.width / source.height;
    if (target > native) h = native / target; else w = target / native;
  }
  w /= edit.zoom; h /= edit.zoom;
  return { x: (1 - w) * edit.pan_x, y: (1 - h) * edit.pan_y, w, h };
}

// The rendered frame's pixel height (text/caption sizes are in output pixels).
function peOutputHeight() {
  if (pe.edit.width && pe.edit.height) return pe.edit.height;
  const crop = peCropRect() || { w: 1, h: 1 };
  const rotated = pe.edit.rotate === 90 || pe.edit.rotate === 270;
  return rotated ? crop.w * pe.source.width : crop.h * pe.source.height;
}

/* ---- sources ---- */
function peSourceJobs() {
  return state.jobs.filter((job) => job.state === "complete" && job.output_url);
}

function peRenderSources() {
  const jobs = peSourceJobs();
  const key = jobs.map((job) => job.id).join(",");
  if (key === pe.sourceKey) return;
  pe.sourceKey = key;
  const select = $("#pe-source");
  select.innerHTML = `<option value="">Choose a video…</option>` + jobs.map((job) => {
    const when = new Date(job.completed_at || job.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const text = `${job.model_label} · ${job.duration ? `${job.duration}s` : "?"} · ${when} · ${(job.prompt || "").replace(/\s+/g, " ").slice(0, 70)}`;
    return `<option value="${job.id}">${escapeHtml(text)}</option>`;
  }).join("");
  select.value = pe.sourceId && jobs.some((job) => job.id === pe.sourceId) ? pe.sourceId : "";
  peRenderNextVideos(true);
}

// "Fade into another video": uploaded videos (role postedit-video) and the
// same finished generations as the source picker. Values are "ref:<id>" /
// "job:<id>" and map to next_reference_id / next_job_id.
function peRenderNextVideos(force = false) {
  const uploads = state.references.filter((reference) => reference.role === "postedit-video");
  const jobs = peSourceJobs();
  const key = uploads.map((reference) => reference.id).join(",") + "|" + jobs.map((job) => job.id).join(",");
  const next = $("#pe-next-video");
  if (force || key !== pe.nextKey) {
    pe.nextKey = key;
    const jobOptions = [...$("#pe-source").options].filter((option) => option.value).map((option) => `<option value="job:${option.value}">${escapeHtml(option.textContent)}</option>`).join("");
    next.innerHTML = `<option value="">None</option>`
      + (uploads.length ? `<optgroup label="Uploaded videos">${uploads.slice().reverse().map((reference) => `<option value="ref:${reference.id}">${escapeHtml(reference.label || reference.filename)}${reference.duration ? ` · ${reference.duration.toFixed(1)}s` : ""}</option>`).join("")}</optgroup>` : "")
      + (jobOptions ? `<optgroup label="Generations">${jobOptions}</optgroup>` : "");
  }
  const edit = pe.edit || {};
  const value = edit.next_reference_id && uploads.some((reference) => reference.id === edit.next_reference_id) ? `ref:${edit.next_reference_id}`
    : edit.next_job_id && jobs.some((job) => job.id === edit.next_job_id) ? `job:${edit.next_job_id}` : "";
  next.value = value;
  $("#pe-next-delete").hidden = !value.startsWith("ref:");
}

// Length of whichever next video is chosen (a generation's job, or an upload's probe).
function peNextVideo() {
  if (pe.edit.next_reference_id) return state.references.find((reference) => reference.id === pe.edit.next_reference_id) || null;
  if (pe.edit.next_job_id) return state.jobs.find((job) => job.id === pe.edit.next_job_id) || null;
  return null;
}

async function peSelectSource(jobId, { keepTrim = false } = {}) {
  peStop();
  if (!jobId) {
    pe.selectToken = (pe.selectToken || 0) + 1;
    pe.sourceId = null; pe.source = null;
    $("#pe-reedit").hidden = true;
    $("#pe-layout").hidden = true; $("#pe-empty").hidden = false; $("#pe-render").disabled = true;
    $("#pe-video").removeAttribute("src");
    peSaveDraft();
    return;
  }
  const token = pe.selectToken = (pe.selectToken || 0) + 1;
  try {
    const info = await Api.request(`${API_BASE}/api/postedit/source/${jobId}`);
    if (token !== pe.selectToken) return; // a newer pick won
    const changed = pe.sourceId !== jobId;
    pe.sourceId = jobId;
    pe.source = info;
    if (changed && !keepTrim) { pe.edit.trim_start = 0; pe.edit.trim_end = null; }
    $("#pe-source").value = jobId;
    $("#pe-reedit").hidden = info.job.model !== "postedit";
    $("#pe-layout").hidden = false; $("#pe-empty").hidden = true; $("#pe-render").disabled = false;
    const video = $("#pe-video");
    video.src = info.job.output_url;
    video.load();
    peSyncForm();
    peRefresh();
    peSaveDraft();
    if (pe.edit.captions && pe.edit.caption_source === "whisper" && !pe.edit.reverse) peEnsureWhisper().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
  }
}

function openPostEdit(jobId) {
  pe.skipDraftSource = true; // the draft's own source must not win the race
  setView("postedit");
  peRenderSources();
  peSelectSource(jobId);
}

// Re-open a Post-Edit render on its ORIGINAL source with the settings it was
// rendered with (crop aspect/zoom/pan come back from request.editor).
async function peReeditJob(jobId) {
  let saved;
  try {
    saved = await Api.request(`${API_BASE}/api/postedit/settings/${jobId}`);
  } catch (error) { return toast(error.message, "error"); }
  if (!saved.source_available) {
    toast("The original video of this edit was removed, so this opens the rendered edit itself with fresh settings.", "error");
    return openPostEdit(jobId);
  }
  const { source_job_id: _source, crop, editor, ...rest } = saved.request;
  pe.skipDraftSource = true;
  setView("postedit");
  pe.edit = { ...peDefaults(), ...rest, ...(editor || {}) };
  pe.selected = null;
  peRenderSources();
  peSyncForm();
  peRenderLists();
  await peSelectSource(saved.source_job_id, { keepTrim: true });
  const missing = (rest.voices || []).length - pe.edit.voices.length;
  toast(`Loaded the saved edit on the original video${crop && !editor ? " (its crop couldn't be restored)" : ""}${missing > 0 ? `; ${missing} voice clip(s) were deleted since` : ""}.`);
}

/* ---- drafts (per-browser convenience) ---- */
function peSaveDraft() {
  clearTimeout(pe.saveTimer);
  pe.saveTimer = setTimeout(() => {
    try { localStorage.setItem(PE_DRAFT_KEY, JSON.stringify({ sourceId: pe.sourceId, edit: pe.edit, tts: { provider: pe.tts.provider, locale: pe.tts.locale, voice: pe.tts.voice } })); } catch { /* storage may be off */ }
  }, 300);
}

function peLoadDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(PE_DRAFT_KEY) || "null");
    if (!draft) return null;
    pe.edit = { ...peDefaults(), ...(draft.edit || {}) };
    if (draft.tts) Object.assign(pe.tts, { provider: draft.tts.provider || "edge", locale: draft.tts.locale || "en-US", voice: { ...pe.tts.voice, ...(draft.tts.voice || {}) } });
    return draft.sourceId || null;
  } catch { return null; }
}

/* ---- form <-> state ---- */
const PE_FIELDS = [
  // [element id, field, kind]
  ["pe-trim-start", "trim_start", "number"], ["pe-trim-end", "trim_end", "number-null"], ["pe-speed", "speed", "number"],
  ["pe-reverse", "reverse", "check"], ["pe-hold-end", "hold_end", "number"], ["pe-fade-in", "fade_in", "number"],
  ["pe-next-transition", "next_transition", "text"],
  ["pe-next-transition-duration", "next_transition_duration", "number"], ["pe-next-fade-out", "next_fade_out", "number"],
  ["pe-next-match-loudness", "next_match_loudness", "check"],
  ["pe-fade-out", "fade_out", "number"], ["pe-fade-color", "fade_color", "text"],
  ["pe-zoom", "zoom", "number"], ["pe-pan-x", "pan_x", "number"], ["pe-pan-y", "pan_y", "number"],
  ["pe-flip-h", "flip_h", "check"], ["pe-flip-v", "flip_v", "check"], ["pe-rotate", "rotate", "int"],
  ["pe-width", "width", "int-null"], ["pe-height", "height", "int-null"], ["pe-fps", "fps", "int-null"],
  ["pe-interpolate", "interpolate", "check"], ["pe-stabilize", "stabilize", "check"],
  ["pe-orig-volume", "original_volume", "number"], ["pe-mute", "mute_original", "check"],
  ["pe-audio-denoise", "audio_denoise", "check"], ["pe-normalize", "normalize", "check"], ["pe-remove-music", "remove_music", "text"],
  ["pe-music-volume", "music_volume", "number"], ["pe-music-start", "music_start", "number"],
  ["pe-music-loop", "music_loop", "check"], ["pe-music-fade", "music_fade_out", "number"],
  ["pe-music-in", "music_in", "number"], ["pe-music-out", "music_out", "number-null"],
  ["pe-duck", "duck", "check"], ["pe-duck-strength", "duck_strength", "number"],
  ["pe-captions", "captions", "check"], ["pe-caption-source", "caption_source", "text"], ["pe-caption-size", "caption_size", "int"],
  ["pe-pop-words", "pop_words", "int"], ["pe-pop-highlight", "pop_highlight", "text"],
  ["pe-pop-uppercase", "pop_uppercase", "check"], ["pe-pop-tilt", "pop_tilt", "check"],
  ["pe-caption-position", "caption_position", "text"], ["pe-caption-color", "caption_color", "text"],
  ["pe-quality", "quality", "text"],
  ["pe-tone-curve", "tone_curve", "text"], ["pe-teal-orange", "teal_orange", "number"], ["pe-glow", "glow", "number"],
  ["pe-rgb-split", "rgb_split", "int"], ["pe-pixelate", "pixelate", "int"], ["pe-slow-zoom", "slow_zoom", "number"],
  ["pe-orig-eq", "original_eq", "text"], ["pe-orig-echo", "original_echo", "text"], ["pe-orig-pitch", "original_pitch", "number"],
  ["pe-voice-fx", "voice_fx", "text"], ["pe-voice-deess", "voice_deess", "check"], ["pe-music-fade-in", "music_fade_in", "number"],
  ["pe-master-bass", "master_bass", "number"], ["pe-master-treble", "master_treble", "number"],
  ["pe-stereo-width", "stereo_width", "number"], ["pe-master-compress", "master_compress", "text"],
];

// Which tab each part of the edit lives on (the dot on a tab = something there differs from the defaults).
const PE_TAB_FIELDS = {
  edit: ["trim_start", "trim_end", "speed", "reverse", "hold_end", "fade_in", "fade_out", "next_job_id", "next_reference_id",
    "aspect", "flip_h", "flip_v", "rotate", "width", "height", "fps", "stabilize"],
  effects: [...PE_LOOK_SLIDERS.map(([field]) => field), ...Object.keys(PE_EFFECTS)],
  audio: ["original_volume", "mute_original", "audio_denoise", "remove_music", "normalize", "music_reference_id", "voices", ...Object.keys(PE_AUDIO_FX)],
  text: ["captions", "texts"],
};
let peBaseCache = null;
function peDiffers(field) {
  const value = pe.edit[field], base = (peBaseCache ||= peDefaults())[field];
  if (Array.isArray(value)) return value.length > 0;
  return (value ?? null) !== (base ?? null) && !(value === "" && base == null);
}
function peRenderTabs() {
  $$("#pe-tabs [data-pe-tab]").forEach((tab) => {
    const fields = PE_TAB_FIELDS[tab.dataset.peTab];
    tab.classList.toggle("is-changed", Boolean(fields && fields.some(peDiffers)));
  });
  const count = (fields) => fields.filter(peDiffers).length || "";
  $("#pe-look-badge").textContent = count(PE_LOOK_SLIDERS.map(([field]) => field));
  $("#pe-fx-badge").textContent = count(Object.keys(PE_EFFECTS));
  $("#pe-master-badge").textContent = count(["master_bass", "master_treble", "master_compress", "stereo_width"]);
}
function peShowTab(name) {
  if (!$(`#pe-tabs [data-pe-tab="${name}"]`)) name = "edit";
  $$("#pe-tabs [data-pe-tab]").forEach((tab) => {
    const on = tab.dataset.peTab === name;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-selected", String(on));
  });
  $$("#pe-controls [data-pe-group]").forEach((card) => { card.hidden = card.dataset.peGroup !== name; });
  try { localStorage.setItem(PE_TAB_KEY, name); } catch { /* storage may be off */ }
}

function peReadField(element, kind) {
  if (kind === "check") return element.checked;
  if (kind === "text") return element.value;
  const raw = element.value.trim();
  if (kind.endsWith("-null") && raw === "") return null;
  const value = kind.startsWith("int") ? parseInt(raw, 10) : parseFloat(raw);
  return Number.isFinite(value) ? value : (kind.endsWith("-null") ? null : 0);
}

function peSyncForm() {
  for (const [id, field, kind] of PE_FIELDS) {
    const element = $(`#${id}`);
    const value = pe.edit[field];
    if (kind === "check") element.checked = Boolean(value);
    else element.value = value == null ? "" : String(value);
  }
  PE_LOOK_SLIDERS.forEach(([field]) => { $(`#pe-look-${field}`).value = pe.edit[field]; });
  $$("#pe-aspect .segment").forEach((button) => button.classList.toggle("is-active", button.dataset.aspect === pe.edit.aspect));
  $$("#pe-letterbox .segment").forEach((button) => button.classList.toggle("is-active", button.dataset.letterbox === pe.edit.letterbox));
  const size = pe.edit.width && pe.edit.height ? `${pe.edit.width}x${pe.edit.height}` : "";
  $("#pe-size-preset").value = [...$("#pe-size-preset").options].some((option) => option.value === size) ? size : "";
  peRenderTtsProvider();
}

function peReadForm() {
  for (const [id, field, kind] of PE_FIELDS) pe.edit[field] = peReadField($(`#${id}`), kind);
  PE_LOOK_SLIDERS.forEach(([field]) => { pe.edit[field] = parseFloat($(`#pe-look-${field}`).value); });
  pe.edit.speed = peClamp(pe.edit.speed || 1, 0.25, 4);
  pe.edit.zoom = peClamp(pe.edit.zoom || 1, 1, 4);
}

/* ---- preview ---- */
function peApplyLook() {
  const edit = pe.edit;
  const warmth = peClamp((6500 - edit.temperature) / 5500, 0, 1);
  const cool = peClamp((edit.temperature - 6500) / 5500, 0, 1);
  const filters = [
    `brightness(${(1 + edit.brightness).toFixed(3)})`, `contrast(${edit.contrast.toFixed(3)})`,
    `saturate(${edit.saturation.toFixed(3)})`, `hue-rotate(${edit.hue - cool * 12}deg)`,
  ];
  if (warmth) filters.push(`sepia(${(warmth * 0.35).toFixed(3)})`);
  if (edit.sharpen < 0) filters.push(`blur(${(-edit.sharpen * 0.6).toFixed(2)}px)`);
  const video = $("#pe-video");
  video.style.filter = filters.join(" ");
  const vignette = $("#pe-vignette");
  vignette.hidden = !edit.vignette;
  vignette.style.background = `radial-gradient(ellipse at center, transparent ${Math.round(70 - edit.vignette * 45)}%, rgba(0,0,0,${(0.35 + edit.vignette * 0.55).toFixed(2)}) 100%)`;
}

function peApplyFrame() {
  const frame = $("#pe-frame"), stage = $("#pe-stage"), video = $("#pe-video");
  video.style.maxWidth = `${stage.clientWidth}px`;
  video.style.maxHeight = `${stage.clientHeight}px`;
  const rotated = pe.edit.rotate === 90 || pe.edit.rotate === 270;
  let fit = 1;
  if (rotated && video.clientWidth && video.clientHeight) {
    fit = Math.min(1, stage.clientWidth / video.clientHeight, stage.clientHeight / video.clientWidth);
  }
  frame.style.transform = `rotate(${pe.edit.rotate}deg) scale(${(pe.edit.flip_h ? -1 : 1) * fit}, ${(pe.edit.flip_v ? -1 : 1) * fit})`;
  const crop = peCropRect();
  const box = $("#pe-crop-box");
  box.hidden = !crop;
  $("#pe-crop-controls").hidden = pe.edit.aspect === "none";
  if (crop) Object.assign(box.style, { left: `${crop.x * 100}%`, top: `${crop.y * 100}%`, width: `${crop.w * 100}%`, height: `${crop.h * 100}%` });
  // Overlays (text/captions) sit over the part of the frame that is kept.
  const target = crop ? box : frame;
  const stageRect = stage.getBoundingClientRect(), rect = target.getBoundingClientRect();
  const place = { left: `${rect.left - stageRect.left}px`, top: `${rect.top - stageRect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` };
  Object.assign($("#pe-overlays").style, place);
  // Cinema bars: same arithmetic as postedit.effect_filters, on the kept part of the frame.
  const bars = $("#pe-bars"), ratio = Number(pe.edit.letterbox);
  const outW = pe.edit.width || rect.width, outH = pe.edit.height || rect.height;
  const bar = ratio ? Math.max(0, (outH - outW / ratio) / 2) / outH : 0;
  bars.hidden = !(bar > 0);
  if (bar > 0) { Object.assign(bars.style, place); bars.style.setProperty("--bar", `${(bar * 100).toFixed(2)}%`); }
}

function peApplyAudioLevels() {
  const video = $("#pe-video");
  video.muted = pe.edit.mute_original;
  video.volume = peClamp(pe.edit.original_volume, 0, 1);
  video.playbackRate = pe.edit.speed;
  video.preservesPitch = true;
}

function peNotes() {
  const notes = [];
  if (pe.edit.reverse) notes.push("reverse");
  if (pe.edit.gamma !== 1) notes.push("gamma");
  if (pe.edit.sharpen > 0) notes.push("sharpen");
  if (pe.edit.denoise) notes.push("denoise");
  if (pe.edit.grain) notes.push("grain");
  if (pe.edit.stabilize) notes.push("stabilize");
  if (pe.edit.fps) notes.push(pe.edit.interpolate ? "interpolation" : "fps");
  const fx = [["tone_curve", "tone curve"], ["teal_orange", "teal & orange"], ["glow", "glow"], ["rgb_split", "RGB split"], ["pixelate", "pixelate"], ["slow_zoom", "slow zoom"]];
  for (const [field, label] of fx) if (peDiffers(field)) notes.push(label);
  if (pe.edit.audio_denoise || pe.edit.normalize || (pe.edit.duck && pe.edit.voices.length)
    || Object.keys(PE_AUDIO_FX).some(peDiffers)) notes.push("audio processing");
  if (pe.edit.next_job_id || pe.edit.next_reference_id) notes.push("fade into the next video");
  if (pe.edit.remove_music && pe.edit.remove_music !== "off") notes.push(pe.edit.remove_music === "voices" ? "voices only (GPU)" : "music removal (GPU)");
  $("#pe-preview-note").textContent = notes.length ? `Render only: ${notes.join(", ")}.` : "";
}

function peRefresh() {
  if (!pe.source) return;
  $("#pe-speed-readout").textContent = `${pe.edit.speed.toFixed(2).replace(/\.?0+$/, "")}x`;
  $("#pe-crop-zoom-readout").textContent = `${pe.edit.zoom.toFixed(2).replace(/\.?0+$/, "")}x`;
  $("#pe-orig-volume-readout").textContent = `${Math.round(pe.edit.original_volume * 100)}%`;
  $("#pe-music-volume-readout").textContent = `${Math.round(pe.edit.music_volume * 100)}%`;
  $("#pe-duck-strength-readout").textContent = `${Math.round(pe.edit.duck_strength * 100)}%`;
  $("#pe-duck-row").hidden = !pe.edit.duck;
  $("#pe-caption-options").hidden = !pe.edit.captions;
  $("#pe-whisper-options").hidden = !pe.edit.captions || pe.edit.caption_source !== "whisper";
  if (pe.edit.captions && pe.edit.caption_source === "whisper") {
    const transcript = pe.transcripts[pe.sourceId];
    $("#pe-whisper-status").textContent = transcript
      ? `${transcript.cues.length} speech lines transcribed${transcript.language ? ` · ${transcript.language}` : ""}`
      : pe.transcribing[pe.sourceId] ? "Transcribing source audio…" : "Ready to transcribe";
  }
  $("#pe-pop-options").hidden = pe.edit.caption_style !== "pop";
  $$("#pe-caption-kind .segment").forEach((button) => button.classList.toggle("is-active", button.dataset.captionStyle === pe.edit.caption_style));
  $("#pe-interpolate").disabled = !pe.edit.fps;
  PE_LOOK_SLIDERS.forEach(([field, , , , , , format]) => { $(`#pe-look-${field}-readout`).textContent = format(pe.edit[field]); });
  const signed = (v, unit = "") => `${v > 0 ? "+" : ""}${v}${unit}`;
  const pct = (v) => (v ? `${Math.round(v * 100)}%` : "off");
  $("#pe-teal-orange-readout").textContent = pct(pe.edit.teal_orange);
  $("#pe-glow-readout").textContent = pct(pe.edit.glow);
  $("#pe-rgb-split-readout").textContent = pe.edit.rgb_split ? `${pe.edit.rgb_split}px` : "off";
  $("#pe-pixelate-readout").textContent = pe.edit.pixelate > 1 ? `${pe.edit.pixelate}px` : "off";
  $("#pe-slow-zoom-readout").textContent = !pe.edit.slow_zoom ? "off" : pe.edit.slow_zoom > 0 ? `push in to ${(1 + pe.edit.slow_zoom).toFixed(2)}x` : `pull out from ${(1 - pe.edit.slow_zoom).toFixed(2)}x`;
  $("#pe-orig-pitch-readout").textContent = pe.edit.original_pitch ? signed(pe.edit.original_pitch, " st") : "unchanged";
  $("#pe-master-bass-readout").textContent = signed(pe.edit.master_bass, " dB");
  $("#pe-master-treble-readout").textContent = signed(pe.edit.master_treble, " dB");
  $("#pe-stereo-width-readout").textContent = pe.edit.stereo_width < 0.05 ? "mono" : `${Math.round(pe.edit.stereo_width * 100)}%`;
  peRenderTabs();
  const { start, end } = peTrim();
  $("#pe-trim-end").placeholder = (pe.source.duration || 0).toFixed(2);
  const nextJob = peNextVideo();
  $("#pe-next-options").hidden = !nextJob;
  const overlap = nextJob ? Math.min(pe.edit.next_transition_duration, peLength() - 0.05, (nextJob.duration || 0) - 0.05) : 0;
  $("#pe-next-readout").textContent = nextJob
    ? `This edit (${peLength().toFixed(2)}s) + next video (${(nextJob.duration || 0).toFixed(2)}s) − ${Math.max(0.05, overlap).toFixed(2)}s transition → about ${(peLength() + (nextJob.duration || 0) - Math.max(0.05, overlap)).toFixed(1)}s (the render uses the next video's exact length). It plays unedited, fitted to this frame size.`
    : "";
  $("#pe-length-readout").textContent = `Kept ${(end - start).toFixed(2)}s of ${pe.source.duration.toFixed(2)}s → output ${peLength().toFixed(2)}s`
    + ` · source ${pe.source.width}×${pe.source.height} @ ${Math.round(pe.source.fps * 100) / 100}fps${pe.source.has_audio ? "" : " · no audio track"}`;
  peApplyLook();
  peApplyFrame();
  peApplyAudioLevels();
  peNotes();
  peRenderTimeline();
  peRenderOverlays();
}

function peChanged() {
  peReadForm();
  peRefresh();
  peSaveDraft();
  if (pe.edit.captions && pe.edit.caption_source === "whisper" && !pe.edit.reverse && pe.sourceId && !pe.transcripts[pe.sourceId]) peEnsureWhisper().catch(() => {});
}

/* ---- preview clock ---- */
function peAudioFor(reference) {
  let audio = pe.audio[reference.id];
  if (!audio) {
    audio = new Audio(reference.preview_url);
    audio.preload = "auto";
    pe.audio[reference.id] = audio;
  }
  return audio;
}

function peSyncAudio() {
  const t = pe.clock.t, playing = pe.clock.playing && $("#pe-mix-preview").checked;
  const wanted = new Set();
  // base = where in the file playback starts; limit = how long it may play (0 = to the end).
  const drive = (reference, local, volume, loopWindow, base = 0, limit = 0) => {
    if (!reference) return;
    const audio = peAudioFor(reference);
    const duration = loopWindow || limit || (audio.duration || reference.duration || 0) - base;
    if (!playing || local < 0 || (!loopWindow && duration && local >= duration)) return;
    wanted.add(reference.id);
    const target = base + (loopWindow ? local % loopWindow : local);
    audio.volume = peClamp(volume, 0, 1);
    if (Math.abs(audio.currentTime - target) > 0.25) audio.currentTime = target;
    if (audio.paused) audio.play().catch(() => {});
  };
  pe.edit.voices.forEach((track) => drive(peReference(track.reference_id), t - track.start, track.volume));
  if (pe.edit.music_reference_id) {
    const music = peReference(pe.edit.music_reference_id);
    if (music) {
      const window = peMusicWindow(music);
      drive(music, t - pe.edit.music_start, pe.edit.music_volume, pe.edit.music_loop && window > 0 ? window : 0, pe.edit.music_in, window);
    }
  }
  Object.entries(pe.audio).forEach(([id, audio]) => { if (!wanted.has(id) && !audio.paused) audio.pause(); });
}

function peFadeLevel(t) {
  const length = peLength();
  let level = 0;
  if (pe.edit.fade_in > 0 && t < pe.edit.fade_in) level = 1 - t / pe.edit.fade_in;
  if (pe.edit.fade_out > 0 && t > length - pe.edit.fade_out) level = Math.max(level, (t - (length - pe.edit.fade_out)) / pe.edit.fade_out);
  return peClamp(level, 0, 1);
}

function peTick() {
  const clock = pe.clock, video = $("#pe-video");
  if (!pe.source) return;
  const { start, end } = peTrim();
  const segment = peSegment(), length = peLength();
  if (clock.playing) {
    if (clock.holdFrom !== null) {
      clock.t = segment + (performance.now() - clock.holdFrom) / 1000;
    } else {
      clock.t = (video.currentTime - start) / pe.edit.speed;
      if (video.currentTime >= end - 0.03 || video.ended) {
        video.pause();
        clock.holdFrom = performance.now();
        clock.t = segment;
      }
    }
    if (clock.t >= length - 0.01) {
      if ($("#pe-loop").checked) { peSeek(0); video.play().catch(() => {}); }
      else peStop();
    }
  }
  pePositionPlayhead(); // first: it reads layout, and the writes below would force a fresh one
  const fade = $("#pe-fade");
  fade.style.background = pe.edit.fade_color;
  fade.style.opacity = peFadeLevel(clock.t);
  setText($("#pe-time"), `${peTime(clock.t)} / ${peTime(length)}`);
  peSyncAudio();
  peRenderOverlays();
  if (clock.playing) clock.raf = requestAnimationFrame(peTick);
}

function peSeek(t) {
  const clock = pe.clock, video = $("#pe-video");
  const { start, end } = peTrim();
  const segment = peSegment();
  clock.t = peClamp(t, 0, peLength());
  if (clock.t < segment) {
    clock.holdFrom = null;
    video.currentTime = start + clock.t * pe.edit.speed;
  } else {
    video.currentTime = Math.max(start, end - 0.04);
    video.pause();
    clock.holdFrom = clock.playing ? performance.now() - (clock.t - segment) * 1000 : null;
  }
  if (!clock.playing) peTick();
}

function pePlay() {
  const clock = pe.clock, video = $("#pe-video");
  if (!pe.source) return;
  if (clock.t >= peLength() - 0.02) peSeek(0);
  clock.playing = true;
  if (clock.t < peSegment()) video.play().catch(() => {});
  else clock.holdFrom = performance.now() - (clock.t - peSegment()) * 1000;
  $("#pe-play").innerHTML = uiIcon("pause");
  cancelAnimationFrame(clock.raf);
  clock.raf = requestAnimationFrame(peTick);
}

function peStop() {
  const clock = pe.clock;
  clock.playing = false;
  clock.holdFrom = null;
  cancelAnimationFrame(clock.raf);
  $("#pe-video").pause();
  Object.values(pe.audio).forEach((audio) => audio.pause());
  $("#pe-play").innerHTML = uiIcon("play");
}

/* ---- overlays (text + captions) ---- */
function peParseSrt(text) {
  const cues = [];
  (text || "").replace(/\r/g, "").trim().split(/\n\s*\n/).forEach((block) => {
    const lines = block.split("\n");
    const index = lines.findIndex((line) => line.includes("-->"));
    if (index < 0) return;
    const [a, b] = lines[index].split("-->").map((part) => {
      const match = part.trim().match(/(\d+):(\d+):(\d+)[,.](\d+)/);
      return match ? (+match[1]) * 3600 + (+match[2]) * 60 + (+match[3]) + (+match[4]) / 1000 : 0;
    });
    const body = lines.slice(index + 1).join(" ").trim();
    if (body) cues.push([a, b, body]);
  });
  return cues;
}

async function peLoadCaptions(referenceId) {
  if (referenceId in pe.captions) return;
  pe.captions[referenceId] = [];
  try {
    const { captions, words } = await Api.request(`${API_BASE}/api/references/${referenceId}/captions`);
    pe.captions[referenceId] = peParseSrt(captions);
    if (words) pe.words[referenceId] = words;
    peRenderOverlays();
  } catch { /* no captions for uploads */ }
}

async function peEnsureWhisper() {
  const sourceId = pe.sourceId;
  if (!sourceId || !pe.source?.has_audio) throw new Error("Choose a source video with audio to transcribe.");
  if (pe.transcripts[sourceId]) return pe.transcripts[sourceId];
  if (pe.transcribing[sourceId]) return pe.transcribing[sourceId];
  const button = $("#pe-whisper-transcribe");
  button.disabled = true;
  const url = `${API_BASE}/api/postedit/transcribe/${encodeURIComponent(sourceId)}`;
  const pending = Api.request(url, { method: "POST" })
    .then(async (initial) => {
      let status = initial;
      while (status.state === "transcribing") {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        status = await Api.request(url, { cache: "no-store" });
      }
      if (status.state !== "ready") throw new Error(status.error || "Whisper transcription did not finish.");
      return status;
    })
    .then((transcript) => {
      pe.transcripts[sourceId] = transcript;
      if (pe.sourceId === sourceId) peRefresh();
      return transcript;
    })
    .catch((error) => {
      if (pe.sourceId === sourceId) {
        $("#pe-whisper-status").textContent = error.message;
        toast(`Whisper transcription failed: ${error.message}`, "error");
      }
      throw error;
    })
    .finally(() => {
      delete pe.transcribing[sourceId];
      button.disabled = false;
      if (pe.sourceId === sourceId && pe.transcripts[sourceId]) peRefresh();
    });
  pe.transcribing[sourceId] = pending;
  peRefresh();
  return pending;
}

function peWhisperItems(items) {
  const { start, end } = peTrim();
  return (items || []).flatMap(([a, b, text]) => {
    const clippedStart = Math.max(a, start), clippedEnd = Math.min(b, end);
    return clippedEnd > clippedStart ? [[(clippedStart - start) / pe.edit.speed,
      (clippedEnd - start) / pe.edit.speed, text]] : [];
  });
}

function peCaptionCues() {
  if (pe.edit.caption_source === "whisper") return peWhisperItems(pe.transcripts[pe.sourceId]?.cues);
  return null;
}

function peCaptionWords() {
  if (pe.edit.caption_source === "whisper") {
    const transcript = pe.transcripts[pe.sourceId];
    return peWhisperItems(transcript?.words?.length ? transcript.words : peEstimateWords(transcript?.cues || []));
  }
  const words = [];
  pe.edit.voices.filter((track) => track.captions).forEach((track) => {
    const own = pe.words[track.reference_id] || peEstimateWords(pe.captions[track.reference_id] || []);
    own.forEach(([a, b, text]) => { if (a + track.start < peLength()) words.push([a + track.start, Math.min(b + track.start, peLength()), text]); });
  });
  return words;
}

// Same as postedit.estimate_words: words share their cue's time by length.
function peEstimateWords(cues) {
  const words = [];
  cues.forEach(([start, end, text]) => {
    const tokens = text.split(/\s+/).filter(Boolean);
    const weights = tokens.map((token) => token.length + 2);
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    let cursor = start;
    tokens.forEach((token, index) => {
      const span = (end - start) * weights[index] / total;
      words.push([cursor, cursor + span, token]);
      cursor += span;
    });
  });
  return words;
}

// Mirrors postedit.pop_caption_events: groups of pop_words words, never across
// a sentence end or a pause over 0.5s. Returns the group and word at time t.
function pePopCaptionAt(t) {
  const length = peLength();
  const words = peCaptionWords();
  words.sort((x, y) => x[0] - y[0]);
  const groups = [];
  words.forEach((word) => {
    const last = groups[groups.length - 1];
    if (!last || last.length >= pe.edit.pop_words || /[.!?…]$/.test(last[last.length - 1][2]) || word[0] - last[last.length - 1][1] > 0.5) groups.push([]);
    groups[groups.length - 1].push(word);
  });
  for (let index = 0; index < groups.length; index += 1) {
    const group = groups[index];
    const next = index + 1 < groups.length ? groups[index + 1][0][0] : length;
    const end = Math.min(next, group[group.length - 1][1] + 0.5, length);
    if (t < group[0][0] || t >= end) continue;
    let active = 0;
    group.forEach((word, position) => { if (position > 0 && t >= word[0]) active = position; });
    return { index, group, active };
  }
  return null;
}

const peTilt = (index) => Math.round((((index * 7919) % 9) - 4) * 0.9 * 10) / 10;

// Overlays are keyed elements so a pop animation plays once when its text
// appears, not on every frame of the preview clock.
function peOverlayItems() {
  const t = pe.clock.t, height = $("#pe-overlays").clientHeight || 1, scale = height / Math.max(1, peOutputHeight());
  const items = [];
  const vertical = (position, top, bottom) => (position === "top" ? `top:${top}` : position === "middle" ? "top:50%" : `bottom:${bottom}`);
  pe.edit.texts.forEach((overlay, index) => {
    const end = overlay.end != null && overlay.end > overlay.start ? overlay.end : peLength();
    if (t < overlay.start || t > end) return;
    const ramp = Math.min(0.3, (end - overlay.start) / 3);
    const alpha = overlay.pop ? peClamp((end - t) / 0.12, 0, 1)
      : overlay.fade ? peClamp(Math.min((t - overlay.start) / ramp, (end - t) / ramp), 0, 1) : 1;
    items.push({
      key: `text-${index}-${overlay.pop ? "pop" : "plain"}`,
      cls: `pe-overlay-text${overlay.position === "middle" ? " is-middle" : ""}${overlay.outline ? " has-outline" : ""}${overlay.box ? " has-box" : ""}${overlay.pop ? " pe-pop" : ""}`,
      style: `${vertical(overlay.position, "6%", "8%")};text-align:${overlay.align};font-size:${(overlay.size * scale).toFixed(1)}px;color:${escapeHtml(overlay.color)};opacity:${alpha.toFixed(2)}${overlay.pop ? `;--tilt:${peTilt(index + 3)}deg` : ""}`,
      inner: `<span>${escapeHtml(overlay.text)}</span>`,
    });
  });
  if (!pe.edit.captions) return items;
  if (pe.edit.caption_style === "pop") {
    const hit = pePopCaptionAt(t);
    if (hit) {
      const token = (text) => {
        const trimmed = text.replace(/[.,;:]+$/, "") || text;
        return escapeHtml(pe.edit.pop_uppercase ? trimmed.toUpperCase() : trimmed);
      };
      const inner = hit.group.map((word, position) => (position === hit.active
        ? `<span style="color:${escapeHtml(pe.edit.pop_highlight)}">${token(word[2])}</span>` : token(word[2]))).join(" ");
      const margin = `${(height * 0.14).toFixed(1)}px`;
      items.push({
        key: `pop-${hit.index}`,
        cls: `pe-overlay-text pe-overlay-popcap pe-pop${pe.edit.caption_position === "middle" ? " is-middle" : ""}`,
        style: `${vertical(pe.edit.caption_position, margin, margin)};font-size:${(pe.edit.caption_size / 288 * height).toFixed(1)}px;color:${escapeHtml(pe.edit.caption_color)};--tilt:${pe.edit.pop_tilt ? peTilt(hit.index) : 0}deg`,
        inner,
      });
    }
    return items;
  }
  if (pe.edit.caption_source === "whisper") {
    const cue = peCaptionCues().find(([a, b]) => t >= a && t < b);
    if (cue) {
      const margin = `${(24 / 288 * height).toFixed(1)}px`;
      items.push({
        key: `cap-whisper-${cue[0]}`,
        cls: `pe-overlay-text pe-overlay-caption${pe.edit.caption_position === "middle" ? " is-middle" : ""}`,
        style: `${vertical(pe.edit.caption_position, margin, margin)};font-size:${(pe.edit.caption_size / 288 * height).toFixed(1)}px;color:${escapeHtml(pe.edit.caption_color)}`,
        inner: `<span>${escapeHtml(cue[2])}</span>`,
      });
    }
    return items;
  }
  pe.edit.voices.filter((track) => track.captions).forEach((track) => {
    const cue = (pe.captions[track.reference_id] || []).find(([a, b]) => t >= a + track.start && t < b + track.start);
    if (!cue) return;
    const margin = `${(24 / 288 * height).toFixed(1)}px`;
    items.push({
      key: `cap-${track.reference_id}-${cue[0]}`,
      cls: `pe-overlay-text pe-overlay-caption${pe.edit.caption_position === "middle" ? " is-middle" : ""}`,
      style: `${vertical(pe.edit.caption_position, margin, margin)};font-size:${(pe.edit.caption_size / 288 * height).toFixed(1)}px;color:${escapeHtml(pe.edit.caption_color)}`,
      inner: `<span>${escapeHtml(cue[2])}</span>`,
    });
  });
  return items;
}

function peRenderOverlays() {
  const host = $("#pe-overlays");
  if (!pe.source) { host.innerHTML = ""; return; }
  const items = peOverlayItems();
  const keys = new Set(items.map((item) => item.key));
  [...host.children].forEach((element) => { if (!keys.has(element.dataset.key)) element.remove(); });
  items.forEach((item) => {
    let element = [...host.children].find((child) => child.dataset.key === item.key);
    if (!element) {
      element = document.createElement("div");
      element.dataset.key = item.key;
      host.append(element);
    }
    if (element.className !== item.cls) element.className = item.cls;
    if (element.getAttribute("style") !== item.style) element.setAttribute("style", item.style);
    if (element.peInner !== item.inner) { element.innerHTML = item.inner; element.peInner = item.inner; }
  });
}

/* ---- timeline ---- */
// The output lanes show a window of the timeline: `span` seconds starting at
// `offset`. Zoom 1 = everything (the output plus any clip running past its end).
const PE_ZOOM_MAX = 32;
const PE_ROW = 26; // px per stacked voice row
const PE_HOLD_MS = 320; // click-and-hold -> zoom in
const PE_SNAP_PX = 8;
pe.view = { zoom: 1, offset: 0 };

function peTotal() {
  let end = peLength();
  pe.edit.voices.forEach((track) => { end = Math.max(end, track.start + (peReference(track.reference_id)?.duration || 0)); });
  pe.edit.texts.forEach((overlay) => { end = Math.max(end, overlay.end ?? overlay.start); });
  return Math.max(0.1, end > peLength() + 0.01 ? end + 0.25 : end);
}
function peSpan() { return peTotal() / pe.view.zoom; }
function peClampView() {
  pe.view.zoom = peClamp(pe.view.zoom, 1, PE_ZOOM_MAX);
  pe.view.offset = peClamp(pe.view.offset, 0, Math.max(0, peTotal() - peSpan()));
}
const pePct = (seconds) => `${((seconds - pe.view.offset) / peSpan()) * 100}%`;

// Zoom so `anchor` (seconds) stays under the same screen position.
function peZoomTo(zoom, anchor = pe.view.offset + peSpan() / 2) {
  const fraction = (anchor - pe.view.offset) / peSpan();
  pe.view.zoom = peClamp(zoom, 1, PE_ZOOM_MAX);
  pe.view.offset = anchor - fraction * peSpan();
  peClampView();
  peRenderTimeline();
}

function peTrackGeometry(track) {
  const timeline = $("#pe-timeline").getBoundingClientRect(), rect = track.getBoundingClientRect();
  return { left: rect.left - timeline.left, width: rect.width || 1 };
}

function peTimeAt(clientX) {
  const rect = $("#pe-voice-track").getBoundingClientRect();
  return pe.view.offset + ((clientX - rect.left) / (rect.width || 1)) * peSpan();
}

function pePositionPlayhead() {
  const track = $("#pe-voice-track");
  const { left, width } = peTrackGeometry(track);
  const span = peSpan();
  // Follow the playhead while playing zoomed in.
  if (pe.clock.playing && pe.view.zoom > 1 && (pe.clock.t > pe.view.offset + span * 0.95 || pe.clock.t < pe.view.offset)) {
    const before = pe.view.offset;
    pe.view.offset = pe.clock.t - span * 0.1;
    peClampView();
    // peRenderTimeline() calls back in here. In the last 5% of the timeline the
    // clamp keeps the view where it was, so re-rendering recursed forever
    // ("Maximum call stack size exceeded" when playing zoomed in to the end).
    if (pe.view.offset !== before) {
      peRenderTimeline();
      return;
    }
  }
  const fraction = (pe.clock.t - pe.view.offset) / span;
  // Every read before any write: this runs each animation frame while playing,
  // and reading offsetTop after setting style forced extra layouts (~3 a frame).
  const textTrack = $("#pe-text-track");
  const top = track.offsetTop, height = textTrack.offsetTop + textTrack.offsetHeight - top;
  const head = $("#pe-playhead");
  head.hidden = fraction < 0 || fraction > 1;
  head.style.left = `${left + fraction * width}px`;
  head.style.top = `${top}px`;
  head.style.height = `${height}px`;
  $("#pe-source-head").style.left = `${($("#pe-video").currentTime / Math.max(0.1, pe.source?.duration || 1)) * 100}%`;
  $("#pe-overview-head").style.left = `${(pe.clock.t / peTotal()) * 100}%`;
}

// Greedy rows so overlapping voice clips never cover each other.
function peVoiceRows() {
  const order = pe.edit.voices.map((track, index) => ({ index, start: track.start, end: track.start + (peReference(track.reference_id)?.duration || 1) }))
    .sort((a, b) => a.start - b.start);
  const rowEnds = [], rows = {};
  order.forEach((clip) => {
    let row = rowEnds.findIndex((end) => end <= clip.start + 0.01);
    if (row < 0) { row = rowEnds.length; rowEnds.push(0); }
    rowEnds[row] = clip.end;
    rows[clip.index] = row;
  });
  return { rows, count: Math.max(1, rowEnds.length) };
}

function peRenderTimeline() {
  if (!pe.source) return;
  peClampView();
  const duration = pe.source.duration || 1, length = Math.max(0.1, peLength()), span = peSpan();
  const { start, end } = peTrim();
  Object.assign($("#pe-trim").style, { left: `${start / duration * 100}%`, width: `${(end - start) / duration * 100}%` });
  const pastEnd = `<div class="pe-past-end" style="left:${pePct(length)}" title="Past the end of the video"></div>`;
  const block = (kind, index, from, to, label, extra = "", top = null) => {
    const selected = pe.selected === `${kind}:${index}`;
    return `<div class="pe-block is-${kind}${selected ? " is-selected" : ""}${to > length + 0.05 ? " is-beyond" : ""}${extra}" data-kind="${kind}" data-index="${index}" style="left:${pePct(from)};width:${Math.max(0, to - from) / span * 100}%${top != null ? `;top:${top}px` : ""}" title="${escapeHtml(label)}">${escapeHtml(label)}</div>`;
  };
  const { rows, count } = pe.drag?.layout || peVoiceRows();
  const voiceTrack = $("#pe-voice-track");
  voiceTrack.style.height = `${count * PE_ROW + 2}px`;
  voiceTrack.innerHTML = pastEnd + pe.edit.voices.map((track, index) => {
    const reference = peReference(track.reference_id);
    const extra = (pe.drag?.index === index && pe.drag.kind === "voice" ? " is-dragging" : "") + (pe.drag?.target === index ? " is-drop-target" : "");
    return block("voice", index, track.start, track.start + (reference?.duration || 1), reference?.label || reference?.filename || "voice", extra, 2 + rows[index] * PE_ROW);
  }).join("");
  const music = pe.edit.music_reference_id ? peReference(pe.edit.music_reference_id) : null;
  $("#pe-music-track").innerHTML = pastEnd + (music ? block("music", 0, pe.edit.music_start, pe.edit.music_loop ? length : pe.edit.music_start + peMusicWindow(music), music.label || music.filename) : "");
  $("#pe-text-track").innerHTML = pastEnd + pe.edit.texts.map((overlay, index) =>
    block("text", index, overlay.start, overlay.end != null && overlay.end > overlay.start ? overlay.end : length, overlay.text,
      pe.drag?.index === index && pe.drag.kind === "text" ? " is-dragging" : "")).join("");
  const step = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120].find((value) => span / value <= 9) || 300;
  let ruler = "";
  for (let s = Math.ceil(pe.view.offset / step) * step; s <= pe.view.offset + span + 1e-6; s += step) {
    const at = (s - pe.view.offset) / span; // keep labels at the very edges inside the ruler
    const shift = at < 0.03 ? "transform:none" : at > 0.97 ? "transform:translateX(-100%)" : "";
    ruler += `<span style="left:${pePct(s)};${shift}">${step < 1 ? s.toFixed(step < 0.5 ? 2 : 1) : Math.round(s)}s</span>`;
  }
  $("#pe-ruler").innerHTML = ruler;
  const total = peTotal();
  Object.assign($("#pe-overview-window").style, { left: `${pe.view.offset / total * 100}%`, width: `${span / total * 100}%` });
  $("#pe-zoom-readout").textContent = pe.view.zoom > 1.001
    ? `${pe.view.zoom < 10 ? pe.view.zoom.toFixed(1) : Math.round(pe.view.zoom)}x · ${peTime(pe.view.offset)}–${peTime(pe.view.offset + span)}`
    : `Whole timeline · ${peTime(total)}`;
  $("#pe-zoom-in").disabled = pe.view.zoom >= PE_ZOOM_MAX;
  $("#pe-zoom-out").disabled = $("#pe-zoom-fit").disabled = pe.view.zoom <= 1.001;
  pePositionPlayhead();
}

// Snap `value` (seconds) to the nearest edge within PE_SNAP_PX, or return it.
function peSnap(values, candidates, width) {
  const threshold = (PE_SNAP_PX / width) * peSpan();
  let best = null;
  values.forEach((value, which) => candidates.forEach((candidate) => {
    const distance = Math.abs(candidate - value);
    if (distance <= threshold && (!best || distance < best.distance)) best = { distance, shift: candidate - value, at: candidate, which };
  }));
  return best;
}

function peShowSnap(at) {
  const line = $("#pe-snap-line");
  if (at == null) { line.hidden = true; return; }
  const track = $("#pe-voice-track");
  const { left, width } = peTrackGeometry(track);
  line.hidden = false;
  line.style.left = `${left + ((at - pe.view.offset) / peSpan()) * width}px`;
  line.style.top = `${track.offsetTop}px`;
  line.style.height = `${$("#pe-text-track").offsetTop + $("#pe-text-track").offsetHeight - track.offsetTop}px`;
}

function peBindTimeline() {
  const timeline = $("#pe-timeline");
  $("#pe-zoom-in").addEventListener("click", () => peZoomTo(pe.view.zoom * 2, pe.clock.t >= pe.view.offset && pe.clock.t <= pe.view.offset + peSpan() ? pe.clock.t : undefined));
  $("#pe-zoom-out").addEventListener("click", () => peZoomTo(pe.view.zoom / 2));
  $("#pe-zoom-fit").addEventListener("click", () => peZoomTo(1));
  timeline.addEventListener("dblclick", (event) => { if (event.target.closest(".pe-output-track") && !event.target.closest(".pe-block")) peZoomTo(1); });
  timeline.addEventListener("wheel", (event) => {
    if (!pe.source || !event.target.closest(".pe-output-track, .pe-ruler, .pe-overview")) return;
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      peZoomTo(pe.view.zoom * Math.pow(1.0018, -event.deltaY), peTimeAt(event.clientX));
    } else if (pe.view.zoom > 1 && (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY))) {
      event.preventDefault();
      const delta = event.shiftKey ? event.deltaY : event.deltaX;
      pe.view.offset += (delta / $("#pe-voice-track").clientWidth) * peSpan();
      peRenderTimeline();
    }
  }, { passive: false });

  // Overview: drag the window (or click to center it there).
  $("#pe-overview").addEventListener("pointerdown", (event) => {
    if (!pe.source) return;
    event.preventDefault();
    const bar = $("#pe-overview"), rect = bar.getBoundingClientRect();
    const toTime = (x) => ((x - rect.left) / rect.width) * peTotal();
    const onWindow = event.target.id === "pe-overview-window";
    const grab = onWindow ? toTime(event.clientX) - pe.view.offset : peSpan() / 2;
    const move = (e) => { pe.view.offset = toTime(e.clientX) - grab; peRenderTimeline(); };
    move(event);
    bar.setPointerCapture(event.pointerId);
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", () => bar.removeEventListener("pointermove", move), { once: true });
  });

  timeline.addEventListener("pointerdown", (event) => {
    if (!pe.source || event.target.closest("#pe-overview")) return;
    const handle = event.target.closest(".pe-handle");
    const blockEl = event.target.closest(".pe-block");
    const track = event.target.closest(".pe-lane-track");
    if (!track) return;
    event.preventDefault();
    if (handle || track.id === "pe-source-track") {
      // Trim handles (or clicking the source lane seeks the source there).
      const rect = track.getBoundingClientRect();
      const at = (e) => peClamp((e.clientX - rect.left) / rect.width, 0, 1) * pe.source.duration;
      if (!handle) {
        const { start, end } = peTrim();
        const time = at(event);
        if (time >= start && time <= end) peSeek((time - start) / pe.edit.speed);
        return;
      }
      const which = handle.dataset.handle;
      peStop();
      const move = (e) => {
        const time = at(e), { start, end } = peTrim();
        if (which === "start") pe.edit.trim_start = +Math.min(time, end - 0.1).toFixed(3);
        else pe.edit.trim_end = +Math.max(time, start + 0.1).toFixed(3);
        $("#pe-trim-start").value = pe.edit.trim_start;
        $("#pe-trim-end").value = pe.edit.trim_end ?? "";
        $("#pe-video").currentTime = which === "start" ? pe.edit.trim_start : (pe.edit.trim_end ?? pe.source.duration) - 0.04;
        peRefresh();
      };
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); peSaveDraft(); peSeek(0); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      return;
    }

    if (!blockEl || blockEl.dataset.kind === "music") {
      // Empty space: click = seek, drag = scrub, click and hold = zoom in
      // around that spot (keep holding to zoom further), then drag to pan.
      const startX = event.clientX;
      let mode = "pending", holdTimer = 0, anchorX = startX, anchorOffset = 0, lastZoomX = startX;
      const zoomHere = (x) => {
        mode = "hold";
        timeline.classList.add("is-grabbing");
        peZoomTo(pe.view.zoom * 2, peTimeAt(x));
        anchorX = x; anchorOffset = pe.view.offset; lastZoomX = x;
        holdTimer = setTimeout(() => zoomHere(anchorX + (lastZoomX - anchorX)), PE_HOLD_MS * 2.2);
      };
      holdTimer = setTimeout(() => zoomHere(startX), PE_HOLD_MS);
      const move = (e) => {
        if (mode === "pending" && Math.abs(e.clientX - startX) > 4) { clearTimeout(holdTimer); mode = "scrub"; }
        if (mode === "scrub") peSeek(peClamp(peTimeAt(e.clientX), 0, peLength()));
        if (mode === "hold") {
          if (Math.abs(e.clientX - lastZoomX) > 6) clearTimeout(holdTimer); // moving = panning, not zooming
          pe.view.offset = anchorOffset - ((e.clientX - anchorX) / $("#pe-voice-track").clientWidth) * peSpan();
          peRenderTimeline();
        }
      };
      const up = (e) => {
        clearTimeout(holdTimer);
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        timeline.classList.remove("is-grabbing");
        if (mode === "pending") peSeek(peClamp(peTimeAt(e.clientX), 0, peLength()));
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      return;
    }

    // Drag a voice/text block: move it (snapping to edges), or drop a voice
    // clip onto another one to swap their places.
    const kind = blockEl.dataset.kind, index = +blockEl.dataset.index;
    const item = kind === "voice" ? pe.edit.voices[index] : pe.edit.texts[index];
    const clipLength = kind === "voice" ? (peReference(item.reference_id)?.duration || 1)
      : ((item.end != null && item.end > item.start ? item.end : peLength()) - item.start);
    pe.selected = `${kind}:${index}`;
    const originX = event.clientX, originStart = item.start, originEnd = kind === "text" ? item.end : null;
    const width = $("#pe-voice-track").clientWidth || 1;
    const candidates = [0, pe.clock.t, peLength()];
    pe.edit.voices.forEach((other, i) => {
      if (kind === "voice" && i === index) return;
      candidates.push(other.start, other.start + (peReference(other.reference_id)?.duration || 0));
    });
    pe.edit.texts.forEach((other, i) => { if (!(kind === "text" && i === index)) candidates.push(other.start, other.end ?? peLength()); });
    let moved = false;
    const move = (e) => {
      if (Math.abs(e.clientX - originX) > 3) moved = true;
      if (!moved) return;
      // Rows are frozen for the drag, or the clip under the pointer would hop rows as the dragged one overlaps it.
      pe.drag = pe.drag || { kind, index, target: null, layout: kind === "voice" ? peVoiceRows() : null };
      let start = Math.max(0, originStart + ((e.clientX - originX) / width) * peSpan());
      let snapAt = null;
      if (!e.altKey) {
        const snap = peSnap([start, start + clipLength], candidates, width);
        if (snap) { start = Math.max(0, start + snap.shift); snapAt = snap.at; }
      }
      if (kind === "voice") {
        const over = document.elementsFromPoint(e.clientX, e.clientY)
          .find((el) => el.classList?.contains("is-voice") && +el.dataset.index !== index);
        pe.drag.target = over ? +over.dataset.index : null;
        if (pe.drag.target != null) snapAt = null;
      }
      item.start = +start.toFixed(2);
      if (originEnd != null) item.end = +(originEnd + (item.start - originStart)).toFixed(2);
      peRenderTimeline();
      peShowSnap(snapAt);
    };
    const up = (e) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      const target = pe.drag?.target;
      pe.drag = null;
      peShowSnap(null);
      if (!moved) { peSeek(peClamp(peTimeAt(e.clientX), 0, peLength())); peRenderTimeline(); return; }
      if (kind === "voice" && target != null) {
        const other = pe.edit.voices[target];
        item.start = other.start;
        other.start = originStart;
        pe.selected = `voice:${index}`;
        const name = (track) => peReference(track.reference_id)?.label || "clip";
        toast(`Swapped “${name(item).slice(0, 40)}” and “${name(other).slice(0, 40)}”.`);
      }
      peRenderTimeline();
      peRenderLists();
      peRenderOverlays();
      peSaveDraft();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

function peBindCropDrag() {
  const box = $("#pe-crop-box");
  box.addEventListener("pointerdown", (event) => {
    const crop = peCropRect();
    if (!crop) return;
    event.preventDefault();
    const frame = $("#pe-video").getBoundingClientRect();
    const startX = event.clientX, startY = event.clientY, panX = pe.edit.pan_x, panY = pe.edit.pan_y;
    const rotated = pe.edit.rotate === 90 || pe.edit.rotate === 270;
    const move = (e) => {
      let dx = e.clientX - startX, dy = e.clientY - startY;
      // Undo the preview's rotation/flip so dragging follows the pointer.
      const angle = -pe.edit.rotate * Math.PI / 180;
      [dx, dy] = [dx * Math.cos(angle) - dy * Math.sin(angle), dx * Math.sin(angle) + dy * Math.cos(angle)];
      if (pe.edit.flip_h) dx = -dx;
      if (pe.edit.flip_v) dy = -dy;
      const frameW = rotated ? frame.height : frame.width, frameH = rotated ? frame.width : frame.height;
      if (crop.w < 1) pe.edit.pan_x = peClamp(panX + dx / (frameW * (1 - crop.w)), 0, 1);
      if (crop.h < 1) pe.edit.pan_y = peClamp(panY + dy / (frameH * (1 - crop.h)), 0, 1);
      $("#pe-pan-x").value = pe.edit.pan_x;
      $("#pe-pan-y").value = pe.edit.pan_y;
      peApplyFrame();
      peRenderOverlays();
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); peSaveDraft(); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

/* ---- music part picker (waveform) ---- */
function peMusicWindow(music) {
  const end = pe.edit.music_out ?? music?.duration ?? 0;
  return Math.max(0, end - pe.edit.music_in);
}

function peChooseMusic(id) {
  pe.edit.music_reference_id = id;
  const music = id ? peReference(id) : null;
  // Start from the file's own trim (e.g. what was picked for a Stitch), else the whole song.
  pe.edit.music_in = music?.trim_start || 0;
  pe.edit.music_out = music?.trim_end ?? null;
  peMusicListenStop();
  peRenderLists();
  peSyncForm();
  peRenderMusicPart();
  peSaveDraft();
}

async function peWaveform(music) {
  pe.waves = pe.waves || {};
  if (pe.waves[music.id]) return pe.waves[music.id];
  pe.waves[music.id] = new Promise(async (resolve) => {
    // Server-computed peaks (~10 KB) instead of downloading and decoding the
    // whole file (3-7 MB of music) on the phone; the old way stays as a fallback.
    if (music.peaks_url) {
      try {
        const { duration, peaks } = await Api.request(music.peaks_url);
        if (peaks?.length) return resolve({ duration, peaks: Float32Array.from(peaks) });
      } catch { /* fall back to decoding it here */ }
    }
    try {
      const data = await (await fetch(music.preview_url)).arrayBuffer();
      const context = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 2, 44100);
      const buffer = await context.decodeAudioData(data);
      const channel = buffer.getChannelData(0), bins = 4000, size = Math.max(1, Math.floor(channel.length / bins));
      const peaks = new Float32Array(bins);
      for (let bin = 0; bin < bins; bin += 1) {
        let peak = 0;
        for (let i = bin * size, end = Math.min(channel.length, i + size); i < end; i += 16) peak = Math.max(peak, Math.abs(channel[i]));
        peaks[bin] = peak;
      }
      resolve({ duration: buffer.duration, peaks });
    } catch { delete pe.waves[music.id]; resolve(null); } // a network blip shouldn't hide the waveform until reload
  });
  return pe.waves[music.id];
}

async function peRenderMusicPart() {
  const music = pe.edit.music_reference_id ? peReference(pe.edit.music_reference_id) : null;
  if (!music) return;
  const duration = music.duration || 0;
  const start = pe.edit.music_in, end = pe.edit.music_out ?? duration;
  $("#pe-music-range-readout").textContent = duration
    ? `${peTime(start)} → ${pe.edit.music_out == null ? "end" : peTime(end)} (${(end - start).toFixed(1)}s of ${peTime(duration)})` : "";
  $("#pe-music-in").max = duration || "";
  $("#pe-music-out").max = duration || "";
  if (duration) Object.assign($("#pe-wave-sel").style, { left: `${start / duration * 100}%`, width: `${Math.max(0, end - start) / duration * 100}%` });
  const canvas = $("#pe-wave-canvas"), wave = $("#pe-wave");
  const width = wave.clientWidth, height = wave.clientHeight;
  if (!width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  const data = await peWaveform(music);
  if (pe.edit.music_reference_id !== music.id) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!data) return;
  ctx.fillStyle = "rgba(130, 196, 90, 0.55)";
  const bar = canvas.width / data.peaks.length, mid = canvas.height / 2;
  data.peaks.forEach((peak, index) => {
    const h = Math.max(1, peak * (canvas.height - 6 * ratio));
    ctx.fillRect(index * bar, mid - h / 2, Math.max(1, bar - ratio * 0.5), h);
  });
}

function peMusicListenStop() {
  const listen = pe.musicListen;
  if (!listen) return;
  listen.audio.pause();
  cancelAnimationFrame(listen.raf);
  pe.musicListen = null;
  $("#pe-wave-head").hidden = true;
  $("#pe-music-listen").innerHTML = "&#9654; Listen";
}

function peMusicListen() {
  if (pe.musicListen) return peMusicListenStop();
  const music = peReference(pe.edit.music_reference_id);
  if (!music) return;
  peStop();
  const audio = new Audio(music.preview_url);
  const end = pe.edit.music_out ?? Infinity;
  audio.currentTime = pe.edit.music_in;
  audio.volume = 1;
  audio.play().catch(() => {});
  pe.musicListen = { audio, raf: 0 };
  $("#pe-music-listen").innerHTML = "&#9632; Stop";
  const head = $("#pe-wave-head");
  head.hidden = false;
  const tick = () => {
    if (!pe.musicListen) return;
    const duration = music.duration || audio.duration || 1;
    head.style.left = `${audio.currentTime / duration * 100}%`;
    if (audio.currentTime >= end || audio.ended) return peMusicListenStop();
    pe.musicListen.raf = requestAnimationFrame(tick);
  };
  tick();
}

function peBindMusicPart() {
  const wave = $("#pe-wave");
  wave.addEventListener("pointerdown", (event) => {
    const music = peReference(pe.edit.music_reference_id);
    const duration = music?.duration;
    if (!duration) return;
    event.preventDefault();
    const rect = wave.getBoundingClientRect();
    const at = (e) => +(peClamp((e.clientX - rect.left) / rect.width, 0, 1) * duration).toFixed(2);
    const handle = event.target.closest("[data-wave-handle]")?.dataset.waveHandle || "in";
    const apply = (e) => {
      const time = at(e);
      if (handle === "in") {
        pe.edit.music_in = Math.min(time, (pe.edit.music_out ?? duration) - 0.1);
      } else {
        pe.edit.music_out = time >= duration - 0.05 ? null : Math.max(time, pe.edit.music_in + 0.1);
      }
      $("#pe-music-in").value = pe.edit.music_in;
      $("#pe-music-out").value = pe.edit.music_out ?? "";
      peRenderMusicPart();
      peRenderTimeline();
    };
    apply(event);
    const up = () => {
      window.removeEventListener("pointermove", apply);
      window.removeEventListener("pointerup", up);
      peSaveDraft();
      if (pe.musicListen) { peMusicListenStop(); peMusicListen(); } // restart from the new start
    };
    window.addEventListener("pointermove", apply);
    window.addEventListener("pointerup", up);
  });
  $("#pe-music-listen").addEventListener("click", peMusicListen);
  ["#pe-music-in", "#pe-music-out"].forEach((id) => $(id).addEventListener("change", () => {
    const music = peReference(pe.edit.music_reference_id);
    const duration = music?.duration || Infinity;
    pe.edit.music_in = peClamp(pe.edit.music_in, 0, Math.max(0, duration - 0.1));
    if (pe.edit.music_out != null) pe.edit.music_out = peClamp(pe.edit.music_out, pe.edit.music_in + 0.1, duration);
    peSyncForm();
    peRenderMusicPart();
  }));
}

/* ---- lists: voices, music, text ---- */
function peRenderLists() {
  peRenderNextVideos();
  // Voice clips: every Post-Edit voice reference; "On timeline" puts it in the edit.
  const voices = state.references.filter((reference) => reference.role === "postedit-voice");
  const ids = new Set(voices.map((reference) => reference.id));
  pe.edit.voices = pe.edit.voices.filter((track) => ids.has(track.reference_id));
  $("#pe-voice-list").innerHTML = voices.length ? voices.slice().reverse().map((reference) => {
    const index = pe.edit.voices.findIndex((track) => track.reference_id === reference.id);
    const track = pe.edit.voices[index];
    return `<div class="pe-track${pe.selected === `voice:${index}` && track ? " is-selected" : ""}" data-voice-ref="${reference.id}">
      <div class="pe-track-head">
        <button type="button" class="icon-button" data-pe-preview="${reference.id}" title="Play this clip" aria-label="Play this clip">${uiIcon("play")}</button>
        <span class="pe-track-title" title="${escapeHtml(reference.label || reference.filename)}">${escapeHtml(reference.label || reference.filename)}</span>
        <span class="pe-track-meta">${reference.duration ? `${reference.duration.toFixed(1)}s` : ""}</span>
        <label class="pe-inline-check"><input type="checkbox" data-pe-voice-on ${track ? "checked" : ""}> On timeline</label>
        <button type="button" class="icon-button" data-pe-delete-ref="${reference.id}" title="Delete this clip" aria-label="Delete this clip">${uiIcon("trash")}</button>
      </div>
      ${track ? `<div class="pe-track-fields">
        <label>Starts at (s)<input type="number" min="0" step="0.05" value="${track.start}" data-pe-voice="start"></label>
        <label>Volume<input type="range" min="0" max="2" step="0.05" value="${track.volume}" data-pe-voice="volume"></label>
        <label class="pe-check"><input type="checkbox" ${track.captions ? "checked" : ""} data-pe-voice="captions"> Captions</label>
        <button type="button" class="secondary-button" data-pe-voice-here title="Move the clip to the playhead">At playhead</button>
      </div>` : ""}
    </div>`;
  }).join("") : `<p class="hint">No voice clips yet. Type a script above and generate one, or upload a recording.</p>`;
  $("#pe-voice-clear-row").hidden = !voices.length;
  pe.edit.voices.forEach((track) => peLoadCaptions(track.reference_id));

  const music = state.references.filter((reference) => PE_MUSIC_ROLES.has(reference.role) && reference.category === "audio");
  if (pe.edit.music_reference_id && !music.some((reference) => reference.id === pe.edit.music_reference_id)) pe.edit.music_reference_id = null;
  $("#pe-music-list").innerHTML = music.length ? [
    `<label class="pe-track pe-track-head"><input type="radio" name="pe-music" value="" ${pe.edit.music_reference_id ? "" : "checked"}> <span class="pe-track-title">No music</span></label>`,
    ...music.slice().reverse().map((reference) => `<div class="pe-track pe-track-head" data-music-name="${escapeHtml(`${reference.label || ""} ${reference.filename}`.toLowerCase())}">
      <input type="radio" name="pe-music" value="${reference.id}" ${pe.edit.music_reference_id === reference.id ? "checked" : ""} aria-label="Use this music">
      <button type="button" class="icon-button" data-pe-preview="${reference.id}" title="Play" aria-label="Play">${uiIcon("play")}</button>
      <span class="pe-track-title" title="${escapeHtml(reference.filename)}">${escapeHtml(reference.label || reference.filename)}</span>
      <span class="pe-track-meta">${reference.duration ? peTime(reference.duration) : ""}${reference.role === "stitch-music" ? " · from Stitch" : ""}</span>
      <button type="button" class="icon-button" data-pe-delete-ref="${reference.id}" title="Delete" aria-label="Delete">${uiIcon("trash")}</button>
    </div>`)].join("") : `<p class="hint">No music yet.</p>`;
  $("#pe-music-search").hidden = music.length < 2;
  peFilterMusic();
  $("#pe-music-controls").hidden = !pe.edit.music_reference_id;
  if (pe.edit.music_reference_id) requestAnimationFrame(peRenderMusicPart);

  $("#pe-text-list").innerHTML = pe.edit.texts.map((overlay, index) => `<div class="pe-track${pe.selected === `text:${index}` ? " is-selected" : ""}" data-text-index="${index}">
    <div class="pe-track-head"><span class="pe-track-title">Text ${index + 1}</span><button type="button" class="icon-button" data-pe-text-remove title="Remove" aria-label="Remove text">${uiIcon("trash")}</button></div>
    <textarea rows="2" maxlength="500" data-pe-text="text">${escapeHtml(overlay.text)}</textarea>
    <div class="pe-track-fields">
      <label>Start (s)<input type="number" min="0" step="0.05" value="${overlay.start}" data-pe-text="start"></label>
      <label>End (s)<input type="number" min="0" step="0.05" value="${overlay.end ?? ""}" placeholder="end" data-pe-text="end"></label>
      <label>Size<input type="number" min="8" max="300" value="${overlay.size}" data-pe-text="size"></label>
      <label>Color<input type="color" value="${escapeHtml(overlay.color)}" data-pe-text="color"></label>
      <label>Position<select data-pe-text="position">${["top", "middle", "bottom"].map((value) => `<option ${overlay.position === value ? "selected" : ""}>${value}</option>`).join("")}</select></label>
      <label>Align<select data-pe-text="align">${["left", "center", "right"].map((value) => `<option ${overlay.align === value ? "selected" : ""}>${value}</option>`).join("")}</select></label>
      <label class="pe-check"><input type="checkbox" ${overlay.outline ? "checked" : ""} data-pe-text="outline"> Outline</label>
      <label class="pe-check"><input type="checkbox" ${overlay.box ? "checked" : ""} data-pe-text="box"> Box</label>
      <label>Animation<select data-pe-text="animation">${[["none", "None"], ["fade", "Fade"], ["pop", "Pop (TikTok)"]].map(([value, label]) =>
        `<option value="${value}" ${(overlay.pop ? "pop" : overlay.fade ? "fade" : "none") === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
    </div>
  </div>`).join("");
  if (pe.source) { peRenderTimeline(); peNotes(); }
}

function pePreviewClip(id, button) {
  const reference = peReference(id);
  if (!reference) return;
  const audio = peAudioFor(reference);
  if (!audio.paused) { audio.pause(); button.innerHTML = uiIcon("play"); return; }
  audio.currentTime = reference.trim_start || 0;
  audio.volume = 1;
  audio.play().catch(() => {});
  button.innerHTML = uiIcon("pause");
  audio.onpause = () => { button.innerHTML = uiIcon("play"); };
}

// Search box over the music list: hides non-matching tracks (the chosen one and "No music" always stay).
function peFilterMusic() {
  const terms = $("#pe-music-search").value.toLowerCase().split(/\s+/).filter(Boolean);
  let shown = 0;
  $$("[data-music-name]", $("#pe-music-list")).forEach((row) => {
    const chosen = $("input[type=radio]", row)?.checked;
    row.hidden = !chosen && !terms.every((term) => row.dataset.musicName.includes(term));
    if (!row.hidden) shown += 1;
  });
  let empty = $(".pe-music-empty", $("#pe-music-list"));
  if (terms.length && !shown) {
    if (!empty) $("#pe-music-list").insertAdjacentHTML("beforeend", `<p class="hint pe-music-empty">No music matches.</p>`);
  } else empty?.remove();
}

function peBindLists() {
  $("#pe-music-search").addEventListener("input", peFilterMusic);
  const voiceList = $("#pe-voice-list"), musicList = $("#pe-music-list"), textList = $("#pe-text-list");
  const deleteRef = async (id) => {
    if (!confirm("Delete this audio clip?")) return;
    try {
      await Api.request(`${API_BASE}/api/references/${id}`, { method: "DELETE" });
      state.references = state.references.filter((reference) => reference.id !== id);
      pe.edit.voices = pe.edit.voices.filter((track) => track.reference_id !== id);
      if (pe.edit.music_reference_id === id) pe.edit.music_reference_id = null;
      peRenderLists(); peSaveDraft();
    } catch (error) { toast(error.message, "error"); }
  };
  $("#pe-voice-clear").addEventListener("click", async () => {
    const voices = state.references.filter((reference) => reference.role === "postedit-voice");
    if (!voices.length) return;
    if (!confirm(`Delete all ${voices.length} voice clip${voices.length === 1 ? "" : "s"}?\n\nThey are removed from this edit and from the server. Music and other references are kept. This can't be undone.`)) return;
    try {
      const { deleted } = await Api.request(`${API_BASE}/api/postedit/voices`, { method: "DELETE" });
      state.references = state.references.filter((reference) => reference.role !== "postedit-voice");
      pe.edit.voices = [];
      peRenderLists(); peSaveDraft();
      toast(`Deleted ${deleted} voice clip${deleted === 1 ? "" : "s"}`);
    } catch (error) { toast(error.message, "error"); }
  });
  [voiceList, musicList].forEach((list) => list.addEventListener("click", (event) => {
    const preview = event.target.closest("[data-pe-preview]");
    if (preview) return pePreviewClip(preview.dataset.pePreview, preview);
    const remove = event.target.closest("[data-pe-delete-ref]");
    if (remove) return deleteRef(remove.dataset.peDeleteRef);
    const here = event.target.closest("[data-pe-voice-here]");
    if (here) {
      const track = pe.edit.voices.find((item) => item.reference_id === here.closest("[data-voice-ref]").dataset.voiceRef);
      if (track) { track.start = +pe.clock.t.toFixed(2); peRenderLists(); peSaveDraft(); }
    }
  }));
  voiceList.addEventListener("change", (event) => {
    const row = event.target.closest("[data-voice-ref]");
    if (!row) return;
    const id = row.dataset.voiceRef;
    if (event.target.matches("[data-pe-voice-on]")) {
      if (event.target.checked) pe.edit.voices.push({ reference_id: id, start: +pe.clock.t.toFixed(2), volume: 1, captions: true });
      else pe.edit.voices = pe.edit.voices.filter((track) => track.reference_id !== id);
      peRenderLists(); peSaveDraft();
      return;
    }
    const track = pe.edit.voices.find((item) => item.reference_id === id);
    const field = event.target.dataset.peVoice;
    if (!track || !field) return;
    track[field] = field === "captions" ? event.target.checked : Math.max(0, peNum(event.target.value));
    peRenderTimeline(); peRenderOverlays(); peSaveDraft();
  });
  voiceList.addEventListener("input", (event) => {
    if (event.target.dataset.peVoice !== "volume") return;
    const track = pe.edit.voices.find((item) => item.reference_id === event.target.closest("[data-voice-ref]").dataset.voiceRef);
    if (track) { track.volume = peNum(event.target.value, 1); peSaveDraft(); }
  });
  musicList.addEventListener("change", (event) => {
    if (event.target.name !== "pe-music") return;
    peChooseMusic(event.target.value || null);
  });
  const textChange = (event) => {
    const row = event.target.closest("[data-text-index]");
    const field = event.target.dataset.peText;
    if (!row || !field) return;
    const overlay = pe.edit.texts[+row.dataset.textIndex];
    if (["outline", "box"].includes(field)) overlay[field] = event.target.checked;
    else if (field === "animation") { overlay.fade = event.target.value === "fade"; overlay.pop = event.target.value === "pop"; }
    else if (field === "end") overlay.end = event.target.value.trim() === "" ? null : Math.max(0, peNum(event.target.value));
    else if (field === "start") overlay.start = Math.max(0, peNum(event.target.value));
    else if (field === "size") overlay.size = peClamp(Math.round(peNum(event.target.value, 48)), 8, 300);
    else overlay[field] = event.target.value;
    pe.selected = `text:${row.dataset.textIndex}`;
    peRenderTimeline(); peRenderOverlays(); peSaveDraft();
  };
  textList.addEventListener("input", textChange);
  textList.addEventListener("change", textChange);
  textList.addEventListener("click", (event) => {
    if (!event.target.closest("[data-pe-text-remove]")) return;
    pe.edit.texts.splice(+event.target.closest("[data-text-index]").dataset.textIndex, 1);
    pe.selected = null;
    peRenderLists(); peRenderOverlays(); peSaveDraft();
  });
  $("#pe-text-add").addEventListener("click", () => {
    if (pe.edit.texts.length >= 20) return toast("At most 20 text overlays.", "error");
    const start = +pe.clock.t.toFixed(2);
    pe.edit.texts.push({ text: "Your text", start, end: +Math.min(peLength(), start + 3).toFixed(2), position: "bottom", align: "center", size: 48, color: "#ffffff", box: false, outline: true, fade: true, pop: false });
    pe.selected = `text:${pe.edit.texts.length - 1}`;
    peRenderLists(); peRenderOverlays(); peSaveDraft();
    $("#pe-text-list [data-text-index]:last-child textarea")?.select();
  });
  const upload = (buttonId, inputId, role, onDone) => {
    $(buttonId).addEventListener("click", () => $(inputId).click());
    $(inputId).addEventListener("change", async (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      try {
        const reference = await Api.upload(file, role);
        state.references = [...state.references.filter((item) => item.id !== reference.id), reference];
        onDone(reference);
        peRenderLists(); peSaveDraft();
      } catch (error) { toast(error.message, "error"); }
    });
  };
  upload("#pe-next-upload", "#pe-next-file", "postedit-video", (reference) => {
    pe.edit.next_reference_id = reference.id; pe.edit.next_job_id = "";
    peRenderNextVideos(true); peRefresh();
  });
  $("#pe-next-video").addEventListener("change", (event) => {
    const [kind, id] = event.target.value.split(":");
    pe.edit.next_job_id = kind === "job" ? id : "";
    pe.edit.next_reference_id = kind === "ref" ? id : "";
    $("#pe-next-delete").hidden = kind !== "ref";
    peRefresh(); peSaveDraft();
  });
  $("#pe-next-delete").addEventListener("click", async () => {
    const id = pe.edit.next_reference_id;
    if (!id) return;
    try {
      await Api.request(`${API_BASE}/api/references/${id}`, { method: "DELETE" });
      state.references = state.references.filter((reference) => reference.id !== id);
      pe.edit.next_reference_id = "";
      peRenderNextVideos(true); peRefresh(); peSaveDraft();
    } catch (error) { toast(error.message, "error"); }
  });
  upload("#pe-voice-upload", "#pe-voice-file", "postedit-voice", (reference) => pe.edit.voices.push({ reference_id: reference.id, start: +pe.clock.t.toFixed(2), volume: 1, captions: true }));
  upload("#pe-music-upload", "#pe-music-file", "postedit-music", (reference) => peChooseMusic(reference.id));
  bindUrlMusic("#pe-music-url", "#pe-music-url-add", "postedit-music", (reference) => peChooseMusic(reference.id));
}

// A link field + Download next to an uploader: the server fetches YouTube / TikTok / Instagram media
// (music slots get MP3; Studio references get the clean video without the platform watermark).
function bindUrlMusic(inputSelector, buttonSelector, role, onAdded) {
  const input = $(inputSelector), button = $(buttonSelector);
  const go = async () => {
    const url = input.value.trim();
    if (!url) return toast("Paste a YouTube, TikTok or Instagram link first.", "error");
    button.disabled = true;
    button.textContent = "Downloading…";
    toast(role === "references" ? "Downloading the video…" : "Downloading the audio…");
    try {
      const reference = await Api.request(`${API_BASE}/api/references/from-url`, { method: "POST", body: { url, role }, signal: timeoutSignal(330_000) });
      mergeReferences([reference]);
      input.value = "";
      onAdded(reference);
      toast(`Added ${reference.filename}${reference.trim_start ? ` (starts at ${Number(reference.trim_start).toFixed(1)}s, skipping silence)` : ""}.`);
    } catch (error) { toast(error.name === "TimeoutError" ? "The download timed out." : error.message, "error"); }
    finally { button.disabled = false; button.textContent = "Download"; }
  };
  button.addEventListener("click", go);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); go(); } });
}

/* ---- TTS ---- */
async function peLoadEdgeVoices() {
  if (pe.tts.edgeVoices) return pe.tts.edgeVoices;
  try {
    const { voices } = await edgeVoices();
    pe.tts.edgeVoices = voices;
  } catch (error) {
    toast(error.message, "error");
    pe.tts.edgeVoices = [];
  }
  return pe.tts.edgeVoices;
}

async function peRenderTtsProvider() {
  const provider = pe.tts.provider;
  $$("#pe-tts-provider .segment").forEach((button) => button.classList.toggle("is-active", button.dataset.provider === provider));
  $("#pe-tts-locale-row").hidden = provider !== "edge";
  $("#pe-tts-pitch-row").hidden = provider !== "edge";
  $("#pe-tiktok-settings").hidden = provider !== "tiktok";
  const select = $("#pe-tts-voice");
  // Voice lists load asynchronously: only the latest render may fill the
  // select, or a slow Edge list would overwrite the TikTok one after a switch.
  const token = pe.tts.renderToken = (pe.tts.renderToken || 0) + 1;
  const stale = () => token !== pe.tts.renderToken || pe.tts.provider !== provider;
  if (provider === "edge") {
    const voices = await peLoadEdgeVoices();
    if (stale()) return;
    const locales = [...new Set(voices.map((voice) => voice.locale))].sort();
    const localeSelect = $("#pe-tts-locale");
    localeSelect.innerHTML = locales.map((locale) => `<option ${locale === pe.tts.locale ? "selected" : ""}>${escapeHtml(locale)}</option>`).join("");
    const shown = voices.filter((voice) => voice.locale === pe.tts.locale);
    select.innerHTML = shown.map((voice) => `<option value="${escapeHtml(voice.id)}">${escapeHtml(`${voice.label} · ${voice.gender}${voice.traits ? ` · ${voice.traits}` : ""}`)}</option>`).join("");
    if (shown.some((voice) => voice.id === pe.tts.voice.edge)) select.value = pe.tts.voice.edge;
    else if (shown[0]) pe.tts.voice.edge = shown[0].id;
  } else {
    if (!pe.tts.tiktokVoices) {
      try { pe.tts.tiktokVoices = (await Api.request(`${API_BASE}/api/tts/voices?provider=tiktok`)).voices; } catch { pe.tts.tiktokVoices = []; }
    }
    if (stale()) return;
    const groups = [...new Set(pe.tts.tiktokVoices.map((voice) => voice.group))];
    select.innerHTML = groups.map((group) => `<optgroup label="${escapeHtml(group)}">${pe.tts.tiktokVoices.filter((voice) => voice.group === group)
      .map((voice) => `<option value="${escapeHtml(voice.id)}">${escapeHtml(voice.label)}</option>`).join("")}</optgroup>`).join("");
    select.value = pe.tts.voice.tiktok;
    peLoadTiktokSettings();
  }
}

function peRenderTiktokSettings() {
  const settings = pe.tts.settings;
  if (!settings) return;
  $$("#pe-tiktok-mode .segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === settings.tiktok_mode));
  $("#pe-tiktok-session-row").hidden = settings.tiktok_mode !== "direct";
  $("#pe-tiktok-proxy-row").hidden = settings.tiktok_mode !== "proxy";
  $("#pe-tiktok-proxy").value = settings.tiktok_proxy_url || "";
  $("#pe-tiktok-session").placeholder = settings.tiktok_session_set ? `saved (…${settings.tiktok_session_hint})` : "paste the sessionid value";
  $("#pe-tiktok-session-clear").hidden = !settings.tiktok_session_set;
  $("#pe-tiktok-fallback-row").hidden = settings.tiktok_mode === "public";
  $("#pe-tiktok-fallback").checked = settings.tiktok_fallback !== false;
  $("#pe-tiktok-state").textContent = settings.tiktok_mode === "public" ? "· Public service (no login)"
    : settings.tiktok_mode === "direct" ? (settings.tiktok_session_set ? "· Direct, session saved" : "· Direct, needs a session id")
    : "· Custom proxy";
}

async function peLoadTiktokSettings() {
  if (pe.tts.settings) return peRenderTiktokSettings();
  try { pe.tts.settings = await Api.request(`${API_BASE}/api/tts/settings`); } catch { return; }
  peRenderTiktokSettings();
}

async function peSaveTiktokSettings(changes) {
  try {
    pe.tts.settings = await Api.request(`${API_BASE}/api/tts/settings`, { method: "PUT", body: changes });
    $("#pe-tiktok-session").value = "";
    peRenderTiktokSettings();
    toast("TikTok settings saved.");
  } catch (error) { toast(error.message, "error"); }
}

function peTtsBody(text) {
  return {
    provider: pe.tts.provider, voice: $("#pe-tts-voice").value, text,
    rate: parseInt($("#pe-tts-rate").value, 10), pitch: pe.tts.provider === "edge" ? parseInt($("#pe-tts-pitch").value, 10) : 0,
  };
}

async function peGenerateVoice() {
  const text = $("#pe-tts-text").value.trim();
  if (!text) return toast("Write the script first.", "error");
  const button = $("#pe-tts-generate"), status = $("#pe-tts-status");
  button.disabled = true;
  status.textContent = "Generating…";
  const started = performance.now();
  try {
    const reference = await Api.request(`${API_BASE}/api/tts`, { method: "POST", body: peTtsBody(text) });
    state.references = [...state.references.filter((item) => item.id !== reference.id), reference];
    pe.captions[reference.id] = peParseSrt(reference.captions);
    if (reference.words) pe.words[reference.id] = reference.words;
    pe.edit.voices.push({ reference_id: reference.id, start: +pe.clock.t.toFixed(2), volume: 1, captions: true });
    status.textContent = `Added a ${reference.duration?.toFixed(1) ?? "?"}s clip at ${peTime(pe.clock.t)} (${((performance.now() - started) / 1000).toFixed(1)}s).`;
    peRenderLists(); peSaveDraft();
    if (reference.duration && pe.clock.t + reference.duration > peLength() + 0.05) {
      status.textContent += " It runs past the end of the video: press Fit voice to hold the last frame.";
    }
  } catch (error) {
    status.textContent = "";
    toast(error.message, "error");
  } finally { button.disabled = false; }
}

async function peSampleVoice() {
  const button = $("#pe-tts-sample");
  button.disabled = true;
  try {
    const reference = await Api.request(`${API_BASE}/api/tts`, { method: "POST", body: { ...peTtsBody("This is how this voice sounds in your recap."), label: "voice sample" } });
    const audio = new Audio(reference.preview_url);
    const cleanup = () => Api.request(`${API_BASE}/api/references/${reference.id}`, { method: "DELETE" }).catch(() => {});
    audio.onended = cleanup;
    audio.onerror = cleanup;
    await audio.play();
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
}

/* ---- render ---- */
function peRequestBody() {
  const edit = pe.edit, crop = peCropRect();
  const body = { ...edit, source_job_id: pe.sourceId, crop: crop ? { x: +crop.x.toFixed(5), y: +crop.y.toFixed(5), w: +crop.w.toFixed(5), h: +crop.h.toFixed(5) } : null };
  body.editor = { aspect: edit.aspect, zoom: edit.zoom, pan_x: edit.pan_x, pan_y: edit.pan_y };
  ["aspect", "zoom", "pan_x", "pan_y"].forEach((key) => delete body[key]);
  if (!body.fps) body.interpolate = false;
  body.texts = edit.texts.filter((overlay) => overlay.text.trim()).map((overlay) => ({ ...overlay, text: overlay.text.slice(0, 500) }));
  return body;
}

async function peRender() {
  if (!pe.sourceId) return;
  const sourceId = pe.sourceId;
  const button = $("#pe-render");
  button.disabled = true;
  try {
    if (pe.edit.captions && pe.edit.caption_source === "whisper") {
      button.textContent = "Transcribing…";
      await peEnsureWhisper();
    }
    if (sourceId !== pe.sourceId) throw new Error("The source changed while transcribing. Review the edit and render again.");
    button.textContent = "Rendering…";
    const job = await Api.request(`${API_BASE}/api/postedit`, { method: "POST", body: peRequestBody() });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    toast("Rendering your edit. It shows up in the Studio's generations list as a Post-Edit job.");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = !pe.sourceId; button.textContent = "Render edit"; }
}

/* ---- wiring ---- */
function loadPostEdit() {
  if (!pe.bound) {
    pe.bound = true;
    bindPostEdit();
    const draftSource = peLoadDraft();
    peRenderSources();
    peSyncForm();
    peRenderLists();
    if (draftSource && !pe.skipDraftSource && peSourceJobs().some((job) => job.id === draftSource)) peSelectSource(draftSource, { keepTrim: true });
    pe.skipDraftSource = false;
    return;
  }
  peRenderSources();
  peRenderLists();
  if (pe.source) requestAnimationFrame(peRefresh);
}

function leavePostEdit() {
  if (!pe.bound) return;
  peStop();
  peMusicListenStop();
}

function bindPostEdit() {
  $("#pe-look-sliders").innerHTML = PE_LOOK_SLIDERS.map(([field, label, min, max, step, value]) => `<div class="field-row">
    <label class="pe-slider-label" for="pe-look-${field}"><span>${label} <span class="field-readout" id="pe-look-${field}-readout"></span></span><button type="button" data-pe-look-reset="${field}" title="Reset">reset</button></label>
    <input id="pe-look-${field}" type="range" min="${min}" max="${max}" step="${step}" value="${value}">
  </div>`).join("");
  hydrateIcons($("#postedit-view"));
  const controls = $("#pe-controls");
  controls.addEventListener("input", (event) => { if (event.target.id?.startsWith("pe-") && !event.target.closest(".pe-track-list, #pe-tts-text, #pe-tiktok-settings")) peChanged(); });
  controls.addEventListener("change", (event) => { if (event.target.id?.startsWith("pe-") && !event.target.closest(".pe-track-list, #pe-tiktok-settings") && !["pe-tts-voice", "pe-tts-locale"].includes(event.target.id)) peChanged(); });
  $("#pe-caption-kind").addEventListener("click", (event) => {
    const style = event.target.closest("[data-caption-style]")?.dataset.captionStyle;
    if (!style || style === pe.edit.caption_style) return;
    pe.edit.caption_style = style;
    if (style === "pop" && pe.edit.caption_size === 22 && pe.edit.caption_position === "bottom") {
      pe.edit.caption_size = 30;
      pe.edit.caption_position = "middle";
    } else if (style === "classic" && pe.edit.caption_size === 30 && pe.edit.caption_position === "middle") {
      pe.edit.caption_size = 22;
      pe.edit.caption_position = "bottom";
    }
    peSyncForm(); peRefresh(); peSaveDraft();
  });
  $("#pe-whisper-transcribe").addEventListener("click", () => { peEnsureWhisper().catch(() => {}); });
  $("#pe-look-sliders").addEventListener("click", (event) => {
    const field = event.target.dataset.peLookReset;
    if (!field) return;
    $(`#pe-look-${field}`).value = PE_LOOK_SLIDERS.find(([name]) => name === field)[5];
    peChanged();
  });
  $("#pe-look-presets").addEventListener("click", (event) => {
    const look = event.target.closest("[data-look]")?.dataset.look;
    if (!look) return;
    PE_LOOK_SLIDERS.forEach(([field, , , , , value]) => { pe.edit[field] = PE_LOOKS[look][field] ?? value; });
    for (const [field, value] of Object.entries(PE_LOOK_FX)) pe.edit[field] = PE_LOOKS[look][field] ?? value;
    $$("#pe-look-presets [data-look]").forEach((button) => button.classList.toggle("is-active", button.dataset.look === look));
    peSyncForm(); peRefresh(); peSaveDraft();
  });
  $("#pe-letterbox").addEventListener("click", (event) => {
    const value = event.target.closest("[data-letterbox]")?.dataset.letterbox;
    if (!value) return;
    pe.edit.letterbox = value;
    peSyncForm(); peRefresh(); peSaveDraft();
  });
  // "reset" links next to the effect/audio sliders: data-pe-reset="element-id:default".
  $("#pe-controls").addEventListener("click", (event) => {
    const spec = event.target.closest("[data-pe-reset]")?.dataset.peReset;
    if (!spec) return;
    event.preventDefault();
    const [id, value] = spec.split(":");
    $(`#${id}`).value = value;
    peChanged();
  });
  $("#pe-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-pe-tab]");
    if (tab) peShowTab(tab.dataset.peTab);
  });
  $("#pe-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const tabs = $$("#pe-tabs [data-pe-tab]"), at = tabs.findIndex((tab) => tab.classList.contains("is-active"));
    const next = tabs[(at + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    peShowTab(next.dataset.peTab); next.focus(); event.preventDefault();
  });
  let savedTab = "edit";
  try { savedTab = localStorage.getItem(PE_TAB_KEY) || "edit"; } catch { /* storage may be off */ }
  peShowTab(savedTab);
  $("#pe-aspect").addEventListener("click", (event) => {
    const aspect = event.target.closest("[data-aspect]")?.dataset.aspect;
    if (!aspect) return;
    pe.edit.aspect = aspect;
    if (aspect === "free" && pe.edit.zoom === 1) pe.edit.zoom = 1.25;
    peSyncForm(); peRefresh(); peSaveDraft();
  });
  $("#pe-size-preset").addEventListener("change", (event) => {
    const [width, height] = event.target.value ? event.target.value.split("x").map(Number) : [null, null];
    $("#pe-width").value = width ?? "";
    $("#pe-height").value = height ?? "";
    peChanged();
  });
  $$("[data-pe-mark]").forEach((button) => button.addEventListener("click", () => {
    const video = $("#pe-video");
    const time = +video.currentTime.toFixed(3);
    if (button.dataset.peMark === "start") pe.edit.trim_start = Math.min(time, peTrim().end - 0.1);
    else pe.edit.trim_end = Math.max(time, peTrim().start + 0.1);
    peSyncForm(); peRefresh(); peSaveDraft();
  }));
  $("#pe-fit-voice").addEventListener("click", () => {
    const ends = pe.edit.voices.map((track) => track.start + (peReference(track.reference_id)?.duration || 0));
    if (!ends.length) return toast("Add a voice clip first.", "error");
    pe.edit.hold_end = +Math.max(0, Math.max(...ends) + 0.4 - peSegment()).toFixed(2);
    peSyncForm(); peRefresh(); peSaveDraft();
    toast(pe.edit.hold_end ? `Holding the last frame for ${pe.edit.hold_end}s.` : "The video is already long enough for every voice clip.");
  });
  $("#pe-source").addEventListener("change", (event) => peSelectSource(event.target.value));
  $("#pe-play").addEventListener("click", () => (pe.clock.playing ? peStop() : pePlay()));
  $("#pe-render").addEventListener("click", peRender);
  $("#pe-reset").addEventListener("click", () => {
    if (!confirm("Reset every Post-Edit setting to its default? The source and your voice clips stay.")) return;
    pe.edit = peDefaults();
    pe.selected = null;
    peSyncForm(); peRenderLists(); peRefresh(); peSaveDraft();
  });
  const video = $("#pe-video");
  video.addEventListener("loadedmetadata", () => { peSeek(0); peRefresh(); });
  video.addEventListener("seeked", () => { if (!pe.clock.playing) { peRenderOverlays(); pePositionPlayhead(); } });
  window.addEventListener("resize", () => { if (document.body.dataset.view === "postedit" && pe.source) { peRefresh(); peRenderMusicPart(); } });
  document.addEventListener("keydown", (event) => {
    if (document.body.dataset.view !== "postedit" || event.code !== "Space" || !pe.source) return;
    if (event.target.closest("input, textarea, select, button")) return;
    event.preventDefault();
    if (pe.clock.playing) peStop(); else pePlay();
  });
  peBindTimeline();
  peBindCropDrag();
  peBindLists();
  peBindMusicPart();
  $("#pe-reedit-button").addEventListener("click", () => peReeditJob(pe.sourceId));

  $("#pe-tts-provider").addEventListener("click", (event) => {
    const provider = event.target.closest("[data-provider]")?.dataset.provider;
    if (!provider) return;
    pe.tts.provider = provider;
    peRenderTtsProvider(); peSaveDraft();
  });
  $("#pe-tts-locale").addEventListener("change", (event) => { pe.tts.locale = event.target.value; peRenderTtsProvider(); peSaveDraft(); });
  $("#pe-tts-voice").addEventListener("change", (event) => { pe.tts.voice[pe.tts.provider] = event.target.value; peSaveDraft(); });
  const rateReadout = () => {
    const rate = parseInt($("#pe-tts-rate").value, 10), pitch = parseInt($("#pe-tts-pitch").value, 10);
    $("#pe-tts-rate-readout").textContent = `${rate >= 0 ? "+" : ""}${rate}%`;
    $("#pe-tts-pitch-readout").textContent = `${pitch >= 0 ? "+" : ""}${pitch}Hz`;
  };
  $("#pe-tts-rate").addEventListener("input", rateReadout);
  $("#pe-tts-pitch").addEventListener("input", rateReadout);
  rateReadout();
  $("#pe-tts-text").addEventListener("input", (event) => { $("#pe-tts-count").textContent = `${event.target.value.length} / 5000`; });
  $("#pe-tts-generate").addEventListener("click", peGenerateVoice);
  $("#pe-tts-sample").addEventListener("click", peSampleVoice);
  $("#pe-tiktok-mode").addEventListener("click", (event) => {
    const mode = event.target.closest("[data-mode]")?.dataset.mode;
    if (mode) peSaveTiktokSettings({ tiktok_mode: mode });
  });
  $("#pe-tiktok-save").addEventListener("click", () => {
    const changes = { tiktok_proxy_url: $("#pe-tiktok-proxy").value };
    const session = $("#pe-tiktok-session").value.trim();
    if (session) changes.tiktok_session_id = session;
    peSaveTiktokSettings(changes);
  });
  $("#pe-tiktok-session-clear").addEventListener("click", () => peSaveTiktokSettings({ tiktok_session_id: "" }));
  $("#pe-tiktok-fallback").addEventListener("change", (event) => peSaveTiktokSettings({ tiktok_fallback: event.target.checked }));
}

/* ------------------------------------------------------------ settings -- */
// Settings tab: download processing (backend/downloads.py). Every [data-ds]
// control maps to a dotted path in the saved settings; changes save
// themselves (debounced) and refresh the watermark preview.
const ds = { bound: false, settings: null, saveTimer: null, previewTimer: null, previewUrl: null, voicesLoaded: false };

function dsGet(path) { return path.split(".").reduce((node, key) => node?.[key], ds.settings); }
function dsSet(path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((node, key) => node[key], ds.settings)[last] = value;
}

function dsFill() {
  $$("#settings-view [data-ds]").forEach((element) => {
    const value = dsGet(element.dataset.ds);
    if (element.type === "checkbox") element.checked = Boolean(value);
    else element.value = value ?? "";
  });
  dsReadouts();
}

function dsReadouts() {
  $$("#settings-view [data-ds-readout]").forEach((element) => {
    const value = Number(dsGet(element.dataset.dsReadout));
    const scale = Number(element.dataset.scale || 1);
    const shown = value * scale;
    element.innerHTML = `${Number.isInteger(shown) ? shown : shown.toFixed(shown < 1 ? 1 : 1).replace(/\.0$/, "")}${element.dataset.unit || ""}`;
  });
  ["text", "logo"].forEach((kind) => {
    const position = dsGet(`${kind}.position`);
    $$(`[data-ds-moving="${kind}"]`).forEach((row) => { row.hidden = row.dataset.dsMove !== position; });
    $$(`[data-ds-static="${kind}"]`).forEach((row) => { row.hidden = position === "bounce" || position === "jump"; });
  });
  const tagSource = dsGet("audio_tag.source");
  $$("[data-ds-tag-source]").forEach((row) => { row.hidden = row.dataset.dsTagSource !== tagSource; });
  $("#settings-view .ds-controls").classList.toggle("is-off", !ds.settings.enabled);
  const example = ds.settings.filename_pattern.replace(/\{(\w+)\}/g, (match, key) => ({
    model: "minimax_h3", date: "20260926-173000", id: "3fa9c1d2", prompt: "a-knight-in-the-rain", duration: "15s", size: "1280x704",
  }[key] ?? match));
  $("#ds-pattern-example").textContent = `e.g. ${example}.mp4`;
}

function dsVisualOn() {
  const s2 = ds.settings;
  if (!s2) return false; // the settings didn't load (offline) - nothing to preview yet
  return (s2.text.enabled && s2.text.text.trim()) || (s2.logo.enabled && ds.logoUrl) || (s2.tiled.enabled && s2.tiled.text.trim());
}

async function loadDownloadSettings() {
  try {
    const result = await Api.request(`${API_BASE}/api/settings/downloads`);
    ds.settings = result.settings;
    dsSetLogo(result.logo_url);
    dsSetOutro(result.outro);
    dsSetTagFile(result.audio_tag_file);
    dsFill();
    const newest = state.jobs.find((job) => job.state === "complete" && job.download_url);
    const link = $("#ds-test-download");
    link.hidden = !newest;
    if (newest) link.href = newest.download_url;
    if (newest?.duration) $("#ds-preview-at").max = Math.max(0.5, newest.duration);
    dsPreview();
    if (!ds.voicesLoaded) {
      ds.voicesLoaded = true;
      try {
        const { voices } = await edgeVoices();
        $("#ds-tag-voice").innerHTML = voices.map((voice) => `<option value="${escapeHtml(voice.id)}">${escapeHtml(`${voice.locale} · ${voice.label} (${voice.gender})`)}</option>`).join("");
        $("#ds-tag-voice").value = ds.settings.audio_tag.voice;
      } catch { ds.voicesLoaded = false; }
    }
  } catch (error) { toast(error.message, "error"); }
}

function dsSetLogo(url) {
  ds.logoUrl = url;
  const img = $("#ds-logo-img");
  img.hidden = !url;
  if (url) img.src = url;
  $("#ds-logo-remove").hidden = !url;
}

// The outro video's player + "4.0 s · 1920×1080 · with sound" line.
function dsSetOutro(outro) {
  ds.outro = outro;
  const video = $("#ds-outro-video");
  video.hidden = !outro;
  if (outro && !video.src.endsWith(outro.url)) video.src = outro.url;
  if (!outro) video.removeAttribute("src");
  $("#ds-outro-remove").hidden = !outro;
  $("#ds-outro-upload").textContent = outro ? "Replace video…" : "Upload video…";
  $("#ds-outro-info").textContent = outro
    ? `${outro.duration.toFixed(1)} s · ${outro.width}×${outro.height} · ${outro.has_audio ? "with sound" : "no sound"}. Resized to each video's size and frame rate on download.`
    : "MP4, MOV, WEBM or MKV, up to 60 s and 50 MB.";
}

// The uploaded audio-tag sound: player + length line.
// Post-Edit and Settings both list Edge voices (35 KB); fetch once per page.
let edgeVoicesFetch = null;
function edgeVoices() {
  edgeVoicesFetch ??= Api.request(`${API_BASE}/api/tts/voices?provider=edge`).catch((error) => { edgeVoicesFetch = null; throw error; });
  return edgeVoicesFetch;
}

function dsSetTagFile(file) {
  ds.tagFile = file;
  const audio = $("#ds-tag-audio");
  audio.hidden = !file;
  if (file && !audio.src.endsWith(file.url)) audio.src = file.url;
  if (!file) audio.removeAttribute("src");
  $("#ds-tag-remove").hidden = !file;
  $("#ds-tag-upload").textContent = file ? "Replace sound…" : "Upload sound…";
  $("#ds-tag-info").textContent = file
    ? `${file.duration.toFixed(1)} s. Mixed in at the chosen spot; a sound longer than the video is cut off at its end.`
    : "MP3, WAV, M4A, AAC, OGG, OPUS or FLAC, up to 60 s and 20 MB.";
}

function dsSaveState(text, error = false) {
  const node = $("#ds-save-state");
  node.textContent = text;
  node.classList.toggle("is-error", error);
}

function dsScheduleSave() {
  dsSaveState("Saving…");
  clearTimeout(ds.saveTimer);
  const edit = ds.edits = (ds.edits || 0) + 1;
  ds.saveTimer = setTimeout(async () => {
    ds.saveTimer = null;
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads`, { method: "PUT", body: ds.settings });
      // Only take the server's (clamped) copy if nothing changed while this save was
      // in flight - otherwise it would undo the newer edit (their own save follows).
      if (edit !== ds.edits) return;
      ds.settings = result.settings;
      dsSaveState("Saved");
      dsPreview();
    } catch (error) { dsSaveState(error.message, true); }
  }, 450);
}

function dsPreview() {
  clearTimeout(ds.previewTimer);
  if (!ds.settings) return;
  ds.previewTimer = setTimeout(async () => {
    const box = $(".ds-preview"), img = $("#ds-preview-img"), empty = $("#ds-preview-empty");
    const visual = dsVisualOn();
    empty.hidden = Boolean(visual);
    empty.textContent = visual ? "" : "Turn on a text, logo or tiled watermark to see it here (audio and metadata don't show in a picture).";
    box.classList.add("is-loading");
    try {
      const at = $("#ds-preview-at").value;
      const response = await fetch(`${API_BASE}/api/settings/downloads/preview?at=${encodeURIComponent(at)}`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || `Preview failed (${response.status})`);
      }
      const blob = await response.blob();
      if (ds.previewUrl) URL.revokeObjectURL(ds.previewUrl);
      ds.previewUrl = URL.createObjectURL(blob);
      img.src = ds.previewUrl;
      $("#ds-preview-note").textContent = ds.settings.enabled ? "" : "Download processing is off: downloads are the untouched originals until you turn it on.";
    } catch (error) {
      $("#ds-preview-note").textContent = error.message;
    } finally { box.classList.remove("is-loading"); }
  }, 250);
}

function bindDownloadSettings() {
  const view = $("#settings-view");
  const sectionLinks = [...view.querySelectorAll("[data-settings-link]")];
  const updateSettingsIndex = () => {
    if (view.hidden) return;
    let activeId = sectionLinks[0]?.dataset.settingsLink;
    for (const link of sectionLinks) {
      const section = document.getElementById(link.dataset.settingsLink);
      if (section && section.getBoundingClientRect().top <= 180) activeId = section.id;
    }
    for (const link of sectionLinks) {
      const active = link.dataset.settingsLink === activeId;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
  };
  for (const link of sectionLinks) link.addEventListener("click", () => {
    sectionLinks.forEach((item) => item.classList.toggle("is-active", item === link));
  });
  window.addEventListener("scroll", updateSettingsIndex, { passive: true });
  updateSettingsIndex();
  const onChange = (event) => {
    const element = event.target.closest("[data-ds]");
    if (!element || !ds.settings) return;
    let value = element.type === "checkbox" ? element.checked : element.value;
    if (element.type === "range" || element.type === "number") value = Number(value);
    dsSet(element.dataset.ds, value);
    dsReadouts();
    dsScheduleSave();
  };
  view.addEventListener("input", onChange);
  view.addEventListener("change", onChange);
  $("#ds-preview-at").addEventListener("input", (event) => { $("#ds-preview-at-readout").textContent = `${Number(event.target.value).toFixed(2).replace(/0$/, "")}s`; dsPreview(); });
  $("#ds-preview-refresh").addEventListener("click", dsPreview);
  $("#ds-logo-upload").addEventListener("click", () => $("#ds-logo-file").click());
  $("#ds-logo-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/logo`, { method: "POST", body });
      dsSetLogo(result.logo_url);
      if (!ds.settings.logo.enabled) { ds.settings.logo.enabled = true; dsFill(); dsScheduleSave(); }
      dsPreview();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#ds-tag-upload").addEventListener("click", () => $("#ds-tag-file").click());
  $("#ds-tag-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const button = $("#ds-tag-upload");
    button.disabled = true;
    button.textContent = "Uploading…";
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/audio-tag`, { method: "POST", body });
      dsSetTagFile(result.audio_tag_file);
      // Uploading a sound means "use it".
      if (!ds.settings.audio_tag.enabled || ds.settings.audio_tag.source !== "file") {
        Object.assign(ds.settings.audio_tag, { enabled: true, source: "file" });
        dsFill();
        dsScheduleSave();
      }
      toast("Sound saved as the audio watermark.");
    } catch (error) {
      toast(error.message, "error");
      dsSetTagFile(ds.tagFile);
    } finally { button.disabled = false; }
  });
  $("#ds-tag-remove").addEventListener("click", async () => {
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/audio-tag`, { method: "DELETE" });
      dsSetTagFile(result.audio_tag_file);
    } catch (error) { toast(error.message, "error"); }
  });
  $("#ds-outro-upload").addEventListener("click", () => $("#ds-outro-file").click());
  $("#ds-outro-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const button = $("#ds-outro-upload");
    button.disabled = true;
    button.textContent = "Uploading…";
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/outro`, { method: "POST", body });
      dsSetOutro(result.outro);
      if (!ds.settings.outro.enabled) { ds.settings.outro.enabled = true; dsFill(); dsScheduleSave(); }
      toast("Outro video saved - it's appended to downloads while \"Process downloads\" is on.");
    } catch (error) {
      toast(error.message, "error");
      dsSetOutro(ds.outro);
    } finally { button.disabled = false; }
  });
  $("#ds-outro-remove").addEventListener("click", async () => {
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/outro`, { method: "DELETE" });
      dsSetOutro(result.outro);
    } catch (error) { toast(error.message, "error"); }
  });
  $("#ds-logo-remove").addEventListener("click", async () => {
    try {
      const result = await Api.request(`${API_BASE}/api/settings/downloads/logo`, { method: "DELETE" });
      dsSetLogo(result.logo_url);
      dsPreview();
    } catch (error) { toast(error.message, "error"); }
  });
}

function loadSettingsView() {
  if (!ds.bound) { ds.bound = true; bindDownloadSettings(); }
  loadDownloadSettings();
}

/* --------------------------------------------------------- enhancement -- */
// The Enhancement tab (backend/enhancer.py). Text fields save themselves
// (debounced); the API key is sent only when typed and never comes back.

let enhanceSaveTimer = null;
let enhanceLoaded = false;
let enhanceLogJobId = null;
let enhanceLogRenderedId = null;
let enhanceLogRenderedCount = -1;
let enhanceLogRenderedPhase = null;
let enhanceLogFollow = true; // false once the reader scrolls up; true again at the bottom
let enhanceLogRenderedState = null;
let enhanceLogRenderedRevision = -1;
let enhanceLogBusy = false;

async function loadEnhancement() {
  try {
    state.enhancement = await Api.request(`${API_BASE}/api/enhancement`);
  } catch (error) {
    enhanceSaveState(`Couldn't load: ${error.message}`, true);
    return;
  }
  await testEnhancement();
  if (document.body.dataset.view !== "enhance") return;
  fillEnhancementForm();
  enhanceLoaded = true;
  refreshEnhancementLog();
}

function fillEnhancementForm() {
  const config = state.enhancement;
  const modelSelect = $("#enhance-model");
  if (config.model && ![...modelSelect.options].some((option) => option.value === config.model)) {
    const option = new Option(`${config.model} (saved; unavailable)`, config.model);
    modelSelect.add(option);
  }
  modelSelect.value = config.model;
  $("#enhance-base-url").value = config.base_url;
  for (const key of ["top_p", "top_k", "min_p", "repeat_penalty", "seed"]) $(`#enhance-${key.replaceAll("_", "-")}`).value = config[key];
  $("#enhance-extra-body").value = JSON.stringify(config.extra_body || {}, null, 2);
  $("#enhance-temperature").value = config.temperature;
  $("#enhance-thinking-level").value = config.thinking_level;
  if (!$("#agentic-thinking-research")) buildAgenticThinkingStages();
  for (const [key] of AGENTIC_THINKING_STAGES) $(`#agentic-thinking-${key}`).value = config[`agentic_thinking_${key}`] || "inherit";
  $("#enhance-context-size").value = config.context_size;
  $("#enhance-instructions").value = config.instructions;
  $("#agentic-browser-enabled").checked = Boolean(config.agentic_browser_enabled);
  $("#agentic-video-search").checked = Boolean(config.agentic_video_search);
  $("#agentic-manage-references").checked = Boolean(config.agentic_manage_references);
  $("#agentic-character-screenshots").checked = Boolean(config.agentic_character_screenshots);
  $("#agentic-lora-gallery").checked = Boolean(config.agentic_lora_gallery);
  $("#agentic-continuation-tail").checked = Boolean(config.agentic_continuation_tail);
  $("#agentic-continuation-tail-audio").checked = Boolean(config.agentic_continuation_tail_audio);
  $("#agentic-scene-screenshots").checked = Boolean(config.agentic_scene_screenshots);
  $("#agentic-rushed-dialogue-check").checked = Boolean(config.agentic_rushed_dialogue_check);
  $("#agentic-stage-enabled").checked = Boolean(config.agentic_stage_enabled);
  $("#agentic-stage-review").checked = Boolean(config.agentic_stage_review);
  $("#agentic-stage-approval").checked = Boolean(config.agentic_stage_approval);
  $("#agentic-whisper-check").checked = Boolean(config.agentic_whisper_check);
  for (const [id, key] of AGENTIC_REVIEW_OPTIONS) $(id).checked = Boolean(config[key]);
  $("#agentic-review-cap").checked = config.agentic_review_cap ?? true;
  syncAgenticSettingGroups();
  $("#agentic-final-review").checked = Boolean(config.agentic_final_review);
  $("#agentic-review-rounds").value = config.agentic_review_rounds ?? 1;
  $("#agentic-refinement-prompt").value = config.agentic_refinement_prompt || "";
  renderAgenticPresetOptions();
}

function renderAgenticPresetOptions() {
  const names = Object.keys((state.presets || {}).minimax_h3 || {}).sort((a, b) => a.localeCompare(b));
  for (const [selector, saved, blank] of [
    ["#agentic-stage-preset", state.enhancement?.agentic_stage_preset, "Choose a saved MiniMax H3 preset"],
    ["#agentic-full-preset", state.enhancement?.agentic_full_preset, "Use current Studio settings"],
  ]) {
    const select = $(selector);
    select.replaceChildren(new Option(blank, ""), ...names.map((name) => new Option(name, name)));
    if (saved && !names.includes(saved)) select.add(new Option(`${saved} (missing)`, saved));
    select.value = saved || "";
  }
}

function enhanceSaveState(text, error = false) {
  const node = $("#enhance-save-state");
  node.textContent = text;
  node.classList.toggle("is-error", error);
}

function scheduleEnhancementSave() {
  enhanceSaveState("Unsaved changes…");
  clearTimeout(enhanceSaveTimer);
  enhanceSaveTimer = setTimeout(() => saveEnhancement(), 700);
}

async function saveEnhancement() {
  clearTimeout(enhanceSaveTimer);
  if (!enhanceLoaded) return false;
  let extraBody;
  try { extraBody = JSON.parse($("#enhance-extra-body").value || "{}"); }
  catch { enhanceSaveState("Extra settings must be valid JSON", true); return false; }
  const body = {
    model: $("#enhance-model").value,
    temperature: Number($("#enhance-temperature").value), thinking_level: $("#enhance-thinking-level").value,
    top_p: Number($("#enhance-top-p").value), top_k: Number($("#enhance-top-k").value),
    min_p: Number($("#enhance-min-p").value), repeat_penalty: Number($("#enhance-repeat-penalty").value),
    seed: Number($("#enhance-seed").value), extra_body: extraBody,
    context_size: Number($("#enhance-context-size").value),
    instructions: $("#enhance-instructions").value,
    agentic_browser_enabled: $("#agentic-browser-enabled").checked,
    agentic_video_search: $("#agentic-video-search").checked,
    agentic_manage_references: $("#agentic-manage-references").checked,
    agentic_character_screenshots: $("#agentic-character-screenshots").checked,
    agentic_lora_gallery: $("#agentic-lora-gallery").checked,
    agentic_continuation_tail: $("#agentic-continuation-tail").checked,
    agentic_continuation_tail_audio: $("#agentic-continuation-tail-audio").checked,
    agentic_scene_screenshots: $("#agentic-scene-screenshots").checked,
    agentic_rushed_dialogue_check: $("#agentic-rushed-dialogue-check").checked,
    agentic_stage_enabled: $("#agentic-stage-enabled").checked,
    agentic_stage_preset: $("#agentic-stage-preset").value,
    agentic_full_preset: $("#agentic-full-preset").value,
    agentic_stage_review: $("#agentic-stage-review").checked,
    agentic_stage_approval: $("#agentic-stage-enabled").checked && $("#agentic-stage-approval").checked,
    agentic_whisper_check: $("#agentic-stage-enabled").checked && $("#agentic-whisper-check").checked,
    ...Object.fromEntries(AGENTIC_REVIEW_OPTIONS.map(([id, key]) => [key, $(id).checked])),
    agentic_final_review: $("#agentic-final-review").checked,
    agentic_review_cap: $("#agentic-review-cap").checked,
    agentic_review_rounds: Math.max(0, Math.min(10, Math.round(Number($("#agentic-review-rounds").value || 1)))),
    ...Object.fromEntries(AGENTIC_THINKING_STAGES.map(([key]) => [`agentic_thinking_${key}`, $(`#agentic-thinking-${key}`).value])),
    agentic_refinement_prompt: $("#agentic-refinement-prompt").value,
  };
  enhanceSaveState("Saving…");
  try {
    state.enhancement = await Api.request(`${API_BASE}/api/enhancement`, { method: "PUT", body });
    enhanceSaveState("Saved"); renderStatus(); return true;
  } catch (error) { enhanceSaveState(error.message, true); return false; }
}

async function testEnhancement() {
  const result = $("#enhance-test-result");
  result.textContent = "Checking…";
  try {
    const reply = await Api.request(`${API_BASE}/api/enhancement/models`);
    const models = reply.models;
    state.llamaOnline = models.length > 0; renderStatus();
    result.textContent = `${models.length} Llama model(s) available.`;
    const select = $("#enhance-model");
    const selected = select.value || state.enhancement?.model || "";
    const options = models.map((model) => new Option(model, model));
    if (selected && !models.includes(selected)) {
      options.unshift(new Option(`${selected} (saved; unavailable)`, selected));
    }
    select.replaceChildren(...options);
    select.value = selected && [...select.options].some((option) => option.value === selected)
      ? selected : (models[0] || "");
  } catch (error) { state.llamaOnline = false; renderStatus(); result.textContent = error.message; }
}

async function refreshEnhancePreview() {
  const body = enhanceRequestBody();
  if (!body.prompt.trim()) body.prompt = "(your current prompt goes here)";
  try {
    const preview = await Api.request(`${API_BASE}/api/enhancement/preview`, { method: "POST", body });
    $("#enhance-preview").textContent = preview.messages.map((message) => `── ${message.role.toUpperCase()} ──\n${message.content}`).join("\n\n");
    $("#enhance-preview-size").textContent = `${preview.characters.toLocaleString()} characters · about ${Math.round(preview.characters / 3.5).toLocaleString()} input tokens`;
  } catch (error) {
    $("#enhance-preview").textContent = error.message;
  }
}

async function refreshEnhancementLog() {
  if (enhanceLogBusy || document.body.dataset.view !== "enhance") return;
  enhanceLogBusy = true;
  try {
    const { jobs } = await Api.request(`${API_BASE}/api/enhancement/jobs`, { cache: "no-store" });
    const picker = $("#enhance-log-job");
    const selected = enhanceLogJobId && jobs.some((job) => job.id === enhanceLogJobId)
      ? enhanceLogJobId : (jobs[0]?.id || "");
    if (picker.options.length !== jobs.length || picker.value !== selected) {
      picker.replaceChildren(...(jobs.length ? jobs.map((job) => {
        const time = new Date(job.created_at * 1000).toLocaleTimeString();
        return new Option(`${time} · ${job.model} · ${job.prompt_preview}`, job.id);
      }) : [new Option("No recent sessions", "")]));
      picker.value = selected;
    }
    enhanceLogJobId = selected;
    $("#enhance-steer-input").disabled = $("#enhance-steer-send").disabled = !selected;
    if (!selected) {
      $("#enhance-log-state").textContent = "Idle";
      $("#enhance-log-cancel").hidden = true;
      $("#enhance-log-foot").textContent = "Waiting for a prompt";
      $("#enhance-log-body").replaceChildren();
      $("#enhance-log-body").append(Object.assign(document.createElement("p"), {className: "enhance-log-empty", textContent: "Run Enhance in the Studio to see its queue and model activity here."}));
      enhanceLogRenderedId = null; enhanceLogRenderedCount = -1;
      return;
    }
    const job = await Api.request(`${API_BASE}/api/enhancement/jobs/${selected}`, { cache: "no-store" });
    $("#enhance-log-cancel").hidden = !AGENTIC_ACTIVE_STATES.has(job.state);
    const stateNode = $("#enhance-log-state");
    stateNode.textContent = job.state.replaceAll("_", " ");
    stateNode.dataset.state = job.state;
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - job.created_at));
    $("#enhance-log-foot").textContent = `${job.model}  │  ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")} elapsed  │  ${["complete", "failed", "cancelled"].includes(job.state) ? job.state : job.phase || job.state.replaceAll("_", " ")}`;
    if (enhanceLogRenderedId === selected && enhanceLogRenderedCount === job.log.length &&
        enhanceLogRenderedState === job.state && enhanceLogRenderedRevision === (job.log_revision || 0) &&
        enhanceLogRenderedPhase === job.phase) return;
    const body = $("#enhance-log-body");
    if (enhanceLogRenderedId !== selected) { body.replaceChildren(); enhanceLogFollow = true; }
    // Update rows in place (streamed rows grow) and append new ones, so a reader scrolled
    // up keeps their place; only follow the bottom while they're parked there.
    const textOf = (entry) => `${({user: "❯ ", tool: "┊ ⚙ ", assistant: "☤ ", stream: "☤ ", thinking: "◇ ", ok: "✓ ", warn: "⚠ ", error: "✗ "})[entry.kind] || "· "}${entry.text}`;
    const rows = $$(".enhance-log-row:not(.enhance-log-live)", body);
    rows.slice(job.log.length).forEach((row) => row.remove());
    let live = $(".enhance-log-live", body);
    job.log.forEach((entry, index) => {
      let row = rows[index];
      if (!row) {
        row = document.createElement("div"); row.className = "enhance-log-row";
        const time = document.createElement("span"); time.className = "enhance-log-time";
        const content = document.createElement("span"); content.className = "enhance-log-text";
        row.append(time, content);
        body.insertBefore(row, live);
      }
      row.dataset.kind = entry.kind;
      const time = new Date(entry.t * 1000).toLocaleTimeString();
      if (row.firstChild.textContent !== time) row.firstChild.textContent = time;
      const text = textOf(entry);
      if (row.lastChild.textContent !== text) row.lastChild.textContent = text;
    });
    const active = AGENTIC_ACTIVE_STATES.has(job.state) && job.state !== "awaiting_approval";
    if (active && !live) {
      live = document.createElement("div"); live.className = "enhance-log-row enhance-log-live"; live.dataset.kind = "system";
      live.append(Object.assign(document.createElement("span"), {className: "enhance-log-time"}), Object.assign(document.createElement("span"), {className: "enhance-log-text"}));
      body.append(live);
    } else if (!active && live) { live.remove(); live = null; }
    if (live) live.lastChild.textContent = `… ${job.phase || job.state.replaceAll("_", " ")}`;
    if (enhanceLogFollow) body.scrollTop = body.scrollHeight;
    enhanceLogRenderedId = selected; enhanceLogRenderedCount = job.log.length; enhanceLogRenderedState = job.state;
    enhanceLogRenderedRevision = job.log_revision || 0;
    enhanceLogRenderedPhase = job.phase;
  } catch (error) {
    $("#enhance-log-foot").textContent = `Log unavailable: ${error.message}`;
  } finally { enhanceLogBusy = false; }
}

const STEER_COMMANDS = [
  { token: "/status", label: "what Hermes is doing and which clips are done" },
  { token: "/plan", label: "list the planned clips" },
  { token: "/clip ", label: "/clip N <text> - steer one clip only" },
  { token: "/accept ", label: "accept the staging previews, with optional notes" },
  { token: "/exclude ", label: "exclude the staging previews and retry, with optional notes" },
  { token: "/stop", label: "end this run" },
  { token: "/help", label: "list the commands" },
];
const steerMenu = { matches: [], index: 0 };

function renderSteerMenu() {
  const menu = $("#enhance-steer-menu");
  menu.hidden = !steerMenu.matches.length;
  menu.innerHTML = steerMenu.matches.map((entry, index) => `<button type="button" class="alias-option${index === steerMenu.index ? " is-active" : ""}" role="option" data-steer-index="${index}"><span class="alias-option-token">${escapeHtml(entry.token.trim())}</span><span class="alias-option-label">${escapeHtml(entry.label)}</span></button>`).join("");
}

function updateSteerMenu() {
  const value = $("#enhance-steer-input").value;
  steerMenu.matches = /^\/\S*$/.test(value) ? STEER_COMMANDS.filter((entry) => entry.token.startsWith(value.toLowerCase()) && entry.token.trim() !== value) : [];
  steerMenu.index = Math.min(steerMenu.index, Math.max(0, steerMenu.matches.length - 1));
  renderSteerMenu();
}

function pickSteerCommand(entry) {
  const input = $("#enhance-steer-input");
  input.value = entry.token;
  input.focus();
  steerMenu.matches = [];
  renderSteerMenu();
}

async function sendSteer(event) {
  event.preventDefault();
  const input = $("#enhance-steer-input"), button = $("#enhance-steer-send");
  const text = input.value.trim();
  if (!text || !enhanceLogJobId) return;
  button.disabled = true;
  try {
    await Api.request(`${API_BASE}/api/enhancement/jobs/${encodeURIComponent(enhanceLogJobId)}/steer`, { method: "POST", body: { text } });
    input.value = "";
    steerMenu.matches = []; renderSteerMenu();
    enhanceLogFollow = true;
    enhanceLogRenderedCount = -1;
    await refreshEnhancementLog();
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; input.focus(); }
}

function bindSteer() {
  const input = $("#enhance-steer-input");
  $("#enhance-steer").addEventListener("submit", sendSteer);
  input.addEventListener("input", updateSteerMenu);
  input.addEventListener("blur", () => setTimeout(() => { steerMenu.matches = []; renderSteerMenu(); }, 120));
  input.addEventListener("keydown", (event) => {
    if (!steerMenu.matches.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      steerMenu.index = (steerMenu.index + (event.key === "ArrowDown" ? 1 : -1) + steerMenu.matches.length) % steerMenu.matches.length;
      renderSteerMenu();
    } else if (event.key === "Tab" || (event.key === "Enter" && steerMenu.matches[steerMenu.index].token.endsWith(" "))) {
      event.preventDefault();
      pickSteerCommand(steerMenu.matches[steerMenu.index]);
    } else if (event.key === "Escape") { steerMenu.matches = []; renderSteerMenu(); }
  });
  $("#enhance-steer-menu").addEventListener("mousedown", (event) => {
    const option = event.target.closest("[data-steer-index]");
    if (!option) return;
    event.preventDefault();
    pickSteerCommand(steerMenu.matches[Number(option.dataset.steerIndex)]);
  });
}

// Settings that only matter with their parent switch on: staging options hide, YouTube search greys out.
// Settings key suffix -> label; each select is built from #enhance-thinking-level's levels plus "Same as main".
const AGENTIC_THINKING_STAGES = [
  ["research", "Web research"], ["plan_review", "Plan review"], ["plan_fixes", "Plan fixes"],
  ["footage_review", "Footage review"], ["frame_analysis", "Continuation frames"], ["screenshots", "Screenshot picks"],
];

function buildAgenticThinkingStages() {
  const levels = [...$("#enhance-thinking-level").options].map((option) => `<option value="${option.value}">${option.textContent}</option>`).join("");
  $("#agentic-thinking-stages").innerHTML = AGENTIC_THINKING_STAGES.map(([key, label]) =>
    `<div class="field-row"><label for="agentic-thinking-${key}">${label}</label><select id="agentic-thinking-${key}"><option value="inherit">Same as main</option>${levels}</select></div>`).join("");
  for (const [key] of AGENTIC_THINKING_STAGES) $(`#agentic-thinking-${key}`).addEventListener("change", scheduleEnhancementSave);
}

const AGENTIC_REVIEW_OPTIONS = [
  ["#agentic-stage-series-review", "agentic_stage_series_review"], ["#agentic-stage-best-of-two", "agentic_stage_best_of_two"],
  ["#agentic-final-whisper-check", "agentic_final_whisper_check"], ["#agentic-final-series-review", "agentic_final_series_review"],
  ["#agentic-final-best-of-two", "agentic_final_best_of_two"],
];

function syncAgenticSettingGroups() {
  $("#agentic-stage-options").hidden = !$("#agentic-stage-enabled").checked;
  $("#agentic-stage-review-options").hidden = !$("#agentic-stage-review").checked;
  $("#agentic-final-review-options").hidden = !$("#agentic-final-review").checked;
  $("#agentic-review-cap-options").hidden = !$("#agentic-review-cap").checked;
  const web = $("#agentic-browser-enabled").checked;
  $("#agentic-video-search").disabled = !web;
  $("#agentic-video-search").closest("label").classList.toggle("is-disabled", !web);
}

function bindEnhancement() {
  bindSteer();
  const logBody = $("#enhance-log-body");
  logBody.addEventListener("scroll", () => {
    enhanceLogFollow = logBody.scrollTop + logBody.clientHeight >= logBody.scrollHeight - 8;
  }, { passive: true });
  $("#enhance-log-job").addEventListener("change", (event) => {
    enhanceLogJobId = event.target.value; enhanceLogRenderedCount = -1; refreshEnhancementLog();
  });
  setInterval(() => { if (document.body.dataset.view === "enhance") refreshEnhancementLog(); }, 2000);
  $("#enhance-log-cancel").addEventListener("click", async () => {
    if (!enhanceLogJobId) return;
    try {
      await Api.request(`${API_BASE}/api/enhancement/jobs/${enhanceLogJobId}/cancel`, { method: "POST" });
      refreshEnhancementLog();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#enhance-model").addEventListener("change", scheduleEnhancementSave);
  buildAgenticThinkingStages();
  $("#enhance-thinking-level").addEventListener("change", scheduleEnhancementSave);
  $("#enhance-context-size").addEventListener("change", scheduleEnhancementSave);  // on change: a half-typed number is out of range
  $("#agentic-stage-enabled").addEventListener("change", syncAgenticSettingGroups);
  $("#agentic-browser-enabled").addEventListener("change", syncAgenticSettingGroups);
  $("#agentic-stage-review").addEventListener("change", syncAgenticSettingGroups);
  $("#agentic-final-review").addEventListener("change", syncAgenticSettingGroups);
  $("#agentic-review-cap").addEventListener("change", syncAgenticSettingGroups);
  ["#agentic-browser-enabled", "#agentic-video-search", "#agentic-manage-references", "#agentic-character-screenshots", "#agentic-lora-gallery", "#agentic-continuation-tail", "#agentic-continuation-tail-audio", "#agentic-scene-screenshots", "#agentic-rushed-dialogue-check", "#agentic-stage-enabled", "#agentic-stage-preset", "#agentic-full-preset",
   "#agentic-stage-review", "#agentic-stage-approval", "#agentic-whisper-check", "#agentic-final-review", "#agentic-review-cap", "#agentic-review-rounds",
   ...AGENTIC_REVIEW_OPTIONS.map(([id]) => id)].forEach((selector) =>
    $(selector).addEventListener("change", scheduleEnhancementSave));
  $("#agentic-refinement-prompt").addEventListener("input", scheduleEnhancementSave);
  ["#enhance-temperature", "#enhance-top-p", "#enhance-top-k",
   "#enhance-min-p", "#enhance-repeat-penalty", "#enhance-seed", "#enhance-extra-body", "#enhance-instructions"]
    .forEach((id) => $(id).addEventListener("input", scheduleEnhancementSave));
  $("#enhance-instructions-reset").addEventListener("click", () => {
    $("#enhance-instructions").value = state.enhancement.default_instructions; saveEnhancement();
  });
  $("#enhance-test").addEventListener("click", testEnhancement);
  $("#enhance-preview-refresh").addEventListener("click", refreshEnhancePreview);
  $("#enhance-preview-card").addEventListener("toggle", (event) => { if (event.currentTarget.open) refreshEnhancePreview(); });
  $("#enhance-form").addEventListener("submit", (event) => event.preventDefault());
  window.addEventListener("pagehide", () => { if (enhanceSaveTimer) saveEnhancement(); });
}

// Below 0.1 MP two decimals would round tiny sizes (64x64 = 0.004) to 0.00.
function formatMegapixels(value) {
  return Number(value).toFixed(Number(value) < 0.1 ? 3 : 2);
}

let resolutionUpdateSeq = 0;
let resolutionUpdatePromise = Promise.resolve();

const resolutionCache = new Map();
function updateResolution() {
  const seq = ++resolutionUpdateSeq;
  const megapixels = Number($("#megapixels").value);
  const aspect = $("#aspect").value;
  if (!Number.isFinite(megapixels)) return resolutionUpdatePromise;
  const step = resolutionStep();
  $("#width").step = step;
  $("#height").step = step;
  $("#megapixels").max = maxMegapixels();
  const model = state.model;
  // A pure function of (model, MP, aspect): init and model switches asked for the same one twice.
  const key = `${model}|${megapixels}|${aspect}`;
  let lookup = resolutionCache.get(key);
  if (!lookup) {
    lookup = Api.request(`${API_BASE}/api/resolution`, { method: "POST", body: { model, megapixels, aspect }, signal: timeoutSignal(20_000) });
    resolutionCache.set(key, lookup);
    lookup.catch(() => resolutionCache.delete(key));
  }
  resolutionUpdatePromise = lookup.then((result) => {
    if (seq !== resolutionUpdateSeq) return;
    $("#width").value = result.width;
    $("#height").value = result.height;
    $("#resolution-detail").textContent = `Output guide ${result.width} × ${result.height} · ${formatMegapixels(result.megapixels)} MP`;
  }).catch(() => { /* Browser controls can be mid-edit; backend remains authoritative. */ });
  return resolutionUpdatePromise;
}

// Width/height grid and size cap per model (GenerationRequest.validate_resolution).
const resolutionStep = () => ({ ltx_2_5: 64, wan_2_2: 16 })[state.model] || 32;
const maxMegapixels = () => ({ ltx_2_5: 4.2, wan_2_2: state.options?.wan_limits?.max_megapixels || 1 })[state.model] || 2.5;

function updateMegapixelsFromResolution() {
  const step = resolutionStep();
  const snap = (value) => Math.max(step, Math.min(2048, Math.round(Number(value) / step) * step));
  let width = snap($("#width").value);
  let height = snap($("#height").value);
  if (width * height < 4_096) { // 64x64 (0.004 MP), the smallest size the backend accepts
    const scale = Math.sqrt(4_096 / (width * height));
    width = snap(width * scale);
    height = snap(height * scale);
  }
  const maxPixels = maxMegapixels() * 1_000_000;
  if (width * height > maxPixels) {
    const scale = Math.sqrt(maxPixels / (width * height));
    width = Math.max(step, Math.floor(width * scale / step) * step);
    height = Math.max(step, Math.floor(height * scale / step) * step);
  }
  $("#width").value = width;
  $("#height").value = height;
  const ratio = width / height;
  const closestAspect = Object.entries(aspectRatios).sort((a, b) =>
    Math.abs(Math.log(ratio / a[1])) - Math.abs(Math.log(ratio / b[1])))[0][0];
  $("#aspect").value = closestAspect;
  const megapixels = width * height / 1_000_000;
  $("#megapixels").value = formatMegapixels(megapixels);
  $("#resolution-detail").textContent = `Output guide ${width} × ${height} · ${formatMegapixels(megapixels)} MP`;
}

/* H3 CFG / negative prompt / sigma shift. The shift placeholders show the
   mode's own default (what a blank field keeps - see _apply_h3_quality and
   _apply_ref2v_pdd in backend/generation.py). */
function renderCfg() {
  const ref2v = state.mode === "ref2v";
  // Tested 2026-09-25 (see GenerationRequest.cfg): with Parasyte on, CFG 1.5
  // already rendered a scene unrelated to the prompt, so CFG is locked to 1.
  const globalLock = !state.h3CfgEnabled; // server-side lock, see H3_CFG_ENABLED
  const parasyteLock = globalLock || (!ref2v && $("#parasyte-enabled").checked);
  $("#cfg").disabled = parasyteLock;
  if (parasyteLock) $("#cfg").value = 1;
  const cfgOn = Number($("#cfg").value) > 1;
  $("#negative-prompt-row").hidden = !cfgOn;
  const refTurbo = ref2v && $("#ref2v-turbo-enabled").checked;
  const hint = $("#cfg-hint");
  hint.classList.toggle("is-warning", parasyteLock || (cfgOn && refTurbo));
  hint.textContent = globalLock ? state.h3CfgLockReason : parasyteLock
    ? "CFG is locked to 1 while the Parasyte turbo LoRA is on: with it on, CFG 1.5 rendered a scene unrelated to the prompt. Turn Parasyte off in Speedup & Saves (and raise Steps) to use CFG."
    : cfgOn && refTurbo
      ? "Untested with the Reference turbo LoRA on - it is distilled for CFG 1 and may ignore the prompt. Turn it off in Speedup & Saves if the result looks unrelated."
      : "CFG 1 = off. Up to 1.5 adds the negative prompt (about 2x sampling time); these H3 checkpoints lost the prompt entirely at 1.75 and above.";
  const pdd = ref2v && $("#ref2v-turbo-enabled").checked && $("#ref2v-turbo").value === "pdd" && !$("#minimax-checkpoint").value;
  $("#shift-video").placeholder = pdd ? "12 (PDD)" : "8";
  $("#shift-audio").placeholder = pdd ? "3 (PDD)" : "5";
}

/* H3 latent upscale - output size and stability warnings come from
   /api/upscale-check (backend/generation.py h3_upscale_warnings). */
let upscaleCheckSeq = 0;

// Advanced H3 upscale settings (request.upscale_advanced, models.H3UpscaleAdvanced).
// Controls carry data-ua="<field>"; the defaults match the server's, which are
// what every job ran with before these were exposed.
const UPSCALE_ADVANCED_DEFAULTS = {
  upscaler: "model", interpolation: "bilinear", upscaler_device: "cuda", upscaler_precision: "fp16",
  keep_upscaler_resident: false, keep_model_resident: false, unload_model_after: true, upscale_chunks_first: false,
  chunk_frames: 0, chunk_overlap: 17, anchor_strength: 0.999,
  tiling: "off", token_budget: 70000, grid_rows: 2, grid_cols: 2, tile_width: 768, tile_height: 768,
  overlap_width: 128, overlap_height: 128, fade_width: 32, fade_height: 32, min_tile_size: 256,
  overlap_mode: "earlier", overlap_blend: "linear", joint_steps: true, sampler: "", scheduler: "",
};

function fillUpscaleAdvancedSelects(options) {
  const current = readUpscaleAdvanced(); // before refilling: an empty select reads as its default
  const fill = (id, values, first) => {
    $(id).innerHTML = [`<option value="">${first}</option>`, ...values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)].join("");
  };
  fill("#h3-ua-sampler", options.samplers, "Same as render");
  fill("#h3-ua-scheduler", options.schedulers, "Same as render");
  const frames = (from, to) => Array.from({ length: (to - from) / 17 + 1 }, (_, i) => from + i * 17);
  $("#h3-ua-chunk-frames").innerHTML = `<option value="0">Auto (fits VRAM)</option>`
    + frames(34, 408).map((value) => `<option value="${value}">${value} (${(value / 24).toFixed(1)} s)</option>`).join("");
  $("#h3-ua-chunk-overlap").innerHTML = frames(0, 68).map((value) => `<option value="${value}">${value}</option>`).join("");
  writeUpscaleAdvanced(current);
}

function readUpscaleAdvanced() {
  const out = { ...UPSCALE_ADVANCED_DEFAULTS };
  $$("[data-ua]").forEach((input) => {
    const key = input.dataset.ua;
    if (input.type === "checkbox") out[key] = input.checked;
    else if ("uaNumber" in input.dataset) out[key] = input.value === "" ? UPSCALE_ADVANCED_DEFAULTS[key] : Number(input.value);
    else out[key] = input.value;
  });
  return out;
}

function writeUpscaleAdvanced(values) {
  const merged = { ...UPSCALE_ADVANCED_DEFAULTS, ...(values || {}) };
  $$("[data-ua]").forEach((input) => setField(`#${input.id}`, merged[input.dataset.ua]));
  renderUpscaleAdvanced();
}

// Multi GPU presets cover only this Settings card; built-ins are read-only.
let multiGpuPresets = { builtin: {}, saved: {} };

function currentMultiGpuPreset() {
  return { multi_gpu_offload: $("#multi-gpu-offload").value,
    multi_gpu_split_gb: Number($("#multi-gpu-split-gb").value) };
}

function multiGpuPresetKey(preset) {
  return `${preset.multi_gpu_offload}:${preset.multi_gpu_offload === "split" ? preset.multi_gpu_split_gb : ""}`;
}

function renderMultiGpuPresets(preferred = "") {
  const select = $("#multi-gpu-preset");
  const previous = preferred || select.value;
  const options = (group, label) => {
    const names = Object.keys(multiGpuPresets[group]);
    return names.length ? `<optgroup label="${label}">${names.map((name) => `<option value="${escapeHtml(`${group}:${name}`)}">${escapeHtml(name)}</option>`).join("")}</optgroup>` : "";
  };
  select.innerHTML = `<option value="">Custom</option>${options("builtin", "Built-in")}${options("saved", "Saved")}`;
  const key = multiGpuPresetKey(currentMultiGpuPreset());
  const [previousGroup, ...previousName] = previous.split(":");
  if (multiGpuPresets[previousGroup]?.[previousName.join(":")] &&
      multiGpuPresetKey(multiGpuPresets[previousGroup][previousName.join(":")]) === key) {
    select.value = previous;
  } else {
    for (const group of ["builtin", "saved"]) {
      const name = Object.keys(multiGpuPresets[group]).find((item) => multiGpuPresetKey(multiGpuPresets[group][item]) === key);
      if (name) { select.value = `${group}:${name}`; break; }
    }
  }
  $("#multi-gpu-preset-delete").hidden = !select.value.startsWith("saved:");
  $("#multi-gpu-preset-hint").textContent = select.value ? "" : "Save as… to keep this allocation.";
}

function applyMultiGpuPreset(value) {
  const [group, ...parts] = value.split(":");
  const preset = multiGpuPresets[group]?.[parts.join(":")];
  if (!preset) return;
  setField("#multi-gpu-offload", preset.multi_gpu_offload);
  setField("#multi-gpu-split-gb", preset.multi_gpu_split_gb);
  $("#multi-gpu-split-row").hidden = preset.multi_gpu_offload !== "split";
  renderMultiGpuPresets(value);
  scheduleSessionSettingsSave();
  updatePresetStatus();
}

async function loadMultiGpuPresets(pending = null) {
  try { multiGpuPresets = await (pending || Api.request(`${API_BASE}/api/multi-gpu-presets`)); }
  catch (error) { toast(`Couldn't load Multi GPU presets: ${error.message}`, "error"); }
  renderMultiGpuPresets();
}

async function saveMultiGpuPreset() {
  const current = $("#multi-gpu-preset").value;
  const name = prompt("Name this Multi GPU preset:", current.startsWith("saved:") ? current.slice(6) : "")?.trim();
  if (!name) return;
  if (multiGpuPresets.saved[name] && !confirm(`Replace the saved Multi GPU preset "${name}"?`)) return;
  try {
    multiGpuPresets = await Api.request(`${API_BASE}/api/multi-gpu-presets`, { method: "PUT", body: { name, settings: currentMultiGpuPreset() } });
    renderMultiGpuPresets(`saved:${name}`);
    toast(`Saved Multi GPU preset "${name}"`);
  } catch (error) { toast(error.message, "error"); }
}

async function deleteMultiGpuPreset() {
  const value = $("#multi-gpu-preset").value;
  if (!value.startsWith("saved:")) return;
  const name = value.slice(6);
  if (!confirm(`Delete the Multi GPU preset "${name}"?`)) return;
  try {
    multiGpuPresets = await Api.request(`${API_BASE}/api/multi-gpu-presets?name=${encodeURIComponent(name)}`, { method: "DELETE" });
    renderMultiGpuPresets();
  } catch (error) { toast(error.message, "error"); }
}

// Latent upscale presets (GET/PUT/DELETE /api/upscale-presets): built-in
// ones are read-only, saved ones are shared by every browser. A preset is
// only the upscale card's own settings - never the render size or seeds. The
// select shows whichever preset matches the card right now, else "Custom".
let upscalePresets = { builtin: {}, saved: {} };

function currentUpscalePreset() {
  return {
    upscale_factor: Number($("#h3-upscale-factor").value), upscale_steps: Number($("#h3-upscale-steps").value),
    upscale_denoise: Number($("#h3-upscale-denoise").value), upscale_chunking: $("#h3-upscale-chunking").checked,
    upscale_advanced: readUpscaleAdvanced(),
  };
}

// Same key order on both sides, so a JSON comparison is a field comparison.
function upscalePresetKey(preset) {
  return JSON.stringify([Number(preset.upscale_factor), Number(preset.upscale_steps), Number(preset.upscale_denoise),
    Boolean(preset.upscale_chunking), Object.keys(UPSCALE_ADVANCED_DEFAULTS).map((key) => (preset.upscale_advanced || {})[key] ?? UPSCALE_ADVANCED_DEFAULTS[key])]);
}

function matchingUpscalePreset() {
  const key = upscalePresetKey(currentUpscalePreset());
  for (const group of ["builtin", "saved"]) {
    const name = Object.keys(upscalePresets[group]).find((item) => upscalePresetKey(upscalePresets[group][item]) === key);
    if (name) return `${group}:${name}`;
  }
  return "";
}

function renderUpscalePresets() {
  const select = $("#h3-upscale-preset");
  const options = (group, label) => {
    const names = Object.keys(upscalePresets[group]);
    return names.length ? `<optgroup label="${label}">${names.map((name) => `<option value="${escapeHtml(`${group}:${name}`)}">${escapeHtml(name)}</option>`).join("")}</optgroup>` : "";
  };
  select.innerHTML = `<option value="">Custom</option>${options("builtin", "Built-in")}${options("saved", "Saved")}`;
  const match = matchingUpscalePreset();
  select.value = match;
  $("#h3-upscale-preset-delete").hidden = !match.startsWith("saved:");
  $("#h3-upscale-preset-hint").textContent = {
    "builtin:Best quality": "Auto tiling at 35k tokens: wider tiles, about 35% faster than untiled at 1856×1024.",
    "builtin:Fastest": "Auto tiling at 20k tokens: narrow strips, the fastest measured (about 43% faster than untiled at 1856×1024).",
    "builtin:Stable (untiled)": "No spatial tiling: the second pass samples each time chunk whole. Slower, but it rendered clean the 15 s seeds that broke with tiling (Sept 30).",
  }[match] || (match ? "" : "Settings don't match a preset. Save as… to keep them.");
}

function applyUpscalePreset(value) {
  const [group, ...rest] = value.split(":");
  const preset = upscalePresets[group]?.[rest.join(":")];
  if (!preset) return;
  setField("#h3-upscale-factor", preset.upscale_factor);
  setField("#h3-upscale-steps", preset.upscale_steps);
  setField("#h3-upscale-denoise", preset.upscale_denoise);
  setField("#h3-upscale-chunking", preset.upscale_chunking);
  writeUpscaleAdvanced(preset.upscale_advanced);
  updateUpscaleCheck();
  scheduleSessionSettingsSave();
  renderPresetBar(); // the main preset shows "modified" if this changed it
}

async function loadUpscalePresets(pending = null) {
  try { upscalePresets = await (pending || Api.request(`${API_BASE}/api/upscale-presets`)); }
  catch (error) { toast(`Couldn't load upscale presets: ${error.message}`, "error"); }
  renderUpscalePresets();
}

async function saveUpscalePreset() {
  const current = $("#h3-upscale-preset").value;
  const name = prompt("Name this upscale preset:", current.startsWith("saved:") ? current.slice(6) : "")?.trim();
  if (!name) return;
  if (upscalePresets.saved[name] && !confirm(`Replace the saved upscale preset "${name}"?`)) return;
  try {
    upscalePresets = await Api.request(`${API_BASE}/api/upscale-presets`, { method: "PUT", body: { name, settings: currentUpscalePreset() } });
    renderUpscalePresets();
    toast(`Saved upscale preset "${name}"`);
  } catch (error) { toast(error.message, "error"); }
}

async function deleteUpscalePreset() {
  const value = $("#h3-upscale-preset").value;
  if (!value.startsWith("saved:")) return;
  const name = value.slice(6);
  if (!confirm(`Delete the upscale preset "${name}"?`)) return;
  try {
    upscalePresets = await Api.request(`${API_BASE}/api/upscale-presets?name=${encodeURIComponent(name)}`, { method: "DELETE" });
    renderUpscalePresets();
  } catch (error) { toast(error.message, "error"); }
}

// Shows only the rows that apply to the chosen upscaler/tiling mode, and
// sums up each group in its closed summary.
function renderUpscaleAdvanced() {
  const values = readUpscaleAdvanced();
  $$("[data-ua-when]").forEach((row) => {
    const [, key, op, want] = row.dataset.uaWhen.match(/^(\w+)(!?=)(.+)$/);
    row.hidden = op === "=" ? String(values[key]) !== want : String(values[key]) === want;
  });
  const chunking = $("#h3-upscale-chunking").checked;
  $$("#h3-ua-temporal [data-ua]").forEach((input) => { input.disabled = !chunking; });
  $("#h3-ua-temporal-state").textContent = !chunking ? "· off (turn on Chunk over time)"
    : `· ${values.chunk_frames || "auto"} frames, overlap ${values.chunk_overlap}`;
  $("#h3-ua-spatial-state").textContent = {
    off: "· off", auto: `· auto, ${values.token_budget.toLocaleString()} tokens`,
    rows_cols: `· ${values.grid_rows} × ${values.grid_cols}`, specific_size: `· ${values.tile_width} × ${values.tile_height} px`,
  }[values.tiling] ?? "";
  renderUpscalePresets();
}

function h3UpscaleActive() {
  return state.model === "minimax_h3" && $("#h3-upscale-enable").checked;
}

async function checkH3Upscale(request = collectSettings()) {
  return Api.request(`${API_BASE}/api/upscale-check`, { method: "POST", signal: timeoutSignal(20_000), body: {
    mode: request.mode, width: request.width, height: request.height, upscale_factor: request.upscale_factor,
    upscale_steps: request.upscale_steps, upscale_denoise: request.upscale_denoise,
    upscale_chunking: request.upscale_chunking, upscale_advanced: request.upscale_advanced, duration: request.duration,
    ref2v_turbo_enabled: request.ref2v_turbo_enabled, ref2v_turbo: request.ref2v_turbo, checkpoint: request.checkpoint,
  }});
}

async function updateUpscaleCheck() {
  if (!h3UpscaleActive()) return;
  const seq = ++upscaleCheckSeq;
  // Keep the displayed target in sync while the server check is in flight.
  // This makes factor edits feel immediate even when the warning calculation
  // takes a moment or the API is temporarily unavailable.
  const request = collectSettings();
  const factor = Number(request.upscale_factor);
  if (Number.isFinite(factor) && factor > 0) {
    const targetWidth = Math.max(32, Math.round(request.width * factor / 32) * 32);
    const targetHeight = Math.max(32, Math.round(request.height * factor / 32) * 32);
    $("#h3-upscale-detail").textContent =
      `${request.width} × ${request.height} → ${targetWidth} × ${targetHeight} · ${formatMegapixels(targetWidth * targetHeight / 1_000_000)} MP`;
  }
  // The server check waits for a 200 ms pause: arrowing through the factor or
  // dragging a slider used to send one request per step (8 steps -> 9 POSTs).
  await new Promise((resolve) => setTimeout(resolve, 200));
  if (seq !== upscaleCheckSeq) return;
  let result;
  try { result = await checkH3Upscale(); } catch (error) {
    // A real settings conflict (e.g. overlap >= the auto chunk) shows here at once;
    // anything else is usually a field mid-edit, and Generate re-checks.
    if (seq === upscaleCheckSeq && error.code === "invalid_upscale") {
      const item = document.createElement("li");
      item.className = "is-unstable";
      item.textContent = error.message;
      $("#h3-upscale-warnings").replaceChildren(item);
      $("#h3-upscale-warnings").hidden = false;
    }
    return;
  }
  if (seq !== upscaleCheckSeq) return;
  const pieces = [`${request.width} × ${request.height} → ${result.width} × ${result.height} · ${formatMegapixels(result.megapixels)} MP`];
  if (request.upscale_chunking) pieces.push(`${result.chunks} time chunk${result.chunks === 1 ? "" : "s"} of ${result.chunk_frames} frames`);
  if (result.tiling) pieces.push(result.tiling);
  $("#h3-upscale-detail").textContent = pieces.join(" · ");
  const list = $("#h3-upscale-warnings");
  list.replaceChildren(...result.warnings.map((warning) => {
    const item = document.createElement("li");
    item.className = warning.level === "unstable" ? "is-unstable" : "";
    item.textContent = warning.message;
    return item;
  }));
  list.hidden = !result.warnings.length;
}

function repairHiddenInvalidFields() {
  $$("#generate-form input:invalid").forEach((input) => {
    // Visible, or only inside a closed section (the invalid handler opens it): the browser explains.
    if (input.offsetParent !== null || (input.closest("details:not([open])") && !input.closest("[hidden]"))) return;
    if (input.type !== "number" && input.type !== "range") return;
    let value = Number.parseFloat(input.value);
    if (!Number.isFinite(value)) value = Number.parseFloat(input.defaultValue);
    if (!Number.isFinite(value)) return;
    const min = input.min === "" ? -Infinity : Number(input.min), max = input.max === "" ? Infinity : Number(input.max);
    value = Math.min(max, Math.max(min, value));
    const step = input.step === "" ? 1 : input.step === "any" ? 0 : Number(input.step);
    if (step > 0) {
      const base = Number.isFinite(min) ? min : 0;
      value = base + Math.round((value - base) / step) * step;
      if (value > max) value -= step;
      const decimals = (String(input.step).split(".")[1] || "").length;
      value = Number(value.toFixed(decimals));
    }
    input.value = String(value);
  });
}

// Asks before queuing an upscale to a size marked unstable. Returns false to cancel.
async function confirmH3Upscale(request = collectSettings()) {
  if (!h3UpscaleActive()) return true;
  let result;
  try { result = await checkH3Upscale(request); } catch { return true; } // the server validates on submit anyway
  const unstable = result.warnings.filter((warning) => warning.level === "unstable");
  if (!unstable.length) return true;
  return confirm(`Upscaling to ${result.width} × ${result.height} may not render well:\n\n`
    + unstable.map((warning) => `• ${warning.message}`).join("\n\n") + "\n\nGenerate anyway?");
}

// Snapshots the current width/height/aspect/megapixels once, the first time
// the reference image's resolution is matched, so a later revert (reference
// replaced or removed, or mode switched back to text) can put back exactly
// what was there before - not just "text" defaults. A second match while one
// is already pending does NOT re-snapshot, so the original pre-match values
// survive matching against several reference images in a row.
function captureResolutionSnapshot() {
  if (state.savedResolution) return;
  state.savedResolution = {
    width: $("#width").value, height: $("#height").value,
    aspect: $("#aspect").value, megapixels: $("#megapixels").value,
  };
}

function restoreResolutionSnapshot() {
  if (!state.savedResolution) return;
  const { width, height, aspect, megapixels } = state.savedResolution;
  $("#width").value = width;
  $("#height").value = height;
  $("#aspect").value = aspect;
  $("#megapixels").value = megapixels;
  $("#resolution-detail").textContent = `Output guide ${width} × ${height} · ${formatMegapixels(Number(megapixels))} MP`;
  state.savedResolution = null;
}

async function matchResolutionToReference() {
  const reference = currentReference(state.firstFrameId);
  if (!reference) return;
  const image = new Image();
  image.src = reference.preview_url;
  try {
    await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; });
  } catch { return toast("Couldn't read the reference image's resolution.", "error"); }
  captureResolutionSnapshot();
  $("#width").value = image.naturalWidth;
  $("#height").value = image.naturalHeight;
  updateMegapixelsFromResolution();
}

// Video reference rows: "Match length" sets Duration to the video's trimmed
// length (to the tenth - the slider normally moves in whole seconds, so it
// switches to free steps until it's dragged again), and "Match size" sets the
// resolution to the video's own frame size (snapped to the model's grid).
function matchDurationToVideo(id) {
  const reference = currentReference(id);
  const seconds = reference && effectiveDuration(reference);
  if (!seconds) return toast("Couldn't read this video's length.", "error");
  const input = $("#duration");
  const clamped = Math.min(Number(input.max), Math.max(Number(input.min), Math.round(seconds * 10) / 10));
  input.step = "any";
  input.value = clamped;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  toast(clamped === Math.round(seconds * 10) / 10
    ? `Duration set to ${clamped}s to match the video.`
    : `The video is ${seconds.toFixed(1)}s; Duration set to ${clamped}s, the closest this model allows.`);
}

async function matchSizeToVideo(id) {
  const reference = currentReference(id);
  if (!reference) return;
  const video = document.createElement("video");
  video.preload = "metadata";
  video.muted = true;
  video.src = reference.preview_url;
  try {
    await new Promise((resolve, reject) => { video.onloadedmetadata = resolve; video.onerror = reject; });
  } catch { return toast("Couldn't read this video's size.", "error"); }
  const { videoWidth: width, videoHeight: height } = video;
  video.removeAttribute("src");
  video.load();
  if (!width || !height) return toast("Couldn't read this video's size.", "error");
  captureResolutionSnapshot();
  $("#width").value = width;
  $("#height").value = height;
  updateMegapixelsFromResolution();
  $("#width").dispatchEvent(new Event("change", { bubbles: true }));
  toast(`Resolution set from the video (${width} × ${height}) to ${$("#width").value} × ${$("#height").value}.`);
}

/* ------------------------------------------------- reference tokens -- */

// ref2v and SeedHunter reference pools speak MiniMax H3's own canonical
// <Picture N>/<Video N>/<Audio N> tokens directly (see referenceToken()).
// Keyframe mode has no reference pool - it keeps the plain @first/@last
// convenience words its backend alias_map still expects.
function promptTags() {
  if (state.model === "seedhunter") {
    return [
      ...referencesByRole("sh-images"), ...referencesByRole("sh-videos"), ...referencesByRole("sh-audios"),
    ].map((reference) => ({ token: referenceToken(reference) || `@${reference.alias}`, label: reference.label, reference }));
  }
  if (state.mode === "ref2v") {
    return referencesByRole("references").map((reference) => ({ token: referenceToken(reference) || `@${reference.alias}`, label: reference.label, reference }));
  }
  return [];
}

const FL2V_LEGACY_TAGS = ["image1", "First", "image2", "Last"];

// Autocomplete while typing in the prompt. Each completer looks at the text
// before the caret; the first that matches fills the menu, and the chosen
// entry replaces the matched text (entries may place the caret with "§").
//   @  FL2V legacy tags               <  reference/subject labels, <d>, <scenetrans>, <cutoff>
//   [  [Shot N], [English], [unclear], Reference task types
//   Reference retention lines: markers after "<Label N> (...): "
//   line start: section keys (Tab only, so Enter still ends a short line)
const isH3 = () => state.model === "minimax_h3" || state.model === "seedhunter"; // SeedHunter is H3-based
const isH3Reference = () => state.model === "minimax_h3" && state.mode === "ref2v";
const H3_SECTION_KEYS = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"];
const H3_REF_SECTION_KEYS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"];
const RETENTION_TIPS = {
  fully_preserved: "the defined role is fully kept", partially_preserved: "used, some traits changed or partly kept",
  attribute_transfer: "traits moved onto a different subject", weak_reference: "only broad similarity",
  fully_copy: "whole source audio is the whole final track", partially_copy: "part / some layers copied",
  reference: "not copied - timbre, rhythm, style or words referenced",
};

// <Subject N> labels in the prompt, with the start of their definition line.
function definedSubjects() {
  const text = $("#prompt").value;
  const found = new Map();
  for (const match of text.matchAll(/<\s*Subject\s+(\d+)\s*>/gi)) {
    const number = Number(match[1]);
    if (!found.has(number)) found.set(number, null);
  }
  for (const match of text.matchAll(/^<\s*Subject\s+(\d+)\s*>\s+is\s+(.+)$/gim)) {
    found.set(Number(match[1]), match[2].replace(/<[^>]+>/g, "").slice(0, 60));
  }
  return [...found.entries()].sort((a, b) => a[0] - b[0]);
}

const ALIAS_COMPLETERS = [
  { // FL2V legacy @tags (H3/LTX keyframe mode)
    pattern: /@([A-Za-z0-9_]*)$/,
    when: () => state.mode === "fl2v" && state.model !== "seedhunter",
    entries: (fragment) => FL2V_LEGACY_TAGS
      .filter((tag) => tag.toLowerCase().startsWith(fragment.toLowerCase()))
      .map((tag) => ({ token: `@${tag}`, label: null, insert: `@${tag}`, category: "image" })),
  },
  {
    pattern: /<([^<>\n]*)$/,
    entries: (fragment) => {
      const typed = fragment.toLowerCase().replace(/\s+/g, " ");
      const entries = promptTags().map((entry) => ({ ...entry, insert: entry.token, category: tokenCategory(entry.token) }));
      if (isH3Reference()) {
        const subjects = definedSubjects();
        subjects.forEach(([number, label]) => entries.push({ token: `<Subject ${number}>`, label, insert: `<Subject ${number}>`, category: "subject" }));
        const next = Math.max(0, ...subjects.map(([number]) => number)) + 1;
        entries.push({ token: `<Subject ${next}>`, label: "new subject", insert: `<Subject ${next}>`, category: "subject" });
      }
      if (isH3()) {
        entries.push(
          { token: "<d>", label: "spoken or sung line", insert: "<d>[English] §</d>", kind: "tag" },
          { token: "</d>", label: "end of line", insert: "</d>", kind: "tag" },
          { token: "<scenetrans>", label: "line crosses a cut", insert: "<scenetrans>", kind: "tag" },
          { token: "<cutoff>", label: "cut off by the video's end", insert: "<cutoff>", kind: "tag" },
        );
      }
      return entries.filter((entry) => entry.token.slice(1, -1).toLowerCase().replace(/\s+/g, " ").startsWith(typed));
    },
  },
  {
    // "[" plus what's typed; in an open task tag "[video editing + au" the
    // part before the last " + " is kept.
    pattern: /\[((?:[a-z]+(?: [a-z]+)* \+ )*)([A-Za-z ]*)$/,
    when: isH3,
    entries: (prefix, fragment) => {
      const typed = fragment.toLowerCase();
      const entries = [];
      if (!prefix) {
        entries.push(
          { token: `[Shot ${nextShot()}]`, label: "next shot", insert: `[Shot ${nextShot()}] §`, kind: "shot" },
          { token: "[English]", label: "language tag inside <d>", insert: "[English] §", kind: "lang" },
          { token: "[unclear]", label: "unintelligible span", insert: "[unclear]", kind: "tag" },
        );
      }
      if (isH3Reference()) {
        const used = prefix.split(" + ").filter(Boolean);
        const after = $("#prompt").value.slice($("#prompt").selectionEnd);
        H3_TASK_TYPES.filter((type) => !used.includes(type)).forEach((type) => entries.push({
          token: `[${prefix}${type}]`, label: "task type", kind: "task",
          insert: `[${prefix}${type}${after.startsWith("]") ? "" : "] "}§`,
        }));
      }
      return entries.filter((entry) => entry.token.slice(1 + (entry.kind === "task" ? prefix.length : 0)).toLowerCase().startsWith(typed));
    },
  },
  { // retention_analysis: "<Subject 1> (appears in [Shot 1]): fu" / "<Audio 1>: "
    pattern: /(?:^|\n)<\s*(Subject|Picture|Image|Video|Audio)\s+\d+\s*>(?:[ \t]*\([^()\n]*\))?:[ \t]*([a-z_]*)$/i,
    when: isH3Reference,
    entries: (kind, fragment) => (/audio/i.test(kind)
      ? ["fully_copy", "partially_copy", "reference", "weak_reference"]
      : ["fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference"])
      .filter((marker) => marker.startsWith(fragment) && marker !== fragment)
      .map((marker) => ({ token: marker, label: RETENTION_TIPS[marker], insert: `${marker} - §`, kind: "marker" })),
    lastGroupOnly: true,
  },
  { // section keys at the start of a line
    pattern: /(?:^|\n)([a-z_]{2,})$/,
    when: isH3,
    tabOnly: true,
    entries: (fragment) => (isH3Reference() ? H3_REF_SECTION_KEYS : H3_SECTION_KEYS)
      .filter((key) => key.startsWith(fragment) && key !== fragment)
      .map((key) => ({ token: `${key}:`, label: "section - Tab", insert: isH3Reference() ? `${key}:\n§` : `${key}: §`, kind: "key" })),
  },
  { // Agentic finishing triggers: "post edit", "stitch"
    pattern: FINISH_FRAGMENT,
    when: isH3,
    entries: finishSuggestions,
    lastGroupOnly: true,
  },
];

function updateAliasMenu() {
  const textarea = $("#prompt");
  if (textarea.selectionStart !== textarea.selectionEnd) return closeAliasMenu();
  const before = textarea.value.slice(0, textarea.selectionStart);
  for (const completer of ALIAS_COMPLETERS) {
    if (completer.when && !completer.when()) continue;
    const match = before.match(completer.pattern);
    if (!match) continue;
    const matches = completer.entries(...match.slice(1));
    if (!matches.length) continue;
    // Only the typed fragment is replaced for retention markers; otherwise the whole match.
    const replace = completer.lastGroupOnly ? match[match.length - 1].length : match[0].replace(/^\n/, "").length;
    const previous = state.aliasMatches[state.aliasIndex]?.token;
    state.aliasMatches = matches.map((entry) => ({ ...entry, replace }));
    state.aliasIndex = Math.max(0, state.aliasMatches.findIndex((entry) => entry.token === previous));
    state.aliasTabOnly = Boolean(completer.tabOnly);
    return renderAliasMenu();
  }
  closeAliasMenu();
}

// Where the caret is on screen: the highlight layer wraps text exactly like the
// textarea, so the text before the caret is laid out in it with a marker (and
// the layer restored straight after, before anything is painted).
function caretClientRect() {
  const textarea = $("#prompt");
  const layer = $("#prompt-highlight");
  const saved = layer.innerHTML;
  layer.innerHTML = `${escapeHtml(textarea.value.slice(0, textarea.selectionStart))}<span data-caret>\u200b</span>`;
  layer.scrollTop = textarea.scrollTop;
  const rect = $("[data-caret]", layer).getBoundingClientRect();
  layer.innerHTML = saved;
  layer.scrollTop = textarea.scrollTop;
  return rect;
}

// Normally the menu hangs under the prompt box; in fullscreen that edge is off
// screen, so it opens at the caret instead (above it when there's no room below).
function placeAliasMenu(menu, fullscreen) {
  if (!fullscreen) { menu.style.top = ""; menu.style.left = ""; return; }
  const editor = menu.parentElement.getBoundingClientRect();
  const caret = caretClientRect();
  const left = Math.max(0, Math.min(caret.left - editor.left, editor.width - menu.offsetWidth));
  let top = caret.bottom - editor.top + 4;
  const view = window.visualViewport;
  const visibleBottom = view ? view.offsetTop + view.height : window.innerHeight; // above the phone keyboard
  if (caret.bottom + 4 + menu.offsetHeight > visibleBottom - 8) top = caret.top - editor.top - menu.offsetHeight - 4;
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.max(0, top)}px`;
}

function renderAliasMenu() {
  const menu = $("#alias-menu");
  const fullscreen = $("#prompt-suite").classList.contains("is-fullscreen");
  menu.innerHTML = state.aliasMatches.map((entry, index) => {
    const label = entry.label ? `<span class="alias-option-label">${escapeHtml(entry.label)}</span>` : "";
    // Fullscreen: a small picture of the reference above its token, so <Picture 2> vs <Picture 3> is obvious.
    const thumb = fullscreen && /^(<(Picture|Image|Video) \d+>|@\w+)$/i.test(entry.token) ? referenceThumb(entry.token, entry.reference) : null;
    const kind = entry.kind ? ` hl-${entry.kind}` : "";
    return `<button type="button" class="alias-option${kind} ${index === state.aliasIndex ? "is-active" : ""}" role="option" data-category="${entry.category || entry.kind || "image"}" data-alias-index="${index}">${thumb ? `<img class="alias-option-thumb" src="${escapeHtml(thumb)}" alt="">` : ""}<span class="alias-option-token">${escapeHtml(entry.token)}</span>${label}</button>`;
  }).join("");
  menu.hidden = false;
  $$('[data-alias-index]', menu).forEach((option) => option.addEventListener("mousedown", (event) => {
    event.preventDefault();
    insertAlias(state.aliasMatches[Number(option.dataset.aliasIndex)]);
  }));
  placeAliasMenu(menu, fullscreen);
  // Re-place once thumbnails have loaded and changed the menu's height.
  $$("img", menu).forEach((img) => img.addEventListener("load", () => placeAliasMenu(menu, fullscreen), { once: true }));
  menu.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
}

function insertAlias(entry) {
  const textarea = $("#prompt");
  const end = textarea.selectionStart;
  const start = end - entry.replace;
  const caret = entry.insert.indexOf("§");
  const text = entry.insert.replace("§", "");
  textarea.focus();
  textarea.setSelectionRange(start, end);
  // execCommand keeps the completion on the browser's undo stack (Ctrl+Z).
  if (!document.execCommand("insertText", false, text)) textarea.setRangeText(text, start, end, "end");
  const position = start + (caret < 0 ? text.length : caret);
  textarea.setSelectionRange(position, position);
  closeAliasMenu();
}

function closeAliasMenu() {
  $("#alias-menu").hidden = true;
  state.aliasMatches = [];
}

function handleAliasKeys(event) {
  if (!state.aliasMatches.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    state.aliasIndex = (state.aliasIndex + direction + state.aliasMatches.length) % state.aliasMatches.length;
    renderAliasMenu();
  } else if (event.key === "Tab" || (event.key === "Enter" && !state.aliasTabOnly)) {
    event.preventDefault();
    insertAlias(state.aliasMatches[state.aliasIndex]);
  } else if (event.key === "Escape") closeAliasMenu();
}

/* ---------------------------------------------------------------- ui -- */

function bindUI() {
  bindPromptHighlight();
  bindSnippetResize();
  bindTriggerTip();
  bindLoraHover();
  bindRefHover();
  bindCameraPreview();
  bindAliasDialog();
  bindScrollReleasesFocus();
  bindMobileTabs();
  bindViewTabs();
  bindCompare();
  bindBestReasons();
  bindDurationMeter();
  bindLoraGallery();
  // Any settings change re-estimates (debounced). Typing in the prompt doesn't affect render time.
  ["input", "change"].forEach((type) => $("#generate-form").addEventListener(type, (event) => {
    if (event.target.id !== "prompt") scheduleEstimate();
  }));
  $("#prompt-snippets").addEventListener("mousedown", (event) => { if (event.target.closest("[data-snippet]")) event.preventDefault(); });
  // A plain mouse wheel scrolls the one-line bar sideways.
  $("#prompt-snippets").addEventListener("wheel", (event) => {
    const bar = event.currentTarget;
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || bar.scrollWidth <= bar.clientWidth) return;
    event.preventDefault();
    bar.scrollLeft += event.deltaY;
  }, { passive: false });
  $("#prompt-snippets").addEventListener("click", (event) => {
    const button = event.target.closest("[data-snippet]");
    if (!button) return;
    const item = state.promptSnippets[Number(button.dataset.snippet)];
    if (item.action) { item.action(); return; } // the Camera group's (+)/(−)
    showSnippetHint(button); // before inserting, so the preview shows what this click adds
    insertSnippet(item);
  });
  // The caption under the bar explains the hovered / focused / tapped chip
  // (phones never show the title tooltip).
  $("#prompt-snippets").addEventListener("pointerover", (event) => {
    if (event.pointerType === "mouse") showSnippetHint(event.target.closest("[data-snippet]"));
  });
  $("#prompt-snippets").addEventListener("focusin", (event) => showSnippetHint(event.target.closest("[data-snippet]")));
  ["#minimax-lora-list", "#ltx-lora-list"].forEach((host) => $(host).addEventListener("change", () => { renderPromptSnippets(); updateLoraCount(); }));
  $("#alerts-toggle").addEventListener("click", toggleAlerts);
  document.addEventListener("pointerdown", unlockAudio, { once: true });

  $("#job-info-close").addEventListener("click", closeJobInfo);
  $("#job-info-backdrop").addEventListener("click", closeJobInfo);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#job-info").hidden) closeJobInfo();
  });
  $("#ref-viewer-close").addEventListener("click", closeViewer);
  $("#ref-viewer-backdrop").addEventListener("click", closeViewer);
  $("#ref-viewer-prev").addEventListener("click", () => viewerNavigate(-1));
  $("#ref-viewer-next").addEventListener("click", () => viewerNavigate(1));
  document.addEventListener("keydown", (event) => {
    if (!state.viewer) return;
    if (event.key === "Escape") closeViewer();
    else if (event.key === "ArrowLeft") viewerNavigate(-1);
    else if (event.key === "ArrowRight") viewerNavigate(1);
  });

  $("#model").addEventListener("change", async (event) => {
    await setModel(event.target.value);
    await applySelectedPreset();
    saveSessionSettings();
  });
  ["input", "change"].forEach((type) => $("#generate-form").addEventListener(type, (event) => {
    if (event.target.id !== "prompt") scheduleSessionSettingsSave();
  }));
  $("#multi-gpu-offload").addEventListener("change", () => {
    $("#multi-gpu-split-row").hidden = $("#multi-gpu-offload").value !== "split";
    renderMultiGpuPresets();
    scheduleSessionSettingsSave();
    updatePresetStatus();
  });
  $("#multi-gpu-split-gb").addEventListener("change", () => {
    renderMultiGpuPresets();
    scheduleSessionSettingsSave();
    updatePresetStatus();
  });
  $("#multi-gpu-preset").addEventListener("change", (event) => {
    if (event.currentTarget.value) applyMultiGpuPreset(event.currentTarget.value);
    else renderMultiGpuPresets();
  });
  $("#multi-gpu-preset-save").addEventListener("click", saveMultiGpuPreset);
  $("#multi-gpu-preset-delete").addEventListener("click", deleteMultiGpuPreset);
  $("#settings-reset").addEventListener("click", resetSettings);
  $("#mode").addEventListener("change", (event) => setMode(event.target.value));
  $("#preset").addEventListener("change", (event) => selectPreset(event.target.value));
  $("#preset-new").addEventListener("click", () => openPresetDialog());
  $("#preset-rename").addEventListener("click", () => openPresetDialog(selectedPresetName()));
  bindPresetDialog();
  $("#preset-save").addEventListener("click", updateSelectedPreset);
  $("#preset-delete").addEventListener("click", deleteSelectedPreset);
  // After the handlers above have applied the change.
  ["input", "change"].forEach((type) => $("#generate-form").addEventListener(type, (event) => {
    if (event.target.id !== "prompt" && event.target.id !== "preset") setTimeout(updatePresetStatus);
  }));

  $("#first-frame-upload").addEventListener("click", () => $("#first-frame-file").click());
  $("#last-frame-upload").addEventListener("click", () => $("#last-frame-file").click());
  $("#reference-upload").addEventListener("click", () => $("#reference-file").click());
  $("#first-frame-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "first"));
  $("#last-frame-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "last"));
  $("#reference-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "references"));
  $("#first-frame-match").addEventListener("click", matchResolutionToReference);
  $("#first-frame-remove").addEventListener("click", () => { state.firstFrameId = null; restoreResolutionSnapshot(); renderKeyframes(); });
  $("#last-frame-remove").addEventListener("click", () => { state.lastFrameId = null; renderKeyframes(); });
  bindDropZones($("#keyframe-panel"));
  bindDropZones($("#reference-panel"));

  $("#sh-image-upload").addEventListener("click", () => $("#sh-image-file").click());
  $("#sh-video-upload").addEventListener("click", () => $("#sh-video-file").click());
  $("#sh-audio-upload").addEventListener("click", () => $("#sh-audio-file").click());
  $("#sh-image-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "sh-images"));
  $("#sh-video-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "sh-videos"));
  $("#sh-audio-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "sh-audios"));
  $("#sh-first-frame-upload").addEventListener("click", () => $("#sh-first-frame-file").click());
  $("#sh-last-frame-upload").addEventListener("click", () => $("#sh-last-frame-file").click());
  $("#sh-first-frame-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "sh-first"));
  $("#sh-last-frame-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "sh-last"));
  $("#sh-first-frame-remove").addEventListener("click", () => { state.shFirstFrameId = null; renderKeyframes(); renderSeedhunterGalleries(); });
  $("#sh-last-frame-remove").addEventListener("click", () => { state.shLastFrameId = null; renderKeyframes(); renderSeedhunterGalleries(); });
  bindDropZones($("#seedhunter-reference-panel"));

  $("#sh-upscale-enable").addEventListener("change", () => {
    const enabled = $("#sh-upscale-enable").checked;
    $("#sh-upscale-params").hidden = !enabled;
    $("#sh-upscale-seed-row").hidden = !enabled;
  });
  $("#sh-rife-enable").addEventListener("change", () => { $("#sh-rife-params").hidden = !$("#sh-rife-enable").checked; });
  $("#sh-random-upscale-seed").addEventListener("click", () => { $("#sh-upscale-seed").value = Math.floor(Math.random() * 2147483648); });
  $("#h3-upscale-enable").addEventListener("change", () => {
    $("#h3-upscale-params").hidden = !$("#h3-upscale-enable").checked;
    updateUpscaleCheck();
  });
  $("#cfg").addEventListener("input", renderCfg);
  [$("#mode"), $("#ref2v-turbo"), $("#minimax-checkpoint"), ...$$('#h3-speedups input[type="checkbox"]')]
    .forEach((input) => input.addEventListener("change", () => setTimeout(renderCfg))); // after setMode() etc.
  ["#h3-upscale-factor", "#h3-upscale-steps", "#h3-upscale-denoise", "#duration"].forEach((id) => $(id).addEventListener("input", updateUpscaleCheck));
  $("#h3-upscale-chunking").addEventListener("change", (event) => {
    const input = event.currentTarget;
    if (!input.checked && !confirm("Turn off upscale chunking?\n\nWithout it the 3D upscaler processes the whole clip in one go. "
      + "On long clips that needs more VRAM than the card has: 15 s at 1856 × 1024 asked for one 21.4 GiB block and ran "
      + "out of memory after the full first pass (about 9 minutes).\n\nOnly turn it off for short clips or to compare seams.")) {
      input.checked = true;
      return;
    }
    updateUpscaleCheck();
  });
  $("#h3-random-upscale-seed").addEventListener("click", () => { $("#h3-upscale-seed").value = Math.floor(Math.random() * 2147483648); });
  $$("[data-ua]").forEach((input) => input.addEventListener(input.tagName === "SELECT" || input.type === "checkbox" ? "change" : "input", () => {
    renderUpscaleAdvanced();
    updateUpscaleCheck();
  }));
  $("#h3-upscale-chunking").addEventListener("change", renderUpscaleAdvanced);
  ["#h3-upscale-factor", "#h3-upscale-steps", "#h3-upscale-denoise"].forEach((id) => $(id).addEventListener("input", renderUpscalePresets));
  $("#h3-upscale-preset").addEventListener("change", (event) => {
    if (event.currentTarget.value) applyUpscalePreset(event.currentTarget.value);
    else renderUpscalePresets(); // "Custom" isn't something to apply
  });
  $("#h3-upscale-preset-save").addEventListener("click", saveUpscalePreset);
  $("#h3-upscale-preset-delete").addEventListener("click", deleteUpscalePreset);
  $("#h3-ua-reset").addEventListener("click", () => {
    writeUpscaleAdvanced({});
    updateUpscaleCheck();
    scheduleSessionSettingsSave();
  });
  // Every path that changes the render size (megapixels, typed size, Match
  // resolution, reverting a match, Retry) rewrites #resolution-detail, so
  // watching it keeps the upscale target and warnings current.
  new MutationObserver(updateUpscaleCheck).observe($("#resolution-detail"), { childList: true, characterData: true, subtree: true });
  [$("#mode"), $("#ref2v-turbo"), $("#ref2v-turbo-enabled"), $("#minimax-checkpoint")]
    .forEach((input) => input.addEventListener("change", () => setTimeout(updateUpscaleCheck))); // after setMode() etc.
  $("#sh-continue-clear").addEventListener("click", clearContinuation);

  $("#generate-form").addEventListener("submit", (event) => { event.preventDefault(); generate(); });
  // A field that fails the browser's validation inside a closed section (card
  // or sub-card) blocks Generate silently, since it can't be focused. Open its
  // sections so the browser can show the message.
  // A field the user can't see at all (another model's or mode's panel) can't
  // be focused, so an off-step or out-of-range value there blocked Generate
  // with no message. The click runs before the form validates: snap those
  // fields to a valid value first; visible ones still get the browser's message.
  $("#generate-button").addEventListener("click", repairHiddenInvalidFields, true);
  // Enter in a field (or a phone keyboard's Go key) is an implicit form
  // submit - it queued a render from the seed/steps/width/... boxes. Generate
  // only from the button or Ctrl/Cmd+Enter in the prompt; Enter just commits.
  $("#generate-form").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing || event.target.tagName !== "INPUT") return;
    if (["checkbox", "radio", "button", "submit"].includes(event.target.type)) return;
    event.preventDefault();
    if (window.matchMedia?.("(pointer: coarse)").matches) event.target.blur(); // closes the keyboard; blur commits
    else event.target.dispatchEvent(new Event("change", { bubbles: true }));
  });
  $("#generate-form").addEventListener("invalid", (event) => {
    for (let node = event.target.parentElement; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true;
  }, true);
  $("#enhance").addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); enhance(); });
  $("#agentic").addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); agentic(); });
  $("#enhance-undo").addEventListener("click", undoEnhance);
  $("#enhance-settings").addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); setView("enhance"); });
  const extraPrompt = $("#enhance-extra-prompt");
  try { extraPrompt.value = localStorage.getItem("h3-enhance-extra-prompt") || ""; } catch { /* storage unavailable */ }
  extraPrompt.addEventListener("input", () => { try { localStorage.setItem("h3-enhance-extra-prompt", extraPrompt.value); } catch { /* storage unavailable */ } });
  $("#enhance-back").addEventListener("click", () => setView("studio"));
  bindEnhancement();
  $("#prompt").addEventListener("input", updateAliasMenu);
  $("#prompt").addEventListener("keydown", handleAliasKeys);
  $("#prompt").addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") generate(); });

  [$("#megapixels"), $("#aspect")].forEach((input) => input.addEventListener("change", updateResolution));
  [$("#width"), $("#height")].forEach((input) => input.addEventListener("change", updateMegapixelsFromResolution));
  $("#duration").addEventListener("input", () => { $("#duration-readout").textContent = durationReadout($("#duration").value); });

  $("#random-seed").addEventListener("click", () => { $("#seed").value = Math.floor(Math.random() * 2147483648); });
  $$('#h3-speedups input[type="checkbox"]').forEach((box) => box.addEventListener("change", renderSpeedups));
  $("#attention-backend").addEventListener("change", renderSpeedups);
  $("#minimax-checkpoint").addEventListener("change", renderSpeedups);
  $("#minimax-checkpoint").addEventListener("change", updateCheckpointHint);
  $("#checkpoint-rescan").addEventListener("click", rescanCheckpoints);
  $("#ref2v-turbo").addEventListener("change", renderSpeedups);
  $$("[data-lora-reset]").forEach((button) => button.addEventListener("click", () => {
    resetLoraStrengths($(button.dataset.loraReset));
    toast("LoRA strengths reset to recommended.");
  }));
  $("#ltx-tiles").addEventListener("change", () => { $("#ltx-tile-detail").textContent = ltxTileDetails[$("#ltx-tiles").value]; });
  // Wan turbo: the lightx2v / Slow-Mo Fix LoRAs are distilled for 4 steps at CFG 1; the full model wants ~20 at 3.5.
  $("#wan-turbo").addEventListener("change", () => {
    const turbo = $("#wan-turbo").value !== "off";
    $("#wan-steps").value = turbo ? 4 : 20;
    $("#wan-cfg").value = turbo ? 1 : 3.5;
    $("#wan-high-steps").value = "";
    scheduleEstimate();
  });

  $$('[data-filter]').forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    $$('[data-filter]').forEach((item) => item.classList.toggle("is-active", item === button));
    resetJobsPage();
    renderJobs();
  }));
  $("#jobs-search").addEventListener("input", (event) => {
    state.jobSearch = event.target.value;
    // New long-prompt jobs since the last load aren't in fullPrompts yet: refresh then.
    if (state.jobSearch && state.jobs.some((job) => job.prompt_truncated && !fullPrompts?.[job.id])) loadFullPrompts();
    resetJobsPage();
    renderJobs();
  });
  $("#jobs-list").addEventListener("scroll", maybeLoadMoreJobs, { passive: true });
  window.addEventListener("scroll", maybeLoadMoreJobs, { passive: true });
  window.addEventListener("resize", maybeLoadMoreJobs, { passive: true });
  $("#jobs-show-more").addEventListener("click", () => {
    state.jobsShown += JOBS_PAGE;
    renderJobs();
  });

  $("#stitch-toggle").addEventListener("click", () => toggleStitchMode());
  $("#stitch-cancel").addEventListener("click", () => toggleStitchMode(false));
  $("#stitch-complete").addEventListener("click", completeStitch);
  $("#stitch-settings-toggle").addEventListener("click", () => {
    const panel = $("#stitch-settings");
    const willShow = panel.hidden;
    panel.hidden = !willShow;
    $("#stitch-settings-toggle").classList.toggle("is-enabled", willShow);
    $("#stitch-settings-toggle").setAttribute("aria-pressed", willShow ? "true" : "false");
  });
  $("#stitch-transition").addEventListener("change", updateStitchTransitionFields);
  $("#stitch-resolution-mode").addEventListener("change", () => {
    $("#stitch-resolution-custom").hidden = $("#stitch-resolution-mode").value !== "custom";
  });
  $("#stitch-fps-match").addEventListener("change", () => {
    $("#stitch-fps").disabled = $("#stitch-fps-match").checked;
  });
  $("#stitch-music-volume").addEventListener("input", () => {
    $("#stitch-music-volume-readout").textContent = `${$("#stitch-music-volume").value}%`;
  });
  $("#stitch-music-upload").addEventListener("click", () => $("#stitch-music-file").click());
  $("#stitch-music-file").addEventListener("change", (event) => uploadFiles([...event.target.files], "stitch-music"));
  bindUrlMusic("#stitch-music-url", "#stitch-music-url-add", "stitch-music", (reference) => { state.stitchMusicId = reference.id; renderStitchMusic(); });
  bindUrlMusic("#reference-url", "#reference-url-add", "references", (reference) => { renderReferenceGallery(); openTrimEditorIfOverBudget([reference]); });
  bindDropZones($("#stitch-settings"));
}

/* ---------------------------------------------------- speedup & saves -- */

// What each Speedup & Saves option does for the given mode and the selected
// checkpoint. `locked` is set only where there's a tested reason the option
// can't apply - never just to keep a default. Mirrors build_workflow().
function speedupStatus(mode = state.mode) {
  const ref = mode === "ref2v";
  const experimentalRef = ref && $("#experimental-ref-speedups").checked;
  const checkpoint = $("#minimax-checkpoint").value;
  const defaultText = !checkpoint || checkpoint === state.options.fl2v_default_checkpoint;
  const defaultRef = !checkpoint || checkpoint === state.options.ref2v_turbo_checkpoint;
  const slaOn = $("#sla-enabled").checked;
  const row = (box, status) => ({ box: $(box), ...status });
  return {
    parasyte: row("#parasyte-enabled", ref
      ? { locked: "Not used in Reference: Reference runs a separate Ref2V model, and Parasyte is trained for the Text/Keyframe model." }
      : { why: "Turbo LoRA trained for the Text/Keyframe model. Tuned for 6 steps - raise Steps if you turn it off." }),
    lightx: row("#ref2v-turbo-enabled", !ref
      ? { locked: "Only used in Reference: it's trained for the Ref2V model, not the Text/Keyframe one." }
      : !defaultRef
        ? { locked: "Skipped on this checkpoint: it's tuned for the default Ref2V checkpoint and caused heavy artifacts on others (tested Sept 22)." }
        : $("#ref2v-turbo").value === "pdd"
          ? { numberLocked: true, why: "Alibaba PAI's official 8-step accelerator for the Ref2V model. Runs its own fixed 8-step schedule at strength 1, so Steps and the strength box don't apply. Clean at 720p (tested Sept 24)." }
          : { why: "LightX 8-step turbo. At 1280x704 it turned Reference renders into ghosted streaks in testing (Sept 24); fine at small sizes. Raise Steps if you turn it off." }),
    sla: row("#sla-enabled", ref && !experimentalRef
      ? { locked: "Not available in Reference: the Ref2V model isn't distilled for sparse attention - in tests SLA broke the opening or produced noise (Sept 23)." }
      : { why: experimentalRef ? "Experimental in Reference: this prompt showed no speed gain and changed the ending composition."
          : defaultText
          ? "The default turbo checkpoint was distilled to run sparse. Off = full dense attention: slower, exact."
          : "Distilled for the default turbo checkpoint - untested on this one. Off = full dense attention: slower, exact." }),
    refvideo: row("#ref-video-downscale", !ref
      ? { locked: "Only Reference mode takes video references." }
      : { why: "Loads each reference video at half size (a quarter of the tokens). About 2x faster with a video reference, same shot (tested 170s vs 341s)." }),
    sladense: row("#sla-dense-step", ref
      ? { locked: "SLA isn't used in Reference." }
      : !slaOn
        ? { locked: "SLA is off." }
        : { why: "On: one step runs full dense attention (about 3x a sparse step). Off: every step sparse, as the turbo checkpoint was distilled. 150s vs 171s (5s 720p), same look." }),
    spectrum: row("#spectrum-enabled", ref && !experimentalRef
      ? { locked: "Not available in Reference: it gave ghosted, washed-out frames there (tested Sept 23)." }
      : { why: experimentalRef ? "Experimental in Reference: saved about 4s here but produced doubled edges and changed the ending."
          : "Forecasts some steps instead of running them. 117s vs 171s (5s 720p, with SLA all-sparse); a different take at the same quality. Untested together with the block cache." }),
    blockcache: row("#block-cache-enabled", { why: ref
      ? SPEEDUP_NOTES.blockcacheRef
      : "Skips the transformer on steps whose features barely change. 132s vs 171s (5s 720p); a different take at the same quality." }),
    chunking: row("#ffn-chunking-enabled", { why: "Runs the feed-forward in 4 chunks. Bit-identical output, peak VRAM 8.1 -> 6.3 GiB, same speed. Use it if renders run out of VRAM."
      + (ref ? " In Reference it gave NaN renders in 2 of 2 tests on Sept 24, before full VRAM was found to break renders; re-enabled Sept 30, not re-tested yet." : "") }),
    compile: row("#torch-compile-enabled", { why: SPEEDUP_NOTES.compile }),
    lowvram: row("#low-vram-attention-enabled", { why: "Runs attention in head groups (the number) and frees its buffers early, so H3 samples with less VRAM. Same math, same output. For renders that fill the card: 15s Fast Stable sat at 24.4 of 24.6 GiB and came out NaN/corrupted (Sept 30). Works in every mode; speed cost and the exact saving not measured yet." }),
    attention: row("#attention-backend", { why: {
      flash: "Flash attention: the fastest and the closest to exact. 102s vs 111s for Kitchen and 175s for PyTorch (Fast Stable, 5s 928x512, Sept 29).",
      kitchen: "Comfy-Kitchen INT8 attention: about 8% slower than Flash on this GPU and less exact (29 vs 35 dB against PyTorch).",
      pytorch: "Plain PyTorch attention: exact reference, but about 70% slower than Flash.",
    }[$("#attention-backend").value] + (ref || !slaOn ? "" : " With SLA on it covers only SLA's dense steps and short sequences.") }),
  };
}

// Reference-mode block cache result (Ref2V bench, 2026-09-24).
const SPEEDUP_NOTES = {
  compile: "Compiles H3's transformer blocks with PyTorch inductor. Tested Sept 25 at 416x224, 5s: the first run spent about 4s extra compiling, warm runs sampled in 9s vs 11s with clean output (a slightly different take). Not measured at 720p yet. Changing resolution, duration, mode, references, checkpoint or a speedup patch compiles again. Compiled runs are left out of the time estimate.",
  blockcacheRef: "Skips the transformer on steps whose features barely change. With PDD: 253s vs 339s sampling (5s 720p), same identity, a different take.",
};

function renderSpeedups() {
  const status = speedupStatus();
  $("#experimental-ref-speedups-row").hidden = state.mode !== "ref2v";
  $$("[data-speedup]", $("#h3-speedups")).forEach((row) => {
    const item = status[row.dataset.speedup];
    const locked = Boolean(item.locked);
    row.classList.toggle("is-locked", locked);
    item.box.disabled = locked; // keeps its checked state for when it applies again
    const number = $('input[type="number"]', row);
    if (number) number.disabled = locked || !item.box.checked || Boolean(item.numberLocked);
    const choice = $("select", row);
    if (choice && choice !== item.box) choice.disabled = locked || !item.box.checked;
    $(".speedup-why", row).textContent = item.locked || item.why;
  });
}

function fillSelect(id, values) {
  const select = $(id);
  select.innerHTML = values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
}

// Reference mode tags every checkpoint that isn't Ref2V-trained - still
// selectable. The verdict comes from the server's weight fingerprint (see
// backend/checkpoints.py), so newly downloaded models are classified too.
// Each checkpoint's weight fingerprint (backend/checkpoints.py) for the
// current mode: Reference shows how Ref2V-like the weights are; Text and
// Keyframe - one and the same FL2V model in H3 - show how FL2V-like. Every
// entry stays selectable.
function checkpointPercent(item, mode = state.mode) {
  if (item?.ref2v_score == null) return null;
  const ref2v = Math.max(0, Math.min(1, item.ref2v_score));
  return Math.round((mode === "ref2v" ? ref2v : 1 - ref2v) * 100);
}

function checkpointEntry(value) {
  const list = state.options?.minimax_h3_checkpoints || [];
  const defaultValue = state.mode === "ref2v" ? state.options?.ref2v_turbo_checkpoint : state.options?.fl2v_default_checkpoint;
  return list.find((item) => item.value === (value || defaultValue))
    || (value ? null : state.options?.scanned_checkpoints?.find((item) => item.value === defaultValue));
}

function renderCheckpointOptions() {
  const select = $("#minimax-checkpoint");
  const current = select.value;
  const kind = state.mode === "ref2v" ? "Ref2V" : "FL2V";
  const suffix = (item) => {
    const percent = checkpointPercent(item);
    return ` (${kind} ${percent == null ? "?" : `${percent}%`})`;
  };
  fillOptions("#minimax-checkpoint", [
    { value: "", label: `Workflow default${suffix(checkpointEntry(""))}` },
    ...(state.options?.minimax_h3_checkpoints || []).map((item) => ({ value: item.value, label: `${item.label}${item.gguf ? " · GGUF" : ""}${suffix(item)}` })),
  ]);
  select.value = [...select.options].some((option) => option.value === current) ? current : "";
  updateCheckpointHint();
}

function updateCheckpointHint() {
  const hint = $("#checkpoint-ref2v-hint");
  const item = checkpointEntry($("#minimax-checkpoint").value);
  const percent = checkpointPercent(item);
  hint.hidden = Boolean(listLoras()) || !item;
  if (hint.hidden) return;
  const ref = state.mode === "ref2v";
  if (percent == null) {
    hint.textContent = "Couldn't measure this model (the official Ref2V/FL2V checkpoints weren't found to compare against).";
    hint.classList.remove("is-warning");
    return;
  }
  hint.textContent = ref
    ? {
      ref2v: `Ref2V-trained - weights ${percent}% toward the official Ref2V model.`,
      hybrid: `Partly Ref2V-trained - weights ${percent}% toward the official Ref2V model; reference identity may be weaker.`,
      fl2v: `Not Ref2V-trained - weights ${percent}% toward the official Ref2V model. It will run, but references may be ignored or come out wrong.`,
    }[item.ref2v] || ""
    // Text and Keyframe are the same FL2V model, and every H3 checkpoint
    // runs them (the default turbo checkpoint measures FL2V 0%), so this
    // is lineage only - never a warning.
    : `Weights ${percent}% toward the official FL2V model (Text and Keyframe share it). Lineage only - every H3 checkpoint runs Text/Keyframe.`;
  hint.classList.toggle("is-warning", ref && (item.ref2v === "fl2v" || item.ref2v === "hybrid"));
}

async function rescanCheckpoints() {
  const button = $("#checkpoint-rescan");
  button.disabled = true;
  try {
    const result = await Api.request(`${API_BASE}/api/checkpoints/rescan`, { method: "POST" });
    const before = (state.options.minimax_h3_checkpoints || []).length;
    state.options.minimax_h3_checkpoints = result.minimax_h3_checkpoints;
    state.options.scanned_checkpoints = result.scanned_checkpoints;
    renderCheckpointOptions();
    const added = result.minimax_h3_checkpoints.length - before;
    toast(added > 0 ? `Found ${added} new H3 model${added === 1 ? "" : "s"}.` : "No new H3 models found.");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
}

function fillOptions(id, values) {
  const select = $(id);
  select.innerHTML = values.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
}

// One LoRA row, shared by the MiniMax H3 and LTX 2.5 lists. The "i" links to
// the LoRA's source page; LoRAs with trigger words get a "!" whose hover/focus
// tooltip lists them, and are tinted while enabled as a reminder to put a
// trigger in the prompt. Strength starts at the recommended value, which
// data-default-strength keeps for the Reset button.
function loraRowHtml(item, dataAttr, checked) {
  const triggers = item.triggers || [];
  const tip = `Trigger words:\n${triggers.map((word) => `• ${word}`).join("\n")}`;
  const strengthTitle = `Recommended ${item.default_strength}${item.strength_note ? ` — ${item.strength_note}` : ""}`;
  return `
    <div class="field-row lora-row${triggers.length ? " has-trigger" : ""}${checked ? " is-enabled" : ""}" ${dataAttr}>
      <label title="${escapeHtml(item.filename)}">
        <input type="checkbox" data-lora-enabled ${checked ? "checked" : ""}>
        <span class="lora-name">${escapeHtml(item.label)}</span>
        ${item.base ? `<span class="hint" title="Trained on Wan 2.2 ${escapeHtml(item.base)}; ${item.base === "T2V" ? "Text" : "Keyframe"} mode uses those experts">${escapeHtml(item.base)}</span>` : ""}
      </label>
      <span class="lora-badges">
        ${triggers.length ? `<span class="lora-badge lora-badge-trigger" tabindex="0" aria-label="${escapeHtml(tip)}" data-tip="${escapeHtml(tip)}">!</span>` : ""}
        ${item.url ? `<a class="lora-badge" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer" title="Open source page — ${escapeHtml(strengthTitle)}">i</a>` : ""}
      </span>
      <input type="number" class="lora-strength" data-lora-strength data-default-strength="${item.default_strength}" min="0" max="2" step="0.05" value="${item.default_strength}" title="${escapeHtml(strengthTitle)}">
    </div>`;
}

// Rows with a small heading whenever the group changes (General / 2D
// Animation / Anime / 3D Animation...). Headings carry no data attributes, so
// the collect*() readers only ever see real rows.
// Rows are gathered by group (in order of each group's first appearance) so
// LoRAs appended later to the positional H3 slot list still land under the
// right heading; `index` passed to rowHtml is always the item's ORIGINAL
// position, which is what the backend's slot list is keyed on.
function loraListHtml(items, rowHtml, model) {
  const catalog = state.options?.lora_gallery_catalog?.[model] || {};
  const groupOrder = [...(catalog.categories || [])];
  for (const item of items) if (!groupOrder.includes(item.group || "")) groupOrder.push(item.group || "");
  const fileOrder = catalog.order || [];
  const rank = (name) => { const index = fileOrder.indexOf(name); return index < 0 ? Number.MAX_SAFE_INTEGER : index; };
  const ordered = items.map((item, index) => ({ item, index }))
    .sort((a, b) => groupOrder.indexOf(a.item.group || "") - groupOrder.indexOf(b.item.group || "") || rank(a.item.filename) - rank(b.item.filename) || a.index - b.index);
  let group = null;
  return ordered.map(({ item, index }) => {
    const heading = !item.deleted && item.group && item.group !== group ? `<div class="lora-group">${escapeHtml(item.group)}</div>` : "";
    if (!item.deleted) group = item.group;
    return heading + rowHtml(item, index);
  }).join("");
}

function bindLoraList(host, onChange) {
  host.onchange = (event) => {
    const box = event.target.closest("[data-lora-enabled]");
    if (box) box.closest(".lora-row").classList.toggle("is-enabled", box.checked);
    onChange?.();
  };
}

function resetLoraStrengths(host) {
  $$("[data-lora-strength]", host).forEach((input) => { input.value = input.dataset.defaultStrength; });
}

function renderLoraList(slots) {
  const host = $("#minimax-lora-list");
  host.innerHTML = loraListHtml(slots, (slot, index) => slot.deleted
    ? `<div data-lora-index="${index}" hidden><input type="checkbox" data-lora-enabled disabled><input data-lora-strength data-default-strength="1" value="1"></div>`
    : loraRowHtml(slot, `data-lora-index="${index}"`, slot.default_enabled), "minimax_h3");
  bindLoraList(host);
}

function collectLoraSlots() {
  // Indexed by data-lora-index, not DOM order - rows are displayed grouped.
  const slots = [];
  $$('[data-lora-index]', $("#minimax-lora-list")).forEach((row) => {
    slots[Number(row.dataset.loraIndex)] = {
      enabled: $("[data-lora-enabled]", row).checked,
      strength: Number($("[data-lora-strength]", row).value) || 0,
    };
  });
  return slots;
}

// The LoRAs card header: how many are on in the current model's list.
function updateLoraCount() {
  const list = listLoras()?.list || "#minimax-lora-list";
  const on = $$("[data-lora-enabled]", $(list)).filter((box) => box.checked).length;
  $("#lora-count").textContent = on ? `${on} on` : "";
}

// An LTX 2.5 or Wan 2.2 list (LIST_LORAS) from /api/settings/options.
function renderListLoras(model, options) {
  const list = LIST_LORAS[model];
  const loras = options[list.key] || [];
  const max = options[list.max] || 4;
  const host = $(list.list);
  $(list.maxLabel).textContent = max;
  host.innerHTML = loraListHtml(loras, (item) => loraRowHtml(item, `data-ltx-lora="${escapeHtml(item.filename)}"`, false), model);
  const update = () => {
    const boxes = $$("[data-lora-enabled]", host);
    const checked = boxes.filter((box) => box.checked).length;
    boxes.forEach((box) => { box.disabled = !box.checked && checked >= max; });
    if (state.model === model) $("#ltx-lora-count").textContent = checked ? `(${checked} on)` : "";
  };
  bindLoraList(host, update);
  update();
}

// Enabled rows only, in list order - the order they stack on the model.
function collectListLoras(model = state.model) {
  const list = LIST_LORAS[model];
  if (!list) return [];
  return $$("[data-ltx-lora]", $(list.list))
    .filter((row) => $("[data-lora-enabled]", row).checked)
    .map((row) => ({ lora: row.dataset.ltxLora, strength: Number($("[data-lora-strength]", row).value) || 0 }));
}

/* ------------------------------------------------------------ presets -- */

// Named snapshots of a model's settings for MiniMax H3 and LTX 2.5. The prompt,
// seeds and uploaded inputs are left out, so a preset only changes settings.
// Presets live on the server (GET/PUT/DELETE /api/presets), so every browser
// and device sees the same list; state.presets is the loaded copy.
// The preset selected for each model is kept in a cookie, and reloading the page applies it again.
// Settings survive a refresh: the whole form (model, mode, every setting,
// keyframes) is kept in this browser's localStorage and put back on load in
// place of the preset, so a refresh never silently drops you into Text mode.
// "Reset" (#settings-reset) forgets it and reloads into the selected preset,
// or the defaults when no preset is selected. The prompt has its own drafts.
const SESSION_SETTINGS_KEY = "h3-session-settings";
const SETTINGS_RESET_KEY = "h3-settings-reset"; // one-shot: model to open after Reset
let sessionSettingsReady = false;
let sessionSettingsTimer = null;

function saveSessionSettings() {
  clearTimeout(sessionSettingsTimer);
  if (!sessionSettingsReady) return;
  const settings = collectSettings();
  delete settings.prompt;
  // applyRetryRequest only fills these while upscale is on; keep them either way.
  settings.h3_upscale = { factor: $("#h3-upscale-factor").value, steps: $("#h3-upscale-steps").value, denoise: $("#h3-upscale-denoise").value,
    chunking: $("#h3-upscale-chunking").checked, advanced: readUpscaleAdvanced() };
  try { localStorage.setItem(SESSION_SETTINGS_KEY, JSON.stringify(settings)); } catch { /* storage full or blocked */ }
}

function scheduleSessionSettingsSave() {
  if (!sessionSettingsReady) return;
  clearTimeout(sessionSettingsTimer);
  sessionSettingsTimer = setTimeout(saveSessionSettings, 400);
}

// Page load: put the last session's settings back. Returns false (so the
// preset/defaults apply instead) after Reset, when ?model= asks for another
// model, or when nothing usable is saved.
async function restoreSessionSettings(query) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(SESSION_SETTINGS_KEY)); } catch { /* storage blocked or corrupt */ }
  if (!saved || typeof saved !== "object" || !$(`#model option[value="${saved.model}"]`)) return false;
  if (query.get("model") && query.get("model") !== saved.model) return false;
  const exists = (id) => (id && state.references.some((reference) => reference.id === id) ? id : null);
  try {
    // Sessions saved before the attention selector: start on the new default (Flash).
    await applyRetryRequest({ ...saved, attention: saved.attention ?? "flash", prompt: promptDrafts[saved.model] ?? $("#prompt").value,
      first_frame_id: exists(saved.first_frame_id), last_frame_id: exists(saved.last_frame_id) });
    if (saved.h3_upscale) {
      setField("#h3-upscale-factor", saved.h3_upscale.factor);
      setField("#h3-upscale-steps", saved.h3_upscale.steps);
      setField("#h3-upscale-denoise", saved.h3_upscale.denoise);
      setField("#h3-upscale-chunking", saved.h3_upscale.chunking ?? true);
      writeUpscaleAdvanced(saved.h3_upscale.advanced);
    }
    renderPresetBar(); // the selected preset shows "modified" if these differ from it
    await updateResolution();
    return true;
  } catch (error) {
    console.warn("Couldn't restore the last settings", error);
    return false;
  }
}

function resetSettings() {
  const preset = PRESET_MODELS.includes(state.model) ? selectedPresetName() : "";
  const target = preset ? `the "${preset}" preset` : `the ${modelLabels[state.model] || ""} defaults`;
  if (!confirm(`Reset all settings to ${target}? Your prompt and references are kept.`)) return;
  sessionSettingsReady = false; // pagehide must not save the current form back
  clearTimeout(sessionSettingsTimer);
  savePromptDrafts();
  try {
    localStorage.removeItem(SESSION_SETTINGS_KEY);
    sessionStorage.setItem(SETTINGS_RESET_KEY, state.model);
  } catch { /* storage blocked */ }
  location.reload();
}

const PRESET_MODELS = ["minimax_h3", "ltx_2_5", "wan_2_2"];
const PRESET_STORAGE_KEY = "h3-presets"; // pre-server browser copy, migrated once by loadPresets()
// "mode" too: picking a preset never switches Text/Keyframe/Reference (presets
// saved before this still carry a mode; applyPreset and the "modified" check ignore it).
const PRESET_EXCLUDED_FIELDS = ["model", "mode", "prompt", "seed", "upscale_seed", "reference_ids", "reference_video_ids",
  "reference_audio_ids", "first_frame_id", "last_frame_id", "continue_from_job_id", "voice_only_reference_videos"];
const presetCookieName = (model) => `h3_preset_${model}`;

function readCookie(name) {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function writeCookie(name, value) {
  const maxAge = value === null ? 0 : 60 * 60 * 24 * 365;
  document.cookie = `${name}=${encodeURIComponent(value ?? "")}; path=${API_BASE || "/"}; max-age=${maxAge}; SameSite=Lax`;
}

// Loads the server's presets. Presets saved in this browser before they moved to
// the server are uploaded first (any name the server doesn't have yet), then the
// browser copy is removed.
async function loadPresets(pending = null) {
  let local = null;
  try { local = JSON.parse(localStorage.getItem(PRESET_STORAGE_KEY)); } catch { /* storage blocked */ }
  try {
    state.presets = await (pending || Api.request(`${API_BASE}/api/presets`));
    if (local && typeof local === "object") {
      for (const [model, presets] of Object.entries(local)) {
        if (!PRESET_MODELS.includes(model)) continue;
        for (const [name, settings] of Object.entries(presets || {})) {
          if (state.presets[model]?.[name]) continue;
          state.presets = await Api.request(`${API_BASE}/api/presets`, { method: "PUT", body: { model, name, settings } });
        }
      }
      try { localStorage.removeItem(PRESET_STORAGE_KEY); } catch { /* storage blocked */ }
    }
  } catch (error) {
    state.presets = state.presets || {};
    toast(`Couldn't load presets: ${error.message}`, "error");
  }
}

async function storePreset(name, settings) {
  state.presets = await Api.request(`${API_BASE}/api/presets`, { method: "PUT", body: { model: state.model, name, settings } });
}

const presetsFor = (model) => (state.presets || {})[model] || {};

function selectedPresetName(model = state.model) {
  const name = readCookie(presetCookieName(model));
  return name && presetsFor(model)[name] ? name : "";
}

function presetSnapshot() {
  const settings = collectSettings();
  PRESET_EXCLUDED_FIELDS.forEach((field) => delete settings[field]);
  return settings;
}

function renderPresetBar() {
  const supported = PRESET_MODELS.includes(state.model);
  $("#preset-row").hidden = !supported;
  if (!supported) return;
  const names = Object.keys(presetsFor(state.model)).sort((a, b) => a.localeCompare(b));
  const select = $("#preset");
  select.innerHTML = `<option value="">No preset</option>`
    + names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  select.value = selectedPresetName();
  $("#preset-save").disabled = !select.value;
  $("#preset-delete").disabled = !select.value;
  $("#preset-rename").disabled = !select.value;
  updatePresetStatus();
}

// "modified" once the form drifts from the selected preset. Only the preset's own
// keys are compared, so presets saved before a new field existed still match.
function updatePresetStatus() {
  const name = PRESET_MODELS.includes(state.model) ? selectedPresetName() : "";
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

async function applyPreset(name) {
  const preset = presetsFor(state.model)[name];
  if (!preset) return;
  const current = collectSettings();
  // applyRetryRequest restores keyframes from the request, so the current ones are passed through here.
  await applyRetryRequest({ ...current, ...preset, model: state.model, mode: current.mode, prompt: current.prompt, seed: current.seed,
    first_frame_id: state.firstFrameId, last_frame_id: state.lastFrameId });
  // applyRetryRequest only fills these while upscale is on; a preset keeps them either way.
  if (state.model === "minimax_h3") {
    setField("#h3-upscale-factor", preset.upscale_factor);
    setField("#h3-upscale-steps", preset.upscale_steps);
    setField("#h3-upscale-denoise", preset.upscale_denoise);
    setField("#h3-upscale-chunking", preset.upscale_chunking ?? true);
    writeUpscaleAdvanced(preset.upscale_advanced);
  }
}

// Page load and model switch: apply the preset whose name is in this model's cookie.
async function applySelectedPreset() {
  const name = PRESET_MODELS.includes(state.model) ? selectedPresetName() : "";
  if (name) await applyPreset(name);
  renderPresetBar();
}

async function selectPreset(name) {
  writeCookie(presetCookieName(state.model), name || null);
  if (name) {
    await applyPreset(name);
    toast(`Preset "${name}" applied.`);
    if (presetsFor(state.model)[name]?.voice_only_reference_videos) {
      toast("Set Audio only on each video reference you want to use for audio; this preset no longer changes references.");
    }
  }
  renderPresetBar();
  saveSessionSettings();
}

// "Save as" dialog: save the current settings under a new name or over any
// existing preset (no need to switch to it first), or rename a preset.
// Saving selects the preset it saved to, like picking it would.
function openPresetDialog(renaming = "") {
  $("#preset-dialog-title").textContent = `Save ${modelLabels[state.model]} settings as`;
  $("#preset-new-name").value = "";
  renderPresetList(renaming);
  $("#preset-dialog").hidden = false;
  if (renaming) $("#preset-list .preset-rename-input")?.focus();
  else if (!isMobileLayout()) $("#preset-new-name").focus();
}

function closePresetDialog() {
  $("#preset-dialog").hidden = true;
}

function renderPresetList(renaming = "") {
  const names = Object.keys(presetsFor(state.model)).sort((a, b) => a.localeCompare(b));
  const selected = selectedPresetName();
  $("#preset-list-hint").hidden = !names.length;
  $("#preset-list").innerHTML = names.map((name) => name === renaming
    ? `<li class="preset-item is-renaming" data-preset="${escapeHtml(name)}">
        <input type="text" class="preset-rename-input" maxlength="60" value="${escapeHtml(name)}" aria-label="New name for ${escapeHtml(name)}">
        <button type="button" class="secondary-button" data-preset-action="rename-save">Rename</button>
        <button type="button" class="secondary-button" data-preset-action="rename-cancel">Cancel</button>
      </li>`
    : `<li class="preset-item" data-preset="${escapeHtml(name)}">
        <span class="preset-item-name">${escapeHtml(name)}${name === selected ? ` <span class="hint">(selected)</span>` : ""}</span>
        <button type="button" class="secondary-button" data-preset-action="overwrite">Save here</button>
        <button type="button" class="secondary-button job-icon-button" data-preset-action="rename" title="Rename" aria-label="Rename ${escapeHtml(name)}">${uiIcon("pencil")}</button>
      </li>`).join("");
}

async function savePresetAs(name) {
  try { await storePreset(name, presetSnapshot()); } catch (error) { toast(error.message, "error"); return; }
  writeCookie(presetCookieName(state.model), name);
  closePresetDialog();
  renderPresetBar();
  toast(`Preset "${name}" saved.`);
}

async function renamePreset(name, newName) {
  newName = newName.trim().slice(0, 60);
  if (!newName || newName === name) { renderPresetList(); return; }
  try {
    state.presets = await Api.request(`${API_BASE}/api/presets/rename`, { method: "POST", body: { model: state.model, name, new_name: newName } });
  } catch (error) { toast(error.message, "error"); return; }
  if (readCookie(presetCookieName(state.model)) === name) writeCookie(presetCookieName(state.model), newName);
  renderPresetList();
  renderPresetBar();
  toast(`Renamed "${name}" to "${newName}".`);
}

function bindPresetDialog() {
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
    if (action === "overwrite") {
      if (confirm(`Save the current settings over "${name}"?`)) savePresetAs(name);
    } else if (action === "rename") {
      renderPresetList(name);
      $("#preset-list .preset-rename-input")?.focus();
    } else if (action === "rename-save") {
      renamePreset(name, $(".preset-rename-input", item).value);
    } else if (action === "rename-cancel") {
      renderPresetList();
    }
  });
  $("#preset-list").addEventListener("keydown", (event) => {
    if (!event.target.classList.contains("preset-rename-input")) return;
    if (event.key === "Enter") { event.preventDefault(); renamePreset(event.target.closest("[data-preset]").dataset.preset, event.target.value); }
    if (event.key === "Escape") { event.stopPropagation(); renderPresetList(); }
  });
  $("#preset-dialog-close").addEventListener("click", closePresetDialog);
  $("#preset-dialog-backdrop").addEventListener("click", closePresetDialog);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#preset-dialog").hidden) closePresetDialog();
  });
}

async function updateSelectedPreset() {
  const name = selectedPresetName();
  if (!name) return;
  try { await storePreset(name, presetSnapshot()); } catch (error) { return toast(error.message, "error"); }
  renderPresetBar();
  toast(`Preset "${name}" updated.`);
}

async function deleteSelectedPreset() {
  const name = selectedPresetName();
  if (!name || !confirm(`Delete the preset "${name}"?`)) return;
  try {
    const query = new URLSearchParams({ model: state.model, name });
    state.presets = await Api.request(`${API_BASE}/api/presets?${query}`, { method: "DELETE" });
  } catch (error) { return toast(error.message, "error"); }
  writeCookie(presetCookieName(state.model), null);
  renderPresetBar();
  toast(`Preset "${name}" deleted.`);
}

/* -------------------------------------------------------- lora gallery -- */

// A bigger view of the current model's LoRA list: previews, description, triggers
// and recommended strength. "Show more" (or a card's title) opens one LoRA as a big
// single card, with a Back button that returns to the grid at the same scroll position.
// Data comes from static/lora-gallery/index.json, built by tools/fetch_lora_gallery.py.
// Previews are local files, because the gateway's CSP blocks media from other origins.
// Each card's toggle + strength drive the matching list row, so the list stays
// the single source for collectLoraSlots()/collectLtxLoras().
const gallery = { index: null, catalog: null, draft: null, preview: new Map(), detail: null, editing: false, configOpen: false, gridScroll: 0,
  hermesState: "idle", hermesStage: "", hermesBaseline: null, hermesRequest: 0 };
const galleryModel = () => listLoras() ? state.model : "minimax_h3";
const NSFW_BLUR_LEVEL = 4; // Civitai nsfwLevel: 1 PG, 2 PG-13, 4 R, 8 X, 16 XXX
const GALLERY_NSFW_KEY = "h3-lora-gallery-nsfw";

function galleryShowsNsfw() {
  try { return localStorage.getItem(GALLERY_NSFW_KEY) === "on"; } catch { return false; }
}

function galleryItems() {
  if (listLoras()) {
    const list = listLoras();
    return (state.options[list.key] || []).map((item) => ({ item, row: $(`[data-ltx-lora="${CSS.escape(item.filename)}"]`, $(list.list)) }));
  }
  return (state.options.minimax_h3_lora_slots || []).map((item, index) => ({ item, row: $(`[data-lora-index="${index}"]`, $("#minimax-lora-list")) })).filter(({ item }) => !item.deleted);
}

// "By category": cards sorted into their groups (in list order of first
// appearance, ungrouped last), each group under its own heading and line.
const GALLERY_BY_GROUP_KEY = "h3-lora-gallery-by-group";
function galleryByGroup() {
  try { return localStorage.getItem(GALLERY_BY_GROUP_KEY) === "on"; } catch { return false; }
}

function filteredGalleryItems() {
  const items = matchingGalleryItems();
  const order = gallery.catalog?.order || [];
  const position = (name) => { const n = order.indexOf(name); return n < 0 ? Number.MAX_SAFE_INTEGER : n; };
  if (!galleryByGroup()) return items.sort((a, b) => position(a.item.filename) - position(b.item.filename));
  const categories = gallery.catalog?.categories || [];
  const categoryIndex = (name) => { const n = categories.indexOf(name); return n < 0 ? Number.MAX_SAFE_INTEGER : n; };
  return items.sort((a, b) => categoryIndex(a.item.group) - categoryIndex(b.item.group) || position(a.item.filename) - position(b.item.filename));
}

function matchingGalleryItems() {
  const query = $("#lora-gallery-search").value.trim().toLowerCase();
  const group = $("#lora-gallery-group").value;
  const enabledOnly = $("#lora-gallery-enabled-only").checked;
  return galleryItems().filter(({ item, row }) => {
    if (group && item.group !== group) return false;
    if (enabledOnly && !(row && $("[data-lora-enabled]", row).checked)) return false;
    if (!query) return true;
    const info = gallery.index?.[item.filename] || {};
    return [item.label, item.filename, info.name, info.creator, info.description, ...(item.triggers || [])]
      .some((text) => text && text.toLowerCase().includes(query));
  });
}

const galleryEntry = (filename) => galleryItems().find(({ item }) => item.filename === filename);
const galleryPreviews = (filename) => gallery.index?.[filename]?.previews || [];
const galleryAsset = (path) => `${API_BASE}/${path.startsWith("api/") ? "" : "static/"}${escapeHtml(path)}`;
const galleryImageAttrs = (path, eager = false) => {
  const source = galleryAsset(path);
  return window.BunnyGalleryMedia?.imageAttrs(source, eager) || `src="${source}" loading="lazy" decoding="async"`;
};
const galleryIcon = (name) => window.BunnyGalleryMedia?.icon(name) || uiIcon(name);

/* ---------------------------------------------------- trigger aliases -- */
// Short words that stand for a LoRA trigger keyword, stored server-side
// (/api/trigger-aliases) per LoRA family. One entry = one LoRA + target
// keyword + its aliases, typed comma-separated ("tag, tag2" or "tag,tag2").
const aliasEdit = { previous: null }; // { lora, target } of the entry being edited

async function loadTriggerAliases(pending = null) {
  try { setTriggerAliases(await (pending || Api.request(`${API_BASE}/api/trigger-aliases`))); } catch { state.triggerAliases = state.triggerAliases || {}; }
}

function setTriggerAliases(aliases) {
  state.triggerAliases = aliases || {};
  aliasVersion++;
  renderPromptSnippets(); // re-highlights the prompt
  syncGalleryAliases();
}

function aliasLoras() {
  if (!state.options) return [];
  const list = listLoras();
  return list
    ? (state.options[list.key] || []).map((lora) => ({ lora, on: $(`[data-ltx-lora="${CSS.escape(lora.filename)}"] [data-lora-enabled]`, $(list.list))?.checked }))
    : (state.options.minimax_h3_lora_slots || []).map((lora, index) => ({ lora, on: $(`[data-lora-index="${index}"] [data-lora-enabled]`, $("#minimax-lora-list"))?.checked }));
}

// The alias dialog edits LoRA trigger aliases (current model's family) or
// unofficial camera moves (family "camera", no LoRA).
const aliasKind = () => (isUnofficialFamily($("#alias-kind").value) ? $("#alias-kind").value : "lora");
const dialogFamily = () => (aliasKind() === "lora" ? aliasFamily() : aliasKind());
const OFFICIAL_SHOT_PHRASES = ["a medium-wide shot frames", "a medium shot of", "a close shot of", "a close-up of", "an extreme close-up of"];
// The official camera wording, to start a camera move from.
const OFFICIAL_CAMERA_PHRASES = [...CAMERA_MOVES.map(([, verb]) => `The camera ${verb}`), "A POV shot from"];

// Entries grouped from the server's flat alias rows.
function aliasGroups(family = aliasFamily()) {
  const groups = new Map();
  (state.triggerAliases?.[family] || []).forEach(({ alias, lora, target }) => {
    const key = `${lora}\n${target}`;
    if (!groups.has(key)) groups.set(key, { lora, target, aliases: [] });
    groups.get(key).aliases.push(alias);
  });
  return [...groups.values()].sort((a, b) => a.target.localeCompare(b.target));
}

function fillAliasTriggers(target = "") {
  const camera = aliasKind() !== "lora"; // an unofficial family: camera move or shot composition
  const lora = aliasLoras().find(({ lora }) => lora.filename === $("#alias-lora").value)?.lora;
  const words = camera ? (aliasKind() === "shot" ? OFFICIAL_SHOT_PHRASES : OFFICIAL_CAMERA_PHRASES)
    : [...new Set((lora?.triggers || []).filter((trigger) => triggerPattern(trigger)).map((trigger) => trigger.replace(/\s*\(.*\)\s*$/, "").replace(/[\s,.:;]+$/, "").trim()))];
  const select = $("#alias-trigger");
  select.innerHTML = words.map((word) => `<option value="${escapeHtml(word)}">${escapeHtml(word)}</option>`).join("")
    + `<option value="">Custom text…</option>`;
  const value = target || (camera ? "" : words[0]) || "";
  select.value = words.includes(value) ? value : "";
  $("#alias-target").value = value;
}

// Show the fields that apply to the chosen type.
function applyAliasKind() {
  const camera = aliasKind() !== "lora";
  const shot = aliasKind() === "shot";
  $("#alias-lora-field").hidden = camera;
  $("#alias-trigger-label").textContent = camera ? "Start from" : "Trigger";
  $("#alias-target").placeholder = shot ? "e.g. a wide full-environment shot" : camera ? "e.g. The camera pans left with large amplitude at fast speed" : "The keyword that goes in the prompt";
  $("#alias-name").placeholder = shot ? "e.g. wide shot" : camera ? "e.g. whip pan" : "e.g. anim, 2d";
  $("#alias-restore-camera").hidden = !camera;
}

function resetAliasForm() {
  aliasEdit.previous = null;
  $("#alias-name").value = "";
  $("#alias-save").textContent = "Add alias";
  $("#alias-cancel").hidden = true;
  applyAliasKind();
  const loras = aliasLoras();
  // Default to the first enabled LoRA that has triggers.
  const first = loras.find(({ lora, on }) => on && lora.triggers?.length) || loras.find(({ lora }) => lora.triggers?.length) || loras[0];
  if (first) $("#alias-lora").value = first.lora.filename;
  fillAliasTriggers();
}

function renderAliasDialog() {
  const camera = aliasKind() !== "lora";
  $("#alias-dialog-model").textContent = aliasKind() === "shot" ? "Shot compositions (all models)" : camera ? "Camera moves (all models)" : modelLabels[aliasFamily()] || "MiniMax H3";
  const loras = aliasLoras();
  const select = $("#alias-lora");
  const current = select.value;
  select.innerHTML = loras.map(({ lora, on }) => `<option value="${escapeHtml(lora.filename)}">${escapeHtml(lora.label)}${on ? " (on)" : ""}</option>`).join("");
  if (current && loras.some(({ lora }) => lora.filename === current)) select.value = current;
  const labels = new Map(loras.map(({ lora, on }) => [lora.filename, `${lora.label}${on ? " · on" : " · off"}`]));
  const groups = aliasGroups(dialogFamily());
  const aliasCount = groups.reduce((sum, group) => sum + group.aliases.length, 0);
  $("#alias-show-all").textContent = `Show all aliases (${aliasCount})`;
  $("#alias-show-all").disabled = !aliasCount && !camera;
  $("#alias-list-hint").textContent = groups.length
    ? `${aliasCount} alias${aliasCount === 1 ? "" : "es"} for ${groups.length} keyword${groups.length === 1 ? "" : "s"}` : "No aliases yet.";
  $("#alias-list").innerHTML = groups.map((group, index) => `
    <li class="${aliasEdit.previous?.lora === group.lora && aliasEdit.previous?.target === group.target ? "is-editing" : ""}">
      <span class="alias-item-text">${group.aliases.map((alias) => `<b>${escapeHtml(alias)}</b>`).join(", ")} → <code>${escapeHtml(group.target)}</code>
        <span class="alias-item-lora">${escapeHtml(camera ? `${UNOFFICIAL_FAMILIES[aliasKind()].meta[group.target] ? "Built-in" : "Your"} ${UNOFFICIAL_FAMILIES[aliasKind()].noun}` : labels.get(group.lora) || group.lora)}</span></span>
      <button type="button" class="icon-button" data-alias-edit="${index}" title="Edit" aria-label="Edit">${uiIcon("pencil")}</button>
      <button type="button" class="icon-button" data-alias-delete="${index}" title="Delete" aria-label="Delete">${uiIcon("trash")}</button>
    </li>`).join("");
}

// From the gallery: `lora` preselects that LoRA; `target` opens its existing
// aliases for that keyword for editing.
// The dialog shows either the form or (Show all aliases) the full list.
function showAliasList(on) {
  $("#alias-form").hidden = on;
  $("#alias-list-view").hidden = !on;
  if (on) renderAliasDialog();
}

function openAliasDialog(lora = null, target = null, kind = "lora") {
  $("#alias-dialog").hidden = false;
  $("#alias-kind").value = kind;
  showAliasList(false);
  renderAliasDialog();
  resetAliasForm();
  if (lora && $(`#alias-lora option[value="${CSS.escape(lora)}"]`)) {
    $("#alias-lora").value = lora;
    fillAliasTriggers();
    const group = target != null && aliasGroups(dialogFamily()).find((item) => item.lora === lora && item.target === target);
    if (group) startAliasEdit(group);
  }
  renderAliasDialog();
  if (!isMobileLayout()) $("#alias-name").focus({ preventScroll: true });
}

function startAliasEdit(group) {
  aliasEdit.previous = { lora: group.lora, target: group.target };
  $("#alias-name").value = group.aliases.join(", ");
  if (group.lora) $("#alias-lora").value = group.lora;
  fillAliasTriggers(group.target);
  $("#alias-save").textContent = "Save changes";
  $("#alias-cancel").hidden = false;
}

function closeAliasDialog() {
  $("#alias-dialog").hidden = true;
  aliasEdit.previous = null;
}

async function saveAliasForm(event) {
  event.preventDefault();
  const body = {
    model: dialogFamily(), aliases: $("#alias-name").value, lora: aliasKind() === "camera" ? "" : $("#alias-lora").value, target: $("#alias-target").value.trim(),
    previous_lora: aliasEdit.previous?.lora ?? null, previous_target: aliasEdit.previous?.target ?? null,
  };
  try {
    setTriggerAliases(await Api.request(`${API_BASE}/api/trigger-aliases`, { method: "PUT", body }));
    toast(aliasEdit.previous ? "Aliases updated." : "Alias added.");
    resetAliasForm();
    renderAliasDialog();
  } catch (error) { toast(error.message, "error"); }
}

function bindAliasDialog() {
  $$("[data-lora-aliases]").forEach((button) => button.addEventListener("click", () => openAliasDialog()));
  $("#alias-kind").addEventListener("change", () => { resetAliasForm(); renderAliasDialog(); });
  $("#alias-restore-camera").addEventListener("click", async () => {
    try {
      setTriggerAliases(await Api.request(`${API_BASE}/api/trigger-aliases/defaults?model=${aliasKind()}`, { method: "POST" }));
      renderAliasDialog();
      toast(`Built-in ${UNOFFICIAL_FAMILIES[aliasKind()].noun}s restored.`);
    } catch (error) { toast(error.message, "error"); }
  });
  $$("[data-alias-close]").forEach((node) => node.addEventListener("click", closeAliasDialog));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || $("#alias-dialog").hidden) return;
    event.preventDefault(); // tells the gallery underneath not to close too
    closeAliasDialog();
  });
  $("#alias-form").addEventListener("submit", saveAliasForm);
  $("#alias-lora").addEventListener("change", () => fillAliasTriggers());
  $("#alias-trigger").addEventListener("change", (event) => {
    if (event.target.value) $("#alias-target").value = event.target.value;
    else $("#alias-target").focus();
  });
  $("#alias-target").addEventListener("input", (event) => {
    const select = $("#alias-trigger");
    select.value = [...select.options].some((option) => option.value && option.value === event.target.value) ? event.target.value : "";
  });
  $("#alias-cancel").addEventListener("click", () => { resetAliasForm(); renderAliasDialog(); });
  $("#alias-show-all").addEventListener("click", () => showAliasList(true));
  $("#alias-list-back").addEventListener("click", () => showAliasList(false));
  $("#alias-list").addEventListener("click", async (event) => {
    const edit = event.target.closest("[data-alias-edit]");
    const remove = event.target.closest("[data-alias-delete]");
    const group = aliasGroups(dialogFamily())[Number((edit || remove)?.dataset.aliasEdit ?? (edit || remove)?.dataset.aliasDelete)];
    if (!group) return;
    if (edit) {
      startAliasEdit(group);
      showAliasList(false);
      renderAliasDialog();
      $("#alias-name").focus();
      return;
    }
    if (!confirm(`Delete the aliases ${group.aliases.join(", ")} for "${group.target}"?`)) return;
    try {
      const query = new URLSearchParams({ model: dialogFamily(), lora: group.lora, target: group.target });
      setTriggerAliases(await Api.request(`${API_BASE}/api/trigger-aliases?${query}`, { method: "DELETE" }));
      if (aliasEdit.previous?.lora === group.lora && aliasEdit.previous?.target === group.target) resetAliasForm();
      renderAliasDialog();
      if (!aliasGroups(dialogFamily()).length) showAliasList(false);
    } catch (error) { toast(error.message, "error"); }
  });
}

async function openLoraGallery() {
  $("#lora-gallery").hidden = false;
  try { await refreshManagedLoras(); } catch (error) { toast(error.message, "error"); }
  $("#lora-gallery-model").textContent = modelLabels[state.model];
  $("#lora-gallery-nsfw").checked = galleryShowsNsfw();
  const groups = [...new Set(galleryItems().map(({ item }) => item.group).filter(Boolean))];
  const group = $("#lora-gallery-group");
  const previous = group.value;
  group.innerHTML = `<option value="">All groups</option>` + groups.map((name) => `<option>${escapeHtml(name)}</option>`).join("");
  group.value = groups.includes(previous) ? previous : "";
  showGalleryGrid();
  if (!gallery.index) {
    $("#lora-gallery-grid").innerHTML = `<p class="hint lora-gallery-empty">Loading…</p>`;
    await ensureGalleryIndex();
  }
  renderLoraGallery();
  if (!isMobileLayout()) $("#lora-gallery-search").focus({ preventScroll: true });
}

function closeLoraGallery() {
  $("#lora-gallery").hidden = true;
  gallery.detail = null;
  gallery.configOpen = false;
  gallery.hermesRequest++;
  gallery.hermesState = "idle";
  gallery.hermesBaseline = null;
  galleryVideoObserver.disconnect();
  $("#lora-gallery-grid").innerHTML = ""; // stops any playing preview
  $("#lora-gallery-detail").innerHTML = "";
}

function showGalleryGrid() {
  gallery.detail = null;
  gallery.editing = false;
  gallery.configOpen = false;
  gallery.hermesRequest++;
  gallery.hermesState = "idle";
  gallery.hermesBaseline = null;
  $("#lora-gallery-config").hidden = true;
  $("#lora-gallery-detail").hidden = true;
  $("#lora-gallery-detail").innerHTML = "";
  $("#lora-gallery-grid").hidden = false;
  $("#lora-gallery-tools").hidden = false;
}

function openGalleryDetail(filename, editing = false) {
  const entry = galleryEntry(filename);
  if (!entry) return; // a LoRA of the other model family (the list changed under the gallery)
  if (!gallery.detail) gallery.gridScroll = $("#lora-gallery-grid").scrollTop;
  gallery.detail = filename;
  gallery.editing = editing;
  gallery.configOpen = false;
  $("#lora-gallery-config").hidden = true;
  $("#lora-gallery-grid").hidden = true;
  $("#lora-gallery-grid").innerHTML = ""; // no hidden videos keep playing
  $("#lora-gallery-tools").hidden = true;
  const detail = $("#lora-gallery-detail");
  detail.hidden = false;
  detail.innerHTML = galleryCardHtml(entry, true);
  detail.scrollTop = 0;
  observeGalleryVideos(detail);
}

function closeGalleryDetail() {
  showGalleryGrid();
  renderLoraGallery(); // reflects toggles changed in the big card
  $("#lora-gallery-grid").scrollTop = gallery.gridScroll;
}

function galleryMediaHtml(filename, detail) {
  const previews = galleryPreviews(filename);
  if (!previews.length) return `<div class="lora-card-media"><span class="lora-no-preview">No previews on the source page</span></div>`;
  const index = Math.min(gallery.preview.get(filename) || 0, previews.length - 1);
  const preview = previews[index];
  const blurred = !galleryShowsNsfw() && preview.nsfw >= NSFW_BLUR_LEVEL;
  const key = escapeHtml(filename);
  const overlay = blurred
    ? `<button type="button" class="lora-media-overlay" data-gallery-reveal="${key}"><span class="lora-media-nsfw">NSFW preview &middot; tap to show</span></button>` : "";
  const visual = !blurred && preview.video ? galleryVideoHtml(preview) : `<img ${galleryImageAttrs(preview.poster, detail)} alt="">`;
  const arrows = previews.length > 1 ? `
    <button type="button" class="lora-media-nav lora-media-prev" data-gallery-step="-1" aria-label="Previous preview">&#8249;</button>
    <button type="button" class="lora-media-nav lora-media-next" data-gallery-step="1" aria-label="Next preview">&#8250;</button>
    <span class="lora-media-count">${index + 1} / ${previews.length}${preview.video ? " &middot; video" : ""}</span>` : "";
  return `<div class="lora-card-media${detail ? " is-detail" : ""}${blurred ? " is-blurred" : ""}">
      ${visual}${overlay}${arrows}
    </div>
    ${previews.length > 1 ? `<div class="lora-thumbs">${previews.map((entry, i) => `
      <button type="button" class="lora-thumb${i === index ? " is-active" : ""}${!galleryShowsNsfw() && entry.nsfw >= NSFW_BLUR_LEVEL ? " is-blurred" : ""}" data-gallery-thumb="${i}" aria-label="Preview ${i + 1}">
        <img ${galleryImageAttrs(entry.poster)} alt="">${entry.video ? `<span class="lora-thumb-video">${galleryIcon("play")}</span>` : ""}
      </button>`).join("")}</div>` : ""}`;
}

function galleryCardHtml({ item, row }, detail = false) {
  const info = gallery.index?.[item.filename] || {};
  const box = row && $("[data-lora-enabled]", row);
  const enabled = Boolean(box?.checked);
  const strength = row ? $("[data-lora-strength]", row).value : item.default_strength;
  const key = escapeHtml(item.filename);
  const triggers = item.triggers || [];
  const description = info.description || "";
  const source = [info.name && info.name !== item.label ? info.name : "", info.creator ? `by ${info.creator}` : ""].filter(Boolean).join(" ");
  let nav = "";
  if (detail) {
    const list = filteredGalleryItems();
    const position = list.findIndex((entry) => entry.item.filename === item.filename);
    const prev = position > 0 ? list[position - 1].item : null;
    const next = position >= 0 && position < list.length - 1 ? list[position + 1].item : null;
    nav = `<div class="lora-detail-nav">
        <button type="button" class="secondary-button" data-gallery-back>&#8592; Back to gallery</button>
        <span class="lora-detail-lora-nav">
          <button type="button" class="secondary-button" data-gallery-lora="${prev ? escapeHtml(prev.filename) : ""}" ${prev ? "" : "disabled"} title="${prev ? escapeHtml(prev.label) : ""}">&#8249; Previous LoRA</button>
          <button type="button" class="secondary-button" data-gallery-lora="${next ? escapeHtml(next.filename) : ""}" ${next ? "" : "disabled"} title="${next ? escapeHtml(next.label) : ""}">Next LoRA &#8250;</button>
        </span>
      </div>`;
  }
  return `${nav}
    <article class="lora-card${detail ? " lora-card-detail" : ""}${enabled ? " is-enabled" : ""}${item.source === "trainer" ? " is-trainer" : ""}" data-gallery-card="${key}">
      <div class="lora-card-gallery">${galleryMediaHtml(item.filename, detail)}</div>
      <div class="lora-card-body">
        <div class="lora-card-title">
          <div>
            ${detail ? `<h3>${escapeHtml(item.label)}</h3>` : `<h3><button type="button" class="lora-card-open" data-gallery-open title="Open the full card">${escapeHtml(item.label)}</button></h3>`}
            ${source ? `<p class="lora-card-source">${escapeHtml(source)}</p>` : ""}
          </div>
          <div class="lora-card-controls">
            <input type="number" class="lora-strength" data-gallery-strength min="0" max="2" step="0.05" value="${escapeHtml(String(strength))}" aria-label="Strength" ${row ? "" : "disabled"}>
            <input type="checkbox" data-gallery-toggle ${enabled ? "checked" : ""} ${!row || box.disabled ? "disabled" : ""} aria-label="Use ${escapeHtml(item.label)}">
          </div>
        </div>
        <div class="lora-card-meta">
          ${item.source === "trainer" ? `<span class="lora-chip lora-trainer-badge" title="LoRA sent from the Trainer after its final epoch">Trainer LoRA</span>` : ""}
          ${detail && item.source === "link" ? `<span class="lora-chip" title="Hermes named the installed file">File: ${escapeHtml(item.filename.split("/").pop())}</span>` : ""}
          ${item.group ? `<span class="lora-chip">${escapeHtml(item.group)}</span>` : ""}
          ${info.version ? `<span class="lora-chip">${escapeHtml(info.version)}</span>` : ""}
          ${info.base_model ? `<span class="lora-chip">${escapeHtml(info.base_model)}</span>` : ""}
          ${triggers.map((trigger) => `<button type="button" class="lora-chip lora-trigger" data-gallery-trigger="${escapeHtml(trigger.replace(/\s*\(.*\)$/, ""))}" title="Add to the prompt">${escapeHtml(trigger)}</button>`).join("")}
        </div>
        <div class="lora-card-aliases" data-gallery-aliases="${escapeHtml(item.filename)}">${galleryAliasesHtml(item.filename)}</div>
        <p class="lora-card-strength">Recommended ${escapeHtml(String(item.default_strength))}${item.strength_note ? ` &mdash; ${escapeHtml(item.strength_note)}` : ""}</p>
        ${description ? `<p class="lora-card-desc${detail ? " is-open" : ""}">${escapeHtml(description)}</p>` : ""}
        <div class="lora-card-links">
          ${!detail && description ? `<button type="button" class="lora-desc-toggle" data-gallery-open>Show more</button>` : "<span></span>"}
          <button type="button" class="secondary-button" data-gallery-edit>Edit</button>
          ${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Source page &#8599;</a>` : ""}
        </div>
        ${detail && gallery.editing ? `<div class="lora-custom-editor" data-custom-editor="${escapeHtml(item.custom_id || "")}" data-edit-filename="${key}">
          <input type="text" data-custom-name maxlength="100" value="${escapeHtml(item.label)}" aria-label="LoRA name" placeholder="LoRA name">
          <select data-custom-group aria-label="Category">${(gallery.catalog?.categories || [item.group]).map((name) => `<option value="${escapeHtml(name)}" ${name === item.group ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}</select>
          <input type="number" data-custom-recommended min="0" max="2" step="0.05" value="${escapeHtml(String(item.default_strength))}" aria-label="Recommended strength">
          <input type="text" data-custom-triggers value="${escapeHtml(triggers.join(", "))}" aria-label="Trigger words" placeholder="Trigger words, comma separated">
          <textarea data-custom-description aria-label="Description" placeholder="Description">${escapeHtml(description)}</textarea>
          <input type="url" data-custom-url value="${escapeHtml(item.url || "")}" aria-label="Source page URL" placeholder="https://source-page…">
          <button type="button" class="secondary-button" data-gallery-save>Save details</button>
          ${item.custom_id ? `<button type="button" class="secondary-button" data-gallery-scrape>Ask Hermes to import URL</button><button type="button" class="secondary-button danger" data-gallery-delete>Delete LoRA</button>` : ""}
          <span class="hint" data-custom-status></span>
        </div>` : ""}
      </div>
    </article>`;
}

// A card's aliases (grouped per target keyword) and its "Add alias" button.
function galleryAliasesHtml(filename) {
  const groups = aliasGroups().filter((group) => group.lora === filename);
  const lora = aliasLoras().find(({ lora: item }) => item.filename === filename)?.lora;
  const canAdd = (lora?.triggers || []).some((trigger) => triggerPattern(trigger)); // nothing to alias without a trigger
  return groups.map((group) => `<button type="button" class="lora-chip lora-alias-chip" data-gallery-alias-edit="${escapeHtml(group.target)}" title="Edit these aliases">${
    group.aliases.map(escapeHtml).join(", ")} &rarr; ${escapeHtml(group.target)}</button>`).join("")
    + (canAdd ? `<button type="button" class="lora-chip lora-alias-add" data-gallery-alias-add title="Make a short word that stands for one of this LoRA's trigger keywords">+ Add alias</button>` : "");
}

function syncGalleryAliases() {
  $$("[data-gallery-aliases]", $("#lora-gallery")).forEach((node) => { node.innerHTML = galleryAliasesHtml(node.dataset.galleryAliases); });
}

function renderLoraGallery() {
  if (gallery.detail) return;
  const items = filteredGalleryItems();
  const byGroup = galleryByGroup();
  const count = (name) => items.filter(({ item }) => (item.group || "") === name).length;
  $("#lora-gallery-grid").innerHTML = items.length ? items.map((entry, i) => {
    const name = entry.item.group || "";
    const heading = (byGroup || name === "Trainer made") && (i === 0 || (items[i - 1].item.group || "") !== name)
      ? `<h3 class="lora-gallery-section">${escapeHtml(name || "Other")} <span>${count(name)}</span></h3>` : "";
    return heading + galleryCardHtml(entry);
  }).join("")
    : `<p class="hint lora-gallery-empty">No LoRAs match.</p>`;
  observeGalleryVideos($("#lora-gallery-grid"));
}

// Only the preview area is redrawn, so the card's toggle/strength focus is left alone.
function rerenderGalleryMedia(card) {
  const filename = card.dataset.galleryCard;
  card.querySelector(".lora-card-gallery").innerHTML = galleryMediaHtml(filename, card.classList.contains("lora-card-detail"));
  observeGalleryVideos(card);
}

function stepGalleryPreview(card, delta) {
  const filename = card.dataset.galleryCard;
  const count = galleryPreviews(filename).length;
  if (count < 2) return;
  gallery.preview.set(filename, ((gallery.preview.get(filename) || 0) + delta + count) % count);
  rerenderGalleryMedia(card);
}

function playGalleryPreview(card) {
  const filename = card.dataset.galleryCard;
  const preview = galleryPreviews(filename)[gallery.preview.get(filename) || 0];
  if (!preview?.video) return;
  const media = card.querySelector(".lora-card-media");
  media.classList.remove("is-blurred");
  // Keep the arrows/counter; swap the poster + overlay for the video.
  media.querySelectorAll("img, .lora-media-overlay").forEach((node) => node.remove());
  media.insertAdjacentHTML("afterbegin", galleryVideoHtml(preview));
  observeGalleryVideos(media);
}

// Previews autoplay muted and looping, but only while on screen. preload="none" plus
// the observer means a grid of 24+ cards doesn't download every clip at once.
function galleryVideoHtml(preview) {
  return `<video src="${galleryAsset(preview.video)}" poster="${galleryAsset(preview.poster)}" muted loop playsinline controls preload="none" data-gallery-video></video>`;
}

const galleryVideoObserver = new IntersectionObserver((entries) => entries.forEach(({ target, isIntersecting }) => {
  if (isIntersecting) target.play().catch(() => { /* e.g. power-saver mode blocks autoplay - controls still work */ });
  else target.pause();
}), { threshold: 0.35 });

function observeGalleryVideos(container) {
  window.BunnyGalleryMedia?.observe(container);
  $$("video[data-gallery-video]", container).forEach((video) => {
    if (video.dataset.observed) return;
    video.dataset.observed = "1";
    video.muted = true; // the attribute alone isn't always honoured for innerHTML-created videos
    galleryVideoObserver.observe(video);
  });
}

// After a card changes a row: refresh every visible card's toggle (LTX's max-4 lockout can change others).
function syncGalleryCards() {
  galleryItems().forEach(({ item, row }) => {
    const card = $(`[data-gallery-card="${CSS.escape(item.filename)}"]`, $("#lora-gallery"));
    if (!card || !row) return;
    const box = $("[data-lora-enabled]", row);
    const toggle = $("[data-gallery-toggle]", card);
    toggle.checked = box.checked;
    toggle.disabled = box.disabled;
    card.classList.toggle("is-enabled", box.checked);
  });
}

function insertTriggerWord(word) {
  // Like insertTokenAtCursor() but without focusing the prompt behind the dialog (no phone keyboard).
  const textarea = $("#prompt");
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const needsSpace = start > 0 && !/\s$/.test(textarea.value.slice(0, start));
  textarea.setRangeText(`${needsSpace ? " " : ""}${word}`, start, end, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  toast(`Added "${word}" to the prompt.`);
}

async function refreshManagedLoras() {
  const oldOptions = state.options;
  const oldH3 = collectLoraSlots();
  const oldLists = Object.fromEntries(Object.keys(LIST_LORAS).map((model) =>
    [model, new Map(collectListLoras(model).map(({ lora, strength }) => [lora, strength]))]));
  const options = await Api.request(`${API_BASE}/api/settings/options`);
  state.options = options;
  renderLoraList(options.minimax_h3_lora_slots);
  $$('[data-lora-index]', $("#minimax-lora-list")).forEach((row) => {
    const previous = oldH3[Number(row.dataset.loraIndex)];
    if (!previous || row.hidden) return;
    $("[data-lora-enabled]", row).checked = previous.enabled;
    const index = Number(row.dataset.loraIndex);
    const priorDefault = oldOptions?.minimax_h3_lora_slots?.[index]?.default_strength;
    $("[data-lora-strength]", row).value = !previous.enabled && previous.strength === priorDefault
      ? options.minimax_h3_lora_slots[index].default_strength : previous.strength;
    row.classList.toggle("is-enabled", previous.enabled);
  });
  Object.entries(LIST_LORAS).forEach(([model, list]) => {
    renderListLoras(model, options);
    $$('[data-ltx-lora]', $(list.list)).forEach((row) => {
      if (!oldLists[model].has(row.dataset.ltxLora)) return;
      $("[data-lora-enabled]", row).checked = true;
      $("[data-lora-strength]", row).value = oldLists[model].get(row.dataset.ltxLora);
      row.classList.add("is-enabled");
    });
  });
  $("#ltx-lora-list").dispatchEvent(new Event("change"));
  gallery.index = null;
  await ensureGalleryIndex();
  const groups = [...new Set(galleryItems().map(({ item }) => item.group).filter(Boolean))];
  const group = $("#lora-gallery-group"), selected = group.value;
  group.innerHTML = `<option value="">All groups</option>` + groups.map((name) => `<option>${escapeHtml(name)}</option>`).join("");
  group.value = groups.includes(selected) ? selected : "";
  if (gallery.configOpen) { gallery.draft = newGalleryDraft(); renderGalleryConfig(); }
  else if (gallery.detail && galleryEntry(gallery.detail)) openGalleryDetail(gallery.detail, gallery.editing);
  else { showGalleryGrid(); renderLoraGallery(); }
  renderPromptSnippets();
  updateLoraCount();
}

async function uploadGalleryLora() {
  const file = $("#lora-gallery-upload-file").files?.[0];
  const name = $("#lora-gallery-upload-name").value.trim();
  const button = $("#lora-gallery-upload"), status = $("#lora-gallery-upload-status");
  if (!file || !/\.safetensors$/i.test(file.name) || !name) { toast("Choose a .safetensors file and give it a name.", "error"); return; }
  if (file.size > 6 * 1024 ** 3) { toast("LoRA exceeds 6 GiB.", "error"); return; }
  const model = galleryModel();
  button.disabled = true;
  try {
    let reply;
    const size = 8 * 1024 * 1024;
    if (file.size <= size) {
      const body = new FormData();
      body.append("file", file); body.append("model", model); body.append("name", name);
      reply = await Api.request(`${API_BASE}/api/lora-gallery/upload`, { method: "POST", body });
    } else {
      const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
      const total = Math.ceil(file.size / size);
      let next = 0, finished = 0;
      const worker = async () => {
        while (next < total) {
          const index = next++;
          const body = new FormData();
          body.append("upload_id", uploadId); body.append("index", String(index)); body.append("total", String(total));
          body.append("chunk", file.slice(index * size, (index + 1) * size), `${file.name}.part${index}`);
          for (let attempt = 1; ; attempt++) {
            try { await Api.request(`${API_BASE}/api/lora-gallery/chunk`, { method: "POST", body }); break; }
            catch (error) { if (attempt === 4 || (error.status && error.status < 500)) throw error;
              await new Promise((resolve) => setTimeout(resolve, attempt * 1500)); }
          }
          status.textContent = `Uploading ${++finished} of ${total} parts…`;
        }
      };
      await Promise.all(Array.from({ length: Math.min(3, total) }, worker));
      status.textContent = "Installing LoRA…";
      reply = await Api.request(`${API_BASE}/api/lora-gallery/complete`, {
        method: "POST", body: { upload_id: uploadId, total, model, name },
      });
    }
    $("#lora-gallery-upload-file").value = "";
    $("#lora-gallery-upload-name").value = "";
    status.textContent = "Uploaded.";
    await refreshManagedLoras();
    openGalleryDetail(reply.item.filename);
    toast(`Added ${reply.item.label} to the gallery.`);
  } catch (error) { status.textContent = error.message; toast(error.message, "error"); }
  finally { button.disabled = false; }
}

function toggleGalleryLink() {
  const panel = $("#lora-gallery-link-panel");
  panel.hidden = !panel.hidden;
  $("#lora-gallery-link-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) $("#lora-gallery-link-url").focus();
}

const galleryLinkState = { active: false, taskId: null, cancelRequested: false };

function setGalleryLinkButton(mode) {
  const button = $("#lora-gallery-link-add");
  button.textContent = mode === "ready" ? "Download with Hermes" : mode === "cancelling" ? "Cancelling…" : "Cancel";
  button.disabled = mode === "cancelling";
  button.classList.toggle("primary-button", mode === "ready");
  button.classList.toggle("secondary-button", mode !== "ready");
  button.classList.toggle("danger", mode !== "ready");
}

async function cancelGalleryLink() {
  if (!galleryLinkState.active || galleryLinkState.cancelRequested) return;
  galleryLinkState.cancelRequested = true;
  setGalleryLinkButton("cancelling");
  $("#lora-gallery-link-stage").textContent = "Cancelling Hermes and download…";
  if (!galleryLinkState.taskId) return;
  try {
    await Api.request(`${API_BASE}/api/lora-gallery/link/${galleryLinkState.taskId}/cancel`, { method: "POST" });
  } catch (error) {
    galleryLinkState.cancelRequested = false;
    setGalleryLinkButton("active");
    $("#lora-gallery-link-stage").textContent = `Could not cancel: ${error.message}`;
    toast(error.message, "error");
  }
}

async function linkGalleryLora() {
  if (galleryLinkState.active) { await cancelGalleryLink(); return; }
  const urlInput = $("#lora-gallery-link-url"), tokenInput = $("#lora-gallery-link-token");
  const progress = $("#lora-gallery-link-progress");
  const stage = $("#lora-gallery-link-stage"), meter = $("#lora-gallery-link-meter");
  const url = urlInput.value.trim(), token = tokenInput.value.trim(), model = galleryModel();
  if (!/^https:\/\/(?:www\.)?(?:civitai\.com|civitai\.red|civitai\.green|huggingface\.co)\//i.test(url)) {
    toast("Paste a Civitai or Hugging Face HTTPS model link.", "error"); return;
  }
  galleryLinkState.active = true;
  galleryLinkState.taskId = null;
  galleryLinkState.cancelRequested = false;
  setGalleryLinkButton("active");
  progress.hidden = false;
  meter.removeAttribute("value");
  stage.textContent = "Starting Hermes import…";
  try {
    const response = await Api.request(`${API_BASE}/api/lora-gallery/link`, {
      method: "POST", body: { model, url, token },
    });
    tokenInput.value = "";
    let task = response.task;
    galleryLinkState.taskId = task.id;
    if (galleryLinkState.cancelRequested) {
      try {
        await Api.request(`${API_BASE}/api/lora-gallery/link/${task.id}/cancel`, { method: "POST" });
      } catch (error) {
        galleryLinkState.cancelRequested = false;
        setGalleryLinkButton("active");
        toast(`Could not cancel: ${error.message}`, "error");
      }
    }
    while (task.state === "running" || task.state === "cancelling") {
      if (!galleryLinkState.cancelRequested) {
        const downloaded = task.downloaded_bytes || 0, total = task.total_bytes || 0;
        if (total && downloaded) {
          meter.value = Math.min(100, Math.round(downloaded / total * 100));
          stage.textContent = `${task.stage}: ${(downloaded / 1048576).toFixed(0)} / ${(total / 1048576).toFixed(0)} MiB`;
        } else {
          meter.removeAttribute("value");
          stage.textContent = task.stage + "…";
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 1600));
      task = (await Api.request(`${API_BASE}/api/lora-gallery/link/${task.id}`)).task;
    }
    if (task.state === "cancelled") {
      meter.removeAttribute("value");
      stage.textContent = "Download cancelled";
      return;
    }
    if (task.state !== "complete") throw new Error(task.error || "Could not add LoRA from link");
    meter.value = 100;
    stage.textContent = task.already_installed ? "Already in your gallery" : `Installed as ${task.filename || task.item.filename.split("/").pop()}`;
    urlInput.value = "";
    await refreshManagedLoras();
    if (!$("#lora-gallery").hidden && galleryModel() === model) openGalleryDetail(task.item.filename);
    if (task.warning) toast(task.warning, "error");
    toast(task.already_installed ? "This LoRA is already in your gallery." : `Added ${task.item.label} to the gallery.`);
  } catch (error) {
    meter.removeAttribute("value");
    stage.textContent = `Could not add LoRA: ${error.message}`;
    toast(error.message, "error");
  } finally {
    galleryLinkState.active = false;
    galleryLinkState.taskId = null;
    galleryLinkState.cancelRequested = false;
    setGalleryLinkButton("ready");
    tokenInput.value = "";
  }
}

async function saveManagedLora(editor) {
  const body = {
    filename: editor.dataset.editFilename,
    label: $("[data-custom-name]", editor).value,
    group: $("[data-custom-group]", editor).value,
    default_strength: Number($("[data-custom-recommended]", editor).value),
    triggers: $("[data-custom-triggers]", editor).value.split(",").map((word) => word.trim()).filter(Boolean),
    description: $("[data-custom-description]", editor).value,
    url: $("[data-custom-url]", editor).value,
  };
  try {
    await Api.request(`${API_BASE}/api/lora-gallery/catalog/${galleryModel()}/item`, { method: "PATCH", body });
    await refreshManagedLoras();
    toast("LoRA details saved.");
  } catch (error) { $("[data-custom-status]", editor).textContent = error.message; }
}

async function scrapeManagedLora(editor) {
  const id = editor.dataset.customEditor, url = $("[data-custom-url]", editor).value.trim();
  const status = $("[data-custom-status]", editor);
  if (!url) { status.textContent = "Enter the LoRA source URL first."; return; }
  try {
    const response = await Api.request(`${API_BASE}/api/lora-gallery/custom/${id}/scrape`, { method: "POST", body: { url } });
    let task = response.task;
    while (task.state === "running") {
      status.textContent = task.stage + "…";
      await new Promise((resolve) => setTimeout(resolve, 2000));
      task = (await Api.request(`${API_BASE}/api/lora-gallery/scrape/${task.id}`)).task;
    }
    if (task.state !== "complete") throw new Error(task.error || "Could not import LoRA details");
    await refreshManagedLoras();
    if (task.warning) toast(task.warning, "error");
    toast(task.warning ? "Hermes saved the source details." : "Hermes saved the source details and previews.");
  } catch (error) { status.textContent = error.message; toast(error.message, "error"); }
}

async function deleteManagedLora(editor) {
  const id = editor.dataset.customEditor;
  const name = $("[data-custom-name]", editor).value;
  if (!confirm(`Delete "${name}" from the gallery and remove its installed LoRA file?`)) return;
  try {
    await Api.request(`${API_BASE}/api/lora-gallery/custom/${id}`, { method: "DELETE" });
    gallery.detail = null;
    await refreshManagedLoras();
    toast(`Deleted ${name}.`);
  } catch (error) { $("[data-custom-status]", editor).textContent = error.message; }
}

function newGalleryDraft() {
  const items = galleryItems().map(({ item }) => item);
  const catalog = gallery.catalog || {};
  const categories = [...(catalog.categories || [])];
  const assignments = {};
  for (const item of items) {
    assignments[item.filename] = catalog.assignments?.[item.filename] || item.group || "Other";
    if (!categories.includes(assignments[item.filename])) categories.push(assignments[item.filename]);
  }
  const names = new Set(items.map((item) => item.filename));
  const order = (catalog.order || []).filter((name) => names.has(name));
  order.push(...items.map((item) => item.filename).filter((name) => !order.includes(name)));
  return { categories, assignments, order };
}

function renderHermesVisual() {
  const host = $("#lora-hermes-visual"), draft = gallery.draft, baseline = gallery.hermesBaseline;
  if (!host || !draft) return;
  const busy = gallery.hermesState === "running";
  $("#lora-config-save").disabled = busy;
  $("#lora-config-categories").inert = busy;
  $("#lora-config-new").disabled = busy;
  $("#lora-config-add").disabled = busy;
  if (busy) {
    const step = /organizing/i.test(gallery.hermesStage) ? 1 : 0;
    host.innerHTML = `<div class="lora-hermes-panel is-running">
      <div class="lora-hermes-panel-head"><span class="lora-hermes-pulse" aria-hidden="true"></span><strong>Hermes is organizing your LoRAs</strong><span class="hint">${escapeHtml(gallery.hermesStage || "Starting")}</span></div>
      <div class="lora-hermes-progress" aria-hidden="true"><span></span></div>
      <div class="lora-hermes-steps">${["Read gallery details", "Build categories and assignments", "Review before saving"].map((label, index) =>
        `<div class="lora-hermes-step ${index < step ? "is-done" : index === step ? "is-active" : ""}"><span>${index < step ? "&#10003;" : index + 1}</span>${label}</div>`).join("")}</div>
      <p class="hint">This can take a few minutes. The current gallery stays unchanged while Hermes works.</p>
    </div>`;
    return;
  }
  if (gallery.hermesState !== "review" || !baseline) {
    host.innerHTML = `<div class="lora-hermes-intro"><span aria-hidden="true">&#10022;</span><span><strong>Organize with Hermes</strong><br><small>Hermes proposes category names, placements, and display order. You can inspect and edit everything before saving.</small></span></div>`;
    return;
  }
  const labels = new Map(galleryItems().map(({ item }) => [item.filename, item.label]));
  const moved = draft.order.filter((name) => baseline.assignments[name] !== draft.assignments[name]);
  const added = draft.categories.filter((name) => !baseline.categories.includes(name));
  const removed = baseline.categories.filter((name) => !draft.categories.includes(name));
  const sharedBefore = baseline.categories.filter((name) => draft.categories.includes(name));
  const sharedAfter = draft.categories.filter((name) => baseline.categories.includes(name));
  const orderChanged = draft.order.some((name, index) => baseline.order[index] !== name)
    || sharedBefore.some((name, index) => sharedAfter[index] !== name);
  const total = Math.max(draft.order.length, 1);
  host.innerHTML = `<div class="lora-hermes-panel is-review">
    <div class="lora-hermes-panel-head"><span class="lora-hermes-ready" aria-hidden="true">&#10003;</span><strong>Hermes proposal</strong><span class="lora-hermes-unsaved">Not saved yet</span><button type="button" class="secondary-button" id="lora-hermes-discard">Discard proposal</button></div>
    <p class="hint">Compared with the layout you had before running Hermes. Review or adjust the categories below, then save.</p>
    <div class="lora-hermes-stats">
      <div><strong>${draft.categories.length}</strong><span>proposed categories</span></div>
      <div><strong>${added.length}</strong><span>new categories</span></div>
      <div><strong>${moved.length}</strong><span>LoRAs moved</span></div>
      <div><strong>${orderChanged ? "Yes" : "No"}</strong><span>order changed</span></div>
    </div>
    ${removed.length ? `<p class="lora-hermes-removed">Removed categories: ${removed.map(escapeHtml).join(", ")}</p>` : ""}
    <div class="lora-hermes-map" aria-label="Proposed category sizes">${draft.categories.map((name) => {
      const count = draft.order.filter((filename) => draft.assignments[filename] === name).length;
      const before = baseline.order.filter((filename) => baseline.assignments[filename] === name).length;
      const incoming = moved.filter((filename) => draft.assignments[filename] === name).length;
      return `<div class="lora-hermes-map-card"><div class="lora-hermes-map-head"><strong>${escapeHtml(name)}</strong>${added.includes(name) ? `<span class="lora-hermes-new">New</span>` : ""}<span>${count} LoRAs</span></div>
        <div class="lora-hermes-bar" role="img" aria-label="${escapeHtml(name)}: ${count} of ${draft.order.length} LoRAs"><span style="width:${Math.max(count ? 4 : 0, count / total * 100)}%"></span></div>
        <small>${before} before &rarr; ${count} proposed${incoming ? ` &middot; ${incoming} moved in` : ""}</small></div>`;
    }).join("")}</div>
    ${moved.length ? `<details class="lora-hermes-moves"><summary>See ${moved.length} category move${moved.length === 1 ? "" : "s"}</summary><div>${moved.map((name) =>
      `<div><strong>${escapeHtml(labels.get(name) || name)}</strong>${galleryItems().find(({ item }) => item.filename === name)?.item.source === "trainer" ? `<em class="lora-trainer-badge">Trainer</em>` : ""}<span>${escapeHtml(baseline.assignments[name] || "Other")} &rarr; ${escapeHtml(draft.assignments[name])}</span></div>`).join("")}</div></details>` : `<p class="hint">No LoRAs change category. Hermes may have changed category names or display order.</p>`}
  </div>`;
}

function renderGalleryConfig() {
  const draft = gallery.draft;
  if (!draft) return;
  const labels = new Map(galleryItems().map(({ item }) => [item.filename, item.label]));
  const sources = new Map(galleryItems().map(({ item }) => [item.filename, item.source]));
  $("#lora-config-categories").innerHTML = draft.categories.map((category, index) => {
    const names = draft.order.filter((name) => draft.assignments[name] === category && labels.has(name));
    const isNew = gallery.hermesState === "review" && gallery.hermesBaseline && !gallery.hermesBaseline.categories.includes(category);
    return `<section class="lora-config-category${isNew ? " is-suggested" : ""}" data-category-index="${index}">
      <div class="lora-config-category-head"><input type="text" data-category-name maxlength="60" value="${escapeHtml(category)}" aria-label="Category name">
        ${isNew ? `<span class="lora-hermes-new">Hermes suggestion</span>` : ""}
        <button type="button" class="secondary-button" data-category-up ${index === 0 ? "disabled" : ""} aria-label="Move category up">&#8593;</button>
        <button type="button" class="secondary-button" data-category-down ${index === draft.categories.length - 1 ? "disabled" : ""} aria-label="Move category down">&#8595;</button>
        <button type="button" class="secondary-button danger" data-category-delete ${draft.categories.length === 1 ? "disabled" : ""}>Delete</button>
        <span class="hint">${names.length} LoRAs</span></div>
      <div class="lora-config-items">${names.map((name, row) => {
        const before = gallery.hermesState === "review" ? gallery.hermesBaseline?.assignments[name] : null;
        const moved = before && before !== category;
        return `<div class="lora-config-item${moved ? " is-moved" : ""}" data-config-filename="${escapeHtml(name)}">
        <span title="${escapeHtml(name)}">${escapeHtml(labels.get(name))}${sources.get(name) === "trainer" ? `<em class="lora-trainer-badge">Trainer</em>` : ""}${moved ? `<small>Moved from ${escapeHtml(before)}</small>` : ""}</span>
        <select data-config-category aria-label="Category for ${escapeHtml(labels.get(name))}">${draft.categories.map((option) => `<option value="${escapeHtml(option)}" ${option === category ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}</select>
        <button type="button" class="secondary-button" data-item-up ${row === 0 ? "disabled" : ""} aria-label="Move LoRA up">&#8593;</button>
        <button type="button" class="secondary-button" data-item-down ${row === names.length - 1 ? "disabled" : ""} aria-label="Move LoRA down">&#8595;</button>
      </div>`; }).join("") || `<p class="hint">No LoRAs in this category.</p>`}</div>
    </section>`;
  }).join("");
  renderHermesVisual();
}

function openGalleryConfig() {
  if (gallery.configOpen) return;
  $("#lora-gallery-link-panel").hidden = true;
  $("#lora-gallery-link-toggle").setAttribute("aria-expanded", "false");
  gallery.draft = newGalleryDraft();
  gallery.hermesState = "idle";
  gallery.hermesBaseline = null;
  gallery.configOpen = true;
  gallery.detail = null;
  $("#lora-gallery-grid").hidden = true;
  $("#lora-gallery-grid").innerHTML = "";
  $("#lora-gallery-detail").hidden = true;
  $("#lora-gallery-tools").hidden = true;
  $("#lora-gallery-config").hidden = false;
  $("#lora-config-status").textContent = "Rename, reorder, add, or remove categories. Move LoRAs between categories and set their display order.";
  renderGalleryConfig();
}

async function saveGalleryConfig() {
  const status = $("#lora-config-status");
  try {
    const saved = await Api.request(`${API_BASE}/api/lora-gallery/catalog/${galleryModel()}`, { method: "PUT", body: gallery.draft });
    gallery.catalog = saved;
    gallery.draft = null;
    gallery.hermesState = "idle";
    gallery.hermesBaseline = null;
    await refreshManagedLoras();
    status.textContent = "Saved categories and LoRA order.";
    toast("Gallery categories saved.");
  } catch (error) { status.textContent = error.message; toast(error.message, "error"); }
}

async function organizeGalleryWithHermes() {
  const button = $("#lora-config-hermes"), status = $("#lora-config-status");
  const request = ++gallery.hermesRequest;
  const previous = JSON.parse(JSON.stringify(gallery.draft));
  gallery.hermesBaseline = previous;
  gallery.hermesState = "running";
  gallery.hermesStage = "Queued";
  button.disabled = true;
  renderHermesVisual();
  status.textContent = "Hermes is reading the gallery. Your current categories have not changed.";
  try {
    const response = await Api.request(`${API_BASE}/api/lora-gallery/catalog/${galleryModel()}/organize`, { method: "POST" });
    if (request !== gallery.hermesRequest || !gallery.configOpen) return;
    let task = response.task;
    while (task.state === "running") {
      if (gallery.hermesStage !== task.stage) {
        gallery.hermesStage = task.stage;
        renderHermesVisual();
        status.textContent = `${task.stage}…`;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (request !== gallery.hermesRequest || !gallery.configOpen) return;
      task = (await Api.request(`${API_BASE}/api/lora-gallery/organize/${task.id}`)).task;
    }
    if (task.state !== "complete") throw new Error(task.error || "Hermes could not organize the gallery");
    gallery.draft = task.proposal;
    gallery.hermesState = "review";
    renderGalleryConfig();
    status.textContent = "Hermes proposal is ready. The colored summary shows what would change; save when you are satisfied.";
  } catch (error) {
    if (request !== gallery.hermesRequest || !gallery.configOpen) return;
    gallery.hermesState = "idle";
    gallery.hermesBaseline = null;
    renderHermesVisual();
    status.textContent = error.message;
    toast(error.message, "error");
  } finally { if (request === gallery.hermesRequest) button.disabled = false; }
}

function bindLoraGallery() {
  $("#lora-gallery-link-toggle").addEventListener("click", toggleGalleryLink);
  $("#lora-gallery-link-add").addEventListener("click", linkGalleryLora);
  $("#lora-gallery-link-url").addEventListener("keydown", (event) => { if (event.key === "Enter") linkGalleryLora(); });
  $("#lora-gallery-config-button").addEventListener("click", openGalleryConfig);
  $("#lora-config-back").addEventListener("click", () => { gallery.draft = null; showGalleryGrid(); renderLoraGallery(); });
  $("#lora-config-save").addEventListener("click", saveGalleryConfig);
  $("#lora-config-hermes").addEventListener("click", organizeGalleryWithHermes);
  $("#lora-hermes-visual").addEventListener("click", (event) => {
    if (!event.target.closest("#lora-hermes-discard") || !gallery.hermesBaseline) return;
    gallery.draft = gallery.hermesBaseline;
    gallery.hermesBaseline = null;
    gallery.hermesState = "idle";
    $("#lora-config-status").textContent = "Hermes proposal discarded. Your previous draft is restored.";
    renderGalleryConfig();
  });
  $("#lora-config-add").addEventListener("click", () => {
    const input = $("#lora-config-new"), name = input.value.trim();
    if (!name || name.toLowerCase() === "uploaded" || gallery.draft.categories.some((value) => value.toLowerCase() === name.toLowerCase())) {
      toast("Enter a unique category name.", "error"); return;
    }
    gallery.draft.categories.push(name); input.value = ""; renderGalleryConfig();
  });
  $("#lora-config-categories").addEventListener("change", (event) => {
    const draft = gallery.draft, section = event.target.closest("[data-category-index]");
    if (!draft || !section) return;
    const index = Number(section.dataset.categoryIndex);
    if (event.target.matches("[data-category-name]")) {
      const old = draft.categories[index], name = event.target.value.trim();
      if (!name || name.toLowerCase() === "uploaded" || draft.categories.some((value, position) => position !== index && value.toLowerCase() === name.toLowerCase())) {
        toast("Enter a unique category name.", "error"); event.target.value = old; return;
      }
      draft.categories[index] = name;
      for (const filename of draft.order) if (draft.assignments[filename] === old) draft.assignments[filename] = name;
      renderGalleryConfig();
    } else if (event.target.matches("[data-config-category]")) {
      draft.assignments[event.target.closest("[data-config-filename]").dataset.configFilename] = event.target.value;
      renderGalleryConfig();
    }
  });
  $("#lora-config-categories").addEventListener("click", (event) => {
    const button = event.target.closest("[data-category-up], [data-category-down], [data-category-delete], [data-item-up], [data-item-down]");
    if (!button || !gallery.draft) return;
    const draft = gallery.draft, section = button.closest("[data-category-index]");
    const index = Number(section.dataset.categoryIndex), category = draft.categories[index];
    if (button.hasAttribute("data-category-delete")) {
      const replacement = draft.categories.find((value) => value !== category);
      if (!replacement) return;
      for (const filename of draft.order) if (draft.assignments[filename] === category) draft.assignments[filename] = replacement;
      draft.categories.splice(index, 1);
    } else if (button.hasAttribute("data-category-up") || button.hasAttribute("data-category-down")) {
      const other = index + (button.hasAttribute("data-category-up") ? -1 : 1);
      [draft.categories[index], draft.categories[other]] = [draft.categories[other], draft.categories[index]];
    } else {
      const filename = button.closest("[data-config-filename]").dataset.configFilename;
      const group = draft.order.filter((name) => draft.assignments[name] === category);
      const neighbor = group[group.indexOf(filename) + (button.hasAttribute("data-item-up") ? -1 : 1)];
      if (!neighbor) return;
      const a = draft.order.indexOf(filename), b = draft.order.indexOf(neighbor);
      [draft.order[a], draft.order[b]] = [draft.order[b], draft.order[a]];
    }
    renderGalleryConfig();
  });
  $("#lora-gallery-upload-file").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file && !$("#lora-gallery-upload-name").value) $("#lora-gallery-upload-name").value = file.name.replace(/\.safetensors$/i, "");
  });
  $("#lora-gallery-upload").addEventListener("click", uploadGalleryLora);
  $$("[data-lora-gallery]").forEach((button) => button.addEventListener("click", openLoraGallery));
  $$("[data-gallery-close]").forEach((node) => node.addEventListener("click", closeLoraGallery));
  document.addEventListener("keydown", (event) => {
    if ($("#lora-gallery").hidden || !$("#alias-dialog").hidden || event.defaultPrevented) return; // Esc closes the alias dialog on top first
    if (event.key === "Escape") {
      if (gallery.detail) closeGalleryDetail();
      else if (gallery.configOpen) { gallery.draft = null; showGalleryGrid(); renderLoraGallery(); }
      else closeLoraGallery();
    } else if (gallery.detail && (event.key === "ArrowLeft" || event.key === "ArrowRight") && !event.target.matches("input, select, textarea")) {
      stepGalleryPreview($(".lora-card-detail", $("#lora-gallery-detail")), event.key === "ArrowLeft" ? -1 : 1);
    }
  });
  $("#lora-gallery-search").addEventListener("input", renderLoraGallery);
  $("#lora-gallery-group").addEventListener("change", renderLoraGallery);
  $("#lora-gallery-enabled-only").addEventListener("change", renderLoraGallery);
  $("#lora-gallery-by-group").checked = galleryByGroup();
  $("#lora-gallery-by-group").addEventListener("change", (event) => {
    try { localStorage.setItem(GALLERY_BY_GROUP_KEY, event.target.checked ? "on" : "off"); } catch { /* storage blocked */ }
    renderLoraGallery();
  });
  $("#lora-gallery-nsfw").addEventListener("change", (event) => {
    try { localStorage.setItem(GALLERY_NSFW_KEY, event.target.checked ? "on" : "off"); } catch { /* per-browser convenience */ }
    renderLoraGallery();
  });
  const dialog = $("#lora-gallery");
  dialog.addEventListener("click", (event) => {
    const managed = event.target.closest("[data-gallery-save], [data-gallery-scrape], [data-gallery-delete]");
    if (managed) {
      const editor = managed.closest("[data-custom-editor]");
      if (managed.hasAttribute("data-gallery-save")) saveManagedLora(editor);
      else if (managed.hasAttribute("data-gallery-scrape")) scrapeManagedLora(editor);
      else deleteManagedLora(editor);
      return;
    }
    const aliasButton = event.target.closest("[data-gallery-alias-add], [data-gallery-alias-edit]");
    if (aliasButton) {
      const filename = aliasButton.closest("[data-gallery-aliases]").dataset.galleryAliases;
      openAliasDialog(filename, aliasButton.dataset.galleryAliasEdit);
      return;
    }
    const target = event.target.closest("[data-gallery-step], [data-gallery-thumb], [data-gallery-reveal], [data-gallery-open], [data-gallery-edit], [data-gallery-back], [data-gallery-lora], [data-gallery-trigger]");
    if (!target) return;
    const card = target.closest("[data-gallery-card]");
    if (target.dataset.galleryTrigger) insertTriggerWord(target.dataset.galleryTrigger);
    else if (target.dataset.galleryStep) stepGalleryPreview(card, Number(target.dataset.galleryStep));
    else if (target.dataset.galleryThumb) {
      gallery.preview.set(card.dataset.galleryCard, Number(target.dataset.galleryThumb));
      rerenderGalleryMedia(card);
    } else if (target.dataset.galleryReveal) {
      const preview = galleryPreviews(card.dataset.galleryCard)[gallery.preview.get(card.dataset.galleryCard) || 0];
      if (preview?.video) playGalleryPreview(card);
      else {
        card.querySelector(".lora-card-media").classList.remove("is-blurred");
        target.remove();
      }
    } else if (target.hasAttribute("data-gallery-edit")) openGalleryDetail(card.dataset.galleryCard, true);
    else if (target.hasAttribute("data-gallery-open")) openGalleryDetail(card.dataset.galleryCard);
    else if (target.hasAttribute("data-gallery-back")) closeGalleryDetail();
    else if (target.dataset.galleryLora) openGalleryDetail(target.dataset.galleryLora);
  });
  dialog.addEventListener("change", (event) => {
    const card = event.target.closest("[data-gallery-card]");
    const row = card && galleryEntry(card.dataset.galleryCard)?.row;
    if (!row) return;
    if (event.target.matches("[data-gallery-toggle]")) {
      const box = $("[data-lora-enabled]", row);
      box.checked = event.target.checked;
      box.dispatchEvent(new Event("change", { bubbles: true })); // row tint, counts, LTX lockout, trigger snippets
      syncGalleryCards();
    } else if (event.target.matches("[data-gallery-strength]")) {
      const strength = $("[data-lora-strength]", row);
      strength.value = event.target.value;
      strength.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}

/* ---------------------------------------------------------------- sse -- */

// The server only pushes changes, and doesn't replay the current state on connect.
// Phones drop the stream while the page is in the background, so any job updates in
// that window (including "complete" + output_url) were lost until a reload. Each
// reconnect therefore pulls a fresh snapshot. A watchdog reconnects a stream that went quiet (no ping in 35 s).
let eventSource = null;
let eventsConnectedOnce = false;
let resyncInFlight = null;
let lastEventAt = Date.now(); // any event, incl. the server's 15 s ping
let pendingDeltas = null; // deltas that arrive while a snapshot is being fetched
let resyncRetryTimer = null;
let resyncRetryDelay = 0;

function applyJobs(jobs) {
  detectCompletedJobs(jobs);
  const doneCount = (list) => list.filter((job) => job.state === "complete").length;
  if (doneCount(jobs) > doneCount(state.jobs)) scheduleEstimate(); // a new render time was just recorded
  const needsRender = jobsNeedRender(state.jobs, jobs);
  state.jobs = jobs;
  if (needsRender) renderJobs();
  else updateJobMetrics();
  if (document.body.dataset.view === "postedit") peRenderSources();
}

// Live job updates arrive as deltas (publish_jobs in app.py): only changed
// jobs, removed ids, and the order when it changed. seq is consecutive; a gap
// means the server dropped an event for this tab, so re-fetch everything.
let jobsSeq = null;
function applyJobsDelta(delta) {
  // Mid-resync the snapshot may be older than this delta; replay it on top afterwards.
  if (pendingDeltas) { pendingDeltas.push(delta); return; }
  const { seq, upsert, removed, order } = delta;
  if (jobsSeq !== null && seq !== jobsSeq + 1) {
    jobsSeq = null;
    resyncState();
    return;
  }
  jobsSeq = seq;
  const byId = new Map(state.jobs.map((job) => [job.id, job]));
  removed.forEach((id) => byId.delete(id));
  upsert.forEach((job) => byId.set(job.id, job));
  const jobs = order
    ? order.map((id) => byId.get(id)).filter(Boolean)
    : [...byId.values()].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  applyJobs(jobs);
}

function applyReferences(references) {
  state.references = references;
  renderKeyframes();
  renderReferenceGallery();
  renderSeedhunterGalleries();
  renderStitchMusic();
  if (document.body.dataset.view === "postedit" && pe.bound) peRenderLists();
}

function resyncState() {
  resyncInFlight ??= (async () => {
    pendingDeltas = [];
    try {
      const [jobs, status, references] = await Promise.all([
        Api.request(`${API_BASE}/api/jobs`), Api.request(`${API_BASE}/api/status`), Api.request(`${API_BASE}/api/references`),
      ]);
      state.serviceStatus = status;
      renderStatus();
      jobsSeq = null; // the next delta re-anchors on this full snapshot
      applyJobs(jobs);
      applyReferences(references);
      resyncRetryDelay = 0;
      clearTimeout(resyncRetryTimer);
    } catch {
      // Retried until it lands: the stream is often live again by now (a phone
      // waking before its network is), so no reconnect would come to redo it,
      // and a render that finished meanwhile stayed "rendering" until a reload.
      resyncRetryDelay = Math.min(30_000, (resyncRetryDelay || 1_500) * 2);
      clearTimeout(resyncRetryTimer);
      resyncRetryTimer = setTimeout(resyncState, resyncRetryDelay);
    } finally {
      const buffered = pendingDeltas;
      pendingDeltas = null;
      resyncInFlight = null;
      buffered.forEach(applyJobsDelta); // in order; a gap among them triggers another resync
    }
  })();
  return resyncInFlight;
}

function connectEvents() {
  eventSource?.close();
  const source = new EventSource(`${API_BASE}/api/events`);
  eventSource = source;
  lastEventAt = Date.now(); // grace period while it connects
  const seen = () => { lastEventAt = Date.now(); };
  ["open", "ping", "status", "jobs", "jobs-delta", "references"].forEach((name) => source.addEventListener(name, seen));
  source.addEventListener("open", () => {
    if (eventsConnectedOnce) resyncState(); // init() already loaded the first snapshot
    eventsConnectedOnce = true;
  });
  source.addEventListener("status", (event) => { state.serviceStatus = JSON.parse(event.data); renderStatus(); });
  source.addEventListener("jobs", (event) => applyJobs(JSON.parse(event.data)));
  source.addEventListener("jobs-delta", (event) => applyJobsDelta(JSON.parse(event.data)));
  source.addEventListener("references", (event) => applyReferences(JSON.parse(event.data)));
  // EventSource retries by itself unless it gave up (CLOSED) - then reconnect manually.
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && source === eventSource) setTimeout(() => { if (source === eventSource) connectEvents(); }, 3000);
  };
}

function bindEventRecovery() {
  // The server sends at least a ping every 15 s, so a stream quiet for longer
  // than STALE has died (phones suspend it in the background). Coming back to a
  // tab whose stream stayed live no longer reconnects + re-fetches everything.
  const STALE = 35_000;
  const stale = () => !eventSource || eventSource.readyState === EventSource.CLOSED || Date.now() - lastEventAt > STALE;
  const reconnect = (force) => { if (document.visibilityState === "visible" && (force || stale())) connectEvents(); };
  document.addEventListener("visibilitychange", () => reconnect(false));
  window.addEventListener("pageshow", (event) => { if (event.persisted) reconnect(true); }); // restored from bfcache
  window.addEventListener("online", () => reconnect(true));
  setInterval(() => reconnect(false), 10_000);
}

async function init() {
  hydrateIcons();
  bindUI();
  // Refreshing or closing mid-typing: save now rather than after the debounce.
  window.addEventListener("pagehide", () => { savePromptDrafts(); saveSessionSettings(); });
  bindPromptFullscreen();
  bindPingSettings();
  loadPingSettings();
  bindPromptRefs();
  bindPromptRefSplit();
  try {
    const query = new URLSearchParams(location.search);
    // Started now rather than after the snapshot: on a phone each serial round trip is ~150+ ms.
    const presetsFetch = Api.request(`${API_BASE}/api/presets`);
    const aliasesFetch = Api.request(`${API_BASE}/api/trigger-aliases`);
    const upscalePresetsFetch = Api.request(`${API_BASE}/api/upscale-presets`);
    const multiGpuPresetsFetch = Api.request(`${API_BASE}/api/multi-gpu-presets`);
    [presetsFetch, aliasesFetch, upscalePresetsFetch, multiGpuPresetsFetch].forEach((fetch) => fetch.catch(() => {})); // handled where awaited
    const [options, references, jobs, status] = await Promise.all([
      Api.request(`${API_BASE}/api/settings/options`), Api.request(`${API_BASE}/api/references`), Api.request(`${API_BASE}/api/jobs`), Api.request(`${API_BASE}/api/status`),
    ]);
    state.options = options;
    state.references = references;
    state.jobs = jobs;
    jobs.filter((job) => job.state === "complete").forEach((job) => completedJobs.add(job.id));
    state.serviceStatus = status;

    fillSelect("#aspect", options.aspects);
    fillSelect("#sampler", options.samplers);
    fillSelect("#scheduler", options.schedulers);
    fillSelect("#sh-sampler", options.samplers);
    fillSelect("#sh-scheduler", options.schedulers);
    fillSelect("#sh-upscale-model", options.upscale_models);
    fillUpscaleAdvancedSelects(options);
    loadUpscalePresets(upscalePresetsFetch);
    loadMultiGpuPresets(multiGpuPresetsFetch);
    state.h3CfgEnabled = options.h3_cfg_enabled !== false;
    state.h3CfgLockReason = options.h3_cfg_lock_reason || "";
    fillSelect("#sh-overlap-frames", options.overlap_frame_options);
    $("#sh-overlap-frames").value = options.defaults.overlap_frames;
    fillSelect("#ref-size", options.reference_sizing);
    fillOptions("#ltx-tiles", options.ltx_tile_presets);
    fillOptions("#ltx-workflow", options.ltx_workflows);
    fillOptions("#stitch-transition", options.stitch_transitions);
    $("#stitch-transition").value = "cut";
    updateStitchTransitionFields();
    renderCheckpointOptions();
    $("#minimax-checkpoint").value = "";
    renderLoraList(options.minimax_h3_lora_slots);
    Object.keys(LIST_LORAS).forEach((model) => renderListLoras(model, options));
    fillSelect("#wan-sampler", options.samplers);
    fillSelect("#wan-scheduler", options.schedulers);
    $("#wan-sampler").value = options.defaults.sampler;
    $("#wan-scheduler").value = options.defaults.scheduler;
    $("#aspect").value = options.defaults.aspect;
    $("#sampler").value = options.defaults.sampler;
    $("#scheduler").value = options.defaults.scheduler;
    $("#sh-sampler").value = options.samplers.includes("er_sde") ? "er_sde" : options.defaults.sampler;
    $("#sh-scheduler").value = options.schedulers.includes("beta") ? "beta" : options.defaults.scheduler;
    $("#sh-upscale-model").value = options.defaults.upscale_model;
    $("#ref-size").value = options.defaults.reference_sizing;
    $("#ltx-tiles").value = options.defaults.ltx_tile_preset;
    $("#ltx-workflow").value = options.defaults.ltx_workflow;
    $("#ltx-fps").value = options.defaults.ltx_fps;
    $("#duration").value = options.defaults.duration;
    $("#duration-readout").textContent = durationReadout(options.defaults.duration);
    $("#ltx-tile-detail").textContent = ltxTileDetails[options.defaults.ltx_tile_preset];

    let resetModel = null;
    try { resetModel = sessionStorage.getItem(SETTINGS_RESET_KEY); sessionStorage.removeItem(SETTINGS_RESET_KEY); } catch { /* storage blocked */ }
    const wantedModel = query.get("model") || resetModel;
    const requestedModel = options.models.some((item) => item.value === wantedModel) ? wantedModel : options.defaults.model;
    await loadPresets(presetsFetch); // before setModel(), which draws the preset bar
    await loadTriggerAliases(aliasesFetch);
    await setModel(requestedModel);
    setMode(options.defaults.mode || "text");
    restorePromptDrafts();

    updateAlertsButton();
    registerServiceWorker();
    renderKeyframes();
    renderReferenceGallery();
    renderSeedhunterGalleries();
    renderStitchMusic();
    renderJobs();
    // Opened from a notification: jump to that job.
    const linkedJob = query.get("job");
    if (linkedJob && showJob(linkedJob)) history.replaceState(null, "", location.pathname);
    renderStatus();
    await updateResolution();
    renderCfg();
    if (resetModel || !(await restoreSessionSettings(query))) await applySelectedPreset();
    sessionSettingsReady = true;
    saveSessionSettings();

    await restoreAgenticSelection();
    loadEnhancement(); // the status pill shows DeepSeek when that's the Enhance provider
    setInterval(updateJobMetrics, 1000);
    setInterval(updateActiveJobMetrics, 250);
    if (!query.has("snapshot")) { connectEvents(); bindEventRecovery(); }
  } catch (error) {
    toast(`Video could not initialize: ${error.message}`, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);
