// Rocsy Settings: pick a site theme + accent. Every change saves at once
// (PUT /api/settings) and previews live by swapping the /theme.css link.
(() => {
  const grid = document.getElementById("theme-grid");
  const status = document.getElementById("save-status");
  const themeLink = document.getElementById("bb-theme");
  const custom = document.getElementById("accent-custom");
  const chips = [...document.querySelectorAll(".bb-accent-chip")];
  let current = { theme: "default", accent: "" };
  let saveTimer = null;
  let statusTimer = null;

  chips.forEach((chip) => { if (chip.dataset.accent) chip.style.background = chip.dataset.accent; });

  function setStatus(text, kind = "") {
    status.textContent = text;
    status.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { status.textContent = ""; }, 2500);
  }

  function preview() {
    const query = new URLSearchParams({ preview: current.theme, accent: current.accent, t: Date.now() });
    themeLink.href = `/theme.css?${query}`;
  }

  function renderSelection() {
    grid.querySelectorAll(".bb-theme-card").forEach((card) => {
      const on = card.dataset.theme === current.theme;
      card.classList.toggle("is-selected", on);
      card.setAttribute("aria-checked", String(on));
      card.tabIndex = on ? 0 : -1;
    });
    const known = chips.some((chip) => chip.dataset.accent === current.accent);
    chips.forEach((chip) => chip.classList.toggle("is-selected", chip.dataset.accent === current.accent));
    custom.parentElement.classList.toggle("is-selected", Boolean(current.accent) && !known);
    if (current.accent) custom.value = current.accent;
  }

  async function save() {
    setStatus("Saving…");
    try {
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(current),
      });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const saved = await response.json();
      current = { theme: saved.theme, accent: saved.accent };
      renderSelection();
      setStatus("Saved", "ok");
    } catch (error) {
      setStatus(`Couldn't save (${error.message})`, "error");
    }
  }

  function change(values, delay = 0) {
    current = { ...current, ...values };
    renderSelection();
    preview();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, delay);
  }

  function themeCard(theme) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "bb-theme-card";
    card.dataset.theme = theme.id;
    card.setAttribute("role", "radio");
    const [bg, panel, accent, green] = theme.swatch;
    const mock = document.createElement("span");
    mock.className = "bb-theme-mock";
    mock.style.background = bg;
    mock.innerHTML = '<span class="m-panel"><span class="m-line"></span><span class="m-line short"></span>'
      + '<span class="m-bar"></span></span><span class="m-dot"></span>';
    mock.querySelector(".m-panel").style.background = panel;
    mock.querySelector(".m-bar").style.background = accent;
    mock.querySelector(".m-dot").style.background = green;
    if (theme.id === "default") mock.classList.add("is-default");
    const name = document.createElement("span");
    name.className = "bb-theme-name";
    name.textContent = theme.name;
    const note = document.createElement("span");
    note.className = "bb-theme-note";
    note.textContent = theme.note;
    card.append(mock, name, note);
    card.addEventListener("click", () => change({ theme: theme.id }));
    return card;
  }

  // Arrow keys move between themes, like a native radio group.
  grid.addEventListener("keydown", (event) => {
    const cards = [...grid.querySelectorAll(".bb-theme-card")];
    const index = cards.findIndex((card) => card.dataset.theme === current.theme);
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step || index < 0) return;
    event.preventDefault();
    const next = cards[(index + step + cards.length) % cards.length];
    change({ theme: next.dataset.theme });
    next.focus();
  });

  chips.forEach((chip) => chip.addEventListener("click", () => change({ accent: chip.dataset.accent })));
  custom.addEventListener("input", () => change({ accent: custom.value }, 400));

  fetch("/api/settings")
    .then((response) => {
      if (response.status === 401) { location.href = "/login?next=/Settings"; throw new Error("login"); }
      return response.json();
    })
    .then((data) => {
      current = { theme: data.theme, accent: data.accent };
      grid.replaceChildren(...data.themes.map(themeCard));
      renderSelection();
    })
    .catch((error) => { if (error.message !== "login") setStatus("Couldn't load settings", "error"); });
})();

// Password settings are independent of appearance and other saved settings.
(() => {
  const form = document.getElementById("password-form");
  if (!form) return;
  const currentField = document.getElementById("current-password-field");
  const current = document.getElementById("current-password");
  const next = document.getElementById("new-password");
  const confirm = document.getElementById("confirm-new-password");
  const description = document.getElementById("password-description");
  const status = document.getElementById("password-status");
  const save = document.getElementById("password-save");
  const setStatus = (message, kind = "") => { status.textContent = message; status.dataset.kind = kind; };
  const render = (passwordSet) => {
    currentField.hidden = !passwordSet;
    current.required = passwordSet;
    description.textContent = passwordSet
      ? "Enter your current password to change it. Other signed-in devices will need to log in again."
      : "No password is set. Anyone who can reach Rocsy can use it. Set one here to require sign-in.";
    save.textContent = passwordSet ? "Change password" : "Set password";
  };
  fetch("/api/password", { cache: "no-store" }).then(async (response) => {
    if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
    if (!response.ok) throw new Error("Could not load password status.");
    render((await response.json()).password_set);
  }).catch((error) => setStatus(error.message, "error"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (next.value !== confirm.value) { setStatus("New passwords do not match.", "error"); return; }
    save.disabled = true;
    setStatus("Saving…");
    try {
      const response = await fetch("/api/password", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: current.value, new_password: next.value }) });
      if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save password.");
      form.reset();
      render(true);
      setStatus("Password saved.", "ok");
    } catch (error) { setStatus(error.message, "error"); }
    finally { save.disabled = false; }
  });
})();

// Generated data cleanup: the gateway exposes only two explicit, mounted roots
// (ComfyUI outputs and the Master Agent workspace).
(() => {
  const panel = document.getElementById("panel-storage");
  if (!panel) return;
  const summary = document.getElementById("storage-summary");
  const status = document.getElementById("storage-status");
  const refresh = document.getElementById("storage-refresh");
  const scope = document.getElementById("storage-scope");
  const estimate = document.getElementById("storage-estimate");
  const includeUploads = document.getElementById("storage-include-uploads");
  const remove = document.getElementById("storage-delete");
  const formatBytes = (value) => {
    if (!value) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let n = value, unit = 0;
    while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit += 1; }
    return `${n >= 100 || unit === 0 ? Math.round(n) : n.toFixed(1)} ${units[unit]}`;
  };
  const setStatus = (text, kind = "") => { status.textContent = text; status.dataset.kind = kind; };
  const renderEstimate = (data) => {
    const item = data?.estimates?.[scope.value];
    if (!item) { estimate.textContent = "Estimate unavailable."; return; }
    estimate.textContent = `${item.files.toLocaleString()} file${item.files === 1 ? "" : "s"} · approximately ${formatBytes(item.bytes)} will be deleted`;
  };
  [...scope.options].forEach((option) => { option.dataset.label = option.textContent; });
  async function load() {
    try {
      const response = await fetch(`/api/storage?include_uploads=${includeUploads.checked ? "1" : "0"}`, { credentials: "same-origin", cache: "no-store" });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      window.__storageEstimates = data.estimates || {};
      [...scope.options].forEach((option) => {
        const item = data.estimates?.[option.value];
        option.textContent = item ? `${option.dataset.label} — ${item.files.toLocaleString()} file${item.files === 1 ? "" : "s"} · ${formatBytes(item.bytes)}` : option.dataset.label;
      });
      renderEstimate(data);
      const fragment = document.createDocumentFragment();
      data.roots.forEach((root) => {
        const row = document.createElement("div");
        row.className = "bb-storage-root";
        row.innerHTML = `<strong></strong><span></span>`;
        row.querySelector("strong").textContent = root.name;
        row.querySelector("span").textContent = `${root.files.toLocaleString()} file${root.files === 1 ? "" : "s"} · ${formatBytes(root.bytes)}`;
        fragment.append(row);
      });
      const total = document.createElement("p");
      total.className = "bb-section-note bb-tight";
      total.textContent = `${data.files.toLocaleString()} total file${data.files === 1 ? "" : "s"} · ${formatBytes(data.bytes)}`;
      fragment.append(total);
      summary.replaceChildren(fragment);
    } catch (error) { summary.innerHTML = "<p class='bb-section-note'>Couldn’t read generated storage.</p>"; setStatus(error.message, "error"); }
  }
  refresh.addEventListener("click", load);
  includeUploads.addEventListener("change", load);
  scope.addEventListener("change", () => { renderEstimate({ estimates: window.__storageEstimates || {} }); });
  remove.addEventListener("click", async () => {
    const selected = scope.options[scope.selectedIndex].dataset.label || scope.options[scope.selectedIndex].textContent;
    if (!window.confirm(`Permanently delete ${selected.toLowerCase()} from ComfyUI outputs and the Master Agent workspace?`)) return;
    remove.disabled = true; setStatus("Deleting…");
    try {
      const response = await fetch("/api/storage/cleanup", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: scope.value, include_uploads: includeUploads.checked }) });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const cards = data.deleted_cards || 0;
      setStatus(`Deleted ${data.deleted.toLocaleString()} file${data.deleted === 1 ? "" : "s"} (${formatBytes(data.deleted_bytes)})${cards ? ` and removed ${cards} job card${cards === 1 ? "" : "s"}` : ""}.`, "ok");
      load();
    } catch (error) { setStatus(`Couldn’t delete files (${error.message})`, "error"); }
    finally { remove.disabled = false; }
  });
  load();
})();

// Agentic logs: Master Agent chats + Image/Music/Voice Agentic sessions,
// deleted through each service's API (busy sessions are skipped server-side).
(() => {
  const list = document.getElementById("agentic-logs-sources");
  if (!list) return;
  const status = document.getElementById("agentic-logs-status");
  const scope = document.getElementById("agentic-logs-scope");
  const estimate = document.getElementById("agentic-logs-estimate");
  const remove = document.getElementById("agentic-logs-delete");
  let data = { sources: [] };
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const setStatus = (text, kind = "") => { status.textContent = text; status.dataset.kind = kind; };
  const checked = () => [...list.querySelectorAll("input[type=checkbox]:checked")].map((box) => box.value);
  const renderEstimate = () => {
    const ids = new Set(checked());
    const count = data.sources.filter((src) => src.available && ids.has(src.id)).reduce((sum, src) => sum + (src.estimates?.[scope.value] || 0), 0);
    estimate.textContent = ids.size ? `${plural(count, "session log")} will be deleted` : "Choose at least one source.";
    remove.disabled = !ids.size || !count;
  };
  const render = (next) => {
    const previous = list.querySelector("input") ? new Set(checked()) : null;
    data = next;
    const fragment = document.createDocumentFragment();
    data.sources.forEach((src) => {
      const row = document.createElement("label");
      row.className = "bb-storage-root";
      row.innerHTML = `<b class="bb-log-pick"><input type="checkbox"> <strong></strong></b><span></span>`;
      const box = row.querySelector("input");
      box.value = src.id;
      box.disabled = !src.available;
      box.checked = src.available && (previous ? previous.has(src.id) : true);
      row.querySelector("strong").textContent = src.name;
      row.querySelector("span").textContent = src.available
        ? `${plural(src.sessions, "session")}${src.busy ? ` · ${src.busy} running` : ""}`
        : "Service not reachable";
      fragment.append(row);
    });
    list.replaceChildren(fragment);
    renderEstimate();
  };
  async function load() {
    try {
      const response = await fetch("/api/storage/agentic", { credentials: "same-origin", cache: "no-store" });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) { list.innerHTML = "<p class='bb-section-note'>Couldn’t read agentic logs.</p>"; setStatus(error.message, "error"); }
  }
  list.addEventListener("change", renderEstimate);
  scope.addEventListener("change", renderEstimate);
  document.getElementById("storage-refresh")?.addEventListener("click", load);
  remove.addEventListener("click", async () => {
    const label = scope.options[scope.selectedIndex].textContent.toLowerCase();
    if (!window.confirm(`Permanently delete agentic logs (${label})? This can’t be undone.`)) return;
    remove.disabled = true; setStatus("Deleting…");
    try {
      const response = await fetch("/api/storage/agentic/cleanup", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: scope.value, sources: checked() }) });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      const extra = [result.skipped_busy ? `${result.skipped_busy} running skipped` : "", result.errors ? `${result.errors} failed` : ""].filter(Boolean).join(", ");
      setStatus(`Deleted ${plural(result.deleted, "session log")}${extra ? ` (${extra})` : ""}.`, result.errors ? "error" : "ok");
      render(result);
    } catch (error) { setStatus(`Couldn’t delete logs (${error.message})`, "error"); renderEstimate(); }
  });
  load();
})();


// Uninstall: confirm phrase + password, keep or delete the install folder.
(() => {
  const open = document.getElementById("uninstall-open");
  const dialog = document.getElementById("uninstall-dialog");
  if (!open || !dialog) return;
  const form = document.getElementById("uninstall-form");
  const phrase = document.getElementById("uninstall-phrase");
  const password = document.getElementById("uninstall-password");
  const confirm = document.getElementById("uninstall-confirm");
  const status = document.getElementById("uninstall-dialog-status");
  const locationNote = document.getElementById("uninstall-location");
  const effect = document.getElementById("uninstall-effect");
  const choices = [...form.querySelectorAll('[name="uninstall-files"]')];
  let root = "";
  let busy = false;
  let passwordRequired = true;
  const selected = () => choices.find(choice => choice.checked)?.value || "keep";
  const update = () => {
    confirm.disabled = busy || phrase.value !== "UNINSTALL ROCSY" || (passwordRequired && !password.value);
    effect.textContent = selected() === "delete"
      ? `This permanently deletes ${root}, including its settings and files. Docker volumes for Rocsy are removed. Files outside this folder remain.`
      : `Rocsy services are removed. ${root} and its saved data remain on disk.`;
  };
  Promise.resolve(fetch("/api/uninstall/status", { credentials: "same-origin", cache: "no-store" }))
    .then(async response => {
      if (response.status === 401) { location.href = "/login?next=/Settings%23uninstall"; return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      root = data.root;
      passwordRequired = data.password_required !== false;
      password.required = passwordRequired;
      password.closest("label").hidden = !passwordRequired;
      locationNote.textContent = data.available ? `Installation folder: ${root}` : data.reason;
      if (data.last_run?.phase === "failed")
        locationNote.textContent += ` · The last uninstall failed: ${data.last_run.error || "see its journal"}. Nothing was removed after that step; you can try again.`;
      open.disabled = !data.available;
      const deleteChoice = choices.find(choice => choice.value === "delete");
      deleteChoice.disabled = !data.can_delete_files;
      if (!data.can_delete_files && data.available) {
        deleteChoice.parentElement.title = "This folder contained files before Rocsy was installed.";
      }
      document.getElementById("uninstall-dialog-location").textContent = root;
      update();
    }).catch(error => { locationNote.textContent = `Could not check installation: ${error.message}`; });
  open.addEventListener("click", () => {
    form.reset(); status.textContent = ""; busy = false; update(); dialog.showModal(); phrase.focus();
  });
  document.getElementById("uninstall-cancel").addEventListener("click", () => { if (!busy) dialog.close(); });
  form.addEventListener("input", update);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (confirm.disabled || busy) return;
    busy = true; update(); status.textContent = "Starting uninstall…";
    try {
      const response = await fetch("/api/uninstall/start", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: phrase.value, password: password.value, delete_files: selected() === "delete" })
      });
      password.value = "";
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      status.textContent = data.message || "Uninstall started. Rocsy will go offline shortly.";
      open.disabled = true;
      form.querySelectorAll("input").forEach(input => { input.disabled = true; });
      document.getElementById("uninstall-cancel").disabled = true;
    } catch (error) { password.value = ""; status.textContent = error.message; busy = false; update(); }
  });
})();

// Settings -> Modules: replay the home page tour.
(() => {
  const firstRun = document.getElementById("start-first-run");
  if (firstRun) firstRun.addEventListener("click", () => {
    try { localStorage.removeItem("rocsy-onboarding-complete"); } catch (_) {}
    location.href = "/?onboarding=1";
  });
})();

// Settings -> Appearance -> Mobile data (site setting; static/js/data-saver.js
// decides per device and reports what it sees here).
(() => {
  const select = document.getElementById("datasaver-mode");
  const status = document.getElementById("datasaver-status");
  const device = document.getElementById("datasaver-device");
  if (!select) return;
  const setStatus = (text, kind = "") => { status.textContent = text; status.dataset.kind = kind; };

  function describe() {
    const saver = window.RocsyDataSaver;
    if (!saver) { device.textContent = ""; return; }
    // This page runs the mode it loaded with; a new pick applies on the next page load.
    const mode = select.value;
    let text = mode === saver.mode ? `This device: data saver is ${saver.active ? "on" : "off"} (${saver.reason}).`
      : "Reload to apply the new setting on this device.";
    if (saver.phone && !navigator.connection && mode === "auto") {
      text += " This browser can't tell Wi-Fi from mobile data, so Auto won't switch on here. Pick “Always on phones” to save data on it.";
    }
    device.textContent = text;
  }

  fetch("/api/settings", { cache: "no-store", credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : null))
    .then((saved) => { if (saved) { select.value = saved.data_saver || "auto"; select.disabled = false; describe(); } })
    .catch(() => {});
  document.addEventListener("rocsy:datasaver", describe);

  select.addEventListener("change", async () => {
    const mode = select.value;
    select.disabled = true;
    setStatus("Saving…");
    try {
      const response = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ data_saver: mode }),
      });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      select.value = (await response.json()).data_saver;
      setStatus("Saved. Applies as pages load.", "ok");
    } catch (error) {
      setStatus(`Couldn't save (${error.message})`, "error");
    } finally {
      select.disabled = false;
      describe();
    }
  });
})();

// Settings -> Modules -> Trainer card pictures: every dataset is shown unless
// unticked; the excluded names are a site setting the home snapshot reads.
(() => {
  const list = document.getElementById("trainer-art-list");
  const status = document.getElementById("trainer-art-status");
  const empty = document.getElementById("trainer-art-empty");
  if (!list) return;
  const setStatus = (text, kind = "") => { status.textContent = text; status.dataset.kind = kind; };
  let excluded = new Set();

  function render(names) {
    // Excluded datasets that no longer exist stay listed so they can be ticked again.
    const all = [...new Set([...names, ...excluded])].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    empty.hidden = all.length > 0;
    if (!all.length) empty.textContent = "No datasets yet.";
    list.querySelectorAll("label").forEach((node) => node.remove());
    for (const name of all) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = name;
      box.checked = !excluded.has(name);
      label.append(box, ` ${name}`);
      list.append(label);
    }
  }

  Promise.all([
    fetch("/api/settings", { cache: "no-store", credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)),
    fetch("/Trainer/api/datasets", { cache: "no-store", credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]).then(([saved, trainer]) => {
    if (!saved) return;
    excluded = new Set(saved.trainer_art_exclude || []);
    if (!trainer) { empty.textContent = "Trainer is offline, so its datasets can't be listed right now."; }
    render((trainer?.datasets || []).map((item) => item.name));
    list.disabled = false;
  }).catch(() => { empty.textContent = "Couldn't load the dataset list."; });

  list.addEventListener("change", async (event) => {
    const box = event.target;
    if (box.type !== "checkbox") return;
    const next = new Set(excluded);
    if (box.checked) next.delete(box.value); else next.add(box.value);
    list.disabled = true;
    setStatus("Saving…");
    try {
      const response = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trainer_art_exclude: [...next] }),
      });
      if (response.status === 401) { location.href = "/login?next=/Settings"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      excluded = new Set((await response.json()).trainer_art_exclude);
      setStatus("Saved. The home page uses it on its next refresh.", "ok");
    } catch (error) {
      box.checked = !box.checked;
      setStatus(`Couldn't save (${error.message})`, "error");
    } finally {
      list.disabled = false;
    }
  });
})();
