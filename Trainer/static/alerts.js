// Training alerts (ported from Video): a chime while the page is open, plus
// notifications. Notifications go through the service worker (Android refuses the
// plain Notification constructor) and, where supported, Web Push - the server pushes
// when a run finishes, so they arrive even when the tab is closed or the phone is
// locked. iOS only allows this for the app added to the Home Screen.
(() => {
  const base = window.TRAINER_BASE || "/Trainer";
  const $ = (s) => document.querySelector(s);
  // Opened from a notification (?job=...): remember it before the tab code rewrites the URL.
  const openJob = new URLSearchParams(location.search).get("job");
  const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isInstalled = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let alertsEnabled = false; try { alertsEnabled = localStorage.getItem("trainer-alerts") === "on"; } catch (_) {}
  let swRegistration = null, pushSubscribed = false, audioContext = null;
  const seenStates = new Map();   // job id -> last state seen, so only real transitions alert

  async function api(path, body) {
    const r = await fetch(base + path, {method: body ? "POST" : "GET", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }
  function toast(text) {
    let box = $("#trainer-toasts");
    if (!box) { box = document.createElement("div"); box.id = "trainer-toasts"; box.className = "trainer-toasts"; box.setAttribute("role", "status"); document.body.append(box); }
    const node = document.createElement("div"); node.className = "trainer-toast"; node.textContent = text;
    box.append(node); setTimeout(() => node.remove(), 6000);
  }
  const permission = () => ("Notification" in window ? Notification.permission : "unsupported");

  function updateButton() {
    const button = $("#alerts-toggle"); if (!button) return;
    button.classList.toggle("is-enabled", alertsEnabled);
    button.setAttribute("aria-pressed", String(alertsEnabled));
    button.title = !alertsEnabled ? "Get a notification when a LoRA finishes training"
      : pushSubscribed ? "Alerts on · notifications arrive even with the Trainer closed (click to turn off)"
      : permission() === "granted" ? "Alerts on · notifications while this page is open (click to turn off)"
      : isIOS && !isInstalled ? "Chime on · for notifications, add the Trainer to your Home Screen first"
      : "Chime on · click to allow notifications";
  }

  // ---- sound ----
  function unlockAudio() {
    try {
      audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
      if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
    } catch (_) {}
  }
  function chime(failed) {
    unlockAudio();
    if (!audioContext || audioContext.state !== "running") return;
    // Rising marimba for done, a low falling pair for failed.
    const notes = failed ? [[0, 392], [0.16, 311.13]] : [[0, 523.25], [0.11, 659.25], [0.22, 783.99]];
    const now = audioContext.currentTime;
    for (const [delay, freq] of notes) {
      const osc = audioContext.createOscillator(), gain = audioContext.createGain();
      osc.type = "sine"; osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + delay);
      gain.gain.exponentialRampToValueAtTime(0.18, now + delay + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.4);
      osc.connect(gain).connect(audioContext.destination);
      osc.start(now + delay); osc.stop(now + delay + 0.45);
    }
  }
  // Browsers only start audio after a user gesture: unlock on the first one.
  for (const kind of ["pointerdown", "keydown"]) window.addEventListener(kind, () => { if (alertsEnabled) unlockAudio(); }, {once: true, capture: true});

  // ---- push ----
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
  const b64ToBytes = (v) => Uint8Array.from(atob((v + "=".repeat((4 - (v.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  async function syncPush() {
    pushSubscribed = false;
    if (!pushSupported || !swRegistration || !alertsEnabled || permission() !== "granted") return updateButton();
    if (await BunnyHub.available()) {
      try {
        // Drop the Trainer's own subscription (if any): the hub's covers every studio.
        const own = await swRegistration.pushManager.getSubscription();
        if (own) await own.unsubscribe();
        await BunnyHub.subscribe();
        pushSubscribed = true;
      } catch (_) { /* push stays off; in-page notifications still work */ }
      return updateButton();
    }
    try {
      const serverKey = b64ToBytes((await api("/api/push/key")).public_key);
      let sub = await swRegistration.pushManager.getSubscription();
      const key = sub?.options?.applicationServerKey;
      if (sub && key && !new Uint8Array(key).every((byte, i) => byte === serverKey[i])) { await sub.unsubscribe(); sub = null; }
      sub ||= await swRegistration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: serverKey});
      await api("/api/push/subscribe", sub.toJSON());
      pushSubscribed = true;
    } catch (_) { /* push stays off; in-page notifications still work */ }
    updateButton();
  }
  async function removePush() {
    pushSubscribed = false;
    if (await BunnyHub.available()) {
      try { await BunnyHub.unsubscribe(); } catch (_) {}
      return;
    }
    try {
      const sub = await swRegistration?.pushManager.getSubscription();
      if (sub) { await api("/api/push/unsubscribe", {endpoint: sub.endpoint}).catch(() => {}); await sub.unsubscribe(); }
    } catch (_) {}
  }
  async function registerWorker() {
    if (!("serviceWorker" in navigator)) return updateButton();
    try { swRegistration = await navigator.serviceWorker.register(`${base}/sw.js`, {scope: `${base}/`}); }
    catch (_) { return updateButton(); }
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data?.type === "show-job") showJob(e.data.jobId); });
    await syncPush();
  }

  async function toggle() {
    const perm = permission();
    if (alertsEnabled && perm !== "default") {
      alertsEnabled = false; try { localStorage.setItem("trainer-alerts", "off"); } catch (_) {}
      await removePush(); toast("Training alerts off."); return updateButton();
    }
    alertsEnabled = true; try { localStorage.setItem("trainer-alerts", "on"); } catch (_) {}
    unlockAudio();
    if (isIOS && !isInstalled && perm === "unsupported") {
      toast("Chime is on. For notifications on iPhone: tap Share → Add to Home Screen, open the Trainer from there, then tap the bell.");
      return updateButton();
    }
    const granted = perm === "granted" || (perm === "default" && await Notification.requestPermission() === "granted");
    if (!granted) {
      toast(perm === "denied" ? "Chime is on. Notifications are blocked for this site — allow them in the browser's site settings."
                              : "Chime is on. Notifications were not allowed.");
      return updateButton();
    }
    await syncPush();
    if (pushSubscribed && BunnyHub._available) {
      BunnyHub.test();
      toast("Notifications on for this device — every BunnyBytes studio can reach you; pick events in Settings → Notifications. A test is on its way.");
    } else if (pushSubscribed) {
      const sub = await swRegistration.pushManager.getSubscription();
      api("/api/push/test", {endpoint: sub.endpoint}).catch(() => {});
      toast("Notifications on — you'll get one when a LoRA finishes training, even with the Trainer closed. A test notification is on its way.");
    } else toast("Notifications on while this page is open.");
    updateButton();
  }

  // ---- job transitions (called by trainer.js after every job refresh) ----
  function alertFor(job) {
    const failed = job.state === "failed";
    chime(failed);
    navigator.vibrate?.([120, 60, 120]);
    // With a push subscription the server sends the notification itself (same tag).
    if (pushSubscribed || permission() !== "granted") return;
    const title = failed ? "LoRA training failed" : "LoRA training complete";
    const options = {body: `${job.model_title || job.model} · ${job.name}\n${failed ? String(job.error || "").slice(0, 140) : `Dataset ${job.dataset}`}`,
      tag: `trainer-job-${job.id}`, icon: `${base}/static/icons/trainer-192.png`, badge: `${base}/static/icons/trainer-badge-96.png`,
      data: {url: `${base}/?job=${job.id}`}};
    try {
      if (swRegistration) { swRegistration.showNotification(title, options).catch(() => {}); return; }
      const n = new Notification(title, options);
      n.onclick = () => { window.focus(); showJob(job.id); n.close(); };
    } catch (_) {}
  }
  function jobs(list) {
    const first = !seenStates.size && !jobs.seeded;
    jobs.seeded = true;
    for (const job of list) {
      const before = seenStates.get(job.id);
      seenStates.set(job.id, job.state);
      if (first || before === undefined || before === job.state) continue;
      if (alertsEnabled && ["complete", "failed"].includes(job.state)) alertFor(job);
    }
    if (pendingJob && list.some((j) => j.id === pendingJob)) { const id = pendingJob; pendingJob = null; requestAnimationFrame(() => showJob(id)); }
  }

  let pendingJob = openJob;
  function showJob(id) {
    document.querySelector('.tab[data-tab="train"]')?.click();
    const card = document.querySelector(`.job-card[data-job-id="${CSS.escape(id)}"]`);
    if (!card) { pendingJob = id; return; }
    card.scrollIntoView({behavior: "smooth", block: "center"});
    card.classList.remove("job-flash"); void card.offsetWidth; card.classList.add("job-flash");
  }

  window.trainerAlerts = {jobs};
  document.addEventListener("DOMContentLoaded", () => {
    $("#alerts-toggle")?.addEventListener("click", toggle);
    updateButton(); registerWorker();
  });
})();
