// Rocsy Settings: tab switching + the Notifications tab (one Web Push
// subscription per device through /notify-sw, per-service event switches).
(() => {
  /* ---------------------------------------------------------- tabs -- */
  const tabs = [...document.querySelectorAll(".bb-tab")];
  function showTab(name, push = true) {
    let anchor = null;
    if (!tabs.some((tab) => tab.dataset.tab === name)) {
      // A section id (e.g. #connections) opens the tab that holds it.
      anchor = name && document.getElementById(name);
      const holder = anchor?.closest?.(".bb-tab-panel");
      name = holder ? holder.id.replace(/^panel-/, "") : "appearance";
      if (!holder) anchor = null;
    }
    tabs.forEach((tab) => {
      const on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(`panel-${tab.dataset.tab}`).hidden = !on;
    });
    if (push) history.replaceState(null, "", name === "appearance" ? location.pathname : `#${name}`);
    if (name === "notifications") load();
    anchor?.scrollIntoView({ block: "start" });
  }
  tabs.forEach((tab) => tab.addEventListener("click", () => showTab(tab.dataset.tab)));
  document.querySelector(".bb-tabs").addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    const index = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    const next = tabs[(index + step + tabs.length) % tabs.length];
    event.preventDefault();  // the arrow keys would also scroll the page
    showTab(next.dataset.tab);
    next.focus();
  });

  /* -------------------------------------------------- notifications -- */
  const status = document.getElementById("notify-status");
  const dot = document.getElementById("device-dot");
  const title = document.getElementById("device-title");
  const note = document.getElementById("device-note");
  const toggle = document.getElementById("device-toggle");
  const testButton = document.getElementById("device-test");
  const deviceList = document.getElementById("device-list");
  const deviceItems = document.getElementById("device-items");
  const serviceList = document.getElementById("service-list");
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let data = null;
  let registration = null;
  let subscription = null;
  let loaded = false;
  let statusTimer = null;

  function setStatus(text, kind = "") {
    status.textContent = text;
    status.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { status.textContent = ""; }, 2500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
    });
    if (response.status === 401) { location.href = "/login?next=/Settings%23notifications"; throw new Error("login"); }
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `HTTP ${response.status}`);
    return response.json();
  }

  function deviceLabel() {
    const ua = navigator.userAgent;
    const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) || isIOS ? "iPad" : /Android/.test(ua) ? "Android"
      : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "Device";
    const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
      : /Safari\//.test(ua) ? "Safari" : "Browser";
    return `${os} · ${browser}`;
  }

  function keyBytes(base64) {
    const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(padded), (ch) => ch.charCodeAt(0));
  }

  function renderDevice() {
    const permission = supported ? Notification.permission : "unsupported";
    const on = Boolean(subscription && data?.devices.some((device) => device.endpoint === subscription.endpoint));
    dot.dataset.state = on ? "on" : permission === "denied" ? "blocked" : "off";
    toggle.disabled = !supported || permission === "denied" || (isIOS && !standalone);
    toggle.textContent = on ? "Turn off" : "Turn on";
    toggle.classList.toggle("bb-button-primary", !on);
    testButton.hidden = !on;
    if (!supported && isIOS && !standalone) {
      title.textContent = "Add Rocsy to your Home Screen first";
      note.textContent = "On iPhone and iPad, notifications only work from the Home Screen app: tap Share → Add to Home Screen, open it from there, then come back here.";
    } else if (!supported) {
      title.textContent = "This browser can't receive notifications";
      note.textContent = "Try Chrome, Edge, Firefox or Safari.";
    } else if (permission === "denied") {
      title.textContent = "Notifications are blocked";
      note.textContent = "Allow notifications for this site in your browser's site settings, then reload.";
    } else if (on) {
      title.textContent = "Notifications are on";
      note.textContent = `${deviceLabel()} gets the events you switch on below, even with the site closed.`;
    } else {
      title.textContent = "Notifications are off";
      note.textContent = "Turn them on to hear when renders, images, tracks and training runs finish.";
    }
    const devices = data?.devices || [];
    deviceList.hidden = devices.length === 0;
    deviceItems.replaceChildren(...devices.map((device) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = device.label + (subscription?.endpoint === device.endpoint ? " (this device)" : "");
      const when = document.createElement("span");
      when.className = "bb-device-when";
      when.textContent = device.created ? `since ${new Date(device.created * 1000).toLocaleDateString()}` : "";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "bb-link-button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        try {
          if (subscription?.endpoint === device.endpoint) { await subscription.unsubscribe(); subscription = null; }
          data.devices = (await api("/api/notifications/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint: device.endpoint }) })).devices;
          renderDevice();
          setStatus("Removed", "ok");
        } catch (error) { if (error.message !== "login") setStatus(`Couldn't remove (${error.message})`, "error"); }
      });
      item.append(label, when, remove);
      return item;
    }));
  }

  function switchRow(service, event, checked) {
    const row = document.createElement("label");
    row.className = "bb-switch-row";
    const text = document.createElement("span");
    text.textContent = event.label;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "bb-switch";
    input.checked = checked;
    input.setAttribute("role", "switch");
    input.addEventListener("change", async () => {
      setStatus("Saving…");
      try {
        data.prefs = (await api("/api/notifications", { method: "PUT", body: JSON.stringify({ prefs: { [service]: { [event.id]: input.checked } } }) })).prefs;
        setStatus("Saved", "ok");
      } catch (error) {
        input.checked = !input.checked;
        if (error.message !== "login") setStatus(`Couldn't save (${error.message})`, "error");
      }
    });
    row.append(text, input);
    return row;
  }

  function renderServices() {
    serviceList.replaceChildren(...data.catalog.map((service) => {
      const section = document.createElement("section");
      section.className = "bb-section bb-service";
      const head = document.createElement("div");
      head.className = "bb-section-head";
      const name = document.createElement("h2");
      name.textContent = service.label;
      const sub = document.createElement("span");
      sub.className = "bb-status";
      sub.textContent = service.note;
      head.append(name, sub);
      const rows = document.createElement("div");
      rows.className = "bb-switch-list";
      rows.append(...service.events.map((event) => switchRow(service.id, event, data.prefs[service.id][event.id])));
      section.append(head, rows);
      return section;
    }));
  }

  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      data = await api("/api/notifications");
      if (supported) {
        registration = await navigator.serviceWorker.register("/notify-sw", { scope: "/" });
        subscription = await registration.pushManager.getSubscription();
      }
      renderServices();
      renderDevice();
    } catch (error) {
      loaded = false;
      if (error.message !== "login") setStatus(`Couldn't load (${error.message})`, "error");
    }
  }

  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      const on = subscription && data.devices.some((device) => device.endpoint === subscription.endpoint);
      if (on) {
        const endpoint = subscription.endpoint;
        await subscription.unsubscribe();
        subscription = null;
        data.devices = (await api("/api/notifications/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint }) })).devices;
        setStatus("Turned off", "ok");
      } else {
        if ((await Notification.requestPermission()) !== "granted") { renderDevice(); return; }
        registration = registration || await navigator.serviceWorker.register("/notify-sw", { scope: "/" });
        await navigator.serviceWorker.ready;
        subscription = await registration.pushManager.getSubscription()
          || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(data.key) });
        data.devices = (await api("/api/notifications/subscribe", {
          method: "POST", body: JSON.stringify({ subscription: subscription.toJSON(), label: deviceLabel() }),
        })).devices;
        await api("/api/notifications/test", { method: "POST", body: JSON.stringify({ endpoint: subscription.endpoint }) });
        setStatus("Turned on - a test notification is on its way", "ok");
      }
    } catch (error) {
      if (error.message !== "login") setStatus(`Couldn't change notifications (${error.message})`, "error");
    } finally {
      renderDevice();
    }
  });

  testButton.addEventListener("click", async () => {
    try {
      const { sent } = await api("/api/notifications/test", { method: "POST", body: JSON.stringify({ endpoint: subscription?.endpoint }) });
      setStatus(sent ? "Test sent" : "Couldn't reach this device - try turning it off and on", sent ? "ok" : "error");
    } catch (error) { if (error.message !== "login") setStatus(`Test failed (${error.message})`, "error"); }
  });

  showTab(location.hash.slice(1) || "appearance", false);
  // A link to /Settings#<tab> while Settings is already open only changes the hash.
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "appearance", false));
})();
