// Rocsy Settings: Services tab - host supervisors, ComfyUI and containers
// (through the gateway's /api/services -> bunny-hostctl on the host). Start /
// stop / restart and "on boot"; the helper refuses busy or locked items.
(() => {
  const groupsEl = document.getElementById("service-groups");
  const status = document.getElementById("services-status");
  const panel = document.getElementById("panel-services");
  const profileList = document.getElementById("comfy-profile-list");
  const profileStatus = document.getElementById("comfy-profiles-status");
  const profileSave = document.getElementById("comfy-profiles-save");
  const profileReset = document.getElementById("comfy-profiles-reset");
  const gemmSelect = document.getElementById("gemm-tuning");
  const gemmNote = document.getElementById("gemm-tuning-note");
  const gemmNoteBase = gemmNote.textContent;
  const priorityList = document.getElementById("load-priority-list");
  const priorityStatus = document.getElementById("load-priority-status");
  const prioritySave = document.getElementById("load-priority-save");
  let priorityLoaded = false;
  let priorityData = { levels: [], engines: [] };
  let profilesLoaded = false;
  let profileData = [];
  let timer = null;
  let lastKey = "";
  let busyAction = false;
  let statusTimer = null;
  // Windows (WSL2) installs: support is untested, so say so above the service list.
  const platformNote = document.createElement("p");
  platformNote.className = "bb-status";
  platformNote.dataset.kind = "warn";
  platformNote.hidden = true;
  platformNote.style.cssText = "margin:0 0 12px;color:var(--bb-amber)";
  groupsEl.before(platformNote);

  function setStatus(text, kind = "") {
    status.textContent = text;
    status.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { status.textContent = ""; }, 4000);
  }

  function ago(seconds) {
    if (!seconds) return "";
    const s = Math.max(0, Date.now() / 1000 - seconds);
    if (s < 90) return "up just now";
    if (s < 5400) return `up ${Math.round(s / 60)} min`;
    if (s < 172800) return `up ${Math.round(s / 3600)} h`;
    return `up ${Math.round(s / 86400)} days`;
  }

  async function act(item, action, force = false) {
    const verbs = { start: "Start", stop: "Stop", restart: "Restart", "boot-on": "Start at boot", "boot-off": "Don't start at boot" };
    if (!force && (action === "stop" || action === "restart")
        && !confirm(`${verbs[action]} ${item.name}?${action === "stop" ? " It stays off until you start it again." : ""}`)) return false;
    busyAction = true;
    setStatus(`${verbs[action]}: ${item.name}…`);
    try {
      const response = await fetch(`/api/services/${encodeURIComponent(item.id)}/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-BB-Action": "1" }, body: JSON.stringify({ force }),
      });
      if (response.status === 401) { location.href = "/login?next=/Settings%23services"; return false; }
      const data = await response.json().catch(() => ({}));
      if (response.status === 409 && data.can_force && confirm(`${data.error}\n\nDo it anyway?`)) {
        busyAction = false;
        return act(item, action, true);
      }
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setStatus(data.message || "Done", "ok");
      return true;
    } catch (error) {
      setStatus(error.message, "error");
      return false;
    } finally {
      busyAction = false;
      lastKey = "";  // always redraw: the clicked button was disabled
      load();
    }
  }

  function button(item, action, label, extra = "") {
    const allowed = item.actions?.[action];
    const el = document.createElement("button");
    el.type = "button";
    el.className = `bb-button ${extra}`.trim();
    el.textContent = label;
    el.disabled = !allowed?.allowed;
    if (allowed?.reason) el.title = allowed.reason;
    el.addEventListener("click", () => { el.disabled = true; act(item, action); });
    return el;
  }

  function row(item) {
    const el = document.createElement("div");
    el.className = "bb-svc";
    el.dataset.id = item.id;
    const dot = document.createElement("span");
    dot.className = "bb-device-dot";
    dot.dataset.state = item.running ? "on" : item.exists ? "off" : "off";
    if (item.running && /starting|busy|unhealthy/.test(item.state)) dot.dataset.state = "busy";
    if (/error|unreachable|exited \(\d+\)|unhealthy/.test(item.state)) dot.dataset.state = "blocked";
    const text = document.createElement("div");
    text.className = "bb-svc-text";
    const name = document.createElement("span");
    name.className = "bb-svc-name";
    name.textContent = item.name;
    const note = document.createElement("span");
    note.className = "bb-svc-note";
    note.textContent = item.note;
    const state = document.createElement("span");
    state.className = "bb-svc-state";
    state.textContent = [item.state, ago(item.since)].filter(Boolean).join(" · ");
    text.append(name, note, state);

    const controls = document.createElement("div");
    controls.className = "bb-svc-controls";
    if (item.running) {
      controls.append(button(item, "restart", "Restart"), button(item, "stop", "Stop", "bb-button-danger"));
    } else {
      controls.append(button(item, "start", "Start", "bb-button-primary"));
    }
    if (item.boot !== null && item.boot !== undefined) {
      const boot = document.createElement("label");
      boot.className = "bb-svc-boot";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.className = "bb-switch";
      input.setAttribute("role", "switch");
      input.checked = Boolean(item.boot);
      const next = item.boot ? "boot-off" : "boot-on";
      const permission = item.actions?.[next];
      input.disabled = !permission?.allowed;
      if (permission?.reason) { boot.title = permission.reason; boot.classList.add("is-locked"); }
      input.setAttribute("aria-label", `Start ${item.name} at boot`);
      input.addEventListener("change", async () => { if (!(await act(item, next))) input.checked = !input.checked; });
      boot.append(input, "On boot");
      controls.append(boot);
    }
    el.append(dot, text, controls);
    return el;
  }

  function renderProfiles() {
    profileList.replaceChildren(...profileData.map((profile) => {
      const label = document.createElement("label");
      label.className = "bb-profile-field";
      const title = document.createElement("span");
      title.textContent = profile.name + (profile.name === "ltx" ? " (default)" : "");
      const meta = document.createElement("small");
      meta.textContent = `GPU ${profile.gpus}`;
      const input = document.createElement("textarea");
      input.rows = 2;
      input.spellcheck = false;
      input.dataset.profile = profile.name;
      input.value = profile.extra_args;
      label.append(title, meta, input);
      return label;
    }));
  }

  function renderGemm(gemm) {
    if (!gemm) return;
    gemmSelect.value = gemm.mode;
    const pending = gemm.pending_shapes ? `, ${gemm.pending_shapes} waiting for idle time` : "";
    gemmNote.textContent = `${gemmNoteBase} ${gemm.tuned_shapes} shape${gemm.tuned_shapes === 1 ? "" : "s"} tuned${pending}.`;
  }

  async function loadProfiles() {
    if (profilesLoaded) return;
    try {
      const response = await fetch("/api/comfyui/profiles", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      profileData = data.profiles || [];
      profilesLoaded = true;
      renderProfiles();
      renderGemm(data.gemm_tuning);
    } catch (error) {
      profileStatus.textContent = error.message;
      profileStatus.dataset.kind = "error";
    }
  }

  profileReset.addEventListener("click", () => {
    profileData = profileData.map((profile) => ({ ...profile, extra_args: profile.default_extra_args }));
    renderProfiles();
    profileStatus.textContent = "Defaults loaded; save to apply.";
    profileStatus.dataset.kind = "";
  });

  profileSave.addEventListener("click", async () => {
    profileSave.disabled = true;
    profileStatus.textContent = "Saving…";
    try {
      const profiles = Object.fromEntries([...profileList.querySelectorAll("textarea")]
        .map((input) => [input.dataset.profile, input.value.trim()]));
      const response = await fetch("/api/comfyui/profiles", {
        method: "PUT", headers: { "Content-Type": "application/json", "X-BB-Action": "1" },
        body: JSON.stringify({ profiles, gemm_tuning: gemmSelect.value }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
      profileData = data.profiles || [];
      renderProfiles();
      renderGemm(data.gemm_tuning);
      profileStatus.textContent = "Saved";
      profileStatus.dataset.kind = "ok";
    } catch (error) {
      profileStatus.textContent = error.message;
      profileStatus.dataset.kind = "error";
    } finally { profileSave.disabled = false; }
  });

  function renderPriority() {
    priorityList.replaceChildren(...priorityData.engines.map((engine) => {
      const row = document.createElement("div");
      row.className = "bb-priority-row";
      const text = document.createElement("div");
      text.className = "bb-svc-text";
      const name = document.createElement("span");
      name.className = "bb-svc-name";
      name.textContent = engine.name;
      const note = document.createElement("span");
      note.className = "bb-svc-note";
      note.textContent = engine.note;
      text.append(name, note);
      const select = document.createElement("select");
      select.dataset.engine = engine.id;
      select.setAttribute("aria-label", `${engine.name} loading priority`);
      priorityData.levels.forEach((level) => {
        const option = document.createElement("option");
        option.value = level.id;
        option.textContent = `${level.label} · nice ${level.nice}${level.id === engine.default ? " (default)" : ""}`;
        select.append(option);
      });
      select.value = engine.level;
      row.append(text, select);
      return row;
    }));
  }

  async function loadPriority() {
    if (priorityLoaded) return;
    try {
      const response = await fetch("/api/load-priority", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      priorityData = data;
      priorityLoaded = true;
      renderPriority();
    } catch (error) {
      priorityStatus.textContent = error.message;
      priorityStatus.dataset.kind = "error";
    }
  }

  prioritySave.addEventListener("click", async () => {
    prioritySave.disabled = true;
    priorityStatus.textContent = "Saving…";
    priorityStatus.dataset.kind = "";
    try {
      const engines = Object.fromEntries([...priorityList.querySelectorAll("select")]
        .map((select) => [select.dataset.engine, select.value]));
      const response = await fetch("/api/load-priority", {
        method: "PUT", headers: { "Content-Type": "application/json", "X-BB-Action": "1" },
        body: JSON.stringify({ engines }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
      priorityData = data;
      renderPriority();
      priorityStatus.textContent = "Saved · applies on the next load";
      priorityStatus.dataset.kind = "ok";
    } catch (error) {
      priorityStatus.textContent = error.message;
      priorityStatus.dataset.kind = "error";
    } finally { prioritySave.disabled = false; }
  });

  async function load() {
    clearTimeout(timer);
    if (panel.hidden) return;
    if (!busyAction) {
      try {
        const response = await fetch("/api/services", { cache: "no-store" });
        if (response.status === 401) { location.href = "/login?next=/Settings%23services"; return; }
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        platformNote.hidden = !data.platform?.untested;
        platformNote.textContent = data.platform?.untested ? `${data.platform.name} · untested. ${data.platform.note}` : "";
        // Unchanged: keep the rows, so keyboard focus and hover tooltips survive the poll.
        // "since" jitters by microseconds per poll; the minute bucket keeps "up N min" ticking.
        const key = JSON.stringify(data.items, (k, v) => (k === "since" && v ? Math.round(v / 60) : v)) + Math.floor(Date.now() / 60000);
        if (key === lastKey) { timer = setTimeout(load, 5000); return; }
        lastKey = key;
        const groups = {};
        data.items.forEach((item) => { (groups[item.group] ||= []).push(item); });
        const labels = { Host: "Host services", Containers: "Containers" };
        groupsEl.replaceChildren(...Object.entries(groups).map(([group, items]) => {
          const section = document.createElement("section");
          section.className = "bb-section";
          const head = document.createElement("div");
          head.className = "bb-section-head";
          const title = document.createElement("h2");
          title.textContent = labels[group] || group;
          const count = document.createElement("span");
          count.className = "bb-status";
          count.textContent = `${items.filter((item) => item.running).length} of ${items.length} running`;
          head.append(title, count);
          section.append(head, ...items.map(row));
          return section;
        }));
      } catch (error) {
        lastKey = "";
        groupsEl.replaceChildren(Object.assign(document.createElement("p"), { className: "bb-section-note bb-center", textContent: `Couldn't load services: ${error.message}` }));
      }
    }
    timer = setTimeout(load, 5000);
  }

  // Load when the tab is shown (notifications.js switches panels via the hidden attribute).
  new MutationObserver(() => { if (!panel.hidden) { load(); loadProfiles(); loadPriority(); } else clearTimeout(timer); })
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else load(); });
  load();
  if (!panel.hidden) { loadProfiles(); loadPriority(); }
})();
